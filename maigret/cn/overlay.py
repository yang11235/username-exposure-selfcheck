"""站点库三级合并（sites_cn.json > overrides.json > 上游 data.json）。

职责边界：
    - 上游条目 → v2 四字段形态的**适配**（`adapt_upstream`）
    - 三级文件的**深合并**（`load_sites`）
    - 不构造判定逻辑、不做网络请求

纯标准库，不 import aiohttp / 上游模块。

两个必须知道的上游事实（都与文档措辞有出入，已在代码里处理）：
    1. 字段拼写是 `presenseStrs`（少一个 c），不是 `presenceStrs`。全库 836 处
       都是这个拼法，`presenceStrs` 一处也没有。这是上游的历史笔误，不能"顺手修正"，
       否则上游一升级就全丢。
    2. 上游 URL 模板的占位符是 `{username}`，v2 统一为 `{account}`（见 model.pretty_url
       的 `template.format(account=...)`）。适配时必须转换，否则 9,535 个站点全部
       过不了 schema 校验。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .model import SOURCE_CN, SOURCE_OVERRIDES, SOURCE_UPSTREAM, Site, site_from_mapping
from .schema import SchemaError, validate_site

logger = logging.getLogger(__name__)

# 用 __file__ 定位，不依赖 cwd —— 否则在任意目录下运行都会找不到数据文件
CN_DIR = Path(__file__).resolve().parent
DATA_DIR = CN_DIR / "data"
UPSTREAM_DB = CN_DIR.parent / "resources" / "data.json"
OVERRIDES_FILE = DATA_DIR / "overrides.json"
SITES_CN_FILE = DATA_DIR / "sites_cn.json"

UPSTREAM_PLACEHOLDER = "{username}"
ACCOUNT_PLACEHOLDER = "{account}"

# 上游 checkType 缺省时的语义：只比状态码
DEFAULT_E_CODE = 200
DEFAULT_M_CODE = 404

# adapt_upstream 需要从 raw 覆盖到 v2 结果的字段。
# 有这一层，overrides.json 才能既写上游风格（checkType/presenseStrs），
# 又写 v2 风格（直接给 e_code/e_string），两种都能生效。
#
# 注意这里**不再是白名单**：adapt_upstream 以 raw 为底重建，白名单外的键会原样
# 保留（site_from_mapping 会忽略它不认识的键）。否则 overrides 里写一个
# 拼写不同的字段（例如 `url_main`）会静默失效，排查起来毫无线索。
_V2_OVERRIDE_KEYS = (
    "uri_check",
    "uri_pretty",
    "e_code",
    "e_string",
    "e_strings",
    "m_code",
    "m_string",
    "m_strings",
    "m_empty",
    "strip_bad_char",
    "method",
    "post_body",
    "headers",
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
    "disabled",
    "cat",
    "id_type",
    "tags",
    "urlMain",
    "url_main",
    "alexaRank",
    "alexa_rank",
)


# ----------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------
def _as_list(value: Any) -> List[str]:
    """字符串当单元素列表，不能 list() 拆成字符。

    上游 usernameClaimed / usernameUnclaimed 是裸字符串，v2 的 known /
    known_absent 是列表 —— 这是最容易踩的转换坑（model.py 里已有同样处理，
    这里重复一份是因为 adapt_upstream 输出的是 dict，还没进 Site）。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    # 丢掉 null：str(None) 会变成字面量 "None"，进到判据串里就会误命中
    return [str(v) for v in value if v is not None]


def _to_account(value: Any) -> Any:
    """把 {username} 换成 {account}，递归处理 dict / list（post_body 里也会出现）。"""
    if isinstance(value, str):
        return value.replace(UPSTREAM_PLACEHOLDER, ACCOUNT_PLACEHOLDER)
    if isinstance(value, dict):
        return {k: _to_account(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_account(v) for v in value]
    return value


def deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    """逐站深合并：dict 递归下钻，其他类型（含 list）整体替换，null 表示删除。

    为什么必须递归而不是整条替换：overrides.json 里只想改一个 `e_string` 时，
    整条替换会把 urlMain / headers / known 全部冲掉，等于把一条规则写残。

    两条刻意选择的语义：
        - list 整体替换：覆盖 `presenseStrs` 时希望能删掉旧串，合并语义
          （保留旧元素）在规则场景里只会制造歧义
        - null 删除键（RFC 7386 JSON Merge Patch 的约定）：上游有些字段是**有害的**，
          比如 Weibo 的 headers 里硬编码了一个早已失效的 Cookie，带着它请求只会
          更容易被风控。不能"改成空值"，只能删掉，所以需要 null 这一条通道
    """
    merged: Dict[str, Any] = dict(base)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
            continue
        old = merged.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            merged[key] = deep_merge(old, value)
        else:
            merged[key] = value
    return merged


# ----------------------------------------------------------------------
# 上游条目适配
# ----------------------------------------------------------------------
def adapt_upstream(name: str, raw: Mapping[str, Any]) -> Dict[str, Any]:
    """上游 Maigret 条目 → v2 四字段形态的 dict（不构造 Site）。

    上游的判定语义有三种，全部映射成四字段：
        message       → e_code=200, m_code=200, 正/反字符串各取第一个，
                        其余串进 e_strings / m_strings（语义是"任一命中"，
                        对应上游 checking.py 的 any(presenseStrs)）
        status_code   → e_code=200, m_code=404, 字符串空（合法配置，判定时
                        走 status_only 的固定置信度）
        无 checkType  → 同 status_code（上游 6,387 个站走这条默认路径）
        response_url  → 语义是"禁用重定向 + 2xx + presenseStrs"（checking.py），
                        所以有正例串时按 message 映射；没有串才降级为 status_only。
                        把它整体当 status_code 等于删掉字符串校验 —— 任何 200
                        （包括跳回首页）都会被判成 CLAIMED。

    `raw` 允许已经是"上游 + overrides 深合并"的结果，因此末尾会再用 v2 原生字段
    覆盖一遍 —— 这样 overrides 里写 `"checkType": "message"` 或直接写
    `"e_string": "..."` 都能生效。

    结果以 raw 为底（不是白名单重建）：未知字段原样保留，避免"写了但静默失效"。
    """
    check_type = (raw.get("checkType") or "").strip() or "status_code"
    # 注意拼写：上游就是 presenseStrs
    # 判据串里的 {username} 也要转成 {account}：上游有 6 个站的 presenseStrs
    # 直接写着占位符，不转就会以"上游原样形态"漏过 schema 的占位符检查。
    presence = [_to_account(s) for s in _as_list(raw.get("presenseStrs"))]
    absence = [_to_account(s) for s in _as_list(raw.get("absenceStrs"))]

    # response_url 只有"带正例串"时才需要保留字符串校验
    message_like = check_type == "message" or (check_type == "response_url" and bool(presence))

    if message_like:
        e_code = m_code = 200
        # 主判据只取第一个串。
        #
        # 曾经把 presenseStrs[1:] 填进 e_strings 当"任一命中"的候选，理由是
        # 上游 checking.py 对多个正例串用的是 any()。这是**错的**，而且错得
        # 很隐蔽：上游 message 的语义是「absence 优先」——只要反例串命中就判
        # AVAILABLE，正例串再多也不参与；而四字段协议要求存在侧与不存在侧
        # 各自独立判定。把多串当成独立的 OR 候选，等于把上游"反例优先"的
        # 保护拆掉了。
        #
        # 掘金就是活例子：presenseStrs = ['"user_name":', '"user_id":']，
        # 而"用户不存在"的响应里是 `"user_name":""` 和 `"user_id":"0"` ——
        # 两个正例串**都**命中，于是每次都判 rule_ambiguous。
        # 自检脚本（utils/cn_selfcheck.py）实测抓到的。
        #
        # 所以这里只取第一个，附加候选串只能由策展数据显式声明（作者知道
        # 那个串只在单侧出现才敢写）。
        e_string = presence[0] if presence else ""
        m_string = absence[0] if absence else ""
        e_strings = _as_list(raw.get("e_strings"))
        m_strings = _as_list(raw.get("m_strings"))
    else:
        # status_code / response_url（无串）/ 未标注
        e_code, m_code = DEFAULT_E_CODE, DEFAULT_M_CODE
        e_string = m_string = ""
        e_strings, m_strings = [], []

    uri_check = raw.get("urlProbe") or raw.get("url") or ""
    uri_pretty = raw.get("url") or uri_check

    # 以 raw 为底并整体转换占位符：requestPayload / url 里的 {username} 也要换掉，
    # 否则 site_from_mapping 在 post_body 为 None 时会回落到未转换的 requestPayload。
    result: Dict[str, Any] = _to_account(dict(raw))
    result.update(
        {
            "urlMain": raw.get("urlMain") or "",
            "tags": _as_list(raw.get("tags")),
            "id_type": raw.get("type") or "username",
            "uri_check": _to_account(uri_check),
            "uri_pretty": _to_account(uri_pretty),
            # 上游 24 个站用 requestMethod 表示 POST：只读 v2 的 method 会让这些站
            # 变成 GET，body 发不出去，永远判不出来。
            "method": (raw.get("method") or raw.get("requestMethod") or "GET").upper(),
            "post_body": _to_account(raw.get("requestPayload")),
            "e_code": e_code,
            "e_string": e_string,
            "e_strings": e_strings,
            "m_code": m_code,
            "m_string": m_string,
            "m_strings": m_strings,
            "executor": "http",
            "headers": dict(raw.get("headers") or {}),
            "protection": _as_list(raw.get("protection")),
            "known": _as_list(raw.get("usernameClaimed")),
            "known_absent": _as_list(raw.get("usernameUnclaimed")),
            "alexaRank": raw.get("alexaRank"),
            "disabled": bool(raw.get("disabled", False)),
            "valid": True,
        }
    )

    # v2 原生字段优先级最高（同样转换占位符：e_string 写成 {username} 时
    # 应变成 {account} 再被 schema 拒绝，而不是以原样形态漏过检查）
    for key in _V2_OVERRIDE_KEYS:
        if key in raw and raw[key] is not None:
            result[key] = _to_account(raw[key])

    # method 统一大写：上游 requestMethod / v2 method / overrides 三种来源的写法都
    # 可能不规范（"post" / "Post"），执行器按大写匹配，这里做最后一次归一。
    result["method"] = str(result.get("method") or "GET").upper()

    return result


# ----------------------------------------------------------------------
# 数据加载
# ----------------------------------------------------------------------
def load_upstream_raw(db_path: Optional[str] = None) -> Dict[str, Any]:
    """读取上游站点库，返回 {站点名: 原始条目}。

    `db_path` 用于测试替换数据源；默认按 __file__ 定位到
    `maigret/resources/data.json`（只读，绝不写回）。
    """
    path = Path(db_path) if db_path else UPSTREAM_DB
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"找不到上游站点库：{path}") from exc

    sites = data.get("sites")
    if not isinstance(sites, dict):
        raise ValueError(f"{path} 缺少顶层 sites 对象")
    return sites


def _load_overlay_sites(path: Path) -> Dict[str, Any]:
    """读 `{"sites": {...}}` 形态的 overlay 文件；文件不存在视为空（不报错）。"""
    if not path.exists():
        logger.debug("overlay 文件不存在，跳过：%s", path)
        return {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    sites = data.get("sites") or {}
    if not isinstance(sites, dict):
        raise ValueError(f"{path} 的 sites 必须是对象")
    return sites


def load_sites(
    db_path: Optional[str] = None,
    use_overlays: bool = True,
) -> Dict[str, Site]:
    """加载并合并三级站点库，返回 {站点名: Site}。

    合并优先级：sites_cn.json > overrides.json > 上游 data.json，且逐站深合并
    （不是整条替换，见 `deep_merge`）。sites_cn.json 里的站点一律
    `force_include=True`，不参与 alexaRank 排序（§5.2）。

    `use_overlays=False` 只读上游库，用于测试隔离 —— 否则拿一个小 fixture 库
    跑测试时，会莫名多出 30 个中文站和 overrides 里的条目，断言全部对不上。

    校验失败的站点**保留但标记 valid=False**：
        - 保留是为了让诊断能看到"这条规则长什么样、缺什么"
        - 标记 False 是为了守住一条不变量：`valid=True` ⇔ 四字段可判定。
          否则扫描器会拿着一条判不出来的规则去请求，产出必然是误报。
    逐站失败只记 warning，不中断 —— 9,535 条里必然有一批不合格。
    """
    upstream = load_upstream_raw(db_path)
    overrides = _load_overlay_sites(OVERRIDES_FILE) if use_overlays else {}
    cn_sites = _load_overlay_sites(SITES_CN_FILE) if use_overlays else {}

    # 保持上游顺序（报告输出稳定），新增站点按 overlay 文件顺序追加
    names = list(dict.fromkeys([*upstream, *overrides, *cn_sites]))

    sites: Dict[str, Site] = {}
    failed: List[str] = []

    for name in names:
        raw = dict(upstream.get(name) or {})
        if name in overrides:
            raw = deep_merge(raw, overrides[name])

        if name in upstream or name in overrides:
            adapted = adapt_upstream(name, raw)
        else:
            # 只存在于 sites_cn.json 的新站（如小红书）：上游没有任何可适配的条目，
            # 这里给空 dict 而不是 adapt 出的默认值 —— 否则会给它凭空塞上
            # e_code=200/m_code=404 的状态码判据，等于伪造一条没验证过的规则。
            adapted = {}

        if name in cn_sites:
            # sites_cn 条目是 v2 形态，直接覆盖适配结果；深合并保留上游的
            # headers / alexaRank / known 等没被显式改写的字段
            adapted = deep_merge(adapted, cn_sites[name])
            adapted["force_include"] = True
            source = SOURCE_CN
        elif name in overrides:
            source = SOURCE_OVERRIDES
        else:
            source = SOURCE_UPSTREAM
        adapted["source"] = source

        try:
            sites[name] = validate_site(name, adapted)
        except SchemaError as exc:
            site = site_from_mapping(name, adapted, source=source)
            site.valid = False
            sites[name] = site
            failed.append(str(exc))

    if failed:
        preview = "；".join(failed[:5])
        more = f"…（共 {len(failed)} 个）" if len(failed) > 5 else ""
        logger.warning(
            "%d 个站点未通过四字段校验，已标记 valid=False 并保留：%s%s",
            len(failed),
            preview,
            more,
        )

    return sites
