"""公开接口采集测试（`maigret/cn/apis.py`）。

全桩，不联网：联网测试会在 GitHub 上跑，还会因为速率限制随机变红。
夹具是 2026-11-16 从 api.github.com 真实响应裁剪出来的字段。
"""

import asyncio
import json

import pytest

from maigret.cn import apis


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params or {}))
        for key, payload in self.routes.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(200, payload)
        return FakeResponse(404, {})


def _run(coro):
    return asyncio.run(coro)


PROFILE = {
    "login": "yang11235",
    "created_at": "2024-01-04T08:26:20Z",
    "updated_at": "2026-09-17T05:02:18Z",
    "public_repos": 4,
    "followers": 3,
    "html_url": "https://github.com/yang11235",
    "name": None,
    "bio": None,
    "location": None,
}

REPOS = [
    {
        "name": "weread.koplugin",
        "created_at": "2026-10-17T03:35:36Z",
        "pushed_at": "2026-10-18T12:18:00Z",
        "html_url": "https://github.com/yang11235/weread.koplugin",
        "description": "微信读书 koreader 插件",
        "fork": False,
    },
    {
        "name": "forked-thing",
        "created_at": "2026-01-01T00:00:00Z",
        "html_url": "https://github.com/yang11235/forked-thing",
        "description": "fork 来的",
        "fork": True,
    },
]

EVENTS = [
    {
        "type": "PushEvent",
        "created_at": "2026-11-15T14:14:21Z",
        "repo": {"name": "yang11235/username-exposure-selfcheck"},
    },
    {
        "type": "WatchEvent",
        "created_at": "2026-11-16T05:20:20Z",
        "repo": {"name": "soxoj/maigret"},
    },
]

# `/search/issues?q=author:<u>` 的响应。字段截取自 2026-11-16 的真实调用
# （`author:yang11235` 共 7 条，这里留 3 条：两条 issue、一条 PR）。
ISSUES = {
    "total_count": 3,
    "items": [
        {
            "title": "[Feature] 书架支持多选批量下载",
            "created_at": "2026-09-17T05:21:41Z",
            "html_url": "https://github.com/finlater/weread.koplugin/issues/172",
            "state": "open",
        },
        {
            "title": "feat(read_report): expose reporting interval presets",
            "created_at": "2026-10-17T03:40:34Z",
            "html_url": "https://github.com/finlater/weread.koplugin/pull/210",
            "state": "merged",
            # 有 pull_request 说明这是 PR，不是 issue
            "pull_request": {"url": "https://api.github.com/repos/x/y/pulls/210"},
        },
        {
            "title": "fix(web): build each per-username report from its own results",
            "created_at": "2026-11-14T09:47:58Z",
            "html_url": "https://github.com/soxoj/maigret/pull/3392",
            "state": "merged",
            "pull_request": {"url": "https://api.github.com/repos/x/y/pulls/3392"},
        },
    ],
}


# ---------------------------------------------------------------------------
# 日期归一
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2024-01-04T08:26:20Z", "2024-01-04"),
        ("2026-11-16T05:20:20Z", "2026-11-16"),
        ("", ""),
        # 认不出来必须空串：时间线靠它排序，编一个值会造出不存在的活动
        ("not a date", ""),
        ("2024-13-45T00:00:00Z", ""),
        (None, ""),
    ],
)
def test_iso_date(raw, expected):
    assert apis._iso_date(raw) == expected


# ---------------------------------------------------------------------------
# GitHub 采集
# ---------------------------------------------------------------------------
def test_collect_github_builds_timeline():
    session = FakeSession(
        {
            "/users/yang11235/repos": REPOS,
            "/users/yang11235/events/public": EVENTS,
            "/search/issues": ISSUES,
            "/users/yang11235": PROFILE,
        }
    )
    facts = _run(apis.collect_github(session, "yang11235"))
    kinds = {f.kind for f in facts}
    assert "账号" in kinds and "仓库" in kinds and "动态" in kinds

    account = next(f for f in facts if f.kind == "账号")
    assert account.date == "2024-01-04"


def test_collect_github_includes_issue_and_pr_titles():
    """`author:` 搜索给的是"他自己写的标题"，这是唯一的内容层来源。

    区分 issue 与 PR：两者反映的参与程度不同，混成一个桶就看不出在做什么。
    """
    session = FakeSession(
        {
            "/users/x/repos": [],
            "/users/x/events/public": [],
            "/search/issues": ISSUES,
            "/users/x": dict(PROFILE, login="x"),
        }
    )
    facts = _run(apis.collect_github(session, "x"))
    issues = [f for f in facts if f.kind == "议题"]
    prs = [f for f in facts if f.kind == "PR"]

    assert len(issues) == 1
    assert len(prs) == 2
    # 标题原样保留，并带上仓库名（不然报告里看不出他在给谁提）
    assert "书架支持多选批量下载" in issues[0].title
    assert "finlater/weread.koplugin" in issues[0].title
    assert issues[0].date == "2026-09-17"


def test_collect_github_skips_forks():
    """fork 来的仓库不是他写的，不该记成他的作品。"""
    session = FakeSession(
        {
            "/users/x/repos": REPOS,
            "/users/x/events/public": [],
            "/users/x": dict(PROFILE, login="x"),
        }
    )
    facts = _run(apis.collect_github(session, "x"))
    names = [f.title for f in facts if f.kind == "仓库"]
    assert "weread.koplugin" in names
    assert "forked-thing" not in names


def test_collect_github_missing_user_returns_empty():
    """用户不存在时返回空，不能编出事实。"""
    session = FakeSession({"/users/x": {"message": "Not Found"}})
    assert _run(apis.collect_github(session, "x")) == []


def test_collect_github_tolerates_partial_failure():
    """仓库接口挂了，账号与动态仍要拿到。

    整体失败会丢掉已经取到的数据，而那部分数据本身是完好的。
    """
    session = FakeSession(
        {
            "/users/x/repos": RuntimeError("boom"),
            "/users/x/events/public": EVENTS,
            "/users/x": dict(PROFILE, login="x"),
        }
    )
    facts = _run(apis.collect_github(session, "x"))
    assert any(f.kind == "账号" for f in facts)
    assert any(f.kind == "动态" for f in facts)


def test_collect_all_survives_collector_crash(monkeypatch):
    """单个采集器抛异常不该让整轮采集失败。"""

    async def boom(session, username, timeout=20.0):
        raise RuntimeError("boom")

    monkeypatch.setattr(apis, "collect_github", boom)
    assert _run(apis.collect_all(FakeSession({}), "x")) == []


def test_requests_per_collector_matches_actual_calls():
    """预算核算用的数字必须与实际请求数一致。

    不一致的后果是"看不见的额外请求"：日志里只出现一次调用，
    实际却发出更多请求，预算形同虚设。

    断言的是**这个采集器**自己的请求数（`_REQUESTS["collect_github"]`），
    不是全局的 `REQUESTS_PER_COLLECTOR` ——后者是多源里的最大值，
    拿它去校验单个源会在"加了网易云但请求更少"时误报。
    """
    session = FakeSession(
        {
            "/users/x/repos": REPOS,
            "/users/x/events/public": EVENTS,
            "/search/issues": ISSUES,
            "/users/x": dict(PROFILE, login="x"),
        }
    )
    _run(apis.collect_github(session, "x"))
    assert len(session.calls) == apis._REQUESTS["collect_github"]


def test_github_requests_are_anonymous():
    """不构造需要 token 的请求：本项目不做需要用户授权的数据获取。"""
    session = FakeSession(
        {
            "/users/x/repos": [],
            "/users/x/events/public": [],
            "/users/x": dict(PROFILE, login="x"),
        }
    )
    _run(apis.collect_github(session, "x"))
    for _url, params in session.calls:
        assert "token" not in json.dumps(params).lower()


# ---------------------------------------------------------------------------
# 网易云：昵称搜索 → uid → 歌单
# ---------------------------------------------------------------------------
# 夹具取自 2026-11-16 的真实响应，字段按原样保留（含近似账号，用来
# 验证"必须逐字相等"这条过滤）。

NETEASE_SEARCH = {
    "result": {
        "userprofileCount": 3,
        "userprofiles": [
            {"nickname": "yang11235", "userId": 1591220, "province": 110000},
            # 这两个是近似账号，昵称多一位数字。不逐字比对就会把它们算进来。
            {"nickname": "yang112357", "userId": 1402938781, "province": 230000},
            {"nickname": "yang112358", "userId": 94679788, "province": 130000},
        ],
    }
}

NETEASE_DETAIL = {
    "code": 200,
    "level": 9,
    "listenSongs": 11161,
    "createDays": 4695,
    "createTime": -1,  # 实测老账号返回 -1，不能换算成 1969 年
    "profile": {"userId": 1591220, "nickname": "yang11235"},
}

NETEASE_PLAYLISTS = {
    "more": False,
    "playlist": [
        {
            "id": 1933490,
            "name": "我喜欢的音乐",
            "createTime": 1371543339707,  # 2013-06-18
            "trackCount": 232,
            "creator": {"userId": 1591220},
        },
        {
            "id": 999,
            "name": "德沃夏克",
            "createTime": 1474766400000,  # 2016-09-25
            "trackCount": 42,
            "creator": {"userId": 1591220},
        },
    ],
}


def test_collect_netease_exact_nickname_only():
    """昵称必须逐字相等。近似账号的属地（黑龙江/河北）不能进他的画像。"""
    session = FakeSession(
        {
            "/api/search/get/web": NETEASE_SEARCH,
            "/api/v1/user/detail/1591220": NETEASE_DETAIL,
            "/api/user/playlist/": NETEASE_PLAYLISTS,
        }
    )
    facts = _run(apis.collect_netease(session, "yang11235"))

    # 只认同一个 uid：不能出现 1402938781 / 94679788 的数据
    detail_calls = [url for url, _ in session.calls if "/api/v1/user/detail/" in url]
    assert detail_calls == ["https://music.163.com/api/v1/user/detail/1591220"]
    assert facts


def test_collect_netease_playlist_create_times_become_timeline():
    """歌单创建时间是核心产出：一个 2013 年的歌单说明 2013 年就在用。"""
    session = FakeSession(
        {
            "/api/search/get/web": NETEASE_SEARCH,
            "/api/v1/user/detail/1591220": NETEASE_DETAIL,
            "/api/user/playlist/": NETEASE_PLAYLISTS,
        }
    )
    facts = _run(apis.collect_netease(session, "yang11235"))
    dates = {f.date for f in facts if f.kind == "歌单"}
    assert "2013-06-18" in dates
    assert "2016-09-25" in dates

    # 最早的歌单单独成一条"起点"，它是注册时间的可信近似
    origin = next(f for f in facts if f.kind == "起点")
    assert origin.date == "2013-06-18"


def test_collect_netease_returns_nothing_when_no_exact_match():
    """搜到的全是近似账号时必须返回空，不能"最像的那个就是"。"""
    session = FakeSession(
        {
            "/api/search/get/web": {
                "result": {
                    "userprofiles": [
                        {"nickname": "yang112357", "userId": 1402938781},
                    ]
                }
            }
        }
    )
    assert _run(apis.collect_netease(session, "yang11235")) == []


def test_collect_netease_skips_playlist_owned_by_someone_else():
    """歌单接口理论上只回本人的，但 `creator.userId` 是可核对的事实。

    混进别人的歌单，报告里就会出现"他在 2019 年建了这个歌单"——
    而那是另一个人的。
    """
    session = FakeSession(
        {
            "/api/search/get/web": NETEASE_SEARCH,
            "/api/v1/user/detail/1591220": NETEASE_DETAIL,
            "/api/user/playlist/": {
                "playlist": [
                    {
                        "id": 1,
                        "name": "别人的歌单",
                        "createTime": 1371543339707,
                        "creator": {"userId": 999999},
                    },
                    {
                        "id": 2,
                        "name": "自己的歌单",
                        "createTime": 1474766400000,
                        "creator": {"userId": 1591220},
                    },
                ]
            },
        }
    )
    facts = _run(apis.collect_netease(session, "yang11235"))
    names = " ".join(f.title for f in facts)
    assert "别人的歌单" not in names
    assert "自己的歌单" in names


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1371543339707, "2013-06-18"),   # 毫秒
        (1474766400, "2016-09-25"),      # 秒
        (-1, ""),                        # 实测老账号返回 -1，不能变 1969
        (0, ""),
        (None, ""),
        ("abc", ""),
    ],
)
def test_epoch_ms_date(raw, expected):
    assert apis._epoch_ms_date(raw) == expected


def _full_session():
    """覆盖全部采集器的桩，且按**最坏情况**喂数据。

    最坏情况指：网易云搜到多个同名账号（触发封顶）、Discourse 用满请求上限。
    预算预留必须 >= 这个最坏值，否则实际请求会超出预留——那种超发在
    日志与报告里都看不见（只出现一次调用）。
    """
    # 网易云：3 个同名账号 = 搜索 1 次 + 每账号（详情 + 歌单）2 次
    netease_accounts = [
        {"userId": 111 + i, "nickname": "lurker", "province": "广东", "city": "深圳"}
        for i in range(apis.NETEASE_MAX_ACCOUNTS)
    ]
    routes = {
        # GitHub（5 次）
        "/users/lurker/repos": [],
        "/users/lurker/events/public": [],
        "/users/lurker/starred": [],
        "/search/issues": {"total_count": 0, "items": []},
        "/users/lurker": dict(PROFILE, login="lurker"),
        # 网易云
        "/api/search/get/web": {"result": {"userprofiles": netease_accounts}},
        "/api/v1/user/detail/": {"level": 1, "listenSongs": 1, "createTime": 1},
        "/api/user/playlist/": {"playlist": []},
        # Discourse：给了主题才算"读到了"，避免走到"未创建过主题"分支而少发请求
        "/topics/created-by/": {
            "topic_list": {"topics": [
                {"id": i, "slug": f"t{i}", "title": f"主题{i}",
                 "created_at": "2023-01-01T00:00:00Z", "posts_count": 1}
                for i in range(3)
            ]},
            "users": [{"username": "lurker"}],
        },
        "/u/lurker.json": {"user": {"username": "lurker", "created_at": "2023-01-01T00:00:00Z"}},
    }
    return FakeSession(routes)


def test_requests_for_counts_every_collector():
    """预算预留不能少于采集器**实际会发出**的请求数。

    2026-11-17 独立评审"测试有效性"一节指出：这条测试原来只断言
    `total == sum(_REQUESTS.values())`——用左边的定义去验左边的定义，
    是个同义反复，`_REQUESTS` 里写多少它都通过。

    改成：真跑一遍所有采集器（桩 session 记下每次请求），
    把**实际请求数**与预留值比。写得比实际小的那一条会立刻暴露
    （原来网易云写 3、实际最多 7 就是这么漏掉的）。
    """
    session = _full_session()
    facts = _run(apis.collect_all(session, "lurker"))
    assert facts is not None  # 跑通即可，本用例只关心请求数

    actual = len(session.calls)
    reserved = apis.requests_for()
    assert reserved >= actual, (
        f"预留 {reserved} 次，实际发出 {actual} 次——预留少于实际就会静默超预算。"
        f"实际请求：{session.calls}"
    )

    # 每个采集器都要有登记，漏登记的会退回保守值而不是被静默忽略
    for collector in apis._collectors():
        assert collector.__name__ in apis._REQUESTS

    # 网易云的请求数随同名账号数增长，必须与封顶值对齐
    assert apis._REQUESTS["collect_netease"] == 1 + 2 * apis.NETEASE_MAX_ACCOUNTS


def test_collect_all_survives_one_source_failing():
    """一个源挂掉不该拖垮整轮。网易云是新加的源，要能独立失败。"""
    session = FakeSession(
        {
            "/users/x/repos": [],
            "/users/x/events/public": [],
            "/search/issues": {"total_count": 0, "items": []},
            "/users/x": dict(PROFILE, login="x"),
            "/api/search/get/web": RuntimeError("netease down"),
        }
    )
    facts = _run(apis.collect_all(session, "x"))
    # GitHub 那部分照样在
    assert any(f.site == "GitHub" for f in facts)


# ---------------------------------------------------------------------------
# Discourse：一条实现覆盖一整类论坛
# ---------------------------------------------------------------------------
# 夹具取自 2026-11-16 对 meta.appinn.net 的真实响应。
# 关键点：`/topics/created-by/<u>.json` 能列出某人创建过的主题，
# 这是 Discuz 服务端禁用掉的那个能力（见 docs/footprint.md）。

DISCOURSE_PROFILE = {
    "user": {
        "username": "meta",
        "created_at": "2020-12-26T10:57:16.227Z",
        "last_seen_at": "2020-12-28T06:50:45.154Z",
        "last_posted_at": "2020-12-27T14:33:40.198Z",
        "time_read": 3707,
        "trust_level": 1,
    }
}

DISCOURSE_TOPICS = {
    "topic_list": {
        "topics": [
            {
                "id": 12345,
                "slug": "legendary-replace-epic",
                "title": "Legendary 替代 Epic Games Launcher",
                "created_at": "2020-12-26T12:04:30.090Z",
                "posts_count": 10,
            },
            {
                "id": 12346,
                "slug": "another-topic",
                "title": "另一条主题的标题",
                "created_at": "2021-03-01T08:00:00.000Z",
                "posts_count": 3,
            },
        ]
    }
}


class DiscourseSession(FakeSession):
    """按 host 路由的假会话：Discourse 采集器要跨多个站。"""

    def __init__(self, routes):
        super().__init__(routes)

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params or {}))
        for key, payload in self.routes.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(200, payload)
        # 默认 404：模拟"没有这个人"
        return FakeResponse(404, {"errors": ["not found"]})


def test_discourse_collects_topics_and_profile():
    """同时拿到资料页与主题列表时，两样都要产出。"""
    session = DiscourseSession(
        {
            "meta.appinn.net/topics/created-by/meta.json": DISCOURSE_TOPICS,
            "meta.appinn.net/u/meta.json": DISCOURSE_PROFILE,
        }
    )
    facts = _run(apis.collect_discourse(session, "meta"))
    kinds = {f.kind for f in facts}
    assert "主题" in kinds and "账号" in kinds

    topics = [f for f in facts if f.kind == "主题"]
    assert len(topics) == 2
    assert topics[0].title == "Legendary 替代 Epic Games Launcher"
    assert topics[0].date == "2020-12-26"
    # 详情链接要能直接点开，slug 与 id 都在
    assert "/t/legendary-replace-epic/12345" in topics[0].url


def test_discourse_skips_profile_for_robots_restricted_hosts():
    """robots 禁 `/u/` 的站只取主题列表，不发资料页请求。

    实测 4 个站（openKylin / Jittor / Rancher / Roblox）是这种形态：
    `Disallow: /u/` 但允许 `/topics/created-by/`。
    多发的那个请求是越界，不是"多拿点数据"。
    """
    session = DiscourseSession(
        {"meta.appinn.net/topics/created-by/x.json": {"topic_list": {"topics": []}}}
    )
    _run(apis.collect_discourse(session, "x"))
    profile_calls = [url for url, _ in session.calls if "/u/" in url]
    allowed_hosts = {
        host for host, (_label, allow) in apis.DISCOURSE_SITES.items() if allow
    }
    for url in profile_calls:
        assert any(host in url for host in allowed_hosts), url


def test_discourse_skips_builtin_accounts():
    """system / admin 这类内置账号跨站普遍存在，命中必然是别人的站务公告。

    实测 `system` 在 7 个站命中、共 90 条主题（「提问前请先阅读」
    「2024 年度回顾」）。不跳过的话，用户搜 `system` 会看到
    "这个人在 7 个技术论坛都是管理员"——而那 7 个账号分属 7 个运营方。
    """
    session = DiscourseSession({})
    for name in ("system", "admin", "System", "ADMIN"):
        assert _run(apis.collect_discourse(session, name)) == []
    # 一个请求都没发：跳过发生在发请求之前
    assert session.calls == []


def test_discourse_requires_exact_username_match():
    """个别站会对 `/u/xxx.json` 做模糊匹配，昵称不等就**整站不认**。

    2026-11-17 独立评审"测试有效性"一节指出：这条原来的 topics 是**空列表**，
    所以"边解析主题边写入、校验失败也没撤回"这个 bug 完全测不到——
    没有主题可写，当然也就没有主题留在结果里。

    现在把 topics 填成非空（这条路径真实存在，模糊匹配时它同样会返回别人的主题），
    并且断言"主题也一起被丢弃"，而不只是"资料页没产出"。
    """
    session = DiscourseSession(
        {
            "meta.appinn.net/topics/created-by/meta.json": {
                "topic_list": {"topics": [
                    {"id": 1, "slug": "s", "title": "其实是别人的主题",
                     "created_at": "2021-01-01T00:00:00Z", "posts_count": 1}
                ]}
            },
            "meta.appinn.net/u/meta.json": {
                "user": {"username": "metaphor", "created_at": "2020-01-01T00:00:00Z"}
            },
        }
    )
    facts = _run(apis.collect_discourse(session, "meta"))
    assert facts == [], f"身份校验不过却产出了内容：{[f.title for f in facts]}"


def test_discourse_absent_account_produces_nothing():
    """所有站都 404 时产出为空（而不是编一条"未注册"）。"""
    session = DiscourseSession({})
    assert _run(apis.collect_discourse(session, "zzqqxx998877zz")) == []


def test_discourse_reports_no_topics_when_profile_exists():
    """资料页存在但主题列表为空 —— 这是"他真的没发过"，要能如实说。

    不能和"没查到"混为一谈：前者是事实，后者是我们的失败。
    """
    session = DiscourseSession(
        {
            "meta.appinn.net/topics/created-by/lurker.json": {"topic_list": {"topics": []}},
            "meta.appinn.net/u/lurker.json": {
                "user": {"username": "lurker", "created_at": "2022-05-05T00:00:00Z"}
            },
        }
    )
    facts = _run(apis.collect_discourse(session, "lurker"))
    notes = [f for f in facts if f.title == "未创建过主题"]
    assert notes
    assert notes[0].date == "2022-05-05"


def test_discourse_respects_request_cap():
    """请求总量不得超过封顶值：请求预算是所有源共用的。

    实测过教训：封顶设 18（9 站 × 2 路径）时，接口层一次预留把 60 的
    预算吃掉三分之一，后面的论坛资料页与种子请求拿不到配额，
    症状是"接口有数据，痕迹检索却一条论坛内容都没有"。
    """
    routes = {}
    for host in apis.DISCOURSE_SITES:
        routes[f"https://{host}/topics/created-by/x.json"] = {"topic_list": {"topics": []}}
        routes[f"https://{host}/u/x.json"] = {"user": {"username": "x"}}
    session = DiscourseSession(routes)
    _run(apis.collect_discourse(session, "x"))
    assert len(session.calls) <= apis.DISCOURSE_REQUEST_CAP


def test_discourse_failed_topic_request_is_not_reported_as_no_topics():
    """主题列表**读失败**时不能说"未创建过主题"。

    2026-11-17 独立评审 P1（这条是我自己引入的）。

    `get_json` 失败返回 None。原来的实现只用一个 `found_topic` 布尔值，
    于是"请求超时/被限流"和"他真的没发过"混成一种，都会输出：

        未创建过主题 ／ 资料页存在，主题列表为空

    这是**把没读到说成没有**——本项目反复强调最不能犯的错。
    """
    session = DiscourseSession(
        {
            # 主题列表：模拟请求失败（路由里放异常）
            "meta.appinn.net/topics/created-by/lurker.json": RuntimeError("timeout"),
            # 资料页正常
            "meta.appinn.net/u/lurker.json": {
                "user": {"username": "lurker", "created_at": "2022-05-05T00:00:00Z"}
            },
        }
    )
    facts = _run(apis.collect_discourse(session, "lurker"))
    titles = [f.title for f in facts]
    assert "未创建过主题" not in titles, "读失败被误报成了『他真的没发过』"
    # 资料页本身仍然产出（账号存在是读到了的）
    assert any("信任等级" in t or t == "账号资料" for t in titles)


def test_discourse_empty_topic_list_is_reported_as_no_topics():
    """真的读到了、且确实是空列表 → 这时才可以说"未创建过主题"。

    与上一条配对：两种情况的输出必须不同，否则"没读到"和"没发过"又混了。
    """
    session = DiscourseSession(
        {
            "meta.appinn.net/topics/created-by/lurker.json": {"topic_list": {"topics": []}},
            "meta.appinn.net/u/lurker.json": {
                "user": {"username": "lurker", "created_at": "2022-05-05T00:00:00Z"}
            },
        }
    )
    facts = _run(apis.collect_discourse(session, "lurker"))
    titles = [f.title for f in facts]
    assert "未创建过主题" in titles


def test_discourse_topics_dropped_when_identity_check_fails():
    """身份校验不过时，**主题也要一起丢弃**。

    2026-11-17 独立评审 P0。原实现是边解析主题边写入 facts，而身份校验
    （`/u/<u>.json` 的 username 比对）在之后才做。只要站点对用户名做了
    模糊匹配，就会把**另一个人的**主题记成目标的，而且已经写进结果、无法挽回。

    构造：主题列表正常返回（含一条主题），但资料页返回的 username 是别人。
    期望：主题一条都不出。
    """
    session = DiscourseSession(
        {
            # 主题列表：有内容，且是"模糊匹配"出来的别人的主题
            "meta.appinn.net/topics/created-by/meta.json": {
                "topic_list": {
                    "topics": [
                        {
                            "id": 999,
                            "slug": "someone-elses-topic",
                            "title": "别人的主题",
                            "created_at": "2021-01-01T00:00:00Z",
                            "posts_count": 1,
                        }
                    ]
                }
            },
            # 资料页返回的是另一个人
            "meta.appinn.net/u/meta.json": {
                "user": {"username": "metaphor", "created_at": "2020-01-01T00:00:00Z"}
            },
        }
    )
    facts = _run(apis.collect_discourse(session, "meta"))
    assert facts == [], f"身份校验不过却产出了内容：{[f.title for f in facts]}"


def test_discourse_topics_kept_when_identity_check_passes():
    """校验通过时主题照常产出——与上一条配对，确认修复没有把正常路径也堵死。"""
    session = DiscourseSession(
        {
            "meta.appinn.net/topics/created-by/meta.json": DISCOURSE_TOPICS,
            "meta.appinn.net/u/meta.json": DISCOURSE_PROFILE,
        }
    )
    facts = _run(apis.collect_discourse(session, "meta"))
    assert any(f.kind == "主题" for f in facts)


def test_collect_all_reports_failed_sources():
    """某个源采集失败必须说出去，不能静默返回空。

    2026-11-17 独立评审："单一数据源失败无声"。

    实测踩过：一次瞬时网络故障让整轮返回 0 条，而报告与 AI 素材
    **一个字都没提"没读到"**——用户读到的意思是"这个人没有公开痕迹"，
    而事实是"我们这次没读到 GitHub"。两者对用户的意义相反。
    """
    session = FakeSession(
        {
            "/api/search/get/web": RuntimeError("netease down"),
            "/users/lurker": dict(PROFILE, login="lurker"),
            "/users/lurker/repos": [],
            "/users/lurker/events/public": [],
            "/users/lurker/starred": [],
            "/search/issues": {"total_count": 0, "items": []},
        }
    )
    notes: list = []
    _run(apis.collect_all(session, "lurker", notes=notes))
    assert notes, "源失败却没有产生任何提示"
    assert any("网易云" in n for n in notes), notes
    assert any("未读到" in n for n in notes), notes


def test_collect_all_without_notes_param_still_works():
    """不传 notes 时行为不变（向后兼容）。"""
    session = FakeSession({"/api/search/get/web": RuntimeError("netease down")})
    assert _run(apis.collect_all(session, "lurker")) == []
