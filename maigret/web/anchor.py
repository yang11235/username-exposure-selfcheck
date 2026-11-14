"""以某个账号为核心，判断同名账号是不是同一个人。

思路：锚点账号提供姓名、简介、位置、公司、主页这几类可比的信号，其它账号逐个
比对，加起来得分。只做「像不像」，不下身份结论——最终判定仍要人来看。
"""
import re
from urllib.parse import urlparse

# 字段标签（profile_card 已翻译成中文）
NAME_KEYS = ("姓名", "昵称")
BIO_KEYS = ("简介",)
LOC_KEYS = ("位置", "城市", "国家")
ORG_KEYS = ("公司",)
LINK_KEYS = ("主页", "博客", "网站")

_STRIP = re.compile(r"[\s\-_·.,，。()（）\[\]【】/\\|@#:：;；'\"“”‘’]+")


def _norm(text):
    return _STRIP.sub("", (text or "").lower())


def _tokens(text):
    """英文按词切，中文按二元组切，避免中文整句只能全等匹配。"""
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9]{2,}", text)
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    grams = [cjk[i : i + 2] for i in range(len(cjk) - 1)]
    return set(words) | set(grams)


def _jaccard(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _domain(url):
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _fields_map(account):
    """把 [(标签, 值)] 变成 {标签: [值, ...]}，同一个标签可能有多个来源值。"""
    out = {}
    for label, value in account.get("fields") or []:
        if not value:
            continue
        out.setdefault(label, []).append(value)
    return out


def _pick(fields, keys):
    for key in keys:
        if fields.get(key):
            return fields[key]
    return []


def _split_values(values):
    """'A（GitHub） / B（Weibo）' → ['A', 'B']"""
    out = []
    for raw in values:
        for part in str(raw).split(" / "):
            part = re.sub(r"（[^）]*）$", "", part).strip()
            if part:
                out.append(part)
    return out


def score_accounts(anchor_site, accounts, username=None):
    """给每个账号打相关度分。accounts 是 claimed_profiles 列表。

    username 不为 None 时只在同一用户名的组内比对——变体搜索或多用户名场景下，
    不同用户名其实是不同起点，混在一起比会把基准搞乱。
    """
    if username is not None:
        accounts = [a for a in accounts if a.get("username") == username]

    anchor = next((a for a in accounts if a.get("site_name") == anchor_site), None)
    if not anchor:
        return {"error": f"找不到账号 {anchor_site}"}

    af = _fields_map(anchor)
    names = _split_values(_pick(af, NAME_KEYS))
    bios = _split_values(_pick(af, BIO_KEYS))
    locs = _split_values(_pick(af, LOC_KEYS))
    orgs = _split_values(_pick(af, ORG_KEYS))
    links = _split_values(_pick(af, LINK_KEYS))
    anchor_domains = {_domain(link) for link in links if _domain(link)}

    if not (names or bios or locs or orgs or anchor_domains):
        return {
            "error": "核心账号几乎没有可比信息（没有姓名、简介、位置或主页），换个信息更全的账号试试"
        }

    scored = []
    for account in accounts:
        site = account.get("site_name")
        if site == anchor_site:
            continue
        f = _fields_map(account)
        score, reasons = 0.0, []

        # 姓名
        best = None
        for name in _split_values(_pick(f, NAME_KEYS)):
            for target in names:
                a, b = _norm(name), _norm(target)
                if not a or not b:
                    continue
                if a == b:
                    cand = (3.0, f"姓名与核心一致（{name}）")
                elif a in b or b in a:
                    cand = (2.0, f"姓名包含核心（{name}≈{target}）")
                elif _jaccard(a, b) >= 0.5:
                    cand = (1.0, f"姓名相近（{name}≈{target}）")
                else:
                    cand = (0.0, "")
                if cand[0] and (best is None or cand[0] > best[0]):
                    best = cand
        if best:
            score += best[0]
            reasons.append(best[1])

        # 简介
        if bios:
            top = max((_jaccard(bio, x) for bio in _split_values(_pick(f, BIO_KEYS)) for x in bios), default=0)
            if top >= 0.5:
                score += 2.0
                reasons.append("简介高度重合")
            elif top >= 0.2:
                score += 1.0
                reasons.append("简介部分重合")

        # 位置
        for loc in _split_values(_pick(f, LOC_KEYS)):
            if any(_norm(loc) and (_norm(loc) in _norm(t) or _norm(t) in _norm(loc)) for t in locs):
                score += 2.0
                reasons.append(f"位置一致（{loc}）")
                break

        # 公司
        for org in _split_values(_pick(f, ORG_KEYS)):
            if any(_norm(org) == _norm(t) or _norm(org) in _norm(t) for t in orgs):
                score += 1.0
                reasons.append(f"公司一致（{org}）")
                break

        # 主页/博客域名
        doms = {_domain(link) for link in _split_values(_pick(f, LINK_KEYS)) if _domain(link)}
        if doms & anchor_domains:
            score += 3.0
            reasons.append("主页域名与核心相同")

        if score >= 4:
            level = "高"
        elif score >= 2:
            level = "中"
        else:
            level = "低"

        if not reasons:
            other_names = _split_values(_pick(f, NAME_KEYS))
            if other_names and names:
                reasons.append(f"姓名与核心不同（{other_names[0]}）")
            else:
                reasons.append("缺少可对照的字段（无姓名、简介、位置或主页）")

        scored.append(
            {
                "site": site,
                "score": round(score, 1),
                "level": level,
                "reasons": reasons or ["没有可与核心账号对照的字段"],
            }
        )

    scored.sort(key=lambda x: -x["score"])
    counts = {lvl: sum(1 for s in scored if s["level"] == lvl) for lvl in ("高", "中", "低")}
    return {"anchor": anchor_site, "username": username, "items": scored, "counts": counts}
