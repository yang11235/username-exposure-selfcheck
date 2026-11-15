"""Smoke tests for the Flask web interface in maigret.web.app.

The goal is to catch breakage in the basic user flow (render index, kick off
search, redirect to results) without making real network calls. Heavy maigret
internals are mocked; the report-generation smoke test keeps `save_graph_report`
unmocked so regressions like `nt.options.groups = ...` (AttributeError on a
plain dict) are caught automatically.
"""

import asyncio
import json
import os

import pytest

import maigret
import maigret.report
import maigret.settings
from maigret.result import MaigretCheckResult, MaigretCheckStatus
from maigret.web import app as web_app_module

CUR_PATH = os.path.dirname(os.path.realpath(__file__))
TEST_DB = os.path.join(CUR_PATH, 'db.json')


class _SyncThread:
    """Drop-in for threading.Thread that runs target synchronously on start()."""

    def __init__(self, target=None, args=(), kwargs=None, **_):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def web_app(tmp_path):
    web_app_module.app.config['TESTING'] = True
    web_app_module.app.config['REPORTS_FOLDER'] = str(tmp_path)
    web_app_module.app.config['MAIGRET_DB_FILE'] = TEST_DB
    web_app_module.app.config['SETTINGS_FILE'] = str(tmp_path / 'web_settings.json')

    web_app_module.background_jobs.clear()
    web_app_module.job_results.clear()

    yield web_app_module

    web_app_module.background_jobs.clear()
    web_app_module.job_results.clear()


@pytest.fixture
def client(web_app):
    return web_app.app.test_client()


def test_index_renders(client):
    resp = client.get('/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="usernames"' in body
    assert '<form' in body


def test_search_empty_input_redirects_to_index(client):
    resp = client.post('/search', data={'usernames': ''})
    assert resp.status_code == 302
    assert resp.location.rstrip('/').endswith('') or resp.location.endswith('/')


def test_search_redirects_to_status(client, web_app, monkeypatch):
    monkeypatch.setattr(web_app, 'process_search_task', lambda *a, **kw: None)
    monkeypatch.setattr(web_app, 'Thread', _SyncThread)

    resp = client.post('/search', data={'usernames': 'soxoj'})

    assert resp.status_code == 302
    assert '/status/' in resp.location


def test_invalid_timestamp_redirects_to_index(client):
    resp = client.get('/status/nonexistent_ts')
    assert resp.status_code == 302
    assert resp.location.endswith('/')


def test_status_running_renders_status_page(client, web_app, monkeypatch):
    """While the background job is still running, /status/<ts> returns 200."""

    def never_completes(usernames, options, timestamp):
        # leave background_jobs[timestamp]['completed'] as False
        pass

    monkeypatch.setattr(web_app, 'process_search_task', never_completes)
    monkeypatch.setattr(web_app, 'Thread', _SyncThread)

    post = client.post('/search', data={'usernames': 'soxoj'})
    status_resp = client.get(post.location)

    assert status_resp.status_code == 200


def test_completed_search_redirects_to_results(client, web_app, monkeypatch):
    """Happy path: POST /search → background completes → /status/<ts> → /results/<session>."""

    def fake_task(usernames, options, timestamp):
        web_app.job_results[timestamp] = {
            'status': 'completed',
            'session_folder': f'search_{timestamp}',
            'graph_file': f'search_{timestamp}/combined_graph.html',
            'usernames': usernames,
            'individual_reports': [],
        }
        web_app.background_jobs[timestamp]['completed'] = True

    monkeypatch.setattr(web_app, 'process_search_task', fake_task)
    monkeypatch.setattr(web_app, 'Thread', _SyncThread)

    post = client.post('/search', data={'usernames': 'soxoj'})
    assert post.status_code == 302

    status_resp = client.get(post.location)
    assert status_resp.status_code == 302
    assert '/results/search_' in status_resp.location

    results_resp = client.get(status_resp.location)
    assert results_resp.status_code == 200
    assert b'soxoj' in results_resp.data


def test_results_report_links_open_in_new_tab(client, web_app, monkeypatch):
    """CSV/JSON/PDF/HTML report links must open in a new tab, not navigate away
    from the results page."""

    def fake_task(usernames, options, timestamp):
        web_app.job_results[timestamp] = {
            'status': 'completed',
            'session_folder': f'search_{timestamp}',
            'graph_file': f'search_{timestamp}/combined_graph.html',
            'usernames': usernames,
            'individual_reports': [
                {
                    'username': 'soxoj',
                    'csv_file': f'search_{timestamp}/report_soxoj.csv',
                    'json_file': f'search_{timestamp}/report_soxoj.json',
                    'pdf_file': f'search_{timestamp}/report_soxoj.pdf',
                    'html_file': f'search_{timestamp}/report_soxoj.html',
                    'claimed_profiles': [],
                }
            ],
        }
        web_app.background_jobs[timestamp]['completed'] = True

    monkeypatch.setattr(web_app, 'process_search_task', fake_task)
    monkeypatch.setattr(web_app, 'Thread', _SyncThread)

    post = client.post('/search', data={'usernames': 'soxoj'})
    status_resp = client.get(post.location)
    results_resp = client.get(status_resp.location)
    body = results_resp.get_data(as_text=True)

    # 本分支把 Web UI 文案改成了中文，这里断言的是"链接和 target 在同一个
    # <a> 里"，不是"标签叫 CSV Report"，所以跟着改文案不降低测试强度。
    for label in ('CSV 报告', 'JSON 报告', 'PDF 报告', 'HTML 报告'):
        # crude but effective: the link and its target="_blank" must appear
        # within the same <a> tag, not just somewhere on the page.
        idx = body.index(label)
        tag_start = body.rindex('<a ', 0, idx)
        tag = body[tag_start : idx + len(label)]
        assert 'target="_blank"' in tag, f'{label} link missing target="_blank"'


def test_failed_task_redirects_to_index(client, web_app, monkeypatch):
    def failing_task(usernames, options, timestamp):
        web_app.job_results[timestamp] = {'status': 'failed', 'error': 'boom'}
        web_app.background_jobs[timestamp]['completed'] = True

    monkeypatch.setattr(web_app, 'process_search_task', failing_task)
    monkeypatch.setattr(web_app, 'Thread', _SyncThread)

    post = client.post('/search', data={'usernames': 'soxoj'})
    status_resp = client.get(post.location)

    assert status_resp.status_code == 302
    assert status_resp.location.endswith('/')


def test_download_report_serves_file_inside_reports_folder(client, web_app, tmp_path):
    """Happy path: a real file inside REPORTS_FOLDER is served back."""
    target = tmp_path / 'session1'
    target.mkdir()
    (target / 'report.json').write_text('{"ok": true}')

    resp = client.get('/reports/session1/report.json')

    assert resp.status_code == 200
    assert resp.get_data() == b'{"ok": true}'


def test_download_report_blocks_dotdot_traversal(client, web_app, tmp_path):
    """A literal ../ in the path must not escape REPORTS_FOLDER."""
    secret = tmp_path.parent / 'outside_secret.txt'
    secret.write_text('SECRET')

    resp = client.get('/reports/..%2Foutside_secret.txt')

    assert resp.status_code == 404
    assert b'SECRET' not in resp.get_data()


def test_download_report_blocks_sibling_prefix_bypass(client, web_app, tmp_path):
    """Regression: the previous startswith() check let `<reports_root>2/secret`
    bypass containment because '/tmp/maigret_reports2'.startswith('/tmp/maigret_reports')
    is True. send_from_directory enforces a real boundary."""
    sibling = tmp_path.parent / (tmp_path.name + '_sibling')
    sibling.mkdir()
    (sibling / 'leak.txt').write_text('LEAK')

    encoded = '..%2F' + sibling.name + '%2Fleak.txt'
    resp = client.get('/reports/' + encoded)

    assert resp.status_code == 404
    assert b'LEAK' not in resp.get_data()


def test_download_report_blocks_absolute_path(client, web_app, tmp_path):
    """An absolute filename must not escape REPORTS_FOLDER."""
    secret = tmp_path.parent / 'abs_secret.txt'
    secret.write_text('ABSOLUTE')

    resp = client.get('/reports/' + str(secret).lstrip('/'))

    assert resp.status_code == 404
    assert b'ABSOLUTE' not in resp.get_data()


def test_search_passes_cloudflare_bypass_from_settings(client, web_app, monkeypatch):
    """If settings.json enables cloudflare_bypass with a valid FlareSolverr module,
    the web search must forward that config to maigret.search via the
    cloudflare_bypass kwarg. Guards the wiring in maigret_search()."""

    captured = {}

    async def fake_search(*args, **kwargs):
        captured.update(kwargs)
        return {}

    def fake_load(self, paths=None):
        self.cloudflare_bypass = {
            "enabled": True,
            "session_prefix": "test-prefix",
            "trigger_protection": ["cf_js_challenge"],
            "modules": [
                {
                    "name": "flaresolverr",
                    "method": "json_api",
                    "url": "http://flare.test:8191/v1",
                    "max_timeout_ms": 60000,
                }
            ],
        }
        return True, ""

    monkeypatch.setattr(maigret.settings.Settings, 'load', fake_load)
    monkeypatch.setattr(maigret, 'search', fake_search)
    monkeypatch.setattr(maigret.report, 'save_graph_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_csv_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_json_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_pdf_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_html_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'generate_report_context', lambda *a, **kw: {})
    monkeypatch.setattr(web_app, 'Thread', _SyncThread)

    client.post('/search', data={'usernames': 'testuser'})

    assert (
        'cloudflare_bypass' in captured
    ), 'maigret.search was not given a cloudflare_bypass kwarg'
    cf = captured['cloudflare_bypass']
    assert cf is not None
    assert cf['session_prefix'] == 'test-prefix'
    assert cf['trigger_protection'] == ['cf_js_challenge']
    assert len(cf['modules']) == 1
    assert cf['modules'][0]['url'] == 'http://flare.test:8191/v1'
    assert cf['modules'][0]['method'] == 'json_api'


def test_search_omits_cloudflare_bypass_when_disabled(client, web_app, monkeypatch):
    """When settings has no cloudflare_bypass (or enabled=false), the kwarg
    must be None so the default checker pipeline runs."""

    captured = {}

    async def fake_search(*args, **kwargs):
        captured.update(kwargs)
        return {}

    def fake_load(self, paths=None):
        # no cloudflare_bypass attribute at all
        return True, ""

    monkeypatch.setattr(maigret.settings.Settings, 'load', fake_load)
    monkeypatch.setattr(maigret, 'search', fake_search)
    monkeypatch.setattr(maigret.report, 'save_graph_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_csv_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_json_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_pdf_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_html_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'generate_report_context', lambda *a, **kw: {})
    monkeypatch.setattr(web_app, 'Thread', _SyncThread)

    client.post('/search', data={'usernames': 'testuser'})

    assert captured.get('cloudflare_bypass') is None


def test_live_scan_streams_found_and_done(client, web_app, monkeypatch):
    """POST /api/scan starts a background scan; GET .../stream yields the per-site
    'found' event and a terminating 'done' event. Guards the SSE + StreamNotify wiring.
    """

    async def fake_search(*args, **kwargs):
        notify = kwargs['query_notify']
        result = MaigretCheckResult(
            username='soxoj',
            site_name='GitHub',
            site_url_user='https://github.com/soxoj',
            status=MaigretCheckStatus.CLAIMED,
            ids_data={'fullname': 'Soxoj', '_extractor': 'x'},
            tags=['dev'],
        )
        notify.update(result)
        return {'GitHub': {'status': result, 'url_user': result.site_url_user}}

    monkeypatch.setattr(maigret, 'search', fake_search)
    # csv/json/pdf report internals are exercised by
    # test_real_report_generation_does_not_crash; here we only care that a
    # completed live scan wires into the same report + results-page flow.
    monkeypatch.setattr(maigret.report, 'save_csv_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_json_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_pdf_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_html_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'generate_report_context', lambda *a, **kw: {})

    start = client.post('/api/scan', data={'usernames': 'soxoj'})
    assert start.status_code == 200
    job_id = start.get_json()['job_id']

    body = client.get(f'/api/scan/{job_id}/stream').get_data(as_text=True)
    events = [
        json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')
    ]
    types_seen = [e['type'] for e in events]

    assert 'done' in types_seen
    found = [e for e in events if e['type'] == 'found']
    assert found and found[0]['site'] == 'GitHub'
    # _extractor metadata is stripped from the graph payload
    assert '_extractor' not in found[0]['ids']
    assert found[0]['ids']['fullname'] == 'Soxoj'

    # Regression guard: a completed live scan must still produce the same
    # report files + profile list as the classic /search flow, and hand the
    # browser a redirect to the results page that shows them.
    done_event = next(e for e in events if e['type'] == 'done')
    assert done_event['redirect'] == f'/results/search_{job_id}'

    result = web_app.job_results[job_id]
    assert result['status'] == 'completed'
    reports = result['individual_reports']
    assert reports and reports[0]['username'] == 'soxoj'
    assert reports[0]['claimed_profiles'][0]['site_name'] == 'GitHub'

    results_page = client.get(done_event['redirect']).get_data(as_text=True)
    assert 'GitHub' in results_page
    assert 'CSV 报告' in results_page


def test_live_scan_empty_username_rejected(client, web_app):
    resp = client.post('/api/scan', data={'usernames': ''})
    assert resp.status_code == 400


def test_live_scan_stop_unknown_job_404(client, web_app):
    resp = client.post('/api/scan/nope/stop')
    assert resp.status_code == 404


def test_live_start_empty_username_redirects_to_index(client, web_app):
    resp = client.post('/live', data={'usernames': ''})
    assert resp.status_code == 302
    assert resp.location.endswith('/')


def test_live_start_redirects_to_dedicated_live_page(client, web_app, monkeypatch):
    """POST /live starts a job on a NEW page (/live/<job_id>), not inline on
    the index page. That page must show the graph + a Stop button, and must
    NOT unconditionally redirect away on completion (only via the Analyze
    button — see test_live_scan_done_event_offers_redirect_not_auto_navigation)."""

    async def fake_search(*args, **kwargs):
        notify = kwargs['query_notify']
        notify.set_total(0)
        return {}

    monkeypatch.setattr(maigret, 'search', fake_search)

    start = client.post('/live', data={'usernames': 'soxoj'})
    assert start.status_code == 302
    assert start.location.startswith('/live/')
    job_id = start.location.rsplit('/', 1)[1]

    page = client.get(start.location)
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert 'id="graph"' in body
    assert 'id="stopBtn"' in body
    assert 'id="analyzeBtn"' in body
    assert job_id in body
    # No unconditional navigation on completion anymore.
    assert 'window.location.href = ev.redirect' not in body

    # Drain the SSE stream so the background thread's queue is consumed and
    # the job entry is cleaned up tidily.
    client.get(f'/api/scan/{job_id}/stream')


def test_live_results_unknown_job_redirects_to_index(client, web_app):
    resp = client.get('/live/does-not-exist')
    assert resp.status_code == 302
    assert resp.location.endswith('/')


def test_live_results_for_finished_job_skips_sse_and_shows_analyze(client, web_app):
    """If the job already finished (e.g. the user reloaded the Live Results
    page), the page must offer the Analyze redirect immediately instead of
    trying to reopen a dead SSE stream."""
    web_app.job_results['finishedjob'] = {
        'status': 'completed',
        'session_folder': 'search_finishedjob',
        'graph_file': 'search_finishedjob/combined_graph.html',
        'usernames': ['soxoj'],
        'individual_reports': [],
        'found_count': 0,
    }

    resp = client.get('/live/finishedjob')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'const doneRedirect = "/results/search_finishedjob";' in body


def test_live_scan_done_event_offers_redirect_not_auto_navigation(
    client, web_app, monkeypatch
):
    """The SSE 'done' payload still carries the redirect URL (consumed by the
    Analyze button), but nothing server- or client-side forces navigation."""

    async def fake_search(*args, **kwargs):
        notify = kwargs['query_notify']
        result = MaigretCheckResult(
            username='soxoj',
            site_name='GitHub',
            site_url_user='https://github.com/soxoj',
            status=MaigretCheckStatus.CLAIMED,
            ids_data={},
        )
        notify.update(result)
        return {'GitHub': {'status': result, 'url_user': result.site_url_user}}

    monkeypatch.setattr(maigret, 'search', fake_search)
    monkeypatch.setattr(maigret.report, 'save_graph_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_csv_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_json_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_pdf_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_html_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'generate_report_context', lambda *a, **kw: {})

    start = client.post('/live', data={'usernames': 'soxoj'})
    job_id = start.location.rsplit('/', 1)[1]

    body = client.get(f'/api/scan/{job_id}/stream').get_data(as_text=True)
    events = [
        json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')
    ]
    done_event = next(e for e in events if e['type'] == 'done')
    assert done_event['redirect'] == f'/results/search_{job_id}'

    result = web_app.job_results[job_id]
    assert result['status'] == 'completed'
    assert result['found_count'] == 1
    assert 'started_at' in result


def test_live_scan_stop_mid_scan_keeps_already_found_results(
    client, web_app, monkeypatch
):
    """Regression: clicking Stop while a username's scan is still in-flight
    used to discard every 'found' result already streamed to the live graph,
    because the cancelled search() task never returns its own results dict —
    general_results stayed empty, build_reports never ran, and the browser
    got 'Completed — nothing to analyze.' despite the graph showing hits.

    StreamNotify now keeps a running copy of what it already streamed, and
    that's what gets reported when the task is cancelled mid-scan.
    """

    async def fake_search(*args, **kwargs):
        notify = kwargs['query_notify']
        assert 'ValidActive' in notify.sites, 'site map not wired into StreamNotify'
        found = MaigretCheckResult(
            username='soxoj',
            site_name='ValidActive',
            site_url_user='https://play.google.com/store/apps/developer?id=soxoj',
            status=MaigretCheckStatus.CLAIMED,
        )
        notify.update(found)
        # Simulate task.cancel() firing mid-scan, after this one site was
        # already checked and streamed to the browser but before the other
        # (still in-flight) sites finished.
        raise asyncio.CancelledError()

    monkeypatch.setattr(maigret, 'search', fake_search)
    monkeypatch.setattr(maigret.report, 'save_graph_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_csv_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_json_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_pdf_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_html_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'generate_report_context', lambda *a, **kw: {})

    start = client.post('/live', data={'usernames': 'soxoj'})
    job_id = start.location.rsplit('/', 1)[1]

    body = client.get(f'/api/scan/{job_id}/stream').get_data(as_text=True)
    events = [
        json.loads(line[6:]) for line in body.splitlines() if line.startswith('data: ')
    ]
    types_seen = [e['type'] for e in events]
    assert 'stopped' in types_seen
    found = [e for e in events if e['type'] == 'found']
    assert found and found[0]['site'] == 'ValidActive'

    done_event = next(e for e in events if e['type'] == 'done')
    assert (
        done_event.get('redirect') == f'/results/search_{job_id}'
    ), "Stop must not discard already-found results ('nothing to analyze' bug)"

    result = web_app.job_results[job_id]
    assert result['status'] == 'completed'
    assert result['found_count'] == 1
    assert result['individual_reports'][0]['claimed_profiles'][0]['site_name'] == (
        'ValidActive'
    )


def test_real_report_generation_does_not_crash(client, web_app, monkeypatch):
    """End-to-end with mocked maigret.search but REAL report generation.

    This is the regression guard for bugs inside `save_graph_report` and friends
    (e.g. `nt.options.groups = ...` raising AttributeError on a dict). If any of
    the unmocked report functions throws, the task records a failed status and
    this assertion catches it.
    """

    async def fake_search(*args, **kwargs):
        return {}

    monkeypatch.setattr(maigret, 'search', fake_search)
    # Mock the per-username report writers — they are not what we care about here,
    # and pdf/html generation pulls in xhtml2pdf which is slow and brittle.
    monkeypatch.setattr(maigret.report, 'save_csv_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_json_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_pdf_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_html_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'generate_report_context', lambda *a, **kw: {})
    monkeypatch.setattr(web_app, 'Thread', _SyncThread)

    post = client.post('/search', data={'usernames': 'testuser'})
    timestamp = post.location.rsplit('/', 1)[1]

    assert timestamp in web_app.job_results, 'background task did not record any result'
    result = web_app.job_results[timestamp]
    assert (
        result['status'] == 'completed'
    ), f"report generation failed: {result.get('error')!r}"

    # Regression guard: pyvis's default cdn_resources="local" writes a lib/
    # folder relative to the process cwd instead of next to the graph HTML,
    # so the browser 404s fetching lib/bindings/utils.js from /reports/...
    graph_path = os.path.join(web_app.app.config['REPORTS_FOLDER'], result['graph_file'])
    with open(graph_path, encoding='utf-8') as f:
        graph_html = f.read()
    assert 'lib/bindings' not in graph_html
    assert not os.path.exists(os.path.join(os.path.dirname(graph_path), 'lib'))


def test_history_empty_state(client, web_app):
    resp = client.get('/history')
    assert resp.status_code == 200
    assert '还没有执行过检索。' in resp.get_data(as_text=True)


def test_history_link_present_on_every_page(client, web_app):
    resp = client.get('/')
    body = resp.get_data(as_text=True)
    assert 'href="/history"' in body


def test_new_search_link_present_on_every_page(client, web_app):
    resp = client.get('/history')
    body = resp.get_data(as_text=True)
    assert '新建检索' in body
    assert 'href="/"' in body


def test_history_lists_completed_and_failed_runs(client, web_app):
    web_app.job_results['ts_completed'] = {
        'status': 'completed',
        'session_folder': 'search_ts_completed',
        'graph_file': 'search_ts_completed/combined_graph.html',
        'usernames': ['soxoj', 'alice'],
        'individual_reports': [],
        'found_count': 7,
        'started_at': '2026-07-28 10:00:00',
    }
    web_app.job_results['ts_failed'] = {
        'status': 'failed',
        'error': 'boom',
        'usernames': ['bob'],
        'started_at': '2026-07-28 09:00:00',
    }

    resp = client.get('/history')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert '2026-07-28 10:00:00' in body
    assert 'soxoj, alice' in body
    assert '>7<' in body
    assert '已完成' in body
    assert '/results/search_ts_completed' in body

    assert '2026-07-28 09:00:00' in body
    assert 'bob' in body
    assert '失败' in body

    # Newest run listed first.
    assert body.index('search_ts_completed') < body.index('bob')


def test_build_reports_computes_found_count(web_app, monkeypatch):
    """Regression guard: History reads `found_count` off the dict build_reports
    returns, so it must count claimed profiles across all usernames."""
    monkeypatch.setattr(maigret.report, 'save_csv_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_json_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_pdf_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_html_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'generate_report_context', lambda *a, **kw: {})

    claimed = MaigretCheckResult(
        username='soxoj',
        site_name='GitHub',
        site_url_user='https://github.com/soxoj',
        status=MaigretCheckStatus.CLAIMED,
    )
    general_results = [
        (
            'soxoj',
            'username',
            {'GitHub': {'status': claimed, 'url_user': claimed.site_url_user}},
        )
    ]

    report = web_app.build_reports(general_results, ['soxoj'], 'testkey')

    assert report['found_count'] == 1
    assert report['individual_reports'][0]['claimed_profiles'][0]['site_name'] == 'GitHub'


def test_process_search_task_records_started_at_on_success(web_app, monkeypatch):
    async def fake_search_multi(usernames, options):
        return []

    monkeypatch.setattr(web_app, 'search_multiple_usernames', fake_search_multi)
    monkeypatch.setattr(
        web_app,
        'build_reports',
        lambda *a, **kw: {
            'status': 'completed',
            'session_folder': 'x',
            'graph_file': 'x',
            'usernames': [],
            'individual_reports': [],
            'found_count': 0,
        },
    )
    web_app.background_jobs['ts_ok'] = {'completed': False, 'thread': None}

    web_app.process_search_task(['soxoj'], {}, 'ts_ok')

    assert web_app.job_results['ts_ok']['status'] == 'completed'
    assert web_app.job_results['ts_ok']['started_at']


def test_process_search_task_records_started_at_on_failure(web_app, monkeypatch):
    async def failing_search_multi(usernames, options):
        raise RuntimeError('boom')

    monkeypatch.setattr(web_app, 'search_multiple_usernames', failing_search_multi)
    web_app.background_jobs['ts_fail'] = {'completed': False, 'thread': None}

    web_app.process_search_task(['soxoj'], {}, 'ts_fail')

    assert web_app.job_results['ts_fail']['status'] == 'failed'
    assert web_app.job_results['ts_fail']['started_at']


def test_load_settings_defaults_when_no_file(web_app):
    settings = web_app.load_settings()
    assert settings['timeout'] == 10
    assert settings['top_sites'] == 500
    assert settings['tags'] == []
    assert settings['proxy'] == ''
    assert settings['permute'] is False


def test_save_settings_persists_to_file_and_reloads(web_app):
    web_app.save_settings(
        {**web_app.DEFAULT_SETTINGS, 'timeout': 42, 'proxy': '127.0.0.1:9999'}
    )

    assert os.path.exists(web_app.app.config['SETTINGS_FILE'])
    reloaded = web_app.load_settings()
    assert reloaded['timeout'] == 42
    assert reloaded['proxy'] == '127.0.0.1:9999'


def test_settings_update_saves_and_redirects_back(client, web_app):
    resp = client.post(
        '/settings',
        data={
            'timeout': '15',
            'top_sites': '250',
            'tags': ['coding', 'tech'],
            'excluded_tags': ['porn'],
            'site': 'GitHub, Reddit',
            'proxy': '127.0.0.1:1080',
            'permute': 'on',
            'with_domains': 'on',
        },
        headers={'Referer': '/history'},
    )
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/history'

    settings = web_app.load_settings()
    assert settings['timeout'] == 15
    assert settings['top_sites'] == 250
    assert settings['tags'] == ['coding', 'tech']
    assert settings['excluded_tags'] == ['porn']
    assert settings['site_list'] == ['GitHub', 'Reddit']
    assert settings['proxy'] == '127.0.0.1:1080'
    assert settings['permute'] is True
    assert settings['with_domains'] is True
    assert settings['disable_recursive_search'] is False


def test_settings_update_invalid_timeout_falls_back_to_default(client, web_app):
    client.post('/settings', data={'timeout': 'not-a-number', 'top_sites': 'nope'})
    settings = web_app.load_settings()
    assert settings['timeout'] == web_app.DEFAULT_SETTINGS['timeout']
    assert settings['top_sites'] == web_app.DEFAULT_SETTINGS['top_sites']


def test_parse_search_options_uses_saved_settings(web_app):
    web_app.save_settings(
        {
            **web_app.DEFAULT_SETTINGS,
            'timeout': 20,
            'top_sites': 100,
            'proxy': '127.0.0.1:8080',
            'tags': ['gaming'],
            'site_list': ['GitHub'],
            'disable_extracting': True,
        }
    )

    options = web_app.parse_search_options({})

    assert options['timeout'] == 20
    assert options['top_sites'] == 100
    assert options['proxy'] == '127.0.0.1:8080'
    assert options['tags'] == ['gaming']
    assert options['site_list'] == ['GitHub']
    assert options['disable_extracting'] is True
    assert options['all_sites'] is False


def test_parse_search_options_full_mode_ignores_top_sites(web_app):
    options = web_app.parse_search_options({'mode': 'full'})
    assert options['all_sites'] is True


def test_api_sites_returns_site_list(client, web_app):
    resp = client.get('/api/sites')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'sites' in data
    assert isinstance(data['sites'], list)


def test_settings_modal_present_on_every_page(client, web_app):
    resp = client.get('/')
    body = resp.get_data(as_text=True)
    assert 'id="settingsModal"' in body
    assert 'name="timeout"' in body

    resp = client.get('/history')
    body = resp.get_data(as_text=True)
    assert 'id="settingsModal"' in body


# ---------------------------------------------------------------------------
# 快速检索必须含中文站
# ---------------------------------------------------------------------------
def test_fast_mode_includes_chinese_sites(web_app, monkeypatch):
    """Web 端的"快速检索"和命令行的默认扫描必须是同一份站点清单。

    这里用的是 tests/db.json（只有 2 个站），而中文站来自 cn overlay 的
    force_include —— 正好验证"不依赖站点库有多大，中文站都会进来"。
    """
    captured = {}

    async def fake_search(**kwargs):
        captured.update(kwargs.get('site_dict') or {})
        return []

    monkeypatch.setattr(web_app.maigret, 'search', fake_search)

    asyncio.run(
        web_app.maigret_search(
            'wizzer',
            {'top_sites': 500, 'tags': [], 'excluded_tags': [], 'site_list': []},
        )
    )

    assert 'Yuque' in captured
    assert 'v2ex.com' in captured
    # 站内搜索站点也要在默认清单里（B 站/网易云的昵称检索）
    assert 'Bilibili (搜索)' in captured
    assert 'NetEase Music (搜索)' in captured


def test_explicit_site_list_is_not_augmented(web_app, monkeypatch):
    """`--site` 语义是"只查这几个"，不能再塞 20 个中文站进去。"""
    captured = {}

    async def fake_search(**kwargs):
        captured.update(kwargs.get('site_dict') or {})
        return []

    monkeypatch.setattr(web_app.maigret, 'search', fake_search)

    asyncio.run(
        web_app.maigret_search(
            'wizzer',
            {
                'top_sites': 500,
                'tags': [],
                'excluded_tags': [],
                'site_list': ['ValidActive'],
            },
        )
    )

    assert set(captured) == {'ValidActive'}


# ---------------------------------------------------------------------------
# 跨站身份归并
# ---------------------------------------------------------------------------
def _site_result(status='Claimed', avatar=None, evidence=None):
    return {
        'status': {'status': status, 'reason': None, 'confidence': 0.9},
        'url_user': 'https://example.com/u',
        'cn_avatar_hash': avatar,
        'cn_evidence': evidence or [],
    }


def test_cn_clusters_groups_accounts_sharing_an_avatar(web_app):
    """头像 pHash 相同 → 聚成一簇，并且把"靠什么连起来"一起给出来。"""
    results = {
        'Gitee': _site_result(avatar='d35a84ce35748d55'),
        'NetEase Music': _site_result(avatar='d35a84ce35748d55'),
        'GitHub': _site_result(avatar='0000000000000000'),
    }

    groups = web_app._cn_clusters(results)

    assert len(groups) == 1
    assert {m['site'] for m in groups[0]['members']} == {'Gitee', 'NetEase Music'}
    assert groups[0]['confidence'] >= 0.6
    assert any('头像' in reason for m in groups[0]['members'] for reason in m['reasons'])


def test_cn_clusters_ignores_unconfirmed_accounts(web_app):
    """未判定的结果不能进聚类：那等于替用户编造身份关系。"""
    results = {
        'Gitee': _site_result(avatar='d35a84ce35748d55'),
        'Weibo': _site_result(status='Unknown', avatar='d35a84ce35748d55'),
    }

    assert web_app._cn_clusters(results) == []


def test_cn_clusters_returns_empty_on_failure(web_app, monkeypatch):
    """聚类炸了不能让结果页 500：它是锦上添花，不是主体。"""
    import maigret.cn.report as cn_report

    def boom(*args, **kwargs):
        raise RuntimeError('聚类炸了')

    monkeypatch.setattr(cn_report, 'build_accounts', boom)

    assert web_app._cn_clusters({'Gitee': _site_result()}) == []


def test_results_page_renders_identity_clusters(client, web_app):
    """身份簇不该依赖画像：没生成画像时它照样要显示。"""
    web_app.job_results['ts'] = {
        'status': 'completed',
        'session_folder': 'search_ts',
        'graph_file': 'search_ts/combined_graph.html',
        'usernames': ['wizzer'],
        'individual_reports': [
            {
                'username': 'wizzer',
                'csv_file': 'search_ts/report_wizzer.csv',
                'json_file': 'search_ts/report_wizzer.json',
                'pdf_file': 'search_ts/report_wizzer.pdf',
                'html_file': 'search_ts/report_wizzer.html',
                'claimed_profiles': [],
                'profile': None,
                'clusters': [
                    {
                        'confidence': 0.9,
                        'members': [
                            {
                                'site': 'Gitee',
                                'username': 'wizzer',
                                'url': 'https://gitee.com/wizzer',
                                'reasons': ['头像 pHash（距离 2）'],
                            },
                            {
                                'site': 'NetEase Music',
                                'username': 'WizZer',
                                'url': 'https://music.163.com/user/home?id=1',
                                'reasons': ['头像 pHash（距离 2）'],
                            },
                        ],
                    }
                ],
            }
        ],
    }

    body = client.get('/results/search_ts').get_data(as_text=True)

    assert '<div class="cluster-block">' in body
    assert '身份簇 #1' in body
    assert 'https://gitee.com/wizzer' in body
    assert '头像 pHash' in body


def test_results_page_hides_cluster_block_when_empty(client, web_app):
    """没有簇的时候不能留一个空壳区块（会让人以为归并跑失败了）。"""
    web_app.job_results['ts'] = {
        'status': 'completed',
        'session_folder': 'search_ts',
        'graph_file': None,
        'usernames': ['wizzer'],
        'individual_reports': [
            {
                'username': 'wizzer',
                'csv_file': 'search_ts/report_wizzer.csv',
                'json_file': 'search_ts/report_wizzer.json',
                'pdf_file': 'search_ts/report_wizzer.pdf',
                'html_file': 'search_ts/report_wizzer.html',
                'claimed_profiles': [],
                'profile': None,
                'clusters': [],
            }
        ],
    }

    body = client.get('/results/search_ts').get_data(as_text=True)

    # 用标签匹配而不是文字匹配：样式表里也有 "跨站身份归并" 的注释
    assert '<div class="cluster-block">' not in body


def test_build_reports_survives_an_empty_result_set(web_app, monkeypatch):
    """一个用户名都没搜成时，不能因为 `context` 未赋值把整个任务炸掉。

    真实故障（2026-11-15）：设置里勾了「使用 cookies」但 cookies.txt 不存在，
    检索在发出第一个请求前就失败，`search_multiple_usernames` 返回空列表，
    然后 `build_reports` 在循环外读 `context` → UnboundLocalError，
    用户看到的是"检索失败：cannot access local variable 'context'"。
    """
    monkeypatch.setattr(maigret.report, 'save_csv_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_json_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_pdf_report', lambda *a, **kw: None)
    monkeypatch.setattr(maigret.report, 'save_html_report', lambda *a, **kw: None)

    report = web_app.build_reports([], ['yang11235'], 'emptykey')

    assert report['status'] == 'completed'
    assert report['individual_reports'] == []
    assert report['found_count'] == 0
    # 没有结果就没有可分析的东西，别把空上下文喂给 AI 摘要
    assert report['ai_input'] == ''


def test_search_multiple_usernames_raises_when_every_username_fails(web_app, monkeypatch):
    """全部失败必须报错。

    返回空列表的话，用户看到的是一个"已完成但 0 命中"的结果页 ——
    和"这个人确实没账号"长得一模一样，而这正是最不该混淆的两件事。
    """

    async def boom(username, options):
        raise FileNotFoundError("No such file or directory: 'cookies.txt'")

    monkeypatch.setattr(web_app, 'maigret_search', boom)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(web_app.search_multiple_usernames(['a', 'b'], {}))

    assert 'cookies.txt' in str(excinfo.value)


def test_search_multiple_usernames_keeps_the_ones_that_worked(web_app, monkeypatch):
    """部分失败不该连累成功的：全丢掉的话用户会以为谁都没查。"""

    async def half(username, options):
        if username == 'bad':
            raise RuntimeError('站点库读取失败')
        return {'GitHub': {'status': None}}

    monkeypatch.setattr(web_app, 'maigret_search', half)

    out = asyncio.run(web_app.search_multiple_usernames(['bad', 'good'], {}))

    assert [u for u, _, _ in out] == ['good']


# ---------------------------------------------------------------------------
# 回归：浏览器 Cookie 开关必须真的传到检索
# ---------------------------------------------------------------------------
def test_parse_search_options_passes_browser_cookie_setting(web_app):
    """`resolve_cookie_source()` 读的是 options['use_browser_cookies']。

    这个键曾经漏在 parse_search_options 的返回字典里，于是设置页能勾、
    /api/browser-cookies 能显示"检测到 91 条"，检索时却一条都不用——
    开关看起来在工作，实际是死的。锁住它，别再漏。
    """
    web_app.save_settings({**web_app.DEFAULT_SETTINGS, 'use_browser_cookies': True})

    options = web_app.parse_search_options({})

    assert options['use_browser_cookies'] is True


def test_parse_search_options_browser_cookie_setting_defaults_off(web_app):
    """没配置时必须是 False，不能是 None 或缺键。

    缺键会让 `options.get(...)` 静默返回 None，和"用户没开"分不出来；
    显式布尔值才能在下游做可靠判断。
    """
    options = web_app.parse_search_options({})

    assert options['use_browser_cookies'] is False


def test_browser_cookie_source_is_reached_when_enabled(web_app, monkeypatch):
    """端到端：开了浏览器 Cookie，resolve_cookie_source 真的去调 browser_cookies.collect()。

    只断言 options 里有那个键还不够——中间任何一处把 options 换了对象，
    键在、链路断，用户看到的仍然是"勾了没用"。这里直接验调用发生。
    """
    import logging

    from maigret.cn import browser_cookies

    called = {}

    async def fake_collect():
        called['yes'] = True
        return []

    monkeypatch.setattr(browser_cookies, 'collect', fake_collect)

    options = web_app.parse_search_options({})
    options['use_browser_cookies'] = True

    asyncio.run(web_app.resolve_cookie_source(options, logging.getLogger('t')))

    assert called.get('yes') is True


# ---------------------------------------------------------------------------
# 回归：从主页 URL 开始时 logger 必须存在
# ---------------------------------------------------------------------------
def test_stream_search_with_parse_url_does_not_hit_undefined_logger(web_app, monkeypatch):
    """`_stream_search` 里曾漏了 `logger` 定义，被 except 吞成
    「URL 解析失败：name 'logger' is not defined」——于是"从主页 URL 开始"
    只解析出核心锚点，页面里的其它 ID 一个都抽不出来。

    这里替换 extract_ids_from_page 来记录收到的 logger 是否可用：
    logger 未定义时，调用点会抛 NameError，走不到这个假函数。
    """
    seen = {}

    def fake_extract(url, logger, timeout=5):
        seen['logger'] = logger
        return {'other_id': 'username'}

    monkeypatch.setattr(web_app, 'extract_ids_from_page', fake_extract)

    import queue as queue_mod

    q = queue_mod.Queue()
    job = {'queue': q, 'cancelled': True}

    asyncio.run(
        web_app._stream_search(
            job, ['soxoj'], {'parse_url': 'https://github.com/soxoj', 'timeout': 5}
        )
    )

    assert seen.get('logger') is not None

    events = []
    while not q.empty():
        events.append(q.get())

    errors = [e for e in events if e.get('type') == 'error']
    assert not any('logger' in str(e.get('message', '')) for e in errors), errors
    # 解析结果要真的推给前端
    assert any(e.get('type') == 'parsed' for e in events), events


# ---------------------------------------------------------------------------
# 安全：/settings 不能变成开放重定向
# ---------------------------------------------------------------------------
def test_settings_update_rejects_foreign_referrer(client):
    """Referer 是客户端可控头。直接 redirect(request.referrer) 等于给了
    一个跳到任意站点的入口，常被用来把钓鱼页伪装成本工具的后续步骤。
    """
    resp = client.post(
        '/settings',
        data={'timeout': '10', 'top_sites': '500'},
        headers={'Referer': 'https://evil.example.com/phish'},
    )

    assert resp.status_code == 302
    assert 'evil.example.com' not in resp.headers['Location']


def test_settings_update_allows_same_host_referrer(client):
    """站内地址要能跳回去——设置面板每页都有，保存后回到原页面才正常。

    注意返回的是**路径**而不是原始绝对 URL：只回 path 就不必比较 host，
    也就绕开了"Host 头同样可伪造"这个前提。浏览器拿到 Location: /history
    一样会跳回本页。
    """
    resp = client.post(
        '/settings',
        data={'timeout': '10', 'top_sites': '500'},
        headers={'Referer': 'http://localhost/history'},
    )

    assert resp.status_code == 302
    assert resp.headers['Location'] == '/history'


def test_settings_update_keeps_query_string(client):
    """带查询串的站内地址要保留查询串，否则跳回去筛选条件就丢了。"""
    resp = client.post(
        '/settings',
        data={'timeout': '10', 'top_sites': '500'},
        headers={'Referer': 'http://localhost/history?page=2&q=x'},
    )

    assert resp.status_code == 302
    assert resp.headers['Location'] == '/history?page=2&q=x'


def test_settings_update_rejects_control_char_bypass(client):
    """`/\\t/evil.com` 归一化后"以 / 开头、不以 // 开头"会被旧逻辑放行。

    TAB 在写响应头时被剥掉，Location 变成 `//evil.com`，浏览器按协议相对
    地址解析成外站。控制字符必须显式拒绝。
    """
    resp = client.post(
        '/settings',
        data={'timeout': '10', 'top_sites': '500'},
        headers={'Referer': '/\t/evil.example.com/x'},
    )

    assert resp.status_code == 302
    loc = resp.headers['Location']
    assert 'evil.example.com' not in loc
    assert loc == '/'


def test_settings_update_rejects_forged_host_with_matching_referrer(client):
    """同时伪造 Host 和 Referer 不能让外站跳转成立。

    若判定写成 `parsed.netloc == request.host`，而 Host 由客户端控制，
    两边一起伪造就通过了。只回 path 的方案不看 host，天然免疫。
    """
    resp = client.post(
        '/settings',
        data={'timeout': '10', 'top_sites': '500'},
        headers={
            'Host': 'evil.example.com',
            'Referer': 'http://evil.example.com/history',
        },
    )

    assert resp.status_code == 302
    assert 'evil.example.com' not in resp.headers['Location']


def test_settings_update_rejects_prefix_squatting_referrer(client):
    """`localhost:5000.evil.com` 以本站地址开头，startswith 会放行。
    必须按 host 全等判，不能按字符串前缀。
    """
    resp = client.post(
        '/settings',
        data={'timeout': '10', 'top_sites': '500'},
        headers={'Referer': 'http://localhost:5000.evil.com/x'},
    )

    assert resp.status_code == 302
    assert 'evil.com' not in resp.headers['Location']


def test_settings_update_rejects_protocol_relative_referrer(client):
    """`//evil.example.com` 是协议相对地址，浏览器会当成外站。
    它同时"以 / 开头"，只判 startswith('/') 会放行。
    """
    resp = client.post(
        '/settings',
        data={'timeout': '10', 'top_sites': '500'},
        headers={'Referer': '//evil.example.com/x'},
    )

    assert resp.status_code == 302
    assert 'evil.example.com' not in resp.headers['Location']


def test_settings_update_rejects_backslash_referrer(client):
    """反斜杠会被部分浏览器归一成斜杠，`/\\evil.com` 等同 `//evil.com`。"""
    resp = client.post(
        '/settings',
        data={'timeout': '10', 'top_sites': '500'},
        headers={'Referer': '/\\evil.example.com/x'},
    )

    assert resp.status_code == 302
    assert 'evil.example.com' not in resp.headers['Location']


# ---------------------------------------------------------------------------
# job_results 有界保留


# ---------------------------------------------------------------------------
# job_results 有界保留
# ---------------------------------------------------------------------------
def _seed_job(web_app, key, started, finished=None, status='completed'):
    """造一条 job_results 记录。finished=None 表示"还在跑"。"""
    entry = {
        'status': status,
        'started_at': started,
        'session_folder': f'search_{key}',
        'usernames': ['x'],
        'individual_reports': [],
        'found_count': 0,
        'ai_input': '',
        'graph_file': 'g',
    }
    if finished is not None:
        entry['finished_at'] = finished
    web_app.job_results[key] = entry
    web_app.background_jobs[key] = {'completed': finished is not None}


def test_prune_job_results_keeps_most_recently_finished(web_app):
    """裁剪按**完成时间**留最新的，不是开始时间。

    真实故障场景：一个跑了两小时的检索，started_at 是最早的；按开始时间排
    它就是"最旧"，任务一完成立刻把自己裁掉——用户等了两小时，拿到的是
    「无效的检索会话」。这条测试专门造这个形态。
    """
    web_app.job_results.clear()
    web_app.background_jobs.clear()

    limit = web_app.MAX_JOB_RESULTS
    # 造 limit 条"短任务"：开始和完成都集中在 02:xx 之后（都在 slow 完成之后）。
    # 关键：它们的 started_at 必须全部晚于 slow 的 started_at，否则两种排序
    # 都会挑中同一条，测试就分不出对错了。
    for i in range(limit):
        _seed_job(
            web_app,
            f'quick{i:03d}',
            started=f'2026-01-01 02:{i // 60:02d}:{i % 60:02d}',
            finished=f'2026-01-01 02:{i // 60:02d}:{i % 60:02d}',
        )
    # 1 条长任务：开始最早（00:00），完成最晚（04:00）。
    # 按 started_at 排序 → 它排第一 → 被裁掉（错误行为）；
    # 按 finished_at 排序 → 它排最后 → 留下（正确行为）。
    _seed_job(
        web_app, 'slow', started='2026-01-01 00:00:00', finished='2026-01-01 04:00:00'
    )

    # 刻意不传 keep：这里要单独考排序方向，别让 keep 把问题盖住。
    # 调用方（finally 里）会传 keep，但那是第二道保险，不是这道测试的目标。
    web_app._prune_job_results()

    assert len(web_app.job_results) == limit
    # 长任务开始最早、完成最晚 —— 按开始时间排会被误删，按完成时间排必须留下
    assert 'slow' in web_app.job_results, '按开始时间排序会把刚跑完的长任务裁掉'
    # 完成时间最早的那条被丢掉
    assert 'quick000' not in web_app.job_results


def test_prune_job_results_never_drops_in_flight_entries(web_app):
    """没有 finished_at 的条目（正在跑）不参与裁剪。

    旧实现把缺时间的当空串排序键，空串最小 → 排最前 → 第一个被删；
    接着 finally 里写 job_results[key]['started_at'] 抛 KeyError，
    /status 永久停在未完成，前端无限轮询。
    """
    web_app.job_results.clear()
    web_app.background_jobs.clear()

    limit = web_app.MAX_JOB_RESULTS
    for i in range(limit):
        _seed_job(
            web_app,
            f'done{i:03d}',
            started=f'2026-01-01 00:00:{i:02d}',
            finished=f'2026-01-01 00:05:{i:02d}',
        )
    # 一条正在跑的：有 started_at，没有 finished_at
    _seed_job(web_app, 'running', started='2026-01-01 00:00:00')

    web_app._prune_job_results()

    assert 'running' in web_app.job_results, '正在跑的条目被裁掉了'
    # 裁剪仍然发生了（回来一条旧的）
    assert len(web_app.job_results) == limit


def test_prune_job_results_keeps_background_job_record(web_app):
    """裁掉结果后 background_jobs 要留着。

    留着 → /status 走"该检索会话没有结果"（真话：跑完过，报告被回收了）。
    删掉 → /status 走"无效的检索会话"，和从未存在过的 timestamp 分不出来。
    """
    web_app.job_results.clear()
    web_app.background_jobs.clear()

    limit = web_app.MAX_JOB_RESULTS
    for i in range(limit + 1):
        _seed_job(
            web_app,
            f'j{i:03d}',
            started=f'2026-01-01 00:00:{i % 60:02d}',
            finished=f'2026-01-01 00:10:{i % 60:02d}',
        )

    web_app._prune_job_results()

    dropped = [k for k in [f'j{i:03d}' for i in range(limit + 1)]
               if k not in web_app.job_results]
    assert dropped, '这条测试需要至少裁掉一条'
    for key in dropped:
        assert key in web_app.background_jobs, f'{key} 的 background_jobs 被删了'


def test_prune_job_results_is_noop_below_limit(web_app):
    """没超限时不能有任何副作用。"""
    web_app.job_results.clear()
    web_app.background_jobs.clear()
    _seed_job(web_app, 'only', started='2026-01-01 00:00:00', finished='2026-01-01 00:01:00')

    web_app._prune_job_results()

    assert list(web_app.job_results) == ['only']
    assert 'only' in web_app.background_jobs


def test_snapshot_results_is_stable_while_pruning(web_app):
    """/results 与 /history 必须拿快照再遍历。

    直接 `for r in job_results.values()` 在另一线程 pop 时会抛
    `dictionary changed size during iteration`，而裁剪恰好发生在检索结束时。
    """
    web_app.job_results.clear()
    web_app.background_jobs.clear()

    limit = web_app.MAX_JOB_RESULTS
    for i in range(limit):
        _seed_job(
            web_app,
            f'j{i:03d}',
            started=f'2026-01-01 00:00:{i % 60:02d}',
            finished=f'2026-01-01 00:10:{i % 60:02d}',
        )

    snap = web_app._snapshot_results()
    # 快照拿到之后，另一线程裁剪，快照本身不受影响
    _seed_job(web_app, 'extra', started='2026-01-01 05:00:00', finished='2026-01-01 05:01:00')
    web_app._prune_job_results()

    assert len(snap) == limit  # 遍历不会 RuntimeError，长度也不变
    assert all(isinstance(item, tuple) and len(item) == 2 for item in snap)


def test_settings_update_rejects_triple_slash_referrer(client):
    """`///evil.com` 会被 urlparse 解析成 host=""、path="/evil.com"。

    单看 path 是个合法站内路径，就漏过去了；必须按原始串是否以 `//` 开头先挡。
    """
    for ref in ('///evil.example.com/x', '////evil.example.com/x'):
        resp = client.post(
            '/settings',
            data={'timeout': '10', 'top_sites': '500'},
            headers={'Referer': ref},
        )
        assert resp.status_code == 302
        assert 'evil.example.com' not in resp.headers['Location'], ref


def test_settings_update_rejects_uppercase_host_referrer(client):
    """大小写不同的 host 要按外站处理（比较是大小写敏感的）。

    这里不是"误拒合法请求"的 bug：真实浏览器发 Referer 时 host 与 Host 头
    同源同大小写，走的是 path 分支。构造 `EVIL.com` 只是确保不会被当成站内。
    """
    resp = client.post(
        '/settings',
        data={'timeout': '10', 'top_sites': '500'},
        headers={'Referer': 'http://EVIL.example.com/x'},
    )

    assert resp.status_code == 302
    assert 'EVIL.example.com' not in resp.headers['Location']


def test_settings_update_keeps_nested_path(client):
    """多段路径要原样保留，否则 /results/search_xxx 这种跳转就坏了。"""
    resp = client.post(
        '/settings',
        data={'timeout': '10', 'top_sites': '500'},
        headers={'Referer': '/results/search_20260101_120000'},
    )

    assert resp.status_code == 302
    assert resp.headers['Location'] == '/results/search_20260101_120000'


def test_prune_job_results_removes_all_prunable_when_most_are_in_flight(web_app):
    """候选（已完成）少于超出量时，要把候选全部裁掉，不动 in-flight 的。

    上限的语义是"最多保留 50 条已完成结果"，不是"总数硬上限"：正在跑的
    条目无法删除，只能等它们结束。所以这里断言的是"可裁的全裁掉了"，
    而不是"总数降到了 50"——后者在有 in-flight 时本来就不成立。
    """
    web_app.job_results.clear()
    web_app.background_jobs.clear()

    limit = web_app.MAX_JOB_RESULTS
    # 3 条已完成（可裁），60 条 in-flight（不可裁）
    for i in range(3):
        _seed_job(
            web_app, f'done{i}',
            started=f'2026-01-01 00:00:0{i}', finished=f'2026-01-01 00:01:0{i}',
        )
    for i in range(limit + 10):
        _seed_job(web_app, f'run{i:03d}', started='2026-01-01 00:00:00')

    web_app._prune_job_results()

    # 3 条可裁的全部删掉，in-flight 一条不动
    assert all(f'done{i}' not in web_app.job_results for i in range(3))
    assert all(f'run{i:03d}' in web_app.job_results for i in range(limit + 10))


@pytest.mark.parametrize(
    'raw,expected',
    [
        ('false', False),
        ('False', False),
        ('0', False),
        ('no', False),
        ('off', False),
        ('', False),
        ('true', True),
        ('1', True),
        ('yes', True),
        (True, True),
        (False, False),
        (None, False),
    ],
)
def test_as_bool_normalizes_handwritten_settings(web_app, raw, expected):
    """手写 settings.json 时 `"use_cookies": "false"` 必须真的是 False。

    `bool("false")` 是 True——用户以为自己关掉了开关，实际开着。
    这个坑对 use_cookies 和 use_browser_cookies 都成立。
    """
    assert web_app._as_bool(raw) is expected


def test_parse_search_options_honours_string_false(web_app, tmp_path):
    """settings.json 里手写字符串 "false" 时，options 里必须是 False。"""
    import json

    path = web_app.app.config['SETTINGS_FILE']
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'use_cookies': 'false', 'use_browser_cookies': '0'}, f)

    options = web_app.parse_search_options({})

    assert options['use_cookies'] is False
    assert options['use_browser_cookies'] is False


# ---------------------------------------------------------------------------
# 中文补检：不要重复快速检索已经覆盖的站
# ---------------------------------------------------------------------------
def test_cn_probe_no_longer_asks_for_tieba_cookie(web_app):
    """贴吧在快速检索里就能匿名判定，补检不该再要登录 Cookie。

    历史问题：补检里有一条 `probe_tieba` 走 `f/search/res`（"谁发帖提过
    这个词"的全站搜索），必须登录；而贴吧账号判定走的是
    `home/get/panel`，匿名即可。两条路径混在一起，导致用户明明能查到
    贴吧账号，补检却提示"贴吧需要登录 Cookie"，看起来像功能坏了。
    """
    from maigret.web import cn_probe

    assert not hasattr(cn_probe, 'probe_tieba'), 'probe_tieba 应该已经删除'
    names = [p.__name__ for p in cn_probe.PROBES]
    assert 'probe_tieba' not in names
    assert names == ['probe_bilibili', 'probe_weibo']


def test_cn_probe_reads_browser_cookies(web_app):
    """补检必须看得到从浏览器读到的 Cookie。

    踩过：补检只读 cookies.txt，于是"设置里勾了浏览器 Cookie、
    /api/browser-cookies 显示检测到 103 条、补检却说需要登录"——
    两条链路各读各的来源，谁也看不见对方。
    """
    from http.cookiejar import Cookie, CookieJar

    from maigret.web import cn_probe

    jar = CookieJar()
    jar.set_cookie(Cookie(
        version=0, name='SUB', value='weibo-session',
        port=None, port_specified=False,
        domain='.weibo.com', domain_specified=True, domain_initial_dot=True,
        path='/', path_specified=True,
        secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest={},
    ))

    mapping = cn_probe._cookie_map(jar)

    assert 'weibo.com' in mapping
    assert 'SUB=weibo-session' in mapping['weibo.com']


def test_cookie_header_matches_subdomains(web_app):
    """`.weibo.com` 的 cookie 要能被 `s.weibo.com` 用上。

    `_cookie_header` 靠 endswith 判作用域，所以 map 里的域名必须去掉
    前导点后再比较——这一步错了，Cookie 读到了也发不出去。
    """
    from maigret.web import cn_probe

    jar = {'weibo.com': ['SUB=x'], 'baidu.com': ['BAIDUID=y']}

    assert cn_probe._cookie_header(jar, 's.weibo.com') == 'SUB=x'
    assert cn_probe._cookie_header(jar, 'weibo.com') == 'SUB=x'
    assert cn_probe._cookie_header(jar, 'example.com') == ''


# ---------------------------------------------------------------------------
# 补检合并进检索
# ---------------------------------------------------------------------------
def test_stream_search_runs_cn_probe_automatically(web_app, monkeypatch):
    """检索结束要自动跑补检，不再让用户多点一次按钮。

    只对**第一个用户名**补检：permute 会生成几十个变体，逐个补检会把请求量
    放大到几百次，而变体本来就不是真实的人。
    """
    probed = {}

    async def fake_search(username, options, query_notify=None):
        return {'GitHub': {'status': None}}

    async def fake_probe(usernames, browser_jar=None):
        probed['usernames'] = list(usernames)
        return ([{'platform': 'B站', 'name': 'x', 'url': 'u', 'confidence': '高', 'extra': ''}],
                ['微博没有搜到同名用户'])

    monkeypatch.setattr(web_app, 'maigret_search', fake_search)
    monkeypatch.setattr(web_app, 'probe_platforms', fake_probe)

    import queue as queue_mod
    q = queue_mod.Queue()
    job = {'queue': q, 'cancelled': False}
    options = {'permute': False, 'parse_url': '', 'use_browser_cookies': False}

    asyncio.run(web_app._stream_search(job, ['soxoj', 'other'], options))

    # 只补检第一个用户名
    assert probed.get('usernames') == ['soxoj']
    # 结果存进 job，供 run_stream_job 转交结果页
    assert job['cn_probe']['items'][0]['platform'] == 'B站'

    events = []
    while not q.empty():
        events.append(q.get())
    cn_events = [e for e in events if e.get('type') == 'cn']
    assert len(cn_events) == 1
    assert cn_events[0]['notes'] == ['微博没有搜到同名用户']


def test_stream_search_survives_cn_probe_failure(web_app, monkeypatch):
    """补检失败不能连累已经拿到的检索结果。"""
    async def fake_search(username, options, query_notify=None):
        return {'GitHub': {'status': None}}

    async def boom(usernames, browser_jar=None):
        raise RuntimeError('CDP 连不上')

    monkeypatch.setattr(web_app, 'maigret_search', fake_search)
    monkeypatch.setattr(web_app, 'probe_platforms', boom)

    import queue as queue_mod
    q = queue_mod.Queue()
    job = {'queue': q, 'cancelled': False}

    out = asyncio.run(
        web_app._stream_search(job, ['soxoj'], {'parse_url': '', 'use_browser_cookies': False})
    )

    # 检索结果还在
    assert out and out[0][0] == 'soxoj'
    # 补检失败变成一条提示，不是异常
    events = []
    while not q.empty():
        events.append(q.get())
    cn_events = [e for e in events if e.get('type') == 'cn']
    assert cn_events and 'CDP 连不上' in cn_events[0]['notes'][0]


def test_cancelled_search_skips_cn_probe(web_app, monkeypatch):
    """用户点了停止就别再跑补检——他要的是立刻停。"""
    called = {'probe': False}

    async def fake_probe(usernames, browser_jar=None):
        called['probe'] = True
        return [], []

    monkeypatch.setattr(web_app, 'probe_platforms', fake_probe)

    import queue as queue_mod
    q = queue_mod.Queue()
    job = {'queue': q, 'cancelled': True}

    asyncio.run(web_app._stream_search(job, ['soxoj'], {'parse_url': '', 'use_browser_cookies': False}))

    assert called['probe'] is False


def test_results_page_renders_cn_probe_from_job(client, web_app):
    """自动补检的结果要出现在结果页上，用户不用再点按钮。"""
    web_app.job_results['k1'] = {
        'status': 'completed',
        'session_folder': 'search_k1',
        'usernames': ['soxoj'],
        'graph_file': 'search_k1/combined_graph.html',
        'individual_reports': [],
        'cn_probe': {
            'items': [{'platform': 'B站', 'name': 'SoXoJ', 'url': 'https://x',
                       'confidence': '高', 'extra': '粉丝 0'}],
            'notes': ['微博没有搜到同名用户'],
        },
    }

    body = client.get('/results/search_k1').get_data(as_text=True)

    assert 'renderCnProbe' in body
    # 中文在 tojson 里是 \uXXXX 转义，前端 JSON.parse 后还原。
    # 直接断言原文会失败，所以断言转义形态存在即可。
    escaped = json.dumps('SoXoJ', ensure_ascii=True)[1:-1]
    assert escaped in body
    assert 'renderCnProbe' in body
