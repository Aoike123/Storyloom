"""F8 片段边界变更（preview/apply，附录 02 §2 F8 + 00 §5）—— POST
/api/studio/projects/{pid}/fragments/{fid}/boundary 与
/api/studio/projects/{pid}/fragments/{fid}/boundary/apply 行为验证。

本文件覆盖 F8–F10（边界/拆分/合并）preview/apply 命令族；SOURCE-06 实现并
验收 F8 边界，SOURCE-07 实现并验收 F9 拆分，SOURCE-08 实现并验收 F10 合并
（复用同一 fixture/helper 模式）。

fixture/helper 从 tests/studio/test_fragments.py 复制模式（db fixture、
_register/_auth/_cmd/_create_project/_import/_import_active/_create_fragment/
_range_sets/_fragment_revisions/_preview_row、跨域模型经 tests/studio/conftest.py
统一注册、_seed 按 SOURCE-05-b 裁定 6 复用模式）；本文件不写 STUDIO_* env
（session 级由 conftest 统一设定，engine 是进程单例）。
"""
import hashlib
import json
import time
import uuid

import pytest
from sqlalchemy import select, text, update

from backend.core.db import Base, Session, engine, make_ulid
from backend.studio.projects.models import StudioProject
from backend.studio.reviews.models import ChangePreview, ReviewDecision
from backend.studio.sources.fragment_models import Fragment, FragmentRevision, RangeSet

# 基准正文
TEXT_A = "01234567890123456789"  # 20 字符，无空白
TEXT_B = "aa🚀aa"  # UTF-16 长 6；索引 2/3 为 🚀 的代理对
TEXT_C = "   "  # 3 空格，全空白（blank 用例正文）


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


def _boundary_preview(client, token, pid, fid, new_range, target=None, command_id=None):
    return client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/boundary",
        json={
            "target_fragment_id": target if target is not None else fid,
            "new_range": new_range,
            "command_id": command_id or _cmd(),
        },
        headers=_auth(token),
    )


def _boundary_apply(
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
        f"/api/studio/projects/{pid}/fragments/{fid}/boundary/apply",
        json={
            "preview_id": preview_id,
            "command_id": command_id or _cmd(),
            "expected_revision": expected_revision,
            "expected_range_set_revision": expected_range_set_revision,
        },
        headers=_auth(token),
    )


def _count(table) -> int:
    """按表名计数（表名为冻结常量，无注入面）。"""
    with Session() as s:
        return s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def _range_sets(pid, src_id) -> list:
    with Session() as s:
        return s.execute(
            select(RangeSet).where(
                RangeSet.project_id == pid, RangeSet.source_revision_id == src_id
            )
        ).scalars().all()


def _fragments(pid) -> list:
    with Session() as s:
        return s.execute(select(Fragment).where(Fragment.project_id == pid)).scalars().all()


def _fragment_revisions(fid) -> list:
    with Session() as s:
        return (
            s.execute(select(FragmentRevision).where(FragmentRevision.fragment_id == fid))
            .scalars()
            .all()
        )


def _preview_row(preview_id) -> dict:
    with Session() as s:
        p = s.get(ChangePreview, preview_id)
        if p is None:
            return {}
        return {
            "project_id": p.project_id,
            "owner_id": p.owner_id,
            "kind": p.kind,
            "state": p.state,
            "decision_id": p.decision_id,
            "resolved_at": p.resolved_at,
            "created_at": p.created_at,
            "expires_at": p.expires_at,
            "payload": p.payload,
            "baseline": p.baseline,
        }


def _user_id(username) -> str:
    with Session() as s:
        from backend.core.accounts import User

        return (
            s.execute(select(User).where(User.username == username)).scalars().first()
        ).id


def _seed_fragment_direct(pid, src_id, start, end, name) -> tuple:
    """全空白正文上直接落 range_set + fragment + revision（绕过 F1 的 blank 校验，
    同 _seed_t40 直插模式）。返回 (set_id, frag_id, rev_id)。"""
    now = time.time()
    set_id = make_ulid()
    frag_id = make_ulid()
    rev_id = make_ulid()
    with Session() as s:
        s.execute(
            text(
                "INSERT INTO studio_range_sets (id, project_id, source_revision_id, "
                "cas_revision, created_at) VALUES (:id, :pid, :src, 1, :now)"
            ),
            {"id": set_id, "pid": pid, "src": src_id, "now": now},
        )
        s.execute(
            text(
                "INSERT INTO studio_fragment_revisions (id, fragment_id, revision, "
                "source_revision_id, range_start, range_end, reason, "
                "predecessor_fragment_ids, created_at) "
                "VALUES (:id, :fid, 1, :src, :s, :e, 'created', '[]', :now)"
            ),
            {"id": rev_id, "fid": frag_id, "src": src_id, "s": start, "e": end, "now": now},
        )
        s.execute(
            text(
                "INSERT INTO studio_fragments (id, project_id, source_revision_id, "
                "range_set_id, name, summary, state, current_revision_id, "
                "created_at, updated_at, retired_at) "
                "VALUES (:id, :pid, :src, :set, :name, NULL, 'candidate', :cur, :now, :now, NULL)"
            ),
            {
                "id": frag_id,
                "pid": pid,
                "src": src_id,
                "set": set_id,
                "name": name,
                "cur": rev_id,
                "now": now,
            },
        )
        s.commit()
    return set_id, frag_id, rev_id


def _seed_impact(pid, uid, fid) -> dict:
    """T10 跨域种子：SourceRelation(source_fragment_id=fid, revision=1) +
    StudioJob(status='pending', payload 含 fid 与 rev1 范围字面)。直接 INSERT
    （test_fragments._seed_t40 同款已接受模式）。"""
    now = time.time()
    relation_id = make_ulid()
    job_id = make_ulid()
    with Session() as s:
        s.execute(
            text(
                "INSERT INTO studio_source_relations (id, project_id, "
                "source_fragment_id, source_fragment_revision, target_kind, target_id, "
                "target_revision, created_at) "
                "VALUES (:id, :pid, :fid, 1, 'script_object', :tid, 1, :now)"
            ),
            {"id": relation_id, "pid": pid, "fid": fid, "tid": "09" * 13, "now": now},
        )
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
                "payload": json.dumps({"fragment_id": fid, "range": {"start": 0, "end": 10}}),
                "now": now,
            },
        )
        s.commit()
    return {"relation_id": relation_id, "job_id": job_id}


def _source_relation_row(relation_id):
    with Session() as s:
        return s.execute(
            text(
                "SELECT id, project_id, source_fragment_id, source_fragment_revision, "
                "target_kind, target_id, target_revision, created_at "
                "FROM studio_source_relations WHERE id = :id"
            ),
            {"id": relation_id},
        ).first()


def _job_row(job_id):
    with Session() as s:
        return s.execute(
            text(
                "SELECT id, project_id, owner_id, kind, payload, status, priority, "
                "created_at, attempts FROM studio_jobs WHERE id = :id"
            ),
            {"id": job_id},
        ).first()


# ────────────────────────────────────────────────────────────────────────────
# T1 无 token → 401（preview 与 apply 各一）
# ────────────────────────────────────────────────────────────────────────────
def test_t1_no_token_401(db, client):
    ghost = "03" * 13
    pid = "01" * 13
    r1 = client.post(
        f"/api/studio/projects/{pid}/fragments/{ghost}/boundary",
        json={
            "target_fragment_id": ghost,
            "new_range": {"start": 0, "end": 5},
            "command_id": _cmd(),
        },
    )
    assert r1.status_code == 401
    assert r1.json()["error"]["code"] == "unauthenticated"
    r2 = client.post(
        f"/api/studio/projects/{pid}/fragments/{ghost}/boundary/apply",
        json={
            "preview_id": ghost,
            "command_id": _cmd(),
            "expected_revision": 1,
            "expected_range_set_revision": 1,
        },
    )
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "unauthenticated"


# ────────────────────────────────────────────────────────────────────────────
# T2 preview fid 不存在/跨项目 → 404 kind=fragment（不泄漏）；apply preview 不存在
#    → 404 kind=preview
# ────────────────────────────────────────────────────────────────────────────
def test_t2_unknown_and_cross_project_404(db, client):
    ta = _register(client, "f8_t2_alice")
    tc = _register(client, "f8_t2_carol")
    pa = _create_project(client, ta, "T2A")
    pc = _create_project(client, tc, "T2C")
    rev_a = _import_active(client, ta, pa, TEXT_A)
    rev_c = _import_active(client, tc, pc, TEXT_A)
    rc = _create_fragment(client, tc, pc, rev_c["id"], 0, 5, "F2C")
    assert rc.status_code == 201, rc.text
    fid_c = rc.json()["object_ref"]["id"]
    # 本项目 (pa) 一个有效片段，供 apply 路径使用
    ra = _create_fragment(client, ta, pa, rev_a["id"], 0, 5, "F2A")
    assert ra.status_code == 201, ra.text
    fid_a = ra.json()["object_ref"]["id"]

    ghost = "03" * 13
    # preview fid 不存在 → 404 kind=fragment
    r = _boundary_preview(client, ta, pa, ghost, new_range={"start": 0, "end": 5})
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": ghost}
    # preview 跨项目 fid → 404 kind=fragment（不泄漏存在性）
    r = _boundary_preview(client, ta, pa, fid_c, new_range={"start": 0, "end": 5})
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": fid_c}
    # apply preview 不存在 → 404 kind=preview
    r = _boundary_apply(
        client, ta, pa, fid_a, ghost,
        expected_revision=1, expected_range_set_revision=1,
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "preview", "id": ghost}


# ────────────────────────────────────────────────────────────────────────────
# T3 target ≠ 路径 → 422 mismatch（preview 与 apply 各一）
# ────────────────────────────────────────────────────────────────────────────
def test_t3_target_mismatch_422(db, client):
    token = _register(client, "f8_t3_bob")
    pid = _create_project(client, token, "T3Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rcx = _create_fragment(client, token, pid, rev["id"], 0, 5, "F3x")
    assert rcx.status_code == 201, rcx.text
    fid_x = rcx.json()["object_ref"]["id"]
    rcy = _create_fragment(client, token, pid, rev["id"], 5, 10, "F3y", expected=2)
    assert rcy.status_code == 201, rcy.text
    fid_y = rcy.json()["object_ref"]["id"]
    cas = _range_sets(pid, rev["id"])[0].cas_revision

    # preview：target ≠ 路径 → 422 mismatch
    r = _boundary_preview(client, token, pid, fid_x, new_range={"start": 0, "end": 5}, target="04" * 13)
    assert r.status_code == 422, r.text
    v = r.json()["error"]["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("target_fragment_id", "mismatch")
    assert v["message"] == "target_fragment_id 必须与路径片段一致。"

    # apply：preview 的 target（X）≠ 路径 fid（Y）→ 422 mismatch
    rp = _boundary_preview(client, token, pid, fid_x, new_range={"start": 0, "end": 5})
    assert rp.status_code == 200, rp.text
    preview_id = rp.json()["preview_id"]
    r = _boundary_apply(
        client, token, pid, fid_y, preview_id,
        expected_revision=1, expected_range_set_revision=cas,
    )
    assert r.status_code == 422, r.text
    v = r.json()["error"]["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("target_fragment_id", "mismatch")


# ────────────────────────────────────────────────────────────────────────────
# T4 非 active 版本片段 preview（直改 active 构造，同 test_fragments T37 模式）
#    → 422 source_revision_inactive
# ────────────────────────────────────────────────────────────────────────────
def test_t4_inactive_source_422(db, client):
    token = _register(client, "f8_t4_carol")
    pid = _create_project(client, token, "T4Proj")
    v1 = _import_active(client, token, pid, TEXT_A)
    time.sleep(0.002)
    r2 = _import(client, token, pid, "second version content")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()
    rc = _create_fragment(client, token, pid, v1["id"], 0, 5, "F4")
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
    r = _boundary_preview(client, token, pid, fid, new_range={"start": 0, "end": 5})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    assert err["details"]["blocked_by"] == {"kind": "source", "id": v2["id"], "revision": None}


# ────────────────────────────────────────────────────────────────────────────
# T5 new_range 非法：四子用例断言 rule 与 message 含 canonical 长度
#    {5,5}→empty；{0,25}(len20)→out_of_bounds；{2,3}@TEXT_B→proxy_split；
#    {0,3}@全空白→blank
# ────────────────────────────────────────────────────────────────────────────
def test_t5_invalid_new_range_422(db, client):
    # (a) empty 与 (b) out_of_bounds 同用 TEXT_A（len 20）
    token_a = _register(client, "f8_t5_alice")
    pid_a = _create_project(client, token_a, "T5a")
    rev_a = _import_active(client, token_a, pid_a, TEXT_A)
    f_a = _create_fragment(client, token_a, pid_a, rev_a["id"], 0, 10, "F5a")
    assert f_a.status_code == 201, f_a.text
    fid_a = f_a.json()["object_ref"]["id"]

    r = _boundary_preview(client, token_a, pid_a, fid_a, new_range={"start": 5, "end": 5})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "range_invalid"
    assert r.json()["error"]["details"]["rule"] == "empty"
    assert "20" in r.json()["error"]["message"]

    r = _boundary_preview(client, token_a, pid_a, fid_a, new_range={"start": 0, "end": 25})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "range_invalid"
    assert r.json()["error"]["details"]["rule"] == "out_of_bounds"
    assert "20" in r.json()["error"]["message"]

    # (c) proxy_split {2,3} 于 TEXT_B（len 6；2/3 为 🚀 代理对内侧）
    token_b = _register(client, "f8_t5_bob")
    pid_b = _create_project(client, token_b, "T5b")
    rev_b = _import_active(client, token_b, pid_b, TEXT_B)
    f_b = _create_fragment(client, token_b, pid_b, rev_b["id"], 0, 2, "F5b")
    assert f_b.status_code == 201, f_b.text
    fid_b = f_b.json()["object_ref"]["id"]
    r = _boundary_preview(client, token_b, pid_b, fid_b, new_range={"start": 2, "end": 3})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "range_invalid"
    assert r.json()["error"]["details"]["rule"] == "proxy_split"
    assert "6" in r.json()["error"]["message"]

    # (d) blank {0,3} 于全空白正文 TEXT_C（len 3；直插片段绕过 F1 blank 校验）
    token_c = _register(client, "f8_t5_carol")
    pid_c = _create_project(client, token_c, "T5c")
    rev_c = _import_active(client, token_c, pid_c, TEXT_C)
    _, fid_c, _ = _seed_fragment_direct(pid_c, rev_c["id"], 0, 3, "F5c")
    r = _boundary_preview(client, token_c, pid_c, fid_c, new_range={"start": 0, "end": 3})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "range_invalid"
    assert r.json()["error"]["details"]["rule"] == "blank"
    assert "3" in r.json()["error"]["message"]


# ────────────────────────────────────────────────────────────────────────────
# T6 preview 阶段重叠：{5,15} vs {10,20} → 409 range_overlap（conflicts 恰 1）；
#    相接：{10,15} vs {0,10} 相接于 10（严格 < 判定，无重叠）→ 通过到 preview 创建 200
# ────────────────────────────────────────────────────────────────────────────
def test_t6_overlap_409_and_touching_200(db, client):
    # (a) 重叠 → 409
    token_a = _register(client, "f8_t6_alice")
    pid_a = _create_project(client, token_a, "T6a")
    rev_a = _import_active(client, token_a, pid_a, TEXT_A)
    fa = _create_fragment(client, token_a, pid_a, rev_a["id"], 0, 10, "F6A")
    assert fa.status_code == 201, fa.text
    fid_a = fa.json()["object_ref"]["id"]
    fb = _create_fragment(client, token_a, pid_a, rev_a["id"], 10, 20, "F6B", expected=2)
    assert fb.status_code == 201, fb.text
    fid_b = fb.json()["object_ref"]["id"]

    r = _boundary_preview(client, token_a, pid_a, fid_a, new_range={"start": 5, "end": 15})
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "range_overlap"
    conf = err["details"]["conflicts"]
    assert len(conf) == 1
    assert conf[0] == {"fragment_id": fid_b, "name": "F6B", "start": 10, "end": 20}
    # message 点名首个冲突
    assert "F6B" in err["message"]
    assert fid_b in err["message"]

    # (b) 相接 → 200（preview 创建成功）
    token_b = _register(client, "f8_t6_bob")
    pid_b = _create_project(client, token_b, "T6b")
    rev_b = _import_active(client, token_b, pid_b, TEXT_A)
    fl = _create_fragment(client, token_b, pid_b, rev_b["id"], 0, 10, "F6L")
    assert fl.status_code == 201, fl.text
    fr = _create_fragment(client, token_b, pid_b, rev_b["id"], 10, 20, "F6R", expected=2)
    assert fr.status_code == 201, fr.text
    fid_r = fr.json()["object_ref"]["id"]
    cas = _range_sets(pid_b, rev_b["id"])[0].cas_revision

    r = _boundary_preview(client, token_b, pid_b, fid_r, new_range={"start": 10, "end": 15})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"preview_id", "kind", "baseline", "impact"}
    assert body["kind"] == "fragment_boundary"
    # R 的新范围 {10,15} 与 L {0,10} 在 10 处相接（严格 < 判定 → 无重叠），合法
    assert body["baseline"]["fragment"] == {"kind": "fragment", "id": fid_r, "revision": 1}
    assert body["baseline"]["range_set"] == {"id": _range_sets(pid_b, rev_b["id"])[0].id, "cas": cas}


# ────────────────────────────────────────────────────────────────────────────
# T7 有效 preview（candidate 片段 {0,10} → 新 {0,15}）→ 200 恰 4 字段；
#    kind/baseline 精确；impact 三键（空表→空列表）；DB preview 行 pending + TTL 窗
# ────────────────────────────────────────────────────────────────────────────
def test_t7_valid_preview_200(db, client):
    token = _register(client, "f8_t7_dana")
    pid = _create_project(client, token, "T7Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F7")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    set_id = _range_sets(pid, rev["id"])[0].id
    cas = _range_sets(pid, rev["id"])[0].cas_revision

    r = _boundary_preview(client, token, pid, fid, new_range={"start": 0, "end": 15})
    assert r.status_code == 200, r.text
    body = r.json()
    # 恰 4 字段（00 §5.1 冻结形状）
    assert set(body.keys()) == {"preview_id", "kind", "baseline", "impact"}
    assert body["kind"] == "fragment_boundary"
    # baseline 与 F7 同形（fragment 当前 revision + range_set id/cas）
    assert body["baseline"] == {
        "fragment": {"kind": "fragment", "id": fid, "revision": 1},
        "range_set": {"id": set_id, "cas": cas},
    }
    # impact 三键；本项目无跨域引用 → 三列表均空
    assert set(body["impact"].keys()) == {"affected", "needs_review", "preservable"}
    assert body["impact"] == {"affected": [], "needs_review": [], "preservable": []}

    # DB preview 行：pending + 30 分钟 TTL 窗
    pv = _preview_row(body["preview_id"])
    assert pv["kind"] == "fragment_boundary"
    assert pv["state"] == "pending"
    assert pv["decision_id"] is None
    assert pv["resolved_at"] is None
    assert pv["owner_id"] == _user_id("f8_t7_dana")
    assert 1700 < pv["expires_at"] - pv["created_at"] < 1900
    # payload 记录 target 与 new_range
    assert json.loads(pv["payload"]) == {
        "target_fragment_id": fid,
        "new_range": {"start": 0, "end": 15},
    }


# ────────────────────────────────────────────────────────────────────────────
# T8 有效 apply（新 command_id；expected=1；expected_cas=当前）→ 200：
#    object_ref.revision==2、range=={0,15}；DB：FragmentRevision 恰 2 行
#    （rev1 created + rev2 boundary predecessor=[]）、fragment 指向 rev2、
#    range_set.cas==原+1、preview applied、ReviewDecision 恰 1、command +1
# ────────────────────────────────────────────────────────────────────────────
def test_t8_valid_apply_200(db, client):
    token = _register(client, "f8_t8_earl")
    pid = _create_project(client, token, "T8Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F8")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas0 = _range_sets(pid, rev["id"])[0].cas_revision

    rp = _boundary_preview(client, token, pid, fid, new_range={"start": 0, "end": 15})
    assert rp.status_code == 200, rp.text
    preview_id = rp.json()["preview_id"]

    cmds_before = _count("studio_command_records")
    ra = _boundary_apply(
        client, token, pid, fid, preview_id,
        expected_revision=1, expected_range_set_revision=cas0,
    )
    assert ra.status_code == 200, ra.text
    body = ra.json()
    # F3 形状 DTO
    assert body["object_ref"] == {"kind": "fragment", "id": fid, "revision": 2}
    assert body["name"] == "F8"
    assert body["summary"] is None
    assert body["state"] == "candidate"  # 持久状态不变（边界不改确认/候选）
    assert body["revision_is_active"] is True
    assert body["range"] == {"start": 0, "end": 15}
    assert body["source_revision_id"] == rev["id"]
    assert body["predecessor_ids"] == []
    assert body["retired_at"] is None
    assert body["updated_at"] > body["created_at"]

    # DB：FragmentRevision 恰 2 行（rev1 {0,10} created + rev2 {0,15} boundary []）
    revs = sorted(_fragment_revisions(fid), key=lambda r: r.revision)
    assert len(revs) == 2
    assert (revs[0].revision, revs[0].range_start, revs[0].range_end, revs[0].reason) == (1, 0, 10, "created")
    assert (revs[1].revision, revs[1].range_start, revs[1].range_end, revs[1].reason) == (2, 0, 15, "boundary")
    assert json.loads(revs[1].predecessor_fragment_ids) == []
    assert revs[1].source_revision_id == rev["id"]
    # fragment 指向 rev2
    with Session() as s:
        f = s.get(Fragment, fid)
        assert f.current_revision_id == revs[1].id
        assert f.state == "candidate"
        assert f.updated_at > f.created_at
    # range_set.cas == 原 + 1（F8 是 CAS 写点）
    assert _range_sets(pid, rev["id"])[0].cas_revision == cas0 + 1
    # preview applied（decision_id / resolved_at 非空）
    pv = _preview_row(preview_id)
    assert pv["state"] == "applied"
    assert pv["resolved_at"] is not None
    assert pv["decision_id"] is not None
    # ReviewDecision 恰 1 行：target_ref.revision==2、digest 独立重算相等
    with Session() as s:
        decisions = s.execute(select(ReviewDecision).where(ReviewDecision.project_id == pid)).scalars().all()
    assert len(decisions) == 1
    d = decisions[0]
    assert d.decision == "applied"
    assert d.preview_id == preview_id
    assert json.loads(d.target_ref) == {"kind": "fragment", "id": fid, "revision": 2}
    base = json.loads(_preview_row(preview_id)["baseline"])
    assert d.baseline_digest == hashlib.sha256(
        json.dumps(base, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    # command record +1
    assert _count("studio_command_records") == cmds_before + 1


# ────────────────────────────────────────────────────────────────────────────
# T9 CAS 失败无半次改变：preview 后另一 F1 使 cas+1 → apply → 409 preview_stale
#    （conflicts 恰 1 条 range_set{expected,actual}）；预览 superseded；DB 零变化
# ────────────────────────────────────────────────────────────────────────────
def test_t9_cas_conflict_no_half_save(db, client):
    token = _register(client, "f8_t9_frank")
    pid = _create_project(client, token, "T9Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F9")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas0 = _range_sets(pid, rev["id"])[0].cas_revision

    rp = _boundary_preview(client, token, pid, fid, new_range={"start": 0, "end": 15})
    assert rp.status_code == 200, rp.text
    preview_id = rp.json()["preview_id"]

    # 另一 F1 建新片段 → cas +1
    rc2 = _create_fragment(client, token, pid, rev["id"], 10, 20, "F9b", expected=cas0)
    assert rc2.status_code == 201, rc2.text
    cas1 = _range_sets(pid, rev["id"])[0].cas_revision
    assert cas1 == cas0 + 1

    # 快照 apply 前 DB 状态（用于验证零变化）
    revs_before = sorted(_fragment_revisions(fid), key=lambda r: r.revision)
    with Session() as s:
        frag_before = s.get(Fragment, fid)
        cur_rev_before = frag_before.current_revision_id
    dec_before = _count("studio_review_decisions")
    cmds_before = _count("studio_command_records")

    # apply：expected_revision=1（未变）、expected_range_set_revision=cas1（刷新后的 cas）
    ra = _boundary_apply(
        client, token, pid, fid, preview_id,
        expected_revision=1, expected_range_set_revision=cas1,
    )
    assert ra.status_code == 409, ra.text
    err = ra.json()["error"]
    assert err["code"] == "preview_stale"
    assert err["details"]["preview_id"] == preview_id
    conflicts = err["details"]["conflicts"]
    # CAS 预检（step 6）已过（expected 刷新到 cas1）；失配在 baseline（step 7）
    assert len(conflicts) == 1
    assert conflicts[0] == {
        "object": {"kind": "range_set", "id": _range_sets(pid, rev["id"])[0].id},
        "expected": cas0,
        "actual": cas1,
    }
    # 预览置 superseded
    assert _preview_row(preview_id)["state"] == "superseded"

    # DB 零变化：revision 行数不变、cas 不变、无新 Revision、无 Decision、
    # current_revision_id 不变
    revs_after = sorted(_fragment_revisions(fid), key=lambda r: r.revision)
    assert len(revs_after) == len(revs_before) == 1
    assert revs_after[0].id == revs_before[0].id
    assert _range_sets(pid, rev["id"])[0].cas_revision == cas1  # 未再 +1
    with Session() as s:
        frag_after = s.get(Fragment, fid)
        assert frag_after.current_revision_id == cur_rev_before
    assert _count("studio_review_decisions") == dec_before
    assert _count("studio_command_records") == cmds_before  # 失败的 apply 不写 record


# ────────────────────────────────────────────────────────────────────────────
# T10 旧引用不迁移（R11）+ 下游待复核：种子 SourceRelation(rev1) + pending Job
#    → preview impact：affected 恰 1（source_relation rev1）、needs_review 恰 1；
#    apply 后 SourceRelation 逐字段不变（source_fragment_revision 仍 1）、job
#    payload 不变；新 revision=2 不"偷走"旧引用
# ────────────────────────────────────────────────────────────────────────────
def test_t10_old_refs_not_migrated(db, client):
    token = _register(client, "f8_t10_gina")
    uid = _user_id("f8_t10_gina")
    pid = _create_project(client, token, "T10Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F10")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas0 = _range_sets(pid, rev["id"])[0].cas_revision
    seeds = _seed_impact(pid, uid, fid)

    # preview：impact.affected 恰 1（source_relation revision=1）、needs_review 恰 1（job）
    rp = _boundary_preview(client, token, pid, fid, new_range={"start": 0, "end": 15})
    assert rp.status_code == 200, rp.text
    impact = rp.json()["impact"]
    assert impact["affected"] == [
        {"object_ref": {"kind": "source_relation", "id": seeds["relation_id"], "revision": 1}}
    ]
    assert impact["needs_review"] == [
        {
            "object_ref": {"kind": "job", "id": seeds["job_id"], "revision": None},
            "reason": "frozen_input_contains_fragment",
        }
    ]
    assert impact["preservable"] == []

    # apply 前快照
    rel_before = _source_relation_row(seeds["relation_id"])
    job_before = _job_row(seeds["job_id"])
    assert rel_before is not None and job_before is not None

    ra = _boundary_apply(
        client, token, pid, fid, rp.json()["preview_id"],
        expected_revision=1, expected_range_set_revision=cas0,
    )
    assert ra.status_code == 200, ra.text
    assert ra.json()["object_ref"]["revision"] == 2

    # apply 后：SourceRelation 逐字段不变（source_fragment_revision 仍 1）、job payload 不变
    rel_after = _source_relation_row(seeds["relation_id"])
    job_after = _job_row(seeds["job_id"])
    assert rel_after == rel_before
    assert job_after == job_before
    # source_fragment_revision 仍 1：旧引用仍指旧范围（R11——旧引用不自动迁移）
    with Session() as s:
        rel = s.execute(
            text(
                "SELECT source_fragment_id, source_fragment_revision FROM "
                "studio_source_relations WHERE id = :id"
            ),
            {"id": seeds["relation_id"]},
        ).first()
        assert rel[0] == fid
        assert rel[1] == 1
    # 片段已升到 rev2（新边界），但旧 SourceRelation 不指向它
    assert len(_fragment_revisions(fid)) == 2


# ────────────────────────────────────────────────────────────────────────────
# T11 重放：preview 同 command_id+同 body → 200 原 body、预览恰 1 行；
#    apply 同 command_id → 200 原 body、revision 行数与决定行数不变（零副作用）
# ────────────────────────────────────────────────────────────────────────────
def test_t11_replay_zero_side_effects(db, client):
    token = _register(client, "f8_t11_hank")
    pid = _create_project(client, token, "T11Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "F11")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas0 = _range_sets(pid, rev["id"])[0].cas_revision

    # (a) preview 幂等：同 command_id + 同 body → 200 原 body，预览恰 1 行
    c1 = _cmd()
    r1 = _boundary_preview(client, token, pid, fid, new_range={"start": 0, "end": 15}, command_id=c1)
    assert r1.status_code == 200, r1.text
    r2 = _boundary_preview(client, token, pid, fid, new_range={"start": 0, "end": 15}, command_id=c1)
    assert r2.status_code == 200, r2.text  # 重放（非新建）
    assert r2.json() == r1.json()
    assert r2.json()["preview_id"] == r1.json()["preview_id"]
    assert _count("studio_change_previews") == 1  # 零副作用：不产生第二行

    # (b) apply 幂等：新 command_id 正常 apply → 再同 command_id → 200 原 body，
    #     revision 行数与决定行数不变
    c2 = _cmd()
    a1 = _boundary_apply(
        client, token, pid, fid, r1.json()["preview_id"],
        command_id=c2, expected_revision=1, expected_range_set_revision=cas0,
    )
    assert a1.status_code == 200, a1.text
    assert a1.json()["object_ref"]["revision"] == 2
    revs_before = len(_fragment_revisions(fid))
    dec_before = _count("studio_review_decisions")
    a2 = _boundary_apply(
        client, token, pid, fid, r1.json()["preview_id"],
        command_id=c2, expected_revision=1, expected_range_set_revision=cas0,
    )
    assert a2.status_code == 200, a2.text  # 重放
    assert a2.json() == a1.json()
    assert len(_fragment_revisions(fid)) == revs_before  # 零副作用
    assert _count("studio_review_decisions") == dec_before  # 零副作用
    assert _count("studio_change_previews") == 1  # 预览仍恰 1 行


# ────────────────────────────────────────────────────────────────────────────
# F9 片段拆分 helper（复用本文件 fixture/helper 模式；不写 STUDIO_* env）
# ────────────────────────────────────────────────────────────────────────────
def _split_preview(
    client,
    token,
    pid,
    fid,
    split_point,
    left_name=None,
    right_name=None,
    target=None,
    command_id=None,
):
    return client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/split",
        json={
            "target_fragment_id": target if target is not None else fid,
            "split_point": split_point,
            "left_name": left_name,
            "right_name": right_name,
            "command_id": command_id or _cmd(),
        },
        headers=_auth(token),
    )


def _split_apply(
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
        f"/api/studio/projects/{pid}/fragments/{fid}/split/apply",
        json={
            "preview_id": preview_id,
            "command_id": command_id or _cmd(),
            "expected_revision": expected_revision,
            "expected_range_set_revision": expected_range_set_revision,
        },
        headers=_auth(token),
    )


def _decision_rows() -> list:
    with Session() as s:
        return s.execute(select(ReviewDecision)).scalars().all()


# F9 空白半段用例正文（7 字符：3 空格 + 'a' + 3 空格）
TEXT_D = "   a   "


# ────────────────────────────────────────────────────────────────────────────
# T12 无 token → 401（preview/apply 各一）
# ────────────────────────────────────────────────────────────────────────────
def test_t12_no_token_401(db, client):
    ghost = "03" * 13
    pid = "01" * 13
    r1 = client.post(
        f"/api/studio/projects/{pid}/fragments/{ghost}/split",
        json={
            "target_fragment_id": ghost,
            "split_point": 4,
            "command_id": _cmd(),
        },
    )
    assert r1.status_code == 401
    assert r1.json()["error"]["code"] == "unauthenticated"
    r2 = client.post(
        f"/api/studio/projects/{pid}/fragments/{ghost}/split/apply",
        json={
            "preview_id": ghost,
            "command_id": _cmd(),
            "expected_revision": 1,
            "expected_range_set_revision": 1,
        },
    )
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "unauthenticated"


# ────────────────────────────────────────────────────────────────────────────
# T13 preview fid 不存在（26 字符字面量）/跨项目 → 404 kind=fragment（不泄漏）；
#     apply preview 不存在 → 404 kind=preview
# ────────────────────────────────────────────────────────────────────────────
def test_t13_unknown_and_cross_project_404(db, client):
    ta = _register(client, "f9_t13_alice")
    tc = _register(client, "f9_t13_carol")
    pa = _create_project(client, ta, "T13A")
    pc = _create_project(client, tc, "T13C")
    rev_a = _import_active(client, ta, pa, TEXT_A)
    rev_c = _import_active(client, tc, pc, TEXT_A)
    rc = _create_fragment(client, tc, pc, rev_c["id"], 0, 5, "T13C")
    assert rc.status_code == 201, rc.text
    fid_c = rc.json()["object_ref"]["id"]
    ra = _create_fragment(client, ta, pa, rev_a["id"], 0, 5, "T13A")
    assert ra.status_code == 201, ra.text
    fid_a = ra.json()["object_ref"]["id"]

    ghost = "03" * 13  # 26 字符
    # preview fid 不存在 → 404 kind=fragment
    r = _split_preview(client, ta, pa, ghost, 2)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": ghost}
    # preview 跨项目 fid → 404 kind=fragment（不泄漏存在性）
    r = _split_preview(client, ta, pa, fid_c, 2)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": fid_c}
    # apply preview 不存在 → 404 kind=preview
    r = _split_apply(
        client, ta, pa, fid_a, ghost,
        expected_revision=1, expected_range_set_revision=1,
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "preview", "id": ghost}


# ────────────────────────────────────────────────────────────────────────────
# T14 target ≠ 路径 → 422 mismatch（preview 与 apply 各一）
# ────────────────────────────────────────────────────────────────────────────
def test_t14_target_mismatch_422(db, client):
    token = _register(client, "f9_t14_bob")
    pid = _create_project(client, token, "T14Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rcx = _create_fragment(client, token, pid, rev["id"], 0, 5, "T14x")
    assert rcx.status_code == 201, rcx.text
    fid_x = rcx.json()["object_ref"]["id"]
    rcy = _create_fragment(client, token, pid, rev["id"], 5, 10, "T14y", expected=2)
    assert rcy.status_code == 201, rcy.text
    fid_y = rcy.json()["object_ref"]["id"]
    cas = _range_sets(pid, rev["id"])[0].cas_revision

    # preview：target ≠ 路径 → 422 mismatch
    r = _split_preview(client, token, pid, fid_x, 2, target="04" * 13)
    assert r.status_code == 422, r.text
    v = r.json()["error"]["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("target_fragment_id", "mismatch")
    assert v["message"] == "target_fragment_id 必须与路径片段一致。"

    # apply：preview 的 target（X）≠ 路径 fid（Y）→ 422 mismatch
    rp = _split_preview(client, token, pid, fid_x, 2)
    assert rp.status_code == 200, rp.text
    r = _split_apply(
        client, token, pid, fid_y, rp.json()["preview_id"],
        expected_revision=1, expected_range_set_revision=cas,
    )
    assert r.status_code == 422, r.text
    v = r.json()["error"]["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("target_fragment_id", "mismatch")


# ────────────────────────────────────────────────────────────────────────────
# T15 非 active 版本片段 preview（直改 active 构造，同 F8 T4 模式）→
#     422 source_revision_inactive；已 retired（直改 state+retired_at）→
#     422 already_retired
# ────────────────────────────────────────────────────────────────────────────
def test_t15_inactive_and_retired_422(db, client):
    # (a) 非 active 版本
    token_a = _register(client, "f9_t15_alice")
    pid_a = _create_project(client, token_a, "T15a")
    v1 = _import_active(client, token_a, pid_a, TEXT_A)
    time.sleep(0.002)
    r2 = _import(client, token_a, pid_a, "second version content")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()
    rc = _create_fragment(client, token_a, pid_a, v1["id"], 0, 10, "T15a")
    assert rc.status_code == 201, rc.text
    fid_a = rc.json()["object_ref"]["id"]
    # 直改 active 指针 → v2（S4/S5 未放行，测试库专用构造）
    with Session() as s:
        s.execute(
            update(StudioProject)
            .where(StudioProject.id == pid_a)
            .values(active_source_revision_id=v2["id"])
        )
        s.commit()
    r = _split_preview(client, token_a, pid_a, fid_a, 4)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    assert err["details"]["blocked_by"] == {"kind": "source", "id": v2["id"], "revision": None}

    # (b) 已 retired（直改 state + retired_at）
    token_b = _register(client, "f9_t15_bob")
    pid_b = _create_project(client, token_b, "T15b")
    rev_b = _import_active(client, token_b, pid_b, TEXT_A)
    rc = _create_fragment(client, token_b, pid_b, rev_b["id"], 0, 10, "T15b")
    assert rc.status_code == 201, rc.text
    fid_b = rc.json()["object_ref"]["id"]
    now = time.time()
    with Session() as s:
        s.execute(
            update(Fragment)
            .where(Fragment.id == fid_b)
            .values(state="retired", retired_at=now)
        )
        s.commit()
    r = _split_preview(client, token_b, pid_b, fid_b, 4)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "already_retired"
    assert err["details"]["blocked_by"] == {"kind": "fragment", "id": fid_b, "revision": 1}


# ────────────────────────────────────────────────────────────────────────────
# T16 split_point 非法（冻结五判定）：片段 {0,10} @ TEXT_A（20 字符无空白）：
#     =0/=10 → empty；=11/=-1 → out_of_bounds；TEXT_B {0,6} @3 → proxy_split；
#     "   a   " {0,7} @3 → 左半全空白 → blank（断言 rule 与 message 含长度/区间）
# ────────────────────────────────────────────────────────────────────────────
def test_t16_invalid_split_point_422(db, client):
    # (a)–(d) TEXT_A（len 20 无空白），片段 {0,10}
    token_a = _register(client, "f9_t16_alice")
    pid_a = _create_project(client, token_a, "T16a")
    rev_a = _import_active(client, token_a, pid_a, TEXT_A)
    f_a = _create_fragment(client, token_a, pid_a, rev_a["id"], 0, 10, "T16a")
    assert f_a.status_code == 201, f_a.text
    fid_a = f_a.json()["object_ref"]["id"]

    r = _split_preview(client, token_a, pid_a, fid_a, 0)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "range_invalid"
    assert r.json()["error"]["details"]["rule"] == "empty"
    msg = r.json()["error"]["message"]
    assert "20" in msg and "[0, 10)" in msg and "split_point=0" in msg

    r = _split_preview(client, token_a, pid_a, fid_a, 10)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "range_invalid"
    assert r.json()["error"]["details"]["rule"] == "empty"
    msg = r.json()["error"]["message"]
    assert "20" in msg and "[0, 10)" in msg and "split_point=10" in msg

    r = _split_preview(client, token_a, pid_a, fid_a, 11)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "range_invalid"
    assert r.json()["error"]["details"]["rule"] == "out_of_bounds"
    msg = r.json()["error"]["message"]
    assert "20" in msg and "[0, 10)" in msg and "split_point=11" in msg

    r = _split_preview(client, token_a, pid_a, fid_a, -1)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "range_invalid"
    assert r.json()["error"]["details"]["rule"] == "out_of_bounds"
    msg = r.json()["error"]["message"]
    assert "20" in msg and "[0, 10)" in msg and "split_point=-1" in msg

    # (e) TEXT_B "aa🚀aa"（UTF-16 长 6；2/3 为 🚀 代理对内侧），片段 {0,6} @3
    token_b = _register(client, "f9_t16_bob")
    pid_b = _create_project(client, token_b, "T16b")
    rev_b = _import_active(client, token_b, pid_b, TEXT_B)
    f_b = _create_fragment(client, token_b, pid_b, rev_b["id"], 0, 6, "T16b")
    assert f_b.status_code == 201, f_b.text
    fid_b = f_b.json()["object_ref"]["id"]
    r = _split_preview(client, token_b, pid_b, fid_b, 3)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "range_invalid"
    assert r.json()["error"]["details"]["rule"] == "proxy_split"
    msg = r.json()["error"]["message"]
    assert "6" in msg and "[0, 6)" in msg and "split_point=3" in msg

    # (f) TEXT_D "   a   "（7 字符），片段 {0,7} @3 → 左半 "   " 全空白 → blank
    token_c = _register(client, "f9_t16_carol")
    pid_c = _create_project(client, token_c, "T16c")
    rev_c = _import_active(client, token_c, pid_c, TEXT_D)
    f_c = _create_fragment(client, token_c, pid_c, rev_c["id"], 0, 7, "T16c")
    assert f_c.status_code == 201, f_c.text
    fid_c = f_c.json()["object_ref"]["id"]
    r = _split_preview(client, token_c, pid_c, fid_c, 3)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "range_invalid"
    assert r.json()["error"]["details"]["rule"] == "blank"
    msg = r.json()["error"]["message"]
    assert "7" in msg and "[0, 7)" in msg and "split_point=3" in msg


# ────────────────────────────────────────────────────────────────────────────
# T17 有效拆分（{0,10} @ TEXT_A，split_point=4，无 name）：preview 200 恰 4
#     字段（kind=fragment_split、baseline 精确、impact 三键）；apply 200
#     冻结形状 + DB 全量断言（原片段退役且 revision 行数不变、两新片段各 1
#     revision、cas+1、决定恰 1 行 digest 独立重算、preview applied）
# ────────────────────────────────────────────────────────────────────────────
def test_t17_valid_split_full(db, client):
    token = _register(client, "f9_t17_dave")
    pid = _create_project(client, token, "T17Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "T17")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    sets = _range_sets(pid, rev["id"])
    assert len(sets) == 1
    set_id = sets[0].id
    cas0 = sets[0].cas_revision

    # preview：200 恰 4 字段
    rp = _split_preview(client, token, pid, fid, 4)
    assert rp.status_code == 200, rp.text
    body = rp.json()
    assert set(body.keys()) == {"preview_id", "kind", "baseline", "impact"}
    assert body["kind"] == "fragment_split"
    assert body["baseline"] == {
        "fragment": {"kind": "fragment", "id": fid, "revision": 1},
        "range_set": {"id": set_id, "cas": cas0},
    }
    assert body["impact"] == {"affected": [], "needs_review": [], "preservable": []}
    preview_id = body["preview_id"]

    # apply：200 冻结形状
    ra = _split_apply(
        client, token, pid, fid, preview_id,
        expected_revision=1, expected_range_set_revision=cas0,
    )
    assert ra.status_code == 200, ra.text
    ab = ra.json()
    assert set(ab.keys()) == {"left", "right", "original"}
    left, right, orig = ab["left"], ab["right"], ab["original"]
    assert left["object_ref"] == {"kind": "fragment", "id": left["object_ref"]["id"], "revision": 1}
    assert left["range"] == {"start": 0, "end": 4}
    assert right["object_ref"]["revision"] == 1
    assert right["range"] == {"start": 4, "end": 10}
    assert orig == {"object_ref": {"kind": "fragment", "id": fid, "revision": 1}, "state": "retired"}
    left_id, right_id = left["object_ref"]["id"], right["object_ref"]["id"]
    assert left_id != right_id and left_id != fid and right_id != fid

    # DB：原片段 retired（retired_at 非空）且原 revision 行数不变
    with Session() as s:
        frag_now = s.get(Fragment, fid)
    assert frag_now.state == "retired"
    assert frag_now.retired_at is not None
    assert len(_fragment_revisions(fid)) == 1

    # 两新片段：state=candidate、各恰 1 revision 行（reason=split、
    # predecessor=[原 fid]、range 精确）、source_revision_id/range_set_id 同原
    by_id = {f.id: f for f in _fragments(pid)}
    assert len(by_id) == 3
    lf, rf = by_id[left_id], by_id[right_id]
    assert lf.state == "candidate" and rf.state == "candidate"
    assert lf.summary is None and rf.summary is None
    for nf, (s_, e_) in ((lf, (0, 4)), (rf, (4, 10))):
        assert nf.project_id == pid
        assert nf.source_revision_id == rev["id"]
        assert nf.range_set_id == set_id
        assert nf.retired_at is None
        revs = _fragment_revisions(nf.id)
        assert len(revs) == 1
        nr = revs[0]
        assert nr.revision == 1
        assert nr.reason == "split"
        assert nr.range_start == s_ and nr.range_end == e_
        assert nr.source_revision_id == rev["id"]
        assert json.loads(nr.predecessor_fragment_ids) == [fid]
        assert nf.current_revision_id == nr.id

    # cas == 原 + 1
    assert _range_sets(pid, rev["id"])[0].cas_revision == cas0 + 1

    # ReviewDecision 恰 1 行（target_ref 原片段引用、digest 独立重算相等）
    decisions = _decision_rows()
    assert len(decisions) == 1
    d = decisions[0]
    assert d.decision == "applied"
    assert d.preview_id == preview_id
    assert json.loads(d.target_ref) == {"kind": "fragment", "id": fid, "revision": 1}
    assert d.baseline_digest == hashlib.sha256(
        json.dumps(
            {
                "fragment": {"kind": "fragment", "id": fid, "revision": 1},
                "range_set": {"id": set_id, "cas": cas0},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    # preview applied
    assert _preview_row(preview_id)["state"] == "applied"


# ────────────────────────────────────────────────────────────────────────────
# T18 命名：preview left_name="  L1  "、right_name=None → apply 后左 name=="L1"、
#     右 name=="<原 name>（右）"；派生超长（原 name=118 字符、两 name 缺省）
#     → preview 422 validation_failed max_length
# ────────────────────────────────────────────────────────────────────────────
def test_t18_naming_derive_and_max_length(db, client):
    # (a) 给定名 trim + 缺省派生
    token_a = _register(client, "f9_t18_alice")
    pid_a = _create_project(client, token_a, "T18a")
    rev_a = _import_active(client, token_a, pid_a, TEXT_A)
    rc = _create_fragment(client, token_a, pid_a, rev_a["id"], 0, 10, "Orig")
    assert rc.status_code == 201, rc.text
    fid_a = rc.json()["object_ref"]["id"]
    cas_a = _range_sets(pid_a, rev_a["id"])[0].cas_revision
    rp = _split_preview(client, token_a, pid_a, fid_a, 4, left_name="  L1  ", right_name=None)
    assert rp.status_code == 200, rp.text
    ra = _split_apply(
        client, token_a, pid_a, fid_a, rp.json()["preview_id"],
        expected_revision=1, expected_range_set_revision=cas_a,
    )
    assert ra.status_code == 200, ra.text
    with Session() as s:
        lf = s.get(Fragment, ra.json()["left"]["object_ref"]["id"])
        rf = s.get(Fragment, ra.json()["right"]["object_ref"]["id"])
    assert lf.name == "L1"
    assert rf.name == "Orig（右）"

    # (b) 派生超长：原 name=118 字符 → 派生 121 字符 > 120
    token_b = _register(client, "f9_t18_bob")
    pid_b = _create_project(client, token_b, "T18b")
    rev_b = _import_active(client, token_b, pid_b, TEXT_A)
    rc = _create_fragment(client, token_b, pid_b, rev_b["id"], 0, 10, "x" * 118)
    assert rc.status_code == 201, rc.text
    fid_b = rc.json()["object_ref"]["id"]
    r = _split_preview(client, token_b, pid_b, fid_b, 4)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("left_name", "max_length")


# ────────────────────────────────────────────────────────────────────────────
# T19 CAS 冲突无半次：preview 后 F1 建新片段（cas+1）→ apply → 409
#     preview_stale（conflicts 恰 1 条 range_set）+ 预览 superseded；DB 零变化
# ────────────────────────────────────────────────────────────────────────────
def test_t19_cas_conflict_no_half_save(db, client):
    token = _register(client, "f9_t19_erin")
    pid = _create_project(client, token, "T19Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "T19")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas0 = _range_sets(pid, rev["id"])[0].cas_revision

    rp = _split_preview(client, token, pid, fid, 4)
    assert rp.status_code == 200, rp.text
    preview_id = rp.json()["preview_id"]

    # 另一 F1 建新片段 → cas +1
    rc2 = _create_fragment(client, token, pid, rev["id"], 10, 20, "T19b", expected=cas0)
    assert rc2.status_code == 201, rc2.text
    cas1 = _range_sets(pid, rev["id"])[0].cas_revision
    assert cas1 == cas0 + 1

    # apply：expected_revision=1（未变）、expected_range_set_revision=cas1
    #（刷新后的 cas；CAS 预检 step 6 通过，失配在 baseline step 7）
    ra = _split_apply(
        client, token, pid, fid, preview_id,
        expected_revision=1, expected_range_set_revision=cas1,
    )
    assert ra.status_code == 409, ra.text
    err = ra.json()["error"]
    assert err["code"] == "preview_stale"
    assert err["details"]["preview_id"] == preview_id
    conflicts = err["details"]["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0] == {
        "object": {"kind": "range_set", "id": _range_sets(pid, rev["id"])[0].id},
        "expected": cas0,
        "actual": cas1,
    }
    # 预览置 superseded
    assert _preview_row(preview_id)["state"] == "superseded"

    # DB 零变化：原片段仍 candidate、无左/右新片段、cas 不再 +1、无决定
    with Session() as s:
        frag_now = s.get(Fragment, fid)
    assert frag_now.state == "candidate"
    assert frag_now.retired_at is None
    assert len(_fragments(pid)) == 2  # 原片段 + T19b，无拆分产物
    assert _range_sets(pid, rev["id"])[0].cas_revision == cas1
    assert _decision_rows() == []
    assert len(_fragment_revisions(fid)) == 1  # 原 revision 行数不变


# ────────────────────────────────────────────────────────────────────────────
# T20 旧引用不迁移（R11）+ 下游待复核：种子 SourceRelation(rev1) + pending Job
#     → preview impact：affected 恰 1（source_relation rev1）、needs_review 恰 1
#     （job）、preservable 空；apply 后 SourceRelation 逐字段不变（仍指原 fid——
#     原片段已 retired，引用不迁移）、job payload 不变
# ────────────────────────────────────────────────────────────────────────────
def test_t20_old_refs_not_migrated(db, client):
    token = _register(client, "f9_t20_fay")
    uid = _user_id("f9_t20_fay")
    pid = _create_project(client, token, "T20Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "T20")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas0 = _range_sets(pid, rev["id"])[0].cas_revision
    seeds = _seed_impact(pid, uid, fid)

    # preview：impact.affected 恰 1（source_relation revision=1）、needs_review 恰 1（job）
    rp = _split_preview(client, token, pid, fid, 4)
    assert rp.status_code == 200, rp.text
    impact = rp.json()["impact"]
    assert impact["affected"] == [
        {"object_ref": {"kind": "source_relation", "id": seeds["relation_id"], "revision": 1}}
    ]
    assert impact["needs_review"] == [
        {
            "object_ref": {"kind": "job", "id": seeds["job_id"], "revision": None},
            "reason": "frozen_input_contains_fragment",
        }
    ]
    assert impact["preservable"] == []

    # apply 前快照
    rel_before = _source_relation_row(seeds["relation_id"])
    job_before = _job_row(seeds["job_id"])
    assert rel_before is not None and job_before is not None

    ra = _split_apply(
        client, token, pid, fid, rp.json()["preview_id"],
        expected_revision=1, expected_range_set_revision=cas0,
    )
    assert ra.status_code == 200, ra.text

    # apply 后：SourceRelation 逐字段不变（仍指原 fid——原片段已 retired，
    # 引用不迁移）、job payload 不变
    rel_after = _source_relation_row(seeds["relation_id"])
    job_after = _job_row(seeds["job_id"])
    assert rel_after == rel_before
    assert job_after == job_before
    assert rel_after[2] == fid  # source_fragment_id 列不变
    assert rel_after[3] == 1  # source_fragment_revision 仍 1


# ────────────────────────────────────────────────────────────────────────────
# T21 重放：preview 同 command_id+同 body → 200 原 body、预览恰 1 行；
#     apply 同 command_id → 200 原 body、左/右片段各恰 1 行（无重复创建）、
#     决定恰 1 行（零副作用）
# ────────────────────────────────────────────────────────────────────────────
def test_t21_replay_zero_side_effects(db, client):
    token = _register(client, "f9_t21_gus")
    pid = _create_project(client, token, "T21Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    rc = _create_fragment(client, token, pid, rev["id"], 0, 10, "T21")
    assert rc.status_code == 201, rc.text
    fid = rc.json()["object_ref"]["id"]
    cas0 = _range_sets(pid, rev["id"])[0].cas_revision

    # (a) preview 幂等：同 command_id + 同 body → 200 原 body，预览恰 1 行
    c1 = _cmd()
    r1 = _split_preview(client, token, pid, fid, 4, command_id=c1)
    assert r1.status_code == 200, r1.text
    r2 = _split_preview(client, token, pid, fid, 4, command_id=c1)
    assert r2.status_code == 200, r2.text  # 重放（非新建）
    assert r2.json() == r1.json()
    assert r2.json()["preview_id"] == r1.json()["preview_id"]
    assert _count("studio_change_previews") == 1  # 零副作用：不产生第二行

    # (b) apply 幂等：新 command_id 正常 apply → 再同 command_id → 200 原 body，
    #     左/右片段各恰 1 行（无重复创建）、决定恰 1 行
    c2 = _cmd()
    a1 = _split_apply(
        client, token, pid, fid, r1.json()["preview_id"],
        command_id=c2, expected_revision=1, expected_range_set_revision=cas0,
    )
    assert a1.status_code == 200, a1.text
    a2 = _split_apply(
        client, token, pid, fid, r1.json()["preview_id"],
        command_id=c2, expected_revision=1, expected_range_set_revision=cas0,
    )
    assert a2.status_code == 200, a2.text  # 重放
    assert a2.json() == a1.json()

    left_id = a1.json()["left"]["object_ref"]["id"]
    right_id = a1.json()["right"]["object_ref"]["id"]
    assert len(_fragments(pid)) == 3  # 原 + 左 + 右，无重复创建
    assert len(_fragment_revisions(left_id)) == 1
    assert len(_fragment_revisions(right_id)) == 1
    assert len(_fragment_revisions(fid)) == 1  # 原片段不新建 revision
    assert len(_decision_rows()) == 1  # 零副作用
    assert _count("studio_change_previews") == 1


# ────────────────────────────────────────────────────────────────────────────
# F10 片段合并 helper（复用本文件 fixture/helper 模式；不写 STUDIO_* env）
# ────────────────────────────────────────────────────────────────────────────
def _merge_preview(client, token, pid, fragment_ids, merged_name=None, command_id=None):
    return client.post(
        f"/api/studio/projects/{pid}/fragments/merge",
        json={
            "fragment_ids": fragment_ids,
            "merged_name": merged_name,
            "command_id": command_id or _cmd(),
        },
        headers=_auth(token),
    )


def _merge_apply(
    client,
    token,
    pid,
    preview_id,
    command_id=None,
    expected_revision_a=1,
    expected_revision_b=1,
    expected_range_set_revision=None,
):
    return client.post(
        f"/api/studio/projects/{pid}/fragments/merge/apply",
        json={
            "preview_id": preview_id,
            "command_id": command_id or _cmd(),
            "expected_revision_a": expected_revision_a,
            "expected_revision_b": expected_revision_b,
            "expected_range_set_revision": expected_range_set_revision,
        },
        headers=_auth(token),
    )


# F10 CAS 冲突用例正文（40 字符，无空白：四片 {0,10}/{10,20}/{20,30}/{30,40}）
TEXT_E = "0123456789" * 4


# ────────────────────────────────────────────────────────────────────────────
# T22 无 token → 401（preview/apply 各一）
# ────────────────────────────────────────────────────────────────────────────
def test_t22_no_token_401(db, client):
    ghost = "03" * 13
    pid = "01" * 13
    r1 = client.post(
        f"/api/studio/projects/{pid}/fragments/merge",
        json={
            "fragment_ids": [ghost, "04" * 13],
            "command_id": _cmd(),
        },
    )
    assert r1.status_code == 401
    assert r1.json()["error"]["code"] == "unauthenticated"
    r2 = client.post(
        f"/api/studio/projects/{pid}/fragments/merge/apply",
        json={
            "preview_id": ghost,
            "command_id": _cmd(),
            "expected_revision_a": 1,
            "expected_revision_b": 1,
            "expected_range_set_revision": 1,
        },
    )
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "unauthenticated"


# ────────────────────────────────────────────────────────────────────────────
# T23 404 不泄漏：preview 侧 fragment 不存在（26 字符字面量）/跨项目 → 404
#     kind=fragment；apply 侧 preview 不存在/跨项目 → 404 kind=preview
# ────────────────────────────────────────────────────────────────────────────
def test_t23_unknown_and_cross_project_404(db, client):
    ta = _register(client, "f10_t23_alice")
    tc = _register(client, "f10_t23_carol")
    pa = _create_project(client, ta, "T23A")
    pc = _create_project(client, tc, "T23C")
    rev_a = _import_active(client, ta, pa, TEXT_A)
    rev_c = _import_active(client, tc, pc, TEXT_A)
    # pa 两个相接片段（merge preview/apply 路径用）
    ra1 = _create_fragment(client, ta, pa, rev_a["id"], 0, 10, "T23a1")
    assert ra1.status_code == 201, ra1.text
    fid_a1 = ra1.json()["object_ref"]["id"]
    ra2 = _create_fragment(client, ta, pa, rev_a["id"], 10, 20, "T23a2", expected=2)
    assert ra2.status_code == 201, ra2.text
    fid_a2 = ra2.json()["object_ref"]["id"]
    cas_a = _range_sets(pa, rev_a["id"])[0].cas_revision
    # pc 一个片段（跨项目用）
    rc1 = _create_fragment(client, tc, pc, rev_c["id"], 0, 10, "T23c1")
    assert rc1.status_code == 201, rc1.text
    fid_c1 = rc1.json()["object_ref"]["id"]

    ghost = "03" * 13  # 26 字符
    # preview：fragment_ids[0] 不存在 → 404 kind=fragment
    r = _merge_preview(client, ta, pa, [ghost, fid_a1])
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": ghost}
    # preview：跨项目 fid（第二元素）→ 404 kind=fragment（不泄漏存在性）
    r = _merge_preview(client, ta, pa, [fid_a1, fid_c1])
    assert r.status_code == 404, r.text
    assert r.json()["error"]["details"] == {"kind": "fragment", "id": fid_c1}

    # apply：preview 不存在 → 404 kind=preview
    r = _merge_apply(
        client, ta, pa, ghost,
        expected_revision_a=1, expected_revision_b=1,
        expected_range_set_revision=cas_a,
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "preview", "id": ghost}
    # apply：跨项目 preview → 404 kind=preview（不泄漏存在性）
    rp = _merge_preview(client, ta, pa, [fid_a1, fid_a2])
    assert rp.status_code == 200, rp.text
    r = client.post(
        f"/api/studio/projects/{pc}/fragments/merge/apply",
        json={
            "preview_id": rp.json()["preview_id"],
            "command_id": _cmd(),
            "expected_revision_a": 1,
            "expected_revision_b": 1,
            "expected_range_set_revision": 1,
        },
        headers=_auth(tc),
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["details"] == {"kind": "preview", "id": rp.json()["preview_id"]}


# ────────────────────────────────────────────────────────────────────────────
# T24 422 形状：fragment_ids 长度 1 与 3（rule=cardinality）；两 id 相同
#     （rule=duplicate）；422 短路在写事务前（预览 0 行）
# ────────────────────────────────────────────────────────────────────────────
def test_t24_shape_422(db, client):
    token = _register(client, "f10_t24_bob")
    pid = _create_project(client, token, "T24Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    ra = _create_fragment(client, token, pid, rev["id"], 0, 10, "T24a")
    assert ra.status_code == 201, ra.text
    fid_a = ra.json()["object_ref"]["id"]
    rb = _create_fragment(client, token, pid, rev["id"], 10, 20, "T24b", expected=2)
    assert rb.status_code == 201, rb.text
    fid_b = rb.json()["object_ref"]["id"]

    # 长度 1 → cardinality
    r = _merge_preview(client, token, pid, [fid_a])
    assert r.status_code == 422, r.text
    v = r.json()["error"]["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("fragment_ids", "cardinality")
    assert v["message"] == "fragment_ids 必须恰好包含两个片段 ID。"
    # 长度 3 → cardinality（先于 duplicate 判定）
    r = _merge_preview(client, token, pid, [fid_a, fid_b, fid_a])
    assert r.status_code == 422, r.text
    v = r.json()["error"]["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("fragment_ids", "cardinality")
    assert v["message"] == "fragment_ids 必须恰好包含两个片段 ID。"
    # 两 id 相同 → duplicate
    r = _merge_preview(client, token, pid, [fid_a, fid_a])
    assert r.status_code == 422, r.text
    v = r.json()["error"]["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("fragment_ids", "duplicate")
    assert v["message"] == "fragment_ids 中的两个片段 ID 不能相同。"
    # 形状校验短路在写事务前：零 preview 行
    assert _count("studio_change_previews") == 0


# ────────────────────────────────────────────────────────────────────────────
# T25 非 active（直改 active 构造，同 T4/T15 模式）→ 422
#     source_revision_inactive；a 已 retired（先走 F7 退役）→ 422
#     already_retired（a→b 序首个失败即报）
# ────────────────────────────────────────────────────────────────────────────
def test_t25_inactive_and_retired_422(db, client):
    # (a) 非 active 版本（两片均绑 v1，直改 active → v2，a→b 序首个失败即报）
    token_a = _register(client, "f10_t25_alice")
    pid_a = _create_project(client, token_a, "T25a")
    v1 = _import_active(client, token_a, pid_a, TEXT_A)
    time.sleep(0.002)
    r2 = _import(client, token_a, pid_a, "second version content")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()
    fa = _create_fragment(client, token_a, pid_a, v1["id"], 0, 10, "T25a1")
    assert fa.status_code == 201, fa.text
    fid_a1 = fa.json()["object_ref"]["id"]
    fb = _create_fragment(client, token_a, pid_a, v1["id"], 10, 20, "T25a2", expected=2)
    assert fb.status_code == 201, fb.text
    fid_a2 = fb.json()["object_ref"]["id"]
    # 直改 active 指针 → v2（S4/S5 未放行，测试库专用构造）
    with Session() as s:
        s.execute(
            update(StudioProject)
            .where(StudioProject.id == pid_a)
            .values(active_source_revision_id=v2["id"])
        )
        s.commit()
    r = _merge_preview(client, token_a, pid_a, [fid_a1, fid_a2])
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    assert err["details"]["blocked_by"] == {"kind": "source", "id": v2["id"], "revision": None}

    # (b) a 已 retired（先走 F7 退役）→ 422 already_retired
    token_b = _register(client, "f10_t25_bob")
    pid_b = _create_project(client, token_b, "T25b")
    rev_b = _import_active(client, token_b, pid_b, TEXT_A)
    f1 = _create_fragment(client, token_b, pid_b, rev_b["id"], 0, 10, "T25b1")
    assert f1.status_code == 201, f1.text
    fid_b1 = f1.json()["object_ref"]["id"]
    f2 = _create_fragment(client, token_b, pid_b, rev_b["id"], 10, 20, "T25b2", expected=2)
    assert f2.status_code == 201, f2.text
    fid_b2 = f2.json()["object_ref"]["id"]
    cas_b = _range_sets(pid_b, rev_b["id"])[0].cas_revision
    # F7 退役 a：preview → apply（退役不动 cas）
    rp = client.post(
        f"/api/studio/projects/{pid_b}/fragments/{fid_b1}/retire",
        json={"target_fragment_id": fid_b1, "command_id": _cmd()},
        headers=_auth(token_b),
    )
    assert rp.status_code == 200, rp.text
    ra = client.post(
        f"/api/studio/projects/{pid_b}/fragments/{fid_b1}/retire/apply",
        json={
            "preview_id": rp.json()["preview_id"],
            "command_id": _cmd(),
            "expected_revision": 1,
            "expected_range_set_revision": cas_b,
        },
        headers=_auth(token_b),
    )
    assert ra.status_code == 200, ra.text
    assert ra.json()["state"] == "retired"
    # merge preview：a 已 retired → 422 already_retired（a→b 序首个失败即报）
    r = _merge_preview(client, token_b, pid_b, [fid_b1, fid_b2])
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "already_retired"
    assert err["details"]["blocked_by"] == {"kind": "fragment", "id": fid_b1, "revision": 1}


# ────────────────────────────────────────────────────────────────────────────
# T26 不相接（{0,5} 与 {8,12} 有间隙）→ 422 precondition_failed
#     reason=not_contiguous，message 含两范围数值（按请求序报告）；
#     blocked_by = 几何右片
# ────────────────────────────────────────────────────────────────────────────
def test_t26_not_contiguous_422(db, client):
    token = _register(client, "f10_t26_carol")
    pid = _create_project(client, token, "T26Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    fa = _create_fragment(client, token, pid, rev["id"], 0, 5, "T26a")
    assert fa.status_code == 201, fa.text
    fid_a = fa.json()["object_ref"]["id"]
    fb = _create_fragment(client, token, pid, rev["id"], 8, 12, "T26b", expected=2)
    assert fb.status_code == 201, fb.text
    fid_b = fb.json()["object_ref"]["id"]

    r = _merge_preview(client, token, pid, [fid_a, fid_b])
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "not_contiguous"
    assert err["details"]["blocked_by"] == {"kind": "fragment", "id": fid_b, "revision": 1}
    msg = err["message"]
    assert "[0, 5)" in msg and "[8, 12)" in msg
    assert msg.index("[0, 5)") < msg.index("[8, 12)")  # 请求序 [a, b] 报告

    # 子例：请求序 [b, a] → message 两片范围顺序按请求序（[8, 12) 在前）
    r = _merge_preview(client, token, pid, [fid_b, fid_a])
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["details"]["reason"] == "not_contiguous"
    assert err["details"]["blocked_by"] == {"kind": "fragment", "id": fid_b, "revision": 1}
    msg = err["message"]
    assert "[0, 5)" in msg and "[8, 12)" in msg
    assert msg.index("[8, 12)") < msg.index("[0, 5)")  # 请求序 [b, a] 报告
    assert _count("studio_change_previews") == 0


# ────────────────────────────────────────────────────────────────────────────
# T27 有效合并全程：a={0,10}、b={10,20} @ TEXT_A，merged_name 缺省 → preview
#     200 恰 4 字段（kind=fragment_merge；baseline fragment_a/fragment_b/
#     range_set 精确；impact 三键空列表）；apply 200 冻结形状恰 2 字段 + DB
#     全量断言；子例：请求序 [b,a] 亦可合并且 predecessors 保持请求序、
#     merged range 不变
# ────────────────────────────────────────────────────────────────────────────
def test_t27_valid_merge_full(db, client):
    token = _register(client, "f10_t27_dave")
    pid = _create_project(client, token, "T27Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    fa = _create_fragment(client, token, pid, rev["id"], 0, 10, "T27L")
    assert fa.status_code == 201, fa.text
    fid_a = fa.json()["object_ref"]["id"]
    fb = _create_fragment(client, token, pid, rev["id"], 10, 20, "T27R", expected=2)
    assert fb.status_code == 201, fb.text
    fid_b = fb.json()["object_ref"]["id"]
    sets = _range_sets(pid, rev["id"])
    assert len(sets) == 1
    set_id = sets[0].id
    cas0 = sets[0].cas_revision

    # preview：200 恰 4 字段
    rp = _merge_preview(client, token, pid, [fid_a, fid_b])
    assert rp.status_code == 200, rp.text
    body = rp.json()
    assert set(body.keys()) == {"preview_id", "kind", "baseline", "impact"}
    assert body["kind"] == "fragment_merge"
    assert body["baseline"] == {
        "fragment_a": {"kind": "fragment", "id": fid_a, "revision": 1},
        "fragment_b": {"kind": "fragment", "id": fid_b, "revision": 1},
        "range_set": {"id": set_id, "cas": cas0},
    }
    assert body["impact"] == {"affected": [], "needs_review": [], "preservable": []}
    preview_id = body["preview_id"]
    # preview 行 payload：fragment_ids 请求序 + 几何左/右 + 派生名（全角＋）
    pv = _preview_row(preview_id)
    assert pv["kind"] == "fragment_merge"
    assert pv["state"] == "pending"
    assert json.loads(pv["payload"]) == {
        "fragment_ids": [fid_a, fid_b],
        "left_id": fid_a,
        "right_id": fid_b,
        "merged_name": "T27L＋T27R",
    }

    # apply：200 冻结形状恰 2 字段
    ra = _merge_apply(
        client, token, pid, preview_id,
        expected_revision_a=1, expected_revision_b=1,
        expected_range_set_revision=cas0,
    )
    assert ra.status_code == 200, ra.text
    ab = ra.json()
    assert set(ab.keys()) == {"merged", "predecessors"}
    merged = ab["merged"]
    merged_id = merged["object_ref"]["id"]
    assert merged["object_ref"] == {"kind": "fragment", "id": merged_id, "revision": 1}
    assert merged["range"] == {"start": 0, "end": 20}
    assert merged["name"] == "T27L＋T27R"
    assert merged_id != fid_a and merged_id != fid_b
    assert ab["predecessors"] == [
        {"object_ref": {"kind": "fragment", "id": fid_a, "revision": 1}, "state": "retired"},
        {"object_ref": {"kind": "fragment", "id": fid_b, "revision": 1}, "state": "retired"},
    ]

    # DB：a、b 均 retired（retired_at 非空）且各自 revision 行数不变（仍 1）
    with Session() as s:
        fa_now = s.get(Fragment, fid_a)
        fb_now = s.get(Fragment, fid_b)
    assert fa_now.state == "retired" and fa_now.retired_at is not None
    assert fb_now.state == "retired" and fb_now.retired_at is not None
    assert len(_fragment_revisions(fid_a)) == 1
    assert len(_fragment_revisions(fid_b)) == 1

    # merged 片段：state=candidate、恰 1 revision 行（revision=1、reason=merge、
    # predecessor=[a,b] 请求序、range [0,20)、source_revision_id 同原）
    with Session() as s:
        mf = s.get(Fragment, merged_id)
    assert mf is not None
    assert mf.project_id == pid
    assert mf.source_revision_id == rev["id"]
    assert mf.range_set_id == set_id
    assert mf.name == "T27L＋T27R"
    assert mf.summary is None
    assert mf.state == "candidate"
    assert mf.retired_at is None
    revs = _fragment_revisions(merged_id)
    assert len(revs) == 1
    mr = revs[0]
    assert mr.revision == 1
    assert mr.reason == "merge"
    assert mr.range_start == 0 and mr.range_end == 20
    assert mr.source_revision_id == rev["id"]
    assert json.loads(mr.predecessor_fragment_ids) == [fid_a, fid_b]  # 请求序
    assert mf.current_revision_id == mr.id

    # cas == 原 + 1
    assert _range_sets(pid, rev["id"])[0].cas_revision == cas0 + 1

    # ReviewDecision 恰 1 行：target_ref==merged object_ref、digest 独立重算相等
    decisions = _decision_rows()
    assert len(decisions) == 1
    d = decisions[0]
    assert d.decision == "applied"
    assert d.preview_id == preview_id
    assert json.loads(d.target_ref) == {"kind": "fragment", "id": merged_id, "revision": 1}
    assert d.baseline_digest == hashlib.sha256(
        json.dumps(
            {
                "fragment_a": {"kind": "fragment", "id": fid_a, "revision": 1},
                "fragment_b": {"kind": "fragment", "id": fid_b, "revision": 1},
                "range_set": {"id": set_id, "cas": cas0},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    # preview applied
    assert _preview_row(preview_id)["state"] == "applied"

    # 子例：请求序 [b, a] 亦可合并；predecessors 保持请求序、merged range 不变
    token2 = _register(client, "f10_t27_eve")
    pid2 = _create_project(client, token2, "T27Proj2")
    rev2 = _import_active(client, token2, pid2, TEXT_A)
    g1 = _create_fragment(client, token2, pid2, rev2["id"], 0, 10, "T27m")
    assert g1.status_code == 201, g1.text
    gid_a = g1.json()["object_ref"]["id"]
    g2 = _create_fragment(client, token2, pid2, rev2["id"], 10, 20, "T27n", expected=2)
    assert g2.status_code == 201, g2.text
    gid_b = g2.json()["object_ref"]["id"]
    cas2 = _range_sets(pid2, rev2["id"])[0].cas_revision
    rp2 = _merge_preview(client, token2, pid2, [gid_b, gid_a])  # 请求序 [b, a]
    assert rp2.status_code == 200, rp2.text
    assert json.loads(_preview_row(rp2.json()["preview_id"])["payload"])["fragment_ids"] == [gid_b, gid_a]
    ra2 = _merge_apply(
        client, token2, pid2, rp2.json()["preview_id"],
        expected_revision_a=1, expected_revision_b=1,
        expected_range_set_revision=cas2,
    )
    assert ra2.status_code == 200, ra2.text
    ab2 = ra2.json()
    assert ab2["merged"]["range"] == {"start": 0, "end": 20}  # merged range 不变
    assert ab2["merged"]["name"] == "T27m＋T27n"  # 派生按几何左→右
    assert [p["object_ref"]["id"] for p in ab2["predecessors"]] == [gid_b, gid_a]  # 请求序
    revs2 = _fragment_revisions(ab2["merged"]["object_ref"]["id"])
    assert json.loads(revs2[0].predecessor_fragment_ids) == [gid_b, gid_a]  # 请求序


# ────────────────────────────────────────────────────────────────────────────
# T28 命名：给定 merged_name="  M1  " → trim 落库；缺省派生 "左＋右"（全角
#     ＋，T27 已全量断言）；派生超长（左 name 60 字符 + 右 name 61 字符 →
#     122>120）→ 422 max_length；全空白名 → 422 required
# ────────────────────────────────────────────────────────────────────────────
def test_t28_naming_trim_derive_max_length(db, client):
    # (a) 给定 merged_name="  M1  " → trim 落库
    token_a = _register(client, "f10_t28_alice")
    pid_a = _create_project(client, token_a, "T28a")
    rev_a = _import_active(client, token_a, pid_a, TEXT_A)
    fa = _create_fragment(client, token_a, pid_a, rev_a["id"], 0, 10, "T28aL")
    assert fa.status_code == 201, fa.text
    fid_a1 = fa.json()["object_ref"]["id"]
    fb = _create_fragment(client, token_a, pid_a, rev_a["id"], 10, 20, "T28aR", expected=2)
    assert fb.status_code == 201, fb.text
    fid_a2 = fb.json()["object_ref"]["id"]
    cas_a = _range_sets(pid_a, rev_a["id"])[0].cas_revision
    rp = _merge_preview(client, token_a, pid_a, [fid_a1, fid_a2], merged_name="  M1  ")
    assert rp.status_code == 200, rp.text
    assert json.loads(_preview_row(rp.json()["preview_id"])["payload"])["merged_name"] == "M1"
    ra = _merge_apply(
        client, token_a, pid_a, rp.json()["preview_id"],
        expected_revision_a=1, expected_revision_b=1,
        expected_range_set_revision=cas_a,
    )
    assert ra.status_code == 200, ra.text
    assert ra.json()["merged"]["name"] == "M1"
    with Session() as s:
        mf = s.get(Fragment, ra.json()["merged"]["object_ref"]["id"])
    assert mf.name == "M1"  # trim 落库

    # (b) 派生超长：左 name 60 字符 + 右 name 61 字符 → 60+1+61=122 > 120
    token_b = _register(client, "f10_t28_bob")
    pid_b = _create_project(client, token_b, "T28b")
    rev_b = _import_active(client, token_b, pid_b, TEXT_A)
    fc = _create_fragment(client, token_b, pid_b, rev_b["id"], 0, 10, "x" * 60)
    assert fc.status_code == 201, fc.text
    fid_b1 = fc.json()["object_ref"]["id"]
    fd = _create_fragment(client, token_b, pid_b, rev_b["id"], 10, 20, "y" * 61, expected=2)
    assert fd.status_code == 201, fd.text
    fid_b2 = fd.json()["object_ref"]["id"]
    r = _merge_preview(client, token_b, pid_b, [fid_b1, fid_b2])  # 缺省派生
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("merged_name", "max_length")
    assert "122" in v["message"]

    # (c) 全空白名 → 422 required
    r = _merge_preview(client, token_b, pid_b, [fid_b1, fid_b2], merged_name="   ")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert (v["field"], v["rule"]) == ("merged_name", "required")
    # 命名校验短路在写事务前：仅 (a) 成功 preview 的 1 行，(b)/(c) 零写入
    assert _count("studio_change_previews") == 1


# ────────────────────────────────────────────────────────────────────────────
# T29 CAS 冲突无半次：preview 后 F1 建新片段（cas+1）→ apply（expected 用
#     刷新后 cas，使失配落在 baseline 步）→ 409 preview_stale conflicts 恰 1
#     （range_set）+ preview superseded；DB 零变化
# ────────────────────────────────────────────────────────────────────────────
def test_t29_cas_conflict_no_half_save(db, client):
    token = _register(client, "f10_t29_frank")
    pid = _create_project(client, token, "T29Proj")
    rev = _import_active(client, token, pid, TEXT_E)  # 40 字符
    fa = _create_fragment(client, token, pid, rev["id"], 0, 10, "T29a")
    assert fa.status_code == 201, fa.text
    fid_a = fa.json()["object_ref"]["id"]
    fb = _create_fragment(client, token, pid, rev["id"], 10, 20, "T29b", expected=2)
    assert fb.status_code == 201, fb.text
    fid_b = fb.json()["object_ref"]["id"]
    fc = _create_fragment(client, token, pid, rev["id"], 20, 30, "T29c", expected=3)
    assert fc.status_code == 201, fc.text
    fid_c = fc.json()["object_ref"]["id"]
    cas0 = _range_sets(pid, rev["id"])[0].cas_revision

    # preview（merged {0,20} 与 c {20,30} 相接，不冲突）
    rp = _merge_preview(client, token, pid, [fid_a, fid_b])
    assert rp.status_code == 200, rp.text
    preview_id = rp.json()["preview_id"]

    # preview 后另一 F1 建新片段 → cas +1
    rd = _create_fragment(client, token, pid, rev["id"], 30, 40, "T29d", expected=cas0)
    assert rd.status_code == 201, rd.text
    cas1 = _range_sets(pid, rev["id"])[0].cas_revision
    assert cas1 == cas0 + 1

    # 快照 apply 前 DB 状态（用于验证零变化）
    dec_before = _count("studio_review_decisions")
    cmds_before = _count("studio_command_records")

    # apply：expected 用刷新后 cas（CAS 预检 step 6 通过，失配落在 baseline step 7）
    ra = _merge_apply(
        client, token, pid, preview_id,
        expected_revision_a=1, expected_revision_b=1,
        expected_range_set_revision=cas1,
    )
    assert ra.status_code == 409, ra.text
    err = ra.json()["error"]
    assert err["code"] == "preview_stale"
    assert err["details"]["preview_id"] == preview_id
    conflicts = err["details"]["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0] == {
        "object": {"kind": "range_set", "id": _range_sets(pid, rev["id"])[0].id},
        "expected": cas0,
        "actual": cas1,
    }
    # 预览置 superseded
    assert _preview_row(preview_id)["state"] == "superseded"

    # DB 零变化：a、b 仍 candidate、无 merged 行、cas 不再变、无决定、无 record
    with Session() as s:
        fa_now = s.get(Fragment, fid_a)
        fb_now = s.get(Fragment, fid_b)
        fc_now = s.get(Fragment, fid_c)
    assert fa_now.state == "candidate" and fa_now.retired_at is None
    assert fb_now.state == "candidate" and fb_now.retired_at is None
    assert fc_now.state == "candidate"
    assert len(_fragments(pid)) == 4  # a、b、c、d，无合并产物
    assert len(_fragment_revisions(fid_a)) == 1
    assert len(_fragment_revisions(fid_b)) == 1
    assert _range_sets(pid, rev["id"])[0].cas_revision == cas1  # 未再 +1
    assert _count("studio_review_decisions") == dec_before
    assert _count("studio_command_records") == cmds_before  # 失败的 apply 不写 record


# ────────────────────────────────────────────────────────────────────────────
# T30 R11 旧引用不迁移 + 下游待复核：种子 SourceRelation（指 a、rev1）+
#     pending Job（payload 含 a id）（复用 _seed_impact 模式）→ preview
#     impact：affected 恰 1、needs_review 恰 1（并集去重，b 无引用不重复计）；
#     apply 后 SourceRelation 逐字段不变（仍指 a）、job payload 不变
# ────────────────────────────────────────────────────────────────────────────
def test_t30_old_refs_not_migrated(db, client):
    token = _register(client, "f10_t30_gina")
    uid = _user_id("f10_t30_gina")
    pid = _create_project(client, token, "T30Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    fa = _create_fragment(client, token, pid, rev["id"], 0, 10, "T30a")
    assert fa.status_code == 201, fa.text
    fid_a = fa.json()["object_ref"]["id"]
    fb = _create_fragment(client, token, pid, rev["id"], 10, 20, "T30b", expected=2)
    assert fb.status_code == 201, fb.text
    fid_b = fb.json()["object_ref"]["id"]
    cas0 = _range_sets(pid, rev["id"])[0].cas_revision
    seeds = _seed_impact(pid, uid, fid_a)  # 仅指 a（rev1）+ payload 含 a id 的 pending job

    # preview：impact = a、b 并集（按 (kind,id) 去重）；affected 恰 1
    # （source_relation rev1）、needs_review 恰 1（job）——b 无引用不重复计
    rp = _merge_preview(client, token, pid, [fid_a, fid_b])
    assert rp.status_code == 200, rp.text
    impact = rp.json()["impact"]
    assert impact["affected"] == [
        {"object_ref": {"kind": "source_relation", "id": seeds["relation_id"], "revision": 1}}
    ]
    assert impact["needs_review"] == [
        {
            "object_ref": {"kind": "job", "id": seeds["job_id"], "revision": None},
            "reason": "frozen_input_contains_fragment",
        }
    ]
    assert impact["preservable"] == []

    # apply 前快照
    rel_before = _source_relation_row(seeds["relation_id"])
    job_before = _job_row(seeds["job_id"])
    assert rel_before is not None and job_before is not None

    ra = _merge_apply(
        client, token, pid, rp.json()["preview_id"],
        expected_revision_a=1, expected_revision_b=1,
        expected_range_set_revision=cas0,
    )
    assert ra.status_code == 200, ra.text

    # apply 后：SourceRelation 逐字段不变（仍指 a——a 已 retired，引用不迁移）、
    # job payload 不变
    rel_after = _source_relation_row(seeds["relation_id"])
    job_after = _job_row(seeds["job_id"])
    assert rel_after == rel_before
    assert job_after == job_before
    assert rel_after[2] == fid_a  # source_fragment_id 列不变
    assert rel_after[3] == 1  # source_fragment_revision 仍 1


# ────────────────────────────────────────────────────────────────────────────
# T31 重放零副作用：preview 同 command_id+同 body → 200 原体、预览恰 1 行；
#     apply 同 command_id → 200 原体、merged 恰 1 行、决定恰 1 行、无重复创建
# ────────────────────────────────────────────────────────────────────────────
def test_t31_replay_zero_side_effects(db, client):
    token = _register(client, "f10_t31_hank")
    pid = _create_project(client, token, "T31Proj")
    rev = _import_active(client, token, pid, TEXT_A)
    fa = _create_fragment(client, token, pid, rev["id"], 0, 10, "T31a")
    assert fa.status_code == 201, fa.text
    fid_a = fa.json()["object_ref"]["id"]
    fb = _create_fragment(client, token, pid, rev["id"], 10, 20, "T31b", expected=2)
    assert fb.status_code == 201, fb.text
    fid_b = fb.json()["object_ref"]["id"]
    cas0 = _range_sets(pid, rev["id"])[0].cas_revision

    # (a) preview 幂等：同 command_id + 同 body → 200 原 body，预览恰 1 行
    c1 = _cmd()
    r1 = _merge_preview(client, token, pid, [fid_a, fid_b], command_id=c1)
    assert r1.status_code == 200, r1.text
    r2 = _merge_preview(client, token, pid, [fid_a, fid_b], command_id=c1)
    assert r2.status_code == 200, r2.text  # 重放（非新建）
    assert r2.json() == r1.json()
    assert r2.json()["preview_id"] == r1.json()["preview_id"]
    assert _count("studio_change_previews") == 1  # 零副作用：不产生第二行

    # (b) apply 幂等：新 command_id 正常 apply → 再同 command_id → 200 原 body，
    #     merged 恰 1 行、决定恰 1 行、无重复创建
    c2 = _cmd()
    a1 = _merge_apply(
        client, token, pid, r1.json()["preview_id"],
        command_id=c2, expected_revision_a=1, expected_revision_b=1,
        expected_range_set_revision=cas0,
    )
    assert a1.status_code == 200, a1.text
    a2 = _merge_apply(
        client, token, pid, r1.json()["preview_id"],
        command_id=c2, expected_revision_a=1, expected_revision_b=1,
        expected_range_set_revision=cas0,
    )
    assert a2.status_code == 200, a2.text  # 重放
    assert a2.json() == a1.json()

    merged_id = a1.json()["merged"]["object_ref"]["id"]
    assert len(_fragments(pid)) == 3  # a、b + merged，无重复创建
    assert len(_fragment_revisions(merged_id)) == 1
    assert len(_fragment_revisions(fid_a)) == 1
    assert len(_fragment_revisions(fid_b)) == 1
    assert len(_decision_rows()) == 1  # 零副作用
    assert _count("studio_change_previews") == 1
