# Standard library imports
import ast
import asyncio
import logging
import os
import random
import re
import ssl
import sys
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import Mock
from urllib.parse import quote, urlparse

# Third party imports
import aiodns
from alive_progress import alive_bar
from aiohttp import ClientSession, TCPConnector, http_exceptions
from aiohttp.resolver import ThreadedResolver
from aiohttp.client_exceptions import (
    ClientConnectorDNSError,
    ClientConnectorError,
    ClientPayloadError,
    ServerDisconnectedError,
)
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
from socid_extractor import extract, mutate_url  # type: ignore[import-not-found]

# Local imports
from . import errors
from .activation import ParsingActivator, import_aiohttp_cookies
from .cn.checkpoint import ScanCheckpoint, checkpoint_path
from .cn.executors.avatar import attach_avatar_evidence
from .cn.executors.browser import fetch_site_response, reset_escalations
from .cn.extract import extract_evidence, summarize
from .cn.hook import apply_cn_verdict, spec_for
from .cn.verdict import Reason
from .error_detection import detect_error_page
from .errors import CheckError
from .executors import AsyncioQueueGeneratorExecutor
from .result import MaigretCheckResult, MaigretCheckStatus, KeywordMatchStatus, SiteResult
from .sites import MaigretDatabase, MaigretSite
from .utils import ascii_data_display, get_random_user_agent, is_plausible_username


_DNS_ERROR_MARKERS = (
    "could not contact dns servers",  # aiohttp + aiodns wording
    "name or service not known",       # glibc getaddrinfo
    "nodename nor servname",           # macOS getaddrinfo
    "temporary failure in name resolution",  # glibc EAI_AGAIN
    "getaddrinfo failed",              # generic socket error
)


def _is_dns_error(exc: Exception) -> bool:
    """Classify a ClientConnectorError as DNS-class or not.

    Prefers the typed aiohttp subclass; falls back to substring matching on
    the exception text for resolver/getaddrinfo errors that don't surface as
    ClientConnectorDNSError. The substrings are the OS/aiodns wordings
    observed in the wild.
    """
    if isinstance(exc, ClientConnectorDNSError):
        return True
    text = str(exc).lower()
    return any(m in text for m in _DNS_ERROR_MARKERS)


# The HTTP transports disagree about what a SOCKS5 proxy URL means.
#
#   python_socks (via aiohttp_socks, used by SimpleAiohttpChecker)
#     accepts exactly socks5/socks4/http and raises
#     ValueError('Invalid scheme component: socks5h') on anything else, so
#     socks5h:// is a hard crash before a single request is made. Its rdns
#     flag defaults to True for SOCKS5, so socks5:// there already means
#     proxy-side DNS.
#
#   libcurl (via curl_cffi, used by CurlCffiChecker for tls_fingerprint sites)
#     keeps the classic distinction: socks5:// resolves the hostname on the
#     client and passes an address to the proxy, socks5h:// passes the
#     hostname and lets the proxy resolve it.
#
#   requests (via PySocks, used by the database auto-update in db_updater)
#     draws the same distinction as libcurl.
#
# So a single `--proxy socks5://...` resolves most of the database at the
# proxy but the tls_fingerprint sites locally: their hostnames leak to the
# local resolver, and geo-balanced hosts get resolved for the wrong network.
# See issue #2955.
#
# Normalizing the scheme per transport makes both spellings mean proxy-side
# DNS everywhere, so users need not know which transport handles which site.
# Only SOCKS5 is remapped: python_socks defaults rdns to False for SOCKS4,
# so rewriting socks4 would change behavior instead of aligning it.
PYTHON_SOCKS_TRANSPORT = 'python_socks'
LIBCURL_TRANSPORT = 'libcurl'
REQUESTS_TRANSPORT = 'requests'

_PROXY_SCHEME_ALIASES = {
    PYTHON_SOCKS_TRANSPORT: {'socks5h': 'socks5'},
    LIBCURL_TRANSPORT: {'socks5': 'socks5h'},
    REQUESTS_TRANSPORT: {'socks5': 'socks5h'},
}


def normalize_proxy_scheme(proxy: Optional[str], transport: str) -> Optional[str]:
    """Rewrite a proxy URL's scheme to the spelling `transport` understands.

    Only the scheme is rewritten; host, port, credentials and path are passed
    through as given, as are non-SOCKS5 schemes, schemeless values and empty
    values.
    """
    if not proxy:
        return proxy

    scheme, separator, remainder = proxy.partition('://')
    if not separator:
        return proxy

    replacement = _PROXY_SCHEME_ALIASES[transport].get(scheme.lower())
    if replacement is None:
        return proxy

    return f'{replacement}://{remainder}'


SUPPORTED_IDS = (
    "username",
    # 站内数字 ID。中文站里掘金（user_id）、网易云（uid）的个人主页地址
    # 只接受数字 ID，昵称拼不出来。上游没有这个类型，本分支补上——
    # 否则 sites_cn.json 里声明了 id_type=user_id 的规则永远查不到：
    # --id-type 的 choices 里没有它，CLI 会直接拒绝。
    "user_id",
    "yandex_public_id",
    "gaia_id",
    "vk_id",
    "ok_id",
    "wikimapia_uid",
    "steam_id",
    "uidme_uguid",
    "yelp_userid",
    "orcid",
    "qq_id",
    "bilibili_id",
)

BAD_CHARS = "#"


def build_cloudflare_bypass_config(
    settings_obj: Optional[Any], force_enable: bool = False
) -> Optional[Dict[str, Any]]:
    """Resolve Cloudflare webgate config from settings + CLI flag.

    Returns ``None`` when bypass is inactive or no usable module is configured.
    Otherwise returns a dict consumed by ``CloudflareWebgateChecker``:

      - ``trigger_protection``: list of ``site.protection`` values that
        activate the bypass (e.g. ``["cf_js_challenge", "cf_firewall", "webgate"]``)
      - ``modules``: ordered list of backend modules to try; each entry has
        ``name``, ``method`` (``json_api`` for FlareSolverr, ``url_rewrite``
        for CloudflareBypassForScraping), and a method-specific ``url`` plus
        optional ``max_timeout_ms``.
      - ``session_prefix``: prefix for FlareSolverr session reuse.
    """
    raw = {}
    if settings_obj is not None:
        raw = getattr(settings_obj, "cloudflare_bypass", {}) or {}
    enabled = bool(force_enable) or bool(raw.get("enabled", False))
    if not enabled:
        return None

    modules_raw = raw.get("modules") or []
    valid_modules: List[Dict[str, Any]] = []
    for module in modules_raw:
        method = module.get("method")
        url = module.get("url")
        if method == "json_api" and url:
            valid_modules.append(dict(module))
        elif method == "url_rewrite" and url and "{url}" in url:
            valid_modules.append(dict(module))
    if not valid_modules:
        return None

    trigger = raw.get("trigger_protection") or [
        "cf_js_challenge",
        "cf_firewall",
        "webgate",
    ]
    return {
        "trigger_protection": list(trigger),
        "modules": valid_modules,
        "session_prefix": raw.get("session_prefix", "maigret"),
    }


class CheckerBase:
    def __init__(self, *args, **kwargs):
        self.logger = kwargs.get('logger', Mock())
        # Defaults for the request fields populated by .prepare(). Set here
        # so subclasses with a partial prepare() (e.g. AiodnsDomainResolver
        # only assigns url) still have predictable attribute access.
        self.url = None
        self.headers = None
        self.allow_redirects = True
        self.timeout = 0
        self.method = 'get'
        self.payload = None
        self.encoding = None


class SimpleAiohttpChecker(CheckerBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy = normalize_proxy_scheme(kwargs.get('proxy'), PYTHON_SOCKS_TRANSPORT)
        self.cookie_jar = kwargs.get('cookie_jar')
        # 'async' (default) uses aiohttp's DefaultResolver, which is AsyncResolver
        # (powered by aiodns / c-ares) when aiodns is installed. 'threaded' uses
        # ThreadedResolver, which wraps the OS getaddrinfo via a threadpool —
        # slower for high concurrency, but respects the system DNS config
        # (resolv.conf, Windows network adapter settings) instead of having
        # aiodns rediscover it. See issue #2688: aiodns can fail to find any
        # DNS server on Windows / VPN / corporate networks, producing
        # "Could not contact DNS servers" for every site.
        self.dns_resolver = kwargs.get('dns_resolver', 'async')
        # 请求预热地址：先访问一次，让服务端下发 Cookie（B 站搜索接口不带
        # buvid3 会直接 412）。由 cn overlay 的 bootstrap_url 驱动，为空则跳过。
        self.bootstrap_url = kwargs.get('bootstrap_url') or None

    def prepare(self, url, headers=None, allow_redirects=True, timeout=0, method='get', payload=None, encoding=None):
        self.url = url
        self.headers = headers
        self.allow_redirects = allow_redirects
        self.timeout = timeout
        self.method = method
        self.payload = payload
        self.encoding = encoding
        return None

    async def close(self):
        pass

    async def _warm_up(self, session) -> None:
        """先访问一次 bootstrap_url，把服务端下发的 Cookie 收进会话。

        失败一律吞掉：预热只影响成功率，不影响结论。预热失败时主请求照常发，
        最坏结果是 412/403 —— 判定层会把那种情况判成 UNKNOWN，而不是"不存在"。

        必须在**同一个 session** 里做，否则 cookie jar 不同，等于没预热。
        """
        if not self.bootstrap_url:
            return
        timeout = min(self.timeout or 10, 10)
        # 必须沿用主请求的 headers（尤其 User-Agent）：站点下发的 Cookie 往往
        # 和签发时的 UA 绑定，预热用一个 UA、主请求换另一个，等于白预热——
        # B 站就是这么回 412 的。
        headers = dict(self.headers or {})
        headers.pop('Connection', None)
        try:
            await self._make_request(
                session,
                self.bootstrap_url,
                headers,
                True,
                timeout,
                'get',
                self.logger,
            )
        except Exception as exc:  # noqa: BLE001 — 预热失败不是错误
            self.logger.debug("预热 %s 失败（忽略）：%s", self.bootstrap_url, exc)

    async def _make_request(
        self, session, url, headers, allow_redirects, timeout, method, logger, payload=None
    ) -> Tuple[Optional[str], int, Optional[CheckError]]:
        if method.lower() == 'get':
            request_method = session.get
        elif method.lower() == 'post':
            request_method = session.post
        elif method.lower() == 'head':
            request_method = session.head
        else:
            request_method = session.get

        kwargs = {
            'url': url,
            'headers': headers,
            'allow_redirects': allow_redirects,
            'timeout': timeout,
        }
        if payload and method.lower() == 'post':
            if headers and headers.get('Content-Type') == 'application/x-www-form-urlencoded':
                kwargs['data'] = payload
            else:
                kwargs['json'] = payload

        # A rotating (residential) proxy occasionally switches exit node
        # mid-request: aiohttp surfaces this as a truncated body
        # (ClientPayloadError, wrapping the underlying TransferEncodingError
        # or ContentLengthError), a dropped connection (ServerDisconnectedError),
        # or a failure to reach/handshake with the picked proxy node
        # (aiohttp_socks' ProxyConnectionError/ProxyTimeoutError — NOT
        # python_socks' same-named classes, which this connector never
        # raises). One retry on a fresh connection is enough — the pooled
        # connection is already discarded, so the retry goes out through a
        # new exit node. A generic ProxyError (e.g. bad credentials) is
        # deliberately NOT retried: it fails identically every time, so
        # retrying it would just double the cost of every check for no gain.
        transient_retries = 1
        for attempt in range(transient_retries + 1):
            try:
                async with request_method(**kwargs) as response:
                    status_code = response.status
                    # A response_url check runs with redirects disabled, so any
                    # 3xx reads as "user not found". Some sites intermittently
                    # bounce a request back to the SAME url to plant a session
                    # cookie (joyreactor.cc: 302 + `Set-Cookie: jr_captcha=1`,
                    # roughly one request in five to seven): that is not a
                    # not-found, it is a retry request. The session keeps the
                    # cookie, so one repeat returns the real page. Set-Cookie
                    # is what makes it a handshake — without it, a site that
                    # always self-redirects would just cost two requests.
                    if (
                        not allow_redirects
                        and 300 <= status_code < 400
                        and str(response.headers.get("Location", "")) == url
                        and response.headers.get("Set-Cookie")
                        and attempt < transient_retries
                    ):
                        logger.debug(f"Self-redirect to {url}, retrying")
                        continue
                    response_content = await response.content.read()
                    charset = self.encoding or response.charset or "utf-8"
                    decoded_content = response_content.decode(charset, "ignore")

                    error = CheckError("Connection lost") if status_code == 0 else None
                    logger.debug(decoded_content)

                    return decoded_content, status_code, error

            except asyncio.TimeoutError as e:
                return None, 0, CheckError("Request timeout", str(e))
            except ClientConnectorError as e:
                err_type = "Connecting failure (DNS)" if _is_dns_error(e) else "Connecting failure"
                return None, 0, CheckError(err_type, str(e))
            except ServerDisconnectedError as e:
                if attempt < transient_retries:
                    logger.debug(f"Server disconnected, retrying: {e}")
                    continue
                return None, 0, CheckError("Server disconnected", str(e))
            except (ProxyConnectionError, ProxyTimeoutError) as e:
                if attempt < transient_retries:
                    logger.debug(f"Proxy connection error, retrying: {e}")
                    continue
                return None, 0, CheckError("Proxy", str(e))
            except http_exceptions.BadHttpMessage as e:
                return None, 0, CheckError("HTTP", str(e))
            except ProxyError as e:
                return None, 0, CheckError("Proxy", str(e))
            except ClientPayloadError as e:
                if attempt < transient_retries:
                    logger.debug(f"Payload error, retrying: {e}")
                    continue
                return None, 0, CheckError("Payload", str(e))
            except KeyboardInterrupt:
                return None, 0, CheckError("Interrupted")
            except Exception as e:
                if sys.version_info.minor > 6 and (
                    isinstance(e, ssl.SSLCertVerificationError)
                    or isinstance(e, ssl.SSLError)
                ):
                    return None, 0, CheckError("SSL", str(e))
                else:
                    logger.debug(e, exc_info=True)
                return None, 0, CheckError("Unexpected", str(e))

    async def check(self) -> Tuple[Optional[str], int, Optional[CheckError]]:
        from aiohttp_socks import ProxyConnector

        # Use a real SSL context instead of ssl=False to avoid TLS fingerprinting
        # blocks by Cloudflare and similar WAFs. Certificate verification is
        # disabled to handle sites with invalid/expired certs.
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        # Build the TCPConnector with an explicit resolver when 'threaded' is
        # requested. ProxyConnector takes its own resolver kwarg too, so apply
        # the same setting on both code paths.
        resolver = ThreadedResolver() if self.dns_resolver == 'threaded' else None
        if self.proxy:
            connector = ProxyConnector.from_url(self.proxy, resolver=resolver) if resolver else ProxyConnector.from_url(self.proxy)
        else:
            connector = TCPConnector(ssl=ssl_context, resolver=resolver) if resolver else TCPConnector(ssl=ssl_context)

        async with ClientSession(
            connector=connector,
            trust_env=True,
            cookie_jar=self.cookie_jar if self.cookie_jar else None,
        ) as session:
            await self._warm_up(session)

            html_text, status_code, error = await self._make_request(
                session,
                self.url,
                self.headers,
                self.allow_redirects,
                self.timeout,
                self.method,
                self.logger,
                self.payload,
            )

            if error and str(error) == "Invalid proxy response":
                self.logger.debug(error, exc_info=True)

            return str(html_text) if html_text else '', status_code, error


class ProxiedAiohttpChecker(SimpleAiohttpChecker):
    pass


class AiodnsDomainResolver(CheckerBase):
    if sys.platform == 'win32':  # Temporary workaround for Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        loop = asyncio.get_event_loop()
        self.resolver = aiodns.DNSResolver(loop=loop)

    def prepare(self, url, headers=None, allow_redirects=True, timeout=0, method='get', payload=None, encoding=None):
        self.url = url
        return None

    async def check(self) -> Tuple[Optional[str], int, Optional[CheckError]]:
        status = 404
        error = None
        text = ''

        try:
            res = await self.resolver.query(self.url, 'A')
            text = str(res[0].host)
            status = 200
        except aiodns.error.DNSError:
            pass
        except Exception as e:
            self.logger.error(e, exc_info=True)
            error = CheckError('DNS resolve error', str(e))

        return text, status, error


from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession as CurlCffiAsyncSession


class CurlCffiChecker(CheckerBase):
    """Checker using curl_cffi to emulate browser TLS fingerprint and bypass WAF."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.browser_emulate = kwargs.get('browser_emulate', 'chrome')
        self.proxy = normalize_proxy_scheme(kwargs.get('proxy'), LIBCURL_TRANSPORT)

    def prepare(self, url, headers=None, allow_redirects=True, timeout=0, method='get', payload=None, encoding=None):
        self.url = url
        self.headers = headers
        self.allow_redirects = allow_redirects
        self.timeout = timeout
        self.method = method
        self.payload = payload
        self.encoding = encoding
        return None

    async def close(self):
        pass

    async def check(self) -> Tuple[Optional[str], int, Optional[CheckError]]:
        session_kwargs = {}
        if self.proxy:
            session_kwargs['proxies'] = {'http': self.proxy, 'https': self.proxy}

        # Mirrors SimpleAiohttpChecker's payload-error retry: a rotating
        # proxy's CONNECT tunnel occasionally 502s or drops the TLS
        # handshake mid-way (curl_cffi surfaces both as CurlError — e.g.
        # "curl: (56) CONNECT tunnel failed, response 502" or
        # "curl: (35) TLS connect error"). One retry on a fresh connection
        # is enough to usually land on a working exit node.
        connect_retries = 1
        for attempt in range(connect_retries + 1):
            try:
                async with CurlCffiAsyncSession(**session_kwargs) as session:
                    # Strip the User-Agent so curl_cffi can use the impersonated browser's
                    # matching UA. Mixing a random UA with a Chrome TLS fingerprint trips
                    # composite bot scoring (e.g. Cloudflare returns a JS challenge for
                    # "Chrome 91 UA + Chrome 131 TLS"). Keep any site-specific custom headers.
                    headers = {k: v for k, v in (self.headers or {}).items()
                               if k.lower() not in ('user-agent', 'connection')}
                    kwargs = {
                        'url': self.url,
                        'headers': headers or None,
                        'allow_redirects': self.allow_redirects,
                        'timeout': self.timeout if self.timeout else 10,
                        'impersonate': self.browser_emulate,
                    }
                    if self.payload and self.method.lower() == 'post':
                        kwargs['json'] = self.payload

                    if self.method.lower() == 'post':
                        response = await session.post(**kwargs)
                    elif self.method.lower() == 'head':
                        response = await session.head(**kwargs)
                    else:
                        response = await session.get(**kwargs)

                    status_code = response.status_code
                    if self.encoding:
                        response.encoding = self.encoding
                    decoded_content = response.text

                    self.logger.debug(decoded_content)

                    error = CheckError("Connection lost") if status_code == 0 else None
                    return decoded_content, status_code, error

            except asyncio.TimeoutError as e:
                return None, 0, CheckError("Request timeout", str(e))
            except KeyboardInterrupt:
                return None, 0, CheckError("Interrupted")
            except CurlError as e:
                if attempt < connect_retries:
                    self.logger.debug(f"curl_cffi connection error, retrying: {e}")
                    continue
                return None, 0, CheckError("Connecting failure", str(e))
            except Exception as e:
                self.logger.debug(e, exc_info=True)
                return None, 0, CheckError("Unexpected", str(e))


class CloudflareWebgateChecker(CheckerBase):
    """Sends checks through a Cloudflare-bypass proxy.

    Supports two backends, selected by ``modules[0].method`` in settings:

    - ``json_api`` (FlareSolverr): POST to ``/v1`` with ``cmd: request.get``.
      Preserves real upstream status_code, headers and final URL — drop-in
      replacement for SimpleAiohttpChecker.
    - ``url_rewrite`` (CloudflareBypassForScraping ``/html`` endpoint):
      legacy mode. Returns rendered HTML only. Real upstream status is
      lost (proxy answers 200 on success). status_code / response_url
      check types degrade to "200 if HTML returned, AVAILABLE otherwise".
    """

    SESSION_PREFIX_DEFAULT = "maigret"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        config = kwargs.get('config') or {}
        self._modules: List[Dict[str, Any]] = []
        for raw in config.get('modules') or []:
            module = dict(raw)
            module.setdefault('method', 'json_api')
            module.setdefault('name', module.get('method'))
            self._modules.append(module)
        if not self._modules:
            raise ValueError("CloudflareWebgateChecker requires at least one module")
        # Session ID is computed per-request from the target host. Sharing a
        # single session across hosts caused FlareSolverr to break in
        # practice (TLS state / cookies leaking between domains), so each
        # host gets its own Chrome instance.
        self._session_prefix = (
            f"{config.get('session_prefix', self.SESSION_PREFIX_DEFAULT)}-{os.getpid()}"
        )
        self.url = None
        self.headers = None
        self.allow_redirects = True
        self.timeout = 0
        self.method = 'get'
        self.payload = None

    @property
    def session_id(self) -> str:
        """FlareSolverr session ID, scoped per target host."""
        from urllib.parse import urlparse

        host = urlparse(self.url or "").hostname or "default"
        host_safe = re.sub(r"[^a-zA-Z0-9.-]", "_", host)
        return f"{self._session_prefix}-{host_safe}"

    def prepare(self, url, headers=None, allow_redirects=True, timeout=0, method='get', payload=None, encoding=None):
        self.url = url
        self.headers = headers or {}
        self.allow_redirects = allow_redirects
        self.timeout = timeout
        self.method = method
        self.payload = payload
        self.encoding = encoding
        return None

    async def close(self):
        pass

    async def check(self) -> Tuple[Optional[str], int, Optional[CheckError]]:
        attempts: List[str] = []
        last_error: Optional[CheckError] = None
        for module in self._modules:
            method = module.get('method')
            module_name = module.get('name', method or '?')
            if method == 'json_api':
                result = await self._check_flaresolverr(module)
            elif method == 'url_rewrite':
                result = await self._check_url_rewrite(module)
            else:
                self.logger.warning(
                    f"Webgate module '{module_name}' has unknown method "
                    f"'{method}', skipping"
                )
                attempts.append(f"{module_name}:unknown-method")
                continue
            body, status, err = result
            if err is None:
                return result
            last_error = err
            attempts.append(f"{module_name}:{err.type}")
            self.logger.info(
                f"Webgate module '{module_name}' failed for {self.url}: "
                f"{err.type}: {err.desc}. Trying next module if any."
            )
        # All modules failed. The most common case is "user opted into
        # cloudflare_bypass but the solver isn't running" — every per-module
        # attempt ends with "Webgate unreachable" (TCP refused / DNS fail at
        # the configured URL). Detect that case and emit a clear, actionable
        # message; fall back to a generic summary otherwise.
        primary = self._modules[0]
        primary_url = primary.get('url', '?')
        primary_method = primary.get('method', '?')
        start_hint = (
            "docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest"
            if primary_method == 'json_api'
            else "start the local proxy container"
        )
        all_unreachable = bool(attempts) and all(
            # "没连上"和"连上了但一直不说话"是同一件事的两种表现，用户要做的
            # 动作也一样（把 solver 起起来）。按操作系统分开对待会让提示在
            # Windows 上消失：那边连一个没人监听的端口不是立刻 RST，而是等到
            # 超时，于是每次都是 "Webgate timeout"，用户看不到任何可执行的建议。
            #
            # 真正的"solver 活着但解题失败"不在这里：那种情况 FlareSolverr
            # 会回一个 JSON 错误（type 是 "Webgate"），走到下面的通用分支。
            a.endswith(":Webgate unreachable") or a.endswith(":Webgate timeout")
            for a in attempts
        )
        if all_unreachable:
            desc = (
                "cloudflare_bypass is enabled (settings.json or "
                f"--cloudflare-bypass), but the configured solver at "
                f"{primary_url} is not reachable [{', '.join(attempts)}]. "
                f"Either start the solver ({start_hint}) or disable "
                "cloudflare_bypass in settings.json"
            )
        else:
            last_desc = last_error.desc if last_error else "unknown"
            desc = (
                f"all {len(self._modules)} module(s) failed "
                f"[{', '.join(attempts)}]. Last error: {last_desc}. "
                f"Is the solver running at {primary_url}? (hint: {start_hint})"
            )
        return None, 0, CheckError("Webgate unavailable", desc)

    async def _check_flaresolverr(
        self, module: Dict[str, Any]
    ) -> Tuple[Optional[str], int, Optional[CheckError]]:
        endpoint = module.get('url') or 'http://localhost:8191/v1'
        max_timeout_ms = int(module.get('max_timeout_ms', 60000))
        post_method = self.method.lower() == 'post'
        cmd = "request.post" if post_method else "request.get"

        body: Dict[str, Any] = {
            "cmd": cmd,
            "url": self.url,
            "maxTimeout": max_timeout_ms,
            "session": self.session_id,
        }

        proxy = module.get('proxy')
        if isinstance(proxy, str) and proxy:
            body["proxy"] = {"url": proxy}
        elif isinstance(proxy, dict) and proxy.get("url"):
            body["proxy"] = {k: v for k, v in proxy.items() if k in ("url", "username", "password")}

        if post_method and self.payload is not None:
            # FlareSolverr expects postData as urlencoded string for form data,
            # but if site.request_payload is JSON we still send it.
            body["postData"] = (
                "&".join(f"{k}={quote(str(v))}" for k, v in self.payload.items())
            )

        timeout = max(int(self.timeout) if self.timeout else 30, max_timeout_ms / 1000 + 5)

        try:
            async with ClientSession() as session:
                async with session.post(
                    endpoint, json=body, timeout=timeout
                ) as resp:
                    if resp.status >= 500:
                        return None, 0, CheckError(
                            "Webgate", f"FlareSolverr {resp.status}"
                        )
                    data = await resp.json()
        except (ClientConnectorError, ServerDisconnectedError) as e:
            return None, 0, CheckError("Webgate unreachable", str(e))
        except asyncio.TimeoutError:
            return None, 0, CheckError("Webgate timeout", endpoint)
        except Exception as e:
            self.logger.debug(e, exc_info=True)
            return None, 0, CheckError("Webgate", str(e))

        if data.get("status") != "ok":
            return None, 0, CheckError("Webgate", data.get("message", "unknown"))

        solution = data.get("solution") or {}
        upstream_status = int(solution.get("status") or 0)
        response_text = solution.get("response") or ""

        # Diagnostic: warn if FlareSolverr returned the CF challenge page
        # itself (challenge not fully solved) rather than the real content.
        # When this happens with sites that have weak presenseStrs/absenceStrs,
        # maigret's default-true presence rule produces false CLAIMED.
        cf_markers = ("Just a moment", "_cf_chl_opt", "cf-mitigated", "challenges.cloudflare.com")
        if response_text and any(m in response_text for m in cf_markers):
            self.logger.warning(
                f"Webgate response from {self.url} still contains CF challenge "
                f"markers (status={upstream_status}, body={len(response_text)}b). "
                f"FlareSolverr likely did not solve the challenge — site checks "
                f"with weak markers may produce false CLAIMED."
            )

        self.logger.info(
            f"Webgate response: url={self.url} status={upstream_status} "
            f"body_len={len(response_text)}"
        )
        return response_text, upstream_status, None

    async def _check_url_rewrite(
        self, module: Dict[str, Any]
    ) -> Tuple[Optional[str], int, Optional[CheckError]]:
        url_template = module.get('url') or ''
        if "{url}" not in url_template:
            return None, 0, CheckError(
                "Webgate", f"module '{module.get('name')}' url has no {{url}} placeholder"
            )
        from urllib.parse import quote_plus

        proxy_url = url_template.format(url=quote_plus(self.url))
        timeout = self.timeout if self.timeout else 30
        try:
            async with ClientSession() as session:
                async with session.get(proxy_url, timeout=timeout) as resp:
                    if resp.status >= 500:
                        return None, 0, CheckError(
                            "Webgate", f"url_rewrite proxy {resp.status}"
                        )
                    body = await resp.text()
        except (ClientConnectorError, ServerDisconnectedError) as e:
            return None, 0, CheckError("Webgate unreachable", str(e))
        except asyncio.TimeoutError:
            return None, 0, CheckError("Webgate timeout", proxy_url)
        except Exception as e:
            self.logger.debug(e, exc_info=True)
            return None, 0, CheckError("Webgate", str(e))

        # url_rewrite mode CANNOT recover the upstream HTTP status.
        # We assume 200 when HTML is returned; status_code/response_url
        # check types will misfire (see docs).
        return body, 200, None


class CheckerMock:
    def __init__(self, *args, **kwargs):
        pass

    def prepare(self, url, headers=None, allow_redirects=True, timeout=0, method='get', payload=None, encoding=None):
        return None

    async def check(self) -> Tuple[Optional[str], int, Optional[CheckError]]:
        await asyncio.sleep(0)
        return '', 0, None

    async def close(self):
        return


def make_protocol_checker(options: Dict[str, Any], protocol: str):
    checker_factory = options["checkers"][protocol]
    if callable(checker_factory):
        return checker_factory()
    return checker_factory


def debug_response_logging(url, html_text, status_code, check_error):
    with open("debug.log", "a", encoding="utf-8") as f:
        status = status_code or "No response"
        f.write(f"url: {url}\nerror: {check_error}\nr: {status}\n")
        if html_text:
            f.write(f"code: {status}\nresponse: {str(html_text)}\n")


def dump_response(url, html_text, status_code, check_error, site_name=""):
    """--dump-response：把完整响应打到终端（文档 §6.3）。

    为什么这个开关是必需的：写站点规则时要填 `e_string`/`m_string`，这两个值
    必须**逐字**出现在响应里。没有原始响应，填规则就是盲猜，填错的结果是
    "永远 STRUCTURE_CHANGED"——用户看到的是"未判定"，而排查成本极高。

    为什么不复用 debug_response_logging：那个写 `debug.log`（当前目录），
    多站点扫描时几百 KB 混在一起没法看；而且它只在 `-vv` 下触发，
    用户为了看一眼响应被迫开全量调试日志，噪音淹没信息。
    这里直接打终端，配合 `--site 某站` 单站使用，一次看一个。
    """
    head = f"[{site_name}] " if site_name else ""
    print(f"\n{'=' * 72}")
    print(f"{head}URL    : {url}")
    print(f"STATUS : {status_code if status_code is not None else 'No response'}")
    if check_error:
        print(f"ERROR  : {check_error}")
    body = html_text or ""
    print(f"LENGTH : {len(body)} 字符")
    print("-" * 72)
    print(body if body else "<empty body>")
    print("=" * 72)


def process_site_result(
    response, query_notify, logger, results_info: SiteResult, site: MaigretSite
):
    if not response:
        return results_info

    fulltags = site.tags

    # Retrieve other site information again
    username = results_info["username"]
    is_parsing_enabled = results_info["parsing_enabled"]
    url = results_info.get("url_user")
    url_probe = results_info.get("url_probe") or url
    if url_probe != url:
        logger.info(f"{url_probe} (display: {url})")
    else:
        logger.info(url)

    status = results_info.get("status")
    if status is not None:
        # We have already determined the user doesn't exist here
        return results_info

    # Get the expected check type
    check_type = site.check_type

    html_text, status_code, check_error = response

    response_time = None

    if logger.level == logging.DEBUG:
        debug_response_logging(url, html_text, status_code, check_error)

    if results_info.get("dump_response"):
        dump_response(url, html_text, status_code, check_error, site.name)

    # additional check for errors
    if status_code and not check_error:
        check_error = detect_error_page(
            html_text,
            status_code,
            site.errors_dict,
            site.ignore403,
        )

    site_name = site.pretty_name
    # presence flags
    # True by default
    presense_flags = site.presense_strs
    is_presense_detected = False

    if html_text:
        if not presense_flags:
            if check_type == "message" and logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Site %s uses checkType message with empty presenseStrs; "
                    "presence is treated as true for any page.",
                    site.name,
                )
            is_presense_detected = True
            # #2633: When searching non-ASCII usernames (e.g. Chinese),
            # a response that doesn't contain the username at all is likely
            # a generic error/default page, not a valid profile hit.
            if any(ord(c) > 127 for c in username):
                if username not in html_text:
                    logger.debug(
                        "Site %s returned page without non-ASCII username '%s'; "
                        "marking as not detected to avoid false positive.",
                        site.name,
                        username,
                    )
                    is_presense_detected = False
            site.stats["presense_flag"] = None
        else:
            for presense_flag in presense_flags:
                if presense_flag in html_text:
                    is_presense_detected = True
                    site.stats["presense_flag"] = presense_flag
                    logger.debug(presense_flag)
                    break


    # Keyword detection logic
    keywords = results_info.get("keywords", [])
    keyword_match_status = None
    
    if keywords and html_text:
        keywords_found = []
        for keyword in keywords:
            if keyword.lower() in html_text.lower():
                keywords_found.append(keyword)
        
        if keywords_found:
            keyword_match_status = KeywordMatchStatus.KEYWORD_FOUND
            logger.debug(f"Keywords found in {site.name}: {keywords_found}")
        else:
            keyword_match_status = KeywordMatchStatus.KEYWORDS_NOT_FOUND
            logger.debug(f"No keywords found in {site.name}")
    else:
        keyword_match_status = KeywordMatchStatus.NO_KEYWORDS

    def build_result(status, **kwargs):
        return MaigretCheckResult(
            username,
            site_name,
            url,
            status,
            query_time=response_time,
            tags=fulltags,
            keywords=keywords,
            keyword_match_status=keyword_match_status,
            **kwargs,
        )

    if check_error:
        logger.warning(check_error)
        result = build_result(
            MaigretCheckStatus.UNKNOWN,
            error=check_error,
            context=str(check_error),
        )
    elif check_type == "message":
        # Checks if the error message is in the HTML
        is_absence_detected = any(
            [(absence_flag in html_text) for absence_flag in site.absence_strs]
        )
        if not is_absence_detected and is_presense_detected:
            result = build_result(MaigretCheckStatus.CLAIMED)
        else:
            result = build_result(MaigretCheckStatus.AVAILABLE)
    elif check_type == "status_code":
        # Checks if the status code of the response is 2XX
        if 200 <= status_code < 300:
            result = build_result(MaigretCheckStatus.CLAIMED)
        else:
            result = build_result(MaigretCheckStatus.AVAILABLE)
    elif check_type == "response_url":
        # For this detection method, we have turned off the redirect.
        # So, there is no need to check the response URL: it will always
        # match the request.  Instead, we will ensure that the response
        # code indicates that the request was successful (i.e. no 404, or
        # forward to some odd redirect).
        if 200 <= status_code < 300 and is_presense_detected:
            result = build_result(MaigretCheckStatus.CLAIMED)
        else:
            result = build_result(MaigretCheckStatus.AVAILABLE)
    else:
        # It should be impossible to ever get here...
        raise ValueError(
            f"Unknown check type '{check_type}' for " f"site '{site.name}'"
        )

    # cn overlay：四字段判定接管（文档 §5.3）。默认只接管人工策展的中文站，
    # 用 MAIGRET_CN_VERDICT=0 可整体回退到上面的上游逻辑。
    # 必须放在 extract_ids_data 之前：结论若从 CLAIMED 变成 UNKNOWN，
    # 就不能再把这个未确认的页面当成已确认账号去抽关联信息。
    result = apply_cn_verdict(
        result, site, username, html_text, status_code, check_error, logger, results_info
    )

    extracted_ids_data = {}

    # 站内搜索站点**不做** ID 抽取：命中的证据是"搜索结果里有个同名句柄"，
    # 而手上这一页是搜索结果页——上面挂着的是**别人**的主页链接和昵称。
    # 在搜索页上抽 ids，等于把别人的账号信息记到这个用户名头上，
    # 还会顺带污染递归搜索的输入。要抽也得先拿到那个主页，见 docs/cn-rules.md。
    _is_search_site = getattr(spec_for(site), "executor", "") == "search"

    if (
        is_parsing_enabled
        and result.status == MaigretCheckStatus.CLAIMED
        and not _is_search_site
    ):
        extracted_ids_data = extract_ids_data(html_text, logger, site)
        if extracted_ids_data:
            new_usernames = parse_usernames(extracted_ids_data, logger)
            results_info = update_results_info(
                results_info, extracted_ids_data, new_usernames
            )
            result.ids_data = extracted_ids_data

    # cn 证据抽取（文档 §5.6）：只抽站点自己在 `extract` 里声明的字段。
    #
    # 只对 CLAIMED 做，这是硬要求：对未确认的页面抽字段，等于把别人的信息
    # 记到这个用户名头上。判定层刚把"被拦截/登录墙"降级成 UNKNOWN 的页面，
    # 绝不能在这里又被当成真人主页来读。
    if result.status == MaigretCheckStatus.CLAIMED and not _is_search_site:
        cn_fields = _cn_extract_fields(site)
        if cn_fields:
            # 抽取失败不能带走这条站点结果。上面那条 CLAIMED 结论是判定层
            # 花了一次请求换来的，而抽取只是锦上添花——让一个站点的抽取 bug
            # 把"这个账号存在"这个事实一起吞掉，等于用装饰件的故障毁掉主体。
            # 头像那一步（下面的 attach_avatar_evidence）已经是这个处理方式，
            # 这里保持一致。
            try:
                evidence = extract_evidence(
                    html_text,
                    cn_fields,
                    base_url=results_info.get("url_user") or site.url_main or "",
                    anchor=username,
                    site_name=site.name,
                )
            except Exception as exc:
                logger.warning("站点 %s 证据抽取失败：%s", site.name, exc)
                evidence = []
            if evidence:
                results_info["cn_evidence"] = summarize(evidence)
                # 同时并入 ids_data。
                #
                # 为什么两处都要写：`cn_evidence` 只喂命令行报告的"证据明细"，
                # 而 Web 结果页的画像卡片（profile_card.build_profile）和
                # "发现账号"实时流读的都是 `ids_data`。只写前者的话，会出现
                # 命令行里明明有「性别 女」、网页上却一片空白——同一个数据
                # 两个入口两种结果，用户只会认为工具坏了。
                #
                # 用 setdefault 而不是直接赋值：上游 HTML 抽取器已经抽到的
                # 字段不动。两边都有值时，保留先到的那个，避免同一字段
                # 在两个来源之间反复横跳。
                merged = dict(result.ids_data or {})
                for item in evidence:
                    merged.setdefault(item.field, item.value)
                result.ids_data = merged

    # Save status of request
    results_info["status"] = result

    # Save results from request
    results_info["http_status"] = status_code
    results_info["is_similar"] = site.similar_search
    # results_site['response_text'] = html_text
    results_info["rank"] = site.alexa_rank
    return results_info


def format_payload(value: Any, username: str) -> Any:
    """Substitute {username} in request payload strings, including nested ones."""
    if isinstance(value, str):
        return value.format(username=username)
    if isinstance(value, dict):
        return {k: format_payload(v, username) for k, v in value.items()}
    if isinstance(value, list):
        return [format_payload(v, username) for v in value]
    return value


def make_site_result(
    site: MaigretSite, username: str, options: Dict[str, Any], logger, *args, **kwargs
) -> SiteResult:
    results_site: SiteResult = {}

    # Record URL of main site and username
    results_site["site"] = site
    results_site["username"] = username
    results_site["keywords"] = kwargs.get('keywords', [])
    results_site["parsing_enabled"] = options["parsing"]
    # --dump-response：写站点规则时必需，否则只能靠猜 e_string
    results_site["dump_response"] = bool(options.get("dump_response"))
    results_site["url_main"] = site.url_main
    results_site["cookies"] = (
        options.get("cookie_jar")
        and options["cookie_jar"].filter_cookies(site.url_main)
        or None
    )

    headers = {
        "User-Agent": get_random_user_agent(),
        # tell server that we want to close connection after request
        "Connection": "close",
    }

    headers.update(site.headers)

    if "url" not in site.__dict__:
        logger.error("No URL for site %s", site.name)

    if kwargs.get('retry') and site.mirrors:
        site.url_main = random.choice(site.mirrors)
        logger.info(f"Use {site.url_main} as a main url of site {site}")

    # URL of user on site (if it exists)
    url = site.url.format(
        urlMain=site.url_main, urlSubpath=site.url_subpath, username=quote(username)
    )

    # workaround to prevent slash errors
    url = re.sub("(?<!:)/+", "/", url)

    # Select checker. Order of precedence:
    # 1. Cloudflare webgate (FlareSolverr / CloudflareBypassForScraping) when
    #    bypass is active and site.protection requests it.
    # 2. curl_cffi for sites requiring TLS impersonation.
    # 3. Default protocol-specific checker (aiohttp).
    cf_bypass = options.get("cloudflare_bypass")
    needs_webgate = bool(cf_bypass) and any(
        p in cf_bypass["trigger_protection"] for p in site.protection
    )
    needs_impersonation = 'tls_fingerprint' in site.protection

    if needs_webgate:
        checker = CloudflareWebgateChecker(logger=logger, config=cf_bypass)
        logger.info(
            f"Using Cloudflare webgate for {site.name} "
            f"(protection: {list(site.protection)})"
        )
    elif needs_impersonation:
        checker = CurlCffiChecker(
            logger=logger,
            browser_emulate='chrome',
            proxy=options.get('proxy'),
        )
    else:
        checker = make_protocol_checker(options, site.protocol)

    # 请求预热：规则在 cn overlay 里用 bootstrap_url 声明"先访问这里拿 Cookie"。
    # 只对支持它的 checker 生效（SimpleAiohttpChecker 读这个属性），
    # 其它 checker 上挂一个没人读的属性，代价为零。
    if isinstance(checker, CheckerBase):
        cn_spec = spec_for(site)
        bootstrap = getattr(cn_spec, "bootstrap_url", "") if cn_spec else ""
        if bootstrap:
            checker.bootstrap_url = bootstrap

    if isinstance(checker, CheckerMock):
        protocol = site.protocol or "protocol"
        results_site["status"] = MaigretCheckResult(
            username,
            site.name,
            url,
            MaigretCheckStatus.ILLEGAL,
            error=CheckError("Skipped", f"no {protocol} gateway configured"),
        )
    # site check is disabled
    elif site.disabled and not options['forced']:
        logger.debug(f"Site {site.name} is disabled, skipping...")
        results_site["status"] = MaigretCheckResult(
            username,
            site.name,
            url,
            MaigretCheckStatus.ILLEGAL,
            error=CheckError("Check is disabled"),
        )
    # current username type could not be applied
    elif site.type != options["id_type"]:
        results_site["status"] = MaigretCheckResult(
            username,
            site.name,
            url,
            MaigretCheckStatus.ILLEGAL,
            error=CheckError('Unsupported identifier type', f'Want "{site.type}"'),
        )
    # username is not allowed.
    elif site.regex_check and re.search(site.regex_check, username) is None:
        results_site["status"] = MaigretCheckResult(
            username,
            site.name,
            url,
            MaigretCheckStatus.ILLEGAL,
            error=CheckError(
                'Unsupported username format', f'Want "{site.regex_check}"'
            ),
        )
        results_site["url_user"] = ""
        results_site["http_status"] = ""
        results_site["response_text"] = ""
        # query_notify.update(results_site["status"])
    else:
        # URL of user on site (if it exists)
        results_site["url_user"] = url
        url_probe = site.url_probe
        if url_probe is None:
            # Probe URL is normal one seen by people out on the web.
            url_probe = url
        else:
            # There is a special URL for probing existence separate
            # from where the user profile normally can be found.
            # Encode the username here just like the display URL above does:
            # a raw "#" otherwise turns the rest of it into a fragment and the
            # probe silently lands on a different account's URL.
            url_probe = url_probe.format(
                urlMain=site.url_main,
                urlSubpath=site.url_subpath,
                username=quote(username),
            )

        for k, v in site.get_params.items():
            url_probe += f"&{k}={v}"

        results_site["url_probe"] = url_probe

        if site.request_method:
            request_method = site.request_method.lower()
        elif site.check_type == "status_code" and site.request_head_only:
            # In most cases when we are detecting by status code,
            # it is not necessary to get the entire body:  we can
            # detect fine with just the HEAD response.
            request_method = 'head'
        else:
            # Either this detect method needs the content associated
            # with the GET response, or this specific website will
            # not respond properly unless we request the whole page.
            request_method = 'get'

        payload = None
        if site.request_payload:
            payload = format_payload(site.request_payload, username)

        if site.check_type == "response_url":
            # Site forwards request to a different URL if username not
            # found.  Disallow the redirect so we can capture the
            # http status from the original URL request.
            allow_redirects = False
        else:
            # Allow whatever redirect that the site wants to do.
            # The final result of the request will be what is available.
            allow_redirects = True

        future = checker.prepare(
            method=request_method,
            url=url_probe,
            headers=headers,
            allow_redirects=allow_redirects,
            timeout=options['timeout'],
            payload=payload,
            encoding=site.encoding,
        )

        # Store future request object in the results object
        results_site["future"] = future

    results_site["checker"] = checker

    return results_site


async def check_site_for_username(
    site, username, options: Dict[str, Any], logger, query_notify, *args, **kwargs
) -> Tuple[str, SiteResult]:
    keywords = kwargs.get('keywords')
    default_result = make_site_result(
        site, username, options, logger, retry=kwargs.get('retry'), keywords=keywords
    )
    if default_result.get("status"):
        query_notify.update(default_result["status"], site.similar_search)
        return site.name, default_result

    # future = default_result.get("future")
    # if not future:
    # return site.name, default_result

    checker = default_result.get("checker")
    if not checker:
        print(f"error, no checker for {site.name}")
        return site.name, default_result

    # 执行器路由：`executor: browser` 的站点走 CDP，被风控的站点自动升级重试。
    # 收口在这里而不是 checker 内部：浏览器是异步的，而 checker 是同步 HTTP 客户端，
    # 硬塞进去会让两条完全不同的失败语义混在一起。
    response = await fetch_site_response(site, default_result, checker, options, logger)
    html_text = response[0] if response and response[0] else ""

    # Retry once after token-style activation (e.g. Twitter guest token refresh).
    act = site.activation
    if act and html_text:
        marks = act.get("marks") or []
        if marks and any(m in html_text for m in marks):
            method = act["method"]
            try:
                activate_fun = getattr(ParsingActivator(), method)
                await activate_fun(
                    site,
                    logger,
                    url=checker.url,
                    timeout=options['timeout'],
                    html=html_text,
                )
            except AttributeError:
                logger.warning(
                    f"Activation method {method} for site {site.name} not found!",
                    exc_info=True,
                )
            except Exception as e:
                logger.warning(
                    f"Failed activation {method} for site {site.name}: {str(e)}",
                    exc_info=True,
                )
            else:
                merged = dict(checker.headers or {})
                merged.update(site.headers)
                checker.prepare(
                    url=checker.url,
                    headers=merged,
                    allow_redirects=checker.allow_redirects,
                    timeout=checker.timeout,
                    method=checker.method,
                    payload=getattr(checker, 'payload', None),
                    encoding=site.encoding,
                )
                response = await checker.check()

    response_result = process_site_result(
        response, query_notify, logger, default_result, site
    )

    # 头像指纹（文档 §5.5 里 ROI 最高的一条身份信号）。
    #
    # 放在这里而不是 process_site_result 里，是因为那个函数是同步的，
    # 而抓头像必须 await。也因为它需要 html_text——process_site_result
    # 不保存原始响应（response_text 那行是注释掉的），拿不到页面。
    #
    # 只有站点显式声明了 avatar 才会真的发这次请求，见 attach_avatar_evidence。
    if (
        response_result.get('status')
        and response_result['status'].status == MaigretCheckStatus.CLAIMED
    ):
        try:
            await attach_avatar_evidence(
                response_result,
                _cn_extract_fields(site),
                html_text,
                base_url=response_result.get('url_user') or site.url_main or '',
                timeout=min(float(options.get('timeout') or 10), 15.0),
            )
        except Exception as exc:
            # 头像只是锦上添花，任何失败都不该影响这次扫描的结论。
            logger.debug("站点 %s 头像指纹计算失败：%s", site.name, exc)

    if (
        options.get('enrich')
        and response_result.get('status')
        and response_result['status'].status == MaigretCheckStatus.CLAIMED
    ):
        await run_url_mutations(
            checker=checker,
            site=site,
            results_info=response_result,
            options=options,
            logger=logger,
            query_notify=query_notify,
        )

    query_notify.update(response_result['status'], site.similar_search)

    return site.name, response_result


async def debug_ip_request(checker, logger):
    checker.prepare(url="https://icanhazip.com")
    ip, status, check_error = await checker.check()
    if ip:
        logger.debug(f"My IP is: {ip.strip()}")
    else:
        logger.debug(f"IP requesting {check_error.type}: {check_error.desc}")


def get_failed_sites(results: Dict[str, SiteResult]) -> List[str]:
    sites = []
    for sitename, r in results.items():
        status = r.get('status')
        if status and status.error:
            if errors.is_permanent(status.error.type):
                continue
            sites.append(sitename)
    return sites


def restore_checkpoint_result(
    username: str, site: MaigretSite, record: Dict[str, Any]
) -> SiteResult:
    """把 checkpoint 的一行还原成报告层能直接消费的 SiteResult。

    为什么必须还原而不是"跳过就行"：续跑的目的是产出**一份完整的报告**。
    如果只是不重复请求，报告里就只剩后半段扫描到的站点，用户看到"扫了 300 个
    站，报告只有 120 个"，还得手动拼两次结果 —— 那等于没续。所以已完成的
    站点要从 checkpoint 复活进 all_results。

    还原出的条目刻意**不含 response_text / future / checker**：这些字段只有
    真实请求才有，报告层也不用（`generate_report_context` 只读 status、
    url_user、ids_*）。伪造它们反而会让下游误以为页面内容还在手里。
    """
    raw_status = record.get('status')
    try:
        status = MaigretCheckStatus(raw_status)
    except (ValueError, TypeError):
        # 枚举值不认识（手工改过 / 未来新增状态）→ 保守地当作未判定，
        # 不猜成 AVAILABLE：把未知说成"不存在"是这类工具最危险的错误方向。
        status = MaigretCheckStatus.UNKNOWN

    reason = None
    raw_reason = record.get('reason')
    if raw_reason:
        try:
            reason = Reason(raw_reason)
        except ValueError:
            reason = None

    url = record.get('url') or ''
    check_result = MaigretCheckResult(
        record.get('username') or username,
        site.name,
        url,
        status,
        reason=reason,
        confidence=record.get('confidence'),
        # 扩展字段（uid/创建时间/地区/公司/头像）也要复活，否则续跑出来的
        # 报告只有"命中"，没有"命中了什么"——关联图谱和头像会整片空掉。
        ids_data=record.get('ids_data') or None,
    )
    # rate_limited 是独立布尔，报告里"被限流"和"不存在"必须能分开
    check_result.rate_limited = bool(raw_reason == Reason.RATE_LIMITED.value)

    return {
        'site': site,
        'username': username,
        'url_main': getattr(site, 'url_main', '') or '',
        'url_user': url,
        'url_probe': url,
        'http_status': record.get('http_status') if record.get('http_status') is not None else '',
        'status': check_result,
        'is_similar': False,
        'parsing_enabled': False,
        'cookies': None,
        # 标记来源，便于诊断"这条是续跑带进来的还是这次真跑出来的"
        'from_checkpoint': True,
    }


def load_cookie_jar(cookies, logger):
    """准备 cookie jar；来源不可用时返回 None 而不是抛异常。

    为什么不能让它抛：这行在**发出第一个请求之前**。用户勾了"使用 cookies"
    但文件不存在（首次使用、被清理、路径写错）时，抛出去等于整次检索一个站点
    都没查就死掉，而 Web 端只会显示"检索失败：No such file or directory"——
    用户看不出问题出在 cookies 上，更不知道取消勾选就能跑。

    cookies 只是提高成功率，缺了就该降级继续。

    两种入参：
        str           cookies.txt 路径（Netscape 格式），从磁盘读
        CookieJar     已经装好的 jar，直接透传

    为什么要支持透传：从本机浏览器取的 Cookie 是**内存直用**的（见
    `cn/browser_cookies.py`），落盘就违背了"不落盘"这条约束。但下游
    `MairetChecker` 只认 CookieJar，所以这里做一次类型分流，
    而不是逼调用方先写一个临时文件。
    """
    if hasattr(cookies, "filter_cookies"):
        # 必须在 `if not cookies` **之前**判断：空的 CookieJar 是 falsy
        # （__len__ 为 0），排在后面会被当成"没传"直接丢掉。虽然空 jar
        # 本来也发不出 Cookie，但类型分流写在前面才不会随 __len__ 的实现变化。
        logger.debug("使用调用方传入的 cookie jar（内存直用，未落盘）")
        return cookies
    if not cookies:
        return None
    if not os.path.exists(cookies):
        logger.warning(
            "Cookie 文件不存在，本次不使用 cookies：%s（在设置里取消勾选可消除此提示）",
            cookies,
        )
        return None
    logger.debug(f"Using cookies jar file {cookies}")
    return import_aiohttp_cookies(cookies)


async def maigret(
    username: str,
    site_dict: Dict[str, MaigretSite],
    logger,
    query_notify=None,
    proxy=None,
    tor_proxy=None,
    i2p_proxy=None,
    timeout=3,
    is_parsing_enabled=False,
    is_enrich_enabled=False,
    id_type="username",
    debug=False,
    forced=False,
    max_connections=100,
    no_progressbar=False,
    cookies=None,
    retries=0,
    check_domains=False,
    cloudflare_bypass: Optional[Dict[str, Any]] = None,
    keywords=None,
    dns_resolver: str = 'async',
    output_container: Optional[Dict[str, SiteResult]] = None,
    checkpoint: bool = False,
    resume: Optional[str] = None,
    dump_response: bool = False,
    *args,
    **kwargs,
) -> Dict[str, SiteResult]:
    """Main search func

    Checks for existence of username on certain sites.

    Keyword Arguments:
    username               -- Username string will be used for search.
    site_dict              -- Dictionary containing sites data in MaigretSite objects.
    query_notify           -- Notifier object (e.g. QueryNotifyPrint) used to
                              report query progress and results.
    logger                 -- Standard Python logger object.
    timeout                -- Time in seconds to wait before timing out request.
                              Default is 3 seconds.
    is_parsing_enabled     -- Extract additional info from account pages.
    id_type                -- Type of username to search.
                              Default is 'username', see all supported here:
                              https://maigret.readthedocs.io/en/latest/supported-identifier-types.html
    max_connections        -- Maximum number of concurrent connections allowed.
                              Default is 100.
    no_progressbar         -- Displaying of ASCII progressbar during scanner.
    cookies                -- Filename of a cookie jar file to use for each request.
    keywords               -- List of keywords to search for in HTML content.
                              Default is None.
    *args, **kwargs        -- Additional arguments.

    Return Value:
    Dictionary containing results from report. Key of dictionary is the name
    of the social network site, and the value is another dictionary with
    the following keys:
        url_main:      URL of main site.
        url_user:      URL of user on site (if account exists).
        status:        QueryResult() object indicating results of test for
                       account existence.
        http_status:   HTTP status code of query which checked for existence on
                       site.
        response_text: Text that came back from request.  May be None if
                       there was an HTTP error when checking for existence.
    """

    # notify caller that we are starting the query.
    if not query_notify:
        query_notify = Mock()

    query_notify.start(username, id_type)

    # 每次扫描重置浏览器升级预算。
    # 预算挂在模块级（跨站点累计），所以必须在每次扫描开始时清零，
    # 否则同一个进程里第二次扫描会一开始就没额度（web 界面、脚本批量调用都会踩）。
    reset_escalations()

    cookie_jar = load_cookie_jar(cookies, logger)

    def clearweb_checker():
        return SimpleAiohttpChecker(
            proxy=proxy, cookie_jar=cookie_jar, logger=logger, dns_resolver=dns_resolver
        )

    def tor_checker():
        if not tor_proxy:
            return CheckerMock()
        return ProxiedAiohttpChecker(  # type: ignore
            proxy=tor_proxy, cookie_jar=cookie_jar, logger=logger, dns_resolver=dns_resolver
        )

    def i2p_checker():
        if not i2p_proxy:
            return CheckerMock()
        return ProxiedAiohttpChecker(  # type: ignore
            proxy=i2p_proxy, cookie_jar=cookie_jar, logger=logger, dns_resolver=dns_resolver
        )

    def dns_checker():
        if not check_domains:
            return CheckerMock()
        return AiodnsDomainResolver(logger=logger)  # type: ignore

    if logger.level == logging.DEBUG:
        await debug_ip_request(clearweb_checker(), logger)

    # setup parallel executor
    executor = AsyncioQueueGeneratorExecutor(
        logger=logger,
        in_parallel=max_connections,
        timeout=timeout + 0.5,
        *args,
        **kwargs,
    )

    # make options objects for all the requests
    options: Dict[str, Any] = {}
    options["cookies"] = cookie_jar
    options["checkers"] = {
        '': clearweb_checker,
        'tor': tor_checker,
        'dns': dns_checker,
        'i2p': i2p_checker,
    }
    options["parsing"] = is_parsing_enabled
    options["enrich"] = is_enrich_enabled
    options["enrich_requests"] = 0
    options["timeout"] = timeout
    options["id_type"] = id_type
    options["forced"] = forced
    options["cloudflare_bypass"] = cloudflare_bypass
    options["proxy"] = proxy
    options["dump_response"] = dump_response

    # results from analysis of all sites
    # When the caller wants to read partial results after a Ctrl+C
    # cancellation, they pass a dict; we mutate it in place so the partial
    # state remains visible after this coroutine raises CancelledError.
    all_results: Dict[str, SiteResult] = (
        output_container if output_container is not None else {}
    )

    sites = list(site_dict.keys())

    # ---- 断点续查（文档 §3.2.4）------------------------------------------
    # 默认 checkpoint=False / resume=None，这一段完全不执行，老行为零变化。
    # resume 隐含启用 checkpoint：续跑本来就是往同一个文件里继续写，
    # 否则这次续出来的后半段在下次中断时又丢了。
    ckpt: Optional[ScanCheckpoint] = None
    completed: Dict[str, Dict[str, Any]] = {}
    if resume:
        ckpt, completed = ScanCheckpoint.open_for_resume(resume)
        query_notify.warning(
            f'Resuming from checkpoint {resume}: {len(completed)} site(s) already done'
        )
    elif checkpoint:
        ckpt = ScanCheckpoint(checkpoint_path(None, username))
        # 这行必须打在扫描**开始前**：进程被 SIGKILL 时不会有任何收尾输出，
        # 用户得能从屏幕上抄下路径，才能用 --resume 接着跑。
        query_notify.warning(
            f'Checkpoint enabled: progress is saved to {ckpt.path} '
            f'(resume with --resume "{ckpt.path}")'
        )

    if completed:
        restored = 0
        for sitename in list(sites):
            record = completed.get(sitename)
            if record is None:
                continue
            # 同一个 checkpoint 文件被拿去续另一个用户名时必须拒绝：
            # 记录里的 URL / 结论是上一个用户名的，混进报告就是错误数据。
            record_username = record.get('username')
            if record_username and record_username != username:
                logger.warning(
                    "checkpoint 中的站点 %s 属于用户名 %r，本次查询 %r，忽略该记录",
                    sitename, record_username, username,
                )
                continue
            site = site_dict.get(sitename)
            if site is None:
                continue
            # 已完成站点直接复活进结果集，且从待扫列表剔除（tasks_dict 只装
            # sites 里的站点），这样既不发重复请求，报告又是完整的。
            all_results[sitename] = restore_checkpoint_result(username, site, record)
            sites.remove(sitename)
            restored += 1
        if restored:
            query_notify.warning(
                f'Skipped {restored} site(s) already checked in the checkpoint; '
                f'{len(sites)} site(s) left to scan'
            )

    attempts = retries + 1
    while attempts:
        tasks_dict = {}

        for sitename, site in site_dict.items():
            if sitename not in sites:
                continue
            default_result: SiteResult = {
                'site': site,
                'status': MaigretCheckResult(
                    username,
                    sitename,
                    '',
                    MaigretCheckStatus.UNKNOWN,
                    error=CheckError('Request failed'),
                ),
            }
            tasks_dict[sitename] = (
                check_site_for_username,
                [site, username, options, logger, query_notify],
                {
                    'default': (sitename, default_result),
                    'retry': retries - attempts + 1,
                    'keywords': keywords,
                },
            )

        cur_results = []
        # ctrl_c=False is critical: alive_progress's default Ctrl+C handler
        # silently absorbs the first SIGINT (it draws a "⚠" mark on the bar
        # and keeps going), so the user has to press it twice. With
        # ctrl_c=False the signal propagates immediately to asyncio, which
        # cancels the main task — caught in maigret.maigret.main() as
        # CancelledError so the search loop falls through to report
        # generation. See issue: "ctrl+c needs two presses + traceback".
        try:
            with alive_bar(
                len(tasks_dict), title="Searching", force_tty=True,
                disable=no_progressbar, ctrl_c=False, enrich_print=False,
            ) as progress:
                async for result in executor.run(list(tasks_dict.values())):  # type: ignore[arg-type]
                    cur_results.append(result)
                    # `all_results` may be an output container supplied by
                    # the caller — flush each completed site check into it
                    # immediately, so partial progress survives a Ctrl+C
                    # cancellation (issue #2688 follow-up). dict.update
                    # accepts an iterable of (k, v) 2-tuples, which is what
                    # the executor yields, so this is equivalent to the
                    # post-loop update but visible to the caller mid-flight.
                    all_results.update([result])
                    # 断点续查：每完成一站立刻落盘（append + flush + fsync）。
                    # 位置就在 all_results.update 之后 —— 内存和磁盘在这一刻
                    # 记录的是同一件事，不会出现"报告里有、checkpoint 里没有"。
                    # 写在循环内而不是循环后，是因为要救的正是"循环没跑完"。
                    if ckpt is not None:
                        ckpt.record(result[0], result[1])
                    progress()
        except asyncio.CancelledError:
            # Re-raise so the caller's `except CancelledError` runs. The
            # partial `all_results` is already visible to the caller via the
            # output_container kwarg.
            # 数据已经逐行 fsync 过，这里关文件只是别把 fd 泄漏到报告生成阶段。
            if ckpt is not None:
                ckpt.close()
            query_notify.finish()
            raise

        all_results.update(cur_results)

        # rerun for failed sites
        sites = get_failed_sites(dict(cur_results))
        attempts -= 1

        if not sites:
            break

        if attempts:
            query_notify.warning(
                f'Restarting checks for {len(sites)} sites... ({attempts} attempts left)'
            )

    # notify caller that all queries are finished
    query_notify.finish()

    if ckpt is not None:
        ckpt.close()
        logger.debug("checkpoint 已关闭：%s（本次写入 %d 条）", ckpt.path, ckpt.written)

    if is_enrich_enabled:
        query_notify.enrich(f"{options.get('enrich_requests', 0)} extra requests performed")

    return all_results


def timeout_check(value):
    """Check Timeout Argument.

    Checks timeout for validity.

    Keyword Arguments:
    value                  -- Time in seconds to wait before timing out request.

    Return Value:
    Floating point number representing the time (in seconds) that should be
    used for the timeout.

    NOTE:  Will raise an exception if the timeout in invalid.
    """
    from argparse import ArgumentTypeError

    try:
        timeout = float(value)
    except ValueError:
        raise ArgumentTypeError(f"Timeout '{value}' must be a number.")
    if timeout <= 0:
        raise ArgumentTypeError(f"Timeout '{value}' must be greater than 0.0s.")
    return timeout


async def site_self_check(
    site: MaigretSite,
    logger: logging.Logger,
    semaphore,
    db: MaigretDatabase,
    silent=False,
    proxy=None,
    tor_proxy=None,
    i2p_proxy=None,
    skip_errors=False,
    cookies=None,
    auto_disable=False,
    diagnose=False,
    cloudflare_bypass: Optional[Dict[str, Any]] = None,
    dns_resolver: str = 'async',
):
    """
    Self-check a site configuration.

    Args:
        auto_disable: If True, automatically disable sites that fail checks.
                     If False (default), only report issues without disabling.
        diagnose: If True, print detailed diagnosis information.
    """
    changes: Dict[str, Any] = {
        "disabled": False,
        "issues": [],
        "recommendations": [],
    }

    try:
        check_data = [
            (site.username_claimed, MaigretCheckStatus.CLAIMED),
            (site.username_unclaimed, MaigretCheckStatus.AVAILABLE),
        ]

        logger.info(f"Checking {site.name}...")

        results_cache = {}

        for username, status in check_data:
            async with semaphore:
                results_dict = await maigret(
                    username=username,
                    site_dict={site.name: site},
                    logger=logger,
                    timeout=30,
                    id_type=site.type,
                    forced=True,
                    no_progressbar=True,
                    retries=1,
                    proxy=proxy,
                    tor_proxy=tor_proxy,
                    i2p_proxy=i2p_proxy,
                    cookies=cookies,
                    cloudflare_bypass=cloudflare_bypass,
                    dns_resolver=dns_resolver,
                )

                # don't disable entries with other ids types
                if site.name not in results_dict:
                    logger.info(results_dict)
                    changes["issues"].append(f"Site {site.name} not in results (wrong id_type?)")
                    if auto_disable:
                        changes["disabled"] = True
                    continue

                logger.debug(results_dict)

                result = results_dict[site.name]["status"]
                results_cache[username] = results_dict[site.name]

            if result.error and 'Cannot connect to host' in result.error.desc:
                changes["issues"].append("Cannot connect to host")
                if auto_disable:
                    changes["disabled"] = True

            site_status = result.status

            if site_status != status:
                if site_status == MaigretCheckStatus.UNKNOWN:
                    msgs = site.absence_strs
                    etype = site.check_type
                    error_msg = f"Error checking {username}: {result.context}"
                    changes["issues"].append(error_msg)
                    logger.warning(
                        f"Error while searching {username} in {site.name}: {result.context}, {msgs}, type {etype}"
                    )
                    # don't disable sites after the error
                    # meaning that the site could be available, but returned error for the check
                    # e.g. many sites protected by cloudflare and available in general
                    if skip_errors:
                        pass
                    # don't disable in case of available username
                    elif status == MaigretCheckStatus.CLAIMED and auto_disable:
                        changes["disabled"] = True
                elif status == MaigretCheckStatus.CLAIMED:
                    changes["issues"].append(f"Claimed user '{username}' not detected as claimed")
                    logger.warning(
                        f"Not found `{username}` in {site.name}, must be claimed"
                    )
                    logger.info(results_dict[site.name])
                    if auto_disable:
                        changes["disabled"] = True
                else:
                    changes["issues"].append(f"Unclaimed user '{username}' detected as claimed")
                    logger.warning(f"Found `{username}` in {site.name}, must be available")
                    logger.info(results_dict[site.name])
                    if auto_disable:
                        changes["disabled"] = True

        logger.info(f"Site {site.name} checking is finished")

        # Generate recommendations based on issues
        if changes["issues"] and len(results_cache) == 2:
            claimed_result = results_cache.get(site.username_claimed, {})
            unclaimed_result = results_cache.get(site.username_unclaimed, {})

            claimed_http = claimed_result.get("http_status")
            unclaimed_http = unclaimed_result.get("http_status")

            if claimed_http and unclaimed_http:
                if claimed_http != unclaimed_http and site.check_type != "status_code":
                    changes["recommendations"].append(
                        f"Consider checkType: status_code (HTTP {claimed_http} vs {unclaimed_http})"
                    )
                elif (
                    claimed_http == unclaimed_http
                    and site.check_type == "status_code"
                    and unclaimed_result["status"].status == MaigretCheckStatus.CLAIMED
                ):
                    # Same code for both names and the fake one came back
                    # found: a status_code check that answers yes to everyone.
                    # A shared 5xx or 403 is an error, not this.
                    engine_check = (site.engine_obj.site.get("checkType")
                                    if site.engine_obj else None)
                    if engine_check and engine_check != "status_code":
                        changes["recommendations"].append(
                            f"Drop checkType from this entry so the {site.engine_obj.name} "
                            f"engine's {engine_check} check applies (both names got HTTP {claimed_http}), then self-check again"
                        )
                    else:
                        changes["recommendations"].append(
                            f"status_code cannot tell these names apart (both got HTTP {claimed_http}), "
                            f"check the page text instead (checkType: message), then self-check again"
                        )

        # Print diagnosis if requested
        if diagnose and changes["issues"]:
            print(f"\n--- {site.name} DIAGNOSIS ---")
            print(f"  Check type: {site.check_type}")
            print("  Issues:")
            for issue in changes["issues"]:
                print(f"    - {issue}")
            if changes["recommendations"]:
                print("  Recommendations:")
                for rec in changes["recommendations"]:
                    print(f"    -> {rec}")

        # Only modify site if auto_disable is enabled
        if auto_disable and changes["disabled"] != site.disabled:
            site.disabled = changes["disabled"]
            logger.info(f"Switching property 'disabled' for {site.name} to {site.disabled}")
            db.update_site(site)
            if not silent:
                action = "Disabled" if site.disabled else "Enabled"
                print(f"{action} site {site.name}...")
        elif changes["issues"] and not silent and not diagnose:
            # Report issues without disabling
            print(f"Issues found in {site.name}: {len(changes['issues'])} (not auto-disabled)")

        # remove service tag "unchecked"
        if "unchecked" in site.tags:
            site.tags.remove("unchecked")
            db.update_site(site)

    except Exception as e:
        logger.warning(
            f"Self-check of {site.name} failed with unexpected error: {e}",
            exc_info=True,
        )
        changes["issues"].append(f"Unexpected error: {e}")
        if auto_disable and not site.disabled:
            changes["disabled"] = True
            site.disabled = True
            db.update_site(site)
            if not silent:
                print(f"Disabled site {site.name} (unexpected error)...")

    return changes


async def self_check(
    db: MaigretDatabase,
    site_data: dict,
    logger: logging.Logger,
    silent=False,
    max_connections=10,
    proxy=None,
    tor_proxy=None,
    i2p_proxy=None,
    cookies=None,
    auto_disable=False,
    diagnose=False,
    no_progressbar=False,
    cloudflare_bypass: Optional[Dict[str, Any]] = None,
    dns_resolver: str = 'async',
) -> dict:
    """
    Run self-check on sites.

    Args:
        auto_disable: If True, automatically disable sites that fail checks.
                     If False (default), only report issues without disabling.
        diagnose: If True, print detailed diagnosis for each failing site.

    Returns:
        dict with 'needs_update' bool and 'results' list of check results
    """
    sem = asyncio.Semaphore(max_connections)
    tasks = []
    all_sites = site_data
    all_results = []

    def disabled_count(lst):
        return len(list(filter(lambda x: x.disabled, lst)))

    unchecked_old_count = len(
        [site for site in all_sites.values() if "unchecked" in site.tags]
    )
    disabled_old_count = disabled_count(all_sites.values())

    for _, site in all_sites.items():
        check_coro = site_self_check(
            site, logger, sem, db, silent, proxy, tor_proxy, i2p_proxy,
            skip_errors=True, cookies=cookies, auto_disable=auto_disable,
            diagnose=diagnose,
            cloudflare_bypass=cloudflare_bypass,
            dns_resolver=dns_resolver,
        )
        future = asyncio.ensure_future(check_coro)
        tasks.append((site.name, future))

    if tasks:
        with alive_bar(len(tasks), title='Self-checking', force_tty=True,
                       disable=no_progressbar, ctrl_c=False, enrich_print=False) as progress:
            for site_name, f in tasks:
                try:
                    result = await f
                except Exception as e:
                    logger.warning(
                        f"Self-check task for {site_name} raised unexpected error: {e}",
                        exc_info=True,
                    )
                    result = {
                        "disabled": False,
                        "issues": [f"Unexpected error: {e}"],
                        "recommendations": [],
                    }
                result['site_name'] = site_name
                all_results.append(result)
                progress()  # Update the progress bar

    unchecked_new_count = len(
        [site for site in all_sites.values() if "unchecked" in site.tags]
    )
    disabled_new_count = disabled_count(all_sites.values())
    total_disabled = disabled_new_count - disabled_old_count

    # Count issues
    total_issues = sum(1 for r in all_results if r.get('issues'))

    if auto_disable and total_disabled:
        if total_disabled >= 0:
            message = "Disabled"
        else:
            message = "Enabled"
            total_disabled *= -1

        if not silent:
            print(
                f"{message} {total_disabled} ({disabled_old_count} => {disabled_new_count}) checked sites. "
                "Run with `--info` flag to get more information"
            )
    elif total_issues and not silent:
        print(f"\nFound issues in {total_issues} sites (auto-disable is OFF)")
        print("Use --auto-disable to automatically disable failing sites")
        print("Use --diagnose to see detailed diagnosis for each site")

    if unchecked_new_count != unchecked_old_count:
        print(f"Unchecked sites verified: {unchecked_old_count - unchecked_new_count}")

    needs_update = total_disabled != 0 or unchecked_new_count != unchecked_old_count

    return {
        'needs_update': needs_update,
        'results': all_results,
        'total_issues': total_issues,
    }


def _cn_extract_fields(site) -> list:
    """这个站点声明了要抽哪些证据字段（来自 cn 策展数据）。

    返回空列表的含义是"这个站不值得抽"，**不是**"抽全部"。
    上游 data.json 里没有"抽取字段"这个概念，所以只认策展数据；
    这也意味着绝大多数站点不会产生任何抽取请求。

    任何异常都吞掉返回空列表：抽取是锦上添花，不该因为一条策展数据写坏
    就让整次扫描失败。
    """
    try:
        spec = spec_for(site)
    except Exception:
        return []
    if spec is None:
        return []
    return list(spec.extract or [])


def extract_ids_data(html_text, logger, site) -> Dict:
    try:
        return extract(html_text)
    except Exception as e:
        logger.warning(f"Error while parsing {site.name}: {e}", exc_info=True)
        return {}


MAX_MUTATIONS_PER_SITE = 3


def _domain_tail(host: str) -> str:
    parts = (host or "").rsplit(".", 2)
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _canonical_url(url: str) -> tuple:
    p = urlparse(url or "")
    return (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/").lower())


async def run_url_mutations(checker, site, results_info, options, logger, query_notify=None) -> None:
    """Fetch secondary URLs derived from the profile URL via socid_extractor's
    url_mutations and merge extracted fields into results_info.

    Reuses the site's already-configured checker (transport, proxy, cookies,
    TLS impersonation). Never raises: a failing mutation must not sink the
    primary CLAIMED result.
    """
    base_url = results_info.get("url_user")
    if not base_url:
        return

    try:
        mutations = list(mutate_url(base_url))
    except Exception as e:
        logger.debug(f"mutate_url failed for {site.name}: {e}", exc_info=True)
        return

    if not mutations:
        return

    # Drop mutations whose target domain differs from the site's own:
    # mutate_url uses unanchored re.search, so a URL embedded in a query param
    # can match a foreign scheme.
    site_host = (
        (urlparse(base_url).hostname or "")
        or (urlparse(site.url_main or "").hostname or "")
    ).lower()
    site_tail = _domain_tail(site_host)
    if site_tail and "." in site_tail:
        onsite = [(u, h) for u, h in mutations
                  if _domain_tail((urlparse(u).hostname or "").lower()) == site_tail]
        if len(onsite) != len(mutations) and query_notify is not None:
            query_notify.enrich(
                f"{site.name}: skip {len(mutations) - len(onsite)} mutation(s) "
                f"— cross-site (site domain {site_tail})",
                verbose_only=True,
            )
        mutations = onsite
    if not mutations:
        return

    # Drop mutations that would re-fetch the same URL Maigret already probed
    # (compared by scheme+host+path, ignoring query string and trailing slash).
    already_fetched = results_info.get("url_probe")
    if already_fetched:
        probe_canon = _canonical_url(already_fetched)
        deduped = [(u, h) for u, h in mutations if _canonical_url(u) != probe_canon]
        if len(deduped) != len(mutations) and query_notify is not None:
            query_notify.enrich(
                f"{site.name}: skip {len(mutations) - len(deduped)} mutation(s) "
                f"— maigret has built-in check for this mutation",
                verbose_only=True,
            )
        mutations = deduped
    if not mutations:
        return

    if len(mutations) > MAX_MUTATIONS_PER_SITE:
        logger.debug(
            f"{site.name}: {len(mutations)} mutations, capping at "
            f"{MAX_MUTATIONS_PER_SITE}"
        )
        mutations = mutations[:MAX_MUTATIONS_PER_SITE]

    result = results_info.get("status")
    if result is None:
        return
    if result.ids_data is None:
        result.ids_data = {}

    def notify(msg, verbose_only=False):
        if query_notify is not None:
            query_notify.enrich(msg, verbose_only=verbose_only)

    for mut_url, mut_headers in mutations:
        try:
            headers = dict(checker.headers or {})
            headers.update(site.headers or {})
            headers.update(mut_headers or {})
            # Mutations are always GET without a body: drop body-only headers
            # that would otherwise be inherited from the site's base check.
            for h in ("Content-Type", "Content-Length"):
                headers.pop(h, None)

            checker.prepare(
                url=mut_url,
                headers=headers,
                allow_redirects=True,
                timeout=checker.timeout,
                method='get',
                encoding=site.encoding,
            )
            options["enrich_requests"] = options.get("enrich_requests", 0) + 1
            html_text, status_code, err = await checker.check()
            if err:
                notify(f"{site.name}: {mut_url} → error: {err}", verbose_only=True)
                continue
            if not html_text:
                notify(
                    f"{site.name}: {mut_url} → status={status_code}, empty body",
                    verbose_only=True,
                )
                continue

            extra = extract_ids_data(html_text, logger, site)
            if not extra:
                notify(
                    f"{site.name}: {mut_url} → status={status_code}, "
                    f"no socid_extractor scheme matched ({len(html_text)}B body)",
                    verbose_only=True,
                )
                continue

            # мутация дополняет, а не переопределяет — сохраняем поля от основной страницы
            new_field_keys = []
            for k, v in extra.items():
                if k not in result.ids_data:
                    result.ids_data[k] = v
                    new_field_keys.append(k)

            new_usernames = parse_usernames(extra, logger)
            prev_usernames = results_info.get("ids_usernames") or {}
            merged_usernames = dict(prev_usernames)
            new_username_keys = []
            for k, v in new_usernames.items():
                if k not in merged_usernames:
                    merged_usernames[k] = v
                    new_username_keys.append(k)
            results_info["ids_usernames"] = merged_usernames

            extra_links = ascii_data_display(extra.get("links", "[]"))
            if "website" in extra:
                extra_links.append(extra["website"])
            merged_links = list(results_info.get("ids_links") or [])
            new_links = []
            for link in extra_links:
                if link not in merged_links:
                    merged_links.append(link)
                    new_links.append(link)
            results_info["ids_links"] = merged_links

            if not (new_field_keys or new_username_keys or new_links):
                notify(
                    f"{site.name}: enrich returned no new data via {mut_url}",
                    verbose_only=True,
                )
        except Exception as e:
            notify(f"{site.name}: {mut_url} → exception: {e}", verbose_only=True)
            logger.warning(
                f"URL mutation {mut_url} failed for {site.name}: {e}",
                exc_info=True,
            )


def parse_usernames(extracted_ids_data, logger) -> Dict:
    new_usernames = {}
    for k, v in extracted_ids_data.items():
        if "username" in k and not "usernames" in k:
            if is_plausible_username(v):
                new_usernames[v] = "username"
            else:
                logger.debug(
                    f"Rejected non-username value extracted under key {k!r}: {v!r}"
                )
        elif "usernames" in k:
            try:
                tree = ast.literal_eval(v)
                if isinstance(tree, list):
                    for n in tree:
                        if is_plausible_username(n):
                            new_usernames[n] = "username"
                        else:
                            logger.debug(
                                f"Rejected non-username item from list under key {k!r}: {n!r}"
                            )
            except Exception as e:
                logger.warning(e)
        elif k in SUPPORTED_IDS:
            new_usernames[v] = k
    return new_usernames


def update_results_info(results_info, extracted_ids_data, new_usernames):
    results_info["ids_usernames"] = new_usernames
    links = ascii_data_display(extracted_ids_data.get("links", "[]"))
    if "website" in extracted_ids_data:
        links.append(extracted_ids_data["website"])
    results_info["ids_links"] = links
    return results_info
