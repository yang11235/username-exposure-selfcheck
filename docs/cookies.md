# 用浏览器扩展导出 Cookie

## 为什么需要这一步

有些站点必须登录才能判定账号是否存在（微博、知乎、小红书、抖音、快手、脉脉、即刻）。
程序有两种拿登录态的方式：

| 方式 | 前提 | 现状 |
|---|---|---|
| 从本机 Chrome 直接读（CDP） | 浏览器要带调试端口启动 | **Chrome 157+ 基本走不通**，见下 |
| 手动导出 cookies.txt 再粘进去 | 装一个导出扩展 | **推荐**，不受浏览器版本限制 |

### 为什么 CDP 走不通

Chrome 157 起，默认用户目录上禁止开远程调试：

```
DevTools remote debugging requires a non-default data directory.
Specify this using --user-data-dir.
```

即使显式 `--user-data-dir` 指向默认目录也一样被拒（实测本机 Chrome
157.0.8103.52）。而且：

- 指向**临时目录**能连上，但那里没有你的登录态，读到的是访客 cookie
- 把 Cookies 库**复制**到别的目录也没用：App-Bound Encryption（v20）解密失败，
  cookie 会被直接丢弃

这是 Chrome 的安全设计（防恶意软件窃取登录态），不是可以绕过的配置问题。
所以手动导出是当前唯一稳定的路子。

## 装哪个扩展

推荐 **[Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)**。

选它的理由：本地解析、不联网上传（名字里的 LOCALLY 就是这个意思），
导出的是标准 Netscape 格式，正好是本程序吃的格式。

> 不要用会把 cookie 传到服务器的那种扩展。cookie 等于你的登录态，
> 上传等于把账号交出去。

## 导出步骤

**一次导出全部，不用逐站导。**

在浏览器里正常登录好所有想用的站（微博、知乎、百度…），然后：

1. 点扩展图标
2. 选 **Export all cookies** /「导出全部」（不是「导出当前站点」）
3. 得到一个 `cookies.txt`，里面包含所有站点的 cookie

不同扩展的入口名字略有差异，找 "Export all" / "全部" 即可。如果扩展只能导出当前站，
就切换标签页再导一次——但大多数都支持一次性导全部。

> 一次导全部是安全的：程序按域名隔离，`weibo.com` 的 cookie 绝不会发给 `github.com`。
> 实测验证过（见 `tests/test_cn_browser_cookies.py::test_cookies_do_not_leak_to_unrelated_hosts`）。

## 导入到本程序

打开 Web 界面 → 右上角设置（齿轮）→「Cookie」区域。

**推荐用「上传 cookies.txt」**：全量导出通常是几百 KB、上千条，粘进文本框会让
浏览器明显卡顿。选文件更省事，也不会有复制不全的风险。

粘贴文本框仍然保留，适合两种情况：只导一两个站、或者从 F12 手抄。

导入时默认勾选 **「只保留本工具会用到的域名」**，会丢掉 google-analytics、
doubleclick、cnzz 这类纯追踪 cookie（通常占全量的一半以上）。**登录凭据不受影响**。

> 勾选后如果文件里只剩追踪 cookie，会明确告诉你"解析到的 N 条全是追踪域名"，
> 而不是含糊地说格式错误。

点保存。程序会自动识别这是 Netscape 格式并逐域名导入，
同名域名整体替换（避免新旧登录态混在一起）。

保存后会提示你**解锁了哪些站**：

```
已导入 2 个域名的 14 条 Cookie（已丢弃追踪类域名）；可判定的站：微博。
```

如果是这样：

```
已导入 1 个域名的 2 条 Cookie，但里面没有 百度、微博 的登录凭据——
这些站仍然判不出来。扩展导出时记得包含 httpOnly cookie。
```

说明导出时漏了关键 cookie，往下看。

## 关键：必须包含 httpOnly cookie

登录凭据大多是 `httpOnly` 的（JS 读不到，扩展能读到）：

| 站点 | 登录凭据 | 只导出普通 cookie 会怎样 |
|---|---|---|
| 百度 | `BDUSS` | 只有 `BAIDUID`（访客标识），查不出任何个人信息 |
| 微博 | `SUB` | 微博接口返回登录墙，判 BLOCKED |
| 知乎 | `z_c0` | 403 |
| B站 | `SESSDATA` | 只能查到公开信息 |

Get cookies.txt LOCALLY 默认就包含 httpOnly cookie，导出文件里它们长这样
（`#HttpOnly_` 前缀是 Netscape 格式的约定）：

```
#HttpOnly_.baidu.com	TRUE	/	TRUE	1799999999	BDUSS_BFESS	xxxxx
.baidu.com	TRUE	/	FALSE	1799999999	BDUSS	xxxxx
```

> 老版本的本程序会把 `#HttpOnly_` 开头的行当成注释丢掉，导致"导入成功但登录态是空的"。
> 已修复（2026-11-15），现在这些行会被正确解析。

## 验证是否生效

导出导入之后，有两种确认方式：

1. **看保存后的提示**，是否列出了目标站
2. **跑一次检索**，看结果页的「本次未能判定」里，该站是否还报"需要登录"
3. **设置里的「检测」按钮**（这是读浏览器的，与 cookies.txt 无关，
   但能告诉你浏览器里到底登录没登录）

## 安全提醒

- `cookies.txt` 保存路径是 `~/.maigret/cookies.txt`，等于**明文登录态**。
  不要把它放进网盘同步目录、不要提交到 git（`.gitignore` 已包含）
- 导出后如果不再需要，删掉该文件即可；或让 cookie 在站点上过期（退出登录）
- 分享截图前注意别把设置页里的 Cookie 内容框进去

## 各站有效期

cookie 会过期，过期后该站会退回"判不出来"。重新导出一次覆盖即可。
微博的 `SUB` 通常能撑几个月，知乎的 `z_c0` 较短。
