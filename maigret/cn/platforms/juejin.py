"""掘金：`/user_api/v1/user/get?user_id=` 接口。

实测（2026-11-15，`user_id=2137106337240712`）：

    {"err_no":0,"data":{"user_id":"2137106337240712",
      "user_name":"被禁言文章转简书了","company":"","job_title":"软件工程师",
      "avatar_large":"https://p1-jj.byteimg.com/....image",
      "description":"多年从事一线互联网开发的学习历程技术汇总",...}}

`company` 与 `job_title` 是两件事：前者是公司名，后者是职位。画像里的
"公司"只取 `company`——把"软件工程师"写进公司栏会让用户以为自己填错了。
"""

from .base import FieldSpec, PlatformExtractor


class JuejinExtractor(PlatformExtractor):
    name = "juejin"
    hosts = (r"(?:^|//|\.)juejin\.(?:cn|com)/",)
    fields = (
        FieldSpec("nickname", ("data.user_name",), confidence=0.9),
        FieldSpec("bio", ("data.description",), confidence=0.85),
        FieldSpec("company", ("data.company",), confidence=0.85),
    )
    avatar_paths = ("data.avatar_large", "data.avatar_image")


EXTRACTOR = JuejinExtractor()
