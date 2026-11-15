# 部署与使用指南

> 面向本仓库（基于 [soxoj/maigret](https://github.com/soxoj/maigret) 的中文增强版）。
>
> 文中命令写作 `maigret`，等价于 `python -m maigret`。注意 `pip install maigret` 装到的是**上游原版**，
> 不含中文站判定与自查结论——要用本仓库的改动请从源码安装（见 §7）。
>
> 实测环境：Windows 11 + Python 3.13。标注「实测」的结论来自实机跑通；标注「文档」的来自官方文档，未实机复现。
> 上游文档：https://maigret.readthedocs.io

---


## 1. 项目定位

按用户名跨站检索公开账号的 OSINT 工具。输入一个用户名，并发探测 5000+ 站点，判断哪些站点存在该用户名，解析页面提取真实姓名、位置、简介、关联账号等信息，并用新发现的 ID 继续递归检索。

- 站点库：内置 **5897 个站点**（实测启动日志），默认只查流量排名前 500，`-a` 才全查
- 输出：HTML / PDF / Markdown / JSON / CSV / TXT / XMind / 交互关系图 / Neo4j Cypher
- 许可：MIT
- 不需要任何 API Key（除 `--ai` 摘要功能）

**能力边界**：它做的是「同一用户名在哪些站点被占用 + 页面上的公开信息」，不是人肉搜索定位。误报率取决于站点判定方式，高价值结论必须人工复核。

---

## 2. 最短可用路径

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install maigret
maigret someusername
```

查看结果报告：`maigret someusername -H -M -fo results`

---

## 3. 部署方式选型

| 方式 | 适用场景 | 依赖 | 实测状态 |
|---|---|---|---|
| pip + venv | 本地常驻、要写脚本调用 | Python 3.10+ | ✅ 已验证（3.13 可用） |
| Windows 单文件 exe | 不想装 Python | 无 | 文档提供 |
| Docker CLI 镜像 | 服务器 / 隔离环境 | Docker | 文档提供，未实机验证 |
| Docker web 镜像 | 起 Web 界面 | Docker | 文档提供 |
| 源码安装 | 要改站点库或提 PR | Python + git | 未单独验证（等价于 pip） |
| Snap | Linux 桌面 | snapd | 文档提供 |

**推荐**：日常用 `pip + venv`，需要交付给别人用就打包 Docker 的 web 镜像。不要装在全局环境——它拉 40+ 个依赖，会污染系统 site-packages（实测 venv 占用 174 MB）。

---

## 4. 方案 A：pip + venv（推荐）

### 4.1 安装

Windows（PowerShell 或 CMD）：

```bat
cd D:\tools
python -m venv maigret-env
maigret-env\Scripts\activate
python -m pip install --upgrade pip
pip install maigret
```

Linux / macOS：

```bash
python3 -m venv ~/maigret-env
source ~/maigret-env/bin/activate
pip install --upgrade pip
pip install maigret
```

实测耗时：**1 分 26 秒**（干净缓存，下载 40 余个包）。全部为 wheel，无需编译。

### 4.2 验证

```bash
maigret --version
```

实测输出：

```
maigret 0.6.6
Socid-extractor:  0.1.1
Aiohttp:  3.14.5
Requests:  2.34.2
Python:  3.13.14
```

### 4.3 可选依赖

```bash
pip install "maigret[pdf]"     # 仅 PDF 报告需要
```

实测：不装时报错 `PDF reports require the optional 'pdf' extra`；装完（xhtml2pdf + svglib + pypdf，约 8 MB）在 Windows 上直接可生成 PDF，不需要额外的系统图形库。**Linux 上则需要 cairo / libxml2 / libxslt 等系统库**，这也是官方 Dockerfile 里 `apt-get install libcairo2-dev` 的原因。

### 4.4 不想每次激活环境

```bat
:: 方式一：直接用绝对路径
D:\tools\maigret-env\Scripts\maigret.exe someusername --html

:: 方式二：加一条 alias（PowerShell 写入 $PROFILE）
Set-Alias maigret "D:\tools\maigret-env\Scripts\maigret.exe"
```

---

## 5. 方案 B：Windows 单文件 exe

从 Releases 下载 `maigret_standalone.exe`，不需要 Python。

```bat
cd %USERPROFILE%\Downloads
maigret_standalone.exe USERNAME
maigret_standalone.exe USERNAME --html
maigret_standalone.exe --help
```

双击运行会交互式提示输入用户名，跑完后暂停以便查看报告链接。适合临时用、或给别人用。缺点是版本更新要重新下载，且无法 `import maigret` 做二次开发。

---

## 6. 方案 C：Docker

官方发布两个镜像变体，源码里对应 Dockerfile 的两个 stage：

| 镜像 | 用途 | 入口 |
|---|---|---|
| `soxoj/maigret:latest` | CLI（默认 stage） | `maigret` |
| `soxoj/maigret:web` | Web 界面 | `maigret --web $PORT` |

基础镜像是 `python:3.11-slim`，编译期装了 build-essential / libcairo2-dev / libxml2-dev / libxslt1-dev。

### 6.1 CLI

```bash
docker pull soxoj/maigret

# 报告落到宿主机 /mydir
docker run --rm -v /mydir:/app/reports soxoj/maigret:latest username --html -M

# 交互式排障
docker run --rm -it soxoj/maigret:latest username --print-errors -vv

# 走宿主机代理（容器内 127.0.0.1 不是宿主机）
docker run --rm -e HTTP_PROXY=http://host.docker.internal:7890 soxoj/maigret:latest username
```

> 容器工作目录是 `/app`，默认输出目录 `reports` 即 `/app/reports`，所以挂载点写 `/app/reports`。

### 6.2 Web 界面

```bash
docker run -d --name maigret-web -p 5000:5000 soxoj/maigret:web

# 换端口
docker run -d -p 8080:8080 -e PORT=8080 soxoj/maigret:web
```

镜像里 `ENV PORT=5000`、`EXPOSE 5000`，入口会执行 `maigret --web "$PORT"`。

### 6.3 容器内建议关掉数据库自动更新

每次启动都去 GitHub 拉站点库，在容器里是纯浪费，离线环境还会卡住：

```bash
docker run --rm -v /mydir:/app/reports soxoj/maigret:latest username --no-autoupdate
```

或者挂一个 settings 进去：

```bash
docker run --rm -v /mydir/settings.json:/root/.maigret/settings.json ...
```

```json
{ "no_autoupdate": true }
```

### 6.4 自己构建

```bash
docker build -t maigret .                # CLI
docker build --target web -t maigret-web .   # Web 界面
```

构建时加 `YARL_NO_EXTENSIONS=1` 环境变量（官方 Dockerfile 里有），避免 yarl 拉 C 扩展编译。

---

## 7. 方案 D：源码安装（要改站点库时）

```bash
git clone https://github.com/soxoj/maigret
cd maigret
pip install -e .
```

站点定义在 `maigret/resources/data.json`。改完不要用 `json.load`/`json.dump` 整体重写（会打乱格式和 diff），手工编辑对应条目，然后跑：

```bash
python utils/update_site_data.py     # 重新生成 sites.md 与库元数据
```

再提 PR。

---

## 8. 方案 E：Web 界面上线

### 8.1 本地起服务

```bash
maigret --web 5000            # 或 maigret --web（默认 5000）
# 打开 http://127.0.0.1:5000
```

也可以直接走模块入口：`python -m maigret.web.app`。

实测：启动约 6 秒（要加载 5897 站点库），`GET /` 返回 200，页面表单 `POST /live`（302 跳转到 `/live/<id>` 流式出结果），支持 `mode=fast`（top 站点）和 `mode=full`（全量）两种模式，设置弹窗里可改 timeout 和站点数。

### 8.2 不要直接暴露到公网

启动日志会明确警告：

```
WARNING: This is a development server. Do not use it in a production deployment.
```

它内置的是 Flask 开发服务器，单进程、无鉴权、无速率限制，且每个请求会向数千个外部站点发起连接。要对外提供：

```bash
# 用 waitress（Windows 友好）或 gunicorn 托管
pip install waitress
waitress-serve --listen=127.0.0.1:5000 --threads=8 maigret.web.app:app
```

前面再挂 Nginx/Caddy 做 Basic Auth + HTTPS + 请求频率限制。README 里提到的 Render 一键部署也明确说明「实例没有登录验证，任何持 URL 的人都能用」，公开部署等于给别人一个可滥用的扫描出口，IP 被封是小事，被用来骚扰他人才是问题。

另外页面样式从 `cdn.jsdelivr.net` 加载 Bootstrap，内网/离线环境会裸奔，需要自行改成本地静态文件。

---

## 9. 命令行用法

### 9.1 选站点集（决定耗时）

| 目的 | 参数 | 说明 |
|---|---|---|
| 快速摸底 | 默认 | 只查流量排名前 500 |
| 指定站点 | `--site GitHub --site Reddit` | 可重复，实测有效（文档页未列出） |
| 按类别/国家 | `--tags photo,dating` | 标签是**类别 + 两位国家码**，不是语言 |
| 排除类别 | `--exclude-tags nsfw,dating` | 可与 `--tags` 组合 |
| 控制规模 | `--top-sites 1000` | 按 Majestic Million 排名取前 N |
| 全量 | `-a` | 5000+ 站点，耗时成倍增长 |
| 批量输入 | `--input-file names.txt` | 一行一个，`-` 读 stdin，支持 `vk_id:12345` 前缀 |

实测一个反直觉的点：`--top-sites 500` 实际会检查 **1527 个站点**。原因是父平台在榜内时会自动带上其镜像站（如 Twitter → memory.lol），加上递归发现的新 ID 又要再查一轮。选站点时要按这个放大倍数估时间。

### 9.2 精度与递归

```bash
--no-recursion      # 关掉「从页面提取其它 ID 再查」的递归，耗时砍半
--no-extracting     # 只判存在性，不解析页面字段（最快）
--enrich            # 反向：额外请求 API/JSON 端点补充字段，更慢更全
--permute           # 从两个词生成变体（john doe → johndoe / j.doe）再全查
--parse URL         # 给一个主页，先解析出 ID 再检索
```

递归是它最有价值也最危险的功能。实测查 `soxoj` 时从 GitHub 资料里提取到 `sox0j`，又自动查了 `sox0j` 和 `Soxoj1`，最终 12 个账号里有 3 个来自递归。但这意味着一个误报会把无关的人卷进来。

### 9.3 网络控制

```bash
--timeout 30          # 单站点超时，默认 30s
--retries 2           # 临时失败重试
-n 20                 # 并发连接，默认 100
--proxy socks5://127.0.0.1:1080      # 全部流量走代理
--tor-proxy socks5://127.0.0.1:9050  # 只给 .onion 用
--i2p-proxy http://127.0.0.1:4444    # 只给 .i2p 用
--dns-resolver threaded              # DNS 全部解析失败时切换（见 issue #2688）
```

**并发是最该调的参数。** 默认 100 并发在家庭网络/代理后面会直接触发大量连接失败和站点风控。工具自己在报错时也会提示：

```
[!] Too many errors of type "Connecting failure" (5.5%). ... try `-n 10` to lower parallelism
```

实测在受限网络下 100 并发的错误率是 30% 的 Unexpected + 11% 的 Bot protection，降到 10–20 明显改善。完整跑一轮的代价不高，宁可慢一点。

### 9.4 报告

| 参数 | 产物 | 粒度 |
|---|---|---|
| `-H` / `--html` | `report_<user>_plain.html` | 合并 |
| `-M` / `--md` | `report_<user>.md` | 合并，结构化，适合喂给 LLM |
| `-P` / `--pdf` | `report_<user>.pdf` | 合并，需 pdf extra |
| `-G` / `--graph` | `report_<user>_graph.html` | 合并，D3 交互关系图 |
| `-J simple\|ndjson` | `report_<user>_simple.json` | 分用户 |
| `-C` / `--csv` | `report_<user>.csv` | 分用户 |
| `-T` / `--txt` | `report_<user>.txt` | 分用户 |
| `-X` / `--xmind` | XMind XML | 分用户 |
| `--neo4j` | `*_neo4j.cypher` | 图数据库脚本，可重复导入 |
| `-fo PATH` | 输出目录 | 默认 `results` |

组合示例：

```bash
maigret target -H -M -J ndjson -C -fo results
```

实测 `-M` 生成的 Markdown 结构：Summary（命中数、提取到的姓名/位置/性别/简介、国家标签统计、网站类别标签统计、首次出现时间）→ 逐账户详情（URL、标签、解析出的字段）→ 误报声明。字段提取质量相当高，比如从 GitHub API 拿到了 `created_at`、`fullname`、`company`、`bio`、`twitter_username`、`location`。

### 9.5 AI 摘要

```bash
export OPENAI_API_KEY=sk-...
maigret target --ai
maigret target --ai --ai-model gpt-4o-mini
```

把内部 Markdown 报告整份发给 OpenAI 兼容接口，流式输出简短调查摘要（推断的真实姓名、位置、职业、兴趣、语言、置信度、下一步线索）。`openai_api_base_url` 可指向 Azure、OpenRouter 或本地模型（Ollama/vLLM 的 OpenAI 兼容端点）。

注意两点：一是把抓到的个人资料发给第三方服务，数据合规上要先想清楚；二是它会抑制正常的站点进度输出，调试时别开。

### 9.6 输出可读性

```bash
--no-color           # 关 ANSI 颜色
--no-progressbar     # 关进度条动画（写日志/重定向到文件必备）
--print-not-found    # 打印未命中的站点
--print-errors       # 打印各类错误（403/CAPTCHA/国家封禁）
-v / -vv / -vvv      # WARNING / INFO / DEBUG 逐级详细
```

进度条是 `alive_progress` 的动画，重定向到文件会写出一堆控制字符。实测必须加 `--no-progressbar` 才能得到干净的日志。

---

## 10. 实测数据

环境：Windows 11，Python 3.13.14，家用宽带直连（无代理），目标 `soxoj`（项目作者公开账号，用作样本）。

| 项目 | 结果 |
|---|---|
| 安装耗时 | 1 分 26 秒 |
| venv 体积 | 174 MB |
| 启动加载 | 约 6 秒（5897 站点） |
| 单站点检查（GitHub） | 2–3 秒 |
| **默认全量一次（top 500）** | **149 秒**，实际检查 1527 个站点 |
| 命中 | 3 个用户名共 12 个账号 |
| 递归链 | `soxoj` → 提取 `sox0j` → 又发现 `Soxoj1` |
| 错误分布 | Unexpected 30.45% ／ Bot protection 11.39% ／ Connecting failure 5.5% |
| 报告体积 | MD 4 KB，CSV 每用户约 70 KB |

错误占比看着吓人，但主要来自三件事：站点用 Cloudflare 拦截、站点在国内不可达、100 并发触发风控。看命中结果而不是看错误率。

---

## 11. 配置文件

`settings.json` 按以下顺序加载，后加载的覆盖前面的：

```
<包目录>/resources/settings.json          # 出厂默认
/etc/maigret/settings.json                # 系统级（管理员）
~/.maigret/settings.json                  # 用户级
./settings.json                           # 当前工作目录（项目级）
```

文件不存在不算错误。常用字段：

```json
{
    "no_autoupdate": true,
    "autoupdate_check_interval_hours": 24,
    "openai_api_key": "",
    "openai_model": "gpt-4o",
    "openai_api_base_url": "https://api.openai.com/v1",
    "cloudflare_bypass": {
        "enabled": false,
        "session_prefix": "maigret",
        "trigger_protection": ["cf_js_challenge", "cf_firewall", "webgate"],
        "modules": [
            {
                "name": "flaresolverr",
                "method": "json_api",
                "url": "http://localhost:8191/v1",
                "max_timeout_ms": 60000
            }
        ]
    }
}
```

同名配置对应的命令行参数优先级更高。

### 11.1 站点数据库自动更新

启动时若距上次检查超过 24 小时，会去 GitHub 拉一个约 200 字节的元数据文件判断是否有新库；有新库就下载到 `~/.maigret/data.json` 并**整体替换**内置库（不是叠加）。

```bash
maigret user --force-update     # 立即检查
maigret user --no-autoupdate    # 跳过（Docker/CI/离线环境建议开）
maigret user --db my_sites.json # 用自定义库，同时自动跳过更新
```

下载失败或新库与本版本不兼容时回退到内置库，不会中断检索。

### 11.2 令牌缓存

Twitter 的 guest token、Vimeo 的 JWT、Weibo/Wikimapia 的 session cookie 等短期令牌会缓存在 `~/.maigret/activation.json`（权限 0600）。这是会话凭据，删掉无害（下次重新获取），但不要外传。

---

## 12. Python 库调用

官方文档这页有两个坑，实测都需要修正：`data.json` 的相对路径在 pip 安装后不成立；返回结构不是文档写的 `result["url_user"]`。

下面这段是**实测跑通**的写法：

```python
import asyncio, logging
from importlib.resources import files
from maigret import search as maigret_search
from maigret.sites import MaigretDatabase

logging.basicConfig(level=logging.CRITICAL)
log = logging.getLogger("maigret")

# 关键：用 importlib.resources 定位包内数据文件，不要写相对路径
db = MaigretDatabase().load_from_path(
    str(files("maigret.resources").joinpath("data.json"))
)

# 站点筛选与 CLI 同源
sites = db.ranked_sites_dict(names=["GitHub", "Reddit", "VK"])
# sites = db.ranked_sites_dict(top=200, tags=["coding"])
# sites = db.ranked_sites_dict(excluded_tags=["nsfw", "dating"])

async def check(username: str) -> list[dict]:
    res = await maigret_search(
        username, sites, log,
        timeout=20,
        is_parsing_enabled=True,   # 开启 socid_extractor 字段解析
        no_progressbar=True,
    )
    out = []
    for name, r in res.items():
        st = r["status"]           # MaigretCheckResult 对象
        if not st.is_found():
            continue
        out.append({
            "site": name,
            "url": st.site_url_user,
            "status": st.status,   # "Claimed" / "Available" / "Unknown"
            "ids": st.ids_data,    # 解析出的字段
        })
    return out

for item in asyncio.run(check("someusername")):
    print(item["site"], item["status"], item["url"], len(item["ids"]))
```

实测输出：

```
GitHub | Claimed | https://github.com/soxoj | 14 fields
VK | Claimed | https://vk.com/soxoj | 2 fields
```

几个必须知道的细节：

- **返回值形状不统一**。真正执行过探测的站点，返回 dict 带 `url_user`、`http_status`、`rank`；检查阶段就出错的站点只有 `status`/`site`/`url_main` 等基础键，没有 `url_user`。所以读 `r["status"].site_url_user` 比读 `r["url_user"]` 稳。
- `r["status"]` 是 `MaigretCheckResult` 对象，不是 dict。用 `.is_found()`、`.site_url_user`、`.status`、`.ids_data`。
- 已经在自己的 asyncio 事件循环里（FastAPI、aiohttp、Discord bot）时，直接 `await`，不要嵌套 `asyncio.run`。
- `is_parsing_enabled=True` 会显著变慢（每个命中站点多一次页面解析），只做存在性判断时关掉。
- 完整签名见 `maigret/checking.py` 的 `maigret()`，参数与 CLI 一一对应（`proxy`、`tor_proxy`、`i2p_proxy`、`max_connections`、`retries` 等都是关键字参数）。

---

## 13. 国内网络环境的实际约束

实测在国内直连环境下：

- **GitHub、VK、Weibo、FilmWeb、Stack Overflow 等可达**，命中正常
- **Reddit、Twitter/X、Instagram、Facebook 及大部分欧美论坛不可达**，表现为 `Unexpected` / `Connecting failure`
- 一批站点返回 Cloudflare 挑战页，记为 `Bot protection`

结论：不挂代理也能用，但命中面会窄一大截，且错误统计会严重失真。想拿到接近完整的覆盖，必须配代理：

```bash
maigret user --proxy socks5://127.0.0.1:1080 -n 20 --no-progressbar
```

配了代理之后并发要同步降下来（`-n 10~20`），否则代理端先被打爆，反而全是错误。

如果所有站点都报 DNS 错误，切解析器：

```bash
maigret user --dns-resolver threaded
```

---

## 14. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `Too many errors of type "Unexpected"` | 并发过高 / 网络受限 / 站点改版 | `-n 10`，加 `--print-errors` 看具体 |
| `Too many errors of type "Bot protection"` | Cloudflare / 风控 | 换 IP，或配 FlareSolverr（见下） |
| `Connecting failure` | 站点不可达 | 配代理；确认不是 DNS 问题 |
| 所有站点 `Could not contact DNS servers` | aiodns/c-ares 与系统 DNS 冲突（issue #2688） | `--dns-resolver threaded` |
| 大量 403 | 站点封锁数据中心 IP | 换出口 IP，降并发 |
| PDF 报错 `require the optional 'pdf' extra` | 缺依赖 | `pip install "maigret[pdf]"` |
| Linux 上 PDF 报 cairo 相关错误 | 缺系统图形库 | `apt install libcairo2-dev libxml2-dev libxslt1-dev` |
| 日志里全是控制字符 | 进度条动画 | 加 `--no-progressbar` |
| 站点库加载失败 | 网络更新中断 | `--no-autoupdate` 用内置库 |
| Web 页面点查询后 `This site can't be reached` | 服务进程已退出（黑窗口被关掉），或浏览器还是旧标签页 | 重新双击图标；旧标签页必须刷新 |

### Cloudflare 绕过（实验性）

需要本地跑一个 FlareSolverr：

```bash
docker run -d -p 8191:8191 --name flaresolverr ghcr.io/flaresolverr/flaresolverr:latest
maigret user --cloudflare-bypass
```

或在 `~/.maigret/settings.json` 里设 `cloudflare_bypass.enabled = true` 永久开启。只对站点库里标记了 `cf_js_challenge` / `cf_firewall` / `webgate` 的站点生效。官方明确标注该功能实验中，配置结构可能随时变。

### 站点判定出错时

```bash
maigret --self-check                 # 用已知占用/未占用的用户名校验每个站点的判定逻辑
maigret --self-check --auto-disable  # 自动禁用判定失效的站点
maigret --self-check --diagnose      # 输出每个失败站点的详细诊断
```

误报的典型来源是「soft 404」——站点对任意用户名都返回 200。这是这类工具的通病，看到可疑命中先手工打开链接确认。

---

## 15. 使用边界

- 只查公开信息，不碰需要登录/绕过权限的内容
- 递归检索会把无关的人卷进来，对被误伤的对象做任何记录前先核实
- 抓取到的姓名、位置、联系方式属于个人数据，GDPR / 个人信息保护法都适用；做批量检索或长期留存前先确认合法性基础
- 报告里自带误报声明，转交他人时不要删掉
- 别把它架成公开服务——等于给所有人提供一个匿名扫描出口

---

## 附：常用命令速查

```bash
# 快速摸底
maigret username

# 完整报告套件
maigret username -H -M -J ndjson -C -fo results

# 限定范围，降低并发
maigret username --tags coding --top-sites 300 -n 20 --no-progressbar

# 只判存在性，不解析不递归
maigret username --no-extracting --no-recursion

# 多用户名批量
maigret user1 user2 user3 -a --input-file extra.txt

# 从主页反查
maigret --parse https://example.com/profile/12345

# 起 Web 界面
maigret --web 5000

# 自查站点库
maigret --self-check --auto-disable

# 排查错误
maigret username --print-errors -vvv -n 5
```
