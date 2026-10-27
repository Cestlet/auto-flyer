"""OneBot v11 反向 WebSocket 服务端（双向）。

NapCat 以「WebSocket 客户端」模式主动连入本服务（两端方案 :8080 / 单账号方案 :8082）。
同一条连接双向使用：
- 接收 NapCat 推送的群消息事件（计数），回调业务层；
- 需要发送时通过 call_action() 在同一连接上发起 OneBot API 调用（如 send_group_msg），
  NapCat 返回带 echo 的响应（OneBot v11 反向 WS 客户端默认 Universal 模式，支持双向）。

连接模型：
- 每次 NapCat 重连都会建立新连接，用 self._conn 指向当前活跃连接；
  旧连接关闭时用 `is` 判断，避免误清新连接。
- 双向解复用：带 `echo` 字段且匹配在途请求的帧是 API 响应；不带 echo 的帧是事件。
- 心跳（meta_event_type=heartbeat）与生命周期事件仅记日志，不参与业务。
"""

import asyncio
import json
import logging
import uuid
from typing import Awaitable, Callable

import websockets

logger = logging.getLogger("onebot")


class OneBotServer:
    """接收 NapCat 推送的事件，并在同一连接上调用 OneBot API。"""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        access_token: str,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._path = path
        self._token = access_token
        self._conn = None              # 当前活跃的 NapCat 连接
        self.on_event = on_event       # 业务回调（server.py 注入，允许稍后绑定）
        self.on_connect = on_connect   # 连接建立回调
        self.on_disconnect = on_disconnect  # 连接断开回调
        self._pending: dict[str, asyncio.Future] = {}  # echo -> 结果 Future（API 调用匹配）
        self._send_lock = asyncio.Lock()               # ws.send 并发保护

    @property
    def connected(self) -> bool:
        """是否有活跃的 NapCat 连接（用于状态监控）。

        websockets 17+ 无 closed 属性，用 close_code is None 判断未关闭。
        """
        return self._conn is not None and self._conn.close_code is None

    async def start(self) -> None:
        """启动监听并永久运行，直到被取消。"""
        async with websockets.serve(
            self._handle, self._host, self._port, max_size=None
        ):
            logger.info("OneBot WS 监听中：ws://%s:%d%s（等待 NapCat 接入）",
                        self._host, self._port, self._path)
            await asyncio.Future()     # 挂起至被取消（Ctrl+C）

    async def _dispatch_event(self, payload: dict) -> None:
        """业务事件分发协程：独立执行，异常只记日志不中断接收循环。"""
        if self.on_event is None:
            return
        try:
            await self.on_event(payload)
        except Exception:
            logger.exception("业务事件处理异常")

    def _fail_pending(self) -> None:
        """连接断开时把在途 API 调用全部置为失败（调用方走失败分支，保留计数+退避）。"""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("NapCat 连接断开，API 调用未完成"))
        self._pending.clear()

    async def call_action(self, action: str, params: dict,
                          timeout: float = 30.0) -> dict:
        """在当前活跃连接上调用 OneBot API，返回完整响应 dict。

        请求-响应按 echo 匹配（模式同 control_server.py 的 pending+Future）：
        - echo 必须放根级别（放 params 内 NapCat 不回显）
        - 并发安全：发送加锁，响应帧按 echo 路由到各自的 Future
        """
        ws = self._conn
        if ws is None or ws.close_code is not None:
            raise RuntimeError("NapCat 未连接，无法调用 API")
        echo = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            async with self._send_lock:
                await ws.send(json.dumps(
                    {"action": action, "params": params, "echo": echo}))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(echo, None)      # 超时/异常都清槽

    async def _handle(self, ws) -> None:
        """处理单条连接：校验路径与鉴权 → 登记连接 → 循环收帧（事件/API 响应）。"""
        path = ws.request.path
        if path != self._path:
            await ws.close(code=1008, reason="路径不匹配")
            return
        if self._token:
            header = ws.request.headers.get("Authorization", "")
            if header != f"Bearer {self._token}":
                await ws.close(code=1008, reason="token 错误")
                return
        self._conn = ws
        logger.info("NapCat 已接入（%s），开始接收事件...", path)
        if self.on_connect:
            self.on_connect()
        try:
            async for message in ws:
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("收到无法解析的消息：%r", message[:200])
                    continue
                echo = payload.get("echo")
                if echo and echo in self._pending:
                    # ① API 响应：按 echo 匹配完成对应的 Future（NapCat 返回的
                    #    send_group_msg 等 action 响应帧，双向解复用）
                    fut = self._pending.pop(echo)
                    if not fut.done():
                        fut.set_result(payload)
                    continue
                if payload.get("post_type") == "meta_event":
                    # ② 心跳/生命周期事件：仅日志（NapCat 默认 30s 一次心跳）
                    logger.debug("meta_event: %s", payload.get("meta_event_type"))
                else:
                    # ③ 业务事件：异步化处理（create_task），避免单个事件的
                    # await（如等待发送结果）阻塞接收循环——
                    # 否则 A 群发送期间 B 群消息会全部堆积，计数被延迟
                    asyncio.create_task(self._dispatch_event(payload))
        except websockets.ConnectionClosed:
            logger.warning("NapCat 连接断开，等待其自动重连...")
        except Exception:
            logger.exception("OneBot 连接处理异常")
        finally:
            if self._conn is ws:
                self._conn = None
                self._fail_pending()
                if self.on_disconnect:
                    self.on_disconnect()
                logger.warning("当前无 NapCat 连接")
