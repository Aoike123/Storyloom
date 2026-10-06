"""F11 完整制作范围提交 + F12 范围读（附录 02 §2 F11/F12 + §1 表语义）——
POST /api/studio/projects/{pid}/production-scopes、
GET .../production-scopes（?cursor=&limit=）与 .../production-scopes/current
行为验证。

SOURCE-11 实现并验收（T1–T10）。fixture/helper 复用 tests/studio 既有模式
（db fixture、_register/_auth/_cmd/_create_project/_import/_import_active/
_activate（S4/S5 真实激活，照 test_fragment_changes 模式）/_create_fragment/
_confirm/_count；跨域模型经 tests/studio/conftest.py 统一注册）；本文件不写
STUDIO_* env（session 级由 conftest 统一设定，engine 是进程单例）。
"""
import json
import time
import uuid

import pytest
from sqlalchemy import text

from backend.core.db import Base, Session, engine

# 基准正文
TEXT20 = "01234567890123456789"  # 20 字符，无空白（片段 {0,10}/{10,20} 自足）
TEXT30 = "012345678901234567890123456789"  # 30 字符，无空白（T9 三片段）
TEXT_WS = "aaaa  bbbb    cccc"  # 18 字符；非空白段 [0,4)/[6,10)/[14,18)，空白 [4,6)/[10,14)
TEXT_B = "abcdefghijklmnopqrst"  # rev2 用正文（20 字符）


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（projects↔source_revisions 等）：drop 前临时关 FK
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


def _import_active(client, token, pid, content, command_id=None) -> dict:
    """S1 导入即激活（首版），返回修订对象 dict。"""
    r = _import(client, token, pid, content, command_id)
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


def _confirm(client, token, pid, fid, expected=1, command_id=None):
    r = client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/confirm",
        json={"expected_revision": expected, "command_id": command_id or _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _mk_fragment(client, token, pid, rev_id, start, end, name, expected) -> str:
    """F1 建候选（断言 201）并返回 fid。"""
    r = _create_fragment(client, token, pid, rev_id, start, end, name, expected=expected)
    assert r.status_code == 201, r.text
    return r.json()["object_ref"]["id"]


def _create_scope(client, token, pid, fragment_ids=None, command_id=None):
    """F11 请求 helper：fragment_ids=None 即缺省（null = 全部已确认片段）。"""
    return client.post(
        f"/api/studio/projects/{pid}/production-scopes",
        json={"fragment_ids": fragment_ids, "command_id": command_id or _cmd()},
        headers=_auth(token),
    )


def _list_scopes(client, token, pid, cursor=None, limit=None):
    params = {}
    if cursor is not None:
        params["cursor"] = cursor
    if limit is not None:
        params["limit"] = limit
    return client.get(
        f"/api/studio/projects/{pid}/production-scopes",
        params=params,
        headers=_auth(token),
    )


def _current_scope(client, token, pid):
    return client.get(
        f"/api/studio/projects/{pid}/production-scopes/current",
        headers=_auth(token),
    )


def _count(table) -> int:
    """按表名计数（表名为冻结常量，无注入面）。"""
    with Session() as s:
        return s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def _scope_rows(pid) -> list:
    """项目 scope 行（列名→值 dict，按 id 排序）。"""
    with Session() as s:
        rows = s.execute(
            text("SELECT * FROM studio_production_scopes WHERE project_id = :p"),
            {"p": pid},
        ).mappings().all()
    return sorted((dict(r) for r in rows), key=lambda r: r["id"])


_DTO_KEYS = {
    "object_ref",
    "source_revision_id",
    "fragment_ids",
    "covered",
    "excluded_ranges",
    "created_at",
}


# ────────────────────────────────────────────────────────────────────────────
# T1 无 token → 401（三路由各一）
# ────────────────────────────────────────────────────────────────────────────
def test_t1_no_token_401(db, client):
    pid = "01" * 13
    r1 = client.post(
        f"/api/studio/projects/{pid}/production-scopes",
        json={"fragment_ids": None, "command_id": _cmd()},
    )
    r2 = client.get(f"/api/studio/projects/{pid}/production-scopes")
    r3 = client.get(f"/api/studio/projects/{pid}/production-scopes/current")
    for r in (r1, r2, r3):
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "unauthenticated"


# ────────────────────────────────────────────────────────────────────────────
# T2 项目不存在/跨属主 → 404 同形状不泄漏（三路由）
# ────────────────────────────────────────────────────────────────────────────
def test_t2_project_404_same_shape(db, client):
    ta = _register(client, "s11_t2_alice")
    tb = _register(client, "s11_t2_bob")
    pb = _create_project(client, tb, "T2B")
    _import_active(client, tb, pb, TEXT20)

    ghost = "02" * 13
    # (a) POST / (b) GET list / (c) GET current：不存在与跨属主两路同形状
    for make_req in (
        lambda p: _create_scope(client, ta, p, None),
        lambda p: _list_scopes(client, ta, p),
        lambda p: _current_scope(client, ta, p),
    ):
        r1 = make_req(ghost)
        assert r1.status_code == 404, r1.text
        e1 = r1.json()["error"]
        assert e1["code"] == "not_found"
        assert e1["details"] == {"kind": "project", "id": ghost}
        r2 = make_req(pb)  # 跨属主（他账号真实项目）→ 同形状，不泄漏存在性
        assert r2.status_code == 404, r2.text
        e2 = r2.json()["error"]
        assert e2["code"] == "not_found"
        assert e2["details"] == {"kind": "project", "id": pb}
        assert e2["message"] == e1["message"]


# ────────────────────────────────────────────────────────────────────────────
# T3 形状：空数组 422 required；重复 id 422 duplicate；零副作用（无 scope 行）
# ────────────────────────────────────────────────────────────────────────────
def test_t3_body_shape_422_zero_side_effects(db, client):
    token = _register(client, "s11_t3_carol")
    pid = _create_project(client, token, "T3Proj")
    _import_active(client, token, pid, TEXT20)

    # (a) 空数组 → 422 validation_failed(field=fragment_ids, rule=required)
    r1 = _create_scope(client, token, pid, [])
    assert r1.status_code == 422, r1.text
    err1 = r1.json()["error"]
    assert err1["code"] == "validation_failed"
    v1 = err1["details"]["violations"][0]
    assert v1 == {
        "field": "fragment_ids",
        "rule": "required",
        "message": "fragment_ids 不能为空数组；省略或传 null 表示全部已确认片段。",
    }

    # (b) 重复 id → 422 rule=duplicate（第 3 步短路，id 无需存在）
    dup = "03" * 13
    r2 = _create_scope(client, token, pid, [dup, dup])
    assert r2.status_code == 422, r2.text
    err2 = r2.json()["error"]
    assert err2["code"] == "validation_failed"
    v2 = err2["details"]["violations"][0]
    assert v2 == {
        "field": "fragment_ids",
        "rule": "duplicate",
        "message": "fragment_ids 含重复片段 ID。",
    }

    # 零副作用：无 scope 行
    assert _count("studio_production_scopes") == 0


# ────────────────────────────────────────────────────────────────────────────
# T4 片段不存在（26 字符字面量）/跨项目 → 404 kind=fragment 不泄漏（同形状）
# ────────────────────────────────────────────────────────────────────────────
def test_t4_fragment_404_same_shape(db, client):
    ta = _register(client, "s11_t4_alice")
    tb = _register(client, "s11_t4_bob")
    pa = _create_project(client, ta, "T4A")
    _import_active(client, ta, pa, TEXT20)
    pb = _create_project(client, tb, "T4B")
    rev_b = _import_active(client, tb, pb, TEXT20)
    fid_b = _mk_fragment(client, tb, pb, rev_b["id"], 0, 10, "B0", expected=None)

    ghost = "04" * 13  # 26 字符字面量
    r1 = _create_scope(client, ta, pa, [ghost])
    assert r1.status_code == 404, r1.text
    e1 = r1.json()["error"]
    assert e1["code"] == "not_found"
    assert e1["details"] == {"kind": "fragment", "id": ghost}

    # 跨项目（他项目真实片段 id）→ 同形状，不泄漏片段存在性
    r2 = _create_scope(client, ta, pa, [fid_b])
    assert r2.status_code == 404, r2.text
    e2 = r2.json()["error"]
    assert e2["code"] == "not_found"
    assert e2["details"] == {"kind": "fragment", "id": fid_b}
    assert e2["message"] == e1["message"]
    assert _count("studio_production_scopes") == 0


# ────────────────────────────────────────────────────────────────────────────
# T5 片段绑非 active（真实 S4/S5 激活 rev2 后引用 rev1 片段）→ 422
#    source_revision_inactive；candidate 未确认 → 422 not_confirmed
#    （blocked_by 精确、message 含名与状态）
# ────────────────────────────────────────────────────────────────────────────
def test_t5_inactive_and_not_confirmed_422(db, client):
    token = _register(client, "s11_t5_dana")
    pid = _create_project(client, token, "T5Proj")
    rev1 = _import_active(client, token, pid, TEXT20)
    f1 = _mk_fragment(client, token, pid, rev1["id"], 0, 10, "T5F1", expected=None)
    time.sleep(0.002)
    rev2 = _import(client, token, pid, TEXT_B).json()
    _activate(client, token, pid, rev2["id"], rev1["id"])  # 真实 S4/S5 切 active→rev2

    # (a) rev1 片段（绑非 active）→ 422 source_revision_inactive，blocked_by 精确
    r1 = _create_scope(client, token, pid, [f1])
    assert r1.status_code == 422, r1.text
    err1 = r1.json()["error"]
    assert err1["code"] == "precondition_failed"
    assert err1["details"]["reason"] == "source_revision_inactive"
    assert err1["details"]["blocked_by"] == {
        "kind": "fragment",
        "id": f1,
        "revision": 1,
    }

    # (b) rev2 上 candidate 未确认 → 422 not_confirmed（message 含名与持久 state）
    g = _mk_fragment(client, token, pid, rev2["id"], 0, 10, "T5G", expected=None)
    r2 = _create_scope(client, token, pid, [g])
    assert r2.status_code == 422, r2.text
    err2 = r2.json()["error"]
    assert err2["code"] == "precondition_failed"
    assert err2["details"]["reason"] == "not_confirmed"
    assert err2["details"]["blocked_by"] == {
        "kind": "fragment",
        "id": g,
        "revision": 1,
    }
    assert "T5G" in err2["message"]
    assert "candidate" in err2["message"]
    assert _count("studio_production_scopes") == 0


# ────────────────────────────────────────────────────────────────────────────
# T6 有效提交：2 confirmed 片段 {0,10}/{10,20} @ 20 字符无空白文本 → 201 恰 6
#    字段、covered=true、excluded_ranges=[]、fragment_ids=请求序；DB 行精确
# ────────────────────────────────────────────────────────────────────────────
def test_t6_valid_submit_201_db_exact(db, client):
    token = _register(client, "s11_t6_earl")
    pid = _create_project(client, token, "T6Proj")
    rev = _import_active(client, token, pid, TEXT20)
    f0 = _mk_fragment(client, token, pid, rev["id"], 0, 10, "T6F0", expected=None)
    f1 = _mk_fragment(client, token, pid, rev["id"], 10, 20, "T6F1", expected=2)
    _confirm(client, token, pid, f0)
    _confirm(client, token, pid, f1)

    r = _create_scope(client, token, pid, [f1, f0])  # 请求序（反序以证保持）
    assert r.status_code == 201, r.text
    body = r.json()
    # 恰 6 字段（冻结形状）
    assert set(body.keys()) == _DTO_KEYS
    assert body["object_ref"] == {
        "kind": "production_scope",
        "id": body["object_ref"]["id"],
        "revision": None,
    }
    assert isinstance(body["object_ref"]["id"], str)
    assert len(body["object_ref"]["id"]) == 26
    assert body["source_revision_id"] == rev["id"]
    assert body["fragment_ids"] == [f1, f0]  # 请求序保持
    assert body["covered"] is True
    assert body["excluded_ranges"] == []
    assert isinstance(body["created_at"], float)

    # DB 行精确（json 内容、covered=1）
    rows = _scope_rows(pid)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == body["object_ref"]["id"]
    assert row["project_id"] == pid
    assert row["source_revision_id"] == rev["id"]
    assert json.loads(row["fragment_ids"]) == [f1, f0]
    assert row["covered"] == 1
    assert json.loads(row["excluded_ranges"]) == []
    assert row["created_at"] == body["created_at"]


# ────────────────────────────────────────────────────────────────────────────
# T7 覆盖语义：正文含空白（"aaaa  bbbb    cccc"）；3 confirmed（aaaa/bbbb/
#    cccc 三段）只显式列 2 → covered=false 且 excluded_ranges 精确=被裁段的
#    非空白连续段（空白切割生效）；同项目缺省 null → 全部 confirmed →
#    covered=true、excluded=[]
# ────────────────────────────────────────────────────────────────────────────
def test_t7_coverage_semantics_with_blank(db, client):
    token = _register(client, "s11_t7_fred")
    pid = _create_project(client, token, "T7Proj")
    rev = _import_active(client, token, pid, TEXT_WS)
    fa = _mk_fragment(client, token, pid, rev["id"], 0, 4, "T7A", expected=None)
    fb = _mk_fragment(client, token, pid, rev["id"], 6, 10, "T7B", expected=2)
    fc = _mk_fragment(client, token, pid, rev["id"], 14, 18, "T7C", expected=3)
    for fid in (fa, fb, fc):
        _confirm(client, token, pid, fid)

    # (a) 显式列 aaaa/bbbb 两段（裁掉 cccc）→ covered=false；
    # excluded_ranges 精确=被裁段的最大连续非空白段 [14,18)
    # （空白 [4,6)/[10,14) 虽在范围外，但不属于任何段——空白切割生效）
    r1 = _create_scope(client, token, pid, [fa, fb])
    assert r1.status_code == 201, r1.text
    body1 = r1.json()
    assert set(body1.keys()) == _DTO_KEYS
    assert body1["covered"] is False
    assert body1["excluded_ranges"] == [{"start": 14, "end": 18}]
    assert body1["fragment_ids"] == [fa, fb]

    # (b) 同项目缺省 null → 全部 confirmed（按 id 排序）→ covered=true、excluded=[]
    r2 = _create_scope(client, token, pid, None)
    assert r2.status_code == 201, r2.text
    body2 = r2.json()
    assert body2["covered"] is True
    assert body2["excluded_ranges"] == []
    assert body2["fragment_ids"] == sorted([fa, fb, fc])
    # 两读一致：裁减 confirmed 片段必使其非空白范围裸露（(a) 即证）；
    # 覆盖全部 confirmed（=全部非空白码元）才算 covered（(b) 即证）


# ────────────────────────────────────────────────────────────────────────────
# T8 duplicate_scope + 重放：同 command_id 同参 → 200 原体零副作用（scope 行
#    恰 1）；新 command_id 同覆盖（同集合不同请求序）→ 422 duplicate_scope
#    （scope_ref=首行 id）；裁减后新 command → 201 新行（历史只追加）
# ────────────────────────────────────────────────────────────────────────────
def test_t8_duplicate_scope_and_replay(db, client):
    token = _register(client, "s11_t8_gina")
    pid = _create_project(client, token, "T8Proj")
    rev = _import_active(client, token, pid, TEXT20)
    fa = _mk_fragment(client, token, pid, rev["id"], 0, 10, "T8A", expected=None)
    fb = _mk_fragment(client, token, pid, rev["id"], 10, 20, "T8B", expected=2)
    _confirm(client, token, pid, fa)
    _confirm(client, token, pid, fb)

    # 首次提交 [fa, fb] → 201
    c1 = _cmd()
    r1 = _create_scope(client, token, pid, [fa, fb], command_id=c1)
    assert r1.status_code == 201, r1.text
    body1 = r1.json()
    scope_id_1 = body1["object_ref"]["id"]

    # 同 command_id 同参 → 重放：200 原体（00 §3），零副作用（scope 行恰 1）
    r2 = _create_scope(client, token, pid, [fa, fb], command_id=c1)
    assert r2.status_code == 200, r2.text
    assert r2.json() == body1
    assert _count("studio_production_scopes") == 1

    # 新 command_id 同覆盖（同集合不同请求序）→ 422 duplicate_scope（scope_ref=首行 id）
    r3 = _create_scope(client, token, pid, [fb, fa])
    assert r3.status_code == 422, r3.text
    err3 = r3.json()["error"]
    assert err3["code"] == "duplicate_scope"
    assert err3["details"] == {
        "scope_ref": {"kind": "production_scope", "id": scope_id_1, "revision": None}
    }
    assert err3["message"] == "相同片段覆盖的制作范围已提交过，历史只追加不重复。"
    assert _count("studio_production_scopes") == 1

    # 裁减后新 command → 201 新行（历史只追加，两行间 fragment_ids 不同）
    r4 = _create_scope(client, token, pid, [fa])
    assert r4.status_code == 201, r4.text
    body4 = r4.json()
    assert body4["fragment_ids"] == [fa]
    assert body4["covered"] is False
    assert body4["excluded_ranges"] == [{"start": 10, "end": 20}]
    rows = _scope_rows(pid)
    assert len(rows) == 2
    assert json.loads(rows[0]["fragment_ids"]) == [fa, fb]
    assert json.loads(rows[1]["fragment_ids"]) == [fa]
    assert rows[0]["id"] != rows[1]["id"]


# ────────────────────────────────────────────────────────────────────────────
# T9 F12：造 3 个 scope 行（同版本，不同片段集合）→ list 倒序（id DESC）+
#    limit=1 分页两页 + next_cursor 续读正确；新项目（无 active）→ list 空 +
#    current 404；有 active 无 scope → current 404
# ────────────────────────────────────────────────────────────────────────────
def test_t9_list_pagination_and_empty_states(db, client):
    token = _register(client, "s11_t9_hank")
    pid = _create_project(client, token, "T9Proj")
    rev = _import_active(client, token, pid, TEXT30)
    fa = _mk_fragment(client, token, pid, rev["id"], 0, 10, "T9A", expected=None)
    fb = _mk_fragment(client, token, pid, rev["id"], 10, 20, "T9B", expected=2)
    fc = _mk_fragment(client, token, pid, rev["id"], 20, 30, "T9C", expected=3)
    for fid in (fa, fb, fc):
        _confirm(client, token, pid, fid)

    sids = []
    for ids in ([fa], [fa, fb], [fa, fb, fc]):  # 不同片段集合（逐行严格递增）
        r = _create_scope(client, token, pid, ids)
        assert r.status_code == 201, r.text
        sids.append(r.json()["object_ref"]["id"])
        time.sleep(0.002)  # 保证 ULID 严格递增（同 test_source_changes 模式）
    s1, s2, s3 = sids

    # list 倒序（id DESC），无分页参数时一次返回全部
    r = _list_scopes(client, token, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"items", "next_cursor"}
    assert [it["object_ref"]["id"] for it in body["items"]] == [s3, s2, s1]
    assert body["next_cursor"] is None
    for it in body["items"]:
        assert set(it.keys()) == _DTO_KEYS
        assert it["source_revision_id"] == rev["id"]

    # limit=1 分页三页 + next_cursor 续读正确
    p1 = _list_scopes(client, token, pid, limit=1)
    assert p1.status_code == 200, p1.text
    assert [it["object_ref"]["id"] for it in p1.json()["items"]] == [s3]
    assert p1.json()["next_cursor"] == s3
    p2 = _list_scopes(client, token, pid, cursor=p1.json()["next_cursor"], limit=1)
    assert p2.status_code == 200, p2.text
    assert [it["object_ref"]["id"] for it in p2.json()["items"]] == [s2]
    assert p2.json()["next_cursor"] == s2
    p3 = _list_scopes(client, token, pid, cursor=p2.json()["next_cursor"], limit=1)
    assert p3.status_code == 200, p3.text
    assert [it["object_ref"]["id"] for it in p3.json()["items"]] == [s1]
    assert p3.json()["next_cursor"] is None

    # current = id 最大一行（s3）
    rc = _current_scope(client, token, pid)
    assert rc.status_code == 200, rc.text
    assert rc.json()["object_ref"]["id"] == s3
    assert rc.json()["fragment_ids"] == [fa, fb, fc]

    # 新项目（无 active）→ list 空 + current 404
    pid2 = _create_project(client, token, "T9NoActive")
    r_list2 = _list_scopes(client, token, pid2)
    assert r_list2.status_code == 200, r_list2.text
    assert r_list2.json() == {"items": [], "next_cursor": None}
    r_cur2 = _current_scope(client, token, pid2)
    assert r_cur2.status_code == 404, r_cur2.text
    e2 = r_cur2.json()["error"]
    assert e2["code"] == "not_found"
    assert e2["details"] == {"kind": "production_scope", "id": pid2}

    # 有 active 无 scope → current 404（list 亦空）
    pid3 = _create_project(client, token, "T9NoScope")
    _import_active(client, token, pid3, TEXT20)
    r_cur3 = _current_scope(client, token, pid3)
    assert r_cur3.status_code == 404, r_cur3.text
    e3 = r_cur3.json()["error"]
    assert e3["code"] == "not_found"
    assert e3["details"] == {"kind": "production_scope", "id": pid3}
    assert _list_scopes(client, token, pid3).json() == {"items": [], "next_cursor": None}


# ────────────────────────────────────────────────────────────────────────────
# T10 切版本：真实 S4/S5 激活 rev2 → list 只含 rev2 的 scope（rev1 的不在
#     列）、current 404（rev2 无 scope）；rev2 上确认片段后 F11 提交 → 201、
#     current==新行
# ────────────────────────────────────────────────────────────────────────────
def test_t10_version_switch_scopes_follow_active(db, client):
    token = _register(client, "s11_t10_ivan")
    pid = _create_project(client, token, "T10Proj")
    rev1 = _import_active(client, token, pid, TEXT20)
    fa = _mk_fragment(client, token, pid, rev1["id"], 0, 10, "T10A", expected=None)
    _confirm(client, token, pid, fa)
    r1 = _create_scope(client, token, pid, [fa])  # rev1 上一条 scope
    assert r1.status_code == 201, r1.text
    sid_rev1 = r1.json()["object_ref"]["id"]
    assert _current_scope(client, token, pid).json()["object_ref"]["id"] == sid_rev1

    # 真实 S4/S5 激活 rev2
    time.sleep(0.002)
    rev2 = _import(client, token, pid, TEXT_B).json()
    _activate(client, token, pid, rev2["id"], rev1["id"])

    # list 只含 rev2 的 scope（rev1 的不在列）→ 空；current 404（rev2 无 scope）
    r_list = _list_scopes(client, token, pid)
    assert r_list.status_code == 200, r_list.text
    assert r_list.json() == {"items": [], "next_cursor": None}
    assert sid_rev1 not in r_list.text
    r_cur = _current_scope(client, token, pid)
    assert r_cur.status_code == 404, r_cur.text
    e = r_cur.json()["error"]
    assert e["code"] == "not_found"
    assert e["details"] == {"kind": "production_scope", "id": pid}

    # rev2 上确认片段后 F11 提交 → 201、current==新行
    g = _mk_fragment(client, token, pid, rev2["id"], 0, 20, "T10G", expected=None)
    _confirm(client, token, pid, g)
    r2 = _create_scope(client, token, pid, None)
    assert r2.status_code == 201, r2.text
    body2 = r2.json()
    assert body2["source_revision_id"] == rev2["id"]
    assert body2["fragment_ids"] == [g]
    assert body2["covered"] is True
    assert body2["excluded_ranges"] == []
    rc2 = _current_scope(client, token, pid)
    assert rc2.status_code == 200, rc2.text
    assert rc2.json() == body2
    # list 恰含新行（rev1 的仍不在列）
    rl2 = _list_scopes(client, token, pid)
    assert [it["object_ref"]["id"] for it in rl2.json()["items"]] == [
        body2["object_ref"]["id"]
    ]
    # 历史只追加：rev1 的 scope 行仍在库中（未被删改）
    rows = _scope_rows(pid)
    assert len(rows) == 2
    assert {row["source_revision_id"] for row in rows} == {rev1["id"], rev2["id"]}
