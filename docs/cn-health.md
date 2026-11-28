# 中文站点规则存活率

> 由 `python utils/cn_selfcheck.py --write-doc` 自动生成，请勿手改。
> 生成时间：2026-11-28 11:59:10

## 总览

| 指标 | 值 |
|---|---|
| 通过 | 28 |
| ├ 其中首次未命中、重试后通过（抖动） | 0 |
| **规则失效（该修）** | **0** |
| 环境受限（风控/网络，改规则没用） | 3 |
| 跳过（缺样本对 / 非 http 执行器 / 超预算） | 1 |
| **停用（valid=false，待修）** | **16** |

判定标准：`known` 必须判 CLAIMED，`known_absent` 必须判 AVAILABLE。

「环境受限」与「规则失效」分开统计是刻意的：前者说明这个站需要 Cookie 或
浏览器执行器，去改 `e_string` 是白费功夫；后者才是规则真的过期了。
混在一个数字里，维护者会一直在改不该改的地方。

「抖动」单列同理：这些站这次通过了，但第一次没判出来。它们通过是运气
（或重试）而不是规则稳，下次站点再抖一次就可能变成真的失败。

「停用」也单列：`valid=false` 是临时措施而不是删除。规则关掉之后如果不
留一行，看板上「全部通过」就会掩盖「有几条规则其实被关着」这件事。

## 明细

| 站点 | 结果 | 类型 | 说明 |
|---|---|---|---|
| Juejin | PASS | - | - |
| NetEase Music | PASS | - | - |
| Baidu | PASS | - | - |
| Disqus | PASS | - | - |
| Douban | PASS | - | - |
| Gitee | PASS | - | - |
| Yuque | PASS | - | - |
| Moegirl | PASS | - | - |
| SegmentFault | FAIL | ENV | 被反爬拦截（HTTP 468），页面内容不可信，不是规则问题；需要 Cookie 或浏览器执行器 |
| cnblogs.com | PASS | - | - |
| v2ex.com | FAIL | ENV | 被反爬拦截（HTTP 403），页面内容不可信，不是规则问题；需要 Cookie 或浏览器执行器 |
| Ruby China | PASS | - | - |
| OpenI | PASS | - | - |
| openKylin | PASS | - | - |
| KubeSphere Community | PASS | - | - |
| Appinn | PASS | - | - |
| AskTUG | FAIL | ENV | 被反爬拦截（HTTP 403），页面内容不可信，不是规则问题；需要 Cookie 或浏览器执行器 |
| Sina Blog | PASS | - | - |
| NetEase Music (搜索) | SKIP | - | executor=search，不走四字段判据，本脚本不适用 |
| InfoQ 中文 | PASS | - | - |
| NebulaGraph 论坛 | PASS | - | - |
| openUBMC 论坛 | PASS | - | - |
| openSUSE 中文论坛 | PASS | - | - |
| Trae 社区 | PASS | - | - |
| GitHub 中文社区 | PASS | - | - |
| Luanti 中文论坛 | PASS | - | - |
| Marginnote 论坛 | PASS | - | - |
| BrainBBS | PASS | - | - |
| RuyiSDK 社区 | PASS | - | - |
| StarrySky Gitea | PASS | - | - |
| KCWiki 舰娘百科 | PASS | - | - |
| Mooncell | PASS | - | - |
| QQ | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| Bilibili | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| Weibo | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| Zhihu | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| LeetCode CN | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| NGA | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| Chiphell | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| CSDN | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| 小红书 | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| 抖音 | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| 快手 | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| 脉脉 | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| 即刻 | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| Bilibili (搜索) | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| Gurobot GitLab | DISABLED | - | 规则已停用（valid=false），本次未检查 |
| Miaoyun GitLab | DISABLED | - | 规则已停用（valid=false），本次未检查 |
