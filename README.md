# 用户名暴露自检

查清一个用户名在各平台留下了什么——重点是**中文站点**，以及**我自己的哪些信息已经公开**。

> 基于 [soxoj/maigret](https://github.com/soxoj/maigret) 的中文增强版。上游以 MIT 协议发布，本仓库保留原始 LICENSE 与版权声明。
>
> 英文说明见上游 [soxoj/maigret](https://github.com/soxoj/maigret)：本仓库只维护中文说明，上游 README 覆盖安装步骤与全部命令行选项。

本项目**只提供代码，不运营线上服务**。请在自己机器上部署使用。

---

## 它回答什么问题

一个具体的问题：

> 我自己的手机号、真实姓名、单位、常驻地，在公开网络上能拼出多少？

所以跑完除了上游那份「哪些站有这个用户名」的清单，还会多出一段自查结论：

```
自查结论：YOUR_USERNAME
──────────────────────
风险等级：中

命中的证据
  · 邮箱暴露，可被撞库

证据明细
  · 邮箱「you@example.com」— 来源：og:description · 置信度 0.70

本次未能判定
  · 12 个站点需要登录才能判定（Bilibili、Weibo、Zhihu、Douban、Tieba 等）
  · 3 个站点触发了风控，建议稍后重试（Xiaohongshu、SegmentFault、NGA）
```

## 与上游的关系

继承上游的：站点库（9500+ 站）、命令行、Web 界面、HTML/PDF/XMind/图谱报告、AI 摘要、代理与 Tor 支持。

改动集中在 `maigret/cn/` 子包（约 7500 行，纯标准库实现，可脱离 maigret 运行时单独测试）。上游文件只在**接入点**上改动——`checking.py` 的判定钩子、`report.py` 的报告渲染、`web/` 的设置与结果页——判定逻辑与中文站点数据全部收在子包里，便于合并上游更新。

## 主要改动

| 改动 | 位置 | 说明 |
|---|---|---|
| 四字段判定协议 | `cn/verdict.py` | `e_code`/`e_string`/`m_code`/`m_string` 四项同时命中才下结论；顺序固定为「先判被打断，再判存在性」 |
| 中文站点规则库 | `cn/data/sites_cn.json` | 人工策展，每条都带正反样本与实测日期 |
| 平台字段抽取 | `cn/platforms/` | B 站、掘金、网易云、百度贴吧的接口字段抽取（性别、粉丝、账号年龄等） |
| 公开痕迹检索 | `cn/footprint.py` + `cn/content.py` + `cn/apis.py` | 取回「说过什么、什么时候」：公开接口（GitHub 注册/仓库/动态/**他自己提的议题与 PR**/star、网易云歌单创建时间、**Discourse 论坛的他创建过的主题**）、账号主页列出的文章（含内嵌 JSON 页面）、论坛资料页；带请求预算与间隔，默认关闭，只读公开页 |
| 风险分级 | `cn/risk.py` | 按字段组合给风险等级，**不给处置建议** |
| 跨站身份归并 | `cn/identity.py` | 头像 pHash、主页互链、简介里的联系方式；置信度取簇内最弱的一条边 |
| 中文 PDF | `cn/fonts.py` | 自动探测中文字体，找不到只警告不中断 |
| 暗色图谱 | `cn/report.py` | 单文件自包含，节点按类型着色 |
| 浏览器 Cookie | `cn/browser_cookies.py` | 通过 CDP 读取本机 Chrome 已登录的 Cookie，内存直用、不落盘（**仅 Web 端**） |
| 站点健康自检 | `utils/cn_selfcheck.py` | 每日 CI 用真实网络验证每条规则，规则失效则报红 |

## 安装

需要 Python 3.10+。

```bash
git clone <本仓库地址>
cd username-exposure-selfcheck
python -m venv venv
# Windows
venv\Scripts\activate
# Linux / macOS
source venv/bin/activate

pip install -e .
```

## 用法

命令行：

```bash
python -m maigret 用户名                          # 默认扫描 + 自查结论
python -m maigret 用户名 --pdf --graph            # 加 PDF 与图谱报告
python -m maigret 用户名 --site Baidu --site Juejin
```

Web 界面：

```bash
python -m maigret --web 5000
```

打开 http://127.0.0.1:5000 。默认只监听 `127.0.0.1`。

> **源码运行时不要改成 `0.0.0.0`。** 这个版本没有鉴权、没有限流，所有人共用同一个 `/history` 页面，公开等于把所有人的查询记录摊开。
>
> 用 Docker 时容器内必须绑 `0.0.0.0`（否则转发进来的流量到不了），安全边界在 `-p` 的宿主侧：写 `-p 127.0.0.1:5000:5000`，不要写 `-p 5000:5000`。详见[部署指南](docs/deploy.md)。

常用开关：

| 开关 | 作用 |
|---|---|
| `--no-cn-summary` | 关掉自查结论，输出回到上游原样 |
| `--cn-footprint` | 额外检索公开痕迹：这个用户名在论坛上说过什么、什么时候（默认关闭） |
| `--cn-forum HOST` | 指定要探的论坛域名，可重复；默认用 `cn/data/forums_cn.json` 的实测清单 |
| `--cn-thread URL` | 喂一个你自己搜到的帖子地址，抽出该用户在此帖的全部发言与时间 |
| `--dump-response` | 打印每个站点的完整响应（写规则时必需） |
| `--cn-exclusions PATH` | 本地假阳性排除表 |
| `--checkpoint` / `--resume PATH` | 中断后从检查点续跑 |

环境变量 `MAIGRET_CN_VERDICT` 是整体回退开关：`0` 完全回到上游判定行为，`1`（默认）只接管策展过的中文站，`all` 接管全部站点。

### 用本机浏览器的登录态

中文站大量需要登录。有两种拿登录态的方式：

**推荐：浏览器扩展导出 cookies.txt，上传到设置页。** 一次导出全部站点，
不用逐站导。完整步骤见 [导出 Cookie 的操作步骤](docs/cookies.md)。

**备选：从本机 Chrome 直接读（CDP）。** 设置面板有「从本机浏览器读取 Cookie」开关，
连上 Chrome 把 Cookie 读进内存直接用，不落盘。但 **Chrome 136 起默认用户目录
禁止开远程调试**（`DevTools remote debugging requires a non-default data directory`），
所以这条路在常见环境下走不通——要么用独立 profile 重新登录一遍，
要么给系统加 `RemoteDebuggingAllowed` 策略（需管理员）。

两种来源都是取不到就退回 `cookies.txt`，再取不到就不带 Cookie 继续——不中断检索。

命令行用 `--cookies-jar-file` 手动指定导出好的文件。

> 注意「有 cookie」和「已登录」是两件事：`BAIDUID` 是访客标识，
> `BDUSS` 才是百度登录凭据。设置页的「检测」按钮会逐个站点告诉你到底登录了没有。

## 边界

**只用于自查，或已获得明确授权的调查。**

几条刻意的取舍：

- **不给处置建议。** 平台隐私设置菜单几个月就改版一次，过期的指引比没有指引更坏——用户按图索骥找不到，会以为自己已经安全了。
- **盲区必须显式出现。** 只报命中不提盲区，用户会以为「没查到的就是没有」。
- **不做「查他人」的便利性设计。** 无批量、无目标列表导入、无任务队列。这是自查工具，不是调查工具。
- **只访问公开页面，不绕过任何访问控制。** 不登录别人的账号、不破解签名、不抓搜索引擎结果页（`so.com` 的 robots 是 `Disallow: /s`，百度是 `Disallow: /`）。默认只读论坛资料页；帖子级深挖要你自己给地址（`--cn-thread`）。
- **检索与推断分开。** 工具只把「某年某月某日某人说过某句话」这类原文摆出来，不写「所以你多大、在哪上学」——那种判断会过时也会出错，交给报告之后的 AI 分析做，并且要标成推断。

在中国大陆使用时，还需遵守《个人信息保护法》等相关规定。未经授权查询他人信息可能构成违法。

上游的声明同样适用于本仓库：

> For educational and lawful purposes only. You are responsible for complying with all applicable laws (GDPR, CCPA, etc.) in your jurisdiction. The authors bear no responsibility for misuse.

## 文档

- [中文站点规则编写指南](docs/cn-rules.md)——怎么给一个中文站写一条能用的规则，以及六类必错写法
- [公开痕迹检索](docs/footprint.md)——能捞到什么、捞不到什么，以及内置的请求约束
- [检索能力增强记录](docs/retrieval-upgrade.md)——"检索很弱"拆成四个原因，以及绕开搜索引擎补上的四条路（GitHub 议题/PR、网易云歌单时间线、页面内嵌 JSON、标题归因修正）
- [内容访问可行性](docs/content-access-feasibility.md)——各站的 robots 立场与风控实测数据
- [导出 Cookie 的操作步骤](docs/cookies.md)——微博/知乎/小红书这些必须登录的站怎么用
- [站点存活率看板](docs/cn-health.md)——每日自检生成，失效规则会自动降级
- [部署与使用指南](docs/deploy.md)——Windows / Docker / 源码三种装法，命令行用法与常见故障

## 许可

MIT，与上游一致。详见 [LICENSE](LICENSE)。
