"""浏览器执行器：把 CDP 包装成判定层认的 `(status, html)`。

职责边界（写进代码是因为这条边界是法律与伦理边界，不是技术选择）：

    只做**只读的个人主页访问** —— 确认存在 + 读取平台自己公开展示的字段。
    不做内容抓取、不做批量遍历、不下载媒体、不执行用户没要求的点击。
    这个模块里不存在任何"点关注""发消息""改设置"的代码路径，也不应该加。

为什么要浏览器执行器（文档 §3.2.3）：

    小红书 / 抖音 / 快手 这类站点的判定依赖签名参数（X-S、a_bogus），
    而签名算法一直在变——逆向等于签终身维护合同（文档 §1.2⑥）。
    正确做法是在真实浏览器里让页面自己算签名，我们只读渲染结果。
    顺带解决登录态、指纹一致性、体积三个问题。

降级策略：

    浏览器不可用（没启动 / 端口不通 / 站点在浏览器里也加载失败）时，
    返回 UNKNOWN + 结构化原因，**绝不退回 HTTP 猜测**。
    用 HTTP 结果冒充浏览器结果，会把"被拦"变成"不存在"，是最坏的方向。
"""

import ipaddress
import logging
import os
from typing import Optional, Tuple
from urllib.parse import urlparse

from .cdp import CDPBrowser, CDPError

logger = logging.getLogger(__name__)

# 允许用户改端口（有人习惯 9222 之外的端口，或同时开多个 Chrome）
ENV_PORT = "MAIGRET_CDP_PORT"
ENV_HOST = "MAIGRET_CDP_HOST"
# 关掉浏览器执行器，让 browser 站点退回"未判定"
ENV_DISABLE = "MAIGRET_CN_BROWSER"
# 一次运行里最多做多少次"升级重试"（见 MAX_ESCALATIONS）
ENV_MAX_ESCALATIONS = "MAIGRET_CN_MAX_ESCALATIONS"

DEFAULT_PORT = 9222
DEFAULT_HOST = "127.0.0.1"

# 页面 load 之后的静置时间：SPA 还要拉几轮 XHR 才有内容。
# 太短拿到空壳（判不出），太长会让扫描慢得不可用。800ms 是实测折中。
SETTLE_MS = 800

# 一次运行里最多做多少次"HTTP 被判拦截 → 换浏览器重试"。
#
# 为什么要设上限：被风控的站往往不是一两个。真遇到大面积拦截时，每个站都开
# 一次浏览器导航（含 800ms 静置 + 最多 30s 超时），整次扫描会从几分钟变成几小时，
# 而且会在用户屏幕上疯狂开标签页。上限之外保持原来的 BLOCKED 结论——
# 那是诚实的，也不会误导用户。
#
# 50 是折中：足够覆盖用户真正关心的主流平台（中文站点里需要升级的通常个位数），
# 又不会把一次异常扫描变成浏览器压测。
MAX_ESCALATIONS = 50

# 进程级计数。用模块变量而不是实例变量，是因为扫描可能重建 executor。
_escalations_used = 0


def enabled() -> bool:
    return (os.environ.get(ENV_DISABLE) or "").strip().lower() not in ("0", "false", "off")


def max_escalations() -> int:
    raw = os.environ.get(ENV_MAX_ESCALATIONS)
    if raw is None or not str(raw).strip():
        return MAX_ESCALATIONS
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning("%s 不是合法整数，回退到 %d", ENV_MAX_ESCALATIONS, MAX_ESCALATIONS)
        return MAX_ESCALATIONS


def reset_escalations() -> None:
    """重置升级预算（每次扫描开始时调用，测试里也用它保证互不影响）。"""
    global _escalations_used
    _escalations_used = 0


def escalations_left() -> int:
    return max(0, max_escalations() - _escalations_used)


def _consume_escalation() -> bool:
    global _escalations_used
    if _escalations_used >= max_escalations():
        return False
    _escalations_used += 1
    return True


def is_public_url(url: Optional[str]) -> bool:
    """这个 URL 值不值得动用用户的浏览器？

    三类地址一律不升级：

    1. **本地/内网地址**（localhost、127.0.0.0/8、10/8、172.16/12、192.168/16、
       ::1、*.local）。理由有三层，任何一层都足够：
       - 语义：本地服务器不会做风控。它被判 BLOCKED 说明**我们的规则或
         判定逻辑有问题**，用浏览器重试只是把 bug 藏起来。
       - 隐私：把内网地址塞进用户的浏览器，等于让内部 URL 进入浏览历史、
         DNS 缓存和代理日志。这是不该发生的事。
       - 工程：导航一个没人监听的本地端口要等满超时，几千个站下来整次
         扫描会被拖死。
    2. **非 http(s) 协议**（file:、data:、chrome: 等）：不是网络探测对象。
    3. **没有主机名**：同上。

    这个判断不做 DNS 解析——解析本身就要发网络请求，而且会引入超时。
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return False
    if host.endswith(".local") or host.endswith(".internal"):
        return False

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # 不是 IP 字面量，当域名处理（不做解析，见上）。
        return True
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def cdp_port() -> int:
    try:
        return int(os.environ.get(ENV_PORT) or DEFAULT_PORT)
    except (TypeError, ValueError):
        logger.warning("%s 不是合法端口，回退到 %d", ENV_PORT, DEFAULT_PORT)
        return DEFAULT_PORT


def cdp_host() -> str:
    return (os.environ.get(ENV_HOST) or DEFAULT_HOST).strip()


class BrowserExecutor:
    """一个进程内复用一个浏览器连接。

    为什么不每次探测都连一次：CDP 连接本身有成本（WebSocket 握手 + Page/Network
    域启用），而扫描时同一个浏览器会被连续用很多次。复用的代价是要处理
    "连接中途断了"——所以 `fetch` 失败一次会重连再试一次，仍失败才放弃。
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._browser: Optional[CDPBrowser] = None
        # 熔断：连不上浏览器时不要每个站点都重试一次握手。
        # 一次扫描几千个站，每次多等 5 秒超时 = 整次扫描不可用。
        self._unavailable = False

    @property
    def unavailable(self) -> bool:
        return self._unavailable

    async def _ensure(self) -> CDPBrowser:
        if self._unavailable:
            raise CDPError("浏览器不可用（此前连接失败，本次运行不再重试）")
        if self._browser is None:
            try:
                self._browser = await CDPBrowser.connect(
                    host=self.host, port=self.port, settle_ms=SETTLE_MS
                )
            except CDPError:
                self._unavailable = True
                raise
            logger.debug("已连接 Chrome CDP：%s:%d", self.host, self.port)
        return self._browser

    async def fetch(self, url: str, timeout: float = 30.0) -> Tuple[Optional[int], str]:
        """打开 URL 并返回 `(status, html)`。

        失败会重连重试一次：Chrome 崩溃重启、用户手动关标签页都会让
        长连接失效，而扫描是长流程，不该因为一次断连就丢掉后面所有站点。
        """
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                browser = await self._ensure()
                return await browser.fetch(url, timeout=timeout)
            except CDPError as exc:
                last_error = exc
                logger.debug("浏览器取 %s 失败（第 %d 次）：%s", url, attempt, exc)
                await self.close()
        raise CDPError(str(last_error))

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # 关闭失败不影响结果，连接会被 Chrome 自己回收
                pass
            self._browser = None


# 进程级单例：扫描循环是串行的站点队列，一个连接够用。
_default_executor: Optional[BrowserExecutor] = None


def default_executor() -> BrowserExecutor:
    global _default_executor
    if _default_executor is None:
        _default_executor = BrowserExecutor(host=cdp_host(), port=cdp_port())
    return _default_executor


async def close_default_executor() -> None:
    global _default_executor
    if _default_executor is not None:
        await _default_executor.close()
        _default_executor = None


async def fetch_with_browser(url: str, timeout: float = 30.0) -> Tuple[Optional[int], str]:
    """给判定层用的入口：拿不到就抛 CDPError，由调用方翻译成 UNKNOWN 原因。"""
    if not enabled():
        raise CDPError("浏览器执行器已被 %s 关闭" % ENV_DISABLE)
    return await default_executor().fetch(url, timeout=timeout)


# ---------------------------------------------------------------------------
# 与扫描流程的对接
# ---------------------------------------------------------------------------
def browser_available() -> bool:
    """浏览器是否值得一试（未关闭、未熔断）。不发起连接，只做本地判断。"""
    return enabled() and not default_executor().unavailable


async def fetch_site_response(site, results_info, checker, options, log):
    """返回 `(html, status, error)` —— 与上游 checker.check() 同构。

    两种用法，都在这里收口：

    1. **`executor: browser` 的站点**：直接走浏览器。它们要么需要页面自己算签名
       （小红书/抖音），要么纯 HTTP 必然被拦。

    2. **升级重试**：普通 HTTP 探测被判 BLOCKED 时，用浏览器再试一次。
       为什么值得多这一次请求：被风控的站往往是**用户最关心**的主流平台
       （Gitee、语雀实测都是这样），只因为指纹不像浏览器就判"未判定"，
       用户会认为工具坏了。而浏览器用的是用户自己的 Chrome，指纹天然一致。

       失败不改变结论：浏览器也拿不到就保持原来的 BLOCKED，绝不用浏览器结果
       去"洗白"一个本来就没判出来的站点。
    """
    from ..hook import spec_for
    from ..verdict import Reason, judge

    spec = spec_for(site)
    url = results_info.get("url_probe") or results_info.get("url_user")
    username = results_info.get("username", "")
    timeout = float(options.get("timeout") or 30)

    if spec is not None and spec.executor == "browser":
        # 关键：不可用时**绝不退回 HTTP**。
        # 小红书/抖音这类站点的 HTTP 探测必然拿到空壳或 403，判出来是
        # "不存在"或"未判定"，而真相是"我们没法判"。前者会误导用户，
        # 后者才是诚实的。所以这里直接返回错误，让判定层输出 UNKNOWN。
        if not browser_available():
            return "", None, CDPError(
                "该站点需要浏览器执行器，但浏览器不可用"
                "（未启动 --remote-debugging-port 的 Chrome，或被环境变量关闭）"
            )
        try:
            status, html = await fetch_with_browser(url, timeout=timeout)
            return html, status, None
        except CDPError as exc:
            # 明确告诉用户"没判出来是因为浏览器没起来"，而不是静默降级成
            # "不存在"。这是这个执行器唯一正确的失败姿势。
            log.warning("站点 %s 需要浏览器执行器，但浏览器不可用：%s", site.name, exc)
            return "", None, exc

    response = await checker.check()

    if spec is None or not browser_available():
        return response

    html, status, error = response
    if error is not None:
        return response

    verdict = judge(_ResponseLike(status, html or ""), spec, username=username)
    if verdict.reason != Reason.BLOCKED:
        return response

    # 闸门一：本地/内网地址不升级。
    # 这类地址被判 BLOCKED 是**我们的**问题（规则错、判定逻辑错），不是风控。
    # 用浏览器重试只会把 bug 藏起来，还会把内网 URL 灌进用户浏览器。
    if not is_public_url(url):
        log.debug("站点 %s 的目标不是公网地址（%s），不做浏览器升级", site.name, url)
        return response

    # 闸门二：升级次数预算。大面积拦截时不能把一次扫描变成浏览器压测。
    if not _consume_escalation():
        log.warning(
            "站点 %s 被拦截，但本次运行的浏览器升级次数已用尽（上限 %d），"
            "保留“未判定”结论。可用 %s 调整上限。",
            site.name,
            max_escalations(),
            ENV_MAX_ESCALATIONS,
        )
        return response

    log.info("站点 %s 被风控拦截，改用浏览器重试", site.name)
    try:
        b_status, b_html = await fetch_with_browser(url, timeout=timeout)
    except CDPError as exc:
        log.debug("站点 %s 浏览器重试失败：%s", site.name, exc)
        return response

    # 浏览器也拿到拦截页就不必替换：原结论已经是 BLOCKED，替换只会丢证据。
    b_verdict = judge(_ResponseLike(b_status, b_html or ""), spec, username=username)
    if b_verdict.reason == Reason.BLOCKED:
        return response
    return b_html, b_status, None


class _ResponseLike:
    """judge() 只认 .status / .text 两个属性。"""

    __slots__ = ("status", "text")

    def __init__(self, status, text):
        self.status = status
        self.text = text
