"""`maigret.web.ai_summary` 的配置读取与调用。

这个模块此前零测试覆盖，所以它的两个致命问题都是"用户点了按钮才发现"：

1. `ai_prompt.txt` 被 .gitignore 的 `*.txt` 规则排除，从没进过版本库 →
   任何 clone 之后点「AI 分析」必然 FileNotFoundError。
   （那个断言在 test_ai.py，因为它测的是 maigret.ai 的资源。）

2. `load_ai_config()` 在 DeepSeek 分支里把 settings.json 又 json.loads 了一遍
   且没接异常，文件写坏时直接抛 JSONDecodeError，绕过上层那个 try。
"""

import json

import pytest

from maigret.web import ai_summary


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """把 SETTINGS_PATH 指到临时文件，并清掉可能干扰的环境变量。"""
    path = tmp_path / 'settings.json'
    monkeypatch.setattr(ai_summary, 'SETTINGS_PATH', path)
    for name in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    return path


def write_settings(path, data):
    path.write_text(json.dumps(data), encoding='utf-8')


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_reads_key_model_and_endpoint_from_settings(settings_file):
    write_settings(settings_file, {
        'openai_api_key': 'ofm-test-key',
        'openai_model': 'chain:auto',
        'openai_api_base_url': 'http://127.0.0.1:18900/v1',
    })

    cfg = ai_summary.load_ai_config()

    assert cfg['api_key'] == 'ofm-test-key'
    assert cfg['model'] == 'chain:auto'
    assert cfg['base_url'] == 'http://127.0.0.1:18900/v1'
    assert cfg['source'] == 'settings.json'


def test_falls_back_to_defaults_when_only_key_is_set(settings_file):
    write_settings(settings_file, {'openai_api_key': 'sk-x'})

    cfg = ai_summary.load_ai_config()

    assert cfg['model'] == ai_summary.DEFAULTS['model']
    assert cfg['base_url'] == ai_summary.DEFAULTS['base_url']


def test_missing_settings_file_uses_env(settings_file, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-env')

    cfg = ai_summary.load_ai_config()

    assert cfg['api_key'] == 'sk-env'
    assert cfg['source'] == 'OPENAI_API_KEY'


def test_deepseek_env_switches_endpoint(settings_file, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'sk-ds')

    cfg = ai_summary.load_ai_config()

    assert cfg['base_url'] == 'https://api.deepseek.com/v1'
    assert cfg['model'] == 'deepseek-chat'


def test_deepseek_env_keeps_explicit_endpoint(settings_file, monkeypatch):
    """settings 里显式写了端点时，不能被 DeepSeek 的默认值顶掉。"""
    write_settings(settings_file, {'openai_api_base_url': 'http://127.0.0.1:18900/v1'})
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'sk-ds')

    cfg = ai_summary.load_ai_config()

    assert cfg['base_url'] == 'http://127.0.0.1:18900/v1'
    assert cfg['model'] == 'deepseek-chat'


# ---------------------------------------------------------------------------
# 损坏的配置：不能抛异常
# ---------------------------------------------------------------------------
def test_corrupt_settings_does_not_raise(settings_file):
    """settings.json 写坏时回退到默认值，不能让整个请求 500。

    这是真实缺陷：旧实现在 DEEPSEEK_API_KEY 存在时会把文件再解析一遍，
    没有异常保护，写坏的 json 会直接抛 JSONDecodeError。
    """
    settings_file.write_text('{bad json', encoding='utf-8')

    cfg = ai_summary.load_ai_config()

    assert cfg['api_key'] == ''
    assert cfg['model'] == ai_summary.DEFAULTS['model']


def test_corrupt_settings_with_deepseek_env_does_not_raise(settings_file, monkeypatch):
    """走的正是出问题的那个分支：无 key → 环境变量 → DeepSeek 默认值。"""
    settings_file.write_text('{bad json', encoding='utf-8')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'sk-ds')

    cfg = ai_summary.load_ai_config()

    assert cfg['api_key'] == 'sk-ds'
    assert cfg['base_url'] == 'https://api.deepseek.com/v1'


def test_settings_that_is_not_an_object_does_not_raise(settings_file):
    """JSON 合法但不是对象（比如 `[]` 或 `"x"`）也要能降级。"""
    settings_file.write_text('[1, 2, 3]', encoding='utf-8')

    cfg = ai_summary.load_ai_config()

    assert cfg['api_key'] == ''


# ---------------------------------------------------------------------------
# 流式解析
# ---------------------------------------------------------------------------
def test_stream_error_event_is_surfaced_not_swallowed(monkeypatch):
    """网关常见的失败形态：HTTP 200 + 正常开流 + 流内推 error 字段。

    实测（Our Free Model 网关）：
        data: {"choices":[{"delta":{"content":""}}]}      ← 空首包
        data: {"error":{"message":"模型可用账号均在限流中"}}
        data: [DONE]

    如果只读 delta.content，这种情况会得到空字符串，报"上游没有返回任何内容"，
    把"限流到几点解禁"这种可行动信息丢掉了。
    """
    import asyncio

    class FakeContent:
        def __init__(self, lines):
            self._lines = lines

        def __aiter__(self):
            async def gen():
                for line in self._lines:
                    yield line.encode('utf-8')
            return gen()

    class FakeResp:
        status = 200
        headers = {'Content-Type': 'text/event-stream'}

        def __init__(self, lines):
            self.content = FakeContent(lines)

        async def text(self):
            return ''

    class FakePost:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            return False

    lines = [
        'data: {"choices":[{"delta":{"content":""}}]}',
        '',
        'data: {"error":{"message":"模型 dfmodel 的可用账号均在限流中"}}',
        '',
        'data: [DONE]',
        '',
    ]

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, *a, **k):
            return FakePost(FakeResp(lines))

    monkeypatch.setattr(ai_summary.aiohttp, 'ClientSession', FakeSession)

    with pytest.raises(ai_summary._CallError) as excinfo:
        asyncio.run(
            ai_summary._request_once('http://x/v1/chat/completions', {}, {}, 5)
        )

    assert '限流' in str(excinfo.value)
    assert excinfo.value.retryable is True
