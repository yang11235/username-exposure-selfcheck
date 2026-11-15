"""Chrome DevTools Protocol 的最小客户端（只做"打开页面、取 HTML"）。

为什么自己写而不引 Playwright：

    Playwright 自带浏览器内核（~200MB），而本项目明确不做打包分发（文档 §1.2②）。
    让用户为"查一次用户名"多装一个浏览器内核，与"自行部署、依赖要少"的约束冲突。

    更重要的是 **CDP 连用户自己的 Chrome** 能一次解决四件事（文档 §3.2.3）：
        1. 登录态由用户浏览器管理，我们零存储（不碰 Cookie 文件）
        2. 指纹天然一致，风控风险最低
        3. 不用内置内核，免 200MB 体积
        4. 用户能亲眼看到浏览器在做什么，信任成本低

    而 CDP 本身只是"WebSocket + JSON-RPC"，aiohttp 已经带 WebSocket 客户端，
    所以这里**不引入任何新依赖**。

分层（为了能离线测试）：

    Transport      只负责收发文本帧，接口两个方法
    CDPSession     协议层：命令 id 匹配、事件分发、超时
    CDPBrowser     语义层：navigate → 等加载 → 取 outerHTML

    真实环境用 AiohttpTransport；测试用 FakeTransport 直接喂 JSON，
    不需要浏览器也能覆盖"命令匹配""事件早到""超时"这些最容易出错的地方。
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9222

# 单条命令的默认超时。给足时间：冷启动的浏览器打开一个 SPA 要几秒。
DEFAULT_TIMEOUT = 30.0


class CDPError(RuntimeError):
    """CDP 协议层错误（连接不上、命令返回 error、超时）。"""


class Transport(Protocol):
    """文本帧收发。实现方只需保证 send/recv 是协程。"""

    async def send(self, text: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


class AiohttpTransport:
    """基于 aiohttp 的 WebSocket 传输（唯一引入 aiohttp 的地方）。

    `max_msg_size=0` 很关键：CDP 返回的 HTML 动辄几 MB，
    aiohttp 默认 4MB 上限会在取大页面时抛 "Message too large"。
    """

    def __init__(self, session, ws):
        self._session = session
        self._ws = ws

    @classmethod
    async def connect(cls, url: str, timeout: float = DEFAULT_TIMEOUT):
        import aiohttp

        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(url, max_msg_size=0, timeout=timeout)
        except Exception:
            await session.close()
            raise
        return cls(session, ws)

    async def send(self, text: str) -> None:
        await self._ws.send_str(text)

    async def recv(self) -> str:
        import aiohttp

        msg = await self._ws.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            return msg.data
        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            raise CDPError("CDP 连接已关闭")
        if msg.type == aiohttp.WSMsgType.ERROR:
            raise CDPError(f"CDP 连接出错：{self._ws.exception()}")
        # BINARY / PING / PONG 都不是 CDP 的业务帧，继续等
        return await self.recv()

    async def close(self) -> None:
        try:
            await self._ws.close()
        finally:
            await self._session.close()


class CDPSession:
    """命令 id 匹配 + 事件分发。

    两个容易踩的坑，都在这里处理：
        1. **事件会早到**：`Page.loadEventFired` 可能在 `navigate` 返回之前就到达。
           所以必须先注册等待者再发命令，并且把已经收到的事件缓存起来 ——
           否则会一直等到超时（页面明明已经加载完了）。
        2. **响应会乱序**：CDP 允许并发命令，响应按完成顺序返回，不保证与请求
           同序。所以必须按 `id` 匹配，不能假设"下一个响应就是我发的这条"。
    """

    def __init__(self, transport: Transport, timeout: float = DEFAULT_TIMEOUT):
        self._transport = transport
        self._timeout = timeout
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._event_waiters: Dict[str, List[asyncio.Future]] = {}
        self._event_backlog: Dict[str, List[Dict[str, Any]]] = {}
        self._reader: Optional[asyncio.Task] = None
        self._closed = False

    def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        while not self._closed:
            try:
                raw = await self._transport.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._closed:
                    logger.debug("CDP 读取循环结束：%s", exc)
                self._fail_all(CDPError(f"CDP 连接中断：{exc}"))
                return

            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue

            if "id" in msg:
                future = self._pending.pop(msg["id"], None)
                if future and not future.done():
                    future.set_result(msg)
                continue

            method = msg.get("method", "")
            waiters = self._event_waiters.pop(method, [])
            if waiters:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.set_result(msg)
            else:
                # 没人等：缓存起来，等下一个 wait_for_event 来取。
                # 这就是"事件早到"不会丢事件的原因。
                self._event_backlog.setdefault(method, []).append(msg)

    def _fail_all(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def send_command(
        self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        if self._closed:
            raise CDPError("CDP 会话已关闭")
        self.start()

        self._next_id += 1
        cmd_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = future

        try:
            await self._transport.send(
                json.dumps({"id": cmd_id, "method": method, "params": params or {}})
            )
        except Exception:
            # 发送就失败了，这个 future 永远不会有人来 set。
            # 不摘掉的话，读循环后面收到它的响应会 set 到一个无人 await 的
            # future 上，表现为 "Future exception was never retrieved" 噪音。
            self._pending.pop(cmd_id, None)
            raise

        try:
            msg = await asyncio.wait_for(future, timeout or self._timeout)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise CDPError(f"CDP 命令超时：{method}") from None

        if "error" in msg:
            raise CDPError(f"CDP 命令失败 {method}: {msg['error']}")
        return msg.get("result", {})

    async def wait_for_event(self, method: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        """等一个事件。已经缓存过就直接返回（处理"事件早于等待"）。"""
        backlog = self._event_backlog.get(method)
        if backlog:
            return backlog.pop(0)

        self.start()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._event_waiters.setdefault(method, []).append(future)
        try:
            return await asyncio.wait_for(future, timeout or self._timeout)
        except asyncio.TimeoutError:
            raise CDPError(f"等待事件超时：{method}") from None
        finally:
            waiters = self._event_waiters.get(method, [])
            if future in waiters:
                waiters.remove(future)

    async def evaluate(self, expression: str, timeout: Optional[float] = None) -> Any:
        """在页面里执行 JS 并取回值（只支持可 JSON 序列化的返回值）。"""
        result = await self.send_command(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise CDPError(f"页面内 JS 抛异常：{result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    async def close(self) -> None:
        self._closed = True
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        await self._transport.close()


async def list_targets(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, session=None) -> List[Dict[str, Any]]:
    """列出浏览器里可用的页面 target。

    `session` 可注入（测试用），不传就自己开一个短连接。
    """
    import aiohttp

    url = f"http://{host}:{port}/json/list"
    own_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        async with session.get(url, timeout=5) as resp:
            if resp.status != 200:
                raise CDPError(f"{url} 返回 {resp.status}")
            return await resp.json()
    except aiohttp.ClientConnectorError as exc:
        raise CDPError(
            f"连不上 Chrome 的调试端口 {host}:{port} —— "
            f"请先启动带 --remote-debugging-port={port} 的 Chrome"
        ) from exc
    finally:
        if own_session:
            await session.close()


def pick_page_target(targets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """挑一个 type=page 且有 ws 地址的 target。

    优先复用已有标签页而不是新开：用户的 Chrome 里通常已经有登录态，
    新开标签页反而可能落到不同的 profile 上下文里。
    """
    for target in targets:
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target
    return None


class CDPBrowser:
    """语义层：打开一个 URL，等它稳定，把渲染后的 HTML 取回来。"""

    def __init__(self, session: CDPSession, settle_ms: int = 800):
        self._session = session
        # 页面 load 之后 SPA 往往还要拉几轮 XHR 才有内容。
        # 这个等待是"经验值"：太短会拿到空壳，太长会让扫描慢得离谱。
        self._settle_ms = settle_ms

    @classmethod
    async def connect(
        cls,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        settle_ms: int = 800,
    ) -> "CDPBrowser":
        targets = await list_targets(host, port)
        target = pick_page_target(targets)
        if target is None:
            raise CDPError("浏览器里没有可用的页面 target（type=page）")

        transport = await AiohttpTransport.connect(target["webSocketDebuggerUrl"], timeout=timeout)
        session = CDPSession(transport, timeout=timeout)
        session.start()

        browser = cls(session, settle_ms=settle_ms)
        await session.send_command("Page.enable")
        # Network 只为了拿主文档的真实状态码：渲染后的页面 DOM 里没有状态码，
        # 而四字段判定必须比对 e_code/m_code。
        await session.send_command("Network.enable")
        return browser

    async def fetch(self, url: str, timeout: Optional[float] = None) -> tuple:
        """返回 `(status, html)`。status 取自主文档的网络响应，取不到时为 None。"""
        timeout = timeout or DEFAULT_TIMEOUT
        status_box: Dict[str, Any] = {}

        # 先注册等待者再导航：loadEventFired 可能在 navigate 返回前就到
        load_waiter = asyncio.ensure_future(self._session.wait_for_event("Page.loadEventFired", timeout))

        try:
            nav = await self._session.send_command("Page.navigate", {"url": url}, timeout=timeout)
            if nav.get("errorText"):
                load_waiter.cancel()
                raise CDPError(f"导航失败 {url}：{nav['errorText']}")

            try:
                await load_waiter
            except CDPError:
                # 有的页面永远不触发 load（长轮询、被拦），不该因此丢掉已有内容。
                # 继续往下取 DOM，能取到什么算什么。
                logger.debug("页面 %s 未在超时内触发 loadEventFired，仍尝试取 DOM", url)

            if self._settle_ms:
                await asyncio.sleep(self._settle_ms / 1000)

            html = await self._session.evaluate(
                "document.documentElement ? document.documentElement.outerHTML : ''",
                timeout=timeout,
            )
            status = await self._read_status(timeout)
        finally:
            if not load_waiter.done():
                load_waiter.cancel()

        return status, html or ""

    async def _read_status(self, timeout: float) -> Optional[int]:
        """用 Navigation Timing 取主文档状态码。

        为什么不用 `Network.responseReceived`：那需要维护一个跨命令的事件监听，
        而我们的 session 是"命令-响应"模型，事件缓存会被后续调用消耗掉。
        `performance.getEntriesByType('navigation')[0].responseStatus` 是 Chrome 109+
        的标准字段，一次 evaluate 就能拿到，且和 DOM 来自同一次导航。
        """
        try:
            value = await self._session.evaluate(
                "(performance.getEntriesByType('navigation')[0]||{}).responseStatus",
                timeout=timeout,
            )
            return int(value) if value else None
        except (CDPError, TypeError, ValueError):
            return None

    async def close(self) -> None:
        await self._session.close()
