"""Sherlock 格式 ↔ v2。

Sherlock 的判定语义和四字段协议差得最远，所以这个转换要**主动拒绝**
一部分站点。这不是保守，是因为一条错的规则比没有规则坏得多：
用户会拿着一个"查过了"的结论去做决定。

`errorType` 的四种取值逐个对应：

    status_code   → e_code=200 / m_code=404
                    语义是"200 就算存在"，可以完整表达
    message       → e_code=200 / m_code=200 / m_string=errorMsg
                    Sherlock 判"errorMsg 出现在页面里就是不存在"，
                    正好是四字段的不存在侧判据
    response_url  → **无法表达**，输出 valid=false
                    判据在最终 URL 上，而四字段只看状态码和响应体。
                    降级成 status_code 会把"跳回首页"当成"用户存在"。
    unknown       → **无法表达**，输出 valid=false
                    Sherlock 自己都说不确定，我们更没有依据

另外 `regexCheck` 在 v2 里没有对应字段（v2 用 `strip_bad_char` 做清洗，
语义不同：前者是"用户名格式不合法就别查"，后者是"请求前去掉某些字符"）。
它不会被静默丢掉，而是写进 `_note`。
"""

import logging
from typing import Any, Dict, List, Mapping, Optional

from . import ACCOUNT_PLACEHOLDER, NOTE_UNVERIFIED, as_mapping, parse_form_body, to_account_placeholder

logger = logging.getLogger(__name__)

# Sherlock 的占位符是 `{}`（str.format 风格）
_SHERLOCK_PLACEHOLDER = "{}"

# 这些 errorType 无法用四字段表达
UNSUPPORTED_ERROR_TYPES = {
    "response_url": "判据在最终跳转 URL 上，四字段只看状态码和响应体，无法表达",
    "unknown": "Sherlock 自己也不确定这条规则怎么判，没有可转换的依据",
}


def parse(data: Any) -> Dict[str, Dict[str, Any]]:
    """Sherlock 文件 → `{站点名: v2 dict}`。"""
    if not isinstance(data, Mapping):
        raise ValueError("Sherlock 格式必须是一个对象")

    result: Dict[str, Dict[str, Any]] = {}
    for name, entry in data.items():
        if not isinstance(entry, Mapping):
            continue
        name = str(name).strip()
        if not name:
            continue

        url = to_account_placeholder(str(entry.get("url") or "").strip())
        probe = to_account_placeholder(str(entry.get("urlProbe") or "").strip())
        uri_check = probe or url

        raw: Dict[str, Any] = {
            "urlMain": str(entry.get("urlMain") or "").strip(),
            "uri_check": uri_check,
            "uri_pretty": url or uri_check,
            "tags": [],
            "known": _as_list(entry.get("username_claimed")),
            "known_absent": _as_list(entry.get("username_unclaimed")),
            "headers": dict(entry.get("headers") or {}),
            "valid": True,
        }

        error_type = str(entry.get("errorType") or "status_code").strip().lower()
        notes: List[str] = []

        if error_type == "status_code":
            raw["e_code"] = 200
            raw["m_code"] = 404
        elif error_type == "message":
            error_msg = str(entry.get("errorMsg") or "")
            raw["e_code"] = 200
            raw["m_code"] = 200
            raw["e_string"] = ""
            raw["m_string"] = error_msg
            if not error_msg:
                raw["valid"] = False
                notes.append("errorType=message 但 errorMsg 为空，没有可用的不存在侧判据")
            else:
                # Sherlock 的 message 型**只有不存在侧判据**：它判"errorMsg 出现在
                # 页面里就不存在"，存在侧靠的是"没出现"——一个否定条件，四字段
                # 表达不了。
                #
                # 直接映射成 e_code=m_code=200 + 只有 m_string 会踩一个硬坑：
                # `judge()` 对存在侧退化成 status_only，于是任何 200 页面
                # （包括错误页）两侧同时命中，必然返回 RULE_AMBIGUOUS。
                # 这不是"可能出错"，是"每条规则每次查询都出错"。
                #
                # 所以这里明确标成待补，而不是产出一条看着能用、实际全判不出的规则。
                raw["valid"] = False
                notes.append(
                    "errorType=message 只提供了不存在侧判据，四字段要求 e_code==m_code "
                    "时两侧都有串（否则必然双命中）。需要补一条只在真实用户页出现的"
                    "字符串作为 e_string，再把 valid 改回 true"
                )
        else:
            raw["valid"] = False
            notes.append(
                f"errorType={error_type} 无法转换为四字段："
                + UNSUPPORTED_ERROR_TYPES.get(error_type, "未知取值")
            )

        if entry.get("POST") or entry.get("request_payload"):
            raw["method"] = "POST"
            body = parse_form_body(entry.get("request_payload"))
            if body:
                raw["post_body"] = to_account_placeholder(body)
        else:
            raw["method"] = str(entry.get("request_method") or "GET").upper()

        if entry.get("regexCheck"):
            # v2 没有对应字段，写进 _note 而不是丢掉
            notes.append(
                f"regexCheck={entry['regexCheck']!r} 未导入（v2 无对应字段，"
                "需要时改用 strip_bad_char 或 known_absent 样本自检）"
            )

        if notes:
            raw["_note"] = "；".join(notes + [NOTE_UNVERIFIED])
        else:
            raw["_note"] = NOTE_UNVERIFIED

        result[name] = raw

    return result


def _as_list(value: Any) -> List[str]:
    """Sherlock 的 claimed/unclaimed 是裸字符串，v2 是列表。

    这是最容易踩的转换坑：`list("abc")` 会变成 `['a','b','c']`。
    """
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return [str(value)]


def dump(sites: Mapping[str, Any]) -> Dict[str, Any]:
    """`{站点名: Site|dict}` → Sherlock 文件。"""
    result: Dict[str, Any] = {}

    for name, site in sites.items():
        raw = as_mapping(site)
        uri_check = str(raw.get("uri_check") or "")
        uri_pretty = str(raw.get("uri_pretty") or uri_check)

        entry: Dict[str, Any] = {
            "url": _to_sherlock_placeholder(uri_pretty),
            "urlMain": str(raw.get("urlMain") or ""),
            "username_claimed": _first(raw.get("known")),
            "username_unclaimed": _first(raw.get("known_absent")),
        }

        # errorType 由"有没有不存在侧字符串"决定：
        # 有串说明这条规则靠文案判，没有串就只剩状态码可用。
        if str(raw.get("m_string") or "").strip():
            entry["errorType"] = "message"
            entry["errorMsg"] = raw["m_string"]
        else:
            entry["errorType"] = "status_code"
            entry["errorMsg"] = ""

        if raw.get("headers"):
            entry["headers"] = dict(raw["headers"])
        if str(raw.get("method") or "").upper() == "POST":
            entry["POST"] = True
            entry["request_payload"] = to_account_placeholder(raw.get("post_body") or {})
        if str(raw.get("urlProbe") or "") and uri_check != uri_pretty:
            entry["urlProbe"] = _to_sherlock_placeholder(uri_check)

        result[name] = entry

    return result


def _to_sherlock_placeholder(value: str) -> str:
    return value.replace(ACCOUNT_PLACEHOLDER, _SHERLOCK_PLACEHOLDER)


def _first(value: Optional[Any]) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value or "")
