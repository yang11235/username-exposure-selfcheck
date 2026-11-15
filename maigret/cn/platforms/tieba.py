"""百度贴吧：`/home/get/panel?un=` 接口。

## 为什么不用 `/home/main?id=...` 这个"主页"

用户看到的那份带「性别 / 地点」的页面是 `/home/main`，但它对非浏览器流量
一律返回 **403「百度安全验证」**。实测（2026-11-15）12 种组合全部 403：

    2 个 URL × {仅 UA, UA+Referer, 完整头} × {匿名, 带本机浏览器 Cookie}

而且贴吧的登录态 `BDUSS` 属于 `.baidu.com` 域，本机浏览器里只有 `BAIDUID`
和贴吧自己的 `XFI`/`XFT` 指纹 cookie，本来就没有登录态。所以这条路
**不是"再试一次就能通"**，是结构性不可用。

## `panel` 接口实测（2026-11-15）

    GET https://tieba.baidu.com/home/get/panel?ie=utf-8&un=<用户名>

存在 → 200，JSON 形如：

    {"no":0,"error":"成功","data":{
        "name":"maigret","name_show":"maigret","show_nickname":"maigret",
        "portrait":"tb.1.3b8697f0.ZMxDZivGfZJlucc3Khp6CA",
        "sex":"female","tb_age":"19.4","post_num":0,"followed_count":0,...}}

不存在 → 200，`{"no":1130029,"error":"参数错误","data":[]}`
（另一种错误码是 1130025「获取用户信息失败」，同样带 `"data":[]`）

**没有「地点」字段。** 地点只在被 403 的 `/home/main` 上，所以本项目拿不到。
不编造、不拿 IP 属地冒充"所在地"——那是两回事。

## 三个取值的坑

1. `sex` 是 `male` / `female` 这种内部枚举，报告里要翻成中文（`value_map`）。
2. `tb_age`（吧龄）有时是字符串 `"19.4"`、有时是整数 `23`，所以只能当文本取。
3. `portrait` 有时带 `?t=<时间戳>` 后缀（`tb.1.d4d04925.xxx?t=1664738574`），
   拼头像地址时它落在 query 上，不影响取图（实测 200 / image/jpeg）。
"""

from .base import FieldSpec, PlatformExtractor


class TiebaExtractor(PlatformExtractor):
    name = "tieba"
    # 只认 tieba.baidu.com 自己的接口。`home/get/panel` 与 `i/sys/user_json`
    # 都在这个 host 下，两个接口返回的 JSON 结构不同，但字段路径互不冲突
    # （一个在 `data.*`，一个在顶层/`creator.*`），所以共用一份规则也不会错。
    hosts = (r"(?:^|//|\.)tieba\.baidu\.com/",)

    fields = (
        # 贴吧的显示名优先级：name_show（含改过的昵称）> show_nickname > name
        FieldSpec(
            "nickname",
            ("data.name_show", "data.show_nickname", "data.name",
             "creator.show_nickname", "creator.name", "raw_name"),
            confidence=0.9,
        ),
        FieldSpec(
            "gender",
            ("data.sex",),
            confidence=0.9,
            source="接口字段 data.sex",
            value_map={"male": "男", "female": "女"},
        ),
        FieldSpec("followers", ("data.followed_count",), confidence=0.8),
        FieldSpec("account_age", ("data.tb_age",), confidence=0.8),
    )

    avatar_paths = ("data.portrait", "creator.portrait", "creator.itieba_portrait")

    def avatar_url(self, body: str) -> str:
        """`portrait` 是个 ID，不是地址，要拼成百度头像 CDN 的路径。

        实测 `https://himg.bdimg.com/sys/portrait/item/<portrait>` 返回
        200 + image/jpeg。`tb.himg.baidu.com` 那个旧域名证书已经不对了
        （Hostname mismatch），不要用。
        """
        portrait = super().avatar_url(body)
        if not portrait:
            return ""
        return f"https://himg.bdimg.com/sys/portrait/item/{portrait}"


EXTRACTOR = TiebaExtractor()
