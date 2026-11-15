"""头像抓取测试（不联网）。

这一层要挡的东西很具体，每一条都是真实会遇到的：

    - CDN 返回 HTML 的 403 页（防盗链）——Content-Type 不是 image/*
    - 伪装成图片的几十 MB 数据流——体积上限
    - 防盗链地址无限流数据——边下边计数
    - 网络超时/DNS 失败——不能把整次扫描带崩

以及一条产品约束：**只存哈希，不存图片**。图片字节在函数返回后就没有
任何引用，不会进结果、不会进日志。
"""

import asyncio
import os

import pytest

from maigret.cn import identity as ident
from maigret.cn.executors import avatar as avatar_mod
from maigret.cn.executors.avatar import (
    attach_avatar_evidence,
    avatar_hash,
    fetch_avatar_bytes,
)


def run(coro):
    return asyncio.run(coro)


class FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=None):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """最小 aiohttp.ClientSession 替身：只实现 get()。"""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requested = []
        self.closed = False

    def get(self, url, timeout=None, allow_redirects=True):
        self.requested.append(url)
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _enable_avatar(monkeypatch):
    """默认启用；需要测"关掉"的用例自己 setenv。"""
    monkeypatch.delenv(avatar_mod.ENV_DISABLE, raising=False)


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------
def test_downloads_image_bytes():
    session = FakeSession(
        FakeResponse(headers={"Content-Type": "image/png"}, chunks=[b"abc", b"def"])
    )

    data = run(fetch_avatar_bytes("https://cdn.example.com/a.png", session=session))

    assert data == b"abcdef"
    assert session.requested == ["https://cdn.example.com/a.png"]


def test_html_error_page_is_rejected():
    """防盗链最常见的表现：返回 200，但 Content-Type 是 text/html。"""
    session = FakeSession(
        FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, chunks=[b"<html>403</html>"])
    )

    assert run(fetch_avatar_bytes("https://cdn.example.com/a.png", session=session)) is None


def test_oversized_body_is_aborted():
    """边下边计数：超过上限立刻放弃，不能先攒完再判断。"""
    session = FakeSession(
        FakeResponse(
            headers={"Content-Type": "image/jpeg"},
            chunks=[b"x" * 1024] * 10,
        )
    )

    data = run(
        fetch_avatar_bytes("https://cdn.example.com/a.jpg", max_bytes=4096, session=session)
    )

    assert data is None


def test_non_200_is_rejected():
    session = FakeSession(FakeResponse(status=404, headers={"Content-Type": "image/png"}))

    assert run(fetch_avatar_bytes("https://cdn.example.com/a.png", session=session)) is None


def test_network_error_returns_none_instead_of_raising():
    """一个 CDN 超时不该让整次扫描中断。"""
    session = FakeSession(error=asyncio.TimeoutError())

    assert run(fetch_avatar_bytes("https://cdn.example.com/a.png", session=session)) is None


@pytest.mark.parametrize(
    "url",
    ["", "file:///etc/passwd", "data:image/png;base64,AAAA", "ftp://x/a.png"],
)
def test_non_http_urls_are_never_fetched(url):
    session = FakeSession(FakeResponse(headers={"Content-Type": "image/png"}))

    assert run(fetch_avatar_bytes(url, session=session)) is None
    assert session.requested == []


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv(avatar_mod.ENV_DISABLE, "0")
    session = FakeSession(FakeResponse(headers={"Content-Type": "image/png"}, chunks=[b"x"]))

    assert run(fetch_avatar_bytes("https://cdn.example.com/a.png", session=session)) is None
    assert session.requested == [], "关掉之后不该发出任何请求"


# ---------------------------------------------------------------------------
# 哈希
# ---------------------------------------------------------------------------
HAS_PIL = ident.pillow_available()


@pytest.mark.skipif(not HAS_PIL, reason="Pillow 未安装，pHash 不可用")
def test_avatar_hash_returns_phash(monkeypatch):
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (64, 64), (10, 20, 30))
    buf = BytesIO()
    img.save(buf, format="PNG")

    async def fake_fetch(url, timeout=None, max_bytes=None):
        return buf.getvalue()

    monkeypatch.setattr(avatar_mod, "fetch_avatar_bytes", fake_fetch)

    digest = run(avatar_hash("https://cdn.example.com/a.png"))

    assert isinstance(digest, int)
    assert 0 <= digest < 2**64


def test_avatar_hash_is_none_when_download_fails(monkeypatch):
    async def fake_fetch(url, timeout=None, max_bytes=None):
        return None

    monkeypatch.setattr(avatar_mod, "fetch_avatar_bytes", fake_fetch)

    assert run(avatar_hash("https://cdn.example.com/a.png")) is None


def test_garbage_bytes_yield_no_hash():
    """下载到的是截断文件或伪装成图片的文本时，必须返回 None。"""

    async def fake_fetch(url, timeout=None, max_bytes=None):
        return b"not an image at all"

    # 直接调 phash 验证这一层的兜底行为（avatar_hash 依赖它）
    assert ident.phash(b"not an image at all") is None


# ---------------------------------------------------------------------------
# 挂到结果上
# ---------------------------------------------------------------------------
def test_attach_does_nothing_when_avatar_not_declared(monkeypatch):
    """站点没声明 avatar 就不该多发一次请求。"""
    calls = []

    async def fake_hash(url, **kwargs):
        calls.append(url)
        return 12345

    monkeypatch.setattr(avatar_mod, "avatar_hash", fake_hash)
    results = {}

    value = run(
        attach_avatar_evidence(
            results,
            ["nickname", "ip_location"],
            '<img class="avatar" src="https://cdn.example.com/a.png">',
        )
    )

    assert value is None
    assert calls == []
    assert "cn_evidence" not in results


def test_attach_writes_hash_and_evidence(monkeypatch):
    async def fake_hash(url, **kwargs):
        return 0x00FF00FF00FF00FF

    monkeypatch.setattr(avatar_mod, "avatar_hash", fake_hash)
    results = {}

    value = run(
        attach_avatar_evidence(
            results,
            ["avatar"],
            '<img class="avatar" src="https://cdn.example.com/a.png">',
        )
    )

    assert value == "00ff00ff00ff00ff"
    assert results["cn_avatar_hash"] == "00ff00ff00ff00ff"
    assert results["cn_evidence"][0]["field"] == "avatar_hash"
    assert results["cn_evidence"][0]["risk"] == 1


def test_attach_stores_only_the_hash_not_the_image(monkeypatch):
    """产品约束：只存哈希。图片字节不能出现在结果里。"""
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 100

    async def fake_fetch(url, timeout=None, max_bytes=None):
        return image_bytes

    monkeypatch.setattr(avatar_mod, "fetch_avatar_bytes", fake_fetch)
    # 注意要打在 avatar_mod 上：avatar.py 是 `from ..identity import phash`，
    # 打 identity.phash 不会影响已经绑定到本模块的引用。
    monkeypatch.setattr(avatar_mod, "phash", lambda data: 42)

    results = {}
    run(
        attach_avatar_evidence(
            results, ["avatar"], '<img class="avatar" src="https://cdn.example.com/a.png">'
        )
    )

    flat = repr(results)
    assert image_bytes.decode("latin-1") not in flat
    assert results["cn_avatar_hash"] == "000000000000002a"


def test_attach_returns_none_when_page_has_no_usable_avatar(monkeypatch):
    async def fake_hash(url, **kwargs):
        raise AssertionError("没有头像地址时不该调用抓取")

    monkeypatch.setattr(avatar_mod, "avatar_hash", fake_hash)
    results = {}

    value = run(attach_avatar_evidence(results, ["avatar"], "<p>没有图片</p>"))

    assert value is None
    assert "cn_evidence" not in results
