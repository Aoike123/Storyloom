"""DB-07 模型验证：studio_change_previews / studio_review_decisions 表结构与约束。

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

from backend.core.db import Base, engine, Session
from backend.core.accounts import User
from backend.studio.projects.models import StudioProject
from backend.studio.reviews.models import ChangePreview, ReviewDecision


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（previews↔decisions）：drop 前临时关 FK
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
    """Raw-SQL insert of a minimal users row (all NOT NULL columns)."""
    s.execute(
        text(
            "INSERT INTO users (id, username, email, password_hash, auth_token, created)"
            " VALUES (:id, :uname, :email, :ph, :at, :cr)"
        ),
        {"id": uid, "uname": uid, "email": f"{uid}@t", "ph": "x", "at": "t", "cr": 0.0},
    )
    s.commit()


def _make_project(s, uid: str, pid: str):
    p = StudioProject(id=pid, owner_id=uid, name=f"proj-{pid[:8]}")
    s.add(p)
    s.commit()
    return p


# ---------------------------------------------------------------------------
# T0 防御
# ---------------------------------------------------------------------------

def test_t0_engine_url(db):
    """防御断言：engine URL 与 session 丢弃库（conftest 设定的 STUDIO_DATABASE_URL）一致。"""
    assert str(engine.url) == os.environ["STUDIO_DATABASE_URL"]


# ---------------------------------------------------------------------------
# T1 表集合
# ---------------------------------------------------------------------------

def test_t1_table_set(db):
    """session 丢弃库包含本卡两表，且不出现旧系统表（records/tasks）。"""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    expected = {"studio_change_previews", "studio_review_decisions"}
    assert expected <= tables, f"缺表: got {tables}, expected superset of {expected}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"


# ---------------------------------------------------------------------------
# T2 预览约束
# ---------------------------------------------------------------------------

_KINDS = [
    "source_activate",
    "fragment_retire",
    "fragment_boundary",
    "fragment_split",
    "fragment_merge",
    "script_adopt",
    "asset_version_adopt",
    "shot_generate",
    "asset_generate",
    "edit_confirm",
    "release_publish",
]


def test_t2_preview_constraints(db):
    """11 种 kind 各插成功；非法 kind/state → IntegrityError；悬空 FK → IntegrityError；双索引存在。"""
    s = Session()
    uid = _ulid()
    pid = _ulid()
    _make_user(s, uid)
    _make_project(s, uid, pid)

    # 11 种 kind 各插成功（expires_at 显式值）
    for i, kind in enumerate(_KINDS):
        prev = ChangePreview(
            id=_ulid(),
            project_id=pid,
            owner_id=uid,
            kind=kind,
            payload=json.dumps({"i": i}),
            baseline=json.dumps([{"kind": "src", "id": "x", "revision": 1}]),
            impact=json.dumps({"affected": [], "needs_review": [], "preservable": []}),
            expires_at=9999999.0,
        )
        s.add(prev)
        s.commit()

    # 非法 kind → IntegrityError
    bad_kind = ChangePreview(
        id=_ulid(),
        project_id=pid,
        owner_id=uid,
        kind="weird",
        payload="{}",
        baseline="[]",
        impact="{}",
        expires_at=9999999.0,
    )
    s.add(bad_kind)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # state 默认 "pending"
    p_default = ChangePreview(
        id=_ulid(),
        project_id=pid,
        owner_id=uid,
        kind="shot_generate",
        payload="{}",
        baseline="[]",
        impact="{}",
        expires_at=9999999.0,
    )
    s.add(p_default)
    s.commit()
    assert p_default.state == "pending"

    # 非法 state → IntegrityError
    bad_state = ChangePreview(
        id=_ulid(),
        project_id=pid,
        owner_id=uid,
        kind="shot_generate",
        payload="{}",
        baseline="[]",
        impact="{}",
        state="applied_twice",
        expires_at=9999999.0,
    )
    s.add(bad_state)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 project_id → IntegrityError
    dangling_proj = ChangePreview(
        id=_ulid(),
        project_id=_ulid(),
        owner_id=uid,
        kind="shot_generate",
        payload="{}",
        baseline="[]",
        impact="{}",
        expires_at=9999999.0,
    )
    s.add(dangling_proj)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 owner_id → IntegrityError
    dangling_owner = ChangePreview(
        id=_ulid(),
        project_id=pid,
        owner_id=_ulid(),
        kind="shot_generate",
        payload="{}",
        baseline="[]",
        impact="{}",
        expires_at=9999999.0,
    )
    s.add(dangling_owner)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 双索引名存在
    with engine.connect() as conn:
        idx_rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name IN "
                 "('ix_studio_change_previews_proj_state','ix_studio_change_previews_proj_kind')")
        ).fetchall()
    idx_names = {r[0] for r in idx_rows}
    assert "ix_studio_change_previews_proj_state" in idx_names, f"缺索引, got {idx_names}"
    assert "ix_studio_change_previews_proj_kind" in idx_names, f"缺索引, got {idx_names}"

    s.close()


# ---------------------------------------------------------------------------
# T3 可并存
# ---------------------------------------------------------------------------

def test_t3_previews_cannot_dedup(db):
    """同 project 两条同 kind 同 payload 的预览都成功（预览无唯一约束，过程记录）。"""
    s = Session()
    uid = _ulid()
    pid = _ulid()
    _make_user(s, uid)
    _make_project(s, uid, pid)

    payload = json.dumps({"x": 1})
    baseline = json.dumps([{"kind": "src", "id": "a", "revision": 1}])
    impact = json.dumps({"affected": [], "needs_review": [], "preservable": []})

    p1 = ChangePreview(
        id=_ulid(), project_id=pid, owner_id=uid, kind="shot_generate",
        payload=payload, baseline=baseline, impact=impact, expires_at=9999999.0,
    )
    p2 = ChangePreview(
        id=_ulid(), project_id=pid, owner_id=uid, kind="shot_generate",
        payload=payload, baseline=baseline, impact=impact, expires_at=9999999.0,
    )
    s.add(p1)
    s.add(p2)
    s.commit()
    assert p1.id != p2.id
    s.close()


# ---------------------------------------------------------------------------
# T4 决定约束
# ---------------------------------------------------------------------------

def test_t4_decision_constraints(db):
    """decision 合法值；preview_id UNIQUE；多 NULL 合法；悬空 FK 拒绝。"""
    s = Session()
    uid = _ulid()
    pid = _ulid()
    _make_user(s, uid)
    _make_project(s, uid, pid)

    prev = ChangePreview(
        id=_ulid(), project_id=pid, owner_id=uid, kind="shot_generate",
        payload="{}", baseline="[]", impact="{}", expires_at=9999999.0,
    )
    s.add(prev)
    s.commit()

    # decision="applied" 成功
    d1 = ReviewDecision(
        id=_ulid(), project_id=pid, owner_id=uid, preview_id=prev.id,
        target_ref=json.dumps({"kind": "asset", "id": "a1", "revision": 1}),
        decision="applied", baseline_digest="a" * 64,
    )
    s.add(d1)
    s.commit()

    # decision="confirmed" → IntegrityError（表值冻结 applied/rejected）
    d_bad = ReviewDecision(
        id=_ulid(), project_id=pid, owner_id=uid, preview_id=None,
        target_ref=json.dumps({"kind": "asset", "id": "a1", "revision": 2}),
        decision="confirmed", baseline_digest="b" * 64,
    )
    s.add(d_bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # preview_id UNIQUE：对同一 preview 插第二条决定 → IntegrityError
    d2 = ReviewDecision(
        id=_ulid(), project_id=pid, owner_id=uid, preview_id=prev.id,
        target_ref=json.dumps({"kind": "asset", "id": "a1", "revision": 1}),
        decision="rejected", baseline_digest="c" * 64,
    )
    s.add(d2)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 多条 preview_id=NULL 的决定成功（SQLite 多 NULL 合法）
    d_null1 = ReviewDecision(
        id=_ulid(), project_id=pid, owner_id=uid, preview_id=None,
        target_ref=json.dumps({"kind": "asset", "id": "b1", "revision": 1}),
        decision="applied", baseline_digest="d" * 64,
    )
    d_null2 = ReviewDecision(
        id=_ulid(), project_id=pid, owner_id=uid, preview_id=None,
        target_ref=json.dumps({"kind": "asset", "id": "b2", "revision": 1}),
        decision="rejected", baseline_digest="e" * 64,
    )
    s.add(d_null1)
    s.add(d_null2)
    s.commit()

    # 悬空 preview_id → IntegrityError
    d_dangling = ReviewDecision(
        id=_ulid(), project_id=pid, owner_id=uid, preview_id=_ulid(),
        target_ref="{}", decision="applied", baseline_digest="f" * 64,
    )
    s.add(d_dangling)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 project_id → IntegrityError
    d_bad_proj = ReviewDecision(
        id=_ulid(), project_id=_ulid(), owner_id=uid, preview_id=None,
        target_ref="{}", decision="applied", baseline_digest="g" * 64,
    )
    s.add(d_bad_proj)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 owner_id → IntegrityError
    d_bad_owner = ReviewDecision(
        id=_ulid(), project_id=pid, owner_id=_ulid(), preview_id=None,
        target_ref="{}", decision="applied", baseline_digest="h" * 64,
    )
    s.add(d_bad_owner)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    s.close()


# ---------------------------------------------------------------------------
# T5 回填流程（可变过程记录）
# ---------------------------------------------------------------------------

def test_t5_backfill_flow(db):
    """插 preview → 插 decision → 更新 preview 三字段 → commit 成功，读回一致。"""
    s = Session()
    uid = _ulid()
    pid = _ulid()
    _make_user(s, uid)
    _make_project(s, uid, pid)

    prev = ChangePreview(
        id=_ulid(), project_id=pid, owner_id=uid, kind="asset_generate",
        payload=json.dumps({"asset_id": "a1"}),
        baseline=json.dumps([{"kind": "asset", "id": "a1", "revision": 0}]),
        impact=json.dumps({"affected": ["a1"], "needs_review": [], "preservable": []}),
        state="pending",
        decision_id=None,
        resolved_at=None,
        expires_at=9999999.0,
    )
    s.add(prev)
    s.commit()

    digest = "0" * 64
    dec = ReviewDecision(
        id=_ulid(), project_id=pid, owner_id=uid, preview_id=prev.id,
        target_ref=json.dumps({"kind": "asset", "id": "a1", "revision": 1}),
        decision="applied", baseline_digest=digest,
    )
    s.add(dec)
    s.commit()

    # 回填 preview
    now = time.time()
    prev.state = "applied"
    prev.decision_id = dec.id
    prev.resolved_at = now
    s.commit()

    # 读回三值一致
    with Session() as s2:
        loaded = s2.execute(
            select(ChangePreview).where(ChangePreview.id == prev.id)
        ).scalar_one()
        assert loaded.state == "applied"
        assert loaded.decision_id == dec.id
        assert loaded.resolved_at == now

    s.close()


# ---------------------------------------------------------------------------
# T6 baseline_digest 绑定
# ---------------------------------------------------------------------------

def test_t6_baseline_digest_binding(db):
    """两条不同 baseline_digest 的决定（同 target_ref、preview_id=NULL）均可存在。"""
    s = Session()
    uid = _ulid()
    pid = _ulid()
    _make_user(s, uid)
    _make_project(s, uid, pid)

    target_ref = json.dumps({"kind": "asset", "id": "a1", "revision": 2})

    d1 = ReviewDecision(
        id=_ulid(), project_id=pid, owner_id=uid, preview_id=None,
        target_ref=target_ref, decision="applied", baseline_digest="1" * 64,
    )
    d2 = ReviewDecision(
        id=_ulid(), project_id=pid, owner_id=uid, preview_id=None,
        target_ref=target_ref, decision="rejected", baseline_digest="2" * 64,
    )
    s.add(d1)
    s.add(d2)
    s.commit()

    # 读回确认两行都在
    with Session() as s2:
        rows = s2.execute(
            select(ReviewDecision).where(
                ReviewDecision.project_id == pid,
                ReviewDecision.preview_id.is_(None),
            )
        ).scalars().all()
    digests = {r.baseline_digest for r in rows}
    assert "1" * 64 in digests
    assert "2" * 64 in digests
    s.close()


# ---------------------------------------------------------------------------
# T7 源码审计
# ---------------------------------------------------------------------------

def test_t7_source_audit():
    """models.py 不含禁串。"""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "backend" / "studio" / "reviews" / "models.py"
    content = src.read_text(encoding="utf-8")

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
        assert f not in content, f"models.py 含禁串: {f!r}"
