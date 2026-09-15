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
from sqlalchemy import text


ACCESS_HEADER = "X-Storyloom-Model-Access"
SESSION_KIND = "model_access_session"
POOL_KIND = "public_pool_day"
BALANCE_KIND = "public_pool_balance"
FILM_KIND = "public_pool_film"
_access_id: ContextVar[str] = ContextVar("storyloom_model_access", default="")
_pool_lock = Lock()
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_KEY_FIELDS = ("LLM_API_KEY", "IMAGE_API_KEY", "VIDEO_API_KEY")
_RESERVES = {
    "llm": ("PUBLIC_POOL_LLM_RESERVE_CNY", "0.20"),
    "image": ("PUBLIC_POOL_IMAGE_RESERVE_CNY", "0.50"),
    "video": ("PUBLIC_POOL_VIDEO_RESERVE_CNY", "6.00"),
}
# A video reservation that ignores the requested length burns one whole clip's worth of
# budget per attempt, so short clips are priced by the second instead of by the flat cap.
_VIDEO_RESERVE_PER_SECOND = ("PUBLIC_POOL_VIDEO_RESERVE_PER_SECOND_CNY", "0.60")
_VIDEO_RESERVE_FLOOR = ("PUBLIC_POOL_VIDEO_RESERVE_FLOOR_CNY", "1.00")
_VIDEO_DEFAULT_SECONDS = 8
_BUDGETS = {
    "llm": ("PUBLIC_POOL_LLM_DAILY_BUDGET_CNY", "0"),
    "image": ("PUBLIC_POOL_IMAGE_DAILY_BUDGET_CNY", "0"),
    "video": ("PUBLIC_POOL_VIDEO_DAILY_BUDGET_CNY", "0"),
}
_PROVIDER_KEYS = {"llm": "LLM_API_KEY", "image": "IMAGE_API_KEY", "video": "VIDEO_API_KEY"}
_PROVIDER_NAMES = {"llm": "DeepSeek", "image": "硅基流动", "video": "MiniMax"}


class ModelAccessError(Exception):
    pass


class OwnKeys(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    # Every key is optional: a visitor may bring only the providers a step needs. A missing key is
    # reported by the step that requires it instead of blocking the whole session.
    deepseek: SecretStr | None = Field(default=None, min_length=8, max_length=4096)
    siliconflow: SecretStr | None = Field(default=None, min_length=8, max_length=4096)
    minimax: SecretStr | None = Field(default=None, min_length=8, max_length=4096)


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


def seal_secret(value: str) -> str:
    """Encrypt a server-side secret with the deployment key (used for OAuth tokens at rest)."""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def open_secret(ciphertext: str) -> str | None:
    """Decrypt a server-side secret; None means it can no longer be read."""
    try:
        return _fernet().decrypt(str(ciphertext).encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError, TypeError):
        return None


def _encrypt_keys(keys: OwnKeys) -> str:
    provided = {
        "LLM_API_KEY": keys.deepseek,
        "IMAGE_API_KEY": keys.siliconflow,
        "VIDEO_API_KEY": keys.minimax,
    }
    values = {}
    for name, secret in provided.items():
        if secret is None:
            continue
        value = secret.get_secret_value().strip()
        if not value:
            continue
        if any(char in value for char in "\r\n\x00"):
            raise ModelAccessError("API Key 格式无效。")
        values[name] = value
    if not values:
        raise ModelAccessError("请至少填写一把要使用的 API Key。")
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def _decrypt_keys(ciphertext: str) -> dict[str, str]:
    try:
        raw = _fernet().decrypt(ciphertext.encode("ascii"), ttl=None)
        values = json.loads(raw)
    except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise ModelAccessError("自带 Key 会话无法解密，请重新配置。") from None
    if not isinstance(values, dict) or not values or not set(values) <= set(_KEY_FIELDS):
        raise ModelAccessError("自带 Key 会话内容无效，请重新配置。")
    if not all(isinstance(value, str) and value for value in values.values()):
        raise ModelAccessError("自带 Key 会话内容无效，请重新配置。")
    return values


def _locked_config(base: dict[str, str] | None = None) -> dict[str, str]:
    from .environment import LOCKED_MODEL_CONFIG

    configured = dict(LOCKED_MODEL_CONFIG)
    # Visitors cannot select a transport target or model. The operator can still
    # replace a retired DeepSeek model in the environment without a code release.
    if base:
        for key in ("LLM_MODEL", "LLM_FAST_MODEL"):
            if base.get(key):
                configured[key] = base[key]
    return configured


def effective_model_config(base: dict[str, str]) -> dict[str, str]:
    """Overlay the active anonymous session without mutating global settings."""
    record = _access_record()
    if not record:
        if not public_demo_mode():
            return base
        return {**base, **_locked_config(base), "ALLOW_PAID_CALLS": "false"}

    configured = {**base, **_locked_config(base)}
    if record.get("mode") == "own":
        try:
            visitor = _decrypt_keys(str(record.get("credentials", "")))
        except ModelAccessError:
            configured.update({key: "" for key in _KEY_FIELDS})
            configured["ALLOW_PAID_CALLS"] = "false"
            return configured
        # A visitor's session must never fall back to the operator's keys for a provider the
        # visitor did not supply, so every key the session did not bring is cleared.
        configured.update({key: "" for key in _KEY_FIELDS})
        configured.update(visitor)
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


def _video_seconds(duration_seconds=None) -> int:
    try:
        seconds = int(duration_seconds) if duration_seconds is not None else int(os.getenv("VIDEO_DURATION", str(_VIDEO_DEFAULT_SECONDS)))
    except (TypeError, ValueError):
        seconds = _VIDEO_DEFAULT_SECONDS
    return max(1, min(seconds, 60))


def reserve_amount(kind: str, duration_seconds=None) -> Decimal:
    """Reservation for one call. Video scales with requested seconds up to the configured cap."""
    if kind == "video":
        cap = _decimal_env(*_RESERVES["video"])
        per_second = _decimal_env(*_VIDEO_RESERVE_PER_SECOND)
        if per_second > 0:
            floor = _decimal_env(*_VIDEO_RESERVE_FLOOR)
            scaled = max(floor, per_second * _video_seconds(duration_seconds))
            return min(cap, scaled).quantize(Decimal("0.01"))
        return cap
    return _decimal_env(*_RESERVES[kind])


def _pool_record_id(day: str, kind: str) -> str:
    return f'public_pool_{day.replace("-", "")}_{kind}'


def _pool_state(day: str, kind: str) -> dict:
    from .db import Record, Session

    with Session() as db:
        row = db.get(Record, _pool_record_id(day, kind))
        return dict(row.data) if row and row.kind == POOL_KIND else {
            "day": day, "kind": kind, "reserved_cny": "0.00", "blocked": {},
        }


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
    # A failed probe is a transport problem, not proof that the balance is gone: keep the last
    # known verdict so one timeout cannot take the whole shared pool offline.
    def unreachable(reason: str) -> dict:
        if cached and cached.get("state") == "verified":
            return {**cached, "checked_at": time.time(), "state": "verified",
                    "degraded": reason, "available": bool(cached.get("available"))}
        return {"state": "unavailable", "available": False, "checked_at": time.time(),
                "degraded": reason}
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
    except httpx.HTTPStatusError as exc:
        # An explicit rejection (401/402/403) does mean the key cannot be used.
        result = ({"state": "rejected", "available": False, "checked_at": time.time()}
                  if exc.response is not None and exc.response.status_code in (401, 402, 403)
                  else unreachable(type(exc).__name__))
    except (httpx.HTTPError, ValueError, TypeError, InvalidOperation) as exc:
        result = unreachable(type(exc).__name__)
    _save_balance_cache(result)
    return result


def fixed_providers() -> list[dict[str, str]]:
    from .environment import base_model_config

    cfg = _locked_config(base_model_config())
    return [
        {"kind": "llm", "provider": "DeepSeek", "model": cfg["LLM_MODEL"], "purpose": "文本理解与创作"},
        {"kind": "image", "provider": "硅基流动", "model": cfg["IMAGE_MODEL"], "purpose": "人物、服装与场景画面"},
        {"kind": "video", "provider": "MiniMax", "model": cfg["VIDEO_MODEL"], "purpose": "镜头视频生成"},
    ]


def public_pool_status(*, refresh: bool = True) -> dict:
    from .environment import base_model_config

    day = _pool_day()
    enabled = _enabled("PUBLIC_POOL_ENABLED")
    values: dict[str, dict] = {}
    try:
        for kind in _BUDGETS:
            state = _pool_state(day, kind)
            limit = _decimal_env(*_BUDGETS[kind])
            reserve = reserve_amount(kind)
            used = Decimal(str(state.get("reserved_cny", "0"))).quantize(Decimal("0.01"))
            if used < 0:
                raise InvalidOperation
            values[kind] = {
                "state": state,
                "limit": limit,
                "reserve": reserve,
                "used": used,
                "remaining": max(Decimal("0"), limit - used),
            }
    except (ModelAccessError, InvalidOperation):
        return {
            "available": False, "reason": "共享池预算配置无效", "day": day,
            "next_reset_at": _next_reset_at(), "providers": [],
        }

    base = base_model_config()
    llm_ready = bool(base.get(_PROVIDER_KEYS["llm"]))
    deepseek = _deepseek_balance(base, refresh) if enabled and llm_ready else {
        "state": "not_checked" if not enabled else "missing_key",
        "available": llm_ready,
    }
    checks = []
    for kind, item in values.items():
        key_ready = bool(base.get(_PROVIDER_KEYS[kind]))
        blocked = item["state"].get("blocked")
        budget_ready = item["limit"] > 0 and item["reserve"] > 0 and item["remaining"] >= item["reserve"]
        balance_ready = bool(deepseek["available"]) if kind == "llm" else True
        available = enabled and key_ready and budget_ready and not blocked and balance_ready
        if not enabled:
            reason = "共享池未开启"
            check = "disabled"
        elif not key_ready:
            reason = "共享 Key 未配置"
            check = "missing_key"
        elif blocked:
            reason = "供应商 Key 今日已暂停"
            check = "provider_error"
        elif item["limit"] <= 0:
            reason = "今日额度未配置"
            check = "daily_budget"
        elif item["reserve"] <= 0:
            reason = "单次预留金额配置无效"
            check = "daily_budget"
        elif item["remaining"] < item["reserve"]:
            reason = "今日额度不足"
            check = "daily_budget"
        elif kind == "llm" and not balance_ready:
            reason = "暂时无法确认余额"
            check = deepseek["state"]
        else:
            reason = "可用"
            check = deepseek["state"] if kind == "llm" else "daily_budget"
        checks.append({
            "kind": kind,
            "provider": _PROVIDER_NAMES[kind],
            "check": check,
            "available": available,
            "reason": reason,
            "daily_limit_cny": f'{item["limit"]:.2f}',
            "reserve_cny": f'{item["reserve"]:.2f}',
            "used_cny": f'{item["used"]:.2f}',
            "remaining_cny": f'{item["remaining"]:.2f}',
            **({"calls_affordable": int(item["remaining"] // item["reserve"])} if item["reserve"] > 0 else {}),
        })

    available = bool(checks) and all(item["available"] for item in checks)
    # The pool is still worth entering when only some providers are usable: text or image work can
    # continue, and the step that needs a spent provider reports it by name instead of the whole
    # pool looking broken. `available` keeps meaning "all three are usable today".
    selectable = enabled and any(item["available"] for item in checks)
    if not enabled:
        reason = "共享体验池暂未开启"
    elif not available:
        unavailable = next(item for item in checks if not item["available"])
        reason = f'{unavailable["provider"]}：{unavailable["reason"]}'
    else:
        reason = "共享额度今日均可用，先到先得"
    return {
        "available": available,
        "selectable": selectable,
        "reason": reason,
        "day": day,
        "next_reset_at": _next_reset_at(),
        "providers": checks,
    }


def _reserve_public_call(kind: str, duration_seconds=None) -> dict:
    if kind not in _RESERVES:
        raise ModelAccessError("未知的共享模型调用类型。")
    # This check includes the cached provider-balance result. The transaction
    # below repeats the monetary comparison while holding the worker lock.
    status = public_pool_status(refresh=False)
    provider = next((item for item in status["providers"] if item["kind"] == kind), None)
    if not provider or not provider["available"]:
        raise ModelAccessError(provider["reason"] if provider else status["reason"])
    cost = reserve_amount(kind, duration_seconds)
    limit = _decimal_env(*_BUDGETS[kind])
    day = _pool_day()
    from .db import Record, Session

    with _pool_lock, Session.begin() as db:
        # Serialize the read-compare-write across API and worker processes; the in-process
        # lock alone lets two containers overspend the same daily budget. The lock is taken
        # before the read so the read cannot be stale by the time the write happens.
        _lock_pool_row(db)
        row = db.get(Record, _pool_record_id(day, kind))
        data = dict(row.data) if row else {
            "day": day, "kind": kind, "reserved_cny": "0.00", "blocked": {},
        }
        used = Decimal(str(data.get("reserved_cny", "0")))
        if data.get("blocked"):
            raise ModelAccessError("该共享供应商今日已暂停，请改用自己的 Key。")
        if used + cost > limit:
            raise ModelAccessError("今日共享额度不足以开始这次调用，请改用自己的 Key。")
        data = {**data, "reserved_cny": f"{used + cost:.2f}"}
        if row:
            row.data = data
        else:
            db.add(Record(id=_pool_record_id(day, kind), kind=POOL_KIND, data=data))
    return {"mode": "public", "pool_day": day, "pool_kind": kind, "reserved_cny": f"{cost:.2f}"}


def _lock_pool_row(db) -> None:
    """Take a database-level lock so API and worker cannot both pass the budget check."""
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(782347190322)"))


def release_public_call(kind: str, amount, day: str | None = None) -> bool:
    """Return a reservation that never became a real charge.

    Called when the provider rejected the request (HTTP error) or the task failed before any
    provider submission. Repeating the release is harmless because the counter is clamped at zero.
    """
    if kind not in _RESERVES:
        return False
    try:
        refund = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if refund <= 0:
        return False
    target_day = day or _pool_day()
    from .db import Record, Session

    with _pool_lock, Session.begin() as db:
        _lock_pool_row(db)
        row = db.get(Record, _pool_record_id(target_day, kind))
        if not row:
            return False
        data = dict(row.data)
        used = Decimal(str(data.get("reserved_cny", "0")))
        data["reserved_cny"] = f"{max(Decimal('0'), used - refund):.2f}"
        row.data = data
    return True


def authorize_call(kind: str, duration_seconds=None) -> dict:
    record = _access_record()
    if not record:
        if not public_demo_mode():
            return {"mode": "local"}
        raise ModelAccessError("请先在模型使用方式页面选择共享池或配置自己的 Key。")
    mode = record.get("mode")
    if mode == "own":
        return {"mode": "own"}
    if mode == "public":
        return _reserve_public_call(kind, duration_seconds)
    raise ModelAccessError("模型使用会话无效，请重新配置。")


def affordable_shot_budget() -> tuple[int, str] | None:
    """How many shots the shared pool can still pay for, or None when no pool governs.

    A film that plans more shots than the remaining budget can pay for dies half-finished, so the
    shot plan is capped to the affordable count instead of failing at the last clip.
    """
    if not public_demo_mode() or not _enabled("PUBLIC_POOL_ENABLED"):
        return None
    try:
        status = public_pool_status(refresh=False)
    except ModelAccessError:
        return None
    video = next((item for item in status["providers"] if item["kind"] == "video"), None)
    if not video:
        return None
    if not video["available"]:
        return (0, video["reason"])
    affordable = int(video.get("calls_affordable", 0))
    # Keep one call in hand so a single retry does not end the day mid-film.
    return (max(0, affordable - 1), "共享额度今日剩余可支持 " + str(max(0, affordable - 1)) + " 个镜头")


def claim_public_film() -> str | None:
    """Reserve one shared-pool film for this anonymous session today.

    First-come-first-served with no per-visitor limit let the first visitor spend the whole daily
    video budget, so nobody after them could watch anything. Returns a refusal reason, or None when
    the film may start. Sessions bringing their own keys are never limited.
    """
    if not public_demo_mode() or not _enabled("PUBLIC_POOL_ENABLED"):
        return None
    record = _access_record()
    if not record or record.get("mode") != "public":
        return None
    limit = _configured_positive_int("PUBLIC_POOL_FILMS_PER_SESSION_PER_DAY", 1)
    if limit <= 0:
        return None
    access_id = current_access_id()
    day = _pool_day()
    from .db import Record, Session

    key = f'pool_film_{day.replace("-", "")}_{hashlib.sha256(access_id.encode()).hexdigest()[:16]}'
    with _pool_lock, Session.begin() as db:
        _lock_pool_row(db)
        row = db.get(Record, key)
        used = int(row.data.get("films", 0)) if row and row.kind == FILM_KIND else 0
        if used >= limit:
            return f"共享体验池每天为每位访客保留 {limit} 部漫剧；今天已经用完了。可以改用自己的 Key，或明天再来。"
        data = {"day": day, "session": hashlib.sha256(access_id.encode()).hexdigest()[:16],
                "films": used + 1, "updated_at": time.time()}
        if row:
            row.data = data
        else:
            db.add(Record(id=key, kind=FILM_KIND, data=data))
    return None


def release_public_film() -> None:
    """Give the film allowance back when the run never reached a paid submission."""
    if not public_demo_mode() or not _enabled("PUBLIC_POOL_ENABLED"):
        return
    access_id = current_access_id()
    if not access_id:
        return
    day = _pool_day()
    from .db import Record, Session

    key = f'pool_film_{day.replace("-", "")}_{hashlib.sha256(access_id.encode()).hexdigest()[:16]}'
    with _pool_lock, Session.begin() as db:
        _lock_pool_row(db)
        row = db.get(Record, key)
        if not row or row.kind != FILM_KIND:
            return
        data = dict(row.data)
        data["films"] = max(0, int(data.get("films", 0)) - 1)
        data["updated_at"] = time.time()
        row.data = data


def _configured_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(0, min(value, 1000))


def access_paid_state() -> bool | None:
    states = access_paid_states()
    return states["all"] if states is not None else None


def access_paid_states() -> dict[str, bool] | None:
    record = _access_record()
    if not record:
        return {"all": False, **{kind: False for kind in _BUDGETS}} if public_demo_mode() else None
    if record.get("mode") == "own":
        return {"all": True, **{kind: True for kind in _BUDGETS}}
    if record.get("mode") == "public":
        status = public_pool_status(refresh=False)
        by_kind = {item["kind"]: bool(item["available"]) for item in status["providers"]}
        return {"all": bool(status["available"]), **{kind: by_kind.get(kind, False) for kind in _BUDGETS}}
    return {"all": False, **{kind: False for kind in _BUDGETS}}


def mark_public_provider_unavailable(kind: str, status_code: int) -> None:
    if kind not in _BUDGETS or current_access_mode() != "public" or status_code not in (401, 402, 403):
        return
    day = _pool_day()
    from .db import Record, Session

    with _pool_lock, Session.begin() as db:
        # Same database lock as the budget path: two containers marking the same provider must not
        # overwrite each other's block record.
        _lock_pool_row(db)
        row = db.get(Record, _pool_record_id(day, kind))
        data = dict(row.data) if row else {
            "day": day, "kind": kind, "reserved_cny": "0.00", "blocked": {},
        }
        data["blocked"] = {"at": time.time(), "status": status_code}
        if row:
            row.data = data
        else:
            db.add(Record(id=_pool_record_id(day, kind), kind=POOL_KIND, data=data))


def _create_session(body: SessionRequest) -> dict:
    mode = body.mode.strip().lower()
    if mode not in ("public", "own"):
        raise ModelAccessError("请选择共享体验池或使用自己的 Key。")
    if mode == "public":
        if body.keys is not None:
            raise ModelAccessError("共享体验池不接收个人 Key。")
        status = public_pool_status(refresh=True)
        if not status["selectable"]:
            raise ModelAccessError(
                status["reason"] if not _enabled("PUBLIC_POOL_ENABLED")
                else "共享体验池今天没有可用额度，请改用自己的 Key。")
        credentials = ""
    else:
        if body.keys is None:
            raise ModelAccessError("请至少填写一把要使用的 API Key。")
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
