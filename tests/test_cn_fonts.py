"""PDF 中文字体支持测试（不联网、不依赖真实 PDF 渲染）。

背景：`xhtml2pdf` 默认用 Helvetica，它没有 CJK 字形，中文全部渲染成方块。
三条结论都是实测出来的，这里逐条钉住，否则下次升级依赖很容易悄悄退回原样：

    1. `@font-face` 必须写进 HTML 的 `<style>`；写进 `default_css` 参数
       不但不生效，还会让整页文字消失（比方块更糟）。
    2. `src` 必须是 `file://` URI，传 Windows 路径会被当成未知 scheme。
    3. xhtml2pdf 0.2.x 的资源策略默认只允许读工作目录，系统字体在目录外，
       必须用 `extra_roots` 放行。

真正的端到端渲染（生成 PDF 再抽文字）在 `tests/test_cn_pdf_render.py` 里，
需要 `pdf` 可选依赖，没有就跳过。
"""

import logging
from pathlib import Path

import pytest

from maigret.cn import fonts


def test_no_font_returns_none(monkeypatch):
    """找不到字体时返回 None，调用方据此跳过注入（渲染成方块但不崩）。"""
    monkeypatch.setattr(fonts, "CANDIDATES", ["/nonexistent/a.ttf"])
    monkeypatch.delenv(fonts.ENV_FONT, raising=False)

    assert fonts.find_cjk_font() is None


def test_explicit_path_wins(monkeypatch, tmp_path):
    font = tmp_path / "my.ttf"
    font.write_bytes(b"x")
    monkeypatch.setattr(fonts, "CANDIDATES", ["/nonexistent/a.ttf"])
    monkeypatch.delenv(fonts.ENV_FONT, raising=False)

    assert fonts.find_cjk_font(str(font)) == font


def test_env_var_is_used(monkeypatch, tmp_path):
    font = tmp_path / "my.ttf"
    font.write_bytes(b"x")
    monkeypatch.setattr(fonts, "CANDIDATES", ["/nonexistent/a.ttf"])
    monkeypatch.setenv(fonts.ENV_FONT, str(font))

    assert fonts.find_cjk_font() == font


def test_a_missing_explicit_path_falls_back_and_says_so(monkeypatch, tmp_path, caplog):
    """静默回落会让用户以为自己指定的字体生效了。"""
    font = tmp_path / "found.ttf"
    font.write_bytes(b"x")
    monkeypatch.setattr(fonts, "CANDIDATES", [str(font)])
    monkeypatch.delenv(fonts.ENV_FONT, raising=False)

    with caplog.at_level(logging.WARNING):
        result = fonts.find_cjk_font("/nonexistent/typo.ttf")

    assert result == font
    assert any("不存在" in record.message for record in caplog.records)


def test_candidates_put_ttf_before_ttc():
    """reportlab 读 .ttc 需要 subfontIndex，而 xhtml2pdf 的 @font-face 不传这个参数。

    各平台的中文字体大量是 .ttc，所以 .ttc 只能当兜底。
    """
    suffixes = [Path(name).suffix.lower() for name in fonts.CANDIDATES]

    assert ".ttf" in suffixes and ".ttc" in suffixes
    assert max(i for i, s in enumerate(suffixes) if s in (".ttf", ".otf")) < min(
        i for i, s in enumerate(suffixes) if s == ".ttc"
    )


def test_font_style_uses_a_file_uri(tmp_path):
    """传 Windows 路径会被 xhtml2pdf 当成未知 scheme，字体不加载。"""
    font = tmp_path / "cjk.ttf"
    font.write_bytes(b"x")

    style = fonts.build_font_style(font)

    assert "file://" in style
    assert font.as_uri() in style
    assert fonts.FONT_FAMILY in style


def test_font_style_covers_the_elements_reports_use(tmp_path):
    font = tmp_path / "cjk.ttf"
    font.write_bytes(b"x")

    style = fonts.build_font_style(font)

    for tag in ("body", "p", "td", "th", "h1", "h2", "h3", "small", "a"):
        assert tag in style


def test_inject_puts_the_style_inside_head(tmp_path):
    font = tmp_path / "cjk.ttf"
    font.write_bytes(b"x")
    html = '<html><head><meta charset="utf-8" /></head><body><p>x</p></body></html>'

    result = fonts.inject_font_style(html, font)

    assert result.index("<style>") < result.index("</head>")
    assert result.count("<style>") == 1


def test_inject_handles_html_without_a_head(tmp_path):
    font = tmp_path / "cjk.ttf"
    font.write_bytes(b"x")

    result = fonts.inject_font_style("<body><p>x</p></body>", font)

    assert result.startswith("<style>")
    assert "<body>" in result


def test_inject_is_case_insensitive_about_the_head_tag(tmp_path):
    font = tmp_path / "cjk.ttf"
    font.write_bytes(b"x")

    result = fonts.inject_font_style("<HTML><HEAD></HEAD><BODY></BODY></HTML>", font)

    assert result.index("<style>") < result.index("</HEAD>")


def test_resource_policy_without_a_font_is_a_noop():
    """没有字体时不需要放行任何目录，也不该抛异常。"""
    with fonts.resource_policy(None):
        pass


def test_resource_policy_allows_the_font_directory(tmp_path):
    """不放行字体目录会拿到 "Blocked by the resource policy"，字体加载不到。"""
    pytest.importorskip("xhtml2pdf.config.resources")

    from xhtml2pdf.config.resources import active_policy

    font = tmp_path / "cjk.ttf"
    font.write_bytes(b"x")

    with fonts.resource_policy(font):
        policy = active_policy()
        assert policy is not None
        assert font.parent in policy.extra_roots
        # 报告里的图片全部被回调换成本地空白图，不需要联网
        assert policy.allow_remote is False
