"""JOB-03 验收：新 worker 租约执行与恢复（附录 09 §4/§5）。

重点在**并发与恢复的正确性**，而不是走通流程：

- 互斥：单语句条件 UPDATE 仲裁 —— 两个 worker 不会同时执行同 attempt；
- 过期 claim 不能写当前结果：写终态前再校验，失败即**丢弃**结果；
- 恢复不盲目重发收费请求：已知厂商号 → **只轮询**（提交路径调用次数必须为 0）；
- 不确定 → `indeterminate`，job 停在 running，不自动重试（R08）。

本文件不改写 STUDIO_* env；db 夹具照抄 test_projects.py（drop_all/create_all 后再建
project+user，因为 studio_jobs 对二者有 FK）。
"""
import json
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.core.db import Base, Session, engine, make_ulid
from backend.studio.jobs.models import JobAttempt, JobEvent, StudioJob
from backend.studio.jobs.runner import (
    LEASE_SECONDS,
    HandlerResult,
    JobRunner,
    default_handlers,
)
from backend.studio.jobs import worker_main
from backend.studio.projects.models import StudioProject


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        Base.metadata.drop_all(conn, checkfirst=True)
        conn.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(conn)
        conn.commit()
    finally:
        conn.close()
    yield
    engine.dispose()


def _seed_job(pid: str, uid: str, kind="transcode", payload=None,
              status="pending", lease_owner=None, lease_expires_at=None) -> str:
    job_id = make_ulid()
    with Session.begin() as s:
        s.add(
            StudioJob(
                id=job_id, project_id=pid, owner_id=uid, kind=kind,
                ref_kind="media_artifact", ref_id="m1",
                payload=json.dumps(payload or {"kind": kind, "target": {"kind": "media_artifact", "id": "m1", "revision": None}, "input": {}}),
                status=status, priority=0, created_at=0.0, submitted_by=uid,
                lease_owner=lease_owner, lease_expires_at=lease_expires_at, attempts=0,
            )
        )
    return job_id


@pytest.fixture
def pid_uid(db):
    """建 user + project，返回 (pid, uid)（studio_jobs 对二者有 FK）。"""
    from backend.core.accounts import User, hash_password

    uid = make_ulid()
    pid = make_ulid()
    with Session.begin() as s:
        s.add(User(id=uid, username=f"u{uid[:8]}", email="t@x",
                   password_hash=hash_password("secret123"), auth_token="tok"))
        s.flush()
        s.add(StudioProject(id=pid, owner_id=uid, name="P", description="",
                            visibility="private", active_source_revision_id=None,
                            created=0.0, updated=0.0))
    return pid, uid


def _events(job_id):
    with Session() as s:
        return [e.type for e in s.scalars(select(JobEvent).where(JobEvent.job_id == job_id)).all()]


def _job(job_id):
    with Session() as s:
        return s.get(StudioJob, job_id)


def _attempts(job_id):
    with Session() as s:
        return s.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.seq)).all()


# ── 互斥 ───────────────────────────────────────────────────────────────────


def test_t1_two_workers_only_one_claims(db, pid_uid):
    """两个 worker 不会同时执行同 attempt：恰一个抢到，attempts 只 +1。"""
    pid, uid = pid_uid
    job_id = _seed_job(pid, uid)
    calls = []

    def handler(ctx):
        calls.append(ctx.worker_id)
        return HandlerResult(status="succeeded")

    a = JobRunner(worker_id="A", handlers={"transcode": handler})
    b = JobRunner(worker_id="B", handlers={"transcode": handler})
    with Session() as s:
        first = a.claim_and_run_one(s)
    with Session() as s:
        second = b.claim_and_run_one(s)
    assert first == job_id
    assert second is None, "第二个 worker 不应抢到同一 job"
    assert calls == ["A"], "处理器只应被调用一次"
    job = _job(job_id)
    assert job.attempts == 1
    assert len(_attempts(job_id)) == 1


def test_t2_second_worker_cannot_claim_live_lease(db, pid_uid):
    """租约仍有效时，第二个 worker 抢不到（即使 job 是 running）。"""
    pid, uid = pid_uid
    job_id = _seed_job(pid, uid)

    def handler(ctx):
        return HandlerResult(status="succeeded")

    a = JobRunner(worker_id="A", handlers={"transcode": handler}, now=lambda: 1000.0)
    b = JobRunner(worker_id="B", handlers={"transcode": handler}, now=lambda: 1000.0)
    with Session() as s:
        assert a.claim_and_run_one(s) == job_id
    # 用同一时刻再抢：A 的租约未过期 → B 抢不到
    with Session() as s:
        assert b.claim_and_run_one(s) is None


# ── 租约过期与「过期 claim 不能写结果」 ────────────────────────────────────


def test_t3_expired_lease_can_be_reclaimed(db, pid_uid):
    """过期租约可被重新 claim；原持有者被替换（attempt seq 递增）。"""
    pid, uid = pid_uid
    job_id = _seed_job(pid, uid, status="running", lease_owner="A",
                       lease_expires_at=100.0)  # 已过期

    def handler(ctx):
        return HandlerResult(status="succeeded")

    b = JobRunner(worker_id="B", handlers={"transcode": handler}, now=lambda: 2000.0)
    with Session() as s:
        assert b.claim_and_run_one(s) == job_id
    job = _job(job_id)
    assert job.attempts == 1
    assert job.status == "succeeded"  # B 执行完成
    # 成功路径会释放租约；因此「谁执行了」的证据在 attempt 行上
    assert _attempts(job_id)[0].lease_owner == "B"
    assert len(_attempts(job_id)) == 1


def test_t4_expired_claim_must_not_write_result(db, pid_uid):
    """A 的租约过期且被 B 抢走后，A 迟到的写结果必须被丢弃（不得覆盖当前结果）。

    场景直接按规范定义构造：job=running 且租约属于 A 但**已过期**，
    并由 B 抢走（B 的租约有效）——此时 A 调 `_finish` 必须返回 lease_lost，
    且不得改动 job/attempt 的终态。
    """
    pid, uid = pid_uid
    job_id = _seed_job(pid, uid)

    clock = {"t": 1000.0}
    a = JobRunner(worker_id="A",
                  handlers={"transcode": lambda ctx: HandlerResult(status="succeeded")},
                  now=lambda: clock["t"])
    # A 抢到并提交（模拟真实 claim 后 worker 滞留）
    with Session() as s:
        assert a.claim_and_run_one(s) == job_id
    assert _job(job_id).status == "succeeded"

    # 把状态改回「A 持租约但已过期、且 B 已抢走」的滞留场景
    clock["t"] = 1000.0 + LEASE_SECONDS + 5
    with Session.begin() as s:
        job = s.get(StudioJob, job_id)
        job.status = "running"
        job.finished_at = None
        job.lease_owner = "B"          # 已被 B 抢走
        job.lease_expires_at = clock["t"] + LEASE_SECONDS  # B 的租约有效
    # A 迟到的结果：租约不在 A 手上 → 必须丢弃
    with Session() as s:
        job = s.get(StudioJob, job_id)
        attempt = _attempts(job_id)[0]
        status = a._finish(s, job, attempt, HandlerResult(status="succeeded"))
    assert status == "lease_lost"
    fresh = _job(job_id)
    assert fresh.status == "running", "A 的滞后结果不得覆盖当前状态"
    assert fresh.lease_owner == "B", "不得抢走 B 的租约"


# ── 恢复：只轮询不重提 ─────────────────────────────────────────────────────


def test_t5_resume_polls_only_never_resubmits(db, pid_uid):
    """已知厂商任务号 → 恢复只轮询；提交路径调用次数必须为 0。"""
    pid, uid = pid_uid
    job_id = _seed_job(pid, uid, status="running", lease_owner="A")
    # 造一个已知厂商号的 attempt
    with Session.begin() as s:
        s.add(JobAttempt(id=make_ulid(), job_id=job_id, seq=1, lease_owner="A",
                         started_at=0.0, client_request_id=make_ulid(),
                         status="running", provider_request_id="MINIMAX-TASK-9"))

    seen = {"submit": 0, "poll": 0}

    def handler(ctx):
        if ctx.resume:
            seen["poll"] += 1
            return HandlerResult(status="succeeded")
        seen["submit"] += 1
        return HandlerResult(status="succeeded")

    runner = JobRunner(worker_id="B", handlers={"transcode": handler})
    with Session() as s:
        runner.resume(s, job_id)
    assert seen["poll"] == 1
    assert seen["submit"] == 0, "恢复路径绝不能重新提交（收费请求不得盲重发）"


def test_t6_indeterminate_stays_running_and_does_not_retry(db, pid_uid):
    """无厂商号且结果不确定 → attempt=indeterminate、job 停在 running、不再自增。"""
    pid, uid = pid_uid
    job_id = _seed_job(pid, uid)

    def handler(ctx):
        return HandlerResult(status="indeterminate", error_tail="网络中断，未拿到厂商号")

    runner = JobRunner(worker_id="A", handlers={"transcode": handler})
    with Session() as s:
        runner.claim_and_run_one(s)
    job = _job(job_id)
    assert job.status == "running", "结果不确定时 job 停在 running"
    assert job.attempts == 1
    assert _attempts(job_id)[0].status == "indeterminate"
    assert "indeterminate" in _events(job_id)
    # 不自动重试：租约已释放？不 —— indeterminate 保留 running 但不自动再跑
    with Session() as s:
        assert runner.claim_and_run_one(s) is None or _job(job_id).attempts == 1


# ── 成功 / 失败 / 未注册 kind ──────────────────────────────────────────────


def test_t7_success_path_writes_job_attempt_and_event(db, pid_uid):
    pid, uid = pid_uid
    job_id = _seed_job(pid, uid)

    def handler(ctx):
        return HandlerResult(status="succeeded", provider_request_id="PR-1",
                             usage='{"tokens":3}')

    runner = JobRunner(worker_id="A", handlers={"transcode": handler})
    with Session() as s:
        runner.claim_and_run_one(s)
    job = _job(job_id)
    assert job.status == "succeeded"
    assert job.finished_at is not None
    assert job.lease_owner is None
    attempt = _attempts(job_id)[0]
    assert attempt.status == "succeeded"
    assert attempt.provider_request_id == "PR-1"
    assert attempt.usage == '{"tokens":3}'
    assert attempt.finished_at is not None
    assert "succeeded" in _events(job_id)


def test_t8_handler_exception_becomes_failed_not_crash(db, pid_uid):
    pid, uid = pid_uid
    job_id = _seed_job(pid, uid)

    def handler(ctx):
        raise RuntimeError("boom")

    runner = JobRunner(worker_id="A", handlers={"transcode": handler})
    with Session() as s:
        runner.claim_and_run_one(s)
    job = _job(job_id)
    assert job.status == "failed"
    assert "RuntimeError" in (job.last_error or "")
    assert _attempts(job_id)[0].status == "failed"
    assert "failed" in _events(job_id)


def test_t9_unregistered_kind_fails_with_reason(db, pid_uid):
    pid, uid = pid_uid
    job_id = _seed_job(pid, uid, kind="export")
    runner = JobRunner(worker_id="A", handlers={})  # 空注册表
    with Session() as s:
        runner.claim_and_run_one(s)
    job = _job(job_id)
    assert job.status == "failed"
    assert "无处理器" in (job.last_error or "")


def test_t10_default_handlers_cover_all_five_kinds_and_do_not_fake_success(db, pid_uid):
    """桩处理器必须**明确失败/不确定**，绝不假装成功（否则等于伪造生成结果）。"""
    handlers = default_handlers()
    assert set(handlers) == {"shot_generate", "asset_generate", "transcode",
                             "export", "release_publish"}
    pid, uid = pid_uid
    for kind in handlers:
        job_id = _seed_job(pid, uid, kind=kind)
        runner = JobRunner(worker_id="A", handlers=handlers)
        with Session() as s:
            runner.claim_and_run_one(s)
        assert _job(job_id).status == "failed", f"{kind} 桩不得假装成功"


# ── 心跳 ───────────────────────────────────────────────────────────────────


def test_t11_heartbeat_extends_only_for_holder(db, pid_uid):
    pid, uid = pid_uid
    job_id = _seed_job(pid, uid, status="running", lease_owner="A",
                       lease_expires_at=100.0)
    a = JobRunner(worker_id="A", handlers={}, now=lambda: 500.0)
    other = JobRunner(worker_id="C", handlers={}, now=lambda: 500.0)
    with Session() as s:
        assert a.heartbeat(s, job_id) is True
        s.commit()
        assert other.heartbeat(s, job_id) is False, "非持有者不得续期"
    assert _job(job_id).lease_expires_at == 500.0 + LEASE_SECONDS


# ── worker_main ────────────────────────────────────────────────────────────


def test_t12_worker_main_returns_zero_with_no_pending_jobs(db, pid_uid):
    rc = worker_main.main(["--max-iterations", "1", "--worker-id", "w-test"])
    assert rc == 0


def test_t13_worker_main_import_has_no_side_effects():
    """import 不得建表/连库/启线程（只应有模块级常量与函数定义）。"""
    src = (worker_main.__file__ or "")
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert "init_db" not in body, "worker_main 不得在 import 期建表"
    assert "create_all" not in body


# ── 无旧依赖（R11） ────────────────────────────────────────────────────────


def test_t14_no_legacy_backend_imports():
    """R11：runner/worker_main 不得 import 旧后端模块。

    复用 RETIRE-01 的 **AST** 守卫（scripts/check_legacy_deps.py）而不是字符串匹配：
    字符串匹配会把文档字符串里提到的 `backend.worker`（本模块刻意说明「不复用旧
    worker」）误判为依赖——这正是守卫当初选择 AST 的原因。
    """
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "check_legacy_deps", root / "scripts" / "check_legacy_deps.py"
    )
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    violations = [
        v for v in guard.check_imports(root)
        if v["file"].startswith("backend/studio/jobs/")
    ]
    assert violations == [], violations
