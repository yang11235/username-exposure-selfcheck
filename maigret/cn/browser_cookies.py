r"""从本机已登录的浏览器取 Cookie（走 CDP，内存直用，不落盘）。

## 为什么走 CDP，而不是去解密浏览器的 Cookies 数据库

「读 Chrome 的 Cookies 文件」看起来更直接，实际是条死路：

1. **解不开。** Chrome 80 之后 Cookie 值用 AES-256-GCM 加密，主密钥放在
   `Local State` 里、再用 Windows DPAPI（macOS 是 Keychain）保护。要解密就得
   引 `cryptography` —— 而本项目「零新增依赖」是硬约束（见 `executors/cdp.py`
   开头：连 Playwright 都因为 200MB 内核被否掉了）。
2. **解开了也不稳。** Chrome 127 起加了 app-bound 加密（值前缀 `v20`），密钥
   由浏览器进程自己持有，读库这条路在新版上已经走不通。跟着它的加密方案改，
   等于签一份终身维护合同。
3. **数据库被锁。** 浏览器运行时 SQLite 文件是独占的，只能先复制再读——
   又多一份明文副本落在磁盘上。

CDP 拿到的值是**浏览器自己解密好的**，不受加密方案变更影响；浏览器开着就能读；
全程不进磁盘。

## 代价

用户必须让 Chrome 带 `--remote-debugging-port=9222` 启动。这一点无法绕过——
没有调试端口就没有任何官方途径从浏览器里取数据。`browser_info()` 返回 None 时，
调用方要如实告诉用户「浏览器没开调试端口」，而不是静默退回"没有 Cookie"。

## 三条硬约束

1. **只连回环地址。** `MAIGRET_CDP_HOST` 被配成公网地址时直接拒绝：CDP 端口
   是**无鉴权**的，连上去就能读走整台浏览器里所有登录态。这个端口只能在本机。
2. **不落盘、不进日志。** 只保留计数和域名，值从头到尾不写文件、不打日志。
   本项目的边界是「只查自己、不分享、不做云端」，Cookie 是最敏感的那类凭证。
3. **只读。** 这里只有 `Storage.getCookies`，没有任何写 Cookie、开标签页、
   执行脚本的代码路径，也不应该加。
"""

import logging
from http.cookies import Morsel
from typing import Any, Dict, List, Optional

from .executors.browser import cdp_host, cdp_port
from .executors.cdp import AiohttpTransport, CDPSession

logger = logging.getLogger(__name__)

# 连浏览器、取一次 Cookie 的时间上限。比页面加载短：这一步只做一次握手 + 一次
# 命令往返，慢到这个程度说明端口上根本不是浏览器。
DEFAULT_TIMEOUT = 8.0

_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


class BrowserCookieError(RuntimeError):
    """取不到 Cookie 时的统一异常。

    单独一个类型是为了让上层能区分「浏览器没开」和「浏览器开了但命令失败」——
    这两种情况用户要做的事完全不同（去开调试端口 vs 去看浏览器是否被别的
    程序占用）。
    """


def is_loopback(host: str) -> bool:
    """CDP 端口无鉴权，只允许连本机。"""
    host = (host or "").strip().lower()
    if host in _LOOPBACK_NAMES:
        return True
    # 127.0.0.0/8 整段都算回环
    if host.startswith("127."):
        parts = host.split(".")
        return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    return False


async def browser_info(host: Optional[str] = None, port: Optional[int] = None,
                       timeout: float = DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """探测调试端口上有没有浏览器。有则返回 `/json/version` 的内容。

    返回 None 表示「连不上」——这是正常情况（用户没开调试端口），不是错误，
    所以不抛异常，让调用方自己决定怎么提示。
    """
    host = host or cdp_host()
    port = port or cdp_port()
    if not is_loopback(host):
        raise BrowserCookieError(
            f"拒绝连接非本机地址 {host}：CDP 端口没有鉴权，连上去就能读走"
            "整台浏览器里的所有登录态"
        )

    import aiohttp

    url = f"http://{host}:{port}/json/version"
    try:
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                return await response.json(content_type=None)
    except Exception as exc:
        logger.debug("CDP 端口 %s:%s 不可用：%s", host, port, exc)
        return None


def _filter_cookies(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """过滤 CDP 返回的原始 Cookie 列表。

    抽成纯函数是为了能离线测试 —— `collect()` 本身要连浏览器，单测里不该依赖
    本机有没有开着调试端口。

    丢两类：
        空名      CDP 确实会返回 name 为空的条目（分区 Cookie 的残留），
                  发给站点可能让某些网关直接 400，且它没有任何作用
        已过期    expires 是 0 到 1 之间的小数字，是"会话残留"而不是时间戳，
                  带上只是多一层无效请求头
    """
    cookies = []
    for item in raw:
        name = str(item.get("name") or "").strip()
        domain = str(item.get("domain") or "").strip()
        if not name or not domain:
            continue
        expires = item.get("expires")
        if isinstance(expires, (int, float)) and 0 < expires < 1:
            continue
        cookies.append(item)
    return cookies


async def collect(host: Optional[str] = None, port: Optional[int] = None,
                  timeout: float = DEFAULT_TIMEOUT) -> List[Dict[str, Any]]:
    """取出浏览器里所有 Cookie，返回 CDP 的原始结构（已过滤空名/过期）。

    用 `Storage.getCookies` 而不是 `Network.getCookies`：前者是浏览器级命令，
    一次拿全部域名，不需要先开标签页、也不需要知道要查哪些站。扫描前我们
    并不知道用户会命中哪些域名，逐个域名问一遍反而更慢。

    值只在内存里过一遍，不写文件、不进日志。
    """
    info = await browser_info(host=host, port=port, timeout=timeout)
    if not info:
        raise BrowserCookieError(
            f"连不上 {host or cdp_host()}:{port or cdp_port()} —— 浏览器可能没开"
            "调试端口（需要用 --remote-debugging-port=9222 启动 Chrome）"
        )

    ws_url = info.get("webSocketDebuggerUrl")
    if not ws_url:
        raise BrowserCookieError("调试端口没有返回 webSocketDebuggerUrl，无法建立 CDP 连接")

    transport = await AiohttpTransport.connect(ws_url, timeout=timeout)
    session = CDPSession(transport, timeout=timeout)
    session.start()
    try:
        result = await session.send_command("Storage.getCookies", {}, timeout=timeout)
    except Exception as exc:
        raise BrowserCookieError(f"读取 Cookie 失败：{exc}") from exc
    finally:
        await session.close()

    cookies = _filter_cookies(result.get("cookies") or [])

    logger.info(
        "从浏览器取到 %d 条 Cookie，覆盖 %d 个域名",
        len(cookies),
        len({c.get("domain") for c in cookies}),
    )
    return cookies


def summarize(cookies: List[Dict[str, Any]], limit: int = 0) -> Dict[str, int]:
    """按域名统计条数，用于界面上告诉用户「读到了什么」。

    只暴露域名和数量：界面上永远不显示 Cookie 值。
    """
    counts: Dict[str, int] = {}
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").strip()
        if not domain:
            continue
        counts[domain] = counts.get(domain, 0) + 1
    ordered = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    if limit and len(ordered) > limit:
        ordered = dict(list(ordered.items())[:limit])
    return ordered


def to_cookiejar(cookies: List[Dict[str, Any]]):
    """把 CDP 的 Cookie 列表装进 aiohttp 的 CookieJar。

    必须逐条用 `Morsel` 而不是 `update_cookies({name: value}, ...)`：
    后者会把 domain 改成 response_url 的 host，`.bilibili.com` 这种通配域
    就退化成了 `www.bilibili.com`，于是 `api.bilibili.com` 收不到 ——
    表现为"Cookie 明明读到了，B 站还是判不出来"，而且极难查。
    """
    import aiohttp
    from yarl import URL

    jar = aiohttp.CookieJar(unsafe=False)

    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        domain = str(cookie.get("domain") or "").strip()
        if not name or not domain:
            continue

        path = str(cookie.get("path") or "/").strip() or "/"
        morsel = Morsel()
        morsel.set(name, str(cookie.get("value") or ""), str(cookie.get("value") or ""))
        morsel["domain"] = domain
        morsel["path"] = path
        morsel["secure"] = bool(cookie.get("secure"))
        morsel["httponly"] = bool(cookie.get("httpOnly"))

        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            morsel["expires"] = str(int(expires))

        # URL 的 host 不能带前导点；Morsel 上的 domain 才是真正决定作用域的那个。
        host = domain.lstrip(".") or "localhost"
        scheme = "https" if cookie.get("secure") else "http"
        try:
            jar.update_cookies({name: morsel}, response_url=URL(f"{scheme}://{host}{path}"))
        except Exception as exc:  # pragma: no cover - 单条坏 cookie 不该毁掉整批
            logger.debug("跳过一条无法装载的 Cookie（%s@%s）：%s", name, domain, exc)

    return jar
