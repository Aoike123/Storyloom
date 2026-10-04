"""DB-09 模型验证：studio_conversations / studio_messages / studio_proposals 表结构与约束。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件只做防御断言，不自行改写 STUDIO_* env。
"""
import json
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.core.db import Base, engine, Session
from backend.core.accounts import User
from backend.studio.projects.models import StudioProject
from backend.studio.jobs.models import StudioJob
from backend.studio.conversations.models import Conversation, Message, Proposal


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


def _make_user(s, uid: str):
    """ORM insert of a minimal User row."""
    u = User(id=uid, username=uid, email=f"{uid}@t", password_hash="x", auth_token="t")
    s.add(u)
    s.commit()


def _make_project(s, pid: str, uid: str):
    """ORM insert of a minimal StudioProject row."""
    p = StudioProject(id=pid, owner_id=uid, name=f"proj_{pid}")
    s.add(p)
    s.commit()


def _make_conv(s, pid: str, uid: str, kind: str = "project", sid: str | None = None) -> str:
    """ORM insert of a minimal Conversation; returns its id."""
    if sid is None:
        sid = pid
    c = Conversation(id=_ulid(), project_id=pid, owner_id=uid, scope_kind=kind, scope_id=sid)
    s.add(c)
    s.commit()
    return c.id


# ---------------------------------------------------------------- T0


def test_t0_engine_url(db):
    """防御断言：engine URL 与 session 丢弃库（conftest 设定的 STUDIO_DATABASE_URL）一致。"""
    assert str(engine.url) == os.environ["STUDIO_DATABASE_URL"]


# ---------------------------------------------------------------- T1


def test_t1_table_set(db):
    """session 丢弃库包含本卡三张表，且不出现旧系统表（records/tasks）。"""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    expected = {"studio_conversations", "studio_messages", "studio_proposals"}
    assert expected <= tables, f"缺表: got {tables}, expected superset of {expected}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"


# ---------------------------------------------------------------- T2


def test_t2_conversation_isolation(db):
    """会话唯一约束 (project_id, scope_kind, scope_id, owner_id)。"""
    s = Session()
    _make_user(s, "u1")
    _make_user(s, "u2")
    pid = _ulid()
    _make_project(s, pid, "u1")

    # 第一个会话成功
    cid1 = _make_conv(s, pid, "u1", kind="project")

    # 同 (project, scope, owner) 第二个 → IntegrityError
    dup = Conversation(id=_ulid(), project_id=pid, owner_id="u1", scope_kind="project", scope_id=pid)
    s.add(dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 不同 scope_kind → 成功
    cid2 = _make_conv(s, pid, "u1", kind="scene", sid="scene1")

    # 不同 owner → 成功
    cid3 = _make_conv(s, pid, "u2", kind="project")

    # scope_kind="episode" → IntegrityError（不在 CHECK 白名单）
    bad = Conversation(id=_ulid(), project_id=pid, owner_id="u1", scope_kind="episode", scope_id="x")
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    s.close()


# ---------------------------------------------------------------- T3


def test_t3_message_constraints(db):
    """消息：sender CHECK、refs JSON round-trip、job_id FK。"""
    s = Session()
    _make_user(s, "u1")
    pid = _ulid()
    _make_project(s, pid, "u1")
    cid = _make_conv(s, pid, "u1")

    # sender="user" / "assistant" 成功
    m1 = Message(id=_ulid(), conversation_id=cid, sender="user", content="hello")
    s.add(m1)
    s.commit()

    m2 = Message(id=_ulid(), conversation_id=cid, sender="assistant", content="hi")
    s.add(m2)
    s.commit()

    # sender="system" → IntegrityError
    m_bad = Message(id=_ulid(), conversation_id=cid, sender="system", content="nope")
    s.add(m_bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # refs JSON round-trip
    refs_data = [{"kind": "fragment", "id": "x", "label": "L"}]
    m3 = Message(
        id=_ulid(),
        conversation_id=cid,
        sender="user",
        content="with refs",
        refs=json.dumps(refs_data),
    )
    s.add(m3)
    s.commit()
    row = s.execute(select(Message).where(Message.id == m3.id)).scalar_one()
    assert json.loads(row.refs) == refs_data

    # job_id 悬空 → IntegrityError
    m_orphan = Message(id=_ulid(), conversation_id=cid, sender="user", content="x", job_id="nonexistent00000000000000000")
    s.add(m_orphan)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # job_id 指向真实 StudioJob（kind='export' 合法）→ 成功
    job = StudioJob(
        id=_ulid(),
        project_id=pid,
        owner_id="u1",
        kind="export",
        payload="{}",
    )
    s.add(job)
    s.commit()
    m4 = Message(
        id=_ulid(),
        conversation_id=cid,
        sender="assistant",
        content="with job",
        job_id=job.id,
    )
    s.add(m4)
    s.commit()

    s.close()


# ---------------------------------------------------------------- T4


def test_t4_proposal_constraints(db):
    """提案：23 kind 白名单、status CHECK、FK 完整性、同消息多提案。"""
    s = Session()
    _make_user(s, "u1")
    pid = _ulid()
    _make_project(s, pid, "u1")
    cid = _make_conv(s, pid, "u1")
    mid = _ulid()
    m = Message(id=mid, conversation_id=cid, sender="assistant", content="proposal msg")
    s.add(m)
    s.commit()

    # 23 种 kind 全部合法
    kinds = [
        "source_import", "fragment_rename", "fragment_summary", "fragment_split",
        "fragment_merge", "fragment_boundary", "fragment_retire", "script_adopt",
        "scene_create", "shot_create", "shot_update", "shot_reorder", "shot_generate",
        "asset_create", "asset_new_revision", "asset_review", "asset_version_adopt",
        "binding_add", "binding_change", "edit_instance_add", "edit_instance_remove",
        "export", "release_publish",
    ]
    assert len(kinds) == 23
    for k in kinds:
        p = Proposal(
            id=_ulid(),
            conversation_id=cid,
            message_id=mid,
            kind=k,
            payload="{}",
        )
        s.add(p)
        s.commit()

    # kind="weird" → IntegrityError
    p_bad_kind = Proposal(
        id=_ulid(),
        conversation_id=cid,
        message_id=mid,
        kind="weird",
        payload="{}",
    )
    s.add(p_bad_kind)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # status 默认 "pending"
    p_default = Proposal(
        id=_ulid(),
        conversation_id=cid,
        message_id=mid,
        kind="export",
        payload="{}",
    )
    s.add(p_default)
    s.commit()
    assert p_default.status == "pending"

    # status="adopted" 成功
    p_adopted = Proposal(
        id=_ulid(),
        conversation_id=cid,
        message_id=mid,
        kind="export",
        payload="{}",
        status="adopted",
    )
    s.add(p_adopted)
    s.commit()

    # status="pending2" → IntegrityError
    p_bad_status = Proposal(
        id=_ulid(),
        conversation_id=cid,
        message_id=mid,
        kind="export",
        payload="{}",
        status="pending2",
    )
    s.add(p_bad_status)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 conversation_id → IntegrityError
    p_bad_conv = Proposal(
        id=_ulid(),
        conversation_id="nonexistent00000000000000000",
        message_id=mid,
        kind="export",
        payload="{}",
    )
    s.add(p_bad_conv)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 message_id → IntegrityError
    p_bad_msg = Proposal(
        id=_ulid(),
        conversation_id=cid,
        message_id="nonexistent00000000000000000",
        kind="export",
        payload="{}",
    )
    s.add(p_bad_msg)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 同消息挂两条提案成功（同 kind+目标仅一个 pending 是服务层规则，模型层允许并存）
    p_a = Proposal(id=_ulid(), conversation_id=cid, message_id=mid, kind="binding_add", payload="{}")
    p_b = Proposal(id=_ulid(), conversation_id=cid, message_id=mid, kind="binding_change", payload="{}")
    s.add(p_a)
    s.add(p_b)
    s.commit()

    s.close()


# ---------------------------------------------------------------- T5


def test_t5_append_only_shape(db):
    """只追加形状：Message / Proposal 列名集合精确匹配（无通用 updated_at 等）。"""
    msg_cols = {c.name for c in Message.__table__.columns}
    assert msg_cols == {
        "id", "conversation_id", "sender", "content", "refs", "job_id", "created_at",
    }

    prop_cols = {c.name for c in Proposal.__table__.columns}
    assert prop_cols == {
        "id", "conversation_id", "message_id", "kind", "payload",
        "status", "decision_note", "created_at", "decided_at",
    }


# ---------------------------------------------------------------- T6


def test_t6_no_legacy_fields(db):
    """三模型列名 ∩ {stage, node, reader, branch, thread} = ∅。"""
    legacy = {"stage", "node", "reader", "branch", "thread"}
    for model in (Conversation, Message, Proposal):
        cols = {c.name for c in model.__table__.columns}
        assert not (cols & legacy), f"{model.__name__} 含旧字段: {cols & legacy}"


# ---------------------------------------------------------------- T7


def test_t7_source_audit(db):
    """源码审计：models.py 不含禁串。"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "studio" / "conversations" / "models.py"
    ).read_text()
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
    for f in forbidden:
        assert f not in src, f"models.py 含禁串: {f}"
