"""置信度模型（《开发文档 v2.0》§5.4）。

为什么要单独一个模型，而不是像上游那样只给"命中/未命中"：

    上游的结论是一个断言，用户看不出这个断言有多硬。拆成三个因子之后，
    用户才能区分"确认存在"和"大概存在"：

        base      站点规则本身的可信度（人工验证过的站可以配高）
        executor  用什么方式拿到的（浏览器 > 页面 > 站内搜索）
        evidence  证据形态（JSON 字段 > 唯一字符串 > 只有状态码）

    三者相乘，再按 §5.4 分档。

纯标准库，不依赖 Site 的具体实现（用 getattr 读字段），便于单测时传假对象。
"""

import logging
from typing import Any

# 执行器可信度：拿到的响应越接近"站点自己渲染给用户的内容"，越可信。
# search（站内搜索结果页）最低，因为它只能证明"搜到了"，不能证明"这个主页是你的"。
EXECUTOR_FACTOR = {
    "api": 1.0,
    "browser": 0.95,
    "http_page": 0.9,
    "http": 0.9,
    "search": 0.5,
}

# 证据强度：从"响应里有结构化字段"到"只对上了用户名"，逐级递减。
# status_only 不在这张表里 —— 它走 STATUS_ONLY_CONFIDENCE 的固定值，不参与乘法。
EVIDENCE_FACTOR = {
    "json_field": 1.0,
    "unique_string": 0.9,
    # 「0 字节 body」作为反例证据，只对显式声明 m_empty 的站点生效。
    # 压到 unique_string 之下：它确实是站点主动给出的结构信号，但同一个形状
    # 也可能是 WAF / 网关改写出的空响应，所以不能和"页面上写着'用户不存在'"
    # 等价看待。
    "empty_body": 0.85,
    "name_match_only": 0.3,
}

# status_only 的固定置信度（文档 §5.4 的关键护栏）。
#
# 为什么是固定值而不是"乘法之后取上限"：证据只有状态码时，站点 base 和执行器
# 系数都不该影响结论 —— 一个 200 就是 200。参与乘法只会让分档随 base 微调抖动：
# 默认站点（base 0.9 / http 0.9）算出 0.49 落低档，base 调到 0.93 就跨到中档，
# 而证据强度根本没有变化。更糟的是原来的"上限 0.6"是空操作：base 已被夹到 1.0，
# 1.0 × 1.0 × 0.6 正好等于 0.6，min() 永远不生效，文档说的"封顶 0.6"从来没兑现过。
#
# 0.5 落在 medium 档下沿（报告里显示"需自行确认"），同时明确进不了 high 档。
STATUS_ONLY_CONFIDENCE = 0.5

# 未知取值的保守默认值：宁可低估，不可高估。
# executor 未知按 http 档；evidence 未知按 0.6（低于 unique_string，高于 status_only
# 的固定档 —— 不认识的证据形态不等于"只有状态码"）。
_DEFAULT_EXECUTOR_FACTOR = 0.9
_DEFAULT_EVIDENCE_FACTOR = 0.6

# 分档阈值（文档 §5.4：≥0.8 高 / 0.5–0.8 中 / <0.5 低）
HIGH_BAND = 0.8
MEDIUM_BAND = 0.5

logger = logging.getLogger(__name__)


def _base_confidence(site: Any) -> float:
    """取站点基础置信度。

    优先用 Site.base_confidence（它已经处理了类型错误），退回到直接读
    confidence 字典 —— 单测传简单对象时不会因为缺属性就崩。
    """
    getter = getattr(site, "base_confidence", None)
    if isinstance(getter, (int, float)):
        base = float(getter)
    else:
        raw = getattr(site, "confidence", None)
        if isinstance(raw, dict):
            try:
                base = float(raw.get("base", 0.9))
            except (TypeError, ValueError):
                base = 0.9
        else:
            base = 0.9

    # base 来自用户手改的 JSON，属于系统边界，越界值直接夹紧而不是让结果爆掉。
    if base < 0.0:
        logger.warning("站点 %s 的 base 置信度为负数（%s），按 0 处理", _name(site), base)
        return 0.0
    if base > 1.0:
        logger.warning("站点 %s 的 base 置信度超过 1.0（%s），按 1.0 处理", _name(site), base)
        return 1.0
    return base


def _name(site: Any) -> str:
    return str(getattr(site, "name", "<unnamed>"))


def score(site: Any, evidence_type: str) -> float:
    """计算一次命中的置信度：base × 执行器系数 × 证据系数，保留两位小数。

    唯一例外是 status_only：直接返回固定值 STATUS_ONLY_CONFIDENCE，不参与乘法。

    未知 executor / evidence_type 不抛异常 —— 站点库是人工维护的，
    一个拼错的字段不该让整次扫描崩掉，只降级 + 告警。
    """
    evidence = str(evidence_type or "").strip().lower()

    # status_only 不参与乘法，直接给固定值：见 STATUS_ONLY_CONFIDENCE 的说明。
    # 放在最前面返回，是为了让结论与 base / executor 完全解耦 —— 否则同一个
    # "200"，站点把 base 从 0.9 调到 0.93 就会从低置信跨到中置信。
    if evidence == "status_only":
        return STATUS_ONLY_CONFIDENCE

    base = _base_confidence(site)

    executor = str(getattr(site, "executor", "") or "").strip().lower()
    executor_factor = EXECUTOR_FACTOR.get(executor)
    if executor_factor is None:
        logger.warning(
            "站点 %s 的 executor %r 未知，按保守值 %.2f 计算",
            _name(site),
            executor,
            _DEFAULT_EXECUTOR_FACTOR,
        )
        executor_factor = _DEFAULT_EXECUTOR_FACTOR

    evidence_factor = EVIDENCE_FACTOR.get(evidence)
    if evidence_factor is None:
        logger.warning(
            "站点 %s 的 evidence_type %r 未知，按保守值 %.2f 计算",
            _name(site),
            evidence,
            _DEFAULT_EVIDENCE_FACTOR,
        )
        evidence_factor = _DEFAULT_EVIDENCE_FACTOR

    return round(base * executor_factor * evidence_factor, 2)


def confidence_band(value: float) -> str:
    """把分数映射到 high / medium / low 三档。"""
    if value >= HIGH_BAND:
        return "high"
    if value >= MEDIUM_BAND:
        return "medium"
    return "low"
