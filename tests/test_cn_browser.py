"""浏览器执行器测试（全程不启动浏览器）。

为什么要专门测这一层：CDP 是"WebSocket + JSON-RPC"，看起来简单，但有三个
地方错了会**静默**出问题——不报错，只是结果不对或永远超时：

    1. 响应乱序：CDP 允许并发命令，响应按完成顺序回来，不能假设"下一个就是我的"
    2. 事件早到：`Page.loadEventFired` 可能在 `Page.navigate` 返回之前就到达
    3. 事件缓存被消耗：没人等的事件必须存起来，否则下一个等待者会等到超时

这三条都没法用"跑一次真浏览器"稳定复现，只能靠 FakeTransport 精确构造。

真正的浏览器联调在 `utils/cn_selfcheck.py` 和 CI 之外手工做，不进单元测试——
测试依赖一个跑着的 Chrome 是不可接受的（CI 上没有，用户机器上也未必有）。
"""

import asyncio
import json

import pytest

from maigret.cn.executors import browser as browser_mod
from maigret.cn.executors.cdp import CDPBrowser, CDPSession, CDPError, pick_page_target
from maigret.cn.executors.browser import BrowserExecutor, fetch_site_response
from maigret.cn.model import Site
from maigret.cn.verdict import Reason
from maigret.result import MaigretCheckStatus


def run(coro):
    """在同步测试里跑协程。

    不用 pytest-asyncio：那是个额外依赖，而本项目的测试要能在裸环境跑起来
    （用户自己 clone 下来应该 `pytest` 就能过）。
    """
    return asyncio.run(coro)


class FakeTransport:
    """按脚本回放的传输层。

    `script` 是"允许到达的帧列表"，**顺序即到达顺序，与命令顺序无关**——
    这正是要测的乱序场景。没有 id 的项是事件帧，用来构造"事件早到"。

    一条必须遵守的纪律：**带 id 的响应要等对应命令发出去之后才能到达**。
    真实 CDP 里不存在"先收到 id=2 的响应，再发出第 2 条命令"。
    如果 fake 不守这条，读循环会在命令还没发的时候就收到响应、按 id 查不到
    等待者而丢弃，测试就变成在验证一个现实中不存在的时序。

    （这正是最初这个 fake 的毛病：它按位置吐帧，结果两条响应在第一条命令
    发出前就被读完了，随后命令真正发出时反而永远等不到响应。）
    """

    # 等命令发出去的让步上限。不设上限的话，id 写错的测试会静默挂死，
    # 而不是给出"这条响应永远等不到它的命令"这种能直接定位的报错。
    _MAX_YIELDS = 1000

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.closed = False

    async def send(self, text):
        self.sent.append(json.loads(text))

    def _releasable(self, item):
        if not isinstance(item, dict):
            return True
        need = item.get("id")
        # 事件帧（无 id）随时可到：这正是"事件早于等待者"要覆盖的场景。
        return need is None or any(c.get("id") == need for c in self.sent)

    async def recv(self):
        if not self.script:
            await asyncio.sleep(3600)  # 模拟"再也不来消息"
        head = self.script[0]
        for _ in range(self._MAX_YIELDS):
            if self._releasable(head):
                self.script.pop(0)
                return head if isinstance(head, str) else json.dumps(head)
            await asyncio.sleep(0)  # 让出发送方，等命令先出去
        raise AssertionError(f"脚本帧 {head!r} 对应的命令从未发出，检查测试用例的 id")

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# 协议层
# ---------------------------------------------------------------------------
def test_commands_match_by_id_not_by_order():
    """响应乱序时必须按 id 匹配，不能按到达顺序。

    真实场景：同时发 Page.navigate 和 Runtime.evaluate，后者的响应可能先回。
    按顺序匹配会把结果安到错误的命令上——判定会用 A 页面的 HTML 配 B 页面的状态码。
    """
    transport = FakeTransport(
        [
            {"id": 2, "result": {"second": True}},  # 故意先回 id=2
            {"id": 1, "result": {"first": True}},
        ]
    )

    async def go():
        session = CDPSession(transport, timeout=2)
        session.start()
        first = asyncio.ensure_future(session.send_command("A"))
        await asyncio.sleep(0)  # 让第一条先发出去
        second = asyncio.ensure_future(session.send_command("B"))
        return await first, await second

    first, second = run(go())
    assert first == {"first": True}
    assert second == {"second": True}


def test_event_arriving_before_waiter_is_not_lost():
    """事件早于等待者到达时必须缓存。

    这是 CDP 最常见的坑：页面加载太快，`loadEventFired` 在 navigate 返回前
    就到了。如果不缓存，等待者会一直等到超时——表现为"页面明明加载了却报超时"。
    """
    transport = FakeTransport(
        [
            {"method": "Page.loadEventFired", "params": {}},  # 先到的事件
            {"id": 1, "result": {}},
        ]
    )

    async def go():
        session = CDPSession(transport, timeout=2)
        session.start()
        await session.send_command("Page.navigate")
        await asyncio.sleep(0.05)  # 让读循环把事件吃进去
        return await session.wait_for_event("Page.loadEventFired")

    event = run(go())
    assert event["method"] == "Page.loadEventFired"


def test_command_error_becomes_cdp_error():
    transport = FakeTransport([{"id": 1, "error": {"message": "no such target"}}])

    async def go():
        session = CDPSession(transport, timeout=2)
        session.start()
        await session.send_command("Bad")

    with pytest.raises(CDPError, match="no such target"):
        run(go())


def test_command_timeout_raises_instead_of_hanging():
    transport = FakeTransport([])  # 永远不回

    async def go():
        session = CDPSession(transport, timeout=0.2)
        session.start()
        await session.send_command("Slow")

    with pytest.raises(CDPError, match="超时"):
        run(go())


def test_evaluate_surfaces_page_exception():
    """页面内 JS 抛异常要当错误，不能把 None 当成"页面是空的"。"""
    transport = FakeTransport(
        [{"id": 1, "result": {"exceptionDetails": {"text": "ReferenceError"}}}]
    )

    async def go():
        session = CDPSession(transport, timeout=2)
        session.start()
        await session.evaluate("boom()")

    with pytest.raises(CDPError, match="JS 抛异常"):
        run(go())


def test_pick_page_target_skips_non_page_targets():
    """要跳过 service_worker / iframe 之类的 target，只认 page。"""
    targets = [
        {"type": "service_worker", "webSocketDebuggerUrl": "ws://x/1"},
        {"type": "page", "webSocketDebuggerUrl": "ws://x/2"},
    ]
    assert pick_page_target(targets)["webSocketDebuggerUrl"] == "ws://x/2"
    assert pick_page_target([{"type": "service_worker"}]) is None


# ---------------------------------------------------------------------------
# 语义层
# ---------------------------------------------------------------------------
class FakeSession:
    """CDPBrowser 只用到 send_command / evaluate / wait_for_event / close。"""

    def __init__(self, html="<html>ok</html>", status=200, nav_error=None):
        self.html = html
        self.status = status
        self.nav_error = nav_error
        self.commands = []

    async def send_command(self, method, params=None, timeout=None):
        self.commands.append(method)
        if method == "Page.navigate":
            return {"errorText": self.nav_error} if self.nav_error else {"frameId": "F1"}
        return {}

    async def wait_for_event(self, method, timeout=None):
        return {"method": method}

    async def evaluate(self, expression, timeout=None):
        if "outerHTML" in expression:
            return self.html
        return self.status

    async def close(self):
        pass


def test_fetch_returns_status_and_html():
    session = FakeSession(html="<html>profile</html>", status=200)
    browser = CDPBrowser(session, settle_ms=0)
    status, html = run(browser.fetch("https://example.com/u/bob"))
    assert status == 200
    assert html == "<html>profile</html>"


def test_fetch_raises_on_navigation_failure():
    """导航被拒绝（DNS/证书/被拦）要报错，不能返回空 HTML 让判定层去猜。"""
    session = FakeSession(nav_error="net::ERR_NAME_NOT_RESOLVED")
    browser = CDPBrowser(session, settle_ms=0)
    with pytest.raises(CDPError, match="导航失败"):
        run(browser.fetch("https://nonexistent.invalid/"))


# ---------------------------------------------------------------------------
# 熔断与路由
# ---------------------------------------------------------------------------
def test_connect_failure_trips_circuit_breaker(monkeypatch):
    """连不上浏览器时只试一次，之后直接失败。

    一次扫描几千个站，每个站都去握一次手（各等 5 秒超时）会让整次扫描不可用。
    """

    async def boom(**kwargs):
        raise CDPError("连不上")

    monkeypatch.setattr(browser_mod.CDPBrowser, "connect", boom)

    executor = BrowserExecutor()
    with pytest.raises(CDPError):
        run(executor.fetch("https://example.com/"))
    assert executor.unavailable is True

    # 第二次必须立刻失败，不能再尝试连接
    with pytest.raises(CDPError, match="本次运行不再重试"):
        run(executor.fetch("https://example.com/"))


class FakeChecker:
    def __init__(self, html, status=200, error=None):
        self.html = html
        self.status = status
        self.error = error
        self.calls = 0

    async def check(self):
        self.calls += 1
        return self.html, self.status, self.error


BLOCKED_HTML = "您的访问过于频繁，请稍后再试"
SITE = Site(
    "Gitee",
    uri_check="https://gitee.com/{account}",
    e_code=200,
    m_code=404,
    source="sites_cn",
)


def _info():
    return {"username": "wizzer", "url_probe": "https://gitee.com/wizzer"}


@pytest.fixture(autouse=True)
def _reset_budget():
    """升级预算是模块级累计值，测试之间必须隔离。

    不重置的话，测试的执行顺序会决定谁拿得到额度——这种测试迟早会
    在别人机器上随机变红。
    """
    browser_mod.reset_escalations()
    yield
    browser_mod.reset_escalations()


def test_local_and_private_targets_are_never_escalated(monkeypatch):
    """本地/内网地址一律不升级到浏览器。

    这条不只是"测试别连真浏览器"的洁癖。三个理由，任何一个都足够：
      1. 语义：本地服务器不做风控。它被判 BLOCKED 说明**我们的规则或判定
         逻辑有 bug**，用浏览器重试只是把 bug 藏起来。
      2. 隐私：内网 URL 不该进用户的浏览历史、DNS 缓存和代理日志。
      3. 工程：导航一个没人监听的本地端口要等满超时，几千个站会拖死扫描。
    """
    monkeypatch.setattr(browser_mod, "spec_for", lambda site: SITE, raising=False)
    monkeypatch.setattr(browser_mod, "browser_available", lambda: True)

    calls = []

    async def fake_fetch(url, timeout=30.0):
        calls.append(url)
        return 200, "<html>real profile</html>"

    monkeypatch.setattr(browser_mod, "fetch_with_browser", fake_fetch)

    for target in (
        "http://localhost:8080/claimed",
        "http://127.0.0.1:8080/claimed",
        "http://192.168.1.10/claimed",
        "http://10.0.0.5/claimed",
        "http://[::1]:8080/claimed",
        "http://printer.local/claimed",
        "file:///etc/passwd",
    ):
        info = {"username": "wizzer", "url_probe": target}
        html, status, error = run(
            fetch_site_response(SITE, info, FakeChecker(BLOCKED_HTML), {"timeout": 5}, _NullLog())
        )
        assert html == BLOCKED_HTML, f"{target} 不该被浏览器结果替换"
    assert calls == [], f"不该对本地/内网地址发起浏览器导航：{calls}"


def test_public_url_detection():
    """公网判断的边界：域名算公网，IP 字面量按网段判。"""
    assert browser_mod.is_public_url("https://gitee.com/wizzer") is True
    assert browser_mod.is_public_url("https://example.com") is True
    assert browser_mod.is_public_url("https://8.8.8.8/x") is True
    assert browser_mod.is_public_url("http://localhost/") is False
    assert browser_mod.is_public_url("http://127.0.0.1/") is False
    assert browser_mod.is_public_url("http://172.16.0.1/") is False
    assert browser_mod.is_public_url("http://169.254.169.254/") is False
    assert browser_mod.is_public_url("ftp://example.com/x") is False
    assert browser_mod.is_public_url("") is False
    assert browser_mod.is_public_url(None) is False


def test_escalation_budget_is_capped(monkeypatch):
    """大面积拦截时，升级次数必须有上限。

    没有上限的话，一次异常扫描会变成"给用户的浏览器压测"：每个站一次导航、
    一次 800ms 静置、最坏 30s 超时。这里把上限调到 2，验证第 3 个站点
    保持原来的 BLOCKED 结论，而不是继续开浏览器。
    """
    monkeypatch.setenv(browser_mod.ENV_MAX_ESCALATIONS, "2")
    monkeypatch.setattr(browser_mod, "spec_for", lambda site: SITE, raising=False)
    monkeypatch.setattr(browser_mod, "browser_available", lambda: True)

    calls = []

    async def fake_fetch(url, timeout=30.0):
        calls.append(url)
        return 200, "<html>real profile</html>"

    monkeypatch.setattr(browser_mod, "fetch_with_browser", fake_fetch)

    results = [
        run(fetch_site_response(SITE, _info(), FakeChecker(BLOCKED_HTML), {"timeout": 5}, _NullLog()))
        for _ in range(5)
    ]
    assert len(calls) == 2, f"只该升级 2 次，实际 {len(calls)} 次"
    # 前两次拿到了浏览器结果，后面三次保留原拦截页
    assert results[0][0] != BLOCKED_HTML and results[1][0] != BLOCKED_HTML
    assert all(r[0] == BLOCKED_HTML for r in results[2:])


def test_escalation_budget_resets_per_run():
    """预算是"每次扫描"的，不是"每个进程"的。

    不清零的话，web 界面或脚本里第二次扫描会一开始就没额度，
    表现为"第一次扫描能判出来，第二次同样的站全部未判定"。
    """
    assert browser_mod.escalations_left() == browser_mod.max_escalations()
    for _ in range(3):
        browser_mod._consume_escalation()
    assert browser_mod.escalations_left() == browser_mod.max_escalations() - 3
    browser_mod.reset_escalations()
    assert browser_mod.escalations_left() == browser_mod.max_escalations()


def test_blocked_http_result_escalates_to_browser(monkeypatch):
    """HTTP 被判 BLOCKED 时要用浏览器重试——这是恢复主流站点的关键路径。

    Gitee / 语雀实测都是这样：纯 HTTP 一定被拦，浏览器（用户自己的 Chrome）
    一打就通。不升级的话，用户最关心的站永远显示"未判定"。
    """
    monkeypatch.setattr(browser_mod, "spec_for", lambda site: SITE, raising=False)
    monkeypatch.setattr(browser_mod, "browser_available", lambda: True)

    async def fake_fetch(url, timeout=30.0):
        return 200, '<html>real profile db-usr-profile</html>'

    monkeypatch.setattr(browser_mod, "fetch_with_browser", fake_fetch)

    checker = FakeChecker(BLOCKED_HTML)
    html, status, error = run(fetch_site_response(SITE, _info(), checker, {"timeout": 5}, _NullLog()))
    assert checker.calls == 1
    assert status == 200
    assert "real profile" in html


def test_browser_result_does_not_whitewash_a_real_block(monkeypatch):
    """浏览器也拿到拦截页时，保留原来的 BLOCKED 结论。

    用浏览器结果"洗白"一个没判出来的站点，等于把"被拦"变成"不存在"。
    """
    monkeypatch.setattr(browser_mod, "spec_for", lambda site: SITE, raising=False)
    monkeypatch.setattr(browser_mod, "browser_available", lambda: True)

    async def fake_fetch(url, timeout=30.0):
        return 200, BLOCKED_HTML

    monkeypatch.setattr(browser_mod, "fetch_with_browser", fake_fetch)

    checker = FakeChecker(BLOCKED_HTML)
    html, status, error = run(fetch_site_response(SITE, _info(), checker, {"timeout": 5}, _NullLog()))
    assert html == BLOCKED_HTML  # 保留原响应，不替换


def test_browser_unavailable_keeps_original_response(monkeypatch):
    """浏览器没起来时，普通站点照常走 HTTP，不能因此全部失败。"""
    monkeypatch.setattr(browser_mod, "spec_for", lambda site: SITE, raising=False)
    monkeypatch.setattr(browser_mod, "browser_available", lambda: False)

    checker = FakeChecker("<html>ok</html>", 200)
    html, status, error = run(fetch_site_response(SITE, _info(), checker, {"timeout": 5}, _NullLog()))
    assert (html, status, error) == ("<html>ok</html>", 200, None)


def test_browser_site_without_browser_reports_error_not_absence(monkeypatch):
    """`executor: browser` 的站点在浏览器不可用时，必须报错而不是判"不存在"。"""
    spec = Site("小红书", uri_check="https://x.com/{account}", executor="browser", source="sites_cn")
    monkeypatch.setattr(browser_mod, "spec_for", lambda site: spec, raising=False)
    monkeypatch.setattr(browser_mod, "browser_available", lambda: False)

    checker = FakeChecker("", 200)
    html, status, error = run(fetch_site_response(spec, _info(), checker, {"timeout": 5}, _NullLog()))
    assert checker.calls == 0, "浏览器站点不该退回 HTTP 猜测"
    assert error is not None


def test_browser_executor_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv(browser_mod.ENV_DISABLE, "0")
    assert browser_mod.enabled() is False
    monkeypatch.setenv(browser_mod.ENV_DISABLE, "1")
    assert browser_mod.enabled() is True


class _NullLog:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass
