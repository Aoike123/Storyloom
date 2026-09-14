"""Anonymous model-access sessions and the shared public-demo budget gate.

Visitors never choose endpoints or model identifiers.  They either borrow the
operator-funded pool or provide keys for the three fixed providers.  BYOK
credentials are encrypted at rest and tasks retain only an opaque session id.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr


ACCESS_HEADER = "X-Storyloom-Model-Access"
SESSION_KIND = "model_access_session"
POOL_KIND = "public_pool_day"
BALANCE_KIND = "public_pool_balance"
_access_id: ContextVar[str] = ContextVar("storyloom_model_access", default="")
_pool_lock = Lock()
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_KEY_FIELDS = ("LLM_API_KEY", "IMAGE_API_KEY", "VIDEO_API_KEY")
_RESERVES = {
    "llm": ("PUBLIC_POOL_LLM_RESERVE_CNY", "0.20"),
    "image": ("PUBLIC_POOL_IMAGE_RESERVE_CNY", "0.50"),
    "video": ("PUBLIC_POOL_VIDEO_RESERVE_CNY", "6.00"),
}


class ModelAccessError(Exception):
    pass


class OwnKeys(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    deepseek: SecretStr = Field(min_length=8, max_length=4096)
    siliconflow: SecretStr = Field(min_length=8, max_length=4096)
    minimax: SecretStr = Field(min_length=8, max_length=4096)


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    mode: str
    keys: OwnKeys | None = None


router = APIRouter(prefix="/api/model-access", tags=["model-access"])


def public_demo_mode() -> bool:
    return os.getenv("STORYLOOM_DEMO_MODE", "local").strip().lower() == "public"


def current_access_id() -> str:
    return _access_id.get()


@contextmanager
def access_scope(access_id: str | None):
    token = _access_id.set(access_id or "")
    try:
        yield
    finally:
        _access_id.reset(token)


def _record_id(token: str) -> str:
    return "model_access_" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_access_token(token: str) -> str:
    if not isinstance(token, str) or not 32 <= len(token) <= 160 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ModelAccessError("模型使用会话无效，请重新选择使用方式。")
    access_id = _record_id(token)
    record = _access_record(access_id)
    if not record:
        raise ModelAccessError("模型使用会话已失效，请重新选择使用方式。")
    return access_id


def _access_record(access_id: str | None = None) -> dict | None:
    access_id = access_id or current_access_id()
    if not access_id:
        return None
    from .db import Record, Session

    with Session() as db:
        row = db.get(Record, access_id)
        if not row or row.kind != SESSION_KIND or float(row.data.get("expires_at", 0)) <= time.time():
            return None
        return dict(row.data)


def current_access_mode() -> str | None:
    record = _access_record()
    return str(record.get("mode")) if record else None


def _secret_material() -> str:
    configured = os.getenv("MODEL_ACCESS_SECRET", "").strip()
    if configured:
        if len(configured) < 32:
            raise ModelAccessError("MODEL_ACCESS_SECRET 至少需要 32 个字符。")
        return configured
    if public_demo_mode():
        raise ModelAccessError("公网部署缺少 MODEL_ACCESS_SECRET，暂不能保存自带 Key。")

    # Local development gets one durable process-shared secret without asking
    # the user to manage another file. Production must use the environment key.
    from .environment import ROOT

    data = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
    data.mkdir(parents=True, exist_ok=True)
    path = data / ".model-access-secret"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(32))
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise ModelAccessError("本地模型凭证加密密钥无效。")
    return value


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(_secret_material().encode("utf-8")).digest())
    return Fernet(key)


def _encrypt_keys(keys: OwnKeys) -> str:
    values = {
        "LLM_API_KEY": keys.deepseek.get_secret_value().strip(),
        "IMAGE_API_KEY": keys.siliconflow.get_secret_value().strip(),
        "VIDEO_API_KEY": keys.minimax.get_secret_value().strip(),
    }
    if any(not value or any(char in value for char in "\r\n\x00") for value in values.values()):
        raise ModelAccessError("API Key 格式无效。")
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def _decrypt_keys(ciphertext: str) -> dict[str, str]:
    try:
        raw = _fernet().decrypt(ciphertext.encode("ascii"), ttl=None)
        values = json.loads(raw)
    except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise ModelAccessError("自带 Key 会话无法解密，请重新配置。") from None
    if set(values) != set(_KEY_FIELDS) or not all(isinstance(values[key], str) and values[key] for key in _KEY_FIELDS):
        raise ModelAccessError("自带 Key 会话内容无效，请重新配置。")
    return values


def _locked_config() -> dict[str, str]:
    from .environment import LOCKED_MODEL_CONFIG

    return dict(LOCKED_MODEL_CONFIG)


def effective_model_config(base: dict[str, str]) -> dict[str, str]:
    """Overlay the active anonymous session without mutating global settings."""
    record = _access_record()
    if not record:
        if not public_demo_mode():
            return base
        return {**base, **_locked_config(), "ALLOW_PAID_CALLS": "false"}

    configured = {**base, **_locked_config()}
    if record.get("mode") == "own":
        try:
            configured.update(_decrypt_keys(str(record.get("credentials", ""))))
        except ModelAccessError:
            configured.update({key: "" for key in _KEY_FIELDS})
            configured["ALLOW_PAID_CALLS"] = "false"
            return configured
    elif record.get("mode") != "public":
        configured.update({key: "" for key in _KEY_FIELDS})
        configured["ALLOW_PAID_CALLS"] = "false"
        return configured
    configured["ALLOW_PAID_CALLS"] = "true"
    return configured


def _decimal_env(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default).strip())
    except (InvalidOperation, AttributeError):
        raise ModelAccessError(f"{name} 不是有效金额。") from None
    if value < 0 or value > Decimal("1000000"):
        raise ModelAccessError(f"{name} 超出允许范围。")
    return value.quantize(Decimal("0.01"))


def _enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() == "true"


def _pool_day(now: datetime | None = None) -> str:
    return (now or datetime.now(_SHANGHAI)).date().isoformat()


def _pool_record_id(day: str) -> str:
    return "public_pool_" + day.replace("-", "")


def _pool_state(day: str) -> dict:
    from .db import Record, Session

    with Session() as db:
        row = db.get(Record, _pool_record_id(day))
        return dict(row.data) if row and row.kind == POOL_KIND else {"day": day, "reserved_cny": "0.00", "blocked": {}}


def _next_reset_at() -> float:
    now = datetime.now(_SHANGHAI)
    return (datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=_SHANGHAI)).timestamp()


def _balance_cache() -> dict | None:
    from .db import Record, Session

    with Session() as db:
        row = db.get(Record, "public_pool_balance_deepseek")
        return dict(row.data) if row and row.kind == BALANCE_KIND else None


def _save_balance_cache(data: dict) -> None:
    from .db import Record, Session

    with Session.begin() as db:
        row = db.get(Record, "public_pool_balance_deepseek")
        if row:
            row.data = data
        else:
            db.add(Record(id="public_pool_balance_deepseek", kind=BALANCE_KIND, data=data))


def _deepseek_balance(base: dict[str, str], refresh: bool) -> dict:
    if not _enabled("PUBLIC_POOL_VERIFY_DEEPSEEK_BALANCE", "true"):
        return {"state": "budget_guard", "available": True}
    cached = _balance_cache()
    ttl = float(os.getenv("PUBLIC_POOL_BALANCE_CACHE_SECONDS", "60"))
    if cached and time.time() - float(cached.get("checked_at", 0)) < max(15, min(ttl, 600)):
        return cached
    key = base.get("LLM_API_KEY", "")
    if not key:
        return {"state": "missing_key", "available": False, "checked_at": time.time()}
    try:
        response = httpx.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {key}"},
            timeout=5,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        available = payload.get("is_available") is True
        cny = next((item.get("total_balance") for item in payload.get("balance_infos", []) if item.get("currency") == "CNY"), None)
        if cny is not None:
            available = available and Decimal(str(cny)) > _decimal_env("PUBLIC_POOL_MIN_DEEPSEEK_CNY", "0.05")
        result = {"state": "verified", "available": available, "checked_at": time.time()}
    except (httpx.HTTPError, ValueError, TypeError, InvalidOperation):
        result = {"state": "unavailable", "available": False, "checked_at": time.time()}
    _save_balance_cache(result)
    return result


def fixed_providers() -> list[dict[str, str]]:
    cfg = _locked_config()
    return [
        {"kind": "llm", "provider": "DeepSeek", "model": cfg["LLM_MODEL"], "purpose": "文本理解与创作"},
        {"kind": "image", "provider": "硅基流动", "model": cfg["IMAGE_MODEL"], "purpose": "人物、服装与场景画面"},
        {"kind": "video", "provider": "MiniMax", "model": cfg["VIDEO_MODEL"], "purpose": "镜头视频生成"},
    ]


def public_pool_status(*, refresh: bool = True) -> dict:
    from .environment import base_model_config

    day = _pool_day()
    state = _pool_state(day)
    try:
        limit = _decimal_env("PUBLIC_POOL_DAILY_BUDGET_CNY", "0")
        reserves = {kind: _decimal_env(name, default) for kind, (name, default) in _RESERVES.items()}
        used = Decimal(str(state.get("reserved_cny", "0"))).quantize(Decimal("0.01"))
    except (ModelAccessError, InvalidOperation):
        return {
            "available": False, "reason": "共享池预算配置无效", "day": day,
            "daily_limit_cny": "0.00", "used_cny": "0.00", "remaining_cny": "0.00",
            "next_reset_at": _next_reset_at(), "providers": [],
        }
    remaining = max(Decimal("0"), limit - used)
    base = base_model_config()
    keys_ready = all(base.get(key) for key in _KEY_FIELDS)
    blocked = state.get("blocked", {}) if isinstance(state.get("blocked"), dict) else {}
    deepseek = _deepseek_balance(base, refresh) if _enabled("PUBLIC_POOL_ENABLED") and keys_ready else {"state": "not_checked", "available": keys_ready}
    checks = [
        {"kind": "llm", "provider": "DeepSeek", "check": deepseek["state"], "available": bool(deepseek["available"]) and "llm" not in blocked},
        {"kind": "image", "provider": "硅基流动", "check": "daily_budget", "available": bool(base.get("IMAGE_API_KEY")) and "image" not in blocked},
        {"kind": "video", "provider": "MiniMax", "check": "daily_budget", "available": bool(base.get("VIDEO_API_KEY")) and "video" not in blocked},
    ]
    enabled = _enabled("PUBLIC_POOL_ENABLED")
    smallest = min(reserves.values()) if reserves else Decimal("0.01")
    available = enabled and keys_ready and limit > 0 and remaining >= smallest and all(item["available"] for item in checks)
    if not enabled:
        reason = "共享体验池暂未开启"
    elif not keys_ready:
        reason = "共享体验池尚未配置完整"
    elif limit <= 0 or remaining < smallest:
        reason = "今日共享额度已用完"
    elif blocked:
        reason = "共享供应商额度不足，今日体验池已暂停"
    elif not deepseek["available"]:
        reason = "暂时无法确认共享文本模型余额"
    else:
        reason = "今日共享额度可用，先到先得"
    return {
        "available": available,
        "reason": reason,
        "day": day,
        "daily_limit_cny": f"{limit:.2f}",
        "used_cny": f"{min(used, limit):.2f}",
        "remaining_cny": f"{remaining:.2f}",
        "next_reset_at": _next_reset_at(),
        "providers": checks,
    }


def _reserve_public_call(kind: str) -> dict:
    if kind not in _RESERVES:
        raise ModelAccessError("未知的共享模型调用类型。")
    # This check includes the cached provider-balance result. The transaction
    # below repeats the monetary comparison while holding the worker lock.
    status = public_pool_status(refresh=False)
    if not status["available"]:
        raise ModelAccessError(status["reason"])
    cost = _decimal_env(*_RESERVES[kind])
    limit = _decimal_env("PUBLIC_POOL_DAILY_BUDGET_CNY", "0")
    day = _pool_day()
    from .db import Record, Session

    with _pool_lock, Session.begin() as db:
        row = db.get(Record, _pool_record_id(day))
        data = dict(row.data) if row else {"day": day, "reserved_cny": "0.00", "blocked": {}}
        blocked = data.get("blocked", {}) if isinstance(data.get("blocked"), dict) else {}
        used = Decimal(str(data.get("reserved_cny", "0")))
        if kind in blocked:
            raise ModelAccessError("该共享供应商今日已暂停，请改用自己的 Key。")
        if used + cost > limit:
            raise ModelAccessError("今日共享额度不足以开始这次调用，请改用自己的 Key。")
        data = {**data, "reserved_cny": f"{used + cost:.2f}"}
        if row:
            row.data = data
        else:
            db.add(Record(id=_pool_record_id(day), kind=POOL_KIND, data=data))
    return {"mode": "public", "pool_day": day, "reserved_cny": f"{cost:.2f}"}


def authorize_call(kind: str) -> dict:
    record = _access_record()
    if not record:
        if not public_demo_mode():
            return {"mode": "local"}
        raise ModelAccessError("请先在模型使用方式页面选择共享池或配置自己的 Key。")
    mode = record.get("mode")
    if mode == "own":
        return {"mode": "own"}
    if mode == "public":
        return _reserve_public_call(kind)
    raise ModelAccessError("模型使用会话无效，请重新配置。")


def access_paid_state() -> bool | None:
    record = _access_record()
    if not record:
        return False if public_demo_mode() else None
    if record.get("mode") == "own":
        return True
    if record.get("mode") == "public":
        return bool(public_pool_status(refresh=False)["available"])
    return False


def mark_public_provider_unavailable(kind: str, status_code: int) -> None:
    if current_access_mode() != "public" or status_code not in (401, 402, 403):
        return
    day = _pool_day()
    from .db import Record, Session

    with _pool_lock, Session.begin() as db:
        row = db.get(Record, _pool_record_id(day))
        data = dict(row.data) if row else {"day": day, "reserved_cny": "0.00", "blocked": {}}
        blocked = dict(data.get("blocked", {}))
        blocked[kind] = {"at": time.time(), "status": status_code}
        data["blocked"] = blocked
        if row:
            row.data = data
        else:
            db.add(Record(id=_pool_record_id(day), kind=POOL_KIND, data=data))


def _create_session(body: SessionRequest) -> dict:
    mode = body.mode.strip().lower()
    if mode not in ("public", "own"):
        raise ModelAccessError("请选择共享体验池或使用自己的 Key。")
    if mode == "public":
        if body.keys is not None:
            raise ModelAccessError("共享体验池不接收个人 Key。")
        status = public_pool_status(refresh=True)
        if not status["available"]:
            raise ModelAccessError(status["reason"])
        credentials = ""
    else:
        if body.keys is None:
            raise ModelAccessError("请填写 DeepSeek、硅基流动和 MiniMax 的 API Key。")
        credentials = _encrypt_keys(body.keys)

    token = secrets.token_urlsafe(32)
    access_id = _record_id(token)
    hours = max(1, min(int(os.getenv("MODEL_ACCESS_SESSION_HOURS", "24")), 24))
    expires_at = time.time() + hours * 3600
    from .db import Record, Session

    with Session.begin() as db:
        for row in db.query(Record).filter(Record.kind == SESSION_KIND).all():
            if float(row.data.get("expires_at", 0)) < time.time():
                db.delete(row)
        db.add(Record(id=access_id, kind=SESSION_KIND, data={"mode": mode, "credentials": credentials, "expires_at": expires_at}))
    return {"token": token, "mode": mode, "expires_at": expires_at}


def _session_summary() -> dict | None:
    record = _access_record()
    if not record:
        return None
    return {"mode": record["mode"], "expires_at": record["expires_at"]}


@router.get("/status")
def status():
    return {"fixed_providers": fixed_providers(), "pool": public_pool_status(refresh=True), "session": _session_summary()}


@router.post("/sessions")
def create_session(body: SessionRequest):
    try:
        return _create_session(body)
    except (ModelAccessError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from None
