# 中文站点规则编写指南

这份文档只回答一个问题：**怎么给一个中文站点写一条能用的规则。**

背景：WhatsMyName 691 个站点、跑了 11 年，2026-11-09 归档关闭，作者原话是
"维护数百个站点检测超出承载力"。**站点数不是资产，是负债**——9,535 个站里若有
3,000 个常年失效，用户看到一堆"未知"，信任度反而低于 300 个全健康的站。
所以本项目的立场是：宁可少一条规则，也不要一条会误判的规则。

---

## 1. 四字段协议

一条规则只由四个值组成：

| 字段 | 含义 |
|---|---|
| `e_code` / `e_string` | **存在侧**：响应满足这两个条件，说明账号存在 |
| `m_code` / `m_string` | **不存在侧**：响应满足这两个条件，说明账号不存在 |
| `m_empty` | 可选。声明「`m_code` 状态码 + 0 字节 body」就是不存在，见 §4-A′ |

规则是：**状态码与字符串必须同时命中**，不是"或"。这就是四字段协议。

判定顺序不可调整（`maigret/cn/verdict.py:judge`）：

```
规则可用性 → 空 body → 429 → 403/503 → 强风控特征 → 登录墙 → 验证码
→ 四字段判定（弱风控特征在这里结合证据形态处理）
```

为什么"先判被打断，再判存在性"必须排在最前：**反过来必然产生假阳性。**
风控页、登录页、验证码页都是 HTTP 200，页面上还可能带着站点自己的正例串
（知乎的 `blocked.html` 里就含 `"url_token"`）。如果先判存在性，风控页会被
判成"账号存在"——这是最坏的一种误报，因为用户会以为自己暴露了。

---

## 2. 写一条规则的五步

### 第一步：找到能区分两种响应的地址

优先用**机器接口**而不是给人看的页面：接口返回 JSON，字段稳定，改版频率远低于前端模板。

```json
"uri_check":  "https://api.example.cn/user?uid={account}",   // 机器打
"uri_pretty": "https://example.cn/u/{account}"               // 报告里给人看
```

`uri_pretty` 不是可选项：不给的话报告里会展示一个 API 地址，用户看不懂那是什么。

### 第二步：看真实响应

```bash
python -m maigret <known_username> --site Example --dump-response --no-recursion
```

**没有这一步就是在盲猜。** `e_string` 必须逐字出现在响应里，多一个空格、少一个引号都不算命中。

### 第三步：拿两个样本各打一次

`known` 填一个真实存在的用户名，`known_absent` 填一个几乎不可能存在的：

```json
"known": ["real_user"],
"known_absent": ["this_user_should_not_exist_9x8f7"]
```

然后跑自检：

```bash
python utils/cn_selfcheck.py --site Example
```

判定标准与 `verdict.judge` 完全一致，不另立一套：

```
known        → 期望 CLAIMED
known_absent → 期望 AVAILABLE
```

**这一步不能跳过。** 四字段能不能分开两种响应，读 JSON 判断不出来——
两个串互不为子串，也可能在同一种响应里同时出现（见下面第 3 节的掘金）。

### 第四步：填 `extract`（可选）

只声明这个站**确实**有的字段，抽不到不是问题，抽错才是问题：

```json
"extract": ["nickname", "avatar", "ip_location"]
```

不声明 `extract` 的站点，扫描时不会做任何抽取。这是刻意的：抽错的代价远高于抽不到。

### 第五步：写 `_note`

`_note` 是给下一个人看的。写清楚：**这条规则为什么这么写、踩过什么坑、什么时候验证的。**

```json
"_note": "e_string 不能用上游的 \"user_name\":——「不存在」响应里是 \"user_name\":\"\"，照样命中，会与 m_string 双命中（自检实测 rule_ambiguous）。真实用户必有头像 URL。2026-11-14 验证。"
```

没有 `_note` 的规则，半年后没人敢改。

---

## 3. 六类必错写法

这六条全部来自实测踩坑，不是理论推演。`schema.py` 会在入库时拦掉大部分，
但知道"为什么"比知道"会被拦"重要。

### ① 判据串太短（< 3 字符）

```json
"m_string": "[]"        // 2 个字符
```

任何含 `[]` 的页面都会被判成命中。上游 GitLab / Google Plus 就是这种写法。

**改法**：留空改用 `status_only`（置信度固定 0.5），或者换一个更长的串。

### ② 两侧判据互为子串

```json
"e_string": "user_name",
"m_string": "user_name\":\"\""     // 包含 e_string
```

`e_code == m_code` 时，两种响应会**同时命中两侧**，判定器返回 `RULE_AMBIGUOUS`。
这是设计上的选择：规则坏了就说规则坏了，绝不猜一个结果。

### ③ 判据串只是用户名占位符

```json
"e_string": "{account}"
```

只能证明"页面回显了用户名"，证明不了账号存在——大多数站点的错误页也会回显。
`schema.py` 会直接拒绝。

### ④ `e_code == m_code` 却只给了一侧字符串

```json
"e_code": 200, "m_code": 200,
"e_string": "", "m_string": "该用户不存在"
```

看起来"没有错误文案就是存在"，实际不行：存在侧会退化成 `status_only`，
于是**任何** 200 页面（包括错误页）两侧同时命中 → `RULE_AMBIGUOUS`。
不是"可能出错"，是"每条规则每次查询都出错"。

**改法**：补一条只在真实用户页出现的串。补不出来就把 `valid` 设成 `false`。

### ⑤ 把站点页脚的东西当成用户信息

实测两个案例：

- **Gitee**：页面全文唯一的邮箱是站点客服 `client@oschina.cn`。按全文匹配会把客服邮箱写进每个用户的报告。
- **Gitee**：`og:image` 是 `static/images/logo_themecolor_circle.png`，即站点 logo。当成头像会让全站所有用户 pHash 相同，聚类把所有人合成一个人。

现在的防护：邮箱必须出现在用户名 200 字符内；头像优先 `class/id` 带 `avatar` 的 `<img>`，`og:image` 只作兜底且要过站点资源名黑名单。**但新站点仍可能有新花样**——声明 `extract` 前先肉眼看一下抽出来的是什么。

### ⑥ 把风控页当成正常页

小红书实测（2026-11）：状态码 200、92KB、标题「安全限制」、正文只有一句
「当前网络存在风险，请切换可靠网络后重试 300012」。

这类页面的特征是"体积正常、内容是拦截文案"。`verdict.py` 已经收了这类特征
（`_BLOCK_STRONG` 强特征无条件优先），但**新站点会有新文案**。遇到"命中但感觉不对"时，
先 `--dump-response` 看正文，别急着改规则。

---

## 4. 六种站型怎么写

### A. 有 JSON 接口（最好写）

```json
"uri_check": "https://api.example.cn/v1/user/{account}",
"e_code": 200,
"e_string": "\"avatar_url\":\"http",
"m_code": 200,
"m_string": "\"user_id\":\"0\""
```

存在侧挑一个"真实用户必有、空账号必无"的字段。头像 URL、粉丝数、注册时间都属于这类。

接口返回的**不是 HTML**时，四字段仍然照用，只是 `extract` 抽不动东西——那些字段是
按 HTML 写的。这类站点交给 `maigret/cn/platforms/` 里的平台抽取器（按 host 识别，
只对已知接口做 JSON 解析）：

| 平台 | 识别 | 取到的字段 |
|---|---|---|
| Bilibili | `api.bilibili.com/x/web-interface/card` | 昵称、签名、头像 |
| 网易云音乐 | `music.163.com/api/v1/user/detail` | 昵称、签名、头像 |
| 掘金 | `api.juejin.cn/user_api/v1/user/get` | 昵称、简介、公司、头像 |
| 百度贴吧 | `tieba.baidu.com/home/get/panel` | 昵称、性别、粉丝数、账号年龄、头像 |

**微博 / 知乎没有抽取器**：未登录访问分别返回 `403 {"error":"Forbidden"}` 和
`need_login`，拿不到样本。凭印象写路径等于给用户看一个永远为空或永远取错的字段，
不如在文档里写明"这条拿不到"。

### A′. JSON 接口用「0 字节 body」表示不存在

有一类接口不返回任何错误串，它用**整条记录**表示存在、用**空 body** 表示不存在：

```json
"uri_check": "https://example.cn/api/user?un={account}&ie=utf-8",
"e_code": 200,
"e_string": "\"raw_name\"",
"m_code": 200,
"m_empty": true
```

这类接口的典型形态（百度贴吧的 `i/sys/user_json` 就是这样，2026-11-15 实测）：
`maigret` → 200 / 394 B 含 `raw_name`；`noonewouldeverusethis7` → 200 / **0 B**。

这种站写不出 `m_string`——没有反例串可抄。不声明 `m_empty` 时，判定层遇到
"2xx + 空 body"会返回 `UNKNOWN / empty_body`（那是 WAF、反向代理、SPA 空壳的
常态，一律当"不存在"会批量误报），于是这条规则永远只能输出"未判定"。
声明 `m_empty` 就是把"这个接口的空 body 有含义"这件事告诉判定层。

三条约束，缺一不可：

1. **只在状态码等于 `m_code` 时采信。** 403 被网关改写成 200 空响应是常见形态，
   那正是把"被拦住"说成"你没有这个账号"的最短路径。
2. **默认关。** 不写这个字段的站点行为完全不变——开关在站点上而不是判定层里，
   因为"空 body 有含义"只有规则作者知道，判定层无从推断。
3. **存在侧仍然必须有 `e_string`。** `m_empty` 只放行反例侧；两侧都没有字符串
   的规则对"存在"毫无证据，schema 照旧拒绝。

证据系数是 `empty_body = 0.85`（低于 `unique_string` 的 0.9）：它确实是站点主动
给出的结构信号，但同一个形状也可能是网关造出来的。

**优先找带错误码的接口。** `m_empty` 是兜底手段，不是首选：只要能找到
"存在 `"no":0` / 不存在 `"data":[]`"这种带结构的接口，就该用四字段正常判，
顺带还能抽资料。百度贴吧就是这么从 `m_empty` 换成 `home/get/panel` 的——
同一个站往往有好几个接口，`m_empty` 只说明"没得选"，不说明"选得好"。

### B. 只有 HTML 页面

从 `og:title`、`<title>`、结构化数据里找。注意 `og:title` 经常带站点后缀
（`Wizzer (wizzer) - Gitee.com`），抽取层有 `clean_title()` 负责去掉。

### C. 需要登录 / 强风控

```json
"valid": false,
"requires": ["login"],
"_note": "未登录访问返回登录页，无法判定。"
```

**不要为了让它"能判"而放宽判据。** 标成 `valid: false` 的站点在扫描时会返回
`UNKNOWN / login_required`，用户看到的是"这里没查"，这是诚实的；
而放宽判据得到的是"这里查了，结果是错的"。

### D. 需要浏览器 / 签名

```json
"executor": "browser",
"signature": "xhs_xs",
"protection": ["captcha", "signature"]
```

浏览器执行器有两条硬约束（`executors/browser.py`）：只对**公网地址**生效，
且每次运行有**升级次数上限**（默认 50）。原因是测试和本地调试里
`localhost` 被判 BLOCKED 说明我们的规则有 bug，不该去驱动用户真实的 Chrome。

### E. 主页地址拼不出来 → 站内搜索

有些站点的个人主页是数字 ID（B 站 `space.bilibili.com/12345`、微博 oid、
贴吧内部 id），`uri_check` 里写 `{account}` 拼不出地址，只能先搜索。

```json
"executor": "search",
"uri_check": "https://api.example.com/search?keyword={account}",
"search_handle_pattern": "\"mid\":(?P<id>\\d+),\"uname\":\"(?P<handle>[^\"]+)\"",
"search_empty_marker": "\"numResults\":0",
"search_profile_url": "https://space.example.com/{id}",
"bootstrap_url": "https://www.example.com/"
```

五个字段的含义：

| 字段 | 作用 |
|---|---|
| `uri_check` | **搜索地址**，必须带 `{account}`。这类站点没有别的可用地址 |
| `search_handle_pattern` | 从结果里取句柄的正则。必填，且必须有捕获组 |
| `search_empty_marker` | 页面明确表示"结果为 0"的文案。填了才可能判"不存在" |
| `search_profile_url` | 命中后用站内 ID 拼主页地址的模板，`{id}` 会被替换。可选 |
| `bootstrap_url` | 正式搜索前先访问一次的地址，用来拿 Cookie。可选 |

**正则里用具名分组，不要依赖组的顺序。** 不写名字时"第 1 个捕获组"是句柄，但 JSON
接口里 ID 常常排在昵称前面（B 站就是 `"mid":123,"uname":"x"`），这时两组一颠倒，
规则会从"比对昵称"悄悄变成"比对数字 ID"——不报错，只是从此永远对不上。

| 分组名 | 含义 |
|---|---|
| `(?P<handle>...)` | 句柄。写了就用它，不再看第 1 组 |
| `(?P<id>...)` | 该条结果的站内 ID，交给 `search_profile_url` 拼主页 |

**`bootstrap_url` 不是可选优化，是能不能判的前提。** B 站搜索接口要首页下发的
`buvid3` Cookie：不预热时第一次能过、之后一律 412（实测连续 3 次）。412 映射成
`BLOCKED`，所以漏了这个字段的表现是"规则看着没问题，结论永远是未判定"。

**`search_profile_url` 是为了报告里给得出链接。** 搜索接口通常只回一个数字 ID，
没有可直接展示的地址；不写模板就只能给出搜索页链接，而搜索页上挂的是**别人**的
账号。宁可少给一个链接，也不给一个点开是别人主页的链接。

判据只有一条：**结果里有没有与目标逐字相同的句柄**。

这不是偷懒，是唯一安全的判据。搜索结果页**必然回显你搜的那个词**，所以
`e_string = "{account}"` 这种写法在这里会把每一次搜索都判成"命中"——每个用户
都"注册过"每一个站点，而且错得极其自信。所以搜索站点**不走四字段校验**，
`schema.py` 校验的是上面这几个字段。

结论的三种去向，注意第二种：

| 情况 | 结论 |
|---|---|
| 找到逐字相同的句柄 | 命中（置信度封在低档：搜索只证明"存在一个叫这个名字的账号"） |
| 结果区渲染了，但没有同名句柄 | **未判定**，不是"不存在"。搜索有分页、有排序，多数站点还按昵称而非句柄匹配 |
| 页面明确写着"没有找到" | 不存在 |

**关于 B 站 / 微博 / 贴吧**：三家的搜索页对未登录用户都不好用，但原因不同。

- **B 站**能判：`api.bilibili.com/x/web-interface/search/type` 匿名可用，
  只是要预热拿 Cookie，且返回 412 时必须先按状态码下结论（错误 JSON 里一个结果
  条目都没有，按正文判会得出"没有同名账号"）。
- **微博 / 贴吧**仍不可用：登录墙 + 前端渲染，拿不到可解析的结果区。它们的正确
  输出是「未判定」而不是「不存在」——把"被拦住"说成"没注册"才是真的做坏了。

站内搜索站点**不做 ID 抽取**：手上这一页是搜索结果页，上面挂着的是别人的主页链接
和昵称，在它上面抽 `ids` 等于把别人的账号信息记到这个用户名头上。

### F. 冷门站点：写进库里，但不进默认扫描

默认扫描（前 500 站）会并入人工策展的中文站，但**不是每一条都值得每次运行**。
`default_include: false` 就是给这种情况用的：

```json
"default_include": false,
"_note": "冷门社区，真实自查里几乎不出现，只在 --site 点名时查。"
```

三个字段容易混，区别是**谁在什么时候看它**：

| 字段 | 谁看 | 含义 |
|---|---|---|
| `valid` | 判定层 | 这条规则能不能用来下结论。`false` 的站点扫到了也只给「未判定」 |
| `default_include` | 装配层 | 作者的态度："这条值得每次都查吗"。`false` 时只响应 `--site` / `--tags` |
| `force_include` | 合并层 | 运行期的结果："这一条已经确定要查了"，合并时不许被去重丢掉 |

`default_include` 缺省是 `true`。漏写不会报错，只会让站点静默地留在默认清单里——
所以摘站时务必显式写 `false`，不要靠"删掉这个字段"来暗示。

摘站的判据是**实测**，不是感觉：连着几次真实自查里都没出现过、也没有用户拿它问过，
才值得摘。摘掉之后 `--site` 仍然可以点名查，功能没有损失，只是不再替所有人付这次
请求的时间。



---

## 5. 从别的项目导入规则

三个生态的格式都能转（`utils/cn_import.py`）：

```bash
# WhatsMyName
python utils/cn_import.py --from wmn wmn-data.json -o cn-imported.json

# Sherlock
python utils/cn_import.py --from sherlock data.json --site Zhihu

# 上游 Maigret（本项目的基座）
python utils/cn_import.py --from maigret maigret/resources/data.json --dry-run
```

输出的统计分三档，含义不同：

| 档位 | 含义 |
|---|---|
| 可直接使用 | 四字段齐全，但**还没实测** |
| 待补 | `valid: false`，`_note` 里写了缺什么 |
| 不合格 | `schema.py` 拒绝，原因会打印出来 |

导入后**必须**跑 `utils/cn_selfcheck.py`。转换只能保证"格式对"，
保证不了"能判"——后者只有真打两次才知道。

导入时有两处刻意的拒绝：

- **Sherlock `errorType: message`** 只有不存在侧判据（它判"错误文案出现就不存在"），
  存在侧靠的是"没出现"这个否定条件，四字段表达不了。会标 `valid: false` 并说明要补什么。
- **Sherlock `errorType: response_url`** 判据在最终跳转 URL 上，而四字段只看状态码和响应体。
  降级成 `status_code` 会把"跳回首页"当成"用户存在"。

---

## 6. 提交前自检清单

```
[ ] python utils/cn_selfcheck.py --site <名字>  → PASS
[ ] known 样本确实存在，known_absent 样本确实不存在
[ ] e_string / m_string 都是逐字从真实响应里复制的
[ ] e_code == m_code 时，两侧字符串都有
[ ] 两个判据串不互为子串
[ ] 判据串长度 ≥ 3，且不含 {account}
[ ] 声明了 extract 的话，确认抽出来的不是站点 logo / 客服邮箱
[ ] 接口型站点：确认 platforms/ 里的路径对得上，或者说明为什么没有抽取器
[ ] 站内搜索站点：正则用了 (?P<handle>)，不靠组的顺序
[ ] 站内搜索站点：确认是否需要 bootstrap_url（连打三次会不会 412）
[ ] _note 写了为什么这么写、什么时候验证的
```

自检脚本的退出码只有"规则失效"才返回 1。被风控、需要登录的站点算**环境受限**，
不算失败——把环境问题也算失败的话，每日任务会永久变红，而"永久变红"和"没有任务"等价。

---

## 7. 相关文件

| 文件 | 作用 |
|---|---|
| `maigret/cn/data/sites_cn.json` | 人工策展的站点规则（强制进入默认扫描） |
| `maigret/cn/data/overrides.json` | 对上游规则的局部修正（不强制纳入扫描） |
| `maigret/cn/schema.py` | 入库校验，六类必错写法在这里被拦 |
| `maigret/cn/verdict.py` | 判定引擎，判定顺序在这里 |
| `maigret/cn/extract.py` | 证据抽取，`extract` 字段驱动 |
| `maigret/cn/platforms/` | 接口型站点的 JSON 抽取器（B 站 / 网易云 / 掘金） |
| `maigret/cn/executors/search.py` | 站内搜索执行器（E 型站点），判据是"逐字同名句柄" |
| `maigret/cn/identity.py` | 跨站身份归并，报告和 Web 结果页共用 |
| `utils/cn_selfcheck.py` | 规则自检（真打两次） |
| `utils/cn_import.py` | 多格式导入/导出 |
| `docs/cn-health.md` | 每日自检生成的存活率看板 |
