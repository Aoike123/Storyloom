"""EDIT-01 项目剪辑草稿读写（附录 08 §2/§3、附录 11 §2）行为验证。

覆盖：
- GET 空态（全 null + 空数组，不造样例实例/时长）；
- 首次保存 201（edit_revision=1、cas_revision=1）与二次保存版本链只追加；
- CAS：expected_revision 不符 409（expected/actual 精确）、新建时非 null 422；
- manifest 全部 violations 一次报（field 带下标精确路径、media_id not_found）；
- 跨项目/不存在 edit_id 404 同形状、无 token 401、项目 404；
- 幂等重放 200 原体零新行、异内容 422 reused；
- manifest_digest 服务端计算（客户端传假值被忽略）；
- instances 行与 manifest 一一对应（DTO 与 DB 双侧）；
- 负向断言：不写 studio_confirmed_edits / studio_releases、不改 MediaArtifact、
  不删旧草稿行、毫秒单位为整数。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
db fixture 与 `_register`/`_auth`/`_cmd` 三个 helper 照抄 tests/studio/test_projects.py。
本文件不 import 任何旧后端模块。
"""
import hashlib
import json
import re
import time
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.core.db import Base, Session, engine, make_ulid
from backend.studio.contracts.models import CommandRecord
from backend.studio.edits.models import ConfirmedEdit, EditDraft, EditInstance
from backend.studio.media.models import MediaArtifact
from backend.studio.projects.models import StudioProject
from backend.studio.publishing.models import Release

_ULID_RE = re.compile(r"^[0-9a-hjkmnp-tv-z]{26}$")


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（projects↔source_revisions、assets↔asset_revisions）：drop 前临时关 FK
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        Base.metadata.drop_all(conn, checkfirst=True)
        conn.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(conn)
        conn.commit()
    finally:
        conn.close()
    yield
    engine.dispose()


def _register(client, username) -> str:
    r = client.post(
        "/api/studio/auth/register",
        json={"username": username, "password": "secret123", "email": "t@x"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _cmd() -> str:
    return str(uuid.uuid4())


def _mk_project(client, token, name="EditProj") -> str:
    r = client.post(
        "/api/studio/projects",
        json={"name": name, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mk_media(pid, media_kind="video") -> str:
    """直接 INSERT MediaArtifact（需同时建 project 行——由 _mk_project 完成）。"""
    mid = make_ulid()
    with Session.begin() as s:
        s.add(
            MediaArtifact(
                id=mid,
                project_id=pid,
                media_kind=media_kind,
                filename=f"media/{mid}.mp4",
                size_bytes=4096,
                sha256=hashlib.sha256(mid.encode("utf-8")).hexdigest(),
                duration_ms=8000,
                width=320,
                height=240,
                source="upload",
                source_job_id=None,
                created_at=time.time(),
            )
        )
    return mid


def _inst(mid, iid="inst-1", **over) -> dict:
    item = {
        "id": iid,
        "track_id": 0,
        "timeline_start_ms": 0,
        "media_id": mid,
        "source_in_ms": 0,
        "source_out_ms": 1000,
        "speed": 1.0,
    }
    item.update(over)
    return item


def _manifest(instances) -> dict:
    return {
        "schema": "storyloom.editmanifest/v1",
        "canvas": {"width": 320, "height": 240, "fps": 25},
        "instances": instances,
        "output": {
            "container": "mp4",
            "video": "h264",
            "audio": "aac",
            "width": 320,
            "height": 240,
            "fps": 25,
            "sample_rate": 44100,
        },
    }


def _digest(manifest) -> str:
    """独立重算（不调用被测 service.manifest_digest，避免同源错误）。"""
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get(client, token, pid):
    return client.get(f"/api/studio/projects/{pid}/edit", headers=_auth(token))


def _post(client, token, pid, **body):
    return client.post(
        f"/api/studio/projects/{pid}/edit/draft", json=body, headers=_auth(token)
    )


def _save(client, token, pid, manifest, edit_id=None, expected_revision=None, command_id=None):
    return _post(
        client,
        token,
        pid,
        edit_id=edit_id,
        manifest=manifest,
        expected_revision=expected_revision,
        command_id=command_id or _cmd(),
    )


def _violations(resp) -> list[tuple[str, str]]:
    err = resp.json()["error"]
    assert err["code"] == "validation_failed", err
    return [(v["field"], v["rule"]) for v in err["details"]["violations"]]


def _counts() -> dict:
    with Session() as s:
        return {
            "projects": s.scalar(select(func.count(StudioProject.id))),
            "drafts": s.scalar(select(func.count(EditDraft.id))),
            "instances": s.scalar(select(func.count(EditInstance.id))),
            "confirmed": s.scalar(select(func.count(ConfirmedEdit.id))),
            "releases": s.scalar(select(func.count(Release.id))),
            "media": s.scalar(select(func.count(MediaArtifact.id))),
            "commands": s.scalar(select(func.count(CommandRecord.id))),
        }


# ── GET 空态 ────────────────────────────────────────────────────────────

def test_t1_no_token_401(db, client):
    pid = "01" * 13
    assert _get(client, "x", pid).status_code == 401
    r = client.post(
        f"/api/studio/projects/{pid}/edit/draft",
        json={"edit_id": None, "manifest": None, "expected_revision": None,
              "command_id": _cmd()},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t2_empty_state_all_null_no_samples(db, client):
    token = _register(client, "t2_alice")
    pid = _mk_project(client, token)
    r = _get(client, token, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "edit_id": None,
        "edit_revision": None,
        "cas_revision": None,
        "manifest": None,
        "manifest_digest": None,
        "instances": [],
        "confirmed": None,
    }
    # 空态就是空态：不造样例实例、不造假时长、无任何剪辑写
    c = _counts()
    assert (c["drafts"], c["instances"], c["confirmed"], c["releases"]) == (0, 0, 0, 0)
    # 读命令零副作用：命令记录只有建项目那一条
    assert c["commands"] == 1


def test_t3_project_missing_or_foreign_404(db, client):
    ta = _register(client, "t3_a")
    tb = _register(client, "t3_b")
    pid = _mk_project(client, ta)
    # 非属主读/写都 404 project（不泄漏存在性）
    r = _get(client, tb, pid)
    assert r.status_code == 404
    assert r.json()["error"]["details"] == {"kind": "project", "id": pid}
    mid = _mk_media(pid)
    r2 = _save(client, tb, pid, _manifest([_inst(mid)]))
    assert r2.status_code == 404
    assert r2.json()["error"]["details"] == {"kind": "project", "id": pid}
    # 不存在的项目同形状
    ghost = "01" * 13
    assert _get(client, ta, ghost).status_code == 404
    assert _save(client, ta, ghost, _manifest([])).json()["error"]["details"] == {
        "kind": "project",
        "id": ghost,
    }


# ── 首次保存与版本链 ────────────────────────────────────────────────────

def test_t4_first_save_201_revision_1(db, client):
    token = _register(client, "t4_carol")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    m = _manifest([_inst(mid)])

    r = _save(client, token, pid, m)
    assert r.status_code == 201, r.text  # 首次保存 = 创建（同项目域 POST 约定）
    b = r.json()
    assert b["object_ref"]["kind"] == "edit"
    assert b["object_ref"]["revision"] == 1
    assert _ULID_RE.match(b["object_ref"]["id"]), b["object_ref"]["id"]
    assert b["edit_revision"] == 1
    assert b["cas_revision"] == 1
    assert b["manifest"] == m
    assert b["manifest_digest"] == _digest(m)
    assert re.fullmatch(r"[0-9a-f]{64}", b["manifest_digest"])
    assert isinstance(b["updated_at"], (int, float))
    assert len(b["instances"]) == 1
    dto = b["instances"][0]
    assert dto["object_ref"] == {
        "kind": "edit_instance",
        "id": dto["instance_id"],
        "revision": None,
    }
    assert dto["instance_id"] == "inst-1"
    assert dto["media_id"] == mid
    assert dto["source_in_ms"] == 0 and dto["source_out_ms"] == 1000
    assert dto["speed"] == 1.0
    assert dto["timeline_start_ms"] == 0 and dto["track_id"] == 0

    # DB 侧：1 草稿行 + 1 实例行，且字段逐一对齐
    with Session() as s:
        drafts = s.scalars(select(EditDraft)).all()
        assert len(drafts) == 1
        d = drafts[0]
        assert d.edit_id == b["object_ref"]["id"]
        assert d.edit_revision == 1 and d.cas_revision == 1
        assert json.loads(d.manifest) == m
        assert d.manifest_digest == _digest(m)
        rows = s.scalars(select(EditInstance).where(EditInstance.draft_id == d.id)).all()
        assert len(rows) == 1
        assert rows[0].instance_id == "inst-1" and rows[0].media_id == mid
        assert rows[0].id != rows[0].instance_id  # 行主键 = 服务端 ULID
        assert d.id == rows[0].draft_id

    # GET 回读一致
    g = _get(client, token, pid)
    assert g.status_code == 200
    assert g.json()["edit_id"] == b["object_ref"]["id"]
    assert g.json()["edit_revision"] == 1
    assert g.json()["cas_revision"] == 1
    assert g.json()["manifest"] == m
    assert g.json()["manifest_digest"] == _digest(m)
    assert [i["instance_id"] for i in g.json()["instances"]] == ["inst-1"]
    assert g.json()["confirmed"] is None


def test_t5_second_save_appends_version_chain(db, client):
    token = _register(client, "t5_dave")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    m1 = _manifest([_inst(mid, "inst-1")])
    r1 = _save(client, token, pid, m1)
    assert r1.status_code == 201, r1.text
    eid = r1.json()["object_ref"]["id"]

    mid2 = _mk_media(pid)
    m2 = _manifest(
        [
            _inst(mid, "inst-1", source_out_ms=2000),
            _inst(mid2, "inst-2", timeline_start_ms=2000),
        ]
    )
    r2 = _save(client, token, pid, m2, edit_id=eid, expected_revision=1)
    assert r2.status_code == 201, r2.text
    b2 = r2.json()
    assert b2["object_ref"] == {"kind": "edit", "id": eid, "revision": 2}
    assert b2["edit_revision"] == 2 and b2["cas_revision"] == 2
    assert [i["instance_id"] for i in b2["instances"]] == ["inst-1", "inst-2"]
    assert b2["instances"][0]["source_out_ms"] == 2000
    assert b2["instances"][1]["timeline_start_ms"] == 2000

    # 版本链只追加：旧行仍在，且内容未被改写
    with Session() as s:
        rows = s.scalars(
            select(EditDraft)
            .where(EditDraft.edit_id == eid)
            .order_by(EditDraft.edit_revision.asc())
        ).all()
        assert [d.edit_revision for d in rows] == [1, 2]
        assert json.loads(rows[0].manifest) == m1
        assert rows[0].manifest_digest == _digest(m1)
        assert rows[0].cas_revision == 1
        assert rows[1].cas_revision == 2
        # 旧草稿行的实例行也原样保留（每版本一套行）
        old = s.scalars(
            select(EditInstance).where(EditInstance.draft_id == rows[0].id)
        ).all()
        assert [x.instance_id for x in old] == ["inst-1"]
        assert old[0].source_out_ms == 1000

    g = _get(client, token, pid)
    assert g.json()["edit_revision"] == 2
    assert g.json()["manifest"] == m2
    assert g.json()["manifest_digest"] == _digest(m2)
    assert [i["instance_id"] for i in g.json()["instances"]] == ["inst-1", "inst-2"]


# ── CAS ────────────────────────────────────────────────────────────────

def test_t6_cas_mismatch_409_exact(db, client):
    token = _register(client, "t6_erin")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    m = _manifest([_inst(mid)])
    eid = _save(client, token, pid, m).json()["object_ref"]["id"]

    before = _counts()
    r = _save(client, token, pid, m, edit_id=eid, expected_revision=5)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"] == {
        "object": {"kind": "edit", "id": eid},
        "expected": 5,
        "actual": 1,
    }
    # expected 为 null（已有草稿）同样 409，不落半次保存
    r2 = _save(client, token, pid, m, edit_id=eid)
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["details"] == {
        "object": {"kind": "edit", "id": eid},
        "expected": None,
        "actual": 1,
    }
    after = _counts()
    assert after["drafts"] == before["drafts"] == 1
    assert after["instances"] == before["instances"] == 1
    assert after["commands"] == before["commands"]


def test_t7_new_draft_expected_revision_must_be_null(db, client):
    token = _register(client, "t7_frank")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    r = _save(client, token, pid, _manifest([_inst(mid)]), expected_revision=1)
    assert r.status_code == 422, r.text
    assert _violations(r) == [("expected_revision", "invalid")]
    assert _counts()["drafts"] == 0


# ── manifest 校验 ──────────────────────────────────────────────────────

def test_t8_manifest_shape_violations_all_at_once(db, client):
    token = _register(client, "t8_grace")
    pid = _mk_project(client, token)
    bad = {
        "schema": "storyloom.editmanifest/v2",
        "canvas": {"width": 0, "height": "240", "fps": -1},
        "instances": "not-a-list",
    }
    r = _save(client, token, pid, bad)
    assert r.status_code == 422, r.text
    assert set(_violations(r)) == {
        ("manifest.schema", "invalid"),
        ("manifest.canvas.width", "invalid"),
        ("manifest.canvas.height", "invalid"),
        ("manifest.canvas.fps", "invalid"),
        ("manifest.instances", "invalid"),
    }
    # canvas 整体缺失 → 三键各自 invalid（一次报）
    r2 = _save(client, token, pid, {"schema": "storyloom.editmanifest/v1"})
    assert set(_violations(r2)) >= {
        ("manifest.canvas.width", "invalid"),
        ("manifest.canvas.height", "invalid"),
        ("manifest.canvas.fps", "invalid"),
    }
    assert (r2.json()["error"]["details"]["violations"][0]["field"]) == "manifest.canvas.width"
    assert _counts()["drafts"] == 0


def test_t9_manifest_non_object_422(db, client):
    token = _register(client, "t9_heidi")
    pid = _mk_project(client, token)
    for bad in ("nope", [], 42, None):
        r = _save(client, token, pid, bad)
        assert r.status_code == 422, (bad, r.text)
        assert _violations(r) == [("manifest.schema", "invalid")]
    assert _counts()["drafts"] == 0


def test_t10_instance_violations_precise_paths(db, client):
    token = _register(client, "t10_ivan")
    pid = _mk_project(client, token)
    other_pid = _mk_project(client, token, name="OtherProj")
    mid = _mk_media(pid)
    foreign_mid = _mk_media(other_pid)

    bad0 = {
        # 缺 id、缺 media_id
        "track_id": 0,
        "timeline_start_ms": 0,
        "source_in_ms": 0,
        "source_out_ms": 1000,
        "speed": 1.0,
    }
    bad1 = {
        "id": "inst-2",
        "media_id": foreign_mid,  # 存在但属别的项目 → not_found
        "track_id": 1,
        "timeline_start_ms": -1,
        "source_in_ms": 100,
        "source_out_ms": 50,
        "speed": 5.0,
    }
    bad2 = _inst(mid, "inst-3", source_in_ms=-5, speed=0.1)
    bad3 = _inst(mid, "inst-4", speed=True)
    r = _save(client, token, pid, _manifest([bad0, bad1, bad2, bad3]))
    assert r.status_code == 422, r.text
    got = _violations(r)
    expected = {
        ("manifest.instances[0].id", "required"),
        ("manifest.instances[0].media_id", "required"),
        ("manifest.instances[1].media_id", "not_found"),
        ("manifest.instances[1].track_id", "invalid"),
        ("manifest.instances[1].timeline_start_ms", "invalid"),
        ("manifest.instances[1].source_out_ms", "invalid"),
        ("manifest.instances[1].speed", "invalid"),
        ("manifest.instances[2].source_in_ms", "invalid"),
        ("manifest.instances[2].speed", "invalid"),
        ("manifest.instances[3].speed", "invalid"),
    }
    missing = expected - set(got)
    assert not missing, f"缺 violations: {sorted(missing)}；实际 {got}"
    assert _counts()["drafts"] == 0
    assert _counts()["instances"] == 0

    # 不存在的 media_id → not_found（rule 精确）
    ghost = {"id": "i", "media_id": "0" * 26, "track_id": 0,
             "timeline_start_ms": 0, "source_in_ms": 0, "source_out_ms": 1,
             "speed": 1.0}
    r2 = _save(client, token, pid, _manifest([ghost]))
    assert _violations(r2) == [("manifest.instances[0].media_id", "not_found")]

    # 同 manifest 内实例 id 重复 → 422（DB UNIQUE(draft_id, instance_id) 的保守前置）
    r3 = _save(client, token, pid, _manifest([_inst(mid, "dup"), _inst(mid, "dup")]))
    assert r3.status_code == 422, r3.text
    assert ("manifest.instances[1].id", "invalid") in _violations(r3)


def test_t11_instance_not_object_422(db, client):
    token = _register(client, "t11_judy")
    pid = _mk_project(client, token)
    r = _save(client, token, pid, _manifest(["not-an-object"]))
    assert r.status_code == 422, r.text
    assert ("manifest.instances[0]", "invalid") in _violations(r)


def test_t12_speed_and_time_acceptance_bounds(db, client):
    token = _register(client, "t12_karen")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    # 边界内：0.25 / 4.0 / 整数毫秒
    m = _manifest(
        [
            _inst(mid, "i-min", source_in_ms=0, source_out_ms=4000, speed=0.25),
            _inst(mid, "i-max", source_in_ms=1000, source_out_ms=2000, speed=4.0),
        ]
    )
    r = _save(client, token, pid, m)
    assert r.status_code == 201, r.text
    assert [i["speed"] for i in r.json()["instances"]] == [0.25, 4.0]
    # 一律整数毫秒：入参与回读都不出现浮点时间
    for inst in r.json()["instances"]:
        for k in ("source_in_ms", "source_out_ms", "timeline_start_ms"):
            assert isinstance(inst[k], int) and not isinstance(inst[k], bool), k


# ── edit_id 归属 ────────────────────────────────────────────────────────

def test_t13_cross_project_edit_id_404_not_leaked(db, client):
    token = _register(client, "t13_laura")
    pid1 = _mk_project(client, token, name="P13A")
    pid2 = _mk_project(client, token, name="P13B")
    mid = _mk_media(pid1)
    eid = _save(client, token, pid1, _manifest([_inst(mid)])).json()["object_ref"]["id"]

    # 同属主、跨项目引用 edit_id → 404 not_found(kind=edit) 同形状
    r = _save(client, token, pid2, _manifest([]), edit_id=eid, expected_revision=1)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "edit", "id": eid}

    # 不存在的 edit_id 同形状
    ghost = make_ulid()
    r2 = _save(client, token, pid2, _manifest([]), edit_id=ghost, expected_revision=1)
    assert r2.status_code == 404, r2.text
    assert r2.json()["error"]["details"] == {"kind": "edit", "id": ghost}
    assert _counts()["drafts"] == 1  # 未新增草稿行


# ── 幂等 ────────────────────────────────────────────────────────────────

def test_t14_replay_200_same_body_zero_new_rows(db, client):
    token = _register(client, "t14_mallory")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    m = _manifest([_inst(mid)])
    cmd = _cmd()
    body = {"edit_id": None, "manifest": m, "expected_revision": None,
            "command_id": cmd}
    r1 = _post(client, token, pid, **body)
    assert r1.status_code == 201, r1.text
    first = r1.json()
    before = _counts()

    r2 = _post(client, token, pid, **body)
    assert r2.status_code == 200, (r2.status_code, r2.text)  # 重放 = 200 非 201
    assert r2.json() == first  # 原样返回首次 result_payload
    after = _counts()
    assert after == before, f"重放产生新行: {before} → {after}"
    assert after["commands"] == before["commands"] == 2  # 建项目 + 首次保存


def test_t15_replay_second_save_200(db, client):
    token = _register(client, "t15_nancy")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    m1 = _manifest([_inst(mid, "a")])
    eid = _save(client, token, pid, m1).json()["object_ref"]["id"]
    m2 = _manifest([_inst(mid, "a", source_out_ms=3000)])
    cmd = _cmd()
    body = {"edit_id": eid, "manifest": m2, "expected_revision": 1,
            "command_id": cmd}
    r1 = _post(client, token, pid, **body)
    assert r1.status_code == 201, r1.text
    before = _counts()
    r2 = _post(client, token, pid, **body)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()
    assert _counts() == before


def test_t16_command_id_reused_422(db, client):
    token = _register(client, "t16_olga")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    cmd = _cmd()
    r1 = _save(client, token, pid, _manifest([_inst(mid, "a")]), command_id=cmd)
    assert r1.status_code == 201, r1.text
    before = _counts()
    # 异内容（不同 manifest）同 command_id → 422 reused
    r2 = _save(client, token, pid, _manifest([_inst(mid, "b")]), command_id=cmd)
    assert r2.status_code == 422, r2.text
    assert _violations(r2) == [("command_id", "reused")]
    assert _counts() == before


# ── digest 服务端计算 ───────────────────────────────────────────────────

def test_t17_digest_ignores_client_value(db, client):
    token = _register(client, "t17_paul")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    m = _manifest([_inst(mid)])
    fake = "f" * 64
    r = _post(
        client,
        token,
        pid,
        edit_id=None,
        manifest=m,
        expected_revision=None,
        command_id=_cmd(),
        manifest_digest=fake,  # 额外字段：客户端伪造值必须被忽略
    )
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["manifest_digest"] == _digest(m)
    assert b["manifest_digest"] != fake
    # 同 manifest、变更无关字段不影响 digest（键排序规范化）
    m2 = dict(reversed(list(m.items())))
    assert _digest(m2) == b["manifest_digest"]
    # DB 落库 digest 与响应一致
    with Session() as s:
        d = s.scalars(select(EditDraft)).one()
        assert d.manifest_digest == _digest(m)


# ── 实例行一致性 ────────────────────────────────────────────────────────

def test_t18_instance_rows_one_to_one_with_manifest(db, client):
    token = _register(client, "t18_quinn")
    pid = _mk_project(client, token)
    m1 = _mk_media(pid)
    m2 = _mk_media(pid)
    insts = [
        _inst(m1, "alpha", timeline_start_ms=0, source_in_ms=100, source_out_ms=2100),
        _inst(m2, "beta", timeline_start_ms=1500, source_in_ms=0, source_out_ms=1000,
              speed=2.0),
        _inst(m1, "gamma", timeline_start_ms=4000, source_in_ms=500, source_out_ms=900,
              speed=0.5),
    ]
    m = _manifest(insts)
    r = _save(client, token, pid, m)
    assert r.status_code == 201, r.text
    b = r.json()
    assert [i["instance_id"] for i in b["instances"]] == ["alpha", "beta", "gamma"]

    with Session() as s:
        d = s.scalars(select(EditDraft)).one()
        rows = s.scalars(
            select(EditInstance).where(EditInstance.draft_id == d.id)
        ).all()
    by_id = {x.instance_id: x for x in rows}
    assert set(by_id) == {"alpha", "beta", "gamma"}
    assert len(rows) == len(insts)
    for src in insts:
        row = by_id[src["id"]]
        assert row.media_id == src["media_id"]
        assert row.source_in_ms == src["source_in_ms"]
        assert row.source_out_ms == src["source_out_ms"]
        assert row.speed == src["speed"]
        assert row.timeline_start_ms == src["timeline_start_ms"]
        assert row.track_id == src["track_id"]

    # GET 的实例顺序与当前 manifest 顺序一致（instances 是 manifest 的字面映射）
    g = _get(client, token, pid)
    assert [i["instance_id"] for i in g.json()["instances"]] == [
        "alpha",
        "beta",
        "gamma",
    ]
    assert [
        (i["media_id"], i["source_in_ms"], i["source_out_ms"], i["speed"])
        for i in g.json()["instances"]
    ] == [
        (i["media_id"], i["source_in_ms"], i["source_out_ms"], i["speed"])
        for i in insts
    ]


# ── GET confirmed 分支 + 负向断言 ───────────────────────────────────────

def test_t19_confirmed_read_and_no_writes(db, client):
    token = _register(client, "t19_randy")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    m = _manifest([_inst(mid)])
    eid = _save(client, token, pid, m).json()["object_ref"]["id"]

    # 无确认行 → confirmed = null
    assert _get(client, token, pid).json()["confirmed"] is None

    # 手工插入确认行（本卡不做确认，仅验证 GET 读取路径）
    cid = make_ulid()
    with Session.begin() as s:
        s.add(
            ConfirmedEdit(
                id=cid,
                edit_id=eid,
                edit_revision=1,
                manifest=json.dumps(m, ensure_ascii=False),
                manifest_digest="a" * 64,
                output_spec=json.dumps({"container": "mp4", "fps": 25}),
                created_at=time.time(),
            )
        )
    g = _get(client, token, pid)
    assert g.json()["confirmed"] == {
        "object_ref": {"kind": "confirmed_edit", "id": eid, "revision": 1},
        "manifest_digest": "a" * 64,  # 取确认行自身值，非草稿 digest
        "output_spec": {"container": "mp4", "fps": 25},
    }
    # 再保存草稿不影响确认行，也不新增确认/发布行
    r = _save(client, token, pid, _manifest([_inst(mid, "x")]), edit_id=eid,
              expected_revision=1)
    assert r.status_code == 201, r.text
    assert _get(client, token, pid).json()["confirmed"]["manifest_digest"] == "a" * 64
    c = _counts()
    assert c["confirmed"] == 1
    assert c["releases"] == 0


def test_t20_hard_negatives_no_confirmed_no_release_no_media_change(db, client):
    token = _register(client, "t20_susan")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    media_before = _counts()["media"]
    eid = _save(client, token, pid, _manifest([_inst(mid)])).json()["object_ref"]["id"]
    _save(client, token, pid, _manifest([_inst(mid, "z")]), edit_id=eid,
          expected_revision=1)

    c = _counts()
    assert c["confirmed"] == 0, "EDIT-01 不得写 studio_confirmed_edits"
    assert c["releases"] == 0, "EDIT-01 不得写 studio_releases"
    assert c["media"] == media_before == 1, "EDIT-01 不得改 studio_media_artifacts"
    assert c["drafts"] == 2, "版本链只追加：两次保存 = 两行"
    # 旧草稿行未被删除/改写（rev1 仍在，内容原样）
    with Session() as s:
        rows = s.scalars(
            select(EditDraft).where(EditDraft.edit_id == eid).order_by(
                EditDraft.edit_revision.asc()
            )
        ).all()
        assert [d.edit_revision for d in rows] == [1, 2]
        assert json.loads(rows[0].manifest)["instances"][0]["id"] == "inst-1"
        assert rows[0].manifest_digest == _digest(
            _manifest([_inst(mid)])
        )


def test_t21_get_is_pure_read(db, client):
    token = _register(client, "t21_trent")
    pid = _mk_project(client, token)
    mid = _mk_media(pid)
    _save(client, token, pid, _manifest([_inst(mid)]))
    before = _counts()
    for _ in range(3):
        assert _get(client, token, pid).status_code == 200
    assert _counts() == before
