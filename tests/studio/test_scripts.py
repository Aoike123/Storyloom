"""SCRIPT-01-a 新建剧本草稿对象（附录 03 §2 SC1）—— POST /projects/{pid}/script-objects。

覆盖：action/dialogue 创建（DTO 冻结形状 + DB 行 + source_fragment_revision 出处快照）、
seq 自动顺位/显式/占用冲突、text/kind/seq 字段校验（一次返回全部 violations）、
片段门禁（不存在/跨项目同形 fragment_not_found、未确认、已退役、绑定版本非 active）、
项目 404（不存在/非属主不泄漏）、无 token 401、command_id 幂等重放（200 零副作用）
与 reused 422、草稿不采用（零 ScriptAdoption 行、对象无 stage/状态列写入）。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件不改写 STUDIO_* env。db fixture 与 register/auth/import/fragment helper
照抄 tests/studio/test_fragments.py 的实现。
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
from backend.studio.reviews.models import ChangePreview, ReviewDecision
from backend.studio.scripts.models import ScriptAdoption, ScriptObject, ScriptRevision
from backend.studio.sources.fragment_models import Fragment, RangeSet

TEXT_A = "01234567890123456789"  # 20 字符，无空白
TEXT_B = "abcdefghijklmnopqrst"  # 第二版正文（import 后不自动激活）


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（script_objects↔script_revisions 等）：drop 前临时关 FK
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


def _mk_project(client, token, name="SProj") -> str:
    r = client.post(
        "/api/studio/projects",
        json={"name": name, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _import_active(client, token, pid, content) -> dict:
    """S1 导入并返回 201 修订体（首版自动激活）。"""
    r = client.post(
        f"/api/studio/projects/{pid}/sources",
        json={"content": content, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _range_set_cas(pid) -> int:
    with Session() as s:
        return s.scalar(select(RangeSet.cas_revision).where(RangeSet.project_id == pid))


def _mk_fragment(client, token, pid, source_revision_id, start=0, end=10, name="F",
                 expected="unset") -> str:
    if expected == "unset":
        expected = None  # 首个片段：无既有 range_set 行
    r = client.post(
        f"/api/studio/projects/{pid}/fragments",
        json={
            "source_revision_id": source_revision_id,
            "range": {"start": start, "end": end},
            "name": name,
            "expected_range_set_revision": expected,
            "command_id": _cmd(),
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["object_ref"]["id"]


def _confirm(client, token, pid, fid, expected=1):
    r = client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/confirm",
        json={"expected_revision": expected, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _confirmed_fragment(client, token, pid, source_revision_id, **kw) -> str:
    fid = _mk_fragment(client, token, pid, source_revision_id, **kw)
    _confirm(client, token, pid, fid)
    return fid


def _retire(client, token, pid, fid):
    """F7 preview → apply 真实退役（退役不动 range_set cas，期望取当前值）。"""
    rp = client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/retire",
        json={"target_fragment_id": fid, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert rp.status_code == 200, rp.text
    with Session() as s:
        cas = s.scalar(
            select(RangeSet.cas_revision).where(RangeSet.project_id == pid)
        )
    ra = client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/retire/apply",
        json={
            "preview_id": rp.json()["preview_id"],
            "command_id": _cmd(),
            "expected_revision": 1,
            "expected_range_set_revision": cas,
        },
        headers=_auth(token),
    )
    assert ra.status_code == 200, ra.text


def _activate(client, token, pid, target_revision_id, expected_active_revision_id):
    """S4 preview → S5 apply 真实激活 target 版本（两步均断言 200）。"""
    rp = client.post(
        f"/api/studio/projects/{pid}/source-changes/preview",
        json={
            "kind": "source_activate",
            "target_revision_id": target_revision_id,
            "command_id": _cmd(),
        },
        headers=_auth(token),
    )
    assert rp.status_code == 200, rp.text
    ra = client.post(
        f"/api/studio/projects/{pid}/source-changes/apply",
        json={
            "preview_id": rp.json()["preview_id"],
            "command_id": _cmd(),
            "expected_active_revision_id": expected_active_revision_id,
        },
        headers=_auth(token),
    )
    assert ra.status_code == 200, ra.text


def _create(client, token, pid, fragment_id, *, kind="action", text="测试动作",
            speaker=None, note=None, seq=None, command_id=None):
    body = {
        "fragment_id": fragment_id,
        "kind": kind,
        "text": text,
        "command_id": command_id or _cmd(),
    }
    if speaker is not None:
        body["speaker"] = speaker
    if note is not None:
        body["adaptation_note"] = note
    if seq is not None:
        body["seq"] = seq
    return client.post(
        f"/api/studio/projects/{pid}/script-objects", json=body, headers=_auth(token)
    )


def _counts() -> dict:
    with Session() as s:
        return {
            "objects": s.scalar(select(func.count(ScriptObject.id))),
            "revisions": s.scalar(select(func.count(ScriptRevision.id))),
            "adoptions": s.scalar(select(func.count(ScriptAdoption.id))),
            "commands": s.scalar(select(func.count(CommandRecord.id))),
        }


def _revise(client, token, pid, oid, body):
    """SC4 修订：body 直接传原始请求体（缺席字段不写、显式 null 写 None）。"""
    return client.post(
        f"/api/studio/projects/{pid}/script-objects/{oid}/revise",
        json=body,
        headers=_auth(token),
    )


# ── 成功路径 ────────────────────────────────────────────────────────────────

def test_t1_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/01010101010101010101010101/script-objects",
        json={
            "fragment_id": "02" * 13,
            "kind": "action",
            "text": "x",
            "command_id": _cmd(),
        },
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t2_create_action_201_dto_and_db(db, client):
    token = _register(client, "s1a_t2_alice")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])

    r = _create(client, token, pid, fid, kind="action", text="夜雨中奔跑")
    assert r.status_code == 201, r.text
    body = r.json()
    # DTO 冻结字段集（主模型 2026-10-13 交接单 §3）
    assert set(body) == {
        "object_ref", "kind", "text", "speaker", "adaptation_note",
        "seq", "fragment", "current_revision",
    }
    oid = body["object_ref"]["id"]
    assert body["object_ref"] == {"kind": "script_object", "id": oid, "revision": 1}
    assert re.fullmatch(r"[0-9a-z]{26}", oid) and not (set(oid) & set("ilou"))
    assert body["kind"] == "action"
    assert body["text"] == "夜雨中奔跑"
    assert body["speaker"] is None
    assert body["adaptation_note"] is None
    assert body["seq"] == 1  # 片段内首个对象
    assert body["fragment"] == {"kind": "fragment", "id": fid, "revision": 1}
    assert body["current_revision"] == 1

    # 落库：对象行 + revision=1 行，同事务；出处快照 = 片段当前 revision
    with Session() as s:
        obj = s.get(ScriptObject, oid)
        assert obj is not None
        assert obj.project_id == pid and obj.fragment_id == fid
        assert obj.kind == "action" and obj.seq == 1
        assert obj.speaker is None and obj.retired_at is None
        rv = s.scalar(select(ScriptRevision).where(ScriptRevision.object_id == oid))
        assert rv is not None and rv.id == obj.current_revision_id
        assert rv.revision == 1 and rv.text == "夜雨中奔跑"
        assert rv.speaker is None and rv.adaptation_note is None
        frag = s.get(Fragment, fid)
        assert rv.source_fragment_revision == 1  # 出处快照 = 片段当前 revision 号
        assert frag is not None and frag.state == "confirmed"


def test_t3_create_dialogue_explicit_seq_and_note_201(db, client):
    token = _register(client, "s1a_t3_bob")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])

    r = _create(
        client, token, pid, fid,
        kind="dialogue", text="“别回头。”", speaker="林述",
        note="合并原文两句对白", seq=5,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "dialogue"
    assert body["speaker"] == "林述"
    assert body["adaptation_note"] == "合并原文两句对白"
    assert body["seq"] == 5
    with Session() as s:
        obj = s.get(ScriptObject, body["object_ref"]["id"])
        assert obj.speaker == "林述" and obj.seq == 5
        rv = s.get(ScriptRevision, obj.current_revision_id)
        assert rv.speaker == "林述" and rv.adaptation_note == "合并原文两句对白"


def test_t4_seq_auto_assign_next_free(db, client):
    token = _register(client, "s1a_t4_carol")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])

    r1 = _create(client, token, pid, fid, text="甲")
    r2 = _create(client, token, pid, fid, text="乙")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["seq"] == 1 and r2.json()["seq"] == 2
    # 显式跳到 5 之后，缺省回到 max+1=6
    r3 = _create(client, token, pid, fid, text="丙", seq=5)
    assert r3.status_code == 201 and r3.json()["seq"] == 5
    r4 = _create(client, token, pid, fid, text="丁")
    assert r4.status_code == 201 and r4.json()["seq"] == 6
    # 另一片段独立计数（既有 range_set，CAS 期望 = 当前 cas）
    fid2 = _confirmed_fragment(
        client, token, pid, rev["id"], start=10, end=20, name="G",
        expected=_range_set_cas(pid),
    )
    r5 = _create(client, token, pid, fid2, text="戊")
    assert r5.status_code == 201 and r5.json()["seq"] == 1


def test_t5_seq_taken_422_with_occupant_id(db, client):
    token = _register(client, "s1a_t5_dave")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])

    r1 = _create(client, token, pid, fid, text="占位", seq=3)
    assert r1.status_code == 201, r1.text
    occupant = r1.json()["object_ref"]["id"]
    before = _counts()
    r2 = _create(client, token, pid, fid, text="撞车", seq=3)
    assert r2.status_code == 422, r2.text
    err = r2.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "seq" and v0["rule"] == "taken"
    assert occupant in v0["message"]
    assert _counts() == before  # 零副作用


# ── 字段校验（422 validation_failed）────────────────────────────────────────

def test_t6_text_and_kind_and_seq_violations_422(db, client):
    token = _register(client, "s1a_t6_erin")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])

    # text 空/全空白
    for bad_text in ("", "   ", "\t\n"):
        r = _create(client, token, pid, fid, text=bad_text)
        assert r.status_code == 422, (bad_text, r.text)
        v0 = r.json()["error"]["details"]["violations"][0]
        assert v0["field"] == "text" and v0["rule"] == "required"
    # kind 非枚举
    for bad_kind in ("narration", "ACTION", ""):
        r = _create(client, token, pid, fid, kind=bad_kind)
        assert r.status_code == 422, (bad_kind, r.text)
        v0 = r.json()["error"]["details"]["violations"][0]
        assert v0["field"] == "kind" and v0["rule"] == "enum"
    # seq 非正整数
    for bad_seq in (0, -1):
        r = _create(client, token, pid, fid, seq=bad_seq)
        assert r.status_code == 422, (bad_seq, r.text)
        v0 = r.json()["error"]["details"]["violations"][0]
        assert v0["field"] == "seq" and v0["rule"] == "positive"
    # 多错一次返回全部 violations（text + kind 同时非法）
    r = _create(client, token, pid, fid, kind="bad", text=" ")
    assert r.status_code == 422
    fields = {v["field"] for v in r.json()["error"]["details"]["violations"]}
    assert fields == {"text", "kind"}
    assert _counts()["objects"] == 0  # 全部零副作用


def test_t7_missing_required_body_fields_422(db, client):
    """请求体形态校验：缺 command_id / fragment_id / text / kind 均 422。"""
    token = _register(client, "s1a_t7_frank")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])
    full = {"fragment_id": fid, "kind": "action", "text": "x", "command_id": _cmd()}
    for drop in ("command_id", "fragment_id", "text", "kind"):
        body = {k: v for k, v in full.items() if k != drop}
        r = client.post(
            f"/api/studio/projects/{pid}/script-objects",
            json=body, headers=_auth(token),
        )
        assert r.status_code == 422, (drop, r.text)
    assert _counts()["objects"] == 0


# ── 片段门禁（422 precondition_failed）─────────────────────────────────────

def test_t8_fragment_not_found_and_cross_project_same_shape_422(db, client):
    token = _register(client, "s1a_t8_grace")
    pid = _mk_project(client, token, "A")
    rev = _import_active(client, token, pid, TEXT_A)
    _confirmed_fragment(client, token, pid, rev["id"])
    pid_b = _mk_project(client, token, "B")
    rev_b = _import_active(client, token, pid_b, TEXT_A)
    fid_b = _confirmed_fragment(client, token, pid_b, rev_b["id"])

    for ghost in ("03" * 13, fid_b):  # 不存在 / 跨项目 → 同形状，不泄漏存在性
        r = _create(client, token, pid, ghost)
        assert r.status_code == 422, (ghost, r.text)
        err = r.json()["error"]
        assert err["code"] == "precondition_failed"
        assert err["details"]["reason"] == "fragment_not_found"
        assert err["details"]["blocked_by"] == {
            "kind": "fragment", "id": ghost, "revision": None,
        }


def test_t9_not_confirmed_422(db, client):
    token = _register(client, "s1a_t9_heidi")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _mk_fragment(client, token, pid, rev["id"])  # candidate，未确认

    r = _create(client, token, pid, fid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "not_confirmed"
    assert err["details"]["blocked_by"] == {
        "kind": "fragment", "id": fid, "revision": 1,
    }
    assert _counts()["objects"] == 0


def test_t10_retired_fragment_422(db, client):
    token = _register(client, "s1a_t10_ivan")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])
    _retire(client, token, pid, fid)

    r = _create(client, token, pid, fid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "retired"
    assert err["details"]["blocked_by"]["id"] == fid
    assert _counts()["objects"] == 0


def test_t11_source_revision_inactive_422(db, client):
    """已确认片段绑定的正文版本被切走后 → source_revision_inactive。"""
    token = _register(client, "s1a_t11_judy")
    pid = _mk_project(client, token)
    rev1 = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev1["id"])
    # 第二版导入（不自动激活）→ 真实 S4/S5 激活 rev2
    r2 = client.post(
        f"/api/studio/projects/{pid}/sources",
        json={"content": TEXT_B, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r2.status_code == 201, r2.text
    _activate(client, token, pid, r2.json()["id"], rev1["id"])

    r = _create(client, token, pid, fid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    assert err["details"]["blocked_by"] == {
        "kind": "fragment", "id": fid, "revision": 1,
    }
    assert _counts()["objects"] == 0


# ── 项目 404（不存在 / 非属主，不泄漏存在性）───────────────────────────────

def test_t12_project_404(db, client):
    token = _register(client, "s1a_t12_karen")
    ghost = "01" * 13
    r = _create(client, token, ghost, "02" * 13)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "project", "id": ghost}
    # 非属主：B 的项目，A 写入 → 404
    tb = _register(client, "s1a_t12_laura")
    pid_b = _mk_project(client, tb, "BProj")
    r2 = _create(client, token, pid_b, "02" * 13)
    assert r2.status_code == 404, r2.text
    assert r2.json()["error"]["details"] == {"kind": "project", "id": pid_b}
    assert _counts()["objects"] == 0


# ── 幂等 / 草稿不采用 ───────────────────────────────────────────────────────

def test_t13_replay_same_command_id_200_no_side_effects(db, client):
    token = _register(client, "s1a_t13_mallory")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])
    cmd = _cmd()
    r1 = _create(
        client, token, pid, fid, kind="dialogue", text="“走。”",
        speaker="林述", note="n", seq=2, command_id=cmd,
    )
    assert r1.status_code == 201, r1.text
    first = r1.json()
    before = _counts()
    r2 = _create(
        client, token, pid, fid, kind="dialogue", text="“走。”",
        speaker="林述", note="n", seq=2, command_id=cmd,
    )
    assert r2.status_code == 200, (r2.status_code, r2.text)  # 重放 = 200 非 201
    assert r2.json() == first  # 原样返回首次 result_payload
    after = _counts()
    assert after == before, f"重放产生副作用: {before} -> {after}"
    assert after["objects"] == 1 and after["revisions"] == 1


def test_t14_replay_default_seq_returns_first_payload(db, client):
    """seq 缺省由服务端裁定：重放不参与 seq 比对，仍返回首次分配的 seq。"""
    token = _register(client, "s1a_t14_nancy")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])
    cmd = _cmd()
    r1 = _create(client, token, pid, fid, text="自动位", command_id=cmd)
    assert r1.status_code == 201 and r1.json()["seq"] == 1
    # 之间插入另一条对象占用 seq=2
    r_mid = _create(client, token, pid, fid, text="插队")
    assert r_mid.status_code == 201 and r_mid.json()["seq"] == 2
    r2 = _create(client, token, pid, fid, text="自动位", command_id=cmd)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()  # 不重新分配 seq=3
    assert _counts()["objects"] == 2


def test_t15_command_id_reused_422(db, client):
    token = _register(client, "s1a_t15_olga")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])
    cmd = _cmd()
    r1 = _create(client, token, pid, fid, text="首条", command_id=cmd)
    assert r1.status_code == 201, r1.text
    before = _counts()
    # 同 command_id、不同内容
    r2 = _create(client, token, pid, fid, text="改了", command_id=cmd)
    assert r2.status_code == 422, r2.text
    err = r2.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "command_id" and v0["rule"] == "reused"
    assert _counts() == before


def test_t16_no_adoption_writes(db, client):
    """草稿与采用是两个事实：本卡只写 Object+Revision+CommandRecord，
    零 ScriptAdoption 行、对象无 stage/状态列、retired_at 恒空。"""
    token = _register(client, "s1a_t16_paul")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])
    _create(client, token, pid, fid, kind="action", text="推门")
    _create(client, token, pid, fid, kind="dialogue", text="“谁？”", speaker="林述")
    c = _counts()
    assert c["objects"] == 2 and c["revisions"] == 2 and c["adoptions"] == 0
    with Session() as s:
        objs = s.scalars(select(ScriptObject)).all()
        assert all(o.retired_at is None for o in objs)
        assert not hasattr(ScriptObject, "stage")  # 模型无 stage/状态列
        revs = s.scalars(select(ScriptRevision)).all()
        assert all(rv.revision == 1 for rv in revs)
        assert all(rv.source_fragment_revision == 1 for rv in revs)


# ── SC4 修订当前草稿（t17 起）──────────────────────────────────────────────

def _setup_object(client, token, *, speaker=None, note=None):
    """建项目→导入激活→确认片段→建对象，返回 (pid, fid, oid)。"""
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])
    r = _create(
        client, token, pid, fid,
        kind="dialogue", text="你好", speaker=speaker, note=note,
    )
    assert r.status_code == 201, r.text
    oid = r.json()["object_ref"]["id"]
    return pid, fid, oid


def test_t17_revise_text_200(db, client):
    """修订 text → 200，新 revision=2，DTO 与 SC1 同形，缺席字段取现值。"""
    token = _register(client, "s1a_t17_anna")
    pid, fid, oid = _setup_object(client, token, speaker="林", note="原注")
    before = _counts()
    r = _revise(
        client, token, pid, oid,
        {"text": "你好呀", "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object_ref"] == {"kind": "script_object", "id": oid, "revision": 2}
    assert body["kind"] == "dialogue"
    assert body["text"] == "你好呀"
    assert body["speaker"] == "林"  # 缺席 → 取现值
    assert body["adaptation_note"] == "原注"  # 缺席 → 取现值
    assert body["seq"] == 1
    assert body["fragment"] == {"kind": "fragment", "id": fid, "revision": 1}
    assert body["current_revision"] == 2
    c = _counts()
    assert c["objects"] == before["objects"] == 1
    assert c["revisions"] == before["revisions"] + 1 == 2
    assert c["adoptions"] == 0  # 零采用
    with Session() as s:
        new_rev = s.scalar(
            select(ScriptRevision).where(
                ScriptRevision.object_id == oid, ScriptRevision.revision == 2
            )
        )
        assert new_rev is not None
        assert new_rev.text == "你好呀"
        assert new_rev.speaker == "林"
        assert new_rev.adaptation_note == "原注"
        assert new_rev.source_fragment_revision == 1


def test_t18_speaker_explicit_null_clears(db, client):
    """speaker 显式 null → 清空（新 revision speaker=None，对象行镜像同步 None）。"""
    token = _register(client, "s1a_t18_ben")
    pid, fid, oid = _setup_object(client, token, speaker="林")
    r = _revise(
        client, token, pid, oid,
        {"text": "改", "speaker": None, "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 200, r.text
    assert r.json()["speaker"] is None
    with Session() as s:
        obj = s.get(ScriptObject, oid)
        rev2 = s.scalar(select(ScriptRevision).where(
            ScriptRevision.object_id == oid, ScriptRevision.revision == 2))
        assert rev2.speaker is None
        assert obj.speaker is None  # 对象行镜像同步清空
        assert obj.current_revision_id == rev2.id


def test_t19_speaker_absent_keeps(db, client):
    """speaker 缺席 → 保持原值（不写进请求体 ≠ 显式 null）。"""
    token = _register(client, "s1a_t19_cara")
    pid, fid, oid = _setup_object(client, token, speaker="林")
    r = _revise(
        client, token, pid, oid,
        {"text": "改", "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 200, r.text
    assert r.json()["speaker"] == "林"
    with Session() as s:
        obj = s.get(ScriptObject, oid)
        rev2 = s.scalar(select(ScriptRevision).where(
            ScriptRevision.object_id == oid, ScriptRevision.revision == 2))
        assert rev2.speaker == "林"
        assert obj.speaker == "林"


def test_t20_note_explicit_null_clears(db, client):
    """adaptation_note 显式 null → 清空。"""
    token = _register(client, "s1a_t20_dan")
    pid, fid, oid = _setup_object(client, token, note="原注")
    r = _revise(
        client, token, pid, oid,
        {"text": "改", "adaptation_note": None, "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 200, r.text
    assert r.json()["adaptation_note"] is None
    with Session() as s:
        rev2 = s.scalar(select(ScriptRevision).where(
            ScriptRevision.object_id == oid, ScriptRevision.revision == 2))
        assert rev2.adaptation_note is None


def test_t21_note_absent_keeps(db, client):
    """adaptation_note 缺席 → 保持原值。"""
    token = _register(client, "s1a_t21_eli")
    pid, fid, oid = _setup_object(client, token, note="原注")
    r = _revise(
        client, token, pid, oid,
        {"text": "改", "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 200, r.text
    assert r.json()["adaptation_note"] == "原注"
    with Session() as s:
        rev2 = s.scalar(select(ScriptRevision).where(
            ScriptRevision.object_id == oid, ScriptRevision.revision == 2))
        assert rev2.adaptation_note == "原注"


def test_t22_stale_expected_revision_409(db, client):
    """expected_revision 过期 → 409 revision_conflict，库中无新 revision。"""
    token = _register(client, "s1a_t22_finn")
    pid, fid, oid = _setup_object(client, token)
    before = _counts()
    r = _revise(
        client, token, pid, oid,
        {"text": "改", "expected_revision": 99, "command_id": _cmd()},
    )
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"]["object"] == {"kind": "script_object", "id": oid}
    assert err["details"]["expected"] == 99
    assert err["details"]["actual"] == 1
    assert _counts() == before  # 零写


def test_t23_all_three_absent_422_empty(db, client):
    """三者全缺席 → 422 validation_failed(field=payload, rule=empty)。"""
    token = _register(client, "s1a_t23_gus")
    pid, fid, oid = _setup_object(client, token)
    before = _counts()
    r = _revise(
        client, token, pid, oid,
        {"expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "payload" and v0["rule"] == "empty"
    assert _counts() == before


def test_t24_text_null_others_absent_422_empty(db, client):
    """text 显式 null + 其余缺席 → 同样触发 empty（text null 视同缺席）。"""
    token = _register(client, "s1a_t24_hana")
    pid, fid, oid = _setup_object(client, token)
    r = _revise(
        client, token, pid, oid,
        {"text": None, "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "payload" and v0["rule"] == "empty"


def test_t25_text_blank_422(db, client):
    """text 显式提供但去空白后为空 → 422 field=text rule=blank。"""
    token = _register(client, "s1a_t25_igor")
    pid, fid, oid = _setup_object(client, token)
    r = _revise(
        client, token, pid, oid,
        {"text": "   ", "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "text" and v0["rule"] == "blank"


def test_t26_speaker_too_long_422(db, client):
    """speaker 显式提供且 >80 → 422 field=speaker rule=too_long。"""
    token = _register(client, "s1a_t26_jack")
    pid, fid, oid = _setup_object(client, token)
    r = _revise(
        client, token, pid, oid,
        {"text": "改", "speaker": "x" * 81, "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "speaker" and v0["rule"] == "too_long"


def test_t27_note_too_long_422(db, client):
    """adaptation_note 显式提供且 >300 → 422 field=adaptation_note rule=too_long。"""
    token = _register(client, "s1a_t27_kim")
    pid, fid, oid = _setup_object(client, token)
    r = _revise(
        client, token, pid, oid,
        {"text": "改", "adaptation_note": "x" * 301, "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "adaptation_note" and v0["rule"] == "too_long"


def test_t28_expected_revision_non_positive_422(db, client):
    """expected_revision 非正整数（0）→ 422 field=expected_revision rule=positive。"""
    token = _register(client, "s1a_t28_lea")
    pid, fid, oid = _setup_object(client, token)
    r = _revise(
        client, token, pid, oid,
        {"text": "改", "expected_revision": 0, "command_id": _cmd()},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "expected_revision" and v0["rule"] == "positive"


def test_t29_object_not_exist_404(db, client):
    """对象不存在 → 404 not_found(kind=script_object)。"""
    token = _register(client, "s1a_t29_mia")
    pid = _mk_project(client, token)
    ghost = "02" * 13
    r = _revise(
        client, token, pid, ghost,
        {"text": "改", "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "script_object", "id": ghost}


def test_t30_cross_project_object_404_same_shape(db, client):
    """跨项目对象 → 404，与不存在同形状（不泄漏存在性）。"""
    token = _register(client, "s1a_t30_nina")
    pid_a = _mk_project(client, token, name="ProjA")
    pid_b = _mk_project(client, token, name="ProjB")
    rev = _import_active(client, token, pid_b, TEXT_A)
    fid = _confirmed_fragment(client, token, pid_b, rev["id"])
    r = _create(client, token, pid_b, fid, kind="dialogue", text="你好")
    assert r.status_code == 201, r.text
    oid = r.json()["object_ref"]["id"]
    # oid 属于 pid_b，不是 pid_a → 跨项目（与不存在同形状 404）
    r = _revise(
        client, token, pid_a, oid,
        {"text": "改", "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "script_object", "id": oid}


def test_t31_retired_object_422(db, client):
    """retired 对象 → 422 precondition_failed(reason=retired)。"""
    token = _register(client, "s1a_t31_omar")
    pid, fid, oid = _setup_object(client, token)
    # 无 SC5 退役端点（未来卡）→ 直接置 retired_at 模拟已退役对象
    import time as _time
    with Session() as s:
        obj = s.get(ScriptObject, oid)
        obj.retired_at = _time.time()
        s.commit()
    r = _revise(
        client, token, pid, oid,
        {"text": "改", "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "retired"
    assert err["details"]["blocked_by"]["id"] == oid


def test_t32_fragment_source_revision_inactive_422(db, client):
    """片段绑定的正文版本被切走（来源换版）→ 422 source_revision_inactive。"""
    token = _register(client, "s1a_t32_pia")
    pid = _mk_project(client, token)
    rev1 = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev1["id"])
    r = _create(client, token, pid, fid, kind="dialogue", text="你好")
    assert r.status_code == 201, r.text
    oid = r.json()["object_ref"]["id"]
    # 第二版导入（不自动激活）→ 真实 S4/S5 激活 rev2 → rev1 失效
    r2 = client.post(
        f"/api/studio/projects/{pid}/sources",
        json={"content": TEXT_B, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r2.status_code == 201, r2.text
    _activate(client, token, pid, r2.json()["id"], rev1["id"])
    before = _counts()
    res = _revise(
        client, token, pid, oid,
        {"text": "改", "expected_revision": 1, "command_id": _cmd()},
    )
    assert res.status_code == 422, res.text
    err = res.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    assert err["details"]["blocked_by"] == {"kind": "fragment", "id": fid, "revision": 1}
    assert _counts() == before


def test_t33_fragment_retired_422(db, client):
    """片段已退役 → 422 precondition_failed(reason=retired)。"""
    token = _register(client, "s1a_t33_rita")
    pid = _mk_project(client, token)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])
    r = _create(client, token, pid, fid, kind="dialogue", text="你好")
    assert r.status_code == 201, r.text
    oid = r.json()["object_ref"]["id"]
    _retire(client, token, pid, fid)
    before = _counts()
    res = _revise(
        client, token, pid, oid,
        {"text": "改", "expected_revision": 1, "command_id": _cmd()},
    )
    assert res.status_code == 422, res.text
    err = res.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "retired"
    assert err["details"]["blocked_by"]["id"] == fid
    assert _counts() == before


def test_t34_same_command_id_same_payload_replay_200(db, client):
    """同 command_id 同 payload 字面重放 → 200 同响应，零新 revision/零新 CommandRecord。

    幂等先于 CAS（F1/F4/F6/F7/F8 先例）：第二次请求与原请求逐字节相同
    （expected_revision 仍为首次值 1），首次成功（revision=2）后字面重试
    仍命中重放 200，不撞 CAS → 409。
    """
    token = _register(client, "s1a_t34_sara")
    pid, fid, oid = _setup_object(client, token)
    cmd = _cmd()
    body = {"text": "甲", "expected_revision": 1, "command_id": cmd}
    r1 = _revise(client, token, pid, oid, body)
    assert r1.status_code == 200, r1.text
    before = _counts()
    # 字面重放：同 command_id、同内容、expected_revision 仍为首次值 1（逐字节相同）
    r2 = _revise(client, token, pid, oid, body)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()  # 同响应（重放返回首次 result_payload）
    c = _counts()
    assert c["revisions"] == before["revisions"]  # 无新 revision
    assert c["commands"] == before["commands"]  # 无新 CommandRecord（重放零写）


def test_t35_same_command_id_different_payload_422_reused(db, client):
    """同 command_id 不同 payload → 422 validation_failed(field=command_id, rule=reused)。"""
    token = _register(client, "s1a_t35_tara")
    pid, fid, oid = _setup_object(client, token)
    cmd = _cmd()
    r1 = _revise(
        client, token, pid, oid,
        {"text": "甲", "expected_revision": 1, "command_id": cmd},
    )
    assert r1.status_code == 200, r1.text
    before = _counts()
    # 同 command_id、不同内容（不同 text）；expected_revision 取当前值 2（幂等先于 CAS）
    r2 = _revise(
        client, token, pid, oid,
        {"text": "乙", "expected_revision": 2, "command_id": cmd},
    )
    assert r2.status_code == 422, r2.text
    err = r2.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "command_id" and v0["rule"] == "reused"
    assert _counts() == before


def test_t37_same_command_id_different_content_stale_expected_422_reused(db, client):
    """同 command_id 不同内容 + 过期 expected_revision → 422 reused（幂等先于 CAS）。

    幂等先于 CAS 的新行为：即使 expected_revision=1（首次值，已过期），
    同 command_id 不同内容也先命中幂等 → 422 reused，而非 409 revision_conflict。
    """
    token = _register(client, "s1a_t37_vera")
    pid, fid, oid = _setup_object(client, token)
    cmd = _cmd()
    r1 = _revise(
        client, token, pid, oid,
        {"text": "甲", "expected_revision": 1, "command_id": cmd},
    )
    assert r1.status_code == 200, r1.text
    before = _counts()
    # 同 command_id、不同内容（text="乙"）、expected_revision=1（首次值，已过期）
    r2 = _revise(
        client, token, pid, oid,
        {"text": "乙", "expected_revision": 1, "command_id": cmd},
    )
    assert r2.status_code == 422, r2.text
    err = r2.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "command_id" and v0["rule"] == "reused"
    assert _counts() == before


def test_t36_direct_db_check_zero_adoption_and_mirror(db, client):
    """成功后直查库：无新 adoption 行，对象行 current_revision_id/speaker 镜像/
    updated_at 已更新，旧 revision 行不可变。"""
    token = _register(client, "s1a_t36_uma")
    pid, fid, oid = _setup_object(client, token, speaker="林", note="原注")
    with Session() as s:
        old_obj = s.get(ScriptObject, oid)
        old_rev = s.scalar(select(ScriptRevision).where(
            ScriptRevision.object_id == oid, ScriptRevision.revision == 1))
        old_obj_snapshot = (old_obj.current_revision_id, old_obj.speaker, old_obj.updated_at)
        old_rev_snapshot = (old_rev.text, old_rev.speaker, old_rev.adaptation_note,
                            old_rev.source_fragment_revision, old_rev.created_at)
        old_obj_id = old_obj.id
    r = _revise(
        client, token, pid, oid,
        {"text": "你好呀", "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 200, r.text
    with Session() as s:
        # 零 adoption
        assert s.scalar(select(func.count(ScriptAdoption.id))) == 0
        # 对象行：current_revision_id 指向新 revision、speaker 镜像、updated_at 更新
        obj = s.get(ScriptObject, oid)
        rev2 = s.scalar(select(ScriptRevision).where(
            ScriptRevision.object_id == oid, ScriptRevision.revision == 2))
        assert rev2 is not None
        assert obj.current_revision_id == rev2.id
        assert obj.speaker == rev2.speaker == "林"  # 镜像同步
        assert obj.updated_at > old_obj_snapshot[2]  # updated_at 已更新
        assert obj.id == old_obj_id  # 对象 ID 不变
        # 旧 revision 行不可变
        old_rev = s.scalar(select(ScriptRevision).where(
            ScriptRevision.object_id == oid, ScriptRevision.revision == 1))
        assert (old_rev.text, old_rev.speaker, old_rev.adaptation_note,
                old_rev.source_fragment_revision, old_rev.created_at) == old_rev_snapshot
        # 新 revision 的 source_fragment_revision = 保存时刻片段 revision
        assert rev2.source_fragment_revision == 1


# ═══════════════════ SCRIPT-02 / SC6 采用剧本 revision（preview/apply）═══════════════════
# 覆盖：preview/apply 成功路径（DTO 逐字段、baseline 精确、TTL、零业务副作用）、401、
# 项目/片段/preview 404 不泄漏、mismatch 422、片段门禁四态顺序、幂等重放/异内容 reused、
# expired 惰性置位、终态不改写、CAS 失配、baseline 失配 + superseded、content_hash/
# baseline_digest 独立重算、ReviewDecision 恰 1 行、负向断言（分镜/任务/媒体/对象不变）、
# 采用链（baseline 追加 adoption ref、previous_adoption_id、旧 adoption 行不变）。


def _adopt_preview(client, token, pid, fid, target=None, command_id=None):
    return client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/script-adopt",
        json={
            "target_fragment_id": target if target is not None else fid,
            "command_id": command_id or _cmd(),
        },
        headers=_auth(token),
    )


def _expected_objects(pid, fid) -> list:
    """当前片段全部对象按 seq 升序的 apply 快照（= preview baseline 的对象部分）。"""
    with Session() as s:
        objs = s.scalars(
            select(ScriptObject)
            .where(ScriptObject.fragment_id == fid)
            .order_by(ScriptObject.seq)
        ).all()
        out = []
        for o in objs:
            rv = s.get(ScriptRevision, o.current_revision_id)
            out.append(
                {
                    "object_id": o.id,
                    "revision": rv.revision,
                    "seq": o.seq,
                    "kind": o.kind,
                }
            )
    return out


def _adopt_apply(
    client,
    token,
    pid,
    fid,
    preview_id,
    *,
    command_id=None,
    expected_previous_adoption_id=None,
    expected_object_revisions=None,
):
    if expected_object_revisions is None:
        expected_object_revisions = _expected_objects(pid, fid)
    return client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/script-adopt/apply",
        json={
            "preview_id": preview_id,
            "command_id": command_id or _cmd(),
            "expected_previous_adoption_id": expected_previous_adoption_id,
            "expected_object_revisions": expected_object_revisions,
        },
        headers=_auth(token),
    )


def _adopt_setup(client, token, name="SProj"):
    """项目→导入激活→确认片段→建 2 个对象（dialogue + action），返回 (pid, fid)。"""
    pid = _mk_project(client, token, name)
    rev = _import_active(client, token, pid, TEXT_A)
    fid = _confirmed_fragment(client, token, pid, rev["id"])
    r1 = _create(client, token, pid, fid, kind="dialogue", text="甲", speaker="林")
    r2 = _create(client, token, pid, fid, kind="action", text="乙")
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)
    return pid, fid


def _preview_row(preview_id) -> dict:
    with Session() as s:
        pv = s.get(ChangePreview, preview_id)
        return {
            "state": pv.state,
            "kind": pv.kind,
            "project_id": pv.project_id,
            "owner_id": pv.owner_id,
            "payload": pv.payload,
            "baseline": pv.baseline,
            "impact": pv.impact,
            "decision_id": pv.decision_id,
            "created_at": pv.created_at,
            "resolved_at": pv.resolved_at,
            "expires_at": pv.expires_at,
        }


def _count(table: str) -> int:
    with Session() as s:
        return s.scalar(text(f"SELECT COUNT(*) FROM {table}"))


def _adopt_previews(pid) -> list:
    """本项目 script_adopt 预览行（ChangePreview 属 FK 环，跨测试不保证被
    drop_all 清空，故一律按项目 + kind 过滤）。"""
    with Session() as s:
        return [
            p
            for p in s.scalars(
                select(ChangePreview).where(ChangePreview.project_id == pid)
            ).all()
            if p.kind == "script_adopt"
        ]


def _adopt_decisions(pid) -> list:
    """本项目 script_adoption 终局决定行（ReviewDecision 亦被片段确认等命令写入，
    且属 FK 环不被清空，故按项目 + target_ref.kind 过滤）。"""
    with Session() as s:
        return [
            d
            for d in s.scalars(
                select(ReviewDecision).where(ReviewDecision.project_id == pid)
            ).all()
            if json.loads(d.target_ref).get("kind") == "script_adoption"
        ]


def _cross_counts() -> dict:
    """跨域负向断言的表行数（预览/应用均不得写入任何一行）。"""
    return {
        "shots": _count("studio_shots"),
        "shot_revisions": _count("studio_shot_revisions"),
        "jobs": _count("studio_jobs"),
        "media": _count("studio_media_artifacts"),
        "edit_instances": _count("studio_edit_instances"),
        "releases": _count("studio_releases"),
    }


def _user_id(username) -> str:
    with Session() as s:
        return s.execute(
            text("SELECT id FROM users WHERE username = :u"), {"u": username}
        ).scalar()


def _object_rows() -> dict:
    with Session() as s:
        return {
            o.id: (o.current_revision_id, o.seq, o.kind, o.updated_at, o.retired_at)
            for o in s.scalars(select(ScriptObject)).all()
        }


def _revision_rows() -> dict:
    with Session() as s:
        return {
            r.id: (r.revision, r.text, r.speaker, r.adaptation_note, r.created_at)
            for r in s.scalars(select(ScriptRevision)).all()
        }


def _content_hash(pid, fid) -> str:
    """独立重算冻结的 content_hash（对象按 seq 升序）。"""
    with Session() as s:
        objs = s.scalars(
            select(ScriptObject)
            .where(ScriptObject.fragment_id == fid)
            .order_by(ScriptObject.seq)
        ).all()
        parts = []
        for o in objs:
            rv = s.get(ScriptRevision, o.current_revision_id)
            parts.append(
                f"{rv.revision}:{hashlib.sha256(rv.text.encode('utf-8')).hexdigest()}"
            )
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def _baseline_digest(baseline_text: str) -> str:
    return hashlib.sha256(
        json.dumps(
            json.loads(baseline_text), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


# ── t_sc6_01 无 token → 401（preview/apply 各一）─────────────────────────────

def test_sc6_01_no_token_401(db, client):
    pid = "01" * 13
    fid = "02" * 13
    r1 = client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/script-adopt",
        json={"target_fragment_id": fid, "command_id": _cmd()},
    )
    assert r1.status_code == 401, r1.text
    assert r1.json()["error"]["code"] == "unauthenticated"
    r2 = client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/script-adopt/apply",
        json={
            "preview_id": "03" * 13,
            "command_id": _cmd(),
            "expected_previous_adoption_id": None,
            "expected_object_revisions": [],
        },
    )
    assert r2.status_code == 401, r2.text
    assert r2.json()["error"]["code"] == "unauthenticated"


# ── t_sc6_02 preview 成功：DTO/baseline/TTL/零业务副作用 ────────────────────

def test_sc6_02_preview_success_dto_baseline_ttl_zero_side_effects(db, client):
    token = _register(client, "sc6_02_alice")
    pid, fid = _adopt_setup(client, token)
    objs = _expected_objects(pid, fid)
    before = _counts()
    cross_before = _cross_counts()

    r = _adopt_preview(client, token, pid, fid)
    assert r.status_code == 200, r.text
    body = r.json()
    # 恰 4 字段（00 §5.1 冻结形状）
    assert set(body) == {"preview_id", "kind", "baseline", "impact"}
    assert body["kind"] == "script_adopt"
    assert re.fullmatch(r"[0-9a-z]{26}", body["preview_id"])
    # baseline 精确 = 对象按 seq 升序（无链尾则只有对象项）
    assert body["baseline"] == [
        {"kind": "script_object", "id": o["object_id"], "revision": o["revision"]}
        for o in objs
    ]
    assert body["impact"] == {"affected": [], "needs_review": [], "preservable": []}
    assert set(body["impact"]) == {"affected", "needs_review", "preservable"}

    pv = _preview_row(body["preview_id"])
    assert pv["kind"] == "script_adopt"
    assert pv["state"] == "pending"
    assert pv["decision_id"] is None and pv["resolved_at"] is None
    assert pv["owner_id"] == _user_id("sc6_02_alice")
    assert pv["project_id"] == pid
    assert json.loads(pv["payload"]) == {"target_fragment_id": fid}
    assert json.loads(pv["baseline"]) == body["baseline"]
    assert json.loads(pv["impact"]) == body["impact"]
    assert 1700 < pv["expires_at"] - pv["created_at"] < 1900  # 30 分钟 TTL 窗

    # 零业务副作用：对象/版本/采用不变；commands +1；preview +1；跨域零写
    after = _counts()
    assert after["objects"] == before["objects"]
    assert after["revisions"] == before["revisions"]
    assert after["adoptions"] == 0
    assert after["commands"] == before["commands"] + 1
    assert len(_adopt_previews(pid)) == 1
    assert _cross_counts() == cross_before


# ── t_sc6_03 apply 成功：DTO 逐字段 + DB + ReviewDecision + 负向断言 ─────────

def test_sc6_03_apply_success_dto_db_decision_no_writes(db, client):
    token = _register(client, "sc6_03_bob")
    pid, fid = _adopt_setup(client, token)
    objs = _expected_objects(pid, fid)
    rp = _adopt_preview(client, token, pid, fid)
    assert rp.status_code == 200, rp.text
    preview_id = rp.json()["preview_id"]

    obj_before = _object_rows()
    rev_before = _revision_rows()
    cross_before = _cross_counts()
    commands_before = _count("studio_command_records")

    ra = _adopt_apply(client, token, pid, fid, preview_id, expected_object_revisions=objs)
    assert ra.status_code == 200, ra.text
    body = ra.json()
    aid = body["object_ref"]["id"]
    assert set(body) == {
        "object_ref", "fragment", "object_revisions", "content_hash",
        "previous_adoption_id", "created_at",
    }
    assert body["object_ref"] == {"kind": "script_adoption", "id": aid, "revision": None}
    assert re.fullmatch(r"[0-9a-z]{26}", aid)
    assert body["fragment"] == {"kind": "fragment", "id": fid, "revision": 1}
    assert body["object_revisions"] == objs
    assert body["content_hash"] == _content_hash(pid, fid)  # 独立重算
    assert re.fullmatch(r"[0-9a-f]{64}", body["content_hash"])
    assert body["previous_adoption_id"] is None
    assert isinstance(body["created_at"], float)

    with Session() as s:
        ad = s.get(ScriptAdoption, aid)
    assert ad is not None
    assert ad.project_id == pid and ad.fragment_id == fid
    assert json.loads(ad.object_revisions) == objs
    assert ad.content_hash == body["content_hash"]
    assert ad.previous_adoption_id is None
    assert ad.created_at == body["created_at"]

    pv = _preview_row(preview_id)
    assert pv["state"] == "applied"
    assert pv["resolved_at"] is not None and pv["decision_id"] is not None

    decisions = _adopt_decisions(pid)  # 本项目 script_adoption 决定
    assert len(decisions) == 1  # 恰 1 行
    d = decisions[0]
    assert d.decision == "applied"
    assert d.preview_id == preview_id
    assert d.id == pv["decision_id"]
    assert d.owner_id == _user_id("sc6_03_bob")
    assert json.loads(d.target_ref) == {
        "kind": "script_adoption", "id": aid, "revision": None,
    }
    assert d.baseline_digest == _baseline_digest(pv["baseline"])  # 独立重算

    assert _count("studio_command_records") == commands_before + 1
    assert _count("studio_script_adoptions") == 1
    # 负向：对象/版本行逐字段不变；跨域（分镜/任务/媒体/剪辑/发布）零写
    assert _object_rows() == obj_before
    assert _revision_rows() == rev_before
    assert _cross_counts() == cross_before


# ── t_sc6_04 项目/片段/preview 404（不泄漏存在性，同形状）────────────────────

def test_sc6_04_not_found_shapes(db, client):
    ta = _register(client, "sc6_04_alice")
    tb = _register(client, "sc6_04_bob")
    pid_a, fid_a = _adopt_setup(client, ta, "T04A")
    pid_b, fid_b = _adopt_setup(client, tb, "T04B")
    objs_a = _expected_objects(pid_a, fid_a)
    ghost_pid = "01" * 13
    ghost_fid = "02" * 13
    ghost_pv = "03" * 13

    # 项目 404：preview + apply（不存在）
    r = _adopt_preview(client, ta, ghost_pid, ghost_fid)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "project", "id": ghost_pid}
    r = _adopt_apply(
        client, ta, ghost_pid, ghost_fid, ghost_pv, expected_object_revisions=[]
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {"kind": "project", "id": ghost_pid}

    # 项目 404：非属主（tb 的项目，ta 访问）
    r = _adopt_preview(client, ta, pid_b, fid_b)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {"kind": "project", "id": pid_b}

    # 片段 404：不存在 / 跨项目 → 同形状
    for fid_bad in (ghost_fid, fid_b):
        r = _adopt_preview(client, ta, pid_a, fid_bad)
        assert r.status_code == 404, (fid_bad, r.text)
        assert r.json()["error"]["details"] == {"kind": "fragment", "id": fid_bad}
    r = _adopt_apply(
        client, ta, pid_a, fid_b, ghost_pv, expected_object_revisions=[]
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": fid_b}

    # preview 404：不存在 / 跨项目 → 同形状
    r = _adopt_apply(
        client, ta, pid_a, fid_a, ghost_pv, expected_object_revisions=objs_a
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {"kind": "preview", "id": ghost_pv}
    rp_b = _adopt_preview(client, tb, pid_b, fid_b)
    assert rp_b.status_code == 200, rp_b.text
    r = _adopt_apply(
        client, ta, pid_a, fid_a, rp_b.json()["preview_id"],
        expected_object_revisions=objs_a,
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {
        "kind": "preview", "id": rp_b.json()["preview_id"],
    }
    # 全程零采用写入
    assert _count("studio_script_adoptions") == 0


# ── t_sc6_05 target/kind mismatch 422 ───────────────────────────────────────

def test_sc6_05_target_mismatch_422(db, client):
    token = _register(client, "sc6_05_carol")
    pid, fid = _adopt_setup(client, token)
    rev = None
    # preview：target != 路径 fid
    r = _adopt_preview(client, token, pid, fid, target="04" * 13)
    assert r.status_code == 422, r.text
    v0 = r.json()["error"]["details"]["violations"][0]
    assert v0["field"] == "target_fragment_id" and v0["rule"] == "mismatch"
    assert len(_adopt_previews(pid)) == 0

    # apply：preview.kind != script_adopt（F7 retire 预览行）→ mismatch
    rp = client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/retire",
        json={"target_fragment_id": fid, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert rp.status_code == 200, rp.text
    ra = _adopt_apply(client, token, pid, fid, rp.json()["preview_id"])
    assert ra.status_code == 422, ra.text
    v0 = ra.json()["error"]["details"]["violations"][0]
    assert v0["field"] == "target_fragment_id" and v0["rule"] == "mismatch"

    # apply：payload.target_fragment_id != 路径 fid（同项目另一片段）→ mismatch
    with Session() as s:
        rev = s.scalar(
            select(Fragment.source_revision_id).where(Fragment.id == fid)
        )
    fid2 = _confirmed_fragment(
        client, token, pid, rev, start=10, end=20, name="F2",
        expected=_range_set_cas(pid),
    )
    r2 = _create(client, token, pid, fid2, text="丙")
    assert r2.status_code == 201, r2.text
    rp2 = _adopt_preview(client, token, pid, fid2)
    assert rp2.status_code == 200, rp2.text
    ra2 = _adopt_apply(client, token, pid, fid, rp2.json()["preview_id"])
    assert ra2.status_code == 422, ra2.text
    v0 = ra2.json()["error"]["details"]["violations"][0]
    assert v0["field"] == "target_fragment_id" and v0["rule"] == "mismatch"
    assert _count("studio_script_adoptions") == 0


# ── t_sc6_06 片段门禁四态与顺序 ─────────────────────────────────────────────

def test_sc6_06_gate_order_all_four(db, client):
    token = _register(client, "sc6_06_dave")
    pid = _mk_project(client, token, "Gate")
    rev1 = _import_active(client, token, pid, TEXT_A)
    cand = _mk_fragment(client, token, pid, rev1["id"], start=0, end=5, name="cand")
    empty = _confirmed_fragment(
        client, token, pid, rev1["id"], start=5, end=10, name="empty",
        expected=_range_set_cas(pid),
    )
    empty2 = _confirmed_fragment(
        client, token, pid, rev1["id"], start=10, end=15, name="empty2",
        expected=_range_set_cas(pid),
    )
    withobj = _confirmed_fragment(
        client, token, pid, rev1["id"], start=15, end=20, name="withobj",
        expected=_range_set_cas(pid),
    )
    assert _create(client, token, pid, withobj, text="对象").status_code == 201

    def _gate(fid):
        r = _adopt_preview(client, token, pid, fid)
        assert r.status_code == 422, (fid, r.text)
        err = r.json()["error"]
        assert err["code"] == "precondition_failed"
        return err["details"]["reason"], err["details"]["blocked_by"]

    # (a) candidate（无对象）→ not_confirmed（门禁 4 先于 6）
    reason, blocked = _gate(cand)
    assert reason == "not_confirmed"
    assert blocked == {"kind": "fragment", "id": cand, "revision": 1}
    # (b) confirmed + active + 无对象 → no_objects
    assert _gate(empty)[0] == "no_objects"
    # (c) confirmed + active + 有对象，退役后 → retired（置末）
    _retire(client, token, pid, withobj)
    assert _gate(withobj)[0] == "retired"
    # (d) retired 但无对象 → no_objects（门禁 6 先于 7）
    _retire(client, token, pid, empty)
    assert _gate(empty)[0] == "no_objects"
    # 换版：rev1 全失效
    r2 = client.post(
        f"/api/studio/projects/{pid}/sources",
        json={"content": TEXT_B, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r2.status_code == 201, r2.text
    _activate(client, token, pid, r2.json()["id"], rev1["id"])
    # (e) candidate + 非 active → not_confirmed（门禁 4 先于 5）
    reason, blocked = _gate(cand)
    assert reason == "not_confirmed"
    assert blocked["revision"] == 1
    # (f) confirmed + 非 active + 无对象 → source_revision_inactive（门禁 5 先于 6）
    reason, blocked = _gate(empty2)
    assert reason == "source_revision_inactive"
    assert blocked == {"kind": "fragment", "id": empty2, "revision": 1}
    assert len(_adopt_previews(pid)) == 0


# ── t_sc6_07 preview 幂等（重放 200 / 异内容 reused）────────────────────────

def test_sc6_07_preview_idempotent_replay_and_reused(db, client):
    token = _register(client, "sc6_07_erin")
    pid, fid = _adopt_setup(client, token)
    with Session() as s:
        rev = s.scalar(select(Fragment.source_revision_id).where(Fragment.id == fid))
    fid2 = _confirmed_fragment(
        client, token, pid, rev, start=10, end=20, name="F2",
        expected=_range_set_cas(pid),
    )
    assert _create(client, token, pid, fid2, text="丙").status_code == 201

    cmd = _cmd()
    r1 = _adopt_preview(client, token, pid, fid, command_id=cmd)
    assert r1.status_code == 200, r1.text
    before = _count("studio_command_records")
    r2 = _adopt_preview(client, token, pid, fid, command_id=cmd)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()  # 原样返回首次 result_payload
    assert len(_adopt_previews(pid)) == 1  # 零副作用
    assert _count("studio_command_records") == before

    # 同 command_id、异内容（不同片段）→ 422 reused
    r3 = _adopt_preview(client, token, pid, fid2, command_id=cmd)
    assert r3.status_code == 422, r3.text
    v0 = r3.json()["error"]["details"]["violations"][0]
    assert v0["field"] == "command_id" and v0["rule"] == "reused"
    assert len(_adopt_previews(pid)) == 1


# ── t_sc6_08 apply 幂等（先于状态检查/ CAS）────────────────────────────────

def test_sc6_08_apply_idempotent_replay_and_reused(db, client):
    token = _register(client, "sc6_08_frank")
    pid, fid = _adopt_setup(client, token)
    objs = _expected_objects(pid, fid)
    rp = _adopt_preview(client, token, pid, fid)
    preview_id = rp.json()["preview_id"]
    cmd = _cmd()
    ra1 = _adopt_apply(
        client, token, pid, fid, preview_id, command_id=cmd,
        expected_object_revisions=objs,
    )
    assert ra1.status_code == 200, ra1.text
    body1 = ra1.json()
    aid = body1["object_ref"]["id"]
    snap = (_count("studio_script_adoptions"), len(_adopt_decisions(pid)),
            _count("studio_command_records"))
    ra2 = _adopt_apply(
        client, token, pid, fid, preview_id, command_id=cmd,
        expected_object_revisions=objs,
    )
    assert ra2.status_code == 200, ra2.text
    assert ra2.json() == body1
    assert (_count("studio_script_adoptions"), len(_adopt_decisions(pid)),
            _count("studio_command_records")) == snap  # 重放零写

    # 同 command_id、异内容（previous_adoption_id 不同）→ 422 reused（先于状态/CAS）
    ra3 = _adopt_apply(
        client, token, pid, fid, preview_id, command_id=cmd,
        expected_previous_adoption_id=aid, expected_object_revisions=objs,
    )
    assert ra3.status_code == 422, ra3.text
    v0 = ra3.json()["error"]["details"]["violations"][0]
    assert v0["field"] == "command_id" and v0["rule"] == "reused"
    assert (_count("studio_script_adoptions"), len(_adopt_decisions(pid)),
            _count("studio_command_records")) == snap


# ── t_sc6_09 expired：409 + 惰性置位（不改写已有终态）──────────────────────

def test_sc6_09_expired_lazy_set_409(db, client):
    token = _register(client, "sc6_09_grace")
    pid, fid = _adopt_setup(client, token)
    objs = _expected_objects(pid, fid)
    rp = _adopt_preview(client, token, pid, fid)
    preview_id = rp.json()["preview_id"]
    # pending 且 now > expires_at（就地压 TTL）
    with Session() as s:
        pv = s.get(ChangePreview, preview_id)
        pv.expires_at = time.time() - 1
        s.commit()
    ra = _adopt_apply(
        client, token, pid, fid, preview_id, expected_object_revisions=objs
    )
    assert ra.status_code == 409, ra.text
    err = ra.json()["error"]
    assert err["code"] == "preview_stale"
    assert err["details"]["preview_id"] == preview_id
    assert err["details"]["conflicts"] == [
        {
            "object": {"kind": "preview", "id": preview_id},
            "expected": "pending",
            "actual": "expired",
        }
    ]
    pv = _preview_row(preview_id)
    assert pv["state"] == "expired" and pv["resolved_at"] is not None

    # 已 expired（终态）→ 同形 409，不再改写
    rp2 = _adopt_preview(client, token, pid, fid)
    preview2 = rp2.json()["preview_id"]
    with Session() as s:
        pv2 = s.get(ChangePreview, preview2)
        pv2.state = "expired"
        pv2.resolved_at = 123.0
        s.commit()
    ra2 = _adopt_apply(
        client, token, pid, fid, preview2, expected_object_revisions=objs
    )
    assert ra2.status_code == 409, ra2.text
    assert ra2.json()["error"]["details"]["conflicts"][0]["actual"] == "expired"
    pv2 = _preview_row(preview2)
    assert pv2["state"] == "expired" and pv2["resolved_at"] == 123.0  # 未改写
    assert _count("studio_script_adoptions") == 0
    assert _adopt_decisions(pid) == []


# ── t_sc6_10 终态（applied/rejected）→ 409 同形且不改写 ─────────────────────

def test_sc6_10_terminal_state_409_no_rewrite(db, client):
    token = _register(client, "sc6_10_heidi")
    pid, fid = _adopt_setup(client, token)
    objs = _expected_objects(pid, fid)
    rp = _adopt_preview(client, token, pid, fid)
    preview_id = rp.json()["preview_id"]
    ra1 = _adopt_apply(
        client, token, pid, fid, preview_id, expected_object_revisions=objs
    )
    assert ra1.status_code == 200, ra1.text
    aid = ra1.json()["object_ref"]["id"]
    # 新 command_id 再 apply → 409 actual='applied'，终态不改写
    ra2 = _adopt_apply(
        client, token, pid, fid, preview_id,
        expected_previous_adoption_id=aid, expected_object_revisions=objs,
    )
    assert ra2.status_code == 409, ra2.text
    err = ra2.json()["error"]
    assert err["code"] == "preview_stale"
    assert err["details"]["conflicts"] == [
        {
            "object": {"kind": "preview", "id": preview_id},
            "expected": "pending",
            "actual": "applied",
        }
    ]
    pv = _preview_row(preview_id)
    assert pv["state"] == "applied"
    assert _count("studio_script_adoptions") == 1
    assert len(_adopt_decisions(pid)) == 1

    # rejected 终态 → 409 actual='rejected'，不改写
    rp3 = _adopt_preview(client, token, pid, fid)
    preview3 = rp3.json()["preview_id"]
    with Session() as s:
        pv3 = s.get(ChangePreview, preview3)
        pv3.state = "rejected"
        pv3.resolved_at = 456.0
        s.commit()
    ra3 = _adopt_apply(
        client, token, pid, fid, preview3,
        expected_previous_adoption_id=aid, expected_object_revisions=objs,
    )
    assert ra3.status_code == 409, ra3.text
    assert ra3.json()["error"]["details"]["conflicts"][0]["actual"] == "rejected"
    pv3 = _preview_row(preview3)
    assert pv3["state"] == "rejected" and pv3["resolved_at"] == 456.0
    assert _count("studio_script_adoptions") == 1


# ── t_sc6_11 CAS 失配 409（expected/actual 精确，无半次保存）────────────────

def test_sc6_11_cas_conflict_409_exact_no_half_save(db, client):
    token = _register(client, "sc6_11_ivan")
    pid, fid = _adopt_setup(client, token)
    objs = _expected_objects(pid, fid)

    # (a) 无链尾：expected != None（实际链尾 None）→ object.id == ""
    rp = _adopt_preview(client, token, pid, fid)
    preview_id = rp.json()["preview_id"]
    ghost = "04" * 13
    ra = _adopt_apply(
        client, token, pid, fid, preview_id,
        expected_previous_adoption_id=ghost, expected_object_revisions=objs,
    )
    assert ra.status_code == 409, ra.text
    err = ra.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"] == {
        "object": {"kind": "script_adoption", "id": ""},
        "expected": ghost,
        "actual": None,
    }
    assert _count("studio_script_adoptions") == 0
    assert _preview_row(preview_id)["state"] == "pending"  # 无半次保存

    # (b) 有链尾：expected 失配 → object.id/actual == 真实链尾
    ra1 = _adopt_apply(
        client, token, pid, fid, preview_id, expected_object_revisions=objs
    )
    assert ra1.status_code == 200, ra1.text
    aid1 = ra1.json()["object_ref"]["id"]
    rp2 = _adopt_preview(client, token, pid, fid)
    preview2 = rp2.json()["preview_id"]
    ra2 = _adopt_apply(
        client, token, pid, fid, preview2,
        expected_previous_adoption_id=ghost, expected_object_revisions=objs,
    )
    assert ra2.status_code == 409, ra2.text
    assert ra2.json()["error"]["details"] == {
        "object": {"kind": "script_adoption", "id": aid1},
        "expected": ghost,
        "actual": aid1,
    }
    assert _count("studio_script_adoptions") == 1
    assert len(_adopt_decisions(pid)) == 1
    assert _preview_row(preview2)["state"] == "pending"


# ── t_sc6_12 baseline 失配 409 + preview superseded ─────────────────────────

def test_sc6_12_baseline_mismatch_409_superseded(db, client):
    token = _register(client, "sc6_12_judy")
    pid, fid = _adopt_setup(client, token)
    objs = _expected_objects(pid, fid)
    oid = objs[0]["object_id"]
    rp = _adopt_preview(client, token, pid, fid)
    preview_id = rp.json()["preview_id"]
    # preview 后修订对象 → 快照失配（revision 1 → 2）
    rr = _revise(
        client, token, pid, oid,
        {"text": "改", "expected_revision": 1, "command_id": _cmd()},
    )
    assert rr.status_code == 200, rr.text
    obj_before = _object_rows()
    rev_before = _revision_rows()

    ra = _adopt_apply(
        client, token, pid, fid, preview_id, expected_object_revisions=objs
    )
    assert ra.status_code == 409, ra.text
    err = ra.json()["error"]
    assert err["code"] == "preview_stale"
    assert err["details"]["preview_id"] == preview_id
    conflicts = err["details"]["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["object"] == {"kind": "script_object", "id": oid}
    assert conflicts[0]["expected"]["revision"] == 1
    assert conflicts[0]["actual"]["revision"] == 2
    pv = _preview_row(preview_id)
    assert pv["state"] == "superseded" and pv["resolved_at"] is not None
    # 无半次保存：零采用/零决定，对象快照已变（该变更来自 revise，非本命令）
    assert _count("studio_script_adoptions") == 0
    assert _adopt_decisions(pid) == []
    assert _object_rows() == obj_before
    assert _revision_rows() == rev_before


# ── t_sc6_13 apply 片段门禁重校验 → 409 preview_stale ──────────────────────

def test_sc6_13_apply_gate_recheck_409(db, client):
    token = _register(client, "sc6_13_karen")
    pid, fid = _adopt_setup(client, token)
    objs = _expected_objects(pid, fid)
    rp = _adopt_preview(client, token, pid, fid)
    preview_id = rp.json()["preview_id"]
    # preview 后片段被退役 → apply 门禁重校验 retired
    _retire(client, token, pid, fid)
    ra = _adopt_apply(
        client, token, pid, fid, preview_id, expected_object_revisions=objs
    )
    assert ra.status_code == 409, ra.text
    err = ra.json()["error"]
    assert err["code"] == "preview_stale"
    assert err["details"]["preview_id"] == preview_id
    assert err["details"]["conflicts"] == [
        {
            "object": {"kind": "fragment", "id": fid},
            "expected": "confirmed",
            "actual": "retired",
        }
    ]
    assert _count("studio_script_adoptions") == 0
    assert _adopt_decisions(pid) == []
    assert _preview_row(preview_id)["state"] == "pending"  # 门禁失配不置终态


# ── t_sc6_14 采用链：baseline 追加链尾 ref、previous_adoption_id、旧行不变 ──

def test_sc6_14_adoption_chain_tail(db, client):
    token = _register(client, "sc6_14_laura")
    pid, fid = _adopt_setup(client, token)
    objs = _expected_objects(pid, fid)

    rp1 = _adopt_preview(client, token, pid, fid)
    assert rp1.status_code == 200, rp1.text
    assert rp1.json()["baseline"] == [
        {"kind": "script_object", "id": o["object_id"], "revision": o["revision"]}
        for o in objs
    ]  # 首个采用前无链尾
    ra1 = _adopt_apply(
        client, token, pid, fid, rp1.json()["preview_id"],
        expected_object_revisions=objs,
    )
    assert ra1.status_code == 200, ra1.text
    aid1 = ra1.json()["object_ref"]["id"]
    assert ra1.json()["previous_adoption_id"] is None

    # 第一次采用后：baseline 末尾追加链尾 adoption ref
    rp2 = _adopt_preview(client, token, pid, fid)
    assert rp2.status_code == 200, rp2.text
    assert rp2.json()["baseline"] == [
        {"kind": "script_object", "id": o["object_id"], "revision": o["revision"]}
        for o in objs
    ] + [{"kind": "script_adoption", "id": aid1, "revision": None}]

    with Session() as s:
        old_row = s.get(ScriptAdoption, aid1)
        old_snapshot = (
            old_row.project_id, old_row.fragment_id, old_row.object_revisions,
            old_row.content_hash, old_row.previous_adoption_id, old_row.created_at,
        )
    objs_before = _object_rows()
    revs_before = _revision_rows()

    ra2 = _adopt_apply(
        client, token, pid, fid, rp2.json()["preview_id"],
        expected_previous_adoption_id=aid1, expected_object_revisions=objs,
    )
    assert ra2.status_code == 200, ra2.text
    body2 = ra2.json()
    aid2 = body2["object_ref"]["id"]
    assert aid2 != aid1
    assert body2["previous_adoption_id"] == aid1  # 链尾正确
    assert body2["content_hash"] == _content_hash(pid, fid)
    assert body2["created_at"] > ra1.json()["created_at"]
    with Session() as s:
        ad2 = s.get(ScriptAdoption, aid2)
        assert ad2.previous_adoption_id == aid1
        # 旧 adoption 行逐字段不变（R11：旧采用不被改写）
        old_row = s.get(ScriptAdoption, aid1)
        assert (
            old_row.project_id, old_row.fragment_id, old_row.object_revisions,
            old_row.content_hash, old_row.previous_adoption_id, old_row.created_at,
        ) == old_snapshot
    assert _count("studio_script_adoptions") == 2
    assert len(_adopt_decisions(pid)) == 2
    # 对象/版本行不变
    assert _object_rows() == objs_before
    assert _revision_rows() == revs_before


# ── SC6 impact 种子（跨域只读验证）──────────────────────────────────────────

def _seed_sc6_shot(pid, adoption_id, seq) -> dict:
    now = time.time()
    scene_id, shot_id, shot_rev_id = make_ulid(), make_ulid(), make_ulid()
    with Session() as s:
        s.execute(
            text(
                "INSERT INTO studio_scenes (id, project_id, name, seq, "
                "layout_cas_revision, created_at, updated_at) "
                "VALUES (:id, :pid, :name, :seq, 1, :now, :now)"
            ),
            {"id": scene_id, "pid": pid, "name": f"S{seq}", "seq": seq, "now": now},
        )
        s.execute(
            text(
                "INSERT INTO studio_shots (id, project_id, scene_id, seq, name, "
                "script_adoption_id, current_revision_id, created_at, updated_at) "
                "VALUES (:id, :pid, :scene, 1, :name, :ad, :cur, :now, :now)"
            ),
            {
                "id": shot_id, "pid": pid, "scene": scene_id, "name": f"SH{seq}",
                "ad": adoption_id, "cur": shot_rev_id, "now": now,
            },
        )
        s.execute(
            text(
                "INSERT INTO studio_shot_revisions (id, shot_id, revision, visual, "
                "action, dialogue, transition_in, transition_out, duration_hint_ms, "
                "created_at) VALUES (:id, :sid, 1, '{}', '{}', NULL, NULL, NULL, "
                "NULL, :now)"
            ),
            {"id": shot_rev_id, "sid": shot_id, "now": now},
        )
        s.commit()
    return {"shot_id": shot_id, "shot_rev_id": shot_rev_id}


def _seed_sc6_job_chain(pid, uid, needle, status, seq) -> dict:
    """种子：StudioJob(payload 含 needle, status) → MediaArtifact → EditInstance
    → Release(poster=media)；返回各 id。"""
    now = time.time()
    job_id, media_id = make_ulid(), make_ulid()
    draft_id, inst_id = make_ulid(), make_ulid()
    ce_id, rel_id = make_ulid(), make_ulid()
    with Session() as s:
        s.execute(
            text(
                "INSERT INTO studio_jobs (id, project_id, owner_id, kind, ref_kind, "
                "ref_id, payload, status, priority, created_at, submitted_by, "
                "attempts, usage, last_error, finished_at) VALUES (:id, :pid, :uid, "
                "'shot_generate', NULL, NULL, :payload, :status, 0, :now, :uid, 0, "
                "NULL, NULL, NULL)"
            ),
            {
                "id": job_id, "pid": pid, "uid": uid,
                "payload": json.dumps(
                    {"script_adoption_id": needle, "fragment_id": needle}
                ),
                "status": status, "now": now,
            },
        )
        s.execute(
            text(
                "INSERT INTO studio_media_artifacts (id, project_id, media_kind, "
                "filename, size_bytes, sha256, duration_ms, width, height, source, "
                "source_job_id, created_at) VALUES (:id, :pid, 'video', :fn, 12, "
                ":sha, NULL, NULL, NULL, 'shot_generate', :jid, :now)"
            ),
            {
                "id": media_id, "pid": pid, "fn": f"{seq}.mp4",
                "sha": "ab" * 32, "jid": job_id, "now": now,
            },
        )
        s.execute(
            text(
                "INSERT INTO studio_edit_drafts (id, project_id, edit_id, "
                "edit_revision, manifest, manifest_digest, cas_revision, updated_at) "
                "VALUES (:id, :pid, :eid, 1, '{}', :dig, 1, :now)"
            ),
            {
                "id": draft_id, "pid": pid, "eid": make_ulid(),
                "dig": "cd" * 32, "now": now,
            },
        )
        s.execute(
            text(
                "INSERT INTO studio_edit_instances (id, draft_id, instance_id, "
                "media_id, source_in_ms, source_out_ms, speed, timeline_start_ms, "
                "track_id) VALUES (:id, :draft, :iid, :media, 0, 1000, 1.0, 0, 0)"
            ),
            {
                "id": inst_id, "draft": draft_id, "iid": make_ulid(),
                "media": media_id,
            },
        )
        s.execute(
            text(
                "INSERT INTO studio_confirmed_edits (id, edit_id, edit_revision, "
                "manifest, manifest_digest, output_spec, created_at) VALUES (:id, "
                ":eid, 1, '{}', :dig, '{}', :now)"
            ),
            {"id": ce_id, "eid": make_ulid(), "dig": "ef" * 32, "now": now},
        )
        s.execute(
            text(
                "INSERT INTO studio_releases (id, project_id, name, "
                "confirmed_edit_id, poster_artifact_id, status, "
                "predecessor_release_id, public_dir, published_at, created_at, "
                "updated_at, cas_revision) VALUES (:id, :pid, :name, :ce, :poster, "
                "'draft', NULL, NULL, NULL, :now, :now, 1)"
            ),
            {
                "id": rel_id, "pid": pid, "name": f"R{seq}", "ce": ce_id,
                "poster": media_id, "now": now,
            },
        )
        s.commit()
    return {
        "job_id": job_id, "media_id": media_id,
        "edit_instance_id": inst_id, "release_id": rel_id,
    }


# ── t_sc6_15 impact 三类清单（affected/needs_review/preservable）────────────

def test_sc6_15_impact_lists_cross_domain(db, client):
    token = _register(client, "sc6_15_mallory")
    pid, fid = _adopt_setup(client, token)
    uid = _user_id("sc6_15_mallory")
    objs = _expected_objects(pid, fid)
    # 先产生一条采用（链尾），作为旧采用
    rp0 = _adopt_preview(client, token, pid, fid)
    ra0 = _adopt_apply(
        client, token, pid, fid, rp0.json()["preview_id"],
        expected_object_revisions=objs,
    )
    assert ra0.status_code == 200, ra0.text
    aid1 = ra0.json()["object_ref"]["id"]

    shot = _seed_sc6_shot(pid, aid1, seq=1)
    job_run = _seed_sc6_job_chain(pid, uid, aid1, "running", seq=1)
    job_ok = _seed_sc6_job_chain(pid, uid, fid, "succeeded", seq=2)

    rp = _adopt_preview(client, token, pid, fid)
    assert rp.status_code == 200, rp.text
    impact = rp.json()["impact"]
    assert set(impact) == {"affected", "needs_review", "preservable"}
    assert impact["affected"] == [
        {"object_ref": {"kind": "shot", "id": shot["shot_id"], "revision": 1}}
    ]
    # needs_review 仅 pending/running（succeeded 的 job 不入）
    assert impact["needs_review"] == [
        {
            "object_ref": {"kind": "job", "id": job_run["job_id"], "revision": None},
            "reason": "frozen_input_references_previous_adoption",
        }
    ]
    # preservable = 全 job 组的媒体证据链，按 id 升序
    expected_preservable = sorted(
        [
            {"object_ref": {"kind": "media_artifact", "id": job_run["media_id"], "revision": None}},
            {"object_ref": {"kind": "edit_instance", "id": job_run["edit_instance_id"], "revision": None}},
            {"object_ref": {"kind": "release", "id": job_run["release_id"], "revision": None}},
            {"object_ref": {"kind": "media_artifact", "id": job_ok["media_id"], "revision": None}},
            {"object_ref": {"kind": "edit_instance", "id": job_ok["edit_instance_id"], "revision": None}},
            {"object_ref": {"kind": "release", "id": job_ok["release_id"], "revision": None}},
        ],
        key=lambda x: x["object_ref"]["id"],
    )
    assert impact["preservable"] == expected_preservable
    # baseline 末尾追加旧采用 ref
    assert rp.json()["baseline"][-1] == {
        "kind": "script_adoption", "id": aid1, "revision": None,
    }
    # 预览仍零业务副作用
    assert _count("studio_script_adoptions") == 1
    assert len(_adopt_decisions(pid)) == 1
    assert len(_adopt_previews(pid)) == 2

