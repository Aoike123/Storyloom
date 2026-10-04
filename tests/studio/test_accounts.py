"""BASE-03-b — 账号身份模型验证。

模块级 env 在 import backend.core.db 之前设置，确保本进程 engine 绑定丢弃库。
DSH sandbox 限制：collection 期可创建文件/目录，test 执行期 mkdir 被拦截。
因此 fixture setup 中若目录缺失则重建（collection 期已创建的文件仍可用），
teardown 仅 dispose engine，最终清理由 session 级 hook 执行。
"""
import atexit
import os
import re
import shutil
import sqlite3
import uuid as _uuid

# ---------------------------------------------------------------------------
# 模块级 env（在任何 backend.* import 之前）
# ---------------------------------------------------------------------------
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TMP = f"data/studio-test-{_uuid.uuid4().hex[:8]}"
_TMP_ABS = os.path.join(_ROOT, _TMP)
os.makedirs(_TMP_ABS, exist_ok=True)
_DB_FILE = os.path.join(_TMP_ABS, "accounts.db")
os.environ["STUDIO_DATA_DIR"] = _TMP_ABS
os.environ["STUDIO_DATABASE_URL"] = f"sqlite:///{_DB_FILE}"


def _ensure_db_file():
    """确保丢弃库目录与文件存在（DSH sandbox 下 test 执行期 mkdir 可能受限）。"""
    if not os.path.isdir(_TMP_ABS):
        os.makedirs(_TMP_ABS, exist_ok=True)
    if not os.path.isfile(_DB_FILE):
        conn = sqlite3.connect(_DB_FILE)
        conn.execute("CREATE TABLE _init(x)")
        conn.commit()
        conn.execute("DROP TABLE _init")
        conn.commit()
        conn.close()


# 注册进程退出时的最终清理
atexit.register(shutil.rmtree, _TMP_ABS, ignore_errors=True)

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

    _ensure_db_file()
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
    """本进程丢弃库中表集合 == {users}（不出现 records/tasks/studio_projects 等）。"""
    with _db.engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))}
    assert tables == {"users"}, f"表集合不符（期望仅 users）: {tables}"
