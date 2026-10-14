"""JOB-02 幂等提交作业（附录 09 §1/§3/§4）—— POST /projects/{pid}/jobs 行为验证。

覆盖：401、项目 404（不存在/非属主同形状）、kind 枚举、target 形状与 kind 配对、
目标不存在/跨项目 404 同形状、计费边界（供应商未配置 → 具体缺失项名；收费 kind
未确认成本 → precondition_failed）、成功 201（DTO 全键 + status=pending + attempts=0
+ job_events 恰 1 行 created + 零 attempt + 租约 NULL）、input_digest 独立重算、
**不扩大任务**（提交后改/加对象 → payload/digest/ref_id 不变）、**切片段不重新归属**、
幂等重放 200 原体零新行、异内容 422 reused。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定；本文件不改写 STUDIO_* env。
db fixture 与 register/auth/helper 照抄 tests/studio/test_projects.py。
"""
import json
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.core.db import Base, Session, engine
from backend.studio.contracts.models import CommandRecord
from backend.studio.jobs.models import JobAttempt, JobEvent, StudioJob


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（scripts/shots/assets/projects 互相引用）：drop 前临时关 FK
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


def _mk_project(client, token, name="JProj") -> str:
    r = client.post(
        "/api/studio/projects",
        json={"name": name, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_asset(pid: str) -> str:
    """直接落一行资产（本卡只读目标存在性，不经资产域命令以便聚焦 JOB-02）。"""
    from backend.core.db import make_ulid
    from backend.studio.assets.models import Asset, AssetRevision

    asset_id = make_ulid()
    rev_id = make_ulid()
    with Session.begin() as s:
        s.add(
            Asset(
                id=asset_id,
                project_id=pid,
                type="character",
                name="A",
                character_id=None,
                costume_required=True,
                current_revision_id=rev_id,
            )
        )
        s.flush()
        s.add(
            AssetRevision(
                id=rev_id,
                asset_id=asset_id,
                revision=1,
                spec=json.dumps({"name": "A"}, ensure_ascii=False),
                media_artifact_id=None,
                generation_input=None,
                review_status="unreviewed",
            )
        )
    return asset_id


def _counts() -> dict:
    with Session() as s:
        return {
            "jobs": s.scalar(select(func.count()).select_from(StudioJob)),
            "attempts": s.scalar(select(func.count()).select_from(JobAttempt)),
            "events": s.scalar(select(func.count()).select_from(JobEvent)),
            "commands": s.scalar(select(func.count()).select_from(CommandRecord)),
        }


def _configure_image(monkeypatch):
    monkeypatch.setenv("IMAGE_PROVIDER", "siliconflow")
    monkeypatch.setenv("IMAGE_ENDPOINT", "https://img.example/v1")
    monkeypatch.setenv("IMAGE_MODEL", "m")
    monkeypatch.setenv("IMAGE_API_KEY", "k")


def _configure_video(monkeypatch):
    monkeypatch.setenv("VIDEO_PROVIDER", "minimax")
    monkeypatch.setenv("VIDEO_ENDPOINT", "https://vid.example/v1")
    monkeypatch.setenv("VIDEO_MODEL", "m")
    monkeypatch.setenv("VIDEO_API_KEY", "k")


def _post(client, token, pid, body):
    return client.post(f"/api/studio/projects/{pid}/jobs", json=body, headers=_auth(token))


def _job_body(asset_id, **kw):
    body = {
        "kind": "asset_generate",
        "target": {"kind": "asset", "id": asset_id, "revision": 1},
        "input": {"prompt": "a cat"},
        "priority": 0,
        "confirm_cost": True,
        "command_id": _cmd(),
    }
    body.update(kw)
    return body


# ── 鉴权与项目 404 ─────────────────────────────────────────────────────────


def test_t1_no_token_401(db, client):
    pid = "p"
    r = client.post(
        f"/api/studio/projects/{pid}/jobs",
        json={"kind": "transcode", "target": {"kind": "media_artifact", "id": "x"},
              "input": {}, "command_id": _cmd()},
    )
    assert r.status_code == 401


def test_t2_project_404_both_shapes(db, client):
    """项目不存在与他人项目同形状（不泄漏存在性）。

    两者 details.id 必然不同（各自被请求的 pid），故比对**形状**：code/message 与
    details 的键集合一致，且都只指向 project。
    """
    token_a = _register(client, "ua")
    token_b = _register(client, "ub")
    pid_a = _mk_project(client, token_a, "A")
    body = {"kind": "transcode", "target": {"kind": "media_artifact", "id": "x"},
            "input": {}, "command_id": _cmd()}
    r_missing = _post(client, token_a, "nosuchproject", body)
    r_other = _post(client, token_b, pid_a, body)
    assert r_missing.status_code == 404 and r_other.status_code == 404
    e1, e2 = r_missing.json()["error"], r_other.json()["error"]
    assert e1["code"] == e2["code"] == "not_found"
    assert e1["message"] == e2["message"]
    assert set(e1["details"]) == set(e2["details"]) == {"kind", "id"}
    assert e1["details"]["kind"] == e2["details"]["kind"] == "project"


# ── 形状校验 422 ───────────────────────────────────────────────────────────


def test_t3_kind_enum_422(db, client):
    token = _register(client, "u1")
    pid = _mk_project(client, token)
    r = _post(client, token, pid, {
        "kind": "llm",  # 不在 DB CheckConstraint 冻结集
        "target": {"kind": "asset", "id": "a"},
        "input": {}, "command_id": _cmd(),
    })
    assert r.status_code == 422
    assert r.json()["error"]["details"]["violations"][0]["field"] == "kind"


def test_t4_target_kind_mismatch_422(db, client):
    """kind 与 target.kind 必须配对（否则 export 可指向任意对象）。"""
    token = _register(client, "u2")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    r = _post(client, token, pid, {
        "kind": "export",
        "target": {"kind": "asset", "id": asset_id, "revision": 1},
        "input": {}, "command_id": _cmd(),
    })
    assert r.status_code == 422
    v = r.json()["error"]["details"]["violations"][0]
    assert v["field"] == "target" and v["rule"] == "mismatch"


def test_t5_input_not_object_422(db, client):
    token = _register(client, "u3")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    r = _post(client, token, pid, _job_body(asset_id, input="not-an-object"))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["violations"][0]["field"] == "input"


def test_t6_priority_not_int_422(db, client):
    """priority 非整数：Pydantic 层先拦（FastAPI 默认 422 形状）。

    说明：`priority="high"` 在**请求体形态**层就被 Pydantic 拒绝，因此响应体不是冻结
    错误协议体——这与既有多卡一致（形态层错误走 FastAPI 默认 422，只有规格显式冻结的
    违规才走冻结体）。此处断言 422 且不产生任何行。
    """
    token = _register(client, "u4")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    before = _counts()
    r = _post(client, token, pid, _job_body(asset_id, priority="high"))
    assert r.status_code == 422  # FastAPI 形态层 422
    assert _counts() == before  # 零副作用


def test_t7_target_missing_or_cross_project_404_same_shape(db, client):
    token_a = _register(client, "u5a")
    token_b = _register(client, "u5b")
    pid_a = _mk_project(client, token_a, "A")
    pid_b = _mk_project(client, token_b, "B")
    asset_a = _seed_asset(pid_a)
    # B 用自己的项目 + A 的真实资产 id → 404；B 用自己的项目 + 不存在 id → 404
    r_cross = _post(client, token_b, pid_b, {
        "kind": "asset_generate", "target": {"kind": "asset", "id": asset_a},
        "input": {}, "confirm_cost": True, "command_id": _cmd()})
    r_missing = _post(client, token_b, pid_b, {
        "kind": "asset_generate", "target": {"kind": "asset", "id": "nosuchid"},
        "input": {}, "confirm_cost": True, "command_id": _cmd()})
    assert r_cross.status_code == 404 and r_missing.status_code == 404
    assert r_cross.json()["error"]["code"] == r_missing.json()["error"]["code"] == "not_found"
    assert r_cross.json()["error"]["details"]["kind"] == "asset"


# ── 计费边界（附录 09 §4） ─────────────────────────────────────────────────


def test_t8_provider_not_configured_names_missing(db, client, monkeypatch):
    for k in ("IMAGE_PROVIDER", "IMAGE_ENDPOINT", "IMAGE_MODEL", "IMAGE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    token = _register(client, "u6")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    r = _post(client, token, pid, _job_body(asset_id))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "provider_not_configured"
    # message 必须报出**具体缺失项名**（JOB-01 的 missing_env 语义）
    assert "IMAGE_API_KEY" in err["message"]


def test_t9_paid_kind_requires_cost_confirmation(db, client, monkeypatch):
    _configure_image(monkeypatch)
    token = _register(client, "u7")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    r = _post(client, token, pid, _job_body(asset_id, confirm_cost=False))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "cost_not_confirmed"


def test_t10_local_kind_needs_no_provider_or_cost_confirmation(db, client, monkeypatch):
    """transcode 是纯本地 kind：不需要供应商配置、不需要成本确认。"""
    from backend.core.db import make_ulid
    from backend.studio.media.models import MediaArtifact

    for k in ("IMAGE_API_KEY", "VIDEO_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    token = _register(client, "u8")
    pid = _mk_project(client, token)
    art_id = make_ulid()
    with Session.begin() as s:
        s.add(MediaArtifact(id=art_id, project_id=pid, media_kind="video",
                            filename=f"{pid}/{art_id}.mp4", size_bytes=1,
                            sha256="0" * 64, source="upload"))
    r = _post(client, token, pid, {
        "kind": "transcode", "target": {"kind": "media_artifact", "id": art_id},
        "input": {"to": "mp4"}, "command_id": _cmd()})
    assert r.status_code == 201, r.text


# ── 成功路径 ───────────────────────────────────────────────────────────────


def test_t11_success_201_dto_and_row_shape(db, client, monkeypatch):
    _configure_image(monkeypatch)
    token = _register(client, "u9")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    r = _post(client, token, pid, _job_body(asset_id))
    assert r.status_code == 201, r.text
    d = r.json()
    assert set(d) == {
        "object_ref", "kind", "status", "target", "input_digest", "payload",
        "priority", "attempts", "created_at", "submitted_by", "project_id",
    }
    assert d["object_ref"]["kind"] == "job" and d["object_ref"]["revision"] is None
    assert d["kind"] == "asset_generate"
    assert d["status"] == "pending"
    assert d["attempts"] == 0
    assert d["target"] == {"kind": "asset", "id": asset_id, "revision": 1}
    assert d["payload"]["input"] == {"prompt": "a cat"}
    # 行与事件
    with Session() as s:
        job = s.get(StudioJob, d["object_ref"]["id"])
        assert job is not None and job.status == "pending" and job.attempts == 0
        assert job.lease_owner is None and job.lease_expires_at is None
        events = s.scalars(select(JobEvent).where(JobEvent.job_id == job.id)).all()
        assert len(events) == 1 and events[0].type == "created"
    assert _counts()["attempts"] == 0  # 未执行：零 attempt


def test_t12_input_digest_independently_recomputed(db, client, monkeypatch):
    import hashlib
    _configure_image(monkeypatch)
    token = _register(client, "u10")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    d = _post(client, token, pid, _job_body(asset_id)).json()
    payload = d["payload"]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expect = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert d["input_digest"] == expect
    assert len(d["input_digest"]) == 64


def test_t13_frozen_payload_not_expanded_by_later_objects(db, client, monkeypatch):
    """卡面核心：提交后再改/加对象，已提交 job 的 payload/digest/ref_id 不变。"""
    from backend.core.db import make_ulid
    from backend.studio.assets.models import Asset, AssetRevision

    _configure_image(monkeypatch)
    token = _register(client, "u11")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    d = _post(client, token, pid, _job_body(asset_id)).json()

    # 该资产升版 + 新增另一资产（"未来新增对象"）
    with Session.begin() as s:
        rev_id = make_ulid()
        s.add(AssetRevision(id=rev_id, asset_id=asset_id, revision=2,
                            spec=json.dumps({"name": "A2"}, ensure_ascii=False),
                            media_artifact_id=None, generation_input=None,
                            review_status="unreviewed"))
        asset = s.get(Asset, asset_id)
        asset.current_revision_id = rev_id
        other_id = make_ulid()
        other_rev = make_ulid()
        s.add(Asset(id=other_id, project_id=pid, type="character", name="B",
                    character_id=None, costume_required=True, current_revision_id=other_rev))
        s.flush()
        s.add(AssetRevision(id=other_rev, asset_id=other_id, revision=1,
                            spec=json.dumps({"name": "B"}, ensure_ascii=False),
                            media_artifact_id=None, generation_input=None,
                            review_status="unreviewed"))

    # 读回 DB 行：payload 与 ref_id 必须逐字节不变
    with Session() as s:
        job = s.get(StudioJob, d["object_ref"]["id"])
        assert job.ref_id == asset_id
        assert job.ref_kind == "asset"
        assert json.loads(job.payload) == d["payload"]
    assert d["target"]["revision"] == 1  # 冻结在提交时刻的 revision


def test_t14_switching_target_does_not_reassign_job(db, client, monkeypatch):
    """切片段不重新归属：job 的 ref_kind/ref_id 与 payload 固定于提交时刻。"""
    _configure_image(monkeypatch)
    token = _register(client, "u12")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    d = _post(client, token, pid, _job_body(asset_id)).json()
    with Session() as s:
        job = s.get(StudioJob, d["object_ref"]["id"])
        before = (job.ref_kind, job.ref_id, job.payload)
    # 直接改该资产所属项目（模拟归属变化）后复核 job 三字段
    with Session.begin() as s:
        from backend.studio.assets.models import Asset
        asset = s.get(Asset, asset_id)
        other_pid = _mk_project(client, token, "Other")
        asset.project_id = other_pid
    with Session() as s:
        job = s.get(StudioJob, d["object_ref"]["id"])
        assert (job.ref_kind, job.ref_id, job.payload) == before


def test_t15_replay_200_original_body_zero_new_rows(db, client, monkeypatch):
    _configure_image(monkeypatch)
    token = _register(client, "u13")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    body = _job_body(asset_id)
    first = _post(client, token, pid, body)
    assert first.status_code == 201
    before = _counts()
    again = _post(client, token, pid, body)
    assert again.status_code == 200
    assert again.json() == first.json()
    assert _counts() == before  # 零新行、零新事件


def test_t16_same_command_different_content_422_reused(db, client, monkeypatch):
    _configure_image(monkeypatch)
    token = _register(client, "u14")
    pid = _mk_project(client, token)
    asset_id = _seed_asset(pid)
    body = _job_body(asset_id)
    assert _post(client, token, pid, body).status_code == 201
    body["input"] = {"prompt": "different"}
    r = _post(client, token, pid, body)
    assert r.status_code == 422
    v = r.json()["error"]["details"]["violations"][0]
    assert v["field"] == "command_id" and v["rule"] == "reused"
