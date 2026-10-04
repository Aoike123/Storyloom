"""BASE-03-b — 账号身份模型验证。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例，
各文件不得各自改写 STUDIO_* env）；本文件只做防御断言。
"""
import os
import re

import pytest
import sqlalchemy
from sqlalchemy import text

from backend.core import db as _db
from backend.core.accounts import (
    User,
    hash_password,
    make_user_id,
    verify_password,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    from backend.core.db import Base, engine

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


# ---------------------------------------------------------------------------
# T0 — 防御断言：本进程 engine 绑定丢弃库
# ---------------------------------------------------------------------------

def test_t0_engine_bound_to_discard_db(db) -> None:
    """若 engine 绑定到非丢弃库，立即 fail 并打印实际值。"""
    actual = _db.DATABASE_URL
    expected = os.environ["STUDIO_DATABASE_URL"]
    assert actual == expected, (
        f"engine 绑定的 DATABASE_URL 非丢弃库：\n"
        f"  期望: {expected}\n"
        f"  实际: {actual}"
    )


# ---------------------------------------------------------------------------
# T1 — 建表与插入
# ---------------------------------------------------------------------------

def test_t1_create_and_insert(db) -> None:
    from backend.core.db import Session

    # users 表存在于 sqlite_master
    with _db.engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))}
    assert "users" in tables, f"users 表缺失: {tables}"

    # 插入
    uid = make_user_id()
    pwd_hash = hash_password("secret-1")
    u = User(
        id=uid,
        username="alice",
        password_hash=pwd_hash,
        auth_token="tok-1",
    )
    with Session() as s:
        s.add(u)
        s.commit()

    # 读回
    with Session() as s:
        row = s.query(User).filter_by(id=uid).one()
    assert row.id == uid
    assert row.username == "alice"
    assert row.password_hash == pwd_hash
    assert row.auth_token == "tok-1"


# ---------------------------------------------------------------------------
# T2 — 密码哈希往返
# ---------------------------------------------------------------------------

def test_t2_password_roundtrip() -> None:
    stored = hash_password("secret-1")
    assert verify_password("secret-1", stored) is True
    assert verify_password("wrong", stored) is False
    assert verify_password("x", "garbage") is False
    assert verify_password("x", "") is False
    # 两次哈希同输入 → 不同 salt（字符串不相等），但都能验证
    stored2 = hash_password("secret-1")
    assert stored != stored2, "同输入两次哈希应产生不同 salt"
    assert verify_password("secret-1", stored2) is True


# ---------------------------------------------------------------------------
# T3 — 唯一用户名
# ---------------------------------------------------------------------------

def test_t3_unique_username(db) -> None:
    from backend.core.db import Session

    u1 = User(id=make_user_id(), username="bob", password_hash=hash_password("p1"), auth_token="t1")
    u2 = User(id=make_user_id(), username="bob", password_hash=hash_password("p2"), auth_token="t2")
    with Session() as s:
        s.add(u1)
        s.commit()
        s.add(u2)
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            s.commit()


# ---------------------------------------------------------------------------
# T4 — id 格式
# ---------------------------------------------------------------------------

def test_t4_id_format() -> None:
    for _ in range(5):
        uid = make_user_id()
        assert re.fullmatch(r"user_[0-9a-f]{16}", uid), f"id 格式不符: {uid}"


# ---------------------------------------------------------------------------
# T5 — 表隔离
# ---------------------------------------------------------------------------

def test_t5_table_isolation(db) -> None:
    """丢弃库中新系统表存在且不出现旧系统表（records/tasks）。

    注意：session 级 Base 注册了多个模型（见 conftest），表集合是它们的并集，
    不能断言"仅 users"；隔离性 = 无旧表。
    """
    with _db.engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))}
    assert "users" in tables, f"users 表缺失: {tables}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"
