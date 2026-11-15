"""四字段协议（maigret/cn/schema.py）的行为测试。

守住的是《开发文档 v2.0》§5.3 硬规则 3 与 §6.1 Schema CI：

    一条"对存在与不存在输出完全一致"的规则，必须在**配置期**被拒绝入库，
    而不是等扫描时把 200 空壳页判成账号存在。这条护栏失效一次，整份报告
    的可信度就没了，所以这里断言的是"拒绝/放行"这个结果，不关心报错文案。
"""

import pytest

from maigret.cn.model import Site
from maigret.cn.schema import SchemaError, assert_four_fields, validate_site, validate_sites


def _valid_site(**overrides):
    """一份形态贴近真实中文站的合法定义（掘金 API 探测）。"""
    raw = {
        "uri_check": "https://api.juejin.cn/user_api/v1/user/get?user_id={account}",
        "uri_pretty": "https://juejin.cn/user/{account}",
        "e_code": 200,
        "e_string": '"user_name":',
        "m_code": 200,
        "m_string": '"user_id":"0"',
    }
    raw.update(overrides)
    return raw


def test_valid_four_field_site_is_accepted():
    """四字段齐全、占位符正确、状态码为整数的站点必须能通过校验。"""
    site = validate_site("Juejin", _valid_site())

    assert isinstance(site, Site)
    assert site.name == "Juejin"
    assert (site.e_code, site.m_code) == (200, 200)
    assert site.e_string == '"user_name":'
    assert site.m_string == '"user_id":"0"'
    assert site.uri_check.endswith("{account}")
    assert site.valid is True


def test_dangerous_rule_is_rejected():
    """状态码分不开且没有字符串判据 = 永远判不出来，这是本项目最重要的一条护栏。"""
    dangerous = _valid_site(e_string="", m_string="")

    with pytest.raises(SchemaError) as excinfo:
        validate_site("EmptyShell", dangerous)

    assert excinfo.value.site == "EmptyShell"


def test_invalid_site_skips_completeness_but_still_rejects_dangerous_rule():
    """valid=False 可以"还没写完"，但"永远判不出来"的规则即使标了 invalid 也不许入库。"""
    draft = validate_site("Draft", {"valid": False, "uri_check": "https://x.example/u/{account}"})
    assert draft.valid is False
    assert draft.e_code is None and draft.m_code is None

    with pytest.raises(SchemaError):
        validate_site(
            "DraftButDangerous",
            {
                "valid": False,
                "uri_check": "https://x.example/u/{account}",
                "e_code": 200,
                "m_code": 200,
                "e_string": "",
                "m_string": "",
            },
        )


@pytest.mark.parametrize(
    "broken",
    [
        {"e_code": "200"},          # 字符串状态码：和 resp.status 比大小永远不等
        {"e_code": True},           # bool：True == 1，会静默变成状态码 1
        {"m_code": 200.0},          # float：不是整数状态码
        {"e_string": ["x"]},        # 列表：一路传到 "x" in body 才炸
        {"m_string": None},         # 显式 null：等于没配反例串，规则退化成判不出"不存在"
    ],
    ids=["str-code", "bool-code", "float-code", "list-string", "null-string"],
)
def test_wrong_types_are_rejected(broken):
    """类型错误要在配置期拦住，否则会变成扫描时的 TypeError 或静默的错误判定。"""
    with pytest.raises(SchemaError):
        validate_site("BadType", _valid_site(**broken))


def test_placeholder_must_be_in_url_or_post_body():
    """占位符必须出现在能带上它的地方：URL 里没有、body 里也没有就是拼不出请求。"""
    with pytest.raises(SchemaError):
        validate_site(
            "NoPlaceholder",
            _valid_site(uri_check="https://x.example/api/search", method="POST"),
        )

    site = validate_site(
        "PostOnly",
        _valid_site(
            uri_check="https://discord.com/api/v9/unique-username/unique-usernames",
            method="POST",
            post_body={"username": "{account}", "limit": 1},
        ),
    )
    assert site.method == "POST"
    assert site.post_body == {"username": "{account}", "limit": 1}


def test_validate_sites_keeps_going_after_one_failure():
    """9,535 条里必然有不合格的，单站失败不能让整批加载中断。"""
    sites, errors = validate_sites(
        {
            "Good": _valid_site(),
            "Bad": _valid_site(e_string="", m_string=""),
            "AlsoGood": _valid_site(e_code=200, m_code=404, e_string="", m_string=""),
        }
    )

    assert set(sites) == {"Good", "AlsoGood"}
    assert len(errors) == 1
    assert "Bad" in errors[0]


def test_assert_four_fields_skips_invalid_sites_and_rejects_broken_valid_ones():
    """CI 断言只约束 valid=True：invalid 是"我还不能用"的声明，不该让它永远失败。"""
    ready = Site(
        name="ready",
        uri_check="https://x.example/u/{account}",
        e_code=200,
        e_string="FOUND_MARK",
        m_code=200,
        m_string="ABSENT_MARK",
    )
    draft = Site(name="draft", valid=False)

    assert_four_fields({"ready": ready, "draft": draft})  # 不抛即通过

    incomplete = Site(name="incomplete", uri_check="https://x.example/u/{account}", e_code=200)
    with pytest.raises(SchemaError):
        assert_four_fields({"incomplete": incomplete})

    # 也接受 Site 可迭代对象（hook / 报告层会这样传）
    with pytest.raises(SchemaError):
        assert_four_fields([ready, incomplete])


# ----------------------------------------------------------------------
# 三类"必然误报"的规则（修复 3）
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "field,value",
    [
        ("e_string", "1"),      # 上游真实存在：PromptBase / ngl.link
        ("e_string", "[]"),     # 任何 JSON 响应都含它
        ("m_string", "0"),      # 反向同理：会把"页面里有 0"判成账号不存在
        ("m_string", "[]"),
    ],
    ids=["e-one-char", "e-two-chars", "m-one-char", "m-two-chars"],
)
def test_short_criterion_string_is_rejected(field, value):
    """1~2 个字符的判据串在 HTML 里必然误命中，必须在配置期拒绝。

    守住的是"任何含该字符的页面都被判成命中"：e_string="1" 会让所有带数字 1
    的 200 页变成 CLAIMED。m_string 一并检查，否则反向的假 AVAILABLE 同样致命。
    """
    with pytest.raises(SchemaError) as excinfo:
        validate_site("ShortString", _valid_site(**{field: value}))

    assert field in excinfo.value.reason


def test_same_code_mutual_substring_is_rejected():
    """同码 + 互为子串 = 必然双命中，永远只能输出 RULE_AMBIGUOUS。

    上游真实实例：Runescape 的 e_string="loggedIn" 是 m_string
    '{"error":"NO_PROFILE","loggedIn":"false"}' 的子串 —— 这条规则一条
    "不存在"都给不出来，入库只会污染存活率统计。
    """
    runescape = _valid_site(
        e_string="loggedIn",
        m_string='{"error":"NO_PROFILE","loggedIn":"false"}',
    )

    with pytest.raises(SchemaError) as excinfo:
        validate_site("Runescape", runescape)

    assert "互为子串" in excinfo.value.reason

    # 不同状态码时同样的字符串是合法的：状态码本身就能区分
    ok = validate_site("DifferentCode", _valid_site(e_code=200, m_code=404, **{
        "e_string": "loggedIn",
        "m_string": '{"error":"NO_PROFILE","loggedIn":"false"}',
    }))
    assert ok.e_code == 200


@pytest.mark.parametrize("placeholder", ["{account}", "{username}"])
@pytest.mark.parametrize("field", ["e_string", "m_string"])
def test_echo_placeholder_is_rejected(field, placeholder):
    """判据串只是用户名占位符：只能证明页面回显了用户名，证明不了账号存在。

    不存在的用户页同样会回显用户名（"用户 xxx 不存在"），这种规则对
    "存在/不存在"没有区分力。`{username}` 也拒绝：上游原样形态必须一并挡住，
    否则会从 overlay 的占位符转换缝里漏过去。
    """
    with pytest.raises(SchemaError) as excinfo:
        validate_site("EchoOnly", _valid_site(**{field: placeholder}))

    assert "占位符" in excinfo.value.reason


def test_empty_string_status_only_rule_is_still_accepted():
    """e_string="" 是合法的 status_only 规则，不能被"短串"规则误伤。

    守住的是"只拒绝非空的短串"这条边界：status_only 是上游 1,906 个
    status_code 站的正常形态，误杀它等于把这一整类站点赶出库。
    """
    site = validate_site(
        "Gitee",
        _valid_site(e_code=200, m_code=404, e_string="", m_string=""),
    )

    assert site.valid is True
    assert site.e_string == "" and site.m_string == ""


def test_extra_strings_are_not_length_checked():
    """附加候选串不做长度校验：主串已校验过，附加串只是多一条退路。

    但占位符仍要拒绝 —— 附加串写 {account} 会让"任何回显用户名的页面"都命中。
    """
    site = validate_site(
        "MultiString",
        _valid_site(e_strings=["1", "x"], m_strings=["[]"]),
    )

    assert site.e_strings == ["1", "x"]
    assert site.m_strings == ["[]"]

    with pytest.raises(SchemaError):
        validate_site("EchoExtra", _valid_site(e_strings=["{account}"]))


def test_invalid_site_skips_the_new_rejections():
    """valid=False 跳过 4~6 条：它本身就是"这条规则还不能用"的声明。

    拒绝入库等于让待办从库里消失 —— 诊断时反而看不到"这条规则长什么样、缺什么"。
    """
    draft = validate_site(
        "Draft",
        {
            "valid": False,
            "uri_check": "https://x.example/u/{account}",
            "e_code": 200,
            "e_string": "1",
            "m_code": 200,
            "m_string": "1",
        },
    )

    assert draft.valid is False
    assert draft.e_string == "1"


# ---------------------------------------------------------------------------
# m_empty：空 body 即不存在
# ---------------------------------------------------------------------------
def test_m_empty_lets_a_rule_omit_the_absence_string():
    """声明 m_empty 后，缺 m_string 不再算"判不了"。

    这类接口（贴吧 user_json）用 0 字节 body 表示不存在，本来就没有反例串可写。
    按缺失打回只会逼作者去编一个不存在的字符串 —— 那才是真把规则弄坏。
    """
    site = validate_site(
        "Baidu",
        _valid_site(
            uri_check="https://tieba.baidu.com/i/sys/user_json?un={account}",
            e_string='"raw_name"',
            m_string="",
            m_empty=True,
        ),
    )

    assert site.valid is True
    assert site.m_empty is True
    assert site.m_string == ""


def test_without_m_empty_a_missing_absence_string_is_still_rejected():
    """不声明 m_empty 时，缺 m_string 依然拒绝入库 —— 开关默认关。"""
    with pytest.raises(SchemaError) as excinfo:
        validate_site(
            "Baidu",
            _valid_site(e_string='"raw_name"', m_string=""),
        )

    assert excinfo.value.site == "Baidu"


def test_m_empty_does_not_excuse_a_missing_presence_string():
    """m_empty 只放行反例侧。存在侧仍然必须有判据，否则规则对"存在"毫无证据。"""
    with pytest.raises(SchemaError):
        validate_site("Baidu", _valid_site(e_string="", m_string="", m_empty=True))
