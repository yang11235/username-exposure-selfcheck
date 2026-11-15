# 用户名暴露自检

查清一个用户名在各平台留下了什么——重点是**中文站点**，以及**我自己的哪些信息已经公开**。

> 基于 [soxoj/maigret](https://github.com/soxoj/maigret) 的中文增强版。上游以 MIT 协议发布，本仓库保留原始 LICENSE 与版权声明。
>
> English: [README.en.md](README.en.md)（上游原文） · 上游的简体中文翻译: [README.zh-CN.md](README.zh-CN.md) · [Français](README.fr.md)

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

继承上游的：站点库（3000+ 站）、命令行、Web 界面、HTML/PDF/XMind/图谱报告、AI 摘要、代理与 Tor 支持。

改动集中在 `maigret/cn/` 子包（约 7600 行，纯标准库实现，可脱离 maigret 运行时单独测试）。上游文件只在**接入点**上改动——`checking.py` 的判定钩子、`report.py` 的报告渲染、`web/` 的设置与结果页——判定逻辑与中文站点数据全部收在子包里，便于合并上游更新。

## 主要改动

| 改动 | 位置 | 说明 |
|---|---|---|
| 四字段判定协议 | `cn/verdict.py` | `e_code`/`e_string`/`m_code`/`m_string` 四项同时命中才下结论；顺序固定为「先判被打断，再判存在性」 |
| 中文站点规则库 | `cn/data/sites_cn.json` | 人工策展，每条都带正反样本与实测日期 |
| 平台字段抽取 | `cn/platforms/` | B 站、掘金、网易云、百度贴吧的接口字段抽取（性别、粉丝、账号年龄等） |
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
cd maigret
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
python -m maigret 用户名 --mask                   # 脱敏输出，截图前用
python -m maigret 用户名 --site Baidu --site Juejin
```

Web 界面：

```bash
python -m maigret --web 5000
```

打开 http://127.0.0.1:5000 。默认只监听 `127.0.0.1`。

> **不要改成 `0.0.0.0`。** 这个版本没有鉴权、没有限流，所有人共用同一个 `/history` 页面，公开等于把所有人的查询记录摊开。

常用开关：

| 开关 | 作用 |
|---|---|
| `--no-cn-summary` | 关掉自查结论，输出回到上游原样 |
| `--mask` | 脱敏邮箱、手机号、姓名、属地 |
| `--dump-response` | 打印每个站点的完整响应（写规则时必需） |
| `--cn-exclusions PATH` | 本地假阳性排除表 |
| `--checkpoint` / `--resume PATH` | 中断后从检查点续跑 |

环境变量 `MAIGRET_CN_VERDICT` 是整体回退开关：`0` 完全回到上游判定行为，`1`（默认）只接管策展过的中文站，`all` 接管全部站点。

### 用本机浏览器的登录态

中文站大量需要登录。Web 端设置面板有「从本机浏览器读取 Cookie」开关：它会连上本机已登录的 Chrome（CDP），把 Cookie 读进内存直接用，**不落盘、不写入任何文件**。取不到就退回 `cookies.txt`，再取不到就不带 Cookie 继续——不中断检索。

命令行目前没有对应参数，需要时用 `--cookies-jar-file` 手动指定。

## 边界

**只用于自查，或已获得明确授权的调查。**

几条刻意的取舍：

- **不给处置建议。** 平台隐私设置菜单几个月就改版一次，过期的指引比没有指引更坏——用户按图索骥找不到，会以为自己已经安全了。
- **盲区必须显式出现。** 只报命中不提盲区，用户会以为「没查到的就是没有」。
- **不做「查他人」的便利性设计。** 无批量、无目标列表导入、无任务队列。这是自查工具，不是调查工具。
- **只访问个人主页。** 不做内容抓取、不遍历帖子、不翻评论。

在中国大陆使用时，还需遵守《个人信息保护法》等相关规定。未经授权查询他人信息可能构成违法。

上游的声明同样适用于本仓库：

> For educational and lawful purposes only. You are responsible for complying with all applicable laws (GDPR, CCPA, etc.) in your jurisdiction. The authors bear no responsibility for misuse.

## 文档

- [中文站点规则编写指南](docs/cn-rules.md)——怎么给一个中文站写一条能用的规则，以及六类必错写法
- [站点存活率看板](docs/cn-health.md)——每日自检生成，失效规则会自动降级
- [部署与使用指南](docs/deploy.md)——Windows / Docker / 源码三种装法，命令行用法与常见故障

## 许可

MIT，与上游一致。详见 [LICENSE](LICENSE)。
