"""风险分级：替代"处置建议"（文档 §4.2 / §5.7）。

产品边界（文档 §1.2①）写进代码：**不做"该点哪个开关"**，只做"这事有多严重"。

这样切的原因不是保守，是有效性：平台隐私设置菜单几个月就改版一次，
任何"去设置里关掉 X"的指引都会过期，过期的指引比没有指引更坏——
用户按图索骥找不到，会以为自己已经安全了。

而"风险由证据组合决定"不会过期：真实姓名 + 手机号同时暴露，无论平台
怎么改版，严重性都一样。

第二段"本次未能判定"是**行动线索而非配置指引**：它告诉用户"你的自查有盲区"，
但不说"去点哪个按钮"。
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

LEVEL_LABELS = {3: "高", 2: "中", 1: "低", 0: "无"}

# 证据字段 → 风险等级与理由（文档 §4.2 的表，原样照抄）
#
# 顺序即"从具体到宽泛"。判级时取所有命中规则里最高的那一级，
# 但把命中的规则全部列出来——用户需要知道"为什么是高"，
# 只给一个数字等于让他猜。
RISK_RULES: Tuple[Tuple[frozenset, int, str], ...] = (
    (frozenset({"real_name", "phone"}), 3, "真实姓名与手机号同时暴露"),
    (frozenset({"real_name", "ip_location"}), 3, "真实姓名与常驻地同时暴露"),
    (frozenset({"real_name", "company", "school"}), 3, "姓名、单位、学校同时暴露，可被精准定位"),
    (frozenset({"real_name"}), 2, "真实姓名暴露"),
    (frozenset({"phone"}), 2, "手机号（含部分）暴露"),
    (frozenset({"ip_location", "company"}), 2, "常驻地与单位同时暴露"),
    (frozenset({"email"}), 2, "邮箱暴露，可被撞库"),
    (frozenset({"ip_location"}), 1, "仅 IP 属地暴露"),
)

# 低于这个置信度的证据不参与判级。
#
# 为什么要设：抽取层的置信度是分档的（邮箱 0.7、手机 0.6、属地 0.85）。
# 让一条 0.3 的猜测把整体风险抬到"高"，比不报风险更糟——用户会为一个
# 不存在的暴露去折腾。0.5 这个位置能保住所有真实抽取结果，同时挡掉猜测。
MIN_CONFIDENCE = 0.5


@dataclass
class RiskFinding:
    """一条命中的风险规则。"""

    level: int
    fields: Tuple[str, ...]
    reason: str

    @property
    def label(self) -> str:
        return LEVEL_LABELS.get(self.level, "未知")


@dataclass
class RiskResult:
    """一次自查的风险结论。"""

    level: int = 0
    findings: List[RiskFinding] = field(default_factory=list)
    # 参与判级的字段（已按置信度过滤）
    fields: Tuple[str, ...] = ()
    # 因为置信度不足被忽略的字段，用于排查"为什么没判成高"
    ignored: Tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return LEVEL_LABELS.get(self.level, "未知")


def assess(
    evidence: Iterable,
    min_confidence: float = MIN_CONFIDENCE,
) -> RiskResult:
    """按证据组合判风险等级。

    `evidence` 可以是 `Evidence` 对象，也可以是 `Evidence.to_dict()` 出来的
    字典（报告里存的是后者），两种都认——否则调用方要来回转换，
    迟早会有一处忘了转。

    **部分值也算暴露**：`138****5678`、`张*` 同样是"手机号/姓名已公开"，
    只是展示时脱敏了。把它们排除掉会得出"风险低"的错误结论。
    """
    fields: set = set()
    ignored: set = set()

    for item in evidence or ():
        name, confidence = _field_and_confidence(item)
        if not name:
            continue
        if confidence is not None and confidence < min_confidence:
            ignored.add(name)
            continue
        fields.add(name)

    findings: List[RiskFinding] = []
    for required, level, reason in RISK_RULES:
        if required.issubset(fields):
            findings.append(
                RiskFinding(level=level, fields=tuple(sorted(required)), reason=reason)
            )

    # 命中的规则按严重程度排序，同级按字段多的在前（信息量更大）
    findings.sort(key=lambda f: (-f.level, -len(f.fields)))
    level = max((f.level for f in findings), default=0)

    return RiskResult(
        level=level,
        findings=findings,
        fields=tuple(sorted(fields)),
        ignored=tuple(sorted(ignored)),
    )


def _field_and_confidence(item) -> Tuple[str, Optional[float]]:
    """兼容 Evidence 对象与它的 dict 形态。"""
    if isinstance(item, dict):
        return str(item.get("field") or ""), item.get("confidence")
    return str(getattr(item, "field", "") or ""), getattr(item, "confidence", None)


# ---------------------------------------------------------------------------
# 未判定盲区
# ---------------------------------------------------------------------------
# 判定原因 → 给用户看的说法。
#
# 刻意区分"需要登录"和"触发风控"：前者是**结构性的**（不登录永远判不了），
# 后者是**临时的**（过一会儿可能就好了）。混在一起说，用户会反复重试一件
# 注定失败的事。
_BLIND_SPOT_LABELS: Dict[str, Tuple[str, bool]] = {
    "login_required": ("个站点需要登录才能判定", False),
    "blocked": ("个站点触发了风控，建议稍后重试", True),
    "rate_limited": ("个站点触发了限流，建议稍后重试", True),
    "captcha": ("个站点弹出了验证码", True),
    "signature_failed": ("个站点签名校验失败", False),
    "rule_missing": ("个站点还没有可用规则", False),
    "structure_changed": ("个站点页面结构已变化，规则需要更新", False),
    "timeout": ("个站点请求超时", True),
    "http_error": ("个站点请求失败", True),
    "empty_body": ("个站点返回空页面", True),
    "search_inconclusive": ("个站点只能靠站内搜索，结果里没有同名账号", True),
}


@dataclass
class BlindSpot:
    """一类未判定原因及其涉及站点。"""

    reason: str
    label: str
    retryable: bool
    sites: List[str] = field(default_factory=list)

    def line(self) -> str:
        sample = "、".join(self.sites[:5])
        more = f" 等 {len(self.sites)} 个" if len(self.sites) > 5 else ""
        # 数字与中文之间留空格：混排时这样更好读，也和报告其他地方的写法一致。
        return f"{len(self.sites)} {self.label}（{sample}{more}）"


def blind_spots(reasons: Dict[str, Sequence[str]]) -> List[BlindSpot]:
    """把 {判定原因: [站点名]} 变成可读的盲区列表。

    按"涉及站点数降序"排：用户最先要知道的是哪一类问题影响面最大。
    """
    result: List[BlindSpot] = []
    for reason, sites in (reasons or {}).items():
        if not sites:
            continue
        label, retryable = _BLIND_SPOT_LABELS.get(reason, ("个站点未能判定", True))
        result.append(
            BlindSpot(
                reason=reason,
                label=label,
                retryable=retryable,
                sites=list(sites),
            )
        )
    result.sort(key=lambda item: (-len(item.sites), item.reason))
    return result


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render(
    result: RiskResult,
    spots: Optional[Sequence[BlindSpot]] = None,
    evidence: Optional[Iterable] = None,
    claims: int = 0,
) -> str:
    """渲染成文档 §4.2 的文本形态。

    `claims` 是已确认存在的账号数。它只用于结语：抽不到证据 ≠ 什么都没找到，
    一个"命中 2 个账号"的扫描下面接一句「没有发现已公开的个人信息」，
    读起来像自相矛盾（站内搜索命中就是这种情况——账号找到了，但没有可核对
    的字段）。
    """
    lines = [f"风险等级：{result.label}"]

    if result.findings:
        lines.append("")
        lines.append("命中的证据")
        for finding in result.findings:
            lines.append(f"  · {finding.reason}")

    if evidence:
        detail = _render_evidence(evidence)
        if detail:
            lines.append("")
            lines.append("证据明细")
            lines.extend(detail)
    else:
        detail = []

    if spots:
        lines.append("")
        lines.append("本次未能判定")
        for spot in spots:
            lines.append(f"  · {spot.line()}")

    # 结语必须和上面列出来的东西一致。
    #
    # 原来的判据只有 `level == 0 and not spots`，于是"抽到了昵称和头像指纹、
    # 但没有命中任何风险规则"这种最常见的情况会输出一句
    # 「没有发现已公开的个人信息」——它和上方的证据明细直接打架，
    # 用户看到的是"明明列了两条，却说没发现"。抽错了比抽不到严重得多，
    # 而这里是把抽到的东西说成没抽到。
    if result.level == 0 and not spots and not result.findings:
        if detail:
            lines.append("")
            lines.append("只发现低敏感度信息（昵称、头像等），未构成实质暴露。")
        elif claims:
            lines.append("")
            lines.append(f"找到 {claims} 个同名账号，但没有抽到可核对的公开信息。")
        else:
            lines.append("")
            lines.append("没有发现已公开的个人信息。")

    return "\n".join(lines)


def _render_evidence(evidence: Iterable) -> List[str]:
    from .extract import LABELS

    rows: List[str] = []
    seen = set()
    for item in evidence:
        if isinstance(item, dict):
            name = item.get("field", "")
            value = item.get("value", "")
            source = item.get("source", "")
            confidence = item.get("confidence")
        else:
            name = getattr(item, "field", "")
            value = getattr(item, "value", "")
            source = getattr(item, "source", "")
            confidence = getattr(item, "confidence", None)

        key = (name, value)
        # 空值证据一律不渲染：它没有信息量，只会渲染成「昵称「」」这种残句，
        # 还会让上面的结语判断误以为"有证据"，把"没发现"改成"低敏感度"。
        if not name or not value or key in seen:
            continue
        seen.add(key)

        label = LABELS.get(name, name)
        suffix = f" · 置信度 {confidence:.2f}" if isinstance(confidence, (int, float)) else ""
        rows.append(f"  · {label}「{value}」— 来源：{source}{suffix}")
    return rows
