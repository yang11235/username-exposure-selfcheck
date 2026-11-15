"""多格式互转：WMN / Sherlock / Maigret ↔ 本项目 v2 四字段格式。

为什么需要这一层（文档 §3.4①）：

    WhatsMyName 691 个站点、跑了 11 年，2026-11-09 归档关闭，作者原话是
    "维护数百个站点检测超出承载力"。它的格式本身没问题，问题是同一批站点
    要在三个生态里各写一遍——WMN 用 `uri_check`/`e_code`，Sherlock 用
    `url`/`errorType`，Maigret 用 `url`/`checkType`。

所以这里的方向是**吸收**而不是另起一套：别人的规则一条命令搬进来，
我们的规则也能导回去。站点数不是资产而是负债（文档 §3.4①），
能不能低成本地复用别人已经验证过的规则，决定了这个项目活不活得下去。

三条硬约束：

    1. **不猜**。目标格式表达不了的语义，输出 `valid: false` + `_note` 说明，
       绝不产出一条"看起来能用、实际会误判"的规则。一条错规则的代价
       远高于一条缺规则：前者让用户以为查过了。
    2. **占位符统一**。WMN 用 `{account}`，Sherlock 用 `{}`，
       Maigret 用 `{username}`，v2 统一 `{account}`。
    3. **不静默丢字段**。没有对应字段的信息（Sherlock 的 `regexCheck`、
       `errorUrl`）写进 `_note`。静默丢弃会让用户以为转换是无损的。
"""

import logging
from typing import Any, Dict, Mapping

from ..model import Site

logger = logging.getLogger(__name__)

ACCOUNT_PLACEHOLDER = "{account}"

# 各家的占位符写法。顺序有意义：先换长的，再换短的。
# `{account}` 里不含 `{}` 子串，所以这样替换不会互相吃掉。
_FOREIGN_PLACEHOLDERS = ("{username}", "{}")

FORMATS = ("v2", "wmn", "sherlock", "maigret")

# 导入时附到 `_note` 里的固定提示
NOTE_UNVERIFIED = "导入的规则未经实测验证，使用前请用 utils/cn_selfcheck.py 跑一遍"

# 写进 v2 文件时保留的键。
#
# 为什么需要这个白名单：`overlay.adapt_upstream` 是"以 raw 为底、未知字段原样
# 保留"（那是给内存合并用的，避免 overrides 里写了却静默失效）。但把它直接
# 落盘成 sites_cn.json 会出问题——文件里会同时存在 `checkType`/`presenseStrs`
# （上游语义）和 `e_code`/`e_string`（v2 语义），读的人分不清哪个是准的；
# 更糟的是 `deep_merge(adapted, cn_sites[name])` 会让这份**过期副本**覆盖
# 上游后来的修改。所以落盘前收敛到 v2 自己的字段集。
V2_FIELDS = frozenset(
    {
        "urlMain",
        "url_main",
        "cat",
        "tags",
        "id_type",
        "uri_check",
        "uri_pretty",
        "strip_bad_char",
        "method",
        "post_body",
        "headers",
        "e_code",
        "e_string",
        "e_strings",
        "m_code",
        "m_string",
        "m_strings",
        "executor",
        "bootstrap_url",
        "search_handle_pattern",
        "search_empty_marker",
        "search_profile_url",
        "signature",
        "requires",
        "protection",
        "extract",
        "known",
        "known_absent",
        "confidence",
        "valid",
        "verified_at",
        "verified_source",
        "force_include",
        "default_include",
        "alexa_rank",
        "alexaRank",
        "disabled",
        "source",
        "_note",
    }
)


def strip_to_v2(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """只留 v2 认识的键。未知键丢弃——但 `_note` 里已经写明了哪些信息没带过来。"""
    return {key: value for key, value in raw.items() if key in V2_FIELDS}


def to_account_placeholder(value: Any) -> Any:
    """把任何来源的占位符统一成 `{account}`，递归处理 dict / list。

    post_body 里也会出现占位符，所以必须递归——只换顶层 URL 会让 POST 型
    站点在导入后静默失效（URL 是固定端点，占位符在 body 里）。
    """
    if isinstance(value, str):
        for foreign in _FOREIGN_PLACEHOLDERS:
            value = value.replace(foreign, ACCOUNT_PLACEHOLDER)
        return value
    if isinstance(value, dict):
        return {key: to_account_placeholder(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_account_placeholder(item) for item in value]
    return value


def parse_form_body(body: Any) -> Any:
    """WMN 的 `post_body` 是 query string，v2 要 dict。

    `"login=x&password=y"` → `{"login": "x", "password": "y"}`。
    空串按"没有 body"处理（WMN 里大量条目是 `"post_body": ""`）。
    """
    if not body:
        return None
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        return None

    from urllib.parse import parse_qsl

    pairs = parse_qsl(body, keep_blank_values=True)
    return dict(pairs) if pairs else None


def as_mapping(site: Any) -> Dict[str, Any]:
    """Site 对象或原始 dict 都转成 dict，导出函数只需要一种输入。"""
    if isinstance(site, Site):
        return {
            "urlMain": site.url_main,
            "uri_check": site.uri_check,
            "uri_pretty": site.uri_pretty or site.uri_check,
            "cat": site.cat,
            "tags": list(site.tags),
            "id_type": site.id_type,
            "strip_bad_char": site.strip_bad_char,
            "method": site.method,
            "post_body": site.post_body,
            "headers": dict(site.headers),
            "e_code": site.e_code,
            "e_string": site.e_string,
            "e_strings": list(site.e_strings),
            "m_code": site.m_code,
            "m_string": site.m_string,
            "m_strings": list(site.m_strings),
            "executor": site.executor,
            "signature": site.signature,
            "requires": list(site.requires),
            "protection": list(site.protection),
            "extract": list(site.extract),
            "known": list(site.known),
            "known_absent": list(site.known_absent),
            "valid": site.valid,
            "verified_at": site.verified_at,
        }
    return dict(site)


def load(fmt: str, data: Any) -> Dict[str, Dict[str, Any]]:
    """把任意格式的文件内容解析成 `{站点名: v2 原始 dict}`。

    返回原始 dict 而不是 Site：调用方通常要把它写进 `sites_cn.json`，
    写成 Site 再序列化会把 `_note`、`valid: false` 这类"给人看"的信息丢掉。
    """
    fmt = (fmt or "").lower()
    if fmt == "wmn":
        from . import wmn

        return wmn.parse(data)
    if fmt == "sherlock":
        from . import sherlock

        return sherlock.parse(data)
    if fmt == "maigret":
        from . import maigret_db

        return maigret_db.parse(data)
    if fmt == "v2":
        return _parse_v2(data)
    raise ValueError(f"不认识的格式：{fmt!r}（支持 {'/'.join(FORMATS)}）")


def dump(fmt: str, sites: Mapping[str, Any]) -> Any:
    """把 `{站点名: Site 或 dict}` 导出成目标格式。"""
    fmt = (fmt or "").lower()
    if fmt == "wmn":
        from . import wmn

        return wmn.dump(sites)
    if fmt == "sherlock":
        from . import sherlock

        return sherlock.dump(sites)
    if fmt == "maigret":
        from . import maigret_db

        return maigret_db.dump(sites)
    if fmt == "v2":
        return {name: as_mapping(site) for name, site in sites.items()}
    raise ValueError(f"不认识的格式：{fmt!r}（支持 {'/'.join(FORMATS)}）")


def _parse_v2(data: Any) -> Dict[str, Dict[str, Any]]:
    """v2 文件既可能是 `{name: {...}}`，也可能是 `{"sites": {name: {...}}}`。"""
    if isinstance(data, Mapping) and isinstance(data.get("sites"), Mapping):
        data = data["sites"]
    if not isinstance(data, Mapping):
        raise ValueError("v2 格式必须是一个对象")
    return {
        str(name): to_account_placeholder(dict(raw))
        for name, raw in data.items()
        if isinstance(raw, Mapping)
    }
