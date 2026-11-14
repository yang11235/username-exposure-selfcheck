"""把浏览器里的 Cookie 头写成 maigret 能读的 Netscape cookies.txt。

maigret 用 http.cookiejar.MozillaCookieJar 读取这个文件，格式必须是
domain \t includeSubdomains \t path \t secure \t expiry \t name \t value。
"""
import time
from pathlib import Path

COOKIE_PATH = Path.home() / ".maigret" / "cookies.txt"
_HEADER = "# Netscape HTTP Cookie File\n# 由 Maigret Web 界面生成，可用浏览器扩展重新导出覆盖\n"
_FAR_FUTURE = 365 * 24 * 3600


def _rows():
    """读出现有的数据行（跳过注释和空行）。"""
    if not COOKIE_PATH.exists():
        return []
    rows = []
    for line in COOKIE_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if len(line.split("\t")) >= 7:
            rows.append(line)
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
    """判断粘贴的是不是 Netscape 格式（浏览器扩展导出的就是这种，制表符分隔 7 列）。"""
    for line in (text or "").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if len(line.split("\t")) >= 7:
            return True
    return False


def import_netscape(text):
    """导入整份 cookies.txt（可含多个域名），返回 (总条数, 域数)。"""
    rows, domains = [], set()
    for line in (text or "").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        rows.append("\t".join(parts[:7]))
        domains.add(parts[0].lstrip("."))
    if not rows:
        raise ValueError("没有解析到 Netscape 格式的行（应为制表符分隔的 7 列）")

    # 同名域名整体替换，避免新旧登录态混在一起
    kept = [row for row in _rows() if row.split("\t")[0].lstrip(".") not in domains]
    _write(kept + rows)
    return len(rows), len(domains)
