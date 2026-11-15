r"""站内搜索执行器（文档 §5.1、§7 P2）。

## 它解决什么问题

有些站点的个人主页地址**没法从用户名拼出来**：B 站是数字 uid、微博是数字 oid、
贴吧是内部 id。对这些站点，`uri_check` 里写 `{account}` 是没用的，只能走站内搜索。

而站内搜索**不能套四字段协议**，这是本模块存在的唯一理由：

    搜索结果页里必然出现你搜的那个词。如果拿 `e_string = "{account}"` 去判，
    每一次搜索都"命中"，于是每个用户都"注册过"每一个站点——这是最典型的
    假阳性，而且错得极其自信。

所以判据换成一条：**结果里有没有一个句柄与目标逐字相同**。
"搜到了包含这个词的内容"不等于"这个账号是你的"。

## 三条硬约束

1. **默认 UNKNOWN，不猜。** 只有两种情况能下确定结论：
   - 找到逐字相同的句柄 → CLAIMED（且置信度被 `EXECUTOR_FACTOR["search"] = 0.5`
     压到中低档，因为搜索只能证明"搜到了"，不能证明"这个主页是你的"）；
   - 页面明确表示"搜索结果为 0" → AVAILABLE。
   其余一律 UNKNOWN。搜索结果**分页且有排序**，"第一页里没有"根本推不出
   "不存在"——把那种情况判成 AVAILABLE，用户会以为自己没注册某个站。

2. **被拦就是被拦。** 登录墙、验证码、风控页各自映射到对应的 `Reason`，
   绝不退化成"没找到"。微博和贴吧的搜索页对未登录用户基本不可用，
   这条约束直接决定了它们的正确输出是"未判定"而不是"不存在"。

3. **不复用扫描用的 cookie。** 搜索走独立会话，理由和头像抓取一样：
   搜索请求会带上关键词，把登录态和关键词一起发给站点，泄露的是
   "这个登录用户正在查谁"。

## 站点配置

字段都从 `Site` 上读（用 `getattr`，加不加字段都能跑）：

    search_handle_pattern  结果条目里"句柄"的正则，**第 1 个捕获组**是句柄
    search_empty_marker    明确表示"结果为 0"的文案
    search_profile_url     用站内 ID 拼主页地址的模板，如
                           `https://space.bilibili.com/{id}`（可选）
    bootstrap_url          正式搜索前先访问一次的地址，用来拿 Cookie（可选）

搜索地址就用 `uri_check`，不另开一个字段：`uri_check` 本来就是"探测要请求的
地址"，而对这类站点它**只能是**搜索地址——主页地址拼不出来才是走搜索的原因。
两个字段存同一个值，只会让人分不清哪个生效。

`search_handle_pattern` 缺失时直接判 UNKNOWN / RULE_MISSING：没有它就无法
把"结果条目"和"页面上的其他链接"分开，硬猜必然误判。

### 正则里可以给分组起名字

    (?P<handle>[^"]+)   句柄。不写就退回"第 1 个捕获组"
    (?P<id>\d+)         该条结果的站内 ID，配合 search_profile_url 拼主页

JSON 接口里 ID 往往排在昵称**前面**（B 站是 `"mid":123,"uname":"x"`）。
想让"第 1 组"仍然是句柄，就得能给前面那个组起名字；否则两组顺序一颠倒，
规则会从"比对昵称"悄悄变成"比对数字 ID"，而且永远不会报错——直到某天
有人发现报告里点开的链接全是别人的主页。

### 为什么要 bootstrap_url

B 站搜索接口需要首页下发的 `buvid3` Cookie：不预热时第一次能过，之后一律
412（实测连续 3 次）。412 会被映射成 BLOCKED（不是"不存在"），所以不预热
的后果是"规则看着没问题，结果永远是未判定"。
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..scoring import score
from ..verdict import (
    Reason,
    Status,
    Verdict,
    block_signals,
    looks_like_captcha,
    looks_like_login_wall,
    unknown_verdict,
)

logger = logging.getLogger(__name__)

# 站点配置字段名（与 Site 上的属性同名）
FIELD_HANDLE_PATTERN = "search_handle_pattern"
FIELD_EMPTY_MARKER = "search_empty_marker"
FIELD_PROFILE_URL = "search_profile_url"

# 正则里的具名分组（可选）：
#   (?P<handle>...)  句柄。没写就退回"第 1 个捕获组"
#   (?P<id>...)      该条结果的站内 ID，配合 search_profile_url 拼出主页地址
# 为什么要具名：JSON 接口里 ID 往往排在昵称**前面**（B 站是 mid,uname），
# 想让第 1 组仍是句柄就必须能给"前面那个组"起名字，否则两组顺序一颠倒，
# 规则就从"比对昵称"悄悄变成"比对数字 ID"，且永远不会报错。
GROUP_HANDLE = "handle"
GROUP_ID = "id"

DEFAULT_TIMEOUT = 15.0

# 只保留前若干个候选：一个宽泛的关键词能搜出几百条，逐个比对没有意义，
# 也容易把"第 137 条里恰好有同名"当成命中。
MAX_CANDIDATES = 50


@dataclass
class SearchOutcome:
    """一次站内搜索的结论。

    `candidates` 只用于解释和测试，**不进报告**：搜索结果里的其他用户名是
    别人的公开昵称，把一列别人的昵称写进"我的自查报告"既没用也不合适。
    """

    verdict: Verdict
    candidates: int = 0
    handles: List[str] = field(default_factory=list)
    # 命中时该账号的主页地址（由 `search_profile_url` 拼出）。空串表示
    # 规则没声明模板，调用方应退回搜索页地址 —— 有主页地址才值得点开。
    profile_url: str = ""

    def __bool__(self) -> bool:
        return self.verdict.status == Status.CLAIMED


def _site_name(site: Any) -> str:
    return str(getattr(site, "name", "<unnamed>"))


def _field(site: Any, name: str) -> str:
    value = getattr(site, name, "")
    return str(value).strip() if value else ""


def build_search_url(site: Any, account: str) -> str:
    """把用户名填进搜索地址（即 `uri_check`）。"""
    from urllib.parse import quote_plus

    template = _field(site, "uri_check")
    if not template:
        return ""
    # 用户名里的空格、`+`、`&` 都必须编码，否则会被当成查询参数分隔符。
    return template.replace("{account}", quote_plus(account)).replace(
        "{username}", quote_plus(account)
    )


def extract_matches(site: Any, html: str) -> Optional[List[tuple]]:
    """按站点配置的正则抽出 `(句柄, 站内 ID)` 列表。

    返回 `None` 表示**没法抽**（没配正则或正则写错），与"抽到了 0 个"
    （返回空列表）是两件事：前者说明规则不完整，后者说明结果区是空的。

    ID 可能为空字符串（规则没写 `(?P<id>...)`），这时调用方退回搜索页地址。
    """
    pattern = _field(site, FIELD_HANDLE_PATTERN)
    if not pattern:
        return None
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        logger.warning("站点 %s 的 %s 不是合法正则：%s", _site_name(site), FIELD_HANDLE_PATTERN, exc)
        return None

    named = compiled.groupindex
    matches: List[tuple] = []
    seen = set()
    for match in compiled.finditer(html or ""):
        if GROUP_HANDLE in named:
            handle = match.group(GROUP_HANDLE)
        elif match.groups():
            handle = match.group(1)
        else:
            handle = match.group(0)
        handle = (handle or "").strip()
        if not handle:
            continue
        ident = (match.group(GROUP_ID) if GROUP_ID in named else "") or ""
        key = (handle, ident)
        if key in seen:
            continue
        seen.add(key)
        matches.append((handle, ident.strip()))
        if len(matches) >= MAX_CANDIDATES:
            break
    return matches


def extract_handles(site: Any, html: str) -> Optional[List[str]]:
    """只要句柄的薄封装（报告里不展示别人的 ID，测试也大多只关心句柄）。"""
    matches = extract_matches(site, html)
    if matches is None:
        return None
    return [handle for handle, _ in matches]


def build_profile_url(site: Any, ident: str) -> str:
    """用 `search_profile_url` 把站内 ID 拼成主页地址。拼不出来返回空串。

    为什么不直接用搜索结果里的链接：JSON 接口里通常没有可直接展示的地址
    （B 站只给 mid）。让规则显式声明模板，比在执行器里写死各站 URL 好维护。
    """
    template = _field(site, FIELD_PROFILE_URL)
    if not template or not ident:
        return ""
    from urllib.parse import quote

    return template.replace("{id}", quote(str(ident), safe=""))


def _interrupted_reason(site: Any, html: str) -> Optional[Verdict]:
    """先判"被打断"，再判"有没有"。顺序反过来必然产生假阳性。

    这个顺序不是风格问题：风控页和登录墙都是 HTTP 200、页面结构完整、
    正文里还常常带着你搜的那个词。先判存在性的话，它们会被判成 CLAIMED。
    """
    if not (html or "").strip():
        return unknown_verdict(Reason.EMPTY_BODY)

    if looks_like_login_wall(html):
        return unknown_verdict(Reason.LOGIN_REQUIRED)

    if looks_like_captcha(html):
        return unknown_verdict(Reason.CAPTCHA)

    strong, _ = block_signals(html)
    if strong:
        return unknown_verdict(Reason.BLOCKED, evidence="、".join(strong[:3]))

    return None


def decide(
    site: Any,
    account: str,
    html: str,
    http_status: Optional[int] = None,
) -> SearchOutcome:
    """从已经拿到的搜索页正文下结论。纯函数，不发请求，便于测试。"""
    # 非 200 先下结论，再看正文。412/403 的响应体往往是站点自己的错误 JSON
    # 或 WAF 页，里面一个结果条目都没有 —— 按正文判会得出"没有同名账号"，
    # 而事实是"我们被拦了"。这两件事对用户的意义完全相反。
    if http_status is not None and http_status != 200:
        return SearchOutcome(
            unknown_verdict(
                _status_to_reason(http_status),
                evidence=f"搜索页返回 {http_status}",
                http_status=http_status,
            )
        )

    interrupted = _interrupted_reason(site, html)
    if interrupted is not None:
        interrupted.http_status = http_status
        return SearchOutcome(interrupted)

    matches = extract_matches(site, html)
    if matches is None:
        return SearchOutcome(
            Verdict(
                Status.UNKNOWN,
                reason=Reason.RULE_MISSING,
                evidence=f"缺少 {FIELD_HANDLE_PATTERN}，无法从搜索结果里取出句柄",
                http_status=http_status,
            )
        )
    handles = [handle for handle, _ in matches]

    # 逐字比对用 casefold：大小写不同的同一个句柄是同一个账号，
    # 但 "wizzer2" 与 "wizzer" 不是 —— 这里不做任何模糊匹配。
    target = (account or "").casefold()
    matched = next(
        ((handle, ident) for handle, ident in matches if handle.casefold() == target),
        ("", ""),
    )

    if matched[0]:
        return SearchOutcome(
            Verdict(
                Status.CLAIMED,
                evidence=f"搜索结果里有同名账号：{matched[0]}",
                evidence_type="name_match_only",
                # 搜索执行器的系数是 0.5，这里再乘 name_match_only 的 0.3：
                # 搜索只能证明"存在一个叫这个名字的账号"，证明不了"是你的"。
                confidence=score(site, "name_match_only"),
                http_status=http_status,
            ),
            candidates=len(handles),
            handles=handles,
            profile_url=build_profile_url(site, matched[1]),
        )

    empty_marker = _field(site, FIELD_EMPTY_MARKER)
    if empty_marker and empty_marker.lower() in (html or "").lower():
        return SearchOutcome(
            Verdict(
                Status.AVAILABLE,
                evidence=f"搜索页明确表示没有结果：{empty_marker}",
                http_status=http_status,
            ),
            candidates=0,
        )

    # 走到这里说明"结果区渲染出来了，但里面没有逐字相同的句柄"。
    # 这不等于不存在：搜索有分页、有排序，也按昵称而不是句柄匹配。
    return SearchOutcome(
        Verdict(
            Status.UNKNOWN,
            reason=Reason.SEARCH_INCONCLUSIVE,
            evidence=f"取到 {len(handles)} 个候选句柄，没有与 {account} 逐字相同的",
            http_status=http_status,
        ),
        candidates=len(handles),
        handles=handles,
    )


def _status_to_reason(status: int) -> Reason:
    if status == 429:
        return Reason.RATE_LIMITED
    if status in (401, 403, 412):
        # 412 是中文站最常用的风控码（B 站、部分 WAF）。它和 403 是一回事：
        # "我不给你看"，不是"没有这个账号"。
        return Reason.BLOCKED
    if status >= 500:
        return Reason.BLOCKED
    return Reason.HTTP_ERROR


async def _prime(session: Any, site: Any, headers: dict, timeout: float) -> None:
    """先访问一次 `bootstrap_url`，把服务端下发的 Cookie 收进会话。

    有些搜索接口（B 站 `api.bilibili.com/x/web-interface/search/type`）第一次
    能过、之后一律 412：它要的是首页下发的 `buvid3` Cookie。不预热的话，
    规则本身没写错，却会稳定地得到"被拦截"。

    失败一律忽略：预热只是提高成功率，预热不到就照常请求，让真正的错误
    在判定层体现出来，别在请求前就把它咽掉。
    """
    url = _field(site, "bootstrap_url")
    if not url:
        return
    try:
        async with session.get(
            url, timeout=timeout, headers=headers, allow_redirects=True
        ) as response:
            await response.text(errors="replace")
    except Exception as exc:  # noqa: BLE001 — 预热失败不影响主请求
        logger.debug("站点 %s 预热 %s 失败（忽略）：%s", _site_name(site), url, exc)


async def search(
    site: Any,
    account: str,
    session: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> SearchOutcome:
    """跑一次站内搜索。任何异常都变成带原因的 UNKNOWN，不向外抛。

    注意：主流程**不走这里**。真实扫描时请求由 `checking.py` 的 checker 发出
    （它统一处理 UA、代理、预热），判定再由 `hook.apply_cn_verdict` 调本模块的
    `decide()`。这个函数留给自检脚本和单独调用——两条路径共用同一个 `decide`，
    所以判据不会分叉。
    """
    url = build_search_url(site, account)
    if not url:
        return SearchOutcome(
            Verdict(
                Status.UNKNOWN,
                reason=Reason.RULE_MISSING,
                evidence="uri_check 是空的，不知道该请求哪个搜索地址",
            )
        )

    import aiohttp

    headers = {"User-Agent": "Mozilla/5.0 (compatible; maigret-cn self-check)"}
    own_session = session is None
    if own_session:
        # trust_env=False：搜索走干净会话，不继承环境里的代理和 cookie。
        session = aiohttp.ClientSession(trust_env=False)

    try:
        await _prime(session, site, headers, timeout)
        async with session.get(
            url, timeout=timeout, headers=headers, allow_redirects=True
        ) as response:
            status = response.status
            if status == 200:
                html = await response.text(errors="replace")
                return decide(site, account, html, http_status=status)
            # 非 200 不代表"不存在"：搜索接口改版、限流、被拦都会走到这里。
            return SearchOutcome(
                unknown_verdict(
                    _status_to_reason(status),
                    evidence=f"搜索页返回 {status}",
                    http_status=status,
                )
            )
    except Exception as exc:  # noqa: BLE001 — 任何失败都降级为可解释的 UNKNOWN
        import asyncio

        reason = (
            Reason.TIMEOUT if isinstance(exc, asyncio.TimeoutError) else Reason.HTTP_ERROR
        )
        logger.debug("站点 %s 站内搜索失败：%s", _site_name(site), exc, exc_info=True)
        return SearchOutcome(unknown_verdict(reason, evidence=str(exc)[:200]))
    finally:
        if own_session:
            await session.close()
