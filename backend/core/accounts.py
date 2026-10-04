"""Storyloom Studio — 账号身份模型（D09）。

字段语义沿用已批准的旧账号系统：PBKDF2-HMAC-SHA256 密码哈希（stdlib only，
210000 迭代，格式 pbkdf2$迭代数$salt16hex$dk_hex）、每次登录轮换的不透明
会话 token。模型注册到 backend.core.db 的 Base（新表 users，位于新库
data/studio/story.db，与旧库无共享）。不 import 任何旧 backend 模块。
"""
import hashlib
import hmac
import secrets
import time
import uuid

from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

PBKDF2_ITERATIONS = 210_000


def make_user_id() -> str:
    return f"user_{uuid.uuid4().hex[:16]}"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)  # 16 bytes random salt
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_hex, dk_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(256), default="")
    auth_token: Mapped[str] = mapped_column(String(128), default="", index=True)
    created: Mapped[float] = mapped_column(Float, default=time.time)
