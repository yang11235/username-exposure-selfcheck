"""中文平台补检。

只覆盖**快速检索覆盖不到**的平台。这一点很重要：贴吧、B 站搜索已经并进
`sites_cn.json`，走四字段判定（贴吧用匿名可用的 `home/get/panel`），
不该在这里重复一遍。

能用的情况：
  B 站   —— 公开搜索接口，先用首页拿 buvid 再查，无需登录
  微博   —— s.weibo.com/user，必须有 weibo.com 的登录 Cookie

历史说明：这里曾有一条 `probe_tieba`，走 `f/search/res` 做"谁发帖提过这个
词"的全站搜索。它和贴吧账号判定是两件不同的事，且必须登录才能用；
2026-11-15 起贴吧账号改为匿名可判的四字段规则，那条搜索路径被删除——
它只会错误地提示"贴吧需要登录 Cookie"，而实际上快速检索早就查过了。

结果只是线索，不是「已确认账号」——同名不等于同一人。
"""
import re

import aiohttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
TIMEOUT = aiohttp.ClientTimeout(total=25)


def _cookie_map(browser_jar=None):
    """返回 {域名: ["k=v", ...]}，浏览器来源优先于 cookies.txt。

    两个来源的优先级和 `resolve_cookie_source` 的口径一致：浏览器里的是
    "当前"的，cookies.txt 是手动快照。浏览器有就用浏览器的，没有才读文件。

    为什么要合并而不是二选一：补检要访问多个域名（微博 + B 站），可能
    浏览器里只有其中一个的登录态、另一个在 cookies.txt 里。按域名逐条
    叠加能同时用上两边的。
    """
    out = {}

    # 先放 cookies.txt 的，浏览器来源后写会覆盖同名域
    from .cookie_store import _rows  # 复用同一份解析，避免两套逻辑

    for row in _rows():
        parts = row.split("\t")
        domain = parts[0].lstrip(".")
        out.setdefault(domain, []).append(f"{parts[5]}={parts[6]}")

    if browser_jar is not None:
        from maigret.cn.browser_cookies import to_cookie_header_map

        for domain, pairs in to_cookie_header_map(browser_jar).items():
            out[domain.lstrip(".")] = pairs

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


PROBES = (probe_bilibili, probe_weibo)


async def probe_platforms(usernames, browser_jar=None):
    """对所有用户名跑一遍补检，返回 (items, notes)。

    `browser_jar` 是从本机 Chrome 取到的 CookieJar（cdp，内存直用）。
    传进来时必须优先于 cookies.txt：用户在设置里勾了"从浏览器读取"，
    那份是最新的，而 cookies.txt 是某天手动粘进去的快照。

    原来这里只读 cookies.txt，于是出现了「设置里勾了浏览器 Cookie、
    /api/browser-cookies 也显示检测到 103 条，补检却说需要登录」的矛盾——
    两条链路各自读各自的来源，谁也看不到对方。
    """
    jar = _cookie_map(browser_jar)
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
