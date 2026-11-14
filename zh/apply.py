"""Maigret Web 界面汉化补丁。

用法：
    python apply.py            应用汉化（首次会备份英文原文到 orig/）
    python apply.py --check    只检查，报告哪些文案没匹配上（升级后用这个看有没有失效）
    python apply.py --restore  还原英文原文

只改可见文案，不动 Jinja 变量、表单 name/id、JS 变量名、tag 的 value 值。
pip install -U maigret 之后重新跑一次即可（会覆盖新版本的同名模板，先 --check 看看）。
"""
import importlib.util
import re
import shutil
import sys
from pathlib import Path


def _locate_site_packages() -> Path:
    """找到已安装的 maigret 包目录。

    先把仓库根目录加进 sys.path 让本地源码优先；找不到安装版本时，
    退回到可编辑安装（pip install -e .）指向的仓库自身。
    """
    repo = Path(__file__).resolve().parent.parent
    for candidate in (repo,):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    try:
        spec = importlib.util.find_spec("maigret")
        if spec and spec.origin:
            return Path(spec.origin).resolve().parent
    except (ImportError, ValueError):
        pass
    return repo / "maigret"


SP = _locate_site_packages()
TPL = SP / "web" / "templates"
ORIG = Path(__file__).resolve().parent / "orig"

# 1x1 透明 PNG。报告里的占位头像原本指向 imgur，国内不可达会造成长时间超时。
BLANK_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

# Web 端 AI 摘要路由（配置读 ~/.maigret/settings.json，兼容 OpenAI 协议）
AI_ROUTE = '''@app.route('/api/ai/<job_id>', methods=['POST'])
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

    return {'ok': True, 'text': text, 'model': cfg.get('model'), 'source': cfg.get('source')}'''

CN_ROUTE = '''@app.route('/api/cn/<job_id>', methods=['POST'])
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

    return {'ok': True, 'items': items, 'notes': notes}'''

ANCHOR_ROUTE = '''@app.route('/api/anchor/<job_id>', methods=['POST'])
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
    return {'ok': True, **data}'''

COOKIE_UI = '''                        <h6>登录 Cookie（可选）</h6>
                        <div class="mb-3 form-check">
                            <input type="checkbox" class="form-check-input" id="settings-use_cookies"
                                name="use_cookies" {{ 'checked' if web_settings.use_cookies }}>
                            <label class="form-check-label" for="settings-use_cookies">检索时携带 Cookie（微博、贴吧等站点需要）</label>
                        </div>
                        <div class="mb-3">
                            <label for="settings-cookie-domain" class="form-label">域名</label>
                            <input type="text" class="form-control" id="settings-cookie-domain"
                                name="cookie_domain" placeholder="weibo.com">
                        </div>
                        <div class="mb-3">
                            <label for="settings-cookie-text" class="form-label">Cookie 内容</label>
                            <textarea class="form-control" id="settings-cookie-text" name="cookie_text" rows="4"
                                placeholder="两种都行：① 浏览器 F12 → 网络 → 请求头里 Cookie: 后面的内容（需填域名）；② 扩展导出的 cookies.txt 全文（制表符分隔，可含多个域名，直接粘贴）"></textarea>
                            <div class="form-text">
                                {% if cookie_domains %}已保存：{% for d, n in cookie_domains.items() %}{{ d }}（{{ n }} 条）{% if not loop.last %}、{% endif %}{% endfor %}{% else %}还没有保存任何 Cookie{% endif %}
                            </div>
                        </div>

'''

# (文件, 原文, 译文)。原文必须能在文件里唯一匹配到。
PATCHES: list[tuple[str, str, str]] = [
    # ---------- base.html ----------
    ("base.html", '<html lang="en" data-bs-theme="dark">', '<html lang="zh-CN" data-bs-theme="dark">'),
    ("base.html", "<title>Maigret Web Interface</title>", "<title>用户名暴露自检</title>"),
    ("base.html", ">Fork me on GitHub</a>", ">GitHub 项目</a>"),
    ("base.html",
     '<h1 class="h4 mb-0">Maigret Web Interface</h1>',
     '<div>\n'
     '                        <h1 class="h4 mb-0">用户名暴露自检</h1>\n'
     '                        <div class="small text-muted">查清一个用户名在各平台留下了什么</div>\n'
     '                    </div>'),
    ("base.html", 'class="btn btn-outline-secondary">New Search</a>', 'class="btn btn-outline-secondary">新建检索</a>'),
    ("base.html", 'class="btn btn-outline-secondary">History</a>', 'class="btn btn-outline-secondary">历史记录</a>'),
    ("base.html", "Toggle Dark/Light Mode", "明暗切换"),
    ("base.html", 'title="Settings"', 'title="设置"'),
    ("base.html", 'id="settingsModalLabel">Settings</h5>', 'id="settingsModalLabel">设置</h5>'),
    ("base.html", 'aria-label="Close"', 'aria-label="关闭"'),
    ("base.html", ">Timeout (seconds)</label>", ">超时（秒）</label>"),
    ("base.html", ">Number of Sites (Fast check)</label>", ">站点数量（快速检索）</label>"),
    ("base.html", "<h6>Advanced Options</h6>", "<h6>高级选项</h6>"),
    ("base.html", ">Enable Username Permutations</label>", ">生成用户名变体</label>"),
    ("base.html", ">Disable Recursive Search</label>", ">关闭递归检索</label>"),
    ("base.html", ">Disable Information Extraction</label>", ">关闭信息提取</label>"),
    ("base.html", ">Check Domains</label>", ">检查域名</label>"),
    ("base.html", "<h6>Proxy URLs</h6>", "<h6>代理</h6>"),
    ("base.html", ">Proxy URL</label>", ">代理地址</label>"),
    ("base.html", 'placeholder="e.g., 127.0.0.1:1080"', 'placeholder="例如 127.0.0.1:1080"'),
    ("base.html", ">TOR Proxy URL</label>", ">Tor 代理地址</label>"),
    ("base.html", 'placeholder="Default: 127.0.0.1:9050"', 'placeholder="默认 127.0.0.1:9050"'),
    ("base.html", ">I2P Proxy URL</label>", ">I2P 代理地址</label>"),
    ("base.html", 'placeholder="Default: 127.0.0.1:4444"', 'placeholder="默认 127.0.0.1:4444"'),
    ("base.html", "<h6>Filters</h6>", "<h6>筛选</h6>"),
    ("base.html", ">Specify Sites (Optional)</label>", ">指定站点（可选）</label>"),
    ("base.html", 'placeholder="Type to search for sites..."', 'placeholder="输入站点名搜索…"'),
    (
        "base.html",
        ">Tags (click to cycle: include &rarr; exclude &rarr; neutral)</label>",
        ">标签（点击循环：包含 &rarr; 排除 &rarr; 中立）</label>",
    ),
    ("base.html", "</span> Included (whitelist)", "</span> 包含（白名单）"),
    ("base.html", "</span> Excluded (blacklist)", "</span> 排除（黑名单）"),
    ("base.html", "</span> Neutral", "</span> 中立"),
    ("base.html", 'data-bs-dismiss="modal">Cancel</button>', 'data-bs-dismiss="modal">取消</button>'),
    ("base.html", ">Save Settings</button>", ">保存设置</button>"),
    ("base.html", "Powered by <a", "由 <a"),
    ("base.html", "Licensed under <a", "遵循 <a"),
    ("base.html", "separator.textContent = 'Countries';", "separator.textContent = '国家/地区';"),
    # 布局修正：右上角 "Fork me" 角标会压住设置按钮，原断点 991px 太窄
    ("base.html", "@media (max-width: 991px) {", "@media (max-width: 1299px) {"),
    # ---------- index.html ----------
    ("index.html", ">Usernames to Search</label>", ">要检索的用户名</label>"),
    (
        "index.html",
        'placeholder="Enter one or more usernames (separated by spaces or commas)..."',
        'placeholder="输入一个或多个用户名，用空格或逗号分隔…"',
    ),
    ("index.html", 'aria-label="Search mode"', 'aria-label="检索模式"'),
    ("index.html", ">Fast check ({{ web_settings.top_sites }} sites)</label>", ">快速检索（{{ web_settings.top_sites }} 个站点）</label>"),
    ("index.html", ">Full check</label>", ">全量检索</label>"),
    (
        "index.html",
        """        <p class="text-muted">
            Filters, proxies and advanced options now live in
            <a href="#" data-bs-toggle="modal" data-bs-target="#settingsModal">Settings</a> (&#9881; top right).
        </p>""",
        """        <p class="text-muted">
            筛选、代理和高级选项都在
            <a href="#" data-bs-toggle="modal" data-bs-target="#settingsModal">设置</a>里（右上角 &#9881;）。
        </p>""",
    ),
    (
        "index.html",
        """            Start Search
        </button>""",
        """            开始检索
        </button>""",
    ),
    # ---------- live.html ----------
    ("live.html", '<h1 class="mb-0">Live Results</h1>', '<h1 class="mb-0">实时结果</h1>'),
    ("live.html", 'style="display:none;">Analyze</a>', 'style="display:none;">查看报告</a>'),
    ("live.html", 'class="btn btn-danger">Stop</button>', 'class="btn btn-danger">停止</button>'),
    ("live.html", 'class="text-muted">Starting…</span>', 'class="text-muted">启动中…</span>'),
    ("live.html", "<span>Checked: <strong", "<span>已检查：<strong"),
    ("live.html", "<span>Found: <strong", "<span>命中：<strong"),
    ("live.html", "Double-click a site node to open the profile.", "双击站点节点可打开对应主页。"),
    ("live.html", "statusEl.textContent = 'Scanning ' + ev.username + '…';", "statusEl.textContent = '正在检索 ' + ev.username + '…';"),
    ("live.html", "textContent = 'Stopping…';", "textContent = '正在停止…';"),
    ("live.html", "statusEl.textContent = 'Stopped.';", "statusEl.textContent = '已停止。';"),
    ("live.html", "statusEl.textContent = 'Error: ' + ev.message;", "statusEl.textContent = '错误：' + ev.message;"),
    ("live.html", "'Completed — ' + foundCount + ' accounts found.'", "'完成，共找到 ' + foundCount + ' 个账号。'"),
    ("live.html", "'Completed — nothing to analyze.'", "'完成，没有可分析的结果。'"),
    # ---------- results.html ----------
    ("results.html", '<h1 class="mb-4">Search Results</h1>', '<h1 class="mb-4">检索结果</h1>'),
    (
        "results.html",
        """<p>The search has completed. <a href="{{ url_for('index')}}">Back to start.</a></p>""",
        """<p>检索已完成。<a href="{{ url_for('index')}}">返回首页</a></p>""",
    ),
    ("results.html", "<h3>Combined Graph</h3>", "<h3>合并关系图</h3>"),
    ("results.html", "<h3>Individual Reports</h3>", "<h3>单独报告</h3>"),
    ("results.html", 'rel="noopener">CSV Report</a>', 'rel="noopener">CSV 报告</a>'),
    ("results.html", 'rel="noopener">JSON Report</a>', 'rel="noopener">JSON 报告</a>'),
    ("results.html", 'rel="noopener">PDF Report</a>', 'rel="noopener">PDF 报告</a>'),
    ("results.html", 'rel="noopener">HTML Report</a>', 'rel="noopener">HTML 报告</a>'),
    ("results.html", "<strong>Claimed Profiles:</strong>", "<strong>已注册账号：</strong>"),
    ("results.html", "<p>No claimed profiles found.</p>", "<p>没有找到已注册账号。</p>"),
    ("results.html", "<p>No individual reports available.</p>", "<p>没有可用的单独报告。</p>"),
    # ---------- history.html ----------
    ("history.html", '<h1 class="mb-4">Search History</h1>', '<h1 class="mb-4">检索历史</h1>'),
    ("history.html", "<p>No searches have been run yet.</p>", "<p>还没有执行过检索。</p>"),
    ("history.html", "<th>Started</th>", "<th>开始时间</th>"),
    ("history.html", "<th>Usernames</th>", "<th>用户名</th>"),
    ("history.html", "<th>Found</th>", "<th>命中</th>"),
    ("history.html", "<th>Status</th>", "<th>状态</th>"),
    ("history.html", '<span class="badge bg-success">completed</span>', '<span class="badge bg-success">已完成</span>'),
    ("history.html", '<span class="badge bg-danger" title="{{ result.error }}">failed</span>',
     '<span class="badge bg-danger" title="{{ result.error }}">失败</span>'),
    ("history.html", 'btn-outline-warning">View</a>', 'btn-outline-warning">查看</a>'),
    # ---------- status.html ----------
    ("status.html", "<h2>Search in progress...</h2>", "<h2>检索进行中…</h2>"),
    (
        "status.html",
        "<p>Your request is being processed in the background. This page will automatically redirect once the results are ready.</p>",
        "<p>请求正在后台处理，结果就绪后本页会自动跳转。</p>",
    ),
    ("status.html", '<span class="visually-hidden">Loading...</span>', '<span class="visually-hidden">加载中…</span>'),
    # ---------- app.py 提示语 ----------
    ("app.py", "flash('Settings saved.', 'success')", "flash('设置已保存。', 'success')"),
    ("app.py", "flash('At least one username is required', 'danger')", "flash('至少需要输入一个用户名', 'danger')"),
    ("app.py", "flash('Unknown or expired scan session.', 'danger')", "flash('扫描会话不存在或已过期。', 'danger')"),
    ("app.py", "flash('Invalid search session.', 'danger')", "flash('无效的检索会话。', 'danger')"),
    ("app.py", "flash('No results found for this search session.', 'warning')", "flash('该检索会话没有结果。', 'warning')"),
    ("app.py", "result.get('error', 'Unknown error occurred.')", "result.get('error', '未知错误。')"),
    ("app.py", "f'Search failed: {error_msg}'", "f'检索失败：{error_msg}'"),
    ("app.py", "flash('No results found for this session ID.', 'danger')", "flash('该会话没有结果。', 'danger')"),
    # ---- 结果页画像 / AI 摘要：导入 ----
    ("app.py", "from maigret.report import generate_report_context",
     "from maigret.report import generate_report_context\n"
     "from maigret.web.profile_card import account_fields, build_profile\n"
     "from maigret.web.ai_summary import load_ai_config, summarize"),
    ("app.py", "'claimed_profiles': claimed_profiles,\n            }\n        )",
     "'claimed_profiles': claimed_profiles,\n"
     "                'profile': build_profile(username, results, brief=context.get('brief', '')),\n"
     "            }\n        )"),
    ("app.py",
     "                        'tags': (\n"
     "                            site_data.get('status').tags\n"
     "                            if site_data.get('status')\n"
     "                            else []\n"
     "                        ),\n"
     "                    }\n"
     "                )",
     "                        'tags': (\n"
     "                            site_data.get('status').tags\n"
     "                            if site_data.get('status')\n"
     "                            else []\n"
     "                        ),\n"
     "                        'fields': account_fields(site_data.get('status')),\n"
     "                    }\n"
     "                )"),
    # ---- AI 摘要：路由 + Markdown 输入 ----
    ("app.py", "        context = generate_report_context(general_results)",
     "        # 本地补丁：原来传的是全部结果，多用户名/变体搜索时每份报告都混进了\n"
     "        # 别人的命中，画像和摘要会串。只传当前用户名自己的结果。\n"
     "        context = generate_report_context([(username, id_type, results)])"),
    ("app.py",
     "'graph_file': f\"search_{session_key}/combined_graph.html\",\n        'usernames': usernames,",
     "'graph_file': f\"search_{session_key}/combined_graph.html\",\n"
     "        'ai_input': maigret.report.generate_markdown_report(context),\n"
     "        'usernames': usernames,"),
    ("app.py", "@app.route('/api/scan/<job_id>/stream')",
     AI_ROUTE + "\n\n@app.route('/api/scan/<job_id>/stream')"),
    # ---- 登录 Cookie：落到 ~/.maigret/cookies.txt ----
    ("app.py", 'app.config["COOKIES_FILE"] = "cookies.txt"',
     '_MAIGRET_HOME = os.path.join(os.path.expanduser("~"), ".maigret")\n'
     'os.makedirs(_MAIGRET_HOME, exist_ok=True)\n'
     'app.config["COOKIES_FILE"] = os.path.join(_MAIGRET_HOME, "cookies.txt")'),
    ("app.py", "    'with_domains': False,\n}",
     "    'with_domains': False,\n    'use_cookies': False,\n}"),
    ("app.py", "'use_cookies': 'use_cookies' in form,", "'use_cookies': bool(settings.get('use_cookies')),"),
    ("app.py", "        'with_domains': 'with_domains' in form,\n    }",
     "        'with_domains': 'with_domains' in form,\n        'use_cookies': 'use_cookies' in form,\n    }"),
    ("app.py", "def settings_update():\n    save_settings(parse_settings_form(request.form))\n",
     "def settings_update():\n"
     "    save_settings(parse_settings_form(request.form))\n"
     "    _domain = (request.form.get('cookie_domain') or '').strip()\n"
     "    _text = (request.form.get('cookie_text') or '').strip()\n"
     "    if _text:\n"
     "        try:\n"
     "            if looks_netscape(_text):\n"
     "                _n, _d = import_netscape(_text)\n"
     "                flash(f'已导入 {_d} 个域名的 {_n} 条 Cookie。', 'success')\n"
     "            elif _domain:\n"
     "                _n = save_cookies(_domain, _text)\n"
     "                flash(f'已保存 {_domain} 的 {_n} 个 Cookie。', 'success')\n"
     "            else:\n"
     "                flash('粘贴的是 Cookie 头，还需要填写域名。', 'danger')\n"
     "        except Exception as _exc:\n"
     "            flash(f'Cookie 保存失败：{_exc}', 'danger')\n"),
    ("app.py", "from maigret.web.ai_summary import load_ai_config, summarize",
     "from maigret.web.ai_summary import load_ai_config, summarize\n"
     "from maigret.web.cookie_store import (\n"
     "    cookie_domains, import_netscape, looks_netscape, save_cookies,\n"
     ")\n"
     "from maigret.web.cn_probe import probe_platforms"),
    ("app.py", "@app.route('/api/sites')", ANCHOR_ROUTE + "\n\n@app.route('/api/sites')"),
    ("app.py", "from maigret.web.cn_probe import probe_platforms",
     "from maigret.web.cn_probe import probe_platforms\n"
     "from maigret.web.anchor import score_accounts\n"
     "from maigret.permutator import Permute\n"
     "from maigret.maigret import extract_ids_from_page\n"
     "from maigret.web.url_anchor import resolve as resolve_anchor"),
    # 变体搜索与 URL 起点
    ("app.py", "        'permute': settings['permute'],",
     "        'permute': ('permute' in form) if 'permute_touched' in form else settings['permute'],\n"
     "        'parse_url': (form.get('parse_url') or '').strip(),"),
    ("app.py", "async def _stream_search(job, usernames, options):\n    q = job['queue']\n    general_results = []",
     "async def _stream_search(job, usernames, options):\n"
     "    q = job['queue']\n"
     "    general_results = []\n"
     "\n"
     "    # 从主页 URL 反解出「站点 + 用户名」，记为本次检索的核心锚点\n"
     "    parse_url = options.get('parse_url')\n"
     "    if parse_url:\n"
     "        try:\n"
     "            _anchor_site, _anchor_user = resolve_anchor(parse_url)\n"
     "        except Exception:\n"
     "            _anchor_site, _anchor_user = None, None\n"
     "        if _anchor_site:\n"
     "            job['anchor'] = {'site': _anchor_site, 'username': _anchor_user}\n"
     "            q.put({'type': 'anchor', 'site': _anchor_site, 'username': _anchor_user})\n"
     "\n"
     "    if parse_url:\n"
     "        try:\n"
     "            extracted = extract_ids_from_page(\n"
     "                parse_url, logger, timeout=int(options.get('timeout', 10))\n"
     "            )\n"
     "        except Exception as exc:\n"
     "            extracted = {}\n"
     "            q.put({'type': 'error', 'message': f'URL 解析失败：{exc}'})\n"
     "        if extracted:\n"
     "            q.put({'type': 'parsed', 'items': [f'{v}（{t}）' for v, t in extracted.items()]})\n"
     "            if not job.get('anchor'):\n"
     "                _s, _u = resolve_anchor(parse_url, None, extracted)\n"
     "                if _s:\n"
     "                    job['anchor'] = {'site': _s, 'username': _u}\n"
     "                    q.put({'type': 'anchor', 'site': _s, 'username': _u})\n"
     "            for value in extracted:\n"
     "                if value and value not in usernames:\n"
     "                    usernames.append(value)\n"
     "\n"
     "    if options.get('permute') and len(usernames) > 1:\n"
     "        try:\n"
     "            variants = Permute({u: 'username' for u in usernames}).gather(method='strict')\n"
     "        except Exception:\n"
     "            variants = {}\n"
     "        added = [v for v in variants if v not in usernames][:40]\n"
     "        if added:\n"
     "            q.put({'type': 'variants', 'items': added})\n"
     "            usernames.extend(added)"),
    ("app.py", "    return {'web_settings': load_settings()}",
     "    return {'web_settings': load_settings(), 'cookie_domains': cookie_domains()}"),
    ("app.py", "if __name__ == '__main__':", CN_ROUTE + "\n\nif __name__ == '__main__':"),
    # ---- 设置面板：Cookie 区块 ----
    ("base.html", "<h6>代理</h6>", COOKIE_UI + "<h6>代理</h6>"),
    # ---- 核心锚点：主页 URL 反解出的账号自动作为锚点 ----
    ("app.py", "        job_results[timestamp] = build_reports(general_results, usernames, timestamp)",
     "        job_results[timestamp] = build_reports(general_results, usernames, timestamp)\n"
     "        _anchor = background_jobs.get(timestamp, {}).get('anchor')\n"
     "        if _anchor:\n"
     "            job_results[timestamp]['anchor'] = _anchor"),
    ("app.py", "    return render_template(\n        'results.html',\n        usernames=result_data['usernames'],",
     "    return render_template(\n"
     "        'results.html',\n"
     "        anchor=result_data.get('anchor'),\n"
     "        usernames=result_data['usernames'],"),
    # live 流程同样带上锚点
    ("app.py", "            job_results[job_id] = build_reports(general_results, usernames, job_id)",
     "            job_results[job_id] = build_reports(general_results, usernames, job_id)\n"
     "            _anchor = job.get('anchor')\n"
     "            if _anchor:\n"
     "                job_results[job_id]['anchor'] = _anchor"),
    # ---- 首页：URL 起点 + 变体开关 ----
    ("index.html", '                <label class="btn btn-outline-secondary" for="mode-full">全量检索</label>\n            </div>',
     '                <label class="btn btn-outline-secondary" for="mode-full">全量检索</label>\n'
     '            </div>\n'
     '\n'
     '            <div class="row g-2 mt-3">\n'
     '                <div class="col-md-7">\n'
     '                    <label for="parse_url" class="form-label">从主页 URL 开始（可选）</label>\n'
     '                    <input type="text" class="form-control" id="parse_url" name="parse_url"\n'
     '                        placeholder="https://github.com/soxoj —— 从页面里抽出其它 ID/用户名一起查">\n'
     '                </div>\n'
     '                <div class="col-md-5 d-flex align-items-end">\n'
     '                    <div class="form-check mb-2">\n'
     '                        <input class="form-check-input" type="checkbox" id="permute" name="permute"\n'
     "                            {{ 'checked' if web_settings.permute }}>\n"
     '                        <label class="form-check-label" for="permute">生成用户名变体（需两个以上名字，如 john doe）</label>\n'
     '                        <input type="hidden" name="permute_touched" value="1">\n'
     '                    </div>\n'
     '                </div>\n'
     '            </div>'),
    # ---- 实时页：显示变体与 URL 解析结果 ----
    ("live.html", '<div class="progress mb-3" style="height: 8px;">',
     '<div id="live-extra" class="small text-muted mb-2"></div>\n'
     '    <div class="progress mb-3" style="height: 8px;">'),
    ("live.html", "        } else if (ev.type === 'stopped') {",
     "        } else if (ev.type === 'anchor') {\n"
     "            window.__anchorHint = '核心账号：' + ev.site + (ev.username ? '（' + ev.username + '）' : '');\n"
     "            document.getElementById('live-extra').textContent = window.__anchorHint;\n"
     "        } else if (ev.type === 'variants') {\n"
     "            document.getElementById('live-extra').textContent =\n"
     "                '已生成变体 ' + ev.items.length + ' 个：' + ev.items.join('、');\n"
     "        } else if (ev.type === 'parsed') {\n"
     "            document.getElementById('live-extra').textContent =\n"
     "                (window.__anchorHint ? window.__anchorHint + ' · ' : '')\n"
     "                + '从 URL 解析到：' + ev.items.join('、');\n"
     "        } else if (ev.type === 'stopped') {"),
]

# 报告页与 Markdown 报告：文案是 Python 拼的，单独列
REPORT_TARGETS = ("simple_report.tpl", "simple_report_pdf.tpl")
for _f in REPORT_TARGETS:
    PATCHES += [
        (_f, "<title>{{ username }} -- Maigret username search report</title>",
         "<title>{{ username }} —— 用户名暴露自检报告</title>"),
        (_f, "Username search report for {{ username }}", "{{ username }} 的用户名检索报告"),
        (_f, '<a href="https://github.com/soxoj/maigret">Maigret</a> at {{ generated_at }}',
         '<a href="https://github.com/soxoj/maigret">Maigret</a> 生成于 {{ generated_at }}'),
        (_f, ">Generated by ", ">由 "),
        (_f, ">Supposed personal data</", ">推断的个人信息</"),
        (_f, ">Brief</", ">概要</"),
        (_f, "Geo: {% for k, v in countries_tuple_list %}", "地区：{% for k, v in countries_tuple_list %}"),
        (_f, "Interests: {% for k, v in interests_tuple_list %}", "兴趣：{% for k, v in interests_tuple_list %}"),
        (_f, "First seen: {{ first_seen }}", "首次出现：{{ first_seen }}"),
        (_f, "Tags: {{ v.status.tags | join(', ') }}", "标签：{{ v.status.tags | join(', ') }}"),
    ]

PATCHES += [
    ("simple_report.tpl", 'alt="Photo"', 'alt="头像"'),
    # 占位头像原指 imgur，国内不可达会让报告生成卡住（每个账号重试 3 次超时），换成内嵌空白图
    ("simple_report.tpl", "'https://i.imgur.com/040fmbw.png'", f"'{BLANK_PNG}'"),
    ("simple_report_pdf.tpl", "'https://i.imgur.com/040fmbw.png'", f"'{BLANK_PNG}'"),
    ("simple_report_pdf.tpl", ">Details</h4>", ">详细信息</h4>"),
    # ---- report.py：摘要句与 Markdown 标题 ----
    ("report.py", "f\"Search by {id_type} {username} returned {found_accounts} accounts.\"",
     "f\"按 {id_type} 检索 {username}，命中 {found_accounts} 个账号。\""),
    ("report.py", 'brief_text.append("Found target\'s other IDs: " + ", ".join(ids_list) + ".")',
     'brief_text.append("发现目标的其他 ID：" + ", ".join(ids_list) + "。")'),
    ("report.py", 'f"Extended info extracted from {extended_info_count} accounts."',
     'f"从 {extended_info_count} 个账号提取到扩展信息。"'),
    ("report.py", 'lines.append("## Summary\\n")', 'lines.append("## 概要\\n")'),
    ("report.py", '"**Information extracted from accounts:**\\n"', '"**从账号中提取的信息：**\\n"'),
    ("report.py", 'f"**Country tags:** {geo}\\n"', 'f"**国家标签：** {geo}\\n"'),
    ("report.py", 'f"**Website tags:** {tags}\\n"', 'f"**站点类别：** {tags}\\n"'),
    ("report.py", 'f"**First seen:** {first_seen}"', 'f"**首次出现：** {first_seen}"'),
    ("report.py", 'f"**Last seen:** {last_seen}"', 'f"**最后出现：** {last_seen}"'),
    ("report.py", 'lines.append("## Accounts found\\n")', 'lines.append("## 命中的账号\\n")'),
    ("report.py", "f\"{run_info['sites_count']} sites checked\"", "f\"检查 {run_info['sites_count']} 个站点\""),
    ("report.py", "lines.append(f\"# Report by searching on username \\\"{username}\\\"\\n\")",
     "lines.append(f\"# 用户名 {username} 的检索报告\\n\")"),
    ("report.py", 'gen_line = f"Generated at {generated_at} by [Maigret](https://github.com/soxoj/maigret)"',
     'gen_line = f"生成于 {generated_at}，工具 [Maigret](https://github.com/soxoj/maigret)"'),
    ("report.py", "f'Countries: {\", \".join(countries)}\\n'", "f'国家/地区：{\", \".join(countries)}\\n'"),
    ("report.py", "f'Interests (tags): {\", \".join(interests)}\\n'", "f'兴趣标签：{\", \".join(interests)}\\n'"),
    # PDF 渲染会下载远程头像，国内网络常卡在重试（gravatar/imgur 不可达）。
    # 一律返回本地空白图，PDF 里不显示头像，换取生成速度与稳定性。
    ("report.py", '    if _is_safe_report_image_url(uri):\n        return uri\n    return _BLANK_IMAGE_PATH',
     '    # 本地补丁：不再联网下载头像。远程图片在国内常不可达，\n'
     '    # xhtml2pdf 会逐个重试直到超时，一次扫描要多等几分钟。\n'
     '    return _BLANK_IMAGE_PATH'),
    # 关系图：没有命中的用户名（变体搜索常见）不要建空节点，否则图里堆一圈孤点
    ("report.py",
     "    for username, id_type, results in username_results:\n"
     "        # Add username node, using normalized version directly if different\n"
     "        norm_username = username.lower()\n"
     "        username_node_name = graph.add_node(id_type, norm_username)\n\n"
     "        for website_name, dictionary in results.items():",
     "    for username, id_type, results in username_results:\n"
     "        norm_username = username.lower()\n"
     "        # 本地补丁：先确认这个用户名有命中，再建节点。\n"
     "        # 变体搜索会带进来十几个 0 命中的用户名，原实现照样建孤点，图里全是空节点。\n"
     "        has_hit = any(\n"
     "            (d.get('status') is not None\n"
     "             and not d.get('is_similar')\n"
     "             and d['status'].status == MaigretCheckStatus.CLAIMED)\n"
     "            for d in results.values()\n"
     "        )\n"
     "        if not has_hit:\n"
     "            continue\n"
     "        username_node_name = graph.add_node(id_type, norm_username)\n\n"
     "        for website_name, dictionary in results.items():"),
]

CATEGORIES = {
    "gaming": "游戏", "coding": "编程", "photo": "摄影", "music": "音乐", "blog": "博客",
    "finance": "金融", "freelance": "自由职业", "dating": "交友", "tech": "科技", "forum": "论坛",
    "porn": "成人", "erotic": "情色", "webcam": "直播", "video": "视频", "movies": "影视",
    "hacking": "黑客", "art": "艺术", "discussion": "讨论", "sharing": "分享", "writing": "写作",
    "wiki": "百科", "business": "商务", "shopping": "购物", "sport": "体育", "books": "图书",
    "news": "新闻", "documents": "文档", "travel": "旅行", "maps": "地图", "hobby": "爱好",
    "apps": "应用", "classified": "分类信息", "career": "职业", "geosocial": "地理社交",
    "streaming": "流媒体", "education": "教育", "networking": "社交网络", "torrent": "种子",
    "science": "科学", "medicine": "医疗", "reading": "阅读", "stock": "股票",
    "messaging": "即时通讯", "trading": "交易", "links": "链接", "fashion": "时尚", "tasks": "任务",
    "military": "军事", "auto": "汽车", "gambling": "赌博", "cybercriminal": "网络犯罪",
    "review": "评测", "bookmarks": "书签", "design": "设计", "tor": "Tor", "i2p": "I2P",
    "q&a": "问答", "crypto": "加密货币", "ai": "AI",
}

COUNTRIES = {
    "ae": "阿联酋", "ao": "安哥拉", "ar": "阿根廷", "at": "奥地利", "au": "澳大利亚",
    "az": "阿塞拜疆", "bd": "孟加拉国", "be": "比利时", "bg": "保加利亚", "br": "巴西",
    "by": "白俄罗斯", "ca": "加拿大", "ch": "瑞士", "cl": "智利", "cn": "中国",
    "co": "哥伦比亚", "cr": "哥斯达黎加", "cz": "捷克", "de": "德国", "dk": "丹麦",
    "dz": "阿尔及利亚", "ee": "爱沙尼亚", "eg": "埃及", "es": "西班牙", "eu": "欧盟",
    "fi": "芬兰", "fr": "法国", "gb": "英国", "gr": "希腊", "hk": "中国香港",
    "hr": "克罗地亚", "hu": "匈牙利", "id": "印度尼西亚", "ie": "爱尔兰", "il": "以色列",
    "in": "印度", "ir": "伊朗", "it": "意大利", "jp": "日本", "kg": "吉尔吉斯斯坦",
    "kr": "韩国", "kz": "哈萨克斯坦", "la": "老挝", "lk": "斯里兰卡", "lt": "立陶宛",
    "ma": "摩洛哥", "md": "摩尔多瓦", "mg": "马达加斯加", "mk": "北马其顿", "mx": "墨西哥",
    "ng": "尼日利亚", "nl": "荷兰", "no": "挪威", "ph": "菲律宾", "pk": "巴基斯坦",
    "pl": "波兰", "pt": "葡萄牙", "re": "留尼汪", "ro": "罗马尼亚", "rs": "塞尔维亚",
    "ru": "俄罗斯", "sa": "沙特阿拉伯", "sd": "苏丹", "se": "瑞典", "sg": "新加坡",
    "sk": "斯洛伐克", "sv": "萨尔瓦多", "th": "泰国", "tn": "突尼斯", "tr": "土耳其",
    "tw": "中国台湾", "ua": "乌克兰", "uk": "英国", "us": "美国", "uz": "乌兹别克斯坦",
    "ve": "委内瑞拉", "vi": "美属维尔京群岛", "vn": "越南", "za": "南非",
}

CAT_RE = re.compile(r'(<option value="([a-z&]+)">)([^<]*)(</option>)')
COUNTRY_RE = re.compile(r'(<option value="([a-z]{2}|global)" data-group="country">)([^<]*)(</option>)')

# 实时进度流的原始实现（连接一断就把 job 删掉，重连必然 404）
OLD_STREAM = '''@app.route('/api/scan/<job_id>/stream')
def scan_stream(job_id):
    job = live_jobs.get(job_id)
    if not job:
        return "Unknown job", 404

    def gen():
        try:
            while True:
                event = job['queue'].get()
                yield f"data: {json.dumps(event)}\\n\\n"
                if event.get('type') == 'done':
                    break
        finally:
            live_jobs.pop(job_id, None)

    return Response(gen(), mimetype='text/event-stream')'''

NEW_STREAM = '''@app.route('/api/scan/<job_id>/stream')
def scan_stream(job_id):
    job = live_jobs.get(job_id)
    if not job:
        # 重连兜底：任务已结束、报告已生成的话，直接让前端去结果页，
        # 而不是丢一个 404 让 EventSource 放弃重试。
        if job_id in job_results:
            payload = {'type': 'done', 'redirect': f'/results/search_{job_id}'}
            return Response(
                f"data: {json.dumps(payload)}\\n\\n", mimetype='text/event-stream'
            )
        return "Unknown job", 404

    def gen():
        # 不在这里 pop job：浏览器切标签页、休眠、刷新都会断开这条流，
        # 一旦 pop 掉，重连就 404，页面会永久停在"已检查 N/M"。
        while True:
            try:
                event = job['queue'].get(timeout=20)
            except queue.Empty:
                yield ": keepalive\\n\\n"  # 心跳，避免代理/浏览器掐掉空闲连接
                continue
            yield f"data: {json.dumps(event)}\\n\\n"
            if event.get('type') == 'done':
                break

    return Response(gen(), mimetype='text/event-stream')'''

# 设置与报告的落盘位置（相对路径 / /tmp 会随启动目录漂移）
OLD_PATHS = '''app.config["REPORTS_FOLDER"] = os.path.abspath('/tmp/maigret_reports')
app.config["SETTINGS_FILE"] = "web_settings.json"'''

NEW_PATHS = '''app.config["REPORTS_FOLDER"] = os.path.join(_MAIGRET_HOME, "reports")
app.config["SETTINGS_FILE"] = os.path.join(_MAIGRET_HOME, "web_settings.json")'''

# ---- 结果页画像卡片（本地增强，不是汉化） ----
PROFILE_CSS = '''
    /* 画像卡片：聚合各站点抽到的字段，避免结果页只有一串链接 */
    .profile-card {
       border: 1px solid var(--bs-border-color);
       border-radius: 8px;
       padding: 1rem 1.2rem;
       margin-bottom: 1.5rem;
       background: rgba(127, 127, 127, 0.06);
    }
    .profile-head {
       display: flex;
       align-items: baseline;
       gap: 0.75rem;
       flex-wrap: wrap;
    }
    .profile-brief {
       margin: 0.6rem 0 0.8rem;
       color: var(--bs-secondary-color, #6c757d);
    }
    .profile-grid {
       display: grid;
       grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
       gap: 0.35rem 1.5rem;
    }
    .profile-grid .kv {
       display: flex;
       gap: 0.5rem;
       font-size: 0.92rem;
       line-height: 1.55;
    }
    .profile-grid .k {
       flex: 0 0 4.8rem;
       color: var(--bs-secondary-color, #6c757d);
    }
    .profile-grid .v {
       flex: 1 1 auto;
       word-break: break-word;
    }
    .profile-grid .v em {
       font-style: normal;
       opacity: 0.55;
    }
    .profile-grid .tagpair {
       display: inline-block;
       white-space: nowrap;
       margin-right: 0.45rem;
    }
    .profile-item {
       flex-wrap: wrap;
    }
    .acct-fields {
       flex-basis: 100%;
       margin-top: 0.35rem;
       display: flex;
       flex-wrap: wrap;
       gap: 0.25rem 1rem;
       font-size: 0.84rem;
       color: var(--bs-secondary-color, #6c757d);
    }
    .acct-fields b {
       font-weight: 600;
       margin-right: 0.3rem;
       color: var(--bs-body-color);
    }
    .ai-out {
       margin-top: 0.9rem;
       padding-top: 0.9rem;
       border-top: 1px solid var(--bs-border-color);
       white-space: pre-wrap;
       font-size: 0.92rem;
       line-height: 1.7;
    }
    .ai-out.ai-error {
       color: var(--bs-danger, #dc3545);
    }
'''

PROFILE_CARD = '''

        {% for report in individual_reports %}{% if report.profile %}
        <div class="profile-card">
            <div class="profile-head">
                <h3 class="mb-0">{{ report.username }} 的画像</h3>
                <span class="text-muted">命中 {{ report.profile.accounts }} 个站点</span>
                <button type="button" class="btn btn-sm btn-outline-warning ms-auto ai-btn" data-job="{{ timestamp }}">AI 分析</button>
            </div>
            {% if report.profile.brief %}<p class="profile-brief">{{ report.profile.brief }}</p>{% endif %}
            <div class="profile-grid">
                {% for label, value in report.profile.fields %}
                <div class="kv"><span class="k">{{ label }}</span><span class="v">{{ value }}</span></div>
                {% endfor %}
                {% if report.profile.first_seen %}
                <div class="kv"><span class="k">最早记录</span><span class="v">{{ report.profile.first_seen }}</span></div>
                {% endif %}
                {% if report.profile.countries %}
                <div class="kv"><span class="k">国家/地区</span><span class="v">{% for name, n in report.profile.countries %}<span class="tagpair">{{ name }}<em>×{{ n }}</em></span>{% endfor %}</span></div>
                {% endif %}
                {% if report.profile.interests %}
                <div class="kv"><span class="k">兴趣领域</span><span class="v">{% for name, n in report.profile.interests %}<span class="tagpair">{{ name }}<em>×{{ n }}</em></span>{% endfor %}</span></div>
                {% endif %}
            </div>
            <div class="ai-out" hidden></div>
        </div>
        {% endif %}{% endfor %}'''

AI_SCRIPT = '''
<script>
document.querySelectorAll('.ai-btn').forEach(function (btn) {
    btn.addEventListener('click', async function () {
        const card = btn.closest('.profile-card');
        const out = card.querySelector('.ai-out');
        btn.disabled = true;
        btn.textContent = '分析中…';
        out.hidden = false;
        out.classList.remove('ai-error');
        out.textContent = '正在调用模型分析，通常需要 10–30 秒…';
        try {
            const resp = await fetch('/api/ai/' + btn.dataset.job, { method: 'POST' });
            const data = await resp.json();
            out.textContent = data.ok ? data.text : (data.error || '调用失败');
            out.classList.toggle('ai-error', !data.ok);
        } catch (err) {
            out.textContent = '请求失败：' + err;
            out.classList.add('ai-error');
        } finally {
            btn.disabled = false;
            btn.textContent = 'AI 分析';
        }
    });
});
</script>'''

ACCOUNT_FIELDS = '''
                            {% if profile.fields %}
                            <div class="acct-fields">
                                {% for label, value in profile.fields %}<span><b>{{ label }}</b>{{ value }}</span>{% endfor %}
                            </div>
                            {% endif %}'''

RESULTS_CARD_ANCHOR = """<p>检索已完成。<a href="{{ url_for('index')}}">返回首页</a></p>"""
RESULTS_ITEM_ANCHOR = """                            {% endif %}
                        </li>"""

# AI 摘要控件：分开处理，因为老版本已经插过画像卡、只缺 AI 部分
AI_BTN_OLD = '<span class="text-muted">命中 {{ report.profile.accounts }} 个站点</span>'
AI_BTN_NEW = AI_BTN_OLD + (
    '\n                <button type="button" class="btn btn-sm btn-outline-warning ms-auto ai-btn"'
    ' data-job="{{ timestamp }}">AI 分析</button>'
)
AI_PANEL_OLD = """                {% endif %}
            </div>
        </div>
        {% endif %}{% endfor %}"""
AI_PANEL_NEW = """                {% endif %}
            </div>
            <div class="ai-out" hidden></div>
        </div>
        {% endif %}{% endfor %}"""
AI_CSS_OLD = """    .acct-fields b {
       font-weight: 600;
       margin-right: 0.3rem;
       color: var(--bs-body-color);
    }"""
AI_CSS_NEW = AI_CSS_OLD + """
    .ai-out {
       margin-top: 0.9rem;
       padding-top: 0.9rem;
       border-top: 1px solid var(--bs-border-color);
       white-space: pre-wrap;
       font-size: 0.92rem;
       line-height: 1.7;
    }
    .ai-out.ai-error {
       color: var(--bs-danger, #dc3545);
    }"""

# 中文补检表格样式（用独有注释做幂等标记）
CN_CSS_MARK = "/* 中文补检表格 */"
CN_CSS = """
    /* 中文补检表格 */
    .cn-out {
       margin-top: 0.9rem;
       padding-top: 0.9rem;
       border-top: 1px solid var(--bs-border-color);
       font-size: 0.9rem;
    }
    .cn-out table { width: 100%; }
    .cn-out th, .cn-out td { padding: 0.25rem 0.5rem; border-bottom: 1px solid var(--bs-border-color); }
    .cn-out .cn-note { color: var(--bs-secondary-color, #6c757d); font-size: 0.85rem; margin: 0.4rem 0 0; }
"""

CN_BTN_OLD = '<button type="button" class="btn btn-sm btn-outline-warning ms-auto ai-btn" data-job="{{ timestamp }}">AI 分析</button>'
CN_BTN_NEW = CN_BTN_OLD + (
    '\n                <button type="button" class="btn btn-sm btn-outline-info ms-1 cn-btn"'
    ' data-job="{{ timestamp }}">中文补检</button>'
)
CN_PANEL_OLD = '<div class="ai-out" hidden></div>'
CN_PANEL_NEW = CN_PANEL_OLD + '\n            <div class="cn-out" hidden></div>'

# 核心账号比对：每个账号行加一个「设为核心」，选中后按锚点信息给其它账号打分
ANCHOR_ROW_OLD = '<li class="profile-item">'
ANCHOR_ROW_NEW = '<li class="profile-item" data-site="{{ profile.site_name }}" data-user="{{ report.username }}">'
ANCHOR_LINK_OLD = '<a href="{{ profile.url }}" target="_blank">{{ profile.site_name }}</a>'
ANCHOR_BTN = (
    '\n                                <button type="button"'
    ' class="btn btn-sm btn-outline-secondary ms-2 anchor-btn"'
    ' data-site="{{ profile.site_name }}" data-user="{{ report.username }}"'
    ' data-job="{{ timestamp }}">设为核心</button>'
)
ANCHOR_PANEL_OLD = '<div class="cn-out" hidden></div>'
ANCHOR_PANEL_NEW = ANCHOR_PANEL_OLD + '\n            <div class="anchor-out" hidden></div>'

ANCHOR_CSS = """
    /* 核心账号比对 */
    .anchor-badge {
       margin-left: 0.5rem;
       padding: 1px 7px;
       border-radius: 10px;
       font-size: 0.75rem;
       white-space: nowrap;
    }
    .anchor-badge.lv-high { background: #198754; color: #fff; }
    .anchor-badge.lv-mid { background: #ffc107; color: #000; }
    .anchor-badge.lv-low { background: #6c757d; color: #fff; }
    .anchor-badge.lv-core { background: #0dcaf0; color: #000; }
    .profile-item.anchor-low { opacity: 0.45; }
    .anchor-out {
       margin-top: 0.9rem;
       padding-top: 0.9rem;
       border-top: 1px solid var(--bs-border-color);
       font-size: 0.9rem;
    }
"""

ANCHOR_SCRIPT = """
<script>
const LEVEL_CLASS = { '高': 'lv-high', '中': 'lv-mid', '低': 'lv-low' };

const AUTO_ANCHOR = {{ (anchor | tojson) if anchor else 'null' }};

document.querySelectorAll('.anchor-btn').forEach(function (btn) {
    btn.addEventListener('click', async function (event) {
        event.stopPropagation();
        const label = btn.textContent;
        btn.disabled = true;
        btn.textContent = '比对中…';
        try {
            const resp = await fetch('/api/anchor/' + btn.dataset.job, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ site: btn.dataset.site, username: btn.dataset.user })
            });
            const data = await resp.json();
            if (!data.ok) { alert(data.error || '比对失败'); return; }
            applyAnchor(data);
        } catch (err) {
            alert('请求失败：' + err);
        } finally {
            btn.disabled = false;
            btn.textContent = label;
        }
    });
});

function rowsOf(user) {
    return Array.prototype.filter.call(
        document.querySelectorAll('.profile-item[data-site]'),
        function (r) { return r.dataset.user === user; }
    );
}

function applyAnchor(data) {
    const rows = rowsOf(data.username);
    const find = function (site) {
        return rows.find(function (r) { return r.dataset.site === site; }) || null;
    };
    data.items.forEach(function (it) {
        const row = find(it.site);
        if (!row) return;
        let badge = row.querySelector('.anchor-badge');
        if (!badge) { badge = document.createElement('span'); row.appendChild(badge); }
        badge.textContent = it.level + ' ' + it.score;
        badge.className = 'anchor-badge ' + (LEVEL_CLASS[it.level] || 'lv-low');
        badge.title = it.reasons.join('；');
        row.classList.toggle('anchor-low', it.level === '低');
    });
    const coreRow = find(data.anchor);
    if (coreRow) {
        let badge = coreRow.querySelector('.anchor-badge');
        if (!badge) { badge = document.createElement('span'); coreRow.appendChild(badge); }
        badge.textContent = '核心';
        badge.className = 'anchor-badge lv-core';
        coreRow.classList.remove('anchor-low');
    }

    const box = document.querySelector('.anchor-out');
    if (!box) return;
    box.hidden = false;
    box.innerHTML = '核心账号：<b>' + data.anchor + '</b>' + (data.username ? '（' + data.username + '）' : '') + ' · 高 ' + data.counts['高']
        + ' / 中 ' + data.counts['中'] + ' / 低 ' + data.counts['低']
        + ' <label class="ms-3"><input type="checkbox" id="hideLow"> 隐藏低相关</label>';
    document.getElementById('hideLow').addEventListener('change', function () {
        rows.filter(function (r) { return r.classList.contains('anchor-low'); }).forEach(function (row) {
            row.style.display = this.checked ? 'none' : '';
        }, this);
    });
}

// 从主页 URL 发起检索时，自动以该主页对应的账号为核心并立即比对
if (AUTO_ANCHOR && AUTO_ANCHOR.site) {
    const target = Array.prototype.find.call(
        document.querySelectorAll('.anchor-btn'),
        function (b) { return b.dataset.site === AUTO_ANCHOR.site; }
    );
    if (target) {
        // 等 DOM 稳定后再点，避免与折叠动画抢时序
        setTimeout(function () { target.click(); }, 300);
    }
}
</script>"""

CN_SCRIPT = '''<script>
document.querySelectorAll('.cn-btn').forEach(function (btn) {
    btn.addEventListener('click', async function () {
        const card = btn.closest('.profile-card');
        const out = card.querySelector('.cn-out');
        btn.disabled = true;
        btn.textContent = '查询中…';
        out.hidden = false;
        out.textContent = '正在查询 B站、微博、贴吧…';
        try {
            const resp = await fetch('/api/cn/' + btn.dataset.job, { method: 'POST' });
            const data = await resp.json();
            if (!data.ok) {
                out.textContent = data.error || '补检失败';
                return;
            }
            const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
            let html = '';
            if (data.items.length) {
                html += '<table><thead><tr><th>平台</th><th>昵称</th><th>置信</th><th>说明</th></tr></thead><tbody>';
                for (const it of data.items) {
                    html += '<tr><td>' + esc(it.platform) + '</td>'
                        + '<td><a href="' + esc(it.url) + '" target="_blank" rel="noopener">' + esc(it.name) + '</a></td>'
                        + '<td>' + esc(it.confidence) + '</td><td>' + esc(it.extra) + '</td></tr>';
                }
                html += '</tbody></table>';
            } else {
                html += '<p class="mb-1">没有搜到同名账号。</p>';
            }
            for (const note of data.notes) html += '<p class="cn-note">' + esc(note) + '</p>';
            out.innerHTML = html;
        } catch (err) {
            out.textContent = '请求失败：' + err;
        } finally {
            btn.disabled = false;
            btn.textContent = '中文补检';
        }
    });
});
</script>'''


def path_of(name: str) -> Path:
    """模板在 web/templates/，报告模板在 resources/，app.py 在 web/ 下。"""
    if name.endswith(".html"):
        return TPL / name
    if name.endswith(".tpl"):
        return SP / "resources" / name
    if name == "app.py":
        return SP / "web" / name
    return SP / name


def backup(name: str) -> bool:
    """保留英文原文，返回是否新建了备份。

    已存在则对比内容：内容不同说明上游升级了，此时用新原文覆盖旧备份，
    否则补丁会一直套在旧原文上打，永远检测不到上游的改动。
    """
    src = path_of(name)
    dst = ORIG / name
    ORIG.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def transform(name: str, text: str) -> tuple[str, list[str]]:
    """对该文件应用全部替换，返回新内容和未匹配到的原文列表。"""
    missing = []

    # 先把 app.py 里拼 URL 的路径规范化（放在最前面，后面的补丁才能匹配到稳定形态）
    if name == "app.py":
        text = re.sub(            r'os\.path\.join\(\s*f"search_\{session_key\}",\s*f"report_\{safe_username\}(\.\w+)"\s*\)',
            r'f"search_{session_key}/report_{safe_username}\1"',
            text,
        )
        text = re.sub(
            r'os\.path\.join\(\s*f"search_\{session_key\}",\s*"combined_graph\.html"\s*\)',
            r'f"search_{session_key}/combined_graph.html"',
            text,
        )
        # 路径常量要先落位，后面的 COOKIES_FILE 补丁会引用 _MAIGRET_HOME
        text = text.replace(OLD_PATHS, NEW_PATHS)

    for fname, old, new in PATCHES:
        if fname != name:
            continue
        if new in text:
            continue  # 已经应用过（插入型补丁的 new 一定包含 old，只能靠这条判重）
        if old not in text:
            missing.append(old)
            continue
        text = text.replace(old, new)

    if name == "base.html":
        text = COUNTRY_RE.sub(
            lambda m: m.group(1) + f"{m.group(2).upper()} - {COUNTRIES[m.group(2)]}" + m.group(4)
            if m.group(2) in COUNTRIES else m.group(0),
            text,
        )
        text = CAT_RE.sub(
            lambda m: m.group(1) + CATEGORIES[m.group(2)] + m.group(4)
            if m.group(2) in CATEGORIES else m.group(0),
            text,
        )

    if name == "app.py":
        # 断流修复：原实现在生成器结束时 pop 掉 job，浏览器一断线重连就 404，
        # 页面永久停在"已检查 N/M"。改成保留 job + 心跳 + 重连兜底。
        text = text.replace(OLD_STREAM, NEW_STREAM)

        # 路径修复：设置与报告原来用相对路径 / /tmp，会随启动目录漂移，
        # 换个目录起服务就读到另一份设置、写另一处报告。统一放到 ~/.maigret。
        text = text.replace(OLD_PATHS, NEW_PATHS)

    if name == "results.html":
        if "profile-card" not in text:
            # 结果页增强：顶部画像卡 + 每个账号的字段展开
            text = text.replace("    </style>", PROFILE_CSS + "    </style>", 1)
            text = text.replace(RESULTS_CARD_ANCHOR, RESULTS_CARD_ANCHOR + PROFILE_CARD, 1)
            text = text.replace(RESULTS_ITEM_ANCHOR, RESULTS_ITEM_ANCHOR.replace('</li>', ACCOUNT_FIELDS + '\n                        </li>'), 1)
        if "ai-btn" not in text:
            text = text.replace(AI_BTN_OLD, AI_BTN_NEW, 1)
            text = text.replace(AI_PANEL_OLD, AI_PANEL_NEW, 1)
            text = text.replace(AI_CSS_OLD, AI_CSS_NEW, 1)
        if "cn-btn" not in text:
            text = text.replace(CN_BTN_OLD, CN_BTN_NEW, 1)
            text = text.replace(CN_PANEL_OLD, CN_PANEL_NEW, 1)
        if "anchor-btn" not in text:
            text = text.replace(ANCHOR_ROW_OLD, ANCHOR_ROW_NEW, 1)
            text = text.replace(ANCHOR_LINK_OLD, ANCHOR_LINK_OLD + ANCHOR_BTN, 1)
            text = text.replace(ANCHOR_PANEL_OLD, ANCHOR_PANEL_NEW, 1)
        if ".anchor-badge" not in text:
            text = text.replace("    </style>", ANCHOR_CSS + "    </style>", 1)
        if "AUTO_ANCHOR" not in text:
            # 用脚本里的独有标识判重：先前用 "/api/anchor/" 判断，会被路由或
            # 旧版脚本提前满足，导致新脚本永远插不进去。
            text = text.replace("{% endblock %}", ANCHOR_SCRIPT + "\n{% endblock %}", 1)
        if CN_CSS_MARK not in text:
            text = text.replace("    </style>", CN_CSS + "    </style>", 1)
        if "/api/ai/" not in text:
            text = text.replace("{% endblock %}", AI_SCRIPT + "\n{% endblock %}", 1)
        if "/api/cn/" not in text:
            text = text.replace("{% endblock %}", CN_SCRIPT + "\n{% endblock %}", 1)
    return text, missing


def sync_feature_files() -> None:
    """把 zh/ 下的功能模块同步进 site-packages（画像卡片、AI 摘要依赖它们）。"""
    here = Path(__file__).resolve().parent
    for name in ("profile_card.py", "ai_summary.py", "cookie_store.py", "cn_probe.py",
                 "anchor.py", "url_anchor.py"):
        src = here / name
        dst = SP / "web" / name
        if not src.exists():
            continue
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            continue
        shutil.copy2(src, dst)
        print(f"[已部署] web/{name}")


def main() -> int:
    restore = "--restore" in sys.argv
    check_only = "--check" in sys.argv

    names = sorted({p[0] for p in PATCHES})
    if restore:
        for name in names:
            src = ORIG / name
            if not src.exists():
                print(f"[跳过] 没有备份：{name}")
                continue
            shutil.copy2(src, path_of(name))
            print(f"[还原] {name}")
        return 0

    total_missing = 0

    # 每次都从英文原文重放，而不是在已打过补丁的文件上再匹配一次。
    # 后者有两个致命问题：上游升级后旧补丁继续套在旧原文上，看不出上游改了
    # 什么；重复运行还会把「上次没匹配上的」当成「这次已应用」。
    if not check_only:
        sync_feature_files()

    for name in names:
        path = path_of(name)
        source = ORIG / name

        if source.exists():
            text = source.read_bytes().decode("utf-8")
        else:
            # 首次运行：当前文件就是上游原文，先存下来当基线
            text = path.read_bytes().decode("utf-8")
            ORIG.mkdir(parents=True, exist_ok=True)
            source.write_bytes(text.encode("utf-8"))

        new_text, missing = transform(name, text)

        for old in missing:
            total_missing += 1
            print(f"[未匹配] {name}: {old[:70]!r}")

        if check_only:
            continue

        if new_text == text:
            print(f"[无改动] {name}")
            continue

        path.write_bytes(new_text.encode("utf-8"))
        print(f"[已应用] {name}")

    if total_missing:
        print()
        print(f"!! 有 {total_missing} 处补丁未匹配，退出码 1。")
        print("   原因通常是上游改了这些文件的代码或文案。")
        print("   请对照本文件里的 PATCHES 修正对应的 old 字符串。")
        print("   现在每轮都从 zh/orig/ 的原文重放，所以已应用的部分不会重复叠加。")
        return 1

    print(f"\n全部 {len(names)} 个文件的补丁均已应用，0 处失配。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
