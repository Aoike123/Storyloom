"""S4/S5 正文变更（附录 01 §2 + 00 §5；kind=source_activate）——
POST /api/studio/projects/{pid}/source-changes/preview 与 .../apply 行为验证。

SOURCE-09 实现并验收 S4 preview（T1–T10）；SOURCE-10 实现 S5 apply（T11–T18：
显式应用新正文，仅切换 active_source_revision_id + ReviewDecision）；正文
更新只支持整篇重导入 + 激活预览切换，不提供局部文本 diff 编辑命令
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


def _apply(client, token, pid, preview_id, expected, command_id=None):
    """S5 apply 请求 helper：POST /projects/{pid}/source-changes/apply。"""
    return client.post(
        f"/api/studio/projects/{pid}/source-changes/apply",
        json={
            "preview_id": preview_id,
            "command_id": command_id or _cmd(),
            "expected_active_revision_id": expected,
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


def _project_row(pid) -> dict:
    """项目行（active 指针 + created/updated 数值）。"""
    with Session() as s:
        p = s.get(StudioProject, pid)
        return {
            "active_source_revision_id": p.active_source_revision_id,
            "created": p.created,
            "updated": p.updated,
        }


def _decision_rows() -> list:
    """ReviewDecision 全表行（target_ref 解析为 dict；按 id 排序）。"""
    rows = _table_rows("studio_review_decisions")
    for r in rows:
        r["target_ref"] = json.loads(r["target_ref"])
    return rows


def _user_id(username) -> str:
    with Session() as s:
        return (
            s.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username})
            .scalars()
            .first()
        )


def _set_active(pid, revision_id):
    """直改 active 指针（测试库专用构造）。S4/S5 已放行但保留直改：真实激活会留
    ChangePreview/ReviewDecision 行，而本文件多个测试对两表做精确行数断言；
    本 fixture 只是快速到达"两版本各带片段、active 回 rev1"的前置，不承担
    激活行为验证（激活行为由 T1–T18 经真实命令覆盖）。"""
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
    """rev1（active）3 片段 → 导入 rev2（非 active）→ 切 active 到 rev2 建其片段
    → 切回 rev1（_set_active 直改，理由见该 helper 注释）。返回 (rev1, rev2, rev1_fids, rev2_fids)。"""
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


# ────────────────────────────────────────────────────────────────────────────
# T11 无 token → 401
# ────────────────────────────────────────────────────────────────────────────
def test_t11_no_token_401(db, client):
    pid = "01" * 13
    r = client.post(
        f"/api/studio/projects/{pid}/source-changes/apply",
        json={
            "preview_id": "02" * 13,
            "command_id": _cmd(),
            "expected_active_revision_id": "03" * 13,
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


# ────────────────────────────────────────────────────────────────────────────
# T12 preview 不存在（26 字符字面量）/跨项目 → 404 kind=preview 不泄漏；
#     kind mismatch（F 系 fragment preview 的 id）→ 422 mismatch
# ────────────────────────────────────────────────────────────────────────────
def test_t12_preview_404_and_kind_mismatch(db, client):
    ta = _register(client, "s5_t12_alice")
    tb = _register(client, "s5_t12_bob")
    pa = _create_project(client, ta, "T12A")
    rev_a1 = _import_active(client, ta, pa, TEXT)
    time.sleep(0.002)
    _import(client, ta, pa, TEXT2)
    pb = _create_project(client, tb, "T12B")
    _import_active(client, tb, pb, TEXT)
    time.sleep(0.002)
    rev_b2 = _import(client, tb, pb, TEXT2).json()
    pv_b = _preview(client, tb, pb, rev_b2["id"])
    assert pv_b.status_code == 200, pv_b.text
    pv_b_id = pv_b.json()["preview_id"]

    # F 系 fragment_retire preview（kind mismatch 素材）
    r = _create_fragment(client, ta, pa, rev_a1["id"], 0, 10, "F0", expected=None)
    assert r.status_code == 201, r.text
    fid = r.json()["object_ref"]["id"]
    rp = client.post(
        f"/api/studio/projects/{pa}/fragments/{fid}/retire",
        json={"target_fragment_id": fid, "command_id": _cmd()},
        headers=_auth(ta),
    )
    assert rp.status_code == 200, rp.text
    frag_pv_id = rp.json()["preview_id"]

    previews_before = _count("studio_change_previews")
    cmds_before = _count("studio_command_records")

    # (a) preview 不存在（26 字符字面量）→ 404 kind=preview
    ghost = "04" * 13
    r1 = _apply(client, ta, pa, ghost, rev_a1["id"])
    assert r1.status_code == 404, r1.text
    e1 = r1.json()["error"]
    assert e1["code"] == "not_found"
    assert e1["details"] == {"kind": "preview", "id": ghost}

    # (b) 跨项目（他项目真实 preview id）→ 同形状，不泄漏存在性
    r2 = _apply(client, ta, pa, pv_b_id, rev_a1["id"])
    assert r2.status_code == 404, r2.text
    e2 = r2.json()["error"]
    assert e2["code"] == "not_found"
    assert e2["details"] == {"kind": "preview", "id": pv_b_id}
    assert e2["message"] == e1["message"]

    # (c) kind mismatch：fragment_retire preview 的 id 来 apply → 422 mismatch
    r3 = _apply(client, ta, pa, frag_pv_id, rev_a1["id"])
    assert r3.status_code == 422, r3.text
    err = r3.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "preview_id"
    assert v["rule"] == "mismatch"
    assert v["message"] == "preview_id 对应的预览不是正文激活预览。"

    # 三路全零写：无新增 preview/command/decision，active 不变
    assert _count("studio_change_previews") == previews_before
    assert _count("studio_command_records") == cmds_before
    assert _count("studio_review_decisions") == 0
    assert _project_row(pa)["active_source_revision_id"] == rev_a1["id"]


# ────────────────────────────────────────────────────────────────────────────
# T13 有效 apply 全程：rev1（3 片段）+ rev2 → S4 → S5(expected=rev1) → 200
#     新项目 DTO（active==rev2、updated_at 变化）；DB：ReviewDecision 恰 1
#     （digest 独立重算相等、target_ref 精确）、preview applied+decision_id
#     回填；旧依据全留（版本行/片段绑定逐字段不变）；rev1 写拒绝 422、rev2
#     写 201；再 S4+S5 激活回 rev1 → 派生标记自动消失（重新可写 201）
# ────────────────────────────────────────────────────────────────────────────
def test_t13_valid_apply_full_flow(db, client):
    token = _register(client, "s5_t13_alice")
    pid = _create_project(client, token, "T13Proj")
    rev1, rev2, fids1, fids2 = _scenario_two_revisions(client, token, pid)

    # apply 前快照：版本/片段/范围集合全行 + 项目行
    tables = (
        "studio_source_revisions",
        "studio_fragments",
        "studio_fragment_revisions",
        "studio_range_sets",
    )
    before = {t: _table_rows(t) for t in tables}
    proj_before = _project_row(pid)
    assert proj_before["active_source_revision_id"] == rev1["id"]

    # S4 preview → S5 apply(expected=rev1)
    rp = _preview(client, token, pid, rev2["id"])
    assert rp.status_code == 200, rp.text
    pv = rp.json()
    ra = _apply(client, token, pid, pv["preview_id"], rev1["id"])
    assert ra.status_code == 200, ra.text
    body = ra.json()
    # 200 新项目 DTO（附录 01 P3 全字段形状；active==rev2、updated_at 变化）
    assert set(body.keys()) == {
        "id",
        "name",
        "description",
        "visibility",
        "active_source_revision_id",
        "created_at",
        "updated_at",
    }
    assert body["id"] == pid
    assert body["active_source_revision_id"] == rev2["id"]
    assert body["created_at"] == proj_before["created"]
    assert body["updated_at"] != proj_before["updated"]
    proj_after = _project_row(pid)
    assert proj_after["active_source_revision_id"] == rev2["id"]
    assert proj_after["updated"] == body["updated_at"]

    # DB：ReviewDecision 恰 1（digest 独立重算相等、target_ref 精确）、
    # preview applied + decision_id 回填
    decisions = _decision_rows()
    assert len(decisions) == 1
    d = decisions[0]
    assert d["project_id"] == pid
    assert d["owner_id"] == _user_id("s5_t13_alice")
    assert d["preview_id"] == pv["preview_id"]
    assert d["decision"] == "applied"
    assert d["target_ref"] == {
        "kind": "source_revision",
        "id": rev2["id"],
        "revision": None,
    }
    digest = hashlib.sha256(
        json.dumps(pv["baseline"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert d["baseline_digest"] == digest
    prow = _preview_row(pv["preview_id"])
    assert prow["state"] == "applied"
    assert prow["decision_id"] == d["id"]
    assert prow["resolved_at"] is not None

    # 旧依据全留：rev1/rev2 两版本行与片段/版本/范围集合行逐字段不变；
    # 片段仍绑 rev1、持久 state 仍 candidate
    for t, rows in before.items():
        assert _table_rows(t) == rows, t
    frag_rows = _table_rows("studio_fragments")
    for fid in fids1:
        row = next(r for r in frag_rows if r["id"] == fid)
        assert row["source_revision_id"] == rev1["id"]
        assert row["state"] == "candidate"
    # 附录 02 §1 读时派生（F3 读端点属后续卡，不在本卡范围）：绑定版本(rev1)
    # ≠ 项目 active(rev2) 且持久 state=candidate → DTO state 派生
    # pending_review、revision_is_active=false；其派生输入（绑定关系与持久
    # 状态）如上逐字段不变，故派生结果确定

    # 非 active 一切写命令拒绝（附录 02 §3）：对 rev1 建片段 → 422
    r_old = _create_fragment(
        client, token, pid, rev1["id"], 40, 50, "OLD", expected=None
    )
    assert r_old.status_code == 422, r_old.text
    err = r_old.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    # 新任务用新来源：对 rev2 建 → 201（G0 占 {0,10}，cas 现为 2）
    r_new = _create_fragment(
        client, token, pid, rev2["id"], 10, 20, "NEW", expected=2
    )
    assert r_new.status_code == 201, r_new.text
    assert r_new.json()["state"] == "candidate"

    # 再 S4+S5 激活回 rev1 → "重新激活旧版本自动消失"（附录 02 §1）：
    # 派生标记消失、rev1 重新可写（无需任何回写命令；cas 仍为 4）
    rp2 = _preview(client, token, pid, rev1["id"])
    assert rp2.status_code == 200, rp2.text
    ra2 = _apply(client, token, pid, rp2.json()["preview_id"], rev2["id"])
    assert ra2.status_code == 200, ra2.text
    assert ra2.json()["active_source_revision_id"] == rev1["id"]
    r_back = _create_fragment(
        client, token, pid, rev1["id"], 40, 50, "BACK", expected=4
    )
    assert r_back.status_code == 201, r_back.text
    assert r_back.json()["state"] == "candidate"
    # 原 rev1 片段持久行仍逐字段不变（state 从未落库改写）
    for fid in fids1:
        row = next(r for r in _table_rows("studio_fragments") if r["id"] == fid)
        assert row["source_revision_id"] == rev1["id"]
        assert row["state"] == "candidate"


# ────────────────────────────────────────────────────────────────────────────
# T14 幂等重放：apply 同 command_id → 200 原体、决定恰 1、active 不二次切换
#     （updated_at 不再变）；同 command_id 不同 expected → 422 reused
# ────────────────────────────────────────────────────────────────────────────
def test_t14_replay_and_reused(db, client):
    token = _register(client, "s5_t14_bob")
    pid = _create_project(client, token, "T14Proj")
    rev1 = _import_active(client, token, pid, TEXT)
    time.sleep(0.002)
    rev2 = _import(client, token, pid, TEXT2).json()

    pv = _preview(client, token, pid, rev2["id"]).json()
    c1 = _cmd()
    r1 = _apply(client, token, pid, pv["preview_id"], rev1["id"], command_id=c1)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["active_source_revision_id"] == rev2["id"]

    # 同 command_id 重放 → 200 原体；决定恰 1；active 不二次切换
    r2 = _apply(client, token, pid, pv["preview_id"], rev1["id"], command_id=c1)
    assert r2.status_code == 200, r2.text
    assert r2.json() == body1
    assert len(_decision_rows()) == 1
    proj = _project_row(pid)
    assert proj["active_source_revision_id"] == rev2["id"]
    assert proj["updated"] == body1["updated_at"]  # updated_at 不再变

    # 同 command_id 不同 expected → 422 validation_failed(rule=reused)
    r3 = _apply(client, token, pid, pv["preview_id"], rev2["id"], command_id=c1)
    assert r3.status_code == 422, r3.text
    err = r3.json()["error"]
    assert err["code"] == "validation_failed"
    v = err["details"]["violations"][0]
    assert v["field"] == "command_id"
    assert v["rule"] == "reused"
    assert v["message"] == "command_id 已被其他命令使用，请生成新的。"
    assert len(_decision_rows()) == 1
    assert _project_row(pid)["updated"] == body1["updated_at"]


# ────────────────────────────────────────────────────────────────────────────
# T15 CAS/baseline 两层失败：preview 后先 apply 另一个到 rev3 的 preview 使
#     active 变 → (a) 原 apply 仍带旧 expected=rev1 → 409 revision_conflict
#     (object={project,pid})；(b) 带 expected=rev3（当前）→ 409 preview_stale
#     conflicts 恰 1（project 对象）+ preview superseded；两路均无半次
# ────────────────────────────────────────────────────────────────────────────
def test_t15_cas_then_baseline_stale(db, client):
    token = _register(client, "s5_t15_carol")
    pid = _create_project(client, token, "T15Proj")
    rev1 = _import_active(client, token, pid, TEXT)
    time.sleep(0.002)
    rev2 = _import(client, token, pid, TEXT2).json()
    time.sleep(0.002)
    rev3 = _import(client, token, pid, TEXT3).json()

    pv_a = _preview(client, token, pid, rev2["id"]).json()  # baseline.active=rev1
    pv_b = _preview(client, token, pid, rev3["id"]).json()  # baseline.active=rev1

    # 先 apply 另一个到 rev3 的 preview 使 active 变
    rb = _apply(client, token, pid, pv_b["preview_id"], rev1["id"])
    assert rb.status_code == 200, rb.text
    assert rb.json()["active_source_revision_id"] == rev3["id"]
    assert len(_decision_rows()) == 1
    cmds_after_b = _count("studio_command_records")

    # (a) 原 apply 仍带旧 expected=rev1 → 409 revision_conflict（CAS 先报）
    ra = _apply(client, token, pid, pv_a["preview_id"], rev1["id"])
    assert ra.status_code == 409, ra.text
    err = ra.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"]["object"] == {"kind": "project", "id": pid}
    assert err["details"]["expected"] == rev1["id"]
    assert err["details"]["actual"] == rev3["id"]
    assert err["message"] == "当前正文版本已变化，请刷新后重试。"
    # 无半次：preview_a 仍 pending、active 不再变、无新决定/命令
    assert _preview_row(pv_a["preview_id"])["state"] == "pending"
    assert _project_row(pid)["active_source_revision_id"] == rev3["id"]
    assert len(_decision_rows()) == 1
    assert _count("studio_command_records") == cmds_after_b

    # (b) 带 expected=rev3（当前）→ CAS 过、写事务内 baseline 失配 → 409
    # preview_stale（conflicts 恰 1，project 对象）+ preview superseded
    rb2 = _apply(client, token, pid, pv_a["preview_id"], rev3["id"])
    assert rb2.status_code == 409, rb2.text
    err2 = rb2.json()["error"]
    assert err2["code"] == "preview_stale"
    assert err2["details"]["preview_id"] == pv_a["preview_id"]
    assert err2["details"]["conflicts"] == [
        {
            "object": {"kind": "project", "id": pid},
            "expected": rev1["id"],
            "actual": rev3["id"],
        }
    ]
    assert err2["message"] == "预览基准已变化（当前正文版本），请重新预览。"
    prow = _preview_row(pv_a["preview_id"])
    assert prow["state"] == "superseded"
    assert prow["resolved_at"] is not None
    assert prow["decision_id"] is None
    # 无半次：active 不再变、无新决定
    assert _project_row(pid)["active_source_revision_id"] == rev3["id"]
    assert len(_decision_rows()) == 1


# ────────────────────────────────────────────────────────────────────────────
# T16 过期/终态：直改 expires_at 过期 → apply 409 preview_stale 且惰性置
#     expired；已 applied preview 换新 command_id 再 apply → 409
#     (actual=applied) 不改写；过期可重算：新 command_id 重新 S4 → 新
#     preview_id apply 200
# ────────────────────────────────────────────────────────────────────────────
def test_t16_expired_and_terminal_states(db, client):
    token = _register(client, "s5_t16_dana")
    # ── 场景 1：过期 → 惰性置 expired + 409；过期可重算（新 preview → 200）
    pid = _create_project(client, token, "T16A")
    rev1 = _import_active(client, token, pid, TEXT)
    time.sleep(0.002)
    rev2 = _import(client, token, pid, TEXT2).json()
    pv1 = _preview(client, token, pid, rev2["id"]).json()
    # 直改 expires_at 过期（测试库专用构造）
    with Session() as s:
        s.execute(
            update(ChangePreview)
            .where(ChangePreview.id == pv1["preview_id"])
            .values(expires_at=time.time() - 1)
        )
        s.commit()
    r1 = _apply(client, token, pid, pv1["preview_id"], rev1["id"])
    assert r1.status_code == 409, r1.text
    err = r1.json()["error"]
    assert err["code"] == "preview_stale"
    assert err["details"]["preview_id"] == pv1["preview_id"]
    assert err["details"]["conflicts"] == [
        {
            "object": {"kind": "preview", "id": pv1["preview_id"]},
            "expected": "pending",
            "actual": "expired",
        }
    ]
    # 惰性置 expired（独立提交生效）；active 不变、无决定
    prow = _preview_row(pv1["preview_id"])
    assert prow["state"] == "expired"
    assert prow["resolved_at"] is not None
    assert _project_row(pid)["active_source_revision_id"] == rev1["id"]
    assert len(_decision_rows()) == 0

    # 过期可重算：以新 command_id 重新 S4 preview → 新 preview_id apply 200
    pv2 = _preview(client, token, pid, rev2["id"]).json()
    assert pv2["preview_id"] != pv1["preview_id"]
    r2 = _apply(client, token, pid, pv2["preview_id"], rev1["id"])
    assert r2.status_code == 200, r2.text
    assert r2.json()["active_source_revision_id"] == rev2["id"]
    assert len(_decision_rows()) == 1

    # ── 场景 2：已 applied preview 换新 command_id 再 apply → 409
    # （actual=applied），终态行不改写
    pid2 = _create_project(client, token, "T16B")
    r1b = _import_active(client, token, pid2, TEXT)
    time.sleep(0.002)
    r2b = _import(client, token, pid2, TEXT2).json()
    pvb = _preview(client, token, pid2, r2b["id"]).json()
    ok = _apply(client, token, pid2, pvb["preview_id"], r1b["id"])
    assert ok.status_code == 200, ok.text
    row_before = _preview_row(pvb["preview_id"])
    assert row_before["state"] == "applied"
    r3 = _apply(
        client, token, pid2, pvb["preview_id"], r1b["id"], command_id=_cmd()
    )
    assert r3.status_code == 409, r3.text
    err3 = r3.json()["error"]
    assert err3["code"] == "preview_stale"
    assert err3["details"]["preview_id"] == pvb["preview_id"]
    assert err3["details"]["conflicts"] == [
        {
            "object": {"kind": "preview", "id": pvb["preview_id"]},
            "expected": "pending",
            "actual": "applied",
        }
    ]
    # 终态不可逆：行不改写（state/decision_id/resolved_at 全同）
    assert _preview_row(pvb["preview_id"]) == row_before
    assert _project_row(pid2)["active_source_revision_id"] == r2b["id"]
    assert len([d for d in _decision_rows() if d["project_id"] == pid2]) == 1


# ────────────────────────────────────────────────────────────────────────────
# T17 旧任务/媒体保持实际依据（卡片验收）：种子 pending job+media（照 T8
#     模式）→ apply 后 job/media 行逐字段不变（不自动重生成、不迁移）
# ────────────────────────────────────────────────────────────────────────────
def test_t17_jobs_media_keep_actual_basis(db, client):
    token = _register(client, "s5_t17_earl")
    uid = _user_id("s5_t17_earl")
    pid = _create_project(client, token, "T17Proj")
    rev1, rev2, fids1, _ = _scenario_two_revisions(
        client, token, pid, rev2_ranges=None
    )
    _seed_jobs_media(pid, uid, fids1[0])
    jobs_before = _table_rows("studio_jobs")
    media_before = _table_rows("studio_media_artifacts")
    assert len(jobs_before) == 2
    assert len(media_before) == 1

    pv = _preview(client, token, pid, rev2["id"]).json()
    r = _apply(client, token, pid, pv["preview_id"], rev1["id"])
    assert r.status_code == 200, r.text
    assert r.json()["active_source_revision_id"] == rev2["id"]

    # 逐字段不变：apply 只切换 active 指针，不重生成、不迁移任何任务/媒体
    assert _table_rows("studio_jobs") == jobs_before
    assert _table_rows("studio_media_artifacts") == media_before


# ────────────────────────────────────────────────────────────────────────────
# T18 目标版本不可变：apply 后 rev1/rev2 行（raw/canonical/hash/previous/
#     created_at 等全列）逐字段不变
# ────────────────────────────────────────────────────────────────────────────
def test_t18_target_revision_immutable(db, client):
    token = _register(client, "s5_t18_fred")
    pid = _create_project(client, token, "T18Proj")
    rev1, rev2, _, _ = _scenario_two_revisions(client, token, pid)
    revs_before = _table_rows("studio_source_revisions")
    assert len(revs_before) == 2

    pv = _preview(client, token, pid, rev2["id"]).json()
    r = _apply(client, token, pid, pv["preview_id"], rev1["id"])
    assert r.status_code == 200, r.text
    assert r.json()["active_source_revision_id"] == rev2["id"]

    # 版本行逐字段不变（apply 只 UPDATE projects.active 指针，无版本写路径）
    assert _table_rows("studio_source_revisions") == revs_before
