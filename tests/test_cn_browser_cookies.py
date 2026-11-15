"""从本机浏览器取 Cookie（maigret/cn/browser_cookies.py）的行为测试。

守住的底线：

    这是整个项目里唯一会**读取用户登录凭证**的模块，所以它的护栏不是
    "功能正确"，而是"不越界"：

        1. 只连回环地址 —— CDP 端口无鉴权，连上去就能读走整台浏览器
        2. 跨域作用域不能退化 —— `.bilibili.com` 退成 `www.bilibili.com`
           会让 `api.bilibili.com` 收不到，表现为"读了却没用"，极难查
        3. 只暴露域名和条数 —— 值不进页面、不进日志

全部用例离线，不依赖本机有没有开调试端口。
"""

import asyncio

import pytest

from maigret.cn import browser_cookies as bc


# ----------------------------------------------------------------------
# 回环限制
# ----------------------------------------------------------------------
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.1.2.3"])
def test_loopback_hosts_are_allowed(host):
    assert bc.is_loopback(host) is True


@pytest.mark.parametrize(
    "host", ["192.168.1.5", "10.0.0.1", "example.com", "8.8.8.8", "", None, "127.0.0.1.evil.com"]
)
def test_remote_hosts_are_rejected(host):
    """CDP 端口没有鉴权，连上去就能读走整台浏览器的登录态。"""
    assert bc.is_loopback(host) is False


def test_browser_info_refuses_a_remote_host():
    """拒绝要抛异常，不能静默连过去。"""
    with pytest.raises(bc.BrowserCookieError) as excinfo:
        asyncio.run(bc.browser_info(host="192.168.1.5", port=9222))

    assert "非本机" in str(excinfo.value)


# ----------------------------------------------------------------------
# 过滤
# ----------------------------------------------------------------------
def test_filter_drops_cookies_without_a_name_or_domain():
    """CDP 会返回空名条目（分区 Cookie 残留），发给站点可能直接 400。"""
    raw = [
        {"name": "", "value": "x", "domain": "www.douyin.com"},
        {"name": "ok", "value": "y", "domain": "www.douyin.com"},
        {"name": "no_domain", "value": "z", "domain": ""},
    ]

    kept = bc._filter_cookies(raw)

    assert [c["name"] for c in kept] == ["ok"]


def test_filter_drops_session_residue_expiry():
    """expires 在 0 到 1 之间是会话残留，不是时间戳。"""
    raw = [
        {"name": "a", "value": "1", "domain": "x.com", "expires": 0.5},
        {"name": "b", "value": "2", "domain": "x.com", "expires": 1893456000.0},
        {"name": "c", "value": "3", "domain": "x.com", "expires": -1},
        {"name": "d", "value": "4", "domain": "x.com"},
    ]

    kept = bc._filter_cookies(raw)

    assert [c["name"] for c in kept] == ["b", "c", "d"]


# ----------------------------------------------------------------------
# CookieJar 作用域（本模块最容易错的一处）
# ----------------------------------------------------------------------
def _jar():
    """构造一个装好两条 Cookie 的 jar。

    必须包在 `asyncio.run` 里：`aiohttp.CookieJar()` 构造时要拿当前事件循环
    （内部定时器用于过期清理），在同步测试函数里直接调会报
    `RuntimeError: no running event loop`。
    """
    raw = [
        {"name": "SESSDATA", "value": "abc", "domain": ".bilibili.com", "path": "/",
         "secure": True, "httpOnly": True, "expires": 1893456000.0},
        {"name": "sessionid", "value": "xyz", "domain": "www.douyin.com", "path": "/",
         "secure": True, "httpOnly": True, "expires": -1},
    ]
    return asyncio.run(_build(raw))


async def _build(raw):
    return bc.to_cookiejar(raw)


def test_wildcard_domain_still_reaches_subdomains():
    """`.bilibili.com` 必须覆盖 `api.bilibili.com`。

    回归的是真实踩到的一处：如果按 `update_cookies({name: value}, response_url=...)`
    装载，domain 会被改写成 response_url 的 host，通配域就退化成具体子域，
    于是 B 站接口收不到 Cookie —— 现象是"读到了 91 条，B 站照样判不出来"。
    """
    from yarl import URL

    jar = _jar()

    assert "SESSDATA" in jar.filter_cookies(URL("https://api.bilibili.com/x/web-interface/card"))
    assert "SESSDATA" in jar.filter_cookies(URL("https://space.bilibili.com/1"))


def test_cookies_do_not_leak_to_unrelated_hosts():
    """Cookie 只发给自己的域，绝不能全站乱发。"""
    from yarl import URL

    jar = _jar()

    assert dict(jar.filter_cookies(URL("https://example.com/"))) == {}
    assert dict(jar.filter_cookies(URL("https://gitee.com/x"))) == {}
    # 抖音那条只属于 www.douyin.com
    assert dict(jar.filter_cookies(URL("https://www.douyin.com/user/x"))) != {}


# ----------------------------------------------------------------------
# 摘要：只暴露域名和条数
# ----------------------------------------------------------------------
def test_summarize_counts_by_domain_and_never_exposes_values():
    raw = [
        {"name": "a", "value": "SECRET-1", "domain": ".douyin.com"},
        {"name": "b", "value": "SECRET-2", "domain": ".douyin.com"},
        {"name": "c", "value": "SECRET-3", "domain": "gitee.com"},
    ]

    summary = bc.summarize(raw)

    assert summary == {".douyin.com": 2, "gitee.com": 1}
    assert "SECRET" not in repr(summary)


def test_summarize_orders_by_count_desc():
    raw = [{"name": f"n{i}", "value": "v", "domain": "a.com"} for i in range(3)]
    raw.append({"name": "x", "value": "v", "domain": "b.com"})

    assert list(bc.summarize(raw)) == ["a.com", "b.com"]


def test_summarize_limit_truncates():
    raw = [{"name": f"n{i}", "value": "v", "domain": f"d{i}.com"} for i in range(5)]

    assert len(bc.summarize(raw, limit=2)) == 2
