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
from backend.studio.projects.models import StudioProject
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
    # S4/S5 激活命令未放行（sources 路由仅有 POST/GET）：测试内直接把项目
    # active 指针指向 v2，构造"v1 非 active"状态
    with Session() as s:
        s.execute(
            update(StudioProject)
            .where(StudioProject.id == pid)
            .values(active_source_revision_id=v2["id"])
        )
        s.commit()
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


# T22 非 active 版本上的候选片段（直改 active，T4 同款构造）
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
    # 测试内直接把项目 active 指针指向 v2，构造"片段绑定版本非 active"状态
    with Session() as s:
        s.execute(
            update(StudioProject)
            .where(StudioProject.id == pid)
            .values(active_source_revision_id=v2["id"])
        )
        s.commit()
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
    # 直改 active 指针 → v2（S4/S5 未放行，测试库专用构造）
    with Session() as s:
        s.execute(
            update(StudioProject)
            .where(StudioProject.id == pid)
            .values(active_source_revision_id=v2["id"])
        )
        s.commit()
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
