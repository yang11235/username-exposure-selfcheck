#!/usr/bin/env python3
"""中文站点规则自检（文档 §6.2）。

为什么必须真跑，而不是靠"值是从上游抄来的"来担保：

    四字段协议要求「存在」和「不存在」两种响应能被 `e_string`/`m_string`
    区分开。这个性质**不能靠读 JSON 判断**——两个串互不为子串，也可能在
    同一种响应里同时出现。掘金就是活例子：`"user_name":` 和 `"user_id":"0"`
    互不包含，但"用户不存在"的响应里 `"user_name":""` 照样含有前者，
    于是每次判定都是 RULE_AMBIGUOUS。

    只有拿 known / known_absent 两个样本各打一次，才知道规则到底能不能判。

判定标准（与 verdict.judge 一致，不另立标准）：
    known        → 期望 CLAIMED
    known_absent → 期望 AVAILABLE

用法：
    python utils/cn_selfcheck.py                # 全部策展站点
    python utils/cn_selfcheck.py --site Juejin  # 只查一个
    python utils/cn_selfcheck.py --write-doc    # 同时更新 docs/cn-health.md
"""

import argparse
import asyncio
import json
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _bootstrap_cn_namespace() -> None:
    """让 `maigret.cn.*` 在**只装了 aiohttp** 的环境里也能导入。

    背景：`maigret/__init__.py` 会 import `checking`，后者一路拉进
    aiodns / alive_progress / aiohttp_socks / socid_extractor / curl_cffi，
    `report` 还要 jinja2、xmind。这些是完整运行时的依赖，自检一个都用不到。

    CI 只装 aiohttp 是刻意的（见 workflow 里"安装依赖"一段的注释）：
    装全量会引入 xhtml2pdf 这类带系统库的包，让这个每日任务因为无关的依赖
    问题变红。但这条注释只在 workflow 里写着，脚本本身并不知道——
    于是 `from maigret.cn.model import ...` 先触发了包初始化，
    在 CI 里必然 ImportError，自检一次都没跑起来过。

    这里给 `maigret` 注册一个不执行 `__init__.py` 的轻量命名空间，
    `maigret.cn` 及其子模块照常按路径导入（它们都是纯标准库实现）。
    本地装了全量依赖时 `import maigret` 会成功，直接复用，行为不变。
    """
    try:
        import maigret  # noqa: F401
        return
    except Exception:  # 缺模块、缺系统库都走降级，不让自检死在这一步
        pass
    # 导入失败时 CPython 已把半初始化的模块从 sys.modules 摘掉，不会残留。
    pkg = types.ModuleType("maigret")
    pkg.__path__ = [str(ROOT / "maigret")]
    sys.modules["maigret"] = pkg


_bootstrap_cn_namespace()

from maigret.cn.model import Site  # noqa: E402
from maigret.cn.overlay import load_sites  # noqa: E402
from maigret.cn.verdict import Status, judge  # noqa: E402

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 每个站点两次请求之间隔一下：这批站大多有风控，打太密会拿到假阴性
DELAY_SECONDS = 1.5
TIMEOUT_SECONDS = 20


async def fetch(session, url):
    try:
        async with session.get(url, timeout=TIMEOUT_SECONDS) as resp:
            body = await resp.text(errors="replace")
            return resp.status, body
    except Exception as exc:  # 网络错误也要当成"判定失败"上报，不能静默跳过
        return None, f"__ERROR__ {type(exc).__name__}: {exc}"


async def check_site(session, site: Site):
    """对单个站点跑 known / known_absent 两个样本。"""
    # 本脚本验的是**四字段协议**（e_string/m_string 能不能把两种响应分开），
    # 只有 http 执行器是这套判据。search / browser 执行器走的是
    # executors/search.py 里"结果里有没有逐字相同的句柄"那套逻辑，
    # 它们的站点通常也没有 e_code/m_code —— 交给 judge() 只会得到
    # RULE_MISSING，被算成"规则失效"，每天把 CI 顶红。
    # 这类站点在这里显式跳过，而不是伪造一个"通过"。
    if site.executor != "http":
        return {
            "site": site.name,
            "result": "SKIP",
            "detail": f"executor={site.executor}，不走四字段判据，本脚本不适用",
        }

    if not site.known or not site.known_absent:
        return {
            "site": site.name,
            "result": "SKIP",
            "detail": "缺少 known/known_absent 样本对",
        }

    if "{account}" not in site.uri_check:
        return {"site": site.name, "result": "SKIP", "detail": "uri_check 无占位符"}

    rows = []
    for account, expected in ((site.known[0], Status.CLAIMED), (site.known_absent[0], Status.AVAILABLE)):
        url = site.uri_check.replace("{account}", account)
        status, body = await fetch(session, url)
        verdict = judge(SimpleNamespace(status=status, text=body), site, username=account)
        ok = verdict.status == expected
        rows.append(
            {
                "account": account,
                "expected": expected.value,
                "got": verdict.status.value,
                "reason": verdict.reason.value if verdict.reason else "",
                "confidence": verdict.confidence,
                "http": status,
                "ok": ok,
                "url": url,
            }
        )
        await asyncio.sleep(DELAY_SECONDS)

    failed = [r for r in rows if not r["ok"]]
    if not failed:
        return {"site": site.name, "result": "PASS", "rows": rows, "detail": ""}
    return {
        "site": site.name,
        "result": "FAIL",
        "kind": classify(failed),
        "rows": rows,
        "detail": _diagnose(failed[0], site),
    }


# 这些原因说明"我们没拿到可判定的响应"，而不是"规则写错了"。
# 两者必须分开统计：否则维护者会去改一条本来正确的规则，
# 而真正的问题（需要 Cookie / 需要浏览器执行器 / 网络不通）永远没人处理。
ENV_REASONS = {"blocked", "captcha", "login_required", "empty_body", "http_error", "timeout"}


# 反爬/拒绝服务的状态码。看到这些码说明**请求被挡了**，页面内容不可信，
# 改规则也修不好——归 ENV。
#
# 468 是 SegmentFault 的反爬码（非标准，但在中文站里不是孤例）。
# 实测 2026-11-14：SegmentFault 对 known/known_absent 都返回 468，
# 自检把它算成"规则失效"，于是每日 CI 为一件没人能改规则修好的事变红。
BLOCK_STATUS_CODES = {401, 403, 405, 418, 429, 468, 503, 520, 521, 522, 523, 524}


def classify(rows):
    """把失败分成 RULE（规则问题）与 ENV（环境问题）。

    分界线是"改了 sites_cn.json 能不能修好"：
        ENV  —— 被风控、被限流、需要登录、请求失败。改规则是白费功夫。
        RULE —— 请求通了、状态码是预期的，但判据串对不上。这才是规则过期。

    这个划分决定了 CI 红不红，写错的代价是把告警变成噪音。
    """
    if not rows:
        return "ENV"
    if any(r["http"] is None for r in rows):
        return "ENV"
    if any(r["reason"] in ENV_REASONS for r in rows):
        return "ENV"
    # 反爬状态码：请求根本没被当成正常访问，正文内容不可信。
    if any(r["http"] in BLOCK_STATUS_CODES for r in rows):
        return "ENV"
    # 5xx 一律当环境问题：服务端自己挂了，和规则无关。
    if any(isinstance(r["http"], int) and 500 <= r["http"] < 600 for r in rows):
        return "ENV"
    return "RULE"


def _diagnose(row, site):
    """把失败翻译成人能直接动手改的话。"""
    reason = row["reason"]

    # 反爬状态码优先说明。否则会输出"两侧都不命中"这种指向规则的措辞，
    # 维护者照着去改 e_string/m_string，改完再跑还是 468——白折腾。
    if isinstance(row.get("http"), int) and row["http"] in BLOCK_STATUS_CODES:
        return (
            f"被反爬拦截（HTTP {row['http']}），页面内容不可信，不是规则问题；"
            f"需要 Cookie 或浏览器执行器"
        )

    if reason == "rule_ambiguous":
        return (
            f"双命中：e_string={site.e_string!r} 与 m_string={site.m_string!r} "
            f"在同一种响应里同时出现，规则必须换一个只在单侧出现的串"
        )
    if reason == "structure_changed":
        if not site.e_string and not site.m_string:
            # status_code 型规则：判据就是状态码，报出实际码比空说"过期"有用得多
            return (
                f"状态码不符：期望 e={site.e_code}/m={site.m_code}，"
                f"实际 HTTP {row['http']}"
            )
        return (
            f"两侧都不命中：e_string={site.e_string!r} / m_string={site.m_string!r} "
            f"与当前页面不符（实际 HTTP {row['http']}）"
        )
    if reason == "blocked":
        return "被风控拦截，无法判定（不是规则错误，需要 Cookie 或浏览器执行器）"
    if reason == "login_required":
        return "需要登录才能看到内容"
    if reason == "empty_body":
        return "响应体为空"
    if reason == "rule_missing":
        return "规则被标记为不可用（valid=false）"
    if row["http"] is None:
        return "网络请求失败"
    return f"期望 {row['expected']}，实际 {row['got']}（{reason or '无原因'}）"


# 是否处于"机器可读输出"模式。开启后人类可读的进度一律走 stderr，
# 保证 stdout 上只有 JSON——否则 `> out.json` 拿到的是一堆进度行加一段
# JSON，`json.load()` 会在第一个字符就报错（CI 里踩过一次）。
_JSON_MODE = False


def _progress(message: str = "") -> None:
    print(message, file=sys.stderr if _JSON_MODE else sys.stdout)


async def main_async(args):
    sites = load_sites()
    curated = {
        name: s
        for name, s in sites.items()
        if s.source in ("sites_cn", "overrides") and s.valid
    }
    if args.site:
        curated = {n: s for n, s in curated.items() if n in args.site}
    if not curated:
        _progress("没有可自检的站点（检查 --site 参数）")
        return 1

    _progress(f"自检 {len(curated)} 个策展站点（每站 2 次请求，间隔 {DELAY_SECONDS}s）\n")
    results = []
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for name, site in curated.items():
            row = await check_site(session, site)
            results.append(row)
            mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "·"}[row["result"]]
            _progress(f"{mark} {name:22} {row['result']:5} {row['detail']}")
            for r in row.get("rows", []):
                _progress(
                    f"    {r['account'][:24]:26} 期望 {r['expected']:9} "
                    f"实际 {r['got']:9} {r['reason'] or '-':18} conf={r['confidence']}"
                )
    return results


def write_doc(results, path):
    passed = [r for r in results if r["result"] == "PASS"]
    failed = [r for r in results if r["result"] == "FAIL"]
    skipped = [r for r in results if r["result"] == "SKIP"]
    rule_broken = [r for r in failed if r.get("kind") == "RULE"]
    env_blocked = [r for r in failed if r.get("kind") != "RULE"]

    lines = [
        "# 中文站点规则存活率",
        "",
        "> 由 `python utils/cn_selfcheck.py --write-doc` 自动生成，请勿手改。",
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 通过 | {len(passed)} |",
        f"| **规则失效（该修）** | **{len(rule_broken)}** |",
        f"| 环境受限（风控/网络，改规则没用） | {len(env_blocked)} |",
        f"| 跳过（缺样本对） | {len(skipped)} |",
        "",
        "判定标准：`known` 必须判 CLAIMED，`known_absent` 必须判 AVAILABLE。",
        "",
        "「环境受限」与「规则失效」分开统计是刻意的：前者说明这个站需要 Cookie 或",
        "浏览器执行器，去改 `e_string` 是白费功夫；后者才是规则真的过期了。",
        "混在一个数字里，维护者会一直在改不该改的地方。",
        "",
        "## 明细",
        "",
        "| 站点 | 结果 | 类型 | 说明 |",
        "|---|---|---|---|",
    ]
    for r in results:
        kind = r.get("kind") or "-"
        note = r["detail"].replace("|", "\\|") or "-"
        lines.append(f"| {r['site']} | {r['result']} | {kind} | {note} |")

    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" 不能省：Windows 上 write_text 默认把 \n 翻成 \r\n，
    # 而 CI 在 Linux 上写的是 \n。同一份看板在两种环境下产出不同字节，
    # 每次本地跑完提交都会是一整个文件的假 diff。
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    _progress(f"\n已写入 {path}")


def exit_code(results) -> int:
    """只有"规则失效"才返回 1。

    被风控/需登录/超时的站点改规则也修不好，让它们把 CI 一直挂在红色，
    等于把告警训练成噪音——最后没人再看这个任务。

    抽成函数是因为这个判据是 CI 的成败契约，必须能被单独测到：
    它一旦写错（比如把 ENV 也算成失败），每日任务就永久变红，
    而"永久红"和"没有任务"是等价的。
    """
    rule_broken = [
        r for r in results if r.get("result") == "FAIL" and r.get("kind") == "RULE"
    ]
    return 1 if rule_broken else 0


def main():
    global _JSON_MODE

    parser = argparse.ArgumentParser(description="中文站点规则自检")
    parser.add_argument("--site", nargs="*", help="只检查指定站点（可多个）")
    parser.add_argument("--write-doc", action="store_true", help="更新 docs/cn-health.md")
    parser.add_argument(
        "--json", action="store_true",
        help="把结果以 JSON 打到 stdout（进度改走 stderr，可直接管道给 jq）",
    )
    parser.add_argument(
        "--json-out", metavar="PATH",
        help="把结果写成 JSON 文件，stdout 保持人类可读。CI 里用这个。",
    )
    args = parser.parse_args()

    # 只要有任何一种机器可读输出，进度就不能再占 stdout。
    _JSON_MODE = bool(args.json or args.json_out)

    if aiohttp is None:
        _progress("需要 aiohttp：pip install aiohttp")
        return 2

    results = asyncio.run(main_async(args))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _progress(f"JSON 结果已写入 {args.json_out}")
    if args.json:
        # 进度已经走 stderr，这里的 stdout 是干净的 JSON
        print(json.dumps(results, ensure_ascii=False, indent=2))
    if args.write_doc:
        write_doc(results, ROOT / "docs" / "cn-health.md")

    failed = [r for r in results if r["result"] == "FAIL"]
    rule_broken = [r for r in failed if r.get("kind") == "RULE"]
    env_blocked = [r for r in failed if r.get("kind") != "RULE"]
    _progress(
        f"\n结果：{len(results) - len(failed)} 通过 / "
        f"{len(rule_broken)} 规则失效 / {len(env_blocked)} 环境受限"
    )
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
