"""自动传单派发 —— Linux 云端服务器端入口（多用户多租户）。

每个用户一个 UserEngine：
- 一个 NapCat 容器（napcat-<user>）绑定该用户自己的 QQ 号，反向 WS 连独立端口
  （ws_port 直接取自 users.json，建用户时由 webapp 按 ws_base_port + idx 分配写入），
  事件进来计数、触发后同一连接 call_action 直发传单
- 用户业务配置/状态/发送记录/图片库全部隔离在 users/<username>/
- 发送确认：API 返回 message_id 即成功，message_sent 事件软确认
- 群级 enabled 开关、自动检测非目标群（get_group_info 查群名）

users_watch_loop 每 10 秒对比 users.json，动态启动/停止用户引擎。

调试：python server.py --simulate <用户名> <群号>
  跳过计数条件，直接走完整「触发→发送→确认」链路，用于联调。
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from onebot_server import OneBotServer

import container_mgr
from qzone_client import QzoneClient
from keyword_filter import KeywordFilter
from comment_gen import CommentGenerator

BASE_DIR = Path(__file__).resolve().parent
SYSTEM_CONFIG_FILE = BASE_DIR / "config.json"
USERS_FILE = BASE_DIR / "users.json"
PENDING_TRIGGER_NAME = "pending_trigger.json"

logger = logging.getLogger("flyer")

DETECT_QUERY_INTERVAL = 1800    # 群名查询失败后的重试间隔（秒）
MAX_DISCONNECTED_TIME = 90      # NapCat 断连超过此秒数自动重启容器

QZONE_COOKIES_TTL = 600         # Qzone Cookies 缓存时长（秒），过期后从 NapCat 重新获取
QZONE_PENDING_TEST_NAME = "pending_qzone_test.json"
QZONE_TEST_RESULT_NAME = "qzone_test_result.json"
QZONE_PENDING_EXECUTE_NAME = "pending_qzone_execute.json"
QZONE_COOKIES_FILE = "qzone_cookies.json"   # 扫码登录持久化的完整 Qzone Cookies
QZONE_COOKIES_FILE_TTL = 12 * 3600          # 扫码 cookies 有效期（12 小时，过期需重新扫码）


# ─────────────────────────── 系统配置与账号库 ───────────────────────────

def load_system_config() -> dict:
    """加载系统配置（web/logging/docker 段）。"""
    cfg_path = SYSTEM_CONFIG_FILE
    if not cfg_path.exists():
        sys.exit(f"配置不存在：{cfg_path}")
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"配置解析失败：{e}")
    return cfg


def load_users() -> dict:
    """加载账号库（users.json）。"""
    if not USERS_FILE.exists():
        sys.exit(f"账号库不存在：{USERS_FILE}")
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"users.json 解析失败：{e}")


# ─────────────────────────── 日志 ───────────────────────────

# 模块级日志配置：setup_logging 写入，start_engine 为运行时新增用户补 handler
_LOG_DIR: Path = BASE_DIR / "logs"
_LOG_FILE_LEVEL: int = logging.DEBUG
_LOG_FMT: logging.Formatter | None = None


def setup_logging(cfg: dict) -> None:
    global _LOG_DIR, _LOG_FILE_LEVEL, _LOG_FMT
    _LOG_DIR = BASE_DIR / cfg.get("logging", {}).get("log_dir", "logs")
    _LOG_DIR.mkdir(exist_ok=True)
    console_level = cfg.get("logging", {}).get("console_level", "INFO")
    _LOG_FILE_LEVEL = getattr(
        logging,
        str(cfg.get("logging", {}).get("file_level", "DEBUG")).upper(),
        logging.DEBUG)

    _LOG_FMT = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                 datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    console.setFormatter(_LOG_FMT)
    root.addHandler(console)

    # 每用户一个文件日志（server_<user>_YYYYMMDD.log）
    for username in load_users():
        _ensure_user_file_handler(username)

    # websockets 库日志调为 WARNING，避免刷屏
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _ensure_user_file_handler(username: str) -> None:
    """为 flyer.<username> 补文件日志 handler（幂等，重复调用不叠加）。

    setup_logging 启动时为现有用户调用；webapp 建用户后 users_watch_loop
    动态启动的新引擎也会调用（否则新用户日志只进控制台不进文件）。
    """
    u_logger = logging.getLogger(f"flyer.{username}")
    for h in u_logger.handlers:
        if isinstance(h, RotatingFileHandler) and f"server_{username}_" in str(h.baseFilename):
            return                      # 已存在同名文件 handler，跳过
    if _LOG_FMT is None:
        return
    handler = RotatingFileHandler(
        _LOG_DIR / f"server_{username}_{time.strftime('%Y%m%d')}.log",
        maxBytes=5 * 1024 * 1024, backupCount=7, encoding="utf-8")
    handler.setLevel(_LOG_FILE_LEVEL)
    handler.setFormatter(_LOG_FMT)
    u_logger.addHandler(handler)


# ─────────────────────────── 单用户引擎 ───────────────────────────

@dataclass
class GroupState:
    """每个目标群的运行状态。"""
    message_count: int = 0          # 当前累计消息数
    last_send_time: float = 0.0     # 上次成功发送的时间戳（Unix 秒）
    sending: bool = False           # 从触发到确认完成全程锁定
    last_error_time: float = 0.0    # 最近一次失败时间（用于退避）
    last_main_msg_time: float = 0.0  # 最近一次本账号在该群发消息的时间（事件确认用）


class UserEngine:
    """单个用户的完整发送引擎：配置/状态/计数/触发/发送/记录。"""

    def __init__(self, username: str, user_meta: dict) -> None:
        self.username = username
        self.user_meta = user_meta                     # users.json 中的账号信息
        self.user_dir = BASE_DIR / "users" / username
        self.config_file = self.user_dir / "config.json"
        self.state_file = self.user_dir / "state.json"
        self.status_file = self.user_dir / "status.json"
        self.trigger_file = self.user_dir / PENDING_TRIGGER_NAME
        self.qzone_test_file = self.user_dir / QZONE_PENDING_TEST_NAME
        self.qzone_test_result_file = self.user_dir / QZONE_TEST_RESULT_NAME
        self.qzone_execute_file = self.user_dir / QZONE_PENDING_EXECUTE_NAME
        self.logger = logging.getLogger(f"flyer.{username}")
        self.cfg = self.load_config()                  # 用户业务配置
        self.states: dict[int, GroupState] = {}
        self.history: deque[dict] = deque(maxlen=200)  # 发送历史
        self.detected: dict[int, dict] = {}            # 自动检测的非目标群
        self.started_at = time.time()
        self.onebot: OneBotServer | None = None
        self.tasks: list[asyncio.Task] = []
        self._disconnected_since: float | None = None  # None=已连接, 数值=断连时间戳
        self._qq_online: bool = True                   # QQ 真实登录状态（区别于 WebSocket 连接）
        self._health_check_failures: int = 0            # 连续健康检查失败次数

        # ── 空间监控（qzone）状态 ──
        self._qzone_client: QzoneClient | None = None  # 当前 Qzone 客户端（含 cookies）
        self._qzone_cookies: str = ""                  # 最近一次获取的 Qzone Cookies
        self._qzone_cookies_fetched_at: float = 0.0    # cookies 获取时间（TTL 缓存）
        self._qzone_dedup: set[str] = set()            # 已处理说说 tid（去重）
        self._qzone_dedup_dirty: bool = False          # 去重集有变更待持久化
        self._qzone_nicknames: dict[str, str] = {}     # 监控目标 uin -> 昵称缓存
        self.qzone_pending: deque[dict] = deque(maxlen=100)  # 待执行动态（手动模式）
        self.qzone_state: dict = {
            "last_check_time": None,
            "total_checked": 0,
            "total_matched": 0,
            "total_forwarded": 0,
            "total_liked": 0,
            "total_commented": 0,
            "error": "",
            "history": deque(maxlen=50),
        }
        self._load_qzone_dedup()

    # ── 配置与状态 ──

    def load_config(self) -> dict:
        """加载并校验用户业务配置，缺失/非法字段直接报错退出。"""
        cfg_path = self.config_file
        if not cfg_path.exists():
            sys.exit(f"用户配置不存在：{cfg_path}")
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            sys.exit(f"配置解析失败：{e}")

        def need(key: str):
            if key not in cfg:
                sys.exit(f"配置缺少必填项：{key}")

        need("global")
        need("groups")
        # groups 允许为空（新用户尚未添加群，引擎照常启动，检测到的群可一键添加）
        groups = {}
        for gid_str, overrides in cfg["groups"].items():
            groups[int(gid_str)] = overrides or {}
        for key in ("message_threshold", "send_interval"):
            if not isinstance(cfg["global"].get(key), int) or cfg["global"].get(key) <= 0:
                sys.exit(f"配置错误：global.{key} 必须为正整数")
        return {**cfg, "groups": groups}

    def load_state(self) -> None:
        """从 state.json 恢复状态。"""
        for gid in self.cfg["groups"]:
            self.states[gid] = GroupState()
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.logger.warning("state.json 读取失败，按全新状态启动")
            return
        for gid, st in data.items():
            gid = int(gid)
            if gid in self.states:
                self.states[gid].message_count = int(st.get("message_count", 0))
                self.states[gid].last_send_time = float(st.get("last_send_time", 0.0))
        self.logger.info("已恢复状态：%s",
                         {gid: {"count": s.message_count,
                                "last_send": time.strftime(
                                    "%m-%d %H:%M:%S",
                                    time.localtime(s.last_send_time))}
                          for gid, s in self.states.items()})

    def save_state(self) -> None:
        data = {str(gid): {"message_count": s.message_count,
                           "last_send_time": s.last_send_time}
                for gid, s in self.states.items()}
        try:
            self.state_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            self.logger.error("状态保存失败：%s", e)

    def resolve_group_config(self, gid: int) -> dict:
        """合并全局与单群配置：单群覆盖全局。"""
        merged = dict(self.cfg["global"])
        merged.update(self.cfg["groups"].get(gid, {}))
        return merged

    def record_history(self, gid: int, success: bool, **extra) -> None:
        """记录一次发送历史（成功/失败），管理台「发送记录」展示。"""
        self.history.append({"time": time.time(), "group_id": gid,
                             "success": success, **extra})

    # ── 事件处理 ──

    async def _query_group_name(self, gid: int) -> None:
        """通过 API 自动查询群名（get_group_info）。"""
        try:
            resp = await self.onebot.call_action(
                "get_group_info", {"group_id": gid},
                timeout=self.cfg.get("api_timeout", 30))
            if resp.get("retcode") == 0:
                name = (resp.get("data") or {}).get("group_name", "")
                if name:
                    self.detected.setdefault(gid, {})["name"] = name
                    self.logger.info("已自动检测群名：群 %s -> %s", gid, name)
        except Exception:
            self.logger.debug("群 %s 群名查询失败（非群成员或超时）", gid)

    async def handle_group_message(self, payload: dict) -> None:
        """群消息事件处理：排除本账号 → 计数 → 触发判断。"""
        gid = int(payload["group_id"])
        if gid not in self.states:
            # 自动检测非目标群：记录出现时间，并异步查询群名（管理台可一键添加）
            now = time.time()
            d = self.detected.setdefault(gid, {"name": "", "first_seen": now,
                                               "last_seen": now, "queried_ts": 0})
            d["last_seen"] = now
            if not d.get("name") and now - d.get("queried_ts", 0) >= DETECT_QUERY_INTERVAL:
                d["queried_ts"] = now
                asyncio.create_task(self._query_group_name(gid))
            return
        # 群发送开关：enabled=false 的群完全忽略（不计数不触发）
        if not self.resolve_group_config(gid).get("enabled", True):
            return
        user_id = payload.get("user_id")
        self_id = payload.get("self_id")

        # 本账号自己发消息推送的 message_sent 事件视为发送确认信号
        if payload.get("post_type") == "message_sent":
            self.states[gid].last_main_msg_time = time.time()
            self.logger.info("群 %s 检测到本账号发送消息事件（message_sent），"
                             "已记录确认时间戳", gid)
            return
        # 排除本账号自己的消息（不计入计数，记录确认时间戳）
        if user_id == self_id and not self.cfg["global"].get("count_own_messages", False):
            return

        state = self.states[gid]
        group_cfg = self.resolve_group_config(gid)
        state.message_count += 1
        now = time.time()
        elapsed = now - state.last_send_time
        threshold = group_cfg["message_threshold"]
        interval = group_cfg["send_interval"]
        self.logger.info("群 %s 新消息，计数 %d/%d，距上次发送 %.0f 秒",
                         gid, state.message_count, threshold, elapsed)

        # 触发条件：计数≥阈值 且 间隔已过 且 不在发送中 且 已过失败退避期
        if (state.message_count >= threshold
                and elapsed >= interval
                and not state.sending
                and now - state.last_error_time >= self.cfg.get("error_cooldown", 300)):
            # 全局在途检查：同一时刻只处理一个群的发送
            if any(s.sending for s in self.states.values()):
                self.logger.info("其他群正在发送中，群 %s 跳过本轮"
                                 "（保留计数，下一条新消息重试）", gid)
                return
            self.logger.info("🎯 群 %s 满足触发条件（计数≥%d 且间隔≥%d 秒），下发发送指令",
                             gid, threshold, interval)
            await self.try_send_flyer(gid, state)

    def build_flyer_message(self, gid: int) -> list[dict]:
        """构造 OneBot 消息段数组（文字 + 图片）。

        群级 template/image 覆盖全局；default_image 为空时从图片清单随机选一张。
        图片路径为 NapCat 容器内的绝对路径（image_root 基准）。
        """
        gc = self.resolve_group_config(gid)
        template = gc.get("template") or self.cfg.get("message", {}).get("template", "")
        image_name = gc.get("image") or self.cfg.get("message", {}).get("default_image")
        if not image_name:
            images = self.cfg.get("message", {}).get("images", [])
            if images:
                image_name = random.choice(images)
        segments = []
        if template:
            segments.append({"type": "text", "data": {"text": template}})
        if image_name:
            root = self.cfg.get("message", {}).get("image_root", "/app/images")
            segments.append({"type": "image",
                             "data": {"file": f"{root.rstrip('/')}/{Path(image_name).name}"}})
        return segments

    async def try_send_flyer(self, gid: int, state: GroupState) -> None:
        """同一连接 call_action(send_group_msg) → API 返回 message_id 即成功。"""
        state.sending = True                         # 上锁：单事件循环中检查→置锁原子
        try:
            if self.onebot is None or not self.onebot.connected:
                raise RuntimeError("主号 NapCat 未连接")
            if not self._qq_online:
                raise RuntimeError("QQ 账号已离线（可能被踢下线）")
            message = self.build_flyer_message(gid)
            if not message:
                raise RuntimeError(f"群 {gid} 没有可发送内容（模板与图片均为空）")
            sent_at = state.last_main_msg_time       # 调用前取基准（事件确认用）
            resp = await self.onebot.call_action(
                "send_group_msg", {"group_id": gid, "message": message},
                timeout=self.cfg.get("api_timeout", 30))
            if resp.get("retcode") == 0:
                msg_id = (resp.get("data") or {}).get("message_id", "未知")
                state.message_count = 0
                state.last_send_time = time.time()
                self.save_state()
                self.logger.info("✅ 群 %s 传单发送成功（API 返回 message_id=%s），"
                                 "计数已重置为 0", gid, msg_id)
                content = next((s["data"].get("text", "") for s in message
                                if s["type"] == "text"), "")
                self.record_history(gid, True, message_id=msg_id,
                                    content=content[:100] or None)
                await self.confirm_main_msg(gid, state,
                                            self.cfg.get("confirm_timeout", 15),
                                            sent_at=sent_at)
            else:
                raise RuntimeError(
                    f"send_group_msg 失败：retcode={resp.get('retcode')}，"
                    f"{resp.get('wording') or resp.get('message') or '未知错误'}")
        except asyncio.TimeoutError:
            state.last_error_time = time.time()
            self.logger.error("❌ 群 %s 等待发送回报超时，保留计数待重试", gid)
            self.record_history(gid, False, error="等待发送回报超时")
            # 超时可能是 QQ 离线导致，触发健康检查
            if self._qq_online:
                self._qq_online = False
                self.logger.warning("⚠️ 发送超时，疑似 QQ 已离线，标记为离线状态")
        except Exception as e:
            state.last_error_time = time.time()
            self.logger.error("❌ 群 %s 发送失败：%s，保留计数"
                              "（退避 %d 秒后由新消息重试）",
                              gid, e, self.cfg.get("error_cooldown", 300))
            self.record_history(gid, False, error=str(e)[:200])
            # retcode=1200 通常是 QQ 离线导致
            err_str = str(e)
            if "retcode=1200" in err_str and self._qq_online:
                self._qq_online = False
                self.logger.warning("⚠️ 发送失败（retcode=1200），疑似 QQ 已离线，标记为离线状态")
        finally:
            state.sending = False                    # 解锁

    async def confirm_main_msg(self, gid: int, state: GroupState, timeout: float,
                               sent_at: float | None = None) -> None:
        """发送后的事件流软确认：确认到→记日志；超时→仅告警，不误判失败。"""
        if sent_at is None:
            sent_at = state.last_main_msg_time
        for _ in range(int(timeout * 2)):            # 0.5 秒轮询一次
            if state.last_main_msg_time > sent_at:
                self.logger.info("✅ 群 %s 发送已获事件流确认（message_sent）", gid)
                return
            await asyncio.sleep(0.5)
        self.logger.warning("⚠️ 群 %s %d 秒内未收到 message_sent 事件确认"
                            "（以 API 返回 message_id 为准）", gid, int(timeout))

    # ── OneBot 服务 ──

    def run_forever(self) -> OneBotServer:
        """构造该用户的 OneBot 服务实例（端口/token 来自 users.json）。"""
        oc = self.cfg.get("onebot", {})
        srv = OneBotServer(
            host=oc.get("host", "0.0.0.0"),
            port=int(self.user_meta["ws_port"]),
            path=oc.get("path", "/onebot/v11/ws"),
            access_token=self.user_meta.get("ws_access_token", ""),
            on_connect=self._on_ws_connect,
            on_disconnect=self._on_ws_disconnect,
        )
        srv.on_event = self.handle_group_message
        self.onebot = srv
        return srv

    @property
    def qq_online(self) -> bool:
        """QQ 账号是否真实在线（WebSocket 已连 + 健康检查通过）。"""
        ws_ok = self.onebot.connected if self.onebot else False
        return ws_ok and self._qq_online

    def _on_ws_connect(self) -> None:
        """NapCat 连接建立回调。"""
        self._disconnected_since = None
        self._qq_online = True  # 连接建立时假定 QQ 在线，等待健康检查确认
        self._health_check_failures = 0
        self.logger.info("🔄 NapCat 已连接，即将执行 QQ 健康检查...")
        # 连接建立后立即触发一次健康检查
        asyncio.create_task(self._delayed_health_check(), name=f"hcheck-onconnect-{self.username}")

    async def _delayed_health_check(self) -> None:
        """连接建立后延迟 3 秒执行一次健康检查。"""
        await asyncio.sleep(3)
        online = await self._check_qq_online()
        if not online:
            self._qq_online = False
            self._health_check_failures = 1
            self.logger.warning("⚠️ NapCat 已连接但 QQ 账号离线（可能被踢），"
                                "将在下一次健康检查后自动重启容器")

    def _on_ws_disconnect(self) -> None:
        """NapCat 连接断开回调。"""
        self._disconnected_since = time.time()
        self._qq_online = False
        self.logger.warning("🔄 NapCat 连接断开，开始计时自动重连...")

    async def _check_qq_online(self) -> bool:
        """通过调用 get_status 检查 QQ 账号是否真实在线。

        与 get_login_info（本地缓存）不同，get_status 反映的是 NapCat
        与 QQ 服务器的真实连接状态。当账号被踢下线时，status.online 应为 false。

        兜底：如果 get_status 没有 online 字段（兼容旧版 NapCat），
        则尝试 get_group_info 查询第一个目标群，API 失败说明 QQ 离线。
        """
        if self.onebot is None or not self.onebot.connected:
            return False
        try:
            resp = await self.onebot.call_action(
                "get_status", {}, timeout=10.0)
            if resp.get("retcode") == 0:
                data = resp.get("data") or {}
                # 标准 OneBot v11：online 字段表示 QQ 是否在线
                online = data.get("online")
                if online is not None:
                    return bool(online)
                # 兼容：NapCat 可能用其他字段
                good = data.get("good")
                if good is not None:
                    return bool(good)
            # retcode != 0 或缺少状态字段，降级检查
            self.logger.debug("get_status 未返回 online 字段，降级检查")
        except (asyncio.TimeoutError, RuntimeError) as e:
            self.logger.warning("QQ 健康检查（get_status）超时/异常：%s", e)
            # 超时说明 NapCat 可能卡住，标记为离线

        # 兜底：查询第一个目标群是否存在（需要 QQ 服务器交互）
        try:
            target_gids = [gid for gid in self.states
                           if self.resolve_group_config(gid).get("enabled", True)]
            if target_gids:
                gid = target_gids[0]
                resp = await self.onebot.call_action(
                    "get_group_info", {"group_id": gid}, timeout=10.0)
                if resp.get("retcode") == 0 and resp.get("data"):
                    return True
                self.logger.debug("get_group_info 返回异常：retcode=%s",
                                  resp.get("retcode"))
                return False
        except (asyncio.TimeoutError, RuntimeError) as e:
            self.logger.warning("QQ 健康检查（get_group_info）超时/异常：%s", e)
            return False

        # 没有目标群时无法兜底，先假设在线
        return True

    async def health_check_loop(self) -> None:
        """每 30 秒检测 QQ 真实在线状态，离线时自动重启 NapCat 容器。

        QQ 被踢下线后 NapCat 的 WebSocket 连接可能仍保持，但
        get_login_info API 会返回错误，据此可准确判断 QQ 离线。
        """
        while True:
            await asyncio.sleep(30)
            if self.onebot is None or not self.onebot.connected:
                # WebSocket 已断连，auto_reconnect_loop 会处理
                self._qq_online = False
                continue
            online = await self._check_qq_online()
            if online:
                if not self._qq_online:
                    self.logger.info("✅ QQ 已恢复在线")
                self._qq_online = True
                self._health_check_failures = 0
                self.logger.debug("QQ 健康检查通过（_check_qq_online=True）")
                continue
            # QQ 离线（被踢下线）
            self._health_check_failures += 1
            if self._qq_online:
                # 首次检测到离线
                self._qq_online = False
                self.logger.warning("⚠️ QQ 账号已离线（可能被踢下线），"
                                    "标记为离线状态等待自动恢复（第 %d 次检测失败）",
                                    self._health_check_failures)
            else:
                self.logger.warning("⚠️ QQ 仍处于离线状态（第 %d 次检测失败），"
                                    "等待自动恢复...", self._health_check_failures)
            # 连续 2 次检测离线（约 60 秒）后自动重启 NapCat 容器
            if self._health_check_failures >= 2:
                self.logger.warning("⚠️ QQ 离线已持续 %d 次检测（约 %d 秒），自动重启容器...",
                                    self._health_check_failures,
                                    self._health_check_failures * 30)
                try:
                    container_mgr.restore_qq_session(self.username)
                    container_mgr.restart_container(self.username)
                    self._health_check_failures = 0
                    self.logger.info("✅ NapCat 容器已重启，等待 QQ 重新登录...")
                except container_mgr.ContainerError as e:
                    self.logger.error("❌ NapCat 容器重启失败：%s", e)

    # ── Web 管理协作任务 ──

    def write_status(self) -> None:
        """dump 实时状态到 status.json（Web 管理端读取）。"""
        data = {
            "timestamp": time.time(),
            "started_at": self.started_at,
            "sender_online": self.qq_online,
            "napcat_online": self.onebot.connected if self.onebot else False,
            "qq_online": self._qq_online,
            "groups": {},
            "history": list(self.history)[-100:],
            "detected_groups": {str(gid): d for gid, d in self.detected.items()},
        }
        for gid, st in self.states.items():
            gc = self.resolve_group_config(gid)
            data["groups"][str(gid)] = {
                "name": self.cfg["groups"].get(gid, {}).get("name", ""),
                "enabled": gc.get("enabled", True),
                "message_count": st.message_count,
                "threshold": gc["message_threshold"],
                "send_interval": gc["send_interval"],
                "last_send_time": st.last_send_time,
                "sending": st.sending,
                "last_error_time": st.last_error_time,
            }
        qz = self.cfg.get("qzone") or {}
        data["qzone"] = {
            "enabled": bool(qz.get("enabled")),
            "manual_mode": bool(qz.get("manual_mode", False)),
            "monitor_count": len(qz.get("monitor_list") or []),
            "cookies_ok": bool(self._qzone_client),
            "error": self.qzone_state.get("error", ""),
            "last_check_time": self.qzone_state.get("last_check_time"),
            "total_checked": self.qzone_state.get("total_checked", 0),
            "total_matched": self.qzone_state.get("total_matched", 0),
            "total_forwarded": self.qzone_state.get("total_forwarded", 0),
            "total_liked": self.qzone_state.get("total_liked", 0),
            "total_commented": self.qzone_state.get("total_commented", 0),
            "history": list(self.qzone_state.get("history", deque()))[-20:],
            "pending": [
                {"uin": p.get("uin"), "tid": str(p.get("tid", "")),
                 "nickname": p.get("nickname", ""),
                 "content": (p.get("content") or "")[:80],
                 "create_time": p.get("create_time", "")}
                for p in self.qzone_pending
            ],
        }
        try:
            tmp = self.status_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.status_file)
        except OSError as e:
            self.logger.error("status.json 写入失败：%s", e)

    async def status_loop(self) -> None:
        """每 3 秒写一次状态文件。"""
        while True:
            await asyncio.sleep(3)
            self.write_status()

    async def auto_reconnect_loop(self) -> None:
        """每 10 秒检测 NapCat 断连状态，超过阈值自动重启容器。

        配合 onebot_server 的 on_connect/on_disconnect 回调跟踪连接状态。
        """
        while True:
            await asyncio.sleep(10)
            if self._disconnected_since is None:
                continue
            elapsed = time.time() - self._disconnected_since
            if elapsed < MAX_DISCONNECTED_TIME:
                continue
            self.logger.warning("⚠️ NapCat 断连已 %d 秒（阈值 %d 秒），自动重启容器...",
                                int(elapsed), MAX_DISCONNECTED_TIME)
            try:
                # 先恢复备份的 QQ 登录态
                container_mgr.restore_qq_session(self.username)
                # 重启容器
                container_mgr.restart_container(self.username)
                self._disconnected_since = None  # 重启后重置计时，等待重新连接
                self.logger.info("✅ NapCat 容器已重启，等待重新连接...")
            except container_mgr.ContainerError as e:
                self.logger.error("❌ NapCat 容器重启失败：%s", e)
                # 重置计时器，避免反复失败日志刷屏
                self._disconnected_since = time.time()

    async def trigger_loop(self) -> None:
        """每 1 秒轮询 pending_trigger.json（Web 管理端手动触发）。"""
        while True:
            await asyncio.sleep(1)
            if not self.trigger_file.exists():
                continue
            try:
                data = json.loads(self.trigger_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.logger.warning("pending_trigger.json 解析失败，跳过")
                self.trigger_file.unlink(missing_ok=True)
                continue
            self.trigger_file.unlink(missing_ok=True)   # 先删除保证只处理一次
            gid = int(data.get("group_id", 0))
            if time.time() - float(data.get("timestamp", 0)) > 60:
                self.logger.warning("手动触发已过期（群 %s），忽略", gid)
                continue
            if gid not in self.states:
                self.logger.error("手动触发失败：群 %s 未在配置中", gid)
                continue
            st = self.states[gid]
            if not self.resolve_group_config(gid).get("enabled", True):
                self.logger.info("手动触发忽略：群 %s 已停用发送", gid)
                continue
            if st.sending:
                self.logger.info("手动触发忽略：群 %s 正在发送中", gid)
                continue
            if self.onebot is None or not self.qq_online:
                self.logger.error("手动触发失败：群 %s 发送端未在线（QQ 离线或 NapCat 未连接）", gid)
                continue
            self.logger.info("🖱️ 收到手动触发：群 %s", gid)
            asyncio.create_task(self.try_send_flyer(gid, st))

    async def config_watch_loop(self) -> None:
        """每 10 秒检测该用户 config.json 变更并热重载。"""
        last_mtime = self.config_file.stat().st_mtime if self.config_file.exists() else 0
        while True:
            await asyncio.sleep(10)
            try:
                mtime = self.config_file.stat().st_mtime
            except OSError:
                continue
            if mtime == last_mtime:
                continue
            last_mtime = mtime
            try:
                new_cfg = self.load_config()         # 复用严格校验
            except SystemExit:
                self.logger.error("配置热重载失败（校验未通过），沿用旧配置")
                continue
            self.cfg.clear()
            self.cfg.update(new_cfg)                 # 原地替换，闭包立即生效
            for gid in new_cfg["groups"]:
                self.states.setdefault(gid, GroupState())
            for gid in list(self.states):
                if gid not in new_cfg["groups"]:
                    del self.states[gid]
            self.save_state()
            self.logger.info("配置已热重载：%d 个群", len(new_cfg["groups"]))

    # ── 空间监控（qzone）──

    def _load_qzone_dedup(self) -> None:
        """加载已处理说说 tid 去重集（qzone_dedup.json）。"""
        f = self.user_dir / "qzone_dedup.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            self._qzone_dedup = set(data)
        except (OSError, json.JSONDecodeError):
            self._qzone_dedup = set()
        if len(self._qzone_dedup) > 8000:
            self._qzone_dedup = set(list(self._qzone_dedup)[-3000:])
        self.logger.info("空间监控去重集加载：%d 条", len(self._qzone_dedup))

    def _save_qzone_dedup(self) -> None:
        """持久化去重集（限制条数，防文件无限增长）。"""
        try:
            f = self.user_dir / "qzone_dedup.json"
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(list(self._qzone_dedup)[-5000:]),
                           encoding="utf-8")
            os.replace(tmp, f)
            self._qzone_dedup_dirty = False
        except OSError as e:
            self.logger.error("空间监控去重集保存失败：%s", e)

    def _load_qzone_cookies_file(self) -> str:
        """读取管理台扫码登录持久化的完整 Qzone Cookies（含 qzone 域凭证）。

        NapCat 的 get_cookies 只返回 ptlogin 域的部分 cookies，h5 写接口
        （点赞/评论）需要 qzone.qq.com 域凭证（qzone_token/p_skey），因此
        优先使用扫码登录的完整 cookies。
        """
        f = self.user_dir / QZONE_COOKIES_FILE
        if not f.exists():
            return ""
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            cookies = data.get("cookies", "")
            if not cookies:
                return ""
            fetched = float(data.get("fetched_at", 0))
            if time.time() - fetched > QZONE_COOKIES_FILE_TTL:
                self.logger.warning("Qzone 扫码 cookies 已过期（%.0f 小时），"
                                    "需重新扫码", QZONE_COOKIES_FILE_TTL / 3600)
                return ""
            if "p_skey=" not in cookies and "skey=" not in cookies:
                self.logger.warning("Qzone 扫码 cookies 缺少 p_skey/skey，忽略")
                return ""
            return cookies
        except (OSError, json.JSONDecodeError) as e:
            self.logger.warning("Qzone 扫码 cookies 读取失败：%s", e)
            return ""

    async def _fetch_qzone_cookies(self) -> str:
        """获取 Qzone Cookies：优先管理台扫码登录的完整 cookies，NapCat 兜底。

        NapCat 的 get_cookies/get_credentials 只含 ptlogin 域凭证，
        h5.qzone.qq.com 写接口（点赞/评论）会因缺 qzone 域凭证返回 403，
        因此优先使用扫码登录的完整 cookies（users/<u>/qzone_cookies.json）。
        """
        if self.onebot is None or not self.onebot.connected:
            return ""
        # ① 扫码登录持久化的完整 cookies
        file_cookies = self._load_qzone_cookies_file()
        if file_cookies:
            return self._ensure_uin_in_cookies(file_cookies)
        # ① get_credentials：cookies 更全（可能含 qzone_token 等 h5 登录判定 cookie）
        try:
            resp = await self.onebot.call_action("get_credentials", {}, timeout=10)
            if resp.get("retcode") == 0:
                data = resp.get("data") or {}
                cookies = data.get("cookies", "") or ""
                pskey = (data.get("pskey") or {})
                qz_skey = pskey.get("qzone.qq.com", "") or ""
                if qz_skey:
                    parts = []
                    uin = self.user_meta.get("qq_number") or ""
                    if not uin:
                        m = re.search(r"(?:^|;)\s*uin=(\d+)", cookies)
                        if m:
                            uin = m.group(1)
                    if uin:
                        parts.append(f"uin={uin}")
                        parts.append(f"p_uin={uin}")
                    parts.append(f"p_skey={qz_skey}")
                    # 保留 cookies 中其它字段（qzone_token/skey/pt4_token 等）
                    seen = {"uin", "p_uin", "p_skey"}
                    for pair in cookies.split(";"):
                        pair = pair.strip()
                        k = pair.split("=", 1)[0] if "=" in pair else ""
                        if k and k not in seen:
                            parts.append(pair)
                    self.logger.info("Qzone 凭证获取成功（get_credentials/pskey，"
                                     "uin=%s，字段数=%d）", uin or "?", len(parts))
                    return "; ".join(parts)
                if cookies and "p_skey=" in cookies:
                    self.logger.info("Qzone 凭证获取成功（get_credentials/cookies，含 p_skey，"
                                     "字段=%s）",
                                     [k.split("=", 1)[0].strip()
                                      for k in cookies.split(";") if "=" in k][:12])
                    return self._ensure_uin_in_cookies(cookies)
        except Exception as e:
            self.logger.debug("get_credentials 失败：%s", e)
        # ② get_cookies(domain=qzone.qq.com)：必须含 p_skey
        try:
            resp = await self.onebot.call_action(
                "get_cookies", {"domain": "qzone.qq.com"}, timeout=10)
            if resp.get("retcode") == 0:
                cookies = (resp.get("data") or {}).get("cookies", "") or ""
                if cookies and "p_skey=" in cookies:
                    self.logger.info("Qzone 凭证获取成功（get_cookies，含 p_skey）")
                    return self._ensure_uin_in_cookies(cookies)
                if cookies and "p_skey=" not in cookies:
                    self.logger.warning(
                        "get_cookies 返回的 cookies 不含 p_skey（仅 skey），"
                        "Qzone API 将拒绝，字段：%s",
                        [k.split("=", 1)[0] for k in cookies.split(";")
                         if "=" in k][:10])
        except Exception as e:
            self.logger.debug("get_cookies 失败：%s", e)
        self.logger.warning("获取 Qzone 凭证失败（get_credentials 与 get_cookies 均不可用）")
        return ""

    @staticmethod
    def _ensure_uin_in_cookies(cookies: str) -> str:
        """确保 cookie 串含 uin（Qzone 接口需要），缺失时补占位。"""
        if re.search(r"(?:^|;)\s*uin=", cookies):
            return cookies
        m = re.search(r"(?:^|;)\s*p_uin=(\d+)", cookies)
        uin = m.group(1) if m else ""
        return f"uin={uin}; {cookies}" if uin else cookies

    async def _qzone_refresh_client(self) -> bool:
        """按 TTL 刷新 Qzone 客户端（拿 cookies）。成功返回 True。"""
        now = time.time()
        if (self._qzone_client is not None and self._qzone_cookies
                and now - self._qzone_cookies_fetched_at <= QZONE_COOKIES_TTL):
            return True
        cookies = await self._fetch_qzone_cookies()
        if not cookies:
            self.qzone_state["error"] = "获取 Qzone 凭证失败（NapCat 未连接或未绑定 QQ）"
            return False
        self._qzone_cookies = cookies
        self._qzone_cookies_fetched_at = now
        self._qzone_client = QzoneClient(cookies, self.user_meta.get("qq_number", ""))
        self.qzone_state["error"] = ""
        self.logger.info("Qzone 凭证已刷新（TTL %ds）", QZONE_COOKIES_TTL)
        return True

    async def qzone_loop(self) -> None:
        """空间监控轮询循环：按 poll_interval 检查监控列表，触发动作。"""
        while True:
            qz = self.cfg.get("qzone") or {}
            interval = max(int(qz.get("poll_interval", 60) or 60), 15)
            await asyncio.sleep(interval)
            if not qz.get("enabled", False):
                continue
            if self.onebot is None or not self.qq_online:
                self.qzone_state["error"] = "发送端未在线（QQ 离线或 NapCat 未连接）"
                continue
            try:
                await self._qzone_check()
            except Exception as e:
                self.logger.error("空间监控轮询异常：%s", e, exc_info=True)
                self.qzone_state["error"] = f"轮询异常：{e}"

    async def _qzone_check(self) -> None:
        """单轮检查：取/刷新凭证 → 分页抓取全部动态 → 关键词过滤。

        匹配的动态：手动模式（manual_mode=true）进入待执行队列由管理台
        点击执行；自动模式（false）立即执行（动作间带随机延时）。
        """
        qz = self.cfg.get("qzone") or {}
        monitor_list = qz.get("monitor_list") or []
        if not monitor_list:
            return
        if not await self._qzone_refresh_client():
            return
        client = self._qzone_client
        qfilter = KeywordFilter(qz.get("keywords") or [])
        manual = bool(qz.get("manual_mode", False))
        max_posts = max(int(qz.get("max_posts", 100) or 100), 10)
        self.qzone_state["last_check_time"] = time.strftime("%Y-%m-%d %H:%M:%S")

        for target in monitor_list:
            uin = str(target.get("uin", "")).strip()
            if not uin or not target.get("enabled", True):
                continue
            # 昵称缓存（每目标一轮只查一次）
            if uin not in self._qzone_nicknames:
                nickname = ""
                try:
                    info = await asyncio.to_thread(client.get_user_info, uin)
                    nickname = (info or {}).get("nickname", "") or ""
                except Exception:
                    pass
                self._qzone_nicknames[uin] = nickname
                if len(self._qzone_nicknames) > 200:
                    self._qzone_nicknames.clear()
            nickname = self._qzone_nicknames.get(uin) or target.get("name", "") or uin

            try:
                posts = await self._qzone_fetch_all(client, uin, max_posts)
            except Exception as e:
                self.logger.error("抓取 %s 空间动态失败：%s", uin, e)
                continue
            if not posts:
                continue

            for post in posts:
                tid = str(post.get("tid", ""))
                if not tid or tid in self._qzone_dedup:
                    continue
                content = post.get("content", "") or ""
                if qfilter.is_active and not qfilter.match(content):
                    continue
                self.qzone_state["total_checked"] += 1
                post["nickname"] = nickname
                self.qzone_state["total_matched"] += 1
                if manual:
                    # 手动模式：进入待执行队列，等管理台点击执行
                    self._qzone_enqueue_pending(post)
                else:
                    # 自动模式：立即执行（动作间随机延时）
                    await self._qzone_do_actions(post, qz)
                    self._qzone_dedup.add(tid)
                    self._qzone_dedup_dirty = True
        if self._qzone_dedup_dirty:
            self._save_qzone_dedup()

    async def _qzone_fetch_all(self, client: QzoneClient, uin: str,
                               max_posts: int) -> list:
        """分页抓取目标用户的全部动态（上限 max_posts，页间随机延时防风控）。

        Qzone 的 emotion_cgi_msglist_v6 每页最多 35 条，但实测 num 过大
        （35）可能被接口拒绝返回空，采用保守的 20 条/页，通过 pos 翻页。
        """
        page_size = 20
        all_posts: list = []
        pos = 0
        while len(all_posts) < max_posts:
            batch = await asyncio.to_thread(client.fetch_shuoshuo, uin, page_size, pos)
            if not batch:
                break
            all_posts.extend(batch)
            if len(batch) < page_size:
                break
            pos += len(batch)
            if len(all_posts) < max_posts:
                # 分页间隔随机延时（0.8-2.5 秒），避免高频请求被风控
                await asyncio.sleep(random.uniform(0.8, 2.5))
        return all_posts[:max_posts]

    def _qzone_enqueue_pending(self, post: dict) -> None:
        """加入待执行队列（按 tid 去重，保留最新）。"""
        tid = str(post.get("tid", ""))
        for i, p in enumerate(self.qzone_pending):
            if str(p.get("tid", "")) == tid:
                self.qzone_pending[i] = post
                return
        self.qzone_pending.append(post)

    async def _qzone_action_delay(self, qz: dict) -> None:
        """动作之间随机延时（防风控）。"""
        rng = qz.get("action_delay") or {}
        lo = max(float(rng.get("min", 2)), 0.5)
        hi = max(float(rng.get("max", 8)), lo)
        await asyncio.sleep(random.uniform(lo, hi))

    async def _qzone_do_actions(self, post: dict, qz: dict) -> None:
        """对一条匹配的说说执行动作：转发（OneBot）/ 点赞 / 评论（Qzone API）。

        各动作之间随机延时（action_delay 范围），降低被风控的概率。
        """
        actions = qz.get("actions") or {}
        tid = str(post.get("tid", ""))
        uin = str(post.get("uin", ""))
        results: dict = {}

        # 1. 转发（复用 OneBot 连接，发送文本消息）
        if actions.get("forward", True) and qz.get("forward_list"):
            message = KeywordFilter.format_content(post)
            sent = 0
            for target in qz["forward_list"]:
                if not target.get("enabled", True):
                    continue
                ttype = target.get("type", "group")
                tgt_id = str(target.get("id", "")).strip()
                if not tgt_id:
                    continue
                try:
                    if ttype == "group":
                        resp = await self.onebot.call_action(
                            "send_group_msg",
                            {"group_id": int(tgt_id),
                             "message": [{"type": "text", "data": {"text": message}}]},
                            timeout=self.cfg.get("api_timeout", 30))
                    else:
                        resp = await self.onebot.call_action(
                            "send_private_msg",
                            {"user_id": int(tgt_id),
                             "message": [{"type": "text", "data": {"text": message}}]},
                            timeout=self.cfg.get("api_timeout", 30))
                    if resp.get("retcode") == 0:
                        sent += 1
                except Exception as e:
                    self.logger.error("空间动态转发到 %s(%s) 失败：%s",
                                      ttype, tgt_id, e)
            self.qzone_state["total_forwarded"] += 1
            results["forward"] = {"success": sent > 0, "sent": sent}
            if actions.get("like", False) or actions.get("comment", False):
                await self._qzone_action_delay(qz)

        # 2. 点赞
        if actions.get("like", False) and self._qzone_client:
            like_ok = await asyncio.to_thread(
                self._qzone_client.like_shuoshuo, uin, tid, post)
            self.qzone_state["total_liked"] += 1
            results["like"] = {"success": like_ok}
            if actions.get("comment", False):
                await self._qzone_action_delay(qz)

        # 3. 评论
        if actions.get("comment", False) and self._qzone_client:
            gen = CommentGenerator(qz.get("comment_templates") or [],
                                   qz.get("ai") or {})
            mode = actions.get("comment_mode", "template")
            comment_text = await asyncio.to_thread(gen.generate, post, mode)
            if comment_text:
                ok = await asyncio.to_thread(
                    self._qzone_client.comment_shuoshuo, uin, tid, comment_text, post)
                self.qzone_state["total_commented"] += 1
                results["comment"] = {"success": ok, "text": comment_text}
            else:
                results["comment"] = {"success": False, "text": ""}

        self.qzone_state["history"].append({
            "time": time.time(),
            "uin": uin,
            "tid": tid,
            "nickname": post.get("nickname", ""),
            "content": (post.get("content", "") or "")[:80],
            "actions": results,
        })
        done = [k for k, v in results.items() if v.get("success")]
        self.logger.info("空间动态动作完成：昵称=%s 内容=%s... 动作=[%s]",
                         post.get("nickname", ""), (post.get("content") or "")[:30],
                         ", ".join(done) or "无")

    @staticmethod
    def _qzone_cookie_val(cookies: str, key: str) -> str:
        """从 cookie 串中取指定 key 的值（截断显示，防泄露）。"""
        m = re.search(r"(?:^|;)\s*" + re.escape(key) + r"=([^;]*)", cookies)
        if not m or not m.group(1):
            return "缺失"
        v = m.group(1)
        return f"{len(v)}字符:{v[:8]}..."

    def _qzone_diag_raw(self, client: QzoneClient, uin: str) -> str:
        """直接调用 Qzone 用户信息 API，返回原始响应摘要（诊断用）。"""
        url = ("https://user.qzone.qq.com/proxy/domain/base.qzone.qq.com/"
               "cgi-bin/user/cgi_userinfo_get_all")
        params = {"uin": uin, "g_tk": client._g_tk,
                  "callback": "_preloadCallback", "format": "jsonp"}
        try:
            r = client._session.get(url, params=params, timeout=10)
            return f"HTTP {r.status_code} {r.text[:250]}"
        except Exception as e:
            return f"请求异常: {e}"

    # ── 空间监控测试与手动执行（Web 管理台协作）──

    async def qzone_test_loop(self) -> None:
        """每 1 秒检测管理台下发的测试/手动执行请求文件。"""
        while True:
            await asyncio.sleep(1)
            await self._handle_qzone_test()
            await self._handle_qzone_execute()

    async def _handle_qzone_execute(self) -> None:
        """处理手动执行请求（pending_qzone_execute.json，tid 可选=全部）。

        对目标动态执行设定动作（转发/点赞/评论），条间随机延时防风控，
        执行成功后移入去重集并从待执行队列移除。
        """
        f = self.qzone_execute_file
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            f.unlink(missing_ok=True)
            return
        f.unlink(missing_ok=True)
        if time.time() - float(data.get("timestamp", 0)) > 120:
            self.logger.warning("手动执行请求已过期，忽略")
            return

        # 注意：webapp 可能写入 tid=None（表示全部），不能用 str(None)
        tid_val = data.get("tid")
        target_tid = str(tid_val).strip() if tid_val else ""
        qz = self.cfg.get("qzone") or {}
        if not qz.get("monitor_list"):
            self.logger.warning("手动执行：空间监控未配置监控目标")
            return
        if self.onebot is None or not self.qq_online:
            self.logger.error("手动执行失败：发送端未在线（QQ 离线或 NapCat 未连接）")
            return
        if not await self._qzone_refresh_client():
            self.logger.error("手动执行失败：Qzone 凭证不可用")
            return

        to_exec = [p for p in self.qzone_pending
                   if not target_tid or str(p.get("tid", "")) == target_tid]
        if not to_exec:
            self.logger.info("手动执行：无匹配的待执行动态%s",
                             f"（tid={target_tid}）" if target_tid else "（全部）")
            return

        self.logger.info("🖱️ 手动执行 %d 条动态%s", len(to_exec),
                         f"（tid={target_tid}）" if target_tid else "（全部）")
        for i, post in enumerate(to_exec):
            await self._qzone_do_actions(post, qz)
            tid = str(post.get("tid", ""))
            self._qzone_dedup.add(tid)
            self._qzone_dedup_dirty = True
            # 从待执行队列移除
            self.qzone_pending = deque(
                [p for p in self.qzone_pending if str(p.get("tid", "")) != tid],
                maxlen=100)
            # 多条之间随机延时（1-3 倍 action_delay，避免连续操作被风控）
            if i < len(to_exec) - 1:
                rng = qz.get("action_delay") or {}
                lo = max(float(rng.get("min", 3)), 1.0)
                hi = max(float(rng.get("max", 10)), lo)
                await asyncio.sleep(random.uniform(lo, hi))
        if self._qzone_dedup_dirty:
            self._save_qzone_dedup()
        self.logger.info("✅ 手动执行完成：%d 条", len(to_exec))

    async def _handle_qzone_test(self) -> None:
        if not self.qzone_test_file.exists():
            return
        try:
            data = json.loads(self.qzone_test_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.qzone_test_file.unlink(missing_ok=True)
            return
        self.qzone_test_file.unlink(missing_ok=True)
        if time.time() - float(data.get("timestamp", 0)) > 60:
            return
        result = {"success": False, "checked_at": time.time(),
                  "message": "NapCat 未连接，无法获取 QQ 凭证"}
        if self.onebot is not None and self.onebot.connected:
                cookies = await self._fetch_qzone_cookies()
                if not cookies:
                    result["message"] = ("获取 Qzone 凭证失败（NapCat 已连接但拿不到 "
                                         "p_skey，请确认已绑定 QQ 且未掉线）")
                else:
                    # 优先用 cookies 里的 uin（实际登录者本人），users.json 的
                    # qq_number 可能过时（用户换号重登后未同步）
                    uin = self.user_meta.get("qq_number") or ""
                    m = re.search(r"(?:^|;)\s*uin=(\d+)", cookies)
                    if m:
                        uin = m.group(1)
                    elif not uin:
                        m = re.search(r"(?:^|;)\s*p_uin=(\d+)", cookies)
                        if m:
                            uin = m.group(1)
                    client = QzoneClient(cookies, uin)
                    if uin:
                        info = await asyncio.to_thread(client.get_user_info, uin)
                        if info:
                            result["success"] = True
                            result["nickname"] = info.get("nickname", "")
                            result["message"] = (f"连接成功：{info.get('nickname', '')}"
                                                 f"（{uin}），Qzone 凭证有效")
                            # 诊断：抓取不同页大小对比（确认 num 参数兼容性）
                            try:
                                n5 = len(await asyncio.to_thread(
                                    client.fetch_shuoshuo, uin, 5, 0))
                                n20 = len(await asyncio.to_thread(
                                    client.fetch_shuoshuo, uin, 20, 0))
                                n35 = len(await asyncio.to_thread(
                                    client.fetch_shuoshuo, uin, 35, 0))
                                result["fetch_diag"] = f"num=5:{n5}条, num=20:{n20}条, num=35:{n35}条"
                                result["message"] += f"；抓取诊断：{result['fetch_diag']}"
                            except Exception as e:
                                result["message"] += f"；抓取诊断异常：{e}"
                            # 诊断：点赞/评论接口实测（用最新一条说说）
                            try:
                                posts = await asyncio.to_thread(
                                    client.fetch_shuoshuo, uin, 1, 0)
                                if posts:
                                    p0 = posts[0]
                                    t0 = str(p0.get("tid", ""))
                                    like_ok = await asyncio.to_thread(
                                        client.like_shuoshuo, uin, t0, p0)
                                    c_ok = await asyncio.to_thread(
                                        client.comment_shuoshuo, uin, t0, "测试评论", p0)
                                    result["action_diag"] = (
                                        f"点赞={'成功' if like_ok else '失败'}，"
                                        f"评论={'成功' if c_ok else '失败'}（tid={t0[:12]}...，"
                                        f"appid={p0.get('appid')}，fid={str(p0.get('fid'))[:14]}...）")
                                    result["message"] += f"；动作诊断：{result['action_diag']}"
                            except Exception as e:
                                result["message"] += f"；动作诊断异常：{e}"
                            except Exception as e:
                                result["message"] += f"；动作诊断异常：{e}"
                        else:
                            fields = [k.split("=", 1)[0].strip()
                                      for k in cookies.split(";") if "=" in k]
                            diag = await asyncio.to_thread(
                                self._qzone_diag_raw, client, uin)
                            result["message"] = (
                                f"Qzone 凭证无效或已过期（get_user_info 失败，uin={uin}，"
                                f"cookies 字段={fields}，p_skey 长度="
                                f"{self._qzone_cookie_val(cookies, 'p_skey')}；"
                                f"原始响应：{diag}）")
                    else:
                        result["message"] = "该用户未绑定 QQ 号，无法测试"
        try:
            tmp = self.qzone_test_result_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(result, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.qzone_test_result_file)
        except OSError as e:
            self.logger.error("空间监控测试结果写入失败：%s", e)

    # ── 生命周期 ──

    def start(self) -> None:
        """启动该引擎的全部协程（onebot + 管理循环 + 空间监控）。"""
        self.load_state()
        onebot = self.run_forever()
        self.tasks = [
            asyncio.create_task(onebot.start(), name=f"onebot-{self.username}"),
            asyncio.create_task(self.status_loop(), name=f"status-{self.username}"),
            asyncio.create_task(self.trigger_loop(), name=f"trigger-{self.username}"),
            asyncio.create_task(self.config_watch_loop(), name=f"config-{self.username}"),
            asyncio.create_task(self.auto_reconnect_loop(), name=f"reconnect-{self.username}"),
            asyncio.create_task(self.health_check_loop(), name=f"health-{self.username}"),
            asyncio.create_task(self.qzone_loop(), name=f"qzone-{self.username}"),
            asyncio.create_task(self.qzone_test_loop(), name=f"qzonetest-{self.username}"),
        ]
        self.logger.info("🚀 用户引擎启动（%s）：目标群 %s，反向 WS 端口 %s",
                         self.username, list(self.states.keys()),
                         self.user_meta["ws_port"])

    async def stop(self) -> None:
        """停止引擎：取消所有协程并保存状态。"""
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        self.save_state()
        if self._qzone_dedup_dirty:
            self._save_qzone_dedup()
        self.logger.info("用户引擎已停止（%s）", self.username)

    async def simulate(self, gid: int) -> None:
        """联调模式：跳过计数条件，直接走完整发送链路。"""
        self.logger.info("--simulate 模式：向群 %s 直接发送传单", gid)
        if gid not in self.states:
            self.logger.error("群 %s 未在配置的 groups 中", gid)
            return
        for _ in range(30):
            if self.onebot and self.onebot.connected and self._qq_online:
                break
            await asyncio.sleep(1)
        if self.onebot is None or not self.onebot.connected:
            self.logger.error("发送端未在 30 秒内接入，模拟触发取消")
        elif not self._qq_online:
            self.logger.error("QQ 账号已离线，模拟触发取消")
            return
        await self.try_send_flyer(gid, self.states[gid])


# ─────────────────────────── 引擎管理 ───────────────────────────

engines: dict[str, UserEngine] = {}


def start_engine(username: str, user_meta: dict) -> UserEngine:
    """创建并启动一个用户引擎（重复启动先停旧的）。"""
    old = engines.get(username)
    if old is not None:
        return old
    _ensure_user_file_handler(username)   # 运行时新增用户（webapp 创建）补日志 handler
    engine = UserEngine(username, user_meta)
    engines[username] = engine
    engine.start()
    return engine


async def stop_engine(username: str) -> None:
    engine = engines.pop(username, None)
    if engine is not None:
        await engine.stop()


async def users_watch_loop(sys_cfg: dict) -> None:
    """每 10 秒对比 users.json：新增用户起引擎，删除/禁用用户停引擎。

    用户可通 Web 管理台自行关闭/启动自己的引擎（users.json 中 disabled 字段）。
    """
    last_users: dict[str, bool] = {}  # username -> disabled
    while True:
        await asyncio.sleep(10)
        try:
            users = load_users()
        except SystemExit:
            continue
        current = {u: users[u].get("disabled", False) for u in users}

        for username, disabled in current.items():
            was_disabled = last_users.get(username)
            if was_disabled is None:
                # 新用户出现
                if not disabled:
                    try:
                        start_engine(username, users[username])
                    except SystemExit as e:
                        logger.error("用户 %s 引擎启动失败：%s", username, e)
            elif disabled and not was_disabled:
                # 引擎被关闭（disabled 从 false→true）
                await stop_engine(username)
                logger.info("用户 %s 引擎已关闭（配置禁用）", username)
            elif not disabled and was_disabled:
                # 引擎被重新启用（disabled 从 true→false）
                try:
                    start_engine(username, users[username])
                except SystemExit as e:
                    logger.error("用户 %s 引擎启动失败：%s", username, e)

        # 用户被从 users.json 删除
        for username in last_users:
            if username not in current:
                await stop_engine(username)

        last_users = current


# ─────────────────────────── 入口 ───────────────────────────

def main() -> None:
    sys_cfg = load_system_config()
    setup_logging(sys_cfg)

    async def _run() -> None:
        if "--simulate" in sys.argv:
            # 联调模式：python server.py --simulate <用户名> <群号>
            idx = sys.argv.index("--simulate")
            username = sys.argv[idx + 1]
            gid = int(sys.argv[idx + 2])
            user_meta = load_users().get(username)
            if not user_meta:
                logger.error("用户 %s 不存在", username)
                return
            engine = UserEngine(username, user_meta)
            engine.load_state()
            onebot = engine.run_forever()
            onebot_task = asyncio.create_task(onebot.start(), name="onebot")
            await engine.simulate(gid)
            onebot_task.cancel()
            try:
                await onebot_task
            except asyncio.CancelledError:
                pass
            engine.save_state()
            logger.info("模拟触发完成，退出")
            return
        # 正常模式：启动所有用户引擎 + 用户管理循环常驻
        for username, user_meta in load_users().items():
            try:
                start_engine(username, user_meta)
            except SystemExit as e:
                logger.error("用户 %s 引擎启动失败：%s", username, e)
        logger.info("🚀 自动传单派发（多用户服务器端）启动，用户：%s",
                    list(engines.keys()))
        watch_task = asyncio.create_task(users_watch_loop(sys_cfg), name="users_watch")
        try:
            await watch_task
        except asyncio.CancelledError:
            pass
        finally:
            for engine in list(engines.values()):
                engine.save_state()
            logger.info("已退出，状态已保存")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        for engine in engines.values():
            engine.save_state()
        logger.info("收到 Ctrl+C，已退出并保存状态")


if __name__ == "__main__":
    main()
