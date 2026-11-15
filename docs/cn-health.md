# 中文站点规则存活率

> 由 `python utils/cn_selfcheck.py --write-doc` 自动生成，请勿手改。
> 生成时间：2026-11-14 21:29:32

## 总览

| 指标 | 值 |
|---|---|
| 通过 | 12 |
| **规则失效（该修）** | **0** |
| 环境受限（风控/网络，改规则没用） | 8 |
| 跳过（缺样本对） | 0 |

判定标准：`known` 必须判 CLAIMED，`known_absent` 必须判 AVAILABLE。

「环境受限」与「规则失效」分开统计是刻意的：前者说明这个站需要 Cookie 或
浏览器执行器，去改 `e_string` 是白费功夫；后者才是规则真的过期了。
混在一个数字里，维护者会一直在改不该改的地方。

## 明细

| 站点 | 结果 | 类型 | 说明 |
|---|---|---|---|
| Juejin | PASS | - | - |
| NetEase Music | PASS | - | - |
| Bilibili | PASS | - | - |
| Weibo | FAIL | ENV | 被反爬拦截（HTTP 403），页面内容不可信，不是规则问题；需要 Cookie 或浏览器执行器 |
| Zhihu | PASS | - | - |
| Disqus | FAIL | ENV | 两侧都不命中：e_string='https://disqus.com/api/users/' / m_string='User matching query does not exist' 与当前页面不符（实际 HTTP None） |
| Douban | PASS | - | - |
| Gitee | PASS | - | - |
| Yuque | PASS | - | - |
| Moegirl | FAIL | ENV | 状态码不符：期望 e=200/m=404，实际 HTTP None |
| SegmentFault | FAIL | ENV | 被反爬拦截（HTTP 468），页面内容不可信，不是规则问题；需要 Cookie 或浏览器执行器 |
| cnblogs.com | PASS | - | - |
| v2ex.com | FAIL | ENV | 状态码不符：期望 e=200/m=404，实际 HTTP None |
| Ruby China | FAIL | ENV | 状态码不符：期望 e=200/m=404，实际 HTTP None |
| OpenI | PASS | - | - |
| openKylin | PASS | - | - |
| KubeSphere Community | PASS | - | - |
| Appinn | FAIL | ENV | 状态码不符：期望 e=200/m=404，实际 HTTP None |
| AskTUG | FAIL | ENV | 状态码不符：期望 e=200/m=404，实际 HTTP None |
| Sina Blog | PASS | - | - |
