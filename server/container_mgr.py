"""NapCat 容器管理：webapp 通过 subprocess docker 动态创建/管理每用户的 NapCat 容器。

容器命名 napcat-<username>（用户名经白名单正则校验，防注入）；
用户名支持普通名（admin）与邮箱（user@example.com，容器名自动 sanitize 为
napcat-<localpart>-<hash4>，因 docker 容器名不允许 @ / . / 大写）。
flyer-server 容器需挂载 docker.sock 并提供 docker CLI（见 docker-compose.yml）。

关键流程（绑定）：
1. ensure_container：预置 webui.json（端口）+ docker run（NAPCAT_WEBUI_SECRET_KEY 固定 token）
2. 用户扫码登录 → 容器 config 卷出现 onebot11_<QQ号>.json
3. write_reverse_ws：把反向 WS 配置写入该文件（4.x network.websocketClients / 3.x enableWsReverse 双兼容）
4. restart_container 生效
"""

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("container")

# 用户名白名单：字母/数字/_/-/@/.（@ . 用于邮箱用户名），排除路径危险字符
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.@-]{3,64}$")
WS_PATH = "/onebot/v11/ws"


class ContainerError(RuntimeError):
    """容器操作失败。"""


def _run(*args: str, timeout: int = 30) -> str:
    """执行 docker 命令，返回 stdout；失败抛 ContainerError。"""
    try:
        r = subprocess.run(["docker", *args], capture_output=True,
                           text=True, timeout=timeout)
    except FileNotFoundError:
        raise ContainerError(
            "docker 命令不可用（flyer-server 容器未挂载 docker CLI？）")
    except subprocess.TimeoutExpired:
        raise ContainerError(f"docker {' '.join(args[:2])} 超时")
    if r.returncode != 0:
        raise ContainerError(
            f"docker {' '.join(args[:2])} 失败：{r.stderr.strip()[:200]}")
    return r.stdout.strip()


def check_username(username: str) -> None:
    if not USERNAME_RE.match(username):
        raise ContainerError(f"用户名不合法：{username}")


def container_name(username: str) -> str:
    """NapCat 容器名：普通用户名 → napcat-<username>；
    邮箱用户名（含 @）→ napcat-<localpart>-<hash4>（容器名不允许 @/. 和大写）。"""
    check_username(username)
    if "@" not in username:
        return f"napcat-{username}"
    local = username.split("@")[0].lower()
    local = re.sub(r"[^a-z0-9_-]", "-", local)[:20].strip("-")
    h = hashlib.md5(username.encode("utf-8")).hexdigest()[:4]
    return f"napcat-{local}-{h}"


# ─────────────────────────── 容器状态 ───────────────────────────

def container_exists(username: str) -> bool:
    try:
        _run("inspect", container_name(username))
        return True
    except ContainerError:
        return False


def is_running(username: str) -> bool:
    try:
        out = _run("inspect", "-f", "{{.State.Running}}", container_name(username))
        return out.strip() == "true"
    except ContainerError:
        return False


def restart_container(username: str) -> None:
    _run("restart", container_name(username))
    logger.info("容器 %s 已重启（反向 WS 配置生效）", container_name(username))


def get_host_dir(username: str) -> Path:
    """返回该用户的 NapCat 容器在宿主机上的数据目录（HOST_SERVER_DIR/napcat-<username>）。"""
    host_server = os.environ.get("HOST_SERVER_DIR", "/root/flyer/server")
    return Path(host_server) / container_name(username)


def backup_qq_session(username: str) -> None:
    """从容器内备份 QQ 登录态（guild1.db 等）到宿主机 data/ 目录。

    重启容器时 restore_qq_session 恢复，实现自动登录。
    """
    name = container_name(username)
    data_dir = get_host_dir(username) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # 用 docker cp 备份 guild1.db 系列文件（QQ 登录态数据库）
    for fname in ("guild1.db", "guild1.db-shm", "guild1.db-wal"):
        try:
            _run("cp", f"{name}:/app/napcat/{fname}",
                 str(data_dir / fname))
        except ContainerError:
            pass  # 文件可能不存在，静默跳过
    # 备份 nt_qq 配置目录（含自动登录配置）
    try:
        out = _run("exec", name, "sh", "-c",
                    "ls -d /home/*/.config/QQ/nt_qq_* 2>/dev/null || ls -d /root/.config/QQ/nt_qq_* 2>/dev/null || true")
        for nt_dir in out.splitlines():
            nt_dir = nt_dir.strip()
            if nt_dir:
                subprocess.run(
                    ["docker", "cp", f"{name}:{nt_dir}/config", str(data_dir / "nt_qq_config")],
                    capture_output=True, timeout=30)
    except (ContainerError, subprocess.TimeoutExpired):
        pass
    logger.info("QQ 登录态已备份到 %s", data_dir)


def restore_qq_session(username: str) -> None:
    """从宿主机 data/ 恢复 QQ 登录态到容器内，实现自动登录。"""
    name = container_name(username)
    data_dir = get_host_dir(username) / "data"
    if not data_dir.is_dir():
        logger.info("无已备份的 QQ 登录态（首次运行），跳过恢复")
        return
    # 恢复 guild1.db 系列文件
    for fname in ("guild1.db", "guild1.db-shm", "guild1.db-wal"):
        src = data_dir / fname
        if src.is_file():
            try:
                _run("cp", str(src), f"{name}:/app/napcat/{fname}")
            except ContainerError as e:
                logger.warning("恢复 %s 失败：%s", fname, e)
    # 恢复 nt_qq 配置目录
    nt_config = data_dir / "nt_qq_config"
    if nt_config.is_dir():
        try:
            # 先查目标容器中的 nt_qq 目录
            out = _run("exec", name, "sh", "-c",
                        "ls -d /home/*/.config/QQ/nt_qq_* 2>/dev/null || ls -d /root/.config/QQ/nt_qq_* 2>/dev/null || true")
            for nt_dir in out.splitlines():
                nt_dir = nt_dir.strip()
                if nt_dir:
                    subprocess.run(
                        ["docker", "cp", str(nt_config), f"{name}:{nt_dir}/config"],
                        capture_output=True, timeout=30)
        except (ContainerError, subprocess.TimeoutExpired):
            pass
    logger.info("QQ 登录态已恢复到容器 %s", name)


# ─────────────────────────── 自动登录配置 ───────────────────────────

def set_auto_login(username: str, qq_number: str) -> None:
    """在 NapCat 配置中启用 QQ 自动登录（qq_auto_login + autoLogin）。"""
    name = container_name(username)
    _exec_python_script(name, _QQ_AUTO_LOGIN_SCRIPT)
    _exec_python_script(name, _NAP_AUTO_LOGIN_SCRIPT.replace("{QQ}", qq_number))
    logger.info("已启用 QQ 自动登录（%s）", qq_number)


# 通过临时文件传递 Python 脚本，避免 shell 引号嵌套
_QQ_AUTO_LOGIN_SCRIPT = """import sys,json
try:
  d=json.load(open('/app/napcat/qqnt.json'))
except:
  d={}
d['qq_auto_login']=True
d['qq_auto_delay']=3
open('/app/napcat/qqnt.json','w').write(json.dumps(d,ensure_ascii=False,indent=2))
"""

_NAP_AUTO_LOGIN_SCRIPT = """import sys,json
f='/app/napcat/config/napcat_{QQ}.json'
try:
  d=json.load(open(f))
except:
  d={}
d['autoLogin']=True
open(f,'w').write(json.dumps(d,ensure_ascii=False,indent=2))
"""


def _exec_python_script(name: str, script: str) -> None:
    """在容器内执行 Python 脚本（通过临时文件传递，避免 shell 引号问题）。"""
    import uuid
    tag = uuid.uuid4().hex[:8]
    tmpfile = f"/tmp/nalc_{tag}.py"
    try:
        # 用 base64 编码传递脚本，彻底规避引号问题
        b64 = base64.b64encode(script.encode()).decode()
        _run("exec", name, "sh", "-c",
             f"echo '{b64}' | base64 -d > {tmpfile} && python3 {tmpfile} 2>/dev/null || true")
    except ContainerError:
        pass


def remove_container(username: str) -> None:
    """删除容器（force）。不存在时静默通过。"""
    try:
        _run("rm", "-f", container_name(username))
        logger.info("容器 %s 已删除", container_name(username))
    except ContainerError:
        pass


def logout_qq(username: str) -> None:
    """登出指定用户的 QQ（清除登录态 + 禁用自动登录），容器重启后不自动登录。

    操作步骤：
    1. 禁用 qqnt.json 的 qq_auto_login
    2. 禁用 napcat_{QQ}.json 的 autoLogin
    3. 清除 guild1.db 系列 QQ 登录态数据库
    4. 清除 nt_qq 配置目录（含自动登录配置）
    5. 重启容器（使配置生效、QQ 进入未登录状态）
    """
    name = container_name(username)
    if not container_exists(username):
        logger.info("容器 %s 不存在，跳过登出", name)
        return

    # 1) 禁用 qq_auto_login（qqnt.json）
    _exec_python_script(name, _QQ_DISABLE_AUTO_LOGIN_SCRIPT)

    # 2) 禁用所有 napcat_{QQ}.json 的 autoLogin
    _exec_python_script(name, _NAP_DISABLE_AUTO_LOGIN_SCRIPT)

    # 3) 清除 QQ 登录态数据库（guild1.db 系列）
    _run("exec", name, "sh", "-c",
         "rm -f /app/napcat/guild1.db /app/napcat/guild1.db-shm /app/napcat/guild1.db-wal "
         "2>/dev/null || true")

    # 4) 清除 nt_qq 配置目录（含登录态缓存）
    _run("exec", name, "sh", "-c",
         "rm -rf /home/*/.config/QQ/nt_qq_* /root/.config/QQ/nt_qq_* "
         "2>/dev/null || true")

    # 5) 清除容器卷中备份的登录态（防止下次容器重建时恢复）
    data_dir = get_host_dir(username) / "data"
    if data_dir.is_dir():
        for fname in ("guild1.db", "guild1.db-shm", "guild1.db-wal"):
            (data_dir / fname).unlink(missing_ok=True)
        nt_config = data_dir / "nt_qq_config"
        if nt_config.is_dir():
            import shutil
            shutil.rmtree(nt_config, ignore_errors=True)

    # 6) 重启容器
    try:
        restart_container(username)
        logger.info("QQ 登出完成，容器 %s 已重启（自动登录已禁用）", name)
    except ContainerError as e:
        logger.warning("容器 %s 重启失败（可手动重启）：%s", name, e)


# ─────────────────────────── QQ 登出自动登录禁用脚本 ───────────────────────────

_QQ_DISABLE_AUTO_LOGIN_SCRIPT = """import sys,json
try:
  d=json.load(open('/app/napcat/qqnt.json'))
except:
  d={}
d['qq_auto_login']=False
open('/app/napcat/qqnt.json','w').write(json.dumps(d,ensure_ascii=False,indent=2))
"""

_NAP_DISABLE_AUTO_LOGIN_SCRIPT = """import sys,json,os,glob
for f in glob.glob('/app/napcat/config/napcat_*.json'):
  try:
    d=json.load(open(f))
    d['autoLogin']=False
    open(f,'w').write(json.dumps(d,ensure_ascii=False,indent=2))
  except:
    pass
"""


# ─────────────────────────── 创建 ───────────────────────────

def ensure_container(username: str, user_meta: dict, sys_cfg: dict) -> None:
    """确保该用户的 NapCat 容器存在并运行。

    - 预置 napcat-<user>/config/webui.json（固定端口）
    - 创建 users/<user>/images 目录（NapCat 挂载 /app/images 读图）
    - docker run（host 网络，NAPCAT_WEBUI_SECRET_KEY 固定 WebUI token）

    注意：docker run 的 -v 挂载源由宿主机 daemon 解析，必须用**宿主机绝对路径**。
    flyer-server 容器内执行 docker CLI 时，通过 compose 映射的 /host-flyer
    （= 宿主机 ~/flyer/server）定位宿主机目录（HOST_SERVER_DIR 环境变量可覆盖）。
    """
    check_username(username)
    name = container_name(username)
    if container_exists(username):
        return
    host_server = os.environ.get("HOST_SERVER_DIR", "/root/flyer/server")
    napcat_dir = Path(host_server) / name
    config_dir = napcat_dir / "config"
    qq_dir = napcat_dir / "qq"
    cache_dir = napcat_dir / "cache"
    logs_dir = napcat_dir / "logs"
    data_dir = napcat_dir / "data"          # QQ 登录态备份（guild1.db）
    images_dir = Path(host_server) / "users" / username / "images"
    config_dir.mkdir(parents=True, exist_ok=True)
    qq_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    # 预置 webui.json（token 由环境变量固定，NapCat 会自动写回）。
    # 注意：NapCat 4.x WebUI 端口固定 6099（不读此文件的 port 字段）；
    # 此处写实际端口仅兼容 3.x（3.x 读取 webui.json 的 port）
    actual_port = int(sys_cfg.get("docker", {}).get("webui_actual_port", 6099))
    webui = {"host": "0.0.0.0", "port": actual_port}
    (config_dir / "webui.json").write_text(
        json.dumps(webui, ensure_ascii=False), encoding="utf-8")

    image = sys_cfg.get("docker", {}).get("napcat_image",
                                          "mlikiowa/napcat-docker:latest")
    uid = sys_cfg.get("docker", {}).get("napcat_uid", 1000)
    gid = sys_cfg.get("docker", {}).get("napcat_gid", 1000)
    # ACCOUNT 环境变量：entrypoint.sh 据此加 -q <QQ> 实现自动登录
    # （napcat 镜像 entrypoint 支持 ACCOUNT；已绑定用户重建容器时自动快速登录）
    account = str(user_meta.get("qq_number") or "")
    env_args = [
        "-e", f"NAPCAT_UID={uid}", "-e", f"NAPCAT_GID={gid}",
        "-e", f"NAPCAT_WEBUI_SECRET_KEY={user_meta['webui_token']}",
        # 固定 JWT 密钥：webapp 可复现 WebUI 凭据调用 RefreshQRcode 强制换新二维码
        # （未设置时 NapCat 每次启动随机生成，外部无法构造凭据）
        "-e", f"NAPCAT_WEBUI_JWT_SECRET_KEY={user_meta.get('webui_jwt_key', '')}",
    ]
    if account:
        env_args += ["-e", f"ACCOUNT={account}"]
    _run(
        "run", "-d", "--name", name, "--restart", "unless-stopped",
        "--network", "host",
        *env_args,
        "-v", f"{config_dir}:/app/napcat/config",
        "-v", f"{qq_dir}:/app/napcat/qq",
        "-v", f"{cache_dir}:/app/napcat/cache",
        "-v", f"{logs_dir}:/app/napcat/logs",
        "-v", f"{images_dir}:/app/images",
        image,
        timeout=120,
    )
    logger.info("容器 %s 已创建（WebUI 端口 %s，ACCOUNT=%s）",
                name, user_meta["webui_port"], account or "无（待扫码绑定）")


def get_qrcode_image(username: str) -> str:
    """读取 NapCat 容器内的登录二维码图片，返回 base64 字符串；无二维码返回空串。

    扫码登录不依赖 NapCat WebUI（4.x WebUI 端口固定 6099，多容器端口冲突），
    直接从容器读取 NapCat 每次刷新写入的 qrcode.png 展示给用户扫码。
    """
    name = container_name(username)
    try:
        r = subprocess.run(
            ["docker", "exec", name, "sh", "-c",
             "cat /app/napcat/cache/qrcode.png 2>/dev/null || true"],
            capture_output=True, timeout=15)
        if r.returncode != 0 or not r.stdout:
            return ""
        return base64.b64encode(r.stdout).decode("utf-8")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


# ─────────────────────────── WebUI token / QQ 检测 ───────────────────────────

def get_webui_token(username: str, user_meta: dict) -> str:
    """读取卷内 webui.json 的当前 token（扫码后 NapCat 可能刷新），兜底用预生成值。"""
    cfg_file = Path(__file__).resolve().parent / container_name(username) / "config" / "webui.json"
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        token = data.get("token")
        if token:
            return token
    except (OSError, json.JSONDecodeError):
        pass
    return user_meta.get("webui_token", "")


def find_onebot11_file(username: str) -> Path | None:
    """找到该用户 config 卷中**最新**的 onebot11_<QQ号>.json（扫码登录后生成）。

    多个 onebot11_*.json 文件时按**修改时间**取最新的（后登录的 QQ 号）。
    兜底匹配 onebot11.json（NapCat 不同版本命名不同，4.x 可能无 QQ 号后缀）。
    """
    config_dir = Path(__file__).resolve().parent / container_name(username) / "config"
    if not config_dir.is_dir():
        return None
    files = list(config_dir.glob("onebot11_*.json"))
    if files:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0]
    # 兜底：无 QQ 号后缀的 onebot11.json
    fallback = config_dir / "onebot11.json"
    return fallback if fallback.is_file() else None


def detect_qq_number(username: str) -> str | None:
    """从 onebot11_<QQ号>.json 文件名解析绑定 QQ 号。"""
    f = find_onebot11_file(username)
    if f is None:
        return None
    m = re.search(r"onebot11_(\d+)\.json$", f.name)
    return m.group(1) if m else None


# ─────────────────────────── 反向 WS 配置写入 ───────────────────────────

def write_reverse_ws(username: str, user_meta: dict) -> str:
    """把反向 WS 配置写入**所有** onebot11_*.json / onebot11.json，返回 QQ 号。

    兼容 4.x（network.websocketClients 数组）与 3.x（enableWsReverse/wsReverseUrls）。
    写完后调用方需 restart_container 生效。

    重要：写入所有匹配的文件，避免 NapCat 切换 QQ 号后 WS 配置丢失
    （2026-08-12 修复：管理员 NapCat 登录了新 QQ 号，新 onebot11 文件缺少 WS 配置）。
    """
    config_dir = Path(__file__).resolve().parent / container_name(username) / "config"
    if not config_dir.is_dir():
        raise ContainerError(f"配置目录不存在：{config_dir}")

    files = list(config_dir.glob("onebot11_*.json")) + [config_dir / "onebot11.json"]
    files = [f for f in files if f.is_file()]

    if not files:
        raise ContainerError(
            "未找到 onebot11_*.json——请确认已扫码登录（登录后 NapCat 自动生成该配置）")

    ws_url = f"ws://127.0.0.1:{user_meta['ws_port']}{WS_PATH}"
    ws_token = user_meta.get("ws_access_token", "")

    def _write_one(cfg_path: Path) -> None:
        """写入单个 onebot11 配置文件的 WS 段。"""
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        if "network" in data or "websocketClients" in data:
            # 4.x：network.websocketClients 数组（name 唯一）
            network = data.setdefault("network", {})
            clients = network.setdefault("websocketClients", [])
            entry = {"name": f"flyer-{username}", "enable": True, "url": ws_url,
                     "token": ws_token, "messagePostFormat": "array",
                     "reportSelfMessage": True, "reconnectInterval": 5000}
            for i, c in enumerate(clients):
                if c.get("name") == f"flyer-{username}":
                    clients[i] = entry
                    break
            else:
                clients.append(entry)
        else:
            # 3.x：顶层 enableWsReverse + wsReverseUrls
            data["enableWsReverse"] = True
            data["wsReverseUrls"] = [ws_url]
            data["token"] = ws_token
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        logger.info("反向 WS 已写入 %s（%s）", cfg_path.name, ws_url)

    for f in files:
        _write_one(f)

    # 从最新文件解析 QQ 号
    latest = max(files, key=lambda p: p.stat().st_mtime)
    m = re.search(r"onebot11_(\d+)\.json$", latest.name)
    return m.group(1) if m else ""


# ─────────────────────────── 绑定状态 ───────────────────────────

def wait_for_onebot11(username: str, timeout: float = 60.0) -> bool:
    """轮询等待 onebot11_*.json 出现（扫码登录完成的信号）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if find_onebot11_file(username) is not None:
            return True
        time.sleep(3)
    return False