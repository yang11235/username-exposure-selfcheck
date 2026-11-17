"""公开接口式的痕迹采集（比解析 HTML 可靠得多）。

## 为什么需要这一层

`content.py` 解析的是 HTML 列表页，能覆盖博客/论坛这类"页面上列着文章"的形态。
但实测下来，扫描命中的多数平台主页**页面上根本没有条目**——GitHub 的个人主页
只显示 pinned 仓库，MyAnimeList 的主页是一堆统计图表，Weibo 的主页是登录墙。

而这些平台往往有**公开 API**，返回带精确时间戳的 JSON：

    https://api.github.com/users/<name>          创建时间
    https://api.github.com/users/<name>/repos    仓库创建/推送时间
    https://api.github.com/users/<name>/events   公开动态（精确到分钟）
    https://api.github.com/search/issues?q=author:<name>
                                                  他在哪些仓库提过 issue/PR（正文来源）

对"反推时间线"这件事，JSON 比 HTML 强一个量级：字段稳定、时间带时区、
不受前端改版影响。所以这一层不是兜底，是**首选**——能从 API 拿就不解析页面。

## 中文平台的两个入口（2026-11-16 实测补入）

中文平台的主页地址大多不能用用户名拼出来（B 站是数字 uid、掘金是 24 位 id），
但**站内搜索接口**能按昵称拿到 uid，再用 uid 取内容列表。这条链不需要
搜索引擎，所以不碰任何 SERP 的 robots：

    网易云  搜索昵称 → uid → 歌单列表（每个歌单有 createTime）
    掘金    搜索昵称 → uid → 文章列表 / 沸点列表（ctime）

网易云的歌单创建时间是**极强的时间线信号**：一个 2013 年建的歌单说明
2013 年就在用这个平台。掘金同理（文章 ctime）。

## 合规

只调**公开、无需 token** 的接口，只取平台自己公开展示的字段。
`api.github.com` 的 robots 是 404（没有 robots 文件），说明站点未对
自动访问设限；GitHub 的 API 文档也明确允许匿名读取公开数据（有速率限制）。
速率限制本身就是约束：未认证每小时 60 次，一次自查远远用不到。

新增的中文接口逐个核对过 robots（`*` 段，按 RFC 9309 最长匹配）：

    music.163.com   /api/search/get/web /api/user/playlist /api/v1/user/detail  允许
    blog.csdn.net   /community/home-api/v1/get-business-list                    允许
    api.juejin.cn   robots 是 404（未设限）
    api.bilibili.com /x/web-interface/card                                      禁止 → 不主动新增请求

**不做的**：不取私有仓库、不取邮箱（那是需要 token 且用户没同意的数据）、
不构造需要签名的请求。
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ApiFact:
    """一条从公开接口取到的事实。

    `kind` 是给人看的分类（账号/仓库/动态），`date` 统一到 `YYYY-MM-DD`。
    """

    site: str
    kind: str
    title: str
    date: str = ""
    url: str = ""
    detail: str = ""


def _iso_date(value: str) -> str:
    """`2024-01-04T08:26:20Z` → `2024-01-04`。取不到返回空串。"""
    text = (value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        candidate = text[:10]
        try:
            year, month, day = (int(part) for part in candidate.split("-"))
        except ValueError:
            return ""
        if 1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
            return candidate
    return ""


def _epoch_ms_date(value: Any) -> str:
    """毫秒/秒时间戳 → `YYYY-MM-DD`。取不到或明显不合理返回空串。

    网易云的 `createTime` 是**毫秒**时间戳（13 位）。做过两道校验，
    因为实测遇到过 `createTime: -1`（老账号接口不再返回该字段）：

        1. 必须是正数（`-1` 会被 `strftime` 悄悄算成 1969 年）
        2. 年份必须落在 1990..2100（毫秒/秒传错会偏到 1970 或 5 万年之后）

    不做这两道校验的后果很具体：报告里会出现「1969-12-31 创建的歌单」，
    而用户会以为那是真的。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    # 13 位是毫秒，10 位是秒；按量级判断，不猜
    seconds = number / 1000.0 if number > 1e11 else number
    try:
        import datetime

        moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    if not 1990 <= moment.year <= 2100:
        return ""
    return moment.strftime("%Y-%m-%d")


async def collect_github(
    session: Any,
    username: str,
    timeout: float = 20.0,
    notes: Optional[List[str]] = None,
) -> List[ApiFact]:
    """从一个 GitHub 账号取公开时间线。

    取三样，都是匿名可读的：
        账号本身   created_at / updated_at
        仓库列表   created_at / pushed_at（最多 30 个，按最近推送排序）
        公开动态   created_at（最多 30 条，精确到分钟）

    为什么这三样：`created_at` 回答"什么时候开始在这个圈子出现"，
    仓库与动态回答"哪个时期在做什么"。对定位人生阶段，这比任何一条
    帖子标题都实在。
    """
    facts: List[ApiFact] = []
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "maigret-cn-selfcheck",
    }

    async def get_json(url: str, params: Optional[Dict[str, Any]] = None):
        try:
            async with session.get(
                url, params=params, headers=headers, timeout=timeout
            ) as response:
                if response.status not in (200, 404):
                    # 403（限额）/ 5xx 都是"没读到"，不是"没有"
                    _note(notes, f"GitHub 接口返回 HTTP {response.status}，该部分未读到")
                    return None
                if response.status == 404:
                    # 404 是明确的"这个用户名不存在"，不是失败
                    return None
                return await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 — 单次失败不该中断整轮
            logger.debug("GitHub 接口 %s 失败：%s", url, exc)
            _note(notes, f"GitHub 接口请求失败（{type(exc).__name__}），该部分未读到")
            return None

    profile = await get_json(f"https://api.github.com/users/{username}")
    if not isinstance(profile, dict) or profile.get("message"):
        return facts

    # 返回的必须**就是**这个用户名。
    #
    # 2026-11-17 独立评审 P2#18：GitHub 对被重命名/被接管的旧用户名会 301 到
    # 新账号，`/users/<旧名>` 于是返回**另一个账号**的 created_at、仓库、动态。
    # Discourse 层已经做了这个比对，这里漏了——同样的错在别处修过就该在每处都修。
    # 比对一次很便宜；不比对，整份 GitHub 数据都可能是别人的。
    if str(profile.get("login") or "").strip().lower() != username.strip().lower():
        logger.debug(
            "GitHub 返回的 login=%r 与查询名 %r 不符，丢弃", profile.get("login"), username
        )
        return facts

    site = "GitHub"
    account_url = profile.get("html_url") or f"https://github.com/{username}"

    parts = []
    for label, key in (
        ("注册", "created_at"),
        ("最近更新", "updated_at"),
        ("公开仓库", "public_repos"),
        ("关注者", "followers"),
    ):
        value = profile.get(key)
        if value not in (None, ""):
            parts.append(f"{label}：{value}")

    facts.append(
        ApiFact(
            site=site,
            kind="账号",
            title="；".join(parts) or "账号资料",
            date=_iso_date(profile.get("created_at", "")),
            url=account_url,
        )
    )

    # 名称/简介/地区是平台公开展示的字段，有就记（没有就不记，不猜）
    for label, key in (("姓名", "name"), ("简介", "bio"), ("地区", "location")):
        value = profile.get(key)
        if value:
            facts.append(
                ApiFact(
                    site=site,
                    kind=label,
                    title=str(value),
                    date=_iso_date(profile.get("updated_at", "")),
                    url=account_url,
                )
            )

    repos = await get_json(
        f"https://api.github.com/users/{username}/repos",
        params={"sort": "created", "direction": "desc", "per_page": 30},
    )
    if isinstance(repos, list):
        for repo in repos:
            if not isinstance(repo, dict) or repo.get("fork"):
                continue
            facts.append(
                ApiFact(
                    site=site,
                    kind="仓库",
                    title=str(repo.get("name") or ""),
                    date=_iso_date(repo.get("created_at", "")),
                    url=str(repo.get("html_url") or ""),
                    detail=str(repo.get("description") or "")[:160],
                )
            )

    events = await get_json(
        f"https://api.github.com/users/{username}/events/public",
        params={"per_page": 30},
    )
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            repo = (event.get("repo") or {}).get("name") or ""
            facts.append(
                ApiFact(
                    site=site,
                    kind="动态",
                    title=f"{event.get('type', '')} {repo}".strip(),
                    date=_iso_date(event.get("created_at", "")),
                    url=f"https://github.com/{repo}" if repo else account_url,
                )
            )

    # 第四样：他在哪些仓库提过 issue / PR —— 这是唯一"说过什么"的正文来源。
    #
    # 为什么单独加这一条：上面三样都只说明"他有个账号、建过什么仓库"，
    # 而 `author:` 搜索返回的是**他自己写的标题**，带创建时间。实测
    # `author:yang11235` 拿到 7 条（issue + PR），能看出他在给哪个项目
    # 提什么需求——这是画像里最实在的一段。
    #
    # 搜索接口的匿名配额是 10 次/小时（core 是 60 次/小时），
    # 一次自查用 1 次，够用。所以这里不做分页，只取第一页。
    issues = await get_json(
        "https://api.github.com/search/issues",
        params={"q": f"author:{username}", "sort": "created", "order": "asc", "per_page": 30},
    )
    if isinstance(issues, dict):
        for issue in issues.get("items") or []:
            if not isinstance(issue, dict):
                continue
            # pull_request 存在说明是 PR，分开标出来：提 PR 和提 issue
            # 反映的参与程度不同，混成一个桶就看不出他在做什么。
            is_pr = "pull_request" in issue
            title = str(issue.get("title") or "").strip()
            issue_url = str(issue.get("html_url") or "")
            repo_name = ""
            match = re.search(r"github\.com/([^/]+/[^/]+)/", issue_url)
            if match:
                repo_name = match.group(1)
            facts.append(
                ApiFact(
                    site=site,
                    kind="PR" if is_pr else "议题",
                    title=title + (f"（{repo_name}）" if repo_name else ""),
                    date=_iso_date(issue.get("created_at", "")),
                    url=issue_url,
                    detail=f"状态：{issue.get('state', '')}",
                )
            )

    # 第五样：他 star 了什么 —— 唯一能反映"感兴趣但没参与"的信号。
    #
    # 为什么值得单独取：仓库说明他**写了什么**，议题说明他**说了什么**，
    # 而 star 说明他**关注什么**。三者回答三个不同的问题。
    # 实测 `yang11235` star 了 maigret / koreader / weread.koplugin，
    # 与他的议题内容完全对上——这种"兴趣与行为一致"的信号，
    # 比单看议题更可靠。
    #
    # 不取 `starred_at`：那个字段要么带 `Accept: application/vnd.github.star+json`
    # 头才有，要么只返回裸仓库。为了不增加请求头差异带来的不确定性，
    # 这里用仓库本身的 `pushed_at` 作为时间参考，并明确标成"仓库最近推送"，
    # **不冒充"他 star 的时间"**。
    starred = await get_json(
        f"https://api.github.com/users/{username}/starred",
        params={"per_page": 30, "sort": "created", "direction": "desc"},
    )
    if isinstance(starred, list):
        for repo in starred:
            if not isinstance(repo, dict):
                continue
            full_name = str(repo.get("full_name") or "")
            if not full_name:
                continue
            language = str(repo.get("language") or "")
            facts.append(
                ApiFact(
                    site=site,
                    kind="star",
                    title=full_name + (f"（{language}）" if language else ""),
                    date=_iso_date(repo.get("pushed_at", "")),
                    url=str(repo.get("html_url") or ""),
                    detail="关注的项目（时间取该项目最近推送，不是 star 的时间）",
                )
            )

    return facts


async def collect_all(
    session: Any,
    username: str,
    timeout: float = 20.0,
    notes: Optional[List[str]] = None,
) -> List[ApiFact]:
    """跑所有已知的公开接口采集器。

    加新平台的门槛是三条：接口匿名可读、返回带时间戳的结构化字段、
    robots 不禁止。不满足就不加——宁可少一个源，也不要一个会误导的源。

    现在的源，以及合规依据：

        GitHub     api.github.com（robots 404，未设限；文档允许匿名读公开数据）
        NetEase    music.163.com（`*` 段允许 /api/search 与 /api/user/playlist）
        Discourse  `topics/created-by/<u>.json` 在 9 个实测站全部允许；
                   `/u/<u>.json` 有 5 个站被 `Disallow: /u/` 禁掉 →
                   **只对允许的站发那一条**，见 `collect_discourse`

    刻意没加的，理由都写在这里，免得下一个人重复试：

        掘金        只有文章搜索，没有用户搜索。实测 `key_word=yang11235`
                    返回 19 条，**逐字含该用户名的 0 条**——全是分词把
                    "yang" 与 "11235" 拆开后的噪音。接进来只会制造噪声。
                    主页 `/user/yang11235` 返回的是 SPA 空壳（无 title、无内容）。
        B 站        `api.bilibili.com/robots.txt` 的 `*` 段禁止
                    `/x/web-interface/card`。`sites_cn.json` 里那条规则是
                    判定层在用（历史包袱），但**采集层不该再往上加请求**。
        Gitee       `/api/v5/*` 被 robots 明令禁止。
        博客园 RSS  `feed.cnblogs.com` 的 robots 是 `Disallow: /`。
        V2EX        `api.v2ex.com` 与 `www.v2ex.com` 在本机均不可达（连不上）。
        GitLab      `gitlab.com/robots.txt` 的 `*` 段禁 `/api/v4/users`。
        开源中国    `my.oschina.net/<u>` 返回 3245 字节的站点壳（title 是
                    "OSCHINA - 开源 × AI"，不是用户页），抽不出内容。
        腾讯云社区  `/developer/user/<u>` 302 跳转，跟随后仍是空壳。
        简书/知乎/微博/小红书  需登录或 robots 禁。
    """
    facts: List[ApiFact] = []
    for collector in _collectors():
        try:
            facts.extend(
                await collector(session, username, timeout=timeout, notes=notes)
            )
        except Exception as exc:  # noqa: BLE001 — 单个源失败不该中断整轮
            logger.debug("%s 采集失败：%s", collector.__name__, exc)
            # 失败必须说出去（2026-11-17 独立评审："单一数据源失败无声"）。
            #
            # 原来这里只有一行 debug 日志，调用方拿到的是空 facts——
            # 报告里看不出"GitHub 这次没读到"，用户会以为"他在 GitHub 上没有痕迹"。
            # 实测踩过：一次瞬时网络故障让整轮返回 0 条，而报告与 AI 素材
            # **一个字都没提"没读到"**，看起来就是"这个人没有公开痕迹"。
            if notes is not None:
                notes.append(
                    f"公开接口 {collector.__name__} 采集失败（{type(exc).__name__}），"
                    "该来源本次未读到"
                )
    return facts


def _note(notes: Optional[List[str]], message: str) -> None:
    """记一条"没读到"的提示。传 None 就是不要提示（保持向后兼容）。

    为什么要单独一个函数：采集器内部的失败路径很多（每个 try/except 一处），
    每处都写 `if notes is not None: notes.append(...)` 太啰嗦，
    而漏掉一处就等于那条路径继续静默。
    """
    if notes is not None:
        notes.append(message)


def _collectors():
    """采集器清单。**延迟到调用时再引用**，因为后两个在文件后半段。

    按"信息密度"排序，不是按字母序：预算紧张时先被截断的是后面的源。
    """
    return (
        collect_github,     # 时间线最密：账号 + 仓库 + 动态 + 议题/PR + star
        collect_discourse,  # 主题标题 + 注册/最后发帖时间，多站一次覆盖
        collect_netease,    # 歌单创建时间，跨度常常十几年
    )


# ---------------------------------------------------------------------------
# 中文平台：按昵称搜索 → 拿 uid → 取内容列表
# ---------------------------------------------------------------------------
# 为什么走"先搜索再取内容"两步，而不是直接拼主页地址：
#   中文平台的个人主页地址普遍不能用用户名拼出来（网易云是数字 uid，
#   掘金是 24 位 id）。站内搜索是唯一能"从用户名换到 uid"的公开入口，
#   而它**不经过搜索引擎**，所以不碰任何 SERP 的 robots 限制。
#
# 搜索本身只能证明"站里有个昵称叫这个的账号"，不能证明"就是你"。
# 所以每个采集器都做两件事，两件都不能省：
#   1. **昵称必须逐字相等**（忽略大小写与首尾空白），不做包含匹配
#   2. 输出里标注来源是"昵称搜索逐字匹配"，不是"已确认"
#
# 搜索结果的取样条数：取 10 是因为实测「搜一个用户名」返回的近似账号
# 不会超过几条（`yang11235` 返回 3 个）。取太大没有意义，
# 反而增加被当成扫描的概率。
SEARCH_SAMPLE_SIZE = 10


# ---------------------------------------------------------------------------
# Discourse 论坛：一条实现覆盖一整类站
# ---------------------------------------------------------------------------
# Discourse 是当前技术社区最主流的论坛程序（openKylin、Appinn、openSUSE 中文、
# Rancher、NVIDIA 中文论坛都是）。它把**同一份 JSON** 挂在每个页面的
# `.json` 后缀上，不需要 API key、不需要登录。实测 12 个站能干净区分
# "有这人 / 没这人"：
#
#     /u/<user>.json                     账号存在→200，不存在→404
#     /topics/created-by/<user>.json     他创建过的主题列表（带标题和时间）
#
# **第二条是本次收集面扩展里最有价值的一条**：Discuz 的"列出该用户发过的
# 主题"被服务端关掉了（`do=thread` 永远返回「提示信息」，见 docs/footprint.md），
# 而 Discourse 直接给你这个列表。也就是说，"他参与过哪些帖子"这件事，
# 在 Discourse 站上不需要人工喂 URL。
#
# ## 合规：必须按路径逐站判断，不能整站放开
#
# Discourse 默认 robots 里有 `Disallow: /u/`（担心用户页被爬爆），
# 实测 12 个站里有 5 个禁 `/u/`：
#
#     bbs.openkylin.top / discuss.jittor.cn / forums.rancher.cn /
#     forum.robloxdev.cn / discuss.nebula-graph.com.cn
#
# （2026-11-17 校正：这段注释原来写"9 个站里有 4 个禁"、列表也只列 4 个，
# 是扩站后忘了同步。独立评审核验时发现计数与字典对不上，
# 逐站重算 robots 后确认字典本身的 True/False 全部正确，只有注释过时。）
#
# 但 **`/topics/created-by/` 在 12 个站全部允许**。
# 所以下面按 `allow_profile` 逐站判断：禁 `/u/` 的站只取主题列表，
# 不禁的站两样都取。宁少一条，不越界一次。
#
# ## 字段（实测 meta.appinn.net /u/meta.json）
#
#     user.created_at        注册时间
#     user.last_seen_at      最后活跃
#     user.last_posted_at    最后发帖
#     user.time_read         累计阅读秒数
#     user.trust_level       信任等级（0-4，反映社区参与深度）

# 实测可用且 robots 允许的站：host -> (显示名, 是否允许 /u/<user>.json)
#
# 为什么列白名单而不是"任意 Discourse 站"：做不到。每个站的 robots 不同
# （有的禁 /u/、有的连 topics 都禁），而且"是不是 Discourse"本身要实测。
# 没验证过的站不写进来——否则"这个站查不到"会被读成"这个站没有"。
DISCOURSE_SITES = {
    "meta.appinn.net": ("小众软件论坛", True),
    "forum.trae.cn": ("Trae 社区", True),
    "bbs.openkylin.top": ("openKylin 论坛", False),
    "forum.suse.org.cn": ("openSUSE 中文论坛", True),
    "discuss.jittor.cn": ("Jittor 论坛", False),
    "discuss.openubmc.cn": ("openUBMC 论坛", True),
    "forums.rancher.cn": ("Rancher 中文论坛", False),
    "forum.robloxdev.cn": ("Roblox 开发者论坛", False),
    "forums.developer.nvidia.cn": ("NVIDIA 开发者中文论坛", True),
    "forum.github-zh.cn": ("GitHub 中文社区", True),
    "luanti.forum.cosoc.cn": ("Luanti 中文论坛", True),
    "discuss.nebula-graph.com.cn": ("NebulaGraph 论坛", False),
}

# 单个站最多取多少个主题。Discourse 列表 30 条一页，不翻页就够看
# "他最近在做什么"；翻页会让请求量按站数翻倍。
DISCOURSE_TOPICS_PER_SITE = 30

# 这一轮的 Discourse 总请求上限。
#
# 为什么是 6 而不是"12 站 × 2 路径 = 24"：请求预算是**所有源共用**的，
# 一次自查总共 60 次。接口层如果吃掉 18 次，后面的论坛资料页、
# 已确认账号主页、种子帖子就都没额度了——实测过这一点：预算 20 时
# 接口层一次预留把额度占满，站点探测和种子请求全部拿不到配额，
# 症状是"接口有数据，痕迹检索却一条论坛内容都没有"。
#
# 6 次 ≈ 3 个站（每站 2 条路径）或 6 个站（每站 1 条路径）。
# 按"能不能拿到内容"排序：优先取 robots 允许 `/u/` 的站（能同时拿到
# 注册时间与主题列表），禁 `/u/` 的站排在后面只取主题。
DISCOURSE_REQUEST_CAP = 6

# 论坛安装时自动创建的内置账号。**跨站普遍存在**，而且内容几乎全是
# 站务公告（「提问前，请首先阅读」「2024 年度回顾」）。
#
# 实测 `system` 在 7 个站都命中、共 90 条主题——如果用户搜的正好是
# `system` / `admin`，输出会变成"这个人在 7 个技术论坛都是管理员"，
# 而那 7 个账号分属 7 个不同的运营方，与他毫无关系。
#
# 这些名字**不是"不能查"，是"查出来的东西必然不是他"**。所以直接跳过：
# 产出为零好过产出一堆别人的站务公告。这与项目一贯的立场一致——
# 宁可少给，不要错给。
DISCOURSE_RESERVED = frozenset(
    {
        "system",
        "admin",
        "administrator",
        "moderator",
        "discourse",
        "staff",
        "support",
        "team",
        "guest",
        "anonymous",
    }
)


def _discourse_order():
    """按"产出密度"给站点排序：能同时拿资料页的站优先。

    robots 允许 `/u/` 的站一次能产出"注册时间 + 最后活跃 + 主题列表"，
    禁 `/u/` 的站只有主题列表。预算封顶 6 次时，先跑前者能让同样的
    请求数换来更多信息。

    字典本身有序（3.7+ 保序），这里只是把两类分开，不改变同类内部的顺序
    （内部顺序按 `DISCOURSE_SITES` 的书写顺序，实测有效的站写在前面）。
    """
    full = [(h, v) for h, v in DISCOURSE_SITES.items() if v[1]]
    topics_only = [(h, v) for h, v in DISCOURSE_SITES.items() if not v[1]]
    return full + topics_only


async def collect_discourse(
    session: Any,
    username: str,
    timeout: float = 20.0,
    notes: Optional[List[str]] = None,
) -> List[ApiFact]:
    """Discourse 论坛：注册/活跃时间 + 他创建过的主题。

    两件事分别取，各自有独立的合规判断（见上）。

    站与站之间不设间隔——不同主体，不像同一站点翻页那样会给单个站压力。
    但总量有上限，避免 12 站 × 2 路径把预算吃光。
    """
    facts: List[ApiFact] = []
    target = (username or "").strip()
    if not target:
        return facts
    # 内置账号直接跳过：命中必然是别人的站务公告，见 DISCOURSE_RESERVED。
    if target.lower() in DISCOURSE_RESERVED:
        logger.debug("Discourse：%r 是论坛内置账号名，跳过", target)
        return facts

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; maigret-cn-selfcheck)",
    }
    budget = DISCOURSE_REQUEST_CAP

    async def get_json(url: str):
        nonlocal budget
        if budget <= 0:
            return None
        budget -= 1
        try:
            async with session.get(url, headers=headers, timeout=timeout) as response:
                if response.status == 404:
                    # 404 = 这个用户名在该站不存在，是明确答案，不是失败
                    return None
                if response.status != 200:
                    _note(notes, f"Discourse {host} 返回 HTTP {response.status}，该站未读到")
                    return None
                return await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Discourse %s 失败：%s", url, exc)
            _note(notes, f"Discourse {host} 请求失败（{type(exc).__name__}），该站未读到")
            return None

    for host, (label, allow_profile) in _discourse_order():
        # 先取主题列表：它在所有实测站都允许，而且即使没有资料页也能产出内容。
        #
        # ⚠️ `topics` 有三种状态，必须分开（2026-11-17 修）：
        #     None       请求失败/被限流/超时 → "**没读到**"，不能下任何断言
        #     dict+空列表 成功读到，他真的没发过主题 → 可以说"未创建过主题"
        #     dict+有条目 正常产出
        # 原来只用一个 `found_topic` 布尔值，把前两种混成一种，
        # 于是请求失败时会输出"未创建过主题"——**把没读到说成没有**，
        # 这正是本项目最不能犯的错。
        topics = await get_json(f"https://{host}/topics/created-by/{target}.json")
        topics_read = isinstance(topics, dict)
        found_topic = False
        # 主题先缓冲在 `topic_facts` 里，**等身份校验通过再并进 facts**。
        # 2026-11-17 独立评审 P0：原来是边解析边写入，而身份校验在下面
        # （`/u/<u>.json` 的 username 比对）。若那个站对用户名做了模糊匹配，
        # 我们会把**另一个人的**主题记成目标的，且已经写进结果、无法挽回。
        topic_facts: List[ApiFact] = []
        if topics_read:
            topic_list = topics.get("topic_list") or {}
            for topic in (topic_list.get("topics") or [])[:DISCOURSE_TOPICS_PER_SITE]:
                if not isinstance(topic, dict):
                    continue
                title = str(topic.get("title") or "").strip()
                if not title:
                    continue
                found_topic = True
                slug = str(topic.get("slug") or "")
                topic_id = topic.get("id")
                topic_facts.append(
                    ApiFact(
                        site=label,
                        kind="主题",
                        title=title,
                        date=_iso_date(topic.get("created_at", "")),
                        url=(
                            f"https://{host}/t/{slug}/{topic_id}"
                            if topic_id
                            else f"https://{host}/"
                        ),
                        detail=f"回复 {topic.get('posts_count', 0)}",
                    )
                )

        # 资料页只在 robots 允许的站取。
        # `topics/created-by` 返回空列表有两种含义——"他没发过主题"和
        # "这个人根本不存在"。要靠资料页区分，但资料页被禁时无法区分，
        # 所以那种情况下宁可不产出（不把"没读到"说成"没发过"）。
        if not allow_profile:
            # 没有资料页可用于校验身份。该端点本身是"服务端按用户名限定"的
            # （不存在该用户时 404，实测 12 个站一致），所以这里的主题是他本人的。
            facts.extend(topic_facts)
            continue
        profile = await get_json(f"https://{host}/u/{target}.json")
        if not isinstance(profile, dict):
            # 资料页没读到：无法确认身份，**连主题一起丢弃**。
            # 宁可少给，也不要在无法确认的情况下把内容记到他名下。
            continue
        user = profile.get("user") or {}
        if not isinstance(user, dict):
            continue
        # 昵称必须逐字相等：个别站会对 `/u/xxx.json` 做模糊匹配，
        # 比对一次很便宜。**比对不过就整体丢弃（含主题）**——
        # 模糊匹配一旦发生，`/topics/created-by/` 很可能也匹配到了同一个人。
        if str(user.get("username") or "").strip().lower() != target.lower():
            continue

        # 身份校验通过，主题现在才可以并入
        facts.extend(topic_facts)

        parts = []
        for key, name in (("trust_level", "信任等级"), ("time_read", "累计阅读秒")):
            value = user.get(key)
            if value not in (None, ""):
                parts.append(f"{name}：{value}")

        created = _iso_date(user.get("created_at", ""))
        last_seen = _iso_date(user.get("last_seen_at", ""))
        last_posted = _iso_date(user.get("last_posted_at", ""))
        detail = f"最后活跃 {last_seen or '未知'}；最后发帖 {last_posted or '未知'}"

        # 一个可用字段都没返回时，不要产出一行"账号资料"——
        # 实测 forum.trae.cn 的部分账号只回 9 个键，没有 created_at / trust_level。
        # 那种情况下唯一诚实的话是"这个用户名存在，但该站限制了资料接口"。
        # 记成"账号资料"是纯噪音，用户还得自己判断这行有没有信息。
        if not parts and not created and not last_seen and not last_posted:
            facts.append(
                ApiFact(
                    site=label,
                    kind="账号",
                    title="存在此用户名（该站限制了资料接口）",
                    date="",
                    url=f"https://{host}/u/{target}",
                    detail="只能确认账号存在，取不到时间字段",
                )
            )
            continue

        facts.append(
            ApiFact(
                site=label,
                kind="账号",
                title="；".join(parts) or "账号资料",
                date=created,
                url=f"https://{host}/u/{target}",
                detail=detail,
            )
        )

        # 有资料页但没主题：这是"他真的没发过"，可以如实说。
        # 让报告能区分"没发过"和"没查到"。
        #
        # 前提是**主题列表这次真的读到了**（`topics_read`）。
        # 读失败时不产出任何结论——那是"没读到"，不是"没有"。
        if topics_read and not found_topic:
            facts.append(
                ApiFact(
                    site=label,
                    kind="账号",
                    title="未创建过主题",
                    date=_iso_date(user.get("created_at", "")),
                    url=f"https://{host}/u/{target}",
                    detail="资料页存在，主题列表为空",
                )
            )

    return facts


async def collect_netease(
    session: Any,
    username: str,
    timeout: float = 20.0,
    notes: Optional[List[str]] = None,
) -> List[ApiFact]:
    """网易云音乐：昵称搜索 → uid → 用户详情 + 歌单列表。

    ## 为什么这个源值得接

    歌单的 `createTime` 是**极强的**时间线信号：一个 2013 年建的歌单说明
    这个人 2013 年就在用这个平台。实测一个账号拿到 7 个歌单，跨度
    2013-06 ～ 2025-08，其中「我喜欢的音乐」创建于 2013-06-18 —— 那基本
    就是注册时间（网易云把"我喜欢的音乐"在注册时自动建出来）。

    这个信号比"注册时间"更有信息量：它同时给出**起点**和**持续活跃的证据**，
    而且每条都带具体日期。

    ## 两步请求，都不需要登录

        1. /api/search/get/web?s=<昵称>&type=1002
           返回 userprofiles[]，含 nickname / userId / province / city
        2. /api/v1/user/detail/<uid>        等级、听歌数、创建天数
           /api/user/playlist/?uid=<uid>    歌单列表，每个带 createTime

    ## 三道过滤，缺一条就会把别人的数据算到他头上

    1. **昵称逐字相等**：实测搜 `yang11235` 返回 3 个用户
       （`yang11235` / `yang112357` / `yang112358`）。不做精确比对，
       后两个的省份（黑龙江、河北）会被写进他的画像。
    2. **歌单必须属于该 uid**：接口是"取某人的歌单"，理论上不会有别人的，
       但 `creator.userId` 是可核对的事实，顺手核对一次很便宜。
    3. **歌单创建时间必须合理**：`createTime` 实测出现过 `-1`，
       直接换算会得到 1969 年。

    ## 不取的东西

    不取听歌记录（`/api/user/record`，属于"在听什么"，比"建过什么歌单"
    敏感得多），不取关注/粉丝列表（那是别人的数据），不取收藏的歌手。
    """
    facts: List[ApiFact] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Referer": "https://music.163.com/",
    }
    target = (username or "").strip().lower()
    if not target:
        return facts

    # 第一步：昵称 → uid（只认逐字相等的那个）
    try:
        async with session.get(
            "https://music.163.com/api/search/get/web",
            params={"s": username, "type": 1002, "offset": 0, "total": "true",
                    "limit": SEARCH_SAMPLE_SIZE},
            headers=headers,
            timeout=timeout,
        ) as response:
            if response.status != 200:
                # 不是 200 就是"没读到"（429 限额、5xx、被拦）。
                # 静默返回会让报告显示"网易云上没痕迹"而不是"这次没读到"。
                _note(
                    notes,
                    f"网易云搜索接口返回 HTTP {response.status}，该来源未读到",
                )
                return facts
            payload = await response.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("网易云昵称搜索失败：%s", exc)
        _note(notes, f"网易云搜索请求失败（{type(exc).__name__}），该来源未读到")
        return facts

    if not isinstance(payload, dict):
        return facts
    profiles = ((payload.get("result") or {}).get("userprofiles") or [])
    exact = [
        item for item in profiles
        if isinstance(item, dict)
        and str(item.get("nickname") or "").strip().lower() == target
    ]
    if not exact:
        return facts

    # 同名不同 uid：**不能合并**。
    #
    # 2026-11-17 独立评审 P0。搜索接口会返回多个昵称逐字相同的账号
    # （不同 userId，是不同的人）。原实现把它们的歌单全记在同一个
    # `site="网易云音乐"` 名下，报告里就变成"他的歌单"——
    # 而其中一部分属于另一个同名的人。
    #
    # 处理方式：多条同名时按 uid 区分站点标签（`网易云音乐(uid:1591220)`），
    # 并额外产出一条提示说明歧义。这样既不丢信息，也不会把两个人混成一个。
    # 只选"最像的那个"是错的——没有依据排序；直接丢弃也是错的——会漏掉真的那个。
    ambiguous = len(exact) > 1
    if ambiguous:
        facts.append(
            ApiFact(
                site="网易云音乐",
                kind="提示",
                title=f"搜到 {len(exact)} 个昵称完全相同的账号，已按 uid 分别列出",
                date="",
                url="https://music.163.com/",
                detail=(
                    "这些账号是**不同的人**（uid 不同），数据未合并。"
                    "哪一个是你要找的，需要你自己判断。"
                ),
            )
        )

    site = "网易云音乐"
    # 封顶，让请求数真的有上界（与 `_REQUESTS["collect_netease"]` 对齐）
    if len(exact) > NETEASE_MAX_ACCOUNTS:
        facts.append(
            ApiFact(
                site="网易云音乐",
                kind="提示",
                title=f"同名账号有 {len(exact)} 个，只取前 {NETEASE_MAX_ACCOUNTS} 个",
                date="",
                url="https://music.163.com/",
                detail="控制请求量，未处理的同名账号请自行到站内核对",
            )
        )
    for profile in exact[:NETEASE_MAX_ACCOUNTS]:
        uid = profile.get("userId")
        if uid in (None, ""):
            continue
        # 同名多条时把 uid 写进站点标签，避免报告里看起来像同一个人
        site = f"网易云音乐(uid:{uid})" if ambiguous else "网易云音乐"
        account_url = f"https://music.163.com/#/user/home?id={uid}"

        # 第二步：用户详情（等级/听歌数/创建天数）
        detail = None
        try:
            async with session.get(
                f"https://music.163.com/api/v1/user/detail/{uid}",
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.status == 200:
                    detail = await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("网易云用户详情 %s 失败：%s", uid, exc)

        registered = ""
        if isinstance(detail, dict):
            parts = []
            level = detail.get("level")
            songs = detail.get("listenSongs")
            days = detail.get("createDays")
            if level not in (None, ""):
                parts.append(f"等级：{level}")
            if songs not in (None, ""):
                parts.append(f"累计听歌：{songs}")
            if days not in (None, ""):
                parts.append(f"注册天数：{days}")
            if parts:
                facts.append(
                    ApiFact(
                        site=site,
                        kind="账号",
                        title="；".join(parts),
                        date="",
                        url=account_url,
                        detail="来源：昵称搜索逐字匹配",
                    )
                )
            # createDays 能反推注册日：今天 - N 天。只在能算出来时才写，
            # 算不出来就留空，不用"今天"兜底（那会造出一条假的活跃时间）。
            if isinstance(days, int) and 0 < days < 365 * 40:
                import datetime

                moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
                registered = moment.strftime("%Y-%m-%d")

        # 第三步：歌单列表（核心产出）
        try:
            async with session.get(
                "https://music.163.com/api/user/playlist/",
                params={"uid": uid, "limit": 50, "offset": 0},
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    continue
                playlists = await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("网易云歌单 %s 失败：%s", uid, exc)
            continue

        if not isinstance(playlists, dict):
            continue
        items = playlists.get("playlist") or []
        oldest = ""
        for item in items:
            if not isinstance(item, dict):
                continue
            creator = item.get("creator") or {}
            if creator.get("userId") not in (None, "", uid) and str(creator.get("userId")) != str(uid):
                continue
            created = _epoch_ms_date(item.get("createTime"))
            if not created:
                continue
            if not oldest or created < oldest:
                oldest = created
            name = str(item.get("name") or "").strip()
            count = item.get("trackCount")
            title = f"歌单「{name}」" if name else "歌单"
            if count not in (None, ""):
                title += f"（{count} 首）"
            facts.append(
                ApiFact(
                    site=site,
                    kind="歌单",
                    title=title,
                    date=created,
                    url=f"https://music.163.com/#/playlist?id={item.get('id')}",
                    detail="来源：公开歌单列表",
                )
            )

        # 最早歌单 = 这个平台上的活动起点，比"注册天数"更可信
        if oldest:
            facts.append(
                ApiFact(
                    site=site,
                    kind="起点",
                    title=f"最早的公开歌单创建于 {oldest}"
                          + (f"（推算注册约 {registered}）" if registered else ""),
                    date=oldest,
                    url=account_url,
                    detail="来源：歌单 createTime 最小值",
                )
            )

    return facts


# 每个采集器消耗的请求数# 每个采集器消耗的请求数（用于预算核算，避免"看起来只调了一次"）。
#
# 为什么按采集器分别记而不是给一个统一值：这个数字直接喂给
# `reserve_slots()` 做**原子扣减**，估少了实际请求就会超出预算——
# 而超出的那部分在日志里是看不见的（只出现一次调用）。
# 实测每个采集器实际发出的请求：
#
#   collect_github    4 次  /users/{u} + /repos + /events/public + /search/issues
#   collect_netease   3 次  /search/get/web + /user/detail + /user/playlist
#
# 取各源里的最大值给 `REQUESTS_PER_COLLECTOR`（保留原名，调用方在用），
# 需要精确核算的地方用 `requests_for()`。
# 同名不同 uid 时最多处理几个账号。
#
# 2026-11-17 独立评审 P1#9：网易云的实际请求数是 `1 + 2 × 同名账号数`
# （搜索 1 次，每个账号再来详情 + 歌单各 1 次），而 `_REQUESTS` 里写死 3。
# 搜到 2 个同名账号就发 5 次，**超出预留额度且日志与报告里都看不出来**。
# 修法不是改数字（数字永远追不上实际），而是给账号数封顶，
# 让"最坏情况"真的有上界，常量写的也就是这个上界。
NETEASE_MAX_ACCOUNTS = 3

_REQUESTS = {
    "collect_github": 5,      # /users + /repos + /events + /search/issues + /starred
    # 引用常量而不是写死数字：改了封顶值这里自动跟着变。
    # 写死过一个 12，改常量后忘了同步，测试立刻红——这就是为什么不再写数字。
    "collect_discourse": DISCOURSE_REQUEST_CAP,
    # 搜索 1 次 + 每个账号（详情 + 歌单）2 次，账号数封顶见上。
    "collect_netease": 1 + 2 * NETEASE_MAX_ACCOUNTS,
}

# 每个采集器消耗的请求数（保守值 = 最大值）。预算按"最坏情况"预留，
# 宁可少跑一个源，也不要静默超发。
REQUESTS_PER_COLLECTOR = max(_REQUESTS.values())


def requests_for(collectors=None) -> int:
    """这一轮采集预计要发多少个请求。

    `collectors` 不传就是全部。预算层用它一次性预留，
    避免"只按一次调用记账"造成的静默超发。
    """
    items = collectors if collectors is not None else _collectors()
    total = 0
    for collector in items:
        total += _REQUESTS.get(getattr(collector, "__name__", ""), REQUESTS_PER_COLLECTOR)
    return total
