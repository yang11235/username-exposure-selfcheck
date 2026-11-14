"""中文平台补检。

思路：小红书 / 抖音 / 快手 / 贴吧这些站没有「用户名 → 个人主页」的固定路径，
拼 URL 只能得到 SPA 外壳或 403。所以不拼路径，改走站内搜索接口，把用户名
当关键词搜，再按同名程度给出置信度。

能用的情况：
  B 站   —— 公开搜索接口，先用首页拿 buvid 再查，无需登录
  微博   —— s.weibo.com/user，必须有 weibo.com 的登录 Cookie
  贴吧   —— f/search/res，必须有 tieba.baidu.com 的登录 Cookie

结果只是线索，不是「已确认账号」——同名不等于同一人。
"""
import re

import aiohttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
TIMEOUT = aiohttp.ClientTimeout(total=25)


def _cookie_map():
    """读 ~/.maigret/cookies.txt，返回 {域名: ["k=v", ...]}。"""
    from .cookie_store import _rows  # 复用同一份解析，避免两套逻辑

    out = {}
    for row in _rows():
        parts = row.split("\t")
        domain = parts[0].lstrip(".")
        out.setdefault(domain, []).append(f"{parts[5]}={parts[6]}")
    return out


def _cookie_header(jar, host):
    for domain, pairs in jar.items():
        if host == domain or host.endswith("." + domain):
            return "; ".join(pairs)
    return ""


def _text(value):
    return re.sub(r"<[^>]+>", "", value or "").strip()


def _grade(name, username):
    """同名给高置信，包含关系给低置信。"""
    a, b = (name or "").strip().lower(), (username or "").strip().lower()
    if not a:
        return "低"
    if a == b:
        return "高"
    if b in a or a in b:
        return "中"
    return "低"


async def probe_bilibili(session, username, jar):
    headers = {"User-Agent": UA, "Referer": "https://search.bilibili.com/"}
    # 先访问首页拿 buvid3，否则接口会返回风控页
    try:
        async with session.get("https://www.bilibili.com/", headers={"User-Agent": UA}) as resp:
            await resp.read()
    except Exception:
        pass

    async with session.get(
        "https://api.bilibili.com/x/web-interface/search/type",
        params={"search_type": "bili_user", "keyword": username},
        headers=headers,
    ) as resp:
        if resp.status != 200:
            return [], f"B站接口 HTTP {resp.status}"
        try:
            data = await resp.json(content_type=None)
        except Exception:
            return [], "B站接口返回的不是 JSON"

    if data.get("code") != 0:
        return [], f"B站接口错误码 {data.get('code')}"

    items = []
    for user in (data.get("data") or {}).get("result") or []:
        name = _text(user.get("uname"))
        mid = user.get("mid")
        if not name or not mid:
            continue
        extra = f"粉丝 {user.get('fans', '?')}"
        level = user.get("level") or user.get("level_id")
        if level:
            extra += f" · 等级 {level}"
        items.append(
            {
                "platform": "B站",
                "name": name,
                "url": f"https://space.bilibili.com/{mid}",
                "confidence": _grade(name, username),
                "extra": extra,
            }
        )
    return items, ""


async def probe_weibo(session, username, jar):
    cookie = _cookie_header(jar, "weibo.com")
    if not cookie:
        return [], "微博需要登录 Cookie（在设置里粘贴 weibo.com 的 Cookie）"

    async with session.get(
        "https://s.weibo.com/user",
        params={"q": username},
        headers={"User-Agent": UA, "Cookie": cookie},
        allow_redirects=True,
    ) as resp:
        if resp.status != 200:
            return [], f"微博 HTTP {resp.status}（Cookie 可能已过期）"
        html = await resp.text()

    if "card-wrap" not in html and "登录" in html:
        return [], "微博 Cookie 已失效，重新登录后再导出一次"

    items = []
    for href, name in re.findall(r'<a class="name"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        name = _text(name)
        if not name:
            continue
        url = href if href.startswith("http") else f"https:{href}"
        items.append(
            {
                "platform": "微博",
                "name": name,
                "url": url,
                "confidence": _grade(name, username),
                "extra": "",
            }
        )
    return items, "" if items else "微博没有搜到同名用户"


async def probe_tieba(session, username, jar):
    cookie = _cookie_header(jar, "tieba.baidu.com")
    if not cookie:
        return [], "贴吧需要登录 Cookie（在设置里粘贴 tieba.baidu.com 的 Cookie）"

    async with session.get(
        "https://tieba.baidu.com/f/search/res",
        params={"qw": username, "ie": "utf-8"},
        headers={"User-Agent": UA, "Cookie": cookie},
    ) as resp:
        if resp.status != 200:
            return [], f"贴吧 HTTP {resp.status}（Cookie 可能已过期）"
        html = await resp.text()

    items = []
    for name in re.findall(r'class="p_author_name"[^>]*>([^<]+)<', html):
        name = name.strip()
        if name and name != "匿名用户":
            items.append(
                {
                    "platform": "贴吧",
                    "name": name,
                    "url": f"https://tieba.baidu.com/home/main?un={name}",
                    "confidence": _grade(name, username),
                    "extra": "",
                }
            )
    return items, "" if items else "贴吧没有搜到相关发言"


PROBES = (probe_bilibili, probe_weibo, probe_tieba)


async def probe_platforms(usernames):
    """对所有用户名跑一遍补检，返回 (items, notes)。"""
    jar = _cookie_map()
    items, notes = [], []

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        for username in usernames:
            for probe in PROBES:
                try:
                    got, note = await probe(session, username, jar)
                except Exception as exc:  # 单个平台失败不影响其它
                    got, note = [], f"{probe.__name__} 失败：{exc}"
                items.extend(got)
                if note and note not in notes:
                    notes.append(note)

    # 按平台+链接去重，高置信排前面
    seen, unique = set(), []
    order = {"高": 0, "中": 1, "低": 2}
    for item in sorted(items, key=lambda x: order.get(x["confidence"], 3)):
        key = (item["platform"], item["url"])
        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique, notes
