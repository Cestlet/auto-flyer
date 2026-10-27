"""
QQ 扫码登录模块（从 qqtranspond 项目迁移）。

通过 ptlogin2 二维码扫码获取**完整的 Qzone Cookies**（含 qzone.qq.com 域
的 p_skey/qzone_token 等）——NapCat 的 get_cookies 只返回 ptlogin 域的部分
cookies，h5.qzone.qq.com 写接口（点赞/评论）会因缺 qzone 域凭证返回 403。

流程: 获取 login_sig → 获取二维码 → 轮询登录状态 → 跟进重定向收集 Cookie
"""
import re
import time
import logging
from typing import Optional, Dict, Tuple
from urllib.parse import urlparse, parse_qs

import requests

logger = logging.getLogger("flyer.qr_login")

# Qzone 的 appid 与 daid
APPID = "549000912"
DAID = "5"
# 登录成功后的跳转 URL
JUMP_URL = "https://qzs.qq.com/qzone/v5/loginsucc.html?para=izone"
# 登录页（用于获取 login_sig）
XLOGIN_URL = "https://xui.ptlogin2.qq.com/cgi-bin/xlogin"


def hash33(t: str) -> int:
    """计算 ptqrtoken 的哈希算法（与 QQ 前端一致）。"""
    e = 0
    for ch in t:
        e += (e << 5) + ord(ch)
    return 2147483647 & e


class QRLogin:
    """QQ 扫码登录器：获取 login_sig → 二维码 → 轮询 → 收集 Cookie。"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        })
        self._qrsig: str = ""
        self._ptqrtoken: int = 0
        self._login_sig: str = ""
        self._cookies: str = ""
        self._uin: str = ""

    # ========== 第一步：获取 login_sig ==========

    def _fetch_login_sig(self):
        """访问 xlogin 页面获取 login_sig（部分账号缺失会导致登录失败）。"""
        params = {
            "proxy_url": JUMP_URL,
            "appid": APPID,
            "target": "self",
            "s_url": JUMP_URL,
            "pt_no_auth": "1",
            "pt_3rd_aid": "0",
            "daid": DAID,
        }
        try:
            resp = self.session.get(XLOGIN_URL, params=params, timeout=10)
            m = re.search(r'login_sig\s*[:=]\s*["\']([^"\']+)["\']', resp.text)
            if m:
                self._login_sig = m.group(1)
            if not self._login_sig:
                for cookie in resp.cookies:
                    if cookie.name == "pt_login_sig":
                        self._login_sig = cookie.value
                        break
            if not self._login_sig:
                logger.warning("未能从 xlogin 页面提取 login_sig，继续尝试")
        except requests.RequestException as e:
            logger.warning(f"获取 login_sig 失败: {e}")

    # ========== 第二步：获取二维码 ==========

    def get_qrcode(self) -> Optional[bytes]:
        """获取登录二维码图片（PNG），失败返回 None。"""
        self._fetch_login_sig()

        url = "https://ssl.ptlogin2.qq.com/ptqrshow"
        params = {
            "appid": APPID,
            "e": "2",
            "l": "M",
            "s": "3",
            "d": "72",
            "v": "4",
            "t": str(int(time.time() * 1000)),
            "daid": DAID,
            "pt_3rd_aid": "0",
        }
        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()

            for cookie in resp.cookies:
                if cookie.name == "qrsig":
                    self._qrsig = cookie.value
                    break

            if not self._qrsig:
                logger.error("未能获取 qrsig cookie")
                return None

            self._ptqrtoken = hash33(self._qrsig)
            logger.info("二维码获取成功")
            return resp.content

        except requests.RequestException as e:
            logger.error(f"获取二维码失败: {e}")
            return None

    # ========== 第三步：轮询登录状态 ==========

    def poll_login(self) -> Dict:
        """轮询检查登录状态。

        返回 {"status": waiting|scanned|success|expired|error|unknown, ...}
        """
        url = "https://ssl.ptlogin2.qq.com/ptqrlogin"
        params = {
            "ptqrtoken": self._ptqrtoken,
            "webqq_type": "10",
            "remember_uin": "1",
            "login2qq": "1",
            "aid": APPID,
            "u1": JUMP_URL,
            "ptredirect": "0",
            "h": "1",
            "t": "1",
            "g": "1",
            "from_ui": "1",
            "ptlang": "2052",
            "action": f"0-0-{int(time.time() * 1000)}",
            "js_ver": "22082211",
            "js_type": "0",
            "login_sig": self._login_sig,
            "pt_uistyle": "40",
            "daid": DAID,
            "o1vId": "1",
        }

        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            text = resp.text.strip()
        except requests.RequestException as e:
            logger.error(f"轮询登录状态失败: {e}")
            return {"status": "error", "message": f"请求失败: {e}"}

        # 解析 ptuiCB 回调（字段数与空格可能变化，通用提取）
        inner_m = re.search(r"ptuiCB\s*\((.*)\)\s*;?\s*$", text, re.DOTALL)
        if not inner_m:
            logger.warning(f"无法解析登录响应: {text[:500]}")
            return {"status": "error", "message": "响应格式异常"}

        fields = re.findall(r"'([^']*)'", inner_m.group(1))
        if not fields:
            logger.warning(f"登录响应无有效字段: {text[:500]}")
            return {"status": "error", "message": "响应格式异常"}

        code = fields[0]
        redirect_url = fields[2] if len(fields) > 2 else ""
        message = fields[4] if len(fields) > 4 else ""
        uin = fields[5] if len(fields) > 5 else ""

        result = {"code": code, "message": message, "uin": uin,
                  "redirect_url": redirect_url}

        if code == "0":
            logger.info(f"扫码登录成功! uin={uin}")
            result["status"] = "success"
            cookies, final_uin = self._follow_redirect(redirect_url)
            if cookies:
                self._cookies = cookies
                self._uin = final_uin or uin
                result["cookies"] = self._cookies
                result["final_uin"] = self._uin
        elif code == "66":
            result["status"] = "waiting"   # 二维码未失效，等待扫描
        elif code == "67":
            result["status"] = "scanned"   # 已扫描，等待确认
        elif code == "65":
            result["status"] = "expired"   # 二维码已过期
        else:
            result["status"] = "unknown"

        return result

    # ========== 第四步：跟进重定向获取完整 Cookie ==========

    def _follow_redirect(self, redirect_url: str) -> Tuple[Optional[str], Optional[str]]:
        """跟进登录重定向，收集完整 Cookie（含 qzone 域 p_skey/qzone_token）。"""
        if not redirect_url:
            return None, None

        try:
            self.session.get(redirect_url, timeout=15, allow_redirects=True)
        except requests.RequestException as e:
            logger.warning(f"重定向跟随过程中出现异常: {e}")

        # 收集关键域名的 cookie
        collected = {}
        for cookie in self.session.cookies:
            domain = cookie.domain or ""
            if any(d in domain for d in [".qq.com", ".qzone.qq.com",
                                         ".ptlogin2.qq.com"]):
                collected[cookie.name] = cookie.value

        cookie_str = "; ".join(f"{k}={v}" for k, v in collected.items())

        uin = collected.get("uin", "")
        if not uin:
            parsed = urlparse(redirect_url)
            uin = parse_qs(parsed.query).get("uin", [""])[0]

        logger.info(f"Cookie 收集完成，共 {len(collected)} 项")
        if not cookie_str:
            return None, uin or None
        return cookie_str, uin or None

    # ========== 获取结果 ==========

    @property
    def cookies(self) -> str:
        return self._cookies

    @property
    def uin(self) -> str:
        return self._uin

    @property
    def is_logged_in(self) -> bool:
        return bool(self._cookies and self._uin)
