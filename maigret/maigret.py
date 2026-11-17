"""
Maigret main module
"""

import asyncio
import logging
import os
import sys
import platform
from argparse import ArgumentParser, BooleanOptionalAction, RawDescriptionHelpFormatter
from typing import Any, Dict, List, Optional, Tuple
import os.path as path
from maigret.utils import extract_usernames, read_input_file

try:
    from socid_extractor import extract, parse
except ImportError as e:
    raise ImportError(
        "Missing dependency: socid_extractor\n\n"
        "If installed from PyPI:\n"
        "    pip install -U maigret\n\n"
        "If running from a cloned repository:\n"
        "    pip install -e .\n\n"
        "Then run Maigret as:\n"
        "    python -m maigret <username>"
    ) from e


from .__version__ import __version__
from .checking import (
    timeout_check,
    SUPPORTED_IDS,
    self_check,
    BAD_CHARS,
    maigret,
    build_cloudflare_bypass_config,
)
from . import errors
from .notify import QueryNotifyPrint, print_donate_banner, print_intro_banner
from .report import (
    save_csv_report,
    save_xmind_report,
    save_html_report,
    save_pdf_report,
    generate_report_context,
    save_txt_report,
    SUPPORTED_JSON_REPORT_FORMATS,
    save_json_report,
    get_plaintext_report,
    sort_report_by_data_points,
    save_graph_report,
    save_neo4j_report,
    save_markdown_report,
)
from .result import SiteResult
from .activation import load_activation_cache, save_activation_cache
from .cn.hook import augment_sites
from .sites import MaigretDatabase
from .submit import Submitter
from .utils import get_dict_ascii_tree
from .settings import Settings
from .permutator import Permute


def extract_ids_from_page(url, logger, timeout=5) -> dict:
    results = {}
    # url, headers
    reqs: List[Tuple[str, set]] = [(url, set())]
    try:
        # temporary workaround for URL mutations MVP
        from socid_extractor import mutate_url

        reqs += list(mutate_url(url))
    except Exception as e:
        logger.warning(e)

    for req in reqs:
        url, headers = req
        print(f'Scanning webpage by URL {url}...')
        page, _ = parse(url, cookies_str='', headers=headers, timeout=timeout)
        logger.debug(page)
        info = extract(page)
        if not info:
            print('Nothing extracted')
        else:
            print(get_dict_ascii_tree(info.items(), new_line=False), ' ')
        for k, v in info.items():
            # keys containing "username" are owned by extract_usernames() below,
            # which validates them; adding them here would bypass that check
            if "username" not in k and k in SUPPORTED_IDS:
                results[v] = k

        for username in extract_usernames(info, logger):
            results[username] = 'username'

    return results


def extract_ids_from_results(results: Dict[str, SiteResult], db: MaigretDatabase) -> dict:
    ids_results = {}
    for website_name in results:
        dictionary = results[website_name]
        new_usernames = dictionary.get('ids_usernames')
        if new_usernames:
            for u, utype in new_usernames.items():
                ids_results[u] = utype

        for url in dictionary.get('ids_links', []):
            ids_results.update(db.extract_ids_from_url(url))

    return ids_results


def setup_arguments_parser(settings: Settings):
    from aiohttp import __version__ as aiohttp_version
    from requests import __version__ as requests_version
    from socid_extractor import __version__ as socid_version

    version_string = '\n'.join(
        [
            f'%(prog)s {__version__}',
            f'Socid-extractor:  {socid_version}',
            f'Aiohttp:  {aiohttp_version}',
            f'Requests:  {requests_version}',
            f'Python:  {platform.python_version()}',
        ]
    )

    parser = ArgumentParser(
        formatter_class=RawDescriptionHelpFormatter,
        description=f"Maigret v{__version__}\n"
        "Documentation: https://maigret.readthedocs.io/\n"
        "All settings are also configurable through files, see docs.",
    )
    parser.add_argument(
        "username",
        nargs='*',
        metavar="USERNAMES",
        help="One or more usernames to search by.",
    )
    parser.add_argument(
        "--input-file",
        dest="input_file",
        metavar="PATH",
        help="Read identifiers from a file, one per line ('-' for stdin). "
        "A line can carry its own id type, e.g. vk_id:12345.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=version_string,
        help="Display version information and dependencies.",
    )
    parser.add_argument(
        "--timeout",
        action="store",
        metavar='TIMEOUT',
        dest="timeout",
        type=timeout_check,
        default=settings.timeout,
        help="Time in seconds to wait for response to requests "
        f"(default {settings.timeout}s). "
        "A longer timeout will be more likely to get results from slow sites. "
        "On the other hand, this may cause a long delay to gather all results. ",
    )
    parser.add_argument(
        "--retries",
        action="store",
        type=int,
        metavar='RETRIES',
        default=settings.retries_count,
        help="Attempts to restart temporarily failed requests.",
    )
    parser.add_argument(
        "-n",
        "--max-connections",
        action="store",
        type=int,
        dest="connections",
        default=settings.max_connections,
        help=f"Allowed number of concurrent connections (default {settings.max_connections}).",
    )
    parser.add_argument(
        "--dns-resolver",
        dest="dns_resolver",
        default="async",
        choices=("async", "threaded"),
        help=(
            "DNS resolver to use. 'async' (default) uses aiohttp's AsyncResolver "
            "(via aiodns/c-ares) — fastest under high concurrency. 'threaded' uses "
            "the OS getaddrinfo via a threadpool — slower, but respects the system "
            "DNS configuration. Switch to 'threaded' if you see "
            "'Could not contact DNS servers' for every site (issue #2688)."
        ),
    )
    parser.add_argument(
        "--no-recursion",
        action="store_true",
        dest="disable_recursive_search",
        default=(not settings.recursive_search),
        help="Disable recursive search by additional data extracted from pages.",
    )
    parser.add_argument(
        "--no-extracting",
        action="store_true",
        dest="disable_extracting",
        default=(not settings.info_extracting),
        help="Disable parsing pages for additional data and other usernames.",
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        default=False,
        help="Fetch secondary API/JSON endpoints derived from profile URLs "
        "(via socid_extractor url_mutations) and merge extracted fields into "
        "results. Off by default; adds extra HTTP requests per claimed site.",
    )
    parser.add_argument(
        "--id-type",
        dest="id_type",
        default='username',
        choices=SUPPORTED_IDS,
        help="Specify identifier(s) type (default: username).",
    )
    parser.add_argument(
        "--permute",
        action="store_true",
        default=False,
        help="Permute at least 2 usernames to generate more possible usernames.",
    )
    parser.add_argument(
        "--db",
        metavar="DB_FILE",
        dest="db_file",
        default=settings.sites_db_path,
        help="Load Maigret database from a JSON file or HTTP web resource.",
    )
    parser.add_argument(
        "--no-autoupdate",
        action="store_true",
        dest="no_autoupdate",
        default=settings.no_autoupdate,
        help="Disable automatic database updates on startup.",
    )
    parser.add_argument(
        "--force-update",
        action="store_true",
        dest="force_update",
        default=False,
        help="Force check for database updates and download if available.",
    )
    parser.add_argument(
        "--cookies-jar-file",
        metavar="COOKIE_FILE",
        dest="cookie_file",
        default=settings.cookie_jar_file,
        help="File with cookies.",
    )
    parser.add_argument(
        "--ignore-ids",
        action="append",
        metavar='IGNORED_IDS',
        dest="ignore_ids_list",
        default=settings.ignore_ids_list,
        help="Do not make search by the specified username or other ids.",
    )
    # reports options
    parser.add_argument(
        "--folderoutput",
        "-fo",
        dest="folderoutput",
        default=settings.reports_path,
        metavar="PATH",
        help="If using multiple usernames, the output of the results will be saved to this folder.",
    )
    parser.add_argument(
        "--proxy",
        "-p",
        metavar='PROXY_URL',
        action="store",
        dest="proxy",
        default=settings.proxy_url,
        help="Make requests over a proxy. e.g. socks5://127.0.0.1:1080 "
        "(socks5:// and socks5h:// are equivalent, both resolve at the proxy)",
    )
    parser.add_argument(
        "--tor-proxy",
        metavar='TOR_PROXY_URL',
        action="store",
        default=settings.tor_proxy_url,
        help="Specify URL of your Tor gateway. Default is socks5://127.0.0.1:9050",
    )
    parser.add_argument(
        "--i2p-proxy",
        metavar='I2P_PROXY_URL',
        action="store",
        default=settings.i2p_proxy_url,
        help="Specify URL of your I2P gateway. Default is http://127.0.0.1:4444",
    )
    parser.add_argument(
        "--with-domains",
        action="store_true",
        default=settings.domain_search,
        help="Enable (experimental) feature of checking domains on usernames.",
    )
    parser.add_argument(
        "--cloudflare-bypass",
        action="store_true",
        default=False,
        help="Enable Cloudflare webgate bypass for sites with protection cf_js_challenge / cf_firewall / webgate. "
             "Requires a local CloudflareBypassForScraping instance (see settings.json -> cloudflare_bypass.modules[0].url).",
    )

    filter_group = parser.add_argument_group(
        'Site filtering', 'Options to set site search scope'
    )
    filter_group.add_argument(
        "-a",
        "--all-sites",
        action="store_true",
        dest="all_sites",
        default=settings.scan_all_sites,
        help="Use all sites for scan.",
    )
    filter_group.add_argument(
        "--top-sites",
        action="store",
        default=settings.top_sites_count,
        metavar="N",
        type=int,
        help="Count of sites for scan ranked by Alexa Top (default: 500).",
    )
    filter_group.add_argument(
        "--tags", dest="tags", default='', help="Specify tags of sites (see `--stats`)."
    )
    filter_group.add_argument(
        "--exclude-tags",
        dest="exclude_tags",
        default='',
        help="Specify tags to exclude from search (blacklist).",
    )
    filter_group.add_argument(
        "--keywords",
        nargs='+',
        metavar='KEYWORD',
        dest="keywords",
        default=[],
        help="Specify keywords to search for in HTML content. Sites containing both username AND any keyword get special highlighting. e.g. --keywords tech python",
    )
    filter_group.add_argument(
        "--site",
        action="append",
        metavar='SITE_NAME',
        dest="site_list",
        default=settings.scan_sites_list,
        help="Limit analysis to just the specified sites (multiple option).",
    )
    filter_group.add_argument(
        "--use-disabled-sites",
        action="store_true",
        default=settings.scan_disabled_sites,
        help="Use disabled sites to search (may cause many false positives).",
    )

    modes_group = parser.add_argument_group(
        'Operating modes',
        'Various functions except the default search by a username. '
        'Modes are executed sequentially in the order of declaration.',
    )
    modes_group.add_argument(
        "--parse",
        dest="parse_url",
        default='',
        metavar='URL',
        help="Parse page by URL and extract username and IDs to use for search.",
    )
    modes_group.add_argument(
        "--submit",
        metavar='URL',
        type=str,
        dest="new_site_to_submit",
        default=False,
        help="URL of existing profile in new site to submit.",
    )
    modes_group.add_argument(
        "--self-check",
        action="store_true",
        default=settings.self_check_enabled,
        help="Do self check for sites and database. Use --auto-disable to disable failing sites.",
    )
    modes_group.add_argument(
        "--auto-disable",
        action="store_true",
        default=False,
        help="With --self-check: automatically disable sites that fail checks.",
    )
    modes_group.add_argument(
        "--diagnose",
        action="store_true",
        default=False,
        help="With --self-check: print detailed diagnosis for each failing site.",
    )
    modes_group.add_argument(
        "--stats",
        action="store_true",
        default=False,
        help="Show database statistics (most frequent sites engines and tags).",
    )
    modes_group.add_argument(
        "--web",
        metavar='PORT',
        type=int,
        nargs='?',  # Optional PORT value
        const=settings.web_interface_port,  # PORT when `--web` is given without a value
        default=None,  # Explicitly set default to None
        help="Launch the web interface on the specified port "
        "(default: web_interface_port from settings, 5000 out of the box).",
    )
    # 断点续查（文档 §3.2.4）。默认全关：中文站扫描 5–10 分钟、中途中断是常态，
    # 但这是新增能力，不该改变老用户"不加参数就没有额外磁盘写入"的既有行为。
    checkpoint_group = parser.add_argument_group(
        'Checkpointing',
        'Save per-site progress while scanning so an interrupted scan can be resumed.',
    )
    checkpoint_group.add_argument(
        "--checkpoint",
        action="store_true",
        dest="checkpoint",
        default=False,
        help="Save progress after each checked site to "
        "~/.maigret/checkpoints/<username>-<timestamp>.jsonl. "
        "The path is printed when the scan starts.",
    )
    checkpoint_group.add_argument(
        "--resume",
        metavar='PATH',
        dest="resume",
        default=None,
        help="Resume an interrupted scan from a checkpoint file: sites already "
        "recorded there are not requested again, and their results are merged "
        "into the report. Implies --checkpoint.",
    )
    output_group = parser.add_argument_group(
        'Output options', 'Options to change verbosity and view of the console output'
    )
    # 调试开关（文档 §6.3）。写站点规则时要填 e_string/m_string，这两个值必须
    # 逐字出现在响应里；看不到原始响应就只能盲猜，填错的后果是"永远未判定"。
    output_group.add_argument(
        "--dump-response",
        action="store_true",
        dest="dump_response",
        default=False,
        help="Print the full HTTP response for every checked site. "
        "Use with --site <name> when writing or fixing a site rule.",
    )
    output_group.add_argument(
        "--cn-summary",
        action=BooleanOptionalAction,
        dest="cn_summary",
        default=True,
        help="Print the self-check summary after the scan: risk level, cross-site "
        "identity clusters, and the sites this run could not judge. On by "
        "default — it is the whole point of this fork. Use --no-cn-summary "
        "for the bare upstream output.",
    )
    output_group.add_argument(
        "--cn-exclusions",
        dest="cn_exclusions",
        default=None,
        metavar="PATH",
        help="Local false-positive exclusion table (JSON). Entries matched by it "
        "are marked in the report, never removed.",
    )
    output_group.add_argument(
        "--cn-exclusions-url",
        dest="cn_exclusions_url",
        default=os.environ.get("MAIGRET_CN_EXCLUSIONS_URL") or None,
        metavar="URL",
        help="Remote false-positive exclusion table (https only). Falls back to "
        "the local cache, then to no rules at all; never fails the run. "
        "Defaults to $MAIGRET_CN_EXCLUSIONS_URL.",
    )
    output_group.add_argument(
        "--cn-footprint",
        action="store_true",
        dest="cn_footprint",
        default=False,
        help="Also retrieve public trace content (what the username actually "
        "posted, with timestamps) from forums that allow anonymous reads. "
        "Off by default: it makes extra requests to third-party sites. "
        "Only public pages are read — no login bypass, no SERP scraping.",
    )
    output_group.add_argument(
        "--cn-forum",
        action="append",
        dest="cn_forums",
        default=None,
        metavar="HOST",
        help="Forum host to probe for traces (repeatable). Defaults to the "
        "verified list in maigret/cn/data/forums_cn.json.",
    )
    output_group.add_argument(
        "--cn-thread",
        action="append",
        dest="cn_threads",
        default=None,
        metavar="URL",
        help="Thread URL to mine for this username's posts (repeatable). "
        "This is how you feed it a lead you found yourself.",
    )
    output_group.add_argument(
        "--cn-footprint-budget",
        type=int,
        dest="cn_footprint_budget",
        default=60,
        metavar="N",
        help="Max page requests for the trace retrieval (default: 60).",
    )
    output_group.add_argument(
        "--print-not-found",
        action="store_true",
        dest="print_not_found",
        default=settings.print_not_found,
        help="Print sites where the username was not found.",
    )
    output_group.add_argument(
        "--print-errors",
        action="store_true",
        dest="print_check_errors",
        default=settings.print_check_errors,
        help="Print errors messages: connection, captcha, site country ban, etc.",
    )
    output_group.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        dest="verbose",
        default=False,
        help="Display extra information and metrics.",
    )
    output_group.add_argument(
        "--info",
        "-vv",
        action="store_true",
        dest="info",
        default=False,
        help="Display extra/service information and metrics.",
    )
    output_group.add_argument(
        "--debug",
        "-vvv",
        "-d",
        action="store_true",
        dest="debug",
        default=False,
        help="Display extra/service/debug information and metrics, save responses in debug.log.",
    )
    output_group.add_argument(
        "--no-color",
        action="store_true",
        dest="no_color",
        default=(not settings.colored_print),
        help="Don't color terminal output",
    )
    output_group.add_argument(
        "--no-progressbar",
        action="store_true",
        dest="no_progressbar",
        default=(not settings.show_progressbar),
        help="Don't show progressbar.",
    )

    report_group = parser.add_argument_group(
        'Report formats', 'Supported formats of report files'
    )
    report_group.add_argument(
        "-T",
        "--txt",
        action="store_true",
        dest="txt",
        default=settings.txt_report,
        help="Create a TXT report (one report per username).",
    )
    report_group.add_argument(
        "-C",
        "--csv",
        action="store_true",
        dest="csv",
        default=settings.csv_report,
        help="Create a CSV report (one report per username).",
    )
    report_group.add_argument(
        "-H",
        "--html",
        action="store_true",
        dest="html",
        default=settings.html_report,
        help="Create an HTML report file (general report on all usernames).",
    )
    report_group.add_argument(
        "-X",
        "--xmind",
        action="store_true",
        dest="xmind",
        default=settings.xmind_report,
        help=(
            "Generate a legacy XML XMind mindmap with a manifest for modern "
            "readers (one report per username)."
        ),
    )
    report_group.add_argument(
        "-P",
        "--pdf",
        action="store_true",
        dest="pdf",
        default=settings.pdf_report,
        help="Generate a PDF report (general report on all usernames).",
    )
    report_group.add_argument(
        "-M",
        "--md",
        action="store_true",
        dest="md",
        default=settings.md_report,
        help="Generate a Markdown report (general report on all usernames).",
    )
    report_group.add_argument(
        "-G",
        "--graph",
        action="store_true",
        dest="graph",
        default=settings.graph_report,
        help="Generate a graph report (general report on all usernames).",
    )
    report_group.add_argument(
        "--neo4j",
        action="store_true",
        dest="neo4j",
        default=settings.neo4j_report,
        help="Generate a Neo4j Cypher report (general report on all usernames).",
    )
    report_group.add_argument(
        "-J",
        "--json",
        action="store",
        metavar='TYPE',
        dest="json",
        default=settings.json_report_type,
        choices=SUPPORTED_JSON_REPORT_FORMATS,
        help=f"Generate a JSON report of specific type: {', '.join(SUPPORTED_JSON_REPORT_FORMATS)}"
        " (one report per username).",
    )

    report_group.add_argument(
        "--ai",
        action="store_true",
        dest="ai",
        default=False,
        help="Generate an AI-powered analysis of the search results using OpenAI API. "
        "Requires OPENAI_API_KEY env var or openai_api_key in settings.",
    )
    report_group.add_argument(
        "--ai-model",
        dest="ai_model",
        default=settings.openai_model,
        help="OpenAI model to use for AI analysis (default: gpt-5.4).",
    )

    parser.add_argument(
        "--reports-sorting",
        default=settings.report_sorting,
        choices=('default', 'data'),
        help="Method of results sorting in reports (default: in order of getting the result)",
    )
    return parser


async def load_cn_exclusions(args, logger):
    """装配排除表：本地 > 远程 > 内置。

    只在确实要用的时候才去碰网络：用户既没给本地表也没给远程地址时，
    这里返回一个空表（而不是 None），让调用方的分支少一层。
    **任何失败都降级**——排除表是可选增强，不该让一次自查跑不起来。
    """
    from .cn.exclusions import build, load_remote

    remote_rules, remote_warnings = [], []
    if getattr(args, "cn_exclusions_url", None):
        remote_rules, remote_warnings = await load_remote(
            args.cn_exclusions_url, cache_path=None
        )
        for warning in remote_warnings:
            # 用 warning 而不是 debug：远程表决定了"哪些结果会被标成误报"，
            # 加载失败时用户必须知道这次看到的是本地规则还是空规则。
            logger.warning("排除表：%s", warning)

    try:
        table = build(
            local_path=getattr(args, "cn_exclusions", None),
            remote_rules=remote_rules,
            remote_warnings=remote_warnings,
        )
    except Exception as exc:
        # 本地表格式写错是最常见的用户失误，不能因此中断整个扫描。
        from .cn.exclusions import ExclusionTable

        logger.warning("排除表加载失败（%s），本次不排除任何结果", exc)
        return ExclusionTable(rules=[], warnings=[f"排除表加载失败：{exc}"])

    return table


def print_cn_summary(username: str, results: Dict[str, Any], table=None, logger=None) -> str:
    """输出单个用户名的自查结论块，返回实际打印的文本（空串表示没打印）。

    返回文本是为了让调用方/测试不必去抓 stdout：这个函数只做渲染和打印，
    判断"该不该打印"的责任留在外面。
    """
    from .cn.hook import MODE_OFF
    from .cn.hook import mode as cn_verdict_mode
    from .cn.report import apply_exclusions, build_summary

    logger = logger or logging.getLogger('maigret')

    # `MAIGRET_CN_VERDICT=0` 是整体回退开关，回退就该回到上游行为：
    # 判定关掉之后 `status.reason` 不再有值（那是本分支的产物），
    # 汇总会把所有非命中站点统统归到"没有可用规则"——比不输出更误导。
    if cn_verdict_mode() == MODE_OFF:
        logger.debug('MAIGRET_CN_VERDICT=0，跳过自查结论')
        return ""

    if not results:
        return ""

    try:
        excluded = apply_exclusions(results, table)
        summary = build_summary(results, username=username, excluded=excluded, table=table)
    except Exception as exc:
        # 汇总层出问题绝不能吞掉已经跑完的扫描结果：报告文件仍然照常写盘。
        logger.debug("自查结论生成失败：%s", exc, exc_info=True)
        return ""

    if not summary:
        return ""

    print()
    print(summary)
    return summary


def claimed_pages(results: Dict[str, Any], limit: int = 20) -> list:
    """从扫描结果里挑出"已确认账号的主页"，供痕迹检索用。

    为什么会走到这一步：论坛帖子需要先找到 URL，而"列出某用户发过的帖子"
    被 Discuz 服务端禁用（多个参数组合、多个站点一致）、搜索引擎结果页
    被 robots 禁止（360 `Disallow: /s`、百度与 Bing `Disallow: /search`）。
    也就是说**工具无法自己发现"他发过什么"**，但它知道"他在哪些站有账号"——
    那些主页上的列表，是唯一不需要人肉找就能拿到的内容源。

    只取 CLAIMED 的，且跳过明显没有列表的（如纯 API 地址）。
    """
    pages = []
    seen = set()
    for site_name, result in (results or {}).items():
        status = result.get('status')
        value = getattr(status, 'status', status)
        value = getattr(value, 'value', value)
        if str(value).lower() != 'claimed':
            continue

        url = result.get('url_user') or result.get('url_probe') or ''
        if not url.startswith('http'):
            continue
        # API 端点回的是 JSON，抽不出条目，白白消耗一次请求
        from urllib.parse import urlparse

        host = urlparse(url).netloc.lower()
        if host.startswith('api.') or '/api/' in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        pages.append((str(site_name), url))
        if len(pages) >= limit:
            break
    return pages


async def print_cn_footprint(
    username: str,
    results=None,
    forums=None,
    threads=None,
    budget: int = 60,
    logger=None,
) -> str:
    """跑一轮公开痕迹检索并打印，返回打印出的文本（空串表示没打印）。

    为什么单独一个函数而不是并进 `print_cn_summary`：
        - 那个函数是**离线**的（只汇总已有结果），这个要**发网络请求**
        - 两者失败代价不同：汇总失败只是少一段文字，检索失败可能是
          请求打到了别人服务器上却没拿到东西。分开才好各自处理。
        - 默认关闭。它会对第三方站点发额外请求，不该在用户没要求时发生。

    是 async 的：调用点本身就在事件循环里（扫描主流程），
    在里面 `asyncio.run()` 会直接抛 "cannot be called from a running event loop"。
    """
    logger = logger or logging.getLogger('maigret')

    try:
        from .cn.footprint import render, search_footprints
    except ImportError as exc:  # pragma: no cover
        logger.debug('痕迹检索模块不可用：%s', exc)
        return ""

    try:
        import aiohttp

        # 已确认账号的主页：不需要额外"发现"，扫描结果里就有。
        # 这是没有种子 URL 时唯一的产出源，所以要显式传进去。
        pages = claimed_pages(results or {})

        # trust_env=False：走干净会话，不继承环境里的代理设置，
        # 与其他执行器（executors/search.py）同一口径。
        async with aiohttp.ClientSession(trust_env=False) as session:
            summary = await search_footprints(
                username,
                session,
                sites=forums or None,
                seed_urls=threads or (),
                pages=pages,
                max_requests=max(1, int(budget)),
            )
    except Exception as exc:  # noqa: BLE001 — 绝不因为痕迹检索失败而吞掉扫描结果
        logger.warning('公开痕迹检索失败：%s', exc)
        print()
        print(f"公开痕迹检索：{username}")
        print("─" * 40)
        print(f"  检索未完成：{exc}")
        return ""

    text = render(summary)
    if not text:
        return ""
    print()
    print(text)
    return text


def save_db_safely(db: MaigretDatabase, db_file: str, logger) -> bool:
    """Persist the sites database, tolerating a read-only installation.

    The bundled database lives inside the package, so on a system-wide
    install (distro package, snap, /nix/store) its directory belongs to
    root or is mounted read-only. Returns False instead of raising, so a
    run that already produced its report doesn't die on a cache write.
    """
    try:
        db.save_to_file(db_file)
        return True
    except OSError as e:
        logger.debug(f'Could not write the database to {db_file}: {e}')
        return False


async def main():
    # Logging
    log_level = logging.ERROR
    logging.basicConfig(
        format='[%(filename)s:%(lineno)d] %(levelname)-3s  %(asctime)s %(message)s',
        datefmt='%H:%M:%S',
        level=log_level,
    )
    logger = logging.getLogger('maigret')
    logger.setLevel(log_level)

    # Load settings
    settings = Settings()
    settings_loaded, err = settings.load()

    if not settings_loaded:
        # 只有"文件存在但读不动"才该退出：那说明用户明确写了一份配置，
        # 而我们看不懂它，继续跑等于拿默认值静默覆盖用户的意图。
        #
        # "一份配置文件都没有"不是错误：`settings.json` 在 .gitignore 里，
        # 全新 clone 的仓库里本来就没有它。`Settings.__init__` 已经铺好一套
        # 可用的默认值（见 settings.DEFAULT_SETTINGS），此时应当继续。
        # 原来这里一律 sys.exit(3)，导致源码部署的用户第一条命令就是
        # "None of the default settings files found" 加退出码 3。
        if isinstance(err, Exception):
            logger.error(err)
            sys.exit(3)
        logger.debug(err)

    arg_parser = setup_arguments_parser(settings)
    args = arg_parser.parse_args()

    input_entries: List[Tuple[str, Optional[str]]] = []
    if args.input_file:
        try:
            input_entries = read_input_file(args.input_file, SUPPORTED_IDS)
        except OSError as e:
            arg_parser.error(f"can't read input file: {e}")

    # Resolve Cloudflare webgate config (CLI flag OR settings.cloudflare_bypass.enabled)
    cf_bypass_config = build_cloudflare_bypass_config(
        settings, force_enable=args.cloudflare_bypass
    )
    if cf_bypass_config:
        modules_summary = ", ".join(
            f"{m.get('name', m.get('method'))}({m.get('url')})"
            for m in cf_bypass_config["modules"]
        )
        logger.info(
            f"Cloudflare webgate active: triggers={cf_bypass_config['trigger_protection']}, "
            f"modules=[{modules_summary}]"
        )

    # Re-set logging level based on args
    if args.debug:
        log_level = logging.DEBUG
    elif args.info:
        log_level = logging.INFO
    elif args.verbose:
        log_level = logging.WARNING
    logger.setLevel(log_level)

    # Usernames initial list
    usernames = {
        u: args.id_type
        for u in args.username
        if u and u not in ['-'] and u not in args.ignore_ids_list
    }
    original_usernames = ""
    if args.permute and len(usernames) > 1 and args.id_type == 'username':
        original_usernames = " ".join(usernames.keys())
        usernames = Permute(usernames).gather(method='strict')

    # Added after permutation on purpose: a file can hold thousands of names and
    # ids of several types, and permuting those makes no sense.
    for value, id_type in input_entries:
        if value not in args.ignore_ids_list:
            usernames[value] = id_type or args.id_type

    parsing_enabled = not args.disable_extracting
    recursive_search_enabled = not args.disable_recursive_search

    # Make prompts
    if args.proxy is not None:
        print("Using the proxy: " + args.proxy)


    if args.parse_url:
        extracted_ids = extract_ids_from_page(
            args.parse_url, logger, timeout=args.timeout
        )
        usernames.update(extracted_ids)

    if args.tags:
        args.tags = list(set(str(args.tags).split(',')))

    if args.exclude_tags:
        args.exclude_tags = list(set(str(args.exclude_tags).split(',')))
    else:
        args.exclude_tags = []

    from .db_updater import resolve_db_path, force_update, BUNDLED_DB_PATH

    if args.force_update:
        force_update(
            meta_url=settings.db_update_meta_url,
            color=not args.no_color,
            proxy=args.proxy,
        )

    try:
        db_file = resolve_db_path(
            db_file_arg=args.db_file,
            no_autoupdate=args.no_autoupdate or args.force_update,
            meta_url=settings.db_update_meta_url,
            check_interval_hours=settings.autoupdate_check_interval_hours,
            color=not args.no_color,
            proxy=args.proxy,
        )
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(2)

    if args.top_sites == 0 or args.all_sites:
        args.top_sites = sys.maxsize

    # Create notify object for query results.
    query_notify = QueryNotifyPrint(
        result=None,
        verbose=args.verbose,
        print_found_only=not args.print_not_found,
        skip_check_errors=not args.print_check_errors,
        color=not args.no_color,
        silent=args.ai,
    )

    if args.enrich:
        query_notify.enrich(
            "--enrich is experimental feature, it might make extra requests to get more information"
        )

    print_intro_banner(no_color=args.no_color, silent=args.ai)
    print_donate_banner(no_color=args.no_color, silent=args.ai)

    # Create object with all information about sites we are aware of.
    try:
        db = MaigretDatabase().load_from_path(db_file, proxy=args.proxy)
        query_notify.success(f'Using sites database: {db_file} ({len(db.sites)} sites)')
    except Exception as e:
        logger.warning(f"Failed to load database from {db_file}: {e}")
        if db_file != BUNDLED_DB_PATH:
            query_notify.warning(
                f'Falling back to bundled database: {BUNDLED_DB_PATH}'
            )
            db = MaigretDatabase().load_from_path(BUNDLED_DB_PATH)
            query_notify.success(
                f'Using sites database: {BUNDLED_DB_PATH} ({len(db.sites)} sites)'
            )
        else:
            raise

    # 只存在于 cn overlay 的站点（小红书、B 站搜索…）先注册进库，
    # 否则 `--site "Bilibili (搜索)"` 会在库里找不到，直接"没有要查的站点"。
    from .cn.hook import register_overlay_sites

    register_overlay_sites(db)

    activation_baseline = load_activation_cache(db, logger)

    get_top_sites_for_id = lambda x: db.ranked_sites_dict(
        top=args.top_sites,
        tags=args.tags,
        excluded_tags=args.exclude_tags,
        names=args.site_list,
        disabled=args.use_disabled_sites,
        id_type=x,
    )

    site_data = get_top_sites_for_id(args.id_type)

    # cn overlay：把人工策展的中文站强制并入本次扫描（文档 §5.2 force_include）。
    # 上游的 top 截断按 alexaRank 排序，而 Alexa 2022 年已停服、排序数据失效，
    # 中文站大多没有 alexaRank，永远进不了默认范围 —— 这是"默认扫描只有 5 个
    # 中国站"的根因。force_include 的语义就是不参与排序、直接进。
    #
    # 用户用 --site 显式点名站点时不并入：那是"我只想查这几个站"的明确意图，
    # 再塞 19 个进去等于把 --site 当摆设。
    if not args.site_list:
        site_data = augment_sites(
            site_data,
            db,
            args.id_type,
            tags=args.tags,
            excluded_tags=args.exclude_tags,
        )

    if args.new_site_to_submit:
        submitter = Submitter(db=db, logger=logger, settings=settings, args=args)
        is_submitted = await submitter.dialog(args.new_site_to_submit, args.cookie_file)
        if is_submitted and not save_db_safely(db, db_file, logger):
            query_notify.warning(
                f'The new site was not saved: {db_file} is not writable'
            )
        await submitter.close()

    # Database self-checking
    if args.self_check:
        if len(site_data) == 0:
            query_notify.warning(
                'No sites to self-check with the current filters! Exiting...'
            )
            return

        query_notify.success(
            f'Maigret sites database self-check started for {len(site_data)} sites...'
        )
        check_result = await self_check(
            db,
            site_data,
            logger,
            proxy=args.proxy,
            max_connections=args.connections,
            tor_proxy=args.tor_proxy,
            i2p_proxy=args.i2p_proxy,
            cookies=args.cookie_file,
            auto_disable=args.auto_disable,
            diagnose=args.diagnose,
            no_progressbar=args.no_progressbar,
            cloudflare_bypass=cf_bypass_config,
            dns_resolver=args.dns_resolver,
        )

        is_need_update = check_result.get('needs_update', False)

        if is_need_update:
            if input('Do you want to save changes permanently? [Yn]\n').lower() in (
                'y',
                '',
            ):
                if save_db_safely(db, db_file, logger):
                    print('Database was successfully updated.')
                else:
                    print(f'Database was not updated: {db_file} is not writable.')
            else:
                print('Updates will be applied only for current search session.')

        if args.verbose or args.debug:
            query_notify.info(
                'Scan sessions flags stats: ' + str(db.get_scan_stats(site_data))
            )

    # Database statistics
    if args.stats:
        print(db.get_db_stats())

    report_dir = path.join(os.getcwd(), args.folderoutput)

    # Make reports folder is not exists
    try:
        os.makedirs(report_dir, exist_ok=True)
    except OSError as e:
        logger.error(str(e))
        query_notify.warning(
            f'Could not create the reports directory {report_dir}: {e.strerror}.', '!'
        )
        if os.environ.get('SNAP'):
            query_notify.warning(
                'The snap can only write under your home directory or a connected '
                'removable drive. Run maigret from a folder under your home, '
                'or pass -fo PATH.',
                '!',
            )
        else:
            query_notify.warning(
                'Run maigret from a writable directory, or pass -fo PATH.', '!'
            )
        sys.exit(2)

    # Define one report filename template
    report_filepath_tpl = path.join(report_dir, 'report_{username}{postfix}')

    # Web interface
    if args.web is not None:
        from maigret.web.app import app

        app.config["MAIGRET_DB_FILE"] = db_file

        # args.web is either the specified port or web_interface_port from settings
        port = args.web if args.web else settings.web_interface_port

        # Host configuration: secure by default, but allow override via environment
        host = os.getenv('FLASK_HOST', '127.0.0.1')
        app.run(host=host, port=port)
        return

    if usernames == {}:
        # magic params to exit after init
        query_notify.warning('No usernames to check, exiting.')
        sys.exit(0)

    if len(usernames) > 1 and args.permute and args.id_type == 'username':
        query_notify.warning(
            f"{len(usernames)} permutations from {original_usernames} to check..."
            + get_dict_ascii_tree(usernames, prepend="\t")
        )

    if args.ai:
        from .ai import resolve_api_key

        if not resolve_api_key(settings):
            query_notify.warning(
                'AI analysis requires an OpenAI API key. '
                'Set OPENAI_API_KEY environment variable or add '
                'openai_api_key to settings.json.'
            )
            sys.exit(1)

    if not site_data:
        query_notify.warning('No sites to check, exiting!')
        sys.exit(2)

    if args.ai:
        query_notify.warning(
            f'Starting AI-assisted search on top {len(site_data)} sites from the Maigret database...'
        )
    else:
        query_notify.warning(
            f'Starting a search on top {len(site_data)} sites from the Maigret database...'
        )
        if not args.all_sites:
            query_notify.warning(
                'You can run search by full list of sites with flag `-a`', '!'
            )

    already_checked = set()
    general_results = []
    interrupted = False

    # Install our own SIGINT handler. asyncio.run installs a default one
    # that cancels the main task on first Ctrl+C and raises KeyboardInterrupt
    # on the second — but the cancellation gets buried inside the executor's
    # gather/queue cleanup and doesn't bubble up to our `except CancelledError`
    # below until a SECOND press. Owning the signal directly lets us:
    #   1) cancel the in-flight search task on the FIRST press,
    #   2) fall through to report generation,
    #   3) exit hard on a second press if the user changes their mind.
    import signal as _signal
    _interrupt_count = [0]
    _current_search_task: List[Any] = [None]
    _orig_sigint = _signal.getsignal(_signal.SIGINT)

    def _on_sigint(signum, frame):
        _interrupt_count[0] += 1
        task = _current_search_task[0]
        if _interrupt_count[0] == 1 and task is not None and not task.done():
            # First press: cancel the running search. The cancel propagates
            # as CancelledError to `await maigret(...)` below.
            task.cancel()
            return
        # Second press (or no task running): hard exit. Restore the previous
        # handler and re-raise so __main__.py prints "Maigret interrupted."
        # with exit code 130.
        _signal.signal(_signal.SIGINT, _orig_sigint)
        raise KeyboardInterrupt()

    _signal.signal(_signal.SIGINT, _on_sigint)

    # 排除表只在整轮运行里装配一次，不放进循环：
    # 它是"规则"而不是"本次查询的状态"。每查一个用户名重新拉一遍远程表
    # 既慢，也会让同一批结果套用不同版本的规则——用户看到的两份报告
    # 会互相矛盾，而矛盾的那份看起来就像"漏查了"。
    cn_exclusion_table = None
    if args.cn_summary:
        cn_exclusion_table = await load_cn_exclusions(args, logger)

    while usernames:
        username, id_type = list(usernames.items())[0]
        del usernames[username]

        # First Ctrl+C is caught below and sets `interrupted`. Stop pulling
        # new targets from the queue so we fall through to report generation
        # with whatever has already been collected.
        if interrupted:
            break

        if username.lower() in already_checked:
            continue

        already_checked.add(username.lower())

        if username in args.ignore_ids_list:
            query_notify.warning(
                f'Skip a search by username {username} cause it\'s marked as ignored.'
            )
            continue

        # check for characters do not supported by sites generally
        found_unsupported_chars = set(BAD_CHARS).intersection(set(username))
        if found_unsupported_chars:
            pretty_chars_str = ','.join(
                map(lambda s: f'"{s}"', found_unsupported_chars)
            )
            query_notify.warning(
                f'Found unsupported URL characters: {pretty_chars_str}, skip search by username "{username}"'
            )
            continue

        sites_to_check = get_top_sites_for_id(id_type)

        # Wrap the per-target search in an asyncio.Task so our SIGINT handler
        # can cancel exactly THIS coroutine on first Ctrl+C — not the parent
        # main task, which would also tear down report generation.
        # `partial_results` is the output container that maigret() mutates as
        # site checks complete; on Ctrl+C cancellation it still holds the
        # checks that finished before the cancel, so partial state survives.
        partial_results: Dict[str, SiteResult] = {}
        search_task = asyncio.ensure_future(maigret(
            username=username,
            site_dict=dict(sites_to_check),
            query_notify=query_notify,
            proxy=args.proxy,
            tor_proxy=args.tor_proxy,
            i2p_proxy=args.i2p_proxy,
            timeout=args.timeout,
            is_parsing_enabled=parsing_enabled,
            is_enrich_enabled=args.enrich,
            id_type=id_type,
            debug=args.verbose,
            logger=logger,
            cookies=args.cookie_file,
            forced=args.use_disabled_sites,
            max_connections=args.connections,
            no_progressbar=args.no_progressbar,
            retries=args.retries,
            check_domains=args.with_domains,
            cloudflare_bypass=cf_bypass_config,
            keywords=getattr(args, 'keywords', []),
            dns_resolver=args.dns_resolver,
            output_container=partial_results,
            # 断点续查：两个开关直接透传，路径在 checking 里生成（那里才知道
            # 实际查询的用户名，permutation / --input-file 之后可能与 args 不同）
            checkpoint=args.checkpoint,
            resume=args.resume,
            dump_response=args.dump_response,
        ))
        _current_search_task[0] = search_task
        try:
            results = await search_task
        except (asyncio.CancelledError, KeyboardInterrupt):
            # First Ctrl+C cancelled `search_task` via our SIGINT handler.
            # Keep whatever partial site checks completed before cancellation
            # so the user gets a meaningful report rather than "0 results".
            interrupted = True
            _current_search_task[0] = None
            results = partial_results
            partial_count = len(partial_results)
            remaining = len(usernames) + 1
            general_results.append((username, id_type, results))
            total_so_far = sum(len(d) for _, _, d in general_results)
            query_notify.warning(
                f'Search interrupted by user (Ctrl+C). Kept {partial_count} '
                f'partial site result(s) for "{username}"; skipping {remaining} '
                f'remaining target(s); generating report from '
                f'{total_so_far} total site result(s) across '
                f'{len(general_results)} username(s). '
                f'Press Ctrl+C again to exit without a report.',
                symbol='!',
            )
            # 中断后仍然给结论：用户按 Ctrl+C 是因为等不及，不是不要结果。
            # 而且此时盲区信息恰恰最有用——它说明了为什么结果看起来这么少。
            if args.cn_summary and not args.ai:
                print_cn_summary(username, results, cn_exclusion_table, logger=logger)
            break
        finally:
            _current_search_task[0] = None

        if not args.ai:
            errs = errors.notify_about_errors(
                results, query_notify, show_statistics=args.verbose
            )
            for e in errs:
                query_notify.warning(*e)

        if args.reports_sorting == "data":
            results = sort_report_by_data_points(results)

        general_results.append((username, id_type, results))

        # 自查结论：放在这里而不是最后统一打印，是因为它按用户名组织，
        # 多个用户名时应该紧跟着各自的扫描结果出现。`--ai` 模式下不打印，
        # 与下面的短文本报告保持一致（避免同一份结论出现两次）。
        if args.cn_summary and not args.ai:
            print_cn_summary(username, results, cn_exclusion_table, logger=logger)

        # 公开痕迹检索：默认关闭，`--cn-footprint` 才跑。放在自查结论之后，
        # 因为它是"结论之外还能看到什么"，而不是结论本身。
        if args.cn_footprint and not args.ai:
            await print_cn_footprint(
                username,
                results=results,
                forums=args.cn_forums,
                threads=args.cn_threads,
                budget=args.cn_footprint_budget,
                logger=logger,
            )

        if recursive_search_enabled:
            extracted_ids = extract_ids_from_results(results, db)
            query_notify.warning(f'Extracted IDs: {extracted_ids}')
            usernames.update(extracted_ids)

        # reporting for a one username
        if args.xmind:
            username = username.replace('/', '_')
            filename = report_filepath_tpl.format(username=username, postfix='.xmind')
            save_xmind_report(filename, username, results)
            query_notify.warning(f'XMind report for {username} saved in {filename}')

        if args.csv:
            username = username.replace('/', '_')
            filename = report_filepath_tpl.format(username=username, postfix='.csv')
            save_csv_report(filename, username, results)
            query_notify.warning(f'CSV report for {username} saved in {filename}')

        if args.txt:
            username = username.replace('/', '_')
            filename = report_filepath_tpl.format(username=username, postfix='.txt')
            save_txt_report(filename, username, results)
            query_notify.warning(f'TXT report for {username} saved in {filename}')

        if args.json:
            username = username.replace('/', '_')
            filename = report_filepath_tpl.format(
                username=username, postfix=f'_{args.json}.json'
            )
            save_json_report(filename, username, results, report_type=args.json)
            query_notify.warning(
                f'JSON {args.json} report for {username} saved in {filename}'
            )

    # Restore the original SIGINT handler before report generation. From
    # here on, a Ctrl+C should NOT cancel-and-continue — it should exit.
    _signal.signal(_signal.SIGINT, _orig_sigint)

    # reporting for all the result
    if general_results:
        if interrupted:
            # "Partial results from N username(s)" — disambiguates from the
            # per-site count, which would read confusingly as "N partial
            # results" when N is the username count.
            query_notify.info(
                f'Generating partial results from {len(general_results)} '
                f'username(s) after Ctrl+C...'
            )
        if args.html or args.pdf or args.md:
            query_notify.warning('Generating report info...')
        report_context = generate_report_context(general_results)
        # determine main username
        username = report_context['username']

        if args.html:
            username = username.replace('/', '_')
            filename = report_filepath_tpl.format(
                username=username, postfix='_plain.html'
            )
            save_html_report(filename, report_context)
            query_notify.warning(f'HTML report on all usernames saved in {filename}')

        if args.pdf:
            username = username.replace('/', '_')
            filename = report_filepath_tpl.format(username=username, postfix='.pdf')
            try:
                save_pdf_report(filename, report_context)
            except RuntimeError as e:
                query_notify.warning(str(e))
            else:
                query_notify.warning(
                    f'PDF report on all usernames saved in {filename}'
                )

        if args.md:
            username = username.replace('/', '_')
            filename = report_filepath_tpl.format(username=username, postfix='.md')
            run_flags = []
            if args.tags:
                run_flags.append(f"--tags {args.tags}")
            if args.site_list:
                run_flags.append(f"--site {','.join(args.site_list)}")
            if args.all_sites:
                run_flags.append("--all-sites")
            run_info = {
                "sites_count": sum(len(d) for _, _, d in general_results),
                "flags": " ".join(run_flags) if run_flags else None,
            }
            save_markdown_report(filename, report_context, run_info=run_info)
            query_notify.warning(f'Markdown report on all usernames saved in {filename}')

        if args.graph:
            username = username.replace('/', '_')
            filename = report_filepath_tpl.format(
                username=username, postfix='_graph.html'
            )
            save_graph_report(filename, general_results, db)
            query_notify.warning(f'Graph report on all usernames saved in {filename}')

        if args.neo4j:
            username = username.replace('/', '_')
            filename = report_filepath_tpl.format(
                username=username, postfix='_neo4j.cypher'
            )
            save_neo4j_report(filename, general_results, db)
            query_notify.warning(f'Neo4j report on all usernames saved in {filename}')

        if not args.ai:
            text_report = get_plaintext_report(report_context)
            if text_report:
                query_notify.info('Short text report:')
                print(text_report)

        if args.ai:
            from .ai import get_ai_analysis, resolve_api_key
            from .report import generate_markdown_report

            api_key = resolve_api_key(settings)

            run_flags = []
            if args.tags:
                run_flags.append(f"--tags {args.tags}")
            if args.site_list:
                run_flags.append(f"--site {','.join(args.site_list)}")
            if args.all_sites:
                run_flags.append("--all-sites")
            run_info = {
                "sites_count": sum(len(d) for _, _, d in general_results),
                "flags": " ".join(run_flags) if run_flags else None,
            }

            md_report = generate_markdown_report(report_context, run_info=run_info)

            try:
                await get_ai_analysis(
                    api_key=api_key,
                    markdown_report=md_report,
                    model=args.ai_model,
                    api_base_url=getattr(
                        settings, 'openai_api_base_url', 'https://api.openai.com/v1'
                    ),
                )
            except Exception as e:
                query_notify.warning(f'AI analysis failed: {e}')

    save_activation_cache(db, activation_baseline, logger)


def run():
    try:
        if sys.version_info.minor >= 10:
            asyncio.run(main())
        else:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(main())
    except KeyboardInterrupt:
        print('Maigret is interrupted.')
        sys.exit(1)


if __name__ == "__main__":
    run()
