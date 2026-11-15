"""头像抓取：把头像 URL 变成 64bit pHash，**只保留哈希，不保留图片**。

为什么值得单独一层（文档 §5.5）：中国用户跨平台复用同一头像的比例极高，
而这条信号完全免费、不需登录、不受反爬影响——只要拿到头像 URL 就能算。
它是身份聚类里投入产出比最高的一条边。

三条硬约束，都写进代码：

1. **只存哈希。** 图片字节在函数里走完就丢，不落盘、不进结果、不进日志。
   本项目不做分享也不做云端（文档 §1.2③④），留原图没有任何理由。

2. **不因为头像失败而失败。** 所有异常都吞成 None：头像是锦上添花，
   一个 CDN 超时不该让整次扫描中断，也不该让用户看到 traceback。

3. **体积上限 + 类型校验。** 抓回来的可能是 HTML 错误页、几十 MB 的
   原始图、或者一个会无限流数据的地址。三条都要挡：
   Content-Type 必须是 image/*、边下边计数超过上限就放弃、只接受 http(s)。

隐私：不复用扫描用的 cookie/headers，单独开一个干净会话。
头像通常挂在公开 CDN 上，带上站点的登录态既没必要，也会把"这个人在查谁"
泄露给 CDN。
"""

import logging
from typing import Any, Optional

from ..extract import Evidence, extract_avatar_url
from ..identity import phash

logger = logging.getLogger(__name__)

# 关掉头像抓取（有些用户不希望产生任何额外请求）
ENV_DISABLE = "MAIGRET_CN_AVATAR"

DEFAULT_TIMEOUT = 10.0
# 2MB 足够覆盖任何头像。超过这个大小的"图片"基本是原始大图或伪装成图的
# 数据流，对 pHash 也没有额外价值（反正要缩到 32×32）。
MAX_BYTES = 2 * 1024 * 1024


def enabled() -> bool:
    import os

    return (os.environ.get(ENV_DISABLE) or "").strip().lower() not in (
        "0",
        "false",
        "off",
    )


async def fetch_avatar_bytes(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
    session: Any = None,
) -> Optional[bytes]:
    """下载头像字节。任何失败都返回 None，不抛异常。

    `session` 可注入（测试用），不传就自己开一个干净会话——
    不带扫描用的 cookie，见模块说明。
    """
    if not enabled():
        return None
    if not url or not url.lower().startswith(("http://", "https://")):
        return None

    import aiohttp

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        async with session.get(
            url, timeout=timeout, allow_redirects=True
        ) as response:
            if response.status != 200:
                logger.debug("头像 %s 返回 %s，跳过", url, response.status)
                return None

            content_type = (response.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/"):
                # 常见情况：CDN 返回一个 HTML 的 403/404 页，或者防盗链跳转页。
                logger.debug("头像 %s 的类型是 %r，不是图片，跳过", url, content_type)
                return None

            chunks = []
            total = 0
            async for chunk in response.content.iter_chunked(8192):
                total += len(chunk)
                if total > max_bytes:
                    logger.debug("头像 %s 超过 %d 字节上限，放弃", url, max_bytes)
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception as exc:
        # 超时、DNS、证书、连接重置……全都只是"这个头像拿不到"。
        logger.debug("抓取头像 %s 失败：%s", url, exc)
        return None
    finally:
        if own_session:
            try:
                await session.close()
            except Exception:
                pass


async def avatar_hash(
    url: str, timeout: float = DEFAULT_TIMEOUT, max_bytes: int = MAX_BYTES
) -> Optional[int]:
    """头像 URL → 64bit pHash。拿不到就 None。"""
    data = await fetch_avatar_bytes(url, timeout=timeout, max_bytes=max_bytes)
    if not data:
        return None
    return phash(data)


async def attach_avatar_evidence(
    results_info: dict,
    fields,
    html: str,
    base_url: str = "",
    masked: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[str]:
    """把头像指纹挂到结果里。返回十六进制哈希串，没拿到就 None。

    只在这个站点**显式声明**了 avatar / avatar_hash 时才动手：
    多一次网络请求必须有明确的理由，不能"顺手都抓一遍"。
    """
    declared = set(fields or ())
    if not ({"avatar", "avatar_hash"} & declared):
        return None
    if not enabled():
        return None

    url = extract_avatar_url(html, base_url)
    if not url:
        # JSON 接口（B 站/网易云/掘金）没有 og:image，头像地址在接口字段里。
        # 这一步放在兜底位置：HTML 页面里能抽到就用页面的，抽不到才问平台层。
        from ..platforms import avatar_url as platform_avatar_url

        url = platform_avatar_url(html, base_url)
    if not url:
        logger.debug("页面里没找到可用的头像地址")
        return None

    digest = await avatar_hash(url, timeout=timeout)
    if digest is None:
        return None

    value = f"{digest:016x}"
    results_info["cn_avatar_hash"] = value

    entry = Evidence(
        "avatar_hash",
        value,
        source=f"头像 {url}",
        confidence=0.8,
    )
    results_info.setdefault("cn_evidence", []).append(entry.to_dict(masked=masked))
    return value
