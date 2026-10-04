"""D09 account system (P0): registration, login, session token, current-user resolution.

Passwords are hashed with PBKDF2-HMAC-SHA256 (standard library only — no new dependency). The
session token is opaque and stored on the user row, rotated on each login. ``current_user``
returns the authenticated user or ``None`` when no valid token is presented, so callers can fall
back to the legacy single local maker during the P0→P3 transition before auth becomes mandatory.
"""

import contextvars
import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from .db import LOCAL_OWNER, Session, User, uid

router = APIRouter(prefix="/api/auth", tags=["auth"])

PBKDF2_ITERATIONS = 210_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_hex, dk_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def _user_public(u: User) -> dict:
    return {"id": u.id, "username": u.username, "email": u.email, "created": u.created}


def current_user(request: Request):
    """Resolve the authenticated user from ``Authorization: Bearer <token>`` or the ``sl_auth``
    cookie. Returns ``None`` when no valid token is present (callers may fall back to local).
    """
    token = ""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        token = request.cookies.get("sl_auth", "")
    if not token:
        return None
    with Session() as db:
        return db.scalar(select(User).where(User.auth_token == token))


def require_user(request: Request) -> User:
    """Strict variant: raises 401 when no valid token is present (used once auth is mandatory)."""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "请先登录。")
    return user


# Per-request context, set by the app middleware so deep helpers (the author owner check) can
# resolve the acting identity without the Request being threaded through every call site.
request_ctx: contextvars.ContextVar = contextvars.ContextVar("sl_request", default=None)


def active_user():
    """The logged-in user for the current request (via the request context), or None when there
    is no HTTP request (worker) or no valid token (the pre-account local maker)."""
    request = request_ctx.get()
    if request is None:
        return None
    return current_user(request)


def assert_owner(row):
    """Enforce ownership on author project routes. The background worker (no HTTP request) is
    unrestricted; every real HTTP request must come from a logged-in user who owns the project.
    """
    request = request_ctx.get()
    if request is None:
        return  # worker / background: no HTTP request, no user required
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "请先登录。")
    if row.owner_id != user.id:
        raise HTTPException(403, "无权访问该项目。")


class RegisterIn(BaseModel):
    username: str
    password: str
    email: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


def _issue(user: User) -> dict:
    return {"ok": True, "user": _user_public(user), "token": user.auth_token}


@router.post("/register")
def register(body: RegisterIn):
    username = body.username.strip()
    if not (1 <= len(username) <= 60):
        raise HTTPException(422, "用户名需为 1–60 个字符。")
    if len(body.password) < 6:
        raise HTTPException(422, "密码至少 6 位。")
    with Session.begin() as db:
        if db.scalar(select(User).where(User.username == username)):
            raise HTTPException(409, "该用户名已存在。")
        user = User(
            id=uid("user"),
            username=username,
            email=body.email.strip(),
            password_hash=hash_password(body.password),
            auth_token=secrets.token_urlsafe(32),
        )
        db.add(user)
    return _issue(user)


@router.post("/login")
def login(body: LoginIn):
    with Session.begin() as db:
        user = db.scalar(select(User).where(User.username == body.username.strip()))
        if not user or not verify_password(body.password, user.password_hash):
            raise HTTPException(401, "用户名或密码错误。")
        user.auth_token = secrets.token_urlsafe(
            32
        )  # rotate the active token on each login
    return _issue(user)


@router.get("/me")
def me(user: User = Depends(current_user)):
    if not user:
        raise HTTPException(401, "请先登录。")
    return _issue(user)
