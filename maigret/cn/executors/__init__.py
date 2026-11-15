"""执行器。

    http     走上游 aiohttp checker（默认）
    browser  走 CDP 连用户自己的 Chrome（签名/登录态/指纹问题都在浏览器里解决）
    search   站内搜索：主页地址拼不出来时（B站数字 uid 等）用它

`browser` 是唯一有额外依赖（一个正在运行的 Chrome）的执行器，
所以它的失败必须是**可解释的**：拿不到页面就返回 UNKNOWN + 原因，
绝不退回 HTTP 猜测——那会把"被拦"变成"不存在"。
"""

__all__ = ["browser", "cdp", "search"]
