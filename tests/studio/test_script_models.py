"""DB-03 模型验证：studio_script_objects / studio_script_revisions / studio_script_adoptions 表结构与约束。

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

from backend.core.accounts import User  # noqa: F401  （注册 users 表，与既有模式一致）
from backend.core.db import Base, engine, Session
from backend.studio.projects.models import StudioProject
from backend.studio.sources.fragment_models import Fragment, FragmentRevision, RangeSet
from backend.studio.sources.models import SourceRevision
from backend.studio.scripts.models import ScriptAdoption, ScriptObject, ScriptRevision


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（objects↔revisions 等）：drop 前临时关 FK
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


def _make_base(s) -> tuple[StudioProject, SourceRevision, RangeSet, Fragment, FragmentRevision]:
    """建 user + project + SourceRevision + RangeSet + Fragment（先插，current_revision_id 占位）
    + FragmentRevision（回填），commit 后返回。照 test_fragment_models.py 的 T5 模式。"""
    uid = _ulid()
    s.execute(
        text(
            "INSERT INTO users (id, username, email, password_hash, auth_token, created)"
            " VALUES (:id, :uname, :email, :ph, :at, :cr)"
        ),
        {"id": uid, "uname": uid, "email": f"{uid}@t", "ph": "x", "at": "t", "cr": 0.0},
    )
    p = StudioProject(id=_ulid(), owner_id=uid, name="script-proj")
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
    rs = RangeSet(id=_ulid(), project_id=p.id, source_revision_id=rev.id)
    s.add(rs)
    s.commit()
    frag_id = _ulid()
    frag_rev_id = _ulid()
    frag = Fragment(
        id=frag_id,
        project_id=p.id,
        source_revision_id=rev.id,
        range_set_id=rs.id,
        name="script-frag",
        current_revision_id=frag_rev_id,
    )
    s.add(frag)
    frag_rev = FragmentRevision(
        id=frag_rev_id,
        fragment_id=frag_id,
        revision=1,
        source_revision_id=rev.id,
        range_start=0,
        range_end=3,
        reason="created",
    )
    s.add(frag_rev)
    s.commit()
    return p, rev, rs, frag, frag_rev


def test_t0_engine_url(db):
    """防御断言：engine URL 与 session 丢弃库（conftest 设定的 STUDIO_DATABASE_URL）一致。"""
    assert str(engine.url) == os.environ["STUDIO_DATABASE_URL"]


def test_t1_table_set(db):
    """session 丢弃库包含本卡三张表，且不出现旧系统表（records/tasks）。"""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    expected = {"studio_script_objects", "studio_script_revisions", "studio_script_adoptions"}
    assert expected <= tables, f"缺表: got {tables}, expected superset of {expected}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"


def test_t2_object_constraints(db):
    """(fragment_id, seq) 唯一 & kind CHECK & fragment_id FK。"""
    s = Session()
    p, rev, rs, frag, frag_rev = _make_base(s)

    # 同 fragment 插 (seq=1, kind=action) 与 (seq=2, kind=dialogue) 成功（同片段多对象可保存）
    o1_id, o1_rev_id = _ulid(), _ulid()
    s.add(ScriptObject(id=o1_id, project_id=p.id, fragment_id=frag.id, kind="action", seq=1, current_revision_id=o1_rev_id))
    s.add(ScriptRevision(id=o1_rev_id, object_id=o1_id, revision=1, text="a1", source_fragment_revision=1))
    o2_id, o2_rev_id = _ulid(), _ulid()
    s.add(ScriptObject(id=o2_id, project_id=p.id, fragment_id=frag.id, kind="dialogue", seq=2, current_revision_id=o2_rev_id))
    s.add(ScriptRevision(id=o2_rev_id, object_id=o2_id, revision=1, text="d1", speaker="Alice", source_fragment_revision=1))
    s.commit()
    got1 = s.execute(select(ScriptObject).where(ScriptObject.id == o1_id)).scalar_one()
    got2 = s.execute(select(ScriptObject).where(ScriptObject.id == o2_id)).scalar_one()
    assert got1.kind == "action" and got1.seq == 1
    assert got2.kind == "dialogue" and got2.seq == 2

    # 同 fragment 再插 (seq=1) → IntegrityError（UQ）
    o3 = ScriptObject(id=_ulid(), project_id=p.id, fragment_id=frag.id, kind="action", seq=1, current_revision_id=_ulid())
    s.add(o3)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # kind="narration" → IntegrityError（CHECK）
    o_bad = ScriptObject(
        id=_ulid(), project_id=p.id, fragment_id=frag.id, kind="narration", seq=3, current_revision_id=_ulid()
    )
    s.add(o_bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 fragment_id → IntegrityError（FK）
    o_dang = ScriptObject(
        id=_ulid(), project_id=p.id, fragment_id="nonexistent_frag", kind="action", seq=4, current_revision_id=_ulid()
    )
    s.add(o_dang)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t3_revision_constraints(db):
    """(object_id, revision) 唯一 & revision 序列 & object_id FK。"""
    s = Session()
    p, rev, rs, frag, frag_rev = _make_base(s)

    o_id, r1_id = _ulid(), _ulid()
    s.add(ScriptObject(id=o_id, project_id=p.id, fragment_id=frag.id, kind="action", seq=1, current_revision_id=r1_id))
    s.add(ScriptRevision(id=r1_id, object_id=o_id, revision=1, text="r1", source_fragment_revision=1))
    s.commit()

    # revision 序列 1、2 成功（revision=2 是新增独立行）
    r2_id = _ulid()
    s.add(ScriptRevision(id=r2_id, object_id=o_id, revision=2, text="r2", source_fragment_revision=1))
    s.commit()
    got2 = s.execute(select(ScriptRevision).where(ScriptRevision.id == r2_id)).scalar_one()
    assert got2.object_id == o_id and got2.revision == 2 and got2.text == "r2"

    # 同 object revision=1 两行 → IntegrityError（UQ）
    dup = ScriptRevision(id=_ulid(), object_id=o_id, revision=1, text="dup", source_fragment_revision=1)
    s.add(dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 object_id → IntegrityError（deferred FK 在 commit 校验）
    bad = ScriptRevision(id=_ulid(), object_id="nonexistent_obj", revision=1, text="x", source_fragment_revision=1)
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t4_fk_cycle_deferred(db):
    """FK 环（objects ↔ revisions）deferred：commit 时才校验。

    正向：同一事务内先插 ScriptObject(current_revision_id 占位) 再插 ScriptRevision，
    commit 成功且读回一致；
    对照：ScriptObject(current_revision_id='bogus') 不插对应 revision → commit IntegrityError。
    """
    s = Session()
    p, rev, rs, frag, frag_rev = _make_base(s)

    # 正向：占位 ULID 先行，对应 revision 后插
    o_id, r_id = _ulid(), _ulid()
    s.add(ScriptObject(id=o_id, project_id=p.id, fragment_id=frag.id, kind="action", seq=1, current_revision_id=r_id))
    s.add(ScriptRevision(id=r_id, object_id=o_id, revision=1, text="t", source_fragment_revision=1))
    s.commit()
    got_o = s.execute(select(ScriptObject).where(ScriptObject.id == o_id)).scalar_one()
    got_r = s.execute(select(ScriptRevision).where(ScriptRevision.id == r_id)).scalar_one()
    assert got_o.current_revision_id == r_id
    assert got_r.object_id == o_id
    assert got_r.text == "t" and got_r.source_fragment_revision == 1
    s.close()

    # 对照：指向不存在的 revision，commit 时 deferred FK 校验失败
    s2 = Session()
    p2, _, _, frag2, _ = _make_base(s2)
    s2.add(ScriptObject(id=_ulid(), project_id=p2.id, fragment_id=frag2.id, kind="dialogue", seq=1, current_revision_id="bogus"))
    with pytest.raises(IntegrityError):
        s2.commit()
    s2.rollback()
    s2.close()


def test_t5_adoption_chain(db):
    """采用链：previous_adoption_id 链式 & 链尾 = 最新 created_at & object_revisions JSON 往返 & 悬空 previous。"""
    s = Session()
    p, rev, rs, frag, frag_rev = _make_base(s)

    or_json = json.dumps([{"object_id": _ulid(), "revision": 1, "seq": 1, "kind": "action"}])
    t0 = time.time()
    a1_id = _ulid()
    s.add(
        ScriptAdoption(
            id=a1_id, project_id=p.id, fragment_id=frag.id, object_revisions=or_json,
            content_hash="a" * 64, previous_adoption_id=None, created_at=t0,
        )
    )
    a2_id = _ulid()
    s.add(
        ScriptAdoption(
            id=a2_id, project_id=p.id, fragment_id=frag.id, object_revisions=or_json,
            content_hash="b" * 64, previous_adoption_id=a1_id, created_at=t0 + 1.0,
        )
    )
    s.commit()

    # 读回：object_revisions JSON 往返相等
    got1 = s.execute(select(ScriptAdoption).where(ScriptAdoption.id == a1_id)).scalar_one()
    assert json.loads(got1.object_revisions) == json.loads(or_json)
    assert got1.previous_adoption_id is None

    # 链尾 = 该 fragment 最新 created_at 行
    tail = (
        s.execute(
            select(ScriptAdoption)
            .where(ScriptAdoption.fragment_id == frag.id)
            .order_by(ScriptAdoption.created_at.desc())
        )
        .scalars()
        .first()
    )
    assert tail.id == a2_id
    assert tail.previous_adoption_id == a1_id

    # 悬空 previous_adoption_id → IntegrityError（FK）
    a3 = ScriptAdoption(
        id=_ulid(), project_id=p.id, fragment_id=frag.id, object_revisions=or_json,
        content_hash="c" * 64, previous_adoption_id="nonexistent_adoption",
    )
    s.add(a3)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t6_no_stage_or_state_columns(db):
    """studio_script_objects 无 stage/status/state/node 列（对象无状态列：草稿/采用是两个事实）。"""
    col_names = {c.name for c in ScriptObject.__table__.columns}
    assert not ({"stage", "status", "state", "node"} & col_names), f"状态列 found: {col_names}"


def test_t7_source_audit(db):
    """scripts/models.py 源码不含禁串（...core.db 允许）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    src = (root / "backend/studio/scripts/models.py").read_text(encoding="utf-8")
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
