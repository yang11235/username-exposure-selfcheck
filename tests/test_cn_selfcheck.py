"""自检脚本的 CI 契约测试（不联网）。

`utils/cn_selfcheck.py` 每天在 CI 里跑，它的两个行为直接决定这个任务有没有用：

    1. **退出码语义**：只有"规则失效"才算失败。被风控/需登录的站点占大头，
       把它们也算失败的话，任务每天都红——而"永久红"和"没有任务"等价。
    2. **输出可解析**：`--json-out` 写出来的文件必须能直接 json.load。
       踩过一次：自检会把人类可读的进度行一起打到 stdout，
       `> selfcheck.json` 拿到的是"进度行 + JSON"，json.load 在第一个字符就报错。

这两条都不能靠"跑一次看看"来保证——它们要能在离线环境里被钉死。
"""

import json
import sys
from pathlib import Path

import asyncio
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "utils"))

import cn_selfcheck as sc  # noqa: E402


# ---------------------------------------------------------------------------
# 退出码
# ---------------------------------------------------------------------------
def test_all_pass_exits_zero():
    assert sc.exit_code([{"result": "PASS"}, {"result": "PASS"}]) == 0


def test_env_limited_failures_do_not_fail_the_build():
    """被风控、需登录、超时——改规则也修不好，不能算失败。"""
    results = [
        {"site": "小红书", "result": "FAIL", "kind": "ENV"},
        {"site": "抖音", "result": "FAIL", "kind": "ENV"},
        {"site": "Gitee", "result": "PASS"},
    ]

    assert sc.exit_code(results) == 0


def test_rule_failures_fail_the_build():
    results = [{"site": "Juejin", "result": "FAIL", "kind": "RULE"}]

    assert sc.exit_code(results) == 1


def test_rule_failure_wins_over_env_failures():
    results = [
        {"site": "小红书", "result": "FAIL", "kind": "ENV"},
        {"site": "Juejin", "result": "FAIL", "kind": "RULE"},
    ]

    assert sc.exit_code(results) == 1


def test_empty_results_exit_zero():
    """没有可自检站点时 main_async 会返回 1（无法执行），但 exit_code 本身
    只对"跑完的结果"负责。这里固定住边界，避免以后有人把两种情况混在一起。"""
    assert sc.exit_code([]) == 0


def test_skip_rows_are_not_failures():
    assert sc.exit_code([{"result": "SKIP"}]) == 0


# ---------------------------------------------------------------------------
# 输出流
# ---------------------------------------------------------------------------
def test_progress_goes_to_stdout_by_default(capsys):
    sc._JSON_MODE = False
    sc._progress("hello")

    captured = capsys.readouterr()
    assert captured.out.strip() == "hello"
    assert captured.err == ""


def test_progress_goes_to_stderr_in_json_mode(capsys):
    """JSON 模式下 stdout 必须干净，否则 `--json | jq` 直接崩。"""
    sc._JSON_MODE = True
    try:
        sc._progress("hello")
    finally:
        sc._JSON_MODE = False

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "hello"


# ---------------------------------------------------------------------------
# --json-out
# ---------------------------------------------------------------------------
def test_json_out_writes_parseable_file(tmp_path, monkeypatch, capsys):
    """写出来的文件必须能直接 json.load——这是 CI 的硬要求。"""
    payload = [
        {"site": "Gitee", "result": "PASS", "rows": [], "detail": ""},
        {"site": "Juejin", "result": "PASS", "rows": [], "detail": ""},
    ]

    async def fake_main_async(args):
        sc._progress("进度行，不该进 JSON 文件")
        return payload

    monkeypatch.setattr(sc, "main_async", fake_main_async)
    target = tmp_path / "selfcheck.json"
    monkeypatch.setattr(
        sys, "argv", ["cn_selfcheck.py", "--json-out", str(target)]
    )

    result_code = sc.main()

    assert result_code == 0
    assert target.exists()
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed == payload


def test_json_mode_is_restored_between_calls(monkeypatch, tmp_path):
    """_JSON_MODE 是模块级全局，跑完必须回到 False。

    不还原的话，同进程里后续的人类可读输出会全部跑到 stderr 去，
    表现为"命令行上什么都看不到"。
    """
    async def fake_main_async(args):
        return []

    monkeypatch.setattr(sc, "main_async", fake_main_async)
    monkeypatch.setattr(sys, "argv", ["cn_selfcheck.py", "--json"])
    sc.main()

    assert sc._JSON_MODE is True  # 本次调用确实开了
    # 下一次不带 --json 的调用必须回到人类可读模式
    monkeypatch.setattr(sys, "argv", ["cn_selfcheck.py"])
    sc.main()
    assert sc._JSON_MODE is False


# ---------------------------------------------------------------------------
# RULE / ENV 分类
# ---------------------------------------------------------------------------
def row(http=200, reason="structure_changed"):
    return {"account": "x", "expected": "Claimed", "got": "Unknown",
            "reason": reason, "confidence": 0.0, "http": http, "ok": False}


def test_classify_network_failure_as_env():
    assert sc.classify([row(http=None)]) == "ENV"


def test_classify_blocked_as_env():
    assert sc.classify([row(http=200, reason="blocked")]) == "ENV"


def test_classify_missing_strings_on_a_normal_200_as_rule():
    """请求通了、状态码也是预期的，但判据串对不上——这才是规则真的过期了。"""
    assert sc.classify([row(http=200, reason="structure_changed")]) == "RULE"


def test_classify_anti_bot_status_as_env():
    """反爬状态码不是规则问题。

    实测 2026-11-14：SegmentFault 对正例/反例都返回 468（它的反爬码）。
    旧逻辑把它算成"规则失效"，于是每日 CI 为一件改规则修不好的事变红——
    这正是自检模块自己警告过的"把告警训练成噪音"。
    """
    assert sc.classify([row(http=468, reason="structure_changed")]) == "ENV"
    assert sc.classify([row(http=429)]) == "ENV"
    assert sc.classify([row(http=403)]) == "ENV"


def test_classify_5xx_as_env():
    """服务端自己挂了，和规则无关。"""
    assert sc.classify([row(http=502)]) == "ENV"


def test_classify_empty_as_env():
    """一条结果都没有时不能判成规则失效——那会让 CI 因为别的原因变红。"""
    assert sc.classify([]) == "ENV"


def test_anti_bot_diagnosis_does_not_point_at_rules():
    """诊断文案不能引导维护者去改 e_string/m_string。

    改了也没用（还是 468），下次跑还是红，白折腾一轮。
    """
    site = type("S", (), {"e_string": "a", "m_string": "b", "e_code": 200, "m_code": 404})()
    text = sc._diagnose(row(http=468), site, "ENV")

    assert "468" in text
    assert "不是规则问题" in text
    assert "e_string" not in text


def test_classify_status_code_changed_on_status_only_site_is_rule():
    """status_only 站点换了状态码，就是规则失效——必须红。

    这类规则 `e_string`/`m_string` 全空，状态码是判据的全部（全库 8347 条）。
    曾经想用"状态码不在 e_code/m_code 里"来识别"对方拒绝请求"，那会把
    这一整类真规则错误静默成 ENV：站点把不存在页从 404 改成 410，
    除了改 m_code 没有别的修法，但用户永远收不到通知。
    """
    site = type("S", (), {"e_code": 200, "m_code": 404, "e_string": "", "m_string": ""})()

    assert sc.classify([row(http=410)], site) == "RULE"


def test_classify_url_moved_404_on_message_site_is_rule():
    """message 型站点（e=m=200）接口搬走返回 404——也是规则要改。

    这种情况和"别人家 API key 失效"在响应上完全同形，没法靠状态码区分。
    分不清的时候按 RULE 处理：宁可让人看一眼，也不能让它永久沉底。
    """
    site = type("S", (), {"e_code": 200, "m_code": 200, "e_string": "a", "m_string": "b"})()

    assert sc.classify([row(http=404)], site) == "RULE"


def test_classify_matching_status_with_bad_strings_is_still_rule():
    """状态码符合预期、只是判据串对不上——规则过期。"""
    site = type("S", (), {"e_code": 200, "m_code": 404, "e_string": "a", "m_string": "b"})()

    assert sc.classify([row(http=200)], site) == "RULE"


def test_classify_uses_all_attempts_not_just_the_last():
    """结论不能被抖动顺序左右。

    最后一次是 None（网络抖），但前面已经拿到过 200 且判据不命中——
    页面确实拿到了、判据串确实不在，这是规则过期的铁证。
    只看最后一次会判 ENV（"网络失败"），把真规则错误静默掉。
    """
    site = type("S", (), {"e_code": 200, "m_code": 404, "e_string": "a", "m_string": "b"})()
    r = row(http=None)
    r["history"] = [
        {"http": 200, "got": "Unknown", "reason": "structure_changed"},
        {"http": None, "got": "Unknown", "reason": ""},
    ]

    assert sc.classify([r], site) == "RULE"


def test_classify_does_not_let_one_5xx_veto_a_decidable_attempt():
    """一次 5xx 不能否决掉"页面拿到了但判据不命中"。

    重试机制让"先 500、再 200 且判据不命中"真实可达：服务端抖了一下，
    重试拿到页面，页面里已经没有判据串——这是规则过期，不是环境问题。
    按"一票 ENV 否决"会把它归成环境受限，等于用新的静默替换旧的静默。
    """
    site = type("S", (), {"e_code": 200, "m_code": 404, "e_string": "a", "m_string": "b"})()
    r = row(http=200)
    r["history"] = [
        {"http": 500, "got": "Unknown", "reason": "structure_changed"},
        {"http": 200, "got": "Unknown", "reason": "structure_changed"},
    ]

    assert sc.classify([r], site) == "RULE"


def test_classify_all_env_attempts_is_env():
    """全都不可判定才是 ENV。"""
    site = type("S", (), {"e_code": 200, "m_code": 404, "e_string": "a", "m_string": "b"})()
    r = row(http=None)
    r["history"] = [
        {"http": None, "got": "Unknown", "reason": ""},
        {"http": 502, "got": "Unknown", "reason": "structure_changed"},
    ]

    assert sc.classify([r], site) == "ENV"


def test_diagnose_uses_the_deciding_attempt_not_the_first_failed_one():
    """诊断要解释"做出结论"的那一侧，不是排在前面的那一侧。

    典型错位：正例被 403 风控、反例正常返回但不含判据串。kind 是 RULE
    （反例那侧有可判定证据、CI 要红），failed[0] 却是被风控的正例行。
    拿它去解释就会输出"不是规则错误，需要 Cookie"——维护者被指向相反方向。
    """
    site = type("S", (), {"e_string": "a", "m_string": "b", "e_code": 200, "m_code": 404})()
    pos = {
        "account": "pos", "expected": "Claimed", "got": "Unknown",
        "reason": "blocked", "confidence": 0.0, "http": 403, "ok": False,
        "history": [{"http": 403, "got": "Unknown", "reason": "blocked"}],
    }
    neg = {
        "account": "neg", "expected": "Available", "got": "Unknown",
        "reason": "structure_changed", "confidence": 0.0, "http": 200, "ok": False,
        "history": [{"http": 200, "got": "Unknown", "reason": "structure_changed"}],
    }
    failed = [pos, neg]

    kind = sc.classify(failed, site)
    assert kind == "RULE"

    deciding = sc._deciding_row(failed, kind)
    assert deciding["account"] == "neg"  # 证据在反例那侧，不是被风控的正例

    text = sc._diagnose(deciding, site, kind)
    assert "不是规则错误" not in text
    assert "需要 Cookie" not in text


def test_rule_kind_never_says_not_a_rule_problem():
    """kind=RULE 的文案里不能出现"不是规则问题"这类反向措辞。

    CI 已经因为 RULE 变红了，文案再说不是规则问题，等于让维护者别管它。
    """
    site = type("S", (), {"e_string": "a", "m_string": "b", "e_code": 200, "m_code": 404})()
    for reason in ("structure_changed", "rule_ambiguous", "rule_missing", "unknown_reason"):
        text = sc._diagnose(
            {"account": "u", "expected": "Claimed", "got": "Unknown", "reason": reason,
             "confidence": 0.0, "http": 200, "ok": False},
            site, "RULE",
        )
        assert "不是规则错误" not in text, reason
        assert "需要 Cookie" not in text, reason
        assert "与判据串无关" not in text, reason


def test_diagnose_matches_kind():
    """kind 与 detail 不能打架。

    kind=RULE（要红）而文案说"网络请求失败，与判据串无关"，维护者会被
    指向两个相反的方向；反之 kind=ENV（不红）而文案教他改规则，更糟。
    """
    site = type("S", (), {"e_string": "a", "m_string": "b", "e_code": 200, "m_code": 404})()

    env_text = sc._diagnose(row(http=500), site, "ENV")
    assert "5xx" not in env_text and "e_string" not in env_text
    assert "服务端故障" in env_text

    rule_text = sc._diagnose(row(http=200), site, "RULE")
    assert "e_string" in rule_text


def test_diagnose_5xx_says_server_fault_not_realign_status():
    """5xx 是服务端挂了，让维护者去"对齐状态码"是错误建议。"""
    site = type("S", (), {"e_string": "", "m_string": "", "e_code": 200, "m_code": 404})()
    text = sc._diagnose(row(http=503), site, "ENV")

    assert "服务端" in text
    assert "对齐" not in text


def test_status_mismatch_diagnosis_points_at_the_status_code():
    """文案要说清"状态码要先对齐"，但不能断言"这是外部故障"。

    接口搬家、URL 拼错、e_code 笔误都会表现成"码不对"，凭响应分不出来。
    早先的文案直接写"不是判据串的问题，确认 API key 是否失效"，那是
    过拟合到 Disqus 一个案例，会劝退"URL 拼错了"这种正确修复。
    """
    site = type("S", (), {"e_string": "a", "m_string": "b", "e_code": 200, "m_code": 404})()
    text = sc._diagnose(row(http=400), site, "RULE")

    assert "400" in text
    assert "状态码" in text
    assert "API key" not in text  # 不替人下"是外部 key 失效"的结论


def test_network_failure_diagnosis_does_not_mention_strings():
    """没拿到响应时页面根本不存在，谈判据串是误导。

    原来这条排在 structure_changed 之后，网络失败会输出
    "两侧都不命中：e_string=... 与当前页面不符（实际 HTTP None）"——
    维护者会去改串，而问题在网络上。
    """
    site = type("S", (), {"e_string": "a", "m_string": "b", "e_code": 200, "m_code": 404})()
    text = sc._diagnose(row(http=None), site, "ENV")

    assert "网络" in text
    assert "e_string" not in text
    assert "None" not in text


# ---------------------------------------------------------------------------
# 接口拒绝请求（配额 / 密钥）：是环境问题，不是规则失效
# ---------------------------------------------------------------------------
# 2026-11-24 的每日自检就红在这里：Disqus 两侧都是 HTTP 400，正文里两个判据串
# 都不出现。那是 GitHub runner 的共享出口 IP 把公共 API key 的配额用光了，
# 对方回的是 {"code":18,"response":"Usage limit exceeded"} 这类信封——
# 不是"页面改版"，改 e_string 改一万遍也不会好。
QUOTA_BODY = '{"code":18,"response":"Usage limit exceeded"}'


def _disqus_site():
    return type(
        "S", (),
        {
            "name": "Disqus",
            "executor": "http",
            "known": ["blue"],
            "known_absent": ["noonewouldeverusethis7"],
            "uri_check": "https://example.invalid/{account}",
            "e_code": 200,
            "m_code": 400,
            "e_string": "https://disqus.com/api/users/",
            "m_string": "User matching query does not exist",
        },
    )()


def test_api_error_is_env_not_rule():
    r = row(http=400, reason="structure_changed")
    r["api_error"] = "Usage limit exceeded"
    assert sc.classify([r]) == "ENV"


def test_api_error_diagnosis_names_the_rejection():
    """文案要写出接口原话，否则"400 + 判据不命中"和"页面改版"长得一模一样。"""
    r = row(http=400, reason="structure_changed")
    r["api_error"] = "Usage limit exceeded"

    text = sc._diagnose(r, _disqus_site(), "ENV")

    assert "接口拒绝" in text
    assert "Usage limit exceeded" in text
    assert "e_string" not in text


def test_api_error_is_only_detected_when_no_needle_matches():
    """识别范围要钉死：反例侧是同一个 400 信封，但那是**有效答案**。"""
    site = _disqus_site()
    assert sc._api_error(site, 400, QUOTA_BODY) == "Usage limit exceeded"

    absent = '{"code":2,"response":"Invalid argument, \'user\': User matching query does not exist."}'
    assert sc._api_error(site, 400, absent) == "", "m_string 命中的响应不能被截胡成 ENV"

    # 正常 200 的 JSON API 响应也可能带 code/response 字段，不能算拒绝
    assert sc._api_error(site, 200, QUOTA_BODY) == ""
    # 不是信封的 400（网关错误页、纯文本）仍按原逻辑走，不能被静默
    assert sc._api_error(site, 400, "<html>Bad Request</html>") == ""


@pytest.mark.asyncio
async def test_api_error_is_not_retried(monkeypatch):
    """配额被拒不会因为等 3 秒就好，多打一次只是把公共 key 烧得更快。"""
    calls = []

    async def fake_fetch(session, url):
        calls.append(url)
        return 400, QUOTA_BODY

    monkeypatch.setattr(sc, "fetch", fake_fetch)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)
    monkeypatch.setattr(sc, "RETRY_DELAY_SECONDS", 0)

    result = await sc.check_site(None, _disqus_site())

    assert len(calls) == 2  # 两个样本各一次，都没有重试
    assert result["result"] == "FAIL"
    assert result["kind"] == "ENV"


# ---------------------------------------------------------------------------
# 只验四字段协议：非 http 执行器必须 SKIP
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_executor_is_skipped_not_failed():
    """search / browser 执行器不走 e_string/m_string，必须跳过。

    这类站点通常根本没配 e_code/m_code（判据在 executors/search.py 里）。
    交给 judge() 只会得到 RULE_MISSING，被 classify 归成 RULE，
    于是每日自检永久变红——而"永久红"和"没有任务"等价。
    """
    site = type(
        "S", (),
        {
            "name": "NetEase Music (搜索)",
            "executor": "search",
            "known": ["wizzer"],
            "known_absent": ["zzqqxx998877zz"],
            "uri_check": "https://example.invalid/s?q={account}",
        },
    )()

    result = await sc.check_site(None, site)

    assert result["result"] == "SKIP"
    assert "search" in result["detail"]
    assert sc.exit_code([result]) == 0


@pytest.mark.asyncio
async def test_http_executor_still_runs(monkeypatch):
    """跳过逻辑不能顺手把 http 执行器也跳过——那是自检的全部意义。"""
    site = type(
        "S", (),
        {
            "name": "Gitee",
            "executor": "http",
            "known": ["a"],
            "known_absent": ["b"],
            "uri_check": "https://example.invalid/{account}",
        },
    )()

    async def fake_fetch(session, url):
        return 200, "ok"

    monkeypatch.setattr(sc, "fetch", fake_fetch)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)
    monkeypatch.setattr(
        sc, "judge",
        lambda resp, s, username: type(
            "V", (), {"status": sc.Status.CLAIMED if username == "a" else sc.Status.AVAILABLE,
                      "reason": None, "confidence": 0.5},
        )(),
    )

    result = await sc.check_site(None, site)

    assert result["result"] == "PASS"


# ---------------------------------------------------------------------------
# 失败重试：站点抖动不能让每日 CI 变红
# ---------------------------------------------------------------------------
def _http_site(name="Sina Blog", known=("a",), absent=("b",)):
    return type(
        "S", (),
        {
            "name": name,
            "executor": "http",
            "known": list(known),
            "known_absent": list(absent),
            "uri_check": "https://example.invalid/{account}",
            "e_code": 200,
            "m_code": 404,
            "e_string": "x",
            "m_string": "",
        },
    )()


def _fake_judge(resp, s, username):
    """按状态码出结论：200 → Claimed，404 → Available，其余 Unknown。"""
    status = resp.status
    if status == 200:
        st = sc.Status.CLAIMED
    elif status == 404:
        st = sc.Status.AVAILABLE
    else:
        st = sc.Status.UNKNOWN
    return type("V", (), {"status": st, "reason": None, "confidence": 0.5})()


@pytest.mark.asyncio
async def test_transient_failure_is_retried_and_passes(monkeypatch):
    """偶发 404 一次不能让整个站点判失败。

    实测 2026-11-22：新浪博客同一个正例账号连打 20 次全 200，但偶发一次 404，
    那一次就让每日 CI 变红——"每天随机红一次"和"没有告警"是等价的。
    """
    calls = {"a": 0}

    async def fake_fetch(session, url):
        if url.endswith("/a"):
            calls["a"] += 1
            return (404, "") if calls["a"] == 1 else (200, "ok")
        return 404, ""

    monkeypatch.setattr(sc, "fetch", fake_fetch)
    monkeypatch.setattr(sc, "judge", _fake_judge)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)
    monkeypatch.setattr(sc, "RETRY_DELAY_SECONDS", 0)

    result = await sc.check_site(None, _http_site())

    assert result["result"] == "PASS"
    assert calls["a"] == 2  # 第一次失败后确实重试了


@pytest.mark.asyncio
async def test_persistent_failure_is_still_rule(monkeypatch):
    """重试不能把"规则真错了"也放过——两次都错才是真错。"""
    async def fake_fetch(session, url):
        if url.endswith("/a"):
            return 200, "页面里没有判据串"
        return 404, ""

    monkeypatch.setattr(sc, "fetch", fake_fetch)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)
    monkeypatch.setattr(sc, "RETRY_DELAY_SECONDS", 0)

    # 正例返回 200 但判据串不命中 → structure_changed，这是真的规则过期。
    # 用字符串而不是 Reason 枚举：classify 比较的就是 .value。
    monkeypatch.setattr(
        sc, "judge",
        lambda resp, s, username: type(
            "V", (), {"status": sc.Status.UNKNOWN,
                      "reason": type("R", (), {"value": "structure_changed"})(),
                      "confidence": 0.0},
        )(),
    )

    result = await sc.check_site(None, _http_site())

    assert result["result"] == "FAIL"
    assert result["kind"] == "RULE"


@pytest.mark.asyncio
async def test_retry_does_not_rerun_successful_side(monkeypatch):
    """通过的样本不该被重打——每个站点两条请求是自检的请求预算。"""
    calls = []

    async def fake_fetch(session, url):
        calls.append(url)
        return (200, "ok") if url.endswith("/a") else (404, "")

    monkeypatch.setattr(sc, "fetch", fake_fetch)
    monkeypatch.setattr(sc, "judge", _fake_judge)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)

    result = await sc.check_site(None, _http_site())

    assert result["result"] == "PASS"
    assert len(calls) == 2  # 正例一次、反例一次，没有多余重试


@pytest.mark.asyncio
async def test_anti_bot_code_is_not_retried(monkeypatch):
    """反爬码不重试——它说的是"你在被限制"，再打只会把封禁拖长。

    注意 404 是**要**重试的：新浪博客的抖动就长成偶发 404。
    """
    calls = []

    async def fake_fetch(session, url):
        calls.append(url)
        return 468, "blocked"

    monkeypatch.setattr(sc, "fetch", fake_fetch)
    monkeypatch.setattr(sc, "judge", _fake_judge)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)
    monkeypatch.setattr(sc, "RETRY_DELAY_SECONDS", 0)

    await sc.check_site(None, _http_site())

    assert len(calls) == 2  # 两个样本各一次，都没有重试


def test_worth_retrying_matrix():
    """重试口径要钉死，否则调它的时候没人知道会影响到哪些站。

    除反爬码外都重试：404 / 400 也重试，因为新浪博客的抖动就长成偶发 404；
    468 / 429 / 403 不重试，重打只会把封禁拖长。
    """
    assert sc._worth_retrying(404) is True
    assert sc._worth_retrying(400) is True
    assert sc._worth_retrying(None) is True   # 网络没通
    assert sc._worth_retrying(502) is True    # 服务端瞬时故障
    assert sc._worth_retrying(468) is False   # SegmentFault 反爬码
    assert sc._worth_retrying(429) is False
    assert sc._worth_retrying(403) is False


@pytest.mark.asyncio
async def test_network_failure_is_retried(monkeypatch):
    """网络没通是"结果不确定"，值得再试一次。"""
    calls = {"a": 0}

    async def fake_fetch(session, url):
        if url.endswith("/a"):
            calls["a"] += 1
            return (None, "__ERROR__") if calls["a"] == 1 else (200, "ok")
        return 404, ""

    monkeypatch.setattr(sc, "fetch", fake_fetch)
    monkeypatch.setattr(sc, "judge", _fake_judge)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)
    monkeypatch.setattr(sc, "RETRY_DELAY_SECONDS", 0)

    result = await sc.check_site(None, _http_site())

    assert result["result"] == "PASS"
    assert calls["a"] == 2


@pytest.mark.asyncio
async def test_time_budget_marks_remaining_as_skip(monkeypatch):
    """超预算的站点记 SKIP，不能记 FAIL。

    记 FAIL 的话，网络大面积不通那天，剩下的站会被集体误报成"规则失效"——
    而这批站根本没被检查过。
    """
    monkeypatch.setattr(sc, "BUDGET_SECONDS", 0)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)

    async def fake_check_site(session, site, deadline=None):
        return {"site": site.name, "result": "PASS", "rows": [], "detail": ""}

    monkeypatch.setattr(sc, "check_site", fake_check_site)

    class Args:
        site = None

    results = await sc.main_async(Args())

    # 结果里会有停用规则留下的 DISABLED 行，它们本来就不发请求，不算"超预算"
    checked = [r for r in results if r["result"] != "DISABLED"]
    assert checked, "应该有被检查的站点"
    assert all(r["result"] == "SKIP" for r in checked)
    assert sc.exit_code(results) == 0


@pytest.mark.asyncio
async def test_unchecked_second_sample_keeps_rule_failure(monkeypatch):
    """正例已判定规则失效、反例因预算没查：结论要保留，不能降成 SKIP。

    "200 且判据串不在页面上"是规则失效的确凿证据，跟反例有没有来得及查
    无关。把整站降成 SKIP 等于用一个没做的检查去否掉一个已经做出来的检查。
    """
    async def fake_fetch(session, url):
        await asyncio.sleep(0.05)
        return 200, "no match"

    def fake_judge(resp, s, username):
        return type(
            "V", (),
            {"status": sc.Status.UNKNOWN,
             "reason": type("R", (), {"value": "structure_changed"})(),
             "confidence": 0.0},
        )()

    monkeypatch.setattr(sc, "fetch", fake_fetch)
    monkeypatch.setattr(sc, "judge", fake_judge)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)
    monkeypatch.setattr(sc, "RETRY_DELAY_SECONDS", 0)

    import time

    result = await sc.check_site(None, _http_site(), deadline=time.monotonic() + 0.02)

    assert result["result"] == "FAIL"
    assert result["kind"] == "RULE"
    # 要说明另一半没查，否则会以为两侧都验过了
    assert "未检查" in result["detail"]


@pytest.mark.asyncio
async def test_all_samples_unchecked_is_skip(monkeypatch):
    """全都没查（预算一开始就用完）：记 SKIP，不能凭空造出 FAIL。"""
    import time

    async def fake_fetch(session, url):  # pragma: no cover - 不应该被调用
        raise AssertionError("预算已过，不该发请求")

    monkeypatch.setattr(sc, "fetch", fake_fetch)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)

    result = await sc.check_site(None, _http_site(), deadline=time.monotonic() - 1)

    assert result["result"] == "SKIP"
    assert sc.exit_code([result]) == 0


def test_disabled_rules_stay_visible():
    """valid=false 的规则要看板留痕，不能从视野里消失。

    只是把它们从待检列表里过滤掉，"这条规则被停用了"这件事就没人记得——
    停用是临时措施，不是删除。

    断言整行而不是 "停用" 两个字：模板正文里本来就有"停用"，那种断言是空转
    （DISABLED 行被误写成 SKIP 也照样通过）。
    """
    results = [
        {"site": "Juejin", "result": "PASS", "rows": [], "detail": ""},
        {"site": "Disqus", "result": "DISABLED", "detail": "规则已停用（valid=false），本次未检查"},
    ]
    assert sc.exit_code(results) == 0  # 停用不算失败

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cn-health.md"
        sc.write_doc(results, p)
        text = p.read_text(encoding="utf-8")

    assert "| Disqus | DISABLED |" in text


# ---------------------------------------------------------------------------
# 只装 aiohttp 的环境里也必须能 import
# ---------------------------------------------------------------------------
def test_cn_namespace_bootstraps_without_full_deps(monkeypatch):
    """CI 只装 aiohttp，`maigret/__init__.py` 会因缺 aiodns 等直接抛 ImportError。

    自检一次都没跑起来的那次故障就是它：脚本在 import 阶段就死了，
    根本走不到"跑规则"那一步，日志里只有一句"缺依赖"。
    这里模拟"maigret 包初始化失败"，确认降级命名空间能让 maigret.cn 的
    纯标准库模块照常导入。
    """
    saved = {k: v for k, v in sys.modules.items() if k == "maigret" or k.startswith("maigret.")}
    for key in saved:
        del sys.modules[key]

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "maigret":
            raise ImportError("Missing required dependency while starting Maigret.")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    try:
        sc._bootstrap_cn_namespace()

        pkg = sys.modules["maigret"]
        assert pkg.__path__ == [str(sc.ROOT / "maigret")]
        # 没有执行 __init__.py，所以不会有 checking/maigret 这些重依赖模块
        assert not hasattr(pkg, "search")
    finally:
        monkeypatch.undo()
        for key in [k for k in sys.modules if k == "maigret" or k.startswith("maigret.")]:
            del sys.modules[key]
        sys.modules.update(saved)



# ---------------------------------------------------------------------------
# 变异存活点：以下每条都对应一个"改坏了但测试仍绿"的位置
# ---------------------------------------------------------------------------
def test_main_returns_error_when_no_sites(monkeypatch, tmp_path):
    """没有可查站点时：退出码 1，且不写 --json-out。

    写个 [] 进去，下游 json.load 拿到空列表会以为"全都通过了"；
    返回 0 则 CI 把"什么都没查"当成成功。两者都是静默。
    """
    async def fake_main_async(args):
        return []

    monkeypatch.setattr(sc, "main_async", fake_main_async)
    target = tmp_path / "selfcheck.json"
    monkeypatch.setattr(sys, "argv", ["cn_selfcheck.py", "--json-out", str(target)])

    code = sc.main()

    assert code == 1
    assert not target.exists()


def test_main_skips_doc_for_partial_run(monkeypatch, tmp_path):
    """--site 时不能写看板：局部结果覆盖全量快照，再 force push 就找不回来。"""
    doc = tmp_path / "cn-health.md"
    doc.write_text("原始看板", encoding="utf-8")
    written = []

    async def fake_main_async(args):
        return [{"site": "Juejin", "result": "PASS", "rows": [], "detail": ""}]

    monkeypatch.setattr(sc, "main_async", fake_main_async)
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    monkeypatch.setattr(sc, "write_doc", lambda results, path: written.append(path))
    monkeypatch.setattr(sys, "argv", ["cn_selfcheck.py", "--site", "Juejin", "--write-doc"])

    sc.main()

    assert written == [], "指定 --site 时不该写看板"


@pytest.mark.asyncio
async def test_main_async_emits_disabled_rows(monkeypatch):
    """valid=false 的站点要真的产出 DISABLED 行，不是只被过滤掉。

    这条钉的是接线：过滤逻辑写在 main_async 里，只测 write_doc 的渲染
    证明不了它有没有被调用。
    """
    real_load = sc.load_sites

    def fake_load():
        sites = real_load()
        # 取一个真实的策展站，临时把它标成停用
        name = next(n for n, s in sites.items() if s.source in ("sites_cn", "overrides") and s.valid)
        sites[name].valid = False
        return sites

    monkeypatch.setattr(sc, "load_sites", fake_load)
    monkeypatch.setattr(sc, "BUDGET_SECONDS", 0)  # 别的站一律走超预算 → SKIP，不发请求

    class Args:
        site = None

    results = await sc.main_async(Args())

    disabled = [r for r in results if r["result"] == "DISABLED"]
    assert disabled, "停用的站点必须有一条 DISABLED 行"
    assert sc.exit_code(results) == 0


def test_write_doc_reports_flaky_sites():
    """重试后通过的站点必须单列，否则"在抖"这条信息被吞掉。"""
    import tempfile

    results = [
        {"site": "Juejin", "result": "PASS",
         "rows": [{"account": "a", "attempts": 1, "expected": "Claimed",
                   "got": "Claimed", "reason": "", "confidence": 0.5, "ok": True}],
         "detail": ""},
        {"site": "Ruby China", "result": "PASS",
         "rows": [{"account": "b", "attempts": 2, "expected": "Claimed",
                   "got": "Claimed", "reason": "", "confidence": 0.5, "ok": True}],
         "detail": ""},
    ]

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cn-health.md"
        sc.write_doc(results, p)
        text = p.read_text(encoding="utf-8")

    assert "抖动" in text
    assert "| Ruby China | PASS | - | 重试 2 次后通过（抖动） |" in text


def test_sites_from_env_splits_on_newline_and_comma(monkeypatch):
    """含空格的站名必须能整串传进来。

    `Sina Blog` 是真实站名。走 shell 展开时，不加引号会被词分割成
    `Sina` 和 `Blog` 两个站、加引号又整串当一个——两种都是错的，
    所以 CI 走环境变量、由脚本自己切分。
    """
    monkeypatch.setenv("CN_SELFCHECK_SITES", "Sina Blog\nJuejin, NetEase Music (搜索)")
    assert sc._sites_from_env() == ["Sina Blog", "Juejin", "NetEase Music (搜索)"]

    monkeypatch.setenv("CN_SELFCHECK_SITES", "   ")
    assert sc._sites_from_env() is None


@pytest.mark.asyncio
async def test_check_site_wires_deciding_row_into_detail(monkeypatch):
    """端到端：check_site 产出的 kind 与 detail 必须一致。

    单独测 _deciding_row 证明不了它被接进了 check_site——把
    `_diagnose(_deciding_row(...))` 改回 `_diagnose(failed[0])`，那种测试
    仍然全绿。这里走完整链路：正例被 403 风控、反例 200 但不含判据串。
    """
    site = _http_site()

    def fake_judge(resp, s, username):
        if resp.status == 403:
            return type("V", (), {"status": sc.Status.UNKNOWN,
                                  "reason": type("R", (), {"value": "blocked"})(),
                                  "confidence": 0.0})()
        return type("V", (), {"status": sc.Status.UNKNOWN,
                              "reason": type("R", (), {"value": "structure_changed"})(),
                              "confidence": 0.0})()

    async def fake_fetch(session, url):
        return (403, "blocked") if url.endswith("/a") else (200, "no match")

    monkeypatch.setattr(sc, "fetch", fake_fetch)
    monkeypatch.setattr(sc, "judge", fake_judge)
    monkeypatch.setattr(sc, "DELAY_SECONDS", 0)
    monkeypatch.setattr(sc, "RETRY_DELAY_SECONDS", 0)

    result = await sc.check_site(None, site)

    assert result["kind"] == "RULE"
    # 文案必须解释反例那侧（结构不符），不能解释被风控的正例。
    # 断言"指向哪一侧"而不是"不含某句话"：删掉 ENV 措辞之后，
    # 错误接线会退化成一句含糊的"规则判不出来（原因 blocked）"，
    # 不含禁词但依然把人指向被风控的那一侧。
    assert "blocked" not in result["detail"]
    assert "两侧都不命中" in result["detail"] or "判据串" in result["detail"]
