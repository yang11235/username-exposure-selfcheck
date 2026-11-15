"""四字段判定引擎（《开发文档 v2.0》§5.3）。

为什么判定要独立成一个模块：

    上游 `checking.py` 的判定本质是"200 就是命中"。中国站大量返回 200 的
    空壳页、风控页、登录墙，这套逻辑必然把"被拦截"判成"账号存在"。而一次
    这样的误报，就足以让整份报告失去可信度。

    所以这里的判定顺序是硬性的：**先判"被打断"，再判存在性**。顺序错了
    （哪怕只把 429 判断挪到后面一行），风控页里恰好出现的 e_string 就会
    让结论翻转成 CLAIMED。

依赖约束：

    - 纯标准库，不 import aiohttp：resp 是 duck-typed 的（只要有 .status 和 .text），
      测试可以直接传 SimpleNamespace，不需要起网络。
    - 不 import maigret.checking / maigret.result：既避免循环依赖（上游会反过来
      调本模块），也让判定逻辑能脱离运行时单独跑。
    - 与上游 MaigretCheckStatus 的一致性靠 `to_upstream_status()` + 枚举值逐字对齐
      来保证，而不是靠 import。
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional

from .scoring import score

logger = logging.getLogger(__name__)

# 用于把"正文"从 HTML 里剥出来（见 _visible_text）
_SCRIPT_OR_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# 弱特征单独成立的长度上限（字符，按可见文本算）。
#
# 风控页是"极简错误页"：几十到一两千字符，只有一句话加一个返回按钮。
# 正常内容页带框架、评论、推荐位，可见文本动辄上万字符。
# 超过这个长度还只有弱特征，说明那些词是正文的一部分（比如用户真的在
# 帖子里写了"验证码"），不是拦截提示。
_BLOCK_WEAK_MAX_TEXT = 5000


class Status(str, Enum):
    """判定状态。

    值必须与上游 `MaigretCheckStatus` **逐字一致**：hook 层会执行
    `MaigretCheckStatus(verdict.status.value)`，值写错会在运行时抛 ValueError。
    所以这里不是"自定义枚举"，而是上游枚举的一份镜像。
    """

    CLAIMED = "Claimed"      # 命中
    AVAILABLE = "Available"  # 未命中
    UNKNOWN = "Unknown"      # 检测被干扰，无法下结论
    ILLEGAL = "Illegal"      # 用户名在该站非法


class Reason(str, Enum):
    """结构化的"为什么没判定成功"。

    上游 `context` 字段是自由字符串（`str(check_error)`），报告和聚类都没法用。
    改成枚举后，"12 个站点需要登录才能判定"这类汇总才能自动生成（文档 §4.2）。
    """

    BLOCKED = "blocked"                    # 403/503/风控页
    RATE_LIMITED = "rate_limited"          # 429
    LOGIN_REQUIRED = "login_required"      # 登录墙
    CAPTCHA = "captcha"
    SIG_FAILED = "signature_failed"        # 签名校验失败
    RULE_AMBIGUOUS = "rule_ambiguous"      # e 与 m 同时命中，规则坏了
    RULE_MISSING = "rule_missing"          # 规则压根没配（e_code/m_code 都缺）
    STRUCTURE_CHANGED = "structure_changed"  # 两个都没命中，页面结构变了
    EMPTY_BODY = "empty_body"              # 2xx 但正文为空，请求通了而已
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    # 只用于站内搜索执行器（executors/search.py）：搜索结果区渲染出来了，
    # 但里面没有与目标逐字相同的句柄。**不等于不存在** —— 搜索有分页、有排序，
    # 多数站点还按昵称而不是句柄匹配，所以"第一页里没有"推不出"没注册"。
    # 单独立一个值，是为了让报告能把它和"页面结构变了"分开说：
    # 前者是搜索这类判据的天花板，后者是规则过期，用户要做的事完全不同。
    SEARCH_INCONCLUSIVE = "search_inconclusive"


@dataclass
class Verdict:
    """一次判定的结果。

    `rate_limited` 是独立布尔而不是 status 的一个变体（文档 §3.2.2 抄 holehe 的点）：
    "被限流"和"不存在"必须能分开，否则用户会以为自己没注册某个站。
    """

    status: Status
    reason: Optional[Reason] = None
    rate_limited: bool = False
    evidence: str = ""          # 命中的证据字符串（排查规则时最关键的信息）
    evidence_type: str = ""     # json_field / unique_string / status_only / name_match_only
    confidence: float = 0.0
    http_status: Optional[int] = None


# ---------------------------------------------------------------------------
# 页面特征识别
# ---------------------------------------------------------------------------

# 风控页特征分两档，这是本模块最容易误伤的地方，取舍写在这里：
#
#   强特征：技术上不可能出现在正常页面里（Cloudflare 挑战页的专属脚本名、
#           拦截页的固定标题）。命中即判定，不需要任何别的证据。
#   弱特征：正常页面文案里也会出现。"验证码"是登录框的标配，"请稍后再试"是
#           客服提示。凭一个词判 BLOCKED，会把真实命中打成 UNKNOWN。
#
# 弱特征因此**不单独决定状态**，必须和字符串证据一起看（见 judge 里的
# `_WEAK_DISCOUNT`）：页面已经有站点自己的正例/反例串时，说明内容是真的渲染
# 出来了，弱词只是页面上的普通文案，只降置信度；只有"证据仅剩状态码"时，
# 状态码 + 可疑文案两者都不硬，才保守地判 BLOCKED。
#
# 这里刻意**没有**"页面很短所以是拦截页"这类用长度当判据的分支：真实风控页
# 3.5KB 很常见，正常首页也可能被压到 2KB（SPA 空壳），长度分不开两者。
#
# 两个方向的误判代价并不对称：
#   漏判（真风控页判成正常页）→ 可能产出假 CLAIMED，这是最坏结果；
#   误判（正常页判成风控页）→ 只是 UNKNOWN，用户看到"未判定"。
# 但"宁可误判"不等于"可以用长度猜"——误判同样消耗可信度，且 20KB 正常主页
# 被登录弹窗里的两个词打成 UNKNOWN，等于把最常见的一类站点全部判不出来。
_BLOCK_STRONG = (
    "cf-browser-verification",   # Cloudflare 挑战页专属资源名
    "cf_chl_opt",
    "just a moment",             # Cloudflare 挑战页标题
    "attention required",        # Cloudflare 拦截页标题
    "access denied",
    "403 forbidden",
    "您的访问过于频繁",
    "访问过于频繁",
    "请求过于频繁",
    "系统繁忙",                   # 淘宝/支付宝系风控页常见
    # 小红书实测（2026-11）：状态码 200、标题"安全限制"、正文只有一句
    # "当前网络存在风险，请切换可靠网络后重试 300012"。这是**网络层**风控，
    # 和账号是否存在毫无关系，而 200 状态码会让 status_only 规则直接判成
    # "存在"。整句足够长、语义足够专一，正常页面不可能出现，按强特征处理。
    "当前网络存在风险",
)

_BLOCK_WEAK = (
    "访问异常",
    "网络异常",
    "异常访问",
    "请稍后再试",
    "安全验证",
    "人机验证",
    "滑动验证",
    "验证码",
    "captcha",
    "拒绝访问",
    # 小红书的拦截页标题。只放弱特征：正常页面里"账号安全限制"这类文案
    # 是存在的（安全中心、帮助中心），凭它单独判 BLOCKED 会误伤。
    "安全限制",
)

# 弱特征命中、但页面确有字符串证据时，对置信度的折扣。
# 不改变状态的理由：证据串是站点自己渲染的内容，比"页面上出现了验证码三个字"
# 硬得多；但页面同时带着可疑文案，说明环境不干净，分数该降一档。
_WEAK_DISCOUNT = 0.8

_LOGIN_WALL_MARKERS = (
    "请先登录",
    "登录后查看",
    "您需要登录",
    "请登录后",
    "sign in to continue",
    "login required",
)

# 验证码识别刻意用"容器/脚本特征"而不是"验证码"三个字：
# 后者在登录页文案里到处都是，前者只会在真的挂了验证码组件时出现。
_CAPTCHA_MARKERS = (
    "geetest",              # 极验，国内最常用
    "nc_1_wrapper",         # 阿里 NoCaptcha 容器
    "nc_1_n1z",
    "secsdk-captcha",       # 阿里 secsdk 验证码
    "verify-slide",         # 通用滑块容器
    "slide-verify",
    "tcaptcha",             # 腾讯防水墙
    "yidun",                # 网易易盾
    "captcha-container",
)


def _as_text(text: Any) -> str:
    """把 resp.text 统一成 str。

    某些执行器（或测试用的假对象）会给 bytes；非字符串一律当空串，
    否则后面的 `in` 判断会抛 TypeError。
    """
    if text is None:
        return ""
    if isinstance(text, str):
        return text
    if isinstance(text, bytes):
        return text.decode("utf-8", "replace")
    return str(text)


def _matched_markers(lowered_body: str, markers: Iterable[str]) -> list:
    """返回命中的特征词（去重后）。传入的 body 必须已转小写。"""
    return [m for m in markers if m in lowered_body]


def _visible_text(html: str) -> str:
    """去掉 `<script>` / `<style>` / 注释后的正文。

    为什么弱特征词必须只在可见文本上匹配：

        "验证码" 和 "captcha" 在正常站点的打包 JS 里到处都是。实测 Gitee 的
        个人主页（108KB）同时含这两个词 —— 登录弹窗组件和风控 SDK 都打包在
        bundle 里。对整页裸匹配的结果是：一个完全正常的个人主页被判成风控页，
        用户看到"被拦截"，而真相是"你的账号好好的"。

        这类误判比漏判更伤信任：漏判只是"没查到"，误判是"骗了你"。

    强特征不走这个函数：`cf-browser-verification` 恰恰出现在 `<script src>`
    里，剥掉脚本就再也识别不了 Cloudflare 挑战页了。
    """
    if not html:
        return ""
    without_scripts = _SCRIPT_OR_STYLE_RE.sub(" ", html)
    return _HTML_COMMENT_RE.sub(" ", without_scripts)


def block_signals(text: str) -> tuple:
    """返回 (强特征命中, 弱特征命中)，两者都是去重后的元组。

    拆开返回而不是合成一个 bool，是因为这两档的**用法不同**：强特征命中即
    BLOCKED；弱特征要看证据形态（有字符串证据 → 只降置信度；只剩状态码 →
    判 BLOCKED）。把它们压成一个 bool 就必然要在调用处重新扫一遍正文。

    两档的匹配范围也不同（见 _visible_text 的说明）：
        强特征 → 整页（Cloudflare 标记在 script 里）
        弱特征 → 可见文本（正常页面的 JS 里全是"验证码"）
    """
    body = _as_text(text)
    lowered_full = body.lower()
    lowered_visible = _visible_text(body).lower()
    return (
        tuple(_matched_markers(lowered_full, _BLOCK_STRONG)),
        tuple(_matched_markers(lowered_visible, _BLOCK_WEAK)),
    )


def looks_like_block_page(text: str) -> bool:
    """只凭正文就能断定是风控页 —— 也就是**命中强特征**。

    弱特征（"验证码"这类正常页面也会出现的词）刻意不在这里下结论：判定需要
    字符串证据的上下文，见 block_signals 与 judge。所以本函数的语义是
    "这段正文不可能来自正常页面"，而不是"这段正文像风控页"。
    """
    strong, _ = block_signals(text)
    return bool(strong)


def looks_like_login_wall(text: str) -> bool:
    """识别登录墙：页面结构正常，但内容被"请先登录"挡住。"""
    lowered = _as_text(text).lower()
    return bool(_matched_markers(lowered, _LOGIN_WALL_MARKERS))


def looks_like_captcha(text: str) -> bool:
    """识别验证码组件（看容器特征，不看"验证码"文案）。"""
    lowered = _as_text(text).lower()
    return bool(_matched_markers(lowered, _CAPTCHA_MARKERS))


# ---------------------------------------------------------------------------
# 证据形态
# ---------------------------------------------------------------------------

def _looks_like_json_fragment(needle: str) -> bool:
    """判断证据串是不是结构化片段（JSON 键值 / 引号包裹的字段名）。

    为什么需要这个启发式：四字段协议里 e_string 既可以写 `"registered_at":`
    也可以写 `用户不存在`，两者的可信度差一档（1.0 vs 0.9），但 Site 里没有
    字段显式标注。用形态猜比统一按低档算更准确，也不会因为猜错而放宽——
    猜错只会让 unique_string 被当成 json_field 或反之，差 0.1。
    """
    stripped = needle.strip()
    if not stripped:
        return False
    if stripped[0] in "\"{[":
        return True
    return '":' in stripped


def _evidence_type(needle: str, username: Optional[str]) -> str:
    """推断证据形态。

    name_match_only 是最后一档：如果证据串就是用户名本身，那它只能证明
    "页面回显了用户名"，证明不了"这个用户存在"——很多站的 404 页也会回显。
    """
    if not needle:
        return "status_only"
    if username and needle.strip().lower() == username.strip().lower():
        return "name_match_only"
    if _looks_like_json_fragment(needle):
        return "json_field"
    return "unique_string"


def _hit(
    status: Optional[int],
    body: str,
    code: Optional[int],
    needle: str,
    extras: Iterable[str],
    username: Optional[str],
) -> tuple:
    """判定单侧（存在侧或不存在侧）是否命中，返回 (hit, evidence_type, matched)。

    主串与附加串是"任一命中"（对应上游 message 型的 any(presenseStrs)）。
    `matched` 返回**实际命中的那个串**：报告和日志里必须能看出命中的是哪一条，
    否则规则改版时只能知道"命中了"，不知道命中在旧串还是新串上。

    主串为空且没有附加串不是"没配"，而是 **status_only 规则**：只能靠状态码判。
    这种情况必须打上 status_only 标记，让 scoring 走固定 0.5 的档位。
    """
    if code is None:
        return False, "", ""
    if status != code:
        return False, "", ""

    candidates = [c for c in (needle, *extras) if c]
    if not candidates:
        return True, "status_only", ""

    for candidate in candidates:
        if candidate in body:
            return True, _evidence_type(candidate, username), candidate

    # 一个都没命中：返回主串的证据形态，调用方据此判断"这条规则本来靠什么判"
    return False, _evidence_type(candidates[0], username), ""


# ---------------------------------------------------------------------------
# 判定入口
# ---------------------------------------------------------------------------

def judge(resp: Any, site: Any, username: Optional[str] = None) -> Verdict:
    """判定一次探测响应。

    参数
    ----
    resp      duck-typed 响应对象，只需 `.status: int` 和 `.text: str`
    site      cn.model.Site（或任何有 e_code/e_string/m_code/m_string 的对象）
    username  被查询的用户名，仅用于识别 name_match_only 这类弱证据

    判定顺序（文档 §5.3，不可调整）：
        规则可用性 → 空 body → 429 → 403/503 → 强风控特征 → 登录墙 → 验证码
        → 四字段判定（弱风控特征在这里结合证据形态处理）

    "规则可用性"排在最前，连 429 都不判：规则本身不可用时，任何结论都没有
    意义 —— 一条 valid=false 的规则（比如状态码分不开又缺反例串）对任何响应
    都只能输出"未判定"，先花一次请求去分辨"是被限流还是没配好"没有价值。
    """
    # ---- 0. 规则可用性 ----
    # sites_cn.json 里 CSDN / Baidu / NGA 这类是 valid=false + e_code=m_code=200
    # + m_string=""。不拦这一条的话，一个不含 e_string 的 200 页会走到
    # "存在侧没命中、不存在侧只比状态码 → 命中"的分支，直接输出 AVAILABLE，
    # 等于告诉用户"你没有这个账号"。
    if not getattr(site, "valid", True):
        return unknown_verdict(Reason.RULE_MISSING, evidence="站点规则未通过校验")

    status = getattr(resp, "status", None)
    if not isinstance(status, int):
        status = None
    body = _as_text(getattr(resp, "text", ""))
    lowered = body.lower()

    # ---- 1. 空 body 的 2xx ----
    # WAF / 反向代理 / SPA 空壳返回"200 + 空 body"是中文站的常态：请求确实通了，
    # 但页面上没有任何可判定的内容。这时 e_code=200 的规则会命中存在侧，
    # 产出 CLAIMED 0.49 —— 正是本模块最想消灭的那类误报。
    # 空 body 只能证明"请求通了"，证明不了任何事，所以只能 UNKNOWN。
    #
    # 例外：HEAD 请求的 body 天然为空，这类规则需要按"响应头 + 状态码"单独判，
    # 得在 executor 层处理（当前没有 HEAD 支持），判定层不能一刀切地当成空响应。
    if status is not None and 200 <= status < 300 and not body.strip():
        # 例外一：站点显式声明了 m_empty —— 这类接口用"0 字节 body"表示不存在，
        # 空响应本身就是判据（百度贴吧的 user_json：存在的用户返回
        # `{"raw_name":...}`，不存在的返回空 body，没有反例串可写）。
        #
        # 为什么必须先确认状态码就是规则里写的 m_code：空 body 同时也是 WAF /
        # 反向代理 / SPA 空壳的常态。只在"状态码与规则声明的反例状态码一致"
        # 时采信，能挡掉"403 被网关改写成 200 空响应"这一类误判 —— 那正是
        # 把"被拦住"说成"你没有这个账号"的最短路径。
        #
        # 其余情况一律维持 UNKNOWN：空 body 只能证明请求通了，证明不了任何事。
        m_code = getattr(site, "m_code", None)
        if getattr(site, "m_empty", False) and status == m_code:
            return Verdict(
                Status.AVAILABLE,
                evidence=f"HTTP {status} 响应体为空（站点声明空响应即不存在）",
                evidence_type="empty_body",
                confidence=score(site, "empty_body"),
                http_status=status,
            )

        return Verdict(
            Status.UNKNOWN,
            reason=Reason.EMPTY_BODY,
            evidence=f"HTTP {status} 响应体为空",
            http_status=status,
        )

    # ---- 2. 限流 ----
    # 必须排在正文特征之前：429 页面里往往带着站点的通用模板，可能恰好包含 e_string。
    if status == 429:
        return Verdict(
            Status.UNKNOWN,
            reason=Reason.RATE_LIMITED,
            rate_limited=True,
            evidence="HTTP 429",
            http_status=429,
        )

    # ---- 3. 拦截 ----
    # 403/503 不看正文直接判：这两个状态码本身就是"拒绝服务"，正文内容不可信。
    if status in (403, 503):
        return Verdict(
            Status.UNKNOWN,
            reason=Reason.BLOCKED,
            evidence=f"HTTP {status}",
            http_status=status,
        )

    # ---- 4. 强风控特征 ----
    # 无条件优先，不看字符串证据：`cf-browser-verification`、`访问过于频繁` 这类词
    # 正常页面不可能出现，而真实风控页**会**带上站点的 e_string（blocked.html 里
    # 就带着知乎的 "url_token"）。若因为"e_string 命中了"就推翻强特征，
    # 风控页会被判成 CLAIMED，这是最坏的一种误报。
    strong_hits, weak_hits = block_signals(body)
    if strong_hits:
        logger.debug("站点 %s 命中强风控特征 %r", _site_name(site), strong_hits[0])
        return Verdict(
            Status.UNKNOWN,
            reason=Reason.BLOCKED,
            evidence=strong_hits[0],
            http_status=status,
        )

    # ---- 5. 登录墙 ----
    login_marker = _matched_markers(lowered, _LOGIN_WALL_MARKERS)
    if login_marker:
        return Verdict(
            Status.UNKNOWN,
            reason=Reason.LOGIN_REQUIRED,
            evidence=login_marker[0],
            http_status=status,
        )

    # ---- 6. 验证码 ----
    # 同样只看可见文本：`geetest` / `tcaptcha` 会出现在每个页面的 script src 里
    # （SDK 预加载），按整页匹配等于把所有接了验证码 SDK 的正常站点都判成拦截。
    captcha_marker = _matched_markers(_visible_text(body).lower(), _CAPTCHA_MARKERS)
    if captcha_marker:
        return Verdict(
            Status.UNKNOWN,
            reason=Reason.CAPTCHA,
            evidence=captcha_marker[0],
            http_status=status,
        )

    # ---- 7. 四字段正常判定 ----
    e_code = getattr(site, "e_code", None)
    m_code = getattr(site, "m_code", None)
    e_string = getattr(site, "e_string", "") or ""
    m_string = getattr(site, "m_string", "") or ""
    e_strings = list(getattr(site, "e_strings", None) or [])
    m_strings = list(getattr(site, "m_strings", None) or [])

    if e_code is None and m_code is None:
        # 规则本身没配好，不是用户的问题。
        # 单独用 RULE_MISSING 而不是 STRUCTURE_CHANGED：这两者要分开统计——
        # "规则没写"是待办（补规则），"结构变了"是故障（修规则），混在一个桶里
        # 会让存活率看板看不出该干什么（§6.2）。
        logger.warning("站点 %s 未配置 e_code/m_code，无法判定", _site_name(site))
        return Verdict(
            Status.UNKNOWN,
            reason=Reason.RULE_MISSING,
            evidence="站点未配置 e_code/m_code",
            http_status=status,
        )

    hit_e, e_type, e_matched = _hit(status, body, e_code, e_string, e_strings, username)

    # m_empty 站点的反例判据是"空 body"，不是状态码。空 body 的情况在第 1 步
    # 就已经返回了，能走到这里说明 body 非空 —— 此时不存在侧**一点证据都没有**。
    # 不能让它退回 `_hit` 的 status_only 分支：那样同一份非空 body 会同时命中
    # 存在侧和不存在侧，输出 RULE_AMBIGUOUS，等于这条规则永远失效。
    m_empty = bool(getattr(site, "m_empty", False))
    if m_empty and not m_string and not m_strings:
        hit_m, m_type, m_matched = False, "", ""
    else:
        hit_m, m_type, m_matched = _hit(status, body, m_code, m_string, m_strings, username)

    # ---- 7.1 弱风控特征：只在没有字符串证据时才升级为 BLOCKED ----
    # 有字符串证据（页面真的渲染出了站点自己的正例/反例串）时，弱词只是页面文案，
    # 不能改变结论 —— 20KB 的正常主页，登录弹窗里"验证码"+"安全验证"两个词
    # 同时出现是常态，凭它判 BLOCKED 会把真实命中全部打成 UNKNOWN。
    # 只剩状态码时，状态码和可疑文案两者都不硬，保守判 BLOCKED。
    weak_note = ""
    weak_factor = 1.0
    if weak_hits:
        has_string_evidence = (hit_e and e_type != "status_only") or (
            hit_m and m_type != "status_only"
        )
        # 弱特征还要过长度这一关：见 _BLOCK_WEAK_MAX_TEXT。
        # Gitee 的 108KB 主页含"验证码"+"captcha"（都在打包 JS 里），
        # 可见文本上万个字，明显不是拦截页。
        short_enough = len(_visible_text(body)) <= _BLOCK_WEAK_MAX_TEXT
        if not has_string_evidence and short_enough:
            logger.debug("站点 %s 弱风控特征 %r 且无字符串证据", _site_name(site), weak_hits)
            return Verdict(
                Status.UNKNOWN,
                reason=Reason.BLOCKED,
                evidence="、".join(weak_hits),
                http_status=status,
            )
        weak_note = f"（弱风控特征：{'、'.join(weak_hits)}）"
        weak_factor = _WEAK_DISCOUNT

    # 两个都命中 = 规则坏了（例如 e_string 是 m_string 的子串），不是用户的问题。
    # 绝不猜一个：猜错就是把"规则 bug"变成"用户账号存在"。
    if hit_e and hit_m:
        logger.warning(
            "站点 %s 规则歧义：e(%s:%r) 与 m(%s:%r) 同时命中，请修正规则",
            _site_name(site),
            e_code,
            e_string,
            m_code,
            m_string,
        )
        return Verdict(
            Status.UNKNOWN,
            reason=Reason.RULE_AMBIGUOUS,
            evidence=f"e={e_string!r} m={m_string!r} 同时命中",
            http_status=status,
        )

    if hit_e:
        confidence = score(site, e_type)
        # 非对称配置告警：存在侧只能靠状态码、不存在侧却有字符串校验，
        # 这是"200 空壳页"最容易漏网的一种配置，日志里留痕便于事后追规则。
        if e_type == "status_only" and (m_string or m_strings):
            logger.warning(
                "站点 %s 存在侧为 status_only、不存在侧有字符串校验（非对称配置），"
                "命中置信度固定为 %.2f",
                _site_name(site),
                confidence,
            )
        return Verdict(
            Status.CLAIMED,
            evidence=(e_matched or f"HTTP {e_code}") + weak_note,
            evidence_type=e_type,
            confidence=round(confidence * weak_factor, 2),
            http_status=status,
        )

    if hit_m:
        confidence = score(site, m_type)
        return Verdict(
            Status.AVAILABLE,
            evidence=(m_matched or f"HTTP {m_code}") + weak_note,
            evidence_type=m_type,
            # 未命中也走同一套评分：状态码单独说"不存在"同样不可靠
            # （文档 §5.3 的示例写死 0.95，这里改为按证据强度算，口径一致）。
            confidence=round(confidence * weak_factor, 2),
            http_status=status,
        )

    # 两个都没命中：页面结构变了，或者站点把两种结果都渲染成同一套模板。
    # 这是"规则需要修"的信号，不是"账号不存在"的证据。
    logger.debug(
        "站点 %s 结构不符：status=%s，期望 e(%s:%r) / m(%s:%r)",
        _site_name(site),
        status,
        e_code,
        e_string,
        m_code,
        m_string,
    )
    return Verdict(
        Status.UNKNOWN,
        reason=Reason.STRUCTURE_CHANGED,
        evidence=f"status={status} 未命中 e({e_code}:{e_string!r}) / m({m_code}:{m_string!r})",
        http_status=status,
    )


def unknown_verdict(
    reason: Reason,
    *,
    evidence: str = "",
    http_status: Optional[int] = None,
    rate_limited: bool = False,
) -> Verdict:
    """给 hook 层用的 UNKNOWN 构造器。

    TIMEOUT / HTTP_ERROR / SIG_FAILED 这些原因在 judge() 里永远出现不了
    （超时和异常在拿到 resp 之前就发生了），但上游 hook 需要能产出带
    reason 的 UNKNOWN，否则这几个枚举值就成了死代码。
    """
    return Verdict(
        Status.UNKNOWN,
        reason=reason,
        evidence=evidence,
        http_status=http_status,
        rate_limited=rate_limited,
    )


def to_upstream_status(verdict: Verdict) -> str:
    """返回上游 `MaigretCheckStatus` 可接受的字符串值。

    hook 层用法：`MaigretCheckStatus(to_upstream_status(v))`。
    本模块刻意不 import 上游（会形成循环依赖，也让本文件无法脱离运行时测试）。
    """
    return verdict.status.value


def _site_name(site: Any) -> str:
    return str(getattr(site, "name", "<unnamed>"))
