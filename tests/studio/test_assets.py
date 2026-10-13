"""ASSET-01-a 创建人物身份（附录 05 §2 AS1）—— POST /projects/{pid}/assets。

覆盖：character/costume/scene 创建、costume→character 归属一致性、
spec 必填键与 body_type、type/name 非法、跨项目/无 token、command_id 幂等重放
（200 且不新增行）与 reused 422、无 occurrence/binding 副作用。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件不改写 STUDIO_* env。db fixture 照抄 tests/studio/test_projects.py。
"""
import re
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.core.db import Base, Session, engine
from backend.studio.assets.models import Asset, AssetRevision
from backend.studio.contracts.models import CommandRecord
from backend.studio.relations.models import AssetBinding, AssetOccurrence
from backend.studio.projects.models import StudioProject


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


def _mk_project(client, token, name="Proj") -> str:
    r = client.post(
        "/api/studio/projects",
        json={"name": name, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mk_asset(client, token, pid, *, type_, name, spec, character_id=None):
    body = {"type": type_, "name": name, "spec": spec, "command_id": _cmd()}
    if character_id is not None:
        body["character_id"] = character_id
    return client.post(
        f"/api/studio/projects/{pid}/assets", json=body, headers=_auth(token)
    )


def _counts() -> dict:
    with Session() as s:
        return {
            "assets": s.scalar(select(func.count(Asset.id))),
            "revisions": s.scalar(select(func.count(AssetRevision.id))),
            "commands": s.scalar(select(func.count(CommandRecord.id))),
            "occurrences": s.scalar(select(func.count(AssetOccurrence.id))),
            "bindings": s.scalar(select(func.count(AssetBinding.id))),
        }


# ── 成功路径 ────────────────────────────────────────────────────────────────

def test_t1_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/01010101010101010101010101/assets",
        json={
            "type": "character",
            "name": "X",
            "spec": {"appearance": "a"},
            "command_id": _cmd(),
        },
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t2_create_character_201(db, client):
    token = _register(client, "a2_alice")
    pid = _mk_project(client, token)
    r = _mk_asset(
        client,
        token,
        pid,
        type_="character",
        name="  林 述  ",
        spec={
            "appearance": "短发、深色夹克",
            "body_type": "clothed",
            "height_cm": 178,
            "notes": "主角",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # DTO 形状（主模型冻结，与 AS1 之后详情读一致）
    assert set(body) == {
        "object_ref",
        "type",
        "name",
        "character",
        "current_revision",
        "versions",
        "occurrences",
        "bindings",
    }
    aid = body["object_ref"]["id"]
    assert body["object_ref"] == {"kind": "asset", "id": aid, "revision": 1}
    assert re.fullmatch(r"[0-9a-z]{26}", aid) and not (set(aid) & set("ilou"))
    assert body["type"] == "character"
    assert body["name"] == "林 述"  # trim 后保存
    assert body["character"] is None
    assert body["current_revision"] == {
        "revision": 1,
        "spec": {
            "appearance": "短发、深色夹克",
            "body_type": "clothed",
            "height_cm": 178,
            "notes": "主角",
        },
        "media_artifact_id": None,
        "review_status": "unreviewed",
    }
    assert len(body["versions"]) == 1
    v0 = body["versions"][0]
    assert v0["revision"] == 1
    assert v0["spec"] == body["current_revision"]["spec"]
    assert v0["media_artifact_id"] is None
    assert v0["review_status"] == "unreviewed"
    assert isinstance(v0["created_at"], (int, float))
    assert body["occurrences"] == [] and body["bindings"] == []

    # 落库：身份稳定 + 版本行 + costume_required 默认 true
    with Session() as s:
        a = s.get(Asset, aid)
        assert a is not None and a.project_id == pid and a.type == "character"
        assert a.name == "林 述" and a.character_id is None
        assert a.costume_required is True
        rev = s.scalar(select(AssetRevision).where(AssetRevision.asset_id == aid))
        assert rev.id == a.current_revision_id
        assert rev.revision == 1 and rev.review_status == "unreviewed"
        assert rev.media_artifact_id is None and rev.generation_input is None


def test_t3_create_costume_with_character_201(db, client):
    token = _register(client, "a3_bob")
    pid = _mk_project(client, token)
    rc = _mk_asset(
        client,
        token,
        pid,
        type_="character",
        name="人物A",
        spec={"appearance": "高个"},
    )
    assert rc.status_code == 201, rc.text
    char_id = rc.json()["object_ref"]["id"]

    r = _mk_asset(
        client,
        token,
        pid,
        type_="costume",
        name="  夜行衣  ",
        character_id=char_id,
        spec={"description": "黑色紧身衣", "colors": ["black"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "costume"
    assert body["name"] == "夜行衣"
    assert body["character"] == {"kind": "asset", "id": char_id}
    assert body["object_ref"]["revision"] == 1
    assert body["current_revision"]["review_status"] == "unreviewed"
    assert body["current_revision"]["media_artifact_id"] is None
    with Session() as s:
        a = s.get(Asset, body["object_ref"]["id"])
        assert a.character_id == char_id
        assert a.costume_required is True  # 服装行也保持模型默认（本卡不改语义）


def test_t4_create_scene_201(db, client):
    token = _register(client, "a4_carol")
    pid = _mk_project(client, token)
    r = _mk_asset(
        client,
        token,
        pid,
        type_="scene",
        name="旧仓库",
        spec={"location": "城郊旧仓库", "time_of_day": "night"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "scene"
    assert body["character"] is None
    assert body["current_revision"]["spec"] == {
        "location": "城郊旧仓库",
        "time_of_day": "night",
    }
    assert body["current_revision"]["media_artifact_id"] is None


def test_t5_custom_spec_keys_allowed_costume_and_scene(db, client):
    """主模型冻结：costume/scene 允许自定义键，只要有非空内容。"""
    token = _register(client, "a5_dave")
    pid = _mk_project(client, token)
    rc = _mk_asset(
        client, token, pid, type_="character", name="C", spec={"appearance": "x"}
    )
    char_id = rc.json()["object_ref"]["id"]
    r1 = _mk_asset(
        client,
        token,
        pid,
        type_="costume",
        name="自定义服装",
        character_id=char_id,
        spec={"fabric": "silk"},
    )
    assert r1.status_code == 201, r1.text
    r2 = _mk_asset(
        client, token, pid, type_="scene", name="自定义场景", spec={"mood": "压抑"}
    )
    assert r2.status_code == 201, r2.text


# ── 归属前置（422 precondition_failed）─────────────────────────────────────

def test_t6_costume_missing_character_422(db, client):
    token = _register(client, "a6_erin")
    pid = _mk_project(client, token)
    for missing in (None, ""):
        body = {
            "type": "costume",
            "name": "无主服装",
            "spec": {"description": "d"},
            "command_id": _cmd(),
        }
        if missing is not None:
            body["character_id"] = missing
        r = client.post(
            f"/api/studio/projects/{pid}/assets", json=body, headers=_auth(token)
        )
        assert r.status_code == 422, (missing, r.text)
        err = r.json()["error"]
        assert err["code"] == "precondition_failed"
        assert err["details"]["reason"] == "costume_requires_character"
        assert err["details"]["blocked_by"] == {
            "kind": "asset",
            "id": None,
            "revision": None,
        }


def test_t7_character_with_character_id_422(db, client):
    token = _register(client, "a7_frank")
    pid = _mk_project(client, token)
    rc = _mk_asset(
        client, token, pid, type_="character", name="C1", spec={"appearance": "x"}
    )
    cid = rc.json()["object_ref"]["id"]
    # character 带 character_id
    r = _mk_asset(
        client,
        token,
        pid,
        type_="character",
        name="C2",
        spec={"appearance": "y"},
        character_id=cid,
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "character_id_not_allowed"
    assert err["details"]["blocked_by"] == {
        "kind": "asset",
        "id": cid,
        "revision": None,
    }
    # scene 带 character_id
    r2 = _mk_asset(
        client,
        token,
        pid,
        type_="scene",
        name="S",
        spec={"location": "l"},
        character_id=cid,
    )
    assert r2.status_code == 422, r2.text
    assert r2.json()["error"]["code"] == "precondition_failed"
    # 无副作用：只存在最初 1 个 character
    assert _counts()["assets"] == 1


def test_t8_costume_character_id_wrong_type_422(db, client):
    token = _register(client, "a8_grace")
    pid = _mk_project(client, token)
    rc = _mk_asset(
        client, token, pid, type_="character", name="C", spec={"appearance": "x"}
    )
    cid = rc.json()["object_ref"]["id"]
    rcost = _mk_asset(
        client,
        token,
        pid,
        type_="costume",
        name="K1",
        character_id=cid,
        spec={"description": "d"},
    )
    assert rcost.status_code == 201, rcost.text
    rscene = _mk_asset(
        client, token, pid, type_="scene", name="S", spec={"location": "l"}
    )
    assert rscene.status_code == 201, rscene.text

    # character_id 指向 costume / scene → 422（非 character）
    for wrong in (rcost.json()["object_ref"]["id"], rscene.json()["object_ref"]["id"]):
        r = _mk_asset(
            client,
            token,
            pid,
            type_="costume",
            name="K2",
            character_id=wrong,
            spec={"description": "d"},
        )
        assert r.status_code == 422, (wrong, r.text)
        err = r.json()["error"]
        assert err["code"] == "precondition_failed"
        assert err["details"]["reason"] == "character_not_character"
        assert err["details"]["blocked_by"]["id"] == wrong


def test_t9_costume_character_id_nonexistent_422(db, client):
    token = _register(client, "a9_heidi")
    pid = _mk_project(client, token)
    ghost = "01" * 13
    r = _mk_asset(
        client,
        token,
        pid,
        type_="costume",
        name="K",
        character_id=ghost,
        spec={"description": "d"},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "character_not_found"
    assert err["details"]["blocked_by"] == {
        "kind": "asset",
        "id": ghost,
        "revision": None,
    }


def test_t10_costume_character_cross_project_422(db, client):
    """同属主两个项目：A 项目的 character 不能作为 B 项目 costume 的归属。"""
    token = _register(client, "a10_ivan")
    pid_a = _mk_project(client, token, "A")
    pid_b = _mk_project(client, token, "B")
    rc = _mk_asset(
        client, token, pid_a, type_="character", name="C", spec={"appearance": "x"}
    )
    cid = rc.json()["object_ref"]["id"]
    r = _mk_asset(
        client,
        token,
        pid_b,
        type_="costume",
        name="K",
        character_id=cid,
        spec={"description": "d"},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "character_cross_project"
    assert err["details"]["blocked_by"]["id"] == cid


# ── 字段/spec 校验（422 validation_failed）────────────────────────────────

def test_t11_spec_missing_appearance_422(db, client):
    token = _register(client, "a11_judy")
    pid = _mk_project(client, token)
    for bad_spec in ({}, {"body_type": "natural"}, {"appearance": ""}, {"appearance": "   "}):
        r = _mk_asset(
            client, token, pid, type_="character", name="C", spec=bad_spec
        )
        assert r.status_code == 422, (bad_spec, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v0 = err["details"]["violations"][0]
        assert v0["field"] == "spec" and v0["rule"] == "required"
    # appearance 非字符串
    r = _mk_asset(
        client, token, pid, type_="character", name="C", spec={"appearance": 3}
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_failed"


def test_t12_costume_scene_empty_spec_422(db, client):
    token = _register(client, "a12_karen")
    pid = _mk_project(client, token)
    rc = _mk_asset(
        client, token, pid, type_="character", name="C", spec={"appearance": "x"}
    )
    cid = rc.json()["object_ref"]["id"]
    # costume：空 spec / 全空值
    for bad in ({}, {"description": ""}, {"colors": []}):
        r = _mk_asset(
            client,
            token,
            pid,
            type_="costume",
            name="K",
            character_id=cid,
            spec=bad,
        )
        assert r.status_code == 422, (bad, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        assert err["details"]["violations"][0]["field"] == "spec"
    # scene：空 spec
    r = _mk_asset(client, token, pid, type_="scene", name="S", spec={})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_failed"


def test_t13_type_invalid_422(db, client):
    token = _register(client, "a13_laura")
    pid = _mk_project(client, token)
    for bad_type in ("prop", "CHARACTER", "", "Character"):
        r = _mk_asset(
            client, token, pid, type_=bad_type, name="X", spec={"appearance": "a"}
        )
        assert r.status_code == 422, (bad_type, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v0 = err["details"]["violations"][0]
        assert v0["field"] == "type" and v0["rule"] == "invalid"


def test_t14_name_invalid_422(db, client):
    token = _register(client, "a14_mallory")
    pid = _mk_project(client, token)
    for bad_name in ("", "   ", "\t\n"):
        r = _mk_asset(
            client, token, pid, type_="character", name=bad_name, spec={"appearance": "a"}
        )
        assert r.status_code == 422, (bad_name, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v0 = err["details"]["violations"][0]
        assert v0["field"] == "name" and v0["rule"] == "required"
        assert _counts()["assets"] == 0
    r = _mk_asset(
        client, token, pid, type_="character", name="a" * 121, spec={"appearance": "a"}
    )
    assert r.status_code == 422, r.text
    v0 = r.json()["error"]["details"]["violations"][0]
    assert v0["field"] == "name" and v0["rule"] == "max_length"
    # 边界：120 字符可建
    r2 = _mk_asset(
        client, token, pid, type_="character", name="b" * 120, spec={"appearance": "a"}
    )
    assert r2.status_code == 201, r2.text


def test_t15_body_type_invalid_422(db, client):
    token = _register(client, "a15_nancy")
    pid = _mk_project(client, token)
    for bad in ("bare", "Natural", "clothed "):
        r = _mk_asset(
            client,
            token,
            pid,
            type_="character",
            name="C",
            spec={"appearance": "a", "body_type": bad},
        )
        assert r.status_code == 422, (bad, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v0 = err["details"]["violations"][0]
        assert v0["field"] == "spec.body_type" and v0["rule"] == "invalid"
    # 合法值通过
    for ok in ("natural", "clothed"):
        r = _mk_asset(
            client,
            token,
            pid,
            type_="character",
            name=f"OK-{ok}",
            spec={"appearance": "a", "body_type": ok},
        )
        assert r.status_code == 201, (ok, r.text)


# ── 项目 404（不存在 / 非属主）─────────────────────────────────────────────

def test_t16_project_404(db, client):
    token = _register(client, "a16_olga")
    # 不存在的项目
    ghost = "01" * 13
    r = _mk_asset(
        client, token, ghost, type_="character", name="X", spec={"appearance": "a"}
    )
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "project", "id": ghost}
    # 非属主：B 的项目，A 创建 → 404（不泄漏存在性）
    tb = _register(client, "a16_paul")
    pid_b = _mk_project(client, tb, "BProj")
    r2 = _mk_asset(
        client, token, pid_b, type_="character", name="X", spec={"appearance": "a"}
    )
    assert r2.status_code == 404, r2.text
    assert r2.json()["error"]["details"] == {"kind": "project", "id": pid_b}
    assert _counts()["assets"] == 0  # 无副作用


# ── 幂等 / 负面恢复 ────────────────────────────────────────────────────────

def test_t17_replay_same_command_id_200_no_new_rows(db, client):
    token = _register(client, "a17_quinn")
    pid = _mk_project(client, token)
    cmd = _cmd()
    payload = {
        "type": "character",
        "name": "Replay",
        "spec": {"appearance": "a", "notes": "n"},
        "command_id": cmd,
    }
    r1 = client.post(
        f"/api/studio/projects/{pid}/assets", json=payload, headers=_auth(token)
    )
    assert r1.status_code == 201, r1.text
    first = r1.json()
    before = _counts()
    r2 = client.post(
        f"/api/studio/projects/{pid}/assets", json=payload, headers=_auth(token)
    )
    assert r2.status_code == 200, (r2.status_code, r2.text)  # 重放 = 200 非 201
    assert r2.json() == first  # 原样返回首次 result_payload
    after = _counts()
    assert after == before, f"重放产生副作用: {before} -> {after}"
    assert after["assets"] == 1 and after["revisions"] == 1


def test_t18_command_id_reused_422(db, client):
    token = _register(client, "a18_randy")
    pid = _mk_project(client, token)
    cmd = _cmd()
    r1 = client.post(
        f"/api/studio/projects/{pid}/assets",
        json={
            "type": "character",
            "name": "First",
            "spec": {"appearance": "a"},
            "command_id": cmd,
        },
        headers=_auth(token),
    )
    assert r1.status_code == 201, r1.text
    before = _counts()
    # 同 command_id、不同 spec
    r2 = client.post(
        f"/api/studio/projects/{pid}/assets",
        json={
            "type": "character",
            "name": "First",
            "spec": {"appearance": "DIFFERENT"},
            "command_id": cmd,
        },
        headers=_auth(token),
    )
    assert r2.status_code == 422, r2.text
    err = r2.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "command_id" and v0["rule"] == "reused"
    assert _counts() == before


def test_t19_no_occurrence_or_binding_writes(db, client):
    """本卡不写关系表，也不自动采用：创建后 occurrences/bindings 恒空。"""
    token = _register(client, "a19_susan")
    pid = _mk_project(client, token)
    rc = _mk_asset(
        client, token, pid, type_="character", name="C", spec={"appearance": "a"}
    )
    cid = rc.json()["object_ref"]["id"]
    _mk_asset(
        client,
        token,
        pid,
        type_="costume",
        name="K",
        character_id=cid,
        spec={"description": "d"},
    )
    _mk_asset(client, token, pid, type_="scene", name="S", spec={"location": "l"})
    c = _counts()
    # 命令记录 4 条：建项目 1 + 建资产 3（项目创建同样走 command_id 幂等）
    assert c["assets"] == 3 and c["revisions"] == 3 and c["commands"] == 4
    assert c["occurrences"] == 0 and c["bindings"] == 0
    # 每个身份的 versions 仅 1 条；无自动采用/确认
    with Session() as s:
        rows = s.scalars(select(AssetRevision)).all()
        assert all(rv.revision == 1 for rv in rows)
        assert all(rv.review_status == "unreviewed" for rv in rows)
        assert all(rv.media_artifact_id is None for rv in rows)


def test_t20_two_projects_isolated_ids_and_spec(db, client):
    """身份稳定：同名同规格在两个项目各建一条，互不覆盖（无去重逻辑）。"""
    token = _register(client, "a20_trent")
    p1 = _mk_project(client, token, "Iso1")
    p2 = _mk_project(client, token, "Iso2")
    r1 = _mk_asset(
        client, token, p1, type_="character", name="同名", spec={"appearance": "a"}
    )
    r2 = _mk_asset(
        client, token, p2, type_="character", name="同名", spec={"appearance": "a"}
    )
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["object_ref"]["id"] != r2.json()["object_ref"]["id"]
    with Session() as s:
        assert s.scalar(select(func.count(StudioProject.id))) == 2
        assert s.scalar(select(func.count(Asset.id))) == 2
