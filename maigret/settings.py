import os
import os.path as path
import json
from typing import List

SETTINGS_FILES_PATHS = [
    path.join(path.dirname(path.realpath(__file__)), "resources/settings.json"),
    # System-wide policy. Lets an administrator or a distribution package
    # configure Maigret without editing inside the installed package, whose
    # directory is read-only on a system-wide install anyway.
    '/etc/maigret/settings.json',
    path.expanduser('~/.maigret/settings.json'),
    path.join(os.getcwd(), 'settings.json'),
]

# 兜底默认值。
#
# 为什么需要这一层：`settings.json` 在 .gitignore 里（第 41 行），全新 clone
# 的仓库里根本没有这个文件，而 `Settings` 只有类级类型注解、没有实际默认值。
# 结果是 `python -m maigret --help` 在参数解析阶段就抛
# `AttributeError: 'Settings' object has no attribute 'timeout'` —— 用户还没
# 开始用就先看到一个 traceback。
#
# 本项目的分发形态是"源码自行部署"（不提供安装包），用户的第一条命令就是
# `python -m maigret`。它必须能跑起来。配置文件仍然优先：`load()` 会用文件
# 内容覆盖这里的任何键。
DEFAULT_SETTINGS = {
    # 下面这些数值不是拍脑袋定的：`tests/test_cli.py` 的 DEFAULT_ARGS 就是
    # 本项目对"没有配置文件时各参数默认值"的成文契约。原来的 settings.json
    # 不进版本库（.gitignore 第 41 行），导致这份契约失去了唯一的事实来源，
    # 只能由测试来锚定。改这里的任何数值前，先看 DEFAULT_ARGS 是否也跟着改。
    "retries_count": 0,
    # 必须是字面量 "resources/data.json"，不能换成绝对路径：
    # `db_updater.resolve_db_path()` 靠 `db_file_arg == "resources/data.json"`
    # 判断"这是默认库"，从而走自动更新 + `_best_local()` 的兜底解析。
    # 换成绝对路径会掉进"用户自定义库"分支，行为完全不同（不再自动更新）。
    "sites_db_path": "resources/data.json",
    "timeout": 30,
    "max_connections": 100,
    "recursive_search": True,
    "info_extracting": True,
    "cookie_jar_file": None,
    "ignore_ids_list": [],
    "reports_path": "reports",
    "proxy_url": None,
    "tor_proxy_url": "socks5://127.0.0.1:9050",
    "i2p_proxy_url": "http://127.0.0.1:4444",
    "domain_search": False,
    "scan_all_sites": False,
    "top_sites_count": 500,
    "scan_disabled_sites": False,
    "scan_sites_list": [],
    "self_check_enabled": False,
    "print_not_found": False,
    "print_check_errors": False,
    "colored_print": True,
    "show_progressbar": True,
    "report_sorting": "default",
    "json_report_type": "",
    "txt_report": False,
    "csv_report": False,
    "xmind_report": False,
    "pdf_report": False,
    "html_report": False,
    "graph_report": False,
    "neo4j_report": False,
    "md_report": False,
    "web_interface_port": 5000,
    "no_autoupdate": False,
    "db_update_meta_url": (
        "https://raw.githubusercontent.com/soxoj/maigret/main/"
        "maigret/resources/db_meta.json"
    ),
    "autoupdate_check_interval_hours": 24,
    "cloudflare_bypass": {},
    "openai_api_key": "",
    "openai_model": "gpt-5.4",
    "presence_strings": [],
    "supposed_usernames": [],
}


class Settings:
    # main maigret setting
    retries_count: int
    sites_db_path: str
    timeout: int
    max_connections: int
    recursive_search: bool
    info_extracting: bool
    cookie_jar_file: str
    ignore_ids_list: List
    reports_path: str
    proxy_url: str
    tor_proxy_url: str
    i2p_proxy_url: str
    domain_search: bool
    scan_all_sites: bool
    top_sites_count: int
    scan_disabled_sites: bool
    scan_sites_list: List
    self_check_enabled: bool
    print_not_found: bool
    print_check_errors: bool
    colored_print: bool
    show_progressbar: bool
    report_sorting: str
    json_report_type: str
    txt_report: bool
    csv_report: bool
    xmind_report: bool
    pdf_report: bool
    html_report: bool
    graph_report: bool
    neo4j_report: bool
    md_report: bool
    web_interface_port: int
    no_autoupdate: bool
    db_update_meta_url: str
    autoupdate_check_interval_hours: int
    cloudflare_bypass: dict
    openai_api_key: str
    openai_model: str

    # submit mode settings
    presence_strings: list
    supposed_usernames: list

    def __init__(self):
        # 先铺默认值，再由 load() 覆盖。这样"没有配置文件"是可用状态，
        # 而不是每个属性访问都可能 AttributeError。
        self.__dict__.update(DEFAULT_SETTINGS)

    def load(self, paths=None):
        was_inited = False

        if not paths:
            paths = SETTINGS_FILES_PATHS

        for filename in paths:
            data = {}

            try:
                with open(filename, "r", encoding="utf-8") as file:
                    data = json.load(file)
            except FileNotFoundError:
                # treast as a normal situation
                pass
            except Exception as error:
                return False, ValueError(
                    f"Problem with parsing json contents of "
                    f"settings file '{filename}':  {str(error)}."
                )

            self.__dict__.update(data)
            if data:
                was_inited = True

        return (
            was_inited,
            f'None of the default settings files found: {", ".join(paths)}',
        )

    @property
    def json(self):
        return self.__dict__
