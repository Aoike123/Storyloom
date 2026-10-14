"""JOB-01 供应商适配器验收测试（附录 09 §5/§6，ROOT-04 冻结）。

覆盖三个纯协议（文本 DeepSeek/OpenAI 兼容、图片硅基流动、视频 MiniMax H3 /
火山方舟 Tasks）的成功与失败、请求可追溯、边界拒绝与「失败不自动重发」。

纪律：全部使用注入的假 transport，绝不打真实端点（`test_adapters_never_touch_real_network`
把 httpx 与 socket 的出口全部封死）；本文件不 import 任何旧 backend 模块，
结尾用 AST + 源码扫描做负向断言。
"""

import ast
import base64
import inspect
import io
import json
import pathlib
import socket

import httpx
import pytest
from PIL import Image

from backend.infrastructure.providers import base, image, llm, video

LLM_ENV = {
    "LLM_BASE_URL": "https://api.deepseek.com",
    "LLM_MODEL": "deepseek-chat",
    "LLM_API_KEY": "test-llm-key",
}
IMAGE_ENV = {
    "IMAGE_PROVIDER": "siliconflow",
    "IMAGE_ENDPOINT": "https://api.siliconflow.cn/v1/images/generations",
    "IMAGE_MODEL": "Tongyi-MAI/Z-Image-Turbo",
    "IMAGE_API_KEY": "test-image-key",
}
VIDEO_ENV = {
    "VIDEO_PROVIDER": "minimax",
    "VIDEO_ENDPOINT": "https://api.minimax.cn/v2/video_generation",
    "VIDEO_MODEL": "MiniMax-H3",
    "VIDEO_API_KEY": "test-video-key",
}
SSE_HEADERS = {"content-type": "text/event-stream; charset=utf-8"}


@pytest.fixture(autouse=True)
def _provider_env(monkeypatch):
    """注入确定的环境快照，使缺省 env=None 的调用也绝不读真实凭证/端点。"""
    for key, value in {**LLM_ENV, **IMAGE_ENV, **VIDEO_ENV}.items():
        monkeypatch.setenv(key, value)


# ── 假 HTTP ─────────────────────────────────────────────────────────────
def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


class _Queue:
    def __init__(self, responses=(), errors=()):
        self.responses = list(responses)
        self.errors = list(errors)
        self.count = 0

    def next(self):
        self.count += 1
        if self.errors:
            raise self.errors.pop(0)
        if self.responses:
            return self.responses.pop(0)
        raise AssertionError("假 transport 收到未预期的调用")


class _StreamCtx:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc_info):
        return False


class FakeResponse:
    def __init__(
        self,
        *,
        status_code=200,
        headers=None,
        json_body=None,
        json_error=None,
        lines=(),
        chunks=(),
        read_body=b"",
    ):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._json_body = json_body
        self._json_error = json_error
        self._lines = list(lines)
        self._chunks = list(chunks)
        self._read_body = read_body

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json_body

    def read(self):
        return self._read_body

    def iter_lines(self):
        yield from self._lines

    def iter_bytes(self):
        yield from self._chunks


class FakeHttp:
    """注入式 transport：与 httpx 模块同形（post/get/stream）。"""

    def __init__(
        self,
        *,
        post=None,
        get=None,
        stream=None,
        post_errors=(),
        get_errors=(),
        stream_errors=(),
    ):
        self.posts = _Queue(_as_list(post), post_errors)
        self.gets = _Queue(_as_list(get), get_errors)
        self.streams = _Queue(_as_list(stream), stream_errors)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.posts.next()

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.gets.next()

    def stream(self, method, url, **kwargs):
        self.calls.append(("stream", method, url, kwargs))
        return _StreamCtx(self.streams.next())

    @property
    def total(self):
        return len(self.calls)

    def methods(self):
        return [call[0] for call in self.calls]


def sse(*events, done=True):
    lines = []
    for event in events:
        lines.append("data: " + json.dumps(event, ensure_ascii=False))
        lines.append("")
    if done:
        lines.append("data: [DONE]")
        lines.append("")
    return lines


def delta_event(text):
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def finish_event(reason="stop"):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def usage_event(total=10):
    return {
        "choices": [],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": total},
    }


def make_image_bytes(width, height, fmt="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 40)).save(buffer, format=fmt)
    return buffer.getvalue()


# ── base ────────────────────────────────────────────────────────────────
class TestBase:
    def test_provider_error_metadata_is_serializable(self):
        err = base.ProviderError("限流", kind=base.KIND_RATE_LIMIT, status=429, provider="image")
        payload = err.to_dict()
        assert json.loads(json.dumps(payload))["kind"] == base.KIND_RATE_LIMIT
        assert payload["status"] == 429
        assert payload["message"] == "限流"
        assert payload["retryable"] is True
        assert isinstance(err, Exception) and str(err) == "限流"

    def test_retryable_metadata_follows_billing_risk(self):
        # 结果不确定或厂商业务拒绝：重试有重复计费风险 → 不允许自动重发。
        assert base.ProviderError("网络", kind=base.KIND_NETWORK).retryable is False
        assert base.ProviderError("超时", kind=base.KIND_TIMEOUT).retryable is False
        assert base.ProviderError("认证", kind=base.KIND_AUTH, status=401).retryable is False
        assert base.ProviderError("厂商", kind=base.KIND_PROVIDER, status=500).retryable is False
        assert base.ProviderError("格式", kind=base.KIND_FORMAT).retryable is False
        # 本地未出网 / 厂商明确拒绝：重试安全。
        assert base.ProviderError("校验", kind=base.KIND_VALIDATION).retryable is True
        assert base.ProviderError("配置", kind=base.KIND_CONFIG).retryable is True
        assert base.ProviderError("限流", kind=base.KIND_RATE_LIMIT).retryable is True

    def test_classify_http_status(self):
        assert base.classify_http_status(401) == base.KIND_AUTH
        assert base.classify_http_status(403) == base.KIND_AUTH
        assert base.classify_http_status(429) == base.KIND_RATE_LIMIT
        assert base.classify_http_status(400) == base.KIND_PROVIDER
        assert base.classify_http_status(500) == base.KIND_PROVIDER

    def test_classify_transport_error(self):
        assert base.classify_transport_error(httpx.ConnectTimeout("x")) == base.KIND_TIMEOUT
        assert base.classify_transport_error(httpx.ReadTimeout("x")) == base.KIND_TIMEOUT
        assert base.classify_transport_error(httpx.ConnectError("x")) == base.KIND_NETWORK
        assert base.classify_transport_error(httpx.RemoteProtocolError("x")) == base.KIND_NETWORK

    @pytest.mark.parametrize(
        "bad",
        [
            "http://api.example/v1",
            "https://user:pass@api.example/v1",
            "https://api.example/v1?token=1",
            "https://api.example/v1#frag",
            "https://",
            "",
        ],
    )
    def test_require_https_endpoint_rejects(self, bad):
        with pytest.raises(base.ProviderError) as caught:
            base.require_https_endpoint(bad, field="IMAGE_ENDPOINT")
        assert caught.value.kind in (base.KIND_VALIDATION, base.KIND_CONFIG)
        assert "IMAGE_ENDPOINT" in caught.value.message

    def test_require_https_endpoint_accepts_clean_https(self):
        url = "https://api.siliconflow.cn/v1/images/generations"
        assert base.require_https_endpoint(url, field="IMAGE_ENDPOINT") == url

    def test_require_https_url_allows_signed_query(self):
        url = "https://cdn.example/a.png?sign=abc&t=1"
        assert base.require_https_url(url, field="图片下载地址") == url
        with pytest.raises(base.ProviderError) as caught:
            base.require_https_url("http://cdn.example/a.png", field="图片下载地址")
        assert caught.value.kind == base.KIND_VALIDATION

    def test_missing_env_names_are_specific(self):
        assert base.missing_env("llm", {"LLM_BASE_URL": "https://x"}) == (
            "LLM_MODEL",
            "LLM_API_KEY",
        )
        assert base.is_configured("llm", LLM_ENV) is True
        # provider 取值受支持才算就绪。
        assert base.missing_env("image", {**IMAGE_ENV, "IMAGE_PROVIDER": "openai"}) == (
            "IMAGE_PROVIDER",
        )
        assert base.missing_env("video", {**VIDEO_ENV, "VIDEO_PROVIDER": "ark"}) == ()
        assert base.missing_env("video", {**VIDEO_ENV, "VIDEO_PROVIDER": "foo"}) == (
            "VIDEO_PROVIDER",
        )
        assert base.missing_env("image", {}) == (
            "IMAGE_PROVIDER",
            "IMAGE_ENDPOINT",
            "IMAGE_MODEL",
            "IMAGE_API_KEY",
        )

    def test_require_configured_reports_specific_missing_key(self):
        with pytest.raises(base.ProviderError) as caught:
            base.require_configured("image", {})
        assert caught.value.kind == base.KIND_CONFIG
        assert caught.value.missing == (
            "IMAGE_PROVIDER",
            "IMAGE_ENDPOINT",
            "IMAGE_MODEL",
            "IMAGE_API_KEY",
        )
        assert "IMAGE_API_KEY" in caught.value.message

    def test_unknown_provider_kind_is_validation_error(self):
        with pytest.raises(base.ProviderError) as caught:
            base.missing_env("audio", {})
        assert caught.value.kind == base.KIND_VALIDATION


# ── llm ─────────────────────────────────────────────────────────────────
class TestLlm:
    def _request(self, **overrides):
        kwargs = dict(
            payload={"brief": "写一个开头"}, env=LLM_ENV, max_tokens=512, timeout=30
        )
        kwargs.update(overrides)
        return llm.build_chat_request(**kwargs)

    def test_build_request_is_json_mode_and_traceable(self):
        request = self._request(stream=True)
        assert request.url == "https://api.deepseek.com/chat/completions"
        body = request.as_body()
        assert body["model"] == "deepseek-chat"
        assert body["response_format"] == {"type": "json_object"}
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert json.loads(body["messages"][-1]["content"]) == {"brief": "写一个开头"}
        assert request.prompt == json.dumps({"brief": "写一个开头"}, ensure_ascii=False)

    def test_explicit_model_overrides_env_and_stays_traceable(self):
        request = self._request(model="deepseek-reasoner")
        assert request.model == "deepseek-reasoner"
        assert request.as_body()["model"] == "deepseek-reasoner"

    def test_deepseek_body_has_no_thinking_parameters(self):
        for model in ("deepseek-chat", "deepseek-reasoner"):
            request = self._request(model=model)
            text = json.dumps(request.as_body(), ensure_ascii=False)
            assert "thinking" not in text
            assert "reasoning" not in text
        with pytest.raises(base.ProviderError) as caught:
            llm.assert_no_thinking_parameters(
                {"model": "m", "thinking": {"type": "disabled"}}
            )
        assert caught.value.kind == base.KIND_VALIDATION
        with pytest.raises(base.ProviderError):
            llm.assert_no_thinking_parameters({"model": "m", "x": {"reasoning_effort": "low"}})

    def test_stream_sse_path_extracts_deltas_usage_and_json(self):
        events = []
        fake = FakeHttp(
            stream=FakeResponse(
                headers=SSE_HEADERS,
                lines=sse(
                    delta_event('{"title": "'),
                    delta_event('你好，世界"}'),
                    finish_event("stop"),
                    usage_event(total=10),
                ),
            )
        )
        result = llm.call_chat(
            self._request(stream=True), http=fake, on_event=lambda k, c: events.append((k, c))
        )
        assert result.streamed is True
        assert result.content == '{"title": "你好，世界"}'
        assert result.parsed == {"title": "你好，世界"}
        assert result.usage["total_tokens"] == 10
        assert result.model == "deepseek-chat"
        assert result.prompt == json.dumps({"brief": "写一个开头"}, ensure_ascii=False)
        assert result.request_body["model"] == "deepseek-chat"
        assert fake.streams.count == 1 and fake.posts.count == 0
        assert ("delta", '{"title": "') in events
        assert ("delta", '{"title": "你好，世界"}') in events

    def test_buffered_json_path_parses_json_mode(self):
        fake = FakeHttp(
            post=FakeResponse(
                json_body={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": '{"ok": true}'}}
                    ],
                    "usage": {"total_tokens": 2},
                }
            )
        )
        result = llm.call_chat(self._request(), http=fake)
        assert result.parsed == {"ok": True}
        assert result.usage["total_tokens"] == 2
        assert result.streamed is False
        assert fake.posts.count == 1

    def test_buffered_gateway_returning_json_despite_stream_true(self):
        fake = FakeHttp(
            stream=FakeResponse(
                headers={"content-type": "application/json"},
                json_body={
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": 1}'}}],
                    "usage": {"total_tokens": 1},
                },
                read_body=b"{}",
            )
        )
        result = llm.call_chat(self._request(stream=True), http=fake)
        assert result.parsed == {"ok": 1}
        assert result.streamed is True
        assert fake.streams.count == 1

    def test_invalid_json_mode_content_is_format_error_without_retry(self):
        fake = FakeHttp(
            post=FakeResponse(
                json_body={
                    "choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]
                }
            )
        )
        with pytest.raises(base.ProviderError) as caught:
            llm.call_chat(self._request(), http=fake)
        assert caught.value.kind == base.KIND_FORMAT
        assert fake.posts.count == 1

    def test_http_failure_is_classified_and_not_retried(self):
        fake = FakeHttp(
            post=FakeResponse(status_code=500, json_body={"error": {"message": "boom"}})
        )
        with pytest.raises(base.ProviderError) as caught:
            llm.call_chat(self._request(), http=fake)
        assert caught.value.kind == base.KIND_PROVIDER
        assert caught.value.status == 500
        assert "boom" in caught.value.message
        assert fake.posts.count == 1

    def test_transport_failure_is_classified_and_not_retried(self):
        fake = FakeHttp(post_errors=[httpx.ConnectError("refused")])
        with pytest.raises(base.ProviderError) as caught:
            llm.call_chat(self._request(), http=fake)
        assert caught.value.kind == base.KIND_NETWORK
        assert caught.value.retryable is False
        assert fake.posts.count == 1

    @pytest.mark.parametrize(
        ("status", "kind"),
        [(401, base.KIND_AUTH), (403, base.KIND_AUTH), (429, base.KIND_RATE_LIMIT)],
    )
    def test_auth_and_rate_limit_classification(self, status, kind):
        fake = FakeHttp(
            stream=FakeResponse(status_code=status, json_body={"error": {"message": "no"}})
        )
        with pytest.raises(base.ProviderError) as caught:
            llm.call_chat(self._request(stream=True), http=fake)
        assert caught.value.kind == kind
        assert fake.streams.count == 1 and fake.posts.count == 0

    def test_stall_timeout(self):
        fake = FakeHttp(stream=FakeResponse(headers=SSE_HEADERS, lines=[""]))
        with pytest.raises(base.ProviderError) as caught:
            llm.call_chat(self._request(stream=True), http=fake, stall_timeout=0)
        assert caught.value.kind == base.KIND_TIMEOUT
        assert fake.streams.count == 1

    def test_256kb_output_limit(self):
        assert llm.MAX_OUTPUT_BYTES == 256 * 1024
        fake = FakeHttp(
            stream=FakeResponse(
                headers=SSE_HEADERS,
                lines=sse(delta_event("a" * (llm.MAX_OUTPUT_BYTES + 1))),
            )
        )
        with pytest.raises(base.ProviderError) as caught:
            llm.call_chat(self._request(stream=True), http=fake)
        assert caught.value.kind == base.KIND_FORMAT
        assert "256KB" in caught.value.message
        assert fake.streams.count == 1

    def test_premature_end_is_format_error(self):
        fake = FakeHttp(
            stream=FakeResponse(
                headers=SSE_HEADERS, lines=sse(delta_event('{"a": 1}'), done=False)
            )
        )
        with pytest.raises(base.ProviderError) as caught:
            llm.call_chat(self._request(stream=True), http=fake)
        assert caught.value.kind == base.KIND_FORMAT
        assert fake.streams.count == 1

    def test_config_missing_reports_exact_key(self):
        env = {"LLM_BASE_URL": "https://api.deepseek.com", "LLM_MODEL": "deepseek-chat"}
        with pytest.raises(base.ProviderError) as caught:
            llm.build_chat_request(payload={"a": 1}, env=env)
        assert caught.value.kind == base.KIND_CONFIG
        assert caught.value.missing == ("LLM_API_KEY",)
        assert "LLM_API_KEY" in caught.value.message

    def test_non_https_base_url_rejected(self):
        with pytest.raises(base.ProviderError) as caught:
            llm.build_chat_request(
                payload={"a": 1}, env={**LLM_ENV, "LLM_BASE_URL": "http://api.deepseek.com"}
            )
        assert caught.value.kind == base.KIND_VALIDATION
        assert "LLM_BASE_URL" in caught.value.message

    def test_prompt_and_payload_are_mutually_exclusive(self):
        with pytest.raises(base.ProviderError):
            llm.build_chat_request(payload={"a": 1}, prompt="x", env=LLM_ENV)
        with pytest.raises(base.ProviderError):
            llm.build_chat_request(env=LLM_ENV)

    def test_max_tokens_and_timeout_bounds(self):
        for bad in (255, 32769, "512", 512.0, True):
            with pytest.raises(base.ProviderError):
                self._request(max_tokens=bad)
        for bad in (9, 601, "30"):
            with pytest.raises(base.ProviderError):
                self._request(timeout=bad)
        assert self._request(max_tokens=256, timeout=10).max_tokens == 256


# ── image ───────────────────────────────────────────────────────────────
class TestImage:
    def test_text_to_image_is_1024_and_traceable(self):
        request = image.build_image_request(prompt="一只猫", env=IMAGE_ENV)
        assert request.url == IMAGE_ENV["IMAGE_ENDPOINT"]
        assert request.body["image_size"] == "1024x1024"
        assert request.body["model"] == IMAGE_ENV["IMAGE_MODEL"]
        assert request.mode == "text_to_image" and request.reference_count == 0
        with pytest.raises(base.ProviderError):
            image.build_image_request(prompt="一只猫", env=IMAGE_ENV, image_size="512x512")

    def test_generate_success(self):
        fake = FakeHttp(
            post=FakeResponse(json_body={"images": [{"url": "https://cdn.example/a.png"}]})
        )
        result = image.generate_image(prompt="一只猫", env=IMAGE_ENV, http=fake)
        assert result.url == "https://cdn.example/a.png"
        assert result.model == IMAGE_ENV["IMAGE_MODEL"]
        assert result.prompt == "一只猫"
        assert result.mode == "text_to_image"
        assert fake.posts.count == 1
        sent = fake.calls[0][2]
        assert sent["json"]["model"] == IMAGE_ENV["IMAGE_MODEL"]
        assert sent["headers"]["Authorization"].startswith("Bearer ")

    @pytest.mark.parametrize(
        ("status", "kind"),
        [(401, base.KIND_AUTH), (429, base.KIND_RATE_LIMIT), (400, base.KIND_PROVIDER)],
    )
    def test_generate_failures_are_classified_and_not_retried(self, status, kind):
        fake = FakeHttp(post=FakeResponse(status_code=status, json_body={"message": "bad key"}))
        with pytest.raises(base.ProviderError) as caught:
            image.generate_image(prompt="x", env=IMAGE_ENV, http=fake)
        assert caught.value.kind == kind
        assert fake.posts.count == 1

    def test_generate_transport_failure_uncertain(self):
        fake = FakeHttp(post_errors=[httpx.ReadTimeout("slow")])
        with pytest.raises(base.ProviderError) as caught:
            image.generate_image(prompt="x", env=IMAGE_ENV, http=fake)
        assert caught.value.kind == base.KIND_TIMEOUT
        assert caught.value.retryable is False
        assert fake.posts.count == 1

    @pytest.mark.parametrize(
        "body",
        [
            {"images": []},
            {},
            {"images": [{"url": "http://cdn.example/a.png"}]},
            {"images": [{"url": None}]},
        ],
    )
    def test_generate_response_format_errors(self, body):
        fake = FakeHttp(post=FakeResponse(json_body=body))
        with pytest.raises(base.ProviderError) as caught:
            image.generate_image(prompt="x", env=IMAGE_ENV, http=fake)
        assert caught.value.kind in (base.KIND_FORMAT, base.KIND_VALIDATION)
        assert fake.posts.count == 1

    def test_unconfigured_or_wrong_provider_is_config_error(self):
        env = {k: v for k, v in IMAGE_ENV.items() if k != "IMAGE_API_KEY"}
        with pytest.raises(base.ProviderError) as caught:
            image.generate_image(prompt="x", env=env)
        assert caught.value.kind == base.KIND_CONFIG
        assert caught.value.missing == ("IMAGE_API_KEY",)
        wrong = {**IMAGE_ENV, "IMAGE_PROVIDER": "openai"}
        with pytest.raises(base.ProviderError) as caught2:
            image.generate_image(prompt="x", env=wrong)
        assert caught2.value.kind == base.KIND_CONFIG

    def test_reference_image_count_boundaries(self):
        ref = image.DATA_URI_PREFIX + "AAAA"
        with pytest.raises(base.ProviderError) as caught:
            image.build_image_request(prompt="x", references=[], env=IMAGE_ENV)
        assert caught.value.kind == base.KIND_VALIDATION
        one = image.build_image_request(prompt="x", references=[ref], env=IMAGE_ENV)
        assert one.reference_count == 1 and one.mode == "reference_images"
        assert one.body["image"] == ref and "image2" not in one.body
        nine = image.build_image_request(prompt="x", references=[ref] * 9, env=IMAGE_ENV)
        assert nine.reference_count == 9 and nine.body["image9"] == ref
        with pytest.raises(base.ProviderError) as caught2:
            image.build_image_request(prompt="x", references=[ref] * 10, env=IMAGE_ENV)
        assert caught2.value.detail["reference_count"] == 10
        with pytest.raises(base.ProviderError):
            image.build_image_request(prompt="x", references=["not-a-data-uri"], env=IMAGE_ENV)

    def test_reference_steps_boundaries(self):
        ref = image.DATA_URI_PREFIX + "AAAA"
        assert image.MIN_STEPS == 1 and image.MAX_STEPS == 100
        for bad in (0, 101, -1, "50", 50.0, True):
            with pytest.raises(base.ProviderError) as caught:
                image.build_image_request(prompt="x", references=[ref], steps=bad, env=IMAGE_ENV)
            assert caught.value.kind == base.KIND_VALIDATION
        assert (
            image.build_image_request(prompt="x", references=[ref], steps=1, env=IMAGE_ENV).body[
                "num_inference_steps"
            ]
            == 1
        )
        assert (
            image.build_image_request(prompt="x", references=[ref], steps=100, env=IMAGE_ENV).body[
                "num_inference_steps"
            ]
            == 100
        )
        default = image.build_image_request(prompt="x", references=[ref], env=IMAGE_ENV)
        assert default.body["num_inference_steps"] == image.DEFAULT_STEPS
        assert image.MAX_REFERENCE_IMAGES == 9

    def test_download_is_https_only(self):
        fake = FakeHttp(stream=FakeResponse(chunks=[b"x"]))
        with pytest.raises(base.ProviderError) as caught:
            image.download_image_bytes("http://cdn.example/a.png", http=fake)
        assert caught.value.kind == base.KIND_VALIDATION
        assert fake.streams.count == 0

    def test_download_size_guard(self):
        assert image.MAX_IMAGE_BYTES == 20 * 1024 * 1024
        fake = FakeHttp(stream=FakeResponse(chunks=[b"x" * 6, b"y" * 6]))
        with pytest.raises(base.ProviderError) as caught:
            image.download_image_bytes(
                "https://cdn.example/a.png", http=fake, max_bytes=10
            )
        assert caught.value.kind == base.KIND_VALIDATION
        assert fake.streams.count == 1

    def test_download_http_error_is_classified(self):
        fake = FakeHttp(stream=FakeResponse(status_code=403, json_body={"message": "denied"}))
        with pytest.raises(base.ProviderError) as caught:
            image.download_image_bytes("https://cdn.example/a.png", http=fake)
        assert caught.value.kind == base.KIND_AUTH
        assert "denied" in caught.value.message
        assert fake.streams.count == 1

    @pytest.mark.parametrize(
        ("width", "height", "rule"),
        [
            (255, 255, "min_edge"),
            (5761, 3000, "max_edge"),
            (256, 1024, "aspect_ratio"),
            (1024, 256, "aspect_ratio"),
        ],
    )
    def test_dimension_validation_rejects(self, width, height, rule):
        with pytest.raises(base.ProviderError) as caught:
            image.validate_image_size(width, height)
        assert caught.value.kind == base.KIND_VALIDATION
        assert caught.value.detail["rule"] == rule

    @pytest.mark.parametrize(
        ("width", "height"),
        [(256, 256), (256, 640), (640, 256), (5760, 5760), (5760, 3000)],
    )
    def test_dimension_validation_accepts_boundaries(self, width, height):
        assert image.validate_image_size(width, height) == (width, height)

    def test_normalize_png_converts_to_png(self):
        normalized = image.normalize_png(make_image_bytes(300, 300, fmt="JPEG"))
        assert normalized.media_type == "image/png"
        assert normalized.data.startswith(b"\x89PNG\r\n\x1a\n")
        assert (normalized.width, normalized.height) == (300, 300)
        with Image.open(io.BytesIO(normalized.data)) as reopened:
            assert reopened.format == "PNG"

    def test_normalize_png_rejects_corrupt_and_oversize(self):
        with pytest.raises(base.ProviderError) as caught:
            image.normalize_png(b"not an image")
        assert caught.value.kind == base.KIND_FORMAT
        with pytest.raises(base.ProviderError) as caught2:
            image.normalize_png(make_image_bytes(300, 300), max_bytes=10)
        assert caught2.value.kind == base.KIND_VALIDATION
        with pytest.raises(base.ProviderError):
            image.normalize_png(make_image_bytes(128, 128))

    def test_fetch_png_downloads_and_normalizes(self):
        raw = make_image_bytes(256, 256)
        fake = FakeHttp(stream=FakeResponse(chunks=[raw[:10], raw[10:]]))
        normalized = image.fetch_png(
            "https://cdn.example/a.png", http=fake, max_bytes=5 * 1024 * 1024
        )
        assert normalized.data.startswith(b"\x89PNG")
        assert fake.streams.count == 1
        with pytest.raises(base.ProviderError):
            image.fetch_png("http://cdn.example/a.png", http=FakeHttp())

    def test_encode_reference_bytes_returns_png_data_uri(self):
        uri = image.encode_reference_bytes(make_image_bytes(300, 300, fmt="JPEG"))
        assert uri.startswith(image.DATA_URI_PREFIX)
        payload = base64.b64decode(uri.removeprefix(image.DATA_URI_PREFIX))
        with Image.open(io.BytesIO(payload)) as reopened:
            assert reopened.format == "PNG"


# ── video ───────────────────────────────────────────────────────────────
class TestVideo:
    def _request(self, **overrides):
        kwargs = dict(prompt="镜头一", duration_seconds=8, env=VIDEO_ENV)
        kwargs.update(overrides)
        return video.build_video_request(**kwargs)

    def test_build_minimax_request(self):
        request = self._request(references=["https://cdn.example/r1.png"])
        assert request.provider == "minimax"
        assert request.body["duration"] == 8
        assert request.body["ratio"] == "16:9"
        assert request.body["resolution"] == "768P"
        assert request.body["content"][0] == {"type": "text", "text": "镜头一"}
        assert request.body["content"][1]["image_url"] == {"url": "https://cdn.example/r1.png"}
        assert request.reference_count == 1
        assert request.duration_seconds == 8

    def test_duration_boundaries(self):
        for bad in (5, 11, 0, -1, 8.5, "abc", True, "8.0"):
            with pytest.raises(base.ProviderError) as caught:
                self._request(duration_seconds=bad)
            assert caught.value.kind == base.KIND_VALIDATION
        assert video.MIN_DURATION_SECONDS == 6 and video.MAX_DURATION_SECONDS == 10
        assert self._request(duration_seconds=6).duration_seconds == 6
        assert self._request(duration_seconds=10).duration_seconds == 10
        assert self._request(duration_seconds="8").duration_seconds == 8

    def test_reference_boundaries_and_https_or_approved_local(self):
        ref = "https://cdn.example/r.png"
        assert video.MAX_REFERENCE_IMAGES == 6
        with pytest.raises(base.ProviderError) as caught:
            self._request(references=[ref] * 7)
        assert caught.value.kind == base.KIND_VALIDATION
        assert caught.value.detail["reference_count"] == 7
        assert self._request(references=[ref] * 6).reference_count == 6
        assert self._request(references=None).reference_count == 0
        with pytest.raises(base.ProviderError):
            self._request(references=["http://cdn.example/r.png"])
        local = video.LOCAL_DATA_PREFIX + "AAAA"
        with pytest.raises(base.ProviderError):
            self._request(references=[local])
        assert self._request(references=[local], approved_local=True).reference_count == 1
        assert (
            self._request(references=["/data/reviewed/frame.png"], approved_local=True).reference_count
            == 1
        )

    def test_request_size_guard(self):
        assert video.MAX_REQUEST_BYTES == 64 * 1024 * 1024
        with pytest.raises(base.ProviderError) as caught:
            video.check_request_size({"a": "x" * 20}, max_bytes=10)
        assert caught.value.kind == base.KIND_VALIDATION
        assert video.request_size({"a": "x"}) == video.check_request_size({"a": "x"})
        with pytest.raises(base.ProviderError):
            self._request(max_request_bytes=10)

    def test_submit_success_and_traceability(self):
        fake = FakeHttp(post=FakeResponse(json_body={"task_id": "mm-123"}))
        submission = video.submit_video(self._request(), http=fake)
        assert submission.task_id == "mm-123"
        assert submission.model == "MiniMax-H3"
        assert submission.prompt == "镜头一"
        assert submission.duration_seconds == 8
        assert submission.request_body["duration"] == 8
        assert fake.posts.count == 1

    def test_submit_missing_task_id_is_format_error(self):
        fake = FakeHttp(post=FakeResponse(json_body={"foo": "bar"}))
        with pytest.raises(base.ProviderError) as caught:
            video.submit_video(self._request(), http=fake)
        assert caught.value.kind == base.KIND_FORMAT
        assert fake.posts.count == 1

    def test_submit_uncertain_failure_is_not_retried(self):
        fake = FakeHttp(post_errors=[httpx.ReadTimeout("slow")])
        with pytest.raises(base.ProviderError) as caught:
            video.submit_video(self._request(), http=fake)
        assert caught.value.kind == base.KIND_TIMEOUT
        assert caught.value.retryable is False
        assert "未重试" in caught.value.message
        assert fake.posts.count == 1

    def test_advance_submits_when_no_provider_task_id(self):
        fake = FakeHttp(post=FakeResponse(json_body={"task_id": "mm-1"}))
        result = video.advance_video(self._request(), http=fake)
        assert isinstance(result, video.VideoSubmission)
        assert fake.methods() == ["post"]

    def test_existing_task_id_polls_only_and_never_resubmits(self):
        fake = FakeHttp(
            get=FakeResponse(
                json_body={
                    "task": {"status": "Success", "content": {"url": "https://cdn.example/v.mp4"}}
                }
            )
        )
        result = video.advance_video(self._request(), provider_task_id="mm-1", http=fake)
        assert isinstance(result, video.VideoPoll)
        assert fake.posts.count == 0
        assert fake.gets.count == 1
        assert fake.methods() == ["get"]
        assert result.status == "Success"
        assert result.video_url == "https://cdn.example/v.mp4"
        assert result.done is True and result.failed is False
        assert fake.calls[0][1].endswith("/v2/query/video_generation/mm-1")
        assert video.plan_video_action("mm-1") == "poll"
        assert video.plan_video_action(None) == "submit"
        assert video.plan_video_action("") == "submit"

    def test_poll_failure_classification(self):
        fake = FakeHttp(get_errors=[httpx.ConnectError("refused")])
        with pytest.raises(base.ProviderError) as caught:
            video.poll_video("mm-1", provider="minimax", env=VIDEO_ENV, http=fake)
        assert caught.value.kind == base.KIND_NETWORK
        assert caught.value.retryable is False
        with pytest.raises(base.ProviderError) as caught2:
            video.poll_video(
                "mm-1",
                provider="minimax",
                env=VIDEO_ENV,
                http=FakeHttp(get=FakeResponse(status_code=500)),
            )
        assert caught2.value.kind == base.KIND_PROVIDER
        invalid = FakeHttp(get=FakeResponse(json_body={}))
        with pytest.raises(base.ProviderError) as caught3:
            video.poll_video("bad id!", provider="minimax", env=VIDEO_ENV, http=invalid)
        assert caught3.value.kind == base.KIND_VALIDATION
        assert invalid.gets.count == 0

    def test_poll_failed_task_status(self):
        fake = FakeHttp(get=FakeResponse(json_body={"task": {"status": "Fail"}}))
        result = video.poll_video("mm-1", provider="minimax", env=VIDEO_ENV, http=fake)
        assert result.done is True and result.failed is True
        assert result.video_url is None

    def test_ark_provider_submit_and_poll_paths(self):
        env = {**VIDEO_ENV, "VIDEO_PROVIDER": "ark", "VIDEO_MODEL": "doubao-seedance-1-0"}
        request = self._request(env=env)
        assert "duration" not in request.as_body()
        fake = FakeHttp(
            post=FakeResponse(json_body={"id": "ark-9"}),
            get=FakeResponse(
                json_body={
                    "status": "succeeded",
                    "content": {"video_url": "https://cdn.example/a.mp4"},
                }
            ),
        )
        assert video.submit_video(request, http=fake).task_id == "ark-9"
        polled = video.poll_video("ark-9", provider="ark", env=env, http=fake)
        assert polled.done is True
        assert polled.video_url == "https://cdn.example/a.mp4"
        assert fake.calls[1][1] == env["VIDEO_ENDPOINT"] + "/ark-9"

    def test_video_config_missing_reports_key(self):
        env = {k: v for k, v in VIDEO_ENV.items() if k != "VIDEO_API_KEY"}
        with pytest.raises(base.ProviderError) as caught:
            video.build_video_request(prompt="x", duration_seconds=8, env=env)
        assert caught.value.kind == base.KIND_CONFIG
        assert caught.value.missing == ("VIDEO_API_KEY",)


# ── 出网与旧依赖负向断言 ────────────────────────────────────────────────
def test_adapters_never_touch_real_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("适配器尝试真实出网")

    monkeypatch.setattr(httpx, "post", boom)
    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(httpx, "stream", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    assert base.default_http() is httpx

    llm_fake = FakeHttp(
        stream=FakeResponse(
            headers=SSE_HEADERS,
            lines=sse(delta_event('{"a": 1}'), finish_event("stop")),
        )
    )
    llm_result = llm.call_chat(
        llm.build_chat_request(payload={"a": 1}, env=LLM_ENV, stream=True), http=llm_fake
    )
    assert llm_result.parsed == {"a": 1}

    image_fake = FakeHttp(
        post=FakeResponse(json_body={"images": [{"url": "https://cdn.example/a.png"}]})
    )
    assert image.generate_image(prompt="x", env=IMAGE_ENV, http=image_fake).url.startswith(
        "https://"
    )

    video_fake = FakeHttp(post=FakeResponse(json_body={"task_id": "mm-1"}))
    submission = video.submit_video(
        video.build_video_request(prompt="x", duration_seconds=8, env=VIDEO_ENV),
        http=video_fake,
    )
    assert submission.task_id == "mm-1"
    assert (llm_fake.total, image_fake.total, video_fake.total) == (1, 1, 1)


BANNED_MODULES = (
    "backend.providers",
    "backend.image_provider",
    "backend.llm_stream",
    "backend.workflows",
    "backend.worker",
    "backend.db",
    "backend.provider_usage",
    "backend.diagnostics",
)
BANNED_RELATIVE = (
    "providers",
    "image_provider",
    "llm_stream",
    "workflows",
    "worker",
    "db",
    "provider_usage",
    "diagnostics",
)


def test_adapter_sources_have_no_legacy_backend_imports():
    for module in (base, llm, image, video):
        source = inspect.getsource(module)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in BANNED_MODULES, f"{module.__name__} → {alias.name}"
                    assert not alias.name.startswith("backend."), f"{module.__name__} → {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                name = node.module or ""
                if node.level:
                    assert name not in BANNED_RELATIVE, f"{module.__name__} → .{name}"
                else:
                    assert name not in BANNED_MODULES and not name.startswith("backend.")
        for banned in BANNED_MODULES:
            assert banned not in source, f"{module.__name__} 源码引用旧栈 {banned}"
        assert "sqlalchemy" not in source.lower()
        # 不建 StudioJob：既无属性，也不在代码里引用该名字（纯文档里的否定陈述不算）。
        assert not hasattr(module, "StudioJob")
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "StudioJob", module.__name__
            elif isinstance(node, ast.Attribute):
                assert node.attr != "StudioJob", module.__name__


def test_forbidden_module_files_were_not_created():
    package_dir = pathlib.Path(base.__file__).parent
    assert not (package_dir / "client.py").exists()
    assert not (package_dir / "protocols.py").exists()
