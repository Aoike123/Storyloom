"""DB-06 模型验证：studio_source_relations / studio_asset_occurrences / studio_asset_bindings 表结构与约束。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件只做防御断言，不自行改写 STUDIO_* env。
"""
import json
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.core.accounts import User
from backend.core.db import Base, engine, Session
from backend.studio.assets.models import Asset, AssetRevision
from backend.studio.projects.models import StudioProject
from backend.studio.relations.models import AssetBinding, AssetOccurrence, SourceRelation
from backend.studio.scripts.models import ScriptAdoption, ScriptObject, ScriptRevision
from backend.studio.sources.fragment_models import Fragment, FragmentRevision, RangeSet
from backend.studio.sources.models import SourceRevision
from backend.studio.storyboard.models import Scene, Shot, ShotRevision


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（projects↔source_revisions 等）：drop 前临时关 FK
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


def _make_base(s):
    """创建全部前置行（user/project/source_rev/range_set/fragment+rev/
    script_object+rev/adoption/scene/shot+rev/asset+rev），返回所需 ID 字典。

    deferred FK 环（frag↔frag_rev、obj↔obj_rev、shot↔shot_rev、asset↔asset_rev）
    在同一事务内先后插入，commit 时校验。
    """
    uid = _ulid()
    s.execute(
        text(
            "INSERT INTO users (id, username, email, password_hash, auth_token, created)"
            " VALUES (:id, :uname, :email, :ph, :at, :cr)"
        ),
        {"id": uid, "uname": uid, "email": f"{uid}@t", "ph": "x", "at": "t", "cr": 0.0},
    )

    pid = _ulid()
    s.add(StudioProject(id=pid, owner_id=uid, name="Base"))
    s.commit()

    src_rev_id = _ulid()
    s.add(
        SourceRevision(
            id=src_rev_id,
            project_id=pid,
            previous_revision_id=None,
            raw_content="hello",
            raw_hash="a" * 64,
            canonical_content="hello",
            canonical_hash="b" * 64,
            char_length=5,
        )
    )
    s.commit()

    rs_id = _ulid()
    s.add(RangeSet(id=rs_id, project_id=pid, source_revision_id=src_rev_id))
    s.commit()

    # Fragment + FragmentRevision（deferred 环：同事务内先后插入，commit 时校验）
    frag_id = _ulid()
    frag_rev_id = _ulid()
    s.add(
        Fragment(
            id=frag_id,
            project_id=pid,
            source_revision_id=src_rev_id,
            range_set_id=rs_id,
            name="Frag1",
            state="candidate",
            current_revision_id=frag_rev_id,
        )
    )
    s.add(
        FragmentRevision(
            id=frag_rev_id,
            fragment_id=frag_id,
            revision=1,
            source_revision_id=src_rev_id,
            range_start=0,
            range_end=10,
            reason="created",
        )
    )
    s.commit()

    # ScriptObject + ScriptRevision（deferred 环）
    obj_id = _ulid()
    obj_rev_id = _ulid()
    s.add(
        ScriptObject(
            id=obj_id,
            project_id=pid,
            fragment_id=frag_id,
            kind="action",
            seq=1,
            current_revision_id=obj_rev_id,
        )
    )
    s.add(
        ScriptRevision(
            id=obj_rev_id,
            object_id=obj_id,
            revision=1,
            text="action text",
            source_fragment_revision=1,
        )
    )
    s.commit()

    adoption_id = _ulid()
    s.add(
        ScriptAdoption(
            id=adoption_id,
            project_id=pid,
            fragment_id=frag_id,
            object_revisions=json.dumps([obj_id]),
            content_hash="c" * 64,
        )
    )
    s.commit()

    scene_id = _ulid()
    s.add(Scene(id=scene_id, project_id=pid, name="Scene1", seq=1))
    s.commit()

    # Shot + ShotRevision（deferred 环）
    shot_id = _ulid()
    shot_rev_id = _ulid()
    s.add(
        Shot(
            id=shot_id,
            project_id=pid,
            scene_id=scene_id,
            seq=1,
            name="Shot1",
            script_adoption_id=adoption_id,
            current_revision_id=shot_rev_id,
        )
    )
    s.add(
        ShotRevision(
            id=shot_rev_id,
            shot_id=shot_id,
            revision=1,
            visual="v",
            action="a",
        )
    )
    s.commit()

    # Asset(character) + AssetRevision（deferred 环）
    asset_id = _ulid()
    asset_rev_id = _ulid()
    s.add(
        Asset(
            id=asset_id,
            project_id=pid,
            type="character",
            name="Char1",
            character_id=None,
            current_revision_id=asset_rev_id,
        )
    )
    s.add(
        AssetRevision(
            id=asset_rev_id,
            asset_id=asset_id,
            revision=1,
            spec="{}",
            review_status="unreviewed",
        )
    )
    s.commit()
    return {
        "user_id": uid,
        "project_id": pid,
        "source_revision_id": src_rev_id,
        "range_set_id": rs_id,
        "fragment_id": frag_id,
        "fragment_revision": 1,
        "script_object_id": obj_id,
        "script_adoption_id": adoption_id,
        "scene_id": scene_id,
        "shot_id": shot_id,
        "asset_id": asset_id,
    }


def test_t0_engine_url(db):
    """防御断言：engine URL 与 session 丢弃库一致。"""
    assert str(engine.url) == os.environ["STUDIO_DATABASE_URL"]


def test_t1_table_set(db):
    """session 丢弃库包含本卡三张表，且不出现旧系统表（records/tasks）。"""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    expected = {"studio_source_relations", "studio_asset_occurrences", "studio_asset_bindings"}
    assert expected <= tables, f"缺表: got {tables}, expected superset of {expected}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"


def test_t2_source_relations(db):
    """出处关系：CHECK target_kind、(fragment, kind, target) 唯一、悬空 FK。"""
    s = Session()
    ids = _make_base(s)
    pid, frag_id = ids["project_id"], ids["fragment_id"]

    # 1) 合法插入（target_kind=script_object）
    r1 = SourceRelation(
        id=_ulid(),
        project_id=pid,
        source_fragment_id=frag_id,
        source_fragment_revision=1,
        target_kind="script_object",
        target_id=ids["script_object_id"],
        target_revision=1,
    )
    s.add(r1)
    s.commit()

    # 2) target_kind="asset" → IntegrityError（CHECK）
    bad = SourceRelation(
        id=_ulid(),
        project_id=pid,
        source_fragment_id=frag_id,
        source_fragment_revision=1,
        target_kind="asset",
        target_id=ids["asset_id"],
        target_revision=1,
    )
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 3) 同 (fragment, kind, target) 重复 → IntegrityError（UNIQUE）
    dup = SourceRelation(
        id=_ulid(),
        project_id=pid,
        source_fragment_id=frag_id,
        source_fragment_revision=1,
        target_kind="script_object",
        target_id=ids["script_object_id"],
        target_revision=1,
    )
    s.add(dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 4) 不同 target_id → 成功
    r2 = SourceRelation(
        id=_ulid(),
        project_id=pid,
        source_fragment_id=frag_id,
        source_fragment_revision=1,
        target_kind="shot",
        target_id=ids["shot_id"],
        target_revision=1,
    )
    s.add(r2)
    s.commit()

    # 5) 悬空 fragment_id → IntegrityError（FK）
    dangling = SourceRelation(
        id=_ulid(),
        project_id=pid,
        source_fragment_id="nonexistent_frag",
        source_fragment_revision=1,
        target_kind="script_object",
        target_id=ids["script_object_id"],
        target_revision=1,
    )
    s.add(dangling)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t3_occurrences_null_bucket(db):
    """出现：NULL 桶唯一（coalesce 表达式索引）、不同 range 成功、重复 range 失败、悬空 FK。"""
    s = Session()
    ids = _make_base(s)
    pid, frag_id, asset_id = ids["project_id"], ids["fragment_id"], ids["asset_id"]

    # 1) 第一条 range 全 NULL → 成功
    o1 = AssetOccurrence(
        id=_ulid(),
        project_id=pid,
        asset_id=asset_id,
        asset_revision=1,
        source_fragment_id=frag_id,
        source_fragment_revision=1,
        range_start=None,
        range_end=None,
    )
    s.add(o1)
    s.commit()

    # 2) 第二条 range 全 NULL（同 asset+fragment）→ IntegrityError（NULL 桶唯一）
    o2 = AssetOccurrence(
        id=_ulid(),
        project_id=pid,
        asset_id=asset_id,
        asset_revision=1,
        source_fragment_id=frag_id,
        source_fragment_revision=1,
        range_start=None,
        range_end=None,
    )
    s.add(o2)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 3) (10,20) 成功
    o3 = AssetOccurrence(
        id=_ulid(),
        project_id=pid,
        asset_id=asset_id,
        asset_revision=1,
        source_fragment_id=frag_id,
        source_fragment_revision=1,
        range_start=10,
        range_end=20,
    )
    s.add(o3)
    s.commit()

    # 4) (30,40) 成功（不同 range）
    o4 = AssetOccurrence(
        id=_ulid(),
        project_id=pid,
        asset_id=asset_id,
        asset_revision=1,
        source_fragment_id=frag_id,
        source_fragment_revision=1,
        range_start=30,
        range_end=40,
    )
    s.add(o4)
    s.commit()

    # 5) 重复 (10,20) → IntegrityError
    o5 = AssetOccurrence(
        id=_ulid(),
        project_id=pid,
        asset_id=asset_id,
        asset_revision=1,
        source_fragment_id=frag_id,
        source_fragment_revision=1,
        range_start=10,
        range_end=20,
    )
    s.add(o5)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 6) 悬空 asset_id → IntegrityError
    o6 = AssetOccurrence(
        id=_ulid(),
        project_id=pid,
        asset_id="nonexistent_asset",
        asset_revision=1,
        source_fragment_id=frag_id,
        source_fragment_revision=1,
        range_start=10,
        range_end=20,
    )
    s.add(o6)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 7) 悬空 fragment_id → IntegrityError
    o7 = AssetOccurrence(
        id=_ulid(),
        project_id=pid,
        asset_id=asset_id,
        asset_revision=1,
        source_fragment_id="nonexistent_frag",
        source_fragment_revision=1,
        range_start=10,
        range_end=20,
    )
    s.add(o7)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t4_bindings(db):
    """绑定：purpose CHECK、(shot, asset, purpose, target_char) 唯一、悬空 FK。"""
    s = Session()
    ids = _make_base(s)
    pid, shot_id, asset_id = ids["project_id"], ids["shot_id"], ids["asset_id"]

    # 1) purpose 三种各插成功（同 shot+asset 不同 purpose）
    for purpose in ("visual", "background", "prop"):
        b = AssetBinding(
            id=_ulid(),
            project_id=pid,
            shot_id=shot_id,
            asset_id=asset_id,
            asset_revision=1,
            purpose=purpose,
            target_character_id=None,
        )
        s.add(b)
    s.commit()

    # 2) purpose="fx" → IntegrityError（CHECK）
    bad = AssetBinding(
        id=_ulid(),
        project_id=pid,
        shot_id=shot_id,
        asset_id=asset_id,
        asset_revision=1,
        purpose="fx",
        target_character_id=None,
    )
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 3) 同 (shot, asset, purpose) 重复 → IntegrityError
    #    上面已插 visual+NULL，再插一条 visual+NULL
    dup = AssetBinding(
        id=_ulid(),
        project_id=pid,
        shot_id=shot_id,
        asset_id=asset_id,
        asset_revision=1,
        purpose="visual",
        target_character_id=None,
    )
    s.add(dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 4) 两条 visual 绑定不同 target_character（NULL→'' vs 某有效 id）→ 成功
    b_char = AssetBinding(
        id=_ulid(),
        project_id=pid,
        shot_id=shot_id,
        asset_id=asset_id,
        asset_revision=1,
        purpose="visual",
        target_character_id=asset_id,  # 指向已存在的 character 资产
    )
    s.add(b_char)
    s.commit()

    # 5) 悬空 shot_id → IntegrityError
    d1 = AssetBinding(
        id=_ulid(),
        project_id=pid,
        shot_id="nonexistent_shot",
        asset_id=asset_id,
        asset_revision=1,
        purpose="visual",
        target_character_id=None,
    )
    s.add(d1)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 6) 悬空 asset_id → IntegrityError
    d2 = AssetBinding(
        id=_ulid(),
        project_id=pid,
        shot_id=shot_id,
        asset_id="nonexistent_asset",
        asset_revision=1,
        purpose="visual",
        target_character_id=None,
    )
    s.add(d2)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 7) 悬空 target_character_id → IntegrityError
    d3 = AssetBinding(
        id=_ulid(),
        project_id=pid,
        shot_id=shot_id,
        asset_id=asset_id,
        asset_revision=1,
        purpose="visual",
        target_character_id="nonexistent_char",
    )
    s.add(d3)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t5_fixed_version(db):
    """AssetBinding 列含 asset_revision 且 NOT NULL——版本固定于绑定。"""
    col = AssetBinding.__table__.c["asset_revision"]
    assert col is not None, "asset_revision 列不存在"
    assert col.nullable is False, f"asset_revision 应为 NOT NULL，got nullable={col.nullable}"


def test_t6_no_legacy_fields(db):
    """三模型列名 ∩ {stage, node, reader, branch, usage_count} = ∅。"""
    forbidden = {"stage", "node", "reader", "branch", "usage_count"}
    for model in (SourceRelation, AssetOccurrence, AssetBinding):
        col_names = {c.name for c in model.__table__.columns}
        overlap = col_names & forbidden
        assert not overlap, f"{model.__tablename__} 含旧字段: {overlap}"


def test_t7_source_audit(db):
    """models.py 不含禁串。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    f = root / "backend/studio/relations/models.py"
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
