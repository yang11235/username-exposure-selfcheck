"""置信度模型（maigret/cn/scoring.py）的行为测试。

守住《开发文档 v2.0》§5.4 的关键护栏：**仅靠状态码判定的命中永远进不了
高置信档**。中国站大量"200 + 空壳页"，这类判断不该在报告里被当成事实；
同时站点库是人工维护的 JSON，一个拼错的字段不能让整次扫描崩掉。
"""

from types import SimpleNamespace

import pytest

from maigret.cn.model import Site
from maigret.cn.scoring import STATUS_ONLY_CONFIDENCE, confidence_band, score


def test_status_only_is_fixed_confidence_regardless_of_site_settings():
    """status_only 返回固定 0.5，不参与乘法。

    守住的是"分档不随 base 微调抖动"：一个 200 就是 200，站点把 base 从 0.9
    调到 1.0、把 executor 从 http 换成 api，都不该改变这个结论的强度。
    修复前这里是 0.9×0.9×0.6=0.49（低档）/ 1.0×1.0×0.6=0.6（中档），
    同一个"仅状态码"证据在不同站点上落在两个档位。
    """
    default_site = Site(name="Gitee", e_code=200, m_code=404, executor="http")
    api_site = Site(
        name="ApiSite", e_code=200, m_code=404, executor="api", confidence={"base": 1.0}
    )

    assert score(default_site, "status_only") == 0.5
    assert score(api_site, "status_only") == 0.5
    assert STATUS_ONLY_CONFIDENCE == 0.5


def test_status_only_lands_on_medium_band_and_never_high():
    """0.5 必须落在 medium 档下沿（"需自行确认"），且明确进不了 high 档。"""
    assert confidence_band(STATUS_ONLY_CONFIDENCE) == "medium"

    site = Site(name="Any", executor="api", confidence={"base": 1.0})
    assert confidence_band(score(site, "status_only")) == "medium"
    assert confidence_band(score(site, "status_only")) != "high"


def test_status_only_fixed_value_does_not_leak_into_other_evidence_types():
    """固定值只针对 status_only：其他证据形态仍然走乘法，否则所有站都被拉平成 0.5。"""
    site = Site(name="Gitee", executor="api", confidence={"base": 1.0})

    assert score(site, "json_field") == pytest.approx(1.0)
    assert score(site, "unique_string") == pytest.approx(0.9)
    assert score(site, "name_match_only") == pytest.approx(0.3)


def test_unknown_executor_and_evidence_do_not_raise():
    """未知 executor / evidence_type 走保守默认值，只降级不抛异常。"""
    typo_executor = SimpleNamespace(
        name="TypoExecutor", confidence={"base": 1.0}, executor="headless-chrome-v2"
    )
    assert score(typo_executor, "unique_string") == pytest.approx(0.81)

    typo_evidence = SimpleNamespace(name="TypoEvidence", confidence={"base": 1.0}, executor="api")
    assert score(typo_evidence, "vibes") == pytest.approx(0.6)

    # 空字符串同样按未知处理
    assert score(SimpleNamespace(name="Empty", confidence={"base": 1.0}, executor=""), "") == pytest.approx(
        0.54
    )


@pytest.mark.parametrize(
    "base,expected",
    [
        (-0.4, 0.0),
        (1.7, 1.0),
    ],
    ids=["negative", "above-one"],
)
def test_out_of_range_base_is_clamped(base, expected):
    """base 来自手改的 JSON，属于系统边界：越界值要夹紧，而不是让结果爆掉。"""
    site = SimpleNamespace(name="Boundary", confidence={"base": base}, executor="api")

    assert score(site, "json_field") == pytest.approx(expected)


@pytest.mark.parametrize(
    "value,band",
    [
        (1.0, "high"),
        (0.8, "high"),
        (0.79, "medium"),
        (0.5, "medium"),
        (0.49, "low"),
        (0.0, "low"),
    ],
)
def test_confidence_band_boundaries(value, band):
    """分档边界按 §5.4：≥0.8 高 / 0.5–0.8 中 / <0.5 低。"""
    assert confidence_band(value) == band


def test_score_is_rounded_to_two_decimals():
    """报告里展示的是两位小数，评分就该按两位小数收敛，避免 0.8100000000000001。"""
    site = Site(name="Juejin", executor="http", confidence={"base": 0.9})

    assert score(site, "json_field") == pytest.approx(0.81)      # 0.9 × 0.9 × 1.0
    assert score(site, "unique_string") == pytest.approx(0.73)   # 0.9 × 0.9 × 0.9
