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
import re
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.core.db import Base, Session, engine
from backend.studio.contracts.models import CommandRecord
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
