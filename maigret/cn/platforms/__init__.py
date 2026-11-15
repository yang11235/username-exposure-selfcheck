"""平台专用抽取器：JSON 接口站点的字段解析。

`extract.py` 的通用抽取是给 HTML 写的，而中文主流平台的探测地址几乎都是
JSON 接口。这一层负责"从 JSON 里按实测路径取字段"，通用层继续负责 HTML。

用法（`extract.py` 内部就是这么调的）：

    from . import platforms
    items = platforms.extract(body, fields, base_url)
    avatar = platforms.avatar_url(body, base_url)

## 为什么用 `detect()` 而不是按站点名分发

站点名是站点库的键，会随上游改名；而"这个接口属于哪个平台"由 host 决定，
稳定得多。同一个平台的不同接口（card / space / search）可以共用一份字段表。

## 已实现 / 未实现

| 平台 | 状态 | 原因 |
|---|---|---|
| B 站 | 已实现 | card 接口匿名可访问，实测有 name/face/sign |
| 网易云 | 已实现 | user/detail 匿名可访问，实测有 nickname/avatarUrl/signature |
| 掘金 | 已实现 | user/get 匿名可访问，实测有 user_name/avatar_large/description |
| 百度贴吧 | 已实现 | `home/get/panel` 匿名可访问，实测有 name_show/sex/tb_age/followed_count |
| 微博 | 未实现 | `ajax/profile/info` 未登录返回 403 `{"error":"Forbidden"}`，拿不到响应就没法核对路径 |
| 知乎 | 未实现 | `api/v4/members/*` 未登录返回 403 `need_login`，同上 |

未实现的平台不是"忘了写"，是**没有实测样本就不写路径**——写错了不会报错，
只会静默抽空，比不支持更糟。等有登录态样本时再补。
"""

from typing import Iterable, List, Optional

from ..extract import Evidence
from . import bilibili, juejin, netease, tieba
from .base import FieldSpec, PlatformExtractor, resolve

# 顺序有意义：host 正则先匹配到的胜出。目前四个平台的 host 互不重叠。
EXTRACTORS = (
    bilibili.EXTRACTOR,
    netease.EXTRACTOR,
    juejin.EXTRACTOR,
    tieba.EXTRACTOR,
)

__all__ = [
    "EXTRACTORS",
    "FieldSpec",
    "PlatformExtractor",
    "avatar_url",
    "detect",
    "extract",
    "resolve",
]


def detect(base_url: str, body: str) -> Optional[PlatformExtractor]:
    """认出这条响应属于哪个平台；认不出返回 None。

    两个条件都要满足：host 匹配 **且** 正文能解析成 JSON 对象。
    只看 host 不够——同一个域名下也可能返回 HTML 错误页，那种情况该交给
    通用 HTML 抽取，而不是让平台抽取器返回空。
    """
    for extractor in EXTRACTORS:
        if extractor.matches(base_url) and extractor.payload(body) is not None:
            return extractor
    return None


def extract(body: str, wanted: Iterable[str], base_url: str = "") -> List[Evidence]:
    extractor = detect(base_url, body)
    if extractor is None:
        return []
    return extractor.extract(body, wanted)


def avatar_url(body: str, base_url: str = "") -> str:
    extractor = detect(base_url, body)
    if extractor is None:
        return ""
    return extractor.avatar_url(body)
