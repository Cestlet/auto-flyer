"""
QQ 空间 API 客户端（从 qqtranspond 项目迁移，适配多用户云端架构）。

通过 HTTP 请求获取指定用户的 QQ 空间动态，以及点赞和评论。
Cookies 由调用方提供——多用户版从该用户的 NapCat 容器（get_cookies /
get_credentials）获取，因此每个用户以自己的 QQ 身份执行操作。

注意：本模块为同步 requests 实现；在 server.py 的 asyncio 事件循环中
调用时需用 asyncio.to_thread 包裹，避免阻塞事件循环。
"""
import re
import json
import logging
import requests
from typing import List, Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger("flyer.qzone")


class QzoneClient:
    """QQ空间客户端，用于获取说说、点赞、评论"""

    def __init__(self, cookies: str, uin: str):
        """
        :param cookies: qzone.qq.com 域下的完整 Cookie 字符串
        :param uin:     当前登录的 QQ 号
        """
        self.cookies = cookies
        self.uin = uin
        self._g_tk = self._calc_g_tk(cookies)            # p_skey 优先（旧接口）
        self._g_tk_skey = self._calc_g_tk_skey(cookies)  # skey 优先（h5 写接口）
        self._qzonetokens: dict = {}   # host_uin -> qzonetoken（h5 写接口需要）
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": "https://user.qzone.qq.com/",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._set_cookies()

    @staticmethod
    def _calc_g_tk(cookie_str: str) -> int:
        """
        根据 p_skey 或 skey 计算 g_tk (用来调用 qzone api)
        """
        def _get_cookie(key: str) -> str:
            m = re.search(rf'(?:^|;)\s*{re.escape(key)}\s*=\s*([^;]+)', cookie_str)
            return m.group(1) if m else ""

        skey = _get_cookie("p_skey") or _get_cookie("skey") or ""
        h = 5381
        for ch in skey:
            h = h + (h << 5) + ord(ch)
        return h & 0x7fffffff

    @staticmethod
    def _calc_g_tk_skey(cookie_str: str) -> int:
        """
        用 skey（而非 p_skey）计算 g_tk —— h5.qzone.qq.com 写接口
        （点赞/评论）使用 skey 派生的 g_tk，用 p_skey 会被网关 403。
        """
        def _get_cookie(key: str) -> str:
            m = re.search(rf'(?:^|;)\s*{re.escape(key)}\s*=\s*([^;]+)', cookie_str)
            return m.group(1) if m else ""

        skey = _get_cookie("skey") or _get_cookie("p_skey") or ""
        h = 5381
        for ch in skey:
            h = h + (h << 5) + ord(ch)
        return h & 0x7fffffff

    def _set_cookies(self):
        """将 Cookie 字符串设置到 session 中"""
        self._session.cookies.clear()
        for item in self.cookies.split(";"):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                self._session.cookies.set(k.strip(), v.strip(), domain=".qzone.qq.com")

    def _jsonp_parse(self, text: str) -> Optional[dict]:
        """解析 JSONP 回调响应。

        兼容任意回调名（emotion_cgi_msglist_v6 返回 _preloadCallback，
        cgi_userinfo_get_all 返回 _Callback），容忍尾部分号与换行。
        """
        text = text.strip()
        m = re.match(r'^[A-Za-z_$][\w$]*\((.*)\)\s*;?\s*$', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 也可能是纯 JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    # ========== 获取说说 ==========

    def fetch_shuoshuo(
        self,
        target_uin: str,
        num: int = 10,
        pos: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        获取某个 QQ 用户的说说列表

        :param target_uin: 目标 QQ 号
        :param num:        获取条数 (1-35)
        :param pos:        起始偏移
        :return:           说说列表
        """
        url = (
            "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/"
            "cgi-bin/emotion_cgi_msglist_v6"
        )
        params = {
            "uin": target_uin,
            "ftype": 0,
            "sort": 0,
            "pos": pos,
            "num": num,
            "replynum": 0,
            "g_tk": self._g_tk,
            "callback": "_preloadCallback",
            "code_version": 1,
            "format": "jsonp",
            "need_private_comment": 1,
        }
        self._set_cookies()

        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"请求 Qzone API 失败 ({target_uin}): {e}")
            return []

        data = self._jsonp_parse(resp.text)
        if not data:
            logger.warning(f"Qzone 返回格式异常 (uin={target_uin}): 前100字={resp.text[:100]}")
            return []

        if data.get("code") != 0:
            logger.warning(
                f"Qzone API 返回错误 code={data.get('code')} "
                f"msg={data.get('message', '')} (uin={target_uin})"
            )
            return []

        msglist = data.get("msglist", [])
        results = []
        for msg in msglist:
            content = self._extract_content(msg)
            pics = self._extract_pictures(msg)
            create_time = self._parse_time(msg.get("created_time", 0))

            results.append({
                "tid": msg.get("tid", ""),
                # 新版点赞/评论接口需要 appid/fid（fid 兜底取 cellid/ugcrightkey/tid）
                "appid": msg.get("appid", 311),
                "fid": (msg.get("fid") or msg.get("cellid")
                        or msg.get("ugcrightkey") or msg.get("tid", "")),
                "uin": target_uin,
                "content": content,
                "pictures": pics,
                "create_time": create_time,
                "source_name": msg.get("source_name", ""),
                "certified": msg.get("certified", 0),
            })

        return results

    # ========== qzonetoken（h5 写接口需要） ==========

    def get_qzonetoken(self, host_uin: str) -> str:
        """
        获取指定用户的 qzonetoken（h5.qzone.qq.com/mqzone/index 页面内嵌），
        按 host_uin 缓存。失败返回空串。
        """
        if host_uin in self._qzonetokens:
            return self._qzonetokens[host_uin]
        url = "https://h5.qzone.qq.com/mqzone/index"
        headers = {"Referer": "https://h5.qzone.qq.com/"}
        # 尝试多种请求形态（aioqzone 不带参数；部分环境需要 uin/g_tk）
        attempts = [
            ("无参数", None),
            ("uin", {"uin": host_uin}),
            ("uin+gtk", {"uin": host_uin, "g_tk": self._g_tk_skey}),
        ]
        for label, params in attempts:
            try:
                resp = self._session.get(url, params=params, headers=headers,
                                         timeout=10)
                if resp.status_code != 200:
                    logger.warning(f"获取 qzonetoken HTTP {resp.status_code}"
                                   f"（{label}，{host_uin}）")
                    continue
                html = resp.text
                m = re.search(r'window\.shine0callback.*?return "([0-9a-f]+?)";',
                              html, re.DOTALL)
                if m:
                    token = m.group(1)
                    self._qzonetokens[host_uin] = token
                    logger.info(f"qzonetoken 获取成功（{host_uin}，{label}）")
                    return token
                logger.warning(
                    f"qzonetoken 未匹配（{label}，{host_uin}），页面 len={len(html)}，"
                    f"shine0@{html.find('shine0')}，qzonetoken@{html.find('qzonetoken')}，"
                    f"附近: {html[max(0, html.find('qzonetoken')-60):html.find('qzonetoken')+160]}")
            except Exception as e:
                logger.warning(f"获取 qzonetoken 异常（{label}，{host_uin}）: {e}")
        return ""

    # ========== 点赞 ==========

    def like_shuoshuo(self, target_uin: str, tid: str,
                      post: Optional[dict] = None) -> bool:
        """
        给一条说说点赞（新版 h5 接口 internal_dolike_app）。

        :param target_uin: 目标 QQ 号
        :param tid:        说说 ID
        :param post:       说说完整数据（提供 appid/fid；缺省回退 tid）
        :return:           True 表示成功
        """
        appid = int((post or {}).get("appid", 311))
        fid = str((post or {}).get("fid") or tid)
        # unikey/curkey：新版格式 https://user.qzone.qq.com/{uin}/mood/{fid}
        unikey = f"https://user.qzone.qq.com/{target_uin}/mood/{fid}"
        url = ("https://h5.qzone.qq.com/proxy/domain/w.qzone.qq.com/"
               "cgi-bin/likes/internal_dolike_app")
        params = {
            "opuin": target_uin,
            "unikey": unikey,
            "curkey": unikey,
            "appid": appid,
            "opr_type": "like",       # like=点赞, unlike=取消
            "format": "purejson",
            "g_tk": self._g_tk_skey,  # h5 接口用 skey 派生的 g_tk
        }
        qzonetoken = self.get_qzonetoken(target_uin)
        if qzonetoken:
            params["qzonetoken"] = qzonetoken
        headers = {"Referer": "https://h5.qzone.qq.com/",
                   "X-Requested-With": "XMLHttpRequest"}
        self._set_cookies()

        try:
            resp = self._session.post(url, data=params, headers=headers, timeout=10)
            if resp.status_code != 200:
                logger.error(f"点赞 HTTP {resp.status_code}: {resp.text[:200]}")
                return False
            data = self._jsonp_parse(resp.text) or {}
            if data.get("code") == 0 or data.get("retcode") == 0:
                logger.info(f"点赞成功: uin={target_uin}, tid={tid}, fid={fid}")
                return True
            else:
                logger.warning(
                    f"点赞失败: uin={target_uin}, tid={tid}, "
                    f"code={data.get('code')}, msg={data.get('message', '')}, "
                    f"body={resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"点赞异常 ({target_uin}, {tid}): {e}")
            return False

    # ========== 评论 ==========

    def comment_shuoshuo(self, target_uin: str, tid: str, content: str,
                         post: Optional[dict] = None) -> bool:
        """
        给一条说说评论（新版 h5 接口 qzoneOperation/addComment，JSON）。

        :param target_uin: 目标 QQ 号（被评论者）
        :param tid:        说说 ID
        :param content:    评论内容
        :param post:       说说完整数据（提供 appid/fid；缺省回退 tid）
        :return:           True 表示成功
        """
        appid = int((post or {}).get("appid", 311))
        fid = str((post or {}).get("fid") or tid)
        url = "https://h5.qzone.qq.com/webapp/json/qzoneOperation/addComment"
        payload = {
            "uin": int(self.uin or target_uin),   # 登录者
            "ownuin": int(target_uin),            # 说说主人
            "srcId": fid,                         # 说说 fid
            "isPrivateComment": 0,
            "content": content,
            "appid": appid,
            "g_tk": self._g_tk_skey,              # h5 接口用 skey 派生的 g_tk
        }
        qzonetoken = self.get_qzonetoken(target_uin)
        if qzonetoken:
            payload["qzonetoken"] = qzonetoken
        headers = {
            "Referer": "https://h5.qzone.qq.com/",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        self._set_cookies()

        try:
            resp = self._session.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code != 200:
                logger.error(f"评论 HTTP {resp.status_code}: {resp.text[:200]}")
                return False
            data = self._jsonp_parse(resp.text) or {}
            if data.get("code") == 0 or data.get("retcode") == 0:
                logger.info(f"评论成功: uin={target_uin}, tid={tid}, content={content[:30]}")
                return True
            else:
                logger.warning(
                    f"评论失败: uin={target_uin}, tid={tid}, "
                    f"code={data.get('code')}, msg={data.get('message', '')}, "
                    f"body={resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"评论异常 ({target_uin}, {tid}): {e}")
            return False

    # ========== 工具方法 ==========

    @staticmethod
    def _extract_content(msg: Dict) -> str:
        """从消息体中提取纯文本内容"""
        content = msg.get("content", "") or ""
        return content.strip()

    @staticmethod
    def _extract_pictures(msg: Dict) -> List[str]:
        """提取图片 URL 列表"""
        pics = []
        for pic_info in msg.get("pic", []):
            url = pic_info.get("url_path", "")
            if url:
                if not url.startswith("http"):
                    url = "https://" + url.lstrip("//")
                pics.append(url)
        return pics

    @staticmethod
    def _parse_time(timestamp) -> str:
        """将时间戳转为可读字符串"""
        try:
            ts = int(timestamp)
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            return str(timestamp)

    def get_user_info(self, target_uin: str) -> Optional[Dict[str, Any]]:
        """获取用户基本信息（昵称、头像等）。

        注意：cgi_userinfo_get_all 成功响应**没有 code 字段**，且用户信息在
        顶层（{"uin":..., "nickname":..., "avatar":...}）；失败时可能返回
        {"code": 非0, "message": ...}。两者都要兼容。
        """
        url = (
            "https://user.qzone.qq.com/proxy/domain/base.qzone.qq.com/"
            "cgi-bin/user/cgi_userinfo_get_all"
        )
        params = {
            "uin": target_uin,
            "g_tk": self._g_tk,
            "callback": "_preloadCallback",
            "format": "jsonp",
        }
        self._set_cookies()
        try:
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = self._jsonp_parse(resp.text)
            if not data:
                logger.warning(f"用户信息解析失败 ({target_uin}): {resp.text[:120]}")
                return None
            # 成功：无 code 或 code==0；数据在顶层或 data 子字段
            if data.get("code") in (None, 0):
                info = data.get("data") or data
                return {
                    "uin": str(info.get("uin", target_uin)),
                    "nickname": info.get("nickname", "") or "",
                    "avatar": info.get("avatar", "") or "",
                }
            logger.warning(f"获取用户信息失败 ({target_uin}): code={data.get('code')}, "
                           f"msg={data.get('message', '')}")
        except Exception as e:
            logger.warning(f"获取用户信息失败 ({target_uin}): {e}")
        return None
