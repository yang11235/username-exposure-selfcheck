"""把用户填的主页 URL 反解成「站点名 + 用户名」，用作核心账号锚点。

只做字符串匹配，不发网络请求：拿站点库里每条规则的 urlMain 比域名，
再用 url / urlProbe 模板反推 {username} 的位置，最后用 regexCheck 校验。
"""
import json
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

_PLACEHOLDER = "{username}"
_DB_PATH = Path.home() / ".maigret" / "data.json"
_cache = {"mtime": None, "sites": {}}


def load_sites():
    """读站点库（只取站点字典部分），按 mtime 缓存。"""
    try:
        mtime = _DB_PATH.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] == mtime and _cache["sites"]:
        return _cache["sites"]
    try:
        data = json.loads(_DB_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sites = data.get("sites") or {}
    _cache["mtime"], _cache["sites"] = mtime, sites
    return sites


def _host(url):
    try:
        host = urlparse(url if "//" in url else "//" + url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _path(url):
    try:
        return urlparse(url).path.rstrip("/")
    except ValueError:
        return ""


def _template_to_regex(template):
    """https://github.com/{username} → 正则，捕获用户名。"""
    if _PLACEHOLDER not in template:
        return None
    path = _path(template)
    escaped = re.escape(path).replace(re.escape(_PLACEHOLDER), "([^/?#]+)")
    return re.compile("^" + escaped + "$", re.I)


def _host_match(main_host, host):
    """主域名相同也算——B站主页在 space.bilibili.com，而 urlMain 是 bilibili.com。"""
    if not main_host or not host:
        return False
    if main_host == host:
        return True
    # 只允许「目标域名是主域名的子域」，避免把 github.io 当成 github.com
    return main_host in host and host.endswith("." + main_host)


def resolve(url, sites=None, extra_candidates=None):
    """返回 (site_name, username) 或 (None, None)。

    sites 省略时自动从 ~/.maigret/data.json 读取；extra_candidates 是已经
    解析出的 {值: 类型}（来自 extract_ids_from_page），用作兜底。
    """
    if sites is None:
        sites = load_sites()
    host = _host(url)
    if not host:
        return None, None
    target_path = _path(url)

    matched = []
    for name, info in (sites or {}).items():
        main = info.get("urlMain") or ""
        if _host_match(_host(main), host):
            matched.append((name, info))

    if not matched:
        return None, None

    # 精确同域优先，子域匹配排在后面（B站的双层域名容易被误判）
    matched.sort(key=lambda kv: _host(kv[1].get("urlMain") or "") != host)

    # 优先用 url / urlProbe 模板精确反解
    for name, info in matched:
        for key in ("url", "urlProbe"):
            template = info.get(key) or ""
            pattern = _template_to_regex(template)
            if not pattern:
                continue
            m = pattern.match(target_path)
            if m:
                value = unquote(m.group(1))
                check = info.get("regexCheck")
                if check:
                    try:
                        if not re.search(check, value):
                            continue
                    except re.error:
                        pass
                return name, value

    # 模板对不上（比如主页带子路径或 query），退回路径里的候选段。
    # 注意 github.com/soxoj/repo 这种：仓库页的第一段才是用户名，最后一段是仓库名。
    segments = [unquote(s) for s in target_path.split("/") if s]
    skip = {"", "u", "user", "users", "people", "members", "profile", "n", "home", "main"}
    name = matched[0][0]
    info = matched[0][1]
    check = info.get("regexCheck")

    def _ok(value):
        if not check:
            return True
        try:
            return bool(re.search(check, value))
        except re.error:
            return True

    for value in segments[:2]:
        if value not in skip and _ok(value):
            return name, value

    # 同名域名下缩不出用户名：站点认出来了，用已知解析结果里最像的那个
    for value in (extra_candidates or {}):
        for site_name, _info in matched:
            if value.lower() in site_name.lower() or site_name.lower() in value.lower():
                return site_name, value

    return None, None
