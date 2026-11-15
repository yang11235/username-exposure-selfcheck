"""证据抽取：从已确认的个人主页里取出**平台自己公开展示**的字段。

边界（写进代码是因为这是伦理边界，不是技术选择）：

    只读页面上平台主动展示给任何访客看的内容——昵称、简介、IP 属地、
    公司、学校、主页上明写的邮箱/电话、头像。
    **不做手机号/身份证反查**（文档 §1.2⑤），**不做内容抓取**，
    也**不猜**页面上没写的东西。

字段与风险等级对齐文档 §5.6：

    ip_location 2 / real_name 3 / email 3 / phone 3 / company 2 / school 2 /
    avatar_hash 1

抽取失败时**返回空列表**，不返回"猜测值"。这一层最坏的失败不是漏抽，
而是抽出一个错的——用户会照着错的信息去改密码、去申诉。
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# 字段 → 风险等级（文档 §5.6 的表，原样照抄）
RISK = {
    "avatar_hash": 1,
    "nickname": 1,
    "ip_location": 2,
    "company": 2,
    "school": 2,
    "bio": 1,
    "real_name": 3,
    "email": 3,
    "phone": 3,
    # 性别属于人口学信息：不像昵称那样随时能改，也不像姓名那样直接指向个人，
    # 所以定 2 级（中）—— 报告里会标出来，但不当作高危。
    "gender": 2,
    # 粉丝数 / 关注数 / 账号年龄是"账号体量"类指标，本身不指向个人，
    # 但组合起来有辨识度，按 1 级（低）处理。
    "followers": 1,
    "following_count": 1,
    "account_age": 1,
}

# 字段 → 中文名，报告里给人看的
LABELS = {
    "avatar_hash": "头像指纹",
    "nickname": "昵称",
    "ip_location": "IP 属地",
    "company": "公司",
    "school": "学校",
    "bio": "简介",
    "real_name": "姓名",
    "email": "邮箱",
    "phone": "手机号",
    "gender": "性别",
    "followers": "粉丝数",
    "following_count": "关注数",
    "account_age": "账号年龄",
}


@dataclass
class Evidence:
    """一条证据。

    `source` 是"从哪儿抽到的"，不是"哪个站点"——同一条微博主页上，
    昵称来自 meta 标签、IP 属地来自正文，两者的可靠性差很多，
    排查时必须能分清。
    """

    field: str
    value: str
    source: str
    confidence: float = 0.8
    risk: int = 0
    partial: bool = False

    def __post_init__(self):
        if not self.risk:
            self.risk = RISK.get(self.field, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field,
            "label": LABELS.get(self.field, self.field),
            "value": self.value,
            "source": self.source,
            "confidence": round(self.confidence, 2),
            "risk": self.risk,
            "partial": self.partial,
        }


# ---------------------------------------------------------------------------
# 从 HTML 取文本
# ---------------------------------------------------------------------------
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def visible_text(html: str) -> str:
    """去掉 script/style/注释/标签后的正文。

    为什么必须去掉 script：打包后的 JS 里什么字符串都有——邮箱正则、
    手机号占位符、示例数据。直接全文匹配会把 JS 里的示例抽成"证据"。
    """
    text = _SCRIPT_STYLE_RE.sub(" ", html or "")
    text = _COMMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _jsonld_blocks(html: str) -> List[Any]:
    """取出所有 JSON-LD 结构化数据。

    很多主页（尤其 GitHub、掘金这类）会带 schema.org 的 Person 数据，
    这是**平台自己标注**的字段，比正则猜可靠得多。
    """
    blocks = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
        re.IGNORECASE | re.DOTALL,
    ):
        raw = match.group(1).strip()
        try:
            blocks.append(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return blocks


def _meta(html: str, *names: str) -> str:
    """读 og:/profile: 之类的 meta 标签。"""
    for name in names:
        pattern = (
            r'<meta[^>]+(?:property|name)=["\']'
            + re.escape(name)
            + r'["\'][^>]+content=["\']([^"\']*)["\']'
        )
        match = re.search(pattern, html or "", re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
        # content 写在 name 前面的写法也合法
        pattern_rev = (
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']'
            + re.escape(name)
            + r'["\']'
        )
        match = re.search(pattern_rev, html or "", re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# 各字段的抽取器
# ---------------------------------------------------------------------------
# IP 属地：微博/小红书/抖音/知乎都在主页公开展示。
# 中文冒号、英文冒号、全角空格都见过，全都要认。
_IP_LOCATION_RE = re.compile(
    r"IP\s*属\s*地\s*[:：]?\s*([\u4e00-\u9fa5]{2,10}(?:[\u4e00-\u9fa5]{0,6})?)"
)

# 邮箱。排除图片/静态资源名：`logo@2x.png` 这种会被误抽成邮箱。
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,10}")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp")

# 邮箱必须出现在用户名附近多少字符之内才算"这个人的"。
#
# 为什么需要这个窗口：实测 Gitee 个人主页（wizzer）全文里唯一的邮箱是
# `client@oschina.cn`——那是站点页脚的客服邮箱。只按"页面上有邮箱"来抽，
# 会把每个用户的报告都写成"邮箱：客服邮箱"，比漏抽坏得多。
# 200 字符的依据：正常主页的昵称/简介/联系区块挨得很近，而页脚离用户名
# 隔着导航、正文、备案信息，远超这个距离。
EMAIL_ANCHOR_WINDOW = 200

# 中国大陆手机号。只在平台**自己展示**时抽取，绝不用于反查（文档 §1.2⑤）。
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

# 头像：class/id 里带 avatar 的 img，以及"看起来像站点自有资源"的过滤
_AVATAR_IMG_RE = re.compile(
    r'<img[^>]+(?:class|id)=["\'][^"\']*(?:avatar|portrait|head-img)[^"\']*["\'][^>]*>',
    re.IGNORECASE,
)
_SRC_RE = re.compile(r'(?:src|data-src)=["\']([^"\']+)["\']', re.IGNORECASE)

# 站点自有资源（logo、favicon、占位图）的特征。
#
# 为什么必须过滤：实测 Gitee 个人主页的 `og:image` 是
# `https://gitee.com/static/images/logo_themecolor_circle.png`——**站点 logo**。
# 拿它当头像算 pHash，全站所有用户的哈希都一样，等于把"头像匹配"变成
# "所有人都是同一个人"。这比抽不到严重得多。
_SITE_ASSET_HINTS = (
    "logo",
    "favicon",
    "/static/",
    "/assets/",
    "placeholder",
    "default_avatar",
    "defaultavatar",
    "avatar_default",
    "noavatar",
    "no-avatar",
    "blank.",
    "spacer.",
)

# 标题里常见的分隔符：`Wizzer (wizzer) - Gitee.com`
_TITLE_SEPS = (" - ", " | ", "｜", " — ", " · ", " – ", "_")


def extract_ip_location(text: str) -> List[Evidence]:
    found = []
    for match in _IP_LOCATION_RE.finditer(text):
        value = match.group(1).strip()
        # "IP属地" 后面常跟着别的词，截断到合理长度
        value = value[:10]
        if value:
            found.append(Evidence("ip_location", value, "正文 IP 属地", confidence=0.85))
    return found


def extract_email(text: str, anchor: str = "") -> List[Evidence]:
    """抽邮箱。**必须给 anchor（用户名），且邮箱要出现在它附近。**

    不给 anchor 就返回空——这是刻意的严格：没有"这是谁的页面"这个前提，
    页面上任何邮箱都不能归到这个用户名头上。实测 Gitee 的 `client@oschina.cn`
    就是站点页脚的客服邮箱，按全文匹配会给每个用户都写上它。
    """
    if not anchor:
        return []

    lowered = text.lower()
    anchor_low = anchor.lower()
    positions = []
    start = 0
    while True:
        found = lowered.find(anchor_low, start)
        if found < 0:
            break
        positions.append(found)
        start = found + 1
    if not positions:
        return []

    found_items = []
    for match in _EMAIL_RE.finditer(text):
        value = match.group(0)
        if value.lower().endswith(_IMAGE_EXT):
            continue
        # 常见误抽：版本号/示例域名
        if "@example." in value.lower() or value.startswith("user@"):
            continue
        distance = min(abs(match.start() - pos) for pos in positions)
        if distance > EMAIL_ANCHOR_WINDOW:
            continue
        found_items.append(
            Evidence("email", value, "正文邮箱（用户名附近）", confidence=0.7)
        )
    return found_items


def extract_phone(text: str) -> List[Evidence]:
    found = []
    for match in _PHONE_RE.finditer(text):
        found.append(Evidence("phone", match.group(1), "正文手机号", confidence=0.6))
    return found


def looks_like_site_asset(url: str) -> bool:
    """这个图片地址是不是站点自己的资源（logo / favicon / 占位图）。"""
    lowered = (url or "").lower()
    return any(hint in lowered for hint in _SITE_ASSET_HINTS)


def extract_avatar_url(html: str, base_url: str = "") -> str:
    """取**用户头像**的 URL。取不到返回空串。

    候选顺序有讲究，和直觉相反：

        1. `class/id` 里带 avatar 的 `<img>` —— 最可靠。平台用它渲染的就是
           这个人的头像。
        2. `og:image` / `twitter:image` —— **最后才用**。这两个标签经常是
           站点 logo 或运营图，不是用户头像。实测 Gitee 的 og:image 就是
           `static/images/logo_themecolor_circle.png`。

    为什么不能反过来：拿站点 logo 当头像算 pHash，全站所有用户的哈希都一样，
    身份聚类会把所有人合成一个人。这比"抽不到头像"严重得多——抽不到只是
    少一条信号，抽错是凭空造出一个错误的身份关系。
    """
    if not html:
        return ""

    def absolute(value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        return urljoin(base_url, value) if base_url else value

    # 1. avatar 类名的 img
    for tag in _AVATAR_IMG_RE.finditer(html):
        src = _SRC_RE.search(tag.group(0))
        if not src:
            continue
        candidate = absolute(src.group(1))
        if candidate and not looks_like_site_asset(candidate):
            return candidate

    # 2. og:image / twitter:image（过滤掉站点自有资源）
    for name in ("og:image", "twitter:image"):
        candidate = absolute(_meta(html, name))
        if candidate and not looks_like_site_asset(candidate):
            return candidate

    return ""


def extract_from_jsonld(html: str) -> List[Evidence]:
    """从 schema.org Person 数据里取姓名/公司/学校。"""
    found: List[Evidence] = []
    for block in _jsonld_blocks(html):
        items = block if isinstance(block, list) else [block]
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("@type", "")).lower()
            if kind not in ("person", "profilepage"):
                continue

            name = item.get("name")
            if isinstance(name, str) and name.strip():
                # schema.org 的 name 是**展示名**，对应昵称而不是实名。
                # 除非站点在 extract 里显式要 real_name，否则按昵称处理——
                # 把昵称当实名会让风险等级从 1 跳到 3，属于夸大。
                found.append(
                    Evidence("nickname", name.strip(), "JSON-LD name", confidence=0.8)
                )

            for key, field_name in (("worksFor", "company"), ("alumniOf", "school")):
                org = item.get(key)
                org_name = ""
                if isinstance(org, dict):
                    org_name = str(org.get("name", "")).strip()
                elif isinstance(org, str):
                    org_name = org.strip()
                if org_name:
                    found.append(
                        Evidence(field_name, org_name, f"JSON-LD {key}", confidence=0.75)
                    )
    return found


def clean_title(value: str, site_name: str = "") -> str:
    """去掉标题里的站点后缀。

    实测 Gitee：`og:title` 是 `Wizzer (wizzer) - Gitee.com`，
    直接当昵称会写成"昵称：Wizzer (wizzer) - Gitee.com"。
    判据是"最后一段像不像站点名"——包含站点名，或者以域名后缀结尾。
    """
    value = (value or "").strip()
    for sep in _TITLE_SEPS:
        if sep not in value:
            continue
        head, _, tail = value.rpartition(sep)
        if not head.strip():
            continue
        tail_clean = tail.strip()
        looks_like_site = bool(re.search(r"\.[a-z]{2,}$", tail_clean.lower())) or (
            site_name and site_name.lower() in tail_clean.lower()
        )
        if looks_like_site:
            return head.strip()
    return value


def strip_account_suffix(value: str, anchor: str = "") -> str:
    """去掉昵称尾部括号里的用户名。

    实测 Gitee：`og:title` 是 `Wizzer (wizzer) - Gitee.com`，去掉站点后缀后
    剩 `Wizzer (wizzer)`，于是报告里写成"昵称「Wizzer (wizzer)」"——
    括号里那截是我们自己的查询词，不是用户填的昵称。

    **只在括号内容等于 anchor 时才删**，不按"括号里像 ID 就删"的规则：
    「老王（摄影师）」「阿杰（上海）」这类括号是昵称的一部分，删掉就把
    用户的昵称改错了。宁可留着冗余，也不能改用户的自我描述。
    """
    if not value or not anchor:
        return value
    pattern = r"\s*[（(]\s*" + re.escape(anchor.strip()) + r"\s*[)）]\s*$"
    stripped = re.sub(pattern, "", value, flags=re.IGNORECASE)
    return stripped.strip() or value


def extract_nickname(html: str, site_name: str = "", anchor: str = "") -> List[Evidence]:
    found = []
    og_title = _meta(html, "og:title", "profile:username")
    if og_title:
        name = strip_account_suffix(clean_title(og_title, site_name), anchor)
        if name:
            found.append(Evidence("nickname", name, "og:title", confidence=0.6))
    return found


def extract_real_name(html: str, text: str = "") -> List[Evidence]:
    """实名：**故意不实现**。

    schema.org 的 `name` 是展示名（昵称），把它当实名会让风险等级从 1 跳到 3，
    等于夸大用户被识别的程度。真正的实名来源（论文署名、企业公示）不在
    个人主页里，需要另外的数据源，不在本项目范围内。

    留着这个函数而不是删掉，是为了让"为什么没有 real_name"有代码层面的答案，
    而不是让人以为漏了。
    """
    return []


def extract_avatar_hash(html: str, text: str = "") -> List[Evidence]:
    """头像指纹：**不在这一层算**。

    算 pHash 要先下载图片，而下载是调用方的事（需要超时、大小上限、代理）。
    这里只提供 `extract_avatar_url()`，由调用方取回字节后交给
    `identity.phash()`。硬在这里发请求会让这个纯函数变成有副作用的网络调用，
    测试也就没法离线跑了。
    """
    return []


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _extract_company(html: str, ctx: Dict[str, Any]) -> List[Evidence]:
    return [e for e in extract_from_jsonld(html) if e.field == "company"]


def _extract_school(html: str, ctx: Dict[str, Any]) -> List[Evidence]:
    return [e for e in extract_from_jsonld(html) if e.field == "school"]


# 抽取器统一签名：(html, ctx) -> List[Evidence]。
# ctx 里带 text / anchor / site_name，因为"这是谁的页面"是很多抽取的前提条件
# （见 extract_email 的说明）。
_EXTRACTORS = {
    "ip_location": lambda html, ctx: extract_ip_location(ctx["text"]),
    "email": lambda html, ctx: extract_email(ctx["text"], anchor=ctx.get("anchor", "")),
    "phone": lambda html, ctx: extract_phone(ctx["text"]),
    "nickname": lambda html, ctx: extract_nickname(
        html, ctx.get("site_name", ""), anchor=ctx.get("anchor", "")
    ),
    "company": _extract_company,
    "school": _extract_school,
    "real_name": extract_real_name,
    "avatar": extract_avatar_hash,
    "avatar_hash": extract_avatar_hash,
}


def extract_evidence(
    html: str,
    fields: Optional[Iterable[str]] = None,
    base_url: str = "",
    anchor: str = "",
    site_name: str = "",
) -> List[Evidence]:
    """按站点声明的 `extract` 列表抽取。字段名对不上就跳过，不报错。

    `fields` 为空时**不抽任何东西**——不是"抽全部"。理由是抽取会带来
    额外请求（头像）和额外解读，只有站点明确声明要哪些字段时才值得做。

    `anchor` 是这次查询的用户名，用于判断"页面上的信息属于谁"（见 extract_email）。
    """
    if not fields:
        return []

    # 平台专用抽取优先：B 站/网易云/掘金的探测地址返回的是 JSON，一行 meta
    # 标签都没有，通用 HTML 抽取在这里永远只能抽到空。认出平台就直接按实测
    # 路径取字段，不再跑一遍 HTML 规则——JSON 正文被当成"可见文本"去猜昵称，
    # 只会把字段名当昵称抽出来。
    from . import platforms

    extractor = platforms.detect(base_url, html)
    if extractor is not None:
        return extractor.extract(html, fields)

    ctx = {
        "text": visible_text(html),
        "anchor": anchor,
        "site_name": site_name,
        "base_url": base_url,
    }
    results: List[Evidence] = []
    seen = set()

    for name in fields:
        extractor = _EXTRACTORS.get(name)
        if extractor is None:
            logger.debug("未知的抽取字段 %r，跳过", name)
            continue
        for item in extractor(html, ctx):
            key = (item.field, item.value)
            if key in seen:
                continue
            seen.add(key)
            results.append(item)

    return results


def dedupe(items: List[Evidence]) -> List[Evidence]:
    """同一字段同一值只留置信度最高的那条。"""
    best: Dict[tuple, Evidence] = {}
    for item in items:
        key = (item.field, item.value)
        if key not in best or item.confidence > best[key].confidence:
            best[key] = item
    return list(best.values())


def summarize(items: List[Evidence]) -> List[Dict[str, Any]]:
    """按风险等级降序输出，便于报告直接渲染（高危在前）。"""
    ordered = sorted(dedupe(items), key=lambda e: (-e.risk, -e.confidence, e.field))
    return [item.to_dict() for item in ordered]
