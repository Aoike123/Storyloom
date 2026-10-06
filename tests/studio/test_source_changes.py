"""S4 正文变更影响预览（附录 01 §2 S4 + 00 §5；kind=source_activate）——
POST /api/studio/projects/{pid}/source-changes/preview 行为验证。

SOURCE-09 实现并验收 S4 preview（S5 apply 属 SOURCE-10，不在本文件范围）；
正文更新只支持整篇重导入 + 激活预览切换，不提供局部文本 diff 编辑命令
（decisions.md 2026-10-04），impact 一律按整篇语义计算。

fixture/helper 复用 tests/studio 既有模式（db fixture、_register/_auth/_cmd/
_create_project/_import/_import_active/_create_fragment/_count/_preview_row/
_user_id、直改 active 指针构造非 active 版本片段照 test_fragment_changes T4
模式、跨域种子直插照 test_fragment_changes._seed_impact 模式；跨域模型经
tests/studio/conftest.py 统一注册）；本文件不写 STUDIO_* env（session 级由
conftest 统一设定，engine 是进程单例）。
"""
import hashlib
import json
import time
import uuid

import pytest
from sqlalchemy import text, update

from backend.core.db import Base, Session, engine, make_ulid
from backend.studio.projects.models import StudioProject
from backend.studio.reviews.models import ChangePreview

# 基准正文：50 字符，无空白（rev1 片段 {0,10}/{10,20}/{30,40} 自足）
TEXT = "01234567890123456789012345678901234567890123456789"
# rev2 用正文（20 字符，片段 {0,10} 自足）
TEXT2 = "abcdefghijklmnopqrst"
TEXT3 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
# 四种整篇变形（T7：插字/删选区/移段/重复字串，均派生自 TEXT）
DIFF_INSERT = TEXT[:20] + "INSERTED" + TEXT[20:]
DIFF_DELETE = TEXT[:10] + TEXT[30:]
DIFF_MOVE = TEXT[30:40] + TEXT[:30] + TEXT[40:]
DIFF_DUP = TEXT + TEXT[:20]


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


def _preview(client, token, pid, target, kind="source_activate", command_id=None):
    """S4 preview 请求helper：POST /projects/{pid}/source-changes/preview。"""
    return client.post(
        f"/api/studio/projects/{pid}/source-changes/preview",
        json={
            "kind": kind,
            "target_revision_id": target,
            "command_id": command_id or _cmd(),
        },
        headers=_auth(token),
    )


def _count(table) -> int:
    """按表名计数（表名为冻结常量，无注入面）。"""
    with Session() as s:
        return s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def _table_rows(table) -> list:
    """全表行转储（列名→值 dict 列表，按 id 排序；表名为冻结常量，无注入面）。"""
    with Session() as s:
        rows = s.execute(text(f"SELECT * FROM {table}")).mappings().all()
    return sorted((dict(r) for r in rows), key=lambda r: r["id"])


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
            "impact": p.impact,
        }


def _user_id(username) -> str:
    with Session() as s:
        return (
            s.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username})
            .scalars()
            .first()
        )


def _set_active(pid, revision_id):
    """直改 active 指针（S5 未放行，测试库专用构造——同 test_fragment_changes T4 模式）。"""
    with Session() as s:
        s.execute(
            update(StudioProject)
            .where(StudioProject.id == pid)
            .values(active_source_revision_id=revision_id)
        )
        s.commit()


def _seed_rev1_three_fragments(client, token, pid, rev1_id) -> list:
    """rev1（active）上建 3 片段 {0,10}/{10,20}/{30,40}，返回 fid 列表（创建序）。"""
    fids = []
    for i, (s0, e0) in enumerate(((0, 10), (10, 20), (30, 40))):
        r = _create_fragment(
            client, token, pid, rev1_id, s0, e0, f"F{i}",
            expected=None if i == 0 else i + 1,
        )
        assert r.status_code == 201, r.text
        fids.append(r.json()["object_ref"]["id"])
    return fids


def _scenario_two_revisions(client, token, pid, rev1_text=TEXT, rev2_text=TEXT2, rev2_ranges=((0, 10),)):
    """rev1（active）3 片段 → 导入 rev2（非 active）→ 直改 active 到 rev2 建其片段
    → 直改回 rev1。返回 (rev1, rev2, rev1_fids, rev2_fids)。"""
    rev1 = _import_active(client, token, pid, rev1_text)
    fids1 = _seed_rev1_three_fragments(client, token, pid, rev1["id"])
    time.sleep(0.002)  # 保证 ULID 严格递增
    rev2 = _import(client, token, pid, rev2_text).json()
    fids2 = []
    if rev2_ranges:
        _set_active(pid, rev2["id"])
        for i, (s0, e0) in enumerate(rev2_ranges):
            r = _create_fragment(
                client, token, pid, rev2["id"], s0, e0, f"G{i}",
                expected=None if i == 0 else i + 1,
            )
            assert r.status_code == 201, r.text
            fids2.append(r.json()["object_ref"]["id"])
        _set_active(pid, rev1["id"])
    return rev1, rev2, fids1, fids2


def _seed_jobs_media(pid, uid, fid) -> dict:
    """T8 跨域种子（直插，照 test_fragment_changes._seed_impact 已接受模式）：
    pending job（payload 冻结输入含 fid）+ succeeded job（payload 含 fid）+
    MediaArtifact(source_job_id=succeeded)。返回各 id。"""
    now = time.time()
    pending_job_id = make_ulid()
    succeeded_job_id = make_ulid()
    artifact_id = make_ulid()
    with Session() as s:
        for jid, status in ((pending_job_id, "pending"), (succeeded_job_id, "succeeded")):
            s.execute(
                text(
                    "INSERT INTO studio_jobs (id, project_id, owner_id, kind, ref_kind, "
                    "ref_id, payload, status, priority, created_at, submitted_by, "
                    "attempts, usage, last_error, finished_at) "
                    "VALUES (:id, :pid, :uid, 'shot_generate', NULL, NULL, :payload, "
                    ":status, 0, :now, :uid, 0, NULL, NULL, NULL)"
                ),
                {
                    "id": jid,
                    "pid": pid,
                    "uid": uid,
                    "payload": json.dumps({"fragment_id": fid, "range": {"start": 0, "end": 10}}),
                    "status": status,
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
            {"id": artifact_id, "pid": pid, "sha": "cd" * 32, "jid": succeeded_job_id, "now": now},
        )
        s.commit()
    return {
        "pending_job_id": pending_job_id,
        "succeeded_job_id": succeeded_job_id,
        "artifact_id": artifact_id,
    }


# ────────────────────────────────────────────────────────────────────────────
# T1 无 token → 401
# ────────────────────────────────────────────────────────────────────────────
def test_t1_no_token_401(db, client):
    pid = "01" * 13
    r = client.post(
        f"/api/studio/projects/{pid}/source-changes/preview",
        json={
            "kind": "source_activate",
            "target_revision_id": "02" * 13,
            "command_id": _cmd(),
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


# ────────────────────────────────────────────────────────────────────────────
# T2 项目不存在/跨属主 → 404 同形状不泄漏（kind=project，id 回显请求 pid）
# ────────────────────────────────────────────────────────────────────────────
def test_t2_project_404_same_shape(db, client):
    ta = _register(client, "s4_t2_alice")
    tb = _register(client, "s4_t2_bob")
    pb = _create_project(client, tb, "T2B")
    rev_b = _import_active(client, tb, pb, TEXT)

    ghost = "01" * 13
    r1 = _preview(client, ta, ghost, rev_b["id"])
    assert r1.status_code == 404, r1.text
    e1 = r1.json()["error"]
    assert e1["code"] == "not_found"
    assert e1["details"] == {"kind": "project", "id": ghost}

    # 跨属主（他账号真实存在的项目）→ 同形状，不泄漏项目存在性
    r2 = _preview(client, ta, pb, rev_b["id"])
    assert r2.status_code == 404, r2.text
    e2 = r2.json()["error"]
    assert e2["code"] == "not_found"
    assert e2["details"] == {"kind": "project", "id": pb}
    assert e2["message"] == e1["message"]


# ────────────────────────────────────────────────────────────────────────────
# T3 kind 非法（"source_edit"）→ 422 validation_failed(field=kind, rule=invalid)
# ────────────────────────────────────────────────────────────────────────────
def test_t3_kind_invalid_422(db, client):
    token = _register(client, "s4_t3_carol")
    pid = _create_project(client, token, "T3Proj")
    rev1 = _import_active(client, token, pid, TEXT)
    time.sleep(0.002)
    rev2 = _import(client, token, pid, TEXT2).json()

    r = _preview(client, token, pid, rev2["id"], kind="source_edit")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "kind"
    assert v["rule"] == "invalid"
    assert v["message"] == "kind 仅支持 source_activate。"
    # 校验短路：零副作用
    assert _count("studio_change_previews") == 0


# ────────────────────────────────────────────────────────────────────────────
# T4 target 不存在（26 字符字面量）/跨项目（他项目版本 id）→ 422 not_found，
#    两路同形状（不泄漏）
# ────────────────────────────────────────────────────────────────────────────
def test_t4_target_not_found_422_same_shape(db, client):
    ta = _register(client, "s4_t4_alice")
    tb = _register(client, "s4_t4_bob")
    pa = _create_project(client, ta, "T4A")
    pb = _create_project(client, tb, "T4B")
    rev_a = _import_active(client, ta, pa, TEXT)
    rev_b = _import_active(client, tb, pb, TEXT)

    ghost = "03" * 13  # 26 字符字面量
    r1 = _preview(client, ta, pa, ghost)
    assert r1.status_code == 422, r1.text
    err1 = r1.json()["error"]
    assert err1["code"] == "validation_failed"
    v1 = err1["details"]["violations"][0]
    assert v1["field"] == "target_revision_id"
    assert v1["rule"] == "not_found"
    assert v1["message"] == "指定正文版本不存在。"

    # 跨项目（他项目真实版本 id）→ 完全同形状（violations 不回显 id，不泄漏）
    r2 = _preview(client, ta, pa, rev_b["id"])
    assert r2.status_code == 422, r2.text
    assert r2.json() == r1.json()
    assert _count("studio_change_previews") == 0


# ────────────────────────────────────────────────────────────────────────────
# T5 target 已是 active → 422 precondition_failed reason=already_active，
#    blocked_by 精确
# ────────────────────────────────────────────────────────────────────────────
def test_t5_already_active_422(db, client):
    token = _register(client, "s4_t5_dana")
    pid = _create_project(client, token, "T5Proj")
    rev = _import_active(client, token, pid, TEXT)

    r = _preview(client, token, pid, rev["id"])
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "already_active"
    assert err["details"]["blocked_by"] == {
        "kind": "source_revision",
        "id": rev["id"],
        "revision": None,
    }
    assert err["message"] == "该正文版本已是当前活跃版本。"
    assert _count("studio_change_previews") == 0


# ────────────────────────────────────────────────────────────────────────────
# T6 有效 preview：rev1 上 3 片段 + rev2 上 1 片段 → 200 恰 4 字段；baseline
#    精确（hash 独立重算）；impact：affected 恰 1（rev2 片段）、needs_review
#    恰 3（rev1 片段 cross_version_mapping）、preservable 空；preview 零业务
#    副作用（仅 change_previews/command_records 各 +1，业务表逐行不变，
#    S3 读 rev1 正文/active 不变）
# ────────────────────────────────────────────────────────────────────────────
def test_t6_valid_preview_200_zero_side_effects(db, client):
    token = _register(client, "s4_t6_alice")
    pid = _create_project(client, token, "T6Proj")
    rev1, rev2, fids1, fids2 = _scenario_two_revisions(client, token, pid)

    # preview 前快照：业务表全行 + S3 读 + 计数
    tables = (
        "studio_projects",
        "studio_source_revisions",
        "studio_fragments",
        "studio_fragment_revisions",
        "studio_range_sets",
    )
    before = {t: _table_rows(t) for t in tables}
    s3_before = client.get(
        f"/api/studio/projects/{pid}/sources/{rev1['id']}", headers=_auth(token)
    )
    assert s3_before.status_code == 200, s3_before.text
    previews_before = _count("studio_change_previews")
    cmds_before = _count("studio_command_records")

    r = _preview(client, token, pid, rev2["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    # 恰 4 字段（00 §5.1 冻结形状）
    assert set(body.keys()) == {"preview_id", "kind", "baseline", "impact"}
    assert body["kind"] == "source_activate"
    # baseline 精确（target_canonical_hash 测试内独立 sha256 重算）
    assert body["baseline"] == {
        "active_revision_id": rev1["id"],
        "target_revision_id": rev2["id"],
        "target_canonical_hash": hashlib.sha256(TEXT2.encode("utf-8")).hexdigest(),
    }
    # impact：affected 恰 1（rev2 片段）；needs_review 恰 3（rev1 片段，
    # reason=cross_version_mapping）；preservable 空；三键不省略
    impact = body["impact"]
    assert set(impact.keys()) == {"affected", "needs_review", "preservable"}
    assert impact["affected"] == [
        {"object_ref": {"kind": "fragment", "id": fids2[0], "revision": 1}}
    ]
    assert impact["needs_review"] == [
        {
            "object_ref": {"kind": "fragment", "id": fid, "revision": 1},
            "reason": "cross_version_mapping",
        }
        for fid in sorted(fids1)
    ]
    assert impact["preservable"] == []

    # 零业务副作用：除 change_previews + command_records 各 +1 外，
    # 业务表行数与字段全不变
    assert _count("studio_change_previews") == previews_before + 1
    assert _count("studio_command_records") == cmds_before + 1
    for t, rows in before.items():
        assert _table_rows(t) == rows, t
    # preview 后 S3 读 rev1 正文不变、仍为 active；片段（DB 行）不变
    s3_after = client.get(
        f"/api/studio/projects/{pid}/sources/{rev1['id']}", headers=_auth(token)
    )
    assert s3_after.status_code == 200, s3_after.text
    assert s3_after.json() == s3_before.json()
    assert s3_after.json()["canonical_content"] == TEXT
    assert s3_after.json()["is_active"] is True


# ────────────────────────────────────────────────────────────────────────────
# T7 四形态 diff 不假称恢复：rev2 分别为 rev1 正文的 插字/删选区/移段/重复
#    字串（四个独立项目场景）→ 每次 preview 的 needs_review 都恰好 = rev1
#    全部非退役片段（reason=cross_version_mapping），响应任何位置不含新
#    范围/映射字段
# ────────────────────────────────────────────────────────────────────────────
def test_t7_diff_forms_never_claim_recovery(db, client):
    token = _register(client, "s4_t7_bob")
    forms = (
        ("insert", DIFF_INSERT),
        ("delete", DIFF_DELETE),
        ("move", DIFF_MOVE),
        ("dup", DIFF_DUP),
    )
    for label, rev2_text in forms:
        pid = _create_project(client, token, f"T7{label}")
        rev1 = _import_active(client, token, pid, TEXT)
        fids1 = _seed_rev1_three_fragments(client, token, pid, rev1["id"])
        time.sleep(0.002)
        rev2 = _import(client, token, pid, rev2_text).json()

        r = _preview(client, token, pid, rev2["id"])
        assert r.status_code == 200, (label, r.text)
        body = r.json()
        assert set(body.keys()) == {"preview_id", "kind", "baseline", "impact"}
        # needs_review 恰好 = rev1 全部非退役片段（整篇语义，一律 cross_version_mapping）
        assert body["impact"]["needs_review"] == [
            {
                "object_ref": {"kind": "fragment", "id": fid, "revision": 1},
                "reason": "cross_version_mapping",
            }
            for fid in sorted(fids1)
        ], label
        # rev2 上无片段、无跨域种子 → affected/preservable 空
        assert body["impact"]["affected"] == [], label
        assert body["impact"]["preservable"] == [], label
        # 不假称恢复：每条 needs_review 只有 {object_ref, reason}，
        # object_ref 只有 {kind, id, revision}——没有任何新范围字段
        for entry in body["impact"]["needs_review"]:
            assert set(entry.keys()) == {"object_ref", "reason"}, label
            assert set(entry["object_ref"].keys()) == {"kind", "id", "revision"}, label
        # 响应任何位置不含新范围/重映射字段
        for forbidden in ("range_start", "range_end", "new_range", "mapped_range", "remap"):
            assert forbidden not in r.text, (label, forbidden)


# ────────────────────────────────────────────────────────────────────────────
# T8 运行中冻结输入 + 历史保留：rev1 片段 f 种子 pending job + succeeded job
#    + MediaArtifact(source_job_id=succeeded) → preview(rev2)：needs_review
#    含 pending job 恰 1（frozen_input_contains_fragment）；preservable 含
#    media_artifact 恰 1；edit/release 表缺表降级不 500
# ────────────────────────────────────────────────────────────────────────────
def test_t8_frozen_input_and_preservable(db, client):
    token = _register(client, "s4_t8_carol")
    uid = _user_id("s4_t8_carol")
    pid = _create_project(client, token, "T8Proj")
    rev1, rev2, fids1, _ = _scenario_two_revisions(
        client, token, pid, rev2_ranges=None
    )
    fid = fids1[0]
    seeds = _seed_jobs_media(pid, uid, fid)
    # 缺表降级：drop edit/release 两表（模拟该表未交付进当前 DB）
    with Session() as s:
        s.execute(text("DROP TABLE studio_edit_instances"))
        s.execute(text("DROP TABLE studio_releases"))
        s.commit()

    r = _preview(client, token, pid, rev2["id"])
    assert r.status_code == 200, r.text  # 缺表降级，不 500
    impact = r.json()["impact"]
    assert set(impact.keys()) == {"affected", "needs_review", "preservable"}
    # needs_review：rev1 三片段 cross_version_mapping + pending job 恰 1
    frag_entries = [e for e in impact["needs_review"] if e["object_ref"]["kind"] == "fragment"]
    assert frag_entries == [
        {
            "object_ref": {"kind": "fragment", "id": f, "revision": 1},
            "reason": "cross_version_mapping",
        }
        for f in sorted(fids1)
    ]
    job_entries = [e for e in impact["needs_review"] if e["object_ref"]["kind"] == "job"]
    assert job_entries == [
        {
            "object_ref": {"kind": "job", "id": seeds["pending_job_id"], "revision": None},
            "reason": "frozen_input_contains_fragment",
        }
    ]
    # preservable：media_artifact 恰 1（source_job_id=succeeded 的证据链）
    assert impact["preservable"] == [
        {"object_ref": {"kind": "media_artifact", "id": seeds["artifact_id"], "revision": None}}
    ]
    # rev2 上无片段 → affected 空
    assert impact["affected"] == []


# ────────────────────────────────────────────────────────────────────────────
# T9 重放零副作用：同 command_id+同 target → 200 原体、preview 恰 1 行；
#    同 command_id 不同 target → 422 reused
# ────────────────────────────────────────────────────────────────────────────
def test_t9_replay_zero_side_effects(db, client):
    token = _register(client, "s4_t9_dana")
    pid = _create_project(client, token, "T9Proj")
    rev1 = _import_active(client, token, pid, TEXT)
    time.sleep(0.002)
    rev2 = _import(client, token, pid, TEXT2).json()
    time.sleep(0.002)
    rev3 = _import(client, token, pid, TEXT3).json()

    # (a) 同 command_id + 同 target → 200 原体，preview 恰 1 行
    c1 = _cmd()
    r1 = _preview(client, token, pid, rev2["id"], command_id=c1)
    assert r1.status_code == 200, r1.text
    r2 = _preview(client, token, pid, rev2["id"], command_id=c1)
    assert r2.status_code == 200, r2.text  # 重放（非新建）
    assert r2.json() == r1.json()
    assert r2.json()["preview_id"] == r1.json()["preview_id"]
    assert _count("studio_change_previews") == 1  # 零副作用：不产生第二行

    # (b) 同 command_id 不同 target → 422 validation_failed(rule=reused)
    r3 = _preview(client, token, pid, rev3["id"], command_id=c1)
    assert r3.status_code == 422, r3.text
    err = r3.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "command_id"
    assert v["rule"] == "reused"
    assert v["message"] == "command_id 已被其他命令使用，请生成新的。"
    assert _count("studio_change_previews") == 1  # 仍恰 1 行


# ────────────────────────────────────────────────────────────────────────────
# T10 preview 行 DB 断言：kind/payload/baseline/impact 落库精确、
#     state=pending、decision_id/resolved_at 为 None、TTL 窗 1700<差值<1900
# ────────────────────────────────────────────────────────────────────────────
def test_t10_preview_row_db_exact(db, client):
    token = _register(client, "s4_t10_earl")
    pid = _create_project(client, token, "T10Proj")
    rev1, rev2, fids1, fids2 = _scenario_two_revisions(client, token, pid)

    r = _preview(client, token, pid, rev2["id"])
    assert r.status_code == 200, r.text
    body = r.json()

    pv = _preview_row(body["preview_id"])
    assert pv["project_id"] == pid
    assert pv["owner_id"] == _user_id("s4_t10_earl")
    assert pv["kind"] == "source_activate"
    assert pv["state"] == "pending"
    assert pv["decision_id"] is None
    assert pv["resolved_at"] is None
    # TTL 窗（30 分钟 = 1800s，服务层计算后写入）
    assert 1700 < pv["expires_at"] - pv["created_at"] < 1900
    # payload/baseline/impact 落库精确（与 200 体一致）
    assert json.loads(pv["payload"]) == {"target_revision_id": rev2["id"]}
    assert json.loads(pv["baseline"]) == {
        "active_revision_id": rev1["id"],
        "target_revision_id": rev2["id"],
        "target_canonical_hash": hashlib.sha256(TEXT2.encode("utf-8")).hexdigest(),
    }
    assert json.loads(pv["baseline"]) == body["baseline"]
    assert json.loads(pv["impact"]) == body["impact"]
