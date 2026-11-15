"""把判定、抽取、聚类、分级、排除表汇总成一段用户能读的输出。

为什么单独一层：前面几个模块各自只回答一个问题（这个站有没有人？抽到了什么？
哪几个账号是同一个人？有多严重？）。用户需要的是一个连贯的结论，
而"把结论拼起来"本身有讲究：

    1. **顺序即优先级**：风险在最前（用户最关心的），其次是身份簇，
       最后是"本次未能判定"。反过来会把最该看的东西压到屏幕外。
    2. **未判定必须显式出现**。只报命中、不提盲区，用户会以为"没查到的
       就是没有"——而中国站大量需要登录，盲区往往比命中还大。
    3. **排除过的要留痕**。静默丢弃一条结果，用户没法判断"没查出来"是
       真没暴露还是被排除了。

这个模块只做汇总，不做 I/O：输入是 `maigret()` 返回的 results 字典，
输出是字符串。这样它能在离线测试里被完整覆盖。
"""

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .extract import Evidence
from .identity import Account, cluster, render as render_clusters
from .risk import BlindSpot, RiskResult, assess, blind_spots, render as render_risk

logger = logging.getLogger(__name__)

# results 里 status 子字典的键名
_STATUS_KEY = "status"


def _status_of(result: Dict[str, Any]) -> Dict[str, Any]:
    """把一条站点结果里的判定取成 dict 形态。

    两种形态都要认，因为它们在两个阶段出现：
        - **活扫描**：`results_info["status"]` 是 `MaigretCheckResult` 对象
        - **落盘后**：JSON 报告里是 dict（`json()` 的输出），测试也照这个造

    只认 dict 的后果不是报错，而是**静默失效**：`_is_claimed` 恒为 False，
    于是身份聚类永远聚不出东西、结语永远是"没有发现"；`_is_definitive`
    也恒为 False，但 `blind_spot_map` 里那句 `if not status: continue`
    又让盲区列表保持为空——两块功能同时消失，界面上却看不出任何异常。
    """
    status = result.get(_STATUS_KEY)
    if isinstance(status, dict):
        return status
    if status is None:
        return {}

    value = getattr(status, "status", None)
    # 上游的 MaigretCheckStatus 是普通 Enum，str() 出来是
    # "MaigretCheckStatus.CLAIMED"，必须取 .value 才是 "Claimed"
    return {
        "status": getattr(value, "value", value),
        "reason": getattr(status, "reason", None),
        "confidence": getattr(status, "confidence", None),
        "url": getattr(status, "url", None),
        "ids": getattr(status, "ids", None) or {},
        "context": getattr(status, "context", None),
    }


def _is_claimed(result: Dict[str, Any]) -> bool:
    value = _status_of(result).get("status")
    # 上游的枚举值有 "Claimed" / "Available" / "Unknown" 三种字符串形态
    return str(value).lower() == "claimed"


def _is_definitive(result: Dict[str, Any]) -> bool:
    """这次判定是否已经给出确定答案（不管答案是"有"还是"没有"）。

    `Available` 是**确定的不存在**，不是盲区。把它算成盲区会让每一次正常
    扫描都列出成百上千个"未能判定"，真正需要用户注意的那几条被彻底淹没。
    同理 `Illegal`（站点不接受这种用户名）也是确定结论。

    反过来，认不出来的状态一律算作"没判定"：多报一条盲区只是噪音，
    少报一条会让用户以为查过了。
    """
    value = str(_status_of(result).get("status")).lower()
    return value in ("claimed", "available", "illegal")


def _reason_of(result: Dict[str, Any]) -> Optional[str]:
    reason = _status_of(result).get("reason")
    if not reason:
        return None
    # Reason 是 str 枚举，序列化后可能是 "Reason.BLOCKED" 或 "blocked"
    return str(reason).split(".")[-1].lower()


# ---------------------------------------------------------------------------
# 证据
# ---------------------------------------------------------------------------
def collect_evidence(results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把所有站点的证据汇成一个列表，并补上站点名。

    为什么补站点名：`Evidence.source` 描述的是"从页面哪一部分抽到的"
    （og:title、正文 IP 属地），排查时还需要知道"哪个站"。
    """
    collected: List[Dict[str, Any]] = []
    for site_name, result in results.items():
        for item in result.get("cn_evidence") or []:
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            entry.setdefault("site", site_name)
            collected.append(entry)
    return collected


def build_accounts(
    results: Dict[str, Any], skip_sites: Optional[Iterable[str]] = None
) -> List[Account]:
    """只把**已确认**的账号变成聚类输入。

    未确认的（Unknown / Available）绝不能进来：聚类是在断言"这些是同一个人"，
    拿一个没判出来的页面去聚类，等于替用户编造身份关系。

    `skip_sites` 是被排除表判定为误报的站点：它们同样不能进来，
    否则一条"站点对所有用户名都返回存在"的误报会把所有人串成一簇。
    """
    skipped = set(skip_sites or ())
    accounts: List[Account] = []
    for site_name, result in results.items():
        if site_name in skipped:
            continue
        if not _is_claimed(result):
            continue

        fields = {
            item.get("field"): item.get("value")
            for item in (result.get("cn_evidence") or [])
            if isinstance(item, dict)
        }

        avatar_hash = None
        raw_hash = result.get("cn_avatar_hash")
        if raw_hash:
            try:
                avatar_hash = int(str(raw_hash), 16)
            except (TypeError, ValueError):
                avatar_hash = None

        accounts.append(
            Account(
                site=site_name,
                username=str(result.get("username") or ""),
                url=str(result.get("url_user") or ""),
                avatar_hash=avatar_hash,
                name=str(fields.get("nickname") or ""),
                location=str(fields.get("ip_location") or ""),
                bio=str(fields.get("bio") or ""),
                # 跨站互链是权重最高的一条证据（0.90），必须接上。
                # 上游把页面里抽到的外链放在 `ids_links`（递归搜索也用这个字段），
                # 不接的话 EDGE_CROSS_LINK 在真实流程里永远不会命中。
                links=[
                    str(link)
                    for link in (result.get("ids_links") or [])
                    if isinstance(link, str) and link
                ],
            )
        )
    return accounts


# ---------------------------------------------------------------------------
# 盲区
# ---------------------------------------------------------------------------
def blind_spot_map(results: Dict[str, Any]) -> Dict[str, List[str]]:
    """按判定原因归组未能判定的站点。

    只收"有明确原因"的：既没命中也没原因的，多半是规则缺失，也归到
    rule_missing 里——让用户知道"这块我们没查"，而不是让它消失。
    """
    groups: Dict[str, List[str]] = {}
    for site_name, result in results.items():
        status = _status_of(result)
        if not status:
            continue
        if _is_definitive(result):
            continue

        reason = _reason_of(result) or "rule_missing"
        groups.setdefault(reason, []).append(site_name)

    for names in groups.values():
        names.sort()
    return groups


# ---------------------------------------------------------------------------
# 排除
# ---------------------------------------------------------------------------
def apply_exclusions(results: Dict[str, Any], table) -> List[Dict[str, Any]]:
    """把命中排除表的条目标上 `excluded_by`，返回被排除的清单。

    **不删结果**：JSON/CSV 报告里照样能看到它，只是多一个字段说明
    "这条被判为已知误报"。静默删除等于把排除表变成黑箱，
    用户没法判断"我的手机号没查出来"是真没暴露还是被谁排除了。
    """
    if table is None:
        return []

    excluded: List[Dict[str, Any]] = []
    for site_name, result in results.items():
        rule = table.explain(
            site_name,
            username=result.get("username"),
            url=result.get("url_user") or result.get("url_probe"),
        )
        if rule is None:
            continue
        result["excluded_by"] = rule.describe()
        excluded.append({"site": site_name, "rule": rule.describe()})
    return excluded


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def build_summary(
    results: Dict[str, Any],
    username: str = "",
    excluded: Optional[Sequence[Dict[str, Any]]] = None,
    table=None,
) -> str:
    """拼出完整的输出块。没有内容时返回空串（调用方据此决定打不打印）。"""
    if not results:
        return ""

    evidence = collect_evidence(results)
    excluded_sites = {
        str(item.get("site")) for item in (excluded or []) if isinstance(item, dict)
    }
    if excluded_sites:
        # 既然已经认定这几条是误报，就不能再把它们的证据算进风险等级——
        # 否则用户看到的是"下面说这条不算，上面却因为它升了级"。
        evidence = [item for item in evidence if item.get("site") not in excluded_sites]

    risk: RiskResult = assess(evidence)
    spots: List[BlindSpot] = blind_spots(blind_spot_map(results))
    accounts = build_accounts(results, skip_sites=excluded_sites)
    clusters = cluster(accounts)

    blocks: List[str] = []

    header = "自查结论" + (f"：{username}" if username else "")
    blocks.append(_rule_line(header))

    # 1. 风险（最该先看的放最前）
    blocks.append(render_risk(risk, spots, evidence=evidence, claims=len(accounts)))

    # 2. 身份簇（只有真的聚出 ≥2 人时才出现，否则是噪音）
    #
    # 判据必须是"真的存在 ≥2 人的簇"，不能看 render() 的返回值里有没有
    # "身份簇"三个字：无簇时 render() 返回的是「未发现可归并的身份簇」，
    # 那句话本身就含这三个字，用子串判断会让这一块永远出现，
    # 把"没聚出东西"包装成一条结论。
    if any(len(item) >= 2 for item in clusters):
        blocks.append("")
        blocks.append("跨站身份归并")
        blocks.append(render_clusters(clusters, min_size=2))

    # 3. 排除过的
    if excluded:
        blocks.append("")
        blocks.append("已排除的疑似误报（不计入上面的风险等级与身份归并）")
        for item in excluded:
            blocks.append(f"  · {item['site']}：{item['rule']}")

    # 4. 排除表本身出问题时要告诉用户，否则他不知道自己看的可能是旧规则
    if table is not None and getattr(table, "warnings", None):
        blocks.append("")
        blocks.append("排除表提示")
        for warning in table.warnings:
            blocks.append(f"  · {warning}")

    return "\n".join(blocks)


def _rule_line(title: str) -> str:
    return f"{title}\n" + "─" * min(60, max(20, len(title) * 2))


# ---------------------------------------------------------------------------
# 兼容入口
# ---------------------------------------------------------------------------
def evidence_to_objects(items: Iterable[Dict[str, Any]]) -> List[Evidence]:
    """把 dict 形态的证据还原成 Evidence 对象（给需要对象的调用方用）。"""
    objects: List[Evidence] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        objects.append(
            Evidence(
                field=str(item.get("field") or ""),
                value=str(item.get("value") or ""),
                source=str(item.get("source") or ""),
                confidence=float(item.get("confidence") or 0.8),
                risk=int(item.get("risk") or 0),
                partial=bool(item.get("partial")),
            )
        )
    return objects
