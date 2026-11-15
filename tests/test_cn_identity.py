"""身份聚类测试（不联网、不需要真实头像）。

这层最容易出的错都是**静默**的：不报错，只是把两个陌生人聚成一个人，
或者把同一个人的两个账号拆开。所以用合成图像和合成文本精确构造场景。

重点守三件事：

    1. pHash 对"同一张图的不同尺寸/轻微变化"稳定，对不同图不敏感
    2. 平台默认头像不能把所有人连成一簇（这是 pHash 聚类最典型的假阳性）
    3. 阈值语义：只有头像及以上强度的证据才能单独连人，姓名/地区/同名不行
"""

import pytest

from maigret.cn import identity as ident
from maigret.cn.identity import (
    Account,
    EDGE_AVATAR,
    EDGE_CROSS_LINK,
    EDGE_NAME,
    EDGE_USERNAME,
    cluster,
    hamming,
    normalize_url,
    phash,
    placeholder_hashes,
    render,
    simhash,
)

HAS_PIL = ident.pillow_available()
needs_pil = pytest.mark.skipif(not HAS_PIL, reason="Pillow 未安装，pHash 不可用")


def _image_bytes(kind: str, size=(128, 128), fmt="PNG", brightness=0, mirror=False):
    """生成可复现的合成图。

    不用随机噪声：噪声图在缩放后高频全变，pHash 本来就该不同，
    测不出"稳定性"这个真正要保证的性质。

    注意：先在固定的 128×128 画布上画，再缩到目标尺寸。如果直接在小画布上
    按绝对坐标画，内容本身就被裁掉了——那测的是"两张不同的图"，不是"同一张图
    的不同分辨率"。
    """
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageEnhance

    canvas = 128
    img = Image.new("RGB", (canvas, canvas), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    if kind == "face_a":
        draw.ellipse([20, 20, 100, 100], fill=(40, 90, 200))
        draw.rectangle([50, 50, 70, 70], fill=(255, 240, 0))
    elif kind == "face_b":
        # 整体明暗分布完全不同：左边亮右边暗
        draw.rectangle([0, 0, 64, 128], fill=(0, 0, 0))
    elif kind == "face_c":
        # 上亮下暗，与 face_b 的低频分布不同
        draw.rectangle([0, 0, 128, 64], fill=(0, 0, 0))
    else:
        raise AssertionError(kind)

    if brightness:
        img = ImageEnhance.Brightness(img).enhance(1.0 + brightness / 255.0)
    if mirror:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if size != (canvas, canvas):
        img = img.resize(size, Image.LANCZOS)

    buf = BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# pHash
# ---------------------------------------------------------------------------
@needs_pil
def test_phash_is_stable_across_resize():
    """同一张图换个尺寸，哈希必须几乎不变——否则各站头像尺寸不同就全废了。"""
    original = phash(_image_bytes("face_a", size=(128, 128)))
    resized = phash(_image_bytes("face_a", size=(64, 64)))

    assert original is not None and resized is not None
    assert hamming(original, resized) <= 2


@needs_pil
def test_phash_separates_different_images():
    """不同图必须拉开距离，否则"头像匹配"毫无意义。"""
    a = phash(_image_bytes("face_a"))
    b = phash(_image_bytes("face_b"))

    assert hamming(a, b) > ident.AVATAR_MAX_DISTANCE


@needs_pil
def test_phash_survives_jpeg_recompression():
    """真实头像多是 JPEG，压缩噪声不该改变判定。"""
    png = phash(_image_bytes("face_a", fmt="PNG"))
    jpg = phash(_image_bytes("face_a", fmt="JPEG"))

    assert hamming(png, jpg) <= ident.AVATAR_MAX_DISTANCE


def test_phash_returns_none_for_garbage():
    """下载到的可能是 HTML 错误页或截断的图。必须返回 None，不能编一个哈希。"""
    assert phash(b"<html>403 Forbidden</html>") is None
    assert phash(b"") is None


@needs_pil
def test_phash_is_brightness_invariant():
    """整体调亮/调暗不该改变哈希。

    pHash 用"与中位数比较"来取位，整体亮度只影响直流分量，所以天然不敏感。
    这条性质很实用：不同站点会把同一张头像做不同的自动调色。
    """
    base = phash(_image_bytes("face_a"))
    brighter = phash(_image_bytes("face_a", brightness=40))

    assert hamming(base, brighter) <= 4


@needs_pil
def test_phash_separates_mirrored_image():
    """镜像翻转后低频分布明显不同，应该被拉开距离。

    这条是"防止哈希退化"的守卫：如果实现写错（比如只取了第一行系数、
    或者位序全乱），所有图都会算出相近的值，上面的"稳定性"测试反而会全绿。
    """
    normal = phash(_image_bytes("face_a"))
    mirrored = phash(_image_bytes("face_a", mirror=True))

    assert hamming(normal, mirrored) > ident.AVATAR_MAX_DISTANCE


def test_hamming_treats_missing_hash_as_missing_not_as_equal():
    """None 不能被当成"距离 0"。

    这是很容易写错的一处：`if a is None: return 0` 会让两个"没头像"的账号
    算成完全相同的头像，直接把全站没设头像的账号聚成一簇。
    """
    assert hamming(None, 123) is None
    assert hamming(123, None) is None
    assert hamming(None, None) is None
    assert hamming(0b1010, 0b1000) == 1


# ---------------------------------------------------------------------------
# 默认头像
# ---------------------------------------------------------------------------
def test_placeholder_hashes_detects_shared_default_avatar():
    """被大量账号共享的哈希 = 平台默认头像，必须被识别出来。"""
    default = 0xDEADBEEF
    accounts = [Account(f"site{i}", f"user{i}", avatar_hash=default) for i in range(6)]
    accounts.append(Account("solo", "unique", avatar_hash=0x1234))

    placeholders = placeholder_hashes(accounts, min_count=5)

    assert default in placeholders
    assert 0x1234 not in placeholders


def test_default_avatar_does_not_merge_strangers():
    """默认头像不能把毫不相干的人连成一簇。

    这是 pHash 聚类最典型的假阳性：知乎/掘金的默认头像是同一张图，
    不筛掉的话几十个陌生账号会连成一片，还顶着 0.85 的"高置信"。
    """
    default = 0xAAAAAAAAAAAAAAAA
    accounts = [
        Account("知乎", f"user{i}", avatar_hash=default, location="北京")
        for i in range(6)
    ]

    clusters = cluster(accounts, threshold=0.6)

    assert len(clusters) == 6, "默认头像不该把任何人连起来"
    assert all(len(c) == 1 for c in clusters)


# ---------------------------------------------------------------------------
# 阈值语义
# ---------------------------------------------------------------------------
def test_avatar_match_is_enough_to_merge():
    """头像距离 ≤ 6 就足以把人连起来（0.85 ≥ 0.6）。"""
    shared = 0x0F0F0F0F0F0F0F0F
    accounts = [
        Account("GitHub", "zhangwei1990", avatar_hash=shared),
        Account("知乎", "zhang-wei-12", avatar_hash=shared ^ 0b11),  # 距离 2
    ]

    clusters = cluster(accounts, threshold=0.6, auto_placeholders=False)

    assert len(clusters) == 1
    assert len(clusters[0]) == 2
    assert clusters[0].confidence == pytest.approx(0.85)


def test_same_name_and_location_alone_never_merge():
    """"都叫张伟、都在北京"远不足以判定是同一个人。

    姓名 0.5、地区 0.3、同名 0.2 都低于 0.6 的阈值，这是有意的：
    中国重名率极高，按这两条合并会把大量陌生人聚成一簇。
    """
    accounts = [
        Account("豆瓣", "zhangwei", name="张伟", location="北京"),
        Account("微博", "zhangwei", name="张伟", location="北京"),
    ]

    clusters = cluster(accounts, threshold=0.6)

    assert len(clusters) == 2


def test_cross_link_is_strongest_evidence():
    """主页互链是用户自己填的，权重最高。

    真实情况几乎总是单向的：用户在 GitHub 简介里贴了知乎主页，知乎那边不填。
    所以规则必须三个方向都看，只比"两边链接集合的交集"会漏掉最常见的场景。
    """
    accounts = [
        Account(
            "GitHub",
            "zw",
            url="https://github.com/zw",
            links=["https://zhihu.com/people/zhang-wei"],
        ),
        Account("知乎", "zhang-wei", url="https://zhihu.com/people/zhang-wei"),
    ]

    clusters = cluster(accounts, threshold=0.6)

    assert len(clusters) == 1
    assert clusters[0].confidence == pytest.approx(0.90)
    assert clusters[0].edges[0].rule == EDGE_CROSS_LINK


def test_cluster_confidence_is_min_edge_not_average():
    """簇置信度取最小边权重，不是平均（文档 §5.5）。

    A—B 是铁证（互链 0.90），B—C 只是头像（0.85）：整簇可信度是 0.85。
    如果按平均算会得到 0.875，把"最弱那条连接"的风险藏起来。
    """
    accounts = [
        Account(
            "GitHub",
            "zw",
            url="https://github.com/zw",
            links=["https://zhihu.com/people/zw"],
        ),
        Account(
            "知乎",
            "zw",
            url="https://zhihu.com/people/zw",
            avatar_hash=0x00FF00FF00FF00FF,
        ),
        Account("掘金", "1556564", avatar_hash=0x00FF00FF00FF00FF),
    ]

    clusters = cluster(accounts, threshold=0.6, auto_placeholders=False)

    assert len(clusters) == 1
    assert len(clusters[0]) == 3
    assert clusters[0].confidence == pytest.approx(0.85)


def test_transitive_chain_merges_all_members():
    """A—B、B—C 有边，A—C 没有边，三者仍应同簇（并查集的传递性）。"""
    accounts = [
        Account("GitHub", "a", avatar_hash=0xFFFF0000FFFF0000),
        Account("知乎", "b", avatar_hash=0xFFFF0000FFFF0000),
        Account("掘金", "c", avatar_hash=0xFFFF0000FFFF0000),
    ]

    clusters = cluster(accounts, threshold=0.6, auto_placeholders=False)

    assert len(clusters) == 1
    assert len(clusters[0]) == 3


# ---------------------------------------------------------------------------
# 文本信号
# ---------------------------------------------------------------------------
def test_simhash_is_close_for_similar_bios():
    a = simhash("热爱开源，前端工程师，现居杭州")
    b = simhash("热爱开源，前端工程师，现居杭州。")

    assert a is not None and b is not None
    assert hamming(a, b) <= ident.BIO_MAX_DISTANCE


def test_simhash_is_far_for_different_bios():
    a = simhash("热爱开源，前端工程师，现居杭州")
    b = simhash("摄影爱好者，周末爬山，养了两只猫")

    assert hamming(a, b) > ident.BIO_MAX_DISTANCE


def test_simhash_ignores_too_short_text():
    """太短的简介算出来的哈希没有区分度，必须当成"信号不可用"。"""
    assert simhash("这个人") is None
    assert simhash("") is None
    assert simhash("暂无") is None


def test_simhash_ignores_platform_default_bio():
    """平台预填的默认简介几百万人在用，不能当身份证据。

    这和默认头像是同一类陷阱：不排掉的话，"简介相似"会把所有懒得填简介的
    账号连成一簇。
    """
    assert simhash("这个人很懒，什么都没留下") is None
    assert simhash("还没有填写简介") is None
    # 用户改过的版本有区分度，要照常计算
    assert simhash("这个人很懒，但还是关注了三个话题") is not None


def test_simhash_is_stable_across_processes():
    """必须是稳定哈希：内置 hash() 有随机种子，跨进程会变。

    聚类结果要能复现、缓存的哈希要能跨进程比较，所以用 FNV-1a 自己算。
    """
    assert simhash("热爱开源的前端工程师") == simhash("热爱开源的前端工程师")
    assert ident._stable_hash("abc") == 0xE71FA2190541574B & 0xFFFFFFFFFFFFFFFF


def test_normalize_url_collapses_equivalent_forms():
    """同一个人的主页有很多种写法，必须收敛成一个。"""
    assert normalize_url("https://www.GitHub.com/zw/") == "github.com/zw"
    assert normalize_url("http://github.com/zw?tab=repos") == "github.com/zw"
    assert normalize_url("https://github.com/zw#readme") == "github.com/zw"
    assert normalize_url("") == ""


def test_single_character_name_never_matches():
    """单字姓名（"王"）会和所有人撞，必须排除。"""
    accounts = [
        Account("豆瓣", "a", name="王"),
        Account("微博", "b", name="王"),
    ]
    assert cluster(accounts, threshold=0.5) != []  # 至少能跑
    clusters = cluster(accounts, threshold=0.5)
    assert all(len(c) == 1 for c in clusters)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def test_render_shows_reason_per_member():
    shared = 0x0F0F0F0F0F0F0F0F
    accounts = [
        Account("GitHub", "zhangwei1990", avatar_hash=shared),
        Account("知乎", "zhang-wei-12", avatar_hash=shared),
    ]

    text = render(cluster(accounts, auto_placeholders=False))

    assert "身份簇 #1" in text
    assert "0.85" in text
    assert "头像匹配" in text
    assert "zhangwei1990" in text


def test_render_hides_singletons_by_default():
    """单账号簇没有"归并"的含义，默认不列出来。"""
    accounts = [Account("豆瓣", "lonely")]

    assert "未发现可归并的身份簇" in render(cluster(accounts))
    assert "lonely" in render(cluster(accounts), min_size=1)
