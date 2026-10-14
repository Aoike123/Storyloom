"""硅基流动生图协议：1024² 文生图、参考图合成、https-only 下载与 PNG 归一。

只读抽取自旧 `image_provider.*`，改写依赖：
- 不 import 旧 `db`/`provider_usage`/`image_errors`/`reference_image_model`；
- 不再写 `DATA/media`：归一结果以 PNG 字节返回，落盘由媒体存储接口负责（附录 08）；
- 保留纯逻辑：1024×1024 文生图、参考图合成 1–9 张、steps 1–100、https-only 下载、
  20MB/最短边 256px/最长边 5760px/宽高比 0.4–2.5 校验、PIL 归一 PNG。

超范围输入一律 ProviderError（validation），绝不静默截断。
"""

import base64
import io
from dataclasses import dataclass
from typing import Any, Mapping

from PIL import Image, UnidentifiedImageError

from . import base
from .base import ProviderError

IMAGE_ENV_KEYS = base.REQUIRED_ENV["image"]
SUPPORTED_PROVIDER = "siliconflow"

IMAGE_SIZE = "1024x1024"
MIN_REFERENCE_IMAGES = 1
MAX_REFERENCE_IMAGES = 9
MIN_STEPS = 1
MAX_STEPS = 100
DEFAULT_STEPS = 50

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MIN_EDGE_PX = 256
MAX_EDGE_PX = 5760
MIN_ASPECT_RATIO = 0.4
MAX_ASPECT_RATIO = 2.5
MAX_ENCODED_REFERENCE_BYTES = 30 * 1024 * 1024

DATA_URI_PREFIX = "data:image/png;base64,"
DOWNLOAD_TIMEOUT_SECONDS = 60.0
GENERATION_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class ImageRequest:
    """一次生图请求的纯数据描述（model/prompt/mode/参考图数可追溯）。"""

    url: str
    body: Mapping[str, Any]
    model: str
    prompt: str
    mode: str
    reference_count: int

    def as_body(self) -> dict:
        return base.json_clone(self.body)

    def headers(self, api_key: str) -> dict:
        return {"Authorization": f"Bearer {api_key}"}


@dataclass(frozen=True)
class ImageResult:
    url: str
    model: str
    prompt: str
    mode: str
    reference_count: int
    request_body: Mapping[str, Any]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class NormalizedImage:
    data: bytes
    width: int
    height: int
    media_type: str = "image/png"


def validate_steps(steps) -> int:
    """steps 必须是 1–100 的整数（不接受字符串强转，避免静默截断）。"""
    if isinstance(steps, bool) or not isinstance(steps, int) or not MIN_STEPS <= steps <= MAX_STEPS:
        raise ProviderError(
            f"参考图合成质量参数必须是 {MIN_STEPS}–{MAX_STEPS} 的整数。",
            kind=base.KIND_VALIDATION,
            provider="image",
            detail={"field": "steps", "value": steps},
        )
    return steps


def validate_references(references):
    """参考图必须是 1–9 张已归一 PNG 的 data URI；越界不截断。"""
    if not isinstance(references, (list, tuple)) or not MIN_REFERENCE_IMAGES <= len(references) <= MAX_REFERENCE_IMAGES:
        count = len(references) if isinstance(references, (list, tuple)) else 0
        raise ProviderError(
            f"参考图合成需要 {MIN_REFERENCE_IMAGES}–{MAX_REFERENCE_IMAGES} 张图片，不能丢弃多余参考图后继续。",
            kind=base.KIND_VALIDATION,
            provider="image",
            detail={"reference_count": count},
        )
    for index, media in enumerate(references):
        if not isinstance(media, str) or not media.startswith(DATA_URI_PREFIX):
            raise ProviderError(
                "参考图必须是已归一的 PNG data URI。",
                kind=base.KIND_VALIDATION,
                provider="image",
                detail={"index": index},
            )
    return list(references)


def build_image_request(
    *,
    prompt,
    references=None,
    steps=None,
    model=None,
    env=None,
    image_size=IMAGE_SIZE,
) -> ImageRequest:
    """构造硅基流动生图请求体；文生图固定 1024×1024。"""
    source = base.require_configured("image", env)
    if str(source.get("IMAGE_PROVIDER") or "").strip() != SUPPORTED_PROVIDER:
        raise ProviderError(
            "请选择硅基流动生图接口，其他协议尚未接入。",
            kind=base.KIND_CONFIG,
            provider="image",
            missing=("IMAGE_PROVIDER",),
        )
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProviderError(
            "生图 prompt 必须是非空字符串。", kind=base.KIND_VALIDATION, provider="image"
        )
    resolved_model = str(model or source.get("IMAGE_MODEL") or "").strip()
    if not resolved_model:
        raise ProviderError(
            "生图模型未配置。", kind=base.KIND_CONFIG, missing=("IMAGE_MODEL",), provider="image"
        )
    target = base.require_https_endpoint(source.get("IMAGE_ENDPOINT"), field="IMAGE_ENDPOINT")

    if references is None:
        if image_size != IMAGE_SIZE:
            raise ProviderError(
                f"文生图尺寸固定为 {IMAGE_SIZE}。",
                kind=base.KIND_VALIDATION,
                provider="image",
                detail={"image_size": image_size},
            )
        body = {"model": resolved_model, "prompt": prompt, "image_size": IMAGE_SIZE}
        return ImageRequest(
            url=target,
            body=body,
            model=resolved_model,
            prompt=prompt,
            mode="text_to_image",
            reference_count=0,
        )

    resolved_steps = validate_steps(DEFAULT_STEPS if steps is None else steps)
    checked = validate_references(references)
    body = {
        "model": resolved_model,
        "prompt": prompt,
        "num_inference_steps": resolved_steps,
    }
    for index, media in enumerate(checked):
        body["image" if index == 0 else f"image{index + 1}"] = media
    return ImageRequest(
        url=target,
        body=body,
        model=resolved_model,
        prompt=prompt,
        mode="reference_images",
        reference_count=len(checked),
    )


def request_image(
    request: ImageRequest,
    *,
    http=None,
    env=None,
    api_key=None,
) -> ImageResult:
    """执行一次生图请求；恰好一次出网调用，失败不重发。"""
    source = base.require_configured("image", env)
    resolved_key = api_key if api_key is not None else source.get("IMAGE_API_KEY")
    transport = http if http is not None else base.default_http()
    body = request.as_body()

    try:
        response = transport.post(
            request.url,
            headers=request.headers(resolved_key),
            json=body,
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
    except base.TRANSPORT_ERRORS as exc:
        raise base.transport_error(
            exc,
            provider="image",
            message="生图请求结果不确定，未自动重试，请核对供应商记录。",
        ) from None

    if response.status_code >= 400:
        raise base.http_error(
            response.status_code,
            provider="image",
            detail=base.response_error_detail(response),
        )

    try:
        data = response.json()
    except (ValueError, TypeError):
        raise ProviderError(
            "生图接口未返回合法 JSON，未自动重试。",
            kind=base.KIND_FORMAT,
            provider="image",
        ) from None
    try:
        url = data["images"][0]["url"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError(
            "生图接口未返回有效图片地址，未自动重试。",
            kind=base.KIND_FORMAT,
            provider="image",
        ) from None
    base.require_https_url(url, field="生图结果地址")
    return ImageResult(
        url=url,
        model=request.model,
        prompt=request.prompt,
        mode=request.mode,
        reference_count=request.reference_count,
        request_body=body,
        raw=data if isinstance(data, dict) else {},
    )


def generate_image(
    *,
    prompt,
    references=None,
    steps=None,
    model=None,
    env=None,
    http=None,
    api_key=None,
    image_size=IMAGE_SIZE,
) -> ImageResult:
    """build + request 的便捷入口（文生图或参考图合成）。"""
    request = build_image_request(
        prompt=prompt,
        references=references,
        steps=steps,
        model=model,
        env=env,
        image_size=image_size,
    )
    return request_image(request, http=http, env=env, api_key=api_key)


def validate_download_url(url, *, field="图片下载地址") -> str:
    """下载地址必须 https（允许签名查询参数）。"""
    return base.require_https_url(url, field=field)


def validate_image_size(width, height) -> tuple[int, int]:
    """最短边 ≥256px、最长边 ≤5760px、宽高比 0.4–2.5。"""
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise ProviderError(
            "图片尺寸必须是正整数。",
            kind=base.KIND_VALIDATION,
            provider="image",
            detail={"width": width, "height": height},
        )
    if min(width, height) < MIN_EDGE_PX:
        raise ProviderError(
            f"图片最短边必须 ≥{MIN_EDGE_PX}px。",
            kind=base.KIND_VALIDATION,
            provider="image",
            detail={"width": width, "height": height, "rule": "min_edge"},
        )
    if max(width, height) > MAX_EDGE_PX:
        raise ProviderError(
            f"图片最长边必须 ≤{MAX_EDGE_PX}px。",
            kind=base.KIND_VALIDATION,
            provider="image",
            detail={"width": width, "height": height, "rule": "max_edge"},
        )
    ratio = width / height
    if not MIN_ASPECT_RATIO <= ratio <= MAX_ASPECT_RATIO:
        raise ProviderError(
            f"图片宽高比必须在 {MIN_ASPECT_RATIO}–{MAX_ASPECT_RATIO} 之间。",
            kind=base.KIND_VALIDATION,
            provider="image",
            detail={"width": width, "height": height, "ratio": ratio, "rule": "aspect_ratio"},
        )
    return width, height


def download_image_bytes(url, *, http=None, max_bytes=MAX_IMAGE_BYTES) -> bytes:
    """https-only 流式下载并守住字节上限（≤ max_bytes，默认 20MB）。"""
    target = validate_download_url(url)
    transport = http if http is not None else base.default_http()
    raw = bytearray()
    try:
        with transport.stream(
            "GET", target, timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False
        ) as response:
            if response.status_code >= 400:
                raise base.http_error(
                    response.status_code,
                    provider="image",
                    detail=base.response_error_detail(response),
                )
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > max_bytes:
                    raise ProviderError(
                        f"图片超过 {max_bytes} 字节（20MB）限制。",
                        kind=base.KIND_VALIDATION,
                        provider="image",
                        detail={"max_bytes": max_bytes, "received": len(raw)},
                    )
    except ProviderError:
        raise
    except base.TRANSPORT_ERRORS as exc:
        raise base.transport_error(exc, provider="image") from None
    return bytes(raw)


def normalize_png(raw, *, max_bytes=MAX_IMAGE_BYTES) -> NormalizedImage:
    """校验下载结果并归一为 PNG 字节（不落盘）。

    :raises ProviderError: 超过字节上限、尺寸越界或不是可识别图片。
    """
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ProviderError(
            "图片内容为空或类型无效。", kind=base.KIND_VALIDATION, provider="image"
        )
    if len(raw) > max_bytes:
        raise ProviderError(
            f"图片超过 {max_bytes} 字节（20MB）限制。",
            kind=base.KIND_VALIDATION,
            provider="image",
            detail={"max_bytes": max_bytes, "received": len(raw)},
        )
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.width, image.height
            validate_image_size(width, height)
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="PNG")
    except ProviderError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise ProviderError(
            "图片下载或校验失败，未重新调用生图模型。",
            kind=base.KIND_FORMAT,
            provider="image",
        ) from None
    return NormalizedImage(data=buffer.getvalue(), width=width, height=height)


def fetch_png(url, *, http=None, max_bytes=MAX_IMAGE_BYTES) -> NormalizedImage:
    """https-only 下载 + 校验 + PNG 归一。"""
    return normalize_png(download_image_bytes(url, http=http, max_bytes=max_bytes), max_bytes=max_bytes)


def encode_reference_bytes(
    raw,
    *,
    max_bytes=MAX_IMAGE_BYTES,
    max_encoded_bytes=MAX_ENCODED_REFERENCE_BYTES,
) -> str:
    """本地已审参考图字节 → 归一 PNG data URI（尺寸/体积越界即拒绝）。"""
    normalized = normalize_png(raw, max_bytes=max_bytes)
    if len(normalized.data) > max_encoded_bytes:
        raise ProviderError(
            f"转换后的参考图片超过 {max_encoded_bytes} 字节（30MB）。",
            kind=base.KIND_VALIDATION,
            provider="image",
            detail={"max_encoded_bytes": max_encoded_bytes, "encoded": len(normalized.data)},
        )
    return DATA_URI_PREFIX + base64.b64encode(normalized.data).decode("ascii")
