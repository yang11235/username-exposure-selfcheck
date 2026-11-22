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
    text = sc._diagnose(row(http=468), site)

    assert "468" in text
    assert "不是规则问题" in text
    assert "e_string" not in text


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

