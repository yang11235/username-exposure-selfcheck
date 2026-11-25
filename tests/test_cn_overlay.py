"""三级站点库合并（maigret/cn/overlay.py）的行为测试。

守住两件事：

    1. 上游 9,535 条 → v2 四字段的适配不能"翻译错"（尤其是 `usernameClaimed`
       这种裸字符串字段，拆成字符列表会让 known 变成 ['1','5','5',…]）。
    2. 合并优先级必须是 sites_cn > overrides > 上游，且是**逐站深合并**：
       只改一个 e_string 不能把 headers / known / alexaRank 冲掉。

除"真实库不变量"外，合并优先级一律用临时 fixture 验证，不依赖真实库内容。
"""

import json
from pathlib import Path

import pytest

from maigret.cn import overlay
from maigret.cn.model import Site, site_from_mapping
from maigret.cn.overlay import adapt_upstream, deep_merge, load_sites, load_upstream_raw
from maigret.cn.schema import assert_four_fields

CN_SITES_FILE = Path(__file__).resolve().parents[1] / "maigret" / "cn" / "data" / "sites_cn.json"


def _cn_file_keys():
    return set(json.loads(CN_SITES_FILE.read_text(encoding="utf-8"))["sites"])


def _cn_entries():
    return json.loads(CN_SITES_FILE.read_text(encoding="utf-8"))["sites"]


# _note 里出现这些词，说明规则作者自己都不确定 —— 这种条目不能有 verified_at
CN_DOUBT_MARKERS = ("未实测", "未核实", "未验证", "需复核", "待复核", "待验证", "可能")


# ----------------------------------------------------------------------
# deep_merge
# ----------------------------------------------------------------------
def test_deep_merge_recurses_into_dicts():
    """只改一个嵌套键时，同层的其他键必须保留（否则一条规则会被写成残废）。"""
    base = {
        "urlMain": "https://weibo.com",
        "e_string": "upstream-found",
        "headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://weibo.com/"},
    }
    patch = {"headers": {"Referer": "https://m.weibo.cn/"}}

    merged = deep_merge(base, patch)

    assert merged["headers"] == {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://m.weibo.cn/",
    }
    assert merged["e_string"] == "upstream-found"
    assert merged["urlMain"] == "https://weibo.com"
    # 合并不能反过来污染入参
    assert base["headers"]["Referer"] == "https://weibo.com/"


def test_deep_merge_replaces_lists_wholesale():
    """list 是整体替换而不是追加：否则删不掉上游的旧判据串，只会越叠越歧义。"""
    merged = deep_merge(
        {"presenseStrs": ['"user_name":', '"user_id":'], "tags": ["social"]},
        {"presenseStrs": ['"nickname":']},
    )

    assert merged["presenseStrs"] == ['"nickname":']
    assert merged["tags"] == ["social"]


def test_deep_merge_null_deletes_key():
    """null 表示删除键（RFC 7386），用来清掉上游有害字段，比如已失效的硬编码 Cookie。"""
    base = {
        "headers": {"User-Agent": "Mozilla/5.0", "Cookie": "SUB=expired-session"},
        "tags": ["cn", "social"],
    }

    merged = deep_merge(base, {"headers": {"Cookie": None}, "tags": None})

    assert "Cookie" not in merged["headers"]
    assert merged["headers"] == {"User-Agent": "Mozilla/5.0"}
    assert "tags" not in merged


# ----------------------------------------------------------------------
# adapt_upstream
# ----------------------------------------------------------------------
def test_adapt_message_type_takes_first_presence_and_absence_string():
    """checkType=message 的语义是"同状态码、靠字符串区分"，两个状态码都必须是 200。"""
    raw = {
        "checkType": "message",
        "urlMain": "https://juejin.cn/",
        "url": "https://juejin.cn/user/{username}",
        "urlProbe": "https://api.juejin.cn/user_api/v1/user/get?user_id={username}",
        "presenseStrs": ['"user_name":', '"user_id":'],
        "absenceStrs": ['"user_id":"0"', '"err_no":404'],
    }

    adapted = adapt_upstream("Juejin", raw)

    assert (adapted["e_code"], adapted["m_code"]) == (200, 200)
    assert adapted["e_string"] == '"user_name":'
    assert adapted["m_string"] == '"user_id":"0"'
    assert adapted["uri_check"] == "https://api.juejin.cn/user_api/v1/user/get?user_id={account}"
    assert adapted["uri_pretty"] == "https://juejin.cn/user/{account}"


@pytest.mark.parametrize("check_type", [None, "status_code", "response_url"])
def test_adapt_status_code_families_fall_back_to_200_404(check_type):
    """status_code / 未标注 / response_url 只能靠状态码，映射成 200/404 且字符串为空。"""
    raw = {
        "urlMain": "https://github.com/",
        "url": "https://github.com/{username}",
        "urlProbe": "https://api.github.com/users/{username}",
        "usernameClaimed": "blue",
        "usernameUnclaimed": "noonewouldeverusethis7",
    }
    if check_type is not None:
        raw["checkType"] = check_type

    adapted = adapt_upstream("GitHub", raw)

    assert (adapted["e_code"], adapted["m_code"]) == (200, 404)
    assert adapted["e_string"] == ""
    assert adapted["m_string"] == ""


def test_adapt_converts_username_placeholder_recursively():
    """上游模板是 {username}，v2 统一为 {account}；post_body 里的也必须一起换。"""
    raw = {
        "checkType": "status_code",
        "urlProbe": "https://discord.com/api/v9/unique-username/unique-usernames",
        "url": "https://discord.com/users/{username}",
        "method": "POST",
        "requestPayload": {
            "username": "{username}",
            "context": {"referrer": "/u/{username}"},
            "probe": ["{username}", "keep-me"],
        },
    }

    adapted = adapt_upstream("Discord", raw)

    assert adapted["uri_check"] == "https://discord.com/api/v9/unique-username/unique-usernames"
    assert adapted["uri_pretty"] == "https://discord.com/users/{account}"
    assert adapted["post_body"] == {
        "username": "{account}",
        "context": {"referrer": "/u/{account}"},
        "probe": ["{account}", "keep-me"],
    }
    assert "{username}" not in json.dumps(adapted, ensure_ascii=False)


def test_adapt_username_claimed_string_becomes_single_item_list():
    """上游 usernameClaimed 是裸字符串，转成 known 列表时绝不能拆成单字符列表。"""
    adapted = adapt_upstream(
        "Juejin",
        {
            "checkType": "message",
            "presenseStrs": ['"user_name":'],
            "absenceStrs": ['"user_id":"0"'],
            "usernameClaimed": "1556564194374926",
            "usernameUnclaimed": "999999999999",
        },
    )

    assert adapted["known"] == ["1556564194374926"]
    assert adapted["known_absent"] == ["999999999999"]
    assert len(adapted["known"]) == 1

    # 再确认一次穿过 Site 构造后仍然是单元素列表
    site = site_from_mapping("Juejin", adapted)
    assert isinstance(site, Site)
    assert site.known == ["1556564194374926"]


# ----------------------------------------------------------------------
# 修复 6：上游用 requestMethod 表示 POST
# ----------------------------------------------------------------------
def test_adapt_reads_request_method_from_upstream_field():
    """上游 24 个站用 requestMethod 表示 POST，只读 v2 的 method 会让它们全变 GET。

    守住的是 POST 站的 body：方法错了 body 就发不出去，探测永远 UNKNOWN。
    """
    adapted = adapt_upstream(
        "figshare",
        {
            "checkType": "message",
            "requestMethod": "POST",
            "urlProbe": "https://api.figshare.com/v2/articles/search",
            "requestPayload": {"search_for": ":author: {username}"},
            "presenseStrs": ["[{"],
            "absenceStrs": ["[]"],
        },
    )

    assert adapted["method"] == "POST"
    assert site_from_mapping("figshare", adapted).method == "POST"

    # v2 原生 method 优先级更高，且统一大写
    assert adapt_upstream("X", {"requestMethod": "POST", "method": "put"})["method"] == "PUT"
    assert adapt_upstream("Y", {"method": "post"})["method"] == "POST"
    # 两者都没有时默认 GET
    assert adapt_upstream("Z", {})["method"] == "GET"


# ----------------------------------------------------------------------
# 修复 7：message 型多串是"任一命中"
# ----------------------------------------------------------------------
def test_adapt_does_not_treat_extra_presence_strings_as_or_candidates():
    """上游的第 2..n 个 presenseStrs **不能**自动变成 OR 候选串。

    这条守的是一个已经踩过的坑，方向与"怕漏判"相反：

        上游 `message` 的语义是「absence 优先」——只要反例串命中就判
        AVAILABLE，正例串再多也不参与。四字段协议要求存在侧与不存在侧
        各自独立判定，于是"多串任一命中"就变成破坏性的：掘金的
        presenseStrs = ['"user_name":', '"user_id":']，而"不存在"的响应里
        是 `"user_name":""` 和 `"user_id":"0"` —— 两个正例串都命中，
        每次判定都是 rule_ambiguous（utils/cn_selfcheck.py 实测抓到）。

    所以附加候选串只能由策展数据显式声明：作者确认那个串只在单侧出现，
    才敢写成 OR 候选。
    """
    adapted = adapt_upstream(
        "MultiString",
        {
            "checkType": "message",
            "urlProbe": "https://x.example/api/{username}",
            "presenseStrs": ["FOUND_A", "FOUND_B", "FOUND_C"],
            "absenceStrs": ["ABSENT_A", "ABSENT_B"],
        },
    )

    assert adapted["e_string"] == "FOUND_A"
    assert adapted["e_strings"] == [], "上游多串不应自动变成 OR 候选"
    assert adapted["m_string"] == "ABSENT_A"
    assert adapted["m_strings"] == []

    site = site_from_mapping("MultiString", adapted)
    assert site.e_strings == []
    assert site.m_strings == []

    # 单串时附加列表为空，不制造无意义的空元素
    single = adapt_upstream(
        "Single",
        {"checkType": "message", "presenseStrs": ["FOUND"], "absenceStrs": ["ABSENT"]},
    )
    assert single["e_strings"] == [] and single["m_strings"] == []

    # JSON 里的 null 不能变成字面量 "None" —— 那会命中任何含 "None" 的页面
    nulls = adapt_upstream(
        "Nulls",
        {"checkType": "message", "presenseStrs": ["FOUND", None], "absenceStrs": ["ABSENT", None]},
    )
    assert nulls["e_strings"] == [] and nulls["m_strings"] == []
    assert site_from_mapping("Nulls", nulls).e_strings == []


# ----------------------------------------------------------------------
# 修复 8：response_url 的语义是"禁用重定向 + 2xx + presenseStrs"
# ----------------------------------------------------------------------
def test_adapt_response_url_with_presence_keeps_string_check():
    """response_url 带正例串时必须保留字符串校验，不能降级成纯状态码。

    守住的是"任何 200（包括跳回首页）都 CLAIMED"：上游 3 个 response_url 站
    带 presenseStrs，降级成 200/404 + 空串等于把字符串校验删掉。
    """
    adapted = adapt_upstream(
        "RedirectSite",
        {
            "checkType": "response_url",
            "urlProbe": "https://x.example/{username}",
            "presenseStrs": ["PROFILE_OK"],
            "usernameClaimed": "someone",
            "usernameUnclaimed": "noonewouldeverusethis7",
        },
    )

    assert (adapted["e_code"], adapted["m_code"]) == (200, 200)
    assert adapted["e_string"] == "PROFILE_OK"

    # 没有正例串时才降级为 status_only（200/404 + 空串）
    plain = adapt_upstream(
        "PlainRedirect",
        {"checkType": "response_url", "urlProbe": "https://x.example/{username}"},
    )
    assert (plain["e_code"], plain["m_code"]) == (200, 404)
    assert plain["e_string"] == "" and plain["m_string"] == ""


# ----------------------------------------------------------------------
# 修复 9：未知字段不能被静默丢弃
# ----------------------------------------------------------------------
def test_adapt_preserves_unknown_fields_and_snake_case_aliases():
    """adapt_upstream 以 raw 为底重建：未知字段原样保留，snake_case 别名也能生效。

    守住的是"写了但静默失效"：修复前是白名单重建，overrides 里写 `url_main`
    完全不生效且没有任何告警，排查时毫无线索。
    """
    raw = {
        "checkType": "message",
        "urlProbe": "https://x.example/api/{username}",
        "presenseStrs": ["FOUND"],
        "absenceStrs": ["ABSENT"],
        "url_main": "https://snake.example/",
        "urlMain": "https://camel.example/",
        "alexa_rank": 4321,
        "foo": 1,
    }

    adapted = adapt_upstream("Aliased", raw)

    # 未知字段保留，交给 site_from_mapping 忽略（它不认识 foo 也不会报错）
    assert adapted["foo"] == 1
    assert adapted["url_main"] == "https://snake.example/"
    assert adapted["alexa_rank"] == 4321

    site = site_from_mapping("Aliased", adapted)
    assert site.url_main == "https://snake.example/"   # snake_case 优先
    assert site.alexa_rank == 4321

    # camelCase 形态同样生效
    camel = site_from_mapping(
        "Camel",
        adapt_upstream(
            "Camel",
            {
                "checkType": "message",
                "urlProbe": "https://x.example/api/{username}",
                "presenseStrs": ["FOUND"],
                "absenceStrs": ["ABSENT"],
                "urlMain": "https://camel.example/",
                "alexaRank": 99,
            },
        ),
    )
    assert camel.url_main == "https://camel.example/"
    assert camel.alexa_rank == 99


# ----------------------------------------------------------------------
# load_sites：真实库
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def upstream_keys():
    return set(load_upstream_raw())


@pytest.fixture(scope="module")
def upstream_only():
    return load_sites(use_overlays=False)


@pytest.fixture(scope="module")
def merged():
    return load_sites()


def test_use_overlays_off_reads_upstream_only(upstream_only, upstream_keys):
    """use_overlays=False 必须只读上游库，否则小 fixture 测试会莫名多出中文站。"""
    cn_only = _cn_file_keys() - upstream_keys
    assert cn_only, "前提：sites_cn.json 里应当有上游没有的站点（如小红书）"

    assert not (cn_only & set(upstream_only))
    assert all(site.source != "sites_cn" for site in upstream_only.values())
    assert not any(site.force_include for site in upstream_only.values())


def test_use_overlays_on_includes_all_cn_sites_as_forced(merged):
    """sites_cn.json 的每一条都要进库，且 force_include=True（不参与 alexaRank 排序）。"""
    cn_keys = _cn_file_keys()
    cn_sites = {name: site for name, site in merged.items() if site.source == "sites_cn"}

    assert set(cn_sites) == cn_keys
    assert all(site.force_include for site in cn_sites.values())
    assert "小红书" in cn_sites
    assert "抖音" in cn_sites


# ----------------------------------------------------------------------
# 修复 4：verified_at 的语义是"最近一次自检通过时间"
# ----------------------------------------------------------------------
def test_verified_at_is_only_set_for_upstream_verified_rules():
    """verified_at 非空 ⇔ 规则值逐字来自上游已自检数据，且 _note 没有不确定声明。

    守住的是数据诚实：verified_at 被用于存活率统计，给"未实测/需复核"的规则
    盖章就是数据造假 —— 而可信度是这个产品的全部。
    """
    upstream = load_upstream_raw()

    for name, entry in _cn_entries().items():
        if not (entry.get("verified_at") or "").strip():
            continue

        assert entry.get("valid") is True, f"{name}: valid=false 却盖了 verified_at"

        note = entry.get("_note", "")
        doubtful = [m for m in CN_DOUBT_MARKERS if m in note]
        assert not doubtful, f"{name}: _note 声明不确定（{doubtful}）却盖了 verified_at"

        # 人工实测：本分支自己打正反例测出来的规则（上游库里没有这条）。
        # 允许它盖日期，但要求比上游自检更硬 —— 必须给出正例和反例，
        # 并且 _note 里写明实测日期。只写日期不写过程仍然不算数。
        if str(entry.get("verified_source") or "").lower() == "manual":
            assert entry.get("known") and entry.get("known_absent"), (
                f"{name}: 人工实测的规则必须同时给出 known 与 known_absent"
            )
            assert "实测" in note and str(entry.get("verified_at")) in note, (
                f"{name}: 人工实测的 _note 必须写明「实测」与具体日期"
            )
            continue

        raw = upstream.get(name)
        assert raw, f"{name}: 上游没有这条规则，不可能通过上游自检"
        assert entry.get("known") and entry.get("known_absent"), (
            f"{name}: known / known_absent 必须成对存在，才谈得上自检"
        )

        # 四字段的值默认应当与"上游适配结果"一致；偏离必须写进 _note。
        #
        # 为什么允许偏离：自检会实测出上游规则的错误。掘金的 e_string 就是
        # 例子——上游给的 '"user_name":' 在"不存在"响应里也会命中，必须换成
        # '"avatar_large":"https' 才能区分。这种修正当然不在上游数组里。
        # 真正要防的是**静默编造**：改了值却不说明为什么，下一个人无从判断
        # 这是修正还是抄错。所以判据不是"必须一致"，而是"偏离必须留痕"。
        baseline = adapt_upstream(name, raw)
        deviated = [
            field
            for field in ("e_code", "e_string", "m_code", "m_string")
            if field in entry and entry[field] != baseline.get(field)
        ]
        if deviated:
            assert note.strip(), (
                f"{name}: {'/'.join(deviated)} 偏离上游适配结果，但没有 _note 说明原因"
            )


def test_every_cn_entry_declares_verified_at():
    """每条都必须显式写 verified_at（空串或日期）。

    空串是"没验证过"的显式声明，缺字段则看起来像"忘了填" —— 存活率统计
    依赖这个字段，字段的缺席与否必须能一眼看出来。
    """
    for name, entry in _cn_entries().items():
        assert "verified_at" in entry, f"{name}: 缺少 verified_at 字段"


def test_unverified_entries_have_empty_verified_at():
    """valid=false / _note 带不确定声明的条目，verified_at 必须为空。

    单独一条断言，是为了让"有人为了数字好看又填回去"时立刻失败。
    """
    entries = _cn_entries()

    # 前提：库里确实存在这两类条目，否则这条测试退化成永真
    assert any(e.get("valid") is False for e in entries.values())
    assert any(
        any(m in (e.get("_note") or "") for m in CN_DOUBT_MARKERS) for e in entries.values()
    )

    for name, entry in entries.items():
        if entry.get("valid") is False:
            assert not (entry.get("verified_at") or "").strip(), f"{name}: valid=false 却盖了章"
        if any(m in (entry.get("_note") or "") for m in CN_DOUBT_MARKERS):
            assert not (entry.get("verified_at") or "").strip(), f"{name}: _note 不确定却盖了章"


def test_qq_rule_is_marked_invalid_because_jsonp_wrapper_matches_both_sides(merged):
    """QQ 的 e_string 是 JSONP 包装函数名，存在/不存在响应都含它 → 必然双命中。

    守住的是"永远给不出不存在的规则不许标 valid"：portraitCallBack( 在两侧都出现，
    只有 m_string 出现在不存在侧，所以任何不存在响应都会同时命中 e 与 m。
    """
    qq = _cn_entries()["QQ"]

    assert qq["valid"] is False
    assert "portraitCallBack" in qq["_note"]
    assert not (qq.get("verified_at") or "").strip()
    assert merged["QQ"].valid is False


# ----------------------------------------------------------------------
# load_sites：合并优先级（临时 fixture，不依赖真实库）
# ----------------------------------------------------------------------
@pytest.fixture()
def layered_db(tmp_path, monkeypatch):
    """造一个三层库：上游有 3 站，overrides 改 2 站，sites_cn 改 1 站。"""
    upstream = {
        "sites": {
            "Alpha": {
                "checkType": "message",
                "urlMain": "https://alpha.example/",
                "url": "https://alpha.example/u/{username}",
                "urlProbe": "https://alpha.example/api/{username}",
                "presenseStrs": ["UPSTREAM_FOUND"],
                "absenceStrs": ["UPSTREAM_ABSENT"],
                "alexaRank": 1234,
                "headers": {"User-Agent": "ua-upstream", "Referer": "https://alpha.example/"},
            },
            "Beta": {
                "checkType": "message",
                "urlMain": "https://beta.example/",
                "url": "https://beta.example/u/{username}",
                "urlProbe": "https://beta.example/api/{username}",
                "presenseStrs": ["UPSTREAM_FOUND"],
                "absenceStrs": ["UPSTREAM_ABSENT"],
            },
            "Gamma": {
                "checkType": "message",
                "urlMain": "https://gamma.example/",
                "url": "https://gamma.example/u/{username}",
                "urlProbe": "https://gamma.example/api/{username}",
                "presenseStrs": ["UPSTREAM_FOUND"],
                "absenceStrs": ["UPSTREAM_ABSENT"],
            },
        }
    }
    overrides = {
        "sites": {
            "Alpha": {"e_string": "OVERRIDE_FOUND"},
            "Beta": {"e_string": "OVERRIDE_FOUND", "url_main": "https://beta-snake.example/"},
        }
    }
    cn_sites = {"sites": {"Alpha": {"e_string": "CN_FOUND", "tags": ["cn"]}}}

    upstream_path = tmp_path / "data.json"
    overrides_path = tmp_path / "overrides.json"
    cn_path = tmp_path / "sites_cn.json"
    upstream_path.write_text(json.dumps(upstream, ensure_ascii=False), encoding="utf-8")
    overrides_path.write_text(json.dumps(overrides, ensure_ascii=False), encoding="utf-8")
    cn_path.write_text(json.dumps(cn_sites, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(overlay, "OVERRIDES_FILE", overrides_path)
    monkeypatch.setattr(overlay, "SITES_CN_FILE", cn_path)

    return load_sites(db_path=str(upstream_path))


def test_merge_priority_is_cn_over_overrides_over_upstream(layered_db):
    """sites_cn > overrides > 上游：三层对同一字段的争夺必须按这个顺序收敛。"""
    assert layered_db["Alpha"].e_string == "CN_FOUND"
    assert layered_db["Beta"].e_string == "OVERRIDE_FOUND"
    assert layered_db["Gamma"].e_string == "UPSTREAM_FOUND"

    assert layered_db["Alpha"].source == "sites_cn"
    assert layered_db["Beta"].source == "overrides"
    assert layered_db["Gamma"].source == "upstream"


def test_merge_keeps_upstream_fields_not_touched_by_overlay(layered_db):
    """深合并的意义：覆盖一层只改一个字段，上游的 headers / alexaRank / 反例串仍在。"""
    alpha = layered_db["Alpha"]

    assert alpha.headers == {
        "User-Agent": "ua-upstream",
        "Referer": "https://alpha.example/",
    }
    assert alpha.alexa_rank == 1234
    assert alpha.m_string == "UPSTREAM_ABSENT"
    assert alpha.tags == ["cn"]                 # sites_cn 这一层整体替换了 tags
    assert alpha.force_include is True
    assert alpha.uri_check == "https://alpha.example/api/{account}"
    assert layered_db["Beta"].force_include is False


def test_override_snake_case_url_main_takes_effect_end_to_end(layered_db):
    """overrides 里写 snake_case 的 url_main 必须真正生效。

    守住的是"写了但静默失效"：修复前 adapt_upstream 是白名单重建，url_main
    这种白名单外的键会被丢掉，而且没有任何告警，排查时只能靠猜。
    """
    assert layered_db["Beta"].url_main == "https://beta-snake.example/"
    # 没被覆盖的站点仍用上游值
    assert layered_db["Gamma"].url_main == "https://gamma.example/"


# ----------------------------------------------------------------------
# 不变量
# ----------------------------------------------------------------------
def test_every_valid_site_in_real_db_satisfies_four_fields(merged):
    """valid=True ⇔ 四字段可判定；否则扫描器会拿一条判不出来的规则去请求，产出必然误报。"""
    assert any(site.valid for site in merged.values()), "前提：库里应当有可用站点"
    assert any(not site.valid for site in merged.values()), "前提：库里应当有被降级的站点"

    assert_four_fields(merged)  # 不抛即通过


def test_baidu_rule_separates_present_and_absent_on_the_real_payloads():
    """百度这条规则的两侧判据必须真的分得开 —— 拿实测报文验，不拿编的验。

    两个响应都是 200，所以状态码帮不上忙，全靠字符串：

        存在   {"no":0,"error":"成功","data":{...}}
        不存在 {"no":1130029,"error":"参数错误","data":[]}

    `"data":[]` 之所以比错误码可靠：不存在有两种错误码（1130029 参数错误、
    1130025 获取用户信息失败），但 `data` 都是空数组。用错误码当判据，
    换一种错误就漏判。
    """
    from types import SimpleNamespace

    from maigret.cn.verdict import Status, judge

    site = load_sites()["Baidu"]
    present = '{"no":0,"error":"\\u6210\\u529f","data":{"name_show":"maigret","sex":"female"}}'
    absent = '{"no":1130029,"error":"\\u53c2\\u6570\\u9519\\u8bef","data":[]}'
    absent_alt = '{"no":1130025,"error":"\\u83b7\\u53d6\\u7528\\u6237\\u4fe1\\u606f\\u5931\\u8d25","data":[]}'

    claimed = judge(SimpleNamespace(status=200, text=present), site, "maigret")
    assert claimed.status is Status.CLAIMED

    for body in (absent, absent_alt):
        verdict = judge(SimpleNamespace(status=200, text=body), site, "maigret")
        assert verdict.status is Status.AVAILABLE, body


def test_disqus_absent_side_answers_400_not_200(merged):
    """Disqus 的反例侧是 HTTP 400 —— 这是每日自检一直红的真因。

    2026-11-25 实测（逐条核过原始响应）：

        存在   HTTP 200 + {"code":0,"response":{...,"avatar":{"permalink":
                       "https://disqus.com/api/users/avatars/blue.jpg"}}}
        不存在 HTTP 400 + {"code":2,"response":"Invalid argument, 'user':
                       User matching query does not exist."}

    上游把这条标成 checkType=status_code，overrides 改成 message 后
    e_code 与 m_code 都被映射成 200；而 verdict._hit() 先比状态码再比串，
    于是反例侧的 400 永远对不上 200，判据串根本没机会被看到 —— 自检报
    "两侧都不命中（实际 HTTP 400）"，看起来像站点改版，实际是我们把
    反例的状态码写错了。
    """
    from types import SimpleNamespace

    from maigret.cn.verdict import Status, judge

    site = merged["Disqus"]
    assert site.valid, "Disqus 不该再是停用状态"
    assert (site.e_code, site.m_code) == (200, 400)

    present = (
        '{"code":0,"response":{"username":"blue","avatar":'
        '{"permalink":"https://disqus.com/api/users/avatars/blue.jpg"}}}'
    )
    absent = '{"code":2,"response":"Invalid argument, \'user\': User matching query does not exist."}'

    assert judge(SimpleNamespace(status=200, text=present), site, "blue").status is Status.CLAIMED
    assert (
        judge(SimpleNamespace(status=400, text=absent), site, "noonewouldeverusethis7").status
        is Status.AVAILABLE
    )
