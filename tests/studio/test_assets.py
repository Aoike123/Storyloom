"""ASSET-01-a 创建人物身份（附录 05 §2 AS1）—— POST /projects/{pid}/assets。

覆盖：character/costume/scene 创建、costume→character 归属一致性、
spec 必填键与 body_type、type/name 非法、跨项目/无 token、command_id 幂等重放
（200 且不新增行）与 reused 422、无 occurrence/binding 副作用。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件不改写 STUDIO_* env。db fixture 照抄 tests/studio/test_projects.py。
"""
import json
import os
import re
import struct
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.core.db import Base, Session, engine
from backend.studio.assets.models import Asset, AssetRevision
from backend.studio.contracts.models import CommandRecord
from backend.studio.jobs.models import StudioJob
from backend.studio.media.models import MediaArtifact
from backend.studio.media.store import (
    artifact_path,
    media_root,
    store_media,
    verify_integrity,
)
from backend.studio.relations.models import AssetBinding, AssetOccurrence
from backend.studio.projects.models import StudioProject
from backend.studio.storyboard.models import Scene, Shot


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
        spec={
            "location": "城郊旧仓库",
            "description": "堆满木箱的旧仓库",
            "time_of_day": "night",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "scene"
    assert body["character"] is None
    assert body["current_revision"]["spec"] == {
        "location": "城郊旧仓库",
        "description": "堆满木箱的旧仓库",
        "time_of_day": "night",
    }
    assert body["current_revision"]["media_artifact_id"] is None


def test_t5_custom_spec_keys_allowed_costume_and_scene(db, client):
    """附录 05 §1：必填键齐备时允许自由扩展键；扩展键原样存取。"""
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
        spec={"description": "丝绸长袍", "fabric": "silk"},
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["current_revision"]["spec"]["fabric"] == "silk"
    r2 = _mk_asset(
        client,
        token,
        pid,
        type_="scene",
        name="自定义场景",
        spec={"location": "地下室", "description": "潮湿的地下室", "mood": "压抑"},
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["current_revision"]["spec"]["mood"] == "压抑"


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
        spec={"location": "l", "description": "d"},
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
        client,
        token,
        pid,
        type_="scene",
        name="S",
        spec={"location": "l", "description": "d"},
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
        assert v0["field"] == "spec.appearance" and v0["rule"] == "required"
    # appearance 非字符串
    r = _mk_asset(
        client, token, pid, type_="character", name="C", spec={"appearance": 3}
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_failed"


def test_t12_required_spec_keys_missing_422(db, client):
    """附录 05 §1 必填键（2026-10-13 主模型裁定按附录收紧，非"任意非空键"）：

    costume 必填 description；scene 必填 location + description。
    缺失/非字符串/全空白均 422 validation_failed，逐键报 spec.<key>/required。
    """
    token = _register(client, "a12_karen")
    pid = _mk_project(client, token)
    rc = _mk_asset(
        client, token, pid, type_="character", name="C", spec={"appearance": "x"}
    )
    cid = rc.json()["object_ref"]["id"]
    # costume：description 缺失 / 空串 / 全空白 / 非字符串 —— 只有扩展键也不算数
    for bad in (
        {},
        {"description": ""},
        {"description": "   "},
        {"description": 7},
        {"fabric": "silk"},
        {"colors": ["black"]},
    ):
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
        assert err["details"]["violations"] == [
            {
                "field": "spec.description",
                "rule": "required",
                "message": "服装规格必含非空 description（字符串）。",
            }
        ], (bad, err)
    # scene：location 与 description 均必填，缺席逐个报出
    cases = (
        ({}, {"spec.location", "spec.description"}),
        ({"location": "l"}, {"spec.description"}),
        ({"description": "d"}, {"spec.location"}),
        ({"location": "  ", "description": ""}, {"spec.location", "spec.description"}),
        ({"time_of_day": "night"}, {"spec.location", "spec.description"}),
    )
    for bad, missing in cases:
        r = _mk_asset(client, token, pid, type_="scene", name="S", spec=bad)
        assert r.status_code == 422, (bad, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        fields = {v["field"] for v in err["details"]["violations"]}
        assert fields == missing, (bad, err)
        assert all(v["rule"] == "required" for v in err["details"]["violations"])

    # 必填键齐备即通过（含扩展键）
    ok = _mk_asset(
        client,
        token,
        pid,
        type_="scene",
        name="S",
        spec={"location": "l", "description": "d", "weather": "rain"},
    )
    assert ok.status_code == 201, ok.text


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
    _mk_asset(
        client,
        token,
        pid,
        type_="scene",
        name="S",
        spec={"location": "l", "description": "d"},
    )
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


# ── AS10 costume-required（ASSET-01-b：bare 模式明确无需图）─────────────────

def _mk_character(client, token, pid, name="人物X") -> str:
    r = _mk_asset(
        client, token, pid, type_="character", name=name, spec={"appearance": "a"}
    )
    assert r.status_code == 201, r.text
    return r.json()["object_ref"]["id"]


def _costume_required(client, pid, aid, *, value, expected_revision=1,
                      command_id=None, token=None):
    body = {
        "value": value,
        "expected_revision": expected_revision,
        "command_id": command_id or _cmd(),
    }
    headers = _auth(token) if token is not None else {}
    return client.post(
        f"/api/studio/projects/{pid}/assets/{aid}/costume-required",
        json=body,
        headers=headers,
    )


def test_t21_set_false_then_true_200(db, client):
    """200 翻转 true→false→true：响应恰 2 字段、DB 落库、updated_at 推进。"""
    token = _register(client, "a21_alice")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)

    with Session() as s:
        before = s.get(Asset, aid)
        assert before.costume_required is True
        updated0 = before.updated_at

    # true → false（bare 模式明确无需图）
    r1 = _costume_required(client, pid, aid, value=False, token=token)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert set(body1) == {"object_ref", "costume_required"}  # 恰 2 字段
    assert body1["object_ref"] == {"kind": "asset", "id": aid, "revision": 1}
    assert body1["costume_required"] is False
    with Session() as s:
        a = s.get(Asset, aid)
        assert a.costume_required is False
        assert a.updated_at > updated0  # 落写推进 updated_at
        updated1 = a.updated_at

    # false → true（改回）
    r2 = _costume_required(client, pid, aid, value=True, token=token)
    assert r2.status_code == 200, r2.text
    assert r2.json() == {
        "object_ref": {"kind": "asset", "id": aid, "revision": 1},
        "costume_required": True,
    }
    with Session() as s:
        a = s.get(Asset, aid)
        assert a.costume_required is True
        assert a.updated_at > updated1


def test_t22_same_value_explicit_confirm_no_revision_side_effects(db, client):
    """value 与现值相同也算成功（显式确认语义），照常落写（updated_at 推进）；
    无新 AssetRevision 行、current_revision_id 不变、review_status 不变。"""
    token = _register(client, "a22_bob")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    with Session() as s:
        a0 = s.get(Asset, aid)
        current0 = a0.current_revision_id
        updated0 = a0.updated_at
    counts0 = _counts()

    r = _costume_required(client, pid, aid, value=True, token=token)  # 同值
    assert r.status_code == 200, r.text
    assert r.json()["costume_required"] is True
    with Session() as s:
        a = s.get(Asset, aid)
        assert a.costume_required is True
        assert a.current_revision_id == current0  # 不动 current 指针
        assert a.updated_at > updated0  # 照常落写
        rev = s.get(AssetRevision, current0)
        assert rev.review_status == "unreviewed"  # 不动 review_status
    counts1 = _counts()
    # 只多 1 条 CommandRecord；assets/revisions 行数不变，无 occurrence/binding
    assert counts1["revisions"] == counts0["revisions"]
    assert counts1["assets"] == counts0["assets"]
    assert counts1["commands"] == counts0["commands"] + 1
    assert counts1["occurrences"] == 0 and counts1["bindings"] == 0


def test_t23_non_character_422_not_character(db, client):
    """costume / scene 调用 → 422 precondition_failed reason=not_character，零副作用。"""
    token = _register(client, "a23_carol")
    pid = _mk_project(client, token)
    cid = _mk_character(client, token, pid)
    rcost = _mk_asset(
        client, token, pid, type_="costume", name="K", character_id=cid,
        spec={"description": "d"},
    )
    rscene = _mk_asset(
        client, token, pid, type_="scene", name="S",
        spec={"location": "l", "description": "d"},
    )
    counts0 = _counts()
    for r_asset in (rcost, rscene):
        aid = r_asset.json()["object_ref"]["id"]
        r = _costume_required(client, pid, aid, value=False, token=token)
        assert r.status_code == 422, r.text
        err = r.json()["error"]
        assert err["code"] == "precondition_failed"
        assert err["details"]["reason"] == "not_character"
        assert err["details"]["blocked_by"] == {
            "kind": "asset", "id": aid, "revision": 1,
        }
    assert _counts() == counts0  # 零副作用（无命令记录落库）


def test_t24_expected_revision_mismatch_409(db, client):
    """expected_revision 错误 → 409 revision_conflict，actual=真实当前 revision。"""
    token = _register(client, "a24_dave")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    counts0 = _counts()
    for bad_expected in (0, 2, 99):
        r = _costume_required(
            client, pid, aid, value=False, expected_revision=bad_expected,
            token=token,
        )
        assert r.status_code == 409, (bad_expected, r.text)
        err = r.json()["error"]
        assert err["code"] == "revision_conflict"
        assert err["details"]["object"] == {"kind": "asset", "id": aid}
        assert err["details"]["expected"] == bad_expected
        assert err["details"]["actual"] == 1  # 真实当前 revision
    assert _counts() == counts0
    with Session() as s:
        assert s.get(Asset, aid).costume_required is True  # 未写


def test_t25_asset_and_project_404_same_shape(db, client):
    """资产 404：幽灵 id 与跨项目 id 同形状；项目 404：幽灵与非属主同形状。"""
    token = _register(client, "a25_erin")
    pid = _mk_project(client, token)
    pid_b = _mk_project(client, token, "B")
    aid_b = _mk_character(client, token, pid_b)  # B 项目的资产

    # 资产不存在 / 跨项目 → 同形状 404（不泄漏存在性）
    for ghost in ("01" * 13, aid_b):
        r = _costume_required(client, pid, ghost, value=False, token=token)
        assert r.status_code == 404, (ghost, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"] == {"kind": "asset", "id": ghost}

    # 项目不存在 / 非属主 → 同形状 404
    other = _register(client, "a25_frank")
    pid_c = _mk_project(client, other, "C")
    for ghost_pid in ("02" * 13, pid_c):
        r = _costume_required(client, ghost_pid, "03" * 13, value=False, token=token)
        assert r.status_code == 404, (ghost_pid, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"] == {"kind": "project", "id": ghost_pid}


def test_t26_no_token_401(db, client):
    r = _costume_required(client, "01" * 13, "02" * 13, value=False)
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t27_value_not_strict_bool_and_missing_fields_422(db, client):
    """value 非严格布尔（"false"/1/null）→ 422；缺字段 → 422；均零副作用。"""
    token = _register(client, "a27_grace")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    counts0 = _counts()
    for bad_value in ("false", 1, None):
        r = _costume_required(client, pid, aid, value=bad_value, token=token)
        assert r.status_code == 422, (bad_value, r.text)
    # 缺字段（value / expected_revision / command_id 均必填）
    full = {"value": False, "expected_revision": 1, "command_id": _cmd()}
    for drop in ("value", "expected_revision", "command_id"):
        body = {k: v for k, v in full.items() if k != drop}
        r = client.post(
            f"/api/studio/projects/{pid}/assets/{aid}/costume-required",
            json=body, headers=_auth(token),
        )
        assert r.status_code == 422, (drop, r.text)
    assert _counts() == counts0  # 请求体形态校验拦截，零副作用
    with Session() as s:
        assert s.get(Asset, aid).costume_required is True


def test_t28_replay_200_and_reused_422(db, client):
    """幂等：同 command_id 同内容 → 200 同 payload、commands 行数不变、
    updated_at 不二次推进；同 command_id 不同 value → 422 reused 零副作用。"""
    token = _register(client, "a28_heidi")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    cmd = _cmd()

    r1 = _costume_required(client, pid, aid, value=False, command_id=cmd, token=token)
    assert r1.status_code == 200, r1.text
    first = r1.json()
    with Session() as s:
        updated1 = s.get(Asset, aid).updated_at
    counts0 = _counts()

    # 重放：同 command_id 同内容（AS10 不推进 revision，expected=1 仍成立）
    r2 = _costume_required(client, pid, aid, value=False, command_id=cmd, token=token)
    assert r2.status_code == 200, (r2.status_code, r2.text)
    assert r2.json() == first  # 原样返回首次 result_payload
    assert _counts() == counts0  # 零写：无新 CommandRecord
    with Session() as s:
        assert s.get(Asset, aid).updated_at == updated1  # 不二次推进

    # 同 command_id 不同 value → 422 validation_failed(field=command_id, rule=reused)
    r3 = _costume_required(client, pid, aid, value=True, command_id=cmd, token=token)
    assert r3.status_code == 422, r3.text
    err = r3.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "command_id" and v0["rule"] == "reused"
    assert _counts() == counts0
    with Session() as s:
        a = s.get(Asset, aid)
        assert a.costume_required is False  # 零副作用
        assert a.updated_at == updated1


# ── ASSET-01-c 物理场景资产 vs 叙事场（studio_scenes）分离 ──────────────────

def _all_keys(obj):
    """DTO 内所有键（递归 dict/list），用于"无 scene_id"断言。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_keys(item)


def test_t29_scene_optional_keys_type_validation(db, client):
    """scene 可选键 time_of_day/weather：非 None 必须 string（rule=invalid）；
    None/字符串通过；自由扩展键仍允许。character 可选键行为不变（回归）。"""
    token = _register(client, "a29_iris")
    pid = _mk_project(client, token)
    # 非字符串 → 422 validation_failed rule=invalid（逐个报出）
    for bad in (123, True, {}):
        r = _mk_asset(
            client, token, pid, type_="scene", name="S1",
            spec={"location": "l", "description": "d", "time_of_day": bad},
        )
        assert r.status_code == 422, (bad, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v = err["details"]["violations"][0]
        assert v["field"] == "spec.time_of_day" and v["rule"] == "invalid"
        assert len(err["details"]["violations"]) == 1  # 必填键齐备，仅报可选键
    for bad in (123, True, {}):
        r = _mk_asset(
            client, token, pid, type_="scene", name="S2",
            spec={"location": "l", "description": "d", "weather": bad},
        )
        assert r.status_code == 422, (bad, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v = err["details"]["violations"][0]
        assert v["field"] == "spec.weather" and v["rule"] == "invalid"
    # None 或字符串 → 通过
    r = _mk_asset(
        client, token, pid, type_="scene", name="S3",
        spec={"location": "l", "description": "d", "time_of_day": None, "weather": "rain"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["current_revision"]["spec"] == {
        "location": "l", "description": "d", "time_of_day": None, "weather": "rain",
    }
    # 两键同时非法 → 两条 violation 一次报出
    r = _mk_asset(
        client, token, pid, type_="scene", name="S4",
        spec={"location": "l", "description": "d", "time_of_day": 1, "weather": 2},
    )
    assert r.status_code == 422, r.text
    fields = {v["field"] for v in r.json()["error"]["details"]["violations"]}
    assert fields == {"spec.time_of_day", "spec.weather"}
    # 自由扩展键（未声明键）仍允许
    r = _mk_asset(
        client, token, pid, type_="scene", name="S5",
        spec={"location": "l", "description": "d", "mood": "压抑"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["current_revision"]["spec"]["mood"] == "压抑"
    # 回归：character 可选键校验行为不变（notes 非字符串 422；合法组合 201）
    r = _mk_asset(
        client, token, pid, type_="character", name="C",
        spec={"appearance": "a", "notes": 3},
    )
    assert r.status_code == 422, r.text
    v = r.json()["error"]["details"]["violations"][0]
    assert v["field"] == "spec.notes" and v["rule"] == "invalid"
    r = _mk_asset(
        client, token, pid, type_="character", name="C",
        spec={"appearance": "a", "body_type": "natural", "height_cm": 170, "notes": "n"},
    )
    assert r.status_code == 201, r.text


def test_t30_create_scene_asset_writes_no_storyboard_rows(db, client):
    """创建 scene 资产后，叙事场/镜头表（studio_scenes/studio_shots）零行：
    物理场景资产只写 assets/asset_revisions/command_records。"""
    token = _register(client, "a30_jack")
    pid = _mk_project(client, token)
    r = _mk_asset(
        client, token, pid, type_="scene", name="旧仓库",
        spec={"location": "城郊旧仓库", "description": "堆满木箱", "time_of_day": "night"},
    )
    assert r.status_code == 201, r.text
    with Session() as s:
        assert s.scalar(select(func.count(Scene.id))) == 0
        assert s.scalar(select(func.count(Shot.id))) == 0
        c = _counts()
        assert c["assets"] == 1 and c["revisions"] == 1


def test_t31_narrative_scene_rows_do_not_leak_into_asset_dto(db, client):
    """分离性：叙事场行（直接向 studio_scenes 插行）不干扰资产——
    资产 DTO 键集合恰为冻结 8 键、无任何 scene_id；scene 与 character
    DTO 同键集合；叙事场行原样保留、不被资产命令触碰。"""
    token = _register(client, "a31_karl")
    pid = _mk_project(client, token)
    # 直接向叙事场表插一行（模拟已存在的叙事场）
    narrative_id = "04" * 13
    with Session() as s:
        s.add(Scene(id=narrative_id, project_id=pid, name="叙事场一", seq=1))
        s.commit()
    with Session() as s:
        assert s.scalar(select(func.count(Scene.id))) == 1

    r_scene = _mk_asset(
        client, token, pid, type_="scene", name="物理场景A",
        spec={"location": "l", "description": "d", "weather": "rain"},
    )
    assert r_scene.status_code == 201, r_scene.text
    scene_body = r_scene.json()
    r_char = _mk_asset(
        client, token, pid, type_="character", name="人物A",
        spec={"appearance": "a"},
    )
    assert r_char.status_code == 201, r_char.text
    char_body = r_char.json()

    # DTO 键集合恰为冻结 8 键，且 scene 与 character 同形状（同键集合）
    expected = {
        "object_ref", "type", "name", "character",
        "current_revision", "versions", "occurrences", "bindings",
    }
    assert set(scene_body) == expected
    assert set(char_body) == expected
    # 无任何 scene_id（递归）：DTO 不携带叙事场引用
    assert "scene_id" not in set(_all_keys(scene_body))
    assert "scene_id" not in set(_all_keys(char_body))
    # 叙事场 id 不出现在任何 DTO 值中
    assert narrative_id not in json.dumps(scene_body)
    assert narrative_id not in json.dumps(char_body)
    # 资产命令未触碰叙事场表：行原样保留
    with Session() as s:
        row = s.get(Scene, narrative_id)
        assert row is not None and row.name == "叙事场一" and row.seq == 1
        assert s.scalar(select(func.count(Shot.id))) == 0


# ── ASSET-01-d 修订规格（AS4）POST /assets/{aid}/spec ───────────────────────

def _revise_spec(client, pid, aid, *, spec, expected_revision,
                 command_id=None, token=None):
    body = {
        "spec": spec,
        "expected_revision": expected_revision,
        "command_id": command_id or _cmd(),
    }
    headers = _auth(token) if token is not None else {}
    return client.post(
        f"/api/studio/projects/{pid}/assets/{aid}/spec",
        json=body,
        headers=headers,
    )


def _mk_scene(client, token, pid, name="场景X"):
    r = _mk_asset(
        client, token, pid, type_="scene", name=name,
        spec={"location": "l", "description": "d"},
    )
    assert r.status_code == 201, r.text
    return r.json()["object_ref"]["id"]


def test_t32_revise_spec_200_new_revision(db, client):
    """AS4 成功 200：新 revision=当前+1、DTO 8 键、versions[] 含全部版本；
    直查库：继承 media/generation、review_status=unreviewed、current 指向
    新行、旧 revision 行逐字段不变、occurrences/bindings 行数不变。"""
    token = _register(client, "a32_mona")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)

    # 初始状态：revision=1，media/generation 恒 NULL
    with Session() as s:
        old_rev = s.scalars(
            select(AssetRevision).where(AssetRevision.asset_id == aid)
        ).first()
        assert old_rev.revision == 1
        old_rev_id = old_rev.id
        old_spec_raw = old_rev.spec
        old_media = old_rev.media_artifact_id  # NULL
        old_gen = old_rev.generation_input  # NULL
        old_review = old_rev.review_status
        old_created = old_rev.created_at
        old_asset = s.get(Asset, aid)
        old_current_id = old_asset.current_revision_id
        old_updated = old_asset.updated_at
    counts0 = _counts()

    new_spec = {"appearance": "新外貌", "body_type": "clothed", "height_cm": 175}
    r = _revise_spec(client, pid, aid, spec=new_spec, expected_revision=1, token=token)
    assert r.status_code == 200, r.text
    body = r.json()

    # DTO 8 键
    expected = {
        "object_ref", "type", "name", "character",
        "current_revision", "versions", "occurrences", "bindings",
    }
    assert set(body) == expected
    assert body["object_ref"] == {"kind": "asset", "id": aid, "revision": 2}
    assert body["current_revision"] == {
        "revision": 2, "spec": new_spec,
        "media_artifact_id": None, "review_status": "unreviewed",
    }
    # versions[] 含全部并存版本（revision 升序）
    assert [v["revision"] for v in body["versions"]] == [1, 2]
    assert body["versions"][1]["spec"] == new_spec
    assert body["occurrences"] == [] and body["bindings"] == []

    # 直查库：新 revision 行
    with Session() as s:
        new_rev = s.scalars(
            select(AssetRevision).where(
                AssetRevision.asset_id == aid, AssetRevision.revision == 2
            )
        ).first()
        assert new_rev is not None
        assert json.loads(new_rev.spec) == new_spec
        # 继承上一 revision 的 media/generation（本阶段恒 NULL）
        assert new_rev.media_artifact_id == old_media
        assert new_rev.generation_input == old_gen
        assert new_rev.review_status == "unreviewed"
        # current 指向新行
        asset = s.get(Asset, aid)
        assert asset.current_revision_id == new_rev.id
        assert asset.updated_at >= old_updated
        # 旧 revision 行逐字段不变
        old_row = s.get(AssetRevision, old_rev_id)
        assert old_row.revision == 1
        assert old_row.spec == old_spec_raw
        assert old_row.media_artifact_id == old_media
        assert old_row.generation_input == old_gen
        assert old_row.review_status == old_review
        assert old_row.created_at == old_created
        # occurrences/bindings 行数不变
        assert s.scalar(select(func.count(AssetOccurrence.id))) == counts0["occurrences"]
        assert s.scalar(select(func.count(AssetBinding.id))) == counts0["bindings"]
    # 恰新增 1 个 revision 行
    assert _counts()["revisions"] == counts0["revisions"] + 1


def test_t33_expected_revision_stale_409(db, client):
    """expected_revision 过期（正整数但≠当前）→ 409 revision_conflict，无新 revision 行。"""
    token = _register(client, "a33_nina")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    counts0 = _counts()
    for bad_expected in (2, 99):
        r = _revise_spec(
            client, pid, aid, spec={"appearance": "x"},
            expected_revision=bad_expected, token=token,
        )
        assert r.status_code == 409, (bad_expected, r.text)
        err = r.json()["error"]
        assert err["code"] == "revision_conflict"
        assert err["details"]["object"] == {"kind": "asset", "id": aid}
        assert err["details"]["expected"] == bad_expected
        assert err["details"]["actual"] == 1  # 真实当前 revision
    assert _counts() == counts0  # 无新 revision 行
    with Session() as s:
        assert s.scalar(
            select(func.count(AssetRevision.id)).where(AssetRevision.asset_id == aid)
        ) == 1


def test_t34_field_validation_422(db, client):
    """spec 缺失/非 object、expected_revision 非正整数、请求体含
    name/type/character_id → 422；均零副作用。"""
    token = _register(client, "a34_olga")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    counts0 = _counts()

    # spec 非 object（字符串）→ 422（请求体形态校验）
    r = client.post(
        f"/api/studio/projects/{pid}/assets/{aid}/spec",
        json={"spec": "not-a-dict", "expected_revision": 1, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text
    # spec 缺失 → 422（请求体形态校验）
    r = client.post(
        f"/api/studio/projects/{pid}/assets/{aid}/spec",
        json={"expected_revision": 1, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text

    # expected_revision 非正整数（0）→ 422 validation_failed
    r = _revise_spec(client, pid, aid, spec={"appearance": "x"}, expected_revision=0, token=token)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "expected_revision" and v["rule"] == "invalid"

    # 请求体含 name/type/character_id → 422 validation_failed(rule=forbidden)
    for forbidden_field in ("name", "type", "character_id"):
        body = {
            "spec": {"appearance": "x"},
            "expected_revision": 1,
            "command_id": _cmd(),
            forbidden_field: "v",
        }
        r = client.post(
            f"/api/studio/projects/{pid}/assets/{aid}/spec",
            json=body, headers=_auth(token),
        )
        assert r.status_code == 422, (forbidden_field, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v = err["details"]["violations"][0]
        assert v["field"] == forbidden_field and v["rule"] == "forbidden"

    assert _counts() == counts0  # 零副作用


def test_t35_spec_type_validation_422(db, client):
    """spec 类型校验失败（character 缺 appearance、scene 缺 location、
    可选键类型错）→ 422 violations 逐个报出，无新 revision。"""
    token = _register(client, "a35_paul")
    pid = _mk_project(client, token)
    char_id = _mk_character(client, token, pid)
    scene_id = _mk_scene(client, token, pid)
    counts0 = _counts()

    # character 缺 appearance → 422 violation(field=spec.appearance, rule=required)
    r = _revise_spec(client, pid, char_id, spec={"body_type": "clothed"}, expected_revision=1, token=token)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "spec.appearance" and v["rule"] == "required"

    # scene 缺 location → 422 violation(field=spec.location, rule=required)
    r = _revise_spec(client, pid, scene_id, spec={"description": "d"}, expected_revision=1, token=token)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    v = err["details"]["violations"][0]
    assert v["field"] == "spec.location" and v["rule"] == "required"

    # scene 可选键类型错（time_of_day 非字符串）→ 422 violation(rule=invalid)
    r = _revise_spec(
        client, pid, scene_id,
        spec={"location": "l", "description": "d", "time_of_day": 123},
        expected_revision=1, token=token,
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    v = err["details"]["violations"][0]
    assert v["field"] == "spec.time_of_day" and v["rule"] == "invalid"

    assert _counts() == counts0  # 无新 revision


def test_t36_ghost_and_cross_project_404_and_401(db, client):
    """幽灵资产与跨项目资产同形状 404；项目 404；未认证 401。"""
    token = _register(client, "a36_quinn")
    pid = _mk_project(client, token)
    pid_b = _mk_project(client, token, "B")
    aid_b = _mk_character(client, token, pid_b)  # B 项目的资产

    # 资产不存在 / 跨项目 → 同形状 404（不泄漏存在性）
    for ghost in ("01" * 13, aid_b):
        r = _revise_spec(
            client, pid, ghost, spec={"appearance": "a"},
            expected_revision=1, token=token,
        )
        assert r.status_code == 404, (ghost, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"] == {"kind": "asset", "id": ghost}

    # 项目不存在 / 非属主 → 同形状 404
    other = _register(client, "a36_rhea")
    pid_c = _mk_project(client, other, "C")
    for ghost_pid in ("02" * 13, pid_c):
        r = _revise_spec(
            client, ghost_pid, "03" * 13, spec={"appearance": "a"},
            expected_revision=1, token=token,
        )
        assert r.status_code == 404, (ghost_pid, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"] == {"kind": "project", "id": ghost_pid}

    # 未认证 → 401
    r = _revise_spec(client, "01" * 13, "02" * 13, spec={"appearance": "a"}, expected_revision=1)
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t37_replay_200_and_reused_422(db, client):
    """幂等：同 command_id 同 payload → 200 同响应、零新写；
    同 command_id 不同 payload → 422 rule=reused。"""
    token = _register(client, "a37_sara")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    cmd = _cmd()
    spec1 = {"appearance": "v1"}

    r1 = _revise_spec(client, pid, aid, spec=spec1, expected_revision=1, command_id=cmd, token=token)
    assert r1.status_code == 200, r1.text
    first = r1.json()
    counts0 = _counts()

    # 重放：同 command_id 同 payload（expected_revision 仍为首次值 1）
    r2 = _revise_spec(client, pid, aid, spec=spec1, expected_revision=1, command_id=cmd, token=token)
    assert r2.status_code == 200, (r2.status_code, r2.text)
    assert r2.json() == first  # 原样返回首次 result_payload
    assert _counts() == counts0  # 零写：无新 CommandRecord/revision

    # 同 command_id 不同 payload → 422 validation_failed(field=command_id, rule=reused)
    r3 = _revise_spec(client, pid, aid, spec={"appearance": "v2"}, expected_revision=2, command_id=cmd, token=token)
    assert r3.status_code == 422, r3.text
    err = r3.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "command_id" and v0["rule"] == "reused"
    assert _counts() == counts0


def test_t38_multiple_revisions_versions_all(db, client):
    """连续两次修订：versions[] 含全部并存版本（3 版本），current 指向最新。"""
    token = _register(client, "a38_tara")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)

    r1 = _revise_spec(client, pid, aid, spec={"appearance": "v1"}, expected_revision=1, token=token)
    assert r1.status_code == 200, r1.text
    r2 = _revise_spec(client, pid, aid, spec={"appearance": "v2"}, expected_revision=2, token=token)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    # versions[] 含全部 3 版本（revision 升序）
    assert [v["revision"] for v in body["versions"]] == [1, 2, 3]
    assert body["versions"][2]["spec"] == {"appearance": "v2"}
    assert body["object_ref"]["revision"] == 3
    assert body["current_revision"]["revision"] == 3
    # 直查库：3 个 revision 行
    with Session() as s:
        assert s.scalar(
            select(func.count(AssetRevision.id)).where(AssetRevision.asset_id == aid)
        ) == 3


def test_t39_costume_spec_revision_no_attribution_check(db, client):
    """costume 资产的 spec 修订不校验归属（归属是创建期事实；修订只校验
    自身 spec 形状）。"""
    token = _register(client, "a39_uma")
    pid = _mk_project(client, token)
    char_id = _mk_character(client, token, pid)
    # 创建 costume（归属 char_id）
    r = _mk_asset(
        client, token, pid, type_="costume", name="服装X",
        spec={"description": "d"}, character_id=char_id,
    )
    assert r.status_code == 201, r.text
    costume_id = r.json()["object_ref"]["id"]

    # costume spec 修订：只校验自身 spec 形状（description 必填），不重校验归属
    new_spec = {"description": "新描述", "colors": ["red"]}
    r = _revise_spec(client, pid, costume_id, spec=new_spec, expected_revision=1, token=token)
    assert r.status_code == 200, r.text
    assert r.json()["current_revision"]["spec"] == new_spec

    # costume 缺 description → 422（校验自身 spec 形状）
    r = _revise_spec(client, pid, costume_id, spec={"colors": ["red"]}, expected_revision=2, token=token)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    v = err["details"]["violations"][0]
    assert v["field"] == "spec.description" and v["rule"] == "required"


# ── ASSET-02-a 上传并登记媒体版本（AS5）POST /assets/{aid}/media ────────────

_MEDIA_DTO_KEYS = {
    "object_ref", "type", "name", "character",
    "current_revision", "versions", "occurrences", "bindings",
}


def _wav_bytes(seconds: float = 0.1, rate: int = 8000) -> bytes:
    """构造极小的真实 PCM WAV（16bit mono 静音）：MEDIA-01 的 audio 探测可解析。

    不依赖 ffmpeg 生成，测试快且确定（duration≈100ms）。
    """
    payload = b"\x00\x00" * int(rate * seconds)
    header = b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(payload))
    return header + payload


def _upload_media(client, pid, aid, *, data, filename="clip.wav", token=None,
                  command_id=None, expected_revision=None,
                  content_type="audio/wav"):
    """POST /assets/{aid}/media（multipart/form-data，文件字段名 file）。"""
    form = {"command_id": command_id or _cmd()}
    if expected_revision is not None:
        form["expected_revision"] = str(expected_revision)
    headers = _auth(token) if token is not None else {}
    return client.post(
        f"/api/studio/projects/{pid}/assets/{aid}/media",
        files={"file": (filename, data, content_type)},
        data=form,
        headers=headers,
    )


def _media_files() -> set:
    """私有媒体根下全部文件（含隐藏临时文件）的绝对路径集合。"""
    root = media_root()
    found: set = set()
    if not root.exists():
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            found.add(os.path.join(dirpath, name))
    return found


def _media_row_count() -> int:
    with Session() as s:
        return s.scalar(select(func.count(MediaArtifact.id)))


def _revision_snapshot(s, aid) -> list[tuple]:
    """资产全部 revision 行的逐字段快照（按 revision 升序），用于不可变断言。"""
    rows = s.scalars(
        select(AssetRevision)
        .where(AssetRevision.asset_id == aid)
        .order_by(AssetRevision.revision)
    ).all()
    return [
        (
            r.id, r.revision, r.spec, r.media_artifact_id,
            r.generation_input, r.review_status, r.created_at,
        )
        for r in rows
    ]


def test_t40_media_upload_no_token_401(db, client):
    """无 token 上传 → 401 unauthenticated（鉴权先于一切业务校验）。"""
    r = _upload_media(client, "01" * 13, "02" * 13, data=_wav_bytes(), token=None)
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t41_media_upload_success_201(db, client):
    """成功上传：201 + 8 键 DTO、新 revision=current+1、媒体为新登记行、
    review_status=unreviewed、spec/generation 继承、落盘可校验完整性。"""
    token = _register(client, "a40_vera")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    with Session() as s:
        snapshot0 = _revision_snapshot(s, aid)
        old_spec_text = snapshot0[0][2]
    counts0 = _counts()
    files0 = _media_files()
    data = _wav_bytes()

    r = _upload_media(
        client, pid, aid, data=data, filename="Voice.WAV",
        token=token, expected_revision=1,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == _MEDIA_DTO_KEYS
    assert body["object_ref"] == {"kind": "asset", "id": aid, "revision": 2}
    assert body["current_revision"]["revision"] == 2
    assert body["current_revision"]["spec"] == json.loads(old_spec_text)
    assert body["current_revision"]["review_status"] == "unreviewed"
    media_id = body["current_revision"]["media_artifact_id"]
    assert media_id is not None
    assert [v["revision"] for v in body["versions"]] == [1, 2]
    assert body["versions"][0]["media_artifact_id"] is None
    assert body["versions"][1]["media_artifact_id"] == media_id
    assert body["occurrences"] == [] and body["bindings"] == []

    with Session() as s:
        artifact = s.get(MediaArtifact, media_id)
        assert artifact is not None
        assert artifact.project_id == pid
        assert artifact.media_kind == "audio"
        assert artifact.source == "upload" and artifact.source_job_id is None
        assert artifact.filename == f"{pid}/{media_id}.wav"  # 扩展名归一化小写
        assert artifact.size_bytes == len(data)
        assert verify_integrity(artifact) is True
        path = artifact_path(artifact)
        assert path.is_file()
        assert path == (media_root() / artifact.filename).resolve()
        assert path.is_relative_to((media_root() / pid).resolve())

        new_row = s.get(AssetRevision, s.get(Asset, aid).current_revision_id)
        assert new_row.revision == 2
        assert new_row.media_artifact_id == media_id
        assert new_row.spec == old_spec_text  # 继承上一 revision 的 spec 原样
        assert new_row.generation_input is None  # 继承（本阶段恒 NULL）
        assert new_row.review_status == "unreviewed"
        assert s.get(Asset, aid).current_revision_id == new_row.id
        # 旧 revision 行逐字段不可变
        assert _revision_snapshot(s, aid)[0] == snapshot0[0]

    counts1 = _counts()
    assert counts1["assets"] == counts0["assets"]
    assert counts1["revisions"] == counts0["revisions"] + 1
    assert counts1["commands"] == counts0["commands"] + 1
    assert counts1["occurrences"] == 0 and counts1["bindings"] == 0
    with Session() as s:
        assert s.scalar(select(func.count(StudioJob.id))) == 0  # 零生成
    assert len(_media_files()) == len(files0) + 1  # 恰好新增 1 个落盘文件


def test_t42_media_upload_corrupt_422_no_residue(db, client):
    """损坏文件（随机字节 .mp4 / 空文件）→ 422，且零新 revision、零媒体行、
    零残留文件（含临时文件），原版本保留。"""
    token = _register(client, "a41_walt")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    counts0 = _counts()
    files0 = _media_files()
    media0 = _media_row_count()
    with Session() as s:
        snapshot0 = _revision_snapshot(s, aid)
        current0 = s.get(Asset, aid).current_revision_id

    r = _upload_media(
        client, pid, aid, data=os.urandom(4096), filename="broken.mp4",
        token=token, content_type="video/mp4",
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "file" and v["rule"] == "invalid"

    # 空文件 → 同样 422（MEDIA-01 尺寸校验），零副作用
    r = _upload_media(client, pid, aid, data=b"", filename="empty.wav", token=token)
    assert r.status_code == 422, r.text

    assert _counts() == counts0
    assert _media_row_count() == media0
    assert _media_files() == files0  # 无残留（含临时文件）
    with Session() as s:
        assert _revision_snapshot(s, aid) == snapshot0  # 原版本保留
        assert s.get(Asset, aid).current_revision_id == current0


def test_t43_media_upload_preserves_prior_versions(db, client):
    """已有 2 个版本时上传 → revision=3、versions[] 含全部 3 版；旧行不可变。"""
    token = _register(client, "a42_xena")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    r = _revise_spec(
        client, pid, aid, spec={"appearance": "v2"}, expected_revision=1, token=token
    )
    assert r.status_code == 200, r.text
    with Session() as s:
        snapshot0 = _revision_snapshot(s, aid)
    counts0 = _counts()

    r = _upload_media(client, pid, aid, data=_wav_bytes(), token=token, expected_revision=2)
    assert r.status_code == 201, r.text
    body = r.json()
    assert [v["revision"] for v in body["versions"]] == [1, 2, 3]
    assert body["versions"][0]["spec"] == {"appearance": "a"}
    assert body["versions"][1]["spec"] == {"appearance": "v2"}
    assert body["current_revision"]["spec"] == {"appearance": "v2"}  # 继承 rev2
    assert all(v["media_artifact_id"] is None for v in body["versions"][:2])
    assert body["object_ref"]["revision"] == 3

    with Session() as s:
        snapshot1 = _revision_snapshot(s, aid)
    assert snapshot1[:2] == snapshot0  # 旧行逐字段不变（并存版本保留）
    assert snapshot1[2][1] == 3
    assert snapshot1[2][3] == body["current_revision"]["media_artifact_id"]
    assert snapshot1[2][5] == "unreviewed"
    assert _counts()["revisions"] == counts0["revisions"] + 1


def test_t44_media_upload_replay_200_and_reused_422(db, client):
    """幂等：同 command_id 同资产 → 200 原样重放（零新行、零新文件，且幂等
    先于 CAS：expected_revision 已过期仍重放）；异内容 → 422 rule=reused。"""
    token = _register(client, "a43_yuri")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    other = _mk_character(client, token, pid, name="另一人")
    cmd = _cmd()
    data = _wav_bytes()

    r1 = _upload_media(
        client, pid, aid, data=data, command_id=cmd, expected_revision=1, token=token
    )
    assert r1.status_code == 201, r1.text
    first = r1.json()
    counts0 = _counts()
    files0 = _media_files()

    # 重放：expected_revision 仍为首次值（已过期）也不得被判 409
    r2 = _upload_media(
        client, pid, aid, data=data, command_id=cmd, expected_revision=1, token=token
    )
    assert r2.status_code == 200, (r2.status_code, r2.text)
    assert r2.json() == first  # 原样返回首次 result_payload
    assert _counts() == counts0  # 零新行
    assert _media_files() == files0  # 重放不留孤儿媒体文件

    # 同 command_id 打到另一资产 → 422 validation_failed(field=command_id, rule=reused)
    r3 = _upload_media(client, pid, other, data=data, command_id=cmd, token=token)
    assert r3.status_code == 422, r3.text
    err = r3.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "command_id" and v["rule"] == "reused"
    assert _counts() == counts0
    assert _media_files() == files0


def test_t45_media_upload_cas_conflict_409(db, client):
    """expected_revision 不符 → 409 revision_conflict，expected/actual=真实值，
    且零新行、零残留媒体文件（媒体已落盘也须撤销）。"""
    token = _register(client, "a44_zoe")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    counts0 = _counts()
    files0 = _media_files()

    for bad in (2, 99):
        r = _upload_media(
            client, pid, aid, data=_wav_bytes(), expected_revision=bad, token=token
        )
        assert r.status_code == 409, (bad, r.text)
        err = r.json()["error"]
        assert err["code"] == "revision_conflict"
        assert err["details"]["object"] == {"kind": "asset", "id": aid}
        assert err["details"]["expected"] == bad
        assert err["details"]["actual"] == 1  # 真实当前 revision
    assert _counts() == counts0
    assert _media_files() == files0

    # 成功推进到 revision=2 后，旧 expected 的 actual 应为 2
    ok = _upload_media(client, pid, aid, data=_wav_bytes(), expected_revision=1, token=token)
    assert ok.status_code == 201, ok.text
    counts1 = _counts()
    files1 = _media_files()
    r = _upload_media(client, pid, aid, data=_wav_bytes(), expected_revision=1, token=token)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["details"]["actual"] == 2
    assert _counts() == counts1
    assert _media_files() == files1


def test_t46_media_upload_request_shape_422(db, client):
    """expected_revision 非正整数 → 422 field=expected_revision/rule=invalid；
    非整数字符串、缺 command_id、缺 file → 请求体形态 422；均零副作用。"""
    token = _register(client, "a45_abe")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    counts0 = _counts()
    files0 = _media_files()
    url = f"/api/studio/projects/{pid}/assets/{aid}/media"

    for bad in (0, -1):
        r = _upload_media(client, pid, aid, data=_wav_bytes(), expected_revision=bad, token=token)
        assert r.status_code == 422, (bad, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v = err["details"]["violations"][0]
        assert v["field"] == "expected_revision" and v["rule"] == "invalid"

    # 非整数字符串 → 路由层（Pydantic）请求体形态 422
    r = client.post(
        url,
        files={"file": ("clip.wav", _wav_bytes(), "audio/wav")},
        data={"command_id": _cmd(), "expected_revision": "abc"},
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text

    # 缺 command_id → 422
    r = client.post(
        url,
        files={"file": ("clip.wav", _wav_bytes(), "audio/wav")},
        data={},
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text

    # 缺 file → 422
    r = client.post(url, data={"command_id": _cmd()}, headers=_auth(token))
    assert r.status_code == 422, r.text

    assert _counts() == counts0
    assert _media_files() == files0


def test_t47_media_upload_ghost_and_cross_project_404(db, client):
    """幽灵资产 / 跨项目资产 → 同形状 404 not_found(asset)；幽灵项目 /
    非属主项目 → 404 not_found(project)；均零副作用。"""
    token = _register(client, "a46_amy")
    pid = _mk_project(client, token)
    pid_b = _mk_project(client, token, "B")
    aid_b = _mk_character(client, token, pid_b)  # 同属主但属项目 B
    files0 = _media_files()
    data = _wav_bytes()

    counts_a = _counts()
    for ghost in ("01" * 13, aid_b):
        r = _upload_media(client, pid, ghost, data=data, token=token)
        assert r.status_code == 404, (ghost, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"] == {"kind": "asset", "id": ghost}
    assert _counts() == counts_a

    other = _register(client, "a46_bella")
    pid_c = _mk_project(client, other, "C")
    counts_b = _counts()
    for ghost_pid in ("02" * 13, pid_c):
        r = _upload_media(client, ghost_pid, "03" * 13, data=data, token=token)
        assert r.status_code == 404, (ghost_pid, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"] == {"kind": "project", "id": ghost_pid}
    assert _counts() == counts_b
    assert _media_files() == files0


def test_t48_media_upload_no_implicit_adoption_and_cross_project_isolation(db, client):
    """不隐式采用（occurrences/bindings 恒 0、零 StudioJob）；既有 MediaArtifact
    行不可变；跨项目隔离：A 的上传不触碰 B 的资产/媒体，B 的 aid 打到 A → 404
    且零副作用（媒体落盘只进本资产所属项目目录）。"""
    token = _register(client, "a47_bruno")
    pid_a = _mk_project(client, token, "A")
    pid_b = _mk_project(client, token, "B")
    aid_a = _mk_character(client, token, pid_a)
    aid_b = _mk_character(client, token, pid_b)

    # 既有媒体行（项目 A）：上传不得改动它
    with Session() as s:
        seeded = store_media(
            s, project_id=pid_a, filename="seed.wav",
            data=_wav_bytes(0.05), media_kind="audio", source="upload",
        )
        s.commit()
        seeded_snapshot = (
            seeded.id, seeded.project_id, seeded.media_kind, seeded.filename,
            seeded.size_bytes, seeded.sha256, seeded.duration_ms, seeded.width,
            seeded.height, seeded.source, seeded.source_job_id, seeded.created_at,
        )

    def _project_b_state():
        with Session() as s:
            media = s.scalars(
                select(MediaArtifact).where(MediaArtifact.project_id == pid_b)
            ).all()
            asset = s.get(Asset, aid_b)
            return (
                sorted((m.id, m.filename, m.size_bytes, m.sha256) for m in media),
                _revision_snapshot(s, aid_b),
                (asset.current_revision_id, asset.updated_at),
            )

    b0 = _project_b_state()
    counts0 = _counts()
    files0 = _media_files()

    r = _upload_media(client, pid_a, aid_a, data=_wav_bytes(), token=token, expected_revision=1)
    assert r.status_code == 201, r.text
    media_id = r.json()["current_revision"]["media_artifact_id"]

    with Session() as s:
        new_art = s.get(MediaArtifact, media_id)
        assert new_art.project_id == pid_a
        assert new_art.filename.startswith(f"{pid_a}/")
        assert artifact_path(new_art).is_relative_to((media_root() / pid_a).resolve())
        seeded_again = s.get(MediaArtifact, seeded_snapshot[0])
        assert (
            seeded_again.id, seeded_again.project_id, seeded_again.media_kind,
            seeded_again.filename, seeded_again.size_bytes, seeded_again.sha256,
            seeded_again.duration_ms, seeded_again.width, seeded_again.height,
            seeded_again.source, seeded_again.source_job_id, seeded_again.created_at,
        ) == seeded_snapshot  # 既有媒体行逐字段不变

    assert _project_b_state() == b0  # B 项目零副作用
    counts1 = _counts()
    assert counts1["occurrences"] == 0 and counts1["bindings"] == 0  # 不隐式采用
    assert counts1["assets"] == counts0["assets"]
    with Session() as s:
        assert s.scalar(select(func.count(StudioJob.id))) == 0  # 零生成

    # 跨项目：B 的 aid + A 的 pid → 404，媒体行/文件均零副作用
    files_before = _media_files()
    counts_before = _counts()
    r2 = _upload_media(client, pid_a, aid_b, data=_wav_bytes(), token=token)
    assert r2.status_code == 404, r2.text
    assert r2.json()["error"]["details"] == {"kind": "asset", "id": aid_b}
    assert _counts() == counts_before
    assert _media_files() == files_before
    assert _project_b_state() == b0


def test_t49_media_upload_image_and_unknown_extension_422(db, client):
    """图片扩展名边界：MEDIA-01 的 ALLOWED_EXTENSIONS 不含 png/jpg/jpeg/webp，
    故图片上传按 unsupported_type 原样 422 透传（不放宽 MEDIA-01 常量）；
    未知扩展名同理。均零副作用。"""
    token = _register(client, "a48_cara")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    counts0 = _counts()
    files0 = _media_files()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    for name, blob in (
        ("cover.png", png),
        ("cover.jpg", png),
        ("cover.jpeg", png),
        ("cover.webp", png),
        ("notes.txt", b"hello"),
    ):
        r = _upload_media(client, pid, aid, data=blob, filename=name, token=token)
        assert r.status_code == 422, (name, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v = err["details"]["violations"][0]
        assert (v["field"], v["rule"]) == ("filename", "unsupported_type"), (name, err)

    assert _counts() == counts0
    assert _media_files() == files0


def test_t50_media_upload_integrity_failure_rejected_cleaned(db, client, monkeypatch):
    """落盘后 verify_integrity 复核为假 → 视为损坏：422 拒绝、撤销媒体行、
    删除已落盘文件（不留半写版本，原版本保留）。"""
    from backend.studio.assets import service as assets_service

    token = _register(client, "a49_dino")
    pid = _mk_project(client, token)
    aid = _mk_character(client, token, pid)
    counts0 = _counts()
    files0 = _media_files()
    media0 = _media_row_count()
    with Session() as s:
        snapshot0 = _revision_snapshot(s, aid)

    monkeypatch.setattr(assets_service, "verify_integrity", lambda _artifact: False)
    r = _upload_media(client, pid, aid, data=_wav_bytes(), token=token, expected_revision=1)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "file" and v["rule"] == "invalid"

    assert _counts() == counts0
    assert _media_row_count() == media0
    assert _media_files() == files0  # 已落盘文件被清理，无残留
    with Session() as s:
        assert _revision_snapshot(s, aid) == snapshot0
