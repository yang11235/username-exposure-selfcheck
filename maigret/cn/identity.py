"""身份聚类：把"同一个用户名在不同站点上的账号"归并成"一个人"。

替代上游 `anchor.py` 的两两打分（文档 §5.5）。上游那个实现的三个毛病：
字段标签硬编码、阈值 4/2 写死、完全不用头像和时间。这里换成一个
"加权边 + 并查集"的模型，每条边的权重来自哪条规则是显式的、可解释的。

为什么头像 pHash 是第一优先级（文档 §5.5 原话）：
    中国用户跨平台复用同一头像的比例极高，而这条信号**完全免费、不需登录、
    不受反爬影响**——只要拿到头像 URL 就能算。投入产出比最高。

三条设计红线，都是"宁可少合并，不可乱合并"：

1. **只存哈希，不存原图。** 本项目不做分享、不做云端，头像原图没有任何
   留存的理由；64bit 哈希足够比较，泄漏了也还原不出图。

2. **默认头像必须被识别成占位符。** 这是 pHash 聚类最容易翻车的地方：
   知乎/掘金的默认头像是**同一张图**，所有没设头像的账号哈希全等，
   于是"头像匹配"会把几十个毫不相干的人聚成一簇，还顶着 0.85 的高置信。
   见 `placeholder_hashes()`。

3. **簇置信度取簇内最小边权重**，不是平均（文档 §5.5）。一个簇的可信度由
   最弱那条连接决定——平均分会让"一条铁证 + 九条弱信号"看起来比实际更可信。

依赖策略：Pillow 是**可选**依赖（项目没声明它）。没有 Pillow 时 `phash()`
返回 None，头像这条边直接不存在——不是猜一个值，是这条信号不可用。
"""

import logging
import re
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 边规则（文档 §5.5 的表，权重原样保留）
# ---------------------------------------------------------------------------
# 权重不是"这条信号有多准"，而是"光凭它能不能把人认出来"。
# 0.90 的跨站互链是铁证（用户自己填的）；0.20 的同名只是弱提示，
# 因为同一个用户名在不同站点上极可能是不同的人（英文名、常见词）。
EDGE_CROSS_LINK = "cross_link"
EDGE_AVATAR = "avatar_phash"
EDGE_BIO = "bio_simhash"
EDGE_NAME = "name_exact"
EDGE_LOCATION = "location"
EDGE_USERNAME = "username_same"

EDGE_RULES: Tuple[Tuple[str, float], ...] = (
    (EDGE_CROSS_LINK, 0.90),   # A 站主页链到 B 站
    (EDGE_AVATAR, 0.85),       # 头像感知哈希汉明距离 ≤ 6
    (EDGE_BIO, 0.70),          # 简介 SimHash 汉明距离 ≤ 3
    (EDGE_NAME, 0.50),         # 姓名规范化后全等
    (EDGE_LOCATION, 0.30),     # 城市/省份一致
    (EDGE_USERNAME, 0.20),     # 同一用户名
)
RULE_WEIGHTS: Dict[str, float] = dict(EDGE_RULES)

# 头像 pHash 的判定阈值（64bit 汉明距离）。
# 随机两张图的期望距离约 32，≤6 意味着"几乎逐位相同"，是个很紧的门槛。
AVATAR_MAX_DISTANCE = 6

# 简介 SimHash 的阈值（64bit）。
# 比头像更松一点：简介是自由文本，改几个字、加个 emoji 都会变。
BIO_MAX_DISTANCE = 3

# 一个头像哈希被多少个账号共享时，判定为"平台默认头像"。
#
# 为什么是 5 而不是 2 或 3：真实用户跨 3 个平台复用同一头像很常见，阈值定低了
# 会把这些真实信号一起丢掉；而默认头像在几十上百个账号里出现，5 已经足够
# 把它筛出来。代价是：当账号总数少于 5 时这个守卫基本不生效——这是有意的
# 取舍，小样本下"宁可少合并"由后面的簇置信度和人工复核兜底。
PLACEHOLDER_MIN_COUNT = 5

# 姓名比对的最短长度。单字（"王"、"李"）会和所有人撞，必须排除。
MIN_NAME_LEN = 2

# 平台默认简介（规范化后比对）。
#
# 和默认头像是同一类问题：这些话是平台预填的，几百万个账号一字不差。
# 不排掉的话，"简介相似"会把所有懒得填简介的人连成一簇。
# 只收"整句等于"的固定文案，不做子串匹配——"这个人很懒，但还是关注了 3 个话题"
# 已经是用户自己改过的内容，有区分度。
_BIO_PLACEHOLDERS = {
    "这个人很懒什么都没留下",
    "这个人很懒什么也没留下",
    "这个人很懒什么都没有留下",
    "这个人很懒暂时没有介绍",
    "还没有填写简介",
    "暂无简介",
    "暂无介绍",
    "没有简介",
    "这个人很神秘什么都没写",
    "这个人很神秘什么也没写",
    "nothinghere",
}


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------
@dataclass
class Account:
    """一个站点上的一个账号。

    `avatar_hash` 是 64bit pHash 整数（不是图片、不是 URL）。
    拿不到头像时保持 None —— 与"哈希是 0"区分开，后者是个合法哈希。
    """

    site: str
    username: str
    url: str = ""
    avatar_hash: Optional[int] = None
    bio: str = ""
    name: str = ""
    location: str = ""
    links: List[str] = field(default_factory=list)

    def label(self) -> str:
        return f"{self.site} {self.username}".strip()


@dataclass
class Edge:
    a: int                 # 账号下标
    b: int
    weight: float
    rule: str
    detail: str = ""


@dataclass
class Cluster:
    """一簇 = 判定为同一个人的一组账号。"""

    members: List[Account]
    confidence: float          # 簇内最小边权重；单成员簇为 0
    edges: List[Edge] = field(default_factory=list)
    # members[i] 在原始 accounts 序列里的下标。
    # 留着是为了把 Edge 上的全局下标翻译回成员——簇只是全部账号的一部分，
    # 直接拿 Edge.a 去索引 members 会错位。
    indexes: List[int] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.members)


# ---------------------------------------------------------------------------
# 头像 pHash（Pillow 可选）
# ---------------------------------------------------------------------------
def pillow_available() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except Exception:
        return False
    return True


# 32×32 的 DCT 余弦表，进程内算一次。
_COS_TABLE: Optional[List[List[float]]] = None
_DCT_SIZE = 32
_HASH_SIZE = 8


def _cos_table() -> List[List[float]]:
    global _COS_TABLE
    if _COS_TABLE is None:
        import math

        n = _DCT_SIZE
        _COS_TABLE = [
            [math.cos(math.pi * (2 * x + 1) * u / (2 * n)) for x in range(n)]
            for u in range(n)
        ]
    return _COS_TABLE


def _dct_1d(row: Sequence[float]) -> List[float]:
    """一维 DCT-II。长度 32，纯 Python 足够快（32×32 总共约 6.5 万次乘加）。"""
    table = _cos_table()
    return [sum(row[x] * table[u][x] for x in range(_DCT_SIZE)) for u in range(_DCT_SIZE)]


def phash(image_bytes: bytes) -> Optional[int]:
    """算 64bit 感知哈希。拿不到就返回 None，绝不返回一个假哈希。

    算法与 `imagehash.phash()` 一致（便于将来与它的值互通）：
        缩到 32×32 灰度 → 二维 DCT → 取左上 8×8 低频 → 与中位数比较得 64 位。
    低频系数描述的是"大块明暗分布"，正是头像这种小图的稳定特征；
    高频（纹理、压缩噪声）被丢掉，所以改尺寸、加轻微滤镜都不影响。

    Pillow 不在时返回 None：本项目没把 Pillow 声明成依赖，用户环境可能没有。
    """
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:
        logger.debug("Pillow 不可用，跳过头像 pHash")
        return None

    # Pillow 10 起 LANCZOS 迁到 Image.Resampling 下，10 之前只有顶层别名。
    # 两种写法都留着，避免为了一个重采样常量去卡版本。
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")

    try:
        with Image.open(BytesIO(image_bytes)) as img:
            small = img.convert("L").resize((_DCT_SIZE, _DCT_SIZE), resample)
            # 用 tobytes() 而不是 getdata()：后者在 Pillow 13 已废弃
            # （Pillow 14 移除），而本项目 pytest.ini 设了 filterwarnings=error，
            # 废弃警告会直接变成异常——表现为"所有头像都解码失败"。
            # 灰度图的 tobytes() 就是一串 0-255 的像素值，语义完全等价且更快。
            pixels = list(small.tobytes())
    except (OSError, ValueError, SyntaxError) as exc:
        # 下载到的可能是一段 HTML 错误页、或者被截断的图。都当"没有头像"。
        # 只接这几种"输入不合法"的异常：把 Exception 一网打尽会连代码 bug
        # 一起吞掉，变成"所有头像都算不出哈希"这种极难排查的静默失败。
        logger.debug("头像解码失败，跳过：%s", exc)
        return None

    if len(pixels) != _DCT_SIZE * _DCT_SIZE:
        return None

    rows = [pixels[i * _DCT_SIZE : (i + 1) * _DCT_SIZE] for i in range(_DCT_SIZE)]
    # 可分离性：先行 DCT 再列 DCT，等价于二维 DCT，但少一半计算。
    after_rows = [_dct_1d(row) for row in rows]
    after_cols = [
        [after_rows[y][x] for y in range(_DCT_SIZE)] for x in range(_DCT_SIZE)
    ]
    dct = [_dct_1d(col) for col in after_cols]  # dct[x][u]，转置存法

    low = [dct[x][y] for x in range(_HASH_SIZE) for y in range(_HASH_SIZE)]
    ordered = sorted(low)
    # 偶数个取中间两个的平均，避免 median 在平台上抖动
    median = (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2

    bits = 0
    for value in low:
        bits = (bits << 1) | (1 if value > median else 0)
    return bits


def hamming(a: Optional[int], b: Optional[int]) -> Optional[int]:
    """汉明距离。任一方为 None 时返回 None（不是 0，也不是无穷大）。"""
    if a is None or b is None:
        return None
    return bin(a ^ b).count("1")


def placeholder_hashes(
    accounts: Iterable[Account], min_count: int = PLACEHOLDER_MIN_COUNT
) -> Set[int]:
    """找出"平台默认头像"的哈希。

    为什么必须有这一步：pHash 只回答"这两张图看起来像不像"，回答不了
    "这张图是不是所有人都在用"。而知乎、掘金、豆瓣的默认头像是各平台
    自己的一张图，大量账号共享——不筛掉的话，"头像匹配"这一条会把所有
    没设头像的账号连成一簇，而且权重是 0.85，看起来像铁证。

    这是 pHash 聚类最典型的假阳性来源，比"阈值调太松"严重得多：
    阈值调松只是偶尔多连一条边，默认头像是一次性把几十个陌生人连成一片。
    """
    counts: Dict[int, int] = {}
    for account in accounts:
        if account.avatar_hash is None:
            continue
        counts[account.avatar_hash] = counts.get(account.avatar_hash, 0) + 1
    common = {h for h, c in counts.items() if c >= min_count}
    if common:
        logger.debug("识别到 %d 个疑似默认头像哈希，已在聚类中忽略", len(common))
    return common


# ---------------------------------------------------------------------------
# 文本信号
# ---------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[\s\-_.·,，。、/\\|()（）\[\]【】<>《》'\"]+")


def normalize_text(text: str) -> str:
    """去掉空白与常见分隔符，转小写。用于姓名/城市比对。

    不去掉这些的话，"张 伟" 和 "张伟"、"zhang-wei" 和 "zhangwei" 会被当成
    两个人——而这些恰恰是同一批人在不同站点的写法差异。
    """
    return _PUNCT_RE.sub("", (text or "").strip().lower())


def simhash(text: str, bits: int = 64) -> Optional[int]:
    """文本 SimHash（按字符 3-gram）。

    为什么用字符 3-gram 而不是分词：中文分词要额外词典，而简介是短文本、
    还常混着英文和 emoji。3-gram 对"改几个字"不敏感、对"换一段话"敏感，
    正好是这里要的性质。

    返回 None 的两种情况（都不是"哈希为 0"，而是"这条信号不可用"）：
        - 规范化后短于 4 个字符：太短的简介算出来没有区分度
        - 命中平台默认简介（"这个人很懒"之类，几百万人共用）
    """
    norm = normalize_text(text)
    if len(norm) < 4:
        return None
    if norm in _BIO_PLACEHOLDERS:
        return None

    weights = [0] * bits
    for i in range(len(norm) - 2):
        gram = norm[i : i + 3]
        h = _stable_hash(gram)
        for b in range(bits):
            weights[b] += 1 if (h >> b) & 1 else -1

    result = 0
    for b in range(bits):
        if weights[b] > 0:
            result |= 1 << b
    return result


def _stable_hash(text: str) -> int:
    """跨进程稳定的字符串哈希。

    不能用内置 hash()：Python 3.3+ 对 str 启用了随机化种子（PYTHONHASHSEED），
    同一个字符串在两个进程里得到的值不同。聚类结果要能复现、要能跨进程比较
    缓存下来的哈希，所以这里自己算 FNV-1a。
    """
    h = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def normalize_url(url: str) -> str:
    """把链接规范化成"同一个站点的同一个人"。

    去掉协议、www、末尾斜杠、查询串和锚点——`https://github.com/zhangwei?tab=repos`
    和 `github.com/zhangwei` 是同一个人。
    """
    value = (url or "").strip().lower()
    if not value:
        return ""
    value = re.sub(r"^[a-z]+://", "", value)
    value = re.sub(r"^www\.", "", value)
    value = value.split("#", 1)[0].split("?", 1)[0]
    return value.rstrip("/")


# ---------------------------------------------------------------------------
# 边权重
# ---------------------------------------------------------------------------
def edge_between(
    a: Account,
    b: Account,
    placeholders: Optional[Set[int]] = None,
    avatar_max_distance: int = AVATAR_MAX_DISTANCE,
    bio_max_distance: int = BIO_MAX_DISTANCE,
) -> Optional[Edge]:
    """两条账号之间的最强证据。没有证据返回 None。

    取"最强"而不是"累加"：这些信号高度相关（同一个人往往头像、简介、
    城市全都一样），累加会让一个真实身份轻松突破 1.0，阈值也就失去意义了。
    """
    placeholders = placeholders or set()
    best: Optional[Tuple[float, str, str]] = None

    def consider(rule: str, detail: str, ok: bool) -> None:
        nonlocal best
        if not ok:
            return
        weight = RULE_WEIGHTS[rule]
        if best is None or weight > best[0]:
            best = (weight, rule, detail)

    # 1. 跨站互链：用户自己在 A 站主页填了 B 站主页的链接。最强证据。
    #    三个方向都算，因为用户只会填一边：
    #      A 的简介里贴了 B 的主页 / B 贴了 A 的 / 两边都贴了同一个第三方主页。
    #    只比"两边链接集合的交集"会漏掉最常见的情况——A 贴了 B、B 没贴 A。
    links_a = {normalize_url(u) for u in a.links if normalize_url(u)}
    links_b = {normalize_url(u) for u in b.links if normalize_url(u)}
    url_a, url_b = normalize_url(a.url), normalize_url(b.url)
    hit = ""
    if url_b and url_b in links_a:
        hit = f"链到 {url_b}"
    elif url_a and url_a in links_b:
        hit = f"链到 {url_a}"
    else:
        shared = links_a & links_b
        if shared:
            hit = f"主页互链 {sorted(shared)[0]}"
    if hit:
        consider(EDGE_CROSS_LINK, hit, True)

    # 2. 头像 pHash
    if (
        a.avatar_hash is not None
        and b.avatar_hash is not None
        and a.avatar_hash not in placeholders
        and b.avatar_hash not in placeholders
    ):
        distance = hamming(a.avatar_hash, b.avatar_hash)
        if distance is not None and distance <= avatar_max_distance:
            consider(EDGE_AVATAR, f"头像距离 {distance}", True)

    # 3. 简介 SimHash
    bio_distance = hamming(simhash(a.bio), simhash(b.bio))
    if bio_distance is not None and bio_distance <= bio_max_distance:
        consider(EDGE_BIO, f"简介距离 {bio_distance}", True)

    # 4. 姓名全等（规范化后，且不能是单字）
    name_a, name_b = normalize_text(a.name), normalize_text(b.name)
    if len(name_a) >= MIN_NAME_LEN and name_a == name_b:
        consider(EDGE_NAME, f"姓名 {a.name}", True)

    # 5. 城市一致
    loc_a, loc_b = normalize_text(a.location), normalize_text(b.location)
    if len(loc_a) >= MIN_NAME_LEN and loc_a == loc_b:
        consider(EDGE_LOCATION, f"地区 {a.location}", True)

    # 6. 同一用户名（弱提示：常见词/英文名会撞）
    user_a, user_b = normalize_text(a.username), normalize_text(b.username)
    if user_a and user_a == user_b:
        consider(EDGE_USERNAME, "同名", True)

    if best is None:
        return None
    weight, rule, detail = best
    return Edge(0, 0, weight, rule, detail)


# ---------------------------------------------------------------------------
# 并查集
# ---------------------------------------------------------------------------
class _UnionFind:
    def __init__(self, size: int):
        self._parent = list(range(size))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def cluster(
    accounts: Sequence[Account],
    threshold: float = 0.6,
    placeholders: Optional[Set[int]] = None,
    avatar_max_distance: int = AVATAR_MAX_DISTANCE,
    bio_max_distance: int = BIO_MAX_DISTANCE,
    auto_placeholders: bool = True,
) -> List[Cluster]:
    """把账号聚成"人"。返回的簇按成员数、置信度降序排列。

    `threshold=0.6` 是文档 §5.5 的值，含义很明确：**只有头像及以上强度的
    证据才能单独把人连起来**。姓名（0.5）、地区（0.3）、同名（0.2）都低于
    阈值，它们只能作为"已连通簇内部"的解释性信息存在——这是有意的：
    两个都叫"张伟"、都在北京的人，不是同一个人的概率远大于是。
    """
    if placeholders is None and auto_placeholders:
        placeholders = placeholder_hashes(accounts)
    placeholders = placeholders or set()

    uf = _UnionFind(len(accounts))
    edges: List[Edge] = []

    for i, j in combinations(range(len(accounts)), 2):
        edge = edge_between(
            accounts[i],
            accounts[j],
            placeholders=placeholders,
            avatar_max_distance=avatar_max_distance,
            bio_max_distance=bio_max_distance,
        )
        if edge is None or edge.weight < threshold:
            continue
        edge.a, edge.b = i, j
        edges.append(edge)
        uf.union(i, j)

    groups: Dict[int, List[int]] = {}
    for index in range(len(accounts)):
        groups.setdefault(uf.find(index), []).append(index)

    clusters: List[Cluster] = []
    for indexes in groups.values():
        members = [accounts[i] for i in indexes]
        member_set = set(indexes)
        # 簇内边：两端都在这个簇里的那些
        inner = [e for e in edges if e.a in member_set and e.b in member_set]
        # 置信度取最小边权重（文档 §5.5）：最弱那条连接决定整簇可信度。
        confidence = min((e.weight for e in inner), default=0.0)
        clusters.append(
            Cluster(
                members=members,
                confidence=confidence,
                edges=inner,
                indexes=list(indexes),
            )
        )

    clusters.sort(key=lambda c: (-len(c.members), -c.confidence))
    return clusters


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
_RULE_LABEL = {
    EDGE_CROSS_LINK: "主页互链",
    EDGE_AVATAR: "头像匹配",
    EDGE_BIO: "简介相似",
    EDGE_NAME: "姓名相同",
    EDGE_LOCATION: "地区相同",
    EDGE_USERNAME: "仅同名",
}


def describe(clusters: Sequence[Cluster], min_size: int = 2) -> List[Dict[str, Any]]:
    """把簇整理成可直接序列化的结构（Web 结果页 / JSON 用）。

    和 `render()` 共用同一份"每个成员靠什么连起来"的计算：文本报告和网页
    必须给出同样的理由，否则用户在两处看到不同的解释，只会怀疑哪边算错了。
    """
    described: List[Dict[str, Any]] = []
    for item in clusters:
        if len(item) < min_size:
            continue

        # 键用全局下标（Edge 里存的就是它），再按下标取回成员。
        reasons: Dict[int, List[str]] = {}
        for edge in item.edges:
            label = f"{_RULE_LABEL.get(edge.rule, edge.rule)}（{edge.detail}）"
            reasons.setdefault(edge.a, []).append(label)
            reasons.setdefault(edge.b, []).append(label)

        described.append(
            {
                "confidence": round(float(item.confidence), 2),
                "members": [
                    {
                        "site": member.site,
                        "username": member.username,
                        "url": member.url,
                        "reasons": list(dict.fromkeys(reasons.get(index, []))),
                    }
                    for member, index in zip(item.members, item.indexes)
                ],
            }
        )
    return described


def render(clusters: Sequence[Cluster], min_size: int = 2) -> str:
    """渲染成文档 §5.5 的树形文本。

    默认只输出 ≥2 人的簇：单账号簇没有"归并"的含义，列出来只会淹没重点。
    """
    groups = describe(clusters, min_size=min_size)
    if not groups:
        return "未发现可归并的身份簇（没有足够强的跨站证据）。"

    lines: List[str] = []
    for number, group in enumerate(groups, start=1):
        strength = "，弱" if group["confidence"] < 0.8 else ""
        lines.append(f"身份簇 #{number}（置信度 {group['confidence']:.2f}{strength}）")
        for member in group["members"]:
            note = " · ".join(member["reasons"]) or "无直接证据"
            lines.append(
                f"├─ {member['site']:<10} {member['username']:<18} {note}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()
