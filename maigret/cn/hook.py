"""把 cn.verdict 接进上游判定流程的适配层（唯一的集成点）。

设计原则：**分阶段接管，可一键回退**。

    上游 `process_site_result` 的结论是"200 就是命中"。直接全量替换风险太大：
    9,535 个站点里大部分规则从没按四字段校验过，一旦判定器比上游更严格，
    用户会看到大量"未判定"，反而以为工具坏了。

    所以默认只接管 `sites_cn.json` / `overrides.json` 里**人工策展过**的站点
    （31 个）。上游站点的行为完全不变。这样做的两个好处：
      1. 出问题时 `MAIGRET_CN_VERDICT=0` 就能整体回退，不用改代码
      2. 可以按站点逐个验证，而不是一次押上全部

环境变量 `MAIGRET_CN_VERDICT`：
    `0`     关闭接管，完全走上游逻辑
    `1`     只接管策展站点（默认）
    `all`   所有站点都按四字段判定（需要站点规则本身合格，否则判 UNKNOWN）

为什么从 MaigretSite 现场适配，而不是调 `overlay.load_sites()`：
    全库加载是 3.3 秒 / 59MB（9,540 个 Site 对象）。CLI 每次启动都付这个代价
    不可接受。而扫描时 `MaigretSite` 就在手里，上游字段（presenseStrs /
    absenceStrs / checkType）都还在，现场适配只要几十微秒。策展覆盖值从两个
    小 JSON 读取并缓存，总共不到 100KB。
"""

import logging
import os
from typing import Any, Dict, Optional

from .model import SOURCE_CN, SOURCE_OVERRIDES, Site
from .overlay import (
    OVERRIDES_FILE,
    SITES_CN_FILE,
    _load_overlay_sites,
    adapt_upstream,
    deep_merge,
)
from .schema import SchemaError, validate_site
from .verdict import Reason, Verdict, judge

logger = logging.getLogger(__name__)

ENV_FLAG = "MAIGRET_CN_VERDICT"
MODE_OFF = "0"
MODE_CURATED = "1"
MODE_ALL = "all"

# 策展覆盖值缓存：{站点名: v2 原始 dict}
_curated_cache: Optional[Dict[str, Dict[str, Any]]] = None
# 适配结果缓存：{站点名: Site | None}。None 表示"这个站不接管"
_spec_cache: Dict[str, Optional[Site]] = {}


def mode() -> str:
    """当前接管模式。未知值按默认（策展站点）处理，不让拼错的环境变量静默关掉功能。"""
    raw = (os.environ.get(ENV_FLAG) or MODE_CURATED).strip().lower()
    if raw in (MODE_OFF, MODE_CURATED, MODE_ALL):
        return raw
    logger.warning("%s=%r 不是合法取值（0/1/all），按默认 1 处理", ENV_FLAG, raw)
    return MODE_CURATED


def reset_cache() -> None:
    """清空缓存。测试改环境变量后必须调用，否则会拿到上一个模式的结论。"""
    global _curated_cache
    _curated_cache = None
    _spec_cache.clear()


def curated_raw() -> Dict[str, Dict[str, Any]]:
    """人工策展过的站点覆盖值（sites_cn.json + overrides.json）。

    两个文件都很小（合计 <100KB），解析成本可忽略；overrides 后合并，与
    `load_sites` 的优先级保持一致（sites_cn > overrides）。
    """
    global _curated_cache
    if _curated_cache is None:
        merged: Dict[str, Dict[str, Any]] = {}
        for path in (OVERRIDES_FILE, SITES_CN_FILE):
            for name, patch in _load_overlay_sites(path).items():
                merged[name] = deep_merge(merged.get(name, {}), patch)
        _curated_cache = merged
    return _curated_cache


def _maigret_to_raw(msite: Any) -> Dict[str, Any]:
    """上游 MaigretSite → `adapt_upstream` 认得的原始 dict 形态。

    只用属性读取，不碰 data.json —— 这些值在扫描时已经在内存里了。
    """
    return {
        "urlProbe": getattr(msite, "url_probe", None) or "",
        "url": getattr(msite, "url", None) or "",
        "urlMain": getattr(msite, "url_main", None) or "",
        "checkType": getattr(msite, "check_type", None) or "",
        # 注意拼写：上游就是 presenseStrs（少一个 c）
        "presenseStrs": list(getattr(msite, "presense_strs", None) or []),
        "absenceStrs": list(getattr(msite, "absence_strs", None) or []),
        "usernameClaimed": getattr(msite, "username_claimed", None) or "",
        "usernameUnclaimed": getattr(msite, "username_unclaimed", None) or "",
        "tags": list(getattr(msite, "tags", None) or []),
        "headers": dict(getattr(msite, "headers", None) or {}),
        "protection": list(getattr(msite, "protection", None) or []),
        "requestMethod": getattr(msite, "request_method", None) or "",
        "requestPayload": getattr(msite, "request_payload", None) or None,
        "type": getattr(msite, "type", None) or "username",
        "disabled": bool(getattr(msite, "disabled", False)),
    }


def spec_for(msite: Any) -> Optional[Site]:
    """给一个 MaigretSite 构造 cn 判定用的 Site；不接管则返回 None。

    不接管的情形：
        - 模式为 0
        - 模式为 1 且该站没有被人工策展
        - 适配后 schema 校验失败（规则结构不合法，交给上游按原逻辑处理更安全）

    注意「schema 校验失败」与「条目自己标 valid=false」是两件事：
        - 前者是规则写坏了（缺状态码、占位符缺失），我们构造不出可判定的规则
        - 后者是作者显式声明"这条还不能用"（如 QQ 的 JSONP 包装名问题），
          规则结构完整但判不准。这种仍然接管，输出 RULE_MISSING——
          给用户一行"未判定"，比让上游给一个必然错误的"存在"更诚实。
    """
    current = mode()
    if current == MODE_OFF:
        return None

    name = str(getattr(msite, "name", ""))
    if not name:
        return None

    curated = curated_raw()
    if current == MODE_CURATED and name not in curated:
        return None

    if name in _spec_cache:
        return _spec_cache[name]

    adapted = adapt_upstream(name, _maigret_to_raw(msite))
    if name in curated:
        adapted = deep_merge(adapted, curated[name])
        adapted["source"] = SOURCE_CN if name in _load_overlay_sites(SITES_CN_FILE) else SOURCE_OVERRIDES

    try:
        spec = validate_site(name, adapted)
    except SchemaError as exc:
        # 规则不可判定时**不接管**：上游至少还能给出一个（可能不准的）结论，
        # 而我们只能给 UNKNOWN。两害相权，让上游继续跑，并把原因留痕。
        logger.debug("站点 %s 未通过四字段校验，判定交回上游：%s", name, exc)
        spec = None

    _spec_cache[name] = spec
    return spec


def apply_cn_verdict(
    result: Any,
    msite: Any,
    username: str,
    html_text: str,
    status_code: Optional[int],
    check_error: Any,
    log: Optional[logging.Logger] = None,
    results_info: Optional[dict] = None,
) -> Any:
    """在 `process_site_result` 的判定分支之后调用，覆盖最终结论。

    位置很关键：放在上游 if/elif 链**之后**、`extract_ids_data` **之前**。
        - 之后：上游分支仍要跑，因为 `is_presense_detected` 会写 `site.stats`，
          下游（诊断、self-check）还在用这些副作用
        - 之前：如果结论从 CLAIMED 变成 UNKNOWN，就不能再抽 ids 了，
          否则会把一个未确认的页面当成已确认账号去抽关联信息
    """
    log = log or logger

    spec = spec_for(msite)
    if spec is None:
        return result

    # 站内搜索执行器走另一条判据，见 _apply_search_verdict 的说明。
    if spec.executor == "search":
        return _apply_search_verdict(
            result, spec, username, html_text, status_code, check_error, log, results_info
        )

    verdict = judge(
        _FakeResponse(status=status_code, text=html_text or ""),
        spec,
        username=username,
    )

    if check_error is not None:
        # 请求层出问题时上游已经给出 UNKNOWN，这里只补结构化原因。
        # 但 429/403 这类"状态码本身就说明问题"的结论要保留判定器的：
        # 上游的 detect_error_page 会把 429 也当成一般错误页，
        # 直接写 HTTP_ERROR 会让"被限流"和"连不上"混成一个桶，
        # 用户该退避重试还是该换网络就分不出来了。
        if verdict.reason in _STATUS_AUTHORITATIVE:
            result.status = _upstream_status(verdict.status.value, log, spec.name) or result.status
            result.reason = verdict.reason
            result.rate_limited = verdict.rate_limited
            return result
        result.reason = Reason.HTTP_ERROR
        return result

    new_status = _upstream_status(verdict.status.value, log, spec.name)
    if new_status is None:
        return result

    old_status = getattr(result, "status", None)
    if old_status is not None and str(old_status) != str(new_status):
        log.debug(
            "站点 %s 判定修正：%s → %s（原因：%s）",
            spec.name,
            old_status,
            new_status,
            verdict.reason.value if verdict.reason else "四字段命中",
        )

    result.status = new_status
    result.reason = verdict.reason
    result.confidence = verdict.confidence
    result.rate_limited = verdict.rate_limited
    # context 只在有诊断价值时写：命中证据对排查规则最有用，
    # 但正常命中不该污染 context（上游只在错误分支用它）。
    if verdict.status.value == "Unknown" and verdict.evidence:
        result.context = verdict.evidence
    return result


# 这些原因由状态码/页面特征直接决定，比上游的错误页探测更可信，不应被覆盖
_STATUS_AUTHORITATIVE = (
    Reason.RATE_LIMITED,
    Reason.BLOCKED,
    Reason.CAPTCHA,
    Reason.LOGIN_REQUIRED,
)


def _apply_search_verdict(
    result: Any,
    spec: Any,
    username: str,
    html_text: str,
    status_code: Optional[int],
    check_error: Any,
    log: logging.Logger,
    results_info: Optional[dict] = None,
) -> Any:
    """`executor: search` 站点的结论。

    为什么不复用 `judge()`：四字段协议判的是"这个页面像不像存在账号的页面"，
    而站内搜索页**必然**回显你搜的词。拿 `e_string = "{account}"` 去判，每一次
    搜索都命中，于是每个用户都"注册过"每一个站点。所以这里换成搜索执行器
    自己的判据：结果里有没有与目标**逐字相同**的句柄。

    请求层出过问题时一律判 UNKNOWN：拿半截页面去数候选句柄，结论只会是
    "没有同名账号"，而那是我们自己的失败，不是站点的事实。
    """
    from .executors.search import decide as decide_search
    from .verdict import Status as CnStatus

    if check_error is not None:
        # 状态也要改成 UNKNOWN：上游按 404/错误页可能已经判成 AVAILABLE，
        # 而"搜索接口出问题"和"这个账号不存在"是两件完全不同的事。
        # 只补 reason 不改 status，用户看到的还是"没注册"。
        result.status = _upstream_status(CnStatus.UNKNOWN.value, log, spec.name) or result.status
        result.reason = Reason.HTTP_ERROR
        return result

    outcome = decide_search(spec, username, html_text or "", status_code)
    verdict = outcome.verdict

    new_status = _upstream_status(verdict.status.value, log, spec.name)
    if new_status is None:
        return result

    result.status = new_status
    result.reason = verdict.reason
    result.confidence = verdict.confidence
    result.rate_limited = verdict.rate_limited
    if verdict.status == CnStatus.UNKNOWN and verdict.evidence:
        result.context = verdict.evidence

    # 命中时把链接换成**那个账号的主页**，而不是搜索页。
    # 搜索页地址是"我刚才搜了什么"，主页地址才是"这就是那个账号"——
    # 报告里让人点开一个空搜索框，等于让他自己再找一遍。
    #
    # 必须写 `site_url_user`：终端打印（notify）和 `to_dict()` 读的都是它，
    # 上游的 MaigretCheckResult 上**没有** `.url` 这个属性，写 `.url` 只会
    # 多出一个没人读的字段，链接照旧是搜索页。
    if outcome.profile_url:
        result.site_url_user = outcome.profile_url
        if isinstance(results_info, dict):
            results_info["url_user"] = outcome.profile_url
    return result


def _upstream_status(value: str, log: logging.Logger, site_name: str):
    """把 cn 的 Status 值映射回上游枚举；映射失败返回 None（保持原结论）。"""
    try:
        from ..result import MaigretCheckStatus

        return MaigretCheckStatus(value)
    except (ImportError, ValueError):  # pragma: no cover - 契约破坏时的兜底
        log.warning("站点 %s 的判定状态无法映射回上游枚举，保持原结论", site_name)
        return None


class _FakeResponse:
    """把 `(html_text, status_code)` 包装成 judge() 认的响应对象。

    上游的 response 是个三元组，没有 `.status` / `.text`。用这个薄壳适配，
    而不是让 judge 去理解元组 —— 判定层不该知道上游的数据形状。
    """

    __slots__ = ("status", "text")

    def __init__(self, status: Optional[int], text: str):
        self.status = status
        self.text = text


def register_overlay_sites(db: Any) -> list:
    """把只存在于 cn overlay 的站点注册进站点库，返回新增的名字。

    为什么必须注册而不是只在 `augment_sites` 里临时拼一条：
        `ranked_sites_dict(names=["Bilibili (搜索)"])` 是在**库里**找站点，
        库里没有就直接"没有要查的站点"。于是 `--site` 点名一个新站必然落空，
        `--tags cn` 也筛不到它。注册一次，所有入口（默认扫描、--site、--tags、
        Web 端）就走同一条路径，不用在每个入口各打一次补丁。

    只注册 `valid=True` 的：不可判定的规则进库只会让 `--site` 给出"未判定"。
    """
    if mode() == MODE_OFF:
        return []

    existing = {str(getattr(s, "name", "")): s for s in getattr(db, "sites", [])}
    raw_entries = curated_raw()
    added = []
    retyped = []
    reprobed = []
    for name in curated_raw():
        spec = spec_for(existing[name]) if name in existing else _spec_from_overlay(name)
        if spec is None or not spec.valid:
            continue

        if name in existing:
            # 把 overlay 的 id_type 写回站点库。不做这一步，`ranked_sites_dict`
            # 会按上游那个（错的）type 过滤：掘金/网易云在库里是 username，
            # 而规则要 user_id，于是 `--id-type user_id` 永远选不中它们，
            # 用户名扫描又会拿昵称去当数字 ID 问一遍。
            site = existing[name]
            if spec.id_type and getattr(site, "type", "username") != spec.id_type:
                site.type = spec.id_type
                retyped.append(f"{name}→{spec.id_type}")

            # 探测地址也必须写回，而且**只在 overlay 自己声明了的时候**写。
            #
            # 为什么：判定读的是 `spec_for()` 合并出来的规则（overlay 优先），
            # 而请求打的是站点对象上的 `url_probe`（上游值）。两者不写回一致，
            # 就会出现"用新判据去判旧接口的响应"——表现是 STRUCTURE_CHANGED，
            # 看起来像"站点改版了"，实际是我们自己把判据和地址配错了对。
            # 百度换成 home/get/panel 时就是这么踩的。
            #
            # 只认 overlay 显式声明的字段：`spec` 是合并结果，永远带着上游的
            # uri_check，照它写回等于把所有站都重刷一遍，没必要也没道理。
            raw_entry = raw_entries.get(name) or {}
            probe = str(raw_entry.get("uri_check") or "").strip()
            if probe and getattr(site, "url_probe", None) != probe.replace("{account}", "{username}"):
                site.url_probe = probe.replace("{account}", "{username}")
                reprobed.append(name)
            pretty = str(raw_entry.get("uri_pretty") or "").strip()
            if pretty:
                site.url = pretty.replace("{account}", "{username}")
            continue

        db.sites.append(_msite_from_spec(spec))
        added.append(name)

    if added:
        logger.info("注册 %d 个只存在于 overlay 的站点：%s", len(added), "、".join(added))
    if retyped:
        logger.info("按 overlay 修正 id_type：%s", "、".join(retyped))
    if reprobed:
        logger.info("按 overlay 修正探测地址：%s", "、".join(reprobed))
    return added


def _msite_from_spec(spec: Site) -> Any:
    """把一条只存在于 cn overlay 的站点转成上游认识的 MaigretSite。

    为什么需要这一步：`augment_sites` 原来是 `db.sites_dict.get(name)`，于是
    **只写在 sites_cn.json、上游库里没有的站永远并进不来**——小红书、抖音、
    以及 B 站搜索这类新站全部落空，用户看到的是"我明明加了规则，默认扫描
    里却没有"。

    这里只填上游真正会读的字段（url / type / headers / protection）。
    判定仍由 `spec_for()` 从 overlay 现场取，所以不伪造任何判据字符串：
    塞一份假的 presenseStrs 只会让"cn 判定没接管时"的上游逻辑给出错误结论。
    """
    from maigret.sites import MaigretSite

    url = (spec.uri_pretty or spec.uri_check or "").replace("{account}", "{username}")
    # 请求地址与展示地址必须分开：搜索站的 uri_check 是接口（返回 JSON），
    # uri_pretty 是给人点开的页面。上游正好有 url_probe 表达"探测用哪个地址"，
    # 用它而不是把接口地址塞进 url —— 否则报告里会列出 api.bilibili.com 那种
    # 用户点开一片 JSON 的链接。
    probe = (spec.uri_check or "").replace("{account}", "{username}") or None
    return MaigretSite(
        spec.name,
        {
            "url": url,
            "urlMain": spec.url_main or "",
            "url_probe": probe,
            "type": spec.id_type or "username",
            "tags": list(spec.tags),
            "headers": dict(spec.headers or {}),
            "protection": list(spec.protection or []),
            "similar_search": False,
            "disabled": False,
        },
    )


def augment_sites(
    site_data: Dict[str, Any],
    db: Any,
    id_type: str = "username",
    tags: Optional[list] = None,
    excluded_tags: Optional[list] = None,
) -> Dict[str, Any]:
    """把策展站点强制并入本次扫描列表（文档 §5.2 的 force_include）。

    为什么要在 `ranked_sites_dict(top=N)` 之后做：
        上游的 top 截断是按 alexaRank 排序的，而 Alexa 2022 年就停服了，
        这批排序数据早已失效。中文站（B站、掘金、V2EX…）大多没有 alexaRank，
        永远进不了默认范围 —— 这正是"默认扫描只有 5 个中国站"的根因。
        force_include 的语义就是**不参与排序，直接进**。

    三条过滤，缺一不可：
        1. `valid=False` 的不并 —— 放进来只会多一行"未判定"
        2. `default_include=False` 的不并 —— 小众站保留规则但不占默认位
        3. 用户显式给了 `--tags` / `--exclude-tags` 就按同一套规则过滤，
           否则 `--tags video` 会莫名其妙多出 20 个中文站
    """
    if mode() == MODE_OFF:
        return site_data

    curated = curated_raw()
    sites_dict = getattr(db, "sites_dict", {}) or {}
    wanted = {str(t).lower() for t in (tags or [])}
    unwanted = {str(t).lower() for t in (excluded_tags or [])}
    added = []

    for name in curated:
        if name in site_data:
            continue

        msite = sites_dict.get(name)
        if msite is not None:
            # 上游有的站：以合并后的规则为准。spec_for 返回 None 说明合并后
            # 仍然判不出来，这时**不能**退回去只校验 overlay 原文——
            # 那等于丢掉上游字段去凑一条"看起来合格"的规则。
            spec = spec_for(msite)
            if spec is None:
                continue
        else:
            # 上游没有的站（小红书、B 站搜索等）：从 overlay 现场造一条。
            spec = _spec_from_overlay(name)
            if spec is None:
                continue

        if not spec.valid or not spec.default_include:
            continue
        if getattr(spec, "id_type", "username") != id_type:
            continue

        spec_tags = {str(t).lower() for t in (spec.tags or [])}
        if wanted and not (spec_tags & wanted):
            continue
        if unwanted and (spec_tags & unwanted):
            continue

        if msite is None:
            msite = _msite_from_spec(spec)

        site_data[name] = msite
        added.append(name)

    if added:
        logger.info("强制并入 %d 个策展站点：%s", len(added), "、".join(added))
    return site_data


def _spec_from_overlay(name: str) -> Optional[Site]:
    """只从 overlay 读一条站点（上游库里没有的站）。校验失败返回 None。"""
    raw = curated_raw().get(name)
    if not raw:
        return None
    try:
        return validate_site(name, dict(raw, source=SOURCE_CN))
    except SchemaError as exc:
        logger.debug("站点 %s 未通过四字段校验，不并入默认扫描：%s", name, exc)
        return None
