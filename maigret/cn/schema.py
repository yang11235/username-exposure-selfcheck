"""站点数据 schema 校验（v2 四字段协议）。

职责：在**配置期**拦住那些注定误报的规则，而不是等扫描时才发现判不出来。
对应《开发文档 v2.0》§3.2.1（四字段协议）、§5.3 硬规则 3、§6.1（Schema CI）。

纯标准库，不 import aiohttp / 上游模块，可脱离 maigret 运行时单测。

校验的边界（刻意如此）：
    - 只校验"能不能判"，不校验"判得准不准"。规则值本身是否有效要靠
      `--self-check` 实测，schema 管不了。
    - `valid=False` 的条目是**显式声明"我还不能用"**，跳过四字段完整性检查，
      但危险规则仍然拒绝 —— 一条永远判不出来的规则，即使标了 invalid 也不该入库，
      否则下次有人把它改成 valid 就直接带病上线。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Tuple, Union

from .model import SOURCE_UPSTREAM, Site, site_from_mapping

logger = logging.getLogger(__name__)

# v2 的占位符统一为 {account}；上游用 {username}，转换由 overlay.adapt_upstream 负责
ACCOUNT_PLACEHOLDER = "{account}"


class SchemaError(ValueError):
    """站点定义不合法。

    带 `site` 与 `reason` 两个属性，便于 CI 输出精确定位到哪一条规则坏了。
    """

    def __init__(self, site: str, reason: str):
        self.site = site
        self.reason = reason
        super().__init__(f"[{site}] {reason}")


def _as_text(value: Any) -> str:
    """把 None 归一成空串，其余转 str（保留原始语义用于 strip 判空）。"""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_string_list(value: Any) -> List[str]:
    """把附加候选串归一成字符串列表。

    单个字符串按单元素列表处理（和 model.as_list 口径一致）——否则
    `"e_strings": "x"` 会被逐字符拆开，变成 1 字符判据。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [_as_text(v) for v in value]
    return [_as_text(value)]


def _check_code_type(name: str, field: str, code: Any) -> None:
    """状态码必须是 int。

    特意排除 bool：`True == 1`，一个写错的 `"e_code": true` 会静默变成 1。
    也特意不接受 `"200"` 字符串 —— 判定时要和 `resp.status` 比大小，
    字符串比较永远不相等，只会把站点变成永远 AVAILABLE。
    """
    if code is None:
        return
    if isinstance(code, bool) or not isinstance(code, int):
        raise SchemaError(
            name,
            f"{field} 必须是整数状态码，实际是 {type(code).__name__}: {code!r}",
        )


def _check_string_type(name: str, field: str, value: Any) -> None:
    """判据字符串必须是 str。

    写成列表（`["x"]`）不会立刻报错，但会一路传到 `"x" in body` 才炸，
    而且报的是 TypeError 不是配置错误 —— 在配置期拦住更省事。
    """
    if value is None:
        return
    if not isinstance(value, str):
        raise SchemaError(
            name, f"{field} 必须是字符串，实际是 {type(value).__name__}: {value!r}"
        )


def _payload_has_placeholder(payload: Any) -> bool:
    """POST body 里是否带用户名占位符（递归检查）。

    为什么需要这条：文档 §3.2.1 一边要求支持 `post_body`（现代站点搜索多为
    POST JSON），一边要求 `uri_check` 必须含占位符 —— 这两条对同一批站点是
    冲突的。Discord、LeetCode、HackTheBox 这类站点的 URL 是固定的
    `/api/v9/unique-username/unique-usernames`，用户名在请求体里。
    按"占位符必须出现在 uri_check"一刀切，这 8 个站永远进不了库。
    正确的判据是：**占位符必须出现在能带上它的地方**，URL 或 body 任一即可。
    """
    if isinstance(payload, str):
        return ACCOUNT_PLACEHOLDER in payload
    if isinstance(payload, Mapping):
        return any(_payload_has_placeholder(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_payload_has_placeholder(v) for v in payload)
    return False


# 判据串的最小长度。1~2 个字符的串（上游真实存在 `e_string="1"` 的规则）
# 在 HTML 里必然误命中：任何含数字 1 的页面都会被判成"账号存在"。
MIN_STRING_LEN = 3

# 只回显用户名、证明不了账号存在的占位符。上游的 `{username}` 由 overlay 转成
# `{account}`，两个都要拒绝 —— 否则"上游原样形态"会从转换缝里漏过去。
_ECHO_PLACEHOLDERS = (ACCOUNT_PLACEHOLDER, "{username}")


def _check_short_string(name: str, field: str, value: str) -> None:
    """判据串过短 = 在 HTML 里必然误命中。

    空串是合法的（status_only 规则），只有"非空但太短"才拒绝。
    m_string 一并检查：只盯 e_string 会漏掉反向的假 AVAILABLE ——
    一个 1 字符的 m_string 会把"页面里恰好有这个字符"判成"账号不存在"。
    """
    stripped = value.strip()
    if not stripped or len(stripped) >= MIN_STRING_LEN:
        return
    raise SchemaError(
        name,
        f"{field}={stripped!r} 只有 {len(stripped)} 个字符：这么短的串在 HTML 里"
        f"必然误命中（任何含该字符的页面都会被判成命中），判据至少需要 "
        f"{MIN_STRING_LEN} 个字符；若确实只能靠状态码，请留空改用 status_only",
    )


def _check_substring_trap(
    name: str,
    e_code: Any,
    m_code: Any,
    e_string: str,
    m_string: str,
) -> None:
    """同码 + 互为子串 = 必然双命中，永远只能输出 RULE_AMBIGUOUS。

    上游真实实例：Runescape 的 e_string="loggedIn" 是 m_string
    '{"error":"NO_PROFILE","loggedIn":"false"}' 的子串。这种规则一条都给不出
    "不存在"的结论，入库只会污染存活率统计。
    """
    if e_code is None or m_code is None or e_code != m_code:
        return
    e_stripped = e_string.strip()
    m_stripped = m_string.strip()
    if not e_stripped or not m_stripped:
        return
    if e_stripped in m_stripped or m_stripped in e_stripped:
        raise SchemaError(
            name,
            f"e_code == m_code == {e_code} 且 e_string({e_stripped!r}) 与 "
            f"m_string({m_stripped!r}) 互为子串：任何命中 m_string 的响应都会同时"
            "命中 e_string，该规则必然双命中、永远给不出「不存在」",
        )


def _check_echo_placeholder(name: str, field: str, value: str) -> None:
    """判据串就是用户名占位符 = 只能证明"页面回显了用户名"。

    不存在的用户页同样会回显用户名（"用户 xxx 不存在"），所以这种规则
    对存在与不存在没有区分力，应改用页面上的具体字段名（如 '"user_name":'）。
    """
    stripped = value.strip()
    if not stripped or stripped not in _ECHO_PLACEHOLDERS:
        return
    raise SchemaError(
        name,
        f"{field}={stripped!r} 只是用户名占位符：只能证明页面回显了用户名，"
        "证明不了账号存在，应改用页面上的具体字段名（如 '\"user_name\":'）",
    )


def validate_site(name: str, raw: Mapping[str, Any]) -> Site:
    """校验单个站点定义，返回 Site；不合法抛 SchemaError。

    规则（顺序即优先级，先报最致命的问题）：
        1. 危险规则：e_code == m_code 且 e_string / m_string 都为空 —— 永远无法判定
        2. 四字段完整性（仅 valid=True 时要求）
        3. 类型与占位符：状态码必须是 int，uri_check 必须含 {account}
        4. 判据串过短（<3 字符）—— 在 HTML 里必然误命中
        5. 同码 + e_string/m_string 互为子串 —— 必然双命中
        6. 判据串只是用户名占位符 —— 只能证明页面回显，证明不了账号存在

    第 4~6 条只对 valid=True 生效：valid=False 的条目是"这条规则还不能用"的
    声明，把它拒之门外等于让待办从库里消失。
    """
    if not isinstance(raw, Mapping):
        raise SchemaError(name, f"站点定义必须是对象，实际是 {type(raw).__name__}")

    valid = bool(raw.get("valid", True))
    e_code = raw.get("e_code")
    m_code = raw.get("m_code")
    e_string = _as_text(raw.get("e_string"))
    m_string = _as_text(raw.get("m_string"))
    e_strings = _as_string_list(raw.get("e_strings"))
    m_strings = _as_string_list(raw.get("m_strings"))
    # 「0 字节 body 表示不存在」的显式开关，见 model.Site.m_empty 的说明。
    m_empty = bool(raw.get("m_empty") or raw.get("mEmpty"))

    # ---- 规则 1：危险规则，无论 valid 与否都拒绝 ----
    # 状态码分不开、字符串也没有 —— 这条规则对"存在"和"不存在"输出完全一致，
    # 判定器只能靠猜，入库即等于批量误报（§5.3 硬规则 3）。
    if (
        e_code is not None
        and m_code is not None
        and e_code == m_code
        and not e_string.strip()
        and not m_string.strip()
    ):
        raise SchemaError(
            name,
            f"危险规则：e_code == m_code == {e_code} 且 e_string/m_string 均为空，"
            "该规则永远无法区分存在与不存在（文档 §5.3 硬规则 3）",
        )

    # ---- 规则 3（先做类型，避免后续比较踩到隐式转换）----
    _check_code_type(name, "e_code", e_code)
    _check_code_type(name, "m_code", m_code)
    _check_string_type(name, "e_string", raw.get("e_string"))
    _check_string_type(name, "m_string", raw.get("m_string"))

    # 与 site_from_mapping 相同的取值优先级，让本函数也能直接吃上游形态的字典
    uri_check = raw.get("uri_check") or raw.get("urlProbe") or raw.get("url") or ""
    if not isinstance(uri_check, str):
        raise SchemaError(name, f"uri_check 必须是字符串，实际是 {type(uri_check).__name__}")
    uri_check = uri_check.strip()

    # 空 uri_check 只在 valid=True 时算错：invalid 条目允许只写"我知道它长什么样"，
    # 但只要写了模板，就必须能拼出地址 —— 否则是笔误，不是"未完成"。
    # 例外：POST 型探测的占位符在 body 里，URL 是固定端点，见 _payload_has_placeholder。
    placeholder_ok = ACCOUNT_PLACEHOLDER in uri_check or _payload_has_placeholder(
        raw.get("post_body") or raw.get("requestPayload")
    )
    if uri_check and not placeholder_ok:
        raise SchemaError(
            name,
            f"uri_check 缺少 {ACCOUNT_PLACEHOLDER} 占位符，且 post_body 中也没有，"
            f"无法拼出探测请求：{uri_check!r}"
            "（上游的 {username} 需由 overlay.adapt_upstream 转换）",
        )

    # ---- 站内搜索执行器：判据不是四字段，单独校验 ----
    #
    # 为什么必须分开：搜索结果页**必然回显你搜的那个词**。拿 `e_string` 去判
    # "这个账号存在"，每一次搜索都命中，于是每个用户都"注册过"每一个站点。
    # 这类站点的判据是"结果里有没有逐字相同的句柄"，所以校验对象也跟着换。
    #
    # 仍然要求 uri_check 带占位符：搜索地址必须能塞进用户名，否则发出去的
    # 永远是同一个请求，结论与目标无关。
    if valid and str(raw.get("executor") or "").strip().lower() == "search":
        if not uri_check:
            raise SchemaError(name, "executor=search 但 uri_check 为空，没有搜索地址")

        pattern = str(raw.get("search_handle_pattern") or "").strip()
        if not pattern:
            raise SchemaError(
                name,
                "executor=search 但缺少 search_handle_pattern：没有它就无法把"
                "结果条目和页面上的其他链接分开，只能对整页瞎猜",
            )
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise SchemaError(
                name, f"search_handle_pattern 不是合法正则：{exc}"
            ) from exc
        if compiled.groups < 1:
            # 没有捕获组就只能拿整段匹配当句柄，通常连 HTML 标签一起带进来，
            # 与用户名逐字比对永远不相等 —— 规则看着有，实际永远判不出来。
            raise SchemaError(
                name,
                "search_handle_pattern 必须至少有一个捕获组，第 1 组作为句柄",
            )

        return site_from_mapping(
            name, dict(raw), source=raw.get("source") or SOURCE_UPSTREAM
        )

    # ---- 规则 2：四字段完整性 ----
    if valid:
        if not uri_check:
            raise SchemaError(name, "valid=true 但 uri_check 为空，无法发起探测")

        missing_codes = [f for f, v in (("e_code", e_code), ("m_code", m_code)) if v is None]
        if missing_codes:
            raise SchemaError(
                name, "valid=true 但缺少状态码字段：" + "、".join(missing_codes)
            )

        if e_code == m_code:
            # 状态码相同 → 字符串是唯一判据，两个方向都必须有，缺一个就判不了。
            #
            # 例外是 m_empty：这类接口用"0 字节 body"表示不存在，本来就没有
            # 反例串可写。把它按缺失打回会逼作者去编一个不存在的字符串，
            # 那才是真的把规则弄坏。
            missing_strings = [
                f
                for f, v in (("e_string", e_string), ("m_string", m_string))
                if not v.strip()
            ]
            if m_empty:
                missing_strings = [f for f in missing_strings if f != "m_string"]
            if missing_strings:
                raise SchemaError(
                    name,
                    f"e_code == m_code == {e_code}，状态码无法区分，"
                    f"必须同时提供 {' 和 '.join(missing_strings)}",
                )
        elif not e_string.strip() and not m_string.strip():
            # 上游 status_code 型（200/404 + 空字符串）属于这种情况：合法，但只有状态码判据。
            # 不拒绝（§5.4 用 status_only 的固定置信度兜底），仅留痕。
            logger.debug("%s: 仅状态码判据，无字符串校验", name)

        # ---- 规则 4/5/6：三类"必然误报"的判据，valid=True 时硬拒绝 ----
        # 只对 valid=True 生效：valid=False 的条目本身就不参与判定，它的作用是
        # "把问题记下来"，此时拒绝入库只会让待办消失。
        _check_short_string(name, "e_string", e_string)
        _check_short_string(name, "m_string", m_string)
        _check_substring_trap(name, e_code, m_code, e_string, m_string)
        # 附加候选串不做长度校验（主串已校验过，附加串只是多一条退路），但占位符
        # 必须拒绝：附加串写 {account} 会让"任何回显用户名的页面"都命中，
        # 比短串更彻底地必然误报。
        for field, values in (
            ("e_string", [e_string, *e_strings]),
            ("m_string", [m_string, *m_strings]),
        ):
            for value in values:
                _check_echo_placeholder(name, field, value)

    source = raw.get("source") or SOURCE_UPSTREAM
    return site_from_mapping(name, dict(raw), source=source)


def validate_sites(
    mapping: Mapping[str, Mapping[str, Any]]
) -> Tuple[Dict[str, Site], List[str]]:
    """批量校验。返回（合法站点, 错误信息列表）。

    单站失败不影响其他站点 —— 9,535 条里必然有一批不满足四字段协议，
    批量场景下中断等于整个库加载失败。
    """
    sites: Dict[str, Site] = {}
    errors: List[str] = []

    for name, raw in mapping.items():
        try:
            sites[name] = validate_site(name, raw)
        except SchemaError as exc:
            errors.append(str(exc))
            logger.debug("schema 校验失败：%s", exc)

    return sites, errors


def assert_four_fields(sites: Union[Mapping[str, Site], Iterable[Site]]) -> None:
    """CI 严格断言：所有 `valid=True` 的站点必须满足四字段协议，否则抛 SchemaError。

    `valid=False` 的条目跳过 —— 它们的存在本身就是"这条规则还不能用"的声明
    （如小红书/抖音需浏览器执行器，见 sites_cn.json 的 `_note`）。
    如果连 invalid 条目也要求四字段，这个断言就只能永远失败，失去 CI 价值。
    """
    items = sites.items() if isinstance(sites, Mapping) else ((s.name, s) for s in sites)

    bad: List[str] = []
    names: List[str] = []
    for name, site in items:
        if not site.valid:
            continue
        try:
            # 复用同一套校验逻辑，避免"assert 通过但 validate 不通过"的两套标准
            validate_site(name, site.to_dict())
        except SchemaError as exc:
            bad.append(str(exc))
            names.append(name)

    if bad:
        preview = "；".join(bad[:5])
        more = f"（另有 {len(bad) - 5} 个未列出）" if len(bad) > 5 else ""
        raise SchemaError(
            "、".join(names[:5]) + (f" 等 {len(names)} 个站点" if len(names) > 5 else ""),
            f"{len(bad)} 个 valid 站点不满足四字段协议：{preview}{more}",
        )
