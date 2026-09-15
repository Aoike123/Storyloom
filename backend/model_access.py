"""Who pays for a model call.

Two modes, and no third:

``account``
    A signed-in Zhihu account spends the compute beans granted to it at first login. This is the
    normal path: the account owns its budget, so one visitor cannot consume the day's allowance
    before anyone else arrives.
``own``
    A visitor who attached their own provider keys. This is the fallback once a wallet runs out,
    and the only mode that does not spend the operator's budget.

Visitors never choose endpoints or model identifiers. BYOK credentials are encrypted at rest and
tasks retain only an opaque session id.
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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

ACCESS_HEADER = "X-Storyloom-Model-Access"
SESSION_KIND = "model_access_session"
ACCOUNT_TOKEN_PREFIX = "zhihu:"
OPERATOR_KEY_KIND = "operator_key_day"
_access_id: ContextVar[str] = ContextVar("storyloom_model_access", default="")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_KEY_FIELDS = ("LLM_API_KEY", "IMAGE_API_KEY", "VIDEO_API_KEY")
_PROVIDER_KEYS = {"llm": "LLM_API_KEY", "image": "IMAGE_API_KEY", "video": "VIDEO_API_KEY"}
_KINDS = ("llm", "image", "video")


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


def access_id_for_account(session_id: str) -> str:
    """The access record a signed-in Zhihu browser spends from.

    Keyed by the login session so a task can inherit it exactly like any other access session, which
    keeps the existing worker plumbing unchanged.
    """
    return _record_id(ACCOUNT_TOKEN_PREFIX + str(session_id))


def create_account_session(session_id: str, uid: str, hours: int | None = None) -> dict:
    """Create or refresh the bean-backed access session for a signed-in account."""
    from . import beans
    access_id = access_id_for_account(session_id)
    ttl = max(1, min(int(hours or os.getenv("MODEL_ACCESS_SESSION_HOURS", "24")), 24))
    expires_at = time.time() + ttl * 3600
    from .db import Record, Session

    with Session.begin() as db:
        beans.ensure_wallet(db, uid)
        row = db.get(Record, access_id)
        data = {"mode": "account", "uid": str(uid), "expires_at": expires_at}
        if row:
            row.data = data
            row.version += 1
        else:
            db.add(Record(id=access_id, kind=SESSION_KIND, data=data))
    return {"access_id": access_id, "uid": str(uid), "expires_at": expires_at}


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


def current_account_uid() -> str | None:
    """The signed-in account behind the active access session, when there is one."""
    record = _access_record()
    if not record or record.get("mode") != "account":
        return None
    uid = str(record.get("uid") or "")
    return uid or None


def has_payer() -> bool:
    """Whether this browser can pay for a generation call at all."""
    if not public_demo_mode():
        # A local run uses the operator's own configured keys, so there is nothing to gate.
        return True
    record = _access_record()
    if not record:
        return False
    mode = record.get("mode")
    if mode == "own":
        return True
    if mode == "account":
        return bool(record.get("uid"))
    return False


def payer_requirement_message() -> str | None:
    """Explain how to become able to generate, or None when this browser already can."""
    if has_payer():
        return None
    if not public_demo_mode():
        return None
    return "请先用知乎账号登录领取算力豆；如果要用自己的额度，请在右上角的账号面板里填写自己的 API Key。"


def _secret_material() -> str:
    configured = os.getenv("MODEL_ACCESS_SECRET", "").strip()
    if configured:
        if len(configured) < 32:
            raise ModelAccessError("MODEL_ACCESS_SECRET 至少需要 32 个字符。")
        return configured
    if public_demo_mode():
        raise ModelAccessError("公网部署缺少 MODEL_ACCESS_SECRET，暂不能保存自带 Key。")

    # Local development gets one durable process-shared secret without asking the user to manage
    # another file. Production must use the environment key.
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
    # Visitors cannot select a transport target or model. The operator can still replace a retired
    # DeepSeek model in the environment without a code release.
    if base:
        for key in ("LLM_MODEL", "LLM_FAST_MODEL"):
            if base.get(key):
                configured[key] = base[key]
    return configured


def effective_model_config(base: dict[str, str]) -> dict[str, str]:
    """Overlay the active session without mutating global settings."""
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
    elif record.get("mode") == "account":
        # A signed-in account spends beans against the operator's keys, which is the intended path.
        pass
    else:
        configured.update({key: "" for key in _KEY_FIELDS})
        configured["ALLOW_PAID_CALLS"] = "false"
        return configured
    configured["ALLOW_PAID_CALLS"] = "true"
    return configured


def _day(now: datetime | None = None) -> str:
    return (now or datetime.now(_SHANGHAI)).date().isoformat()


def _operator_key_id(day: str, kind: str) -> str:
    return f'operator_key_{day.replace("-", "")}_{kind}'


def note_operator_key_rejection(kind: str, status_code: int) -> None:
    """Remember that the operator's key for one provider was refused today.

    Without this, every visitor would keep retrying a dead key and see a raw provider error. Only
    the account path is affected: a visitor's own rejected key is their own business.
    """
    if kind not in _KINDS or status_code not in (401, 402, 403):
        return
    if current_access_mode() != "account":
        return
    day = _day()
    from .db import Record, Session

    with Session.begin() as db:
        key = _operator_key_id(day, kind)
        row = db.get(Record, key)
        data = {"day": day, "kind": kind, "blocked": {"at": time.time(), "status": status_code}}
        if row:
            row.data = {**row.data, **data}
        else:
            db.add(Record(id=key, kind=OPERATOR_KEY_KIND, data=data))


def _operator_key_blocked(kind: str) -> bool:
    from .db import Record, Session

    with Session() as db:
        row = db.get(Record, _operator_key_id(_day(), kind))
        return bool(row and row.kind == OPERATOR_KEY_KIND and row.data.get("blocked"))


def _reserve_account_call(record: dict, kind: str, duration_seconds=None, task_id=None) -> dict:
    """A signed-in account pays from its own bean wallet."""
    from . import beans
    uid = str(record.get("uid") or "")
    if not uid:
        raise ModelAccessError("登录账号信息不完整，请重新登录。")
    if _operator_key_blocked(kind):
        raise ModelAccessError("运营方提供的该模型 Key 今日已被供应商暂停。请填写自己的 API Key 继续。")
    try:
        charged = beans.debit(uid, kind, task_id, duration_seconds)
    except beans.BeansExhausted as exc:
        raise ModelAccessError(str(exc)) from None
    return {"mode": "account", "uid": uid, "kind": kind, **charged}


def authorize_call(kind: str, duration_seconds=None, task_id: str | None = None) -> dict:
    record = _access_record()
    if not record:
        if not public_demo_mode():
            return {"mode": "local"}
        raise ModelAccessError("请先用知乎账号登录领取算力豆，或填写自己的 API Key。")
    mode = record.get("mode")
    if mode == "own":
        return {"mode": "own"}
    if mode == "account":
        return _reserve_account_call(record, kind, duration_seconds, task_id)
    # An unknown or retired mode — for example a session created for the removed shared pool — must
    # never be treated as a payer. Say what to do rather than reporting a broken session.
    raise ModelAccessError(payer_requirement_message() or "模型使用会话无效，请重新配置。")


def affordable_shot_budget() -> tuple[int, str] | None:
    """How many shots this account's beans can still pay for, or None when unbounded.

    A film that plans more shots than the wallet can pay for dies half-finished, so the shot plan is
    capped to the affordable count instead of failing at the last clip. Visitors on their own keys
    are not capped here.
    """
    uid = current_account_uid()
    if not uid:
        return None
    from . import beans

    per_clip = beans.cost_of("video")
    if per_clip <= 0:
        return None
    affordable = int(beans.balance(uid) // per_clip)
    # Keep roughly one clip in hand so a single retry does not end the film.
    reserve = 1 if affordable >= 4 else 0
    limit = max(0, affordable - reserve)
    return (limit, f"算力豆剩余可支持 {limit} 个镜头")


def access_paid_states() -> dict[str, bool] | None:
    """Per-kind payability for the current session. None means "not governed here" (local mode)."""
    record = _access_record()
    if not record:
        if public_demo_mode():
            return {"all": False, **{kind: False for kind in _KINDS}}
        return None
    mode = record.get("mode")
    if mode == "own":
        return {"all": True, **{kind: True for kind in _KINDS}}
    if mode == "account":
        from . import beans
        uid = str(record.get("uid") or "")
        by_kind = {kind: bool(uid) and beans.affordable(uid, kind) for kind in _KINDS}
        return {"all": any(by_kind.values()), **by_kind}
    return {"all": False, **{kind: False for kind in _KINDS}}


def access_paid_state() -> bool | None:
    states = access_paid_states()
    return states["all"] if states is not None else None


def fixed_providers() -> list[dict[str, str]]:
    from .environment import base_model_config

    cfg = _locked_config(base_model_config())
    return [
        {"kind": "llm", "provider": "DeepSeek", "model": cfg["LLM_MODEL"], "purpose": "文本理解与创作"},
        {"kind": "image", "provider": "硅基流动", "model": cfg["IMAGE_MODEL"], "purpose": "人物、服装与场景画面"},
        {"kind": "video", "provider": "MiniMax", "model": cfg["VIDEO_MODEL"], "purpose": "镜头视频生成"},
    ]


def _create_session(body: SessionRequest) -> dict:
    mode = body.mode.strip().lower()
    if mode != "own":
        raise ModelAccessError("共享体验池已停用：请用知乎账号登录领取算力豆，或填写自己的 API Key。")
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
    return {"fixed_providers": fixed_providers(), "session": _session_summary()}


@router.post("/sessions")
def create_session(body: SessionRequest):
    try:
        return _create_session(body)
    except (ModelAccessError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from None
