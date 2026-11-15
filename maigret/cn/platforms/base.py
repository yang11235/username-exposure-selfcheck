"""平台抽取器基类：从**接口返回的 JSON** 里取字段。

## 为什么需要这一层

`extract.py` 的通用抽取器是按 HTML 写的：昵称来自 `og:title`，头像来自
`<meta property="og:image">`，属地来自正文里的「IP 属地：xx」。而中文主流
平台（B 站、网易云、掘金、微博、知乎）的主页探测地址都是 **JSON 接口**——
一行 meta 标签都没有。

结果就是：这些站点命中得很准，抽到的字段却永远是空。用户看到的是
「Bilibili：已注册」然后画像里一片空白，而页面上明明写着昵称、签名、粉丝数。
这不是"没抽到"，是"抽错了地方"。

## 三条约束

1. **路径必须来自实测。** 每条 `FieldSpec.paths` 都是对真实响应逐字核对的，
   没有"照着文档猜"的字段。猜错不会报错，只会静默返回空——那和没写一样，
   但会让人以为已经支持了。

2. **只解析 JSON 对象。** 数组、纯文本、错误页一律不认（`payload()` 返回
   None）。判定层已经决定了这条结果是不是"存在"，抽取层不该在这里补救。

3. **一个字段取到就停。** `paths` 是候选顺序，不是"全部都要"：同一个语义
   在不同版本接口里可能有两个键，取第一个非空值即可，重复值只会污染画像。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..extract import Evidence


@dataclass(frozen=True)
class FieldSpec:
    """一个字段的取法。

    `paths` 是 JSON 路径候选，按顺序取第一个非空值。路径语法：
    `a.b.c`、`a.b[0].c`，与常见的 JSONPath 子集一致，够用且不用引入依赖。
    """

    name: str
    paths: Tuple[str, ...]
    confidence: float = 0.9
    # 报告里显示的来源说明。留空则自动写成"接口字段 a.b.c"。
    source: str = ""
    # 接口原始值 → 报告里给人看的写法，如 {"male": "男"}。
    #
    # 为什么要在这一层做而不是留给报告层：接口返回的枚举值是平台内部编码，
    # 报告层拿到的只是一段字符串，无从知道 "male" 该翻成什么。放在这里，
    # 翻译规则和取值路径写在一起，改接口时一起改，不会漂。
    # 不在表里的值原样保留 —— 宁可直接显示 "unknown"，也不要吞掉。
    value_map: Mapping[str, str] = field(default_factory=dict)


_PATH_PART = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve(data: Any, path: str) -> Any:
    """按 `a.b[0].c` 取值，任何一段取不到就返回 None（不抛异常）。"""
    current = data
    for name, index in _PATH_PART.findall(path):
        if index:
            if not isinstance(current, (list, tuple)):
                return None
            position = int(index)
            if position >= len(current):
                return None
            current = current[position]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(name)
        if current is None:
            return None
    return current


class PlatformExtractor:
    """一个平台的抽取规则。子类只需填 `name` / `hosts` / `fields`。"""

    name = ""
    # 用来判断"这次响应是不是这个平台"的正则，匹配的是探测地址（base_url）。
    # 用 host 而不是完整 URL：同一个平台的接口路径经常变，host 很少变。
    hosts: Tuple[str, ...] = ()
    fields: Sequence[FieldSpec] = ()
    avatar_paths: Tuple[str, ...] = ()

    def matches(self, base_url: str) -> bool:
        url = base_url or ""
        return any(re.search(pattern, url, re.IGNORECASE) for pattern in self.hosts)

    def payload(self, body: str) -> Optional[Dict[str, Any]]:
        """把正文解析成 JSON 对象；不是 JSON 对象就返回 None。"""
        if not body or not body.lstrip().startswith("{"):
            return None
        try:
            data = json.loads(body)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _first(self, data: Dict[str, Any], paths: Iterable[str]) -> str:
        for path in paths:
            value = resolve(data, path)
            if value is None or isinstance(value, (dict, list)):
                continue
            text = str(value).strip()
            if text and text not in ("0", "false", "False"):
                return text
        return ""

    def extract(self, body: str, wanted: Iterable[str]) -> List[Evidence]:
        data = self.payload(body)
        if data is None:
            return []
        wanted_set = set(wanted or ())
        results: List[Evidence] = []
        for spec in self.fields:
            if spec.name not in wanted_set:
                continue
            value = self._first(data, spec.paths)
            if not value:
                continue
            if spec.value_map:
                value = spec.value_map.get(value, value)
            results.append(
                Evidence(
                    spec.name,
                    value,
                    source=spec.source or f"接口字段 {spec.paths[0]}",
                    confidence=spec.confidence,
                )
            )
        return results

    def avatar_url(self, body: str) -> str:
        data = self.payload(body)
        if data is None:
            return ""
        return self._first(data, self.avatar_paths)
