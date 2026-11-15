from flask import (
    Flask,
    render_template,
    request,
    send_from_directory,
    Response,
    flash,
    redirect,
    url_for,
)
from werkzeug.exceptions import NotFound
import logging
import os
import asyncio
import json
import queue
import uuid
from datetime import datetime
from threading import Thread
from typing import Any, Dict
import maigret
import maigret.settings
from maigret.checking import build_cloudflare_bypass_config
from maigret.result import MaigretCheckStatus
from maigret.sites import MaigretDatabase
from maigret.report import generate_report_context
from maigret.web.profile_card import account_fields, build_profile
from maigret.web.ai_summary import load_ai_config, summarize
from maigret.web.cookie_store import (
    cookie_domains, import_netscape, looks_netscape, save_cookies,
)
from maigret.web.cn_probe import probe_platforms
from maigret.web.anchor import score_accounts
from maigret.permutator import Permute
from maigret.maigret import extract_ids_from_page
from maigret.web.url_anchor import resolve as resolve_anchor

app = Flask(__name__)
# Use environment variable for secret key, generate random one if not set
app.secret_key = os.getenv('FLASK_SECRET_KEY', os.urandom(24).hex())

# add background job tracking
background_jobs: Dict[str, Any] = {}
job_results = {}

# Live (streaming) scan jobs, keyed by job_id. Each entry:
#   {'queue': Queue, 'cancelled': bool, 'loop': event loop, 'task': asyncio task}
# ponytail: in-memory, single-process. Entries are dropped when their SSE stream
# ends; a stream that's never consumed lingers — fine for a local tool.
live_jobs: Dict[str, Any] = {}


class StreamNotify:
    """query_notify shim: pushes each per-site check into a queue as an SSE event.

    maigret's search loop calls update() once per finished site check, which is
    exactly the granularity we want to stream to the browser.
    """

    def __init__(self, event_queue, username):
        self.q = event_queue
        self.username = username
        self.total = 0
        self.checked = 0
        self.sites = {}
        # Per-site results collected so far, in the shape build_reports()
        # expects. If the scan gets cancelled mid-way (Stop button), this is
        # what's left to report on — otherwise every already-streamed
        # 'found' event is silently discarded because the search() task
        # never returns to hand back its own results dict.
        self.results = {}

    def set_total(self, total):
        self.total = total
        self.q.put({'type': 'start', 'username': self.username, 'total': total})

    def set_sites(self, sites):
        self.sites = sites

    def update(self, result, is_similar=False):
        self.checked += 1
        if not is_similar:
            entry = {'status': result, 'url_user': result.site_url_user}
            site = self.sites.get(result.site_name)
            if site is not None:
                entry['site'] = site
                entry['url_main'] = site.url_main
            self.results[result.site_name] = entry
        if result.status == MaigretCheckStatus.CLAIMED and not is_similar:
            ids = {
                k: v
                for k, v in (result.ids_data or {}).items()
                if k != '_extractor' and isinstance(v, (str, int, float))
            }
            self.q.put(
                {
                    'type': 'found',
                    'username': result.username or self.username,
                    'site': result.site_name,
                    'url': result.site_url_user,
                    'ids': ids,
                }
            )
        self.q.put(
            {
                'type': 'progress',
                'checked': self.checked,
                'total': self.total,
                'site': result.site_name,
            }
        )

    # No-op sinks for the rest of the notifier surface the search loop touches.
    def start(self, message=None, id_type="username"):
        pass

    def finish(self, message=None):
        pass

    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def success(self, *a, **k):
        pass

    def enrich(self, *a, **k):
        pass


# Configuration
app.config["MAIGRET_DB_FILE"] = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'resources', 'data.json'
)
_MAIGRET_HOME = os.path.join(os.path.expanduser("~"), ".maigret")
os.makedirs(_MAIGRET_HOME, exist_ok=True)
app.config["COOKIES_FILE"] = os.path.join(_MAIGRET_HOME, "cookies.txt")
app.config["UPLOAD_FOLDER"] = 'uploads'
app.config["REPORTS_FOLDER"] = os.path.join(_MAIGRET_HOME, "reports")
app.config["SETTINGS_FILE"] = os.path.join(_MAIGRET_HOME, "web_settings.json")

# Search-wide defaults, editable from the Settings modal (base.html). Persisted
# to app.config["SETTINGS_FILE"] so they survive a process restart.
DEFAULT_SETTINGS = {
    'timeout': 10,
    'top_sites': 500,
    'tags': [],
    'excluded_tags': [],
    'site_list': [],
    'proxy': '',
    'tor_proxy': '',
    'i2p_proxy': '',
    'permute': False,
    'disable_recursive_search': False,
    'disable_extracting': False,
    'with_domains': False,
    'use_cookies': False,
    # 从本机已登录的 Chrome 取 Cookie（CDP）。和 use_cookies 是两种来源，
    # 同时打开时以浏览器为准 —— 浏览器里的那份永远是"当前"的。
    'use_browser_cookies': False,
}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    path = app.config["SETTINGS_FILE"]
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                settings.update(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logging.error(f"Failed to load settings from {path}: {e}")
    return settings


def save_settings(settings):
    with open(app.config["SETTINGS_FILE"], 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)


def parse_settings_form(form):
    try:
        timeout = int(form.get('timeout'))
    except (TypeError, ValueError):
        timeout = DEFAULT_SETTINGS['timeout']

    try:
        top_sites = int(form.get('top_sites'))
    except (TypeError, ValueError):
        top_sites = DEFAULT_SETTINGS['top_sites']

    return {
        'timeout': timeout,
        'top_sites': top_sites,
        'tags': form.getlist('tags'),
        'excluded_tags': form.getlist('excluded_tags'),
        'site_list': [s.strip() for s in form.get('site', '').split(',') if s.strip()],
        'proxy': form.get('proxy', '').strip(),
        'tor_proxy': form.get('tor_proxy', '').strip(),
        'i2p_proxy': form.get('i2p_proxy', '').strip(),
        'permute': 'permute' in form,
        'disable_recursive_search': 'disable_recursive_search' in form,
        'disable_extracting': 'disable_extracting' in form,
        'with_domains': 'with_domains' in form,
        'use_cookies': 'use_cookies' in form,
        'use_browser_cookies': 'use_browser_cookies' in form,
    }


@app.context_processor
def inject_settings():
    return {'web_settings': load_settings(), 'cookie_domains': cookie_domains()}


def setup_logger(log_level, name):
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    return logger


async def resolve_cookie_source(options, logger):
    """决定这次检索用哪个 Cookie 来源。

    两种来源：
        use_browser_cookies  从本机已登录的 Chrome 取（CDP，内存直用、不落盘）
        use_cookies          读 ~/.maigret/cookies.txt

    浏览器优先：那份永远是"当前"的，而 cookies.txt 是某天手动粘进去的快照。
    浏览器取不到时**退回** cookies.txt（如果也勾了），再取不到就不带 Cookie
    继续跑 —— 和 `load_cookie_jar` 的降级口径一致：Cookie 只提高成功率，
    不该因为拿不到就让整次检索一个站点都不查。

    为什么失败只 warning 不抛：调试端口没开是最常见的情况（默认就没开），
    为它中断整次检索等于把"可选增强"变成了"硬前置"。
    """
    if options.get('use_browser_cookies'):
        try:
            from maigret.cn import browser_cookies

            raw = await browser_cookies.collect()
            if raw:
                logger.info(
                    "已从本机浏览器载入 %d 条 Cookie（覆盖 %d 个域名）",
                    len(raw),
                    len(browser_cookies.summarize(raw)),
                )
                return browser_cookies.to_cookiejar(raw)
            logger.warning("本机浏览器里没有可用的 Cookie，本次不带 Cookie")
        except Exception as exc:
            logger.warning("读取本机浏览器 Cookie 失败，本次不使用：%s", exc)

    if options.get('use_cookies'):
        return app.config["COOKIES_FILE"]
    return None


async def maigret_search(username, options, query_notify=None):
    logger = setup_logger(logging.WARNING, 'maigret')
    try:
        settings = maigret.settings.Settings()
        settings.load()
        cf_bypass_config = build_cloudflare_bypass_config(settings)
        if cf_bypass_config:
            modules_summary = ", ".join(
                f"{m.get('name', m.get('method'))}({m.get('url')})"
                for m in cf_bypass_config["modules"]
            )
            logger.info(
                f"Cloudflare webgate active: triggers={cf_bypass_config['trigger_protection']}, "
                f"modules=[{modules_summary}]"
            )

        db = MaigretDatabase().load_from_path(app.config["MAIGRET_DB_FILE"])

        # cn overlay 与 CLI 走同一条装配路径，否则 Web 端的"快速检索"会是
        # 一个只有 6 个中文站的子集，而命令行里同样是 500 站却含 20 个中文站——
        # 同一个开关在两个入口给出不同结果，用户只会认为工具坏了。
        from maigret.cn.hook import augment_sites, register_overlay_sites

        register_overlay_sites(db)

        top_sites = int(options.get('top_sites') or 500)
        if options.get('all_sites'):
            top_sites = 999999999  # effectively all

        tags = options.get('tags', [])
        excluded_tags = options.get('excluded_tags', [])
        site_list = options.get('site_list', [])
        logger.info(f"Filtering sites by tags: {tags}, excluded: {excluded_tags}")

        sites = db.ranked_sites_dict(
            top=top_sites,
            tags=tags,
            excluded_tags=excluded_tags,
            names=site_list,
            disabled=False,
            id_type='username',
        )

        if not site_list:
            sites = augment_sites(
                sites, db, 'username', tags=tags, excluded_tags=excluded_tags
            )

        logger.info(f"Found {len(sites)} sites matching the tag criteria")

        if query_notify is not None and hasattr(query_notify, 'set_total'):
            query_notify.set_total(len(sites))
        if query_notify is not None and hasattr(query_notify, 'set_sites'):
            query_notify.set_sites(sites)

        results = await maigret.search(
            username=username,
            site_dict=sites,
            timeout=int(options.get('timeout', 30)),
            logger=logger,
            id_type='username',
            query_notify=query_notify,
            no_progressbar=bool(query_notify),
            cookies=await resolve_cookie_source(options, logger),
            is_parsing_enabled=(not options.get('disable_extracting', False)),
            recursive_search_enabled=(
                not options.get('disable_recursive_search', False)
            ),
            check_domains=options.get('with_domains', False),
            proxy=options.get('proxy', None),
            tor_proxy=options.get('tor_proxy', None),
            i2p_proxy=options.get('i2p_proxy', None),
            cloudflare_bypass=cf_bypass_config,
        )
        return results
    except Exception as e:
        logger.error(f"Error during search: {str(e)}")
        raise


async def search_multiple_usernames(usernames, options):
    results = []
    errors = []
    for username in usernames:
        try:
            search_results = await maigret_search(username.strip(), options)
            results.append((username.strip(), 'username', search_results))
        except Exception as e:
            logging.error(f"Error searching username {username}: {str(e)}")
            errors.append(f"{username}: {e}")

    # 全部用户名都失败时把错误抛上去。否则 build_reports 收到空列表，用户看到的是
    # 一个"已完成但 0 命中"的结果页 —— 和"这人确实没有账号"长得一模一样，
    # 而这正是最不该混淆的两件事。部分失败则保留成功的那几个，继续出报告。
    if not results and errors:
        raise RuntimeError('；'.join(errors[:3]))
    return results


def sanitize_username_for_path(username: str) -> str:
    """Remove path separators and dangerous components from username for safe file path usage."""
    # Replace path separators and null bytes
    sanitized = username.replace('/', '_').replace('\\', '_').replace('\0', '_')
    # Remove . and .. components
    sanitized = sanitized.strip('.')
    # If empty after sanitization, use a fallback
    return sanitized or '_'


def _cn_clusters(results) -> list:
    """跨站身份归并，整理成模板能直接渲染的结构。

    失败一律返回空列表：聚类是锦上添花，一次 numpy/Pillow 缺失不该让整个
    结果页 500。与证据抽取、头像抓取的处理方式保持一致。
    """
    try:
        from maigret.cn.identity import cluster, describe
        from maigret.cn.report import build_accounts

        return describe(cluster(build_accounts(results)))
    except Exception as exc:  # noqa: BLE001 — 降级为空，不打断报告
        logging.warning(f"身份归并失败，本次不展示身份簇：{exc}")
        return []


def build_reports(general_results, usernames, session_key):
    """Write per-username CSV/JSON/PDF/HTML reports + combined graph to disk.

    Shared by the background /search job and the live SSE /api/scan job, so
    both flows land on the same results.html (report buttons + profile list).
    """
    os.makedirs(app.config["REPORTS_FOLDER"], exist_ok=True)
    session_folder = os.path.join(
        app.config["REPORTS_FOLDER"], f"search_{session_key}"
    )
    os.makedirs(session_folder, exist_ok=True)

    graph_path = os.path.join(session_folder, "combined_graph.html")
    maigret.report.save_graph_report(
        graph_path,
        general_results,
        MaigretDatabase().load_from_path(app.config["MAIGRET_DB_FILE"]),
    )

    individual_reports = []
    found_count = 0
    # AI 摘要吃的是**整次检索**的上下文，不是最后一个用户名的画像：
    # 多用户名时只喂最后一个人，摘要会漏掉其他用户名。
    # 一条结果都没有时留空串，`/api/ai` 会据此回"没有可分析的结果"。
    ai_input = ''
    if general_results:
        ai_input = maigret.report.generate_markdown_report(
            generate_report_context(general_results)
        )

    for username, id_type, results in general_results:
        safe_username = sanitize_username_for_path(username)
        report_base = os.path.join(session_folder, f"report_{safe_username}")

        csv_path = f"{report_base}.csv"
        json_path = f"{report_base}.json"
        pdf_path = f"{report_base}.pdf"
        html_path = f"{report_base}.html"

        # 本地补丁：原来传的是全部结果，多用户名/变体搜索时每份报告都混进了
        # 别人的命中，画像和摘要会串。只传当前用户名自己的结果。
        context = generate_report_context([(username, id_type, results)])

        maigret.report.save_csv_report(csv_path, username, results)
        maigret.report.save_json_report(
            json_path, username, results, report_type='ndjson'
        )
        maigret.report.save_pdf_report(pdf_path, context)
        maigret.report.save_html_report(html_path, context)

        claimed_profiles = []
        for site_name, site_data in results.items():
            if (
                site_data.get('status')
                and site_data['status'].status == MaigretCheckStatus.CLAIMED
            ):
                claimed_profiles.append(
                    {
                        'site_name': site_name,
                        'url': site_data.get('url_user', ''),
                        'tags': (
                            site_data.get('status').tags
                            if site_data.get('status')
                            else []
                        ),
                        'fields': account_fields(site_data.get('status')),
                    }
                )

        found_count += len(claimed_profiles)
        individual_reports.append(
            {
                'username': username,
                'csv_file': f"search_{session_key}/report_{safe_username}.csv",
                'json_file': f"search_{session_key}/report_{safe_username}.json",
                'pdf_file': f"search_{session_key}/report_{safe_username}.pdf",
                'html_file': f"search_{session_key}/report_{safe_username}.html",
                'claimed_profiles': claimed_profiles,
                'profile': build_profile(username, results, brief=context.get('brief', '')),
                'clusters': _cn_clusters(results),
            }
        )

    return {
        'status': 'completed',
        'session_folder': f"search_{session_key}",
        'graph_file': f"search_{session_key}/combined_graph.html",
        'ai_input': ai_input,
        'usernames': usernames,
        'individual_reports': individual_reports,
        'found_count': found_count,
    }


def process_search_task(usernames, options, timestamp):
    started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        general_results = loop.run_until_complete(
            search_multiple_usernames(usernames, options)
        )
        job_results[timestamp] = build_reports(general_results, usernames, timestamp)
        _anchor = background_jobs.get(timestamp, {}).get('anchor')
        if _anchor:
            job_results[timestamp]['anchor'] = _anchor

    except Exception as e:
        logging.error(f"Error in search task for timestamp {timestamp}: {str(e)}")
        job_results[timestamp] = {
            'status': 'failed',
            'error': str(e),
            'usernames': usernames,
        }
    finally:
        job_results[timestamp]['started_at'] = started_at
        background_jobs[timestamp]['completed'] = True


def parse_usernames(form):
    usernames_input = form.get('usernames', '').strip()
    return [u.strip() for u in usernames_input.replace(',', ' ').split() if u.strip()]


def parse_search_options(form):
    settings = load_settings()
    return {
        'top_sites': settings['top_sites'],
        'timeout': settings['timeout'],
        'use_cookies': bool(settings.get('use_cookies')),
        'all_sites': form.get('mode') == 'full',
        'disable_recursive_search': settings['disable_recursive_search'],
        'disable_extracting': settings['disable_extracting'],
        'with_domains': settings['with_domains'],
        'proxy': settings['proxy'] or None,
        'tor_proxy': settings['tor_proxy'] or None,
        'i2p_proxy': settings['i2p_proxy'] or None,
        'permute': ('permute' in form) if 'permute_touched' in form else settings['permute'],
        'parse_url': (form.get('parse_url') or '').strip(),
        'tags': settings['tags'],
        'excluded_tags': settings['excluded_tags'],
        'site_list': settings['site_list'],
    }


async def _stream_search(job, usernames, options):
    q = job['queue']
    general_results = []

    # 从主页 URL 反解出「站点 + 用户名」，记为本次检索的核心锚点
    parse_url = options.get('parse_url')
    if parse_url:
        try:
            _anchor_site, _anchor_user = resolve_anchor(parse_url)
        except Exception:
            _anchor_site, _anchor_user = None, None
        if _anchor_site:
            job['anchor'] = {'site': _anchor_site, 'username': _anchor_user}
            q.put({'type': 'anchor', 'site': _anchor_site, 'username': _anchor_user})

    if parse_url:
        try:
            extracted = extract_ids_from_page(
                parse_url, logger, timeout=int(options.get('timeout', 10))
            )
        except Exception as exc:
            extracted = {}
            q.put({'type': 'error', 'message': f'URL 解析失败：{exc}'})
        if extracted:
            q.put({'type': 'parsed', 'items': [f'{v}（{t}）' for v, t in extracted.items()]})
            if not job.get('anchor'):
                _s, _u = resolve_anchor(parse_url, None, extracted)
                if _s:
                    job['anchor'] = {'site': _s, 'username': _u}
                    q.put({'type': 'anchor', 'site': _s, 'username': _u})
            for value in extracted:
                if value and value not in usernames:
                    usernames.append(value)

    if options.get('permute') and len(usernames) > 1:
        try:
            variants = Permute({u: 'username' for u in usernames}).gather(method='strict')
        except Exception:
            variants = {}
        added = [v for v in variants if v not in usernames][:40]
        if added:
            q.put({'type': 'variants', 'items': added})
            usernames.extend(added)
    for username in usernames:
        if job['cancelled']:
            break
        notify = StreamNotify(q, username.strip())
        task = asyncio.ensure_future(
            maigret_search(username.strip(), options, query_notify=notify)
        )
        job['task'] = task
        try:
            results = await task
            general_results.append((username.strip(), 'username', results))
        except asyncio.CancelledError:
            # The task never got to return its own results dict, but every
            # site checked before cancellation already streamed a 'found' /
            # 'progress' event and was captured by the notifier — report on
            # that instead of throwing it away.
            if notify.results:
                general_results.append((username.strip(), 'username', notify.results))
            q.put({'type': 'stopped', 'username': username.strip()})
            break
        except Exception as e:
            if notify.results:
                general_results.append((username.strip(), 'username', notify.results))
            q.put({'type': 'error', 'message': str(e), 'username': username.strip()})
    return general_results


def run_stream_job(job_id, usernames, options):
    started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    job = live_jobs[job_id]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    job['loop'] = loop
    general_results = []
    try:
        general_results = loop.run_until_complete(
            _stream_search(job, usernames, options)
        )
    except Exception as e:
        job['queue'].put({'type': 'error', 'message': str(e)})
    finally:
        loop.close()

    # Same report files + results page as the classic /search flow, so the
    # live graph is a progress view, not a replacement for the report.
    done_event = {'type': 'done'}
    if general_results:
        try:
            job_results[job_id] = build_reports(general_results, usernames, job_id)
            _anchor = job.get('anchor')
            if _anchor:
                job_results[job_id]['anchor'] = _anchor
            job_results[job_id]['started_at'] = started_at
            done_event['redirect'] = f"/results/search_{job_id}"
        except Exception as e:
            logging.error(f"Error building reports for live scan {job_id}: {str(e)}")
    job['queue'].put(done_event)


def start_live_job(usernames, options):
    job_id = uuid.uuid4().hex
    live_jobs[job_id] = {
        'queue': queue.Queue(),
        'cancelled': False,
        'loop': None,
        'task': None,
    }
    Thread(target=run_stream_job, args=(job_id, usernames, options)).start()
    return job_id


@app.route('/api/scan', methods=['POST'])
def scan_start():
    usernames = parse_usernames(request.form)
    if not usernames:
        return {'error': 'At least one username is required'}, 400

    options = parse_search_options(request.form)
    job_id = start_live_job(usernames, options)
    return {'job_id': job_id}


@app.route('/api/ai/<job_id>', methods=['POST'])
def ai_analysis(job_id):
    result = job_results.get(job_id)
    if not result or not result.get('ai_input'):
        return {'error': '这次检索没有可分析的结果'}, 404

    cfg = load_ai_config()
    if not cfg.get('api_key'):
        return {
            'error': '未配置 API Key：在 ~/.maigret/settings.json 写入 openai_api_key 后重启服务'
        }, 400

    try:
        text = asyncio.run(summarize(result['ai_input'], cfg))
    except Exception as exc:
        logging.error(f'AI analysis failed for {job_id}: {exc}')
        return {'error': f'调用失败：{exc}'}, 502

    return {'ok': True, 'text': text, 'model': cfg.get('model'), 'source': cfg.get('source')}

@app.route('/api/scan/<job_id>/stream')
def scan_stream(job_id):
    job = live_jobs.get(job_id)
    if not job:
        # 重连兜底：任务已结束、报告已生成的话，直接让前端去结果页，
        # 而不是丢一个 404 让 EventSource 放弃重试。
        if job_id in job_results:
            payload = {'type': 'done', 'redirect': f'/results/search_{job_id}'}
            return Response(
                f"data: {json.dumps(payload)}\n\n", mimetype='text/event-stream'
            )
        return "Unknown job", 404

    def gen():
        # 不在这里 pop job：浏览器切标签页、休眠、刷新都会断开这条流，
        # 一旦 pop 掉，重连就 404，页面会永久停在"已检查 N/M"。
        while True:
            try:
                event = job['queue'].get(timeout=20)
            except queue.Empty:
                yield ": keepalive\n\n"  # 心跳，避免代理/浏览器掐掉空闲连接
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event.get('type') == 'done':
                break

    return Response(gen(), mimetype='text/event-stream')


@app.route('/api/scan/<job_id>/stop', methods=['POST'])
def scan_stop(job_id):
    job = live_jobs.get(job_id)
    if not job:
        return {'error': 'unknown job'}, 404

    job['cancelled'] = True
    loop = job.get('loop')
    task = job.get('task')
    if loop and task:
        loop.call_soon_threadsafe(task.cancel)
    return {'ok': True}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/anchor/<job_id>', methods=['POST'])
def anchor_score(job_id):
    result = job_results.get(job_id)
    if not result:
        return {'error': '这次检索没有可用的结果'}, 404

    payload = request.get_json(silent=True) or {}
    site = payload.get('site') or request.form.get('site')
    user = payload.get('username') or request.form.get('username')
    if not site:
        return {'error': '没有指定核心账号'}, 400

    accounts = []
    for report in result.get('individual_reports') or []:
        for acc in report.get('claimed_profiles') or []:
            item = dict(acc)
            item['username'] = report.get('username')
            accounts.append(item)
    if not accounts:
        return {'error': '这次检索没有命中的账号'}, 400

    data = score_accounts(site, accounts, user)
    if data.get('error'):
        return data, 400
    return {'ok': True, **data}

@app.route('/api/sites')
def api_sites():
    """Site names/URLs for the Filters site-picker datalist, fetched lazily
    from the Settings modal instead of loading the DB on every page render."""
    db = MaigretDatabase().load_from_path(app.config["MAIGRET_DB_FILE"])
    site_options = []
    for site in db.sites:
        site_options.append(site.name)
        if site.url_main and site.url_main not in site_options:
            site_options.append(site.url_main)
    return {'sites': sorted(set(site_options))}


@app.route('/api/browser-cookies')
def api_browser_cookies():
    """探测本机浏览器里的登录态，供设置页显示"能不能自动读"。

    只返回域名和条数，**永远不返回值**：这个接口的输出会进页面，
    Cookie 值进页面就等于进了浏览器历史、缓存、截图。
    """
    from maigret.cn import browser_cookies

    try:
        info = asyncio.run(browser_cookies.browser_info())
    except browser_cookies.BrowserCookieError as exc:
        return {'available': False, 'reason': str(exc)}, 200

    if not info:
        return {
            'available': False,
            'reason': '没检测到调试端口。用 --remote-debugging-port=9222 启动 Chrome 后重试',
            'port': browser_cookies.cdp_port(),
        }, 200

    try:
        raw = asyncio.run(browser_cookies.collect())
    except Exception as exc:
        return {'available': False, 'browser': info.get('Browser'), 'reason': str(exc)}, 200

    return {
        'available': True,
        'browser': info.get('Browser'),
        'total': len(raw),
        'domains': browser_cookies.summarize(raw, limit=20),
    }, 200


@app.route('/settings', methods=['POST'])
def settings_update():
    save_settings(parse_settings_form(request.form))
    _domain = (request.form.get('cookie_domain') or '').strip()
    _text = (request.form.get('cookie_text') or '').strip()
    if _text:
        try:
            if looks_netscape(_text):
                _n, _d = import_netscape(_text)
                flash(f'已导入 {_d} 个域名的 {_n} 条 Cookie。', 'success')
            elif _domain:
                _n = save_cookies(_domain, _text)
                flash(f'已保存 {_domain} 的 {_n} 个 Cookie。', 'success')
            else:
                flash('粘贴的是 Cookie 头，还需要填写域名。', 'danger')
        except Exception as _exc:
            flash(f'Cookie 保存失败：{_exc}', 'danger')
    flash('设置已保存。', 'success')
    return redirect(request.referrer or url_for('index'))


@app.route('/history')
def history():
    entries = sorted(
        job_results.values(), key=lambda r: r.get('started_at', ''), reverse=True
    )
    return render_template('history.html', entries=entries)


@app.route('/live', methods=['POST'])
def live_start():
    usernames = parse_usernames(request.form)
    if not usernames:
        flash('至少需要输入一个用户名', 'danger')
        return redirect(url_for('index'))

    options = parse_search_options(request.form)
    job_id = start_live_job(usernames, options)
    return redirect(url_for('live_results', job_id=job_id))


@app.route('/live/<job_id>')
def live_results(job_id):
    result = job_results.get(job_id)
    if job_id not in live_jobs and not result:
        flash('扫描会话不存在或已过期。', 'danger')
        return redirect(url_for('index'))

    done_redirect = None
    if result and result.get('status') == 'completed':
        done_redirect = url_for('results', session_id=result['session_folder'])

    return render_template('live.html', job_id=job_id, done_redirect=done_redirect)


# Modified search route
@app.route('/search', methods=['POST'])
def search():
    usernames = parse_usernames(request.form)
    if not usernames:
        flash('至少需要输入一个用户名', 'danger')
        return redirect(url_for('index'))

    # Create timestamp for this search session
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    options = parse_search_options(request.form)
    logging.info(
        f"Starting search for usernames: {usernames} with tags: {options['tags']}, "
        f"excluded: {options['excluded_tags']}"
    )

    # Start background job
    background_jobs[timestamp] = {
        'completed': False,
        'thread': Thread(
            target=process_search_task, args=(usernames, options, timestamp)
        ),
    }
    background_jobs[timestamp]['thread'].start()  # type: ignore[union-attr]

    return redirect(url_for('status', timestamp=timestamp))


@app.route('/status/<timestamp>')
def status(timestamp):
    logging.info(f"Status check for timestamp: {timestamp}")

    # Validate timestamp
    if timestamp not in background_jobs:
        flash('无效的检索会话。', 'danger')
        logging.error(f"Invalid search session: {timestamp}")
        return redirect(url_for('index'))

    # Check if job is completed
    if background_jobs[timestamp]['completed']:
        result = job_results.get(timestamp)
        if not result:
            flash('该检索会话没有结果。', 'warning')
            logging.error(f"No results found for completed session: {timestamp}")
            return redirect(url_for('index'))

        if result['status'] == 'completed':
            # Note: use the session_folder from the results to redirect
            return redirect(url_for('results', session_id=result['session_folder']))
        else:
            error_msg = result.get('error', '未知错误。')
            flash(f'检索失败：{error_msg}', 'danger')
            logging.error(f"Search failed for session {timestamp}: {error_msg}")
            return redirect(url_for('index'))

    # If job is still running, show a status page
    return render_template('status.html', timestamp=timestamp)


@app.route('/results/<session_id>')
def results(session_id):
    # Find completed results that match this session_folder
    result_data = next(
        (
            r
            for r in job_results.values()
            if r.get('status') == 'completed' and r['session_folder'] == session_id
        ),
        None,
    )

    if not result_data:
        flash('该会话没有结果。', 'danger')
        logging.error(f"Results for session {session_id} not found in job_results.")
        return redirect(url_for('index'))

    return render_template(
        'results.html',
        anchor=result_data.get('anchor'),
        usernames=result_data['usernames'],
        graph_file=result_data['graph_file'],
        individual_reports=result_data['individual_reports'],
        timestamp=session_id.replace('search_', ''),
    )


@app.route('/reports/<path:filename>')
def download_report(filename):
    reports_root = app.config["REPORTS_FOLDER"]
    os.makedirs(reports_root, exist_ok=True)
    try:
        return send_from_directory(reports_root, filename)
    except NotFound:
        return "File not found", 404
    except Exception as e:
        logging.error(f"Error serving file {filename}: {str(e)}")
        return "File not found", 404


@app.route('/api/cn/<job_id>', methods=['POST'])
def cn_probe(job_id):
    result = job_results.get(job_id)
    if not result:
        return {'error': '这次检索没有可用的结果'}, 404

    usernames = result.get('usernames') or []
    try:
        items, notes = asyncio.run(probe_platforms(usernames))
    except Exception as exc:
        logging.error(f'CN probe failed for {job_id}: {exc}')
        return {'error': f'补检失败：{exc}'}, 502

    return {'ok': True, 'items': items, 'notes': notes}

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']

    # Host configuration: secure by default
    # Use 127.0.0.1 for local development, 0.0.0.0 only if explicitly set
    host = os.getenv('FLASK_HOST', '127.0.0.1')
    port = int(os.getenv('FLASK_PORT', '5000'))

    app.run(host=host, port=port, debug=debug_mode)
