"""自动传单派发 —— Web 管理服务（独立 Flask 进程，:8081，多用户版）。

功能：
- 用户名+密码登录（users.json 中 pbkdf2 哈希校验）换取内存 session token
- 多租户：普通用户只能管理自己；admin 可切换目标用户（target）并管理所有用户
- 群管理 / 内容与图片 / 状态监控 / 手动触发 / 日志，全部按操作目标用户解析到
  users/<username>/ 下（与 server.py 的文件协作模式一致：status.json 读、pending_trigger.json 写）
- admin 专属：用户创建/删除、NapCat 容器绑定流程（ensure_container → 扫码 →
  wait_for_onebot11 → write_reverse_ws → restart_container）

与 server.py 完全解耦：不 import server.py，配置每次从磁盘读，写配置用原子替换；
只依赖 container_mgr（容器操作）+ flask/werkzeug（随 flask 自带，零新增依赖）。
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import smtplib
import threading
import time
import urllib.request
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import container_mgr
from qr_login import QRLogin

BASE_DIR = Path(__file__).resolve().parent
SYSTEM_CONFIG_FILE = BASE_DIR / "config.json"
USERS_FILE = BASE_DIR / "users.json"
INVITES_FILE = BASE_DIR / "invites.json"
LOGS_DIR = BASE_DIR / "logs"
WEB_DIR = BASE_DIR / "web"

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024     # 10MB
STATUS_STALE_SECONDS = 10             # status.json 超过 10s 视为核心服务离线
MIN_PASSWORD_LEN = 6
EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+$")
VERIFY_CODE_TTL = 600                 # 邮箱验证码有效期（秒）
SEND_CODE_COOLDOWN = 60               # 同一邮箱发送验证码的最小间隔（秒）

app = Flask(__name__, static_folder=None)
users_lock = threading.Lock()         # users.json 写锁
config_lock = threading.Lock()        # 用户配置写锁（跨用户串行即可）
sessions: dict[str, dict] = {}        # token -> {"username", "target", "expiry"}
sessions_lock = threading.Lock()
bind_states: dict[str, dict] = {}     # username -> {"status", "qq_number", "error"}
bind_lock = threading.Lock()
verify_codes: dict[str, dict] = {}    # email -> {"code", "expires_at", "invite_code"}
send_cooldowns: dict[str, float] = {} # email -> 上次发码时间（限流）
verify_lock = threading.Lock()
qzone_login_states: dict[str, dict] = {}  # username -> {"qr": QRLogin, "started_at"}
qzone_login_lock = threading.Lock()

logger = logging.getLogger("webapp")

# 新用户 config.json 模板（满足 server.py load_config 的必填校验；
# image_root 与 container_mgr 挂载 users/<u>/images:/app/images 对应）
NEW_USER_CONFIG = {
    "global": {"message_threshold": 30, "send_interval": 1800,
               "count_own_messages": False},
    "message": {"template": "", "images": [], "default_image": None,
                "image_root": "/app/images"},
    "groups": {},
    "categories": {},
    "qzone": {
        "enabled": False,
        "poll_interval": 60,
        "monitor_list": [],
        "keywords": [],
        "actions": {"forward": True, "like": False, "comment": False,
                    "comment_mode": "template"},
        "comment_templates": [
            {"content": "说得好！{nickname}", "weight": 1},
            {"content": "写的真不错👍", "weight": 1},
        ],
        "forward_list": [],
        "manual_mode": True,             # True=匹配进入待执行队列，手动点按钮执行
        "max_posts": 100,                # 每次轮询最多抓取的动态条数
        "action_delay": {"min": 2, "max": 8},   # 动作间随机延时（秒，防风控）
        "ai": {
            "enabled": False,
            "api_url": "https://api.deepseek.com/v1/chat/completions",
            "api_key": "",
            "model": "deepseek-chat",
            "system_prompt": "你是一个QQ空间的评论助手。请根据用户的说说的内容，生成一条简短、自然、友好的评论（10-30字）。",
            "max_tokens": 100,
            "temperature": 0.8,
        },
    },
    "api_timeout": 30,
    "error_cooldown": 300,
    "confirm_timeout": 15,
}


# ─────────────────────────── 路径助手 ───────────────────────────

def user_dir(username: str) -> Path:
    """users/<username> 目录（构造前必过用户名白名单，防路径穿越）。"""
    container_mgr.check_username(username)
    return BASE_DIR / "users" / username


def user_config_file(username: str) -> Path:
    return user_dir(username) / "config.json"


def user_status_file(username: str) -> Path:
    return user_dir(username) / "status.json"


def user_trigger_file(username: str) -> Path:
    return user_dir(username) / "pending_trigger.json"


def user_images_dir(username: str) -> Path:
    return user_dir(username) / "images"


# ─────────────────────────── 系统配置 / 账号库 / 用户配置读写 ───────────────────────────

def load_sys_config() -> dict:
    """宽松加载系统配置（web/logging/docker 段）：出错返回 {}（webapp 不能退出）。"""
    try:
        return json.loads(SYSTEM_CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_users() -> dict:
    """宽松加载账号库 users.json：出错返回 {}。"""
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_users(users: dict) -> tuple[bool, str]:
    """原子写账号库（tmp + os.replace）。返回 (成功, 错误信息)。"""
    with users_lock:
        try:
            tmp = USERS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, USERS_FILE)
            return True, ""
        except OSError as e:
            return False, f"账号库写入失败：{e}"


def load_user_config(username: str) -> dict:
    """宽松加载用户业务配置：出错返回 {}。"""
    try:
        return json.loads(user_config_file(username).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_user_config(username: str, cfg: dict) -> tuple[bool, str]:
    """原子写用户配置（tmp + os.replace）。返回 (成功, 错误信息)。"""
    with config_lock:
        try:
            path = user_config_file(username)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
            return True, ""
        except OSError as e:
            return False, f"配置写入失败：{e}"


# ─────────────────────────── 邀请码（注册入口） ───────────────────────────

def load_invites() -> dict:
    """宽松加载邀请码库 invites.json：出错返回 {}。"""
    try:
        return json.loads(INVITES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_invites(invites: dict) -> tuple[bool, str]:
    """原子写邀请码库。返回 (成功, 错误信息)。"""
    with users_lock:                       # 与账号库同一把锁（注册流程同时涉及两者）
        try:
            tmp = INVITES_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(invites, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, INVITES_FILE)
            return True, ""
        except OSError as e:
            return False, f"邀请码库写入失败：{e}"


def send_mail(to: str, subject: str, body_html: str) -> tuple[bool, str]:
    """通过 SMTP 发送邮件（config.json smtp 段配置）。返回 (成功, 错误信息)。"""
    smtp_cfg = load_sys_config().get("smtp", {})
    host = smtp_cfg.get("host")
    if not host:
        return False, "SMTP 未配置"
    port = int(smtp_cfg.get("port", 465))
    user = smtp_cfg.get("user", "")
    password = smtp_cfg.get("password", "")
    from_addr = smtp_cfg.get("from") or user
    try:
        if int(smtp_cfg.get("ssl", 1)):
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        if user:
            server.login(user, password)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        msg.set_content(body_html, subtype="html")
        server.send_message(msg)
        server.quit()
        return True, ""
    except Exception as e:
        return False, str(e)


# ─────────────────────────── 认证与多租户解析 ───────────────────────────

def _current_token() -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else ""


def check_password_safe(pwhash: str, password: str) -> bool:
    """check_password_hash 封装：占位符/非法格式哈希抛 ValueError 时视为校验失败。"""
    try:
        return bool(pwhash) and check_password_hash(pwhash, password)
    except ValueError:
        return False


def get_current_user() -> str:
    """从 Bearer token 解析当前登录用户名；无效/过期返回空串。"""
    token = _current_token()
    if not token:
        return ""
    with sessions_lock:
        s = sessions.get(token)
        if not s:
            return ""
        if s["expiry"] <= time.time():
            sessions.pop(token, None)
            return ""
        return s["username"]


def resolve_target() -> tuple[str, str]:
    """返回 (操作目标用户名, 当前登录用户名)。

    普通用户恒为自己；admin 用 session 中的 target（被删/失效时回退本人）。
    未登录返回 ("", "")。
    """
    token = _current_token()
    if not token:
        return "", ""
    with sessions_lock:
        s = sessions.get(token)
        if not s or s["expiry"] <= time.time():
            sessions.pop(token, None)
            return "", ""
        me = s["username"]
        if load_users().get(me, {}).get("role") != "admin":
            return me, me
        target = s.get("target", me)
        return (target if target in load_users() else me), me


def user_list() -> list[dict]:
    """全部用户安全字段列表（admin 用，含在线状态；绝不返回密码/各 token）。

    webui_port 返回 NapCat 实际 WebUI 端口（4.x 固定 6099，见 get_webui_url）。
    """
    out = []
    actual_webui = int(load_sys_config().get("docker", {}).get("webui_actual_port", 6099))
    for name, meta in load_users().items():
        online = False
        sf = user_status_file(name)
        if sf.exists():
            try:
                st = json.loads(sf.read_text(encoding="utf-8"))
                online = time.time() - float(st.get("timestamp", 0)) <= STATUS_STALE_SECONDS
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        out.append({
            "username": name,
            "role": meta.get("role", "user"),
            "idx": meta.get("idx", 0),
            "ws_port": meta.get("ws_port"),
            "webui_port": actual_webui,
            "qq_number": meta.get("qq_number"),
            "bound": bool(meta.get("bound")),
            "container": meta.get("container"),
            "created_at": meta.get("created_at"),
            "online": online,
        })
    return out


# ─────────────────────────── 工具 ───────────────────────────

def sanitize_filename(name: str) -> str:
    """净化文件名，防路径穿越。"""
    return Path(secure_filename(name)).name


def validate_group(data: dict) -> tuple[bool, str]:
    """校验群数据合法性（保证写出的配置 server.py 能加载）。"""
    gid = data.get("group_id")
    if gid is None:
        return False, "缺少 group_id"
    try:
        int(str(gid))
    except ValueError:
        return False, "群号必须为数字"
    name = data.get("name", "")
    if not str(name).strip():
        return False, "群名不能为空"
    for key in ("message_threshold", "send_interval"):
        val = data.get(key)
        if val in (None, ""):
            continue
        try:
            if int(val) <= 0:
                return False, f"{key} 必须为正整数"
        except (TypeError, ValueError):
            return False, f"{key} 必须为正整数"
    if "enabled" in data and not isinstance(data["enabled"], bool):
        return False, "enabled 必须为布尔值"
    return True, ""


def scan_images(username: str, cfg: dict) -> list[dict]:
    """扫描用户图片目录与配置清单的并集。"""
    result = []
    seen = set()
    img_dir = user_images_dir(username)
    if img_dir.is_dir():
        for f in sorted(img_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in ALLOWED_EXTS:
                result.append({"name": f.name, "size": f.stat().st_size,
                               "in_list": f.name in cfg.get("message", {}).get("images", [])})
                seen.add(f.name)
    for name in cfg.get("message", {}).get("images", []):
        if name not in seen:
            result.append({"name": name, "size": None, "in_list": True})
    return result


def get_webui_url(username: str, meta: dict) -> str:
    """拼接该用户 NapCat WebUI 完整地址（含 token）。

    注意：NapCat 4.x 的 WebUI 端口固定为 6099（不读取 webui.json 的 port 字段），
    因此实际端口取 config.json docker.webui_actual_port（默认 6099）；
    users.json 的 webui_port 仅作端口规划记录，拼 URL 时不用。
    """
    sys_cfg = load_sys_config()
    addr = sys_cfg.get("docker", {}).get("server_public_addr", "127.0.0.1")
    actual = int(sys_cfg.get("docker", {}).get("webui_actual_port", 6099))
    token = container_mgr.get_webui_token(username, meta)
    return f"http://{addr}:{actual}/webui?token={token}"


# ─────────────────────────── 路由：认证 ───────────────────────────

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    users = load_users()
    meta = users.get(username)
    if not meta or not check_password_safe(str(meta.get("password", "")), password):
        time.sleep(1)                       # 轻量防爆破
        return jsonify({"error": "用户名或密码错误"}), 401
    if meta.get("active") is False:         # admin 等预置账号无 active 字段视为已激活
        return jsonify({"error": "账号未激活，请先查收激活邮件（含垃圾箱）"}), 403
    token = secrets.token_urlsafe(32)
    ttl = int(load_sys_config().get("web", {}).get("token_ttl_hours", 24)) * 3600
    now = time.time()
    with sessions_lock:
        # 顺手清理过期 session
        for t in [t for t, s in sessions.items() if s["expiry"] <= now]:
            sessions.pop(t, None)
        sessions[token] = {"username": username, "target": username,
                           "expiry": now + ttl}
    logger.info("用户 %s 登录成功", username)
    return jsonify({"token": token, "expires_in": ttl,
                    "username": username, "role": meta.get("role", "user")})


@app.post("/api/logout")
def logout():
    token = _current_token()
    if token:
        with sessions_lock:
            sessions.pop(token, None)
    return jsonify({"message": "已退出"})


# ─────────────────────────── 路由：注册（邮箱验证码） ───────────────────────────

@app.post("/api/register/send-code")
def api_register_send_code():
    """第一步：向邮箱发送 6 位验证码（校验邮箱/邀请码，60 秒限流）。"""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    invite_code = str(data.get("invite_code", "")).strip()
    if not EMAIL_RE.match(email):
        return jsonify({"error": "邮箱格式不正确"}), 400
    users = load_users()
    if email in users:
        return jsonify({"error": "该邮箱已注册"}), 409
    invites = load_invites()
    inv = invites.get(invite_code)
    if not inv or inv.get("used_by"):
        return jsonify({"error": "邀请码无效或已被使用"}), 400
    if inv.get("expires_at") and time.time() > float(inv["expires_at"]):
        return jsonify({"error": "邀请码已过期"}), 400
    now = time.time()
    with verify_lock:
        if now - send_cooldowns.get(email, 0) < SEND_CODE_COOLDOWN:
            remain = int(SEND_CODE_COOLDOWN - (now - send_cooldowns.get(email, 0)))
            return jsonify({"error": f"发送太频繁，请 {remain} 秒后重试"}), 429
    code = f"{secrets.randbelow(1000000):06d}"
    body = (
        "<h3>自动传单派发 · 注册验证码</h3>"
        f"<p>你的验证码是：<b style='font-size:22px'>{code}</b></p>"
        "<p>10 分钟内有效，请勿泄露给他人。</p>"
        "<p>如非本人操作，请忽略本邮件。</p>")
    ok, err = send_mail(email, "【自动传单派发】注册验证码", body)
    if not ok:
        logger.error("验证码邮件发送失败（%s）：%s", email, err)
        return jsonify({"error": f"验证码发送失败（{err}），请稍后重试"}), 500
    with verify_lock:
        verify_codes[email] = {"code": code, "expires_at": now + VERIFY_CODE_TTL,
                               "invite_code": invite_code}
        send_cooldowns[email] = now
    logger.info("已向 %s 发送注册验证码", email)
    return jsonify({"message": f"验证码已发送至 {email}（10 分钟内有效）"})


@app.post("/api/register")
def api_register():
    """第二步：邮箱 + 验证码 + 邀请码 完成注册（验证通过即创建账号，无需再激活）。"""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    invite_code = str(data.get("invite_code", "")).strip()
    code = str(data.get("code", "")).strip()
    if not EMAIL_RE.match(email):
        return jsonify({"error": "邮箱格式不正确"}), 400
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({"error": f"密码至少 {MIN_PASSWORD_LEN} 位"}), 400
    if not re.fullmatch(r"\d{6}", code):
        return jsonify({"error": "验证码为 6 位数字"}), 400
    with verify_lock:
        vc = verify_codes.get(email)
        if not vc:
            return jsonify({"error": "请先点击「发送验证码」"}), 400
        if vc["code"] != code:
            return jsonify({"error": "验证码不正确"}), 400
        if time.time() > float(vc["expires_at"]):
            verify_codes.pop(email, None)
            return jsonify({"error": "验证码已过期，请重新获取"}), 400
        if vc["invite_code"] != invite_code:
            return jsonify({"error": "邀请码与验证码不匹配（请重新发送验证码）"}), 400
        verify_codes.pop(email, None)            # 验证码一次性使用
    users = load_users()
    if email in users:
        return jsonify({"error": "该邮箱已注册"}), 409
    invites = load_invites()
    inv = invites.get(invite_code)
    if not inv or inv.get("used_by"):
        return jsonify({"error": "邀请码无效或已被使用"}), 400
    if inv.get("expires_at") and time.time() > float(inv["expires_at"]):
        return jsonify({"error": "邀请码已过期"}), 400

    # 端口分配：base + idx（config.json docker 段为活配置）
    sys_cfg = load_sys_config()
    docker_cfg = sys_cfg.get("docker", {})
    idx = max((int(u.get("idx", 0)) for u in users.values()), default=0) + 1
    entry = {
        "password": generate_password_hash(password, method="pbkdf2:sha256:600000"),
        "role": "user",
        "active": True,                          # 验证码通过即激活
        "created_at": int(time.time()),
        "idx": idx,
        "ws_port": int(docker_cfg.get("ws_base_port", 8082)) + idx,
        "webui_port": int(docker_cfg.get("webui_base_port", 6098)) + idx,
        "ws_access_token": secrets.token_urlsafe(32),
        "webui_token": secrets.token_urlsafe(16),
        "webui_jwt_key": secrets.token_urlsafe(16),
        "qq_number": None,
        "bound": False,
        "container": container_mgr.container_name(email),
    }
    u_dir = user_dir(email)
    try:
        u_dir.mkdir(parents=True, exist_ok=True)
        (u_dir / "images").mkdir(exist_ok=True)
        (u_dir / "config.json").write_text(
            json.dumps(NEW_USER_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as e:
        return jsonify({"error": f"用户目录创建失败：{e}"}), 500
    users[email] = entry
    ok, err = save_users(users)
    if not ok:
        shutil.rmtree(u_dir, ignore_errors=True)
        return jsonify({"error": err}), 500
    inv["used_by"] = email                       # 消耗邀请码（一次性）
    inv["used_at"] = int(time.time())
    save_invites(invites)
    logger.info("新用户注册成功 %s（idx=%d）", email, idx)
    return jsonify({"message": "注册成功，请登录管理台按引导绑定 QQ 账号"}), 201


@app.get("/api/me")
def api_me():
    """当前登录用户信息（含自己绑定状态）；admin 额外返回全部用户列表（安全字段）。"""
    me = get_current_user()
    if not me:
        return jsonify({"error": "未登录或登录已过期"}), 401
    meta = load_users().get(me, {})
    target, _ = resolve_target()
    data = {"username": me, "role": meta.get("role", "user"),
            "target": target or me,
            "bound": bool(meta.get("bound")),
            "qq_number": meta.get("qq_number")}
    if meta.get("role") == "admin":
        data["users"] = user_list()
    return jsonify(data)


@app.post("/api/admin/switch")
def api_admin_switch():
    """admin 切换操作目标用户（仅内存 session，登出失效）。"""
    me = get_current_user()
    if not me:
        return jsonify({"error": "未登录或登录已过期"}), 401
    if load_users().get(me, {}).get("role") != "admin":
        return jsonify({"error": "仅管理员可切换目标用户"}), 403
    data = request.get_json(silent=True) or {}
    target = str(data.get("username", "")).strip()
    if target not in load_users():
        return jsonify({"error": f"用户 {target} 不存在"}), 404
    token = _current_token()
    with sessions_lock:
        if sessions.get(token):
            sessions[token]["target"] = target
    logger.info("管理员 %s 切换到目标用户 %s", me, target)
    return jsonify({"message": f"已切换到用户 {target}", "target": target})


# ─────────────────────────── 路由：状态与触发 ───────────────────────────

@app.get("/api/status")
def api_status():
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401

    # 从 users.json 读取引擎 disabled 状态
    users = load_users()
    user_meta = users.get(target, {})
    engine_disabled = user_meta.get("disabled", False)

    sf = user_status_file(target)
    if not sf.exists():
        return jsonify({"server_online": False, "engine_disabled": engine_disabled})

    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return jsonify({"server_online": False, "engine_disabled": engine_disabled})

    data["engine_disabled"] = engine_disabled

    # 引擎被禁用时强制离线
    if engine_disabled:
        data["server_online"] = False
        data["napcat_online"] = False
    elif time.time() - float(data.get("timestamp", 0)) > STATUS_STALE_SECONDS:
        data["server_online"] = False
    else:
        data["server_online"] = True
    return jsonify(data)


@app.post("/api/trigger")
def api_trigger():
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    data = request.get_json(silent=True) or {}
    try:
        gid = int(data.get("group_id", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "群号不合法"}), 400
    if gid <= 0:
        return jsonify({"error": "缺少 group_id"}), 400
    # 检查发送端（NapCat）是否在线（读该用户状态文件）
    try:
        status = json.loads(user_status_file(target).read_text(encoding="utf-8"))
        if not status.get("sender_online"):
            return jsonify({"error": "发送端未在线（NapCat 未连接），无法发送"}), 409
    except (json.JSONDecodeError, OSError):
        return jsonify({"error": "核心服务状态不可用（未运行？）"}), 503
    try:
        user_trigger_file(target).write_text(json.dumps(
            {"group_id": gid, "timestamp": time.time()}, ensure_ascii=False),
            encoding="utf-8")
    except OSError as e:
        return jsonify({"error": f"触发指令写入失败：{e}"}), 500
    return jsonify({"message": f"已下发触发指令（群 {gid}），约 1 秒内执行"}), 202


# ─────────────────────────── 路由：引擎开关 ───────────────────────────

@app.post("/api/engine/toggle")
def api_engine_toggle():
    """切换当前目标用户的引擎开关状态（users.json 中 disabled 字段）。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401

    data = request.get_json(silent=True) or {}
    disabled = data.get("disabled", True)
    if not isinstance(disabled, bool):
        return jsonify({"error": "disabled 必须为布尔值"}), 400

    users = load_users()
    if target not in users:
        return jsonify({"error": "用户不存在"}), 404

    if disabled == users[target].get("disabled", False):
        status_text = "已关闭" if disabled else "已开启"
        return jsonify({"message": f"引擎当前已是{status_text}状态，无需重复操作", "disabled": disabled})

    users[target]["disabled"] = disabled
    ok, err = save_users(users)
    if not ok:
        return jsonify({"error": err}), 500

    status_text = "关闭" if disabled else "开启"
    return jsonify({
        "message": f"引擎已{status_text}，约 10 秒内生效",
        "disabled": disabled
    })


# ─────────────────────────── 路由：配置 ───────────────────────────

@app.get("/api/config")
def api_config_get():
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    cfg = load_user_config(target)
    # 脱敏：不返回 onebot/control 等内部段
    return jsonify({
        "global": cfg.get("global", {}),
        "message": cfg.get("message", {}),
        "groups": cfg.get("groups", {}),
        "categories": cfg.get("categories", {}),
    })


@app.put("/api/config")
def api_config_put():
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    data = request.get_json(silent=True) or {}
    cfg = load_user_config(target)

    # 白名单字段合并（绝不触碰 onebot 等段）
    if "global" in data:
        g = data["global"]
        for key in ("message_threshold", "send_interval"):
            if key in g:
                try:
                    if int(g[key]) <= 0:
                        return jsonify({"error": f"{key} 必须为正整数"}), 400
                    cfg.setdefault("global", {})[key] = int(g[key])
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} 必须为正整数"}), 400
        if "count_own_messages" in g:
            cfg.setdefault("global", {})["count_own_messages"] = bool(g["count_own_messages"])
    if "message" in data:
        msg = data["message"]
        if "template" in msg:
            cfg.setdefault("message", {})["template"] = str(msg["template"])
        if "default_image" in msg:
            di = msg["default_image"]
            if di not in (None, ""):
                images = cfg.setdefault("message", {}).get("images", [])
                if di not in images:
                    return jsonify({"error": f"图片「{di}」不在图片清单中"}), 400
            cfg.setdefault("message", {})["default_image"] = di or None

    ok, err = save_user_config(target, cfg)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"message": "已保存，服务器约 10 秒内自动加载"})


# ─────────────────────────── 路由：空间监控（qzone） ───────────────────────────

QZONE_DEFAULT = NEW_USER_CONFIG["qzone"]


def _mask_api_key(key: str) -> str:
    if not key:
        return ""
    return key[:6] + "***" if len(key) > 6 else "***"


def _validate_qzone(data: dict) -> tuple[bool, str]:
    """校验空间监控配置。"""
    if "enabled" in data and not isinstance(data["enabled"], bool):
        return False, "enabled 必须为布尔值"
    if "poll_interval" in data:
        try:
            if int(data["poll_interval"]) < 15:
                return False, "poll_interval 必须 ≥ 15 秒"
        except (TypeError, ValueError):
            return False, "poll_interval 必须为数字"
    if "monitor_list" in data:
        if not isinstance(data["monitor_list"], list):
            return False, "monitor_list 必须为数组"
        for m in data["monitor_list"]:
            uin = str(m.get("uin", "")).strip()
            if not uin.isdigit() or len(uin) < 5:
                return False, f"监控目标 QQ 号不合法：{m.get('uin', '')}"
    if "keywords" in data and not isinstance(data["keywords"], list):
        return False, "keywords 必须为数组"
    if "actions" in data:
        act = data["actions"]
        for key in ("forward", "like", "comment"):
            if key in act and not isinstance(act[key], bool):
                return False, f"actions.{key} 必须为布尔值"
        if "comment_mode" in act and act["comment_mode"] not in ("template", "ai"):
            return False, "comment_mode 必须是 template 或 ai"
    if "forward_list" in data:
        if not isinstance(data["forward_list"], list):
            return False, "forward_list 必须为数组"
        for f in data["forward_list"]:
            if f.get("type") not in ("group", "private"):
                return False, "转发目标 type 必须是 group 或 private"
            if not str(f.get("id", "")).strip().isdigit():
                return False, f"转发目标 ID 不合法：{f.get('id', '')}"
    if "manual_mode" in data and not isinstance(data["manual_mode"], bool):
        return False, "manual_mode 必须为布尔值"
    if "max_posts" in data:
        try:
            if int(data["max_posts"]) < 10:
                return False, "max_posts 必须 ≥ 10"
        except (TypeError, ValueError):
            return False, "max_posts 必须为数字"
    if "action_delay" in data:
        ad = data["action_delay"]
        if not isinstance(ad, dict):
            return False, "action_delay 必须为对象"
        for key in ("min", "max"):
            if key in ad:
                try:
                    if float(ad[key]) <= 0:
                        return False, f"action_delay.{key} 必须为正数"
                except (TypeError, ValueError):
                    return False, f"action_delay.{key} 必须为数字"
    return True, ""


@app.get("/api/qzone")
def api_qzone_get():
    """读取当前目标用户的空间监控配置（ai.api_key 脱敏）。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    cfg = load_user_config(target).get("qzone") or {}
    merged = {**QZONE_DEFAULT, **cfg}
    merged["actions"] = {**QZONE_DEFAULT["actions"], **(cfg.get("actions") or {})}
    merged["comment_templates"] = cfg.get("comment_templates") or QZONE_DEFAULT["comment_templates"]
    merged["forward_list"] = cfg.get("forward_list") or []
    merged["monitor_list"] = cfg.get("monitor_list") or []
    merged["keywords"] = cfg.get("keywords") or []
    merged["ai"] = {**QZONE_DEFAULT["ai"], **(cfg.get("ai") or {})}
    merged["ai"]["api_key"] = _mask_api_key(merged["ai"].get("api_key", ""))
    return jsonify(merged)


@app.put("/api/qzone")
def api_qzone_put():
    """保存当前目标用户的空间监控配置（整段替换 qzone 键）。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    data = request.get_json(silent=True) or {}
    ok, err = _validate_qzone(data)
    if not ok:
        return jsonify({"error": err}), 400

    cfg = load_user_config(target)
    old = cfg.get("qzone") or {}
    new = {**QZONE_DEFAULT, **data}
    # ai.api_key 脱敏标记保留旧值
    if "ai" in data:
        new_ai = {**QZONE_DEFAULT["ai"], **data["ai"]}
        if str(new_ai.get("api_key", "")).startswith("***"):
            new_ai["api_key"] = (old.get("ai") or {}).get("api_key", "")
        new["ai"] = new_ai
    # 保持 actions 结构完整
    new["actions"] = {**QZONE_DEFAULT["actions"], **(data.get("actions") or {})}
    cfg["qzone"] = new
    ok, err = save_user_config(target, cfg)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"message": "空间监控配置已保存，约 10 秒内自动生效"})


@app.post("/api/qzone/test")
def api_qzone_test_post():
    """下发空间监控连接测试（写 pending_qzone_test.json，server 端执行）。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    try:
        user_dir(target).joinpath("pending_qzone_test.json").write_text(
            json.dumps({"timestamp": time.time()}, ensure_ascii=False),
            encoding="utf-8")
    except OSError as e:
        return jsonify({"error": f"测试指令写入失败：{e}"}), 500
    return jsonify({"message": "已下发测试指令，约 2 秒内出结果"})


@app.get("/api/qzone/test")
def api_qzone_test_get():
    """读取空间监控连接测试结果（qzone_test_result.json，无结果返回 404）。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    f = user_dir(target).joinpath("qzone_test_result.json")
    if not f.exists():
        return jsonify({"pending": True, "message": "尚未有测试结果，请先点击「测试连接」"})
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return jsonify({"error": "测试结果读取失败"}), 500
    if time.time() - float(data.get("checked_at", 0)) > 300:
        return jsonify({"pending": True, "message": "测试结果已过期，请重新测试"})
    return jsonify(data)


@app.post("/api/qzone/execute")
def api_qzone_execute():
    """手动执行待处理动态（body: {tid: "可选，缺省=全部"}）。

    写 pending_qzone_execute.json，server 端按设定动作执行，
    条间随机延时防风控。
    """
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    data = request.get_json(silent=True) or {}
    tid = str(data.get("tid", "")).strip()
    if tid and not tid.isalnum():
        return jsonify({"error": "tid 不合法"}), 400
    try:
        user_dir(target).joinpath("pending_qzone_execute.json").write_text(
            json.dumps({"tid": tid or None, "timestamp": time.time()},
                       ensure_ascii=False),
            encoding="utf-8")
    except OSError as e:
        return jsonify({"error": f"执行指令写入失败：{e}"}), 500
    msg = f"已下发执行指令（{'全部待执行动态' if not tid else 'tid=' + tid}），约 1 秒内开始"
    return jsonify({"message": msg})


# ─────────────────────────── 路由：Qzone 扫码登录（完整 Cookies） ───────────────────────────

# NapCat get_cookies 只返回 ptlogin 域凭证，h5 写接口（点赞/评论）需要
# qzone.qq.com 域凭证（qzone_token/p_skey）——通过 ptlogin2 扫码获取完整
# Cookies 并持久化到 users/<用户名>/qzone_cookies.json，server 优先使用。

QZONE_COOKIES_FILE = "qzone_cookies.json"


@app.post("/api/qzone/login/qrcode")
def api_qzone_login_qrcode():
    """获取 Qzone 扫码登录二维码（ptlogin2）。"""
    me = get_current_user()
    if not me:
        return jsonify({"error": "未登录或登录已过期"}), 401
    qr = QRLogin()
    img = qr.get_qrcode()
    if img is None:
        return jsonify({"error": "获取二维码失败（网络或 QQ 服务器异常）"}), 500
    import base64 as _b64
    data_uri = "data:image/png;base64," + _b64.b64encode(img).decode("utf-8")
    with qzone_login_lock:
        qzone_login_states[me] = {"qr": qr, "started_at": time.time()}
    return jsonify({"success": True, "qrcode": data_uri})


@app.get("/api/qzone/login/poll")
def api_qzone_login_poll():
    """轮询扫码登录状态；成功后将完整 Cookies 持久化。"""
    me = get_current_user()
    if not me:
        return jsonify({"error": "未登录或登录已过期"}), 401
    with qzone_login_lock:
        state = qzone_login_states.get(me)
    if not state:
        return jsonify({"success": False, "logged_in": False,
                        "status": "unknown", "message": "未开始扫码"})
    qr = state["qr"]
    result = qr.poll_login()

    if result.get("status") == "success" and result.get("cookies"):
        cookies = result["cookies"]
        uin = result.get("final_uin") or result.get("uin", "")
        try:
            f = user_dir(me).joinpath(QZONE_COOKIES_FILE)
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "cookies": cookies, "uin": uin,
                "fetched_at": time.time(),
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, f)
        except OSError as e:
            return jsonify({"error": f"Cookies 保存失败：{e}"}), 500
        with qzone_login_lock:
            qzone_login_states.pop(me, None)
        logger.info("用户 %s Qzone 扫码登录成功（%s），Cookies 已保存", me, uin)
        return jsonify({"success": True, "logged_in": True, "uin": uin,
                        "message": "登录成功，Qzone 完整凭证已保存"})

    return jsonify({"success": True, "logged_in": False,
                    "status": result.get("status", "unknown"),
                    "message": result.get("message", "")})


@app.get("/api/qzone/login/status")
def api_qzone_login_status():
    """当前 Qzone 扫码凭证状态（是否已保存/是否过期）。"""
    me = get_current_user()
    if not me:
        return jsonify({"error": "未登录或登录已过期"}), 401
    f = user_dir(me).joinpath(QZONE_COOKIES_FILE)
    if not f.exists():
        return jsonify({"logged_in": False, "message": "未登录（Qzone 完整凭证）"})
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return jsonify({"logged_in": False, "message": "凭证文件损坏，请重新扫码"})
    fetched = float(data.get("fetched_at", 0))
    remain = 12 * 3600 - (time.time() - fetched)
    if remain <= 0:
        return jsonify({"logged_in": False, "message": "凭证已过期，请重新扫码"})
    return jsonify({
        "logged_in": True,
        "uin": data.get("uin", ""),
        "message": f"已登录（剩余有效约 {int(remain // 3600)} 小时）",
    })


# ─────────────────────────── 路由：群管理 ───────────────────────────

@app.post("/api/groups")
def api_groups_post():
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    data = request.get_json(silent=True) or {}
    ok, err = validate_group(data)
    if not ok:
        return jsonify({"error": err}), 400
    gid = str(int(data["group_id"]))
    cfg = load_user_config(target)
    if gid in cfg.get("groups", {}):
        return jsonify({"error": f"群 {gid} 已存在"}), 409
    entry = {"name": str(data["name"]).strip()}
    for key in ("message_threshold", "send_interval", "template", "image", "category"):
        if data.get(key) not in (None, ""):
            entry[key] = data[key]
    if "enabled" in data:                        # 发送开关（默认 true）
        entry["enabled"] = bool(data["enabled"])
    cfg.setdefault("groups", {})[gid] = entry
    ok, err = save_user_config(target, cfg)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"message": f"群 {gid} 已添加，服务器约 10 秒内自动加载"}), 201


@app.put("/api/groups/<gid>")
def api_groups_put(gid):
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    data = request.get_json(silent=True) or {}
    cfg = load_user_config(target)
    groups = cfg.setdefault("groups", {})
    if gid not in groups:
        return jsonify({"error": f"群 {gid} 不存在"}), 404
    entry = groups[gid]
    if "name" in data:
        if not str(data["name"]).strip():
            return jsonify({"error": "群名不能为空"}), 400
        entry["name"] = str(data["name"]).strip()
    if "enabled" in data:                      # 发送开关（布尔）
        if not isinstance(data["enabled"], bool):
            return jsonify({"error": "enabled 必须为布尔值"}), 400
        entry["enabled"] = data["enabled"]
    for key in ("message_threshold", "send_interval", "template", "image", "category"):
        if key in data:
            val = data[key]
            if val in (None, ""):
                entry.pop(key, None)          # 清空 = 回退全局
            elif key in ("message_threshold", "send_interval"):
                try:
                    if int(val) <= 0:
                        return jsonify({"error": f"{key} 必须为正整数"}), 400
                    entry[key] = int(val)
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} 必须为正整数"}), 400
            else:
                entry[key] = val
    ok, err = save_user_config(target, cfg)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"message": f"群 {gid} 已更新，服务器约 10 秒内自动加载"})


@app.delete("/api/groups/<gid>")
def api_groups_delete(gid):
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    cfg = load_user_config(target)
    groups = cfg.get("groups", {})
    if gid not in groups:
        return jsonify({"error": f"群 {gid} 不存在"}), 404
    del groups[gid]
    ok, err = save_user_config(target, cfg)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"message": f"群 {gid} 已删除，服务器约 10 秒内自动加载"})


# ─────────────────────────── 路由：群分类管理 ───────────────────────────

@app.post("/api/categories")
def api_categories_post():
    """创建分类。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "分类名称不能为空"}), 400

    cfg = load_user_config(target)
    categories = cfg.setdefault("categories", {})

    # 生成唯一 ID（6 字符 URL-safe）
    cat_id = secrets.token_urlsafe(4)
    while cat_id in categories:
        cat_id = secrets.token_urlsafe(4)

    entry = {
        "name": name,
        "enabled": data.get("enabled", True) if isinstance(data.get("enabled"), bool) else True,
    }
    for key in ("message_threshold", "send_interval"):
        val = data.get(key)
        if val not in (None, ""):
            try:
                if int(val) <= 0:
                    return jsonify({"error": f"{key} 必须为正整数"}), 400
                entry[key] = int(val)
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} 必须为正整数"}), 400

    categories[cat_id] = entry
    ok, err = save_user_config(target, cfg)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"message": f"分类「{name}」已创建", "id": cat_id}), 201


@app.put("/api/categories/<cat_id>")
def api_categories_put(cat_id):
    """更新分类。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    data = request.get_json(silent=True) or {}
    cfg = load_user_config(target)
    categories = cfg.setdefault("categories", {})
    if cat_id not in categories:
        return jsonify({"error": "分类不存在"}), 404

    entry = categories[cat_id]
    if "name" in data:
        name = str(data["name"]).strip()
        if not name:
            return jsonify({"error": "分类名称不能为空"}), 400
        entry["name"] = name
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            return jsonify({"error": "enabled 必须为布尔值"}), 400
        entry["enabled"] = data["enabled"]
    for key in ("message_threshold", "send_interval"):
        if key in data:
            val = data[key]
            if val in (None, ""):
                entry.pop(key, None)
            else:
                try:
                    if int(val) <= 0:
                        return jsonify({"error": f"{key} 必须为正整数"}), 400
                    entry[key] = int(val)
                except (TypeError, ValueError):
                    return jsonify({"error": f"{key} 必须为正整数"}), 400

    ok, err = save_user_config(target, cfg)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"message": f"分类「{entry['name']}」已更新"})


@app.delete("/api/categories/<cat_id>")
def api_categories_delete(cat_id):
    """删除分类，同时清理所有群对该分类的引用。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    cfg = load_user_config(target)
    categories = cfg.get("categories", {})
    if cat_id not in categories:
        return jsonify({"error": "分类不存在"}), 404

    name = categories[cat_id].get("name", cat_id)
    del categories[cat_id]

    # 清理群引用
    for g_entry in cfg.get("groups", {}).values():
        if g_entry.get("category") == cat_id:
            g_entry.pop("category", None)

    ok, err = save_user_config(target, cfg)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"message": f"分类「{name}」已删除"})


@app.post("/api/categories/<cat_id>/toggle")
def api_categories_toggle(cat_id):
    """切换分类开关，同时影响分类下所有群。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled 必须为布尔值"}), 400

    cfg = load_user_config(target)
    categories = cfg.get("categories", {})
    if cat_id not in categories:
        return jsonify({"error": "分类不存在"}), 404

    # 更新分类自身开关
    categories[cat_id]["enabled"] = enabled

    # 同步更新分类下所有群
    count = 0
    for g_entry in cfg.get("groups", {}).values():
        if g_entry.get("category") == cat_id:
            g_entry["enabled"] = enabled
            count += 1

    ok, err = save_user_config(target, cfg)
    if not ok:
        return jsonify({"error": err}), 500
    action = "启用" if enabled else "停用"
    return jsonify({"message": f"分类已{action}，影响 {count} 个群，服务器约 10 秒内生效"})


@app.post("/api/categories/<cat_id>/apply")
def api_categories_apply(cat_id):
    """将分类的默认设置应用到分类下所有群。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    cfg = load_user_config(target)
    categories = cfg.get("categories", {})
    if cat_id not in categories:
        return jsonify({"error": "分类不存在"}), 404

    cat = categories[cat_id]
    applied = 0
    for g_entry in cfg.get("groups", {}).values():
        if g_entry.get("category") == cat_id:
            for key in ("message_threshold", "send_interval"):
                if key in cat:
                    g_entry[key] = cat[key]
            applied += 1

    ok, err = save_user_config(target, cfg)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"message": f"分类设置已应用到 {applied} 个群，服务器约 10 秒内生效"})


# ─────────────────────────── 路由：图片 ───────────────────────────

@app.get("/api/images")
def api_images_list():
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    return jsonify({"images": scan_images(target, load_user_config(target))})


@app.post("/api/images")
def api_images_upload():
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "未选择文件"}), 400
    name = sanitize_filename(file.filename)
    if not name:
        return jsonify({"error": "文件名不合法"}), 400
    if Path(name).suffix.lower() not in ALLOWED_EXTS:
        return jsonify({"error": "仅支持图片格式：jpg/png/gif/webp/bmp"}), 400
    img_dir = user_images_dir(target)
    img_dir.mkdir(parents=True, exist_ok=True)
    if file.content_length and file.content_length > MAX_IMAGE_SIZE:
        return jsonify({"error": "图片不能超过 10MB"}), 400
    try:
        file.save(img_dir / name)
    except OSError as e:
        return jsonify({"error": f"保存失败：{e}"}), 500
    if (img_dir / name).stat().st_size > MAX_IMAGE_SIZE:
        (img_dir / name).unlink(missing_ok=True)
        return jsonify({"error": "图片不能超过 10MB"}), 400
    # 加入配置清单（去重）
    cfg = load_user_config(target)
    images = cfg.setdefault("message", {}).setdefault("images", [])
    if name not in images:
        images.append(name)
        save_user_config(target, cfg)
    return jsonify({"message": f"已上传：{name}（已加入图片清单）"}), 201


@app.delete("/api/images/<path:filename>")
def api_images_delete(filename):
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    name = Path(filename).name
    cfg = load_user_config(target)
    # 检查是否被引用
    if cfg.get("message", {}).get("default_image") == name:
        return jsonify({"error": f"「{name}」是默认图片，请先取消默认后再删除"}), 400
    for gid, g in cfg.get("groups", {}).items():
        if g.get("image") == name:
            return jsonify({"error": f"「{name}」被群 {gid} 指定，请先取消后再删除"}), 400
    try:
        (user_images_dir(target) / name).unlink(missing_ok=True)
    except OSError as e:
        return jsonify({"error": f"删除失败：{e}"}), 500
    images = cfg.get("message", {}).get("images", [])
    if name in images:
        images.remove(name)
        save_user_config(target, cfg)
    return jsonify({"message": f"已删除：{name}"})


@app.get("/api/images/<path:filename>")
def api_images_get(filename):
    """图片预览/下载（admin token）。"""
    target, _ = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    name = Path(filename).name
    img_dir = user_images_dir(target)
    if not img_dir.is_dir():
        return jsonify({"error": "图片目录不存在"}), 404
    return send_from_directory(img_dir, name, conditional=True)


# ─────────────────────────── 路由：日志 ───────────────────────────

@app.get("/api/logs")
def api_logs():
    target, me = resolve_target()
    if not target:
        return jsonify({"error": "未登录或登录已过期"}), 401
    # 按操作目标用户过滤（server_<user>_*.log）；admin 查看自己时附加 webapp 日志
    files = sorted([f.name for f in LOGS_DIR.glob(f"server_{target}_*.log")],
                   reverse=True)
    if target == me and load_users().get(me, {}).get("role") == "admin":
        files += sorted([f.name for f in LOGS_DIR.glob("webapp_*.log")],
                        reverse=True)
    fname = request.args.get("file", "")
    if fname:
        # 防路径穿越
        safe = Path(fname).name
        path = LOGS_DIR / safe
        if not path.is_file():
            return jsonify({"error": f"日志文件不存在：{safe}"}), 404
        try:
            size = path.stat().st_size
            offset = int(request.args.get("offset", 0))
            if offset < 0 or offset > size:
                offset = 0
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                content = f.read()
            return jsonify({"files": files, "file": safe, "content": content,
                            "next_offset": offset + len(content.encode("utf-8")),
                            "size": size})
        except OSError as e:
            return jsonify({"error": f"日志读取失败：{e}"}), 500
    return jsonify({"files": files})


# ─────────────────────────── 路由：admin 用户管理 ───────────────────────────

def require_admin() -> tuple[str, tuple | None]:
    """校验当前登录用户是 admin。

    返回 (用户名, None) 正常；未登录返回 401 响应，非 admin 返回 403 响应。
    """
    me = get_current_user()
    if not me:
        return "", (jsonify({"error": "未登录或登录已过期"}), 401)
    if load_users().get(me, {}).get("role") != "admin":
        return "", (jsonify({"error": "仅管理员可执行此操作"}), 403)
    return me, None


@app.get("/api/admin/users")
def api_admin_users_list():
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    return jsonify({"users": user_list()})


@app.post("/api/admin/users")
def api_admin_users_post():
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = str(data.get("role", "user"))
    try:
        container_mgr.check_username(username)
    except container_mgr.ContainerError as e:
        return jsonify({"error": str(e)}), 400
    users = load_users()
    if username in users:
        return jsonify({"error": f"用户 {username} 已存在"}), 409
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({"error": f"密码至少 {MIN_PASSWORD_LEN} 位"}), 400
    if role not in ("admin", "user"):
        return jsonify({"error": "角色只能是 admin 或 user"}), 400

    # 端口分配：base + idx（config.json docker 段为活配置）
    sys_cfg = load_sys_config()
    docker_cfg = sys_cfg.get("docker", {})
    idx = max((int(u.get("idx", 0)) for u in users.values()), default=0) + 1
    entry = {
        "password": generate_password_hash(password, method="pbkdf2:sha256:600000"),
        "role": role,
        "created_at": int(time.time()),
        "idx": idx,
        "ws_port": int(docker_cfg.get("ws_base_port", 8082)) + idx,
        "webui_port": int(docker_cfg.get("webui_base_port", 6098)) + idx,
        "ws_access_token": secrets.token_urlsafe(32),
        "webui_token": secrets.token_urlsafe(16),
        "webui_jwt_key": secrets.token_urlsafe(16),
        "qq_number": None,
        "bound": False,
        "container": container_mgr.container_name(username),
    }
    # 用户目录 + 模板配置 + 图片目录
    u_dir = user_dir(username)
    try:
        u_dir.mkdir(parents=True, exist_ok=True)
        (u_dir / "images").mkdir(exist_ok=True)
        (u_dir / "config.json").write_text(
            json.dumps(NEW_USER_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as e:
        return jsonify({"error": f"用户目录创建失败：{e}"}), 500
    users[username] = entry
    ok, err = save_users(users)
    if not ok:
        shutil.rmtree(u_dir, ignore_errors=True)
        return jsonify({"error": err}), 500
    logger.info("管理员 %s 创建用户 %s（idx=%d，ws_port=%d，webui_port=%d）",
                admin, username, idx, entry["ws_port"], entry["webui_port"])
    return jsonify({"message": f"用户 {username} 已创建，服务器约 10 秒内自动启动引擎"}), 201


@app.delete("/api/admin/users/<username>")
def api_admin_users_delete(username):
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    try:
        container_mgr.check_username(username)
    except container_mgr.ContainerError as e:
        return jsonify({"error": str(e)}), 400
    if username == admin:
        return jsonify({"error": "不能删除自己"}), 400
    users = load_users()
    if username not in users:
        return jsonify({"error": f"用户 {username} 不存在"}), 404
    container_mgr.remove_container(username)
    del users[username]
    ok, err = save_users(users)
    if not ok:
        return jsonify({"error": err}), 500
    shutil.rmtree(user_dir(username), ignore_errors=True)
    with bind_lock:
        bind_states.pop(username, None)
    logger.info("管理员 %s 删除用户 %s", admin, username)
    return jsonify({"message": f"用户 {username} 已删除（容器与数据已清理，引擎约 10 秒内停止）"})


@app.post("/api/admin/users/<username>/password")
def api_admin_users_password(username):
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({"error": f"密码至少 {MIN_PASSWORD_LEN} 位"}), 400
    users = load_users()
    if username not in users:
        return jsonify({"error": f"用户 {username} 不存在"}), 404
    users[username]["password"] = generate_password_hash(
        password, method="pbkdf2:sha256:600000")
    ok, err = save_users(users)
    if not ok:
        return jsonify({"error": err}), 500
    logger.info("管理员 %s 重置用户 %s 的密码", admin, username)
    return jsonify({"message": f"用户 {username} 的密码已更新"})


# ─────────────────────────── 路由：admin 绑定流程 ───────────────────────────

def bind_worker(username: str) -> None:
    """后台绑定线程：等扫码（onebot11 文件出现）→ 写反向 WS → 重启容器 → 更新账号库。

    全程 sleep 在后台线程，不阻塞 Flask 请求；进度写入 bind_states 供前端轮询。

    修复 2026-08-12：
    - 扫码后立即更新状态为「登录中」（不再卡在「等待扫码登录」）
    - 写反向 WS 后重启容器（NapCat 4.x 未必热加载 onebot11 配置变更）
      QQ 登录态保存在 napcat-<用户>/qq 卷中，重启不会丢失。
    """
    try:
        with bind_lock:
            bind_states[username] = {"status": "running",
                                     "message": "等待扫码登录（onebot11 配置生成）…"}
        # 自定义轮询：检测 onebot11 文件，同时监测 QR 码扫码状态
        deadline = time.time() + 600
        qr_ever_seen = False
        qr_scanned = False
        while time.time() < deadline:
            if container_mgr.find_onebot11_file(username) is not None:
                break
            # 检测 QR 码是否已被扫码：容器内的 qrcode.png 被 NapCat 删除 = 已扫码
            if not qr_scanned:
                qr = container_mgr.get_qrcode_image(username)
                if qr:
                    qr_ever_seen = True
                elif qr_ever_seen:
                    qr_scanned = True
                    with bind_lock:
                        bind_states[username] = {"status": "running",
                                                 "message": "⬆️ 二维码已扫码，等待 QQ 登录完成（约 10-30 秒）…"}
            time.sleep(3)
        else:
            with bind_lock:
                bind_states[username] = {"status": "timeout",
                                         "error": "扫码超时（10 分钟），请重试"}
            logger.warning("用户 %s 绑定超时（未检测到扫码登录）", username)
            return
        users = load_users()
        qq = container_mgr.write_reverse_ws(username, users[username])
        # 重启容器使反向 WS 配置生效（NapCat 4.x 未必热加载 onebot11 配置变更）
        # QQ 登录态保存在 napcat-<用户>/qq 卷中，重启不会丢失
        try:
            container_mgr.restart_container(username)
        except container_mgr.ContainerError as e:
            logger.warning("用户 %s 容器重启失败（绑定仍完成）：%s", username, e)
        # 重启后等待容器就绪，再备份登录态并启用自动登录
        try:
            # 等待容器重新启动
            time.sleep(5)
            container_mgr.backup_qq_session(username)
            container_mgr.set_auto_login(username, qq)
            logger.info("用户 %s QQ 登录态已备份，自动登录已启用", username)
        except Exception as e:
            logger.warning("用户 %s 登录态备份失败（不影响绑定）：%s", username, e)
        users[username]["qq_number"] = int(qq) if str(qq).isdigit() else None
        users[username]["bound"] = True
        ok, err = save_users(users)
        if not ok:
            raise RuntimeError(err)
        with bind_lock:
            bind_states[username] = {"status": "success", "qq_number": qq,
                                     "message": "绑定成功"}
        logger.info("用户 %s 绑定成功（QQ %s）", username, qq)
    except container_mgr.ContainerError as e:
        with bind_lock:
            bind_states[username] = {"status": "error", "error": str(e)}
        logger.error("用户 %s 绑定失败：%s", username, e)
    except Exception as e:
        with bind_lock:
            bind_states[username] = {"status": "error", "error": str(e)[:200]}
        logger.error("用户 %s 绑定异常：%s", username, e)


def _start_bind(username: str) -> tuple[dict, int]:
    """公共绑定启动：ensure_container + 后台线程。返回 (响应体, 状态码)。"""
    users = load_users()
    meta = users.get(username)
    if not meta:
        return {"error": f"用户 {username} 不存在"}, 404
    with bind_lock:
        st = bind_states.get(username, {})
        if st.get("status") == "running":
            # 幂等：流程进行中，直接返回当前进度
            return {"status": "running", "message": st.get("message"),
                    "webui_url": get_webui_url(username, meta),
                    "token": container_mgr.get_webui_token(username, meta)}, 202
    try:
        container_mgr.ensure_container(username, meta, load_sys_config())
    except container_mgr.ContainerError as e:
        return {"error": f"容器创建失败：{e}"}, 500
    with bind_lock:
        bind_states[username] = {"status": "running"}
    threading.Thread(target=bind_worker, args=(username,), daemon=True).start()
    return {
        "status": "running",
        "webui_url": get_webui_url(username, meta),
        "token": container_mgr.get_webui_token(username, meta),
        "message": "请在 WebUI 扫码登录，本页将自动检测完成",
    }, 202


@app.post("/api/admin/users/<username>/bind")
def api_admin_users_bind(username):
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    try:
        container_mgr.check_username(username)
    except container_mgr.ContainerError as e:
        return jsonify({"error": str(e)}), 400
    body, code = _start_bind(username)
    return jsonify(body), code


# ─────────────────────────── 路由：用户自助绑定 ───────────────────────────

@app.post("/api/bind")
def api_bind_self():
    """当前登录用户绑定自己的 QQ（ensure_container → WebUI 扫码 → 自动完成）。"""
    me = get_current_user()
    if not me:
        return jsonify({"error": "未登录或登录已过期"}), 401
    body, code = _start_bind(me)
    return jsonify(body), code


@app.get("/api/bind")
def api_bind_self_status():
    """当前登录用户绑定流程状态（前端轮询）。"""
    me = get_current_user()
    if not me:
        return jsonify({"error": "未登录或登录已过期"}), 401
    with bind_lock:
        st = bind_states.get(me, {"status": "idle"})
    return jsonify(st)


@app.get("/api/bind/qrcode")
def api_bind_qrcode():
    """当前登录用户的 NapCat 登录二维码（base64 图片，前端展示供手机 QQ 扫码）。

    扫码不依赖 NapCat WebUI（4.x WebUI 端口固定 6099，多容器冲突），
    直接读容器内 qrcode.png，绕开端口问题。
    """
    me = get_current_user()
    if not me:
        return jsonify({"error": "未登录或登录已过期"}), 401
    img = container_mgr.get_qrcode_image(me)
    if not img:
        return jsonify({"qr": None,
                        "message": "二维码暂不可用（容器未就绪或已登录？）"})
    return jsonify({"qr": f"data:image/png;base64,{img}"})


def _napcat_refresh_qrcode(username: str) -> tuple[bool, str]:
    """调用 NapCat WebUI 的 RefreshQRcode 接口立即换新二维码。

    两步：POST /api/auth/login {hash: sha256(webui_token + ".napcat")}（白名单接口）
    换取 1 小时有效凭据 → Bearer 凭据调 /api/QQLogin/RefreshQRcode。
    仅对当前占用 6099 端口的容器生效；若目标容器未占用 6099（多容器端口冲突），
    NapCat 返回 Unauthorized，调用方提示用户等待自动换新。
    """
    users = load_users()
    meta = users.get(username)
    if not meta:
        return False, "用户不存在"
    token = meta.get("webui_token", "")
    if not token:
        return False, "该用户未配置 WebUI token"
    try:
        # 1) 换取凭据（auth/login 为白名单接口，无需预认证）
        hash_val = hashlib.sha256(f"{token}.napcat".encode()).hexdigest()
        req_login = urllib.request.Request(
            "http://127.0.0.1:6099/api/auth/login",
            data=json.dumps({"hash": hash_val}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req_login, timeout=10) as resp:
            body = json.loads(resp.read().decode() or "{}")
        if body.get("code") != 0:
            msg = str(body.get("message", "未知错误"))
            if "Unauthorized" in msg or "invalid" in msg:
                return False, "该用户容器未占用 WebUI 端口（多容器端口冲突），请等待自动换新"
            return False, f"NapCat 登录失败：{msg}"
        cred = (body.get("data") or {}).get("Credential", "")
        if not cred:
            return False, "NapCat 未返回凭据"
        # 2) 触发二维码刷新
        req_refresh = urllib.request.Request(
            "http://127.0.0.1:6099/api/QQLogin/RefreshQRcode",
            data=b"{}",
            headers={"Authorization": f"Bearer {cred}",
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req_refresh, timeout=10) as resp:
            body = json.loads(resp.read().decode() or "{}")
        if body.get("code") == 0:
            return True, "已触发 NapCat 刷新，新二维码即将生成"
        msg = str(body.get("message", "未知错误"))
        if "Unauthorized" in msg:
            return False, "该用户容器未占用 WebUI 端口（多容器端口冲突），请等待自动换新"
        return False, f"NapCat 返回：{msg}"
    except Exception as e:
        return False, f"调用失败：{e}"


@app.post("/api/bind/qrcode/refresh")
def api_bind_qrcode_refresh():
    """强制刷新登录二维码：调 NapCat RefreshQRcode 接口立即换新。"""
    me = get_current_user()
    if not me:
        return jsonify({"error": "未登录或登录已过期"}), 401
    ok, msg = _napcat_refresh_qrcode(me)
    if not ok:
        return jsonify({"error": msg}), 400
    return jsonify({"message": msg})


@app.get("/api/admin/users/<username>/qrcode")
def api_admin_users_qrcode(username):
    """admin 查看指定用户（绑定流程中）的登录二维码。"""
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    try:
        container_mgr.check_username(username)
    except container_mgr.ContainerError as e:
        return jsonify({"error": str(e)}), 400
    if username not in load_users():
        return jsonify({"error": f"用户 {username} 不存在"}), 404
    img = container_mgr.get_qrcode_image(username)
    if not img:
        return jsonify({"qr": None,
                        "message": "二维码暂不可用（容器未就绪或已登录？）"})
    return jsonify({"qr": f"data:image/png;base64,{img}"})


@app.post("/api/admin/users/<username>/qrcode/refresh")
def api_admin_users_qrcode_refresh(username):
    """admin 强制刷新指定用户的登录二维码（重启容器）。"""
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    try:
        container_mgr.check_username(username)
    except container_mgr.ContainerError as e:
        return jsonify({"error": str(e)}), 400
    if username not in load_users():
        return jsonify({"error": f"用户 {username} 不存在"}), 404
    ok, msg = _napcat_refresh_qrcode(username)
    if not ok:
        return jsonify({"error": msg}), 400
    return jsonify({"message": msg})


@app.get("/api/admin/users/<username>/bind")
def api_admin_users_bind_status(username):
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    with bind_lock:
        st = bind_states.get(username, {"status": "idle"})
    return jsonify(st)


@app.post("/api/admin/users/<username>/unbind")
def api_admin_users_unbind(username):
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    try:
        container_mgr.check_username(username)
    except container_mgr.ContainerError as e:
        return jsonify({"error": str(e)}), 400
    users = load_users()
    if username not in users:
        return jsonify({"error": f"用户 {username} 不存在"}), 404
    container_mgr.remove_container(username)
    users[username]["bound"] = False
    users[username]["qq_number"] = None
    ok, err = save_users(users)
    if not ok:
        return jsonify({"error": err}), 500
    with bind_lock:
        bind_states.pop(username, None)
    logger.info("管理员 %s 解绑用户 %s（容器已删除，可重新绑定）", admin, username)
    return jsonify({"message": "已解绑（容器已删除，可重新绑定；登录态保留在 napcat-<用户>/qq 卷）"})


# ─────────────────────────── QQ 登出（admin / 自助） ───────────────────────────


@app.post("/api/admin/users/<username>/qq-logout")
def api_admin_users_qq_logout(username):
    """管理员：登出指定用户的 QQ，清除登录态并禁用自动登录。"""
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    try:
        container_mgr.check_username(username)
    except container_mgr.ContainerError as e:
        return jsonify({"error": str(e)}), 400
    users = load_users()
    if username not in users:
        return jsonify({"error": f"用户 {username} 不存在"}), 404
    if not users[username].get("bound"):
        return jsonify({"error": f"用户 {username} 尚未绑定 QQ"}), 400
    try:
        container_mgr.logout_qq(username)
        logger.info("管理员 %s 已登出用户 %s 的 QQ", admin, username)
        return jsonify({"message": "QQ 已登出，容器已重启（自动登录已禁用）"})
    except container_mgr.ContainerError as e:
        return jsonify({"error": f"QQ 登出失败：{e}"}), 500


@app.post("/api/qq-logout")
def api_qq_logout_self():
    """当前登录用户登出自己的 QQ（清除登录态并禁用自动登录）。"""
    me = get_current_user()
    if not me:
        return jsonify({"error": "未登录或登录已过期"}), 401
    users = load_users()
    if me not in users:
        return jsonify({"error": "用户不存在"}), 404
    if not users[me].get("bound"):
        return jsonify({"error": "尚未绑定 QQ"}), 400
    try:
        container_mgr.logout_qq(me)
        logger.info("用户 %s 自助登出了 QQ", me)
        return jsonify({"message": "QQ 已登出，容器已重启（自动登录已禁用）"})
    except container_mgr.ContainerError as e:
        return jsonify({"error": f"QQ 登出失败：{e}"}), 500


# ─────────────────────────── 路由：admin 邀请码管理 ───────────────────────────

@app.get("/api/admin/invites")
def api_admin_invites_list():
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    return jsonify({"invites": load_invites()})


@app.post("/api/admin/invites")
def api_admin_invites_post():
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get("valid_days", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "有效期天数必须为数字（0 = 永久）"}), 400
    code = secrets.token_urlsafe(6)          # 一次性邀请码（用后即焚）
    invites = load_invites()
    inv = {"created_at": int(time.time()), "created_by": admin,
           "used_by": None, "used_at": None}
    if days > 0:
        inv["expires_at"] = int(time.time()) + days * 86400
    invites[code] = inv
    ok, err = save_invites(invites)
    if not ok:
        return jsonify({"error": err}), 500
    logger.info("管理员 %s 生成邀请码 %s（有效 %d 天）", admin, code, days)
    return jsonify({"message": "邀请码已生成", "code": code}), 201


@app.delete("/api/admin/invites/<code>")
def api_admin_invites_delete(code):
    admin, err_resp = require_admin()
    if err_resp:
        return err_resp
    invites = load_invites()
    if code not in invites:
        return jsonify({"error": "邀请码不存在"}), 404
    del invites[code]
    ok, err = save_invites(invites)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"message": "邀请码已删除"})


# ─────────────────────────── 静态页面 ───────────────────────────

@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(WEB_DIR, filename)


# ─────────────────────────── 启动 ───────────────────────────

def setup_logging(cfg: dict) -> None:
    log_dir = LOGS_DIR
    log_dir.mkdir(exist_ok=True)
    level = cfg.get("logging", {}).get("console_level", "INFO")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = RotatingFileHandler(
        log_dir / f"webapp_{time.strftime('%Y%m%d')}.log",
        maxBytes=5 * 1024 * 1024, backupCount=7, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def main() -> None:
    sys_cfg = load_sys_config()
    setup_logging(sys_cfg)
    web = sys_cfg.get("web", {})
    host = web.get("host", "0.0.0.0")
    port = int(web.get("port", 8081))
    logger.info("🚀 Web 管理服务启动（多用户版）：http://%s:%d", host, port)
    # threaded=True：绑定流程使用后台线程，防单线程 dev server 阻塞
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
