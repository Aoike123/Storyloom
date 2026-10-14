"""供应商适配器公共基座：错误分类、HTTPS 校验、配置门槛与注入式 transport。

JOB-01（ROOT-04 附录 09 §5/§6）：本模块是旧栈纯协议的改写抽取，不 import 任何旧
backend 业务模块，不读写旧 Task/Record，不建 StudioJob，不写数据库，也不发起真实
收费调用——所有出网必须经调用方注入的 transport，缺省 `default_http()` 才返回 httpx。

R08 语义：网络中断/超时和 5xx 的提交结果不确定，一律 `retryable=False`（收费调用
绝不自动重发，只能由用户知情后人工重试）；厂商已明确拒绝的限流与本地校验/配置
错误 `retryable=True`（未产生可计费工作）。`retryable` 只是给调用方/UI 的纯元数据，
本包任何函数都不会自行重试。
"""

import json
import os
from urllib.parse import urlparse

import httpx


def json_clone(value):
    """JSON 深拷贝：让纯请求描述对象返回的 body 与内部状态隔离。"""
    return json.loads(json.dumps(value, ensure_ascii=False))

# ── 错误分类 ────────────────────────────────────────────────────────────
KIND_NETWORK = "network"
KIND_TIMEOUT = "timeout"
KIND_AUTH = "auth"
KIND_RATE_LIMIT = "rate_limit"
KIND_PROVIDER = "provider"
KIND_FORMAT = "format"
KIND_CONFIG = "config"
KIND_VALIDATION = "validation"

ALL_KINDS = frozenset(
    {
        KIND_NETWORK,
        KIND_TIMEOUT,
        KIND_AUTH,
        KIND_RATE_LIMIT,
        KIND_PROVIDER,
        KIND_FORMAT,
        KIND_CONFIG,
        KIND_VALIDATION,
    }
)

# 出网前本地定案（config/validation）或厂商已明确拒绝、未产生计费工作（rate_limit）。
_RETRYABLE_KINDS = frozenset({KIND_CONFIG, KIND_VALIDATION, KIND_RATE_LIMIT})

# 注入式 transport 需要模拟的传输层异常（httpx 传输错误与底层 OSError）。
TRANSPORT_ERRORS = (httpx.HTTPError, OSError)


class ProviderError(Exception):
    """可分类、可序列化的供应商错误。

    纯数据字段：`kind`（见 KIND_*）、`status`（HTTP 状态码或 None）、`message`、
    `retryable`（重试是否会重复计费，纯元数据）、`provider`、`missing`（缺失的
    环境变量名）、`detail`（厂商返回的简短说明/异常类名等可序列化细节）。
    """

    def __init__(
        self,
        message,
        *,
        kind=KIND_PROVIDER,
        status=None,
        retryable=None,
        provider=None,
        missing=(),
        detail=None,
    ):
        super().__init__(str(message))
        self.message = str(message)
        self.kind = kind if kind in ALL_KINDS else KIND_PROVIDER
        self.status = None if status is None else int(status)
        self.retryable = (
            self.kind in _RETRYABLE_KINDS if retryable is None else bool(retryable)
        )
        self.provider = provider
        self.missing = tuple(missing)
        self.detail = detail

    def to_dict(self):
        """纯数据、可 json.dumps 的元数据（不含异常链）。"""
        payload = {
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "retryable": self.retryable,
            "provider": self.provider,
        }
        if self.missing:
            payload["missing"] = list(self.missing)
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload

    def __str__(self):
        return self.message


# ── HTTPS 校验 ──────────────────────────────────────────────────────────
def require_https_endpoint(url, *, field):
    """配置里的供应商端点：必须是 https、有主机名、且不含账号/查询/片段。"""
    if not isinstance(url, str) or not url.strip():
        raise ProviderError(
            f"{field} 未配置；真实模型接口必须是 HTTPS 地址。",
            kind=KIND_CONFIG,
            missing=(field,),
            provider=None,
        )
    text = url.strip()
    parsed = urlparse(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderError(
            f"{field} 必须是不含账号、查询参数和片段的 HTTPS 地址。",
            kind=KIND_VALIDATION,
            detail={"field": field},
        )
    return text


def require_https_url(url, *, field):
    """厂商返回/待下载的 URL：只要求 https + 主机名（允许签名查询参数）。"""
    if not isinstance(url, str) or urlparse(url).scheme != "https" or not urlparse(url).hostname:
        raise ProviderError(
            f"{field}必须使用 HTTPS。",
            kind=KIND_VALIDATION,
            detail={"field": field},
        )
    return url


# ── 配置门槛（纯函数，不抛 HTTP 错误） ──────────────────────────────────
REQUIRED_ENV = {
    "llm": ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"),
    "image": ("IMAGE_PROVIDER", "IMAGE_ENDPOINT", "IMAGE_MODEL", "IMAGE_API_KEY"),
    "video": ("VIDEO_PROVIDER", "VIDEO_ENDPOINT", "VIDEO_MODEL", "VIDEO_API_KEY"),
}

SUPPORTED_PROVIDERS = {
    "image": ("siliconflow",),
    "video": ("minimax", "ark"),
}

PROVIDER_ENV_KEY = {"image": "IMAGE_PROVIDER", "video": "VIDEO_PROVIDER"}

PROVIDER_LABELS = {"llm": "文本", "image": "图片", "video": "视频"}


def read_env(env=None):
    """读取环境变量快照：`env` 为 None 时用 `os.environ`（不改写它）。"""
    return os.environ if env is None else env


def missing_env(kind, env=None):
    """返回该供应商未就绪的环境变量名（具体缺失项，供上层映射 422）。

    空串视为缺失；`IMAGE_PROVIDER` / `VIDEO_PROVIDER` 只有在取值受支持时才算就绪，
    取值不受支持时也计入缺失项（便于上层报出可操作的键名）。
    """
    if kind not in REQUIRED_ENV:
        raise ProviderError(
            f"未知的供应商类别：{kind}",
            kind=KIND_VALIDATION,
            detail={"kind": kind},
        )
    source = read_env(env)
    missing = [key for key in REQUIRED_ENV[kind] if not str(source.get(key) or "").strip()]
    provider_key = PROVIDER_ENV_KEY.get(kind)
    allowed = SUPPORTED_PROVIDERS.get(kind)
    if provider_key and allowed and provider_key not in missing:
        if str(source.get(provider_key)).strip() not in allowed:
            missing.append(provider_key)
    return tuple(missing)


def is_configured(kind, env=None):
    """纯函数：必需环境变量是否齐全。"""
    return not missing_env(kind, env)


def require_configured(kind, env=None):
    """配置齐全时返回环境快照，否则抛 config 类 ProviderError（含缺失项名）。"""
    missing = missing_env(kind, env)
    if missing:
        label = PROVIDER_LABELS.get(kind, kind)
        raise ProviderError(
            f"{label}供应商尚未配置完整，缺少：{'、'.join(missing)}。"
            "请在本地设置文件里补齐后再试。",
            kind=KIND_CONFIG,
            missing=missing,
            provider=kind,
        )
    return read_env(env)


# ── transport（可注入；缺省 httpx） ─────────────────────────────────────
def default_http():
    """缺省 transport：httpx 模块本身（测试必须注入假实现，绝不打真实端点）。"""
    return httpx


def classify_transport_error(exc):
    """传输层异常 → 错误类别：超时与网络错误分开。"""
    if isinstance(exc, httpx.TimeoutException):
        return KIND_TIMEOUT
    return KIND_NETWORK


def transport_error(exc, *, provider, message=None):
    """把传输层异常转成 ProviderError（结果不确定，retryable=False）。"""
    if message is None:
        message = "请求结果不确定，未自动重发；请核对供应商记录。"
    return ProviderError(
        message,
        kind=classify_transport_error(exc),
        provider=provider,
        detail={"exception": type(exc).__name__},
    )


def classify_http_status(status):
    """HTTP 状态码 → 错误类别：认证 / 限流 / 厂商业务错误。"""
    code = int(status)
    if code in (401, 403):
        return KIND_AUTH
    if code == 429:
        return KIND_RATE_LIMIT
    return KIND_PROVIDER


def response_error_detail(response):
    """从厂商错误响应里抽一条可读信息；抽不到返回 None（不臆造）。"""
    try:
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("error", "message", "msg", "detail"):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("msg")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def http_error(status, *, provider, detail=None):
    """厂商 HTTP 错误 → ProviderError（不自动重发）。"""
    message = f"{provider} 接口返回 HTTP {int(status)}；本请求不会自动重发。"
    if detail:
        message += f" 厂商信息：{detail}"
    return ProviderError(
        message,
        kind=classify_http_status(status),
        status=int(status),
        provider=provider,
        detail=detail,
    )
