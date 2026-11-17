"""内容页解析测试（`maigret/cn/content.py`）。

夹具是 2026-11-16 从 `bbs.newwise.com` 抓的真实页面，不是手写的。
手写夹具只能证明"解析器符合我对页面的想象"，证明不了它符合真实页面——
这个模块的每一条正则都是从这些真实结构里反推出来的，用假数据测等于白测。

体积已裁剪（只留前 3 个楼层块），但结构一字未改。
"""

from pathlib import Path

import pytest

from maigret.cn import content

FIXTURES = "tests/fixtures/cn"


def _read(name: str) -> str:
    with open(f"{FIXTURES}/{name}", encoding="utf-8", errors="replace") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2009年8月13日", "2009-08-13"),
        ("2009-8-13", "2009-08-13"),
        ("2009-8-13 19:45:30", "2009-08-13 19:45:30"),
        ("发表于 2009-9-18 07:30:30", "2009-09-18 07:30:30"),
        ("", ""),
        # 认不出来的**原样返回**，不编一个值。归一化只在能归一时发生；
        # 上层真正用来排序/推断的是 `footprint.to_iso_date()`，
        # 那个才对"认不出来"返回空串。
        ("3 天前", "3 天前"),
        ("昨天", "昨天"),
        # 非法日期也原样返回：编一个合法日期比留着原串更坏，
        # 因为报告里看不出哪个是编的
        ("2020-13-45", "2020-13-45"),
        ("2009-02-30", "2009-02-30"),
    ],
)
def test_normalize_cn_date(raw, expected):
    assert content._normalize_cn_date(raw) == expected


def test_valid_date_rejects_impossible_calendar_dates():
    assert content._valid_date("2009", "2", "30") is False, "2 月没有 30 号"
    assert content._valid_date("2009", "4", "31") is False, "4 月没有 31 号"
    assert content._valid_date("2008", "2", "29") is True, "2008 是闰年"
    assert content._valid_date("1900", "1", "1") is False, "超出 1990-2100 范围"


@pytest.mark.parametrize(
    "a,b,expected",
    [
        # 允许的两种归一化：大小写、空白
        ("yang11235", "YANG11235", True),
        ("yang11235", "yang11235 ", True),
        ("\u6768\u67d0", "\u6768\u67d0", True),
        # 必须**不**相等（2026-11-17 独立评审 P1#10 收紧）
        #
        # 这几条原来写的是 True（`yang 11235`、`yang_11235` 都当同一人）。
        # 那是把风险写成了规格：`same_author` 是"这层楼是不是他发的"的
        # 唯一判据，归一化每宽一分，"把别人的话记成他说的"就多一条路。
        # 分隔符在各站都是**账号名的一部分**，不是噪声。
        ("yang11235", "yang 11235", False),
        ("yang11235", "yang_11235", False),
        ("yang11235", "yang-11235", False),
        ("john.smith", "johnsmith", False),
        ("a-b", "ab", False),
        ("yang11235", "yang11236", False),
        ("", "", False),  # 空对空不算同一个人
        ("yang11235", "", False),  # 作者为空时不能判成"是他"
        ("wizzer", "wizzer0fn3", False),  # 不做前缀匹配
    ],
)
def test_same_author(a, b, expected):
    assert content.same_author(a, b) is expected


@pytest.mark.parametrize(
    "html,blocked",
    [
        ("<title>百度安全验证</title>", True),
        ("<h1>访问异常页面</h1>", True),
        ("", True),
        ("<div>正常内容</div>", False),
    ],
)
def test_looks_blocked(html, blocked):
    assert content.looks_blocked(html) is blocked


def test_looks_blocked_by_status_wins():
    """状态码说是被拦，就算正文看起来正常也算被拦。

    宁可多报一次"没读到"，也不能把 403 的正文当证据。
    """
    assert content.looks_blocked("<div>正常内容</div>", 403) is True


# ---------------------------------------------------------------------------
# Discuz 楼层
# ---------------------------------------------------------------------------
def test_parse_discuz_posts_extracts_author_time_body():
    posts = content.parse_thread(_read("discuz_author_filter.html"))
    assert posts, "应从真实页面解析出楼层"

    first = posts[0]
    assert first.author == "yang11235"
    assert first.author_id == "463889"
    assert first.posted_at.startswith("2009-")
    assert first.anchor.startswith("pid")
    assert first.text, "楼层正文不该为空"


def test_parse_discuz_posts_keeps_raw_time_string():
    """`posted_at` 必须是原文形态，`date` 才归一化。

    原串是核对用的唯一凭据：归一化一旦算错（时区、错认成编辑时间），
    没有原串就再也追不回来。
    """
    posts = content.parse_thread(_read("discuz_author_filter.html"))
    for post in posts:
        assert "发表于" not in post.posted_at, "「发表于」前缀应被剥掉"
        assert post.posted_at.strip() == post.posted_at


def test_parse_discuz_posts_strips_edit_tail():
    """「本帖最后由 X 于 T 编辑」是改帖时间，不是发言时间，必须去掉。

    不去掉的话，用户看到的时间可能比真实发言晚好几年——
    而"从发言时间推年龄"正是这个工具最容易被依赖的输出。
    """
    posts = content.parse_thread(_read("discuz_author_filter.html"))
    for post in posts:
        assert not post.text.startswith("本帖最后由"), f"残留编辑尾巴：{post.text[:40]}"


def test_parse_discuz_posts_returns_none_for_other_sites():
    """非 Discuz 页面返回 None，不是空列表。

    None = "这不是我认识的结构"，[] = "结构对但没内容"。
    两者混同会让"解析器不匹配"看起来像"这人没发过言"。
    """
    assert content.parse_discuz_posts("<html><body>hello</body></html>") is None


# ---------------------------------------------------------------------------
# Discuz 资料页
# ---------------------------------------------------------------------------
def test_parse_discuz_profile_fields():
    profile = content.parse_profile(_read("discuz_profile.html"))
    assert profile is not None
    assert profile.name == "yang11235", "UID 后缀应从名字里剥掉"
    assert profile.uid == "463889"
    assert profile.registered_at.startswith("2009-1-12")
    assert profile.last_visit.startswith("2012-1-5")
    assert profile.last_post_at.startswith("2010-10-7")
    assert "1010" in profile.online_hours


def test_parse_discuz_profile_absent_page_returns_none():
    """「提示信息」页不能解析出资料——否则"没这个人"会被当成"有这个人"。"""
    assert content.parse_profile(_read("discuz_absent.html")) is None


# ---------------------------------------------------------------------------
# 资料页：签名与站内链接
# ---------------------------------------------------------------------------
def test_parse_discuz_profile_reads_signature():
    """签名必须抽到。

    实测形态不是 `<dd id="sign">`，而是混在统计列表里的
    `<li><em class="xg1">个人签名&nbsp;&nbsp;</em><table><tr><td>…`。
    只认前者的话签名一条都抽不到——连带丢掉签名里的帖子地址，
    而那恰恰是唯一能自动发现的帖子线索。
    """
    profile = content.parse_profile(_read("discuz_profile_with_signature.html"))
    assert profile is not None
    assert profile.signature, "签名不该为空"
    assert "閃光" in profile.signature or "慎入" in profile.signature


def test_parse_discuz_profile_extracts_site_links():
    """签名里的帖子地址要变成可跟进的线索。

    这是"论坛关掉发帖列表"之后唯一还能自动发现帖子的路径：
    用户自己在公开签名里贴了地址。实测 yang11235 的签名里就是
    `http://bbs.newwise.com/thread-481192-1-1.html`。
    """
    profile = content.parse_profile(_read("discuz_profile_with_signature.html"))
    assert profile.site_links == ["481192"]


def test_extract_site_links_handles_both_url_forms():
    """两种帖子地址形态都要认，且要去重。"""
    got = content._extract_site_links(
        "", "http://a.com/thread-111-1-1.html 和 forum.php?mod=viewthread&tid=222 "
        "还有重复的 http://a.com/thread-111-1-1.html"
    )
    assert got == ["111", "222"]


def test_extract_site_links_ignores_non_thread_links():
    """空间页、版块页、外站都不算帖子线索。"""
    got = content._extract_site_links(
        "", "http://a.com/home.php?mod=space&uid=1 http://other.com/thread-999-1-1.html"
    )
    # 站内空间页不是帖子；外站的帖子地址也不该当本站种子
    assert "1" not in got


def test_parse_discuz_profile_reads_stats_from_links():
    """回帖数/积分藏在 `<a>回帖数 1564</a>` 里，不是 `<em>标签</em>值`。

    原来只解析 `<em>` 形态，导致回帖数恒为空——而"回帖 1564、在线 1010 小时"
    正是判断"那几年投不投入"的关键数字。
    """
    profile = content.parse_profile(_read("discuz_profile_with_signature.html"))
    assert profile.post_count == "1564", f"实际 {profile.post_count!r}"
    assert profile.extra.get("积分") == "1808"


def test_permission_wall_detected_for_board_access_rules():
    """版块级访问条件也要认成权限墙。

    实测 tid=481192 的提示是「您需要满足以下条件才能访问这个版块
    访问条件：N币 ≥ 0」，游客连 0 都不满足，**匿名永久不可读**。
    不认出来的话会被当成"帖子没有内容"，最终报成"他没发过言"。
    """
    wall = "您需要满足以下条件才能访问这个版块 访问条件： N币 ≥ 0 您的信息： N币: 0"
    assert content.discuz_permission_wall(wall) is True
    assert content.looks_blocked(wall) is True


# ---------------------------------------------------------------------------
# Discuz Archiver（纯静态精简页）
# ---------------------------------------------------------------------------
def test_parse_discuz_archiver_extracts_all_posts():
    """Archiver 页要能抽全发言与精确时间。

    这是"自动发现帖子"能力的技术基础：`archiver/?tid-X.html&authorid=Y`
    由服务端渲染、不含 JS，比论坛正常页小 15 倍（实测 5.2KB vs 78KB）
    却返回完全相同的内容。
    """
    posts = content.parse_discuz_archiver(_read("discuz_archiver_author.html"))
    assert posts, "应从 Archiver 页解析出楼层"
    assert len(posts) == 15
    assert all(p.author == "yang11235" for p in posts)
    assert all(p.posted_at for p in posts), "每条都要有精确时间"
    assert posts[0].thread_title, "标题要抽到"


def test_parse_discuz_archiver_strips_page_header():
    """第一条正文不能混进页面标题栏。

    Archiver 把正文写在署名**上方**，所以第一条的"段"其实是整个页面头
    （面包屑 + 标题栏）。不过滤的话第一条内容会变成
    「ABSOLUTE POWERFORCE… - 资料&提问互助区 - 壬天堂世界 - Powered by…」，
    看起来像一条发言，实际是页面元信息——这种脏数据会被 AI 当正文分析。
    """
    posts = content.parse_discuz_archiver(_read("discuz_archiver_author.html"))
    assert "Powered by" not in posts[0].text
    assert "›" not in posts[0].text
    assert "壬天堂" not in posts[0].text


def test_parse_discuz_archiver_preserves_newlines():
    """正文里的换行要保留：多段发言压成一行会丢掉结构。"""
    posts = content.parse_discuz_archiver(_read("discuz_archiver_author.html"))
    multi = [p for p in posts if "\n" in p.text]
    assert multi, "应当有含换行的发言（<br /> 要转成 \\n）"


def test_parse_discuz_archiver_returns_none_for_other_pages():
    """不是 Archiver 页就返回 None，不能返回空列表。

    返回 [] 会让上层以为"结构认识但没内容"，从而放弃退回普通页解析。
    """
    assert content.parse_discuz_archiver(_read("discuz_author_filter.html")) is None
    assert content.parse_discuz_archiver("<html></html>") is None


def test_parse_thread_prefers_archiver_parser():
    """统一入口应优先走 Archiver 解析器。"""
    posts = content.parse_thread(_read("discuz_archiver_author.html"))
    assert posts and len(posts) == 15


# ---------------------------------------------------------------------------
# 通用兜底
# ---------------------------------------------------------------------------
def test_parse_generic_page_reads_meta():
    html = (
        '<html><head><title>T</title>'
        '<meta property="article:published_time" content="2009-08-13T19:45:30+08:00">'
        '<meta name="description" content="D">'
        "</head></html>"
    )
    got = content.parse_generic_page(html)
    assert got["title"] == "T"
    assert got["published"].startswith("2009-08-13")
    assert got["description"] == "D"


def test_parse_generic_page_returns_none_when_nothing():
    assert content.parse_generic_page("<html><body>无</body></html>") is None


# ---------------------------------------------------------------------------
# 线索抽取
# ---------------------------------------------------------------------------
def test_mine_clues_only_returns_literals():
    """只回原文里的字面值，不做归一化也不做同义扩展。

    工具不该猜"翠园"是学校——那是 AI 分析的事。这里猜错，用户会
    照着一个错结论去改隐私设置。
    """
    clues = content.mine_clues("我在深圳中学读书，邮箱 a@b.com，QQ：12345678")
    assert "深圳中学" in clues["places"]
    assert "a@b.com" in clues["contacts"]
    assert "12345678" in clues["qq"]


def test_mine_clues_empty_text():
    clues = content.mine_clues("")
    assert clues == {"places": [], "contacts": [], "qq": []}


def test_mine_clues_dedupes():
    clues = content.mine_clues("深圳中学 深圳中学")
    assert clues["places"].count("深圳中学") == 1


# ---------------------------------------------------------------------------
# Discuz 的「不存在」与「没权限」必须分开
# ---------------------------------------------------------------------------
def test_discuz_absent_marker():
    """「无此人」判据必须逐字对上。

    Discuz 的「提示信息」模板同时用于"没有这个用户"和"没有权限"两种情形，
    所以**不能**拿「提示信息」当判据——那会把"我被拦了"写成"这个站没这个人"。
    """
    assert content.discuz_absent(_read("discuz_absent.html")) is True
    assert content.discuz_absent(_read("discuz_profile.html")) is False


def test_discuz_permission_wall_detected():
    wall = (
        "<title>提示信息 - 站 - Powered by Discuz!</title>"
        '<a href="home.php?mod=space">x</a>'
        "抱歉，您没有权限访问该页面"
    )
    assert content.discuz_permission_wall(wall) is True
    assert content.discuz_absent(wall) is False
    assert content.looks_blocked(wall) is True, "权限墙必须被认成被拦"


def test_permission_wall_not_confused_with_login_form():
    """普通 Discuz 页面头部有登录表单（"请 登录 后使用快捷导航"），

    不能因为出现"登录"就判成登录墙——那会把所有正常页面都当墙。
    """
    normal = _read("discuz_profile.html")
    assert "登录" in normal, "夹具里确实有登录表单"
    assert content.looks_blocked(normal) is False


# ---------------------------------------------------------------------------
# 统一入口：三态
# ---------------------------------------------------------------------------
def test_parse_thread_returns_none_for_unparseable():
    """`None` 表示"所有解析器都不认识这个结构"。

    它必须与 `[]`（结构认识但没内容）区分：折叠成同一件事的后果是
    "解析规则没覆盖"被显示成"这个人没发过话"，且没有任何地方提示异常。
    """
    assert content.parse_thread("<html></html>") is None


def test_parse_thread_returns_list_when_structure_matched():
    posts = content.parse_thread(_read("discuz_author_filter.html"))
    assert isinstance(posts, list) and posts


# ---------------------------------------------------------------------------
# 内容条目抽取
# ---------------------------------------------------------------------------
def test_parse_entries_returns_none_for_non_html():
    """不是网页（JSON/空响应）返回 None。

    与 `[]`（是网页但没条目）分开：很多平台主页本来就不列内容，
    把那种情形报成"结构不认识"，用户会以为工具坏了。
    """
    assert content.parse_entries("") is None
    assert content.parse_entries('{"a": 1}') is None


def test_parse_entries_returns_empty_for_html_without_entries():
    html = "<html><body><div>只有资料，没有内容列表</div></body></html>"
    assert content.parse_entries(html) == []


def test_parse_entries_cnblogs():
    html = """
    <html><body>
    <div class="day" role="article">
      <div class="dayTitle"><a href="https://x/p/archive/2026/11">2026年11月</a></div>
      <div class="postTitle"><a class="postTitle2 vertical-middle"
         href="https://x/p/23498987"><span>GitLab 升级记录</span></a></div>
      <div class="postCon">posted @ 2026-11-01 14:57 someone</div>
    </div>
    </body></html>
    """
    entries = content.parse_entries(html, base_url="https://x/")
    assert entries
    assert entries[0].title == "GitLab 升级记录"
    assert entries[0].date.startswith("2026-11-01")
    # 归档链接不是文章，必须排除
    assert all("/p/archive/" not in e.url for e in entries)


def test_parse_entries_generic_pairs_date_with_nearby_link():
    html = """
    <html><body>
    <ul>
      <li><a href="/posts/1">一篇足够长的文章标题</a> 2026-03-05</li>
    </ul>
    </body></html>
    """
    entries = content.parse_entries(html, base_url="https://blog.example.com/")
    assert entries
    assert entries[0].date == "2026-03-05"
    assert entries[0].url == "https://blog.example.com/posts/1"


def test_parse_entries_generic_skips_tiny_link_text():
    """"更多""阅读全文"这类按钮不该被当成文章标题。"""
    html = """
    <html><body><div>2026-03-05 <a href="/more">更多</a> <a href="/x">一个够长的真标题</a></div></body></html>
    """
    entries = content.parse_entries(html, base_url="https://b.example.com/")
    titles = [e.title for e in entries]
    assert "更多" not in titles
    assert "一个够长的真标题" in titles


# ---------------------------------------------------------------------------
# 内嵌 JSON 列表（前端渲染站）
# ---------------------------------------------------------------------------
# 夹具形态来自 2026-11-16 对 blog.csdn.net 主页的真实抽样：
# 文章列表在 `window.__INITIAL_STATE__` 里，页面上没有"日期+链接"的 HTML。
def _embedded_state(payload: dict) -> str:
    import json

    return (
        "<html><body><div id='app'></div>"
        f"<script>window.__INITIAL_STATE__ = {json.dumps(payload, ensure_ascii=False)};</script>"
        "</body></html>"
    )


CSDN_STATE = {
    "pageData": {
        "data": {
            "baseInfo": {
                "latelyList": [
                    {
                        "title": "旅游景点推荐系统 Python+Django+Vue.js",
                        "postTime": 1786320000000,
                        "url": "https://blog.csdn.net/u/article/details/1",
                    },
                    {
                        "title": "二手车在线交易商城 PHP+Thinkphp5+Vue.js",
                        "postTime": 1778774400000,
                        "url": "https://blog.csdn.net/u/article/details/2",
                    },
                ],
                # 专栏列表也在同一层，不该被当成文章
                "columnModule": [
                    {"title": "SQLite从入门到实战", "sum": 23},
                ],
            }
        }
    }
}


def test_parse_entries_embedded_initial_state():
    """CSDN 这类站的文章列表在页面内嵌 JSON 里，HTML 里没有。

    实测：走通用配对得 0 条（页面上没有"日期+链接"），
    走内嵌 JSON 得 20 条。把 20 条报成"这页没有列内容"是错的。
    """
    entries = content.parse_entries(
        _embedded_state(CSDN_STATE), base_url="https://blog.csdn.net/u"
    )
    assert entries
    titles = [e.title for e in entries]
    assert "旅游景点推荐系统 Python+Django+Vue.js" in titles
    assert entries[0].date == "2026-08-10"


def test_parse_entries_embedded_ignores_other_lists():
    """同一页里"最长的那个列表"才是内容列表。

    `columnModule`（专栏）也是 list，混进来就会把专栏当文章发出去。
    """
    entries = content.parse_entries(
        _embedded_state(CSDN_STATE), base_url="https://blog.csdn.net/u"
    )
    titles = [e.title for e in entries]
    assert "SQLite从入门到实战" not in titles


def test_parse_entries_embedded_next_data():
    """Next.js 站的挂载点是 `__NEXT_DATA__`，形态与 window 变量不同。"""
    import json

    html = (
        "<html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(
            {
                "props": {
                    "posts": [
                        {"title": "一篇文章的标题", "created_at": "2025-07-01T10:00:00Z"},
                    ]
                }
            },
            ensure_ascii=False,
        )
        + "</script></body></html>"
    )
    entries = content.parse_entries(html, base_url="https://x.example.com/")
    assert entries
    assert entries[0].title == "一篇文章的标题"
    # `Entry.date` 保留原粒度（时间部分不丢），归一成 YYYY-MM-DD 是
    # `footprint.to_iso_date()` 的职责 —— 与 cnblogs 那条路径同一口径。
    assert entries[0].date.startswith("2025-07-01")


def test_parse_entries_embedded_bad_json_falls_through():
    """内嵌 JSON 坏掉时不能抛异常，要退回去走通用配对。"""
    html = (
        "<html><body><div>2026-03-05 "
        '<a href="/p/1">一个够长的真标题</a></div>'
        "<script>window.__INITIAL_STATE__ = {坏掉的};</script></body></html>"
    )
    entries = content.parse_entries(html, base_url="https://b.example.com/")
    assert entries
    assert entries[0].title == "一个够长的真标题"


def test_parse_entries_skips_meta_label_before_link():
    """链接紧跟在「发起人：」后面时，那是**别人的名字**，不是内容标题。

    实测豆瓣活动页：「发起人：<a>LEE-H</a>」被当成"他在 2011-12-30 发布的内容"。
    把别人的名字记到他头上，比少记一条严重得多。
    """
    html = """
    <html><body><div>
      时间：2011年12月30日 周五<br/>
      <a href="https://www.douban.com/online/1/">豆瓣说，再见！</a>
      <div>发起人：<a href="https://www.douban.com/people/x/">LEE-H</a></div>
    </div></body></html>
    """
    entries = content.parse_entries(html, base_url="https://www.douban.com/")
    titles = [e.title for e in entries]
    assert "LEE-H" not in titles


@pytest.mark.parametrize(
    "text,expected",
    [
        ("豆瓣说，再见！", True),
        ("发起人：LEE-H", False),
        ("时间：2011年12月30日", False),
        ("1309人参加", False),
        ("2011年12月30日", False),
        ("", False),
    ],
)
def test_looks_like_title(text, expected):
    assert content._looks_like_title(text) is expected


# ---------------------------------------------------------------------------
# 软 404：对不存在的用户名也返回 200 + 完整模板页
# ---------------------------------------------------------------------------
def test_soft_404_profile_is_not_treated_as_existing():
    """有一批 Discuz 站对**不存在的用户名**也返回 200 + 完整模板页（软 404）。

    实测 bbs.wuyou.net：`neil` / `sam` / `yang11235` 三种情况返回的页面
    分别是 13755 / 13728 / 11469 字节，前两个（不存在的）**几乎相同**，
    标题都写成 `<用户名>的个人资料`。

    危害是具体的：原实现把 `<h2 class="mbn">活跃概况</h2>` 当成了用户名，
    于是 `Profile(name='活跃概况')` 非空 → 判成"这个用户存在"，
    接着把整页里**别人的**发言收进来。实测 `neil` 与 `sam` 拿到了
    完全相同的联系方式（`184822951@qq.com`、`13950133910`），
    而那些是无关用户在捐款回帖里留的。

    把别人的信息记到他头上，比少报严重得多。
    """
    assert content.parse_profile(_read("discuz_soft_404.html")) is None


def test_soft_404_fixture_reproduces_section_title_trap():
    """夹具必须真的复现那个陷阱：h2 里是区块标题，不是用户名。

    这条断言是给夹具本身做守卫——如果哪天夹具被换成真实资料页，
    下面那条测试会"因为错误的原因通过"。
    """
    raw = _read("discuz_soft_404.html")
    assert '<h2 class="mbn">活跃概况</h2>' in raw
    # 页面里没有 UID，这是软 404 的硬特征
    assert "UID" not in raw


def test_real_profile_still_parses_after_soft_404_guard():
    """加了软 404 判据后，真实资料页必须照旧解析。

    这是防止"修一个 bug 引入另一个"的关键对照：
    判据是"无 UID **且** 无任何时间/计数字段"，真实页两条都不满足。
    """
    profile = content.parse_profile(_read("discuz_profile.html"))
    assert profile is not None
    assert profile.uid == "463889"
    assert profile.registered_at


def test_extract_site_links_ignores_page_html():
    """只从签名取，不扫整页 HTML。

    原实现在整页里也扫帖子链接，理由是"有些人写在个人介绍里"。
    实测证明代价无法接受：bbs.wuyou.net 的资料页侧栏含公共区块
    （最新帖子/热门话题），那些 tid 对**所有用户**都一样，
    于是 `neil` 与 `sam` 都拿到 `site_links=['403684']`，
    各自去拉同一个帖子的全部楼层，两个不同的人拿到 93 条完全相同的内容——
    里面是别人的手机号 `13950133910` 和邮箱 `184822951@qq.com`。

    把别人的联系方式记到目标用户名头上，是这个项目最不能犯的错。
    所以第二个参数（整页 HTML）现在被刻意忽略。
    """
    page = (
        '<div class="sidebar">最新帖子：'
        '<a href="forum.php?mod=viewthread&tid=403684">别人的帖子</a></div>'
    )
    assert content._extract_site_links(page, "") == []


def test_extract_site_links_reads_signature_only():
    """签名里的帖子链接照旧要取到——它是唯一自动可发现的入口。"""
    signature = "我的帖子：http://bbs.newwise.com/thread-481192-1-1.html"
    page = '<a href="forum.php?mod=viewthread&tid=999999">公共区块</a>'
    assert content._extract_site_links(page, signature) == ["481192"]


def test_soft_404_guard_allows_profile_without_uid():
    """没有 UID 但有注册时间的资料页必须认。

    实测 bbs.wuyou.net 整站不显示 UID，真实账号（注册于 2004-01-03）
    只有 `<li>注册时间: 2004-1-3 21:14</li>` 这种**没有 <em> 包裹**的形态。
    如果判据是"必须有 UID"，这个站的真实账号会全部被判成"不存在"——
    那是漏报方向，用户会看到"这个论坛没有我"，而他 2004 年就注册了。
    """
    html = """<html><head><title>neil的个人资料 - 论坛 - Powered by Discuz!</title></head>
    <body>
    <h2 class="mbn">neil的个人资料</h2>
    <ul>
    <li>注册时间: 2004-1-3 21:14</li>
    <li>最后访问: 2004-1-3 21:59</li>
    <li>积分: 1</li>
    </ul>
    <a href="home.php?mod=space">home.php?mod=space</a>
    </body></html>"""
    profile = content.parse_profile(html)
    assert profile is not None
    assert profile.registered_at.startswith("2004-1-3")
    assert profile.last_visit.startswith("2004-1-3")


# ---------------------------------------------------------------------------
# 推荐位 / 侧栏：别人的内容不能被记到他头上（2026-11-17 独立评审 P1#12 / P1#13）
# ---------------------------------------------------------------------------
def test_walk_lists_skips_recommendation_containers():
    """推荐位比本人内容更长时，也不能选它。

    独立评审 P1#12：`_walk_lists` 原来"取最长的那个列表"，
    而 `_LIST_KEYS` 含 `list` / `posts` 这类通用键，推荐位用的正是这种结构：
        {"user": {"articleList": [2 条本人文章]},
         "recommend": {"list": [6 条**别人**的文章]}}
    → 抽出 6 条别人的文章挂到"他的主页内容"下面。
    """
    payload = {
        "user": {"articleList": [{"title": "本人文章1"}, {"title": "本人文章2"}]},
        "recommend": {"list": [{"title": f"别人的推荐{i}"} for i in range(6)]},
    }
    got = content._walk_lists(payload)
    assert [x["title"] for x in got] == ["本人文章1", "本人文章2"]


def test_walk_lists_still_takes_the_users_own_long_list():
    """配对用例：本人列表更长时照常选中，别把功能一起削掉。"""
    payload = {"data": {"latelyList": [{"title": f"本人{i}"} for i in range(20)]}}
    assert len(content._walk_lists(payload)) == 20


def test_generic_entries_skip_sidebar_and_do_not_borrow_dates():
    """侧栏链接既不进条目，也不继承本人条目的日期。

    独立评审 P1#13：日期窗口固定取 ±200 字符，侧栏链接只要落在窗口内
    就会被赋上那个日期（实测三条侧栏条目全部被写成同一个日期，
    其中两条还是别人的文章）。
    """
    html = (
        '<div class="main">'
        '<span>2020-05-05</span><a href="/a/1">我自己的文章甲</a>'
        '</div>'
        '<div class="sidebar hot">'
        '<a href="/b/1">别人的热门文章一</a>'
        '<a href="/b/2">别人的热门文章二</a>'
        '</div>'
    )
    entries = content._parse_generic_entries(html, "https://x.com")
    titles = [e.title for e in entries]
    assert titles == ["我自己的文章甲"], titles
    assert all("别人" not in t for t in titles)


def test_signature_link_in_href_yields_site_link():
    """签名里的 `<a href>` 形态也要能抽出帖子地址。

    独立评审 P1#11：`profile.signature` 是剥过标签的纯文本，
    原来的实现把它交给 `_extract_site_links`，于是 href 里的地址被剥掉，
    只有裸文本 URL 能活下来。现有夹具恰好是裸文本形态，所以测试通过、bug 被掩护。
    """
    src = (Path(__file__).parent / "fixtures" / "cn" / "discuz_profile.html").read_text(
        encoding="utf-8"
    )
    mutated = src.replace(
        "<td>http://bbs.newwise.com/thread-481192-1-1.html<br />",
        '<td><a href="http://bbs.newwise.com/thread-481192-1-1.html">我的帖子</a><br />',
    )
    assert mutated != src, "夹具形态变了，这条测试需要跟着更新"
    profile = content.parse_profile(mutated)
    assert profile is not None
    assert profile.site_links == ["481192"], profile.site_links


def test_profile_with_only_stats_is_not_empty():
    """只有 UID + 统计字段的资料页不算空页。

    独立评审 P2#16：`is_empty()` 原来只看 name/注册/最后访问/上次发表，
    昵称被模板差异吃掉的真实资料页会被判空 → `parse_profile` 返回 None
    → 上层报"结构不认识"，等于把一条能用的资料说成"没读到"。
    """
    profile = content.Profile(uid="463889", post_count="1564")
    assert not profile.is_empty()
    assert content.Profile().is_empty()
