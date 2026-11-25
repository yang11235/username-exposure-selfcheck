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
import os
import re
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

# 单次判定失败后的重试次数（含首次）。只在**失败时**才多打，通过的不受影响。
#
# 为什么必须有：这批站点的响应不稳定。实测新浪博客同一个正例账号
# 连打 20 次全 200，但偶发一次 404 —— 一次抖动就足以让每日 CI 变红，
# 而"每天随机红一次"和"没有告警"是等价的（告警被训练成噪音就没人看了）。
# 重试也顺带把"规则真的错了"和"这次没打通"分开：真错了两次都错，
# 抖动的第二次就过了。
ATTEMPTS = 2
RETRY_DELAY_SECONDS = 3.0

# 整轮自检的墙上时间预算（秒）。超出后剩余站点记 SKIP，不再发起请求。
#
# 单站最坏 = 2 样本 × 2 次 × 20s 超时 + 2 次重试间隔 3s + 2 次样本间隔 1.5s
#          ≈ 89s。31 个待检站最坏 ≈ 46 分钟。
#
# workflow 的 timeout-minutes: 20 是**整个 job** 的墙钟，里面还有 checkout、
# setup-python、pip install、上传产物、git push。所以预算要留够这些：
# 15 分钟给脚本，剩 5 分钟给前后步骤。取 17 分钟是错的——脚本自己能越过
# 预算 89s（deadline 在站间判断，跑着的站不会被打断），加起来顶到 18.5 分钟，
# 再叠上前置步骤就贴 20 分钟上限，随时被 kill。被 kill 比变红更糟：
# 红至少看得出来是这个任务挂了，kill 连退出码都写不回来。
BUDGET_SECONDS = int(os.environ.get("CN_SELFCHECK_BUDGET", 15 * 60))


async def fetch(session, url):
    try:
        async with session.get(url, timeout=TIMEOUT_SECONDS) as resp:
            body = await resp.text(errors="replace")
            return resp.status, body
    except Exception as exc:  # 网络错误也要当成"判定失败"上报，不能静默跳过
        return None, f"__ERROR__ {type(exc).__name__}: {exc}"


# API 型站点（Disqus 这类）拒绝请求时回的是 HTTP 400 + 一个 JSON 错误信封，
# 例如 `{"code":18,"response":"Usage limit exceeded"}`。这不是页面，判据串
# 当然不会出现 —— 但它也**不是**"规则过期"的证据，因为对方压根没回答我们的问题。
#
# 只认 400 且形状对得上的信封：4xx 里 404 是"页面没了"（那是规则问题），
# 5xx 已经在 _is_env_attempt 里单独处理，都不该走到这里。
_API_ERROR_RE = re.compile(r'^\s*\{\s*"code"\s*:\s*-?\d+\s*,\s*"response"\s*:\s*"([^"]{0,160})')


def _api_error(site, status, body) -> str:
    """如果这次响应是"接口拒绝请求"，返回那句错误信息；否则返回空串。

    判据是**两个条件同时成立**：

        1. HTTP 400 + `{"code":n,"response":"..."}` 信封；
        2. 正文里既没有 e_string 也没有 m_string。

    第 2 条是关键，不能省。Disqus 的"用户不存在"本身也是 400 + 同样的信封
    （`{"code":2,"response":"Invalid argument, 'user': User matching query
    does not exist."}`），那是**有效答案**，不是拒绝 —— 只要 m_string 命中，
    就必须让判定层去用，不能被这里截胡成 ENV。
    """
    if status != 400:
        return ""
    match = _API_ERROR_RE.match(body or "")
    if not match:
        return ""
    needles = [site.e_string, site.m_string]
    needles.extend(getattr(site, "e_strings", None) or [])
    needles.extend(getattr(site, "m_strings", None) or [])
    if any(n and n in body for n in needles):
        return ""
    return match.group(1)


def _worth_retrying(status, api_error: str = "") -> bool:
    """这次失败值不值得再打一遍。

    只对**反爬码**说不（403 / 429 / 468 这些，见 BLOCK_STATUS_CODES）：
    它们的意思是"你在被限制"，重打一遍只会把封禁时间拖长。

    其余一律重试，包括 404。这一点反直觉但必须有：实测新浪博客同一个正例
    账号偶发返回 404 —— 抖动就长这样，是个确定的 4xx。按"4xx 不重试"处理，
    这个抖动就永远修不掉，每日 CI 会随机变红。

    接口拒绝请求（`api_error` 非空）同样不重试：配额/密钥被拒不会因为等 3 秒
    就好，多打一次只是把公共 key 的配额烧得更快，反而让恢复更晚。
    """
    if api_error:
        return False
    return status not in BLOCK_STATUS_CODES


async def check_site(session, site: Site, deadline=None):
    """对单个站点跑 known / known_absent 两个样本。

    `deadline` 是 time.monotonic() 的绝对时刻。每次发请求前查一次，
    超了就提前收手——单站最坏 89s，不查的话最后一个站能把整轮拖过上限。
    """
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

        # 命中即停，未命中才重试。抖动是双向的——正例可能偶发 404（假阴性），
        # 反例也可能偶发 200（假阳性）——两个方向都要"重试到命中或放弃"。
        #
        # 代价要认：一条规则若"两次里偶尔蒙对一次"，重试会把它当健康。
        # ATTEMPTS=2 是折中：够挡掉单次抖动，又不至于让偶发误判长期潜伏。
        attempts = []
        for attempt in range(ATTEMPTS):
            if deadline is not None and time.monotonic() > deadline:
                break
            status, body = await fetch(session, url)
            verdict = judge(SimpleNamespace(status=status, text=body), site, username=account)
            api_error = _api_error(site, status, body)
            attempts.append(
                {
                    "http": status,
                    "got": verdict.status.value,
                    "reason": verdict.reason.value if verdict.reason else "",
                    # 接口拒绝了请求（配额/密钥）：判据串不在正文里是必然的，
                    # 不能当成"规则过期"。详见 _api_error。
                    "api_error": api_error,
                }
            )
            if verdict.status == expected or not _worth_retrying(status, api_error):
                break
            if attempt + 1 < ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY_SECONDS)

        if not attempts:
            # deadline 在第一次请求前就到了。这个样本没被检查过，
            # 不能拿它当"判据不命中"——那会凭空造出一条规则失效。
            rows.append(
                {
                    "account": account,
                    "expected": expected.value,
                    "got": "Skipped",
                    "reason": "deadline",
                    "confidence": 0.0,
                    "http": None,
                    "ok": False,
                    "url": url,
                    "attempts": 0,
                    "history": [],
                    "unchecked": True,
                }
            )
            continue

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
                "attempts": len(attempts),
                # 全部尝试都要留下：classify 要据此判断"有没有任何一次
                # 拿到过可判定的响应"。只看最后一次会把结论交给抖动顺序——
                # 先 200 不命中、再抖成 400，真规则错误就被误判成 ENV。
                "history": attempts,
            }
        )
        await asyncio.sleep(DELAY_SECONDS)

    # 没查过的样本不算失败：它们 `ok=False` 只是因为没发请求，不是判据不命中。
    # 混进 failed 会让 classify 拿到一堆 http=None，把站点误判成 ENV。
    unchecked = [r for r in rows if r.get("unchecked")]
    failed = [r for r in rows if not r["ok"] and not r.get("unchecked")]

    # 预算用完导致有样本没查：只有**已查过的样本也没给出结论**时才记 SKIP。
    #
    # 不能无条件记 SKIP：正例已经拿到"200 且判据串不在页面上"——那是规则
    # 失效的确凿证据，它跟"反例有没有来得及查"无关。把整站降成 SKIP，
    # 等于用一个没做的检查去否掉一个已经做出来的检查。
    if unchecked and not failed:
        return {
            "site": site.name,
            "result": "SKIP",
            "detail": "超出本次运行的时间预算，未检查完",
            "rows": rows,
        }
    if not failed:
        return {"site": site.name, "result": "PASS", "rows": rows, "detail": ""}

    kind = classify(failed, site)
    detail = _diagnose(_deciding_row(failed, kind), site, kind)
    if unchecked:
        # 结论成立，但要说清另一半没查——否则维护者会以为两个样本都验过了。
        detail += "（另一侧样本因超出时间预算未检查，结论基于已检查的一侧）"
    return {
        "site": site.name,
        "result": "FAIL",
        "kind": kind,
        "rows": rows,
        # 诊断必须解释**这个结论**，而不是"最后一次尝试"。
        # 两者用不同数据源时必然打架：kind 说 RULE（CI 红）、文案却说
        # "不是规则错误，需要 Cookie"，维护者被指向两个相反方向。
        "detail": detail,
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


def _all_attempts(rows):
    """展开每条失败样本的全部尝试。

    只看最后一次会把结论交给抖动顺序：先拿到 200（规则真的过期，判据不命中）、
    第二次抖成 500，只看最后一次就成了"服务端挂了"→ ENV，真规则错误被静默。
    倒过来先 500 再 200 又判 RULE —— 同一件事两种结论，取决于哪次先到。
    """
    out = []
    for r in rows:
        history = r.get("history")
        if history:
            out.extend(history)
        else:  # 兼容没有 history 的手工构造行（测试与旧数据）
            out.append(
                {
                    "http": r.get("http"),
                    "got": r.get("got"),
                    "reason": r.get("reason", ""),
                    "api_error": r.get("api_error", ""),
                }
            )
    return out


def _is_env_attempt(attempt) -> bool:
    """这一次尝试是不是"没拿到可判定的响应"。"""
    status = attempt.get("http")
    if status is None:
        return True                                    # 网络没通
    if attempt.get("api_error"):
        return True                                    # 接口拒绝了请求（配额/密钥）
    if attempt.get("reason") in ENV_REASONS:
        return True                                    # 风控 / 登录墙 / 空 body
    if status in BLOCK_STATUS_CODES:
        return True                                    # 反爬码
    if isinstance(status, int) and 500 <= status < 600:
        return True                                    # 服务端故障
    return False


def classify(rows, site=None):
    """把失败分成 RULE（规则问题）与 ENV（环境问题）。

    分界线是"改了 sites_cn.json 能不能修好"：
        ENV  —— 被风控、被限流、需要登录、请求失败。改规则是白费功夫。
        RULE —— 请求通了、状态码是预期的，但判据串对不上。这才是规则过期。

    这个划分决定了 CI 红不红，写错的代价是把告警变成噪音。

    **判据是"有没有任何一次拿到可判定的响应"，不是"有没有任何一次是 ENV"。**
    这两者差别很大，写错过一次：重试机制下"先 500、再 200 且判据不命中"是
    真实可达的，那是**服务端抖了一下、页面拿到了、但判据串已经不在页面上**——
    正是规则过期的铁证。按"一票 ENV 否决"会把它归成环境受限，等于用新的
    静默替换旧的静默。

    所以顺序是：先把 ENV 性质的尝试剔掉，只要还剩一次可判定的尝试，
    这次失败就是 RULE（规则判不出来）。全都不可判定才是 ENV。

    `site` 参数保留但**不参与判定**：曾想用"状态码不在 e_code/m_code 里"
    区分"对方拒绝请求"与"我们规则写错了"，这是错的——两者在响应上完全同形。
    接口搬家返回 404、URL 拼错返回 400、e_code 笔误写成 201，都会被它静默；
    status_only 站点（全库 8347 条，判据只有状态码）更是整类失去保护。
    """
    if not rows:
        return "ENV"
    attempts = _all_attempts(rows)
    decidable = [a for a in attempts if not _is_env_attempt(a)]
    return "RULE" if decidable else "ENV"


def _deciding_row(failed, kind):
    """挑出"导致这个结论"的那一行，供 _diagnose 解释。

    `failed[0]` 不等于"做出结论的那一行"：kind 由 classify 扫全部尝试得出，
    而 failed[0] 只是排在前面的那个样本。典型的错位是
    「正例被 403 风控、反例正常返回但不含判据串」——kind 是 RULE（反例那侧
    有可判定证据），failed[0] 却是被风控的正例行。拿它去解释，就会得到
    "不是规则错误，需要 Cookie"，而 CI 已经因为 RULE 变红了。

    所以按 kind 反选：RULE 要一行"可判定"的尝试（那才是证据所在），
    ENV 要一行"不可判定"的尝试。
    """
    for r in failed:
        attempts = _all_attempts([r])
        want_decidable = kind == "RULE"
        wanted = [a for a in attempts if (not _is_env_attempt(a)) == want_decidable]
        if wanted:
            a = wanted[0]
            return {
                **r,
                "http": a["http"],
                "reason": a.get("reason", ""),
                "got": a.get("got", r.get("got")),
                "api_error": a.get("api_error", ""),
            }
    return failed[0]


def _diagnose(row, site, kind):
    """把失败翻译成人能直接动手改的话。

    `kind` 是 classify 的结论，必须传进来，否则文案会和结论打架：
    classify 看的是全部尝试，`row` 只是最后一次，两者数据源不同。
    "先 200 判据不命中、重试时抖成 None" 的 kind 仍是 RULE（页面确实拿到过、
    判据串确实不在），只看 row 就会输出"网络请求失败"——结论要红，
    文案却说与规则无关，维护者被指向两个相反的方向。
    """
    reason = row["reason"]

    # ---- kind 已定为 ENV：文案不能再指向规则 ----
    if kind == "ENV":
        if row["http"] is None:
            return "网络请求失败（没拿到响应，与判据串无关）"
        # 接口拒绝请求要排在状态码分支之前：它是 400，不在反爬码集合里，
        # 落到底部那句"没拿到可判定的响应（structure_changed）"会让人
        # 以为是页面结构变了，跑去改 e_string —— 而真正该做的是等配额恢复。
        if row.get("api_error"):
            return (
                f"接口拒绝了请求（HTTP 400，{row['api_error']}）："
                f"判据串不在正文里是必然的，与规则无关；"
                f"多半是公共 API key 在这个出口 IP 上被限流或失效"
            )
        # 5xx 要排在反爬码之前：503 同时在这两个集合里，但"服务不可用"
        # 的准确描述是服务端故障，说成"被反爬拦截"会让人去配 Cookie——白折腾。
        if isinstance(row["http"], int) and 500 <= row["http"] < 600:
            return f"服务端故障（HTTP {row['http']}），与规则无关；重试也没拿到可判定的响应"
        if isinstance(row["http"], int) and row["http"] in BLOCK_STATUS_CODES:
            return (
                f"被反爬拦截（HTTP {row['http']}），页面内容不可信，不是规则问题；"
                f"需要 Cookie 或浏览器执行器"
            )
        if reason == "blocked":
            return "被风控拦截，无法判定（不是规则错误，需要 Cookie 或浏览器执行器）"
        if reason == "login_required":
            return "需要登录才能看到内容"
        if reason == "captcha":
            return "命中验证码，无法判定（不是规则错误）"
        if reason == "rate_limited":
            return "被限流（HTTP 429），改规则修不好"
        if reason == "empty_body":
            return "响应体为空，没有可判定的内容"
        return f"没拿到可判定的响应（{reason or '无原因'}），与判据串无关"

    # ---- 以下是 kind == RULE：真的需要改规则 ----
    #
    # 这里不能再出现"不是规则错误""需要 Cookie""与判据串无关"这类措辞：
    # kind 已经是 RULE、CI 已经变红，再说不是规则问题就是把维护者指向
    # 相反的方向。_deciding_row 保证进到这里的 row 是一次"可判定"的尝试，
    # 所以 reason 不会是 blocked / login_required / empty_body / captcha。
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
        # 状态码完全错位时，"两侧都不命中"是个误导：判据串是给预期状态码写的，
        # 服务端用别的码回时它必然不命中。但**这不等于不是我们的问题**——
        # 接口搬家、URL 拼错、e_code 笔误，都会表现成"码不对"。
        # 文案只描述现象，不替人下"是外部故障"的结论。
        if isinstance(row.get("http"), int) and row["http"] not in (site.e_code, site.m_code):
            return (
                f"状态码不符（HTTP {row['http']}，规则期望 e={site.e_code}/m={site.m_code}）："
                f"判据串在非预期状态码下必然不命中，所以要先把状态码对齐——"
                f"确认地址/方法是否还对，以及站点是否改用了别的码"
            )
        return (
            f"两侧都不命中：e_string={site.e_string!r} / m_string={site.m_string!r} "
            f"与当前页面不符（实际 HTTP {row['http']}）"
        )
    if reason == "rule_missing":
        return "规则被标记为不可用（valid=false）"
    # 走到这里说明 reason 是个没被单独处理的判据类原因（例如某个新的
    # Verdict.reason）。如实报出来，别硬塞一句"不是规则问题"。
    return (
        f"规则判不出来（期望 {row['expected']}，实际 {row['got']}，原因 {reason or '无'}，"
        f"HTTP {row['http']}）"
    )


# 是否处于"机器可读输出"模式。开启后人类可读的进度一律走 stderr，
# 保证 stdout 上只有 JSON——否则 `> out.json` 拿到的是一堆进度行加一段
# JSON，`json.load()` 会在第一个字符就报错（CI 里踩过一次）。
_JSON_MODE = False


def _progress(message: str = "") -> None:
    print(message, file=sys.stderr if _JSON_MODE else sys.stdout)


async def main_async(args):
    sites = load_sites()
    all_curated = {
        name: s for name, s in sites.items() if s.source in ("sites_cn", "overrides")
    }
    curated = {name: s for name, s in all_curated.items() if s.valid}
    # 显式停用的规则（valid=false）也要出现在结果里。
    #
    # 只把它们从 curated 里过滤掉，看板上就彻底没有这一行了——"这条规则
    # 被停用了"这个事实会从视野里消失，包括停用多久、为什么停。停用是
    # 临时措施，不是删除：它得在看板上继续占一行，才有可能被人想起来修。
    disabled = [n for n, s in all_curated.items() if not s.valid]
    if args.site:
        curated = {n: s for n, s in curated.items() if n in args.site}
        disabled = [n for n in disabled if n in args.site]
    if not curated and not disabled:
        # 返回空列表而不是 1：调用方按"结果列表"消费，返回 int 会让
        # `for r in results` 直接 TypeError，而 --json-out 还会先把这个
        # 1 写进文件，CI 的摘要步骤 json.load 得到 int 后再崩一次。
        # "没有站点可查"用退出码表达，见 main()。
        _progress("没有可自检的站点（检查 --site 参数）")
        return []

    _progress(
        f"自检 {len(curated)} 个策展站点"
        f"（每站最多 {2 * ATTEMPTS} 次请求，间隔 {DELAY_SECONDS}s）\n"
    )
    deadline = time.monotonic() + BUDGET_SECONDS
    results = []
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for name, site in curated.items():
            # 全局时间预算，在**站间**判断。单站内部还会在每次请求前再查一次
            # （见 check_site 的 deadline 参数），两道一起挡住"最后一个站
            # 拖过上限"的情况：只在这里判断的话，一个正在跑的站最坏还能
            # 再耗 89s，足以顶到 workflow 的 20 分钟。
            #
            # 超预算就停手，剩下的站点记 SKIP 而不是 FAIL —— 没跑的不能算失败，
            # 否则预算一到，剩下的站会被集体误报成规则失效。
            if time.monotonic() > deadline:
                results.append(
                    {
                        "site": name,
                        "result": "SKIP",
                        "detail": "超出本次运行的时间预算，未检查",
                    }
                )
                _progress(f"· {name:22} SKIP  超出时间预算，剩余站点未检查")
                continue

            row = await check_site(session, site, deadline)
            results.append(row)
            # 用 .get 而不是下标：现在 DISABLED 是在循环外 append 的，所以
            # 这里只会拿到三种值；但那是靠调用顺序而不是类型保证的，
            # 哪天 check_site 内部想返回别的状态，下标就会 KeyError。
            mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "·", "DISABLED": "-"}.get(
                row["result"], "?"
            )
            # 重试过的要显式标出来：抖动本身是有价值的信息（"这个站今天不稳"），
            # 藏起来的话，看板只能看到"通过"，看不到它其实一直在抖。
            retried = " (重试)" if any(r.get("attempts", 1) > 1 for r in row.get("rows", [])) else ""
            _progress(f"{mark} {name:22} {row['result']:5} {row['detail']}{retried}")
            for r in row.get("rows", []):
                _progress(
                    f"    {r['account'][:24]:26} 期望 {r['expected']:9} "
                    f"实际 {r['got']:9} {r['reason'] or '-':18} conf={r['confidence']}"
                )

    # 停用的规则排在最后，各自一行。它们不发请求（valid=false 的判据不可信，
    # 打了也是白打），但必须留痕——否则看板上"全部通过"会掩盖
    # "有几条规则被关掉了"这件事。
    for name in disabled:
        results.append(
            {
                "site": name,
                "result": "DISABLED",
                "detail": "规则已停用（valid=false），本次未检查",
            }
        )
        _progress(f"- {name:22} DISABLED 规则已停用（valid=false），本次未检查")
    return results


def write_doc(results, path):
    passed = [r for r in results if r["result"] == "PASS"]
    failed = [r for r in results if r["result"] == "FAIL"]
    skipped = [r for r in results if r["result"] == "SKIP"]
    disabled = [r for r in results if r["result"] == "DISABLED"]
    rule_broken = [r for r in failed if r.get("kind") == "RULE"]
    env_blocked = [r for r in failed if r.get("kind") != "RULE"]

    # 抖动过的站点单列出来。重试是为了不让单次抖动把 CI 顶红，但如果连
    # "它在抖"都不显示，看板上就只剩一个干净的"通过"——那等于把
    # "这个站不稳定、下次可能真挂"这条信息也一起吞掉了。
    flaky = [
        r
        for r in passed
        if any(x.get("attempts", 1) > 1 for x in r.get("rows", []))
    ]

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
        f"| ├ 其中首次未命中、重试后通过（抖动） | {len(flaky)} |",
        f"| **规则失效（该修）** | **{len(rule_broken)}** |",
        f"| 环境受限（风控/网络，改规则没用） | {len(env_blocked)} |",
        f"| 跳过（缺样本对 / 非 http 执行器 / 超预算） | {len(skipped)} |",
        f"| **停用（valid=false，待修）** | **{len(disabled)}** |",
        "",
        "判定标准：`known` 必须判 CLAIMED，`known_absent` 必须判 AVAILABLE。",
        "",
        "「环境受限」与「规则失效」分开统计是刻意的：前者说明这个站需要 Cookie 或",
        "浏览器执行器，去改 `e_string` 是白费功夫；后者才是规则真的过期了。",
        "混在一个数字里，维护者会一直在改不该改的地方。",
        "",
        "「抖动」单列同理：这些站这次通过了，但第一次没判出来。它们通过是运气",
        "（或重试）而不是规则稳，下次站点再抖一次就可能变成真的失败。",
        "",
        "「停用」也单列：`valid=false` 是临时措施而不是删除。规则关掉之后如果不",
        "留一行，看板上「全部通过」就会掩盖「有几条规则其实被关着」这件事。",
        "",
        "## 明细",
        "",
        "| 站点 | 结果 | 类型 | 说明 |",
        "|---|---|---|---|",
    ]
    for r in results:
        kind = r.get("kind") or "-"
        note = r["detail"].replace("|", "\\|") or "-"
        attempts = [x.get("attempts", 1) for x in r.get("rows", [])]
        if r["result"] == "PASS" and any(a > 1 for a in attempts):
            note = f"重试 {max(attempts)} 次后通过（抖动）"
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


def _sites_from_env():
    """从 CN_SELFCHECK_SITES 读站点列表（CI 用这条通道）。

    站名里有空格（`Sina Blog`、`NetEase Music (搜索)`）。如果由 shell 用
    `${SITES:+--site $SITES}` 展开，不加引号会被词分割成两个站、加引号又会
    整串当成一个站——两种都是错的。所以让脚本自己按行 / 逗号切分，
    不经过 shell。

    返回 None 表示"没有指定"（不是空列表：空列表的语义是"一个站都不要"）。
    """
    raw = os.environ.get("CN_SELFCHECK_SITES", "")
    if not raw.strip():
        return None
    parts = raw.replace(",", "\n").replace("，", "\n").splitlines()
    return [p.strip() for p in parts if p.strip()]


def main():
    global _JSON_MODE

    parser = argparse.ArgumentParser(description="中文站点规则自检")
    parser.add_argument(
        "--site", nargs="*",
        help="只检查指定站点（可多个）。含空格的站名要加引号；"
             "CI 里用环境变量 CN_SELFCHECK_SITES（换行或逗号分隔）代替。",
    )
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

    # 环境变量里的站点列表与 --site 合并。CI 只设环境变量（站名带空格，
    # 没法安全地走命令行），本地仍可用 --site 临时看几个站。
    env_sites = _sites_from_env()
    if env_sites is not None:
        args.site = list(dict.fromkeys((args.site or []) + env_sites)) or None

    # 只要有任何一种机器可读输出，进度就不能再占 stdout。
    _JSON_MODE = bool(args.json or args.json_out)

    if aiohttp is None:
        _progress("需要 aiohttp：pip install aiohttp")
        return 2

    results = asyncio.run(main_async(args))

    if not results:
        # 没有可查的站点：--json-out 不能写文件（写个 0 进去，
        # 下游 json.load 拿到空列表会以为"全都通过了"）。
        return 1

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _progress(f"JSON 结果已写入 {args.json_out}")
    if args.json:
        # 进度已经走 stderr，这里的 stdout 是干净的 JSON
        print(json.dumps(results, ensure_ascii=False, indent=2))
    if args.write_doc:
        # --site 是"临时看几个站"，不该拿它覆盖整份看板：
        # 看板是全体站点的快照，用一两个站的结果覆盖它，再被 CI 推到
        # cn-health 分支，整份历史就没了。
        if args.site:
            _progress("指定了 --site，跳过写看板（看板需要全量结果，避免被局部结果覆盖）")
        else:
            write_doc(results, ROOT / "docs" / "cn-health.md")

    failed = [r for r in results if r["result"] == "FAIL"]
    rule_broken = [r for r in failed if r.get("kind") == "RULE"]
    env_blocked = [r for r in failed if r.get("kind") != "RULE"]
    # 逐项数，不要用 len(results) - len(failed)：那会把 SKIP / DISABLED
    # 一起算成"通过"，看板上"通过 42"而实际只验了 24 个站。
    passed = [r for r in results if r["result"] == "PASS"]
    skipped = [r for r in results if r["result"] == "SKIP"]
    disabled = [r for r in results if r["result"] == "DISABLED"]
    _progress(
        f"\n结果：{len(passed)} 通过 / "
        f"{len(rule_broken)} 规则失效 / {len(env_blocked)} 环境受限 / "
        f"{len(skipped)} 跳过 / {len(disabled)} 停用"
    )
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
