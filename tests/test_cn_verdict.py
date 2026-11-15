"""四字段判定引擎（maigret/cn/verdict.py）的行为测试 —— 本项目最核心的一组。

守住的底线（《开发文档 v2.0》§5.3 三条硬规则 + §6.4）：

    中国站大量返回 200 的空壳页 / 风控页 / 登录墙。判定必须**先判"被打断"，
    再判存在性**，并且宁可输出 UNKNOWN 也绝不猜一个 CLAIMED。一次把"被拦截"
    判成"账号存在"，整份报告就不可信了。

全部用例用 `SimpleNamespace(status=..., text=...)` 构造响应，不起网络。
页面 fixture 见 tests/fixtures/cn/：found.html 是掘金用户 API 的 JSON 响应，
not_found.html 是掘金 404 的 SPA 外壳，blocked.html 是微博系风控页。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from maigret.cn.model import Site
from maigret.cn.verdict import (
    Reason,
    Status,
    Verdict,
    block_signals,
    judge,
    looks_like_block_page,
    looks_like_captcha,
    looks_like_login_wall,
    to_upstream_status,
    unknown_verdict,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cn"

# 掘金：两个 200，靠正/反字符串区分
JUEJIN = Site(
    name="Juejin",
    e_code=200,
    e_string='"user_name":',
    m_code=200,
    m_string='"user_id":"0"',
)

# 知乎：两个 200，正例是 JSON 字段名，反例是中文提示
ZHIHU = Site(
    name="Zhihu",
    e_code=200,
    e_string='"url_token"',
    m_code=200,
    m_string="用户不存在",
)


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def resp(status, text=""):
    return SimpleNamespace(status=status, text=text)


# ----------------------------------------------------------------------
# 先判"被打断"
# ----------------------------------------------------------------------
def test_429_is_rate_limited_not_absent():
    """429 必须单独成一种"被限流"：和"不存在"混在一起，用户会以为自己没注册。"""
    verdict = judge(resp(429, "Too Many Requests"), JUEJIN)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.RATE_LIMITED
    assert verdict.rate_limited is True
    assert verdict.http_status == 429

    # 顺序护栏：429 页面里恰好带着正例串，也必须还是"被限流"
    assert judge(resp(429, page("found.html")), JUEJIN).reason is Reason.RATE_LIMITED


@pytest.mark.parametrize("status", [403, 503])
def test_403_and_503_are_blocked(status):
    """403/503 不看正文：这两个状态码本身就是"拒绝服务"，正文内容不可信。"""
    verdict = judge(resp(status, page("found.html")), JUEJIN)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.BLOCKED
    assert verdict.http_status == status


def test_200_block_page_is_never_claimed():
    """底线用例：200 的风控页即使正文里带着"存在"判据，也只能是 UNKNOWN + BLOCKED。"""
    body = page("blocked.html")

    # 这份 fixture 里确实带着知乎的正例串，否则这条用例就退化成"空页当然不命中"
    assert ZHIHU.e_string in body
    assert 500 <= len(body) <= 1500

    verdict = judge(resp(200, body), ZHIHU)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.BLOCKED
    assert verdict.status is not Status.CLAIMED
    assert verdict.evidence  # 报告里要能说明是被哪个特征拦下的


def test_login_wall_is_reported_as_login_required():
    """登录墙也是"没判成"，但原因要说清楚是需要登录，而不是账号不存在。"""
    body = page("login_wall.html")
    assert looks_like_login_wall(body) is True

    verdict = judge(resp(200, body), ZHIHU)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.LOGIN_REQUIRED


def test_captcha_container_is_reported_as_captcha():
    """验证码要看容器特征：判定为 CAPTCHA，而不是让用户以为站点挂了。"""
    body = page("captcha.html")
    assert looks_like_captcha(body) is True

    verdict = judge(resp(200, body), JUEJIN)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.CAPTCHA


# ----------------------------------------------------------------------
# 四字段正常判定
# ----------------------------------------------------------------------
def test_found_page_is_claimed_with_structured_evidence():
    """正常命中：CLAIMED，且证据形态是 JSON 字段（最高一档，不被 status_only 封顶）。"""
    verdict = judge(resp(200, page("found.html")), JUEJIN, username="1556564194374926")

    assert verdict.status is Status.CLAIMED
    assert verdict.evidence_type == "json_field"
    assert verdict.evidence == JUEJIN.e_string
    assert verdict.confidence > 0.6
    assert verdict.http_status == 200


def test_not_found_page_is_available():
    """正常未命中：AVAILABLE。中文站常见的"200 + 反例串"必须能判出来。"""
    verdict = judge(resp(200, page("not_found.html")), JUEJIN)

    assert verdict.status is Status.AVAILABLE
    assert verdict.evidence_type == "json_field"
    assert verdict.confidence > 0.0


def test_chinese_absence_string_is_available_with_unique_string_evidence():
    """反例串是中文文案时，证据形态是 unique_string（比 JSON 字段低一档，但仍算数）。"""
    verdict = judge(resp(200, page("not_found.html")), ZHIHU)

    assert verdict.status is Status.AVAILABLE
    assert verdict.evidence_type == "unique_string"
    assert verdict.evidence == "用户不存在"


def test_status_only_hit_is_claimed_but_capped():
    """只配了状态码的命中仍是 CLAIMED，但置信度必须被 0.6 封顶（200 空壳页的护栏）。"""
    site = Site(
        name="Gitee",
        e_code=200,
        m_code=404,
        executor="api",
        confidence={"base": 1.0},
    )

    verdict = judge(resp(200, page("found.html")), site)

    assert verdict.status is Status.CLAIMED
    assert verdict.evidence_type == "status_only"
    assert verdict.confidence <= 0.6


def test_both_sides_hit_is_rule_ambiguous():
    """两个都命中 = 规则坏了：绝不猜一个，否则规则 bug 会变成"账号存在"。"""
    site = Site(
        name="SubstringTrap",
        e_code=200,
        e_string="注册于",
        m_code=200,
        m_string="注册于 2019 年",
    )

    verdict = judge(resp(200, '{"nick":"张三","bio":"注册于 2019 年 3 月"}'), site)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.RULE_AMBIGUOUS


def test_neither_side_hit_is_structure_changed():
    """两个都没命中 = 页面结构变了，是"要修规则"的信号，不是"账号不存在"的证据。"""
    shell = (
        '<!DOCTYPE html><html><head><title>知乎 - 有问题，就会有答案</title></head>'
        '<body><div id="root"></div>'
        '<script src="https://static.zhihu.com/heifetz/main.app.js"></script></body></html>'
    )

    verdict = judge(resp(200, shell), JUEJIN)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.STRUCTURE_CHANGED


def test_missing_rule_is_reported_separately_from_structure_change():
    """"规则没配"是待办，"结构变了"是故障，两者必须能分开统计。"""
    verdict = judge(resp(200, page("homepage.html")), Site(name="小红书", executor="browser"))

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.RULE_MISSING


# ----------------------------------------------------------------------
# 误伤回归
# ----------------------------------------------------------------------
def test_long_normal_page_mentioning_captcha_is_not_blocked():
    """误伤回归：正常主页里出现"验证码"（登录框文案）不能被判成风控页。"""
    body = page("homepage.html")
    site = Site(
        name="Zhihu",
        e_code=200,
        e_string="ProfileHeader-name",
        m_code=200,
        m_string="用户不存在",
    )

    assert len(body) > 8000
    assert "验证码" in body
    assert looks_like_block_page(body) is False

    verdict = judge(resp(200, body), site)

    assert verdict.status is Status.CLAIMED
    assert verdict.reason is None


def test_long_normal_page_with_two_weak_words_and_string_evidence_is_claimed():
    """20KB 正常主页 + 登录弹窗含两个弱词 + e_string 命中 → 必须 CLAIMED。

    守住的是弱特征不再单独决定状态：修复前 len(weak)>=2 直接判 BLOCKED，
    把带登录弹窗的正常主页（中文站最常见的一种）全部打成 UNKNOWN。
    弱词只降置信度（×0.8），并在 evidence 里留痕，方便排查"为什么分数偏低"。
    """
    body = page("homepage.html")
    site = Site(
        name="Zhihu",
        e_code=200,
        e_string="ProfileHeader-name",
        m_code=200,
        m_string="用户不存在",
    )

    # 前提：确实是"长页面 + 两个不同弱词"（按字节算 22KB，中文一个字占 3 字节）
    assert len(body.encode("utf-8")) > 20000
    assert len(body) > 8000
    assert "验证码" in body and "安全验证" in body

    verdict = judge(resp(200, body), site)

    assert verdict.status is Status.CLAIMED
    assert verdict.reason is None
    # 0.9(base) × 0.9(http) × 0.9(unique_string) × 0.8(弱特征折扣) = 0.58
    assert verdict.confidence == pytest.approx(0.58)
    # 命中的弱词要能在证据里看到，否则报告里只有一个降过的分数，无法解释
    assert "安全验证" in verdict.evidence
    assert "验证码" in verdict.evidence


def test_small_risk_page_with_single_weak_word_and_status_only_is_blocked():
    """3.5KB 风控页 + 1 个弱词 + status_only 规则 → UNKNOWN/BLOCKED。

    守住的是"短页面 + 弱词"这个漏洞：修复前只有 1 个弱词、页面又超过 3000 字符，
    判不出 BLOCKED，而 status_only 规则会把这个 200 直接判成 CLAIMED（真误报）。
    只剩状态码时，状态码与可疑文案都不硬，只能保守判 BLOCKED。
    """
    body = page("risk_control_small.html")
    site = Site(name="Gitee", e_code=200, m_code=404)

    # 前提：页面超过旧阈值 3000 字符，且只有一个弱词、没有强特征
    assert len(body) > 3000
    assert block_signals(body) == ((), ("验证码",))

    verdict = judge(resp(200, body), site)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.BLOCKED
    assert verdict.evidence == "验证码"


def test_strong_block_feature_wins_even_when_esstring_hits():
    """风控页同时带着 e_string 时，强特征必须优先（不能因为"证据命中"就判 CLAIMED）。

    真实风控页会保留站点的模板（blocked.html 里就带着知乎的 "url_token"），
    所以"e_string 命中"不能用来推翻强特征 —— 那会把最坏的一类误报放出去。
    """
    body = page("blocked.html")
    site = Site(name="Zhihu", e_code=200, e_string='"url_token"', m_code=200, m_string="用户不存在")

    assert '"url_token"' in body
    strong, _ = block_signals(body)
    assert strong

    verdict = judge(resp(200, body), site)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.BLOCKED
    assert verdict.evidence in strong


# ----------------------------------------------------------------------
# 空 body / 规则不可用
# ----------------------------------------------------------------------
@pytest.mark.parametrize("text", ["", "   ", "\n\t "], ids=["empty", "spaces", "newlines"])
def test_200_with_empty_body_is_unknown_not_claimed(text):
    """200 + 空 body 只能证明"请求通了"，不能证明账号存在。

    守住的是中文站最常见的一类假 CLAIMED：WAF / 反向代理 / SPA 空壳返回
    200 且正文为空，e_code=200 的规则会命中存在侧，产出 CLAIMED 0.49。
    """
    site = Site(name="Gitee", e_code=200, m_code=404)

    verdict = judge(resp(200, text), site)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.EMPTY_BODY
    assert verdict.status is not Status.CLAIMED
    assert verdict.http_status == 200


def test_empty_body_rule_with_string_still_unknown():
    """有 e_string 的规则同样受空 body 保护：没有正文，字符串判据无从命中。"""
    verdict = judge(resp(200, ""), JUEJIN)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.EMPTY_BODY


@pytest.mark.parametrize(
    "response",
    [
        resp(200, page("found.html")),          # 含 e_string：修复前会判 CLAIMED
        resp(200, page("not_found.html")),      # 不含 e_string：修复前会判 AVAILABLE
        resp(200, page("homepage.html")),
        resp(200, ""),
        resp(429, "Too Many Requests"),
    ],
    ids=["with-esstring", "without-esstring", "long-page", "empty", "rate-limited"],
)
def test_invalid_rule_is_unknown_regardless_of_response(response):
    """valid=False 的规则对任何响应都只能 UNKNOWN/RULE_MISSING。

    守住的是"向用户输出假 AVAILABLE"：CSDN/Baidu/NGA 这类是 valid=false +
    e_code=m_code=200 + m_string=""，修复前一个不含 e_string 的 200 页会走到
    "不存在侧只比状态码 → 命中"，输出 AVAILABLE，等于告诉用户"你没有这个账号"。
    """
    site = Site(
        name="CSDN",
        valid=False,
        e_code=200,
        e_string='data-username="',
        m_code=200,
        m_string="",
    )

    verdict = judge(response, site)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.RULE_MISSING
    assert verdict.evidence == "站点规则未通过校验"


# ----------------------------------------------------------------------
# 多判据串（上游 presenseStrs 是"任一命中"）
# ----------------------------------------------------------------------
def test_extra_string_hit_is_claimed_and_evidence_names_the_matched_string():
    """主串不命中、附加串命中时仍然 CLAIMED，且 evidence 是实际命中的那个串。

    守住的是"只取第一个串"造成的漏判：上游 235 个 message 站有多个正例串，
    第一个串改版消失、第二个串还在时，修复前会误报 STRUCTURE_CHANGED。
    """
    site = Site(
        name="MultiString",
        e_code=200,
        e_string='"user_name":',
        e_strings=['"user_id":', '"nickname":'],
        m_code=200,
        m_string='"user_id":"0"',
    )

    verdict = judge(resp(200, '{"nickname":"张三","avatar":"x"}'), site)

    assert verdict.status is Status.CLAIMED
    assert verdict.evidence == '"nickname":'  # 命中第二个附加串
    # 证据形态按**实际命中的那个串**推断：带引号的 JSON 字段名 → json_field
    assert verdict.evidence_type == "json_field"


def test_primary_string_still_wins_when_both_present():
    """主串与附加串都在时，evidence 记录主串：报告里要有一个稳定的主判据。"""
    site = Site(
        name="MultiString",
        e_code=200,
        e_string='"user_name":',
        e_strings=['"nickname":'],
        m_code=200,
        m_string='"user_id":"0"',
    )

    verdict = judge(resp(200, '{"user_name":"张三","nickname":"张三"}'), site)

    assert verdict.status is Status.CLAIMED
    assert verdict.evidence == '"user_name":'


# ----------------------------------------------------------------------
# 与上游的契约
# ----------------------------------------------------------------------
@pytest.mark.parametrize("status", list(Status))
def test_to_upstream_status_is_accepted_by_upstream_enum(status):
    """hook 层会执行 MaigretCheckStatus(to_upstream_status(v))，值写错就是运行时 ValueError。"""
    from maigret.result import MaigretCheckStatus

    assert MaigretCheckStatus(to_upstream_status(Verdict(status))).value == status.value


def test_judge_results_are_serializable_to_upstream_status():
    """判定结果本身也要能被上游枚举接住（CLAIMED / AVAILABLE / UNKNOWN 三条主路径）。"""
    from maigret.result import MaigretCheckStatus

    claimed = judge(resp(200, page("found.html")), JUEJIN)
    available = judge(resp(200, page("not_found.html")), JUEJIN)
    unknown = judge(resp(200, page("blocked.html")), ZHIHU)

    assert MaigretCheckStatus(to_upstream_status(claimed)) is MaigretCheckStatus.CLAIMED
    assert MaigretCheckStatus(to_upstream_status(available)) is MaigretCheckStatus.AVAILABLE
    assert MaigretCheckStatus(to_upstream_status(unknown)) is MaigretCheckStatus.UNKNOWN


def test_unknown_verdict_builder_carries_reason_and_rate_limit_flag():
    """超时/异常在拿到响应之前就发生了，hook 层需要能构造带原因的 UNKNOWN。"""
    verdict = unknown_verdict(Reason.TIMEOUT, evidence="30s", rate_limited=True)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.TIMEOUT
    assert verdict.evidence == "30s"
    assert verdict.rate_limited is True


def test_xiaohongshu_network_risk_page_is_blocked():
    """小红书"网络层风控"页：200 + 空壳正文，必须判 BLOCKED 而不是 CLAIMED。

    真实页面（2026-11 实测，浏览器取回 92KB）：
        <title>安全限制</title>
        正文可见文本只有 "安全限制 当前网络存在风险，请切换可靠网络后重试
        300012 我要反馈 返回首页"

    这是本项目最危险的一类页面：状态码 200、HTML 体积巨大（打包 JS 撑着），
    但正文里没有任何"用户存在"的内容。如果规则是 status_only（e_code=200），
    就会直接判成 CLAIMED —— 一次把风控判成存在，整份报告就不可信了。
    """
    body = (
        "<html><head><title>安全限制</title></head><body>"
        "<div>安全限制</div><div>当前网络存在风险，请切换可靠网络后重试</div>"
        "<div>300012</div><div>我要反馈</div><div>返回首页</div>"
        "</body></html>"
    )
    # status_only 规则：只认 200。修复前这条规则会把它判成 CLAIMED。
    status_only = Site(name="小红书", e_code=200, m_code=404)

    assert looks_like_block_page(body) is True

    verdict = judge(resp(200, body), status_only)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.BLOCKED
    assert verdict.evidence == "当前网络存在风险"


# ---------------------------------------------------------------------------
# m_empty：空 body 就是不存在（百度贴吧这类 JSON 接口）
# ---------------------------------------------------------------------------
def _tieba():
    """一个声明了 m_empty 的假想接口：存在返回 JSON，不存在返回 0 字节。

    形状取自百度贴吧旧的 `i/sys/user_json`（2026-11-15 实测：存在 200/394B
    含 raw_name，不存在 200/0B）。贴吧现在的正式规则已经换成 `home/get/panel`
    （反例侧是 `"data":[]`，不再靠空 body），这里保留它作为 m_empty 这个
    通用特性的样例。
    """
    return Site(
        name="EmptyBodySite",
        uri_check="https://example.cn/api/user?un={account}",
        e_code=200,
        e_string='"raw_name"',
        m_code=200,
        m_empty=True,
    )


def test_m_empty_declares_empty_body_as_absent():
    """声明了 m_empty 的站点，200 + 空 body 是**不存在**，不是"请求通了而已"。

    实测依据（2026-11-15）：贴吧 user_json 对 maigret 返回 200/394B 含
    raw_name，对随机串返回 200/0B。这类接口没有反例串可写，m_string 只能留空，
    不加这个开关就永远判不出"不存在"。
    """
    verdict = judge(resp(200, ""), _tieba())

    assert verdict.status is Status.AVAILABLE
    assert verdict.evidence_type == "empty_body"
    # 0.85 的证据系数 × base 0.9 × http 0.9 ≈ 0.69，落在中档：可以采信，但
    # 不如"页面上写着用户不存在"那么硬。
    assert 0.5 <= verdict.confidence < 0.8


def test_m_empty_does_not_break_the_present_side():
    """存在侧照旧靠 e_string 判。

    回归的是一个真实踩到的坑：`m_string` 为空时 `_hit` 会退化成 status_only
    并恒命中，于是同一份非空 body 同时命中两侧 → RULE_AMBIGUOUS → 这条规则
    永远失效。m_empty 站点的反例证据是"空 body"，body 非空时它一点证据都没有。
    """
    verdict = judge(resp(200, '{"tbs":"x","raw_name":"maigret","id":1}'), _tieba())

    assert verdict.status is Status.CLAIMED
    assert verdict.reason is None
    assert verdict.confidence >= 0.8


def test_m_empty_only_applies_to_the_declared_absence_status():
    """状态码对不上时，空 body 不能当"不存在"。

    挡的是"网关把 403 改写成 200 空响应"这一类：那正是把"被拦住"说成
    "你没有这个账号"的最短路径。
    """
    site = _tieba()

    for status in (403, 503, 404):
        verdict = judge(resp(status, ""), site)
        assert verdict.status is Status.UNKNOWN, status
        assert verdict.status is not Status.AVAILABLE, status

    # 429 排在更前面，理由不变：限流不是结论。
    assert judge(resp(429, ""), site).reason is Reason.RATE_LIMITED


def test_m_empty_does_not_leak_into_sites_without_the_flag():
    """没声明 m_empty 的站点，空 body 必须仍然是 UNKNOWN/EMPTY_BODY。

    这是本次改动最关键的一条不变量：开关默认关，否则每一个 WAF 空壳页都会
    变成"你没有这个账号"。
    """
    plain = Site(name="Gitee", e_code=200, m_code=200, e_string='"raw_name"')

    verdict = judge(resp(200, ""), plain)

    assert verdict.status is Status.UNKNOWN
    assert verdict.reason is Reason.EMPTY_BODY
