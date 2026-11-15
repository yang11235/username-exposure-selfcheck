"""判定 → 抽取的接线测试（不联网）。

`checking.py` 里的这一段是整条链路的接缝：判定层给出 CLAIMED 之后，
抽取层才被允许去读页面。这里没有测试的话，两个模块各自 100% 覆盖、
拼起来却是断的——真实扫描时表现为"报告里什么都没有"，而且不报错。

守三件事：

    1. **只对 CLAIMED 抽**。对未确认的页面抽字段，等于把别人的信息记到
       这个用户名头上。判定层刚把"被拦截/登录墙"降级成 UNKNOWN 的页面，
       绝不能在这里又被当成真人主页来读。
    2. **只抽站点声明过的字段**。`_cn_extract_fields` 返回空列表的含义是
       "这个站不值得抽"，**不是**"抽全部"。
    3. **脱敏开关要生效**。`--mask` 关掉时原样输出，打开时遮住。
"""

import logging

import pytest

from maigret import checking
from maigret.checking import _cn_extract_fields, process_site_result
from maigret.cn.model import Site
from maigret.result import MaigretCheckStatus
from maigret.sites import MaigretSite

LOGGER = logging.getLogger("test-cn-checking")

# 一段真实形态的主页片段：og:title 里有昵称，正文里有 IP 属地
PROFILE_HTML = """
<html><head>
<meta property="og:title" content="老王 (laowang) - Example站">
</head><body>
<div class="user-info">IP属地：上海</div>
</body></html>
"""


def make_site(name="Example", **overrides):
    """构造一个走上游 status_code 判定的站点。

    刻意不用真实的策展站点（Gitee 等）：那条路径依赖 `sites_cn.json` 的
    具体文案，规则一改测试就红，而这里要测的是接线，不是某条规则。
    """
    site = MaigretSite(
        name,
        {
            "url": "https://example.cn/{username}",
            "urlMain": "https://example.cn/",
            "checkType": "status_code",
            "usernameClaimed": "laowang",
            "usernameUnclaimed": "noonewouldeverusethis7",
        },
    )
    for key, value in overrides.items():
        setattr(site, key, value)
    return site


def make_results_info(username="laowang", **overrides):
    info = {
        "username": username,
        "parsing_enabled": False,
        "url_user": f"https://example.cn/{username}",
        "url_probe": f"https://example.cn/{username}",
        "mask": False,
    }
    info.update(overrides)
    return info


def run(response, site=None, results_info=None):
    site = site or make_site()
    results_info = results_info or make_results_info()
    return process_site_result(
        response, None, LOGGER, results_info, site
    )


@pytest.fixture
def cn_spec(monkeypatch):
    """让 spec_for 返回一个声明了 extract 的合成站点。"""

    def install(fields=("nickname",), valid=True):
        spec = Site(
            name="Example",
            uri_check="https://example.cn/{account}",
            e_code=200,
            m_code=404,
            valid=valid,
            extract=list(fields),
        )
        monkeypatch.setattr(checking, "spec_for", lambda site: spec)
        return spec

    return install


# ---------------------------------------------------------------------------
# _cn_extract_fields：空列表的语义
# ---------------------------------------------------------------------------
def test_extract_fields_is_empty_for_sites_without_a_spec(monkeypatch):
    """返回空列表 = "这个站不值得抽"，不是"抽全部"。"""
    monkeypatch.setattr(checking, "spec_for", lambda site: None)

    assert _cn_extract_fields(make_site()) == []


def test_extract_fields_returns_the_declared_list(cn_spec):
    cn_spec(fields=["nickname", "avatar"])

    assert _cn_extract_fields(make_site()) == ["nickname", "avatar"]


def test_extract_fields_swallows_a_broken_spec(monkeypatch):
    """抽取是锦上添花，不该因为一条策展数据写坏就让整次扫描失败。"""

    def boom(site):
        raise RuntimeError("策展数据坏了")

    monkeypatch.setattr(checking, "spec_for", boom)

    assert _cn_extract_fields(make_site()) == []


# ---------------------------------------------------------------------------
# 只对 CLAIMED 抽
# ---------------------------------------------------------------------------
def test_evidence_is_written_for_a_claimed_site(cn_spec):
    cn_spec()

    info = run((PROFILE_HTML, 200, None))

    assert info["status"].status == MaigretCheckStatus.CLAIMED
    assert info["cn_evidence"]
    assert info["cn_evidence"][0]["field"] == "nickname"


def test_evidence_is_not_written_for_an_available_site(cn_spec):
    """账号不存在时页面上的一切都不属于这个用户名。"""
    cn_spec()

    info = run((PROFILE_HTML, 404, None))

    assert info["status"].status != MaigretCheckStatus.CLAIMED
    assert "cn_evidence" not in info


def test_evidence_is_not_written_when_the_site_declares_nothing(monkeypatch):
    monkeypatch.setattr(checking, "spec_for", lambda site: None)

    info = run((PROFILE_HTML, 200, None))

    assert info["status"].status == MaigretCheckStatus.CLAIMED
    assert "cn_evidence" not in info


def test_evidence_is_not_written_for_a_blocked_page(cn_spec):
    """风控页即使被判成 CLAIMED 也不能抽——但这里验证的是判定层的结论。

    403 会被上游判成"不存在"，抽取层因此不会动手；这条测试钉的是
    "判定结论决定抽取与否"这个顺序，不是 403 本身的语义。
    """
    cn_spec()

    info = run((PROFILE_HTML, 403, None))

    assert "cn_evidence" not in info


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
def test_mask_flag_is_honoured(cn_spec):
    """`--mask` 遮的是敏感标识（邮箱/手机号/姓名/属地），不含昵称。

    昵称是用户自己选来公开的展示名，遮了只会让报告读不懂；
    这条测试同时钉住"该遮的遮了"和"不该遮的没被顺手遮掉"。
    """
    cn_spec(fields=["nickname", "ip_location"])

    plain = run((PROFILE_HTML, 200, None), results_info=make_results_info(mask=False))
    masked = run((PROFILE_HTML, 200, None), results_info=make_results_info(mask=True))

    plain_values = {item["field"]: item["value"] for item in plain["cn_evidence"]}
    masked_values = {item["field"]: item["value"] for item in masked["cn_evidence"]}

    assert plain_values["ip_location"] == "上海"
    assert masked_values["ip_location"] == "上*"
    # 昵称不脱敏
    assert masked_values["nickname"] == plain_values["nickname"]


# ---------------------------------------------------------------------------
# 证据形态
# ---------------------------------------------------------------------------
def test_evidence_entries_carry_what_the_report_needs(cn_spec):
    cn_spec()

    info = run((PROFILE_HTML, 200, None))

    entry = info["cn_evidence"][0]
    assert set(entry) >= {"field", "value", "source", "confidence", "risk"}


def test_evidence_is_absent_when_extraction_finds_nothing(cn_spec):
    """页面里没有声明过的字段时，不要留一个空列表占位。

    报告层用 `result.get("cn_evidence") or []` 读它，空列表和不存在等价；
    但 JSON 里多一个 `"cn_evidence": []` 会让人以为"抽了但没抽到"，
    而真相是"这个页面里根本没有这些字段"。
    """
    cn_spec(fields=["nickname"])

    info = run(("<html><body>没有 og:title</body></html>", 200, None))

    assert "cn_evidence" not in info


def test_a_failing_extraction_does_not_break_the_site_result(cn_spec, monkeypatch):
    """抽取层抛异常时，站点结果必须完好——否则一个站点的抽取 bug
    会让整次扫描少一条结果，而用户完全看不出来。"""
    cn_spec()

    def boom(*args, **kwargs):
        raise RuntimeError("抽取炸了")

    monkeypatch.setattr(checking, "extract_evidence", boom)

    info = run((PROFILE_HTML, 200, None))

    assert info["status"].status == MaigretCheckStatus.CLAIMED
    assert "cn_evidence" not in info
