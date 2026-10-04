"""DB-05 模型验证：studio_assets / studio_asset_revisions 表结构与约束。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件只做防御断言，不自行改写 STUDIO_* env。
"""
import json
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.core.accounts import User  # noqa: F401
from backend.core.db import Base, engine, Session
from backend.studio.assets.models import Asset, AssetRevision
from backend.studio.projects.models import StudioProject

# studio_media_artifacts 属 DB-10（尚未建表）：SQLite 不校验 DDL 时父表存在，
# FK 直接发射 REFERENCES 即可，不注册任何 stub（stub 会与 DB-10 真表同名冲突）。


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（assets↔asset_revisions）：drop 前临时关 FK
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


def _make_user(s, uid: str) -> str:
    """Raw-SQL insert of a minimal user row."""
    s.execute(
        text(
            "INSERT INTO users (id, username, email, password_hash, auth_token, created)"
            " VALUES (:id, :uname, :email, :ph, :at, :cr)"
        ),
        {"id": uid, "uname": uid, "email": f"{uid}@t", "ph": "x", "at": "t", "cr": 0.0},
    )
    s.commit()
    return uid


def _make_asset_with_rev(s, project_id, atype, name, character_id=None):
    """Insert an Asset + its revision-1, using the deferred FK cycle pattern.

    Returns (asset, revision) both committed.
    """
    asset_id = _ulid()
    rev_id = _ulid()
    asset = Asset(
        id=asset_id,
        project_id=project_id,
        type=atype,
        name=name,
        character_id=character_id,
        current_revision_id=rev_id,  # placeholder; FK deferred
    )
    s.add(asset)
    rev = AssetRevision(
        id=rev_id,
        asset_id=asset_id,
        revision=1,
        spec=json.dumps({"appearance": "a"}),
        media_artifact_id=None,
    )
    s.add(rev)
    s.commit()
    return asset, rev


def test_t0_engine_url(db):
    """防御断言：engine URL 与 session 丢弃库一致。"""
    assert str(engine.url) == os.environ["STUDIO_DATABASE_URL"]


def test_t1_table_set(db):
    """session 丢弃库包含本卡两张表，且不含旧系统表（records/tasks）。"""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    expected = {"studio_assets", "studio_asset_revisions"}
    assert expected <= tables, f"缺表: got {tables}, expected superset of {expected}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"


def test_t2_asset_constraints(db):
    """3 种 type 各插成功；type='prop' → IntegrityError；
    costume 的 character_id 指向 character 成功；悬空 character_id → IntegrityError。"""
    s = Session()
    uid = _make_user(s, "u1")
    p1 = StudioProject(id=_ulid(), owner_id=uid, name="AssetConstraints")
    s.add(p1)
    s.commit()

    # 3 种 type 各插成功（deferred 环：先 Asset(current=占位) 后 AssetRevision，commit）
    for t in ("character", "costume", "scene"):
        asset, rev = _make_asset_with_rev(s, p1.id, t, f"{t}-ok")
        assert asset.id is not None
        assert rev.revision == 1
    s.close()

    # 重新开 session 做错误用例
    s = Session()
    uid = _make_user(s, "u2")
    p2 = StudioProject(id=_ulid(), owner_id=uid, name="AssetBad")
    s.add(p2)
    s.commit()

    # type="prop" → IntegrityError (CHECK)
    bad = Asset(
        id=_ulid(),
        project_id=p2.id,
        type="prop",
        name="bad",
        character_id=None,
        current_revision_id=_ulid(),
    )
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 先建一个合法的 character
    char_id = _ulid()
    char_rev_id = _ulid()
    char = Asset(
        id=char_id,
        project_id=p2.id,
        type="character",
        name="ValidChar",
        character_id=None,
        current_revision_id=char_rev_id,
    )
    s.add(char)
    char_rev = AssetRevision(
        id=char_rev_id,
        asset_id=char_id,
        revision=1,
        spec=json.dumps({"appearance": "tall"}),
    )
    s.add(char_rev)
    s.commit()

    # costume 的 character_id 指向合法 character → 成功
    cost_id = _ulid()
    cost_rev_id = _ulid()
    cost = Asset(
        id=cost_id,
        project_id=p2.id,
        type="costume",
        name="ValidCostume",
        character_id=char_id,
        current_revision_id=cost_rev_id,
    )
    s.add(cost)
    cost_rev = AssetRevision(
        id=cost_rev_id,
        asset_id=cost_id,
        revision=1,
        spec=json.dumps({"description": "blue"}),
    )
    s.add(cost_rev)
    s.commit()
    assert cost.character_id == char_id

    # character_id 悬空 → IntegrityError (FK non-deferrable)
    dangling = Asset(
        id=_ulid(),
        project_id=p2.id,
        type="costume",
        name="Dangling",
        character_id="nonexistent_char_id",
        current_revision_id=_ulid(),
    )
    s.add(dangling)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t3_revision_constraints(db):
    """(asset_id, revision) 唯一；revision 1、2 并存；
    review_status 非法 → IntegrityError；默认 unreviewed；spec JSON 读回。"""
    s = Session()
    uid = _make_user(s, "u1")
    p1 = StudioProject(id=_ulid(), owner_id=uid, name="RevConstraints")
    s.add(p1)
    s.commit()

    # 建 character + rev1
    char_id = _ulid()
    rev1_id = _ulid()
    char = Asset(
        id=char_id,
        project_id=p1.id,
        type="character",
        name="RevTest",
        character_id=None,
        current_revision_id=rev1_id,
    )
    s.add(char)
    rev1 = AssetRevision(
        id=rev1_id,
        asset_id=char_id,
        revision=1,
        spec=json.dumps({"appearance": "v1"}),
    )
    s.add(rev1)
    s.commit()

    # rev2 并存
    rev2_id = _ulid()
    rev2 = AssetRevision(
        id=rev2_id,
        asset_id=char_id,
        revision=2,
        spec=json.dumps({"appearance": "v2"}),
    )
    s.add(rev2)
    char.current_revision_id = rev2_id
    s.commit()

    # 读回：两版并存
    revs = (
        s.execute(
            select(AssetRevision)
            .where(AssetRevision.asset_id == char_id)
            .order_by(AssetRevision.revision)
        )
        .scalars()
        .all()
    )
    assert [r.revision for r in revs] == [1, 2]

    # 默认 review_status = "unreviewed"
    assert rev1.review_status == "unreviewed"
    assert rev2.review_status == "unreviewed"

    # spec JSON 读回相等
    assert rev1.spec == json.dumps({"appearance": "v1"})

    # 同 asset revision=1 两行 → IntegrityError (UniqueConstraint)
    dup = AssetRevision(
        id=_ulid(),
        asset_id=char_id,
        revision=1,
        spec=json.dumps({"appearance": "dup"}),
    )
    s.add(dup)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # review_status="pending_ai" → IntegrityError (CHECK)
    bad = AssetRevision(
        id=_ulid(),
        asset_id=char_id,
        revision=3,
        spec=json.dumps({"appearance": "x"}),
        review_status="pending_ai",
    )
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t4_media_artifact_null(db):
    """media_artifact_id=None 插入成功（DB-10 表不存在于本 session；非空不在本卡范围）。"""
    s = Session()
    uid = _make_user(s, "u1")
    p1 = StudioProject(id=_ulid(), owner_id=uid, name="MediaNull")
    s.add(p1)
    s.commit()

    asset_id = _ulid()
    rev_id = _ulid()
    asset = Asset(
        id=asset_id,
        project_id=p1.id,
        type="scene",
        name="Scene",
        character_id=None,
        current_revision_id=rev_id,
    )
    s.add(asset)
    rev = AssetRevision(
        id=rev_id,
        asset_id=asset_id,
        revision=1,
        spec=json.dumps({"location": "park", "description": "sunny"}),
        media_artifact_id=None,
    )
    s.add(rev)
    s.commit()

    got = s.execute(select(AssetRevision).where(AssetRevision.id == rev_id)).scalar_one()
    assert got.media_artifact_id is None
    s.close()


def test_t5_no_fragment_id(db):
    """Asset 列名集合不含 'fragment_id'（身份不绑片段）。"""
    col_names = {c.name for c in Asset.__table__.columns}
    assert "fragment_id" not in col_names, f"fragment_id found in Asset columns: {col_names}"


def test_t6_review_single_fact(db):
    """AssetRevision 列名 ∩ {stage, node, ai_status, task_status, checked} = ∅。"""
    col_names = {c.name for c in AssetRevision.__table__.columns}
    forbidden = {"stage", "node", "ai_status", "task_status", "checked"}
    assert not (col_names & forbidden), f"forbidden columns found: {col_names & forbidden}"


def test_t7_source_audit(db):
    """models.py 源码不含禁串。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    f = root / "backend/studio/assets/models.py"
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
