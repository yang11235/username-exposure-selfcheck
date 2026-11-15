"""`maigret.web.cookie_store`：Cookie 的导入、落盘与作用域。

这个模块此前完全没有测试，所以有一个会**静默吃掉登录凭据**的 bug 一直存在：
浏览器扩展导出的 httpOnly cookie 以 `#HttpOnly_` 开头，而"跳过注释"的逻辑
把整行丢掉了。百度的 `BDUSS_BFESS`、passport 的 `PTOKEN` 恰好都是 httpOnly，
导入时看着"成功导入 N 条"，实际拿到的是一个没有登录态的残缺文件。
"""

import time

import pytest

from maigret.web import cookie_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / 'cookies.txt'
    monkeypatch.setattr(cookie_store, 'COOKIE_PATH', path)
    return path


FAR_FUTURE = int(time.time()) + 365 * 24 * 3600


def _coverage():
    """调用 web/app.py 的覆盖检查（它会自己读 cookie_store._rows）。"""
    from maigret.web import app as web_app_module

    return web_app_module._describe_cookie_coverage()


def netscape_row(domain, name, value, secure='FALSE', path='/'):
    return f"{domain}\tTRUE\t{path}\t{secure}\t{FAR_FUTURE}\t{name}\t{value}"


# ---------------------------------------------------------------------------
# httpOnly 行不能被当成注释
# ---------------------------------------------------------------------------
def test_import_keeps_httponly_cookies(store):
    """`#HttpOnly_` 开头的是**数据行**，不是注释。

    丢掉它们的后果不是"少几条"，而是"登录态缺失"——关键凭据都是 httpOnly。
    """
    text = '\n'.join([
        '# Netscape HTTP Cookie File',
        '#HttpOnly_' + netscape_row('.baidu.com', 'BDUSS_BFESS', 'v1', secure='TRUE'),
        netscape_row('.baidu.com', 'BDUSS', 'v2'),
        '#HttpOnly_' + netscape_row('.passport.baidu.com', 'PTOKEN', 'v3', secure='TRUE'),
    ])

    count, domains = cookie_store.import_netscape(text)

    assert count == 3
    assert domains == 2
    written = store.read_text(encoding='utf-8')
    assert 'BDUSS_BFESS' in written
    assert 'PTOKEN' in written


def test_import_strips_the_httponly_prefix(store):
    """落盘要去掉前缀：MozillaCookieJar 不认 `#HttpOnly_`。"""
    text = '#HttpOnly_' + netscape_row('.baidu.com', 'BDUSS_BFESS', 'v', secure='TRUE')

    cookie_store.import_netscape(text)

    written = store.read_text(encoding='utf-8')
    assert '#HttpOnly_' not in written
    assert written.count('.baidu.com') == 1


def test_import_ignores_real_comments(store):
    """真正的注释仍然要跳过。"""
    text = '\n'.join([
        '# Netscape HTTP Cookie File',
        '# 这是说明文字',
        netscape_row('.baidu.com', 'BDUSS', 'v'),
    ])

    count, _ = cookie_store.import_netscape(text)

    assert count == 1


def test_looks_netscape_accepts_httponly_only_file(store):
    """整份文件只有 httpOnly cookie 时也要识别成 Netscape 格式。

    原来这里也是按 `#` 开头跳过，于是扩展导出的一份"全是 httpOnly"的文件
    会被判成"不是 Netscape 格式"，走错误的导入分支。
    """
    text = '#HttpOnly_' + netscape_row('.baidu.com', 'BDUSS_BFESS', 'v', secure='TRUE')

    assert cookie_store.looks_netscape(text) is True


def test_looks_netscape_rejects_plain_text(store):
    assert cookie_store.looks_netscape('随便一段话，不是 cookie 文件') is False
    assert cookie_store.looks_netscape('') is False


# ---------------------------------------------------------------------------
# 同名域名整体替换
# ---------------------------------------------------------------------------
def test_reimport_replaces_same_domain(store):
    """同一域名再导入要整体替换，不能新旧混在一起。

    残留的旧 BDUSS 和新 BDUSS 同时存在时，站点收到的 Cookie 头里有两份同名值，
    判定会失败，而用户以为"导入成功了"。
    """
    cookie_store.import_netscape(netscape_row('.baidu.com', 'BDUSS', 'old'))
    cookie_store.import_netscape(netscape_row('.baidu.com', 'BDUSS', 'new'))

    written = store.read_text(encoding='utf-8')
    assert 'new' in written
    # 旧值不应残留
    assert written.count('BDUSS') == 1


def test_import_merges_different_domains(store):
    """不同域名要能共存（百度 + 微博一起导入）。"""
    cookie_store.import_netscape(
        netscape_row('.baidu.com', 'BDUSS', 'b') + '\n'
        + netscape_row('.weibo.com', 'SUB', 'w')
    )

    domains = cookie_store.cookie_domains()
    assert set(domains) == {'baidu.com', 'weibo.com'}


# ---------------------------------------------------------------------------
# 手工粘贴 Cookie 头
# ---------------------------------------------------------------------------
def test_save_cookie_header_parses_pairs(store):
    n = cookie_store.save_cookies('https://tieba.baidu.com/home', 'BDUSS=a; STOKEN=b')

    assert n == 2
    written = store.read_text(encoding='utf-8')
    assert '\tBDUSS\ta' in written
    assert '\tSTOKEN\tb' in written


def test_save_cookie_header_normalizes_domain(store):
    """从浏览器地址栏复制过来的 URL 要能直接当域名用。"""
    cookie_store.save_cookies('https://passport.baidu.com/v3/login?x=1', 'PTOKEN=v')

    assert 'passport.baidu.com' in cookie_store.cookie_domains()
    assert 'https' not in store.read_text(encoding='utf-8')


def test_save_cookie_header_rejects_empty(store):
    with pytest.raises(ValueError):
        cookie_store.save_cookies('baidu.com', '这不是 cookie')


def test_save_cookie_header_rejects_empty_domain(store):
    with pytest.raises(ValueError):
        cookie_store.save_cookies('', 'a=1')


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------
def test_delete_domain_removes_only_that_domain(store):
    cookie_store.import_netscape(
        netscape_row('.baidu.com', 'BDUSS', 'b') + '\n'
        + netscape_row('.weibo.com', 'SUB', 'w')
    )

    cookie_store.delete_domain('baidu.com')

    assert set(cookie_store.cookie_domains()) == {'weibo.com'}


# ---------------------------------------------------------------------------
# 导入后的覆盖提示（web/app.py 的 _describe_cookie_coverage）
# ---------------------------------------------------------------------------
def test_coverage_reports_missing_login_markers(store):
    """只导入访客 cookie 时要明确说"这些站仍然判不出来"。

    用户按教程导出一份 cookie，很可能漏了 httpOnly 的会话凭据。导入行为
    "成功"、检索却判不出来，界面上看不出差别——所以必须说出来。
    """
    cookie_store.import_netscape(netscape_row('.baidu.com', 'BAIDUID', 'visitor'))

    unlocked, missing = _coverage()

    assert '百度' not in unlocked
    assert '百度' in missing


def test_coverage_reports_unlocked_sites(store):
    cookie_store.import_netscape(
        netscape_row('.baidu.com', 'BDUSS', 'b')
        + '\n' + netscape_row('.weibo.com', 'SUB', 'w')
    )

    unlocked, missing = _coverage()

    assert '百度' in unlocked
    assert '微博' in unlocked
    assert '百度' not in missing


def test_coverage_handles_secure_prefix(store):
    cookie_store.import_netscape(
        netscape_row('.weibo.com', '__Secure-SUB', 'w', secure='TRUE')
    )

    unlocked, _ = _coverage()

    assert '微博' in unlocked


def test_coverage_of_empty_store_is_all_missing(store):
    unlocked, missing = _coverage()

    assert unlocked == []
    assert missing


# ---------------------------------------------------------------------------
# 批量导入：一次覆盖所有域名
# ---------------------------------------------------------------------------
def test_batch_import_covers_many_domains_at_once(store):
    """扩展「导出全部」会把所有站放一个文件里，一次导入即可。

    用户不该为了五个站导出五次。
    """
    text = '\n'.join(
        '#HttpOnly_' + netscape_row(f'.{d}.com', marker, 'v', secure='TRUE')
        for d, marker in (
            ('weibo', 'SUB'), ('zhihu', 'z_c0'),
            ('baidu', 'BDUSS'), ('bilibili', 'SESSDATA'), ('douban', 'dbcl2'),
        )
    )

    count, domains = cookie_store.import_netscape(text)

    assert count == 5
    assert domains == 5
    assert len(cookie_store.cookie_domains()) == 5


# ---------------------------------------------------------------------------
# 追踪域名过滤
# ---------------------------------------------------------------------------
def test_tracker_filter_drops_analytics_but_keeps_logins(store):
    """分析/广告类 cookie 占全量导出的很大一部分，本工具一个都用不到。

    关键是不能误伤登录凭据——所以必须按精确域名或后缀匹配，
    不能用"包含"（`notgoogle-analytics.com` 会被误删）。
    """
    text = '\n'.join([
        '#HttpOnly_' + netscape_row('.weibo.com', 'SUB', 'w', secure='TRUE'),
        netscape_row('.google-analytics.com', '_ga', 'GA1'),
        netscape_row('.doubleclick.net', '_c', 'x'),
        netscape_row('.stats.google-analytics.com', '_gid', 'y'),
        netscape_row('.hm.baidu.com', 'HMACCOUNT', 'z'),
    ])

    count, _ = cookie_store.import_netscape(text, drop_trackers=True)

    assert count == 1
    rows = store.read_text(encoding='utf-8')
    assert '\tSUB\t' in rows
    for gone in ('_ga', 'doubleclick', 'HMACCOUNT'):
        assert gone not in rows


def test_tracker_filter_keeps_unrelated_real_sites(store):
    """黑名单只该丢追踪商，不该丢"站点库没收录的真实站点"。

    误删是静默的——用户不知道少导了什么，所以宁可多留。
    """
    text = '\n'.join([
        netscape_row('.weibo.cn', 'SUB', 'w'),
        netscape_row('.some-forum.example', 'sid', 'x'),
    ])

    count, _ = cookie_store.import_netscape(text, drop_trackers=True)

    assert count == 2


def test_tracker_detection_is_suffix_based_not_substring(store):
    """`notgoogle-analytics.com` 不是 Google 的域，不能误判。"""
    assert cookie_store.is_tracker_domain('.google-analytics.com') is True
    assert cookie_store.is_tracker_domain('.stats.google-analytics.com') is True
    assert cookie_store.is_tracker_domain('.notgoogle-analytics.com') is False
    assert cookie_store.is_tracker_domain('.weibo.com') is False
    assert cookie_store.is_tracker_domain('') is False


def test_tracker_filter_clears_previous_trackers_on_reimport(store):
    """先导全量、再勾过滤重导，旧的追踪记录不能残留。

    勾了「只保留本工具会用到的域名」却还能在文件里看到 doubleclick，
    是自相矛盾的。
    """
    cookie_store.import_netscape(
        netscape_row('.weibo.com', 'SUB', 'w') + '\n'
        + netscape_row('.doubleclick.net', '_c', 'x'),
        drop_trackers=False,
    )
    assert any('doubleclick' in r for r in cookie_store._rows())

    cookie_store.import_netscape(
        netscape_row('.weibo.com', 'SUB', 'w'), drop_trackers=True
    )

    assert not any('doubleclick' in r for r in cookie_store._rows())


def test_all_tracker_input_raises_a_clear_error(store):
    """整份文件都是追踪 cookie 时，别报"没有解析到"——那会误导用户以为格式错了。"""
    text = netscape_row('.doubleclick.net', '_c', 'x')

    with pytest.raises(ValueError) as excinfo:
        cookie_store.import_netscape(text, drop_trackers=True)

    assert '追踪' in str(excinfo.value)


# ---------------------------------------------------------------------------
# 批量导入：一次覆盖所有域名
