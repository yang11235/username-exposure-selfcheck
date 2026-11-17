"""脚迹检索：把"这个用户名在公开场合说过什么"尽量捞回来。

## 定位

这是**检索层**，不是分析层。它只负责"把能查到的原始材料取回来并排好序"，
不回答"这个人多大、在哪上学、做什么工作"。后一类推断交给报告之后的 AI——
理由很实在：**推断会过时、会出错、要负责任，而原始材料不会。**
把两者分开，材料可以一直积累，推断随时可以重做。

## 合规边界（写进代码，不是写在文档里）

1. **只访问公开可读的页面。** 不绕过登录墙、不破解签名、不用别人的身份。
2. **不做搜索引擎结果页抓取。** 实测 `so.com` 的 `robots.txt` 是
   `Disallow: /s`、百度是 `Disallow: /`——在对方明确禁止的地方取数据，
   即使技术上做得到也不做。上一版方案试图走这条路，已作废。
3. **请求预算与间隔是硬约束，不是建议。** 单次运行有上限，每个域名之间有
   间隔。这是为了不被当成爬虫，也是为了不给对方造成压力。
4. **被拦就记"被拦"。** 403/验证页一律记成 `blocked`，绝不退化成"没查到"——
   把"我没读到"说成"他没有"，是本项目最不能犯的错。

## 能做什么、不能做什么（实测结论，见 docs/content-access-feasibility.md）

能做：
- **Discuz 空间页**（匿名可读）：注册时间、最后访问、上次发表时间、在线时长
- **Discuz 按作者筛选楼层**（`&authorid=`）：把某人在某帖的全部发言和时间戳一次取全
- **贴吧手机版**（需用户自己的登录态）：楼层正文

不能做：
- Discuz 的"该用户发过的主题列表"（`do=thread` 被权限拦住，实测多个站一致）
- 搜索引擎结果页（robots 禁止 + 约 40 次请求封 IP）

所以发现新 URL 这一步**必须由人提供或由上层已有结果提供**——
本模块的 `seed_urls` 就是为此存在的入口。
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from . import apis, content
from .content import Post, Profile

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"

# ---------------------------------------------------------------------------
# 预算与礼貌
# ---------------------------------------------------------------------------
# 单次运行的页面请求上限。为什么要有：没有上限的检索器一旦遇到
# "页面里有链接就跟着走"的实现，会自己把自己跑成爬虫。
DEFAULT_MAX_REQUESTS = 60

# 同一域名两次请求之间的最小间隔（秒）。Discuz 站多是个人/小团队维护，
# 打太快不只是"可能被封"，是真的会影响别人。
DEFAULT_DOMAIN_DELAY = 1.2

# 单次运行最多跟进的分页数（每个帖子）。Discuz 的"只看作者"视图
# 一页 10-20 楼，跟 3 页已经能覆盖绝大多数人的发言。
DEFAULT_MAX_PAGES_PER_THREAD = 3

# 每个站点最多深挖几个"签名里的帖子"。
#
# 为什么要上限：签名可能贴一整排帖子地址，而预算是全站共享的。
# 取 3 是因为实测绝大多数人的签名里最多一两个帖子地址；
# 真的贴了十几个的，前 3 个已经足以判断"这人在这站干什么"。
DEFAULT_MAX_THREADS_PER_SITE = 3

# 每个站最多解析的楼层数，防止某些巨型水楼把结果撑爆
DEFAULT_MAX_POSTS_PER_SITE = 200


@dataclass
class Footprint:
    """一条可核对的公开痕迹。这是本模块的输出单位。"""

    site: str
    url: str
    # profile / post：前者是"账号资料说的"，后者是"他自己写的"
    kind: str = "post"
    author: str = ""
    posted_at: str = ""
    text: str = ""
    forum: str = ""
    thread_title: str = ""
    anchor: str = ""
    # 原始时间串归一化后的形态（YYYY-MM-DD），推不出就留空。
    # 保留 `posted_at` 原串是为了可核对：归一化可能出错，原串不会。
    date: str = ""
    clues: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "site": self.site,
            "url": self.url,
            "kind": self.kind,
            "author": self.author,
            "posted_at": self.posted_at,
            "date": self.date,
            "text": self.text,
            "forum": self.forum,
            "thread_title": self.thread_title,
            "anchor": self.anchor,
            "clues": self.clues,
        }


@dataclass
class FetchResult:
    """一次取页面的结果。

    `status` 的取值刻意把"被拦"和"没有"分开：
        ok       取到了
        blocked  被风控/登录墙拦住 —— 必须让用户看到
        notfound 页面确实不存在
        error    网络/超时等
    """

    status: str
    url: str
    text: str = ""
    http_status: Optional[int] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class SiteReport:
    """一个站点的检索结果汇总（用于向用户解释"这个站我查到了什么"）。"""

    site: str
    profile: Optional[Profile] = None
    footprints: List[Footprint] = field(default_factory=list)
    status: str = "ok"
    detail: str = ""
    requests: int = 0

    @property
    def found(self) -> bool:
        return bool(self.profile or self.footprints)


# ---------------------------------------------------------------------------
# 日期归一化
# ---------------------------------------------------------------------------
_DATE_FULL = re.compile(
    r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})"
)


def to_iso_date(value: str) -> str:
    """把各种中文站日期串归一成 `YYYY-MM-DD`，认不出来返回空串。

    刻意只做"日"一级，不做到秒：
        - 各站时区不统一（Discuz 底部写 GMT+8，但用户可以自己改）
        - 做到秒会给人"这个时间精确可信"的错觉，而它可能来自"最后编辑时间"
    **认不出来就返回空**，不用当前时间兜底——那会凭空造出一条"最近活跃"。

    三重校验，都是为了不让"不是日期的东西"混进来：
        1. 前面不能紧跟字母或数字 → `v2020.1.5` 这种版本号不算日期
        2. 年/月/日范围
        3. 日历是否真实存在（`2009-02-30` 不算）
    第 3 条来自评审反馈：原实现只查了范围，`2009-02-30` 会通过并进入时间线。
    """
    match = _DATE_FULL.search(value or "")
    if not match:
        return ""
    # `\b` 对中文无效，这里手动看前一个字符
    start = match.start()
    prefix = (value or "")[:start]
    if prefix and (prefix[-1].isalnum()):
        return ""
    year, month, day = match.group(1), match.group(2), match.group(3)
    if not content._valid_date(year, month, day):
        return ""
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


# ---------------------------------------------------------------------------
# 站点识别
# ---------------------------------------------------------------------------
def _host(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def looks_like_discuz(html_text: str) -> bool:
    """是不是 Discuz 页面。

    靠 `Powered by Discuz` 与 `home.php?mod=space` 两个特征。
    为什么不用域名白名单：Discuz 是模板系统，被大量站点复用，
    **靠结构识别能覆盖的站远多于靠域名枚举**，而且新站不用改代码。
    """
    text = html_text or ""
    return "Powered by Discuz" in text or "home.php?mod=space" in text


def profile_url(host: str, username: str) -> str:
    """Discuz 空间页地址。这是匿名可读的入口。"""
    from urllib.parse import quote

    return f"https://{host}/home.php?mod=space&username={quote(username, safe='')}"


def archiver_author_url(thread_url: str, author_id: str, page: int = 1) -> str:
    """构造 Discuz `archiver/` 的"只看作者"地址。

    **为什么优先用这个而不是 `forum.php?...&authorid=`：**

        普通作者视图  forum.php?mod=viewthread&tid=436216&authorid=463889  78 KB
        Archiver 视图 archiver/?tid-436216.html&authorid=463889          5.2 KB

    同样的 16 条发言，Archiver 小 15 倍。省的不只是流量——请求预算按
    次数算，但单页体积直接影响耗时与被限流的概率。而且 Archiver 页是
    服务端渲染的静态 HTML，没有 JS、没有重排，解析规则也不会随皮肤变。

    另一个好处：`archiver/` **不在** robots.txt 的 Disallow 列表里
    （被禁的是 `/search.php` 等），而论坛自己的搜索页明确 `Disallow: /search.php`，
    所以这条路在合规上也更干净。
    """
    if not thread_url or not author_id:
        return ""
    match = re.search(r"(?:thread-|tid-|tid=)(\d+)", thread_url)
    if not match:
        return ""
    base = f"{urlparse(thread_url).scheme}://{urlparse(thread_url).netloc}"
    url = f"{base}/archiver/?tid-{match.group(1)}.html&authorid={author_id}"
    if page and page > 1:
        url = f"{url}&page={page}"
    return url


def author_filter_url(thread_url: str, author_id: str, page: int = 1) -> str:
    """把主题页地址改造成"只看该作者"的地址。

    Discuz 的 `authorid=` 参数会让服务端**只返回该用户的楼层**，
    这是本模块最有价值的单点能力：一旦知道"他在这个帖里发过言"，
    就能把他的全部发言和精确时间一次取全，不用逐页翻。

    实现上直接把 `mod=viewthread&tid=X` 改成 `mod=viewthread&tid=X&authorid=Y`。
    地址形态可能是 `thread-123-1-1.html`（伪静态）或
    `forum.php?mod=viewthread&tid=123`，两种都处理。
    """
    if not thread_url or not author_id:
        return ""

    # 伪静态形态：thread-123-1-1.html → forum.php?mod=viewthread&tid=123
    match = re.search(r"thread-(\d+)-", thread_url)
    if match:
        base = f"{urlparse(thread_url).scheme}://{urlparse(thread_url).netloc}"
        url = f"{base}/forum.php?mod=viewthread&tid={match.group(1)}&authorid={author_id}"
    elif "mod=viewthread" in thread_url:
        url = re.sub(r"&?authorid=\d+", "", thread_url)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}authorid={author_id}"
    else:
        return ""

    if page and page > 1:
        url = f"{url}&page={page}"
    return url


def thread_page_url(thread_url: str, page: int) -> str:
    """同帖翻页地址（不带 authorid）。"""
    if "mod=viewthread" in thread_url:
        url = re.sub(r"&?page=\d+", "", thread_url)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}page={page}"
    match = re.search(r"thread-(\d+)-(\d+)-(\d+)\.html", thread_url)
    if match:
        return thread_url.replace(
            f"thread-{match.group(1)}-{match.group(2)}-{match.group(3)}.html",
            f"thread-{match.group(1)}-{page}-{match.group(3)}.html",
        )
    return ""


def max_page_from(html_text: str) -> int:
    """从主题页里读出"共 N 页"。读不到返回 1。"""
    match = re.search(r"共\s*(\d+)\s*页", html_text or "")
    if match:
        try:
            return max(1, int(match.group(1)))
        except ValueError:
            return 1
    return 1


# ---------------------------------------------------------------------------
# 站点清单
# ---------------------------------------------------------------------------
FORUMS_FILE = _DATA_DIR / "forums_cn.json"

# 清单文件读不出来时的兜底：只用 2026-11-16 实测确认过的那一条。
# 为什么要有兜底而不是返回空：空清单会让用户看到"没有痕迹"，
# 而异地读取失败被说成"没有"，正是本项目最不该犯的错。
_FALLBACK_FORUMS = ("bbs.newwise.com",)


def load_forums(path: Optional[Any] = None) -> List[str]:
    """读公开痕迹检索用的论坛清单。

    只返回有 `verified_at` 的条目——没实测过的域名放进来，
    用户看到的是"这个站查不到"，而不是"这个站没查"，两者含义相反。
    """
    target = Path(path) if path else FORUMS_FILE
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("论坛清单 %s 读取失败（%s），退回内置清单", target, exc)
        return list(_FALLBACK_FORUMS)

    forums = payload.get("forums") or {}
    hosts: List[str] = []
    for host, meta in forums.items():
        if not isinstance(meta, dict) or not meta.get("verified_at"):
            continue
        hosts.append(str(host))
    return hosts or list(_FALLBACK_FORUMS)


# ---------------------------------------------------------------------------
# 请求节流（给 apis.py 用）
# ---------------------------------------------------------------------------
class _PacedSession:
    """给 `apis.py` 的采集器套一层"每个域名之间有间隔"。

    为什么需要（2026-11-17 独立评审 P2#20）：`apis.py` 里的采集器直接调
    `session.get(...)`，**绕过了 `fetch()` 里的 `_reserve`**，所以 GitHub 的
    5 个请求、网易云的 3 个请求会在同一瞬间连发。而本模块对外承诺的是
    "每个域名之间有间隔"——承诺只对 Discuz 站成立，对接口层不成立。

    预算不需要在这里判：`collect_from_apis` 已经用 `reserve_slots()` 一次性
    预留过整轮额度。这里只补"别连发"这一件事。

    实现成 async context manager，是因为调用方的写法是
    `async with session.get(url) as response:`——包装对象必须同样可 `async with`。
    """

    def __init__(self, session: Any, delay: float) -> None:
        self._session = session
        self._delay = max(0.0, float(delay))
        self._last_hit: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def _wait_turn(self, url: str) -> None:
        host = _host(url)
        while True:
            async with self._lock:
                loop = asyncio.get_running_loop()
                now = loop.time()
                previous = self._last_hit.get(host)
                if previous is None or now - previous >= self._delay:
                    self._last_hit[host] = now
                    return
                wait = self._delay - (now - previous)
            await asyncio.sleep(wait)

    def get(self, url: str, **kwargs: Any) -> "_PacedRequest":
        return _PacedRequest(self, url, kwargs)


class _PacedRequest:
    """`_PacedSession.get()` 的返回值：先等间隔，再真正进 aiohttp 的上下文。"""

    def __init__(self, owner: _PacedSession, url: str, kwargs: Dict[str, Any]) -> None:
        self._owner = owner
        self._url = url
        self._kwargs = kwargs
        self._cm: Any = None

    async def __aenter__(self) -> Any:
        await self._owner._wait_turn(self._url)
        self._cm = self._owner._session.get(self._url, **self._kwargs)
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._cm.__aexit__(*exc)


# ---------------------------------------------------------------------------
# 检索器
# ---------------------------------------------------------------------------
class FootprintFinder:
    """按用户名/种子 URL 去公开页面上捞痕迹。

    生命周期很短：一次自查 new 一个，跑完就丢。不做持久化、不做队列、
    不做"目标任务列表"——那三样一旦有了，这个工具就从自查变成了调查工具。
    """

    def __init__(
        self,
        session: Any,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        domain_delay: float = DEFAULT_DOMAIN_DELAY,
        max_pages_per_thread: int = DEFAULT_MAX_PAGES_PER_THREAD,
        max_threads_per_site: int = DEFAULT_MAX_THREADS_PER_SITE,
        timeout: float = 20.0,
        user_agent: str = "",
    ):
        self.session = session
        self.max_requests = max_requests
        self.domain_delay = domain_delay
        self.max_pages_per_thread = max_pages_per_thread
        self.max_threads_per_site = max(0, int(max_threads_per_site))
        self.timeout = timeout
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        )
        self.requests_made = 0
        self.budget_exhausted = False
        self._last_hit: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    # -- 预算与礼貌 --------------------------------------------------------
    async def _reserve(self, url: str) -> str:
        """原子地"预留一次请求额度"。返回空串表示可以发，否则返回拒绝原因。

        为什么必须把「查预算」「查间隔」「记账」放在同一个锁里做完：
            这三件事之间只要有一个 **await**，并发任务就会读到同一份旧状态，
            然后一起通过检查、一起发出去。实测（预算 2、同域 5 并发）：
            5 个请求全部发出，其中 4 个在同一毫秒——预算和间隔同时失效。
            而这两条是本模块对外的合规承诺，不是性能优化项。

        等间隔也放在这里：先预约"下次最早何时"，预约不到就 sleep 后重试，
        而不是 sleep 完直接发——后者会让所有等待者同时醒来。
        """
        host = _host(url)
        while True:
            async with self._lock:
                if self.requests_made >= self.max_requests:
                    self.budget_exhausted = True
                    return "budget"

                loop = asyncio.get_running_loop()
                now = loop.time()
                previous = self._last_hit.get(host)
                if previous is None or now - previous >= self.domain_delay:
                    self._last_hit[host] = now
                    self.requests_made += 1
                    return ""
                wait = self.domain_delay - (now - previous)

            await asyncio.sleep(wait)

    async def fetch(self, url: str, referer: str = "") -> FetchResult:
        """取一个页面。所有失败都变成可解释的结果，不向外抛。"""
        if not url:
            return FetchResult("error", url, detail="空地址")

        refusal = await self._reserve(url)
        if refusal:
            return FetchResult(
                refusal,
                url,
                detail=f"本次运行的请求预算已用完（{self.max_requests} 次）",
            )

        headers = {"User-Agent": self.user_agent}
        if referer:
            headers["Referer"] = referer

        try:
            async with self.session.get(
                url, timeout=self.timeout, headers=headers, allow_redirects=True
            ) as response:
                raw = await response.read()
                text = _decode(raw, response)
                status = response.status
        except asyncio.TimeoutError:
            return FetchResult("error", url, detail="超时")
        except Exception as exc:  # noqa: BLE001 — 单个站失败不该中断整轮
            logger.debug("取 %s 失败：%s", url, exc)
            return FetchResult("error", url, detail=str(exc)[:160])

        if status == 404:
            return FetchResult("notfound", url, http_status=status)
        if content.looks_blocked(text, status):
            return FetchResult(
                "blocked",
                url,
                http_status=status,
                detail=f"被拦（HTTP {status}）" if status and status != 200 else "页面是验证/登录墙",
            )
        # 其余 5xx 一律算"请求失败"，不能当 ok 继续解析。
        #
        # 2026-11-17 独立评审 P2：`looks_blocked` 只认 503，500 / 502 / 504
        # 会一路落到下面的 `ok`，于是站点的 500 错误页被当成正常页面去解析，
        # 最终报成"页面结构不认识"——把"服务器挂了"说成"我们解析不了"。
        # 两者的处置完全不同：前者该重试或跳过，后者该改解析器。
        if status is not None and status >= 500:
            return FetchResult(
                "error", url, http_status=status, detail=f"服务端错误（HTTP {status}）"
            )
        return FetchResult("ok", url, text=text, http_status=status)

    # -- 单站检索 ----------------------------------------------------------
    async def probe_discuz_site(self, host: str, username: str) -> SiteReport:
        """在一个 Discuz 站上按用户名找资料页，并尽量取他的发言。

        顺序：资料页（拿 uid 与时间线）→ 有 uid 才能用 `authorid` 抽楼层。
        """
        report = SiteReport(site=host)
        url = profile_url(host, username)
        result = await self.fetch(url)
        report.requests = 1

        if result.status == "notfound":
            report.status = "absent"
            report.detail = "该站没有这个用户名"
            return report
        if not result.ok:
            report.status = result.status
            report.detail = result.detail
            return report

        if not looks_like_discuz(result.text):
            report.status = "not_discuz"
            report.detail = "页面不是 Discuz 结构"
            return report

        profile = content.parse_profile(result.text)
        if profile is None:
            # 关键：Discuz 的「提示信息」模板同时用于"没有这个用户"和
            # "没有权限"，只看到「提示信息」就判 absent 会把"我被拦了"
            # 写成"这个站没这个人"。所以先看明确的文案，再看不确定。
            if content.discuz_absent(result.text):
                report.status = "absent"
                report.detail = "该站没有这个用户名"
            elif content.discuz_permission_wall(result.text):
                report.status = "blocked"
                report.detail = "被权限墙拦住（该页对游客不可见），不代表没有这个用户"
            else:
                report.status = "unparsed"
                report.detail = "是 Discuz 页面，但没解析出资料字段"
            return report

        # 资料页上的名字必须与查询的用户名对得上。
        #
        # 为什么要这一步（2026-11-17 独立评审提出）：地址是
        # `/home.php?mod=space&username=<查询名>`，Discuz 自己解析这个参数。
        # 只要它做了一次近似匹配、或者把请求重定向到别的用户，
        # 我们就会把**另一个人的**注册时间、在线时长、回帖数记成目标的。
        # 这与"整帖照收"是同一类错误。
        #
        # 判据分三种，都不能一刀切：
        #   - 名字非空且与查询名不符 → 不记，并如实说明（强证据：这是别人）
        #   - 名字非空且相符         → 正常记
        #   - 名字为空（部分老模板不显示昵称，实测 bbs.wuyou.net 就是）
        #                            → 无法核对，仍然记，但在提示里说明
        # 如果对第三种也拒绝，wuyou 那种站的真实账号会被整站丢掉——那是漏报。
        if profile.name and not content.same_author(profile.name, username):
            report.status = "unparsed"
            report.detail = (
                f"资料页返回的是 {profile.name!r}，与查询名 {username!r} 不符，"
                "已丢弃（可能是站点的近似匹配或重定向）"
            )
            return report

        report.profile = profile
        report.status = "ok"

        # 资料本身也是一条痕迹（注册/最后访问时间对定位阶段极有用）
        report.footprints.append(_profile_footprint(host, url, profile, username))
        return report

    async def reserve_slots(self, count: int) -> bool:
        """一次预留 N 个请求额度，用于"一次调用会发出多次请求"的采集器。

        为什么需要：公开接口采集器（`apis.py`）内部会打 2-3 个地址，
        如果只按"调用一次"记一笔，实际发出的请求数会超过预算——
        那种超量是看不见的，因为日志里只出现一次调用。
        """
        async with self._lock:
            if self.requests_made + count > self.max_requests:
                self.budget_exhausted = True
                return False
            self.requests_made += count
            return True

    async def collect_from_profile_links(
        self, username: str, reports: Sequence["SiteReport"]
    ) -> Tuple[List[Footprint], List[str]]:
        """从资料页里发现的站内帖子地址继续深挖。

        这是本轮补上的关键一环。原来只有 `--cn-thread` 手动喂地址才会深挖帖子，
        Web 端没有这个输入，于是论坛那一路永远只有资料页那一行元信息——
        用户看到的正是"有同名账号，但没提取到什么有效信息"。

        线索来源：用户在**个人签名**里自己贴的帖子地址（实测 yang11235 就有
        `thread-481192-1-1.html`）。这是唯一不需要人肉找、也不需要绕过
        服务端限制的"发现"路径——因为地址是他自己写在公开资料上的。

        限制每个站点最多跟进 `max_threads_per_site` 个帖子：签名可能贴很多，
        而预算是全站共享的，不该被一个站点吃光。
        """
        footprints: List[Footprint] = []
        notes: List[str] = []

        for report in reports:
            if not (report.profile and report.profile.site_links):
                continue
            tids = report.profile.site_links[: self.max_threads_per_site]
            if not tids:
                continue
            skipped = len(report.profile.site_links) - len(tids)
            if skipped > 0:
                notes.append(
                    f"{report.site}：签名里还有 {skipped} 个帖子地址未跟进（单站上限 "
                    f"{self.max_threads_per_site}）"
                )

            for tid in tids:
                thread_url = f"https://{report.site}/forum.php?mod=viewthread&tid={tid}"
                posts, per_notes, status = await self.collect_from_thread(
                    thread_url, username, report.profile.uid or ""
                )
                notes.extend(per_notes)
                if posts:
                    footprints.extend(posts)
                else:
                    # 读不到时必须说明**是哪个帖子、为什么**。
                    # 只说"这个站没有内容"会让用户以为工具没干活；
                    # 而事实可能是"帖子在权限版块里，匿名永远读不到"。
                    reason = _status_reason(status)
                    notes.append(
                        f"{report.site}：资料页里的帖子 tid={tid} 没读到（{reason}）"
                    )

        return footprints, notes

    async def collect_from_apis(self, username: str) -> Tuple[List[Footprint], List[str]]:
        """从公开接口取账号时间线。

        排在最前：接口返回的字段稳定、时间带时区，比解析 HTML 可靠得多。
        实测扫描命中的多数平台（GitHub、MyAnimeList、Weibo…）主页上
        根本没有可抽的条目，但 GitHub 这类有公开 API。
        """
        if not await self.reserve_slots(apis.requests_for()):
            return [], ["公开接口：请求预算不足，未调用"]

        # 用 _PacedSession 包一层：apis.py 直接调 session.get，绕过了 fetch()
        # 里的同域间隔。预算已在上一步整轮预留，这里只补"别连发"。
        paced = _PacedSession(self.session, self.domain_delay)
        # `notes` 收集"某个源这次没读到"这类盲区：没有它，一次网络抖动
        # 就会让报告显示"0 条痕迹"，而用户读到的意思是"这个人没有公开痕迹"。
        api_notes: List[str] = []
        facts = await apis.collect_all(
            paced, username, timeout=self.timeout, notes=api_notes
        )

        footprints: List[Footprint] = []
        for fact in facts:
            # `text` 与 `thread_title` 不写同一个值：`to_ai_material()` 会把
            # 「版块｜原文」两栏都印出来，重复的话素材里每条都是
            # "歌单「X」｜[歌单] 歌单「X」"，白占上下文。
            # `text` 带上 kind 前缀（给 AI 看"这是什么类型"），
            # `thread_title` 留空让渲染层自然退化成只印一栏。
            footprints.append(
                Footprint(
                    site=fact.site,
                    url=fact.url,
                    kind="api",
                    posted_at=fact.date,
                    date=to_iso_date(fact.date),
                    text=f"[{fact.kind}] {fact.title}",
                    clues=content.mine_clues(f"{fact.title} {fact.detail}"),
                )
            )
        return footprints, list(api_notes)

    async def collect_from_pages(
        self,
        pages: Sequence[Tuple[str, str]],
        skip_sites: Optional[Iterable[str]] = None,
    ) -> Tuple[List[Footprint], List[str], List[str]]:
        """从"已知账号的主页"里抽内容条目。

        `pages` 是 `[(站点名, 主页地址)]`，通常来自主扫描里判定为命中的账号。
        这是**发现能力不足时唯一还能产出材料的地方**：论坛帖子需要先找到
        URL（那一步做不到，见模块开头的说明），而账号主页是扫描已经给出的。

        `skip_sites` 是已经有公开接口覆盖的站点。跳过它们有两个理由：
        省一次请求；以及**避免产生误导性的"主页结构不认识"**——
        对 GitHub 这种主页本来就没有条目列表的站，那句话是废话，
        而用户会把它读成"这里没查到东西"。

        返回 `(痕迹, 提示, 被拦的站点)`。第三个值单独返回而不是塞进提示里，
        是因为"被拦"要让用户一眼看到——它意味着"这里没查到"不等于"这里没有"。
        """
        footprints: List[Footprint] = []
        notes: List[str] = []
        blocked: List[str] = []
        skip = {str(item) for item in (skip_sites or ())}

        for site, url in pages:
            if not url or str(site) in skip:
                continue
            result = await self.fetch(url)
            if result.status == "blocked":
                blocked.append(f"{site}（{result.detail}）")
                continue
            if not result.ok:
                notes.append(f"{site}：主页未取到（{result.detail or result.status}）")
                continue

            entries = content.parse_entries(result.text, base_url=url)
            if entries is None:
                # 不是网页（JSON/二进制）。**不能说成"这个人没发过内容"**，
                # 只能说自己没读懂这个响应。
                notes.append(f"{site}：返回的不是网页，没法抽条目")
                continue
            if not entries:
                # 是网页，但上面没有"带日期的条目"。多数平台主页本来就不列内容
                # （GitHub/MyAnimeList/Twitch 都只显示资料与统计），
                # 所以这句要写成"这页没有列出内容"，不能写成"结构不认识"——
                # 后者会让用户以为工具坏了。
                notes.append(f"{site}：主页只显示资料，没有列出的内容条目")
                continue

            for entry in entries:
                footprints.append(
                    Footprint(
                        site=site,
                        url=entry.url or url,
                        kind="content",
                        posted_at=entry.date,
                        date=to_iso_date(entry.date),
                        text=entry.title,
                        thread_title=entry.title,
                        clues=content.mine_clues(entry.title),
                    )
                )
        return footprints, notes, blocked

    async def collect_from_thread(
        self, thread_url: str, username: str, author_id: str = ""
    ) -> Tuple[List[Footprint], List[str], str]:
        """从"一个帖子"里把该用户的发言尽量抽全。

        返回 `(痕迹列表, 提示, 状态)`。提示是给用户看的话术，比如
        "第 4 页被拦，未读完"——被拦必须说出来，不能静默少给。

        状态取值与 `FetchResult.status` 同义（ok / blocked / error / notfound），
        供调用方在整条种子 URL 都没读到东西时如实标记站点状态。
        **没有这个返回值时，"被拦"会退化成"这个站没有痕迹"** ——
        调用方只能看到"痕迹列表为空"，分不出是没读到还是没有。

        有 `author_id` 时用 Discuz 的"只看作者"视图。**实测该视图一次返回
        该作者在该帖的全部楼层，`page` 参数被忽略**（同一帖连打 page=1/2/3
        返回完全相同的 pid 集合）。所以这里只在"返回了新 pid"时才继续翻页，
        否则立即停止——盲目翻页只会拿到重复内容并白白消耗请求预算。
        """
        notes: List[str] = []
        footprints: List[Footprint] = []
        site = _host(thread_url)

        # 已知 uid 时直接打"只看作者"地址：一次就能拿全该作者的楼层，
        # 也省掉"先取整帖再本地过滤"的那一次请求（预算有限，省一次是一次）。
        # 万一该站在 author 视图上不返回作者名，`only_author=True` 本地兜底过滤。
        if author_id:
            # 先试 Archiver：同内容小 15 倍（实测 5.2KB vs 78KB），
            # 且不受皮肤/JS 影响。Archiver 拿不到再退回普通作者视图。
            archiver_url = archiver_author_url(thread_url, author_id)
            if archiver_url:
                got = await self.fetch(archiver_url, referer=thread_url)
                if got.ok:
                    arch_posts = content.parse_discuz_archiver(got.text)
                    if arch_posts:
                        return (
                            _posts_to_footprints(
                                site, archiver_url, arch_posts, username, only_author=True
                            ),
                            notes,
                            "ok",
                        )

            seen_pids: set = set()
            for page in range(1, self.max_pages_per_thread + 1):
                page_url = author_filter_url(thread_url, author_id, page)
                if not page_url:
                    break
                got = await self.fetch(page_url, referer=thread_url)
                if not got.ok:
                    if page == 1:
                        # 第一页就打不开：退回普通地址，别把整帖丢掉
                        break
                    notes.append(f"{site}：第 {page} 页未取到（{got.detail}）")
                    continue
                posts = content.parse_thread(got.text, site=site)
                if posts is None:
                    notes.append(f"{site}：页面结构不认识，这一页没解析出楼层")
                    break
                page_pids = {p.anchor for p in posts if p.anchor}
                # 已经见过这批 pid：说明该视图不分页，停。盲目翻页只会拿到
                # 重复内容并白白消耗预算（实测 Discuz X5.0 就是不分页的）。
                if page_pids and page_pids <= seen_pids:
                    break
                seen_pids |= page_pids
                footprints.extend(
                    _posts_to_footprints(site, page_url, posts, username, only_author=True)
                )
            if footprints:
                return footprints, notes, "ok"

        first = await self.fetch(thread_url)
        if not first.ok:
            note = f"{site}：{first.detail}（{first.status}）"
            return footprints, notes + [note], first.status
        if not looks_like_discuz(first.text):
            # 不是 Discuz（比如贴吧），交给通用解析，能抽多少算多少
            posts = content.parse_thread(first.text, site=site)
            if posts is None:
                return footprints, notes + [f"{site}：页面结构不认识，没解析出楼层"], "unparsed"
            # ⚠️ 必须严格过滤作者（2026-11-17 修）。
            #
            # 这里原来传的是 `only_author=False`（不过滤），等于把整帖**所有人**
            # 的楼层都记成"他说过的话"。实测后果：两个不同用户名在同一条帖子上
            # 拿到完全相同的内容，里面是别人的发言，甚至别人的手机号。
            # 把别人的话记成他说的，比漏记严重得多。
            picked, others = _split_by_author(posts, username)
            if posts and not picked:
                notes.append(
                    f"{site}：该帖有 {len(posts)} 层楼，但没有一层的作者名与 "
                    f"{username!r} 逐字匹配（可能是昵称与用户名不同），未记录"
                )
            return (
                _posts_to_footprints(site, thread_url, picked, username, only_author=True),
                notes,
                "ok",
            )

        # Discuz 的权限墙是 HTTP 200 + 「提示信息」页，`looks_blocked` 认不出来
        # （它认的是验证码/登录墙那类文案）。不单独判一下，这种页会被当成
        # "结构认识但没有楼层"，最终报成"他没发过言"。
        # 实测 tid=481192 就是这种：需要「N币 ≥ 0」才能访问的版块，
        # 游客连 0 都不满足，匿名永久不可读。
        if content.discuz_permission_wall(first.text) and not content.parse_thread(
            first.text, site=site
        ):
            return footprints, notes + [
                f"{site}：tid={thread_url} 在权限版块内，匿名读不到"
            ], "blocked"

        total_pages = min(max_page_from(first.text), self.max_pages_per_thread)

        # 没有 uid：只能翻页后本地过滤。
        #
        # ⚠️ `only_author` 必须是 True（2026-11-17 修）。原来传 False，
        # 而上面那行注释写的是"本地过滤"——注释与代码自相矛盾，代码是错的。
        # 后果与上面那个非 Discuz 分支一样：整帖所有人的发言被记成他的。
        dropped = 0
        unparsed_pages = 0
        for page in range(1, total_pages + 1):
            if page == 1:
                html_text, page_url = first.text, thread_url
            else:
                page_url = thread_page_url(thread_url, page)
                if not page_url:
                    break
                got = await self.fetch(page_url, referer=thread_url)
                if not got.ok:
                    notes.append(f"{site}：第 {page} 页未取到（{got.detail}）")
                    continue
                html_text = got.text
            posts = content.parse_thread(html_text, site=site)
            # `parse_thread` 返回 None = 页面结构不认识（不是"没楼层"）。
            # 2026-11-17 独立评审 P1：原来这里直接把它交给 _posts_to_footprints，
            # 得到空列表后以 "ok" 返回，最终报成"页面能打开，但里面没有这个人的发言"
            # ——把"我们没解析出来"说成了"他没有发言"。
            if posts is None:
                unparsed_pages += 1
                notes.append(f"{site}：第 {page} 页结构不认识，没解析出楼层")
                continue
            picked, others = _split_by_author(posts, username)
            dropped += len(others)
            footprints.extend(
                _posts_to_footprints(site, page_url, picked, username, only_author=True)
            )
        # 有楼层但一层都没匹配上：要说清是"过滤掉了"而不是"他没发过言"。
        if dropped and not footprints:
            notes.append(
                f"{site}：该帖共 {dropped} 层楼，没有一层的作者名与 {username!r} "
                "逐字匹配（可能是昵称与用户名不同），未记录"
            )
        # 一页都没解析出来 → 不能说 ok（ok 的含义是"页面读到了、内容也认了"）
        if unparsed_pages and not footprints:
            return footprints, notes, "unparsed"
        return footprints, notes, "ok"

    async def run(
        self,
        username: str,
        sites: Sequence[str] = (),
        seed_urls: Sequence[str] = (),
        pages: Sequence[Tuple[str, str]] = (),
        concurrency: int = 4,
    ) -> Dict[str, Any]:
        """跑一轮脚迹检索。

        三个输入对应三种"已知信息"，缺哪个就少一块产出：

        `sites`     要探的 Discuz 站域名 —— 能拿到资料页与时间线
        `pages`     已确认账号的主页 [(站点名, 地址)] —— 抽主页上列出的内容条目
        `seed_urls` 用户自己给的帖子地址 —— 抽该用户在此帖的全部发言

        `pages` 和 `seed_urls` 的区别是**发现难度**：
            pages 是主扫描已经给出的（账号存在 → 主页地址已知），不需要额外发现
            seed_urls 需要人自己找到（论坛的"发帖列表"被服务端禁用，
            搜索引擎结果页被 robots 禁止，工具无法自动发现）
        """
        reports: List[SiteReport] = []
        notes: List[str] = []

        # 第一优先：公开接口。字段稳定、时间准确，且不需要"发现"。
        # 排在最前是因为它对时间线推断的贡献最大——先把它拿到，
        # 后面就算预算被页面请求吃掉，核心材料也已经在手上。
        api_footprints, api_notes = await self.collect_from_apis(username)
        notes.extend(api_notes)
        if api_footprints:
            by_site: Dict[str, SiteReport] = {}
            for item in api_footprints:
                bucket = by_site.get(item.site)
                if bucket is None:
                    bucket = SiteReport(site=item.site, status="ok", detail="来源：公开接口")
                    by_site[item.site] = bucket
                    reports.append(bucket)
                bucket.footprints.append(item)

        if sites:
            semaphore = asyncio.Semaphore(max(1, concurrency))

            async def one(host: str) -> SiteReport:
                async with semaphore:
                    try:
                        return await self.probe_discuz_site(host, username)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("站点 %s 检索失败：%s", host, exc)
                        return SiteReport(site=host, status="error", detail=str(exc)[:160])

            # 注意：必须**追加**到已有 reports，不能重新赋值。
            # `reports = await asyncio.gather(...)` 会把前面 API 采集到的
            # 站点报告整个丢掉——实测症状是"接口明明取到 32 条，输出里一条也没有"。
            reports.extend(await asyncio.gather(*[one(h) for h in sites]))

        # 资料页里发现的站内帖子（签名里自己贴的地址）：继续深挖。
        #
        # 顺序很重要：必须排在站点探测**之后**（要先有 profile 才知道链接），
        # 但排在种子 URL 之前——它的线索是工具自己发现的，不需要用户输入，
        # 所以优先于"人给的地址"。实测这一步是论坛那一路从 1 条变 17 条的关键。
        if reports and self.max_threads_per_site:
            link_footprints, link_notes = await self.collect_from_profile_links(
                username, reports
            )
            notes.extend(link_notes)
            by_site = {report.site: report for report in reports}
            for item in link_footprints:
                bucket = by_site.get(item.site)
                if bucket is not None:
                    bucket.footprints.append(item)

        # 已知账号的主页：抽内容条目。这是"没有种子 URL 时"唯一的产出源，
        # 因此排在种子 URL 之前——先把不要人肉找的东西捞干净。
        if pages:
            covered = {report.site for report in reports}
            page_footprints, page_notes, page_blocked = await self.collect_from_pages(
                pages, skip_sites=covered
            )
            notes.extend(page_notes)
            by_host = {report.site: report for report in reports}
            for item in page_footprints:
                bucket = by_host.get(item.site)
                if bucket is None:
                    bucket = SiteReport(site=item.site, status="ok", detail="来源：已确认账号主页")
                    by_host[item.site] = bucket
                    reports.append(bucket)
                bucket.footprints.append(item)
            for entry in page_blocked:
                host = entry.split("（", 1)[0]
                bucket = by_host.get(host)
                if bucket is None:
                    reports.append(SiteReport(site=host, status="blocked", detail=entry))
                else:
                    bucket.status = "blocked"
                    bucket.detail = entry

        # 种子 URL：逐条取回，用已有的 uid（如果同站有资料页）走"只看作者"
        for raw_url in seed_urls:
            url = (raw_url or "").strip()
            if not url:
                continue
            host = _host(url)
            author_id = ""
            for report in reports:
                if report.site == host and report.profile and report.profile.uid:
                    author_id = report.profile.uid
                    break
            posts, per_notes, seed_status = await self.collect_from_thread(
                url, username, author_id
            )
            notes.extend(per_notes)

            bucket = None
            for report in reports:
                if report.site == host:
                    bucket = report
                    break
            if bucket is None:
                # 只由种子 URL 引出的站点（清单里没探过它）。
                # 状态要如实带上：被拦的种子帖如果记成 "ok"，
                # 汇总层永远不会把它列进盲区，用户看到的就是"这个站没有痕迹"。
                bucket = SiteReport(site=host, status=seed_status, detail=f"来源：{url}")
                reports.append(bucket)
            elif seed_status in ("blocked", "error") and not posts:
                # 清单里探过、但种子帖没读到：把失败原因挂上，
                # 但**不改站点自身的判定**（资料页读到了就是读到了）。
                notes.append(f"{host}：种子帖 {url} —— {seed_status}")
            bucket.footprints.extend(posts)

        return {
            "username": username,
            "reports": reports,
            "notes": notes,
            "requests": self.requests_made,
            "budget": self.max_requests,
            "budget_exhausted": self.budget_exhausted,
        }


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _decode(raw: bytes, response: Any) -> str:
    """按响应头猜编码；猜不到按 UTF-8 容错解。

    很多中文老站声明 GBK。不处理的话，抽出来的用户名是乱码，
    `same_author` 比对必然失败——表现为"这个人在这站一条都没发过言"，
    而实际上是编码问题。
    """
    charset = ""
    try:
        charset = (response.charset or "").lower()
    except Exception:  # noqa: BLE001
        charset = ""
    for candidate in (charset, "utf-8", "gb18030"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _profile_footprint(
    site: str, url: str, profile: Profile, username: str
) -> Footprint:
    """把资料页上的几个时间点变成一条可核对的痕迹。

    为什么把资料也当痕迹：`注册时间 / 最后访问 / 上次发表时间` 这三个点
    划出的区间，比单看任何一条帖子都更能定位"这个人哪几年在网上活跃"。
    但那属于推断，这里只把原文摆出来。
    """
    parts = []
    for label, value in (
        ("注册时间", profile.registered_at),
        ("最后访问", profile.last_visit),
        ("上次发表时间", profile.last_post_at),
        ("在线时间", profile.online_hours),
        ("回帖数", profile.post_count),
        ("主题数", profile.thread_count),
    ):
        if value:
            parts.append(f"{label}：{value}")

    text = "；".join(parts)
    return Footprint(
        site=site,
        url=url,
        kind="profile",
        author=profile.name or username,
        posted_at=profile.registered_at,
        date=to_iso_date(profile.registered_at),
        text=text,
        anchor=f"uid{profile.uid}" if profile.uid else "",
        clues=content.mine_clues(profile.signature),
    )


_STATUS_REASON = {
    "blocked": "帖子在权限版块里，匿名读不到（不是没有内容）",
    "notfound": "帖子已被删除或地址失效",
    "error": "网络请求失败",
    "unparsed": "页面结构不认识，没解析出楼层",
    "budget": "请求预算用完了",
    "ok": "页面能打开，但里面没有这个人的发言",
}


def _status_reason(status: str) -> str:
    """把内部状态翻成用户能看懂的原因。

    为什么要这一层：`blocked` 这种词对用户没意义，而"帖子在权限版块里，
    匿名读不到"能直接告诉他该怎么办（换了登录态也没用，因为确认过
    游客连 0 都算不满足）。
    """
    return _STATUS_REASON.get(status, f"状态 {status}")


def _split_by_author(
    posts: Optional[Iterable[Post]], username: str
) -> Tuple[List[Post], List[Post]]:
    """按作者把楼层分成 `(匹配的, 没匹配的)`。

    为什么要这个而不是直接在 `_posts_to_footprints` 里过滤：调用方需要知道
    "被过滤掉了多少层"，才能在**过滤后结果为空**时说出"是过滤掉的"而不是
    "他没发过言"——后者是把"我们没认出来"说成"没有"，属于最坏的一类错。

    作者名解析不出来时归入"没匹配"：没有依据说这层是他发的，
    就不能记在他名下。
    """
    matched: List[Post] = []
    others: List[Post] = []
    for post in posts or ():
        if post.is_empty():
            continue
        if content.same_author(post.author, username):
            matched.append(post)
        else:
            others.append(post)
    return matched, others


def _posts_to_footprints(
    site: str,
    url: str,
    posts: Optional[Iterable[Post]],
    username: str,
    only_author: bool = False,
) -> List[Footprint]:
    """把解析出的楼层转成痕迹。

    `only_author=True` 时**必须**作者名逐字匹配才记。这里刻意不写
    "作者名为空就放过"：作者名解析不出来时，我们没有任何依据说这层楼
    是他发的——而"把别人的话记成他说的"比漏记严重得多。
    """
    found: List[Footprint] = []
    for post in posts or ():
        if post.is_empty():
            continue
        if only_author and not content.same_author(post.author, username):
            continue
        found.append(
            Footprint(
                site=site,
                url=url,
                kind="post",
                author=post.author,
                posted_at=post.posted_at,
                date=to_iso_date(post.posted_at),
                text=post.text,
                forum=post.forum,
                thread_title=post.thread_title,
                anchor=post.anchor,
                clues=content.mine_clues(post.text),
            )
        )
    return found


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def summarize(result: Dict[str, Any]) -> Dict[str, Any]:
    """把检索结果整理成可直接给报告/AI 用的形态。

    这里**只统计与排序，不做推断**。比如"活跃区间"只报最早/最晚两个日期，
    不写"这说明他当时在上初中"——那是 AI 分析该做的事。

    去重按"痕迹身份"来做：**有楼层锚点（pid）时以锚点为准，没有时退化成
    (站点, 时间, 正文前缀) 三元组**。为什么要分两种：同一帖的不同页可能
    重复返回同一楼（Discuz 的"只看作者"视图就不分页），而宽松的三元组
    会把"同一分钟内两条不同的话"错并成一条。锚点才是真身份。

    ## 入参必须带 `reports`（2026-11-17 加的护栏）

    本函数读的是 `report.footprints`（逐站明细），不是扁平的 `footprints`。
    而 `search_footprints()` 返回的**已经是**摘要——它内部调过本函数。
    所以 `summarize(search_footprints(...))` 是**多调了一层**，
    旧行为是静默返回 `total=0`：用户会看到"这个人没有公开痕迹"，
    而实际拿到的 46 条就在入参里躺着。这种"把有说成没有"正是本项目
    最不能犯的错，所以改成显式报错，而不是继续静默。
    """
    if "reports" not in result and any(
        key in result for key in ("footprints", "total", "by_site")
    ):
        raise ValueError(
            "summarize() 收到的是**已经摘要过**的结果（含 footprints/total 但没有 reports）。"
            "search_footprints() 的返回值本身就是摘要，不要再套一层 summarize()。"
        )

    footprints: List[Footprint] = []
    blocked: List[str] = []
    absent: List[str] = []
    unparsed: List[str] = []
    seen: set = set()

    def identity(item: Footprint) -> tuple:
        if item.anchor:
            return (item.site, item.anchor, item.kind)
        return (item.site, item.date, (item.text or "")[:80], item.kind)

    for report in result.get("reports", []):
        if report.status == "absent":
            absent.append(report.site)
        elif report.status in ("blocked", "error", "budget"):
            blocked.append(f"{report.site}（{report.detail or report.status}）")
        elif report.status in ("unparsed", "not_discuz"):
            unparsed.append(f"{report.site}（{report.detail}）")

        for item in report.footprints:
            key = identity(item)
            if key in seen:
                continue
            seen.add(key)
            footprints.append(item)

    dated = [f for f in footprints if f.date]
    dates = sorted(f.date for f in dated)

    by_site: Dict[str, int] = {}
    for item in footprints:
        by_site[item.site] = by_site.get(item.site, 0) + 1

    forums = sorted({f.forum for f in footprints if f.forum})
    places: List[str] = []
    contacts: List[str] = []
    for item in footprints:
        places.extend(item.clues.get("places", []))
        contacts.extend(item.clues.get("contacts", []))
        contacts.extend(item.clues.get("qq", []))

    def uniq(values: Iterable[str]) -> List[str]:
        seen, out = set(), []
        for value in values:
            if value and value not in seen:
                seen.add(value)
                out.append(value)
        return out

    # 同名跨站提示。
    #
    # Discourse / Discord / 各类论坛的"用户名存在"只说明**这个站上有这个
    # 用户名**，不说明是同一个人。这一点在单站输出里不明显（一个站一个账号），
    # 但一次跑 9 个 Discourse 站时会冒出一排"同名的账号"——用户很容易
    # 把它们当成一个人在多处活动，而它们可能只是重名。
    #
    # 所以这里显式数一下：有多个站命中「账号」类痕迹时，在报告里标明
    # "这些账号是否同一人需自行确认"。工具不下结论（与项目一贯立场一致），
    # 但不能让"没说"变成"默认是同一人"。
    account_sites = sorted(
        {f.site for f in footprints if f.kind in ("api",) and "账号" in (f.text or "")}
    )
    same_name_note = ""
    if len(account_sites) >= 3:
        same_name_note = (
            f"{len(account_sites)} 个站点都有这个用户名（{'、'.join(account_sites[:5])}"
            + ("…" if len(account_sites) > 5 else "")
            + "）。**它们是否属于同一个人需要自行确认**——"
            "通用用户名在不同站点被不同人注册是常态。"
        )

    return {
        "username": result.get("username", ""),
        "total": len(footprints),
        "dated": len(dated),
        "range": [dates[0], dates[-1]] if dates else [],
        "by_site": by_site,
        "forums": forums,
        "places": uniq(places),
        "contacts": uniq(contacts),
        "blocked": blocked,
        "absent": absent,
        "unparsed": unparsed,
        # 跨站同名的显式提示（空串表示不适用）
        "same_name_note": same_name_note,
        # 逐页的提示（某页被拦、结构不认识、种子帖失败…）。
        # 放在 summarize 里产出而不是只在 search_footprints 里补：
        # 直接调 summarize 的调用方（测试、将来的报告层）也要能拿到盲区，
        # 否则"被拦"又变成静默消失。
        "notes": list(result.get("notes") or []),
        "requests": result.get("requests", 0),
        "budget": result.get("budget", 0),
        "budget_exhausted": result.get("budget_exhausted", False),
        "footprints": [f.to_dict() for f in sorted(footprints, key=lambda x: x.date or "9999")],
    }


def render(summary: Dict[str, Any], max_items: int = 40) -> str:
    """渲染成给人看的文本块。"""
    if not summary:
        return ""

    lines: List[str] = []
    username = summary.get("username") or ""
    lines.append("公开痕迹检索" + (f"：{username}" if username else ""))
    lines.append("─" * 40)

    total = summary.get("total", 0)
    if not total:
        lines.append("  未检索到公开痕迹。")
    else:
        rng = summary.get("range") or []
        span = f"，时间跨度 {rng[0]} ～ {rng[1]}" if len(rng) == 2 else ""
        lines.append(f"  共 {total} 条痕迹{span}")
        sites = summary.get("by_site") or {}
        if sites:
            lines.append("")
            for site, count in sorted(sites.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {site:<24} {count} 条")

    forums = summary.get("forums") or []
    if forums:
        lines.append("")
        lines.append("  出现过的版块/吧")
        for forum in forums[:12]:
            lines.append(f"    · {forum}")

    items = summary.get("footprints") or []
    if items:
        lines.append("")
        lines.append("  时间线")
        for item in items[:max_items]:
            date = item.get("date") or "----------"
            text = (item.get("text") or "").replace("\n", " ")[:56]
            where = item.get("forum") or item.get("site") or ""
            lines.append(f"    {date}  [{where[:14]:<14}] {text}")
        if len(items) > max_items:
            lines.append(f"    …… 另有 {len(items) - max_items} 条")

    places = summary.get("places") or []
    contacts = summary.get("contacts") or []
    if places or contacts:
        lines.append("")
        lines.append("  出现的线索（原文里的字面值，未做判断）")
        if places:
            lines.append(f"    · 地名：{'、'.join(places[:10])}")
        if contacts:
            lines.append(f"    · 联系方式：{'、'.join(contacts[:5])}")

    # 跨站同名提示：必须排在"没读到"之前，因为它影响的是**已经读到的**
    # 那些命中该怎么解读——用户看到一排同名账号，第一反应就是"他到处都在"。
    same_name = summary.get("same_name_note") or ""
    if same_name:
        lines.append("")
        lines.append("  关于同名的提醒")
        lines.append(f"    · {same_name}")

    # 盲区必须显式出现：只报命中不提盲区，用户会以为"没查到的就是没有"
    blocked = summary.get("blocked") or []
    unparsed = summary.get("unparsed") or []
    notes = summary.get("notes") or []
    if blocked or unparsed or notes:
        lines.append("")
        lines.append("  本次没读到")
        for item in blocked[:10]:
            lines.append(f"    · {item} —— 被拦，不代表没有")
        for item in unparsed[:10]:
            lines.append(f"    · {item}")
        for item in notes[:10]:
            lines.append(f"    · {item}")

    if summary.get("budget_exhausted"):
        lines.append("")
        lines.append(f"  注意：请求数已达到单次上限（{summary.get('budget')} 次），结果可能不完整。")

    return "\n".join(lines)


def to_ai_material(summary: Dict[str, Any], max_chars: int = 6000) -> str:
    """把检索结果压成一段"给 AI 分析的原始素材"。

    与 `render()` 的区别是**目的不同**：
        - `render()` 给人看：要挑重点、要短、排版清楚
        - 这个给 AI 看：要**保留细节和原话**，因为它要靠这些字面信息
          （"放学回来"、"翠园"、时间跨度）去推断，压缩过头就没法推了

    所以这里不做摘要、不挑重点，只做两件事：
      1. 按时间排序，让"哪几年活跃"一眼可见
      2. 截断到字数上限（防止把上下文撑爆）

    明确不做的：不写"这说明什么"。推断留给分析方，材料保持中立。

    **没有痕迹时也要输出盲区**：如果这次全被拦，返回空串会让 AI 得出
    "这个人没有公开痕迹"——而事实是"我们没读到"。两者对用户的意义相反。
    只有在既没有痕迹、也没有任何盲区信息时才返回空串。
    """
    items = summary.get("footprints") or []
    blocked = summary.get("blocked") or []
    notes = summary.get("notes") or []
    unparsed = summary.get("unparsed") or []
    # `unparsed` 必须参与"有没有盲区"的判断。
    #
    # 2026-11-17 独立评审 P1：原来只看 items/blocked/notes，
    # 于是"所有站点都取到了页面但一页都没解析出来"（unparsed 有值、其余全空）
    # 会返回空串 → AI 得到的是"此人无公开痕迹"，而事实是"我们没解析出来"。
    # `render()` 有显示这些，两条通道口径不一致，AI 通道的盲区直接消失。
    if not items and not blocked and not notes and not unparsed:
        return ""

    lines: List[str] = []
    rng = summary.get("range") or []
    lines.append(f"用户名：{summary.get('username', '')}")
    if items:
        if len(rng) == 2:
            lines.append(
                f"痕迹时间跨度：{rng[0]} ～ {rng[1]}（共 {summary.get('total', 0)} 条）"
            )
        else:
            lines.append(f"痕迹数：{summary.get('total', 0)}")

        forums = summary.get("forums") or []
        if forums:
            lines.append(f"出现过的版块/吧：{'、'.join(forums[:20])}")
        places = summary.get("places") or []
        if places:
            lines.append(f"正文里出现的地名：{'、'.join(places[:20])}")

        # 跨站同名提醒要进 AI 素材，否则 AI 会把"多个站有同名账号"
        # 直接写成"这个人在 N 个平台活动"，而那是没有依据的推断。
        same_name = summary.get("same_name_note") or ""
        if same_name:
            lines.append(f"注意（同名，非同一人确认）：{same_name}")

        lines.append("")
        lines.append("逐条痕迹（时间｜站点｜版块｜原文）：")

        used = 0
        written = 0
        for item in items:
            text = (item.get("text") or "").replace("\n", " ").strip()
            row = (
                f"{item.get('date') or '无日期'}｜{item.get('site', '')}"
                f"｜{item.get('forum') or item.get('thread_title') or ''}｜{text}"
            )
            if used + len(row) > max_chars:
                lines.append(f"……（其余 {len(items) - written} 条因长度限制省略）")
                break
            lines.append(row)
            used += len(row)
            written += 1
    else:
        lines.append("本次没有取到任何痕迹。")

    if blocked or notes or unparsed:
        lines.append("")
        lines.append(
            "以下站点本次被拦、读取失败或结构不认识，其内容未进入上面的材料（不代表没有）："
            + "；".join((blocked + notes + unparsed)[:10])
        )
    return "\n".join(lines)


async def search_footprints(
    username: str,
    session: Any,
    sites: Optional[Sequence[str]] = None,
    seed_urls: Sequence[str] = (),
    pages: Sequence[Tuple[str, str]] = (),
    max_requests: int = DEFAULT_MAX_REQUESTS,
    domain_delay: float = DEFAULT_DOMAIN_DELAY,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    """一步跑完检索 + 汇总。CLI / Web 都走这个入口，保证两边口径一致。

    `sites` 不给就用 `forums_cn.json` 里的清单。
    """
    target_sites = list(sites) if sites else load_forums()
    finder = FootprintFinder(
        session,
        max_requests=max_requests,
        domain_delay=domain_delay,
        timeout=timeout,
    )
    raw = await finder.run(
        username,
        sites=target_sites,
        seed_urls=list(seed_urls or ()),
        pages=list(pages or ()),
    )
    summary = summarize(raw)
    summary["sites_probed"] = target_sites
    return summary


def run_sync(username: str, **kwargs: Any) -> Dict[str, Any]:
    """同步封装，给不方便 await 的调用方（如 CLI 主流程）用。"""
    import aiohttp

    async def main() -> Dict[str, Any]:
        # trust_env=False：检索走干净会话，不继承环境变量里的代理设置，
        # 与其他执行器（见 executors/search.py）保持同一口径。
        async with aiohttp.ClientSession(trust_env=False) as session:
            return await search_footprints(username, session, **kwargs)

    return asyncio.run(main())
