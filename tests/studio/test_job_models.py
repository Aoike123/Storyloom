"""DB-08 模型验证：studio_jobs / studio_job_attempts / studio_job_events 表结构与约束。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件只做防御断言，不自行改写 STUDIO_* env。
"""
import json
import os
import time
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.core.accounts import User
from backend.core.db import Base, engine, Session
from backend.studio.jobs.models import JobAttempt, JobEvent, StudioJob
from backend.studio.projects.models import StudioProject


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


def _make_user(s, uid):
    """Raw-SQL insert of a minimal users row (all NOT NULL columns)."""
    s.execute(
        text(
            "INSERT INTO users (id, username, email, password_hash, auth_token, created)"
            " VALUES (:id, :uname, :email, :ph, :at, :cr)"
        ),
        {"id": uid, "uname": uid, "email": f"{uid}@t", "ph": "x", "at": "t", "cr": 0.0},
    )
    s.commit()


def _make_project(s, pid, owner_id):
    """Insert a minimal studio_project row."""
    p = StudioProject(id=pid, owner_id=owner_id, name=f"proj_{pid}")
    s.add(p)
    s.commit()
    return p


def test_t0_engine_url(db):
    """防御断言：engine URL 与 session 丢弃库（conftest 设定的 STUDIO_DATABASE_URL）一致。"""
    assert str(engine.url) == os.environ["STUDIO_DATABASE_URL"]


def test_t1_table_set(db):
    """session 丢弃库包含本卡三张表，且不出现旧系统表（records/tasks）。"""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    expected = {"studio_jobs", "studio_job_attempts", "studio_job_events"}
    assert expected <= tables, f"缺表: got {tables}, expected superset of {expected}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"


def test_t2_job_constraints(db):
    """kind/status CHECK 约束 & FK 约束。"""
    s = Session()
    _make_user(s, "u1")
    p1 = _make_project(s, _ulid(), "u1")

    # 合法 kind + 默认 status → 成功
    j1 = StudioJob(
        id=_ulid(),
        project_id=p1.id,
        owner_id="u1",
        kind="export",
        payload="{}",
    )
    s.add(j1)
    s.commit()
    got = s.execute(select(StudioJob).where(StudioJob.id == j1.id)).scalar_one()
    assert got.status == "pending"
    assert got.attempts == 0
    assert got.priority == 0

    # 非法 kind → IntegrityError
    j_bad_kind = StudioJob(
        id=_ulid(),
        project_id=p1.id,
        owner_id="u1",
        kind="legacy_stage",
        payload="{}",
    )
    s.add(j_bad_kind)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 非法 status → IntegrityError
    j_bad_status = StudioJob(
        id=_ulid(),
        project_id=p1.id,
        owner_id="u1",
        kind="export",
        payload="{}",
        status="weird",
    )
    s.add(j_bad_status)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # project_id 悬空 → IntegrityError
    j_bad_proj = StudioJob(
        id=_ulid(),
        project_id="nonexistent_pid",
        owner_id="u1",
        kind="export",
        payload="{}",
    )
    s.add(j_bad_proj)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # owner_id 悬空 → IntegrityError
    j_bad_owner = StudioJob(
        id=_ulid(),
        project_id=p1.id,
        owner_id="nonexistent_uid",
        kind="export",
        payload="{}",
    )
    s.add(j_bad_owner)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t3_attempt_sequence(db):
    """(job_id, seq) 唯一约束 & status CHECK。"""
    s = Session()
    _make_user(s, "u1")
    p1 = _make_project(s, _ulid(), "u1")
    j1 = StudioJob(
        id=_ulid(),
        project_id=p1.id,
        owner_id="u1",
        kind="export",
        payload="{}",
    )
    s.add(j1)
    s.commit()

    a1 = JobAttempt(
        id=_ulid(),
        job_id=j1.id,
        seq=1,
        lease_owner="w1",
        started_at=time.time(),
        client_request_id=_ulid(),
        status="running",
    )
    s.add(a1)
    s.commit()

    a2 = JobAttempt(
        id=_ulid(),
        job_id=j1.id,
        seq=2,
        lease_owner="w1",
        started_at=time.time(),
        client_request_id=_ulid(),
        status="succeeded",
    )
    s.add(a2)
    s.commit()

    # 重复 seq=1 → IntegrityError
    a_dup = JobAttempt(
        id=_ulid(),
        job_id=j1.id,
        seq=1,
        lease_owner="w1",
        started_at=time.time(),
        client_request_id=_ulid(),
        status="running",
    )
    s.add(a_dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # status="indeterminate" → 成功
    a3 = JobAttempt(
        id=_ulid(),
        job_id=j1.id,
        seq=3,
        lease_owner="w1",
        started_at=time.time(),
        client_request_id=_ulid(),
        status="indeterminate",
    )
    s.add(a3)
    s.commit()

    # status="weird" → IntegrityError
    a_bad = JobAttempt(
        id=_ulid(),
        job_id=j1.id,
        seq=4,
        lease_owner="w1",
        started_at=time.time(),
        client_request_id=_ulid(),
        status="weird",
    )
    s.add(a_bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t4_event_types(db):
    """8 个合法 type 各插一条成功；非法 type → IntegrityError；列集合只追加形状。"""
    s = Session()
    _make_user(s, "u1")
    p1 = _make_project(s, _ulid(), "u1")
    j1 = StudioJob(
        id=_ulid(),
        project_id=p1.id,
        owner_id="u1",
        kind="export",
        payload="{}",
    )
    s.add(j1)
    s.commit()

    valid_types = [
        "created",
        "claimed",
        "progress",
        "provider_submitted",
        "succeeded",
        "failed",
        "cancelled",
        "indeterminate",
    ]
    for t in valid_types:
        ev = JobEvent(
            id=_ulid(),
            job_id=j1.id,
            type=t,
            at=time.time(),
            detail="{}",
        )
        s.add(ev)
        s.commit()

    # 非法 type → IntegrityError
    ev_bad = JobEvent(
        id=_ulid(),
        job_id=j1.id,
        type="weird",
        at=time.time(),
        detail="{}",
    )
    s.add(ev_bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 列集合 == {id, job_id, type, at, detail}（只追加的形状保证）
    col_names = {c.name for c in JobEvent.__table__.columns}
    assert col_names == {"id", "job_id", "type", "at", "detail"}, f"unexpected columns: {col_names}"
    s.close()


def test_t5_no_legacy_columns(db):
    """StudioJob 列名与旧概念字段交集为空；无 task/tasks 表名。"""
    col_names = {c.name for c in StudioJob.__table__.columns}
    legacy = {"stage", "node", "reader", "branch", "workflow"}
    assert not (col_names & legacy), f"legacy columns found: {col_names & legacy}"

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    assert "task" not in tables, f"task table found"
    assert "tasks" not in tables, f"tasks table found"


def test_t6_json_text_roundtrip(db):
    """JSON 列以 Text 存储：json.dumps 写入 → json.loads 读回相等。"""
    s = Session()
    _make_user(s, "u1")
    p1 = _make_project(s, _ulid(), "u1")

    payload_dict = {"a": 1, "b": "中文"}
    j1 = StudioJob(
        id=_ulid(),
        project_id=p1.id,
        owner_id="u1",
        kind="export",
        payload=json.dumps(payload_dict),
    )
    s.add(j1)
    s.commit()

    got = s.execute(select(StudioJob).where(StudioJob.id == j1.id)).scalar_one()
    assert json.loads(got.payload) == payload_dict

    # attempt.usage
    usage_dict = {"tokens": 123}
    a1 = JobAttempt(
        id=_ulid(),
        job_id=j1.id,
        seq=1,
        lease_owner="w1",
        started_at=time.time(),
        client_request_id=_ulid(),
        status="succeeded",
        usage=json.dumps(usage_dict),
    )
    s.add(a1)
    s.commit()

    got_a = s.execute(select(JobAttempt).where(JobAttempt.id == a1.id)).scalar_one()
    assert json.loads(got_a.usage) == usage_dict
    s.close()


def test_t7_source_audit(db):
    """models.py 源码不含禁串。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    f = root / "backend/studio/jobs/models.py"
    src = f.read_text(encoding="utf-8")
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
    for pat in forbidden:
        assert pat not in src, f"models.py contains forbidden string: {pat!r}"
