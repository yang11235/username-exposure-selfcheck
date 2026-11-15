"""平台专用抽取器（`maigret/cn/platforms/`）的测试。

这些用例全部使用**实测响应**的片段，不是编造的样例：路径对不对只能靠真实
报文验证，编出来的 JSON 永远能过。
"""

import json

import pytest

from maigret.cn import platforms
from maigret.cn.extract import extract_evidence, summarize
from maigret.cn.platforms import detect, resolve
from maigret.cn.platforms.base import FieldSpec, PlatformExtractor


# ---------------------------------------------------------------------------
# resolve：路径解析
# ---------------------------------------------------------------------------
def test_resolve_walks_nested_keys():
    assert resolve({"a": {"b": {"c": 1}}}, "a.b.c") == 1


def test_resolve_handles_list_index():
    assert resolve({"a": [{"b": "x"}, {"b": "y"}]}, "a[1].b") == "y"


def test_resolve_returns_none_for_missing_segment():
    assert resolve({"a": {"b": 1}}, "a.c") is None
    assert resolve({"a": 1}, "a.b") is None
    assert resolve({}, "a") is None


def test_resolve_returns_none_for_out_of_range_index():
    assert resolve({"a": [1]}, "a[3]") is None


# ---------------------------------------------------------------------------
# detect：host + JSON 两个条件
# ---------------------------------------------------------------------------
BILI_BODY = json.dumps(
    {
        "code": 0,
        "data": {
            "card": {
                "mid": "2",
                "name": "碧诗",
                "face": "https://i2.hdslb.com/bfs/face/ef04.jpg",
                "sign": "直男过气网红",
            }
        },
    }
)

NETEASE_BODY = json.dumps(
    {
        "profile": {
            "userId": 3308314852,
            "nickname": "WizZer",
            "signature": "",
            "description": "",
            "avatarUrl": "http://p1.music.126.net/x.jpg",
        }
    }
)

JUEJIN_BODY = json.dumps(
    {
        "err_no": 0,
        "data": {
            "user_id": "2137106337240712",
            "user_name": "被禁言文章转简书了",
            "company": "",
            "job_title": "软件工程师",
            "avatar_large": "https://p1-jj.byteimg.com/x.image",
            "description": "多年从事一线互联网开发",
        },
    }
)


# 实测响应（2026-11-15，`/home/get/panel?ie=utf-8&un=maigret`）。
# 注意 `sex` 是 female、`tb_age` 是字符串 —— 这两个都是接口的真实形态。
TIEBA_BODY = json.dumps(
    {
        "no": 0,
        "error": "成功",
        "data": {
            "name": "maigret",
            "name_show": "maigret",
            "show_nickname": "maigret",
            "portrait": "tb.1.3b8697f0.ZMxDZivGfZJlucc3Khp6CA",
            "sex": "female",
            "tb_age": "19.4",
            "post_num": 0,
            "followed_count": 353,
        },
    }
)

# 不存在的用户：`data` 是空数组，两种错误码（1130029 / 1130025）都长这样
TIEBA_ABSENT_BODY = json.dumps(
    {"no": 1130029, "error": "参数错误", "data": []}
)


@pytest.mark.parametrize(
    "base_url,body,name",
    [
        ("https://space.bilibili.com/2", BILI_BODY, "bilibili"),
        ("https://music.163.com/user/home?id=1", NETEASE_BODY, "netease"),
        ("https://juejin.cn/user/1", JUEJIN_BODY, "juejin"),
        ("https://tieba.baidu.com/home/get/panel?un=maigret", TIEBA_BODY, "tieba"),
    ],
)
def test_detect_matches_each_implemented_platform(base_url, body, name):
    extractor = detect(base_url, body)
    assert extractor is not None
    assert extractor.name == name


def test_detect_rejects_html_on_a_platform_host():
    """同一个域名也可能返回 HTML 错误页 —— 那种情况要交给通用抽取。"""
    assert detect("https://space.bilibili.com/2", "<html><title>404</title></html>") is None


def test_detect_rejects_json_from_an_unknown_host():
    assert detect("https://example.com/u/1", BILI_BODY) is None


def test_detect_rejects_json_arrays():
    """顶层是数组时不认：路径没法表达"任意一个元素"，硬取只会取错人。"""
    assert detect("https://space.bilibili.com/2", '[{"name":"x"}]') is None


# ---------------------------------------------------------------------------
# extract：按声明取字段
# ---------------------------------------------------------------------------
def test_bilibili_extracts_nickname_and_bio():
    items = summarize(
        extract_evidence(
            BILI_BODY,
            ["nickname", "bio"],
            base_url="https://space.bilibili.com/2",
        )
    )
    values = {item["field"]: item["value"] for item in items}
    assert values["nickname"] == "碧诗"
    assert values["bio"] == "直男过气网红"


def test_netease_prefers_signature_over_description():
    """两个候选键按顺序取：签名优先，个人介绍兜底。"""
    body = json.dumps(
        {
            "profile": {
                "nickname": "w",
                "signature": "签名优先",
                "description": "介绍兜底",
            }
        }
    )
    items = summarize(
        extract_evidence(body, ["bio"], base_url="https://music.163.com/user/home?id=1")
    )
    assert items[0]["value"] == "签名优先"


def test_netease_falls_back_to_description():
    body = json.dumps({"profile": {"signature": "", "description": "介绍兜底"}})
    items = summarize(
        extract_evidence(body, ["bio"], base_url="https://music.163.com/user/home?id=1")
    )
    assert items[0]["value"] == "介绍兜底"


def test_juejin_does_not_put_job_title_into_company():
    """`job_title` 是职位，不是公司。写进公司栏等于让用户以为自己填错了。"""
    body = json.dumps({"data": {"user_name": "x", "company": "", "job_title": "软件工程师"}})
    items = summarize(
        extract_evidence(body, ["company"], base_url="https://juejin.cn/user/1")
    )
    assert items == []


def test_only_declared_fields_are_extracted():
    items = summarize(
        extract_evidence(BILI_BODY, ["email"], base_url="https://space.bilibili.com/2")
    )
    assert items == []


def test_zero_and_empty_values_are_skipped():
    """空串、0 都不算"抽到了"，否则画像里会出现一堆空字段。"""
    body = json.dumps({"data": {"user_name": "", "description": 0}})
    items = extract_evidence(body, ["nickname", "bio"], base_url="https://juejin.cn/user/1")
    assert items == []


def test_avatar_url_comes_from_the_api_field():
    assert platforms.avatar_url(BILI_BODY, "https://space.bilibili.com/2").endswith(".jpg")
    assert platforms.avatar_url(NETEASE_BODY, "https://music.163.com/user/home?id=1").endswith(
        ".jpg"
    )


def test_avatar_url_is_empty_for_unknown_platforms():
    assert platforms.avatar_url(BILI_BODY, "https://example.com/u/1") == ""


# ---------------------------------------------------------------------------
# 与通用抽取的衔接
# ---------------------------------------------------------------------------
def test_json_response_never_falls_through_to_html_extraction():
    """认出的平台只走平台规则。

    如果还跑一遍 HTML 规则，`visible_text()` 会把 JSON 当正文，
    把 `"name"` 这种键名当昵称抽出来 —— 那比抽不到更糟。
    """
    items = extract_evidence(
        BILI_BODY,
        ["nickname", "avatar", "email"],
        base_url="https://space.bilibili.com/2",
    )
    nicknames = [item for item in items if item.field == "nickname"]
    assert [item.value for item in nicknames] == ["碧诗"]


def test_html_pages_on_other_hosts_still_use_generic_extraction():
    html = '<html><head><meta property="og:title" content="老王"></head></html>'
    items = summarize(extract_evidence(html, ["nickname"], base_url="https://example.com/u/1"))
    assert items[0]["value"] == "老王"


# ---------------------------------------------------------------------------
# 规则本身
# ---------------------------------------------------------------------------
def test_every_field_spec_has_at_least_one_path():
    for extractor in platforms.EXTRACTORS:
        for spec in extractor.fields:
            assert spec.paths, f"{extractor.name}.{spec.name} 没有路径"


def test_wrong_path_silently_yields_nothing():
    """写错路径不会报错，只会抽空 —— 这是设计，也必须被测试钉住。

    正因为不会报错，规则才必须来自实测响应：静默失效比崩溃更难发现。
    """

    class Broken(PlatformExtractor):
        name = "broken"
        hosts = (r"example\.com/",)
        fields = (FieldSpec("nickname", ("nope.nothing",)),)

    assert Broken().extract('{"a":1}', ["nickname"]) == []


# ---------------------------------------------------------------------------
# 百度贴吧：`home/get/panel`
# ---------------------------------------------------------------------------
TIEBA_URL = "https://tieba.baidu.com/home/get/panel?ie=utf-8&un=maigret"


def test_tieba_extracts_gender_and_translates_the_enum():
    """`sex` 是 male/female 这种内部枚举，报告里必须翻成中文。

    不翻的话报告会出现「性别 male」——字段取到了，读起来还是不像中文报告。
    """
    items = {e.field: e.value for e in extract_evidence(TIEBA_BODY, ["gender"], base_url=TIEBA_URL)}

    assert items["gender"] == "女"


def test_tieba_value_map_leaves_unknown_values_alone():
    """枚举里没见过的值原样保留 —— 宁可显示 "unknown"，也不要吞掉。"""
    body = json.dumps({"no": 0, "data": {"name_show": "x", "sex": "unknown"}})

    items = {e.field: e.value for e in extract_evidence(body, ["gender"], base_url=TIEBA_URL)}

    assert items["gender"] == "unknown"


def test_tieba_extracts_nickname_and_account_age():
    """`tb_age` 有时是字符串 "19.4"、有时是整数 23，两种都要能取。"""
    items = {e.field: e.value for e in extract_evidence(
        TIEBA_BODY, ["nickname", "account_age"], base_url=TIEBA_URL)}

    assert items["nickname"] == "maigret"
    assert items["account_age"] == "19.4"

    int_age = json.dumps({"no": 0, "data": {"name_show": "x", "tb_age": 23}})
    got = {e.field: e.value for e in extract_evidence(int_age, ["account_age"], base_url=TIEBA_URL)}
    assert got["account_age"] == "23"


def test_tieba_extracts_followers_count():
    items = {e.field: e.value for e in extract_evidence(TIEBA_BODY, ["followers"], base_url=TIEBA_URL)}

    assert items["followers"] == "353"


def test_tieba_avatar_id_is_turned_into_a_cdn_url():
    """`portrait` 是个 ID 不是地址，必须拼成 himg.bdimg.com 的路径。

    实测 `https://himg.bdimg.com/sys/portrait/item/<portrait>` 返回
    200 + image/jpeg；旧的 `tb.himg.baidu.com` 证书已经不对（Hostname mismatch）。
    """
    url = platforms.avatar_url(TIEBA_BODY, TIEBA_URL)

    assert url == "https://himg.bdimg.com/sys/portrait/item/tb.1.3b8697f0.ZMxDZivGfZJlucc3Khp6CA"
    assert "tb.himg.baidu.com" not in url


def test_tieba_absent_payload_yields_nothing():
    """不存在的用户返回 `"data":[]`，取不到任何字段，也不能报错。"""
    assert extract_evidence(
        TIEBA_ABSENT_BODY, ["nickname", "gender", "followers"], base_url=TIEBA_URL
    ) == []
    assert platforms.avatar_url(TIEBA_ABSENT_BODY, TIEBA_URL) == ""
