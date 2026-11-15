"""断点续查（《开发文档 v2.0》§3.2.4，抄 WhatsMyName 的 checkpoint 机制）。

为什么这个模块是 P0 而不是"锦上添花"：

    中文站扫描动辄 5–10 分钟，其中大量时间花在被墙站点的超时上。中途被打断
    是**常态**而不是意外：Ctrl+C、笔记本合盖、SSH 断线、OOM、直接 SIGKILL。
    上游 `checking.py` 的 `all_results.update([result])` 只把进度留在内存里，
    进程一死全丢 —— 用户只能重头再来一遍，而"再来一遍"大概率在同一个地方
    再死一次。断点续查要解决的就是这件事。

落盘策略是**每完成一站就写一行并 fsync**，而不是"退出时统一保存"：

    - 退出时保存 = 崩了就什么都没写，等于没做
    - 只在内存里攒着定期写 = 丢失窗口内完成的站点
    - 每站一行 append = 最坏情况只丢"正在写的那一行"

关于 flush + fsync 的区别（这是本模块最容易被写错的地方）：

    `f.flush()` 只是把 Python 的用户态缓冲区交给内核 —— 数据到了 OS 的页缓存，
    **还没到磁盘**。此时断电 / 内核 panic / 虚拟机强制关机，数据照样丢。
    `os.fsync(f.fileno())` 才会真正把脏页推给块设备。文档 §3.2.4 强调的就是
    这一点：只 flush 的 checkpoint 在"断电"这个最需要它的场景下完全失效。

为什么读取必须容忍半截行：

    进程可能正好在 `write()` 中途被杀（尤其是 SIGKILL 和断电），文件末尾就会
    留下一行没闭合的 JSON。如果 `open_for_resume` 整体解析失败，那 checkpoint
    机制就等于"在崩溃时崩第二次"——前面 N 个完整记录全部作废。所以逐行解析、
    单行失败就跳过，是这里唯一的正确做法。

依赖约束：**纯标准库**。本模块不认识 MaigretCheckResult / SiteResult，只用
duck-typing 取字段（dict 和对象两种形态都吃），因此可以脱离 maigret 运行时
独立测试，也不会和上游形成循环 import。
"""

import json
import logging
import os
import re
import time
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 默认目录与文档 §1.3「纯本地，全部存 ~/.maigret/」一致
DEFAULT_DIRECTORY = Path.home() / ".maigret" / "checkpoints"

# 记录类型。留出字段是为了以后加 "meta"/"header" 行（比如扫描参数指纹），
# 读取端按 type 过滤即可，不会把元信息当成站点结果。
RECORD_TYPE_SITE = "site"

# Windows 文件名非法字符 + 控制字符。用户名可能含 "/"（`a/b`），
# 不清理会变成子目录，checkpoint 写到意想不到的地方去。
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# 容错取值
# ---------------------------------------------------------------------------
def _get(obj: Any, name: str, default: Any = None) -> Any:
    """从 dict 或普通对象里取字段，取不到一律返回 default。

    为什么两种形态都要支持：扫描循环 yield 的是 `(site_name, SiteResult)`
    二元组，SiteResult 是 TypedDict；但报告层、测试、以及未来的调用方更可能
    直接传 `MaigretCheckResult` 对象。让取字段这一层同时吃两种，比要求调用方
    先转一次更不容易出错。

    绝不抛异常：写 checkpoint 的时候抛异常会连带杀掉正在扫描的任务 ——
    "因为记录进度而丢掉整个扫描"比"少记一行"糟糕得多。
    """
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_value(value: Any) -> Optional[str]:
    """枚举取 `.value`，其它类型取 `str()`。

    这里不能用 `str(enum)`：`Reason` 是 `str, Enum` 的混合枚举，Python 3.11+
    的 `str(Reason.BLOCKED)` 得到的是 `"Reason.BLOCKED"` 而不是 `"blocked"`。
    落盘值一旦变成前者，报告里按原因分组、跨版本读取就全乱了。
    """
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    raw = getattr(value, "value", None)
    if isinstance(raw, str):
        return raw
    return str(value)


def _as_int(value: Any) -> Optional[int]:
    """HTTP 状态码：上游成功时是 int，错误分支是 `""`，统一成 int | None。"""
    if isinstance(value, bool):  # bool 是 int 的子类，先排掉
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    """置信度：上游是 float | None；不能序列化的对象一律降级成 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _looks_like_check_result(obj: Any) -> bool:
    """判断 obj 是不是"判定结果对象"（而不是包着它的 SiteResult）。

    区分方式：MaigretCheckResult 有 site_url_user / username / confidence 这些
    字段，而 MaigretCheckStatus 枚举一个都没有。这样调用方传 SiteResult 还是传
    判定对象，都能落到正确的层级上。
    """
    if obj is None:
        return False
    return any(
        _get(obj, name) is not None
        for name in ("site_url_user", "username", "confidence", "rate_limited")
    )


def _json_safe(value: Any) -> Any:
    """只保留能 JSON 序列化的结构化值，否则返回 None。

    `ids_data` 来自 socid_extractor，理论上都是 str/list/dict，但一旦混进
    非序列化对象，`json.dumps` 会拒掉**整条记录** —— 丢一个字段可以接受，
    丢整条记录等于白扫这个站。所以这里先单独试一次序列化。
    """
    if not isinstance(value, (dict, list)):
        return None
    if not value:
        return None
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return value


def build_record(site_name: str, result: Any) -> Dict[str, Any]:
    """把一次站点检查结果压成一行 checkpoint。

    字段是**报告重建所需的最小集合**：续跑时不能再发请求，报告里的 URL、
    状态、原因、置信度只能从这里来。少一个字段，续跑出来的报告就缺一块。

    所有取值都走 `_get`，任何字段缺失都只是变成 null，不会抛异常。
    """
    inner = _get(result, "status")
    if inner is not None and _looks_like_check_result(inner):
        # 常规路径：result 是 SiteResult，result["status"] 才是判定结果
        holder, check = result, inner
    else:
        # 调用方直接传了判定结果对象（或是个只有 status 属性的假对象）
        holder, check = result, result

    url = (
        _get(check, "site_url_user")
        or _get(holder, "url_user")
        or _get(holder, "url_probe")
    )
    http_status = _as_int(_get(holder, "http_status"))
    if http_status is None:
        http_status = _as_int(_get(check, "http_status"))

    return {
        "type": RECORD_TYPE_SITE,
        "site": str(site_name) if site_name is not None else None,
        "username": _get(check, "username") or _get(holder, "username"),
        # 状态枚举的 value 与上游 MaigretCheckStatus 逐字一致（"Claimed" 等），
        # 重建时可以直接 MaigretCheckStatus(value)。
        "status": _enum_value(_get(check, "status")),
        # 关键：必须是 "blocked" 这种稳定标识，不是 "Reason.BLOCKED"
        "reason": _enum_value(_get(check, "reason")),
        "confidence": _as_float(_get(check, "confidence")),
        "url": str(url) if url is not None else None,
        "http_status": http_status,
        # 抽取到的公开字段（uid/创建时间/地区/公司/头像 URL）。
        # 必须落盘：报告里的关联图谱和头像都读 status.ids_data，不存的话
        # 续跑出来的报告会"变薄"——命中还在，但扩展信息全空，
        # 用户会直观感觉"续出来的不如一次跑完的"。
        "ids_data": _json_safe(_get(check, "ids_data")),
        "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# 目录 fsync
# ---------------------------------------------------------------------------
def _fsync_directory(directory: Path) -> None:
    """把目录项也刷到盘上（只在新建文件时做一次）。

    新建文件时，文件内容的 fsync 只保证"数据块"落盘，"这个文件名存在"这条
    元信息记在目录项里。断电后可能出现"内容在、文件名没了"的幽灵文件。
    POSIX 上要再 fsync 一次目录才算彻底。

    Windows 无法以只读方式打开目录句柄，直接跳过 —— NTFS 的元数据日志
    已经覆盖了这一层，这不是妥协。
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def load_records(path: Any) -> List[Dict[str, Any]]:
    """逐行读 checkpoint，**跳过所有解析不了的行**。

    半截行是常态不是异常：进程在 write() 中途被杀，末尾就是一行没闭合的 JSON。
    这里绝不能整体 try 包住循环然后返回 []，否则一个坏行会让前面所有好记录
    一起作废 —— 那正是断点续查最需要救回来的东西。

    文件不存在、没有读权限，一律返回空列表：续跑一个不存在的 checkpoint
    等于从头开始扫，这是合法用法（比如用户把路径打错了，至少还能跑完）。
    """
    records: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    # 半截行 / 被截断的 UTF-8 / 手改坏的行，都走这里
                    logger.debug("跳过 checkpoint 中无法解析的一行：%r", line[:80])
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError as exc:
        logger.debug("读取 checkpoint %s 失败：%s", path, exc)
        return []
    return records


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
class ScanCheckpoint:
    """一个 append-only 的 JSONL 断点文件。

    用法（扫描循环里）：

        ckpt = ScanCheckpoint(path)
        ...
        ckpt.record(site_name, result)   # 每完成一站
        ...
        ckpt.close()

    续跑（CLI 的 --resume）：

        ckpt, done = ScanCheckpoint.open_for_resume(path)
        # done: {站点名: 最后一次记录}，据此把已完成站点从待扫列表里剔除
    """

    def __init__(self, path: Any):
        self.path = Path(path).expanduser()
        # 目录不存在就建：用户第一次带 --checkpoint 时不该因为缺目录而失败
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.exists()

        # append 模式而不是 "w"：--resume 时要保留已有记录，不能覆盖
        self._handle = open(self.path, "a", encoding="utf-8", newline="\n")
        self._closed = False
        self._written = 0

        if is_new:
            # 只在新建文件时刷目录项，续写不需要
            _fsync_directory(self.path.parent)

    # -- 写 ---------------------------------------------------------------
    def record(self, site_name: str, result: Any) -> None:
        """记录一个站点的检查结果：写一行 + flush + fsync。

        三个动作缺一不可，原因见模块文档。写失败只告警不抛 —— 磁盘满、权限
        变化都不该让正在跑的扫描挂掉。
        """
        if self._closed:
            return

        try:
            payload = build_record(site_name, result)
            # 先整体序列化再写：避免序列化中途失败留下半行
            # （半行虽然读取端能跳过，但没必要自己制造）
            line = json.dumps(payload, ensure_ascii=False)
        except Exception as exc:  # pragma: no cover - build_record 已做容错
            logger.warning("站点 %s 的 checkpoint 记录构造失败：%s", site_name, exc)
            return

        try:
            self._handle.write(line + "\n")
            self._handle.flush()
            # flush 只到 OS 页缓存；断电场景下必须 fsync 才算真的落盘
            os.fsync(self._handle.fileno())
            self._written += 1
        except (OSError, ValueError) as exc:
            logger.warning("写 checkpoint %s 失败（站点 %s）：%s", self.path, site_name, exc)

    # -- 关闭 -------------------------------------------------------------
    def close(self) -> None:
        """关闭文件。可重复调用（扫描循环的取消分支和正常分支都会调）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except (OSError, ValueError):
            pass
        try:
            self._handle.close()
        except OSError:  # pragma: no cover
            pass

    def __enter__(self) -> "ScanCheckpoint":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- 续跑 -------------------------------------------------------------
    @classmethod
    def open_for_resume(cls, path: Any) -> Tuple["ScanCheckpoint", Dict[str, Dict[str, Any]]]:
        """打开（或新建）checkpoint，并返回已完成站点。

        返回 `(实例, {站点名: 记录})` 而不是集合：续跑不仅要"跳过请求"，
        还要把这些记录**还原进最终报告**，否则续出来的报告只有半份数据。

        同一站点出现多次时取**最后一条**：重试成功的结论比首次超时更有价值。
        """
        completed: Dict[str, Dict[str, Any]] = {}
        for record in load_records(path):
            if record.get("type") != RECORD_TYPE_SITE:
                continue
            name = record.get("site")
            if not name:
                continue
            completed[str(name)] = record

        return cls(path), completed

    @property
    def written(self) -> int:
        """本次运行写入的行数（测试和日志用）。"""
        return self._written


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def _safe_username(username: Any) -> str:
    """把用户名压成安全的文件名片段。"""
    name = _ILLEGAL_FILENAME_CHARS.sub("_", str(username or "")).strip(" .")
    return name or "scan"


def checkpoint_path(directory: Any = None, username: str = "") -> Path:
    """生成 `{username}-{YYYYMMDD-HHMMSS}.jsonl` 路径，目录不存在则创建。

    为什么带时间戳而不是固定文件名：同一个用户可能一天扫好几次（换了网络、
    加了新站点），固定名会把上一次的结果和这一次混在同一个文件里，续跑时
    拿到的是两次扫描的拼盘。时间戳让每次运行天然独立，续跑则显式指定文件。
    """
    base = Path(directory).expanduser() if directory else DEFAULT_DIRECTORY
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return base / f"{_safe_username(username)}-{stamp}.jsonl"
