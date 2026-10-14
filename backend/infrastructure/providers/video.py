"""MiniMax H3 / 火山方舟 Tasks 视频协议：提交 + 轮询、64MB 守卫、时长与参考图边界。

只读抽取自旧 `providers.submit_video/poll_video`，改写依赖：
- 不 import 旧 backend 业务模块，不写生成请求记录、不做计费登记；
- 保留纯逻辑：提交 + 轮询协议、64MB 请求守卫、参考图 https 或已审本地路径；
- 时长 6–10s、参考图 ≤6，超出即明确 ProviderError，绝不静默截断；
- **已有厂商任务号则只轮询、绝不重新提交**（`plan_video_action` / `advance_video`）。
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from . import base
from .base import ProviderError

VIDEO_ENV_KEYS = base.REQUIRED_ENV["video"]
MINIMAX_PROVIDER = "minimax"
ARK_PROVIDER = "ark"
PROVIDER_TASK_FIELD = {MINIMAX_PROVIDER: "task_id", ARK_PROVIDER: "id"}

MIN_DURATION_SECONDS = 6
MAX_DURATION_SECONDS = 10
DEFAULT_DURATION_SECONDS = 8

MAX_REFERENCE_IMAGES = 6
MAX_REQUEST_BYTES = 64 * 1024 * 1024
LOCAL_DATA_PREFIX = "data:image/png;base64,"

SUBMIT_TIMEOUT_SECONDS = 60.0
POLL_TIMEOUT_SECONDS = 30.0

TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
TERMINAL_STATUSES = frozenset({"success", "succeeded", "completed", "failed", "fail", "error"})
FAILED_STATUSES = frozenset({"failed", "fail", "error"})


@dataclass(frozen=True)
class VideoRequest:
    """一次视频提交请求的纯数据描述（model/prompt/时长/参考图可追溯）。"""

    url: str
    body: Mapping[str, Any]
    provider: str
    model: str
    prompt: str
    duration_seconds: int
    resolution: str
    reference_count: int

    def as_body(self) -> dict:
        return base.json_clone(self.body)

    def headers(self, api_key: str) -> dict:
        return {"Authorization": f"Bearer {api_key}"}


@dataclass(frozen=True)
class VideoSubmission:
    task_id: str
    provider: str
    model: str
    prompt: str
    duration_seconds: int
    reference_count: int
    request_body: Mapping[str, Any]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class VideoPoll:
    task_id: str
    provider: str
    status: str
    video_url: str | None
    done: bool
    failed: bool
    raw: Mapping[str, Any]


def validate_duration_seconds(seconds) -> int:
    """时长必须是 6–10 的整数秒；浮点/字符串不静默截断。"""
    value = seconds
    if isinstance(value, bool):
        value = None
    elif isinstance(value, float):
        value = int(value) if value.is_integer() else None
    elif isinstance(value, str):
        value = int(value) if value.strip().isdigit() else None
    elif not isinstance(value, int):
        value = None
    if value is None or not MIN_DURATION_SECONDS <= value <= MAX_DURATION_SECONDS:
        raise ProviderError(
            f"视频时长必须是 {MIN_DURATION_SECONDS}–{MAX_DURATION_SECONDS} 的整数秒。",
            kind=base.KIND_VALIDATION,
            provider="video",
            detail={"field": "duration_seconds", "value": seconds},
        )
    return int(value)


def validate_reference(value, *, approved_local=False) -> str:
    """参考图：https 地址，或（明确标记已审时）本地 data URI / 本地路径。"""
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(
            "参考图需为 HTTPS 地址，或使用已审核的本地参考图片。",
            kind=base.KIND_VALIDATION,
            provider="video",
        )
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.hostname:
        return value
    if approved_local and (value.startswith(LOCAL_DATA_PREFIX) or os.path.isabs(value)):
        return value
    raise ProviderError(
        "参考图需为 HTTPS 地址，或使用已审核的本地参考图片。",
        kind=base.KIND_VALIDATION,
        provider="video",
        detail={"approved_local": bool(approved_local)},
    )


def validate_references(references, *, approved_local=False):
    """参考图 ≤6 张；0 张表示纯文生视频。"""
    if references is None:
        return []
    if not isinstance(references, (list, tuple)):
        raise ProviderError(
            "参考图必须是图片列表。", kind=base.KIND_VALIDATION, provider="video"
        )
    if len(references) > MAX_REFERENCE_IMAGES:
        raise ProviderError(
            f"参考图最多 {MAX_REFERENCE_IMAGES} 张，不能丢弃多余参考图后继续。",
            kind=base.KIND_VALIDATION,
            provider="video",
            detail={"reference_count": len(references)},
        )
    return [validate_reference(item, approved_local=approved_local) for item in references]


def request_size(body) -> int:
    """请求体 UTF-8 字节数（用于 64MB 守卫）。"""
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def check_request_size(body, *, max_bytes=MAX_REQUEST_BYTES) -> int:
    """超过服务商 64MB 上限即拒绝（默认上限 64MB）。"""
    size = request_size(body)
    if size > max_bytes:
        raise ProviderError(
            f"参考图片请求超过服务商 {max_bytes} 字节（64MB）限制，请压缩参考图。",
            kind=base.KIND_VALIDATION,
            provider="video",
            detail={"size": size, "max_bytes": max_bytes},
        )
    return size


def _resolve_resolution(provider, model, requested):
    """MiniMax 分辨率受模型支持集约束；火山方舟不做本地分辨率裁剪。"""
    if provider != MINIMAX_PROVIDER:
        return requested
    allowed = ("480P", "768P") if model == "MiniMax-H3-Max" else ("768P", "2K")
    if requested not in allowed:
        raise ProviderError(
            f"分辨率 {requested} 不受所选 MiniMax 模型支持。",
            kind=base.KIND_VALIDATION,
            provider="video",
            detail={"model": model, "resolution": requested, "allowed": list(allowed)},
        )
    return requested


def build_video_request(
    *,
    prompt,
    duration_seconds=None,
    references=None,
    approved_local=False,
    model=None,
    provider=None,
    resolution=None,
    env=None,
    max_request_bytes=MAX_REQUEST_BYTES,
) -> VideoRequest:
    """构造提交请求体；时长/参考图/请求体积全部本地校验后才返回。"""
    source = base.require_configured("video", env)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProviderError(
            "视频 prompt 必须是非空字符串。", kind=base.KIND_VALIDATION, provider="video"
        )
    resolved_provider = str(provider or source.get("VIDEO_PROVIDER") or "").strip()
    if resolved_provider not in PROVIDER_TASK_FIELD:
        raise ProviderError(
            "视频供应商只支持 minimax 或 ark。",
            kind=base.KIND_CONFIG,
            provider="video",
            missing=("VIDEO_PROVIDER",),
        )
    resolved_model = str(model or source.get("VIDEO_MODEL") or "").strip()
    if not resolved_model:
        raise ProviderError(
            "视频模型未配置。", kind=base.KIND_CONFIG, missing=("VIDEO_MODEL",), provider="video"
        )
    if duration_seconds is None:
        duration_seconds = source.get("VIDEO_DURATION", DEFAULT_DURATION_SECONDS)
    resolved_duration = validate_duration_seconds(duration_seconds)
    resolved_resolution = str(
        resolution or source.get("VIDEO_RESOLUTION") or "768P"
    ).strip()
    resolved_resolution = _resolve_resolution(
        resolved_provider, resolved_model, resolved_resolution
    )
    checked_refs = validate_references(references, approved_local=approved_local)

    content = [{"type": "text", "text": prompt}]
    for image in checked_refs:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image},
                "role": "reference_image",
            }
        )
    body = {"model": resolved_model, "content": content}
    if resolved_provider == MINIMAX_PROVIDER:
        body.update(
            duration=resolved_duration, resolution=resolved_resolution, ratio="16:9"
        )
    check_request_size(body, max_bytes=max_request_bytes)
    target = base.require_https_endpoint(source.get("VIDEO_ENDPOINT"), field="VIDEO_ENDPOINT")
    return VideoRequest(
        url=target,
        body=body,
        provider=resolved_provider,
        model=resolved_model,
        prompt=prompt,
        duration_seconds=resolved_duration,
        resolution=resolved_resolution,
        reference_count=len(checked_refs),
    )


def plan_video_action(provider_task_id) -> str:
    """已有厂商任务号 → `poll`（绝不重新提交）；否则 → `submit`。"""
    if provider_task_id is None or str(provider_task_id).strip() == "":
        return "submit"
    return "poll"


def submit_video(
    request: VideoRequest,
    *,
    http=None,
    env=None,
    api_key=None,
) -> VideoSubmission:
    """提交一次视频任务；恰好一次出网调用，失败不重发。"""
    source = base.require_configured("video", env)
    resolved_key = api_key if api_key is not None else source.get("VIDEO_API_KEY")
    transport = http if http is not None else base.default_http()
    body = request.as_body()

    try:
        response = transport.post(
            request.url,
            headers=request.headers(resolved_key),
            json=body,
            timeout=SUBMIT_TIMEOUT_SECONDS,
        )
    except base.TRANSPORT_ERRORS as exc:
        raise base.transport_error(
            exc,
            provider="video",
            message="视频提交结果不确定，未重试。请到供应商控制台核实任务编号。",
        ) from None

    if response.status_code >= 400:
        raise base.http_error(
            response.status_code,
            provider="video",
            detail=base.response_error_detail(response),
        )
    try:
        data = response.json()
    except (ValueError, TypeError):
        raise ProviderError(
            "视频接口未返回合法 JSON，请核对供应商记录。",
            kind=base.KIND_FORMAT,
            provider="video",
        ) from None
    field = PROVIDER_TASK_FIELD[request.provider]
    task_id = data.get(field) if isinstance(data, dict) else None
    if not task_id:
        raise ProviderError(
            "视频接口未返回任务编号，请核对供应商记录。",
            kind=base.KIND_FORMAT,
            provider="video",
        )
    return VideoSubmission(
        task_id=str(task_id),
        provider=request.provider,
        model=request.model,
        prompt=request.prompt,
        duration_seconds=request.duration_seconds,
        reference_count=request.reference_count,
        request_body=body,
        raw=data,
    )


def poll_url(provider_task_id, *, provider, endpoint) -> str:
    """轮询地址：minimax 走 /v2/query/video_generation/{id}，ark 走 endpoint/{id}。"""
    if not TASK_ID_PATTERN.fullmatch(str(provider_task_id)):
        raise ProviderError(
            "供应商任务编号格式无效。",
            kind=base.KIND_VALIDATION,
            provider="video",
            detail={"task_id": str(provider_task_id)},
        )
    target = base.require_https_endpoint(endpoint, field="VIDEO_ENDPOINT")
    if provider == MINIMAX_PROVIDER:
        parsed = urlparse(target)
        return f"{parsed.scheme}://{parsed.netloc}/v2/query/video_generation/{provider_task_id}"
    return target.rstrip("/") + "/" + str(provider_task_id)


def poll_video(
    provider_task_id,
    *,
    provider,
    http=None,
    env=None,
    api_key=None,
) -> VideoPoll:
    """只轮询已有厂商任务号；**绝不重新提交**。"""
    source = base.require_configured("video", env)
    resolved_key = api_key if api_key is not None else source.get("VIDEO_API_KEY")
    transport = http if http is not None else base.default_http()
    if provider not in PROVIDER_TASK_FIELD:
        raise ProviderError(
            "视频供应商只支持 minimax 或 ark。",
            kind=base.KIND_CONFIG,
            provider="video",
            missing=("VIDEO_PROVIDER",),
        )
    target = poll_url(provider_task_id, provider=provider, endpoint=source.get("VIDEO_ENDPOINT"))

    try:
        response = transport.get(
            target,
            headers={"Authorization": f"Bearer {resolved_key}"},
            timeout=POLL_TIMEOUT_SECONDS,
        )
    except base.TRANSPORT_ERRORS as exc:
        raise base.transport_error(
            exc,
            provider="video",
            message="查询视频任务暂时失败，已保留厂商编号。",
        ) from None

    if response.status_code >= 400:
        raise base.http_error(
            response.status_code,
            provider="video",
            detail=base.response_error_detail(response),
        )
    try:
        data = response.json()
    except (ValueError, TypeError):
        raise ProviderError(
            "查询视频任务未返回合法 JSON，已保留厂商编号。",
            kind=base.KIND_FORMAT,
            provider="video",
        ) from None

    if provider == MINIMAX_PROVIDER:
        task = data.get("task") if isinstance(data, dict) else None
        task = task if isinstance(task, dict) else {}
        status = str(task.get("status", "unknown"))
        content = task.get("content") if isinstance(task.get("content"), dict) else {}
        video_url = content.get("url")
    else:
        status = str(data.get("status", "unknown")) if isinstance(data, dict) else "unknown"
        content = data.get("content") if isinstance(data, dict) and isinstance(data.get("content"), dict) else {}
        video_url = content.get("video_url")

    lowered = status.lower()
    return VideoPoll(
        task_id=str(provider_task_id),
        provider=provider,
        status=status,
        video_url=video_url,
        done=lowered in TERMINAL_STATUSES,
        failed=lowered in FAILED_STATUSES,
        raw=data if isinstance(data, dict) else {},
    )


def advance_video(
    request: VideoRequest,
    *,
    provider_task_id=None,
    http=None,
    env=None,
    api_key=None,
):
    """已有厂商任务号只轮询；否则提交一次（返回类型随动作不同）。"""
    if plan_video_action(provider_task_id) == "poll":
        return poll_video(
            provider_task_id, provider=request.provider, http=http, env=env, api_key=api_key
        )
    return submit_video(request, http=http, env=env, api_key=api_key)
