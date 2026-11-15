"""上游 Maigret 格式（data.json）↔ v2。

这个方向的转换不重复实现：`overlay.adapt_upstream` 已经把上游三种
`checkType` 的语义差异处理过了（尤其 `message` 型的"absence 优先"，
以及 `response_url` 不能整体当 `status_code` 的坑）。这里只做两件事：

    1. 从 data.json 的 `{"sites": {...}}` 里取出站点，交给 adapt_upstream
    2. 反向导出：把 v2 的四字段压回上游的 `checkType` + `presenseStrs`/`absenceStrs`

**反向导出是有损的**，因为上游格式表达不了 `e_strings`/`m_strings`
（附加候选串）和 `executor`/`signature`/`requires`/`extract` 这些本项目的
扩展字段。有损的部分不静默丢，写进 `_note`。
"""

import logging
from typing import Any, Dict, List, Mapping

from ..overlay import adapt_upstream
from . import ACCOUNT_PLACEHOLDER, NOTE_UNVERIFIED, as_mapping, strip_to_v2

logger = logging.getLogger(__name__)

_UPSTREAM_PLACEHOLDER = "{username}"

# 上游格式里没有对应字段的 v2 扩展
_V2_ONLY_FIELDS = ("e_strings", "m_strings", "executor", "signature", "requires", "extract")


def parse(data: Any) -> Dict[str, Dict[str, Any]]:
    """上游 data.json → `{站点名: v2 dict}`。"""
    sites = data.get("sites") if isinstance(data, Mapping) else None
    if not isinstance(sites, Mapping):
        raise ValueError("Maigret 格式必须包含 sites 对象")

    result: Dict[str, Dict[str, Any]] = {}
    for name, raw in sites.items():
        if not isinstance(raw, Mapping):
            continue
        name = str(name).strip()
        if not name:
            continue
        # adapt_upstream 以 raw 为底，会把 checkType / presenseStrs / errors
        # 这些上游字段一起带出来。落盘成 v2 文件前收敛掉，理由见 V2_FIELDS。
        adapted = strip_to_v2(adapt_upstream(name, dict(raw)))
        adapted.setdefault("_note", NOTE_UNVERIFIED)
        result[name] = adapted

    return result


def dump(sites: Mapping[str, Any]) -> Dict[str, Any]:
    """`{站点名: Site|dict}` → 上游 data.json 形态。"""
    result: Dict[str, Any] = {}

    for name, site in sites.items():
        raw = as_mapping(site)
        e_code = raw.get("e_code")
        m_code = raw.get("m_code")
        e_string = str(raw.get("e_string") or "")
        m_string = str(raw.get("m_string") or "")

        entry: Dict[str, Any] = {
            "url": _to_upstream_placeholder(str(raw.get("uri_pretty") or raw.get("uri_check") or "")),
            "urlMain": str(raw.get("urlMain") or ""),
            "usernameClaimed": _first(raw.get("known")),
            "usernameUnclaimed": _first(raw.get("known_absent")),
        }

        probe = str(raw.get("uri_check") or "")
        pretty = str(raw.get("uri_pretty") or probe)
        if probe and probe != pretty:
            entry["urlProbe"] = _to_upstream_placeholder(probe)

        # 判据形式由"字符串能不能分开两侧"决定，不由原字段名决定：
        # 上游只有 message 型会读字符串，其余类型一律只看状态码。
        if (e_string or m_string) and e_code == m_code:
            entry["checkType"] = "message"
            entry["presenseStrs"] = [
                s for s in [e_string, *(raw.get("e_strings") or [])] if s
            ]
            entry["absenceStrs"] = [
                s for s in [m_string, *(raw.get("m_strings") or [])] if s
            ]
        else:
            entry["checkType"] = "status_code"

        if raw.get("tags"):
            entry["tags"] = list(raw["tags"])
        if raw.get("headers"):
            entry["headers"] = dict(raw["headers"])
        if raw.get("protection"):
            entry["protection"] = list(raw["protection"])
        if str(raw.get("method") or "GET").upper() == "POST":
            entry["requestMethod"] = "POST"
            entry["requestPayload"] = raw.get("post_body") or {}
        if raw.get("alexa_rank") is not None:
            entry["alexaRank"] = raw["alexa_rank"]
        if raw.get("disabled"):
            entry["disabled"] = True

        lost = [field for field in _V2_ONLY_FIELDS if raw.get(field)]
        # `executor` 默认就是 "http"，把默认值也报成"丢失"会让 9,000 多个站点
        # 全带上一条无意义的提示，真正有损的那几条反而被淹没。
        if str(raw.get("executor") or "http") == "http":
            lost = [field for field in lost if field != "executor"]
        if lost:
            entry["_note"] = (
                "以下字段在上游格式里没有对应位置，导出后丢失："
                + "、".join(lost)
                + "。若要保留，请以 v2 格式为准。"
            )

        result[name] = entry

    return {"sites": result, "engines": {}, "tags": _collect_tags(result)}


def _collect_tags(sites: Mapping[str, Mapping[str, Any]]) -> List[str]:
    tags: List[str] = []
    for entry in sites.values():
        for tag in entry.get("tags") or []:
            if tag not in tags:
                tags.append(tag)
    return sorted(tags)


def _to_upstream_placeholder(value: str) -> str:
    return value.replace(ACCOUNT_PLACEHOLDER, _UPSTREAM_PLACEHOLDER)


def _first(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value or "")
