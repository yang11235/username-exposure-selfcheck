"""多格式互转测试（纯计算，无网络）。

这一层守的是"导入的规则不能是错的"：

    1. **占位符**：WMN 用 `{account}`、Sherlock 用 `{}`、Maigret 用
       `{username}`，漏换一个，规则就变成请求一个字面量 `{}` 的地址。
    2. **状态码类型**：字符串 `"200"` 留在 `e_code` 里，`status == e_code`
       永远不成立，规则会静默失效。
    3. **表达不了的语义必须显式拒绝**：Sherlock 的 `response_url` 和
       `message` 型都要标 `valid: false` + 写明原因。产出一条"看着能用、
       实际必然误判"的规则，比不导入坏得多。
"""

import json
import sys
from pathlib import Path

import pytest

from maigret.cn.importers import (
    FORMATS,
    dump,
    load,
    parse_form_body,
    strip_to_v2,
    to_account_placeholder,
)
from maigret.cn.schema import SchemaError, validate_site

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "utils"))

import cn_import  # noqa: E402


# ---------------------------------------------------------------------------
# 占位符
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://a.cn/{account}", "https://a.cn/{account}"),
        ("https://a.cn/{}", "https://a.cn/{account}"),
        ("https://a.cn/{username}", "https://a.cn/{account}"),
    ],
)
def test_placeholders_are_normalised(raw, expected):
    assert to_account_placeholder(raw) == expected


def test_placeholders_are_normalised_inside_nested_structures():
    """post_body 里的占位符漏换，POST 型站点会在导入后静默失效。"""
    raw = {"user": "{username}", "list": ["{}"], "n": 1}

    assert to_account_placeholder(raw) == {
        "user": "{account}",
        "list": ["{account}"],
        "n": 1,
    }


# ---------------------------------------------------------------------------
# WMN
# ---------------------------------------------------------------------------
def wmn_site(**overrides):
    entry = {
        "name": "Example",
        "uri_check": "https://example.cn/{account}",
        "uri_pretty": "https://example.cn/{account}",
        "e_code": 200,
        "e_string": "退出登录",
        "m_code": 404,
        "m_string": "",
        "known": ["someone"],
        "cat": "social",
        "valid": True,
    }
    entry.update(overrides)
    return entry


def test_wmn_basic_import_passes_schema():
    sites = load("wmn", {"sites": [wmn_site()]})

    site = validate_site("Example", sites["Example"])

    assert site.e_code == 200
    assert site.m_code == 404
    assert site.e_string == "退出登录"
    assert site.uri_check == "https://example.cn/{account}"


def test_wmn_derives_url_main_from_uri_pretty():
    """WMN 没有 urlMain，但报告里要展示站点主页。"""
    sites = load("wmn", {"sites": [wmn_site()]})

    assert sites["Example"]["urlMain"] == "https://example.cn"


def test_wmn_cat_becomes_tags():
    """`--tags cn` 这类过滤靠 tags。不填的话导入的站点永远筛不出来。"""
    sites = load("wmn", {"sites": [wmn_site(cat="coding")]})

    assert sites["Example"]["cat"] == "coding"
    assert sites["Example"]["tags"] == ["coding"]


def test_wmn_post_body_is_parsed_into_a_dict_and_switches_to_post():
    entry = wmn_site(
        uri_check="https://example.cn/login",
        post_body="user={account}&password=x",
    )

    sites = load("wmn", {"sites": [entry]})

    assert sites["Example"]["post_body"] == {"user": "{account}", "password": "x"}
    assert sites["Example"]["method"] == "POST"


def test_wmn_string_status_code_becomes_int():
    """`"200"` 留在 e_code 里，`status == e_code` 永远不成立——规则静默失效。"""
    sites = load("wmn", {"sites": [wmn_site(e_code="200", m_code="404")]})

    assert sites["Example"]["e_code"] == 200
    assert sites["Example"]["m_code"] == 404
    assert isinstance(sites["Example"]["e_code"], int)


def test_wmn_string_valid_flag_becomes_bool():
    sites = load("wmn", {"sites": [wmn_site(valid="false")]})

    assert sites["Example"]["valid"] is False


def test_wmn_multiple_presence_strings_become_extra_candidates():
    """第一个串当主判据，其余进 e_strings（语义是"任一命中"）。"""
    sites = load("wmn", {"sites": [wmn_site(e_string=["主串", "备串1", "备串2"])]})

    assert sites["Example"]["e_string"] == "主串"
    assert sites["Example"]["e_strings"] == ["备串1", "备串2"]


def test_wmn_bare_list_is_accepted():
    sites = load("wmn", [wmn_site()])

    assert "Example" in sites


def test_wmn_without_sites_raises():
    with pytest.raises(ValueError):
        load("wmn", {"nope": 1})


def test_wmn_dump_puts_cat_into_categories():
    """`cat` 必须出现在顶层 categories 里，否则 WMN 自己的校验会报错。"""
    sites = load("wmn", {"sites": [wmn_site(cat="coding")]})

    out = dump("wmn", sites)

    assert out["categories"] == ["coding"]
    assert out["sites"][0]["name"] == "Example"


def test_wmn_dump_encodes_post_body_as_query_string():
    sites = load(
        "wmn",
        {"sites": [wmn_site(uri_check="https://e.cn/login", post_body="a={account}")]},
    )

    entry = dump("wmn", sites)["sites"][0]

    assert entry["post_body"] == "a=%7Baccount%7D"


# ---------------------------------------------------------------------------
# Sherlock
# ---------------------------------------------------------------------------
def test_sherlock_status_code_maps_to_200_404():
    sites = load("sherlock", {"X": {"url": "https://x.cn/{}", "errorType": "status_code"}})

    assert sites["X"]["e_code"] == 200
    assert sites["X"]["m_code"] == 404


def test_sherlock_placeholder_is_converted():
    sites = load("sherlock", {"X": {"url": "https://x.cn/{}"}})

    assert sites["X"]["uri_check"] == "https://x.cn/{account}"


def test_sherlock_url_probe_wins_over_url():
    sites = load(
        "sherlock",
        {"X": {"url": "https://x.cn/{}", "urlProbe": "https://api.x.cn/u/{}"}},
    )

    assert sites["X"]["uri_check"] == "https://api.x.cn/u/{account}"
    assert sites["X"]["uri_pretty"] == "https://x.cn/{account}"


def test_sherlock_message_type_is_rejected_with_a_reason():
    """message 型只有不存在侧判据，e_code == m_code 时必然双命中。

    这不是"可能出错"，是"每条规则每次查询都出错"（judge 返回 rule_ambiguous）。
    所以必须标成待补，而不是产出一条看着能用的规则。
    """
    sites = load(
        "sherlock",
        {"X": {"url": "https://x.cn/{}", "errorType": "message", "errorMsg": "该用户不存在"}},
    )

    assert sites["X"]["valid"] is False
    assert "e_string" in sites["X"]["_note"]


def test_sherlock_response_url_type_is_rejected_with_a_reason():
    sites = load(
        "sherlock",
        {"X": {"url": "https://x.cn/{}", "errorType": "response_url", "errorUrl": "https://x.cn/"}},
    )

    assert sites["X"]["valid"] is False
    assert "response_url" in sites["X"]["_note"]


def test_sherlock_message_without_error_msg_is_rejected():
    sites = load(
        "sherlock", {"X": {"url": "https://x.cn/{}", "errorType": "message", "errorMsg": ""}}
    )

    assert sites["X"]["valid"] is False


def test_sherlock_regex_check_is_not_silently_dropped():
    """v2 没有 regexCheck 字段，静默丢弃会让用户以为转换是无损的。"""
    sites = load(
        "sherlock",
        {"X": {"url": "https://x.cn/{}", "regexCheck": "^[a-z]+$"}},
    )

    assert "regexCheck" in sites["X"]["_note"]


def test_sherlock_post_payload_is_converted():
    sites = load(
        "sherlock",
        {
            "X": {
                "url": "https://x.cn/login",
                "errorType": "status_code",
                "POST": True,
                "request_payload": {"user": "{}"},
            }
        },
    )

    assert sites["X"]["method"] == "POST"
    assert sites["X"]["post_body"] == {"user": "{account}"}


def test_sherlock_claimed_strings_become_lists():
    """`list("blue")` 会变成 ['b','l','u','e']——这是最容易踩的转换坑。"""
    sites = load(
        "sherlock",
        {"X": {"url": "https://x.cn/{}", "username_claimed": "blue", "username_unclaimed": "nouser7"}},
    )

    assert sites["X"]["known"] == ["blue"]
    assert sites["X"]["known_absent"] == ["nouser7"]


def test_sherlock_dump_uses_message_when_an_absence_string_exists():
    sites = {
        "X": {
            "uri_check": "https://x.cn/{account}",
            "e_code": 200,
            "m_code": 200,
            "e_string": "退出",
            "m_string": "不存在",
        }
    }

    entry = dump("sherlock", sites)["X"]

    assert entry["errorType"] == "message"
    assert entry["errorMsg"] == "不存在"
    assert entry["url"] == "https://x.cn/{}"


def test_sherlock_dump_uses_status_code_without_absence_string():
    sites = {"X": {"uri_check": "https://x.cn/{account}", "e_code": 200, "m_code": 404}}

    assert dump("sherlock", sites)["X"]["errorType"] == "status_code"


# ---------------------------------------------------------------------------
# Maigret
# ---------------------------------------------------------------------------
def test_maigret_import_uses_the_four_field_semantics():
    """message 型只取第一个正例串，其余不当 OR 候选（掘金的 rule_ambiguous 教训）。"""
    data = {
        "sites": {
            "Juejin": {
                "url": "https://juejin.cn/user/{username}",
                "checkType": "message",
                "presenseStrs": ['"user_name":', '"user_id":'],
                "absenceStrs": ['"user_id":"0"'],
            }
        }
    }

    sites = load("maigret", data)

    assert sites["Juejin"]["e_string"] == '"user_name":'
    assert sites["Juejin"]["e_strings"] == []


def test_maigret_import_converts_the_placeholder():
    data = {"sites": {"X": {"url": "https://x.cn/{username}", "checkType": "status_code"}}}

    sites = load("maigret", data)

    assert "{username}" not in sites["X"]["uri_check"]
    assert sites["X"]["uri_check"] == "https://x.cn/{account}"


def test_maigret_import_strips_upstream_only_keys():
    """落盘成 v2 文件时必须收敛字段，否则上游语义和 v2 语义混在一份文件里。"""
    data = {
        "sites": {
            "X": {
                "url": "https://x.cn/{username}",
                "checkType": "message",
                "presenseStrs": ["a"],
                "absenceStrs": ["b"],
                "errors": {"account/unhuman": "x"},
            }
        }
    }

    sites = load("maigret", data)

    for key in ("checkType", "presenseStrs", "absenceStrs", "errors"):
        assert key not in sites["X"]


def test_maigret_import_requires_a_sites_object():
    with pytest.raises(ValueError):
        load("maigret", {"sites": []})


def test_maigret_dump_uses_message_when_both_sides_have_strings():
    sites = {
        "X": {
            "uri_check": "https://x.cn/{account}",
            "uri_pretty": "https://x.cn/{account}",
            "e_code": 200,
            "m_code": 200,
            "e_string": "a",
            "m_string": "b",
        }
    }

    entry = dump("maigret", sites)["sites"]["X"]

    assert entry["checkType"] == "message"
    assert entry["presenseStrs"] == ["a"]
    assert entry["absenceStrs"] == ["b"]
    assert entry["url"] == "https://x.cn/{username}"


def test_maigret_dump_reports_lost_v2_only_fields():
    sites = {
        "X": {
            "uri_check": "https://x.cn/{account}",
            "e_code": 200,
            "m_code": 404,
            "extract": ["nickname"],
        }
    }

    entry = dump("maigret", sites)["sites"]["X"]

    assert "extract" in entry["_note"]


def test_maigret_dump_does_not_report_the_default_executor_as_lost():
    """`executor` 默认就是 http，把它也报成丢失会让几千条全带一句废话。"""
    sites = {"X": {"uri_check": "https://x.cn/{account}", "e_code": 200, "m_code": 404}}

    assert "_note" not in dump("maigret", sites)["sites"]["X"]


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
def test_unknown_format_is_rejected():
    with pytest.raises(ValueError):
        load("nope", {})
    with pytest.raises(ValueError):
        dump("nope", {})


def test_all_formats_are_listed():
    assert set(FORMATS) == {"v2", "wmn", "sherlock", "maigret"}


def test_v2_roundtrip_keeps_the_four_fields():
    sites = {
        "X": {
            "uri_check": "https://x.cn/{account}",
            "e_code": 200,
            "m_code": 404,
            "e_string": "a",
        }
    }

    back = load("v2", dump("v2", sites))

    assert back["X"]["e_code"] == 200
    assert back["X"]["m_code"] == 404
    assert back["X"]["e_string"] == "a"


def test_v2_accepts_a_sites_wrapper():
    assert "X" in load("v2", {"sites": {"X": {"uri_check": "https://x.cn/{account}"}}})


def test_wmn_roundtrip_preserves_the_four_fields():
    original = load("wmn", {"sites": [wmn_site()]})
    again = load("wmn", dump("wmn", original))

    assert again["Example"]["e_code"] == 200
    assert again["Example"]["m_code"] == 404
    assert again["Example"]["e_string"] == "退出登录"


def test_strip_to_v2_drops_unknown_keys():
    assert strip_to_v2({"e_code": 200, "checkType": "message"}) == {"e_code": 200}


def test_parse_form_body_handles_empty_and_dict():
    assert parse_form_body("") is None
    assert parse_form_body(None) is None
    assert parse_form_body({"a": "1"}) == {"a": "1"}
    assert parse_form_body("a=1&b=2") == {"a": "1", "b": "2"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_detect_format_recognises_each_shape():
    assert cn_import.detect_format(Path("x.json"), {"sites": []}) == "wmn"
    assert cn_import.detect_format(Path("x.json"), [{"name": "a"}]) == "wmn"
    assert cn_import.detect_format(Path("x.json"), {"sites": {}, "engines": {}}) == "maigret"
    assert cn_import.detect_format(Path("x.json"), {"sites": {}}) == "v2"
    assert cn_import.detect_format(Path("x.json"), {"A": {"url": "u"}}) == "sherlock"


def test_detect_format_does_not_trust_the_filename():
    """用户把 WMN 文件重命名成 data.json 是常态。"""
    assert cn_import.detect_format(Path("data.json"), {"sites": []}) == "wmn"


def test_filter_sites_by_name():
    sites = {"A": {}, "B": {}, "C": {}}

    assert list(cn_import.filter_sites(sites, ["a"], [])) == ["A"]
    assert list(cn_import.filter_sites(sites, [], ["b"])) == ["A", "C"]


def test_cli_writes_a_file_and_reports_stats(tmp_path, capsys):
    src = tmp_path / "wmn.json"
    src.write_text(json.dumps({"sites": [wmn_site()]}), encoding="utf-8")
    out = tmp_path / "out.json"

    code = cn_import.main([str(src), "-o", str(out)])

    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "Example" in data
    assert "可直接使用" in capsys.readouterr().err


def test_cli_keeps_invalid_entries_when_asked(tmp_path):
    """待补的规则也要能存下来，否则问题会从库里消失。"""
    src = tmp_path / "sherlock.json"
    src.write_text(
        json.dumps({"X": {"url": "https://x.cn/{}", "errorType": "message", "errorMsg": "无"}}),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"

    code = cn_import.main([str(src), "--keep-invalid", "-o", str(out)])

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["X"]["valid"] is False
    # 一个可用规则都没有 → 退出码 1。这不是"命令出错"，是"这次导入没有产出
    # 能用的规则"，CI 里要能据此拦住一次无效的批量导入。
    assert code == 1


def test_cli_dry_run_writes_nothing(tmp_path, capsys):
    src = tmp_path / "wmn.json"
    src.write_text(json.dumps({"sites": [wmn_site()]}), encoding="utf-8")

    assert cn_import.main([str(src), "--dry-run"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_missing_file_returns_2(tmp_path):
    assert cn_import.main([str(tmp_path / "nope.json")]) == 2


def test_cli_unknown_site_filter_returns_1(tmp_path):
    src = tmp_path / "wmn.json"
    src.write_text(json.dumps({"sites": [wmn_site()]}), encoding="utf-8")

    assert cn_import.main([str(src), "--site", "NoSuchSite"]) == 1


def test_cli_warns_about_force_include_when_writing_many_into_sites_cn(tmp_path, capsys):
    """sites_cn.json 里的站点会被强制纳入默认扫描，几百个一起进去会明显变慢。"""
    many = [wmn_site(name=f"Site{i}") for i in range(cn_import.FORCE_INCLUDE_WARN_THRESHOLD + 1)]
    src = tmp_path / "wmn.json"
    src.write_text(json.dumps({"sites": many}), encoding="utf-8")
    out = tmp_path / "sites_cn.json"

    cn_import.main([str(src), "-o", str(out)])

    assert "强制纳入默认扫描" in capsys.readouterr().err
