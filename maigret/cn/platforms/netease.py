"""网易云音乐：`/api/v1/user/detail/{uid}` 接口。

实测（2026-11-15，`/api/v1/user/detail/3308314852`）：字段都在 `profile` 下，

    {"profile":{"userId":3308314852,"nickname":"WizZer","signature":"",
      "description":"","avatarUrl":"http://p1.music.126.net/....jpg",
      "province":230000,"city":230100,...}}

`province` / `city` 是数字行政区划码，本项目**不映射**：一份手抄的码表
半年就会过期，而"IP 属地：黑龙江"这种半对的结论比没有更糟。要属地就靠
正文里的「IP 属地：xx」（`extract_ip_location`）。
"""

from .base import FieldSpec, PlatformExtractor


class NetEaseExtractor(PlatformExtractor):
    name = "netease"
    hosts = (r"(?:^|//|\.)music\.163\.com/",)
    fields = (
        FieldSpec("nickname", ("profile.nickname",), confidence=0.9),
        # signature 是签名，description 是"个人介绍"，两者都可能为空
        FieldSpec("bio", ("profile.signature", "profile.description"), confidence=0.85),
    )
    avatar_paths = ("profile.avatarUrl",)


EXTRACTOR = NetEaseExtractor()
