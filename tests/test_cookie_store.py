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
