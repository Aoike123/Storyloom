"""DB-01 模型验证：studio_projects / studio_source_revisions / studio_command_records 表结构与约束。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件只做防御断言，不自行改写 STUDIO_* env。
"""
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.core.accounts import User
from backend.studio.projects.models import StudioProject
from backend.studio.sources.models import SourceRevision
from backend.studio.contracts.models import CommandRecord
from backend.core.db import Base, engine, Session


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（projects↔source_revisions）：drop 前临时关 FK
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        Base.metadata.drop_all(conn, checkfirst=True)
        conn.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(conn)
        conn.commit()
    finally:
        conn.close()
    yield
    engine.dispose()


def _ulid() -> str:
    return "01" + uuid.uuid4().hex[:24]


def _make_users(s, ids):
    """Raw-SQL insert of minimal users rows (all NOT NULL columns)."""
    for uid in ids:
        s.execute(
            text(
                "INSERT INTO users (id, username, email, password_hash, auth_token, created)"
                " VALUES (:id, :uname, :email, :ph, :at, :cr)"
            ),
            {"id": uid, "uname": uid, "email": f"{uid}@t", "ph": "x", "at": "t", "cr": 0.0},
        )
    s.commit()


def test_t0_engine_url(db):
    """防御断言：engine URL 与 session 丢弃库（conftest 设定的 STUDIO_DATABASE_URL）一致。"""
    assert str(engine.url) == os.environ["STUDIO_DATABASE_URL"]


def test_t1_table_set(db):
    """session 丢弃库包含本卡三张表 + users，且不出现旧系统表（records/tasks）。

    session 级 Base 注册全部模型（conftest），表集合为其并集，
    隔离性断言 = 期望表存在 ∧ 无旧表（不依赖各文件 import 顺序）。
    """
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    expected = {"users", "studio_projects", "studio_source_revisions", "studio_command_records"}
    assert expected <= tables, f"缺表: got {tables}, expected superset of {expected}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"


def test_t2_project_constraints(db):
    """项目唯一约束 (owner_id, name) & visibility CHECK。"""
    s = Session()
    _make_users(s, ["u1", "u2"])

    # p1 成功
    p1 = StudioProject(id=_ulid(), owner_id="u1", name="A")
    s.add(p1)
    s.commit()

    # 同 owner 同 name → IntegrityError
    p_dup = StudioProject(id=_ulid(), owner_id="u1", name="A")
    s.add(p_dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 不同 owner 同 name → 成功
    p2 = StudioProject(id=_ulid(), owner_id="u2", name="A")
    s.add(p2)
    s.commit()

    # visibility 非法值 → IntegrityError
    p_bad = StudioProject(id=_ulid(), owner_id="u2", name="B", visibility="private-ok")
    s.add(p_bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # visibility="public" → 成功
    p_pub = StudioProject(id=_ulid(), owner_id="u2", name="C", visibility="public")
    s.add(p_pub)
    s.commit()
    s.close()


def test_t3_revision_chain(db):
    """修订链：project FK + self-referencing previous_revision_id FK。"""
    s = Session()
    _make_users(s, ["u1"])
    p1 = StudioProject(id=_ulid(), owner_id="u1", name="Chain")
    s.add(p1)
    s.commit()

    # rev1 (first, previous=None)
    rev1 = SourceRevision(
        id=_ulid(),
        project_id=p1.id,
        previous_revision_id=None,
        raw_content="hello\r\nworld",
        raw_hash="a" * 64,
        canonical_content="hello\nworld",
        canonical_hash="b" * 64,
        char_length=11,
    )
    s.add(rev1)
    s.commit()

    # rev2 (previous=rev1)
    rev2 = SourceRevision(
        id=_ulid(),
        project_id=p1.id,
        previous_revision_id=rev1.id,
        raw_content="v2",
        raw_hash="c" * 64,
        canonical_content="v2",
        canonical_hash="d" * 64,
        char_length=2,
    )
    s.add(rev2)
    s.commit()

    # 读回一致
    got1 = s.execute(select(SourceRevision).where(SourceRevision.id == rev1.id)).scalar_one()
    assert got1.previous_revision_id is None
    got2 = s.execute(select(SourceRevision).where(SourceRevision.id == rev2.id)).scalar_one()
    assert got2.previous_revision_id == rev1.id
    assert got2.project_id == p1.id

    # project_id 指向不存在的项目 → IntegrityError
    bad_rev = SourceRevision(
        id=_ulid(),
        project_id="nonexistent_pid",
        previous_revision_id=None,
        raw_content="x",
        raw_hash="e" * 64,
        canonical_content="x",
        canonical_hash="f" * 64,
        char_length=1,
    )
    s.add(bad_rev)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # previous_revision_id 指向不存在的修订 → IntegrityError
    bad_rev2 = SourceRevision(
        id=_ulid(),
        project_id=p1.id,
        previous_revision_id="nonexistent_rev",
        raw_content="y",
        raw_hash="e" * 64,
        canonical_content="y",
        canonical_hash="f" * 64,
        char_length=1,
    )
    s.add(bad_rev2)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t4_canonical_fields(db):
    """offset_policy 默认 & CHECK 约束；char_length 原样存取。"""
    s = Session()
    _make_users(s, ["u1"])
    p1 = StudioProject(id=_ulid(), owner_id="u1", name="Canon")
    s.add(p1)
    s.commit()

    # 默认 offset_policy
    rev = SourceRevision(
        id=_ulid(),
        project_id=p1.id,
        raw_content="test",
        raw_hash="a" * 64,
        canonical_content="test",
        canonical_hash="b" * 64,
        char_length=42,
    )
    s.add(rev)
    s.commit()
    assert rev.offset_policy == "lf-utf16-v1"
    assert rev.char_length == 42

    # 显式 offset_policy="other" → IntegrityError
    bad_rev = SourceRevision(
        id=_ulid(),
        project_id=p1.id,
        raw_content="x",
        raw_hash="a" * 64,
        canonical_content="x",
        canonical_hash="b" * 64,
        offset_policy="other",
        char_length=1,
    )
    s.add(bad_rev)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t5_command_records_idempotency(db):
    """(owner_id, command_id) 唯一约束 & project_id 可空。"""
    s = Session()
    _make_users(s, ["u1", "u2"])
    p1 = StudioProject(id=_ulid(), owner_id="u1", name="Cmd")
    s.add(p1)
    s.commit()

    # u1 + cmd1 插入成功
    cr1 = CommandRecord(
        id=_ulid(),
        owner_id="u1",
        project_id=p1.id,
        command_id="cmd1",
    )
    s.add(cr1)
    s.commit()

    # 同 owner + 同 command_id → IntegrityError
    cr_dup = CommandRecord(
        id=_ulid(),
        owner_id="u1",
        project_id=p1.id,
        command_id="cmd1",
    )
    s.add(cr_dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # u2 + cmd1（不同 owner 同 command_id）→ 成功
    cr2 = CommandRecord(
        id=_ulid(),
        owner_id="u2",
        project_id=None,
        command_id="cmd1",
    )
    s.add(cr2)
    s.commit()

    # project_id 可空
    assert cr2.project_id is None
    s.close()


def test_t6_no_stage_or_cover_columns(db):
    """studio_projects 无 stage 列、无 cover 开头列。"""
    col_names = {c.name for c in StudioProject.__table__.columns}
    assert not ({"stage"} & col_names), f"stage column found: {col_names}"
    assert not any(n.startswith("cover") for n in col_names), f"cover* column found: {col_names}"


def test_t7_source_audit(db):
    """三个模型文件源码不含禁串。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    model_files = [
        root / "backend/studio/projects/models.py",
        root / "backend/studio/sources/models.py",
        root / "backend/studio/contracts/models.py",
    ]
    forbidden = [
        "backend.db",
        "backend.app",
        "backend.auth",
        "backend.providers",
        "backend.worker",
        "from ..db",
        "from ..app",
        "from ..auth",
        "from ..environment",
        "load_bootstrap_environment",
    ]
    for f in model_files:
        src = f.read_text(encoding="utf-8")
        for pat in forbidden:
            assert pat not in src, f"{f.name} contains forbidden string: {pat!r}"
