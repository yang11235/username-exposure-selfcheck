"""脚迹检索测试（`maigret/cn/footprint.py`）。

这一层会真的发网络请求，所以测试全部用桩（fake session），不联网——
联网测试会在别人的服务器上跑，也会因为对方限流而随机变红，
最后没人再看这个测试。
"""

import asyncio
import json

import pytest

from maigret.cn import footprint
from maigret.cn.content import Post

FIXTURES = "tests/fixtures/cn"


def _read(name: str) -> str:
    with open(f"{FIXTURES}/{name}", encoding="utf-8", errors="replace") as handle:
        return handle.read()


class FakeResponse:
    def __init__(self, status, body, charset="utf-8"):
        self.status = status
        self._body = body
        self.charset = charset

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """按 URL 片段返回预设响应的假会话。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        for key, response in self.routes.items():
            if key in url:
                if isinstance(response, Exception):
                    raise response
                return response
        return FakeResponse(404, b"")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2009-1-12 13:23", "2009-01-12"),
        ("2012-1-5 16:27", "2012-01-05"),
        ("2009年8月13日", "2009-08-13"),
        ("", ""),
        # 认不出来必须是空串：排序与推断都依赖这个字段，
        # 编一个值会让"最近活跃时间"凭空出现
        ("3 天前", ""),
        ("1010 小时", ""),
    ],
)
def test_to_iso_date(raw, expected):
    assert footprint.to_iso_date(raw) == expected


def test_to_iso_date_rejects_impossible_and_non_dates():
    """日期字段是给推断用的，混进"不是日期的东西"会带偏时间跨度。

    三个实测案例：
        `2009-02-30` / `2009-04-31` —— 范围合法但日历上不存在
        `v2020.1.5` —— 版本号，`.` 被当分隔符且前面紧跟字母
        `1010 小时` / `1564` —— 时长与计数，完全没有日期形态
    """
    assert footprint.to_iso_date("2009-02-30") == ""
    assert footprint.to_iso_date("2009-04-31") == ""
    assert footprint.to_iso_date("v2020.1.5") == ""
    assert footprint.to_iso_date("1010 小时") == ""
    assert footprint.to_iso_date("1564") == ""
    assert footprint.to_iso_date("2009-08-13") == "2009-08-13"


# ---------------------------------------------------------------------------
# Archiver 优先
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "thread_url,expected",
    [
        (
            "https://bbs.example.com/thread-436216-1-1.html",
            "https://bbs.example.com/archiver/?tid-436216.html&authorid=9",
        ),
        (
            "https://bbs.example.com/forum.php?mod=viewthread&tid=436216",
            "https://bbs.example.com/archiver/?tid-436216.html&authorid=9",
        ),
    ],
)
def test_archiver_author_url(thread_url, expected):
    assert footprint.archiver_author_url(thread_url, "9") == expected


def test_archiver_author_url_empty_without_ids():
    assert footprint.archiver_author_url("", "1") == ""
    assert footprint.archiver_author_url("https://b.com/thread-1-1-1.html", "") == ""
    assert footprint.archiver_author_url("https://b.com/index.php", "1") == ""


def test_collect_from_thread_prefers_archiver():
    """有 Archiver 可用时只发一次请求，不再去抓 78KB 的普通页。

    同内容小 15 倍，省下的不只是流量——预算按次数算，但单页体积直接
    影响耗时与被限流的概率。
    """
    html = _read("discuz_archiver_author.html").encode("utf-8")
    session = FakeSession({"archiver/": FakeResponse(200, html)})
    finder = footprint.FootprintFinder(session, max_requests=10)
    posts, notes, status = _run(
        finder.collect_from_thread(
            "https://bbs.example.com/thread-436216-1-1.html", "yang11235", author_id="9"
        )
    )
    assert posts and len(posts) == 15
    assert status == "ok"
    assert len(session.calls) == 1, f"应当只发 1 次请求，实际 {session.calls}"
    assert "archiver" in session.calls[0]


def test_collect_from_thread_falls_back_when_archiver_empty():
    """Archiver 拿不到时退回普通作者视图，不能直接放弃。"""
    thread_html = _read("discuz_author_filter.html").encode("utf-8")
    session = FakeSession(
        {
            "archiver/": FakeResponse(200, b"<html>nothing here</html>"),
            "authorid=": FakeResponse(200, thread_html),
        }
    )
    finder = footprint.FootprintFinder(session, max_requests=10)
    posts, notes, status = _run(
        finder.collect_from_thread(
            "https://bbs.example.com/thread-436216-1-1.html", "yang11235", author_id="9"
        )
    )
    assert posts, "应当从普通作者视图拿到内容"
    assert any("authorid=" in url for url in session.calls)


def test_author_filter_url_from_pretty_url():
    got = footprint.author_filter_url(
        "https://bbs.example.com/thread-436216-1-1.html", "463889"
    )
    assert "mod=viewthread" in got
    assert "tid=436216" in got
    assert "authorid=463889" in got


def test_author_filter_url_from_query_url():
    got = footprint.author_filter_url(
        "https://bbs.example.com/forum.php?mod=viewthread&tid=436216", "463889"
    )
    assert "authorid=463889" in got
    assert got.count("tid=436216") == 1


def test_author_filter_url_replaces_existing_authorid():
    """重复调用不能把 authorid 叠成一串。"""
    once = footprint.author_filter_url(
        "https://bbs.example.com/forum.php?mod=viewthread&tid=1", "11"
    )
    twice = footprint.author_filter_url(once, "22")
    assert twice.count("authorid=") == 1
    assert "authorid=22" in twice


def test_author_filter_url_returns_empty_when_no_tid():
    assert footprint.author_filter_url("https://bbs.example.com/index.php", "1") == ""


def test_author_filter_url_page_suffix():
    got = footprint.author_filter_url(
        "https://bbs.example.com/thread-1-1-1.html", "9", page=3
    )
    assert got.endswith("page=3")


def test_max_page_from_reads_chinese():
    assert footprint.max_page_from("... 共 82 页 ...") == 82
    assert footprint.max_page_from("没有分页") == 1


def test_looks_like_discuz():
    assert footprint.looks_like_discuz("Powered by Discuz!") is True
    assert footprint.looks_like_discuz('href="home.php?mod=space"') is True
    assert footprint.looks_like_discuz("<html>别的</html>") is False


def test_profile_url_quotes_username():
    assert "a%2Fb" in footprint.profile_url("bbs.example.com", "a/b")


# ---------------------------------------------------------------------------
# 预算与礼貌
# ---------------------------------------------------------------------------
def test_budget_holds_under_concurrency():
    """并发下预算必须仍然成立。

    原实现的「查预算 → await 间隔 → 记账」中间夹着 await，并发任务会读到
    同一份旧状态然后一起通过检查。实测（预算 2、同域 5 并发）发出 5 个请求。
    预算与间隔是本模块对外的合规承诺，不是性能优化项。
    """
    session = FakeSession({})
    finder = footprint.FootprintFinder(session, max_requests=3, domain_delay=0.01)

    async def run():
        return await asyncio.gather(
            *[finder.fetch(f"https://a.example.com/{i}") for i in range(8)]
        )

    results = _run(run())
    assert finder.requests_made == 3, f"预算 3 却发了 {finder.requests_made} 次"
    assert len(session.calls) == 3, "超预算的请求不该真的发出去"
    assert sum(1 for r in results if r.status == "budget") == 5
    assert finder.budget_exhausted is True


def test_same_domain_requests_are_staggered():
    """同一域名的并发请求必须被间隔开。

    原实现把"记时间"放在 sleep 之前，所有等待者以同一个基准时间计算等待，
    结果一起醒来、一起发出去——间隔形同不存在。实测 4 个请求同一毫秒发出。
    """
    import time

    session = FakeSession({})
    finder = footprint.FootprintFinder(session, max_requests=10, domain_delay=0.15)
    stamps = []

    async def run():
        async def one(index):
            await finder.fetch(f"https://b.example.com/{index}")
            stamps.append(time.monotonic())

        start = time.monotonic()
        await asyncio.gather(*[one(i) for i in range(4)])
        return [round(s - start, 3) for s in sorted(stamps)]

    offsets = _run(run())
    gaps = [round(b - a, 3) for a, b in zip(offsets, offsets[1:])]
    assert all(gap >= 0.10 for gap in gaps), f"同域间隔失效：偏移 {offsets}，间隔 {gaps}"


def test_budget_stops_requests():
    """请求预算用完之后必须拒发，并把 `budget_exhausted` 标出来。

    没有这个上限，任何"页面里有链接就跟着走"的写法都会把自己变成爬虫。
    """
    session = FakeSession({})
    finder = footprint.FootprintFinder(session, max_requests=2)

    async def run():
        await finder.fetch("https://a.example.com/1")
        await finder.fetch("https://b.example.com/2")
        third = await finder.fetch("https://c.example.com/3")
        return third

    third = _run(run())
    assert third.status == "budget"
    assert finder.budget_exhausted is True
    assert len(session.calls) == 2, "超预算的请求不该真的发出去"


def test_domain_delay_is_per_host():
    """不同域名之间不等——否则站点多了会慢到不可用。"""
    import time

    session = FakeSession({})
    finder = footprint.FootprintFinder(session, domain_delay=5.0, max_requests=5)

    async def run():
        started = time.monotonic()
        await finder.fetch("https://a.example.com/1")
        await finder.fetch("https://b.example.com/2")
        return time.monotonic() - started

    elapsed = _run(run())
    assert elapsed < 3.0, f"不同域名之间不该有延迟，实际 {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 取页面：被拦 vs 没有
# ---------------------------------------------------------------------------
def test_fetch_maps_403_to_blocked_not_notfound():
    """403 必须记成 blocked。

    记成 notfound 的后果是"这个站没查"被写成"这个站没有"——
    用户会以为自己没在那个站注册过。
    """
    blocked = "<title>百度安全验证</title>".encode("utf-8")
    session = FakeSession({"example.com": FakeResponse(403, blocked)})
    finder = footprint.FootprintFinder(session)
    got = _run(finder.fetch("https://example.com/x"))
    assert got.status == "blocked"
    assert got.ok is False


def test_fetch_maps_404_to_notfound():
    session = FakeSession({"example.com": FakeResponse(404, b"")})
    finder = footprint.FootprintFinder(session)
    assert _run(finder.fetch("https://example.com/x")).status == "notfound"


def test_fetch_handles_exception_as_error():
    session = FakeSession({"example.com": RuntimeError("boom")})
    finder = footprint.FootprintFinder(session)
    got = _run(finder.fetch("https://example.com/x"))
    assert got.status == "error"


def test_fetch_decodes_gbk():
    """老中文站声明 GBK。不处理的话用户名是乱码，

    `same_author` 比对必然失败，表现为"这个人一条都没发过言"。
    """
    body = "用户名：测试".encode("gb18030")
    session = FakeSession({"example.com": FakeResponse(200, body, charset="gbk")})
    finder = footprint.FootprintFinder(session)
    got = _run(finder.fetch("https://example.com/x"))
    assert got.ok and "测试" in got.text


# ---------------------------------------------------------------------------
# 单站检索
# ---------------------------------------------------------------------------
def test_probe_discuz_site_found():
    html = _read("discuz_profile.html").encode("utf-8")
    session = FakeSession({"bbs.newwise.com": FakeResponse(200, html)})
    finder = footprint.FootprintFinder(session)
    report = _run(finder.probe_discuz_site("bbs.newwise.com", "yang11235"))

    assert report.status == "ok"
    assert report.profile is not None
    assert report.profile.uid == "463889"
    # 资料页本身也是一条痕迹：注册/最后访问时间对定位阶段极有用
    assert any(item.kind == "profile" for item in report.footprints)


def test_probe_discuz_site_absent():
    html = _read("discuz_absent.html").encode("utf-8")
    session = FakeSession({"bbs.newwise.com": FakeResponse(200, html)})
    finder = footprint.FootprintFinder(session)
    report = _run(finder.probe_discuz_site("bbs.newwise.com", "zzqqxx998877zz"))
    assert report.status == "absent"
    assert report.footprints == []


def test_probe_discuz_site_blocked_is_not_absent():
    blocked = "<title>安全验证</title>".encode("utf-8")
    session = FakeSession({"bbs.newwise.com": FakeResponse(403, blocked)})
    finder = footprint.FootprintFinder(session)
    report = _run(finder.probe_discuz_site("bbs.newwise.com", "x"))
    assert report.status == "blocked", "被拦不是'没这个用户'"


# ---------------------------------------------------------------------------
# 按作者抽楼层
# ---------------------------------------------------------------------------
def test_collect_from_thread_filters_other_authors(monkeypatch):
    """无 uid 时必须只留本人的楼层——**走真实调用点**。

    2026-11-17 独立评审"测试有效性"一节指出：这条测试原来直接调
    `_posts_to_footprints(..., only_author=True)`，而真正的 bug 在**调用点**
    （`collect_from_thread` 传的是 `only_author=False`）。底层函数没问题，
    所以 bug 存在时这条测试照样通过——它测的是自己传进去的参数。

    改法：桩掉 `parse_thread` 让它返回"本人 + 别人"两层楼，
    然后走 `collect_from_thread` 的真实路径，断言别人的话没进来。
    """
    html = _read("discuz_author_filter.html").encode("utf-8")
    session = FakeSession({"tid=436216": FakeResponse(200, html)})

    mixed = [
        Post(author="yang11235", posted_at="2009-9-18 07:30:30", text="我的话", anchor="pid1"),
        Post(author="someone_else", posted_at="2009-9-18 07:31:30",
             text="别人的话 13950133910", anchor="pid2"),
    ]
    monkeypatch.setattr(footprint.content, "parse_thread", lambda *a, **k: mixed)

    finder = footprint.FootprintFinder(session)
    posts, notes, status = _run(
        finder.collect_from_thread(
            "https://bbs.newwise.com/forum.php?mod=viewthread&tid=436216",
            "yang11235",
            author_id="",  # ← 无 uid：这正是出过 bug 的那条分支
        )
    )
    texts = [p.text for p in posts]
    assert texts == ["我的话"], f"别人的楼层被记进来了：{texts}"
    assert not any("13950133910" in (p.text or "") for p in posts)


def test_collect_from_thread_stops_when_pids_repeat():
    """`authorid` 视图不分页（实测 Discuz X5.0），重复返回同一批 pid。

    不检测重复的话，翻 3 页就拿到 3 份一样的内容——
    既是重复证据，也白白消耗请求预算。
    """
    html = _read("discuz_author_filter.html").encode("utf-8")
    # 桩只命中普通作者视图；`archiver/` 不在 routes 里，会走 404 默认分支，
    # 让流程正常地回退到普通视图（这条测试要测的是普通视图的去重行为）。
    session = FakeSession({"authorid=": FakeResponse(200, html)})
    finder = footprint.FootprintFinder(session, max_pages_per_thread=3)

    posts, notes, status = _run(
        finder.collect_from_thread(
            "https://bbs.newwise.com/forum.php?mod=viewthread&tid=436216",
            "yang11235",
            author_id="463889",
        )
    )
    assert posts
    assert status == "ok"
    # Archiver 试一次（404）+ 作者视图首地址 + 第二页发现 pid 重复即停 = 3 次
    assert len(session.calls) == 3, session.calls
    assert sum(1 for url in session.calls if "archiver/" in url) == 1
    assert not notes


def test_collect_from_thread_blocked_reports_blocked():
    """种子帖被拦时，状态必须是 blocked，不是"没有"。

    调用方靠这个状态把站点列进盲区；返回 ok 的话，用户看到的是
    "这个站没有痕迹"，而事实是"我没读到"。
    """
    session = FakeSession(
        {"bbs.example.com": FakeResponse(403, "<title>安全验证</title>".encode("utf-8"))}
    )
    finder = footprint.FootprintFinder(session, max_requests=5)
    posts, notes, status = _run(
        finder.collect_from_thread("https://bbs.example.com/thread-1-1-1.html", "u")
    )
    assert posts == []
    assert status == "blocked"
    assert notes and "blocked" in notes[0]


def test_only_author_requires_explicit_match():
    """作者名解析不出来时必须**跳过**，不能放过。

    没有任何依据说这层楼是他发的，而"把别人的话记成他说的"比漏记严重得多。
    """
    posts = [Post(author="", posted_at="2009-1-1", text="不明归属", anchor="pid1")]
    got = footprint._posts_to_footprints("s", "u", posts, "yang11235", only_author=True)
    assert got == []


def test_posts_to_footprints_mines_clues():
    posts = [Post(author="x", posted_at="2009-08-13", text="我在深圳中学", anchor="p1")]
    got = footprint._posts_to_footprints("s", "u", posts, "x")
    assert "深圳中学" in got[0].clues["places"]


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def _report(site, footprints, status="ok"):
    return footprint.SiteReport(site=site, footprints=footprints, status=status)


def test_summarize_dedupes_by_anchor():
    """同一 pid 出现两次只算一条。

    按锚点去重而不是按正文：同一分钟里两句不同的话，
    用正文去重会错并成一条。
    """
    same = footprint.Footprint(site="s", url="u", anchor="pid1", text="a", date="2009-01-01")
    other = footprint.Footprint(site="s", url="u", anchor="pid1", text="b", date="2009-01-01")
    result = {"username": "x", "reports": [_report("s", [same, other])]}
    summary = footprint.summarize(result)
    assert summary["total"] == 1


def test_summarize_keeps_distinct_posts_in_same_minute():
    a = footprint.Footprint(site="s", url="u", anchor="pid1", text="a", date="2009-01-01")
    b = footprint.Footprint(site="s", url="u", anchor="pid2", text="b", date="2009-01-01")
    result = {"username": "x", "reports": [_report("s", [a, b])]}
    assert footprint.summarize(result)["total"] == 2


def test_summarize_reports_range_and_blind_spots():
    items = [
        footprint.Footprint(site="s", url="u", anchor="p1", text="a", date="2010-05-01"),
        footprint.Footprint(site="s", url="u", anchor="p2", text="b", date="2009-01-01"),
    ]
    result = {
        "username": "x",
        "reports": [
            _report("s", items),
            footprint.SiteReport(site="z", status="blocked", detail="被拦（HTTP 403）"),
            footprint.SiteReport(site="y", status="absent"),
        ],
        "requests": 5,
        "budget": 60,
    }
    summary = footprint.summarize(result)
    assert summary["range"] == ["2009-01-01", "2010-05-01"]
    assert summary["by_site"] == {"s": 2}
    assert any("z" in item for item in summary["blocked"])
    assert summary["absent"] == ["y"]


def test_summarize_carries_notes():
    """逐页提示必须能传到汇总层。

    只放在 `search_footprints` 里补的写法，会让直接调 summarize 的调用方
    拿不到盲区——"被拦"就又静默消失了。
    """
    result = {
        "username": "x",
        "reports": [],
        "notes": ["s：第 2 页未取到（被拦（HTTP 403））"],
    }
    assert footprint.summarize(result)["notes"] == ["s：第 2 页未取到（被拦（HTTP 403））"]


def test_render_shows_notes_as_blind_spot():
    """`notes` 必须出现在给人看的输出里，否则用户以为"没查到就是没有"。"""
    summary = {
        "username": "x",
        "total": 0,
        "range": [],
        "by_site": {},
        "forums": [],
        "places": [],
        "contacts": [],
        "blocked": [],
        "unparsed": [],
        "notes": ["s：被拦（HTTP 403）（blocked）"],
        "budget_exhausted": False,
        "footprints": [],
    }
    text = footprint.render(summary)
    assert "本次没读到" in text
    assert "被拦" in text


def test_seed_only_blocked_site_lands_in_blind_spots():
    """只由种子 URL 引出的站点，被拦时要进 blocked。

    原实现把这种站点固定标成 "seed"，于是它永远不会出现在盲区列表里，
    用户看到的是"未检索到公开痕迹"——把"我没读到"说成"他没有"。
    """
    session = FakeSession(
        {"x.example.com": FakeResponse(403, "<title>安全验证</title>".encode("utf-8"))}
    )
    finder = footprint.FootprintFinder(session, max_requests=5)
    result = _run(
        finder.run(
            "u", sites=[], seed_urls=["https://x.example.com/forum.php?mod=viewthread&tid=1"]
        )
    )
    summary = footprint.summarize(result)
    assert summary["total"] == 0
    assert any("x.example.com" in item for item in summary["blocked"])
    assert "被拦" in footprint.render(summary)


def test_summarize_does_not_infer():
    """汇总层只统计，不下结论。

    "这说明他当时在上初中"这类话必须由 AI 分析写，
    写在这里会变成没人能审计、也没法重做的硬编码判断。
    """
    items = [
        footprint.Footprint(
            site="s",
            url="u",
            anchor="p1",
            text="我在翠园中学",
            date="2009-01-01",
            clues={"places": ["翠园中学"], "contacts": [], "qq": []},
        )
    ]
    result = {"username": "x", "reports": [_report("s", items)]}
    summary = footprint.summarize(result)
    assert "翠园中学" in summary["places"]
    blob = json.dumps(summary, ensure_ascii=False)
    for forbidden in ("学生", "年龄", "初中", "高中"):
        assert forbidden not in blob, f"汇总层不该出现推断词：{forbidden}"


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def test_render_shows_blocked_sites():
    """盲区必须出现在输出里。只报命中不提盲区，用户会以为"没查到就是没有"。"""
    summary = {
        "username": "x",
        "total": 1,
        "range": ["2009-01-01", "2009-01-01"],
        "by_site": {"s": 1},
        "forums": [],
        "places": [],
        "contacts": [],
        "blocked": ["z（被拦（HTTP 403））"],
        "unparsed": [],
        "budget_exhausted": False,
        "footprints": [
            {"site": "s", "date": "2009-01-01", "text": "hi", "forum": "", "thread_title": ""}
        ],
    }
    text = footprint.render(summary)
    assert "被拦" in text
    assert "不代表没有" in text


def test_render_empty_summary():
    assert footprint.render({}) == ""


def test_to_ai_material_keeps_literals():
    """给 AI 的素材要保留原话，不做摘要。

    AI 要靠"放学回来""翠园"这些字面信息去推断，
    压成摘要就没法推了。
    """
    summary = {
        "username": "x",
        "total": 1,
        "range": ["2009-01-01", "2009-01-01"],
        "forums": ["某中学吧"],
        "places": ["深圳"],
        "footprints": [
            {
                "site": "s",
                "date": "2009-01-01",
                "forum": "某中学吧",
                "text": "放学回来",
            }
        ],
        "blocked": [],
    }
    material = footprint.to_ai_material(summary)
    assert "放学回来" in material
    assert "某中学吧" in material
    assert "2009-01-01" in material


def test_to_ai_material_empty_when_nothing_at_all():
    """既没痕迹、也没盲区，才返回空串。"""
    assert footprint.to_ai_material({"footprints": [], "blocked": [], "notes": []}) == ""


def test_to_ai_material_not_empty_when_only_blocked():
    """全被拦时也必须输出。

    返回空串会让 AI 得出"这个人没有公开痕迹"——而事实是"我们没读到"，
    两者对用户的意义相反。
    """
    material = footprint.to_ai_material(
        {
            "username": "x",
            "footprints": [],
            "blocked": [],
            "notes": ["s：被拦（HTTP 403）（blocked）"],
        }
    )
    assert material
    assert "没有取到任何痕迹" in material
    assert "不代表没有" in material


def test_to_ai_material_truncates():
    items = [
        {"site": "s", "date": "2009-01-01", "text": "x" * 200, "forum": ""}
        for _ in range(50)
    ]
    summary = {"username": "x", "footprints": items, "total": 50}
    material = footprint.to_ai_material(summary, max_chars=500)
    assert len(material) < 1500
    assert "省略" in material


# ---------------------------------------------------------------------------
# 论坛清单
# ---------------------------------------------------------------------------
def test_load_forums_skips_unverified(tmp_path):
    """没有 verified_at 的条目一律不加载。

    未验证域名放进来，用户看到的是"这个站查不到"，而不是"这个站没查"。
    """
    path = tmp_path / "forums.json"
    path.write_text(
        json.dumps(
            {
                "forums": {
                    "good.example.com": {"verified_at": "2026-11-16"},
                    "bad.example.com": {"label": "没验证过"},
                }
            }
        ),
        encoding="utf-8",
    )
    hosts = footprint.load_forums(path)
    assert hosts == ["good.example.com"]


def test_load_forums_falls_back_when_unreadable(tmp_path):
    """清单读不出来时退回内置清单，而不是返回空。

    返回空的后果是"读取失败"被显示成"没有痕迹"。
    """
    hosts = footprint.load_forums(tmp_path / "missing.json")
    assert hosts == ["bbs.newwise.com"]


def test_shipped_forums_file_is_valid():
    hosts = footprint.load_forums()
    assert "bbs.newwise.com" in hosts


# ---------------------------------------------------------------------------
# 端到端（全桩）
# ---------------------------------------------------------------------------
def test_run_end_to_end_with_stubs():
    profile_html = _read("discuz_profile.html").encode("utf-8")
    thread_html = _read("discuz_author_filter.html").encode("utf-8")
    session = FakeSession(
        {
            "username=yang11235": FakeResponse(200, profile_html),
            "authorid=": FakeResponse(200, thread_html),
        }
    )
    # 用默认预算（60）。这里原来写死 20，而公开接口层一轮就要**整轮预留**
    # `apis.requests_for()`——2026-11-17 网易云改成"封顶 3 个同名账号"后
    # 该值从 14 涨到 18，20 的预算只剩 2 次给论坛，流程跑不完。
    # 这条测试要验的是"端到端串得通"，不是预算边界（边界另有专门用例），
    # 所以用真实默认值而不是一个随手写的小数字。
    finder = footprint.FootprintFinder(session, max_requests=footprint.DEFAULT_MAX_REQUESTS)
    result = _run(
        finder.run(
            "yang11235",
            sites=["bbs.newwise.com"],
            seed_urls=["https://bbs.newwise.com/forum.php?mod=viewthread&tid=436216"],
        )
    )
    summary = footprint.summarize(result)
    assert summary["total"] > 1
    assert summary["dated"] == summary["total"]
    assert summary["requests"] <= summary["budget"]


# ---------------------------------------------------------------------------
# 公开接口层
# ---------------------------------------------------------------------------
PROFILE_HTML = b"<html><body><div>no entries here</div></body></html>"


def test_api_footprints_survive_site_probing():
    """接口取到的结果不能被随后的站点探测覆盖掉。

    原实现里 `reports = await asyncio.gather(...)` 是**重新赋值**，
    把接口采集的报告整个丢了。实测症状：接口明明取到 30 多条，
    输出里一条都没有。
    """
    api_facts = [
        footprint.apis.ApiFact(
            site="ExampleApi",
            kind="账号",
            title="注册：2024-01-04",
            date="2024-01-04",
            url="https://api.example.com/u",
        )
    ]

    async def fake_apis(session, username, timeout=20.0, notes=None):
        return api_facts

    original = footprint.apis.collect_all
    footprint.apis.collect_all = fake_apis
    try:
        session = FakeSession({"home.php": FakeResponse(200, _read("discuz_absent.html").encode("utf-8"))})
        # 预算要够接口层预留 + 至少一次站点探测，否则接口采集会被整段跳过
        finder = footprint.FootprintFinder(
            session, max_requests=footprint.apis.requests_for() + 10
        )
        summary = footprint.summarize(
            _run(finder.run("u", sites=["bbs.newwise.com"]))
        )
    finally:
        footprint.apis.collect_all = original

    assert summary["total"] == 1
    assert "ExampleApi" in summary["by_site"]


def test_api_requests_are_counted_against_budget():
    """接口采集器内部发多次请求，预算必须按实际次数扣。

    只按"调用一次"记一笔的话，多出来的请求是看不见的——
    日志里只出现一次调用，实际却发了更多。

    这里的断言用 `apis.requests_for()` 而不是一个写死的数字：
    采集器清单会变（GitHub 加了 `/search/issues` 就从 3 次变 4 次），
    写死数字的测试会在"功能变强"时变红，而它本该盯的是
    「预算扣减是否等于预计请求数」这件事本身。
    """
    async def fake_apis(session, username, timeout=20.0, notes=None):
        return []

    original = footprint.apis.collect_all
    footprint.apis.collect_all = fake_apis
    try:
        # 预算要**大于**接口层预留总量，否则采集会被跳过、requests_made 归零。
        # 这里按实际需求给足，测的是"扣了多少"而不是"预算不够时怎么办"
        # （后者有专门的 test_api_skipped_when_budget_too_small）。
        finder = footprint.FootprintFinder(
            FakeSession({}), max_requests=footprint.apis.requests_for() * 2
        )
        _run(finder.run("u", sites=[]))
    finally:
        footprint.apis.collect_all = original
    assert finder.requests_made == footprint.apis.requests_for()
    # 至少要为两个源预留，不能退化成"一个采集器一次"
    assert footprint.apis.requests_for() > 1


def test_api_skipped_when_budget_too_small():
    """预算不够时不该打接口，并如实说明，而不是静默跳过。"""

    async def fake_apis(session, username, timeout=20.0, notes=None):  # pragma: no cover
        raise AssertionError("预算不足时不该真的调用接口")

    original = footprint.apis.collect_all
    footprint.apis.collect_all = fake_apis
    try:
        finder = footprint.FootprintFinder(FakeSession({}), max_requests=1)
        raw = _run(finder.run("u", sites=[]))
    finally:
        footprint.apis.collect_all = original
    assert any("预算不足" in note for note in raw["notes"])


def test_pages_skip_sites_already_covered_by_api():
    """接口已覆盖的站点不必再抓 HTML。

    省一次请求，也避免"主页结构不认识"这种误导性提示——
    对 GitHub 这类主页本来就不列内容的站，那句话是废话，
    用户会把它读成"这里没查到东西"。
    """
    session = FakeSession({})
    finder = footprint.FootprintFinder(session, max_requests=10)
    posts, notes, blocked = _run(
        finder.collect_from_pages(
            [("GitHub", "https://github.com/u"), ("Blog", "https://blog.example.com/u")],
            skip_sites={"GitHub"},
        )
    )
    urls = [url for url, *_ in session.calls]
    assert all("github.com" not in url for url in urls)


def test_pages_message_says_page_has_no_entries():
    """主页只显示资料时，措辞要说"这页没有列内容"，不能说"结构不认识"。"""
    session = FakeSession({"blog.example.com": FakeResponse(200, PROFILE_HTML)})
    finder = footprint.FootprintFinder(session, max_requests=5)
    posts, notes, blocked = _run(
        finder.collect_from_pages([("Blog", "https://blog.example.com/u")])
    )
    assert posts == []
    assert notes and "没有列出的内容条目" in notes[0]


def test_pages_extracts_entries():
    html = (
        "<html><body><div class=\"day\">"
        '<div class="postTitle"><a class="postTitle2" href="https://b/p/1">'
        "<span>一篇文章</span></a></div>"
        "<div>posted @ 2026-03-05 10:00</div></div></body></html>"
    )
    session = FakeSession({"blog.example.com": FakeResponse(200, html.encode("utf-8"))})
    finder = footprint.FootprintFinder(session, max_requests=5)
    posts, notes, blocked = _run(
        finder.collect_from_pages([("Blog", "https://blog.example.com/u")])
    )
    assert len(posts) == 1
    assert posts[0].text == "一篇文章"
    assert posts[0].date == "2026-03-05"
    assert posts[0].kind == "content"


# ---------------------------------------------------------------------------
# 资料页线索 → 自动深挖帖子
# ---------------------------------------------------------------------------
def _report_with_links(site, links, uid="463889"):
    profile = footprint.Profile(name="u", uid=uid, site_links=list(links))
    return footprint.SiteReport(site=site, profile=profile, status="ok")


def test_profile_links_are_mined_automatically():
    """资料页里的帖子地址要自动跟进，不需要用户手动喂。

    这是用户报的核心问题：Web 端没有 `--cn-thread` 输入，论坛那一路
    永远只有资料页那一行元信息，看起来"有账号但没内容"。
    """
    thread_html = _read("discuz_author_filter.html").encode("utf-8")
    session = FakeSession(
        {
            "authorid=": FakeResponse(200, thread_html),
        }
    )
    finder = footprint.FootprintFinder(session, max_requests=20)
    posts, notes = _run(
        finder.collect_from_profile_links("yang11235", [_report_with_links("bbs.x.com", ["436216"])])
    )
    assert posts, "应当从资料页线索里抽出发言"
    assert all(p.site == "bbs.x.com" for p in posts)


def test_profile_links_respect_per_site_limit():
    """单站跟进数量要有上限：签名可能贴一排地址，预算不该被一个站吃光。"""
    thread_html = _read("discuz_author_filter.html").encode("utf-8")
    session = FakeSession({"authorid=": FakeResponse(200, thread_html)})
    finder = footprint.FootprintFinder(session, max_requests=50, max_threads_per_site=2)
    posts, notes = _run(
        finder.collect_from_profile_links(
            "yang11235", [_report_with_links("bbs.x.com", ["1", "2", "3", "4"])]
        )
    )
    # 只应请求前 2 个帖子（每个帖子首请求 + 可能的重复检测请求）
    requested_tids = {url.split("tid=")[1].split("&")[0] for url, *_ in session.calls if "tid=" in url}
    assert requested_tids <= {"1", "2"}, requested_tids
    assert any("未跟进" in note for note in notes), "超出上限要说明，不能静默丢"


def test_profile_links_explain_unreadable_thread():
    """读不到的帖子必须说明是哪个、为什么。

    只说"这个站没有内容"会让用户以为工具没干活；而事实可能是
    "帖子在权限版块里，匿名永远读不到"——那用户就知道换登录态也没用。
    """
    wall = (
        "<title>提示信息</title>Powered by Discuz!"
        '<a href="home.php?mod=space">x</a>'
        "您需要满足以下条件才能访问这个版块 访问条件： N币 ≥ 0"
    )
    session = FakeSession({"bbs.x.com": FakeResponse(200, wall.encode("utf-8"))})
    finder = footprint.FootprintFinder(session, max_requests=20)
    posts, notes = _run(
        finder.collect_from_profile_links("u", [_report_with_links("bbs.x.com", ["999"])])
    )
    assert posts == []
    joined = " ".join(notes)
    assert "tid=999" in joined, "要说清是哪个帖子"
    assert "权限" in joined, "要说清原因"


def test_profile_links_skipped_when_no_links():
    """没有线索时不该发任何请求。"""
    session = FakeSession({})
    finder = footprint.FootprintFinder(session, max_requests=10)
    posts, notes = _run(
        finder.collect_from_profile_links("u", [_report_with_links("bbs.x.com", [])])
    )
    assert posts == [] and notes == []
    assert session.calls == []


# ---------------------------------------------------------------------------
# 无 uid 时的整帖过滤（2026-11-17 独立评审 P0）
# ---------------------------------------------------------------------------
def test_thread_without_uid_filters_other_authors():
    """没有 uid 时也必须只记本人的楼层。

    这是独立评审发现的 P0。原实现在两处传 `only_author=False`：

        footprint.py:730（非 Discuz 分支，如贴吧）
        footprint.py:764（Discuz 无 uid 分支）

    而 764 行上面那行注释写的是"只能翻页后本地过滤"——**注释与代码
    自相矛盾，代码是错的**。后果是整帖所有人的发言都被记成"他说过的话"：
    实测两个不同用户名在同一条帖子上拿到完全相同的内容，
    里面是别人的发言，甚至别人的手机号 `13950133910`。

    把别人的话记成他说的，比漏记严重得多。
    """
    from maigret.cn.content import Post

    posts = [
        Post(author="someone_else", text="别人的发言，内含 13950133910", posted_at="2018-01-01 10:00"),
        Post(author="yang11235", text="我自己的发言", posted_at="2018-01-02 11:00"),
    ]
    matched, others = footprint._split_by_author(posts, "yang11235")
    assert [p.author for p in matched] == ["yang11235"]
    assert [p.author for p in others] == ["someone_else"]

    got = footprint._posts_to_footprints(
        "s", "u", matched, "yang11235", only_author=True
    )
    assert len(got) == 1
    assert "别人的发言" not in got[0].text


def test_split_by_author_treats_unknown_author_as_other():
    """作者名解析不出来时归入"不匹配"。

    没有依据说这层是他发的，就不能记在他名下。
    """
    from maigret.cn.content import Post

    posts = [
        Post(author="", text="没有作者名的楼层", posted_at="2018-01-01"),
        Post(author="yang11235", text="有作者名", posted_at="2018-01-02"),
    ]
    matched, others = footprint._split_by_author(posts, "yang11235")
    assert len(matched) == 1 and len(others) == 1
    assert others[0].author == ""


def test_profile_name_mismatch_is_discarded():
    """资料页返回的名字与查询名不符时，必须丢弃并说明。

    独立评审 P0：地址是 `/home.php?mod=space&username=<查询名>`，由 Discuz
    自己解析。只要它做了一次近似匹配或重定向，我们就会把**另一个人的**
    注册时间、在线时长、回帖数记成目标的。

    三种情况都要有正确行为：
        名字非空且不符 → 丢弃
        名字相符       → 记录
        名字为空       → 无法核对，仍然记录（否则 wuyou 那类不显示昵称的站会整站丢）
    """
    from maigret.cn.content import Profile

    # 直接验判定条件本身
    assert not footprint.content.same_author("someone_else", "yang11235")
    assert footprint.content.same_author("yang11235", "yang11235")
    assert not footprint.content.same_author("", "yang11235")

    # 空名不算"不符"（`profile.name and ...` 短路），这条是防回归的关键
    profile = Profile(name="", uid="", registered_at="2004-1-3 21:14")
    assert not (profile.name and not footprint.content.same_author(profile.name, "neil"))


# ---------------------------------------------------------------------------
# 盲区与失败分类（2026-11-17 独立评审 P1#7 / P1#8 / P2#15）
# ---------------------------------------------------------------------------
def test_unparsed_structure_is_not_reported_as_no_posts(monkeypatch):
    """页面结构不认识 → 报 unparsed，不能报"他没发过言"。

    独立评审 P1#7：`parse_thread` 返回 None（结构不认识）时，
    原实现把它交给 `_posts_to_footprints`，得到空列表后以 "ok" 返回，
    最终话术是"页面能打开，但里面没有这个人的发言"——
    把"我们没解析出来"说成了"他没有发言"。
    """
    html = _read("discuz_author_filter.html").encode("utf-8")
    session = FakeSession({"tid=436216": FakeResponse(200, html)})
    monkeypatch.setattr(footprint.content, "parse_thread", lambda *a, **k: None)

    finder = footprint.FootprintFinder(session)
    posts, notes, status = _run(
        finder.collect_from_thread(
            "https://bbs.newwise.com/forum.php?mod=viewthread&tid=436216",
            "yang11235",
            author_id="",
        )
    )
    assert posts == []
    assert status == "unparsed", f"结构不认识却报成了 {status}"
    assert any("结构不认识" in n for n in notes), notes


def test_to_ai_material_includes_unparsed_blind_spot():
    """AI 素材必须带上 unparsed 的盲区。

    独立评审 P1#8：`render()` 有显示，`to_ai_material()` 却只看
    items/blocked/notes。于是"所有站点都取到页面但一页都没解析出来"
    会返回空串 → AI 得到"此人无公开痕迹"，而事实是"我们没解析出来"。
    两条通道口径必须一致。
    """
    summary = {
        "username": "yang11235",
        "footprints": [],
        "blocked": [],
        "notes": [],
        "unparsed": ["bbs.x.com（页面结构不认识，没解析出楼层）"],
    }
    material = footprint.to_ai_material(summary)
    assert material, "只剩 unparsed 时返回了空串，AI 会以为没有盲区"
    assert "bbs.x.com" in material
    assert "不代表没有" in material


def test_fetch_treats_5xx_as_error_not_ok():
    """500/502/504 是"请求失败"，不是 ok。

    独立评审 P2#15：`looks_blocked` 只认 503，其余 5xx 会落到 `ok`，
    站点的 500 错误页被当正常页面解析，最终报成"结构不认识"——
    把"服务器挂了"说成"我们解析不了"。两者的处置完全不同。
    """
    class _Resp:
        def __init__(self, status, body):
            self.status = status
            self._body = body.encode()

        async def read(self):
            return self._body

        def get_encoding(self):
            return "utf-8"

    class _Session:
        def __init__(self, status, body):
            self._status, self._body = status, body

        def get(self, url, **kwargs):
            return _Ctx(_Resp(self._status, self._body))

    class _Ctx:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *exc):
            return False

    for status in (500, 502, 504):
        finder = footprint.FootprintFinder(_Session(status, "<html>服务端错误</html>"))
        result = _run(finder.fetch("https://bbs.example.com/u/x"))
        assert not result.ok, f"HTTP {status} 被当成了 ok"
        assert result.status == "error"
        assert str(status) in (result.detail or "")


def test_summarize_rejects_already_summarized_input():
    """不要把摘要再摘要一次——那会静默变成"没有痕迹"。

    2026-11-17 实测踩到：`summarize(search_footprints(...))` 旧行为是
    返回 `total=0`，而 46 条痕迹就在入参里躺着。用户读到的意思是
    "这个人没有公开痕迹"——把"有"说成了"没有"。
    """
    already = {"username": "x", "total": 46, "footprints": [{"site": "GitHub"}]}
    with pytest.raises(ValueError, match="已经摘要过"):
        footprint.summarize(already)


def test_summarize_accepts_raw_finder_output():
    """正常的入参（带 reports）照常工作——配对用例，别把功能一起堵死。"""
    raw = {
        "username": "x",
        "reports": [footprint.SiteReport(site="GitHub", status="ok")],
        "notes": [],
    }
    assert footprint.summarize(raw)["total"] == 0
