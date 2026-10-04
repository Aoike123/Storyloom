"""DB-04 模型验证：studio_scenes / studio_shots / studio_shot_revisions 表结构与约束。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件只做防御断言，不自行改写 STUDIO_* env。
"""
import os
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
from backend.studio.storyboard.models import Scene, Shot, ShotRevision


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（shots↔shot_revisions 等）：drop 前临时关 FK
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


def _make_base(s) -> tuple[User, StudioProject, Fragment, ScriptAdoption]:
    """建 user + project + SourceRevision + RangeSet + Fragment/FragmentRevision（deferred 环）
    + ScriptObject/ScriptRevision（deferred 环）+ ScriptAdoption（fragment 级），
    commit 后返回 (user, project, fragment, adoption)。照 test_script_models.py 的 _make_base 扩展。"""
    uid = _ulid()
    user = User(id=uid, username=uid, email=f"{uid}@t", password_hash="x", auth_token="t", created=0.0)
    s.add(user)
    s.commit()

    project = StudioProject(id=_ulid(), owner_id=uid, name="shot-proj")
    s.add(project)
    s.commit()

    src = SourceRevision(
        id=_ulid(),
        project_id=project.id,
        previous_revision_id=None,
        raw_content="hello\r\nworld",
        raw_hash="a" * 64,
        canonical_content="hello\nworld",
        canonical_hash="b" * 64,
        char_length=11,
    )
    s.add(src)
    s.commit()

    rs = RangeSet(id=_ulid(), project_id=project.id, source_revision_id=src.id)
    s.add(rs)
    s.commit()

    frag_id = _ulid()
    frag_rev_id = _ulid()
    fragment = Fragment(
        id=frag_id,
        project_id=project.id,
        source_revision_id=src.id,
        range_set_id=rs.id,
        name="shot-frag",
        current_revision_id=frag_rev_id,
    )
    s.add(fragment)
    s.add(
        FragmentRevision(
            id=frag_rev_id,
            fragment_id=frag_id,
            revision=1,
            source_revision_id=src.id,
            range_start=0,
            range_end=3,
            reason="created",
        )
    )
    s.commit()

    obj_id = _ulid()
    obj_rev_id = _ulid()
    s.add(
        ScriptObject(
            id=obj_id, project_id=project.id, fragment_id=frag_id, kind="action",
            seq=1, current_revision_id=obj_rev_id,
        )
    )
    s.add(
        ScriptRevision(
            id=obj_rev_id, object_id=obj_id, revision=1, text="a1", source_fragment_revision=1,
        )
    )
    s.commit()

    adoption = ScriptAdoption(
        id=_ulid(), project_id=project.id, fragment_id=frag_id,
        object_revisions="[]", content_hash="c" * 64, previous_adoption_id=None,
    )
    s.add(adoption)
    s.commit()

    return user, project, fragment, adoption


def _make_scene(s, project, seq=1, name="S1") -> Scene:
    """建一个场并 commit，返回 Scene。"""
    sc = Scene(id=_ulid(), project_id=project.id, name=name, seq=seq)
    s.add(sc)
    s.commit()
    return sc


def test_t0_engine_url(db):
    """防御断言：engine URL 与 session 丢弃库（conftest 设定的 STUDIO_DATABASE_URL）一致。"""
    assert str(engine.url) == os.environ["STUDIO_DATABASE_URL"]


def test_t1_table_set(db):
    """session 丢弃库包含本卡三张表，且不出现旧系统表（records/tasks）。"""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    expected = {"studio_scenes", "studio_shots", "studio_shot_revisions"}
    assert expected <= tables, f"缺表: got {tables}, expected superset of {expected}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"


def test_t2_scene_constraints(db):
    """(project_id, seq) 唯一 & layout_cas_revision 默认 1 & 悬空 project_id FK。"""
    s = Session()
    user, project, frag, adoption = _make_base(s)

    # scene(seq=1) 成功，layout_cas_revision 默认 1
    sc = Scene(id=_ulid(), project_id=project.id, name="S1", seq=1)
    s.add(sc)
    s.commit()
    got = s.execute(select(Scene).where(Scene.id == sc.id)).scalar_one()
    assert got.seq == 1
    assert got.layout_cas_revision == 1

    # 同 project seq=1 再插 → IntegrityError（UQ project_id, seq）
    dup = Scene(id=_ulid(), project_id=project.id, name="S1-dup", seq=1)
    s.add(dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 不同 project 同 seq=1 → 成功
    p2 = StudioProject(id=_ulid(), owner_id=user.id, name="shot-proj-2")
    s.add(p2)
    s.commit()
    sc2 = Scene(id=_ulid(), project_id=p2.id, name="S1-other", seq=1)
    s.add(sc2)
    s.commit()
    got2 = s.execute(select(Scene).where(Scene.id == sc2.id)).scalar_one()
    assert got2.project_id == p2.id and got2.seq == 1

    # 悬空 project_id → IntegrityError（FK）
    dang = Scene(id=_ulid(), project_id="nonexistent_pid", name="S-dang", seq=1)
    s.add(dang)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t3_shot_constraints(db):
    """(scene_id, seq) 唯一 & 悬空 scene_id / script_adoption_id FK（唯一父级）。"""
    s = Session()
    user, project, frag, adoption = _make_base(s)
    sc = _make_scene(s, project)

    # shot1 (scene seq=1) + rev1 → commit 成功（deferred 环闭合）
    s1, r1 = _ulid(), _ulid()
    s.add(Shot(id=s1, project_id=project.id, scene_id=sc.id, seq=1, name="sh1",
               script_adoption_id=adoption.id, current_revision_id=r1))
    s.add(ShotRevision(id=r1, shot_id=s1, revision=1, visual="v1", action="a1"))
    s.commit()

    # 同 scene seq=1 第二镜头 → IntegrityError（UQ scene_id, seq）
    s2, r2 = _ulid(), _ulid()
    s.add(Shot(id=s2, project_id=project.id, scene_id=sc.id, seq=1, name="sh2",
               script_adoption_id=adoption.id, current_revision_id=r2))
    s.add(ShotRevision(id=r2, shot_id=s2, revision=1, visual="v2", action="a2"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 scene_id → IntegrityError（FK，唯一父级）
    s3, r3 = _ulid(), _ulid()
    s.add(Shot(id=s3, project_id=project.id, scene_id="nonexistent_scene", seq=1, name="sh3",
               script_adoption_id=adoption.id, current_revision_id=r3))
    s.add(ShotRevision(id=r3, shot_id=s3, revision=1, visual="v3", action="a3"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 script_adoption_id → IntegrityError（FK）
    s4, r4 = _ulid(), _ulid()
    s.add(Shot(id=s4, project_id=project.id, scene_id=sc.id, seq=2, name="sh4",
               script_adoption_id="nonexistent_adoption", current_revision_id=r4))
    s.add(ShotRevision(id=r4, shot_id=s4, revision=1, visual="v4", action="a4"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t4_revision_constraints(db):
    """(shot_id, revision) 唯一 & revision 序列 & 悬空 shot_id FK。"""
    s = Session()
    user, project, frag, adoption = _make_base(s)
    sc = _make_scene(s, project)

    shot_id = _ulid()
    r1 = _ulid()
    s.add(Shot(id=shot_id, project_id=project.id, scene_id=sc.id, seq=1, name="sh1",
               script_adoption_id=adoption.id, current_revision_id=r1))
    s.add(ShotRevision(id=r1, shot_id=shot_id, revision=1, visual="v1", action="a1"))
    s.commit()

    # revision=2 成功（独立新行）
    r2 = _ulid()
    s.add(ShotRevision(id=r2, shot_id=shot_id, revision=2, visual="v2", action="a2"))
    s.commit()
    got2 = s.execute(select(ShotRevision).where(ShotRevision.id == r2)).scalar_one()
    assert got2.shot_id == shot_id and got2.revision == 2 and got2.visual == "v2"

    # 同 shot revision=1 两行 → IntegrityError（UQ shot_id, revision）
    dup = ShotRevision(id=_ulid(), shot_id=shot_id, revision=1, visual="dup", action="dup")
    s.add(dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 shot_id → IntegrityError（deferred FK 在 commit 校验）
    bad = ShotRevision(id=_ulid(), shot_id="nonexistent_shot", revision=1, visual="x", action="x")
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t5_fk_cycle_deferred(db):
    """FK 环（shots ↔ shot_revisions）deferred：commit 时才校验。

    正向：同一事务内先插 Shot(current_revision_id 占位) 再插 ShotRevision，
    commit 成功且读回一致；
    对照：Shot(current_revision_id='bogus') 不插对应 revision → commit IntegrityError。
    """
    s = Session()
    user, project, frag, adoption = _make_base(s)
    sc = _make_scene(s, project)

    # 正向：占位 ULID 先行，对应 revision 后插
    shot_id, rev_id = _ulid(), _ulid()
    s.add(Shot(id=shot_id, project_id=project.id, scene_id=sc.id, seq=1, name="sh1",
               script_adoption_id=adoption.id, current_revision_id=rev_id))
    s.add(ShotRevision(id=rev_id, shot_id=shot_id, revision=1, visual="v", action="a"))
    s.commit()
    got_s = s.execute(select(Shot).where(Shot.id == shot_id)).scalar_one()
    got_r = s.execute(select(ShotRevision).where(ShotRevision.id == rev_id)).scalar_one()
    assert got_s.current_revision_id == rev_id
    assert got_r.shot_id == shot_id
    assert got_r.revision == 1 and got_r.visual == "v" and got_r.action == "a"
    s.close()

    # 对照：指向不存在的 revision，commit 时 deferred FK 校验失败
    s2 = Session()
    user2, project2, frag2, adoption2 = _make_base(s2)
    sc2 = _make_scene(s2, project2)
    s2.add(Shot(id=_ulid(), project_id=project2.id, scene_id=sc2.id, seq=1, name="sh-bogus",
                script_adoption_id=adoption2.id, current_revision_id="bogus"))
    with pytest.raises(IntegrityError):
        s2.commit()
    s2.rollback()
    s2.close()


def test_t6_unique_parent_no_delete(db):
    """studio_shots 列恰为 9 列（唯一父级 + 无删除/退役语义列）。"""
    col_names = {c.name for c in Shot.__table__.columns}
    assert col_names == {
        "id", "project_id", "scene_id", "seq", "name",
        "script_adoption_id", "current_revision_id", "created_at", "updated_at",
    }, f"列不符: {col_names}"
    assert not ({"retired_at", "deleted_at", "removed_at"} & col_names), f"删除语义列 found: {col_names}"


def test_t7_source_audit(db):
    """storyboard/models.py 源码不含禁串（...core.db 允许）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    src = (root / "backend/studio/storyboard/models.py").read_text(encoding="utf-8")
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
