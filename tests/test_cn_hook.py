"""cn.hook 接入层测试。

这一层守的不是「判定对不对」（那是 test_cn_verdict 的事），而是**接管边界**：
    1. 接管范围是否可控（默认只碰策展站点，上游 9500 个站行为不变）
    2. 出问题能否一键回退（MAIGRET_CN_VERDICT=0）
    3. 接入点是否放在正确位置（结论变更后不能再抽 ids）

为什么这层必须单独测：判定器再准，如果接入时接管了不该接管的站点，
或者回退开关失效，用户看到的就是「工具坏了」，而不是「工具更保守了」。
"""

import logging

import pytest

from maigret.checking import process_site_result
from maigret.cn import hook
from maigret.cn.verdict import Reason
from maigret.result import MaigretCheckStatus
from maigret.sites import MaigretSite

LOG = logging.getLogger("test_cn_hook")

# 微博的真实上游形态：message 型 + 成对正反例串。
# 用它而不是编一个假站，是因为"接管后是否仍能正确判定"取决于上游字段的
# 真实形状（presenseStrs 的拼写、usernameClaimed 是裸字符串）。
WEIBO_RAW = {
    "url": "https://weibo.com/n/{username}",
    "urlProbe": "https://weibo.com/ajax/profile/info?screen_name={username}",
    "checkType": "message",
    "presenseStrs": ['{"ok":1,"data":{"user":'],
    "absenceStrs": ["该昵称当前没有人使用"],
    "usernameClaimed": "clairekuo",
    "usernameUnclaimed": "noonewouldeverusethis7",
    "tags": ["cn", "social"],
}

NOT_CURATED_RAW = {
    "url": "https://example.com/{username}",
    "checkType": "message",
    "presenseStrs": ["profile_page"],
    "absenceStrs": ["no such user"],
    "usernameClaimed": "alice",
    "usernameUnclaimed": "zzzz9",
    "tags": ["global"],
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """每个用例都从「默认模式 + 空缓存」开始，避免互相污染。"""
    monkeypatch.delenv(hook.ENV_FLAG, raising=False)
    hook.reset_cache()
    yield
    hook.reset_cache()


@pytest.fixture(autouse=True)
def _take_over_all_sites(monkeypatch, clean_env):
    # 显式依赖 clean_env：它会把环境变量删掉，必须让它先跑，否则本 fixture 白设
    """本模块统一用 `MODE_ALL`：对所有站点接管判定，而不只是策展库里的那些。

    为什么必须这样（2026-11-17）：本模块的夹具站原来叫 `Weibo`，
    靠"它在策展库里"来触发接管。现在 `Weibo` 在规则库里是
    `valid=false`（该站 robots 整站禁止），继续用这个名字会走进
    "规则缺失"分支，测不到这些用例本来要测的通用判定逻辑。

    换成一个策展库外的名字 + `MODE_ALL`：hook 会用**上游规则适配**出 spec
    再接管，判定路径与夹具的意图完全一致。

    个别用例需要别的模式时，在用例内部自己 `monkeypatch.setenv` 覆盖即可
    （`test_mode_off_restores_upstream_behaviour` 就是这么做的）。
    """
    monkeypatch.setenv(hook.ENV_FLAG, hook.MODE_ALL)
    # spec 有缓存，不清的话会把上一个用例在别的模式下算出的结果带过来
    hook.reset_cache()


def make_site(name="WeiboUpstreamSample", raw=None):
    """构造一个站点对象。

    默认名字**不在**策展库里（配合上面的 `MODE_ALL` fixture），
    夹具数据仍是微博那套字段（`WEIBO_RAW`）。
    """
    return MaigretSite(name, dict(raw or WEIBO_RAW))


def run(site, body, status=200, username="clairekuo"):
    """跑一遍上游的 process_site_result，返回结果对象。"""
    info = {
        "username": username,
        "parsing_enabled": False,
        "url_user": "https://example.com/" + username,
        "url_probe": "https://example.com/" + username,
    }
    return process_site_result((body, status, None), None, LOG, info, site)["status"]


# ---------------------------------------------------------------------------
# 模式与接管范围
# ---------------------------------------------------------------------------
def test_default_mode_is_curated_only(monkeypatch):
    """默认必须只接管策展站点——全量替换 9500 个未验证规则风险太大。

    本模块的 autouse fixture 会设成 MODE_ALL（为了不依赖"Weibo 是否在策展库里"），
    所以这条要显式还原成"什么都不设"的默认状态再断言。
    """
    monkeypatch.delenv(hook.ENV_FLAG, raising=False)
    hook.reset_cache()
    assert hook.mode() == hook.MODE_CURATED


def test_unknown_env_value_falls_back_to_default(monkeypatch):
    """环境变量拼错时不能静默关掉功能，也不能崩，按默认处理并告警。"""
    monkeypatch.setenv(hook.ENV_FLAG, "yes-please")
    assert hook.mode() == hook.MODE_CURATED


@pytest.mark.parametrize("value", ["0", "1", "all", "ALL", " 1 "])
def test_valid_env_values_are_recognized(monkeypatch, value):
    monkeypatch.setenv(hook.ENV_FLAG, value)
    assert hook.mode() in (hook.MODE_OFF, hook.MODE_CURATED, hook.MODE_ALL)


def test_non_curated_site_is_not_taken_over(monkeypatch):
    """未策展站点必须完全走上游逻辑——这是"改动范围可控"的底线。

    这条要的是**默认模式**（1，只接管策展站），所以显式覆盖 autouse 的 MODE_ALL。
    """
    monkeypatch.setenv(hook.ENV_FLAG, hook.MODE_CURATED)
    hook.reset_cache()
    site = make_site("SomeGlobalSite", NOT_CURATED_RAW)
    # 风控页：如果被接管会判 UNKNOWN，未接管则上游按 200 + presence 判 CLAIMED
    result = run(site, '访问过于频繁 profile_page')
    assert result.status == MaigretCheckStatus.CLAIMED
    assert result.reason is None


def test_mode_off_restores_upstream_behaviour(monkeypatch):
    """MAIGRET_CN_VERDICT=0 必须能整体回退，不需要改代码或删文件。"""
    monkeypatch.setenv(hook.ENV_FLAG, hook.MODE_OFF)
    hook.reset_cache()
    site = make_site()
    result = run(site, '您的访问过于频繁 {"ok":1,"data":{"user":')
    assert result.status == MaigretCheckStatus.CLAIMED


def test_mode_all_takes_over_non_curated_site(monkeypatch):
    """all 模式下任何四字段合格的站点都接管。"""
    monkeypatch.setenv(hook.ENV_FLAG, hook.MODE_ALL)
    hook.reset_cache()
    site = make_site("SomeGlobalSite", NOT_CURATED_RAW)
    result = run(site, '访问过于频繁 profile_page')
    assert result.status == MaigretCheckStatus.UNKNOWN


# ---------------------------------------------------------------------------
# 核心行为：误报拦截
# ---------------------------------------------------------------------------
def test_risk_control_page_with_evidence_string_is_unknown():
    """最关键的一条：风控页即使包含 e_string，也必须判 UNKNOWN。

    这正是上游最致命的误报路径——风控页往往就是站点自己的模板，
    里面照样带着"用户存在"的字段名。只认字符串不认拦截特征，必然误报。
    """
    site = make_site()
    body = '您的访问过于频繁，请稍后再试 {"ok":1,"data":{"user":'
    result = run(site, body)
    assert result.status == MaigretCheckStatus.UNKNOWN
    assert result.reason == Reason.BLOCKED


def test_normal_hit_still_claimed():
    """接管不能把正常命中弄丢——否则用户会以为工具查不出东西。"""
    result = run(make_site(), '{"ok":1,"data":{"user":{"id":1}}}')
    assert result.status == MaigretCheckStatus.CLAIMED
    assert result.confidence and result.confidence > 0.5


def test_absence_string_is_available():
    result = run(make_site(), "该昵称当前没有人使用")
    assert result.status == MaigretCheckStatus.AVAILABLE


def test_empty_200_body_is_unknown():
    """200 + 空 body 是中文站常态（WAF/反代/SPA 空壳），不能判命中。"""
    result = run(make_site(), "")
    assert result.status == MaigretCheckStatus.UNKNOWN
    assert result.reason == Reason.EMPTY_BODY


def test_rate_limited_is_not_downgraded_to_http_error():
    """429 必须归到 rate_limited，不能和"连不上"混成一个桶。

    上游的 detect_error_page 会把 429 也当普通错误页，如果 hook 直接写
    HTTP_ERROR，"该退避重试"和"该换网络"就分不出来了。
    """
    result = run(make_site(), "slow down", status=429)
    assert result.reason == Reason.RATE_LIMITED
    assert result.rate_limited is True


def test_forbidden_is_blocked():
    result = run(make_site(), "denied", status=403)
    assert result.reason == Reason.BLOCKED


def test_invalid_curated_rule_reports_missing_rule():
    """策展条目自己标了 valid=false（如 QQ），接管后必须输出"规则缺失"。

    这里刻意**不**回退给上游：QQ 的 e_string 是 JSONP 包装函数名，存在与
    不存在的响应都包含它，上游会给出一个必然错误的结论。给用户一行
    "未判定（规则缺失）"，比给一个错的"存在"更诚实——这正是产品定位。
    """
    site = make_site("QQ", {"url": "https://user.qzone.qq.com/{username}"})
    spec = hook.spec_for(site)
    assert spec is not None and spec.valid is False

    result = run(site, "portraitCallBack({})")
    assert result.status == MaigretCheckStatus.UNKNOWN
    assert result.reason == Reason.RULE_MISSING


# ---------------------------------------------------------------------------
# 结果字段：报告能不能看到"为什么"
# ---------------------------------------------------------------------------
def test_result_json_exposes_reason_and_confidence():
    """落盘必须带原因，否则报告里只有 Unknown，用户不知道发生了什么。"""
    result = run(make_site(), "")
    data = result.json()
    assert data["reason"] == "empty_body"
    assert "confidence" in data


def test_json_omits_reason_for_clean_hits():
    """正常命中不塞一堆 null，避免污染报告和下游解析。"""
    data = run(make_site(), '{"ok":1,"data":{"user":{}}}').json()
    assert "reason" not in data
    assert "context" not in data


# ---------------------------------------------------------------------------
# 扫描编排：force_include
# ---------------------------------------------------------------------------
def test_augment_sites_adds_curated_sites():
    """策展站点必须能进默认扫描，否则"默认只查 5 个中国站"的问题没解决。"""
    from maigret.sites import MaigretDatabase

    db = MaigretDatabase().load_from_path("maigret/resources/data.json")
    base = db.ranked_sites_dict(top=500, id_type="username")
    assert "Weibo" in base  # 上游本来就有

    augmented = hook.augment_sites(dict(base), db, "username")
    # 语雀没有 alexaRank，永远进不了 Top500，只能靠 force_include
    assert "Yuque" in augmented
    assert len(augmented) > len(base)


def test_augment_sites_skips_sites_that_need_another_id_type():
    """按数字 ID 查的站不能进用户名扫描。

    掘金/网易云的接口要 user_id，拿用户名去问得到的是"查无此人"——那是
    我们问错了问题，不是用户没注册。这种站只能 `--id-type user_id` 查。
    """
    from maigret.sites import MaigretDatabase

    db = MaigretDatabase().load_from_path("maigret/resources/data.json")
    base = db.ranked_sites_dict(top=500, id_type="username")
    augmented = hook.augment_sites(dict(base), db, "username")

    assert "Juejin" not in augmented
    assert "NetEase Music" not in augmented
    # 换成 user_id 扫描时它们才应该出现
    as_uid = hook.augment_sites({}, db, "user_id")
    assert "Juejin" in as_uid


def test_baidu_is_actually_reachable_by_the_default_scan():
    """百度必须真的进默认扫描 —— 这是用户报"百度连查都没查到"的直接原因。

    之前 Baidu 是 valid=false，而 augment_sites 的第一条过滤就是"valid=False
    的不并"，所以它在快速检索里**完全隐身**：不是"查了没查到"，是压根没查。
    现在改用 `home/get/panel` 接口，四字段齐全，才谈得上进扫描。

    同时锁住"这条规则要抽资料"：只判存在、不抽字段，用户看到的还是空白。

    ## 2026-11-16 判据变更（这条断言跟着改，不是放宽）

    原来断言 `e_string == '"no":0'`。`docs/retrieval-analysis.md` §3 记录过
    一种 74 字节的**降级响应**：

        {"no":0,"error":"成功","data":{"tb_vip":false,"followed_count":0}}

    它含 `"no":0`，但不含 `"data":[]` —— 于是按旧判据，**任何用户名**都会被
    判成 Claimed。当时一次性跑 22 条中文规则，前 4 条全部落到这个形态，
    得到 4 个假阳性。文档里已经写明建议改成 `"name_show":`，但一直没改。

    `name_show` 只在真实用户响应里出现（585/865/576/703/586 字节那批），
    降级响应（74B）里没有。5 正 5 负实测 100% 分离。
    这条断言现在盯的是"判据必须是 name_show"，因为换成 `"no":0` 就等于
    重新打开那个批量误报。
    """
    from maigret.sites import MaigretDatabase

    db = MaigretDatabase().load_from_path("maigret/resources/data.json")
    base = db.ranked_sites_dict(top=500, id_type="username")
    augmented = hook.augment_sites(dict(base), db, "username")

    assert "Baidu" in augmented
    spec = hook.spec_for(augmented["Baidu"])
    assert spec is not None and spec.valid is True
    # 两侧都要有字符串判据（同状态码 200，只能靠字符串分）
    assert spec.e_string == '"name_show":'
    assert spec.m_string == '"data":[]'
    assert "gender" in spec.extract
    # 明确禁止回到 "no":0 —— 它对降级响应会把任何用户名判成存在
    assert '"no":0' not in (spec.e_string or "")


def test_augment_sites_respects_default_include_flag():
    """小众站保留规则但不占默认位：仍能 --site 查到，只是不塞进默认扫描。"""
    from maigret.sites import MaigretDatabase

    db = MaigretDatabase().load_from_path("maigret/resources/data.json")
    base = db.ranked_sites_dict(top=500, id_type="username")
    augmented = hook.augment_sites(dict(base), db, "username")

    assert "OpenI" not in augmented
    assert "AskTUG" not in augmented


def test_augment_sites_honours_tag_filters():
    """`--tags video` 不该顺带塞进 20 个中文站。"""
    from maigret.sites import MaigretDatabase

    db = MaigretDatabase().load_from_path("maigret/resources/data.json")
    base = db.ranked_sites_dict(top=500, id_type="username", tags=["video"])
    augmented = hook.augment_sites(dict(base), db, "username", tags=["video"])

    for name in augmented:
        if name in ("Yuque", "v2ex.com", "cnblogs.com"):
            raise AssertionError(f"{name} 没有 video 标签，不该被并入")


def test_augment_sites_is_noop_when_disabled(monkeypatch):
    monkeypatch.setenv(hook.ENV_FLAG, hook.MODE_OFF)
    hook.reset_cache()
    from maigret.sites import MaigretDatabase

    db = MaigretDatabase().load_from_path("maigret/resources/data.json")
    base = db.ranked_sites_dict(top=50, id_type="username")
    assert hook.augment_sites(dict(base), db, "username") == base


def test_augment_sites_skips_browser_only_platforms():
    """小红书/抖音这类需要浏览器执行器的站不能并入 HTTP 扫描。

    并进来只会产生一批必然失败的请求，把"未判定"刷满报告。
    """
    from maigret.sites import MaigretDatabase

    db = MaigretDatabase().load_from_path("maigret/resources/data.json")
    augmented = hook.augment_sites({}, db, "username")
    assert "小红书" not in augmented
    assert "抖音" not in augmented
