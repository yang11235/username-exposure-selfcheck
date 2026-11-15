"""站点数据模型 v2（共享契约）。

本模块只做**数据容器**，不含校验逻辑（校验在 schema.py）、不含合并逻辑
（合并在 overlay.py）。这样 verdict/scoring 可以只依赖本文件，与数据加载解耦。

字段定义对齐《开发文档 v2.0》§5.2。与上游 `MaigretSite` 的关系：
    - 上游用 camelCase → snake_case 动态挂属性，字段靠 `information` 字典喂
    - 这里是显式 dataclass，字段固定、有默认值、可静态检查
    - 两者互转由 overlay.py 负责（上游条目 → Site）

注意：上游 `MaigretSite` 的 headers/tags/errors/activation 是**类级共享可变
对象**，直接 update 会污染所有站点。本 dataclass 全部用 default_factory，
天然隔离。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 数据来源标记，用于排查"这条规则是谁覆盖的"
SOURCE_UPSTREAM = "upstream"
SOURCE_OVERRIDES = "overrides"
SOURCE_CN = "sites_cn"

# 四字段协议的核心字段名
FOUR_FIELDS = ("e_code", "e_string", "m_code", "m_string")


@dataclass
class Site:
    """单个站点的探测规则（v2 四字段协议）。"""

    name: str

    # ---- 基本信息 ----
    url_main: str = ""
    cat: str = ""
    tags: List[str] = field(default_factory=list)
    id_type: str = "username"

    # ---- 探测地址 ----
    # uri_check  给机器打（可能是 API）
    # uri_pretty 给人看（报告里展示这个，否则用户看到的是 API 地址看不懂）
    uri_check: str = ""
    uri_pretty: str = ""
    strip_bad_char: str = ""

    # ---- 请求方式 ----
    method: str = "GET"
    post_body: Optional[Dict[str, Any]] = None
    headers: Dict[str, str] = field(default_factory=dict)

    # ---- 四字段判定 ----
    # e_string / m_string 是**主判据**（报告、日志、schema 都按它们描述规则）；
    # e_strings / m_strings 是附加候选串，语义是"任一命中"。
    #
    # 为什么需要附加串：上游 message 型的 presenseStrs 本来就是多串 OR
    # （checking.py 的判定是 any()），只留第一个会漏判 —— 第一个串改版消失、
    # 第二个串还在时，会被误报成 STRUCTURE_CHANGED。主判据单独留一份是为了
    # 让"这条规则靠什么判"始终只有一个明确答案，不必让人去列表里猜。
    e_code: Optional[int] = None
    e_string: str = ""
    e_strings: List[str] = field(default_factory=list)
    m_code: Optional[int] = None
    m_string: str = ""
    m_strings: List[str] = field(default_factory=list)

    # 「空 body 就是不存在」的显式开关。默认 False，只有站点自己声明才生效。
    #
    # 为什么必须逐站声明、不能全局放开：判定层遇到"2xx + 空 body"默认返回
    # UNKNOWN(EMPTY_BODY)，因为那是 WAF / 反向代理 / SPA 空壳的常态，把它当
    # "不存在"会批量误报。但有一类接口例外 —— 它用"返回整条记录"表示存在、
    # "返回 0 字节"表示不存在（百度贴吧的 user_json 就是：存在的用户返回
    # `{"raw_name":...}`，不存在的返回空 body）。这类接口没有反例串可写，
    # m_string 只能留空，于是永远判不出"不存在"。
    #
    # 开关放在站点上而不是让判定层去猜，是因为"空 body 有含义"这件事只有
    # 规则的作者知道，判定层无从推断。默认关 = 不加这个字段的站点行为完全不变。
    m_empty: bool = False

    # ---- 站内搜索（executor == "search" 时使用，见 executors/search.py）----
    # 为什么需要单独一组字段：B 站/微博/贴吧的主页地址没法从用户名拼出来
    # （数字 uid / oid），只能走站内搜索。而搜索结果页必然回显你搜的词，
    # 所以判据必须换成"结果里有没有逐字相同的句柄"——这需要一条正则。
    search_handle_pattern: str = ""   # 结果条目里句柄的正则，第 1 组是句柄
    search_empty_marker: str = ""     # 明确表示"搜索结果为空"的文案
    # 命中后用站内 ID 拼主页地址的模板，如 https://space.bilibili.com/{id}
    search_profile_url: str = ""

    # ---- 执行器路由 ----
    executor: str = "http"          # http / browser / search / api
    signature: Optional[str] = None  # 需浏览器上下文取的签名，如 xhs_xs
    requires: List[str] = field(default_factory=list)      # login / cookie
    protection: List[str] = field(default_factory=list)    # captcha / signature

    # ---- 请求预热 ----
    # 有些站点必须先访问一次别处，让服务端下发 Cookie，真正的接口才肯回数据。
    # B 站搜索接口就是典型：不带 buvid3 时第一次能过，之后一律 412 风控。
    # 这个字段只在 http/search 执行器上生效，值为空则完全跳过。
    bootstrap_url: str = ""

    # ---- 抽取与自检 ----
    extract: List[str] = field(default_factory=list)
    known: List[str] = field(default_factory=list)
    known_absent: List[str] = field(default_factory=list)
    confidence: Dict[str, float] = field(default_factory=lambda: {"base": 0.9})
    valid: bool = True
    verified_at: str = ""
    # verified_at 的来源："" / "upstream"（抄自上游已自检数据）/ "manual"（本分支实测）
    verified_source: str = ""

    # ---- 合并与排序 ----
    force_include: bool = False     # True 则强制进入默认扫描，不参与 alexaRank 排序
    # 是否进默认扫描。和 `force_include` 是两件事：
    #   force_include 是合并阶段的产物（"这条记录来自 sites_cn.json"），
    #   default_include 是作者的选择（"这条值得每次都查吗"）。
    # 小众站仍然保留完整规则、仍可用 --site / --tags cn 查到，只是不占默认位。
    default_include: bool = True
    alexa_rank: Optional[int] = None
    disabled: bool = False
    source: str = SOURCE_UPSTREAM   # 该条记录最终来自哪个文件

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def has_four_fields(self) -> bool:
        """四个字段是否齐全（值可以为空字符串，但键必须存在且有状态码）。"""
        return self.e_code is not None and self.m_code is not None

    @property
    def same_status_code(self) -> bool:
        """存在与不存在返回同一个状态码 —— 中文站最常见的陷阱。"""
        return self.e_code is not None and self.e_code == self.m_code

    @property
    def string_only(self) -> bool:
        """只能靠字符串区分（状态码相同）。此时字符串是唯一判据，必须非空。"""
        return self.same_status_code

    @property
    def base_confidence(self) -> float:
        try:
            return float(self.confidence.get("base", 0.9))
        except (TypeError, ValueError):
            return 0.9

    def pretty_url(self, account: str) -> str:
        """报告里展示给人看的 URL；没配 uri_pretty 就退回 uri_check。

        同时接受 `{account}` 与上游的 `{username}`：overlay 会把上游模板统一
        转成 `{account}`，但如果有人绕过 overlay 直接构造 Site，格式化不能
        因为占位符名字不同就抛 KeyError 或原样吐出带花括号的地址。
        """
        template = self.uri_pretty or self.uri_check
        if not template:
            return ""
        try:
            return template.format(account=account, username=account)
        except (KeyError, IndexError):
            return template

    def cleaned_username(self, account: str) -> str:
        """按 strip_bad_char 清洗用户名（WMN 的 strip_bad_char 字段）。"""
        if not self.strip_bad_char:
            return account
        return account.translate({ord(c): None for c in self.strip_bad_char})

    def to_dict(self) -> Dict[str, Any]:
        """序列化回 v2 JSON 形态，便于写回 overlay 文件或调试。"""
        from dataclasses import asdict

        return asdict(self)


def site_from_mapping(name: str, data: Dict[str, Any], source: str = SOURCE_UPSTREAM) -> Site:
    """从（可能混用 camelCase 的）字典构造 Site。

    这里刻意宽松：未知键忽略，缺失键用默认值。严格校验交给 schema.py，
    因为上游 9,535 个站点里大量条目本来就不满足四字段协议，不能因此加载失败。
    """
    def pick(*keys: str, default: Any = None) -> Any:
        for k in keys:
            if k in data and data[k] is not None:
                return data[k]
        return default

    def as_list(*keys: str) -> List[str]:
        """字符串要当成单元素列表，不能用 list() 拆成字符。

        上游 usernameClaimed / usernameUnclaimed 是裸字符串，而 v2 的
        known / known_absent 是列表 —— 这是最容易踩的转换坑。
        """
        value = pick(*keys, default=None)
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value else []
        # 丢掉 null：str(None) 会变成字面量 "None"，而它出现在判据串里
        # 就会命中任何含 "None" 的页面（JSON 里的 null 很常见）。
        return [str(v) for v in value if v is not None]

    return Site(
        name=name,
        url_main=pick("url_main", "urlMain", default="") or "",
        cat=pick("cat", default="") or "",
        tags=as_list("tags"),
        id_type=pick("id_type", "idType", "type", default="username") or "username",
        uri_check=pick("uri_check", "urlProbe", "url_probe", "url", default="") or "",
        uri_pretty=pick("uri_pretty", "url", default="") or "",
        strip_bad_char=pick("strip_bad_char", "stripBadChar", default="") or "",
        method=(pick("method", "requestMethod", default="GET") or "GET").upper(),
        post_body=pick("post_body", "requestPayload", default=None),
        headers=dict(pick("headers", default={}) or {}),
        e_code=pick("e_code", "eCode", default=None),
        e_string=pick("e_string", "eString", default="") or "",
        e_strings=as_list("e_strings", "eStrings"),
        m_code=pick("m_code", "mCode", default=None),
        m_string=pick("m_string", "mString", default="") or "",
        m_strings=as_list("m_strings", "mStrings"),
        m_empty=bool(pick("m_empty", "mEmpty", default=False)),
        search_handle_pattern=pick(
            "search_handle_pattern", "searchHandlePattern", default=""
        )
        or "",
        search_empty_marker=pick(
            "search_empty_marker", "searchEmptyMarker", default=""
        )
        or "",
        search_profile_url=pick(
            "search_profile_url", "searchProfileUrl", default=""
        )
        or "",
        executor=pick("executor", default="http") or "http",
        bootstrap_url=pick("bootstrap_url", "bootstrapUrl", default="") or "",
        signature=pick("signature", default=None),
        requires=as_list("requires"),
        protection=as_list("protection"),
        extract=as_list("extract"),
        known=as_list("known", "usernameClaimed"),
        known_absent=as_list("known_absent", "usernameUnclaimed"),
        confidence=dict(pick("confidence", default={"base": 0.9}) or {"base": 0.9}),
        valid=bool(pick("valid", default=True)),
        verified_at=pick("verified_at", "verifiedAt", default="") or "",
        verified_source=pick("verified_source", "verifiedSource", default="") or "",
        force_include=bool(pick("force_include", default=False)),
        # 缺省 True：绝大多数站没写这个字段，默认行为必须是"进默认扫描"，
        # 否则每加一条规则都要记得补一行，漏写就静默消失。
        default_include=bool(pick("default_include", "defaultInclude", default=True)),
        alexa_rank=pick("alexa_rank", "alexaRank", default=None),
        disabled=bool(pick("disabled", default=False)),
        source=source,
    )
