"""B 站：`/x/web-interface/card` 接口。

实测（2026-11-15，`?mid=2`）响应结构：

    {"code":0,"data":{"card":{"mid":"2","name":"碧诗","sex":"男",
      "face":"https://i2.hdslb.com/bfs/face/....jpg",
      "sign":"https://kami.im 直男过气网红 # We Are Star Dust",
      "fans":1438228,"friend":430,"level_info":{"current_level":6,...}}}}

注意 `name` 是昵称，不是真名——B 站的 `name` 就是用户自己填的昵称。
`sign` 是签名（个人简介），不是"登录签名"。
"""

from .base import FieldSpec, PlatformExtractor


class BilibiliExtractor(PlatformExtractor):
    name = "bilibili"
    hosts = (r"(?:^|//|\.)bilibili\.com/",)
    fields = (
        FieldSpec("nickname", ("data.card.name",), confidence=0.9),
        FieldSpec("bio", ("data.card.sign",), confidence=0.85),
    )
    avatar_paths = ("data.card.face",)


EXTRACTOR = BilibiliExtractor()
