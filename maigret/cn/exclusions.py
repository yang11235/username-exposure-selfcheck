"""远程假阳性排除表（文档 §3.3）。

存在的理由：规则失效和假阳性随时会发生，而"发版修一个误报"的周期是几天到几周。
把排除表放在远程，改一个 JSON 就能修，不用等发版。

**但这个机制本身是安全敏感面，必须先说清楚风险**：

    一张远程排除表可以对安全结果做减法。如果它被篡改（DNS 劫持、镜像投毒、
    账号被盗），攻击者就能让"你的手机号确实挂在某个站上"这条结果静默消失。
    一个只会说"你很安全"的自查工具，比没有工具更危险。

所以这里的每一条约束都不是洁癖：

    1. **只走 HTTPS。** 明文 HTTP 的排除表等于把结果开关交给链路上的任何人。
    2. **本地文件优先。** 远程只能追加排除，不能推翻本地规则——用户可以
       自己写一条本地规则来否决远程。
    3. **每条排除都留痕。** `explain()` 会返回命中的规则和它的来源
       （local / remote / 内置），报告里能查到"这条为什么没算"。
    4. **远程失败绝不阻塞扫描。** 拿不到就用缓存，没有缓存就用空表——
       宁可多报一个误报，也不能因为网络问题让整次自查跑不起来。
    5. **版本号更高就整份忽略。** 未来格式里的字段我们看不懂，
       按老规则解释可能把"不该排除的"排掉，那是不可逆的错。

内置表（`DEFAULT_RULES`）只放**已实测确认**的假阳性，不猜。
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 本实现能理解的最高格式版本。
SUPPORTED_VERSION = 1

# 远程表缓存多久（小时）。太短等于每次扫描都打远程，太长等于修不生效。
DEFAULT_CACHE_TTL_HOURS = 24

# 远程表最多允许多少条排除规则。
#
# 不设上限的话，一个被篡改的表可以把所有站点全排掉，而用户只会看到
# "没有发现暴露"。超过这个数量就整份拒绝，并在日志里明确说明原因。
MAX_REMOTE_RULES = 500

# 内置排除表：只放**实测确认过**的假阳性。
# 目前为空——宁可让用户看到一条可疑结果，也不凭印象预先排除。
DEFAULT_RULES: List[Dict[str, Any]] = []


@dataclass
class ExclusionRule:
    """一条排除规则。

    三种粒度，由细到粗：
        site + username  某个用户名在某站是假阳性（最常见）
        site + url       某个 URL 模式是假阳性
        site             整站不计入命中（规则整体失效时用）
    """

    site: str
    username: Optional[str] = None
    url_pattern: Optional[str] = None
    reason: str = ""
    until: Optional[str] = None       # ISO 日期；过期后规则自动失效
    source: str = "local"             # local / remote / builtin

    def matches(self, site: str, username: Optional[str] = None,
                url: Optional[str] = None) -> bool:
        if self.site.lower() != (site or "").lower():
            return False
        if self.username is not None and (username or "") != self.username:
            return False
        if self.url_pattern is not None:
            pattern = self.url_pattern.lower().rstrip("*")
            if not (url or "").lower().startswith(pattern):
                return False
        return True

    def expired(self, today: Optional[str] = None) -> bool:
        """`until` 已过则规则失效。

        为什么要过期：排除表是"临时兜底"，不是"永久豁免"。没有过期机制，
        一张两年前的表会一直默默吃掉真实命中，而没人记得它为什么存在。
        """
        if not self.until:
            return False
        today = today or time.strftime("%Y-%m-%d")
        return str(self.until) < today

    def describe(self) -> str:
        scope = self.username or self.url_pattern or "整站"
        tail = f"（{self.reason}）" if self.reason else ""
        return f"{self.site}/{scope} ← {self.source}{tail}"


@dataclass
class ExclusionTable:
    """一组排除规则。"""

    rules: List[ExclusionRule] = field(default_factory=list)
    # 远程表加载失败时的说明，报告里可以展示"排除表可能不是最新的"
    warnings: List[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rules)

    def explain(
        self,
        site: str,
        username: Optional[str] = None,
        url: Optional[str] = None,
        today: Optional[str] = None,
    ) -> Optional[ExclusionRule]:
        """返回命中的第一条规则；没命中返回 None。

        返回规则本身而不是 bool，是为了让报告能写清"这条为什么不算"。
        一个说不清理由的排除，用户没法判断该不该信。
        """
        for rule in self.rules:
            if rule.expired(today):
                continue
            if rule.matches(site, username=username, url=url):
                return rule
        return None

    def is_excluded(self, *args, **kwargs) -> bool:
        return self.explain(*args, **kwargs) is not None

    def active_rules(self, today: Optional[str] = None) -> List[ExclusionRule]:
        return [r for r in self.rules if not r.expired(today)]

    def apply(self, results: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """把命中排除表的条目标出来，分成"保留"和"被排除"两组。

        **不删除、只分组**：被排除的条目连同原始字段一起放进 `excluded`，
        并附上 `excluded_by` 说明命中了哪条规则、来自哪张表。
        静默删除会让排除表变成黑箱——而黑箱在安全工具里是不可接受的：
        用户没法判断"我的手机号没被查出来"是真的没暴露，还是被谁排除了。
        """
        excluded: List[Dict[str, Any]] = []
        kept: List[Dict[str, Any]] = []

        for item in results:
            site = item.get("site") or item.get("site_name") or ""
            if isinstance(site, dict):
                site = site.get("name", "")
            username = item.get("username")
            url = item.get("url_user") or item.get("url_probe")
            rule = self.explain(str(site), username=username, url=url)
            if rule is None:
                kept.append(item)
                continue
            marked = dict(item)
            marked["excluded_by"] = rule.describe()
            excluded.append(marked)

        return {"kept": kept, "excluded": excluded}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
class ExclusionFormatError(ValueError):
    """排除表格式不合法。"""


def _parse_rule(raw: Dict[str, Any], source: str) -> ExclusionRule:
    if not isinstance(raw, dict):
        raise ExclusionFormatError(f"规则必须是对象，收到 {type(raw).__name__}")
    site = raw.get("site")
    if not isinstance(site, str) or not site.strip():
        raise ExclusionFormatError("规则缺少 site 字段")
    return ExclusionRule(
        site=site.strip(),
        username=raw.get("username") or None,
        url_pattern=raw.get("url_pattern") or None,
        reason=str(raw.get("reason") or ""),
        until=raw.get("until") or None,
        source=source,
    )


def parse(data: Any, source: str = "local") -> List[ExclusionRule]:
    """把 JSON 内容解析成规则列表。格式不合法抛 ExclusionFormatError。

    支持两种形态，因为贡献者提交的表这两种都见过：
        {"version": 1, "rules": [...]}
        [...]                              （裸数组，按 v1 解释）
    """
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        version = data.get("version", SUPPORTED_VERSION)
        try:
            version = int(version)
        except (TypeError, ValueError):
            raise ExclusionFormatError(f"version 不是整数：{version!r}")
        if version > SUPPORTED_VERSION:
            # 未来版本可能改变了语义（比如把"排除"改成"加权"）。
            # 按老规则硬解释会做出不可逆的错误排除，宁可整份不用。
            raise ExclusionFormatError(
                f"排除表版本 {version} 高于本程序支持的 {SUPPORTED_VERSION}，已忽略"
            )
        items = data.get("rules") or data.get("exclude") or []
    else:
        raise ExclusionFormatError(f"排除表顶层必须是对象或数组，收到 {type(data).__name__}")

    if not isinstance(items, list):
        raise ExclusionFormatError("rules 必须是数组")

    rules: List[ExclusionRule] = []
    for index, raw in enumerate(items):
        try:
            rules.append(_parse_rule(raw, source))
        except ExclusionFormatError as exc:
            # 单条坏规则不该让整张表作废——表是人手写的，打错一个字段很常见。
            logger.warning("排除表第 %d 条规则无效，已跳过：%s", index, exc)
    return rules


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
def load_file(path: Optional[str], source: str = "local") -> List[ExclusionRule]:
    """读本地排除表。文件不存在返回空列表（不是错误）。"""
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning("排除表 %s 读取失败：%s", path, exc)
        return []
    try:
        return parse(data, source=source)
    except ExclusionFormatError as exc:
        logger.warning("排除表 %s 格式不合法，已忽略：%s", path, exc)
        return []


def _cache_path(default: Optional[str]) -> Optional[str]:
    if default:
        return default
    try:
        home = Path.home() / ".maigret"
        home.mkdir(parents=True, exist_ok=True)
        return str(home / "cn_exclusions.json")
    except OSError:
        return None


def _cache_fresh(path: str, ttl_hours: float) -> bool:
    try:
        age = time.time() - Path(path).stat().st_mtime
    except OSError:
        return False
    return age < ttl_hours * 3600


async def load_remote(
    url: Optional[str],
    cache_path: Optional[str] = None,
    ttl_hours: float = DEFAULT_CACHE_TTL_HOURS,
    session: Any = None,
    timeout: float = 10.0,
) -> tuple:
    """拉远程排除表，返回 (规则列表, 警告列表)。

    失败一律降级：先用缓存，没有缓存就返回空表。**绝不抛异常**——
    排除表是可选增强，不该让一次自查因为它的网络问题跑不起来。
    """
    warnings: List[str] = []
    if not url:
        return [], warnings

    scheme = urlparse(url).scheme.lower()
    if scheme != "https":
        # 明文 HTTP 的排除表可以被链路上任何人改写，等于把"结果开关"交出去。
        warnings.append(f"排除表地址不是 HTTPS（{scheme or '空'}），已忽略")
        return [], warnings

    path = _cache_path(cache_path)
    if path and _cache_fresh(path, ttl_hours):
        rules = load_file(path, source="remote")
        if rules:
            return rules, warnings

    import aiohttp

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(url, timeout=timeout) as response:
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            text = await response.text()
        data = json.loads(text)
        rules = parse(data, source="remote")
    except Exception as exc:
        warnings.append(f"远程排除表加载失败（{exc}），沿用本地规则")
        # 回落到缓存（可能已过期，但比没有强）
        cached = load_file(path, source="remote") if path else []
        return cached, warnings
    finally:
        if own_session:
            try:
                await session.close()
            except Exception:
                pass

    if len(rules) > MAX_REMOTE_RULES:
        # 一个被篡改的表可以把所有站点全排掉，用户只会看到"没有发现暴露"。
        warnings.append(
            f"远程排除表有 {len(rules)} 条规则，超过上限 {MAX_REMOTE_RULES}，整份拒绝"
        )
        return [], warnings

    if path:
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.debug("排除表缓存写入失败：%s", exc)

    return rules, warnings


def build(
    local_path: Optional[str] = None,
    remote_rules: Sequence[ExclusionRule] = (),
    remote_warnings: Sequence[str] = (),
    builtin: Optional[Sequence[Dict[str, Any]]] = None,
) -> ExclusionTable:
    """合并本地 + 远程 + 内置。

    **顺序即优先级**：本地 > 远程 > 内置。`explain()` 返回第一条命中的规则。
    把本地放最前，是为了让用户在远程表出问题（投毒、误排）时，
    有一份自己完全掌控的规则可以先命中。
    """
    rules: List[ExclusionRule] = list(load_file(local_path, source="local"))
    rules.extend(remote_rules)

    base = DEFAULT_RULES if builtin is None else builtin
    for raw in base:
        try:
            rules.append(_parse_rule(raw, source="builtin"))
        except ExclusionFormatError:
            continue

    return ExclusionTable(rules=rules, warnings=list(remote_warnings))
