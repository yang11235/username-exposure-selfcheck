# 用户名暴露自检

查清一个用户名在各平台留下了什么。

基于 [soxoj/maigret](https://github.com/soxoj/maigret) 的中文增强版本。

本项目**只提供代码，不运营线上服务**。请在自己机器上部署使用。

---

## 与上游的关系

上游 [maigret](https://github.com/soxoj/maigret) 用 MIT 协议发布，允许修改和再分发。本仓库保留原始 LICENSE 与版权声明。

分支策略：

- `main` — 保持与上游一致，只用来同步，不提交自己的改动
- `zh` — 定制内容都在这里

同步上游更新：

```bash
git fetch upstream
git checkout main && git merge upstream/main    # 通常能快进
git checkout zh && git rebase main              # 冲突在这里出现
```

## 新增了什么

### 界面中文化

上游没有 i18n 层（没有 gettext，也没有 Flask-Babel），所以是直接替换字符串。覆盖导航栏、检索表单、设置面板、实时进度页、结果页和历史页。

**这一点需要留意**：上游改界面文案时，翻译可能失效。为了能及时发现，`zh/apply.py` 里有一套补丁校验机制（见下文「维护补丁」）。

### 核心账号比对

同一用户名在不同平台常常不是同一个人。结果页每个账号旁有「设为核心」，选定一个信息最全的账号（比如本人在用的 GitHub）作锚点，其余账号按姓名、简介、位置、公司、主页域名打分：

| 信号 | 加分 |
|---|---|
| 主页域名与核心相同 | +3 |
| 姓名完全一致 / 包含 / 相似 | +3 / +2 / +1 |
| 简介重合度 ≥0.5 / ≥0.2 | +2 / +1 |
| 位置一致 | +2 |
| 公司一致 | +1 |

≥4 判高，≥2 判中，其余判低。低相关会变灰，可隐藏。每条结果附判定理由。

只做「像不像」，不下身份结论。没有可比字段的账号一律判低——没有证据就是没有证据。

### 从主页 URL 起步

首页可填一个主页地址（如 `https://github.com/用户名`），会：

1. 从页面抽出关联的 ID / 用户名，一并加入检索
2. 把该 URL 反解成「站点 + 用户名」，**自动作为核心锚点**

反解用站点库的 `urlMain` / `url` 模板，因此各站不同路径都能识别（`weibo.com/n/x`、`douban.com/people/x`、`space.bilibili.com/123`）。

### 用户名变体

设置里的「生成用户名变体」勾选后生效，与命令行 `--permute` 行为一致。**需要输入两个以上名字**，单个用户名没有可变的部分。

上游这个开关原本是失效的（值存了但没被使用），本版已修。

### AI 摘要

对检索结果生成结构化摘要（姓名、位置、职业、兴趣、置信度等）。在 `~/.maigret/settings.json` 配置：

```json
{
  "openai_api_base": "https://api.example.com/v1",
  "openai_api_key": "sk-...",
  "openai_model": "gpt-4o-mini"
}
```

兼容 OpenAI 接口的服务都能用，包括本地部署的。

**必须用流式请求**。不少网关有「首字节 60 秒超时」，非流式要等整段生成完才返回第一个字节，长报告必然被掐断并报 `ABORTED`。代码已改为流式。

### 登录 Cookie 导入

部分平台（微博、贴吧等）不登录看不到完整资料。设置面板支持两种格式，粘贴即用：

1. 浏览器 F12 → 网络 → 请求头里 `Cookie:` 后面那串（需填域名）
2. 扩展导出的 `cookies.txt` 全文（制表符分隔，可含多个域名，自动识别）

注意微博的 `SUB`、贴吧的 `BDUSS` 都是 HttpOnly，控制台里 `document.cookie` 读不到，只能从请求头或扩展导出获取。

**Cookie 等于登录凭据**，只在需要时勾选「检索时携带 Cookie」，用完取消。

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
maigret --web 5000
```

打开 http://127.0.0.1:5000

默认只监听 `127.0.0.1`。**不要改成 `0.0.0.0` 对外暴露**——这个版本没有鉴权、没有限流、所有人共用同一个 `/history` 页面，公开等于把所有人的查询记录摊开。

## 维护补丁

`zh/apply.py` 用于把改动重新应用到一份干净的上游代码上，适合上游升级后的场景。

```bash
python zh/apply.py           # 应用，失配则退出码 1
python zh/apply.py --check   # 只检查，不写文件
python zh/apply.py --restore # 回滚到英文原文
```

设计要点（这几个坑都踩过）：

- **每轮从 `zh/orig/` 的原文重放**，不在已打过补丁的文件上再匹配。否则上游升级后旧补丁会继续套在旧原文上，看不出上游改了什么。
- **补丁失配必须报错退出**。早期版本是静默跳过并返回 0，导致界面又变回英文却没人知道。
- **读写用二进制**。Windows 上 `read_text`/`write_text` 默认把 `\n` 转成 `\r\n`，会让整个文件显示成「全部重写」的假 diff。

## 已提交给上游的修复

这三处本版也用到了，已提 PR：

- [#3392](https://github.com/soxoj/maigret/pull/3392) 每份报告混入其他用户名结果
- [#3393](https://github.com/soxoj/maigret/pull/3393) 关系图堆满无命中的空节点
- [#3394](https://github.com/soxoj/maigret/pull/3394) Web 端 Permute 开关失效

合并后可以去掉本仓库里的对应改动。

## 使用边界

这个工具会汇总一个用户名在各平台的账号、位置、公司、社交关系。

**只用于自查，或已获得明确授权的调查。**

上游的声明同样适用于本仓库：

> For educational and lawful purposes only. You are responsible for complying with all applicable laws (GDPR, CCPA, etc.) in your jurisdiction. The authors bear no responsibility for misuse.

在中国大陆使用时，还需遵守《个人信息保护法》等相关规定。未经授权查询他人信息可能构成违法。

## 许可

MIT，与上游一致。详见 [LICENSE](LICENSE)。
