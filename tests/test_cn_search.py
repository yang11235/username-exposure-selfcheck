"""站内搜索执行器（maigret/cn/executors/search.py）的测试。

这个模块的价值全在**判据**上，所以测试也围绕判据写：一组用例证明它能在
该命中时命中，另一组证明它在该沉默时沉默——后者更重要。搜索结果页必然回显
被搜的词，把那种回显当成"账号存在"会让每个用户都"注册过"每一个站点。
"""

import asyncio
from unittest.mock import Mock

import pytest

from maigret.cn.executors import search as search_mod
from maigret.cn.model import Site
from maigret.cn.verdict import Reason, Status


def run(coro):
    return asyncio.run(coro)


def bili_site(**kwargs):
    params = dict(
        name='Bilibili',
        executor='search',
        uri_check='https://search.bilibili.com/upuser?keyword={account}',
        search_handle_pattern=r'space\.bilibili\.com/(\d+)',
        search_empty_marker='没有找到相关用户',
    )
    params.update(kwargs)
    return Site(**params)


class FakeResponse:
    def __init__(self, status=200, text=''):
        self.status = status
        self._text = text

    async def text(self, errors='replace'):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """最小 aiohttp.ClientSession 替身：只实现 get()。"""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requested = []

    def get(self, url, timeout=None, headers=None, allow_redirects=True):
        self.requested.append(url)
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self):
        pass


# ---------------------------------------------------------------------------
# 搜索地址
# ---------------------------------------------------------------------------


def test_search_url_fills_the_placeholder():
    site = bili_site()
    assert search_mod.build_search_url(site, 'wizzer') == (
        'https://search.bilibili.com/upuser?keyword=wizzer'
    )


def test_search_url_quotes_special_characters():
    """空格、`&`、`+` 必须编码，否则会被当成查询参数分隔符。"""
    url = search_mod.build_search_url(bili_site(), 'a b&c+d')
    assert url.endswith('keyword=a+b%26c%2Bd')


def test_search_url_accepts_the_username_placeholder():
    """v2 统一用 {account}，但导入的规则里可能还留着 {username}。"""
    site = bili_site(uri_check='https://example.com/s?q={username}')
    assert search_mod.build_search_url(site, 'wizzer').endswith('q=wizzer')


def test_search_url_is_empty_without_a_template():
    assert search_mod.build_search_url(Site(name='X'), 'wizzer') == ''


# ---------------------------------------------------------------------------
# 句柄抽取
# ---------------------------------------------------------------------------


def test_extract_handles_returns_none_without_a_pattern():
    """返回 None 而不是空列表：`没配正则` 和 `结果区是空的` 是两件事。"""
    assert search_mod.extract_handles(Site(name='X'), '<a>1</a>') is None


def test_extract_handles_rejects_a_broken_pattern():
    site = bili_site(search_handle_pattern='space.bilibili.com/(\\d+')
    assert search_mod.extract_handles(site, '<a>1</a>') is None


def test_extract_handles_deduplicates_and_keeps_order():
    html = (
        '<a href="//space.bilibili.com/2">a</a>'
        '<a href="//space.bilibili.com/1">b</a>'
        '<a href="//space.bilibili.com/2">c</a>'
    )
    assert search_mod.extract_handles(bili_site(), html) == ['2', '1']


def test_extract_handles_caps_the_candidate_list():
    html = ''.join(
        f'<a href="//space.bilibili.com/{i}">x</a>'
        for i in range(search_mod.MAX_CANDIDATES + 20)
    )
    handles = search_mod.extract_handles(bili_site(), html)
    assert len(handles) == search_mod.MAX_CANDIDATES


# ---------------------------------------------------------------------------
# 判定：该命中时命中
# ---------------------------------------------------------------------------


def test_decide_claims_on_an_exact_handle_match():
    html = '<a href="//space.bilibili.com/12345">wizzer</a>'
    outcome = search_mod.decide(bili_site(), '12345', html, 200)

    assert outcome.verdict.status == Status.CLAIMED
    assert outcome.verdict.evidence_type == 'name_match_only'
    assert '12345' in outcome.verdict.evidence
    assert outcome.candidates == 1
    assert bool(outcome) is True


def test_decide_match_is_case_insensitive():
    """大小写不同的同一个句柄是同一个账号（B 站数字 uid 的规则不涉及这条，
    所以这里用一个按名字取句柄的站点）。"""
    site = bili_site(search_handle_pattern=r'@(\w+)')
    assert search_mod.decide(site, 'wizzer', '<a>@WizZer</a>').verdict.status == Status.CLAIMED


def test_claimed_confidence_stays_in_the_low_band():
    """搜索只能证明"存在一个叫这个名字的账号"，不能证明"是你的"。"""
    html = '<a href="//space.bilibili.com/12345">x</a>'
    verdict = search_mod.decide(bili_site(), '12345', html).verdict
    # base 0.9 × executor(search) 0.5 × evidence(name_match_only) 0.3
    assert verdict.confidence == pytest.approx(0.14, abs=0.01)
    assert verdict.confidence < 0.5


# ---------------------------------------------------------------------------
# 判定：该沉默时沉默（本模块的核心）
# ---------------------------------------------------------------------------


def test_decide_does_not_claim_when_the_page_merely_echoes_the_keyword():
    """整条设计的立足点：搜索页回显关键词 ≠ 账号存在。

    这里模拟一个只回显关键词、没有任何匹配账号的页面。如果判据是
    `e_string = "{account}"`，这条会被判成 CLAIMED —— 而且每一次都错。
    """
    html = '<h1>“wizzer”的搜索结果</h1><p>共 0 条</p>'
    outcome = search_mod.decide(bili_site(), 'wizzer', html)

    assert outcome.verdict.status != Status.CLAIMED
    assert outcome.verdict.reason == Reason.SEARCH_INCONCLUSIVE


def test_decide_is_inconclusive_when_handles_exist_but_none_match():
    """有结果但没有同名句柄 —— 推不出"不存在"：搜索有分页、有排序。"""
    html = (
        '<a href="//space.bilibili.com/1">wizzer_fan</a>'
        '<a href="//space.bilibili.com/2">not_wizzer</a>'
    )
    outcome = search_mod.decide(bili_site(), 'wizzer', html)

    assert outcome.verdict.status == Status.UNKNOWN
    assert outcome.verdict.reason == Reason.SEARCH_INCONCLUSIVE
    assert outcome.candidates == 2


def test_decide_available_only_with_an_explicit_empty_marker():
    html = '<div class="empty">没有找到相关用户</div>'
    outcome = search_mod.decide(bili_site(), 'wizzer', html)

    assert outcome.verdict.status == Status.AVAILABLE
    assert outcome.candidates == 0


def test_decide_is_inconclusive_without_an_empty_marker():
    """没配"空结果"文案时，一个空结果区推不出"不存在"。"""
    site = bili_site(search_empty_marker='')
    assert search_mod.decide(site, 'wizzer', '<div></div>').verdict.status == Status.UNKNOWN


def test_decide_reports_rule_missing_without_a_handle_pattern():
    site = bili_site(search_handle_pattern='')
    outcome = search_mod.decide(site, 'wizzer', '<a>wizzer</a>')

    assert outcome.verdict.status == Status.UNKNOWN
    assert outcome.verdict.reason == Reason.RULE_MISSING


# ---------------------------------------------------------------------------
# 判定：先判被打断
# ---------------------------------------------------------------------------


def test_decide_reports_login_wall_before_looking_for_matches():
    """登录墙里往往就带着你搜的那个词，先判存在性必然误报。"""
    html = '<div>请先登录后查看</div><a href="//space.bilibili.com/12345">wizzer</a>'
    outcome = search_mod.decide(bili_site(), '12345', html)

    assert outcome.verdict.status == Status.UNKNOWN
    assert outcome.verdict.reason == Reason.LOGIN_REQUIRED


def test_decide_reports_captcha():
    html = '<div class="geetest_panel"><div class="geetest_widget"></div></div>'
    outcome = search_mod.decide(bili_site(), 'wizzer', html)
    assert outcome.verdict.reason == Reason.CAPTCHA


def test_decide_reports_a_block_page():
    html = '<html><title>Just a moment...</title><body>_cf_chl_opt</body></html>'
    outcome = search_mod.decide(bili_site(), 'wizzer', html)
    assert outcome.verdict.reason == Reason.BLOCKED


def test_decide_reports_an_empty_body():
    outcome = search_mod.decide(bili_site(), 'wizzer', '   ')
    assert outcome.verdict.reason == Reason.EMPTY_BODY


# ---------------------------------------------------------------------------
# 发请求
# ---------------------------------------------------------------------------


def test_search_returns_the_verdict_for_a_200():
    session = FakeSession(FakeResponse(200, '<a href="//space.bilibili.com/12345">x</a>'))
    outcome = run(search_mod.search(bili_site(), '12345', session=session))

    assert outcome.verdict.status == Status.CLAIMED
    assert session.requested[0].endswith('keyword=12345')


@pytest.mark.parametrize(
    'status, reason',
    [
        (429, Reason.RATE_LIMITED),
        (403, Reason.BLOCKED),
        (401, Reason.BLOCKED),
        (503, Reason.BLOCKED),
        (404, Reason.HTTP_ERROR),
    ],
)
def test_search_maps_non_200_statuses_to_reasons(status, reason):
    session = FakeSession(FakeResponse(status, ''))
    outcome = run(search_mod.search(bili_site(), 'wizzer', session=session))

    assert outcome.verdict.status == Status.UNKNOWN
    assert outcome.verdict.reason == reason
    assert outcome.verdict.http_status == status


def test_search_never_raises_on_a_transport_error():
    session = FakeSession(error=asyncio.TimeoutError())
    outcome = run(search_mod.search(bili_site(), 'wizzer', session=session))

    assert outcome.verdict.status == Status.UNKNOWN
    assert outcome.verdict.reason == Reason.TIMEOUT


def test_search_reports_rule_missing_without_a_url():
    outcome = run(search_mod.search(Site(name='X'), 'wizzer', session=FakeSession()))
    assert outcome.verdict.reason == Reason.RULE_MISSING


def test_search_uses_its_own_session_without_env_proxies():
    """搜索走独立会话，不继承环境代理和登录态。

    只验证语义：不传 session 时自己开一个。真实会话创建需要事件循环，
    这里直接看源码里的 `trust_env=False`——它保证企业代理不会把
    站内搜索请求转发出去。
    """
    import inspect

    source = inspect.getsource(search_mod.search)
    assert 'trust_env=False' in source


def test_search_primes_the_bootstrap_url_first():
    """bootstrap 必须在搜索请求**之前**打：Cookie 要到手才有用。"""
    site = bili_site(bootstrap_url='https://www.bilibili.com/')
    session = FakeSession(FakeResponse(200, ''))

    run(search_mod.search(site, 'wizzer', session=session))

    assert session.requested[0] == 'https://www.bilibili.com/'
    assert session.requested[1].endswith('keyword=wizzer')


def test_search_skips_bootstrap_without_the_field():
    session = FakeSession(FakeResponse(200, ''))
    run(search_mod.search(bili_site(), 'wizzer', session=session))

    assert len(session.requested) == 1


def test_bootstrap_failure_does_not_abort_the_search():
    """预热失败只是"少了个 Cookie"，不该把整次检索变成错误。"""
    site = bili_site(bootstrap_url='https://www.bilibili.com/')

    class FlakySession(FakeSession):
        def get(self, url, timeout=None, headers=None, allow_redirects=True):
            if 'search' not in url:
                self.requested.append(url)
                raise OSError('预热炸了')
            return super().get(url, timeout, headers, allow_redirects)

    session = FlakySession(FakeResponse(200, '<a href="//space.bilibili.com/999">x</a>'))
    outcome = run(search_mod.search(site, 'wizzer', session=session))

    assert outcome.verdict.reason == Reason.SEARCH_INCONCLUSIVE


def test_named_groups_keep_the_handle_when_id_comes_first():
    """JSON 接口里 ID 排在昵称前面（B 站 `mid,uname`），具名分组才不会错位。"""
    site = bili_site(
        search_handle_pattern=r'"mid":(?P<id>\d+),"uname":"(?P<handle>[^"]+)"',
        search_profile_url='https://space.bilibili.com/{id}',
    )
    html = '"mid":12345,"uname":"wizzer"'

    matches = search_mod.extract_matches(site, html)
    outcome = search_mod.decide(site, 'wizzer', html)

    assert matches == [('wizzer', '12345')]
    assert outcome.verdict.status == Status.CLAIMED
    assert outcome.profile_url == 'https://space.bilibili.com/12345'


def test_claimed_without_profile_url_falls_back_to_empty():
    """没声明模板时不要瞎拼地址，让调用方退回搜索页链接。"""
    outcome = search_mod.decide(bili_site(), '12345', '<a href="//space.bilibili.com/12345">x</a>')

    assert outcome.verdict.status == Status.CLAIMED
    assert outcome.profile_url == ''


@pytest.mark.parametrize('status', [412, 403, 429, 500])
def test_non_200_is_decided_before_looking_at_the_body(status):
    """412/403 的错误 JSON 里一个结果条目都没有，按正文判会得出"没有同名账号"。"""
    outcome = search_mod.decide(bili_site(), 'wizzer', '{"code":-412,"message":"请求被拦截"}', http_status=status)

    assert outcome.verdict.status == Status.UNKNOWN
    assert outcome.verdict.reason != Reason.SEARCH_INCONCLUSIVE


# ---------------------------------------------------------------------------
# 与其它层的约定
# ---------------------------------------------------------------------------


def test_search_reason_has_a_blind_spot_label():
    """新的 Reason 必须在盲区表里有中文说法，否则报告里显示的是兜底文案。"""
    from maigret.cn.risk import _BLIND_SPOT_LABELS

    assert Reason.SEARCH_INCONCLUSIVE.value in _BLIND_SPOT_LABELS


def test_site_round_trips_the_search_fields():
    site = bili_site()
    restored = Site(**{k: v for k, v in site.to_dict().items()})

    assert restored.search_handle_pattern == site.search_handle_pattern
    assert restored.search_empty_marker == site.search_empty_marker


# ---------------------------------------------------------------------------
# schema：搜索站点校验的是它自己的判据
# ---------------------------------------------------------------------------


def test_schema_accepts_a_search_site_without_four_fields():
    """搜索站点不该被四字段校验拦住：它的判据是句柄正则，不是 e_string。"""
    from maigret.cn.schema import validate_site

    site = validate_site(
        'Bilibili',
        {
            'executor': 'search',
            'uri_check': 'https://search.bilibili.com/upuser?keyword={account}',
            'search_handle_pattern': r'space\.bilibili\.com/(\d+)',
        },
    )
    assert site.executor == 'search'


def test_schema_rejects_a_search_site_without_a_handle_pattern():
    from maigret.cn.schema import SchemaError, validate_site

    with pytest.raises(SchemaError):
        validate_site(
            'Bilibili',
            {'executor': 'search', 'uri_check': 'https://x/{account}'},
        )


def test_schema_rejects_a_pattern_without_a_capture_group():
    """没有捕获组就只能拿整段匹配当句柄，与用户名逐字比对永远不相等。"""
    from maigret.cn.schema import SchemaError, validate_site

    with pytest.raises(SchemaError):
        validate_site(
            'Bilibili',
            {
                'executor': 'search',
                'uri_check': 'https://x/{account}',
                'search_handle_pattern': 'space.bilibili.com',
            },
        )


def test_schema_rejects_a_broken_pattern():
    from maigret.cn.schema import SchemaError, validate_site

    with pytest.raises(SchemaError):
        validate_site(
            'Bilibili',
            {
                'executor': 'search',
                'uri_check': 'https://x/{account}',
                'search_handle_pattern': 'a(b',
            },
        )


# ---------------------------------------------------------------------------
# hook：搜索站点绕开四字段判定
# ---------------------------------------------------------------------------


class _UpstreamResult:
    """apply_cn_verdict 只关心这几个属性。"""

    def __init__(self):
        self.status = 'Claimed'   # 上游按 200 判的，正是我们要覆盖的那个错
        self.reason = None
        self.confidence = 0.0
        self.rate_limited = False
        self.context = ''


def _patch_spec(monkeypatch, site):
    from maigret.cn import hook

    monkeypatch.setattr(hook, 'spec_for', lambda msite: site)
    return hook


def test_hook_overrides_the_status_code_verdict_for_search_sites(monkeypatch):
    """上游按 200 判 CLAIMED，而搜索页 200 是常态 —— 必须被搜索判据覆盖。"""
    site = bili_site()
    hook = _patch_spec(monkeypatch, site)

    result = _UpstreamResult()
    html = '<a href="//space.bilibili.com/1">someone_else</a>'
    out = hook.apply_cn_verdict(result, object(), 'wizzer', html, 200, None, Mock())

    assert out.status.value == 'Unknown'
    assert out.reason == Reason.SEARCH_INCONCLUSIVE


def test_hook_keeps_a_claimed_search_hit(monkeypatch):
    site = bili_site()
    hook = _patch_spec(monkeypatch, site)

    result = _UpstreamResult()
    html = '<a href="//space.bilibili.com/wizzer">wizzer</a>'
    site_handle = bili_site(search_handle_pattern=r'space\.bilibili\.com/([\w]+)')
    hook = _patch_spec(monkeypatch, site_handle)

    out = hook.apply_cn_verdict(result, object(), 'wizzer', html, 200, None, Mock())

    assert out.status.value == 'Claimed'
    assert out.confidence < 0.5


def test_hook_does_not_trust_a_partial_response(monkeypatch):
    """请求层报错时页面是半截的，数出来的"没有同名账号"不是站点的事实。"""
    site = bili_site()
    hook = _patch_spec(monkeypatch, site)

    result = _UpstreamResult()
    out = hook.apply_cn_verdict(result, object(), 'wizzer', '', 200, Mock(), Mock())

    assert out.reason == Reason.HTTP_ERROR


def test_hook_rewrites_the_link_to_the_profile_not_the_search_page(monkeypatch):
    """报告里要能点开那个账号，不是点开一个空搜索框。

    这里盯的是 `site_url_user`：终端打印和 `to_dict()` 都读它。上游结果对象上
    没有 `.url`，写那个属性不会报错，但链接永远改不过来。
    """
    site = bili_site(
        search_handle_pattern=r'"mid":(?P<id>\d+),"uname":"(?P<handle>[^"]+)"',
        search_profile_url='https://space.bilibili.com/{id}',
    )
    hook = _patch_spec(monkeypatch, site)

    result = _UpstreamResult()
    result.site_url_user = 'https://search.bilibili.com/upuser?keyword=wizzer'
    info = {'url_user': result.site_url_user}
    html = '"mid":12345,"uname":"wizzer"'

    out = hook.apply_cn_verdict(result, object(), 'wizzer', html, 200, None, Mock(), info)

    assert out.site_url_user == 'https://space.bilibili.com/12345'
    assert info['url_user'] == 'https://space.bilibili.com/12345'


def test_hook_keeps_the_search_url_when_no_profile_template(monkeypatch):
    """没有模板就别瞎拼：留着搜索页地址，总比拼出一个 404 强。"""
    site = bili_site(search_handle_pattern=r'space\.bilibili\.com/([\w]+)')
    hook = _patch_spec(monkeypatch, site)

    result = _UpstreamResult()
    result.site_url_user = 'https://search.bilibili.com/upuser?keyword=wizzer'
    out = hook.apply_cn_verdict(
        result, object(), 'wizzer', '<a href="//space.bilibili.com/wizzer">w</a>', 200, None, Mock()
    )

    assert out.site_url_user == 'https://search.bilibili.com/upuser?keyword=wizzer'


# ---------------------------------------------------------------------------
# checking：搜索页上不抽 ID
# ---------------------------------------------------------------------------


def _msite():
    from maigret.sites import MaigretSite

    return MaigretSite(
        'Bilibili',
        {
            'url': 'https://search.bilibili.com/upuser?keyword={username}',
            'urlMain': 'https://www.bilibili.com/',
            'checkType': 'status_code',
        },
    )


def _run_site_result(monkeypatch, spec, html):
    import logging

    from maigret import checking
    from maigret.cn import hook

    # 两个模块各有一份 `spec_for` 的引用，都得换掉：
    # checking 用它判断要不要抽 ids，hook 用它取判定规则。
    # 只换一个的话，另一个会去查真实的策展站点库，测试就变成在测 B 站那条规则。
    monkeypatch.setattr(checking, 'spec_for', lambda site: spec)
    monkeypatch.setattr(hook, 'spec_for', lambda site: spec)
    info = {
        'username': 'wizzer',
        'parsing_enabled': True,
        'url_user': 'https://search.bilibili.com/upuser?keyword=wizzer',
        'url_probe': 'https://search.bilibili.com/upuser?keyword=wizzer',
        'mask': False,
    }
    return checking.process_site_result(
        (html, 200, None), None, logging.getLogger('t'), info, _msite()
    )


def test_search_sites_do_not_extract_ids_from_the_search_page(monkeypatch):
    """搜索页上挂着的是**别人**的主页链接，抽出来会算到这个用户名头上。"""
    from maigret import checking

    def boom(*args, **kwargs):
        raise AssertionError('不该在搜索结果页上抽 ids')

    monkeypatch.setattr(checking, 'extract_ids_data', boom)

    # 页面里必须有一个**逐字命中**的句柄，否则这条测试会因为"判成未判定"
    # 而通过，跟抽不抽 ids 完全无关 —— 那就测了个寂寞。
    site = bili_site(search_handle_pattern=r'space\.bilibili\.com/([\w]+)')
    html = '<a href="https://space.bilibili.com/wizzer">wizzer</a>'
    out = _run_site_result(monkeypatch, site, html)

    assert out['status'].status.value == 'Claimed'
    assert out['status'].ids_data in (None, {})


def test_http_sites_still_extract_ids(monkeypatch):
    """对照组：普通站点照旧抽。没有它，上面那条测试可能只是"永远没进那个分支"。"""
    from maigret import checking
    from maigret.cn.model import Site

    called = []

    def fake_extract(html, logger, site):
        called.append(html)
        return {'other_username': 'laowang'}

    monkeypatch.setattr(checking, 'extract_ids_data', fake_extract)
    monkeypatch.setattr(checking, 'parse_usernames', lambda ids, logger: [])

    spec = Site(name='X', uri_check='https://x.cn/{account}', e_code=200, m_code=404)
    out = _run_site_result(monkeypatch, spec, '<html>hi</html>')

    assert called
    assert out['status'].ids_data == {'other_username': 'laowang'}
