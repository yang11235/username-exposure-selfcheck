"""PDF 报告的中文字体支持。

为什么需要这一层：`xhtml2pdf` 默认用 Helvetica，它没有任何 CJK 字形，
中文会渲染成方块（实测：整份报告只剩 ■）。对一个"中文站点自查"的分支来说，
这等于 PDF 报告不可用。

三条实测结论（xhtml2pdf 0.2.28 + reportlab 5.x，2026-11-14）：

    1. `@font-face` 写在 `default_css` 参数里**不会生效，而且更糟**：
       文字整段消失，页面变成空白。必须写进 HTML 的 `<style>` 块。
    2. `src` 必须是 `file://` URI（`Path.as_uri()`），传 Windows 路径
       （`C:/...` 或 `C:\\...`）会被当成未知 scheme。
    3. 从 0.2.x 某版起 xhtml2pdf 加了资源策略，默认只允许读工作目录内的
       文件。系统字体在目录外，必须用 `extra_roots` 显式放行，否则报
       "Blocked by the resource policy"。

**不内置字体文件**：Noto Sans CJK 一类完整字库十几 MB，塞进包里既拖慢安装，
也把字体许可问题带进项目。这里改为探测系统已有字体，找不到就保持原样
（渲染成方块，但至少不崩、不缺页）——比内置一个来路不明的字体好。
"""

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

logger = logging.getLogger(__name__)

# 允许用户指定字体，覆盖自动探测
ENV_FONT = "MAIGRET_CN_PDF_FONT"

# 探测顺序：**先 .ttf/.otf，后 .ttc**。
#
# 为什么：reportlab 的 TTFont 读 .ttc 需要指定 subfontIndex，而 xhtml2pdf
# 的 @font-face 走的是自己那条路，不传这个参数。.ttf 没有这个问题。
# 各平台的中文字体大量是 .ttc（msyh.ttc / simsun.ttc / NotoSansCJK-*.ttc），
# 所以 .ttc 只作兜底。
CANDIDATES: List[str] = [
    # ---- .ttf / .otf：reportlab 直接能读，优先 ----
    # Windows
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\simkai.ttf",
    r"C:\Windows\Fonts\simfang.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux
    "/usr/share/fonts/truetype/arphic/uming.ttf",
    # ---- .ttc：兜底 ----
    # Windows
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    # macOS
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    # Linux
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
]

FONT_FAMILY = "MaigretCJK"


def find_cjk_font(explicit: Optional[str] = None) -> Optional[Path]:
    """找一个能用的中文字体文件。找不到返回 None（调用方据此跳过注入）。

    `explicit` 优先，其次是环境变量 `MAIGRET_CN_PDF_FONT`，最后按
    `CANDIDATES` 顺序探测。**只检查文件存在**，不检查字体内容——
    读字体表要额外依赖（fontTools），而候选列表里全是已知的中文字体。
    """
    for candidate in (explicit, os.environ.get(ENV_FONT)):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
        # 用户显式指定了却不存在，要说出来：静默回落到自动探测，
        # 用户会以为自己指定的字体生效了。
        logger.warning("指定的中文字体不存在：%s，改用自动探测", candidate)

    for candidate in CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def build_font_style(font_path: Path) -> str:
    """生成注入用的 `<style>` 块。

    `src` 必须是 `file://` URI：实测传 Windows 路径会被 xhtml2pdf 当成
    未知 scheme，字体不加载，文字整段消失。
    """
    uri = font_path.as_uri()
    return (
        "<style>\n"
        f'@font-face {{ font-family: "{FONT_FAMILY}"; src: url("{uri}"); }}\n'
        "body, p, td, th, li, div, span, h1, h2, h3, h4, h5, h6, small, a "
        f'{{ font-family: "{FONT_FAMILY}", sans-serif; }}\n'
        "</style>\n"
    )


def inject_font_style(html: str, font_path: Path) -> str:
    """把字体样式插进 `<head>` 里。

    必须插进 HTML，不能走 `default_css` 参数：实测走 `default_css` 时
    xhtml2pdf 不但不加载字体，还会把整页文字吞掉（页面空白）。
    """
    style = build_font_style(font_path)
    lowered = html.lower()
    for marker in ("</head>", "<body"):
        index = lowered.find(marker)
        if index >= 0:
            return html[:index] + style + html[index:]
    # 没有 head 也没有 body：塞到最前面，让 xhtml2pdf 自己整理
    return style + html


@contextmanager
def resource_policy(font_path: Optional[Path]) -> Iterator[None]:
    """放行字体所在目录的读取权限。

    xhtml2pdf 0.2.x 起默认只允许读工作目录内的文件，系统字体在目录外，
    不放行就会拿到 "Blocked by the resource policy"，字体不加载。

    老版本没有这个 API：那时也没有限制，直接放行即可。所以这里对导入失败
    是**静默跳过**，不是降级——两种情况的结果都是"能读到字体"。
    """
    if font_path is None:
        yield
        return

    try:
        from xhtml2pdf.config.resources import (  # type: ignore[import-not-found]
            ResourceAccessPolicy,
            use_policy,
        )
    except ImportError:
        yield
        return

    policy = ResourceAccessPolicy(
        # 报告里的图片全部被 link_callback 换成本地空白图，不需要联网。
        # 关掉远程抓取既符合实际，也顺手堵掉 SSRF 面。
        allow_remote=False,
        extra_roots=(font_path.parent,),
    )
    with use_policy(policy):
        yield
