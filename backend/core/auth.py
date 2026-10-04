"""Storyloom Studio — 严格鉴权（D09 新实现）。

与旧 backend/auth.py 的两点差异：
1. 无匿名回退——所有业务 HTTP 端点必须登录；旧实现允许未登录请求回退为
   本地匿名身份，本模块的 require_user 一律要求有效 token，否则 401。
2. 无 contextvar/worker 旁路——旧实现在无 HTTP 请求上下文时直接放行
   （后台任务不受所有权约束）；本模块的 assert_owner 一律校验登录与
   所有权，后台任务的身份走 StudioJob 的 owner_id（由 JOB 卡实现），
   不经过本模块。

凭证解析沿用 D09 语义：``Authorization: Bearer <token>``（前缀大小写
不敏感）优先，其次 ``sl_auth`` cookie；token 为不透明串，存于用户行，
每次登录轮换（旧 token 立即失效）。不 import 任何旧 backend 模块
（backend/auth.py、backend/db.py 等）；错误统一 raise StudioAPIError
（冻结错误协议，附录 00 §4）。
"""
import secrets

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from .accounts import User, hash_password, make_user_id, verify_password
from .db import Session
from ..studio.contracts.errors import StudioAPIError

router = APIRouter(prefix="/api/studio/auth", tags=["studio-auth"])

AUTH_COOKIE = "sl_auth"


def current_user(request: Request) -> User | None:
    """从 ``Authorization: Bearer <token>``（前缀大小写不敏感）或 ``sl_auth``
    cookie 解析当前用户；无凭证或 token 无效 → None。
    """
    token = ""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        token = request.cookies.get(AUTH_COOKIE, "")
    if not token:
        return None
    with Session() as db:
        return db.scalar(select(User).where(User.auth_token == token))


def require_user(request: Request) -> User:
    """严格版：无有效 token 一律 raise 401（冻结错误协议），无匿名回退。"""
    user = current_user(request)
    if not user:
        raise StudioAPIError.unauthenticated()
    return user


def assert_owner(request: Request, owner_id: str, kind: str = "project") -> User:
    """一律校验登录与所有权：先 require_user，再比对 owner_id，不符 raise
    403。无 HTTP 请求的后台任务不走此函数（其身份走 StudioJob.owner_id）。
    """
    user = require_user(request)
    if user.id != owner_id:
        raise StudioAPIError.forbidden(kind, owner_id)
    return user


class RegisterIn(BaseModel):
    username: str
    password: str
    email: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


def _user_public(u: User) -> dict:
    return {"id": u.id, "username": u.username, "email": u.email, "created": u.created}


def _issue(user: User) -> dict:
    return {"user": _user_public(user), "token": user.auth_token}


@router.post("/register", status_code=201)
def register(body: RegisterIn):
    username = body.username.strip()
    violations: list[dict[str, str]] = []
    if not (1 <= len(username) <= 60):
        violations.append(
            {"field": "username", "rule": "length", "message": "用户名需为 1–60 个字符。"}
        )
    if len(body.password) < 6:
        violations.append(
            {"field": "password", "rule": "min_length", "message": "密码至少 6 位。"}
        )
    if violations:
        raise StudioAPIError.validation_failed(violations)
    with Session.begin() as db:
        if db.scalar(select(User).where(User.username == username)):
            raise StudioAPIError.validation_failed(
                [{"field": "username", "rule": "unique", "message": "该用户名已存在。"}]
            )
        user = User(
            id=make_user_id(),
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
            raise StudioAPIError.unauthenticated("用户名或密码错误。")
        user.auth_token = secrets.token_urlsafe(32)  # 每次登录轮换 token
    return _issue(user)


@router.get("/me")
def me(user: User = Depends(require_user)):
    return _issue(user)
