"""Tests for maigret.ai terminal output."""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from maigret.ai import _stream_response, _write_encodable

# 子进程探针的导入环境：把仓库根塞进 PYTHONPATH。
# 探针在 tmp_path 里跑，`sys.path[0]` 是那个临时目录，所以 `import maigret`
# 只有在包被 pip 安装过时才成功。CI 是 `pip install -e .`，本地直接跑 pytest
# 往往没装——那种失败来自环境，不是代码。
def _probe_env(base=None):
    env = dict(base or os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root + ((os.pathsep + existing) if existing else "")
    return env


def _run_probe(tmp_path, name, lines, encoding):
    """Run a probe in a child process whose streams really use `encoding`.

    Forcing the encoding rather than mocking the failure is deliberate: this then
    fails on any machine if the guard goes, the same way the banner test does.
    """
    probe = tmp_path / name
    probe.write_text("\n".join(lines) + "\n", encoding="utf-8")

    env = _probe_env()
    env["PYTHONIOENCODING"] = encoding
    return subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True,
        encoding=encoding,
        errors="replace",
        env=env,
        cwd=str(tmp_path),
    )


def test_spinner_renders_on_a_stderr_that_cannot_encode_braille(tmp_path):
    """The spinner frames are braille, which cp1252 -- the ANSI codepage of a
    stock Windows install -- cannot encode. Writing one raised UnicodeEncodeError
    inside the spinner's daemon thread: the thread died with a traceback printed
    over the output, and the animation stopped for the rest of the run.
    """
    result = _run_probe(
        tmp_path,
        "spinner_probe.py",
        [
            "import sys, time",
            "from maigret.ai import _Spinner",
            "",
            "spinner = _Spinner('probe')",
            "spinner.start()",
            "time.sleep(0.3)",
            "spinner.stop()",
            "sys.stdout.write('FRAMES=' + ''.join(spinner._frames) + chr(10))",
            "sys.stdout.write('STILL_RUNNING=' + str(spinner._thread.is_alive()) + chr(10))",
            "sys.stdout.write('REACHED_END' + chr(10))",
        ],
        "cp1252",
    )

    assert result.returncode == 0, f"the spinner must not crash the run: stderr={result.stderr!r}"
    assert "REACHED_END" in result.stdout, f"stdout={result.stdout!r}"
    # The daemon thread swallows nothing: an encode failure surfaces as a
    # traceback on stderr and leaves the spinner dead for the rest of the run.
    assert "UnicodeEncodeError" not in result.stderr, f"stderr={result.stderr!r}"
    assert "Exception in thread" not in result.stderr, f"stderr={result.stderr!r}"
    # It still animates, using frames the stream can carry.
    assert "FRAMES=|/-\\" in result.stdout, f"stdout={result.stdout!r}"


def test_spinner_keeps_its_braille_when_the_stream_can_encode_it(tmp_path):
    """The frame set is chosen from the stream, not swapped unconditionally: a
    UTF-8 terminal must still get the original animation."""
    result = _run_probe(
        tmp_path,
        "spinner_utf8_probe.py",
        [
            "import sys",
            "from maigret.ai import _Spinner",
            "",
            "sys.stdout.write('FRAMES=' + ''.join(_Spinner('probe')._frames) + chr(10))",
        ],
        "utf-8",
    )

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    braille = "".join(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])
    assert f"FRAMES={braille}" in result.stdout, f"stdout={result.stdout!r}"


# --- the analysis itself, which goes to stdout --------------------------------

CYRILLIC = "Привет"


def cp1252(errors):
    """A real cp1252 text stream, so the codec decides rather than a mock."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors=errors)


def read_back(stream):
    stream.flush()
    return stream.buffer.getvalue().decode("cp1252")


def test_raw_write_to_a_cp1252_stdout_is_what_fails():
    """Pin the defect itself, so these tests fail if the platform changes.

    surrogateescape is what sys.stdout actually carries here, and it is not a
    guard against this: it rescues lone surrogates only, so an ordinary
    unencodable character still raises.
    """
    stream = cp1252("surrogateescape")

    with pytest.raises(UnicodeEncodeError):
        stream.write(CYRILLIC)
        stream.flush()


@pytest.mark.parametrize("errors", ["strict", "surrogateescape"])
def test_a_reply_outside_the_codepage_does_not_raise(errors):
    stream = cp1252(errors)

    _write_encodable(stream, CYRILLIC)

    assert read_back(stream) == "?" * len(CYRILLIC)


def test_a_reply_the_codepage_can_carry_is_written_unchanged():
    """The replacement is a fallback, not the normal path."""
    stream = cp1252("strict")

    _write_encodable(stream, "Ana Gómez, née Muñoz")

    assert read_back(stream) == "Ana Gómez, née Muñoz"


def test_a_stream_that_does_not_raise_is_left_to_its_own_handler():
    """The straight write comes first; replacement is only the recovery path.

    A backslashreplace stream -- which is what sys.stderr carries -- encodes
    without raising, so the helper must not reach for its fallback and flatten
    the escapes to '?'. Without this case, always replacing passes every other
    test here: the strings the codepage can carry round-trip unchanged, and the
    ones it cannot raise either way.
    """
    stream = cp1252("backslashreplace")

    _write_encodable(stream, CYRILLIC)

    assert read_back(stream) == CYRILLIC.encode("cp1252", "backslashreplace").decode("cp1252")
    assert "?" not in read_back(stream)


def test_streaming_an_international_reply_finishes(monkeypatch):
    """The end-to-end path. The exception propagated out of the coroutine and
    ended the analysis mid-sentence, with the earlier tokens left on screen.
    """
    import asyncio

    class FakeContent:
        def __aiter__(self):
            return self._lines()

        async def _lines(self):
            for token in ("Найден ", "профиль", " 完了"):
                yield b'data: {"choices":[{"delta":{"content":"' + token.encode() + b'"}}]}'
            yield b"data: [DONE]"

    class FakeResp:
        content = FakeContent()

    class FakeSpinner:
        def stop(self):
            pass

    stream = cp1252("surrogateescape")
    monkeypatch.setattr(sys, "stdout", stream)

    _, analysis = asyncio.run(_stream_response(FakeResp(), FakeSpinner(), first_token=True))

    # The value handed back to the caller is the real text; only the terminal
    # copy is degraded to what the codepage can show.
    assert analysis == "Найден профиль 完了"
    assert "?" in read_back(stream)


# ---------------------------------------------------------------------------
# ai_prompt.txt 必须真的在仓库里
# ---------------------------------------------------------------------------
def test_ai_prompt_file_ships_with_the_package():
    """AI 系统提示词是**资源文件**，必须随包存在。

    它曾经被 .gitignore 的 `*.txt` 规则一起忽略，所以从来没进过版本库——
    任何一次 clone 之后点「AI 分析」都必然抛
    `FileNotFoundError: maigret/resources/ai_prompt.txt`，界面上只显示
    「调用失败：...」。没有这条断言，同类问题还会再发生一次。
    """
    from maigret.ai import load_ai_prompt

    prompt = load_ai_prompt()

    assert prompt.strip(), '系统提示词是空的'
    # 内容特征：至少要提到这是一份 OSINT 报告分析任务
    assert 'OSINT' in prompt
    assert len(prompt) > 500, f'提示词只有 {len(prompt)} 字符，像是被截断了'


def test_ai_prompt_is_not_gitignored():
    """防止 `*.txt` 之类的通配规则把它再排除一次。

    只在有 .git 的仓库里跑；装成 wheel 的环境跳过。
    """
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    if not (repo_root / '.git').exists():
        pytest.skip('不在 git 仓库里')

    proc = subprocess.run(
        ['git', 'check-ignore', '-q', 'maigret/resources/ai_prompt.txt'],
        cwd=str(repo_root),
        capture_output=True,
    )
    # 退出码 1 = 未被忽略（期望）；0 = 被忽略（有问题）
    assert proc.returncode == 1, 'ai_prompt.txt 又被 .gitignore 忽略了'
