"""把浏览器里的 Cookie 头写成 maigret 能读的 Netscape cookies.txt。

maigret 用 http.cookiejar.MozillaCookieJar 读取这个文件，格式必须是
domain \t includeSubdomains \t path \t secure \t expiry \t name \t value。
"""
import time
from pathlib import Path

COOKIE_PATH = Path.home() / ".maigret" / "cookies.txt"
_HEADER = "# Netscape HTTP Cookie File\n# 由 Maigret Web 界面生成，可用浏览器扩展重新导出覆盖\n"
_FAR_FUTURE = 365 * 24 * 3600

# Chrome 扩展（Get cookies.txt LOCALLY、EditThisCookie 等）导出 httpOnly
# cookie 时会写 `#HttpOnly_.baidu.com\t...`——用 `#` 做前缀是 Netscape 格式的
# 约定（curl 也认）。但整行以 `#` 开头，很容易被"跳过注释"的逻辑一起丢掉。
#
# 后果很具体：百度的登录凭据 BDUSS_BFESS 和 passport 的 PTOKEN 都是 httpOnly，
# 恰恰是最关键的几条。把它们当注释扔掉，导入完看着"成功导入 N 条"，
# 实际拿到的是一个没有登录态的残缺文件。
_HTTPONLY_PREFIX = "#HttpOnly_"


def _data_line(line):
    """把一行拆成 7 列；不是数据行则返回 None。

    `#HttpOnly_` 开头的行是数据（httpOnly cookie），其余 `#` 开头才是注释。
    返回时把前缀去掉——落盘不需要它，MozillaCookieJar 也不认这个前缀。
    """
    if not line or not line.strip():
        return None
    if line.startswith(_HTTPONLY_PREFIX):
        line = line[len(_HTTPONLY_PREFIX):]
    elif line.startswith("#"):
        return None
    parts = line.split("\t")
    if len(parts) < 7:
        return None
    return parts


def _rows():
    """读出现有的数据行（跳过注释和空行，保留 httpOnly 行）。"""
    if not COOKIE_PATH.exists():
        return []
    rows = []
    for line in COOKIE_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = _data_line(line)
        if parts:
            rows.append("\t".join(parts[:7]))
    return rows


def _write(rows):
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(rows)
    COOKIE_PATH.write_text(_HEADER + (body + "\n" if body else ""), encoding="utf-8")


def parse_pairs(text):
    """把 'a=1; b=2' 这样的 Cookie 头拆成 [(name, value)]。"""
    pairs = []
    for chunk in text.replace("\r", "").replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name, value = name.strip(), value.strip().strip('"')
        if name and value:
            pairs.append((name, value))
    return pairs


def normalize_domain(domain):
    domain = (domain or "").strip().lower()
    if "//" in domain:
        domain = domain.split("//", 1)[1]
    domain = domain.split("/")[0].split(":")[0]
    return domain.lstrip(".")


def save_cookies(domain, cookie_header):
    """写入/替换某个域名的 Cookie，返回写入条数。"""
    domain = normalize_domain(domain)
    if not domain:
        raise ValueError("域名不能为空")
    pairs = parse_pairs(cookie_header)
    if not pairs:
        raise ValueError("没有解析出 name=value，请粘贴浏览器请求头里 Cookie 后面的内容")

    kept = [row for row in _rows() if row.split("\t")[0].lstrip(".") != domain]
    expiry = int(time.time()) + _FAR_FUTURE
    for name, value in pairs:
        kept.append("\t".join([f".{domain}", "TRUE", "/", "TRUE", str(expiry), name, value]))
    _write(kept)
    return len(pairs)


def cookie_domains():
    """返回 {域名: Cookie 条数}，用于界面显示。"""
    counts = {}
    for row in _rows():
        domain = row.split("\t")[0].lstrip(".")
        counts[domain] = counts.get(domain, 0) + 1
    return counts


def delete_domain(domain):
    domain = normalize_domain(domain)
    kept = [row for row in _rows() if row.split("\t")[0].lstrip(".") != domain]
    _write(kept)


def looks_netscape(text):
    """判断粘贴的是不是 Netscape 格式（浏览器扩展导出的就是这种，制表符分 7 列）。

    只认 `#HttpOnly_` 开头的数据行，其余忽略——见 _data_line 的说明。
    """
    for line in (text or "").splitlines():
        if _data_line(line):
            return True
    return False


def import_netscape(text):
    """导入整份 cookies.txt（可含多个域名），返回 (总条数, 域数)。

    两种来源都能用：
        浏览器扩展导出的 cookies.txt（可能带 #HttpOnly_ 行）
        手动拼的 Netscape 格式

    同名域名整体替换，避免新旧登录态混在一起——旧 BDUSS 残留会和新的打架，
    表现为"导入成功但站点判不出来"。
    """
    rows, domains = [], set()
    for line in (text or "").splitlines():
        parts = _data_line(line)
        if not parts:
            continue
        rows.append("\t".join(parts[:7]))
        domains.add(parts[0].lstrip("."))
    if not rows:
        raise ValueError("没有解析到 Netscape 格式的行（应为制表符分隔的 7 列）")

    kept = [row for row in _rows() if row.split("\t")[0].lstrip(".") not in domains]
    _write(kept + rows)
    return len(rows), len(domains)
