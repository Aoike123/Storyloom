"""DB-10 模型验证：五表——studio_media_artifacts /
studio_edit_drafts / studio_edit_instances / studio_confirmed_edits /
studio_releases（条件唯一 + 版本链）的结构与约束。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件只做防御断言，不自行改写 STUDIO_* env。

五表声明类：MediaArtifact（media 模块）、EditDraft/EditInstance/ConfirmedEdit
（edits 模块，ConfirmedEdit 为 DB-10 验收时主模型补入）、Release（publishing
模块）；conftest 注册表已 import 全部模型模块，本文件不注册任何占位表。
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
from backend.studio.jobs.models import StudioJob
from backend.studio.media.models import MediaArtifact
from backend.studio.edits.models import EditDraft, EditInstance, ConfirmedEdit
from backend.studio.publishing.models import Release


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


def _make_user(s, uid) -> None:
    """Raw-SQL insert of a minimal users row (all NOT NULL columns)."""
    s.execute(
        text(
            "INSERT INTO users (id, username, email, password_hash, auth_token, created)"
            " VALUES (:id, :uname, :email, :ph, :at, :cr)"
        ),
        {"id": uid, "uname": uid, "email": f"{uid}@t", "ph": "x", "at": "t", "cr": 0.0},
    )
    s.commit()


def _make_project(s, uid, pid) -> None:
    s.add(StudioProject(id=pid, owner_id=uid, name=f"proj-{pid}"))
    s.commit()


def _make_job(s, uid, pid, jid, kind) -> None:
    s.add(StudioJob(id=jid, project_id=pid, owner_id=uid, kind=kind, payload="{}"))
    s.commit()


def _insert_confirmed(s, eid, rev) -> str:
    """Raw-SQL 插入 studio_confirmed_edits 一行（不 commit，由调用方控制事务）。

    返回行 id：studio_releases.confirmed_edit_id 的 FK 指向 studio_confirmed_edits.id
    （行 PK，非 edit_id 列）。
    """
    cid = _ulid()
    s.execute(
        text(
            "INSERT INTO studio_confirmed_edits"
            " (id, edit_id, edit_revision, manifest, manifest_digest, output_spec, created_at)"
            " VALUES (:id, :eid, :rev, :mf, :md, :os, :cr)"
        ),
        {
            "id": cid,
            "eid": eid,
            "rev": rev,
            "mf": json.dumps({"version": 1, "edit_id": eid}),
            "md": "a" * 64,
            "os": json.dumps({"format": "mp4"}),
            "cr": time.time(),
        },
    )
    return cid


def test_t0_engine_url(db):
    """防御断言：engine URL 与 session 丢弃库（conftest 设定的 STUDIO_DATABASE_URL）一致。"""
    assert str(engine.url) == os.environ["STUDIO_DATABASE_URL"]


def test_t1_media_full_columns_and_five_tables(db):
    """MediaArtifact 声明类为全 12 列（DB-05 引入的 conftest 最小占位
    已在 DB-10 验收时移除，见 conftest 注释）；session 库五表齐备且无旧表。"""
    expected_cols = {
        "id", "project_id", "media_kind", "filename", "size_bytes", "sha256",
        "duration_ms", "width", "height", "source", "source_job_id", "created_at",
    }
    assert set(MediaArtifact.__table__.columns.keys()) == expected_cols

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    tables = {r[0] for r in rows}
    expected = {
        "studio_media_artifacts", "studio_edit_drafts", "studio_edit_instances",
        "studio_confirmed_edits", "studio_releases",
    }
    assert expected <= tables, f"缺表: got {tables}, expected superset of {expected}"
    assert not (tables & {"records", "tasks"}), f"出现旧系统表: {tables}"

    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(studio_releases)")).fetchall()
    release_cols = {r[1] for r in rows}
    assert len(release_cols) == 12
    assert "predecessor_release_id" in release_cols


def test_t2_media_artifacts(db):
    """3 kind + 5 source 全值合法；非法值 / 悬空 job → IntegrityError；image 无 duration。"""
    s = Session()
    _make_user(s, "u1")
    p = _ulid()
    _make_project(s, "u1", p)

    # 3 kind × 5 source 全值组合合法可插
    combos = [
        ("image", "upload"),
        ("image", "asset_generate"),
        ("video", "shot_generate"),
        ("video", "vendor_normalized"),
        ("audio", "export"),
    ]
    for kind, source in combos:
        s.add(
            MediaArtifact(
                id=_ulid(), project_id=p, media_kind=kind, filename=f"{kind}.dat",
                size_bytes=10, sha256="0" * 64, duration_ms=None, width=None, height=None,
                source=source, source_job_id=None,
            )
        )
    s.commit()

    # media_kind 非法 → IntegrityError
    s.add(MediaArtifact(id=_ulid(), project_id=p, media_kind="text", filename="t",
                        size_bytes=1, sha256="0" * 64, source="upload"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # source 非法 → IntegrityError
    s.add(MediaArtifact(id=_ulid(), project_id=p, media_kind="image", filename="t",
                        size_bytes=1, sha256="0" * 64, source="weird"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # image 行 duration_ms=None 成功（可空）
    m_img = MediaArtifact(id=_ulid(), project_id=p, media_kind="image", filename="img.png",
                          size_bytes=123, sha256="1" * 64, duration_ms=None,
                          width=1920, height=1080, source="upload")
    s.add(m_img)
    s.commit()
    got = s.execute(select(MediaArtifact).where(MediaArtifact.id == m_img.id)).scalar_one()
    assert got.duration_ms is None

    # source_job_id 指向真实 StudioJob(kind='export') 成功
    jid = _ulid()
    _make_job(s, "u1", p, jid, "export")
    m_job = MediaArtifact(id=_ulid(), project_id=p, media_kind="video", filename="v.mp4",
                          size_bytes=999, sha256="2" * 64, duration_ms=1500,
                          source="export", source_job_id=jid)
    s.add(m_job)
    s.commit()
    gotj = s.execute(select(MediaArtifact).where(MediaArtifact.id == m_job.id)).scalar_one()
    assert gotj.source_job_id == jid

    # source_job_id 悬空 → IntegrityError
    s.add(MediaArtifact(id=_ulid(), project_id=p, media_kind="video", filename="v2.mp4",
                        size_bytes=1, sha256="3" * 64, source="export",
                        source_job_id="nonexistent_jid"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t3_edit_drafts(db):
    """版本链：同 edit_id rev 1/2 成功；重复 rev / 悬空 project → IntegrityError；cas 默认 1。"""
    s = Session()
    _make_user(s, "u1")
    p = _ulid()
    _make_project(s, "u1", p)

    eid = _ulid()
    # 同 edit_id rev 1、2 → 两条成功（新编辑版本）
    d1 = EditDraft(id=_ulid(), project_id=p, edit_id=eid, edit_revision=1,
                   manifest=json.dumps({"version": 1}), manifest_digest="a" * 64)
    s.add(d1)
    s.commit()
    d2 = EditDraft(id=_ulid(), project_id=p, edit_id=eid, edit_revision=2,
                   manifest=json.dumps({"version": 2}), manifest_digest="b" * 64)
    s.add(d2)
    s.commit()

    # cas_revision 默认 1
    got = s.execute(select(EditDraft).where(EditDraft.id == d1.id)).scalar_one()
    assert got.cas_revision == 1

    # 重复 rev 1 → IntegrityError
    s.add(EditDraft(id=_ulid(), project_id=p, edit_id=eid, edit_revision=1,
                    manifest="{}", manifest_digest="c" * 64))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 project_id → IntegrityError
    s.add(EditDraft(id=_ulid(), project_id="nonexistent_pid", edit_id=_ulid(),
                    edit_revision=1, manifest="{}", manifest_digest="d" * 64))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t4_edit_instances(db):
    """实例显式行：同 media 不同 instance_id 成功；重复 (draft, instance) / 悬空 FK → IntegrityError。"""
    s = Session()
    _make_user(s, "u1")
    p = _ulid()
    _make_project(s, "u1", p)
    mid = _ulid()
    s.add(MediaArtifact(id=mid, project_id=p, media_kind="video", filename="m.mp4",
                        size_bytes=1, sha256="0" * 64, duration_ms=1000, source="upload"))
    s.commit()
    eid = _ulid()
    d1 = EditDraft(id=_ulid(), project_id=p, edit_id=eid, edit_revision=1,
                   manifest="{}", manifest_digest="a" * 64)
    s.add(d1)
    s.commit()

    # 同 media 两个不同 instance_id → 成功（重复 clip 使用 = 独立实例）
    i1 = EditInstance(id=_ulid(), draft_id=d1.id, instance_id=_ulid(), media_id=mid,
                      source_in_ms=0, source_out_ms=100, speed=1.0, timeline_start_ms=0)
    s.add(i1)
    s.commit()
    i2 = EditInstance(id=_ulid(), draft_id=d1.id, instance_id=_ulid(), media_id=mid,
                      source_in_ms=0, source_out_ms=100, speed=2.0, timeline_start_ms=500)
    s.add(i2)
    s.commit()

    # track_id 默认 0
    got1 = s.execute(select(EditInstance).where(EditInstance.id == i1.id)).scalar_one()
    assert got1.track_id == 0

    # 同 (draft_id, instance_id) 重复 → IntegrityError
    iid = _ulid()
    s.add(EditInstance(id=_ulid(), draft_id=d1.id, instance_id=iid, media_id=mid,
                       source_in_ms=0, source_out_ms=10, speed=1.0, timeline_start_ms=0))
    s.commit()
    s.add(EditInstance(id=_ulid(), draft_id=d1.id, instance_id=iid, media_id=mid,
                       source_in_ms=0, source_out_ms=10, speed=1.0, timeline_start_ms=0))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 draft_id → IntegrityError
    s.add(EditInstance(id=_ulid(), draft_id="nonexistent_did", instance_id=_ulid(),
                       media_id=mid, source_in_ms=0, source_out_ms=10, speed=1.0, timeline_start_ms=0))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # 悬空 media_id → IntegrityError
    s.add(EditInstance(id=_ulid(), draft_id=d1.id, instance_id=_ulid(),
                       media_id="nonexistent_mid", source_in_ms=0, source_out_ms=10,
                       speed=1.0, timeline_start_ms=0))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_t5_confirmed_edits(db):
    """studio_confirmed_edits：同 (edit_id, edit_revision) 重复 → IntegrityError；rev 1、2 各一条成功。"""
    s = Session()
    eid = _ulid()
    # rev 1、2 两条成功（不同版本各可确认一次）
    _insert_confirmed(s, eid, 1)
    s.commit()
    _insert_confirmed(s, eid, 2)
    s.commit()

    # 同 (edit_id, edit_revision) 两条 confirmed → IntegrityError
    # （raw SQL 立即执行：异常在 execute 时抛出，不在 commit 时）
    with pytest.raises(IntegrityError):
        _insert_confirmed(s, eid, 1)
    s.rollback()
    s.close()


def test_t6_releases(db):
    """发布：条件唯一（每 (project, name) 至多一个存活行）+ R12 同名版本链 + 状态/可空列。"""
    s = Session()
    _make_user(s, "u1")
    p = _ulid()
    _make_project(s, "u1", p)
    ceid = _ulid()
    confirmed_row_id = _insert_confirmed(s, ceid, 1)
    s.commit()

    # (1) A: name=X draft 成功
    a = Release(id=_ulid(), project_id=p, name="X", confirmed_edit_id=confirmed_row_id,
                poster_artifact_id=None, status="draft",
                predecessor_release_id=None, public_dir=None, published_at=None)
    s.add(a)
    s.commit()

    # (2) 第二条 name=X draft → IntegrityError（存活唯一）
    s.add(Release(id=_ulid(), project_id=p, name="X", confirmed_edit_id=confirmed_row_id, status="draft"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # (3) A 置 retired 后再建 name=X draft → 成功（退役行放行）
    a.status = "retired"
    s.commit()
    b = Release(id=_ulid(), project_id=p, name="X", confirmed_edit_id=confirmed_row_id,
                status="draft", predecessor_release_id=a.id)
    s.add(b)
    s.commit()

    # (4) 两条同名都 published → 第二条 IntegrityError
    c1 = Release(id=_ulid(), project_id=p, name="Y", confirmed_edit_id=confirmed_row_id, status="published")
    s.add(c1)
    s.commit()
    s.add(Release(id=_ulid(), project_id=p, name="Y", confirmed_edit_id=confirmed_row_id, status="published"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # (5) published + retired 同名 → 成功（R12 版本链）
    d1 = Release(id=_ulid(), project_id=p, name="Z", confirmed_edit_id=confirmed_row_id,
                 status="published", public_dir="public/z1", published_at=1000.0)
    s.add(d1)
    s.commit()
    d2 = Release(id=_ulid(), project_id=p, name="Z", confirmed_edit_id=confirmed_row_id,
                 status="retired", predecessor_release_id=d1.id,
                 public_dir="public/z0", published_at=999.0)
    s.add(d2)
    s.commit()

    # (6) predecessor 指向 published 行成功
    w1 = Release(id=_ulid(), project_id=p, name="W", confirmed_edit_id=confirmed_row_id, status="published")
    s.add(w1)
    s.commit()
    w2 = Release(id=_ulid(), project_id=p, name="W", confirmed_edit_id=confirmed_row_id,
                 status="retired", predecessor_release_id=w1.id)
    s.add(w2)
    s.commit()

    # (7) predecessor 悬空 → IntegrityError
    s.add(Release(id=_ulid(), project_id=p, name="V", confirmed_edit_id=confirmed_row_id,
                  status="draft", predecessor_release_id="nonexistent_rel"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # (8) status="deleted" → IntegrityError
    s.add(Release(id=_ulid(), project_id=p, name="U", confirmed_edit_id=confirmed_row_id, status="deleted"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()

    # (9) public_dir/published_at 可空（A 全 NULL 建成功）+ 非空可读回
    got_a = s.execute(select(Release).where(Release.id == a.id)).scalar_one()
    assert got_a.public_dir is None and got_a.published_at is None
    got_d1 = s.execute(select(Release).where(Release.id == d1.id)).scalar_one()
    assert got_d1.public_dir == "public/z1" and got_d1.published_at == 1000.0
    s.close()


def test_t7_source_audit(db):
    """源码审计：三个 models.py 均不含禁串（允许 ...core.db）。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    banned = (
        "backend.db", "backend.app", "backend.auth", "backend.providers",
        "backend.worker", "from ..db", "from ..app", "from ..auth",
        "from ..environment", "load_bootstrap_environment",
    )
    for rel in (
        "backend/studio/media/models.py",
        "backend/studio/edits/models.py",
        "backend/studio/publishing/models.py",
    ):
        with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as f:
            src = f.read()
        for b in banned:
            assert b not in src, f"{rel} 含禁串: {b}"
