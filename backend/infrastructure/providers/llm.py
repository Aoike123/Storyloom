"""DeepSeek（OpenAI 兼容）文本/会话协议：纯请求构造与响应消费。

只读抽取自旧 `llm_stream.read_completion` + `providers.chat_json`，改写依赖：
保留 SSE 流消费、stall 超时、256KB 上限、usage 提取、JSON 模式与「失败不自动
重发」；**请求体绝不携带 thinking/reasoning 相关参数**（DeepSeek 禁 thinking）；
不得 import 旧 backend 业务模块，不写数据库，不做计费登记。

模型与 prompt 可追溯：`ChatRequest.body()["model"]` / `ChatRequest.prompt` 是实际
发出的值，`LlmResult` 同样带回 `model` / `prompt` / `request_body`。
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Mapping

from . import base
from .base import ProviderError

LLM_ENV_KEYS = base.REQUIRED_ENV["llm"]
CHAT_COMPLETIONS_PATH = "/chat/completions"
STREAM_CONTENT_TYPE = "text/event-stream"

MAX_OUTPUT_BYTES = 256 * 1024
LIMIT_MESSAGE = "模型输出超过 256KB 上限，已停止读取；本次请求不会自动重发。"

MIN_MAX_TOKENS = 256
MAX_MAX_TOKENS = 32768
DEFAULT_MAX_TOKENS = 8192
MIN_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 600.0
DEFAULT_TIMEOUT_SECONDS = 120.0

# 请求体内不得出现的参数名（键名包含以下任一子串即拒绝）。
FORBIDDEN_PARAMETER_NAMES = ("thinking", "reasoning")


def chat_endpoint(base_url, *, field="LLM_BASE_URL"):
    """`LLM_BASE_URL` → `.../chat/completions`（https 强校验）。"""
    return base.require_https_endpoint(base_url, field=field).rstrip("/") + CHAT_COMPLETIONS_PATH


def _iter_keys(node):
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield str(key)
            yield from _iter_keys(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_keys(item)


def assert_no_thinking_parameters(body):
    """请求体不含 thinking/reasoning 参数，否则 ProviderError（DeepSeek 禁 thinking）。"""
    for key in _iter_keys(body):
        lowered = key.lower()
        if any(name in lowered for name in FORBIDDEN_PARAMETER_NAMES):
            raise ProviderError(
                f"请求体不得包含 thinking/reasoning 相关参数：{key}。",
                kind=base.KIND_VALIDATION,
                provider="llm",
                detail={"key": key},
            )
    return body


@dataclass(frozen=True)
class ChatRequest:
    """一次 Chat Completions 请求的纯数据描述（model/prompt 可追溯）。"""

    model: str
    prompt: str
    system: str | None
    url: str
    body: Mapping[str, Any]
    max_tokens: int
    timeout: float
    json_mode: bool
    stream: bool

    def as_body(self) -> dict:
        """返回请求体的深拷贝，调用方改动不会污染本对象。"""
        return json.loads(json.dumps(self.body, ensure_ascii=False))

    def headers(self, api_key: str) -> dict:
        return {"Authorization": f"Bearer {api_key}"}


def build_chat_request(
    *,
    model=None,
    payload=None,
    prompt=None,
    system=None,
    max_tokens=DEFAULT_MAX_TOKENS,
    timeout=DEFAULT_TIMEOUT_SECONDS,
    json_mode=True,
    stream=False,
    base_url=None,
    env=None,
) -> ChatRequest:
    """构造请求体；实际 model/prompt 与请求体一致且可从返回值追溯。"""
    source = base.require_configured("llm", env)
    resolved_model = str(model or source.get("LLM_MODEL") or "").strip()
    if not resolved_model:
        raise ProviderError(
            "文本模型名称未配置。", kind=base.KIND_CONFIG, missing=("LLM_MODEL",), provider="llm"
        )
    if (payload is None) == (prompt is None):
        raise ProviderError(
            "payload 与 prompt 必须且只能提供一个。",
            kind=base.KIND_VALIDATION,
            provider="llm",
        )
    if prompt is None:
        prompt = json.dumps(payload, ensure_ascii=False)
    if not isinstance(prompt, str) or not prompt:
        raise ProviderError("prompt 必须是非空字符串。", kind=base.KIND_VALIDATION, provider="llm")

    resolved_max_tokens = _validate_int(max_tokens, "max_tokens", MIN_MAX_TOKENS, MAX_MAX_TOKENS)
    resolved_timeout = _validate_float(timeout, "timeout", MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)

    target = chat_endpoint(base_url or source.get("LLM_BASE_URL"))

    messages = []
    system_text = system or ""
    if json_mode:
        system_text = (system_text + "\n只输出 JSON。").lstrip("\n")
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": resolved_model,
        "messages": messages,
        "max_tokens": resolved_max_tokens,
        "stream": bool(stream),
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if stream:
        body["stream_options"] = {"include_usage": True}
    assert_no_thinking_parameters(body)
    return ChatRequest(
        model=resolved_model,
        prompt=prompt,
        system=system_text or None,
        url=target,
        body=body,
        max_tokens=resolved_max_tokens,
        timeout=resolved_timeout,
        json_mode=bool(json_mode),
        stream=bool(stream),
    )


def _validate_int(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderError(
            f"{field} 必须是 {minimum}–{maximum} 的整数。",
            kind=base.KIND_VALIDATION,
            provider="llm",
            detail={"field": field},
        )
    if not minimum <= value <= maximum:
        raise ProviderError(
            f"{field} 超出 {minimum}–{maximum} 范围。",
            kind=base.KIND_VALIDATION,
            provider="llm",
            detail={"field": field, "value": value},
        )
    return value


def _validate_float(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderError(
            f"{field} 必须是 {minimum}–{maximum} 的数值。",
            kind=base.KIND_VALIDATION,
            provider="llm",
            detail={"field": field},
        )
    value = float(value)
    if not minimum <= value <= maximum:
        raise ProviderError(
            f"{field} 超出 {minimum}–{maximum} 范围。",
            kind=base.KIND_VALIDATION,
            provider="llm",
            detail={"field": field, "value": value},
        )
    return value


@dataclass(frozen=True)
class StreamOutcome:
    content: str
    usage: Mapping[str, Any]
    finish_reason: str | None
    buffered: bool
    raw: Mapping[str, Any] | None = None


def consume_stream(
    response,
    *,
    on_event=None,
    stall_timeout=None,
    clock=time.monotonic,
    max_bytes=MAX_OUTPUT_BYTES,
) -> StreamOutcome:
    """消费一次流式响应；SSE 增量、stall 超时、256KB 上限、usage 提取。

    非 `text/event-stream`（部分兼容网关无视 stream=true 返回 JSON）时按单个
    缓冲响应处理，绝不重发。
    """
    content_type = str(response.headers.get("content-type", "")).lower()
    if STREAM_CONTENT_TYPE not in content_type:
        return _consume_buffered(response, on_event=on_event, max_bytes=max_bytes)

    content = ""
    content_bytes = 0
    usage: dict = {}
    finish_reason = None
    signalled_reasoning = False
    pending: list[str] = []
    last_progress = clock()
    limit = None if stall_timeout is None else float(stall_timeout)

    def reject_stalled():
        if limit is not None and clock() - last_progress >= limit:
            raise ProviderError(
                "模型连接后在等待时限内没有返回内容，已停止空白等待；本次请求不会自动重发。",
                kind=base.KIND_TIMEOUT,
                provider="llm",
            )

    def events():
        for line in response.iter_lines():
            if line == "":
                if pending:
                    yield "\n".join(pending)
                    pending.clear()
                else:
                    reject_stalled()
            elif line.startswith("data:"):
                pending.append(line[5:].lstrip(" "))
            else:
                # SSE 注释行是传输心跳，不算模型进度。
                reject_stalled()
        if pending:
            yield "\n".join(pending)

    try:
        for event in events():
            if event.strip() == "[DONE]":
                break
            try:
                data = json.loads(event)
            except ValueError:
                raise ProviderError(
                    "流式响应不是合法的 JSON；本次请求不会自动重发。",
                    kind=base.KIND_FORMAT,
                    provider="llm",
                ) from None
            if not isinstance(data, dict):
                raise ProviderError(
                    "流式响应结构无效；本次请求不会自动重发。",
                    kind=base.KIND_FORMAT,
                    provider="llm",
                )
            if data.get("error"):
                raise ProviderError(
                    "模型输出途中返回错误；本次请求不会自动重发。",
                    kind=base.KIND_PROVIDER,
                    provider="llm",
                    detail=base.response_error_detail(_DictResponse(data)),
                )
            if isinstance(data.get("usage"), dict):
                usage = data["usage"]
            for choice in data.get("choices") or []:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or {}
                if (delta.get("reasoning_content") or delta.get("reasoning")) and not signalled_reasoning:
                    signalled_reasoning = True
                    if on_event is not None:
                        on_event("reasoning", "")
                if delta.get("reasoning_content") or delta.get("reasoning"):
                    last_progress = clock()
                chunk = delta.get("content")
                if isinstance(chunk, str) and chunk:
                    content += chunk
                    content_bytes += len(chunk.encode("utf-8"))
                    last_progress = clock()
                    if content_bytes > max_bytes:
                        raise ProviderError(
                            LIMIT_MESSAGE,
                            kind=base.KIND_FORMAT,
                            provider="llm",
                            detail={"bytes": content_bytes, "max_bytes": max_bytes},
                        )
                    if on_event is not None:
                        on_event("delta", content)
                if choice.get("finish_reason") is not None:
                    finish_reason = choice["finish_reason"]
                    last_progress = clock()
            reject_stalled()
    except base.TRANSPORT_ERRORS as exc:
        raise base.transport_error(exc, provider="llm") from None

    return StreamOutcome(
        content=content,
        usage=usage,
        finish_reason=finish_reason,
        buffered=False,
    )


class _DictResponse:
    """把已解析的 dict 包成 response 形状，供 response_error_detail 复用。"""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _consume_buffered(response, *, on_event=None, max_bytes=MAX_OUTPUT_BYTES) -> StreamOutcome:
    response.read()
    try:
        data = response.json()
    except (ValueError, TypeError):
        raise ProviderError(
            "文本接口未返回合法 JSON；本次请求不会自动重发。",
            kind=base.KIND_FORMAT,
            provider="llm",
        ) from None
    if on_event is not None:
        on_event("buffered", "")
    if not isinstance(data, dict):
        raise ProviderError(
            "文本接口响应结构无效；本次请求不会自动重发。",
            kind=base.KIND_FORMAT,
            provider="llm",
        )
    if data.get("error") and not data.get("choices"):
        raise ProviderError(
            "文本接口返回错误；本次请求不会自动重发。",
            kind=base.KIND_PROVIDER,
            provider="llm",
            detail=base.response_error_detail(response),
        )
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ProviderError(
            "文本接口未返回有效 choices；本次请求不会自动重发。",
            kind=base.KIND_FORMAT,
            provider="llm",
        )
    choice = choices[0]
    content = (choice.get("message") or {}).get("content") or ""
    if not isinstance(content, str):
        raise ProviderError(
            "文本接口 content 不是字符串；本次请求不会自动重发。",
            kind=base.KIND_FORMAT,
            provider="llm",
        )
    if len(content.encode("utf-8")) > max_bytes:
        raise ProviderError(
            LIMIT_MESSAGE,
            kind=base.KIND_FORMAT,
            provider="llm",
            detail={"bytes": len(content.encode("utf-8")), "max_bytes": max_bytes},
        )
    return StreamOutcome(
        content=content,
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
        finish_reason=choice.get("finish_reason"),
        buffered=True,
        raw=data,
    )


@dataclass(frozen=True)
class LlmResult:
    model: str
    prompt: str
    content: str
    parsed: Any
    usage: Mapping[str, Any]
    finish_reason: str | None
    request_url: str
    request_body: Mapping[str, Any]
    streamed: bool


def call_chat(
    request: ChatRequest,
    *,
    http=None,
    env=None,
    api_key=None,
    on_event=None,
    stall_timeout=None,
) -> LlmResult:
    """执行一次请求；**恰好一次**出网调用，失败即返回错误、绝不自动重发。"""
    source = base.require_configured("llm", env)
    resolved_key = api_key if api_key is not None else source.get("LLM_API_KEY")
    transport = http if http is not None else base.default_http()
    headers = request.headers(resolved_key)
    body = request.as_body()
    wait = request.timeout if stall_timeout is None else stall_timeout

    try:
        if request.stream:
            if on_event is not None:
                on_event("connecting", "")
            with transport.stream(
                "POST",
                request.url,
                headers=headers,
                json=body,
                timeout=request.timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code >= 300:
                    raise base.http_error(
                        response.status_code,
                        provider="llm",
                        detail=base.response_error_detail(response),
                    )
                if on_event is not None:
                    on_event("connected", "")
                outcome = consume_stream(
                    response, on_event=on_event, stall_timeout=wait
                )
        else:
            response = transport.post(
                request.url,
                headers=headers,
                json=body,
                timeout=request.timeout,
                follow_redirects=False,
            )
            if response.status_code >= 300:
                raise base.http_error(
                    response.status_code,
                    provider="llm",
                    detail=base.response_error_detail(response),
                )
            outcome = _consume_buffered(response, on_event=on_event)
    except ProviderError:
        raise
    except base.TRANSPORT_ERRORS as exc:
        raise base.transport_error(exc, provider="llm") from None

    finish_reason = outcome.finish_reason
    if finish_reason is None:
        raise ProviderError(
            "模型输出连接提前结束；本次请求不会自动重发。",
            kind=base.KIND_FORMAT,
            provider="llm",
        )
    if finish_reason != "stop":
        raise ProviderError(
            f"模型输出未正常完成（finish_reason={finish_reason}）。",
            kind=base.KIND_FORMAT,
            provider="llm",
        )

    parsed = None
    if request.json_mode:
        if on_event is not None:
            on_event("validating", outcome.content)
        try:
            parsed = json.loads(outcome.content)
        except (ValueError, TypeError):
            raise ProviderError(
                "语言模型未返回符合约定的 JSON。",
                kind=base.KIND_FORMAT,
                provider="llm",
            ) from None

    return LlmResult(
        model=request.model,
        prompt=request.prompt,
        content=outcome.content,
        parsed=parsed,
        usage=outcome.usage,
        finish_reason=finish_reason,
        request_url=request.url,
        request_body=body,
        streamed=request.stream,
    )
