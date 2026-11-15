"""排除表测试（不联网）。

这一层是**安全敏感面**：一张远程排除表可以对安全结果做减法。被篡改的话，
"你的手机号确实挂在某个站上"这条结果会静默消失，而用户只会看到"没发现暴露"。

所以测试重点不是"能排除"，而是"该拒绝的必须拒绝"：

    - 明文 HTTP 的远程表整份不用
    - 版本号更高的表整份不用（看不懂的语义不能硬解释）
    - 规则数量超限整份不用（防止一次篡改排掉所有站点）
    - 远程失败必须降级，不能让整次自查跑不起来
    - 每条排除都留痕，不能静默删除
"""

import asyncio
import json

import pytest

from maigret.cn import exclusions as exc_mod
from maigret.cn.exclusions import (
    ExclusionFormatError,
    ExclusionRule,
    ExclusionTable,
    build,
    load_file,
    load_remote,
    parse,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def test_parse_object_form():
    rules = parse({"version": 1, "rules": [{"site": "Weibo", "username": "zhangwei"}]})

    assert len(rules) == 1
    assert rules[0].site == "Weibo"
    assert rules[0].username == "zhangwei"


def test_parse_bare_array_form():
    """贡献者提交的表两种形态都见过，都要认。"""
    rules = parse([{"site": "Douban"}])

    assert rules[0].site == "Douban"


def test_future_version_is_rejected_entirely():
    """版本号更高的表整份不用。

    未来格式可能改变了语义（比如把"排除"改成"降权"）。按老规则硬解释，
    会做出不可逆的错误排除——把真实命中静默吃掉。
    """
    with pytest.raises(ExclusionFormatError, match="版本"):
        parse({"version": 99, "rules": [{"site": "Weibo"}]})


def test_bad_single_rule_is_skipped_not_fatal():
    """表是人手写的，打错一个字段很常见，不该让整张表作废。"""
    rules = parse({"version": 1, "rules": [{"site": "Weibo"}, {"username": "x"}]})

    assert [r.site for r in rules] == ["Weibo"]


def test_non_dict_top_level_is_rejected():
    with pytest.raises(ExclusionFormatError):
        parse("just a string")


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------
def test_whole_site_rule_matches_every_username():
    table = ExclusionTable([ExclusionRule("Weibo", source="remote")])

    assert table.is_excluded("Weibo", username="anyone")
    assert not table.is_excluded("Zhihu", username="anyone")


def test_username_rule_only_matches_that_username():
    table = ExclusionTable([ExclusionRule("Weibo", username="zhangwei")])

    assert table.is_excluded("Weibo", username="zhangwei")
    assert not table.is_excluded("Weibo", username="someoneelse")


def test_url_prefix_rule():
    table = ExclusionTable([ExclusionRule("Douban", url_pattern="https://www.douban.com/people/*")])

    assert table.is_excluded("Douban", url="https://www.douban.com/people/zhangwei/")
    assert not table.is_excluded("Douban", url="https://www.douban.com/group/1/")


def test_site_match_is_case_insensitive():
    table = ExclusionTable([ExclusionRule("Weibo")])

    assert table.is_excluded("weibo")


def test_expired_rule_is_ignored():
    """排除表是临时兜底，不是永久豁免。

    没有过期机制的话，一张两年前的表会一直默默吃掉真实命中，
    而没人记得它为什么存在。
    """
    table = ExclusionTable([ExclusionRule("Weibo", until="2026-01-01")])

    assert not table.is_excluded("Weibo", today="2026-11-14")
    assert table.is_excluded("Weibo", today="2025-12-31")


def test_explain_returns_the_reason_not_just_a_bool():
    """说不清理由的排除，用户没法判断该不该信。"""
    rule = ExclusionRule("Weibo", username="zw", reason="该站改版后所有人都是 200", source="remote")
    table = ExclusionTable([rule])

    found = table.explain("Weibo", username="zw")

    assert found is rule
    assert "改版" in found.describe()
    assert "remote" in found.describe()


# ---------------------------------------------------------------------------
# 应用到结果
# ---------------------------------------------------------------------------
def test_apply_marks_instead_of_deleting():
    """不删除、只分组：被排除的条目连同原始字段一起留下，并附上命中说明。

    静默删除会让排除表变成黑箱——用户没法判断"我的手机号没被查出来"
    是真的没暴露，还是被谁排除了。
    """
    table = ExclusionTable([ExclusionRule("Weibo", reason="规则失效")])
    results = [
        {"site": "Weibo", "username": "zw", "url_user": "https://weibo.com/zw"},
        {"site": "Zhihu", "username": "zw"},
    ]

    outcome = table.apply(results)

    assert len(outcome["kept"]) == 1
    assert outcome["kept"][0]["site"] == "Zhihu"

    assert len(outcome["excluded"]) == 1
    dropped = outcome["excluded"][0]
    # 原始字段必须还在，否则审计线索就断了
    assert dropped["site"] == "Weibo"
    assert dropped["username"] == "zw"
    assert dropped["url_user"] == "https://weibo.com/zw"
    assert "规则失效" in dropped["excluded_by"]
    assert "local" in dropped["excluded_by"]


def test_apply_handles_site_object_form():
    """结果里的 site 有时是站点对象而不是字符串。"""
    table = ExclusionTable([ExclusionRule("Weibo")])
    results = [{"site": {"name": "Weibo"}, "username": "zw"}]

    outcome = table.apply(results)

    assert outcome["excluded"]


# ---------------------------------------------------------------------------
# 远程加载
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requested = []
        self.closed = False

    def get(self, url, timeout=None):
        self.requested.append(url)
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self):
        self.closed = True


def test_plain_http_remote_is_refused():
    """明文 HTTP 的排除表可以被链路上任何人改写，等于把结果开关交出去。"""
    session = FakeSession(FakeResponse(text='{"version":1,"rules":[{"site":"Weibo"}]}'))

    rules, warnings = run(
        load_remote("http://example.com/exclusions.json", cache_path="", session=session)
    )

    assert rules == []
    assert any("HTTPS" in w for w in warnings)
    assert session.requested == []


def test_remote_failure_falls_back_to_cache(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text('{"version":1,"rules":[{"site":"Weibo"}]}', encoding="utf-8")

    session = FakeSession(error=asyncio.TimeoutError())

    rules, warnings = run(
        load_remote(
            "https://example.com/e.json",
            cache_path=str(cache),
            ttl_hours=0,  # 强制回源，触发失败分支
            session=session,
        )
    )

    assert [r.site for r in rules] == ["Weibo"], "远程失败时必须用缓存兜底"
    assert warnings


def test_remote_failure_without_cache_returns_empty(tmp_path):
    """没有任何兜底时返回空表，而不是抛异常。

    排除表是可选增强，不该让一次自查因为它的网络问题跑不起来。
    """
    session = FakeSession(error=ConnectionError("boom"))

    rules, warnings = run(
        load_remote(
            "https://example.com/e.json",
            cache_path=str(tmp_path / "missing.json"),
            session=session,
        )
    )

    assert rules == []
    assert warnings


def test_oversized_remote_table_is_rejected(monkeypatch):
    """被篡改的表可以把所有站点全排掉，用户只会看到"没有发现暴露"。"""
    monkeypatch.setattr(exc_mod, "MAX_REMOTE_RULES", 2)
    payload = json.dumps(
        {"version": 1, "rules": [{"site": f"S{i}"} for i in range(5)]}
    )
    session = FakeSession(FakeResponse(text=payload))

    rules, warnings = run(
        load_remote("https://example.com/e.json", cache_path="", session=session)
    )

    assert rules == []
    assert any("上限" in w for w in warnings)


def test_fresh_cache_avoids_a_second_request(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text('{"version":1,"rules":[{"site":"Weibo"}]}', encoding="utf-8")
    session = FakeSession(FakeResponse(text="{}"))

    rules, _ = run(
        load_remote("https://example.com/e.json", cache_path=str(cache), session=session)
    )

    assert [r.site for r in rules] == ["Weibo"]
    assert session.requested == [], "缓存新鲜时不该再打远程"


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def test_local_rules_come_before_remote(tmp_path):
    """本地优先：远程表出问题时，用户有一份自己完全掌控的规则可以先命中。"""
    local = tmp_path / "local.json"
    local.write_text('{"version":1,"rules":[{"site":"Weibo","reason":"本地"}]}', encoding="utf-8")

    table = build(
        local_path=str(local),
        remote_rules=[ExclusionRule("Weibo", reason="远程", source="remote")],
    )

    rule = table.explain("Weibo")
    assert rule.source == "local"


def test_missing_local_file_is_not_an_error(tmp_path):
    table = build(local_path=str(tmp_path / "nope.json"))

    assert len(table) == 0
    assert table.warnings == []


def test_broken_local_file_is_ignored(tmp_path):
    local = tmp_path / "bad.json"
    local.write_text("{not json", encoding="utf-8")

    assert load_file(str(local)) == []


def test_default_table_is_empty_until_something_is_verified():
    """内置表只放实测确认的假阳性，不凭印象预先排除。

    一个凭印象写出来的排除表，会把真实命中当误报吃掉，而且没人会去查。
    """
    assert exc_mod.DEFAULT_RULES == []
