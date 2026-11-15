"""风险分级测试（纯计算，无网络）。

守两件事：

    1. 文档 §4.2 的每条规则都按原样生效，且"最高级胜出"
    2. **不给处置建议**（文档 §1.2①）——输出里不能出现"去设置里关掉 X"
       这类会过期的指引
"""

import pytest

from maigret.cn.extract import Evidence
from maigret.cn.risk import (
    LEVEL_LABELS,
    MIN_CONFIDENCE,
    RISK_RULES,
    assess,
    blind_spots,
    render,
)


def ev(field_name, value="x", confidence=0.8, source="测试"):
    return Evidence(field_name, value, source, confidence=confidence)


# ---------------------------------------------------------------------------
# 规则表
# ---------------------------------------------------------------------------
def test_rule_table_matches_the_document():
    """规则表是文档 §4.2 的原文，改动必须是有意识的。"""
    table = {(tuple(sorted(f)), level) for f, level, _ in RISK_RULES}

    assert (("phone", "real_name"), 3) in table
    assert (("ip_location", "real_name"), 3) in table
    assert (("company", "real_name", "school"), 3) in table
    assert (("real_name",), 2) in table
    assert (("phone",), 2) in table
    assert (("company", "ip_location"), 2) in table
    assert (("email",), 2) in table
    assert (("ip_location",), 1) in table


def test_no_evidence_means_no_risk():
    result = assess([])

    assert result.level == 0
    assert result.label == "无"
    assert result.findings == []


def test_weak_signals_alone_stay_low():
    """只有昵称/头像指纹/简介时，风险是"无"——它们不是身份暴露。"""
    result = assess([ev("nickname"), ev("avatar_hash"), ev("bio")])

    assert result.level == 0


def test_ip_location_alone_is_low():
    result = assess([ev("ip_location", "广东")])

    assert result.level == 1
    assert result.findings[0].reason == "仅 IP 属地暴露"


def test_email_alone_is_medium():
    result = assess([ev("email", "a@b.com")])

    assert result.level == 2
    assert "撞库" in result.findings[0].reason


def test_real_name_with_phone_is_high():
    """最严重的一类：姓名 + 手机号。"""
    result = assess([ev("real_name", "张*"), ev("phone", "138****5678")])

    assert result.level == 3
    assert result.label == "高"
    assert any("手机号" in f.reason for f in result.findings)


def test_real_name_with_location_is_high():
    result = assess([ev("real_name", "张*"), ev("ip_location", "广东")])

    assert result.level == 3


def test_real_name_company_school_is_high():
    """姓名 + 单位 + 学校，足以精准定位到一个人。"""
    result = assess([ev("real_name"), ev("company"), ev("school")])

    assert result.level == 3
    assert any("精准定位" in f.reason for f in result.findings)


def test_highest_level_wins_and_all_reasons_are_listed():
    """多条规则同时命中时，等级取最高，但理由要全列出来。

    只给一个数字等于让用户猜"为什么是高"。
    """
    result = assess(
        [
            ev("real_name"),
            ev("phone"),
            ev("ip_location"),
            ev("email"),
        ]
    )

    assert result.level == 3
    levels = {f.level for f in result.findings}
    assert levels == {3, 2, 1}, "三条不同等级的规则都该被列出"


def test_findings_are_sorted_by_severity():
    result = assess([ev("real_name"), ev("phone"), ev("ip_location")])

    levels = [f.level for f in result.findings]
    assert levels == sorted(levels, reverse=True)


def test_partial_values_still_count():
    """`138****5678`、`张*` 同样是"已公开"，只是展示时脱敏了。

    把它们当成"没暴露"会得出"风险低"的错误结论——而用户真正需要知道的
    恰恰是"我的手机号已经挂在网上了"。
    """
    partial = Evidence("phone", "138****5678", "正文", confidence=0.8, partial=True)

    result = assess([partial])

    assert result.level == 2


def test_low_confidence_evidence_is_ignored():
    """一条 0.3 的猜测不该把风险抬到"高"。"""
    result = assess(
        [
            ev("real_name", confidence=0.3),
            ev("phone", confidence=0.3),
        ]
    )

    assert result.level == 0
    assert set(result.ignored) == {"real_name", "phone"}


def test_confidence_threshold_boundary():
    """阈值上的那条要算进来，低于它的不算。"""
    assert assess([ev("email", confidence=MIN_CONFIDENCE)]).level == 2
    assert assess([ev("email", confidence=MIN_CONFIDENCE - 0.01)]).level == 0


def test_accepts_dict_evidence():
    """报告里存的是 dict 形态，不能要求调用方来回转换。"""
    result = assess(
        [
            {"field": "real_name", "value": "张*", "confidence": 0.9, "source": "x"},
            {"field": "phone", "value": "138****5678", "confidence": 0.8, "source": "y"},
        ]
    )

    assert result.level == 3


# ---------------------------------------------------------------------------
# 盲区
# ---------------------------------------------------------------------------
def test_blind_spots_group_by_reason_and_sort_by_size():
    spots = blind_spots(
        {
            "login_required": ["小红书", "抖音", "脉脉"],
            "blocked": ["知乎"],
        }
    )

    assert [s.reason for s in spots] == ["login_required", "blocked"]
    assert "3 个站点需要登录才能判定" in spots[0].line()


def test_blind_spots_distinguish_permanent_from_temporary():
    """结构性限制和临时故障必须分开说。

    混在一起说，用户会反复重试一件注定失败的事（不登录永远判不了）。
    """
    spots = {s.reason: s for s in blind_spots({"login_required": ["a"], "blocked": ["b"]})}

    assert spots["login_required"].retryable is False
    assert spots["blocked"].retryable is True


def test_empty_reason_groups_are_dropped():
    assert blind_spots({"blocked": [], "login_required": ["x"]})[0].reason == "login_required"


def test_many_sites_are_summarized_not_listed():
    """50 个站全列出来会淹没重点。"""
    spots = blind_spots({"login_required": [f"site{i}" for i in range(50)]})

    line = spots[0].line()
    assert "等 50 个" in line
    assert len(line) < 120


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def test_render_shows_level_and_reasons():
    text = render(assess([ev("real_name", "张*"), ev("phone", "138****5678")]))

    assert "风险等级：高" in text
    assert "命中的证据" in text


def test_render_includes_blind_spots():
    text = render(assess([]), blind_spots({"login_required": ["小红书", "抖音"]}))

    assert "本次未能判定" in text
    assert "小红书" in text


def test_render_never_gives_remediation_advice():
    """文档 §1.2①：不给处置建议。

    判据是输出里不能出现"去设置""关闭""建议开启"这类指向平台操作的字眼——
    平台隐私菜单几个月就改版一次，这类指引必然过期，过期的指引比没有更坏。
    """
    text = render(
        assess([ev("real_name", "张*"), ev("phone", "138****5678")]),
        blind_spots({"blocked": ["知乎"]}),
        evidence=[ev("real_name", "张*"), ev("phone", "138****5678")],
    )

    for banned in ("设置", "关闭", "开启", "打开", "建议开启", "隐私选项"):
        assert banned not in text, f"输出里出现了处置建议：{banned}"


def test_render_evidence_detail_uses_labels():
    text = render(
        assess([ev("ip_location", "广东", confidence=0.85)]),
        evidence=[ev("ip_location", "广东", confidence=0.85)],
    )

    assert "IP 属地" in text
    assert "置信度 0.85" in text


def test_render_clean_case_says_so():
    """什么都没查到时要明确说"没发现"，不能让用户对着空白页猜。"""
    text = render(assess([]))

    assert "风险等级：无" in text
    assert "没有发现已公开的个人信息" in text


def test_all_levels_have_labels():
    assert LEVEL_LABELS == {3: "高", 2: "中", 1: "低", 0: "无"}


# ---------------------------------------------------------------------------
# 结语必须和上面列出来的东西一致
#
# 这一节来自一次真实扫描：Gitee 抽到了昵称和头像指纹（都不到风险线），
# 输出却是「风险等级：无 / 证据明细 昵称… 头像指纹… / 没有发现已公开的
# 个人信息」——三句话互相打架。抽到的东西被说成没抽到，和抽错一样糟。
# ---------------------------------------------------------------------------
def test_level_zero_with_low_risk_evidence_does_not_claim_nothing_was_found():
    items = [ev("nickname", "老王", confidence=0.6), ev("avatar_hash", "9956", confidence=0.8)]

    text = render(assess(items), evidence=items)

    assert "没有发现已公开的个人信息" not in text
    assert "低敏感度" in text


def test_level_zero_without_any_evidence_says_nothing_was_found():
    text = render(assess([]))

    assert "没有发现已公开的个人信息" in text


def test_evidence_that_fails_to_render_falls_back_to_nothing_found():
    """证据列表里全是空值时，_render_evidence 会返回空——此时说"没发现"是对的。"""
    text = render(assess([]), evidence=[ev("", ""), ev("nickname", "")])

    assert "没有发现已公开的个人信息" in text
