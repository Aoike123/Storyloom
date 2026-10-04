"""DB-02 模型验证：studio_range_sets / studio_fragments / studio_fragment_revisions 表结构与约束。

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
from backend.core.accounts import User  # noqa: F401  （注册 users 表，与既有模式一致）
from backend.studio.projects.models import StudioProject
from backend.studio.sources.models import SourceRevision
from backend.studio.sources.fragment_models import RangeSet, Fragment, FragmentRevision


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（fragments ↔ fragment_revisions 等）：drop 前临时关 FK
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


def _make_base(s) -> tuple[StudioProject, SourceRevision]:
    """Raw-SQL 建一个 user + ORM 建 project + SourceRevision，commit 后返回。"""
    uid = _ulid()
    s.execute(
        text(
            "INSERT INTO users (id, username, email, password_hash, auth_token, created)"
            " VALUES (:id, :uname, :email, :ph, :at, :cr)"
        ),
        {"id": uid, "uname": uid, "email": f"{uid}@t", "ph": "x", "at": "t", "cr": 0.0},
    )
    p = StudioProject(id=_ulid(), owner_id=uid, name="frag-proj")
    s.add(p)
    s.commit()
    rev = SourceRevision(
        id=_ulid(),
        project_id=p.id,
        previous_revision_id=None,
        raw_content="hello\r\nworld",
        raw_hash="a" * 64,
        canonical_content="hello\nworld",
        canonical_hash="b" * 64,
        char_length=11,
    )
    s.add(rev)
    s.commit()
    return p, rev


def test_t0_engine_url(db):
    """防御断言：engine URL 与 session 丢弃库（conftest 设定的 STUDIO_DATABASE_URL）一致。"""
    assert str(engine.url) == os.environ["STUDIO_DATABASE_URL"]


def test_t1_table_set(db):
    """session 丢弃库包含本卡三张表 + ProductionScope 表，且不出现旧系统表（records/tasks）。

    session 级 Base 注册全部模型（conftest + 各测试模块 import），表集合为其并集，
    隔离性断言 = 期望表存在 ∧ 无旧表（不依赖各文件 import 顺序）。
    """
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    expected = {
        "studio_range_sets",
        "studio_fragments",
        "studio_fragment_revisions",
        "studio_production_scopes",
    }
    assert expected <= tables, f"缺表: got {tables}, expected superset of {expected}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"


def test_t2_range_set(db):
    """(project_id, source_revision_id) 唯一 & cas_revision 默认 1 & 悬空 FK。"""
    s = Session()
    p, rev = _make_base(s)

    # 插入成功，cas_revision 默认 1
    rs = RangeSet(id=_ulid(), project_id=p.id, source_revision_id=rev.id)
    s.add(rs)
    s.commit()
    got = s.execute(select(RangeSet).where(RangeSet.id == rs.id)).scalar_one()
    assert got.cas_revision == 1

    # 同 (project, source_revision) 再插 → IntegrityError
    rs_dup = RangeSet(id=_ulid(), project_id=p.id, source_revision_id=rev.id)
    s.add(rs_dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 source_revision_id → IntegrityError
    rs_bad = RangeSet(id=_ulid(), project_id=p.id, source_revision_id="nonexistent_rev")
    s.add(rs_bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t3_fragment_state(db):
    """state 默认 candidate；confirmed 成功；pending_review（派生值）不落库；悬空 range_set_id。"""
    s = Session()
    p, rev = _make_base(s)
    rs = RangeSet(id=_ulid(), project_id=p.id, source_revision_id=rev.id)
    s.add(rs)
    s.commit()

    # 默认 state="candidate"（flush 后 ORM 默认值落地；deferred FK 不阻塞 flush）
    frag = Fragment(
        id=_ulid(),
        project_id=p.id,
        source_revision_id=rev.id,
        range_set_id=rs.id,
        name="frag-a",
        current_revision_id=_ulid(),
    )
    s.add(frag)
    s.flush()
    assert frag.state == "candidate"
    s.rollback()

    # state="confirmed" + 对应 revision → commit 成功
    frag_id = _ulid()
    frag2 = Fragment(
        id=frag_id,
        project_id=p.id,
        source_revision_id=rev.id,
        range_set_id=rs.id,
        name="frag-b",
        state="confirmed",
        current_revision_id=_ulid(),
    )
    s.add(frag2)
    s.flush()
    rev2 = FragmentRevision(
        id=frag2.current_revision_id,
        fragment_id=frag_id,
        revision=1,
        source_revision_id=rev.id,
        range_start=0,
        range_end=3,
        reason="created",
    )
    s.add(rev2)
    s.commit()

    # state="pending_review" → IntegrityError（派生值不落库）
    frag_bad = Fragment(
        id=_ulid(),
        project_id=p.id,
        source_revision_id=rev.id,
        range_set_id=rs.id,
        name="frag-c",
        state="pending_review",
        current_revision_id=_ulid(),
    )
    s.add(frag_bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 range_set_id → IntegrityError
    frag_dang = Fragment(
        id=_ulid(),
        project_id=p.id,
        source_revision_id=rev.id,
        range_set_id="nonexistent_rs",
        name="frag-d",
        current_revision_id=_ulid(),
    )
    s.add(frag_dang)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t4_revision_constraints(db):
    """(fragment_id, revision) 唯一 & CHECK end>start & reason 枚举 & predecessor 默认。"""
    s = Session()
    p, rev = _make_base(s)
    rs = RangeSet(id=_ulid(), project_id=p.id, source_revision_id=rev.id)
    s.add(rs)
    s.commit()

    frag_id = _ulid()
    rev_id = _ulid()
    frag = Fragment(
        id=frag_id,
        project_id=p.id,
        source_revision_id=rev.id,
        range_set_id=rs.id,
        name="frag-a",
        current_revision_id=rev_id,
    )
    s.add(frag)
    rev1 = FragmentRevision(
        id=rev_id,
        fragment_id=frag_id,
        revision=1,
        source_revision_id=rev.id,
        range_start=1,
        range_end=5,
        reason="created",
    )
    s.add(rev1)
    s.commit()

    # 读回：predecessor_fragment_ids 默认 "[]"（JSON 数组）
    got = s.execute(select(FragmentRevision).where(FragmentRevision.id == rev1.id)).scalar_one()
    assert json.loads(got.predecessor_fragment_ids) == []

    # 同 fragment revision=1 两行 → IntegrityError（UQ）
    dup = FragmentRevision(
        id=_ulid(),
        fragment_id=frag_id,
        revision=1,
        source_revision_id=rev.id,
        range_start=2,
        range_end=4,
        reason="boundary",
    )
    s.add(dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # range_end == range_start → IntegrityError（CHECK end>start）
    eq_rev = FragmentRevision(
        id=_ulid(),
        fragment_id=frag_id,
        revision=2,
        source_revision_id=rev.id,
        range_start=3,
        range_end=3,
        reason="boundary",
    )
    s.add(eq_rev)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # range_end < range_start → IntegrityError
    bad_rev = FragmentRevision(
        id=_ulid(),
        fragment_id=frag_id,
        revision=2,
        source_revision_id=rev.id,
        range_start=5,
        range_end=2,
        reason="boundary",
    )
    s.add(bad_rev)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # reason="weird" → IntegrityError
    weird = FragmentRevision(
        id=_ulid(),
        fragment_id=frag_id,
        revision=2,
        source_revision_id=rev.id,
        range_start=1,
        range_end=4,
        reason="weird",
    )
    s.add(weird)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # created_at 为 Float epoch（默认 time.time 生效）
    assert got.created_at > 0.0
    assert got.created_at <= time.time()
    s.close()


def test_t5_fk_cycle_deferred(db):
    """FK 环（fragments ↔ fragment_revisions）deferred：commit 时才校验。

    正向：同一事务内先插 Fragment(current_revision_id 占位) 再插 FragmentRevision，
    commit 成功且读回一致（PRAGMA foreign_keys=ON 下，deferrable 约束延迟到 commit）；
    对照：Fragment(current_revision_id='bogus') 不插对应 revision → commit IntegrityError。
    """
    s = Session()
    p, rev = _make_base(s)
    rs = RangeSet(id=_ulid(), project_id=p.id, source_revision_id=rev.id)
    s.add(rs)
    s.commit()

    # 正向：占位 ULID 先行，对应 revision 后插
    frag_id = _ulid()
    rev_id = _ulid()
    frag = Fragment(
        id=frag_id,
        project_id=p.id,
        source_revision_id=rev.id,
        range_set_id=rs.id,
        name="loop-frag",
        current_revision_id=rev_id,
    )
    s.add(frag)
    revr = FragmentRevision(
        id=rev_id,
        fragment_id=frag_id,
        revision=1,
        source_revision_id=rev.id,
        range_start=1,
        range_end=5,
        reason="created",
    )
    s.add(revr)
    s.commit()

    # 读回一致
    got_f = s.execute(select(Fragment).where(Fragment.id == frag_id)).scalar_one()
    got_r = s.execute(select(FragmentRevision).where(FragmentRevision.id == rev_id)).scalar_one()
    assert got_f.current_revision_id == rev_id
    assert got_r.fragment_id == frag_id
    s.close()

    # 对照：指向不存在的 revision，commit 时 deferred FK 校验失败
    s2 = Session()
    bad = Fragment(
        id=_ulid(),
        project_id=p.id,
        source_revision_id=rev.id,
        range_set_id=rs.id,
        name="loop-frag-bad",
        current_revision_id="bogus",
    )
    s2.add(bad)
    with pytest.raises(IntegrityError):
        s2.commit()
    s2.rollback()
    s2.close()


def test_t6_indexes(db):
    """studio_fragments 的两个索引存在。"""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).fetchall()
    indexes = {r[0] for r in rows}
    for name in ("ix_studio_fragments_proj_rev_state", "ix_studio_fragments_range_set"):
        assert name in indexes, f"缺索引 {name}: got {indexes}"


def test_t7_source_audit(db):
    """fragment_models.py 源码不含禁串（...core.db 允许）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    src = (root / "backend/studio/sources/fragment_models.py").read_text(encoding="utf-8")
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
        assert pat not in src, f"fragment_models.py contains forbidden string: {pat!r}"
