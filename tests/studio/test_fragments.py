"""F1 原子保存候选片段（附录 02 §2 F1）—— POST /api/studio/projects/{pid}/fragments 行为验证；
F4 片段就地改名（附录 02 §2 F4）—— POST /api/studio/projects/{pid}/fragments/{fid}/rename 行为验证；
F6 确认片段（附录 02 §2 F6）—— POST /api/studio/projects/{pid}/fragments/{fid}/confirm 行为验证。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件不自行改写 STUDIO_* env。db fixture 与 register/auth helper 照抄
tests/studio/test_sources.py 的实现；测试参数 (db, client) 序。
每测先建项目 + S1 导入一版作为 active 正文。
"""
import hashlib
import json
import threading
import time
import uuid

import pytest
from sqlalchemy import select, text, update

from backend.core.db import Base, Session, engine
from backend.studio.reviews.models import ReviewDecision
from backend.studio.sources.fragment_models import Fragment, FragmentRevision, RangeSet

# 基准正文
TEXT_A = "01234567890123456789"  # 20 字符，无空白
TEXT_B = "aa🚀aa"  # UTF-16 长 6；索引 2/3 为 🚀 的代理对
TEXT_C = "   "  # 3 空格，blank 用例


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


def _create_project(client, token, name) -> str:
    """P1 建项目，返回项目 id。"""
    r = client.post(
        "/api/studio/projects",
        json={"name": name, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _import(client, token, pid, content, command_id=None):
    return client.post(
        f"/api/studio/projects/{pid}/sources",
        json={"content": content, "command_id": command_id or _cmd()},
        headers=_auth(token),
    )


def _create_fragment(client, token, pid, source_revision_id, start, end, name, expected=None, command_id=None):
    return client.post(
        f"/api/studio/projects/{pid}/fragments",
        json={
            "source_revision_id": source_revision_id,
            "range": {"start": start, "end": end},
            "name": name,
            "expected_range_set_revision": expected,
            "command_id": command_id or _cmd(),
        },
        headers=_auth(token),
    )


def _confirm(client, token, pid, fid, expected, command_id=None):
    return client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/confirm",
        json={"expected_revision": expected, "command_id": command_id or _cmd()},
        headers=_auth(token),
    )


def _rename(client, token, pid, fid, name, expected, command_id=None):
    return client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/rename",
        json={"name": name, "expected_revision": expected, "command_id": command_id or _cmd()},
        headers=_auth(token),
    )


def _count(table) -> int:
    """按表名计数（表名为冻结常量，无注入面）。"""
    with Session() as s:
        return s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def _range_sets(pid, source_revision_id=None):
    with Session() as s:
        stmt = select(RangeSet).where(RangeSet.project_id == pid)
        if source_revision_id is not None:
            stmt = stmt.where(RangeSet.source_revision_id == source_revision_id)
        return s.execute(stmt).scalars().all()


def _fragments(pid):
    with Session() as s:
        return s.execute(select(Fragment).where(Fragment.project_id == pid)).scalars().all()


def _fragment_revisions(fragment_id):
    with Session() as s:
        return s.execute(
            select(FragmentRevision).where(FragmentRevision.fragment_id == fragment_id)
        ).scalars().all()


def _import_active(client, token, pid, content) -> dict:
    """S1 导入并返回 201 修订体（首版自动激活）。"""
    r = _import(client, token, pid, content)
    assert r.status_code == 201, r.text
    return r.json()


def _activate(client, token, pid, target_revision_id, expected_active_revision_id) -> dict:
    """S4 preview → S5 apply 真实激活 target 版本（两步均断言 200），返回新项目 DTO。"""
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
    return ra.json()


# T1 无 token → 401
def test_t1_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/01010101010101010101010101/fragments",
        json={
            "source_revision_id": "02" * 13,
            "range": {"start": 0, "end": 1},
            "name": "x",
            "command_id": _cmd(),
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


# T2 pid 不存在（26 字符 ULID）→ 404 not_found，details={kind:"project", id:pid}
def test_t2_nonexistent_pid_404(db, client):
    token = _register(client, "f1_t2_alice")
    pid = "01" * 13
    r = _create_fragment(client, token, pid, "02" * 13, 0, 1, "x")
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "project", "id": pid}


# T3 source_revision_id 非本项目（他人项目修订，26 字符）→ 404 not_found kind=source
def test_t3_cross_project_revision_404(db, client):
    ta = _register(client, "f1_t3_bob")
    tc = _register(client, "f1_t3_carol")
    pa = _create_project(client, ta, "T3ProjA")
    pc = _create_project(client, tc, "T3ProjC")
    r1 = _import(client, tc, pc, "carol content")
    assert r1.status_code == 201, r1.text
    rid = r1.json()["id"]
    assert len(rid) == 26
    r = _create_fragment(client, ta, pa, rid, 0, 3, "x")
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "source", "id": rid}


# T4 非 active 版本（v1+v2 后对 v1 建片段）→ 422 precondition_failed
def test_t4_inactive_source_revision_422(db, client):
    token = _register(client, "f1_t4_dave")
    pid = _create_project(client, token, "T4Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    time.sleep(0.002)  # 保证 ULID 严格递增
    r2 = _import(client, token, pid, "second version content")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()
    assert v2["is_active"] is False
    # 真实激活：S4 preview → S5 apply 把项目 active 从 v1 切到 v2，
    # 构造"v1 非 active"状态
    _activate(client, token, pid, v2["id"], expected_active_revision_id=v1["id"])
    r = _create_fragment(client, token, pid, v1["id"], 0, 5, "x")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    assert err["details"]["blocked_by"]["kind"] == "source"
    assert err["details"]["blocked_by"]["id"] == v2["id"]


# T5 name：""→required；"x"*121→max_length；" x "→201 保存 "x"
def test_t5_name_validation(db, client):
    token = _register(client, "f1_t5_erin")
    pid = _create_project(client, token, "T5Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    r1 = _create_fragment(client, token, pid, rev["id"], 0, 5, "")
    assert r1.status_code == 422, r1.text
    v0 = r1.json()["error"]["details"]["violations"][0]
    assert v0["field"] == "name"
    assert v0["rule"] == "required"
    assert v0["message"] == "片段名不能为空。"
    r2 = _create_fragment(client, token, pid, rev["id"], 0, 5, "x" * 121)
    assert r2.status_code == 422, r2.text
    v1 = r2.json()["error"]["details"]["violations"][0]
    assert v1["field"] == "name"
    assert v1["rule"] == "max_length"
    assert v1["message"] == "片段名最长 120 个字符。"
    r3 = _create_fragment(client, token, pid, rev["id"], 0, 5, " x ")
    assert r3.status_code == 201, r3.text
    assert r3.json()["name"] == "x"  # 保存 trimmed
    frags = _fragments(pid)
    assert len(frags) == 1 and frags[0].name == "x"


# T6 range {5,5} → 422 range_invalid rule=empty
def test_t6_empty_range_422(db, client):
    token = _register(client, "f1_t6_frank")
    pid = _create_project(client, token, "T6Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    r = _create_fragment(client, token, pid, rev["id"], 5, 5, "x")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "range_invalid"
    assert err["details"]["rule"] == "empty"


# T7 {0,25}（正文 A 长 20）→ 422 rule=out_of_bounds
def test_t7_out_of_bounds_422(db, client):
    token = _register(client, "f1_t7_grace")
    pid = _create_project(client, token, "T7Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    r = _create_fragment(client, token, pid, rev["id"], 0, 25, "x")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "range_invalid"
    assert err["details"]["rule"] == "out_of_bounds"
    assert "20" in err["message"]  # message 含 canonical 长度


# T8 正文 C（"   "）{0,3} → 422 rule=blank
def test_t8_blank_range_422(db, client):
    token = _register(client, "f1_t8_heidi")
    pid = _create_project(client, token, "T8Proj")
    rev = _import_active(client, token, pid, TEXT_C)
    r = _create_fragment(client, token, pid, rev["id"], 0, 3, "x")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "range_invalid"
    assert err["details"]["rule"] == "blank"


# T9 正文 B {2,3}（边界劈代理对）→ 422 rule=proxy_split
def test_t9_proxy_split_422(db, client):
    token = _register(client, "f1_t9_ivan")
    pid = _create_project(client, token, "T9Proj")
    rev = _import_active(client, token, pid, TEXT_B)
    r = _create_fragment(client, token, pid, rev["id"], 2, 3, "x")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "range_invalid"
    assert err["details"]["rule"] == "proxy_split"


# T10 有效创建（A，{0,10}，expected=null）→ 201 + DB 三行
def test_t10_create_201(db, client):
    token = _register(client, "f1_t10_judy")
    pid = _create_project(client, token, "T10Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    r = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["object_ref"]["kind"] == "fragment"
    assert body["object_ref"]["revision"] == 1
    assert body["name"] == "F1"
    assert body["summary"] is None
    assert body["state"] == "candidate"
    assert body["range"] == {"start": 0, "end": 10}
    assert isinstance(body["created_at"], (int, float))
    # DB 直查
    sets = _range_sets(pid, rev["id"])
    assert len(sets) == 1
    assert sets[0].cas_revision == 2  # 建 1 + 本次布局变更 +1
    frags = _fragments(pid)
    assert len(frags) == 1
    f = frags[0]
    assert f.id == body["object_ref"]["id"]
    assert f.state == "candidate"
    assert f.range_set_id == sets[0].id
    assert f.source_revision_id == rev["id"]
    rs_revs = _fragment_revisions(f.id)
    assert len(rs_revs) == 1
    rv = rs_revs[0]
    assert rv.revision == 1
    assert rv.reason == "created"
    assert rv.range_start == 0 and rv.range_end == 10
    assert rv.predecessor_fragment_ids == "[]"
    assert f.current_revision_id == rv.id


# T11 第二片段 expected=null（已有行 cas=2）→ 409 revision_conflict
def test_t11_expected_null_with_existing_set_409(db, client):
    token = _register(client, "f1_t11_karen")
    pid = _create_project(client, token, "T11Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    r1 = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1")
    assert r1.status_code == 201, r1.text
    sets = _range_sets(pid, rev["id"])
    assert len(sets) == 1 and sets[0].cas_revision == 2
    r2 = _create_fragment(client, token, pid, rev["id"], 10, 20, "F2")
    assert r2.status_code == 409, r2.text
    err = r2.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"]["object"] == {"kind": "range_set", "id": sets[0].id}
    assert err["details"]["expected"] is None
    assert err["details"]["actual"] == 2


# T12 第二片段 expected=2 → 201；DB cas==3
def test_t12_second_fragment_expected_ok_201(db, client):
    token = _register(client, "f1_t12_laura")
    pid = _create_project(client, token, "T12Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    r1 = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1")
    assert r1.status_code == 201, r1.text
    r2 = _create_fragment(client, token, pid, rev["id"], 10, 20, "F2", expected=2)
    assert r2.status_code == 201, r2.text
    assert r2.json()["range"] == {"start": 10, "end": 20}
    sets = _range_sets(pid, rev["id"])
    assert len(sets) == 1 and sets[0].cas_revision == 3
    assert len(_fragments(pid)) == 2


# T13 重叠 {5,15} expected=3 → 409 range_overlap：conflicts 恰 2 条
def test_t13_overlap_409(db, client):
    token = _register(client, "f1_t13_monica")
    pid = _create_project(client, token, "T13Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    r1 = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1")
    assert r1.status_code == 201, r1.text
    f1_id = r1.json()["object_ref"]["id"]
    r2 = _create_fragment(client, token, pid, rev["id"], 10, 20, "F2", expected=2)
    assert r2.status_code == 201, r2.text
    f2_id = r2.json()["object_ref"]["id"]
    r3 = _create_fragment(client, token, pid, rev["id"], 5, 15, "F3", expected=3)
    assert r3.status_code == 409, r3.text
    err = r3.json()["error"]
    assert err["code"] == "range_overlap"
    conf = err["details"]["conflicts"]
    assert len(conf) == 2  # {0,10} 与 {10,20} 都与 {5,15} 相交
    got = {(c["fragment_id"], c["name"], c["start"], c["end"]) for c in conf}
    assert got == {(f1_id, "F1", 0, 10), (f2_id, "F2", 10, 20)}
    # message 点名首个冲突片段名与 ID
    assert "F1" in err["message"]
    assert f1_id in err["message"]


# T14 相接（{0,10} 后 {10,20}）→ 201（边界相接合法）
def test_t14_adjacent_ok_201(db, client):
    token = _register(client, "f1_t14_nina")
    pid = _create_project(client, token, "T14Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    r1 = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1")
    assert r1.status_code == 201, r1.text
    r2 = _create_fragment(client, token, pid, rev["id"], 10, 20, "F2", expected=2)
    assert r2.status_code == 201, r2.text
    sets = _range_sets(pid, rev["id"])
    assert len(sets) == 1 and sets[0].cas_revision == 3
    assert len(_fragments(pid)) == 2


# T15 重放（T10 的 command_id+body）→ 200 非 201，id 相同，行数/cas 不变
def test_t15_replay_200(db, client):
    token = _register(client, "f1_t15_olga")
    pid = _create_project(client, token, "T15Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    cmd = _cmd()
    r1 = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1", command_id=cmd)
    assert r1.status_code == 201, r1.text
    before_f = len(_fragments(pid))
    before_cas = _range_sets(pid, rev["id"])[0].cas_revision
    r2 = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1", command_id=cmd)
    assert r2.status_code == 200, (r2.status_code, r2.text)  # 重放 = 200 非 201
    assert r2.json() == r1.json()
    assert r2.json()["object_ref"]["id"] == r1.json()["object_ref"]["id"]
    assert len(_fragments(pid)) == before_f
    assert _range_sets(pid, rev["id"])[0].cas_revision == before_cas


# T16 同 command_id 改 name → 422 rule=reused
def test_t16_command_id_reused_422(db, client):
    token = _register(client, "f1_t16_peter")
    pid = _create_project(client, token, "T16Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    cmd = _cmd()
    r1 = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1", command_id=cmd)
    assert r1.status_code == 201, r1.text
    r2 = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1-changed", command_id=cmd)
    assert r2.status_code == 422, r2.text
    err = r2.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "command_id"
    assert v0["rule"] == "reused"


# T17 无半次保存：重叠失败后 cas 与 fragment/revision 行数均不变
def test_t17_no_half_save(db, client):
    token = _register(client, "f1_t17_quinn")
    pid = _create_project(client, token, "T17Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    r1 = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1")
    assert r1.status_code == 201, r1.text
    r2 = _create_fragment(client, token, pid, rev["id"], 10, 20, "F2", expected=2)
    assert r2.status_code == 201, r2.text
    cas_before = _range_sets(pid, rev["id"])[0].cas_revision
    frags_before = len(_fragments(pid))
    revs_before = sum(
        len(_fragment_revisions(f.id)) for f in _fragments(pid)
    )
    r3 = _create_fragment(client, token, pid, rev["id"], 5, 15, "F3", expected=3)
    assert r3.status_code == 409, r3.text
    assert r3.json()["error"]["code"] == "range_overlap"
    assert _range_sets(pid, rev["id"])[0].cas_revision == cas_before
    assert len(_fragments(pid)) == frags_before
    assert sum(len(_fragment_revisions(f.id)) for f in _fragments(pid)) == revs_before


# T18 并发争 CAS（线程）：{0,10} 与 {5,15} 同时提交，相同 expected=null
# → 恰一个 201，另一 409（revision_conflict / range_overlap）；fragment 行数==1
def test_t18_concurrent_cas(db, client):
    token = _register(client, "f1_t18_ruth")
    pid = _create_project(client, token, "T18Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    assert len(_fragments(pid)) == 0

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def worker(key: str, start: int, end: int, name: str):
        barrier.wait()
        results[key] = _create_fragment(
            client, token, pid, rev["id"], start, end, name, expected=None
        )

    t1 = threading.Thread(target=worker, args=("a", 0, 10, "FA"))
    t2 = threading.Thread(target=worker, args=("b", 5, 15, "FB"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    r_a, r_b = results["a"], results["b"]
    codes = [r_a.status_code, r_b.status_code]
    assert codes.count(201) == 1, (r_a.status_code, r_a.text, r_b.status_code, r_b.text)
    assert 409 in codes, (r_a.status_code, r_a.text, r_b.status_code, r_b.text)
    for r in (r_a, r_b):
        if r.status_code == 409:
            assert r.json()["error"]["code"] in ("revision_conflict", "range_overlap")
    # 至多一个成功：恰 1 个片段、1 个集合行
    assert len(_fragments(pid)) == 1
    assert len(_range_sets(pid, rev["id"])) == 1


# ───────────────────────── F6 确认片段（T19–T26） ─────────────────────────

# T19 无 token → 401
def test_t19_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/01010101010101010101010101/fragments/03030303030303030303030303/confirm",
        json={"expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


# T20 fid 不存在（26 字符 ULID，项目存在且有正文）→ 404 not_found
# details=={"kind":"fragment","id":fid}
def test_t20_nonexistent_fid_404(db, client):
    token = _register(client, "f6_t20_alice")
    pid = _create_project(client, token, "T20Proj")
    _import_active(client, token, pid, TEXT_A)
    fid = "03" * 13
    r = _confirm(client, token, pid, fid, 1)
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "fragment", "id": fid}


# T21 跨项目 fid（他人项目的片段经本项目路径）→ 404 同形状（不泄漏）
def test_t21_cross_project_fid_404(db, client):
    ta = _register(client, "f6_t21_bob")
    tc = _register(client, "f6_t21_carol")
    pa = _create_project(client, ta, "T21ProjA")
    pc = _create_project(client, tc, "T21ProjC")
    revc = _import_active(client, tc, pc, TEXT_A)
    rfrag = _create_fragment(client, tc, pc, revc["id"], 0, 5, "F1")
    assert rfrag.status_code == 201, rfrag.text
    fid = rfrag.json()["object_ref"]["id"]
    _import_active(client, ta, pa, TEXT_A)  # 本项目有正文
    r = _confirm(client, ta, pa, fid, 1)
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "fragment", "id": fid}


# T22 非 active 版本上的候选片段（真实激活切走 active，T4 同款构造）
# → 422 precondition_failed：reason=="source_revision_inactive"、blocked_by.id==active
def test_t22_inactive_source_revision_422(db, client):
    token = _register(client, "f6_t22_dave")
    pid = _create_project(client, token, "T22Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    time.sleep(0.002)  # 保证 ULID 严格递增
    r2 = _import(client, token, pid, "second version content")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()
    assert v2["is_active"] is False
    # 先在 v1（当时 active）下建候选片段
    rc = _create_fragment(client, token, pid, v1["id"], 0, 5, "F1")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    # 真实激活：S4 preview → S5 apply 把项目 active 从 v1 切到 v2，
    # 构造"片段绑定版本非 active"状态
    _activate(client, token, pid, v2["id"], expected_active_revision_id=v1["id"])
    r = _confirm(client, token, pid, fid, 1)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    assert err["details"]["blocked_by"]["kind"] == "source"
    assert err["details"]["blocked_by"]["id"] == v2["id"]


# T23 过期 CAS：候选片段 current revision=1，expected_revision=2
# → 409 revision_conflict：object={kind:fragment,id:fid}、expected==2、actual==1
def test_t23_stale_cas_409(db, client):
    token = _register(client, "f6_t23_erin")
    pid = _create_project(client, token, "T23Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 5, "F1")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    r = _confirm(client, token, pid, fid, 2)
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"]["object"] == {"kind": "fragment", "id": fid}
    assert err["details"]["expected"] == 2
    assert err["details"]["actual"] == 1


# T24 有效确认（candidate、expected=1）→ 200：F3 形状 DTO + DB 副作用
def test_t24_confirm_200(db, client):
    token = _register(client, "f6_t24_frank")
    pid = _create_project(client, token, "T24Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas_before = _range_sets(pid, rev["id"])[0].cas_revision
    cmd_before = _count("studio_command_records")
    assert _count("studio_production_scopes") == 0  # 确认前无生产范围
    r = _confirm(client, token, pid, fid, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object_ref"] == {"kind": "fragment", "id": fid, "revision": 1}
    assert body["state"] == "confirmed"
    assert body["revision_is_active"] is True
    assert body["range"] == {"start": 0, "end": 10}
    assert body["source_revision_id"] == rev["id"]
    assert body["predecessor_ids"] == []
    assert body["name"] == "F1"
    assert body["summary"] is None
    assert body["retired_at"] is None
    # DB 直查：fragment 状态/时间戳
    with Session() as s:
        f = s.get(Fragment, fid)
        assert f.state == "confirmed"
        assert f.updated_at > f.created_at
        range_set_id = f.range_set_id
        decisions = (
            s.execute(select(ReviewDecision).where(ReviewDecision.project_id == pid))
            .scalars()
            .all()
        )
    # ReviewDecision 恰 1 行
    assert len(decisions) == 1
    d = decisions[0]
    assert d.decision == "applied"
    assert d.preview_id is None
    ref = json.loads(d.target_ref)
    assert ref == {"kind": "fragment", "id": fid, "revision": 1}
    # baseline_digest = 64 位 hex 且 == 测试内独立重算值
    assert len(d.baseline_digest) == 64
    int(d.baseline_digest, 16)
    baseline = {
        "fragment_id": fid,
        "fragment_revision": 1,
        "source_revision_id": rev["id"],
        "range_set_id": range_set_id,
        "cas_revision": cas_before,
    }
    expected_digest = hashlib.sha256(
        json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert d.baseline_digest == expected_digest
    # 确认 ≠ 全文覆盖：studio_production_scopes 行数仍 0；range_set cas 未动
    assert _count("studio_production_scopes") == 0
    assert _range_sets(pid, rev["id"])[0].cas_revision == cas_before
    # command_records +1
    assert _count("studio_command_records") == cmd_before + 1


# T25 新 command_id 再确认已确认片段（expected=当前=1）
# → 422 precondition_failed：reason=="not_candidate"、
# blocked_by=={kind:fragment,id:fid,revision:1}
def test_t25_confirm_again_new_cmd_422(db, client):
    token = _register(client, "f6_t25_grace")
    pid = _create_project(client, token, "T25Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    r1 = _confirm(client, token, pid, fid, 1)
    assert r1.status_code == 200, r1.text
    r2 = _confirm(client, token, pid, fid, 1)  # 新 command_id，expected=当前
    assert r2.status_code == 422, r2.text
    err = r2.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "not_candidate"
    assert err["details"]["blocked_by"] == {"kind": "fragment", "id": fid, "revision": 1}


# T26 重放（T24 的 command_id+expected）→ 200 非 201，body 与首次相等，
# ReviewDecision 行数仍 1（零副作用）
def test_t26_replay_200(db, client):
    token = _register(client, "f6_t26_heidi")
    pid = _create_project(client, token, "T26Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F1")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cmd = _cmd()
    r1 = _confirm(client, token, pid, fid, 1, command_id=cmd)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    with Session() as s:
        dec_before = (
            s.execute(select(ReviewDecision).where(ReviewDecision.project_id == pid))
            .scalars()
            .all()
        )
    cmd_records_before = _count("studio_command_records")
    # 重放：同 command_id + 同 expected_revision
    r2 = _confirm(client, token, pid, fid, 1, command_id=cmd)
    assert r2.status_code == 200, (r2.status_code, r2.text)  # 200 非 201
    assert r2.json() == body1
    # 零副作用：ReviewDecision 行数仍 1、command_records 不再增加
    assert len(dec_before) == 1
    with Session() as s:
        dec_after = (
            s.execute(select(ReviewDecision).where(ReviewDecision.project_id == pid))
            .scalars()
            .all()
        )
    assert len(dec_after) == 1
    assert _count("studio_command_records") == cmd_records_before


# T27 无 token → 401
def test_t27_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/01010101010101010101010101/fragments/02020202020202020202020202/rename",
        json={"name": "x", "expected_revision": 1, "command_id": _cmd()},
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthenticated"


# T28 fid 不存在 / 跨项目 → 404 kind=fragment（不泄漏）
def test_t28_unknown_and_cross_project_404(db, client):
    token_a = _register(client, "f4_t28_alice")
    token_b = _register(client, "f4_t28_bob")
    pid_a = _create_project(client, token_a, "T28A")
    pid_b = _create_project(client, token_b, "T28B")
    v1a = _import_active(client, token_a, pid_a, TEXT_A)
    v1b = _import_active(client, token_b, pid_b, TEXT_A)
    rc = _create_fragment(client, token_a, pid_a, v1a["id"], 0, 5, "FA")
    assert rc.status_code == 201, rc.text
    fid_a = rc.json()["object_ref"]["id"]

    # 不存在（合法 26 字符 id）
    ghost = "03" * 13
    r = _rename(client, token_a, pid_a, ghost, "n", 1)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": ghost}

    # 跨项目：B 项目路径下问 A 的片段 → 同形状 404
    r = _rename(client, token_b, pid_b, fid_a, "n", 1)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": fid_a}


# T29 非 active 版本上的片段 → 422 precondition_failed
def test_t29_inactive_source_422(db, client):
    token = _register(client, "f4_t29_carol")
    pid = _create_project(client, token, "T29Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    time.sleep(0.002)
    r2 = _import(client, token, pid, "second version content")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()
    rc = _create_fragment(client, token, pid, v1["id"], 0, 5, "F29")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    # 真实激活：S4 preview → S5 apply 把项目 active 从 v1 切到 v2
    _activate(client, token, pid, v2["id"], expected_active_revision_id=v1["id"])
    r = _rename(client, token, pid, fid, "n", 1)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    assert err["details"]["blocked_by"] == {
        "kind": "source",
        "id": v2["id"],
        "revision": None,
    }


# T30 name 校验（先于 CAS）
def test_t30_name_validation_before_cas(db, client):
    token = _register(client, "f4_t30_dora")
    pid = _create_project(client, token, "T30Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, v1["id"], 0, 5, "F30")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]

    for bad in ("", "   "):
        r = _rename(client, token, pid, fid, bad, 1)
        assert r.status_code == 422, r.text
        v = r.json()["error"]["details"]["violations"][0]
        assert (v["field"], v["rule"]) == ("name", "required")

    r = _rename(client, token, pid, fid, "x" * 121, 1)
    assert r.status_code == 422, r.text
    v = r.json()["error"]["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("name", "max_length")

    # 顺序冻结：expected 给错值（99）时仍先返回 name 校验错而非 409
    r = _rename(client, token, pid, fid, "  ", 99)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_failed"


# T31 过期 CAS → 409 revision_conflict
def test_t31_stale_cas_409(db, client):
    token = _register(client, "f4_t31_erin")
    pid = _create_project(client, token, "T31Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, v1["id"], 0, 5, "F31")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    r = _rename(client, token, pid, fid, "n", 2)  # current=1
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"]["object"] == {"kind": "fragment", "id": fid}
    assert err["details"]["expected"] == 2
    assert err["details"]["actual"] == 1


# T32 有效改名 → 200，ID 不变、不产生 revision、不动 cas
def test_t32_rename_ok_200(db, client):
    token = _register(client, "f4_t32_frank")
    pid = _create_project(client, token, "T32Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, v1["id"], 0, 5, "F32")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    with Session() as s:
        frag_before = s.get(Fragment, fid)
        cas_before = s.execute(
            select(RangeSet).where(RangeSet.project_id == pid)
        ).scalar_one().cas_revision
        revs_before = _fragment_revisions(fid)
    assert len(revs_before) == 1 and revs_before[0].revision == 1
    assert frag_before.created_at == frag_before.updated_at

    r = _rename(client, token, pid, fid, "  新名字  ", 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object_ref"] == {"kind": "fragment", "id": fid, "revision": 1}
    assert body["name"] == "新名字"
    assert body["state"] == "candidate"
    assert body["revision_is_active"] is True
    assert body["range"] == {"start": 0, "end": 5}
    assert body["updated_at"] > body["created_at"]

    with Session() as s:
        frag = s.get(Fragment, fid)
        assert frag.name == "新名字"
        assert frag.updated_at > frag.created_at
        cas_after = s.execute(
            select(RangeSet).where(RangeSet.project_id == pid)
        ).scalar_one().cas_revision
        assert cas_after == cas_before  # 不动 cas
    revs_after = _fragment_revisions(fid)
    assert len(revs_after) == 1 and revs_after[0].revision == 1  # 不产生 revision


# T33 重放 → 200 原 body，零副作用
def test_t33_replay_200_no_side_effect(db, client):
    token = _register(client, "f4_t33_gina")
    pid = _create_project(client, token, "T33Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    cid = _cmd()
    rc = _create_fragment(client, token, pid, v1["id"], 0, 5, "F33", command_id=cid)
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cid_r = _cmd()
    r1 = _rename(client, token, pid, fid, "原名", 1, command_id=cid_r)
    assert r1.status_code == 200, r1.text
    with Session() as s:
        updated_after_1 = s.get(Fragment, fid).updated_at
    # 同 command_id + 同 name → 重放
    r2 = _rename(client, token, pid, fid, "原名", 1, command_id=cid_r)
    assert r2.status_code == 200, r2.text  # 非 201
    assert r2.json() == r1.json()
    with Session() as s:
        frag = s.get(Fragment, fid)
        assert frag.updated_at == updated_after_1  # 零副作用
    assert len(_fragment_revisions(fid)) == 1
    # 同 command_id 不同 name → 422 reused
    r3 = _rename(client, token, pid, fid, "另一名", 1, command_id=cid_r)
    assert r3.status_code == 422, r3.text
    assert r3.json()["error"]["details"]["violations"][0]["rule"] == "reused"


# ───────────────────── F7 片段退役 preview/apply（T34–T43，附录 02 §2 v2.8 + 00 §5） ─────────────────────
# 跨域表（source_relations/script_*/jobs/media_artifacts/scenes/shots/edit_instances/releases）
# 经下列模块导入注册进 Base.metadata，丢弃库 create_all 建表；种子行直接 INSERT
# 构造（SOURCE-05 起 active 切换已改走 S4/S5 真实激活）。不写 STUDIO_* env。

import backend.studio.relations.models  # noqa: F401,E402
import backend.studio.scripts.models  # noqa: F401,E402
import backend.studio.jobs.models  # noqa: F401,E402
import backend.studio.media.models  # noqa: F401,E402
import backend.studio.storyboard.models  # noqa: F401,E402
import backend.studio.edits.models  # noqa: F401,E402
import backend.studio.publishing.models  # noqa: F401,E402

from backend.core.db import make_ulid  # noqa: E402
from backend.studio.reviews.models import ChangePreview  # noqa: E402


def _retire_preview(client, token, pid, fid, target=None, command_id=None):
    return client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/retire",
        json={
            "target_fragment_id": target if target is not None else fid,
            "command_id": command_id or _cmd(),
        },
        headers=_auth(token),
    )


def _retire_apply(
    client,
    token,
    pid,
    fid,
    preview_id,
    command_id=None,
    expected_revision=1,
    expected_range_set_revision=None,
):
    return client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/retire/apply",
        json={
            "preview_id": preview_id,
            "command_id": command_id or _cmd(),
            "expected_revision": expected_revision,
            "expected_range_set_revision": expected_range_set_revision,
        },
        headers=_auth(token),
    )


def _user_id(username) -> str:
    with Session() as s:
        return s.execute(
            text("SELECT id FROM users WHERE username = :u"), {"u": username}
        ).scalar()


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


def _seed_t40(pid, uid, fid) -> dict:
    """跨域种子行直接 INSERT（满足各表 NOT NULL/外键约束；项目/正文先行；
    Scene 行支撑 Shot；ScriptObject 经 deferred FK 环与 ScriptRevision 同事务落两行）。"""
    now = time.time()
    scene_id = make_ulid()
    object_id = make_ulid()
    object_rev_id = make_ulid()
    adoption_id = make_ulid()
    shot_id = make_ulid()
    shot_rev_id = make_ulid()
    relation_id = make_ulid()
    job_id = make_ulid()
    artifact_id = make_ulid()
    with Session() as s:
        s.execute(
            text(
                "INSERT INTO studio_scenes (id, project_id, name, seq, "
                "layout_cas_revision, created_at, updated_at) "
                "VALUES (:id, :pid, 'S1', 1, 1, :now, :now)"
            ),
            {"id": scene_id, "pid": pid, "now": now},
        )
        # ScriptObject ↔ ScriptRevision deferred FK 环：同事务两行，commit 时校验
        s.execute(
            text(
                "INSERT INTO studio_script_objects (id, project_id, fragment_id, kind, "
                "seq, speaker, current_revision_id, created_at, updated_at, retired_at) "
                "VALUES (:id, :pid, :fid, 'dialogue', 1, 'L1', :cur, :now, :now, NULL)"
            ),
            {"id": object_id, "pid": pid, "fid": fid, "cur": object_rev_id, "now": now},
        )
        s.execute(
            text(
                "INSERT INTO studio_script_revisions (id, object_id, revision, text, "
                "speaker, adaptation_note, source_fragment_revision, created_at) "
                "VALUES (:id, :oid, 1, 'hello', 'L1', NULL, 1, :now)"
            ),
            {"id": object_rev_id, "oid": object_id, "now": now},
        )
        s.execute(
            text(
                "INSERT INTO studio_script_adoptions (id, project_id, fragment_id, "
                "object_revisions, content_hash, previous_adoption_id, created_at) "
                "VALUES (:id, :pid, :fid, :objs, :hash, NULL, :now)"
            ),
            {
                "id": adoption_id,
                "pid": pid,
                "fid": fid,
                "objs": json.dumps([{"object_id": object_id, "revision": 1}]),
                "hash": "ab" * 32,
                "now": now,
            },
        )
        s.execute(
            text(
                "INSERT INTO studio_shots (id, project_id, scene_id, seq, name, "
                "script_adoption_id, current_revision_id, created_at, updated_at) "
                "VALUES (:id, :pid, :scene, 1, 'SH1', :ad, :cur, :now, :now)"
            ),
            {
                "id": shot_id,
                "pid": pid,
                "scene": scene_id,
                "ad": adoption_id,
                "cur": shot_rev_id,
                "now": now,
            },
        )
        s.execute(
            text(
                "INSERT INTO studio_shot_revisions (id, shot_id, revision, visual, "
                "action, dialogue, transition_in, transition_out, duration_hint_ms, "
                "created_at) VALUES (:id, :sid, 1, '{}', '{}', NULL, NULL, NULL, NULL, :now)"
            ),
            {"id": shot_rev_id, "sid": shot_id, "now": now},
        )
        s.execute(
            text(
                "INSERT INTO studio_source_relations (id, project_id, "
                "source_fragment_id, source_fragment_revision, target_kind, target_id, "
                "target_revision, created_at) "
                "VALUES (:id, :pid, :fid, 1, 'script_object', :tid, 1, :now)"
            ),
            {"id": relation_id, "pid": pid, "fid": fid, "tid": object_id, "now": now},
        )
        # 冻结输入 payload 含 fid 字面（子串）
        s.execute(
            text(
                "INSERT INTO studio_jobs (id, project_id, owner_id, kind, ref_kind, "
                "ref_id, payload, status, priority, created_at, submitted_by, "
                "attempts, usage, last_error, finished_at) "
                "VALUES (:id, :pid, :uid, 'shot_generate', NULL, NULL, :payload, "
                "'pending', 0, :now, :uid, 0, NULL, NULL, NULL)"
            ),
            {
                "id": job_id,
                "pid": pid,
                "uid": uid,
                "payload": json.dumps({"fragment_id": fid}),
                "now": now,
            },
        )
        s.execute(
            text(
                "INSERT INTO studio_media_artifacts (id, project_id, media_kind, "
                "filename, size_bytes, sha256, duration_ms, width, height, source, "
                "source_job_id, created_at) "
                "VALUES (:id, :pid, 'image', 'a.png', 12, :sha, NULL, NULL, NULL, "
                "'shot_generate', :jid, :now)"
            ),
            {
                "id": artifact_id,
                "pid": pid,
                "sha": "cd" * 32,
                "jid": job_id,
                "now": now,
            },
        )
        s.commit()
    return {
        "scene_id": scene_id,
        "object_id": object_id,
        "object_rev_id": object_rev_id,
        "adoption_id": adoption_id,
        "shot_id": shot_id,
        "shot_rev_id": shot_rev_id,
        "relation_id": relation_id,
        "job_id": job_id,
        "artifact_id": artifact_id,
    }


# T34 无 token → 401（preview 与 apply 各一）
def test_t34_no_token_401(db, client):
    ghost = "03" * 13
    pid = "01" * 13
    r1 = client.post(
        f"/api/studio/projects/{pid}/fragments/{ghost}/retire",
        json={"target_fragment_id": ghost, "command_id": _cmd()},
    )
    assert r1.status_code == 401, r1.text
    assert r1.json()["error"]["code"] == "unauthenticated"
    r2 = client.post(
        f"/api/studio/projects/{pid}/fragments/{ghost}/retire/apply",
        json={
            "preview_id": ghost,
            "command_id": _cmd(),
            "expected_revision": 1,
            "expected_range_set_revision": 1,
        },
    )
    assert r2.status_code == 401, r2.text
    assert r2.json()["error"]["code"] == "unauthenticated"


# T35 preview fid 不存在 → 404 kind=fragment；跨项目 fid → 同形状（不泄漏）
def test_t35_unknown_and_cross_project_fid_404(db, client):
    ta = _register(client, "f7_t35_alice")
    tc = _register(client, "f7_t35_carol")
    pa = _create_project(client, ta, "T35A")
    pc = _create_project(client, tc, "T35C")
    _import_active(client, ta, pa, TEXT_A)
    revc = _import_active(client, tc, pc, TEXT_A)
    rc = _create_fragment(client, tc, pc, revc["id"], 0, 5, "F35")
    assert rc.status_code == 201, rc.text
    fid_c = rc.json()["object_ref"]["id"]

    ghost = "03" * 13
    r = _retire_preview(client, ta, pa, ghost, target=ghost)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": ghost}

    r = _retire_preview(client, ta, pa, fid_c, target=fid_c)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": fid_c}


# T36 target_fragment_id ≠ 路径 fid → 422 validation_failed rule=mismatch
def test_t36_target_mismatch_422(db, client):
    token = _register(client, "f7_t36_bob")
    pid = _create_project(client, token, "T36Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 5, "F36")
    fid = rc.json()["object_ref"]["id"]
    r = _retire_preview(client, token, pid, fid, target="04" * 13)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "target_fragment_id"
    assert v["rule"] == "mismatch"
    assert v["message"] == "target_fragment_id 必须与路径片段一致。"


# T37 非 active 版本上的片段 preview（真实激活切走 active，T4 同款构造）
# → 422 precondition_failed reason=source_revision_inactive
def test_t37_inactive_source_422(db, client):
    token = _register(client, "f7_t37_carol")
    pid = _create_project(client, token, "T37Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    time.sleep(0.002)  # 保证 ULID 严格递增
    r2 = _import(client, token, pid, "second version content")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()
    rc = _create_fragment(client, token, pid, v1["id"], 0, 5, "F37")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    # 真实激活：S4 preview → S5 apply 把项目 active 从 v1 切到 v2
    _activate(client, token, pid, v2["id"], expected_active_revision_id=v1["id"])
    r = _retire_preview(client, token, pid, fid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    assert err["details"]["blocked_by"] == {
        "kind": "source",
        "id": v2["id"],
        "revision": None,
    }


# T38 已退役片段 preview（直改 state='retired'+retired_at）
# → 422 precondition_failed reason=already_retired（v2.8：422 非 409）
def test_t38_already_retired_422(db, client):
    token = _register(client, "f7_t38_dave")
    pid = _create_project(client, token, "T38Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 5, "F38")
    fid = rc.json()["object_ref"]["id"]
    with Session() as s:
        s.execute(
            update(Fragment)
            .where(Fragment.id == fid)
            .values(state="retired", retired_at=time.time())
        )
        s.commit()
    r = _retire_preview(client, token, pid, fid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "already_retired"
    assert err["details"]["blocked_by"] == {
        "kind": "fragment",
        "id": fid,
        "revision": 1,
    }


# T39 有效 preview（candidate 片段）→ 200 恰 4 字段；baseline 精确；
# impact 三列表键齐（空）；DB：preview 行 state='pending'、expires_at=created_at+30min
def test_t39_preview_200(db, client):
    token = _register(client, "f7_t39_erin")
    pid = _create_project(client, token, "T39Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F39")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    set_row = _range_sets(pid, rev["id"])[0]
    set_id, cas = set_row.id, set_row.cas_revision

    r = _retire_preview(client, token, pid, fid)
    assert r.status_code == 200, r.text
    body = r.json()
    # 恰 4 字段（00 §5.1 冻结；记录 state=pending 不外露）
    assert set(body.keys()) == {"preview_id", "kind", "baseline", "impact"}
    assert body["kind"] == "fragment_retire"
    assert len(body["preview_id"]) == 26
    assert body["baseline"] == {
        "fragment": {"kind": "fragment", "id": fid, "revision": 1},
        "range_set": {"id": set_id, "cas": cas},
    }
    # impact 三列表键齐（本库无跨域引用 → 空列表，不省略键）
    assert body["impact"] == {"affected": [], "needs_review": [], "preservable": []}

    pv = _preview_row(body["preview_id"])
    assert pv["state"] == "pending"
    assert pv["kind"] == "fragment_retire"
    assert pv["project_id"] == pid
    assert pv["owner_id"] == _user_id("f7_t39_erin")
    assert pv["decision_id"] is None
    assert pv["resolved_at"] is None
    assert json.loads(pv["payload"]) == {"target_fragment_id": fid}
    # 30 分钟 TTL：expires_at = created_at + 1800（容差区间内）
    assert 1700 < pv["expires_at"] - pv["created_at"] < 1900


# T40 跨域 impact：种子 SourceRelation/ScriptObject/ScriptAdoption/Shot/StudioJob/
# MediaArtifact → affected 恰 4、needs_review 恰 1（job）、preservable 恰 1
# （media_artifact）；各列表按 object_ref.id 排序
def test_t40_cross_domain_impact(db, client):
    token = _register(client, "f7_t40_frank")
    pid = _create_project(client, token, "T40Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F40")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    seeds = _seed_t40(pid, _user_id("f7_t40_frank"), fid)

    r = _retire_preview(client, token, pid, fid)
    assert r.status_code == 200, r.text
    impact = r.json()["impact"]

    # affected 恰 4 条：source_relation/script_object/script_adoption/shot
    expected_affected = [
        {"object_ref": {"kind": "source_relation", "id": seeds["relation_id"], "revision": 1}},
        {"object_ref": {"kind": "script_object", "id": seeds["object_id"], "revision": 1}},
        {"object_ref": {"kind": "script_adoption", "id": seeds["adoption_id"], "revision": None}},
        {"object_ref": {"kind": "shot", "id": seeds["shot_id"], "revision": 1}},
    ]
    expected_affected.sort(key=lambda x: x["object_ref"]["id"])
    assert impact["affected"] == expected_affected

    # needs_review 恰 1（pending job，冻结输入含 fid）
    assert impact["needs_review"] == [
        {
            "object_ref": {"kind": "job", "id": seeds["job_id"], "revision": None},
            "reason": "frozen_input_contains_fragment",
        }
    ]

    # preservable 恰 1（media_artifact，经 source_job_id 证据链）
    assert impact["preservable"] == [
        {"object_ref": {"kind": "media_artifact", "id": seeds["artifact_id"], "revision": None}}
    ]


# T41 有效 apply（新 command_id）→ 200：state='retired'、retired_at 非 null；
# DB：fragment retired、range_set.cas 不变、preview applied（resolved_at/decision_id
# 非 null）、ReviewDecision 恰 1 行（decision=applied、preview_id 正确、
# baseline_digest==sha256(规范化 baseline) 独立重算）
def test_t41_apply_200(db, client):
    token = _register(client, "f7_t41_grace")
    pid = _create_project(client, token, "T41Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F41")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas = _range_sets(pid, rev["id"])[0].cas_revision

    rp = _retire_preview(client, token, pid, fid)
    assert rp.status_code == 200, rp.text
    preview_id = rp.json()["preview_id"]

    ra = _retire_apply(
        client,
        token,
        pid,
        fid,
        preview_id,
        expected_revision=1,
        expected_range_set_revision=cas,
    )
    assert ra.status_code == 200, ra.text
    body = ra.json()
    assert body["object_ref"] == {"kind": "fragment", "id": fid, "revision": 1}
    assert body["state"] == "retired"
    assert body["retired_at"] is not None
    assert body["revision_is_active"] is True
    assert body["range"] == {"start": 0, "end": 10}
    assert body["name"] == "F41"
    assert body["summary"] is None
    assert body["source_revision_id"] == rev["id"]
    assert body["predecessor_ids"] == []

    with Session() as s:
        f = s.get(Fragment, fid)
        assert f.state == "retired"
        assert f.retired_at is not None
        assert f.updated_at > f.created_at
    # range_set 不加 1（附录 02 §3：F4–F7 不动 cas）
    assert _range_sets(pid, rev["id"])[0].cas_revision == cas

    pv = _preview_row(preview_id)
    assert pv["state"] == "applied"
    assert pv["resolved_at"] is not None
    assert pv["decision_id"] is not None

    with Session() as s:
        decisions = (
            s.execute(select(ReviewDecision).where(ReviewDecision.project_id == pid))
            .scalars()
            .all()
        )
    assert len(decisions) == 1
    d = decisions[0]
    assert d.decision == "applied"
    assert d.preview_id == preview_id
    assert d.id == pv["decision_id"]
    assert d.owner_id == _user_id("f7_t41_grace")
    assert json.loads(d.target_ref) == {"kind": "fragment", "id": fid, "revision": 1}
    # baseline_digest = sha256(规范化 preview.baseline)（独立重算）
    base = json.loads(_preview_row(preview_id)["baseline"])
    expected_digest = hashlib.sha256(
        json.dumps(base, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert d.baseline_digest == expected_digest


# T42 baseline 失鲜：preview 后 F1 再建一片段（cas+1）→ apply →
# 409 preview_stale：details.preview_id 正确、conflicts 恰含 range_set 项
# {expected:<preview 时 cas>, actual:<+1>}；preview.state=='superseded'
def test_t42_baseline_stale_409(db, client):
    token = _register(client, "f7_t42_heidi")
    pid = _create_project(client, token, "T42Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc1 = _create_fragment(client, token, pid, rev["id"], 0, 10, "F42a")
    assert rc1.status_code == 201, rc1.text
    fid = rc1.json()["object_ref"]["id"]
    set_row = _range_sets(pid, rev["id"])[0]
    cas0 = set_row.cas_revision
    cas1 = cas0 + 1

    rp = _retire_preview(client, token, pid, fid)
    assert rp.status_code == 200, rp.text
    preview_id = rp.json()["preview_id"]

    rc2 = _create_fragment(client, token, pid, rev["id"], 10, 20, "F42b", expected=cas0)
    assert rc2.status_code == 201, rc2.text
    assert _range_sets(pid, rev["id"])[0].cas_revision == cas1

    ra = _retire_apply(
        client,
        token,
        pid,
        fid,
        preview_id,
        expected_revision=1,
        expected_range_set_revision=cas1,
    )
    assert ra.status_code == 409, ra.text
    err = ra.json()["error"]
    assert err["code"] == "preview_stale"
    assert err["details"]["preview_id"] == preview_id
    conf = err["details"]["conflicts"]
    assert len(conf) == 1
    assert conf[0] == {
        "object": {"kind": "range_set", "id": set_row.id},
        "expected": cas0,
        "actual": cas1,
    }
    pv = _preview_row(preview_id)
    assert pv["state"] == "superseded"
    assert pv["resolved_at"] is not None


# T43 已 applied preview 再 apply（新 command_id）→ 409 preview_stale
# （actual=='applied'）；原 command_id 重放 → 200 原 body、零副作用
# （retired_at 与 ReviewDecision 行数不变）
def test_t43_applied_reapply_and_replay(db, client):
    token = _register(client, "f7_t43_ivan")
    pid = _create_project(client, token, "T43Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F43")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas = _range_sets(pid, rev["id"])[0].cas_revision

    rp = _retire_preview(client, token, pid, fid)
    assert rp.status_code == 200, rp.text
    preview_id = rp.json()["preview_id"]

    cmd_a = _cmd()
    ra1 = _retire_apply(
        client,
        token,
        pid,
        fid,
        preview_id,
        command_id=cmd_a,
        expected_revision=1,
        expected_range_set_revision=cas,
    )
    assert ra1.status_code == 200, ra1.text
    body1 = ra1.json()

    # (a) 新 command_id 再 apply → 409 preview_stale（actual=='applied'）
    ra2 = _retire_apply(
        client,
        token,
        pid,
        fid,
        preview_id,
        expected_revision=1,
        expected_range_set_revision=cas,
    )
    assert ra2.status_code == 409, ra2.text
    err = ra2.json()["error"]
    assert err["code"] == "preview_stale"
    assert err["details"]["preview_id"] == preview_id
    assert err["details"]["conflicts"] == [
        {
            "object": {"kind": "preview", "id": preview_id},
            "expected": "pending",
            "actual": "applied",
        }
    ]

    # (b) 原 command_id 重放 → 200 原 body
    ra3 = _retire_apply(
        client,
        token,
        pid,
        fid,
        preview_id,
        command_id=cmd_a,
        expected_revision=1,
        expected_range_set_revision=cas,
    )
    assert ra3.status_code == 200, (ra3.status_code, ra3.text)
    assert ra3.json() == body1

    # 零副作用：retired_at 不变、ReviewDecision 行数仍 1
    with Session() as s:
        f = s.get(Fragment, fid)
        assert f.retired_at == body1["retired_at"]
        decisions = (
            s.execute(select(ReviewDecision).where(ReviewDecision.project_id == pid))
            .scalars()
            .all()
        )
    assert len(decisions) == 1


# T44 preview 命令自身重放（00 §3）：同 command_id+target → 200 原 body、
# ChangePreview 恰 1 行（零副作用）；同 command_id 不同 target → 422 reused。
def test_t44_preview_replay_idempotent(db, client):
    token = _register(client, "f7_t44_finn")
    pid = _create_project(client, token, "T44Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F44")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]

    cid = _cmd()
    r1 = _retire_preview(client, token, pid, fid, target=fid, command_id=cid)
    assert r1.status_code == 200, r1.text
    pv1 = _preview_row(r1.json()["preview_id"])

    # 重放：同 command_id + 同 target → 200 原 body，预览行不变
    r2 = _retire_preview(client, token, pid, fid, target=fid, command_id=cid)
    assert r2.status_code == 200, r2.text  # 非 201
    assert r2.json() == r1.json()
    assert r2.json()["preview_id"] == r1.json()["preview_id"]
    pv2 = _preview_row(r1.json()["preview_id"])
    assert pv2["created_at"] == pv1["created_at"]
    with Session() as s:
        n = s.execute(
            text("SELECT COUNT(*) FROM studio_change_previews")
        ).scalar()
    assert n == 1  # 零副作用：不产生第二行

    # 同 command_id 不同 target：请求本身非法（target≠路径 fid），
    # 第 3 步 mismatch 先于幂等步触发 → 422 mismatch（reused 分支经 HTTP
    # 不可达，同 F1 non_integer 防御模式——SOURCE-03 裁定 2 同款）
    r3 = _retire_preview(client, token, pid, fid, target="04" * 13, command_id=cid)
    assert r3.status_code == 422, r3.text
    assert r3.json()["error"]["details"]["violations"][0]["rule"] == "mismatch"


# ───────────────────── F2/F3 片段纯读（T45–T55，附录 02 §2 F2/F3，SOURCE-03-a） ─────────────────────
# F3 行冻结字段集（附录 02 §2 F3 行逐字清单；与 F6/F4/F7 成功体 DTO 同形）
_F3_KEYS = {
    "object_ref",
    "name",
    "summary",
    "state",
    "revision_is_active",
    "range",
    "source_revision_id",
    "predecessor_ids",
    "created_at",
    "updated_at",
    "retired_at",
}


def _get_fragment(client, token, pid, fid):
    """F3 GET /projects/{pid}/fragments/{fid}。"""
    return client.get(
        f"/api/studio/projects/{pid}/fragments/{fid}", headers=_auth(token)
    )


def _list_fragments(client, token, pid, query=""):
    """F2 GET /projects/{pid}/fragments（query 为带前导 ? 的查询串）。"""
    return client.get(
        f"/api/studio/projects/{pid}/fragments{query}", headers=_auth(token)
    )


# T45 无 token → 401（F2/F3 两路由各一）
def test_t45_no_token_401(db, client):
    pid = "01" * 13
    fid = "03" * 13
    r1 = client.get(f"/api/studio/projects/{pid}/fragments")
    assert r1.status_code == 401, r1.text
    assert r1.json()["error"]["code"] == "unauthenticated"
    r2 = client.get(f"/api/studio/projects/{pid}/fragments/{fid}")
    assert r2.status_code == 401, r2.text
    assert r2.json()["error"]["code"] == "unauthenticated"


# T46 项目不存在（26 字符）/跨属主 → 两路由均 404 kind=project（同形状不泄漏）
def test_t46_project_404_same_shape(db, client):
    ta = _register(client, "f23_t46_alice")
    tb = _register(client, "f23_t46_bob")
    pb = _create_project(client, tb, "T46ProjB")
    ghost_pid = "01" * 13
    ghost_fid = "03" * 13
    for pid in (ghost_pid, pb):  # 不存在 / 跨属主，两路同形状
        r = _list_fragments(client, ta, pid)
        assert r.status_code == 404, r.text
        assert r.json()["error"]["code"] == "not_found"
        assert r.json()["error"]["details"] == {"kind": "project", "id": pid}
        r = _get_fragment(client, ta, pid, ghost_fid)
        assert r.status_code == 404, r.text
        assert r.json()["error"]["code"] == "not_found"
        assert r.json()["error"]["details"] == {"kind": "project", "id": pid}


# T47 F3 有效读 → 200：字段集精确 == F3 行冻结集（新建片段 revision=1、
# predecessor_ids=[]、range 精确、retired_at=None）；纯读零副作用
def test_t47_get_200_exact_fields(db, client):
    token = _register(client, "f23_t47_carol")
    pid = _create_project(client, token, "T47Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F47")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]

    cmd_before = _count("studio_command_records")
    r = _get_fragment(client, token, pid, fid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == _F3_KEYS
    assert body["object_ref"] == {"kind": "fragment", "id": fid, "revision": 1}
    assert body["name"] == "F47"
    assert body["summary"] is None
    assert body["state"] == "candidate"
    assert body["revision_is_active"] is True
    assert body["range"] == {"start": 0, "end": 10}
    assert body["source_revision_id"] == rev["id"]
    assert body["predecessor_ids"] == []
    assert body["retired_at"] is None
    assert isinstance(body["created_at"], (int, float))
    assert isinstance(body["updated_at"], (int, float))
    assert body["created_at"] == body["updated_at"]  # 新建未改
    # 纯读：零副作用（command_records 行数不变）
    assert _count("studio_command_records") == cmd_before


# T48 F3 fid 不存在（26 字符字面量）/跨项目 → 404 kind=fragment（两路同形状不泄漏）
def test_t48_get_unknown_and_cross_project_404(db, client):
    ta = _register(client, "f23_t48_dave")
    tc = _register(client, "f23_t48_erin")
    pa = _create_project(client, ta, "T48A")
    pc = _create_project(client, tc, "T48C")
    _import_active(client, ta, pa, TEXT_A)
    revc = _import_active(client, tc, pc, TEXT_A)
    rc = _create_fragment(client, tc, pc, revc["id"], 0, 5, "F48")
    assert rc.status_code == 201, rc.text
    fid_c = rc.json()["object_ref"]["id"]

    ghost = "03" * 13  # 26 字符字面量
    r = _get_fragment(client, ta, pa, ghost)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": ghost}
    # 跨项目：A 项目路径下问 C 的片段 → 同形状 404（不泄漏存在性）
    r = _get_fragment(client, ta, pa, fid_c)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": fid_c}


# T49 F3 派生状态（附录 02 §1 读时推导，不落库）：真实 S4/S5 激活 v2 后读
# v1 片段 → pending_review、revision_is_active=false、持久行不变；回切 v1 →
# 恢复 candidate；F7 退役 → retired、retired_at 非空；再切走 retired 恒 retired
def test_t49_derived_state_pending_review_and_retired(db, client):
    token = _register(client, "f23_t49_frank")
    pid = _create_project(client, token, "T49Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, v1["id"], 0, 10, "F49")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    time.sleep(0.002)  # 保证 ULID 严格递增
    r2 = _import(client, token, pid, "second version content")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()

    # (a) 激活 v2 → rev1 片段派生 pending_review（持久行不变）
    _activate(client, token, pid, v2["id"], expected_active_revision_id=v1["id"])
    r = _get_fragment(client, token, pid, fid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "pending_review"
    assert body["revision_is_active"] is False
    assert body["source_revision_id"] == v1["id"]
    with Session() as s:
        f = s.get(Fragment, fid)
        assert f.state == "candidate"  # 持久行不变（pending_review 不落库）

    # (b) 回切 v1 → 恢复 candidate（派生标记自动消失，无回写）
    _activate(client, token, pid, v1["id"], expected_active_revision_id=v2["id"])
    r = _get_fragment(client, token, pid, fid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "candidate"
    assert body["revision_is_active"] is True

    # (c) F7 退役（v1 active）→ state='retired'、retired_at 非空
    cas = _range_sets(pid, v1["id"])[0].cas_revision
    rp = _retire_preview(client, token, pid, fid)
    assert rp.status_code == 200, rp.text
    ra = _retire_apply(
        client, token, pid, fid, rp.json()["preview_id"],
        expected_revision=1, expected_range_set_revision=cas,
    )
    assert ra.status_code == 200, ra.text
    r = _get_fragment(client, token, pid, fid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "retired"
    assert body["retired_at"] is not None
    assert body["revision_is_active"] is True

    # (d) 再切 v2：retired 恒 'retired'（不派生 pending_review）
    _activate(client, token, pid, v2["id"], expected_active_revision_id=v1["id"])
    r = _get_fragment(client, token, pid, fid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "retired"
    assert body["revision_is_active"] is False
    assert body["retired_at"] is not None


# T50 F9 split 后继片段 F3 读：左/右 predecessor_ids == [原 id] 精确；
# 原片段（已退役）仍可读 state='retired'
def test_t50_split_successor_predecessor_ids(db, client):
    token = _register(client, "f23_t50_grace")
    pid = _create_project(client, token, "T50Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F50")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas = _range_sets(pid, rev["id"])[0].cas_revision

    # F9 拆分（preview → apply，直连 HTTP，同 test_fragment_changes helper 形状）
    rp = client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/split",
        json={
            "target_fragment_id": fid,
            "split_point": 4,
            "left_name": None,
            "right_name": None,
            "command_id": _cmd(),
        },
        headers=_auth(token),
    )
    assert rp.status_code == 200, rp.text
    ra = client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/split/apply",
        json={
            "preview_id": rp.json()["preview_id"],
            "command_id": _cmd(),
            "expected_revision": 1,
            "expected_range_set_revision": cas,
        },
        headers=_auth(token),
    )
    assert ra.status_code == 200, ra.text
    left_id = ra.json()["left"]["object_ref"]["id"]
    right_id = ra.json()["right"]["object_ref"]["id"]

    # 左片段：predecessor_ids == [原 id]，range/revision/字段集精确
    rl = _get_fragment(client, token, pid, left_id)
    assert rl.status_code == 200, rl.text
    lb = rl.json()
    assert set(lb.keys()) == _F3_KEYS
    assert lb["object_ref"] == {"kind": "fragment", "id": left_id, "revision": 1}
    assert lb["predecessor_ids"] == [fid]
    assert lb["range"] == {"start": 0, "end": 4}
    assert lb["state"] == "candidate"
    assert lb["revision_is_active"] is True
    # 右片段同 predecessor
    rr = _get_fragment(client, token, pid, right_id)
    assert rr.status_code == 200, rr.text
    assert rr.json()["predecessor_ids"] == [fid]
    assert rr.json()["range"] == {"start": 4, "end": 10}
    # 原片段（已退役）仍可读：state='retired'（任何版本片段都可读）
    ro = _get_fragment(client, token, pid, fid)
    assert ro.status_code == 200, ro.text
    assert ro.json()["state"] == "retired"
    assert ro.json()["retired_at"] is not None


# T51 F2 只列绑定当前 active 版本的片段：rev1 两片 + rev2 一片；active=rev1
# 时 items 恰 2、切 rev2 后恰 1、回切 rev1 恢复恰 2；items 元素为完整 DTO
def test_t51_list_only_active_revision(db, client):
    token = _register(client, "f23_t51_heidi")
    pid = _create_project(client, token, "T51Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    f1 = _create_fragment(client, token, pid, v1["id"], 0, 10, "T51a")
    assert f1.status_code == 201, f1.text
    fid1 = f1.json()["object_ref"]["id"]
    f2 = _create_fragment(client, token, pid, v1["id"], 10, 20, "T51b", expected=2)
    assert f2.status_code == 201, f2.text
    fid2 = f2.json()["object_ref"]["id"]

    # active=v1：恰 2 条，元素为 F3 形状完整 DTO（revision_is_active=True）
    r = _list_fragments(client, token, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_cursor"] is None
    assert {it["object_ref"]["id"] for it in body["items"]} == {fid1, fid2}
    for it in body["items"]:
        assert set(it.keys()) == _F3_KEYS
        assert it["revision_is_active"] is True
        assert it["source_revision_id"] == v1["id"]

    # 导入 v2 并真实激活；v2 下建 1 片
    time.sleep(0.002)  # 保证 ULID 严格递增
    r2 = _import(client, token, pid, "second version content!")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()
    _activate(client, token, pid, v2["id"], expected_active_revision_id=v1["id"])
    f3 = _create_fragment(client, token, pid, v2["id"], 0, 5, "T51c")
    assert f3.status_code == 201, f3.text
    fid3 = f3.json()["object_ref"]["id"]

    # active=v2：恰 1 条（rev1 两片不出现，不泄漏）
    r = _list_fragments(client, token, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [it["object_ref"]["id"] for it in body["items"]] == [fid3]
    assert body["next_cursor"] is None

    # 回切 v1：恰 2 条（v2 片不出现）
    _activate(client, token, pid, v1["id"], expected_active_revision_id=v2["id"])
    r = _list_fragments(client, token, pid)
    assert r.status_code == 200, r.text
    assert {it["object_ref"]["id"] for it in r.json()["items"]} == {fid1, fid2}


# T52 空列表 200 与项目 404 严格分开：项目存在无 active 修订 → 200 空列表；
# 项目存在有 active 无片段 → 200 空列表（R12：空列表 ≠ 读失败）；
# 项目不存在 → 404 kind=project
def test_t52_empty_list_200_vs_project_404(db, client):
    token = _register(client, "f23_t52_ivan")
    # (a) 项目存在、无 active 修订（未导入正文）→ 200 空列表
    pid_empty = _create_project(client, token, "T52Empty")
    r = _list_fragments(client, token, pid_empty)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "next_cursor": None}

    # (b) 项目存在、有 active 修订但无片段 → 200 空列表
    pid_active = _create_project(client, token, "T52Active")
    _import_active(client, token, pid_active, TEXT_A)
    r = _list_fragments(client, token, pid_active)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "next_cursor": None}

    # (c) 项目不存在 → 404 kind=project（≠ 空列表）
    ghost = "01" * 13
    r = _list_fragments(client, token, ghost)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "project", "id": ghost}


# T53 F2 ?state= 过滤：candidate/confirmed/retired 各精确；派生值
# pending_review 合法但恒空集（只列 active 版本）→ 200 空列表；
# 非法值 → 422 validation_failed(field=state, rule=invalid)，message 列合法值
def test_t53_state_filter_and_invalid_422(db, client):
    token = _register(client, "f23_t53_judy")
    pid = _create_project(client, token, "T53Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    f1 = _create_fragment(client, token, pid, rev["id"], 0, 5, "T53a")
    assert f1.status_code == 201, f1.text
    fid1 = f1.json()["object_ref"]["id"]
    f2 = _create_fragment(client, token, pid, rev["id"], 5, 10, "T53b", expected=2)
    assert f2.status_code == 201, f2.text
    fid2 = f2.json()["object_ref"]["id"]
    f3 = _create_fragment(client, token, pid, rev["id"], 10, 15, "T53c", expected=3)
    assert f3.status_code == 201, f3.text
    fid3 = f3.json()["object_ref"]["id"]

    # F1 确认、F2 退役（F7 preview/apply；退役不动 cas）
    r = _confirm(client, token, pid, fid1, 1)
    assert r.status_code == 200, r.text
    cas = _range_sets(pid, rev["id"])[0].cas_revision
    rp = _retire_preview(client, token, pid, fid2)
    assert rp.status_code == 200, rp.text
    ra = _retire_apply(
        client, token, pid, fid2, rp.json()["preview_id"],
        expected_revision=1, expected_range_set_revision=cas,
    )
    assert ra.status_code == 200, ra.text

    # 无过滤 → 3 条全列（含 retired）
    r = _list_fragments(client, token, pid)
    assert r.status_code == 200, r.text
    assert {it["object_ref"]["id"] for it in r.json()["items"]} == {fid1, fid2, fid3}

    # state 过滤各精确
    r = _list_fragments(client, token, pid, "?state=candidate")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [it["object_ref"]["id"] for it in items] == [fid3]
    assert items[0]["state"] == "candidate"
    r = _list_fragments(client, token, pid, "?state=confirmed")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [it["object_ref"]["id"] for it in items] == [fid1]
    assert items[0]["state"] == "confirmed"
    r = _list_fragments(client, token, pid, "?state=retired")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [it["object_ref"]["id"] for it in items] == [fid2]
    assert items[0]["state"] == "retired"
    assert items[0]["retired_at"] is not None

    # 派生值 pending_review：合法过滤值，active 版本内恒空集 → 200 空列表
    r = _list_fragments(client, token, pid, "?state=pending_review")
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "next_cursor": None}

    # 非法值 → 422 validation_failed(field=state, rule=invalid)，message 列合法值
    r = _list_fragments(client, token, pid, "?state=bogus")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "state"
    assert v["rule"] == "invalid"
    assert v["message"] == "state 必须是 candidate/confirmed/retired/pending_review 之一。"


# T54 F2 keyset 分页（逐字对齐 P2）：默认序 id DESC（新→旧）；limit=1 两页 +
# next_cursor 续读；limit clamp（0→1、500→200）不报 422
def test_t54_pagination_keyset_desc(db, client):
    token = _register(client, "f23_t54_karen")
    pid = _create_project(client, token, "T54Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    f1 = _create_fragment(client, token, pid, rev["id"], 0, 5, "T54a")
    assert f1.status_code == 201, f1.text
    fid1 = f1.json()["object_ref"]["id"]
    time.sleep(0.002)  # 保证 ULID 严格递增
    f2 = _create_fragment(client, token, pid, rev["id"], 5, 10, "T54b", expected=2)
    assert f2.status_code == 201, f2.text
    fid2 = f2.json()["object_ref"]["id"]
    assert fid2 > fid1, "ULID 应严格递增"

    # 默认序 id DESC（新→旧），无溢出 → next_cursor 为 null
    r = _list_fragments(client, token, pid)
    assert r.status_code == 200, r.text
    assert [it["object_ref"]["id"] for it in r.json()["items"]] == [fid2, fid1]
    assert r.json()["next_cursor"] is None

    # 第一页 limit=1 → 最新 1 条，next_cursor = 该条 id
    r1 = _list_fragments(client, token, pid, "?limit=1")
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert [it["object_ref"]["id"] for it in b1["items"]] == [fid2]
    assert b1["next_cursor"] == fid2

    # 第二页 cursor 续读 → 最旧 1 条，next_cursor = null
    r2 = _list_fragments(client, token, pid, f"?cursor={fid2}&limit=1")
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert [it["object_ref"]["id"] for it in b2["items"]] == [fid1]
    assert b2["next_cursor"] is None

    # limit clamp（P2 同款）：0→1、500→200，不报 422
    r0 = _list_fragments(client, token, pid, "?limit=0")
    assert r0.status_code == 200, r0.text
    assert len(r0.json()["items"]) == 1
    r500 = _list_fragments(client, token, pid, "?limit=500")
    assert r500.status_code == 200, r500.text
    assert len(r500.json()["items"]) == 2


# T55 F2 cursor 格式非法（非 26 字符 Crockford 小写；含空串）→
# 422 validation_failed(field=cursor, rule=format)
def test_t55_cursor_format_422(db, client):
    token = _register(client, "f23_t55_laura")
    pid = _create_project(client, token, "T55Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    f1 = _create_fragment(client, token, pid, rev["id"], 0, 5, "T55a")
    assert f1.status_code == 201, f1.text
    r = _list_fragments(client, token, pid, "?cursor=xyz")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "cursor" and v0["rule"] == "format"
    assert v0["message"] == "cursor 必须是 26 字符 ULID。"
    # 空串 cursor 同样 422
    r0 = _list_fragments(client, token, pid, "?cursor=")
    assert r0.status_code == 422, r0.text
    assert r0.json()["error"]["details"]["violations"][0]["rule"] == "format"
