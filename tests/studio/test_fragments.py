"""F1 原子保存候选片段（附录 02 §2 F1）—— POST /api/studio/projects/{pid}/fragments 行为验证。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件不自行改写 STUDIO_* env。db fixture 与 register/auth helper 照抄
tests/studio/test_sources.py 的实现；测试参数 (db, client) 序。
每测先建项目 + S1 导入一版作为 active 正文。
"""
import threading
import time
import uuid

import pytest
from sqlalchemy import select, text, update

from backend.core.db import Base, Session, engine
from backend.studio.projects.models import StudioProject
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
