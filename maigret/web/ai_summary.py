"""Web 端 AI 摘要：把检索结果交给 OpenAI 兼容接口，生成一段分析。

配置写在 ~/.maigret/settings.json（与 CLI 的 --ai 共用同一组键）：

    {
        "openai_api_key": "sk-...",
        "openai_model": "gpt-4o",
        "openai_api_base_url": "https://api.openai.com/v1"
    }

没有配置 key 时回退到环境变量 OPENAI_API_KEY / DEEPSEEK_API_KEY；
用 DEEPSEEK_API_KEY 时自动走 DeepSeek 的兼容端点。
"""
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import aiohttp

from maigret.ai import load_ai_prompt

DEFAULTS = {"model": "gpt-4o", "base_url": "https://api.openai.com/v1"}
SETTINGS_PATH = Path.home() / ".maigret" / "settings.json"
MAX_INPUT_CHARS = 15000

_ZH_HINT = "\n\n输出要求：全文使用简体中文，包括各级标题和字段名；语气克制，不要臆测没有证据的结论。"


def load_ai_config():
    """返回 {api_key, model, base_url, source}，source 用于界面提示。"""
    cfg = dict(DEFAULTS, api_key="", source="")

    # 只解析一次，且解析失败时不保留任何"部分成功"的状态。
    # 原来这段在 DeepSeek 分支里又 json.loads 了一遍（且没接异常），
    # settings.json 写坏时会在那里抛 JSONDecodeError —— 上面的 try 保不到它。
    data = {}
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}

        cfg["api_key"] = str(data.get("openai_api_key") or "").strip()
        cfg["model"] = data.get("openai_model") or cfg["model"]
        cfg["base_url"] = data.get("openai_api_base_url") or cfg["base_url"]
        if cfg["api_key"]:
            cfg["source"] = "settings.json"

    if not cfg["api_key"]:
        for env_name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            value = os.environ.get(env_name, "").strip()
            if not value:
                continue
            cfg["api_key"] = value
            cfg["source"] = env_name
            if env_name == "DEEPSEEK_API_KEY":
                # 端点与模型要**成对**跟随：用户可能是「自己在 settings 里写了
                # 端点（比如本地网关），但不想写模型」。只跟端点不跟模型，就会
                # 拿默认的 gpt-4o 去请求那个端点——OpenAI 官方端点没配过、本地
                # 网关也没有这个模型，请求 404/400，而错误信息完全看不出是
                # "模型名没跟着换"。
                if not data.get("openai_api_base_url"):
                    cfg["base_url"] = "https://api.deepseek.com/v1"
                if not data.get("openai_model"):
                    cfg["model"] = "deepseek-chat"
            break

    return cfg


LOG_PATH = Path.home() / ".maigret" / "web.log"
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
# 网关侧「上游 60 秒未返回任何内容」这类软失败，HTTP 是 400 但语义是可重试
_RETRY_MARKERS = ("ABORTED", "已中断", "未返回任何内容", "timeout", "timed out")


def _log(message):
    """失败原因落到文件里，网页上只显示一句话，细节看这个日志。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


async def _request_once(url, payload, headers, timeout):
    """流式请求。

    必须用 stream=True：网关（Our Free Model 这类）有「首字节 60 秒超时」，
    非流式时上游要等整段生成完才吐第一个字节，长报告必被掐断并返回
    HTTP 400 / code=ABORTED。流式则一开始就有增量，能撑住长输出。
    """
    timeout_cfg = aiohttp.ClientTimeout(total=timeout, sock_read=120)
    chunks = []

    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                body = (await resp.text())[:300]
                retryable = resp.status in _RETRY_STATUS or any(
                    m.lower() in body.lower() for m in _RETRY_MARKERS
                )
                raise _CallError(f"HTTP {resp.status}：{body}", retryable)

            ctype = resp.headers.get("Content-Type", "")
            if "event-stream" not in ctype:
                # 少数实现会无视 stream 参数直接返回整段 JSON
                data = json.loads(await resp.text())
                try:
                    return data["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, TypeError):
                    raise _CallError(f"返回格式异常：{str(data)[:300]}")

            async for raw in resp.content:
                line = raw.decode("utf-8", "ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
                if obj.get("error"):
                    raise _CallError(f"上游错误：{str(obj['error'])[:300]}", retryable=True)
                for choice in obj.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        chunks.append(piece)

    text = "".join(chunks).strip()
    if not text:
        raise _CallError("上游没有返回任何内容", retryable=True)
    return text


class _CallError(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


async def summarize(markdown_report, cfg, timeout=180, attempts=3):
    """调用兼容 OpenAI 的接口；网关排队/超时时会重试。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "stream": True,
        "messages": [
            {"role": "system", "content": load_ai_prompt() + _ZH_HINT},
            {"role": "user", "content": markdown_report[:MAX_INPUT_CHARS]},
        ],
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await _request_once(url, payload, headers, timeout)
        except _CallError as exc:
            last_error = exc
            _log(f"AI 第 {attempt}/{attempts} 次失败：{exc}")
            if not exc.retryable or attempt == attempts:
                raise
            await asyncio.sleep(3 * attempt)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = _CallError(f"网络错误：{exc!r}", retryable=True)
            _log(f"AI 第 {attempt}/{attempts} 次网络异常：{exc!r}")
            if attempt == attempts:
                raise last_error
            await asyncio.sleep(3 * attempt)

    raise last_error or RuntimeError("调用失败")
