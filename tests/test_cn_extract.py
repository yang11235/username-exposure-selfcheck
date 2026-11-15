"""证据抽取测试（不联网）。

这一层最坏的失败不是漏抽，而是**抽出一个错的**——用户会照着错的信息去改密码、
去申诉。所以测试重点不在"能抽到"，而在"不该抽的绝不抽"：

    - JS 打包代码里的示例邮箱/手机号不能被当成证据
    - `logo@2x.png` 这种静态资源名不能被当成邮箱
    - 昵称不能被当成实名（风险等级 1 → 3 是夸大）
    - 没声明要抽的字段一个都不抽
"""

import pytest

from maigret.cn.extract import (
    Evidence,
    LABELS,
    RISK,
    extract_avatar_url,
    extract_email,
    extract_evidence,
    extract_from_jsonld,
    extract_ip_location,
    extract_nickname,
    extract_phone,
    mask,
    strip_account_suffix,
    summarize,
    visible_text,
)


def html(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


# ---------------------------------------------------------------------------
# 正文提取
# ---------------------------------------------------------------------------
def test_visible_text_drops_scripts_and_styles():
    """打包 JS 里什么字符串都有，必须排除。"""
    page = html(
        "<p>真实内容</p><script>var demo='a@b.com'; var tpl='13800138000';</script>"
        "<style>.x{content:'c@d.com'}</style>"
    )
    text = visible_text(page)

    assert "真实内容" in text
    assert "a@b.com" not in text
    assert "13800138000" not in text


def test_email_in_bundled_js_is_not_extracted():
    """守住上一条的**后果**：JS 里的示例邮箱不能进证据。

    这是最容易出的假阳性：随便一个 React 打包产物里都有
    `placeholder="user@example.com"`。
    """
    page = html(
        "<p>zhangwei 联系我：zhangwei@qq.com</p>"
        "<script>var placeholder='user@example.com'; var logo='logo@2x.png';</script>"
    )
    found = [e.value for e in extract_email(visible_text(page), anchor="zhangwei")]

    assert found == ["zhangwei@qq.com"]


def test_footer_contact_email_is_not_attributed_to_the_user():
    """站点页脚的客服邮箱不能算成用户的联系方式。

    实测 Gitee 个人主页（wizzer）全文唯一的邮箱是 `client@oschina.cn`——
    Gitee 自己的客服邮箱。按"页面上有邮箱"来抽，会把每个用户的报告都写成
    "邮箱：客服邮箱"。这条比漏抽坏得多：用户会以为自己暴露了联系方式。

    判据是距离：邮箱必须出现在用户名附近。
    """
    filler = "开源项目 贡献者 关注 粉丝 加入于 2020 年 最近活跃 " * 12
    footer = f"{filler}联系我们：client@oschina.cn 京ICP备 12345 号"
    page = html(f"<p>wizzer 的个人主页</p><div>{footer}</div>")

    assert extract_email(visible_text(page), anchor="wizzer") == []


def test_email_without_anchor_is_never_extracted():
    """没有"这是谁的页面"这个前提，任何邮箱都不能归到这个用户名头上。"""
    assert extract_email("联系 zhangwei@qq.com") == []


def test_static_asset_names_are_not_emails():
    """`logo@2x.png` 长得像邮箱，但它是文件名。"""
    assert extract_email("zhangwei logo@2x.png sprite@3x.webp", anchor="zhangwei") == []


def test_example_domain_is_rejected():
    """示例域名是文档占位符，不是真实联系方式。"""
    assert extract_email("zhangwei user@example.com", anchor="zhangwei") == []


# ---------------------------------------------------------------------------
# IP 属地
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("IP属地：北京", "北京"),
        ("IP属地:广东", "广东"),
        ("发布于 2024-01-01 IP属地：浙江", "浙江"),
    ],
)
def test_ip_location_variants(text, expected):
    """微博/小红书/知乎的写法略有差异，中英文冒号都要认。"""
    found = extract_ip_location(text)

    assert found and found[0].value == expected
    assert found[0].risk == 2


def test_ip_location_absent_returns_nothing():
    assert extract_ip_location("这个人什么都没写") == []


# ---------------------------------------------------------------------------
# 手机号
# ---------------------------------------------------------------------------
def test_phone_only_matches_china_mobile_format():
    assert [e.value for e in extract_phone("电话 13812345678")] == ["13812345678"]
    # 12 位、以 2 开头、被更长数字包住 —— 都不是手机号
    assert extract_phone("订单号 1381234567890") == []
    assert extract_phone("编号 23812345678") == []


def test_phone_is_not_reverse_looked_up():
    """本项目只抽平台上**明写**的号码，不做反查（文档 §1.2⑤）。

    这条用"没有任何输入就没有输出"来表达：函数签名里根本不存在
    "根据用户名查手机号"的路径。
    """
    assert extract_phone("用户名 zhangwei") == []


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------
def test_jsonld_person_yields_company_and_school():
    page = html(
        "",
        '<script type="application/ld+json">'
        '{"@type":"Person","name":"张伟","worksFor":{"@type":"Organization","name":"某科技"},'
        '"alumniOf":{"@type":"CollegeOrUniversity","name":"浙江大学"}}'
        "</script>",
    )

    items = extract_from_jsonld(page)
    fields = {e.field: e.value for e in items}

    assert fields["company"] == "某科技"
    assert fields["school"] == "浙江大学"
    # schema.org 的 name 是展示名 → 按昵称处理，不是实名
    assert fields["nickname"] == "张伟"
    assert "real_name" not in fields


def test_jsonld_broken_json_is_ignored():
    """页面里的 JSON-LD 经常是坏的（模板拼错），不能因此崩掉整次抽取。"""
    page = html("", '<script type="application/ld+json">{not json</script>')

    assert extract_from_jsonld(page) == []


def test_real_name_is_never_inferred():
    """昵称不能当实名：风险等级从 1 跳到 3，属于夸大用户暴露程度。"""
    page = html("", '<script type="application/ld+json">{"@type":"Person","name":"张伟"}</script>')

    found = extract_evidence(page, ["real_name"])

    assert found == []


# ---------------------------------------------------------------------------
# 头像
# ---------------------------------------------------------------------------
def test_avatar_prefers_avatar_class_over_og_image():
    """头像必须优先取 avatar 类名的 img，og:image 排在后面。

    实测 Gitee：页面的 og:image 是 `static/images/logo_themecolor_circle.png`
    （站点 logo），真正的头像在 `<img class="ui image avatar">` 里。
    反过来的话，全站用户的头像哈希都等于站点 logo 的哈希，身份聚类会把
    所有人合成一个人——比抽不到严重得多。
    """
    page = html(
        '<img class="ui image avatar" src="https://foruda.example.com/avatar/102150_wizzer">',
        '<meta property="og:image" content="https://gitee.com/static/images/logo_themecolor_circle.png">',
    )
    assert extract_avatar_url(page) == "https://foruda.example.com/avatar/102150_wizzer"


def test_site_logo_is_never_treated_as_avatar():
    """只剩 og:image 且它是 logo 时，宁可不抽。"""
    page = html("", '<meta property="og:image" content="https://x.com/static/images/logo.png">')

    assert extract_avatar_url(page) == ""


def test_avatar_falls_back_to_og_image_when_it_is_a_real_photo():
    """og:image 不是站点资源时可以作为头像兜底。"""
    page = html(
        "",
        '<meta property="og:image" content="https://cdn.example.com/users/42.jpg">',
    )
    assert extract_avatar_url(page) == "https://cdn.example.com/users/42.jpg"


def test_avatar_class_img_with_logo_is_skipped():
    """类名对但地址是 logo 的，也要跳过——两个判据都要过。"""
    page = html(
        '<img class="avatar" src="/static/images/logo.png">'
        '<img class="avatar" src="/u/7/avatar.jpg">'
    )
    assert extract_avatar_url(page, "https://site.com/x") == "https://site.com/u/7/avatar.jpg"


def test_default_avatar_urls_are_skipped():
    """`default_avatar` / `noavatar` 这类占位图不是这个人的头像。"""
    page = html('<img class="avatar" src="/img/default_avatar.png">')

    assert extract_avatar_url(page) == ""


def test_avatar_falls_back_to_avatar_img():
    page = html('<img class="user-avatar" data-src="/u/1/avatar.jpg">')
    assert extract_avatar_url(page, "https://site.com/x") == "https://site.com/u/1/avatar.jpg"


def test_avatar_missing_returns_empty():
    assert extract_avatar_url(html("<p>no image</p>")) == ""


# ---------------------------------------------------------------------------
# 抽取入口
# ---------------------------------------------------------------------------
def test_no_declared_fields_means_no_extraction():
    """站点没声明要抽什么，就一个字段都不抽。

    不是"默认抽全部"：抽取会带来额外请求和额外解读，只有明确声明时才值得做。
    """
    page = html("<p>IP属地：北京 zhangwei@qq.com 13812345678</p>")

    assert extract_evidence(page, []) == []
    assert extract_evidence(page, None) == []


def test_unknown_field_is_skipped_silently():
    """站点数据里写了不认识的字段名，跳过即可，不该让整次扫描失败。"""
    page = html("<p>IP属地：北京</p>")

    found = extract_evidence(page, ["ip_location", "totally_unknown"])

    assert [e.field for e in found] == ["ip_location"]


def test_duplicates_are_collapsed():
    """同一个值在页面里出现多次，只留一条。"""
    page = html("<p>IP属地：北京</p><p>IP属地：北京</p>")

    found = extract_evidence(page, ["ip_location"])

    assert len(found) == 1


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
def test_mask_email_keeps_domain():
    assert mask("zhangwei@qq.com", "email") == "zh***@qq.com"


def test_mask_phone_keeps_head_and_tail():
    assert mask("13812345678", "phone") == "138****5678"


def test_mask_real_name_keeps_surname():
    assert mask("张伟", "real_name") == "张*"
    assert mask("欧阳修远", "real_name") == "欧***"


def test_mask_is_idempotent_for_non_sensitive_fields():
    """昵称和头像哈希本身不是敏感标识，遮了反而看不懂。"""
    assert mask("zhangwei", "nickname") == "zhangwei"
    assert mask("abc123def", "avatar_hash") == "abc123def"


def test_evidence_dict_respects_mask_flag():
    item = Evidence("email", "zhangwei@qq.com", "正文邮箱")

    assert item.to_dict()["value"] == "zhangwei@qq.com"
    assert item.to_dict(masked=True)["value"] == "zh***@qq.com"


def test_summarize_puts_high_risk_first():
    """报告里高危字段要排前面——用户最该先处理的是实名和手机号。

    同一风险等级内部按置信度、再按字段名排序，不额外规定顺序：
    手机号和邮箱都是风险 3，谁先谁后不影响用户判断。
    """
    items = [
        Evidence("nickname", "zw", "og:title"),
        Evidence("phone", "13812345678", "正文"),
        Evidence("email", "a@b.com", "正文"),
    ]

    ordered = summarize(items)

    assert {ordered[0]["field"], ordered[1]["field"]} == {"phone", "email"}
    assert ordered[-1]["field"] == "nickname"
    assert [item["risk"] for item in ordered] == [3, 3, 1]


def test_risk_table_covers_every_documented_field():
    """文档 §5.6 的字段表要完整，缺一个就会默认成风险 2，属于静默错。"""
    for field_name in ("ip_location", "real_name", "email", "phone",
                       "company", "school", "avatar_hash"):
        assert field_name in RISK, field_name
        assert field_name in LABELS, field_name


# ---------------------------------------------------------------------------
# 昵称尾部的用户名后缀
#
# 实测 Gitee：og:title 是 `Wizzer (wizzer) - Gitee.com`，去掉站点后缀后剩
# `Wizzer (wizzer)`，报告里就写成"昵称「Wizzer (wizzer)」"——括号里那截
# 是我们自己的查询词，不是用户填的昵称。
# ---------------------------------------------------------------------------
def test_account_suffix_is_stripped_from_the_nickname():
    assert strip_account_suffix("Wizzer (wizzer)", "wizzer") == "Wizzer"


def test_account_suffix_matching_is_case_insensitive():
    assert strip_account_suffix("Wizzer (WizZer)", "wizzer") == "Wizzer"


def test_full_width_parentheses_are_also_stripped():
    assert strip_account_suffix("老王（laowang）", "laowang") == "老王"


def test_parentheses_that_are_part_of_the_nickname_are_kept():
    """「老王（摄影师）」里的括号是昵称的一部分。

    宁可留着冗余，也不能改用户的自我描述——所以只在括号内容等于
    anchor 时才删，不按"括号里像 ID 就删"的规则。
    """
    assert strip_account_suffix("老王（摄影师）", "laowang") == "老王（摄影师）"
    assert strip_account_suffix("阿杰 (上海)", "ajie") == "阿杰 (上海)"


def test_suffix_stripping_is_skipped_without_an_anchor():
    assert strip_account_suffix("Wizzer (wizzer)") == "Wizzer (wizzer)"


def test_extract_nickname_strips_the_account_suffix():
    html = '<meta property="og:title" content="Wizzer (wizzer) - Gitee.com">'

    found = extract_nickname(html, site_name="Gitee", anchor="wizzer")

    assert found[0].value == "Wizzer"


def test_extract_nickname_keeps_the_name_when_it_is_only_the_account():
    """昵称整体就是用户名时，剥完是空串——此时保留原值而不是输出空证据。"""
    html = '<meta property="og:title" content="wizzer (wizzer)">'

    found = extract_nickname(html, anchor="wizzer")

    assert found[0].value == "wizzer"
