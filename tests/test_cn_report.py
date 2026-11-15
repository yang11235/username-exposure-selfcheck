"""自查结论汇总测试（纯计算，无网络）。

这是用户唯一会读的一层，所以它要守的规矩比别的模块更多：

    1. **顺序即优先级**：风险必须在身份簇和盲区之前出现
    2. **盲区必须显式出现**：只报命中、不提未判定，用户会以为"没查到的就是没有"
    3. **排除过的要留痕**：静默丢弃一条结果，用户没法判断"没查出来"是真是假
    4. **不替用户编造身份关系**：未确认的账号绝不进聚类
"""

import pytest

from maigret.cn.exclusions import ExclusionTable, _parse_rule
from maigret.cn.report import (
    apply_exclusions,
    blind_spot_map,
    build_accounts,
    build_summary,
    collect_evidence,
    evidence_to_objects,
)


# ---------------------------------------------------------------------------
# 造数据
# ---------------------------------------------------------------------------
def claimed(username="zhangwei", url="https://example.cn/zhangwei", **extra):
    """一个"确认存在"的站点结果。"""
    result = {
        "status": {"status": "Claimed"},
        "username": username,
        "url_user": url,
    }
    result.update(extra)
    return result


def unknown(reason="blocked", username="zhangwei"):
    return {
        "status": {"status": "Unknown", "reason": f"Reason.{reason.upper()}"},
        "username": username,
        "url_user": "",
    }


def available(username="zhangwei"):
    return {
        "status": {"status": "Available"},
        "username": username,
        "url_user": "",
    }


def ev(field_name, value, confidence=0.85, source="正文", risk=None):
    item = {
        "field": field_name,
        "label": field_name,
        "value": value,
        "source": source,
        "confidence": confidence,
        "risk": risk if risk is not None else 1,
        "partial": False,
    }
    return item


# ---------------------------------------------------------------------------
# collect_evidence
# ---------------------------------------------------------------------------
def test_collect_evidence_adds_the_site_name():
    """Evidence.source 说的是"从页面哪一部分抽到的"，排查时还要知道"哪个站"。"""
    results = {
        "Bilibili": claimed(cn_evidence=[ev("ip_location", "上海", source="正文 IP 属地")]),
    }

    collected = collect_evidence(results)

    assert len(collected) == 1
    assert collected[0]["site"] == "Bilibili"
    assert collected[0]["source"] == "正文 IP 属地"


def test_collect_evidence_ignores_malformed_entries():
    """报告层崩掉比少一条证据严重得多——上游字段形态可能变。"""
    results = {"X": claimed(cn_evidence=["not a dict", None, ev("nickname", "老王")])}

    collected = collect_evidence(results)

    assert len(collected) == 1
    assert collected[0]["value"] == "老王"


def test_collect_evidence_tolerates_missing_key():
    assert collect_evidence({"X": claimed()}) == []


# ---------------------------------------------------------------------------
# build_accounts
# ---------------------------------------------------------------------------
def test_build_accounts_skips_unconfirmed_sites():
    """聚类是在断言"这些是同一个人"。拿没判出来的页面去聚类，等于替用户编造关系。"""
    results = {
        "Gitee": claimed(),
        "Bilibili": unknown(),
        "Weibo": available(),
    }

    accounts = build_accounts(results)

    assert [a.site for a in accounts] == ["Gitee"]


def test_build_accounts_parses_the_avatar_hash():
    results = {"Gitee": claimed(cn_avatar_hash="995694c92dbe46d1")}

    account = build_accounts(results)[0]

    assert account.avatar_hash == 0x995694C92DBE46D1


def test_build_accounts_ignores_a_broken_avatar_hash():
    """坏哈希必须是 None 而不是 0：0 会和别的坏哈希撞成"同一个头像"。"""
    results = {"Gitee": claimed(cn_avatar_hash="not-hex")}

    assert build_accounts(results)[0].avatar_hash is None


def test_build_accounts_reads_fields_from_evidence():
    results = {
        "Bilibili": claimed(
            cn_evidence=[
                ev("nickname", "老王"),
                ev("ip_location", "上海"),
                ev("bio", "写代码的"),
            ]
        )
    }

    account = build_accounts(results)[0]

    assert account.name == "老王"
    assert account.location == "上海"
    assert account.bio == "写代码的"


def test_build_accounts_carries_the_extracted_links():
    """跨站互链是权重最高的一条证据（0.90）。

    不把 `ids_links` 接进 Account 的话，这条规则在真实流程里永远不会命中——
    聚类就只能靠头像和用户名，而用户名重合恰恰是最常见的巧合。
    """
    results = {"Gitee": claimed(ids_links=["https://bilibili.com/space/1"])}

    assert build_accounts(results)[0].links == ["https://bilibili.com/space/1"]


def test_build_accounts_ignores_malformed_links():
    results = {"Gitee": claimed(ids_links=["https://a.cn/x", None, 42, ""])}

    assert build_accounts(results)[0].links == ["https://a.cn/x"]


# ---------------------------------------------------------------------------
# blind_spot_map
# ---------------------------------------------------------------------------
def test_blind_spot_map_groups_by_reason():
    results = {
        "A": unknown("blocked"),
        "B": unknown("blocked"),
        "C": unknown("login_required"),
        "D": claimed(),
    }

    groups = blind_spot_map(results)

    assert sorted(groups["blocked"]) == ["A", "B"]
    assert groups["login_required"] == ["C"]
    assert "D" not in str(groups)


def test_blind_spot_map_keeps_sites_without_a_reason():
    """既没命中也没原因的，多半是规则缺失。让它消失等于假装查过了。"""
    results = {"A": {"status": {"status": "Unknown"}, "username": "u"}}

    assert blind_spot_map(results) == {"rule_missing": ["A"]}


def test_blind_spot_map_ignores_results_without_status():
    assert blind_spot_map({"A": {"username": "u"}}) == {}


def test_available_is_not_a_blind_spot():
    """`Available` 是**确定的不存在**，不是盲区。

    把它算成盲区会让每一次正常扫描都列出成百上千个"未能判定"，
    真正需要注意的那几条被彻底淹没——这是本模块最容易犯的错。
    """
    results = {name: available() for name in ("A", "B", "C")}

    assert blind_spot_map(results) == {}


def test_illegal_is_not_a_blind_spot():
    """站点不接受这种用户名也是确定结论。"""
    results = {"A": {"status": {"status": "Illegal"}, "username": "u"}}

    assert blind_spot_map(results) == {}


def test_unrecognised_status_counts_as_a_blind_spot():
    """认不出来的状态一律算没判定：多报一条是噪音，少报一条会让用户以为查过了。"""
    results = {"A": {"status": {"status": "SomethingNew"}, "username": "u"}}

    assert blind_spot_map(results) == {"rule_missing": ["A"]}


# ---------------------------------------------------------------------------
# apply_exclusions
# ---------------------------------------------------------------------------
def make_table(*raw_rules):
    return ExclusionTable(rules=[_parse_rule(r, source="test") for r in raw_rules])


def test_apply_exclusions_marks_instead_of_deleting():
    """静默删除等于把排除表变成黑箱。"""
    results = {"Weibo": claimed(url="https://weibo.com/u/1")}
    table = make_table({"site": "Weibo", "reason": "站点已下线"})

    excluded = apply_exclusions(results, table)

    assert "Weibo" in results, "结果不能被删掉"
    assert results["Weibo"]["excluded_by"]
    assert excluded[0]["site"] == "Weibo"
    assert "站点已下线" in excluded[0]["rule"]


def test_apply_exclusions_without_a_table_is_a_noop():
    results = {"Weibo": claimed()}

    assert apply_exclusions(results, None) == []
    assert "excluded_by" not in results["Weibo"]


def test_apply_exclusions_leaves_non_matching_entries_alone():
    results = {"Gitee": claimed(), "Weibo": claimed()}
    table = make_table({"site": "Weibo", "reason": "站点已下线"})

    apply_exclusions(results, table)

    assert "excluded_by" not in results["Gitee"]
    assert "excluded_by" in results["Weibo"]


# ---------------------------------------------------------------------------
# build_summary：整体形态
# ---------------------------------------------------------------------------
def test_empty_results_render_nothing():
    """没有结果就返回空串，调用方据此决定不打印。"""
    assert build_summary({}) == ""


def test_summary_puts_risk_first():
    """用户最关心的放最前。反过来的话最该看的东西会被压到屏幕外。"""
    results = {
        "Bilibili": claimed(cn_evidence=[ev("email", "a@b.cn", confidence=0.7)]),
        "Weibo": unknown("blocked"),
    }

    text = build_summary(results, username="zhangwei")

    assert text.index("风险等级") < text.index("本次未能判定")


def test_summary_includes_the_username_in_the_header():
    text = build_summary({"Bilibili": claimed()}, username="zhangwei")

    assert "自查结论：zhangwei" in text


def test_summary_works_without_a_username():
    text = build_summary({"Bilibili": claimed()})

    assert "自查结论" in text
    assert "：" not in text.splitlines()[0]


def test_summary_reports_the_risk_level_for_exposed_email():
    results = {"Bilibili": claimed(cn_evidence=[ev("email", "a@b.cn", confidence=0.7)])}

    text = build_summary(results)

    assert "风险等级：中" in text
    assert "邮箱暴露" in text


def test_summary_says_nothing_found_when_nothing_was_found():
    results = {"Bilibili": available()}

    text = build_summary(results)

    assert "风险等级：无" in text
    assert "没有发现已公开的个人信息" in text


def test_summary_never_mentions_settings_or_buttons():
    """文档 §1.2①：不给处置建议。平台设置菜单几个月就改版一次，指引必然过期。"""
    results = {
        "Bilibili": claimed(
            cn_evidence=[
                ev("real_name", "张伟", risk=3),
                ev("phone", "138****1234", risk=3),
            ]
        )
    }

    text = build_summary(results)

    for banned in ("请前往", "设置里", "关闭", "建议您"):
        assert banned not in text


# ---------------------------------------------------------------------------
# build_summary：盲区
# ---------------------------------------------------------------------------
def test_summary_lists_blind_spots():
    results = {
        "A": claimed(),
        "B": unknown("blocked"),
        "C": unknown("login_required"),
    }

    text = build_summary(results)

    assert "本次未能判定" in text
    assert "需要登录" in text


def test_summary_does_not_hide_a_run_that_judged_nothing():
    """全部未判定时，最不能做的就是输出"没有发现"。"""
    results = {name: unknown("blocked") for name in ("A", "B", "C")}

    text = build_summary(results)

    assert "3 个站点触发了风控" in text


# ---------------------------------------------------------------------------
# build_summary：身份簇
# ---------------------------------------------------------------------------
def test_summary_omits_the_cluster_block_when_there_is_nothing_to_merge():
    """无簇时 render() 返回的是「未发现可归并的身份簇」。

    那句话本身含"身份簇"三个字，用子串判断会让这一块永远出现，
    把"没聚出东西"包装成一条结论。这里守的就是这个回归。
    """
    results = {"Gitee": claimed(), "Bilibili": claimed()}

    text = build_summary(results)

    assert "跨站身份归并" not in text


def test_summary_shows_the_cluster_block_when_two_accounts_are_linked():
    results = {
        "Gitee": claimed(
            username="zhangwei",
            url="https://gitee.com/zhangwei",
            ids_links=["https://bilibili.com/space/1"],
        ),
        "Bilibili": claimed(
            username="laowang",
            url="https://bilibili.com/space/1",
            ids_links=["https://gitee.com/zhangwei"],
        ),
    }

    text = build_summary(results)

    assert "跨站身份归并" in text
    assert "身份簇" in text


# ---------------------------------------------------------------------------
# build_summary：排除
# ---------------------------------------------------------------------------
def test_summary_lists_what_was_excluded():
    results = {"Weibo": claimed(url="https://weibo.com/u/1")}
    table = make_table({"site": "Weibo", "reason": "站点已下线"})

    excluded = apply_exclusions(results, table)
    text = build_summary(results, excluded=excluded, table=table)

    assert "已排除的疑似误报" in text
    assert "站点已下线" in text


def test_summary_surfaces_exclusion_table_warnings():
    """排除表出问题时必须说，否则用户不知道自己看的是不是旧规则。"""
    table = ExclusionTable(rules=[], warnings=["远程排除表加载失败（超时），沿用本地规则"])

    text = build_summary({"Gitee": claimed()}, table=table)

    assert "排除表提示" in text
    assert "超时" in text


def test_summary_is_quiet_about_a_healthy_table():
    table = make_table({"site": "Weibo", "reason": "站点已下线"})

    text = build_summary({"Gitee": claimed()}, table=table)

    assert "排除表提示" not in text


# ---------------------------------------------------------------------------
# evidence_to_objects
# ---------------------------------------------------------------------------
def test_evidence_to_objects_roundtrips():
    objects = evidence_to_objects([ev("email", "a@b.cn", confidence=0.7)])

    assert len(objects) == 1
    assert objects[0].field == "email"
    assert objects[0].value == "a@b.cn"
    assert objects[0].confidence == 0.7


def test_evidence_to_objects_skips_non_dicts():
    """上游字段形态变化时不能抛异常：报告层崩掉比少一条证据严重得多。"""
    assert evidence_to_objects(["x", None, 3]) == []


def test_evidence_to_objects_defaults_a_missing_confidence():
    """缺置信度时给 0.8 而不是 0：0 会被 risk 层当成"不可信"直接丢掉。"""
    objects = evidence_to_objects([{"field": "email", "value": "a@b.cn"}])

    assert objects[0].confidence == 0.8


# ---------------------------------------------------------------------------
# CLI 接线层：maigret.maigret 里的两个入口
#
# 这一层要守的是"出问题也不能让扫描白跑"：排除表加载失败、汇总渲染失败，
# 都只能是少一段输出，绝不能变成一次异常退出。
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
import logging  # noqa: E402
import asyncio  # noqa: E402
import logging  # noqa: E402
from argparse import Namespace  # noqa: E402

from maigret.maigret import load_cn_exclusions, print_cn_summary  # noqa: E402


def _test_logger():
    """环境里没装 pytest-asyncio，所以异步入口用 asyncio.run 驱动。

    顺带说明为什么不装：这个项目把依赖控制得很死（连 Pillow 都是可选的），
    为了四个用例引入一个测试期插件不划算。
    """
    return logging.getLogger("test-cn-report")


def cli_args(**overrides):
    base = {"cn_summary": True, "cn_exclusions": None, "cn_exclusions_url": None}
    base.update(overrides)
    return Namespace(**base)


def test_load_cn_exclusions_without_any_source_gives_an_empty_table():
    table = asyncio.run(load_cn_exclusions(cli_args(), logger=_test_logger()))

    assert len(table) == 0
    assert table.warnings == []


def test_load_cn_exclusions_reads_a_local_file(tmp_path):
    import json

    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps({"version": 1, "rules": [{"site": "Weibo", "reason": "站点已下线"}]}),
        encoding="utf-8",
    )

    table = asyncio.run(
        load_cn_exclusions(cli_args(cn_exclusions=str(path)), logger=_test_logger())
    )

    assert len(table) == 1
    assert table.explain("Weibo") is not None


def test_load_cn_exclusions_survives_a_broken_local_file(tmp_path):
    """用户把 JSON 写错是最常见的失误，不能因此中断整个扫描。"""
    path = tmp_path / "broken.json"
    path.write_text("{ this is not json", encoding="utf-8")

    table = asyncio.run(
        load_cn_exclusions(cli_args(cn_exclusions=str(path)), logger=_test_logger())
    )

    assert len(table) == 0


def test_load_cn_exclusions_rejects_a_plain_http_url():
    """明文 HTTP 的排除表可以被链路上任何人改写，等于把结果开关交出去。"""
    table = asyncio.run(
        load_cn_exclusions(
            cli_args(cn_exclusions_url="http://example.cn/rules.json"),
            logger=_test_logger(),
        )
    )

    assert len(table) == 0
    assert any("HTTPS" in w for w in table.warnings)


def test_print_cn_summary_is_silent_for_empty_results(capsys):
    assert print_cn_summary("zhangwei", {}) == ""
    assert capsys.readouterr().out == ""


def test_print_cn_summary_prints_the_block(capsys):
    results = {"Bilibili": claimed(cn_evidence=[ev("email", "a@b.cn", confidence=0.7)])}

    text = print_cn_summary("zhangwei", results)

    assert "风险等级：中" in text
    assert "风险等级：中" in capsys.readouterr().out


def test_print_cn_summary_never_raises_when_rendering_breaks(capsys, monkeypatch):
    """汇总层崩掉不能吞掉已经跑完的扫描结果——报告文件还得照常写盘。"""
    import maigret.cn.report as report_mod

    def boom(*args, **kwargs):
        raise RuntimeError("渲染炸了")

    monkeypatch.setattr(report_mod, "build_summary", boom)

    assert print_cn_summary("zhangwei", {"Bilibili": claimed()}) == ""
    assert capsys.readouterr().out == ""


def test_print_cn_summary_is_skipped_when_the_verdict_is_disabled(monkeypatch, capsys):
    """`MAIGRET_CN_VERDICT=0` 是整体回退开关，回退就该回到上游行为。

    判定关掉之后 status.reason 不再有值，汇总会把所有非命中站点统统归到
    "没有可用规则"——比不输出更误导。
    """
    monkeypatch.setenv("MAIGRET_CN_VERDICT", "0")

    assert print_cn_summary("zhangwei", {"Bilibili": claimed()}) == ""
    assert capsys.readouterr().out == ""


def test_print_cn_summary_runs_in_the_default_mode(monkeypatch, capsys):
    monkeypatch.setenv("MAIGRET_CN_VERDICT", "1")

    text = print_cn_summary("zhangwei", {"Bilibili": claimed()})

    assert "自查结论" in text
    assert capsys.readouterr().out.strip()


def test_excluded_sites_do_not_count_toward_the_risk_level():
    """既然认定它是误报，就不能再拿它的证据给风险等级加分。

    否则用户看到的是"下面说这条不算，上面却因为它升了级"——两个结论互相拆台。
    """
    results = {
        "Weibo": claimed(url="https://weibo.com/u/1", cn_evidence=[ev("email", "a@b.cn", confidence=0.7)]),
        "Gitee": claimed(),
    }
    table = make_table({"site": "Weibo", "reason": "站点对所有用户名都返回存在"})

    excluded = apply_exclusions(results, table)
    text = build_summary(results, excluded=excluded, table=table)

    assert "风险等级：无" in text
    assert "邮箱暴露" not in text


def test_excluded_sites_do_not_join_identity_clusters():
    """一条"对所有用户名都返回存在"的误报会把所有人串成一簇。"""
    results = {
        "Broken": claimed(username="x", url="https://broken.cn/x", ids_links=["https://gitee.com/zhangwei"]),
        "Gitee": claimed(username="zhangwei", url="https://gitee.com/zhangwei"),
    }
    table = make_table({"site": "Broken", "reason": "站点对所有用户名都返回存在"})

    excluded = apply_exclusions(results, table)
    text = build_summary(results, excluded=excluded, table=table)

    assert "跨站身份归并" not in text


def test_exclusion_block_says_it_is_not_counted():
    """用户需要知道"这条不算"影响到哪些部分。"""
    results = {"Weibo": claimed(url="https://weibo.com/u/1")}
    table = make_table({"site": "Weibo", "reason": "站点已下线"})

    excluded = apply_exclusions(results, table)
    text = build_summary(results, excluded=excluded, table=table)

    assert "不计入" in text


def test_build_accounts_can_skip_sites():
    results = {"Gitee": claimed(), "Broken": claimed()}

    accounts = build_accounts(results, skip_sites={"Broken"})

    assert [a.site for a in accounts] == ["Gitee"]

