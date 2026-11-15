"""WhatsMyName 格式 ↔ v2。

WMN 是最接近本项目的一种格式：它本来就是四字段（`e_code`/`e_string`/
`m_code`/`m_string`）的出处，`{account}` 占位符也和 v2 一致。
所以这个转换几乎是恒等的，只有几处结构性差异需要处理：

    1. WMN 没有 `urlMain`，只有 `uri_pretty` —— 从 uri_pretty 里取
       scheme://netloc 反推。取不到就留空（不是错误，只是少一个展示字段）。
    2. WMN 的 `post_body` 是 query string，v2 是 dict。
    3. WMN 有 `cat`（单值），v2 有 `tags`（列表）。`cat` 原样保留，
       `tags` 用 `[cat]` 填充，否则 `--tags cn` 这类过滤会漏掉导入的站点。
    4. WMN 没有 `known_absent`。不编造——留空，让自检脚本去实测。
"""

import logging
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

from . import NOTE_UNVERIFIED, as_mapping, parse_form_body

logger = logging.getLogger(__name__)


def _as_text(value: Any) -> str:
    """WMN 里同一个字段偶尔是 str、偶尔是 list，统一取第一个。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    if value is None:
        return ""
    return str(value)


def _as_extra_strings(value: Any) -> List[str]:
    """第一个串当主判据，其余进 `e_strings`/`m_strings`（语义是"任一命中"）。"""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value[1:] if item]
    return []


def _as_code(value: Any) -> Optional[int]:
    """状态码必须是 int，否则判定时 `status == e_code` 永远不成立。

    WMN 里出现过 `"200"`（字符串）这种写法，静默转成 int 比留个坑好。
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_valid(value: Any) -> bool:
    """`valid` 在 WMN 里既可能是 bool，也可能是 `"true"`/`"false"` 字符串。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _url_main(uri_pretty: str) -> str:
    try:
        parsed = urlparse(uri_pretty)
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def parse(data: Any) -> Dict[str, Dict[str, Any]]:
    """WMN 文件 → `{站点名: v2 dict}`。

    接受两种顶层形态：`{"sites": [...]}`（WMN 原样）和裸列表（有人会把
    sites 数组单独存一个文件）。
    """
    entries = data.get("sites") if isinstance(data, Mapping) else data
    if not isinstance(entries, (list, tuple)):
        raise ValueError("WMN 格式必须包含 sites 数组")

    result: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = _as_text(entry.get("name")).strip()
        if not name:
            continue

        uri_check = _as_text(entry.get("uri_check")).strip()
        uri_pretty = _as_text(entry.get("uri_pretty")).strip() or uri_check
        cat = _as_text(entry.get("cat")).strip()

        raw: Dict[str, Any] = {
            "urlMain": _url_main(uri_pretty),
            "uri_check": uri_check,
            "uri_pretty": uri_pretty,
            "e_code": _as_code(entry.get("e_code")),
            "e_string": _as_text(entry.get("e_string")),
            "m_code": _as_code(entry.get("m_code")),
            "m_string": _as_text(entry.get("m_string")),
            "known": [str(item) for item in (entry.get("known") or []) if item],
            "valid": _as_valid(entry.get("valid", True)),
            "headers": dict(entry.get("headers") or {}),
            "protection": [str(item) for item in (entry.get("protection") or []) if item],
            "method": _as_text(entry.get("request_method")).upper() or "GET",
            "_note": NOTE_UNVERIFIED,
        }

        # 附加候选串只在这家确实给了多个串时才写，避免生成一堆空列表
        extra_e = _as_extra_strings(entry.get("e_string"))
        extra_m = _as_extra_strings(entry.get("m_string"))
        if extra_e:
            raw["e_strings"] = extra_e
        if extra_m:
            raw["m_strings"] = extra_m

        if cat:
            raw["cat"] = cat
            raw["tags"] = [cat]
        else:
            raw["tags"] = []

        strip_bad_char = _as_text(entry.get("strip_bad_char"))
        if strip_bad_char:
            raw["strip_bad_char"] = strip_bad_char

        body = parse_form_body(entry.get("post_body"))
        if body:
            raw["post_body"] = body
            # 有 body 就必须 POST：WMN 里有大量条目只写了 post_body 没写
            # request_method，默认值 GET 会让探测请求把 body 丢掉，
            # 结果就是"这条规则永远判定失败"，而且失败得毫无提示。
            if raw["method"] == "GET":
                raw["method"] = "POST"

        result[name] = raw

    return result


def dump(sites: Mapping[str, Any]) -> Dict[str, Any]:
    """`{站点名: Site|dict}` → WMN 文件。

    `cat` 必须出现在顶层 `categories` 里，否则 WMN 自己的校验会报错
    （这是 WMN 后期才加的一致性检查）。
    """
    entries: List[Dict[str, Any]] = []
    categories: List[str] = []

    for name, site in sites.items():
        raw = as_mapping(site)
        cat = str(raw.get("cat") or "").strip()
        if cat and cat not in categories:
            categories.append(cat)

        entry: Dict[str, Any] = {
            "name": name,
            "uri_check": raw.get("uri_check") or "",
            "uri_pretty": raw.get("uri_pretty") or raw.get("uri_check") or "",
            "e_code": raw.get("e_code"),
            "e_string": raw.get("e_string") or "",
            "m_code": raw.get("m_code"),
            "m_string": raw.get("m_string") or "",
            "known": list(raw.get("known") or []),
            "cat": cat,
            "valid": bool(raw.get("valid", True)),
        }

        if raw.get("strip_bad_char"):
            entry["strip_bad_char"] = raw["strip_bad_char"]
        if raw.get("headers"):
            entry["headers"] = dict(raw["headers"])
        if raw.get("protection"):
            entry["protection"] = list(raw["protection"])
        if raw.get("method"):
            entry["request_method"] = raw["method"]

        body = raw.get("post_body")
        if body:
            from urllib.parse import urlencode

            # v2 的 post_body 是 dict，WMN 要 query string
            entry["post_body"] = urlencode(body) if isinstance(body, dict) else str(body)

        entries.append(entry)

    return {"categories": categories, "sites": entries}
