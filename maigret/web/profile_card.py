"""从检索结果里聚合出一份「人物画像」，供 Web 结果页直接展示。

只做聚合与去重，不额外发请求：所有数据都来自各站点已经抽取到的 ids_data。
"""
from collections import Counter
from datetime import datetime, timezone

# 字段名 → 中文标签。同一含义的多个键（如 fullname / name）归到同一个标签。
FIELD_LABELS = {
    "fullname": "姓名",
    "name": "姓名",
    "real_name": "姓名",
    "nickname": "昵称",
    "username": "用户名",
    "bio": "简介",
    "description": "简介",
    "about": "简介",
    "location": "位置",
    "city": "城市",
    "country": "国家",
    "region": "地区",
    "company": "公司",
    "organisation": "公司",
    "position": "职位",
    "job": "职位",
    "gender": "性别",
    "age": "年龄",
    "birthday": "生日",
    "website": "主页",
    "blog": "博客",
    "email": "邮箱",
    "phone": "电话",
    "language": "语言",
    "followers": "粉丝数",
    "follower_count": "粉丝数",
    "following_count": "关注数",
    "reputation": "声望",
}

DATE_KEYS = (
    "created_at", "created", "registration_time", "registered_at",
    "join_date", "joined", "date_joined", "signup_date", "member_since",
)

# 标签（站点类别）中文
CATEGORY_ZH = {
    "gaming": "游戏", "coding": "编程", "photo": "摄影", "music": "音乐", "blog": "博客",
    "finance": "金融", "freelance": "自由职业", "dating": "交友", "tech": "科技",
    "forum": "论坛", "porn": "成人", "erotic": "情色", "webcam": "直播", "video": "视频",
    "movies": "影视", "hacking": "黑客", "art": "艺术", "discussion": "讨论",
    "sharing": "分享", "writing": "写作", "wiki": "百科", "business": "商务",
    "shopping": "购物", "sport": "体育", "books": "图书", "news": "新闻",
    "documents": "文档", "travel": "旅行", "maps": "地图", "hobby": "爱好",
    "apps": "应用", "classified": "分类信息", "career": "职业", "geosocial": "地理社交",
    "streaming": "流媒体", "education": "教育", "networking": "社交网络",
    "torrent": "种子", "science": "科学", "medicine": "医疗", "reading": "阅读",
    "stock": "股票", "messaging": "即时通讯", "trading": "交易", "links": "链接",
    "fashion": "时尚", "tasks": "任务", "military": "军事", "auto": "汽车",
    "gambling": "赌博", "cybercriminal": "网络犯罪", "review": "评测",
    "bookmarks": "书签", "design": "设计", "crypto": "加密货币", "ai": "AI",
    "social": "社交", "global": "全球",
}

COUNTRY_ZH = {
    "cn": "中国", "hk": "中国香港", "tw": "中国台湾", "mo": "中国澳门",
    "us": "美国", "ru": "俄罗斯", "jp": "日本", "kr": "韩国", "gb": "英国",
    "uk": "英国", "de": "德国", "fr": "法国", "it": "意大利", "es": "西班牙",
    "nl": "荷兰", "pl": "波兰", "ua": "乌克兰", "by": "白俄罗斯", "kz": "哈萨克斯坦",
    "tr": "土耳其", "ir": "伊朗", "il": "以色列", "in": "印度", "id": "印度尼西亚",
    "vn": "越南", "th": "泰国", "ph": "菲律宾", "my": "马来西亚", "sg": "新加坡",
    "au": "澳大利亚", "ca": "加拿大", "br": "巴西", "mx": "墨西哥", "ar": "阿根廷",
    "za": "南非", "eg": "埃及", "sa": "沙特阿拉伯", "ae": "阿联酋", "eu": "欧盟",
}


def _flatten(value, limit=120):
    """把任意字段值压成一行可读文本，超长或像 URL 的丢掉。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if v not in (None, ""))
    elif isinstance(value, dict):
        return ""
    text = str(value).strip()
    if not text or len(text) > limit:
        return ""
    if text.lower().startswith(("http://", "https://")) and len(text) > 40:
        return ""
    return " ".join(text.split())


def _earliest_date(results):
    """从各账号的注册时间字段里取最早的一个。"""
    best = None
    for _, r in results.items():
        st = r.get("status")
        ids = getattr(st, "ids_data", None) or {}
        for key in DATE_KEYS:
            raw = ids.get(key)
            when = _parse_date(_flatten(raw, 40))
            if when and (best is None or when < best):
                best = when
    return best.strftime("%Y-%m-%d") if best else ""


def _parse_date(text):
    if not text:
        return None
    if text.isdigit() and len(text) >= 10:
        try:
            return datetime.fromtimestamp(int(text[:10]), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    head = text[:10].replace("/", "-")
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        try:
            return datetime.strptime(head, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def account_fields(status):
    """单个账号的字段列表，供结果页展开查看。未识别的键用原始英文名。"""
    ids = getattr(status, "ids_data", None) or {}
    out = []
    for key, value in ids.items():
        if key.lower() == "image" or key.startswith("_"):
            continue
        text = _flatten(value, limit=90)
        if not text:
            continue
        out.append((FIELD_LABELS.get(key.lower(), key), text))
        if len(out) >= 12:
            break
    return out


def _slim_brief(brief, max_sentences=3):
    """变体搜索会把十几个「命中 0 个账号」的句子拼进 brief，读起来像噪音。

    只保留有命中的句子；一个都没有时退回第一句，避免空着。
    """
    if not brief:
        return ""
    parts = [p.strip() for p in str(brief).replace("。 ", "。\n").split("\n") if p.strip()]
    hits = [p for p in parts if "命中" in p and "命中 0 个" not in p]
    if not hits:
        return parts[0][:200] if parts else ""
    text = " ".join(hits[:max_sentences])
    return text[:300]


def build_profile(username, results, brief=""):
    """返回 {username, accounts, fields, first_seen, countries, interests}。"""
    claimed = []
    for site_name, r in results.items():
        st = r.get("status")
        if st is None:
            continue
        try:
            if not st.is_found():
                continue
        except Exception:
            continue
        claimed.append((site_name, r, st))

    if not claimed:
        return None

    buckets = {}
    for site_name, r, st in claimed:
        for key, value in (st.ids_data or {}).items():
            label = FIELD_LABELS.get(key.lower())
            if not label:
                continue
            text = _flatten(value)
            if text:
                entry = buckets.setdefault(label, {})
                slot = entry.setdefault(text, {"n": 0, "sites": []})
                slot["n"] += 1
                if site_name not in slot["sites"]:
                    slot["sites"].append(site_name)

    # 每个标签最多保留两个取值，按出现次数排序；带上来源站点便于核对
    fields = []
    for label in dict.fromkeys(FIELD_LABELS.values()):
        entry = buckets.get(label)
        if not entry:
            continue
        ranked = sorted(entry.items(), key=lambda kv: -kv[1]["n"])[:2]
        parts = []
        for value, slot in ranked:
            if len(slot["sites"]) == 1:
                parts.append(f"{value}（{slot['sites'][0]}）")
            else:
                parts.append(f"{value}（{slot['n']} 处）")
        fields.append((label, " / ".join(parts)))

    def _tag_counts(kind):
        counter = Counter()
        for _, _, st in claimed:
            for tag in (st.tags or []):
                if kind == "country":
                    if tag in COUNTRY_ZH:
                        counter[COUNTRY_ZH[tag]] += 1
                elif tag in CATEGORY_ZH:
                    counter[CATEGORY_ZH[tag]] += 1
        return counter.most_common(8)

    return {
        "username": username,
        "brief": _slim_brief(brief),
        "accounts": len(claimed),
        "fields": fields,
        "first_seen": _earliest_date(results),
        "countries": _tag_counts("country"),
        "interests": _tag_counts("interest"),
        "sites": [name for name, _, _ in claimed],
    }
