"""断点续查测试（文档 §3.2.4）。

这一层守的是**"中断之后还能不能接着跑"**，而不是判定准不准。三个最容易写错
且后果最严重的点，各自单独有测试：

    1. 落盘是否真的落盘 —— 只 flush 不 fsync，断电场景下 checkpoint 等于不存在
    2. 半截行是否被容忍 —— 进程在 write() 中途被杀是常态，一个坏行不能让
       前面 N 个完整记录一起作废
    3. 续跑是否**完整** —— 只跳过请求不合并结果，续出来的报告就是半份

第 3 点尤其容易被忽略：功能"看起来能用"（第二次跑得很快），但报告里少了
前半段数据，用户还得手动拼两次结果。
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from maigret.checking import maigret, restore_checkpoint_result
from maigret.cn import checkpoint as ckpt_mod
from maigret.cn.checkpoint import (
    ScanCheckpoint,
    checkpoint_path,
    load_records,
)
from maigret.cn.verdict import Reason
from maigret.result import MaigretCheckResult, MaigretCheckStatus
from maigret.sites import MaigretSite

LOG = Mock()


def make_result(
    site="Weibo",
    username="alice",
    status=MaigretCheckStatus.CLAIMED,
    reason=None,
    confidence=None,
    url="https://weibo.com/n/alice",
    http_status=200,
):
    """构造一条与上游形状一致的 SiteResult。

    用真 MaigretCheckResult 而不是假对象，是因为 checkpoint 要处理的正是
    上游这个形状：状态是枚举、reason 是 str 枚举、url 在外层 dict 里。
    """
    return {
        "site": MaigretSite(site, {"urlMain": "https://example.com"}),
        "username": username,
        "url_user": url,
        "http_status": http_status,
        "status": MaigretCheckResult(
            username,
            site,
            url,
            status,
            reason=reason,
            confidence=confidence,
        ),
    }


def write_lines(path: Path, *records):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 1. record() 写入后立刻可读（flush 生效）
# ---------------------------------------------------------------------------
def test_record_is_readable_immediately(tmp_path):
    path = tmp_path / "scan.jsonl"
    ckpt = ScanCheckpoint(path)
    ckpt.record("Weibo", make_result())

    # 不 close 就读：模拟"另一个进程/下一次启动"看到的内容。
    # 只靠 Python 缓冲的话这里会是空文件 —— 这正是 flush 要解决的问题。
    records = load_records(path)
    assert len(records) == 1
    assert records[0]["type"] == "site"
    assert records[0]["site"] == "Weibo"
    assert records[0]["status"] == "Claimed"
    assert records[0]["url"] == "https://weibo.com/n/alice"
    assert records[0]["http_status"] == 200
    assert isinstance(records[0]["ts"], float)
    ckpt.close()


def test_record_actually_calls_fsync(tmp_path, monkeypatch):
    """fsync 是"断电也不丢"的唯一保证，不能被无声地优化掉。

    flush 只把数据交给 OS 页缓存；这里断言 fsync 被真的调用过，否则将来有人
    把这一行删掉（性能优化、看起来冗余），测试会立刻失败。
    """
    calls = []
    real_fsync = os.fsync

    def counting_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(ckpt_mod.os, "fsync", counting_fsync)

    ckpt = ScanCheckpoint(tmp_path / "fsync.jsonl")
    ckpt.record("Weibo", make_result())
    assert calls, "record() 必须 fsync，只 flush 挡不住断电"
    ckpt.close()


def test_record_appends_one_line_per_site(tmp_path):
    path = tmp_path / "multi.jsonl"
    ckpt = ScanCheckpoint(path)
    ckpt.record("A", make_result("A"))
    ckpt.record("B", make_result("B"))
    ckpt.close()

    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln]
    assert len(lines) == 2
    assert [json.loads(ln)["site"] for ln in lines] == ["A", "B"]


# ---------------------------------------------------------------------------
# 2. 半截行容错（断点续查最容易忽略的地方）
# ---------------------------------------------------------------------------
def test_truncated_last_line_is_skipped(tmp_path):
    path = tmp_path / "cut.jsonl"
    write_lines(
        path,
        {"type": "site", "site": "Weibo", "username": "alice", "status": "Claimed"},
        {"type": "site", "site": "Zhihu", "username": "alice", "status": "Unknown"},
    )
    # 模拟 SIGKILL 打断在 write() 中途：末尾一行没闭合
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"type": "site", "site": "broken"')

    # 不允许抛异常
    ckpt, completed = ScanCheckpoint.open_for_resume(path)

    assert set(completed) == {"Weibo", "Zhihu"}
    assert "broken" not in completed
    assert len(load_records(path)) == 2
    ckpt.close()


def test_garbage_lines_are_skipped(tmp_path):
    """除了半截 JSON，还可能是被截断的多字节字符、手改坏的行。"""
    path = tmp_path / "garbage.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type": "site", "site": "Weibo"}\n')
        f.write("not json at all\n")
        f.write("\n")
        f.write('{"type": "site", "site": "Zhihu"}\n')
        f.write('{"type": "site", "site": "Douban"')  # 半截

    _, completed = ScanCheckpoint.open_for_resume(path)
    assert set(completed) == {"Weibo", "Zhihu"}


def test_open_for_resume_on_missing_file(tmp_path):
    """路径写错/第一次运行：当成"从零开始扫"，不是报错退出。"""
    path = tmp_path / "does-not-exist.jsonl"
    ckpt, completed = ScanCheckpoint.open_for_resume(path)
    assert completed == {}
    assert path.exists()
    ckpt.close()


def test_last_record_wins_for_duplicate_site(tmp_path):
    """重试会把同一站写两次，最后一次（成功那次）才是终态。"""
    path = tmp_path / "dup.jsonl"
    write_lines(
        path,
        {"type": "site", "site": "Weibo", "status": "Unknown", "reason": "timeout"},
        {"type": "site", "site": "Weibo", "status": "Claimed", "reason": None},
    )
    _, completed = ScanCheckpoint.open_for_resume(path)
    assert completed["Weibo"]["status"] == "Claimed"


def test_non_site_records_are_ignored(tmp_path):
    """以后可能加 meta 行（扫描参数指纹），读取端不能把它当站点。"""
    path = tmp_path / "meta.jsonl"
    write_lines(
        path,
        {"type": "meta", "version": 1},
        {"type": "site", "site": "Weibo", "status": "Claimed"},
    )
    _, completed = ScanCheckpoint.open_for_resume(path)
    assert set(completed) == {"Weibo"}


# ---------------------------------------------------------------------------
# 3. 续跑：已完成站点不再请求，但要进报告
# ---------------------------------------------------------------------------
class FakeExecutor:
    """记录收到多少个待扫任务，不真的发请求。

    用它验证"请求次数"：续跑的核心承诺就是**不重复请求**，这一点只能从
    执行器拿到的任务数量上验证，从结果集上看不出来。
    """

    instances = []

    def __init__(self, *args, **kwargs):
        self.tasks = []
        FakeExecutor.instances.append(self)

    async def run(self, queries):
        self.tasks = list(queries)
        for query in self.tasks:
            _, _, kwargs = query
            yield kwargs["default"]


@pytest.fixture
def fake_executor(monkeypatch):
    FakeExecutor.instances = []
    monkeypatch.setattr("maigret.checking.AsyncioQueueGeneratorExecutor", FakeExecutor)
    return FakeExecutor


def make_sites(*names):
    return {
        name: MaigretSite(name, {"urlMain": f"https://{name.lower()}.example"})
        for name in names
    }


def run_scan(username, site_dict, resume=None, checkpoint=False):
    """跑一遍 maigret()。

    用 asyncio.run 而不是 @pytest.mark.asyncio：本仓库的 pytest 环境里
    pytest-asyncio 是可选依赖，缺了它异步用例会直接报 "async functions are
    not natively supported"。测试不该依赖插件装没装。
    """
    return maigret(
        username=username,
        site_dict=site_dict,
        logger=LOG,
        query_notify=Mock(),
        resume=str(resume) if resume else None,
        checkpoint=checkpoint,
        no_progressbar=True,
    )


def test_resume_skips_completed_sites(fake_executor, tmp_path):
    path = tmp_path / "resume.jsonl"
    write_lines(
        path,
        {
            "type": "site",
            "site": "A",
            "username": "alice",
            "status": "Claimed",
            "reason": None,
            "confidence": 0.9,
            "url": "https://a.example/alice",
            "http_status": 200,
        },
    )

    results = asyncio.run(run_scan("alice", make_sites("A", "B", "C"), resume=path))

    executor = fake_executor.instances[0]
    # 只请求了 B 和 C —— A 已经完成
    assert len(executor.tasks) == 2
    scanned = {kwargs["default"][0] for _, _, kwargs in executor.tasks}
    assert scanned == {"B", "C"}
    # 但报告里必须有 3 个站，否则"续跑出来的报告"缺一半数据
    assert set(results) == {"A", "B", "C"}


def test_resumed_result_is_restored_into_report(fake_executor, tmp_path):
    path = tmp_path / "restore.jsonl"
    write_lines(
        path,
        {
            "type": "site",
            "site": "A",
            "username": "alice",
            "status": "Unknown",
            "reason": "blocked",
            "confidence": 0.0,
            "url": "https://a.example/alice",
            "http_status": 403,
        },
    )

    results = asyncio.run(run_scan("alice", make_sites("A", "B"), resume=path))

    restored = results["A"]
    assert restored["status"].status == MaigretCheckStatus.UNKNOWN
    assert restored["status"].reason == Reason.BLOCKED
    assert restored["url_user"] == "https://a.example/alice"
    assert restored["http_status"] == 403
    assert restored["from_checkpoint"] is True
    # 报告层（generate_report_context）会读这两个字段，不能缺
    assert restored["status"].ids_data is None
    assert restored["site"].name == "A"


def test_resume_ignores_records_of_another_username(fake_executor, tmp_path):
    """同一个 checkpoint 拿去续另一个用户名，必须重扫而不是沿用旧结论。"""
    path = tmp_path / "other.jsonl"
    write_lines(
        path,
        {"type": "site", "site": "A", "username": "alice", "status": "Claimed"},
    )

    asyncio.run(run_scan("bob", make_sites("A", "B"), resume=path))

    assert len(fake_executor.instances[0].tasks) == 2


def test_resume_writes_new_records_into_same_file(fake_executor, tmp_path):
    """续跑必须继续往同一个文件写，否则这次续出来的后半段下次又丢。"""
    path = tmp_path / "append.jsonl"
    write_lines(
        path, {"type": "site", "site": "A", "username": "alice", "status": "Claimed"}
    )

    asyncio.run(run_scan("alice", make_sites("A", "B", "C"), resume=path))

    sites = [rec["site"] for rec in load_records(path)]
    assert sites[0] == "A"          # 老记录还在
    assert set(sites) == {"A", "B", "C"}  # 新的也写进来了


def test_no_checkpoint_by_default(fake_executor, monkeypatch):
    """不加 --checkpoint / --resume 时零影响：连路径都不算，更不碰磁盘。"""
    called = []
    monkeypatch.setattr(
        "maigret.checking.checkpoint_path",
        lambda *a, **kw: called.append((a, kw)),
    )

    asyncio.run(run_scan("alice", make_sites("A", "B")))

    assert called == []
    assert len(fake_executor.instances[0].tasks) == 2


def test_checkpoint_flag_writes_every_finished_site(fake_executor, tmp_path, monkeypatch):
    """--checkpoint 打开时，每完成一站都要落一行（这里用假执行器喂 2 个站）。"""
    target = tmp_path / "alice-test.jsonl"
    monkeypatch.setattr(
        "maigret.checking.checkpoint_path", lambda *a, **kw: target
    )

    asyncio.run(run_scan("alice", make_sites("A", "B"), checkpoint=True))

    records = load_records(target)
    assert {rec["site"] for rec in records} == {"A", "B"}


def test_all_sites_already_done(fake_executor, tmp_path):
    """全部完成的 checkpoint 续跑：不发任何请求，也要正常返回完整结果。"""
    path = tmp_path / "done.jsonl"
    write_lines(
        path,
        *[
            {"type": "site", "site": name, "username": "alice", "status": "Claimed"}
            for name in ("A", "B")
        ],
    )

    results = asyncio.run(run_scan("alice", make_sites("A", "B"), resume=path))

    assert fake_executor.instances[0].tasks == []
    assert set(results) == {"A", "B"}


def test_cancel_closes_checkpoint(tmp_path, monkeypatch):
    """Ctrl+C 走 CancelledError 分支：文件必须被关掉，且已写内容仍在。"""

    class CancellingExecutor:
        def __init__(self, *a, **kw):
            pass

        async def run(self, queries):
            raise asyncio.CancelledError()
            yield  # pragma: no cover - 让 run 成为异步生成器

    target = tmp_path / "cancel.jsonl"
    monkeypatch.setattr(
        "maigret.checking.AsyncioQueueGeneratorExecutor", CancellingExecutor
    )
    monkeypatch.setattr("maigret.checking.checkpoint_path", lambda *a, **kw: target)

    closed = []
    original_close = ScanCheckpoint.close

    def spy_close(self):
        closed.append(self.path)
        original_close(self)

    monkeypatch.setattr(ScanCheckpoint, "close", spy_close)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_scan("alice", make_sites("A"), checkpoint=True))

    # 取消分支必须关文件，否则 fd 会泄漏到报告生成阶段
    assert closed == [target]
    # 能正常读到（文件没被锁死/没留半截状态）
    assert load_records(target) == []


# ---------------------------------------------------------------------------
# 4. reason 落盘是值，不是 repr
# ---------------------------------------------------------------------------
def test_reason_stored_as_value_not_enum_repr(tmp_path):
    path = tmp_path / "reason.jsonl"
    ckpt = ScanCheckpoint(path)
    ckpt.record("Weibo", make_result(reason=Reason.BLOCKED, status=MaigretCheckStatus.UNKNOWN))
    ckpt.close()

    record = load_records(path)[0]
    assert record["reason"] == "blocked"
    # Python 3.11+ 的 str(str-enum) 会给出 "Reason.BLOCKED"，落盘不能是它
    assert record["reason"] != "Reason.BLOCKED"
    assert "Reason." not in json.dumps(record)


def test_status_stored_as_value(tmp_path):
    path = tmp_path / "status.jsonl"
    ckpt = ScanCheckpoint(path)
    ckpt.record("Weibo", make_result(status=MaigretCheckStatus.CLAIMED))
    ckpt.close()
    assert load_records(path)[0]["status"] == "Claimed"


# ---------------------------------------------------------------------------
# 5. 字段缺失不抛异常
# ---------------------------------------------------------------------------
def test_missing_fields_do_not_raise(tmp_path):
    """只给一个 status 属性，其余全缺 —— 写 checkpoint 绝不能因此崩掉扫描。"""
    path = tmp_path / "partial.jsonl"
    fake = SimpleNamespace(status=MaigretCheckResult("alice", "A", "", MaigretCheckStatus.UNKNOWN))

    ckpt = ScanCheckpoint(path)
    ckpt.record("A", fake)  # 不抛
    ckpt.close()

    record = load_records(path)[0]
    assert record["site"] == "A"
    assert record["status"] == "Unknown"
    assert record["url"] is None
    assert record["http_status"] is None
    assert record["reason"] is None
    assert record["confidence"] is None


def test_completely_empty_object_does_not_raise(tmp_path):
    path = tmp_path / "empty.jsonl"
    ckpt = ScanCheckpoint(path)
    ckpt.record("A", SimpleNamespace())  # 一个字段都没有
    ckpt.record("B", None)              # 连对象都没有
    ckpt.close()
    assert len(load_records(path)) == 2


def test_unserializable_confidence_is_dropped(tmp_path):
    """confidence 是第三方算出来的对象时降级成 null，而不是让 json.dumps 抛。"""
    result = make_result()
    result["status"].confidence = object()

    path = tmp_path / "weird.jsonl"
    ckpt = ScanCheckpoint(path)
    ckpt.record("A", result)
    ckpt.close()
    assert load_records(path)[0]["confidence"] is None


# ---------------------------------------------------------------------------
# 6. 路径与目录
# ---------------------------------------------------------------------------
def test_checkpoint_path_creates_directory(tmp_path):
    target = tmp_path / "nested" / "checkpoints"
    assert not target.exists()

    path = checkpoint_path(target, "alice")

    assert target.is_dir()
    assert path.parent == target
    assert path.exists() is False  # 只是算路径，不建文件


def test_checkpoint_path_name_format(tmp_path):
    path = checkpoint_path(tmp_path, "alice")
    assert re.fullmatch(r"alice-\d{8}-\d{6}\.jsonl", path.name)


def test_checkpoint_path_sanitizes_username(tmp_path):
    """用户名含 "/" 时不能变成子目录，否则文件会写到意想不到的地方。"""
    path = checkpoint_path(tmp_path, "a/b:c*d")
    assert path.parent == tmp_path
    assert "/" not in path.name and ":" not in path.name and "*" not in path.name


def test_default_directory_is_under_home():
    assert ckpt_mod.DEFAULT_DIRECTORY == Path.home() / ".maigret" / "checkpoints"


def test_checkpoint_path_accepts_none_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(ckpt_mod, "DEFAULT_DIRECTORY", tmp_path / "default")
    path = checkpoint_path(None, "alice")
    assert path.parent == tmp_path / "default"
    assert path.parent.is_dir()


# ---------------------------------------------------------------------------
# 杂项
# ---------------------------------------------------------------------------
def test_close_is_idempotent(tmp_path):
    ckpt = ScanCheckpoint(tmp_path / "idem.jsonl")
    ckpt.close()
    ckpt.close()  # 取消分支和正常分支都会调，不能二次 close 抛异常


def test_record_after_close_is_noop(tmp_path):
    """CancelledError 分支先 close，之后若还有结果回来，不能写进已关的文件。"""
    path = tmp_path / "after.jsonl"
    ckpt = ScanCheckpoint(path)
    ckpt.close()
    ckpt.record("A", make_result())
    assert load_records(path) == []


def test_context_manager(tmp_path):
    path = tmp_path / "ctx.jsonl"
    with ScanCheckpoint(path) as ckpt:
        ckpt.record("A", make_result())
    assert len(load_records(path)) == 1


def test_restore_unknown_status_falls_back_to_unknown():
    """checkpoint 里出现不认识的状态值时，宁可 UNKNOWN 也不猜 AVAILABLE。"""
    site = MaigretSite("A", {"urlMain": "https://a.example"})
    result = restore_checkpoint_result(
        "alice", site, {"site": "A", "status": "SomethingNew", "url": "https://a.example"}
    )
    assert result["status"].status == MaigretCheckStatus.UNKNOWN
    assert result["status"].reason is None
