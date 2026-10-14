"""Storyloom Studio — 新 worker 租约执行与恢复（JOB-03，附录 09 §4/§5）。

职责：**领取（claim）/ 租约（lease）/ 心跳（heartbeat）/ 执行 / 结果落库 / 恢复**。
不含任何生成协议细节——那归各处理器卡（JOB-08/09/11/12）经 `handlers` 注入。

三条关键语义（卡面验收）：

1. **互斥**：claim 是**单语句条件 UPDATE**，以「UPDATE 影响行数」仲裁。只有把
   `pending`（或**租约已过期**的 `running`）行成功更新为本次 worker 的行才算抢到；
   影响行数为 0 即未抢到。**绝不**用「先 SELECT 再 UPDATE」两步写法（有竞态）。
2. **过期 claim 不能写当前结果**：写终态前**再次**条件校验「仍持有且未过期」，
   失配即**丢弃本次结果**（不写 job/attempt 终态），返回 `"lease_lost"`。
3. **崩溃恢复不盲目重发收费请求**：attempt 已有 `provider_request_id` → 恢复**只轮询**
   该号，绝不重新提交；无厂商号且结果不确定 → `indeterminate`，不自动重试（R08）。

不 import 任何旧后端模块（R11）；不调用旧 author_flow。
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ...core.db import Session, make_ulid
from ..contracts.models import CommandRecord  # noqa: F401  (保持与本域模型同源导入)
from .models import JobAttempt, JobEvent, StudioJob

__all__ = [
    "LEASE_SECONDS",
    "HEARTBEAT_SECONDS",
    "HandlerResult",
    "JobContext",
    "JobRunner",
    "default_handlers",
    "register_handler",
]

# 附录 09 §5 冻结：租约 60s，worker 每 15s 心跳续期；租约过期 = 可被重新 claim
LEASE_SECONDS = 60.0
HEARTBEAT_SECONDS = 15.0

# 收费 kind 不自动重试（附录 09 §4）；本地 kind 可自动重试 1 次
_PAID_KINDS = ("shot_generate", "asset_generate")
_ATTEMPT_LIMIT_PAID = 1
_ATTEMPT_LIMIT_LOCAL = 3  # 首次 + 1 次自动重试 + 冗余（恢复路径仍不重发收费请求）

# 处理器注册表（import 即空；由 default_handlers/register_handler 填充）
_HANDLERS: dict = {}


@dataclass
class HandlerResult:
    """处理器返回值（冻结形状）。"""

    status: str  # succeeded | failed | indeterminate
    provider_request_id: str | None = None
    usage: str | None = None
    cost: str | None = None
    error_tail: str | None = None


@dataclass
class JobContext:
    """传给处理器的上下文（冻结形状）。"""

    job: StudioJob
    attempt: JobAttempt
    payload: dict
    session: object
    worker_id: str
    resume: bool = False  # True = 恢复路径（已有 provider_request_id，只许轮询）
    extra: dict = field(default_factory=dict)


def register_handler(job_kind: str, fn) -> None:
    """按 job kind 注册处理器。"""
    _HANDLERS[job_kind] = fn


def default_handlers() -> dict:
    """5 个 DB 冻结 kind 的**显式桩**处理器。

    真正的生成/转码/导出/发布逻辑归 JOB-08/09/11/12；本卡只证明「按 kind 注册」
    这一机制可用。桩**明确失败**（而不是假装成功）：不调供应商、不写业务表。
    """
    def _stub(kind):
        def handler(ctx: JobContext) -> HandlerResult:
            if ctx.resume:
                # 恢复路径：桩没有真实轮询能力 → 明确 indeterminate（绝不假装完成）
                return HandlerResult(
                    status="indeterminate",
                    error_tail=f"{kind}: 恢复路径无轮询实现（处理器归 JOB-08/09/11/12）",
                )
            return HandlerResult(
                status="failed",
                error_tail=f"{kind}: 处理器未实现（归 JOB-08/09/11/12）",
            )

        return handler

    return {k: _stub(k) for k in
            ("shot_generate", "asset_generate", "transcode", "export", "release_publish")}


class JobRunner:
    """单进程 worker：原子 claim → 执行 → 条件写终态。"""

    def __init__(self, *, worker_id: str, handlers: dict | None = None,
                 now=None, session_factory=None):
        self.worker_id = worker_id
        self.handlers = dict(handlers if handlers is not None else default_handlers())
        self._now = now or time.time
        self._session_factory = session_factory or Session

    # ── 内部 ──

    def _session(self):
        return self._session_factory()

    def _claim(self, session, now: float):
        """原子抢占一个可运行 job（单语句条件 UPDATE 仲裁）。"""
        # 候选：pending，或 running 且租约已过期（本地恢复手段）
        candidate = session.scalar(
            select(StudioJob.id)
            .where(
                (StudioJob.status == "pending")
                | (
                    (StudioJob.status == "running")
                    & (StudioJob.lease_expires_at.is_not(None))
                    & (StudioJob.lease_expires_at < now)
                )
            )
            .order_by(StudioJob.priority.desc(), StudioJob.created_at.asc())
            .limit(1)
        )
        if candidate is None:
            return None
        # 单语句原子抢占：影响行数 == 1 才算抢到（0 = 被他人抢先/条件已变）
        result = session.execute(
            update(StudioJob)
            .where(
                StudioJob.id == candidate,
                (StudioJob.status == "pending")
                | (
                    (StudioJob.status == "running")
                    & (StudioJob.lease_expires_at.is_not(None))
                    & (StudioJob.lease_expires_at < now)
                ),
            )
            .values(
                status="running",
                lease_owner=self.worker_id,
                lease_expires_at=now + LEASE_SECONDS,
                attempts=StudioJob.attempts + 1,
            )
        )
        if result.rowcount != 1:
            return None  # 未抢到：交给下一个轮询
        session.flush()
        return session.get(StudioJob, candidate)

    def _open_attempt(self, session, job: StudioJob, now: float) -> JobAttempt:
        attempt = JobAttempt(
            id=make_ulid(),
            job_id=job.id,
            seq=job.attempts,
            lease_owner=self.worker_id,
            started_at=now,
            client_request_id=make_ulid(),
            status="running",
        )
        session.add(attempt)
        session.flush()
        session.add(
            JobEvent(
                id=make_ulid(), job_id=job.id, type="claimed", at=now,
                detail=f'{{"worker_id":"{self.worker_id}","attempt_seq":{attempt.seq}}}',
            )
        )
        return attempt

    def _holds_lease(self, session, job: StudioJob, now: float) -> bool:
        """写终态前的**再校验**：仍持有该租约且未过期。"""
        fresh = session.get(StudioJob, job.id)
        return (
            fresh is not None
            and fresh.lease_owner == self.worker_id
            and fresh.status == "running"
            and fresh.lease_expires_at is not None
            and fresh.lease_expires_at >= now
        )

    def heartbeat(self, session, job_id: str, worker_id: str | None = None) -> bool:
        """续期：只有仍持有租约者可续。返回是否续期成功。"""
        wid = worker_id or self.worker_id
        now = self._now()
        result = session.execute(
            update(StudioJob)
            .where(
                StudioJob.id == job_id,
                StudioJob.lease_owner == wid,
                StudioJob.status == "running",
            )
            .values(lease_expires_at=now + LEASE_SECONDS)
        )
        return result.rowcount == 1

    # ── 主流程 ──

    def claim_and_run_one(self, session) -> str | None:
        """抢一个 job 并执行；返回 job id（或 None = 无可运行的）。"""
        now = self._now()
        job = self._claim(session, now)
        if job is None:
            session.rollback()
            return None
        attempt = self._open_attempt(session, job, now)
        session.commit()

        import json as _json

        try:
            payload = _json.loads(job.payload)
        except (TypeError, ValueError):
            payload = {}

        # 恢复路径判定：已知厂商任务号（本 attempt 或既有 attempt 任一）
        known_provider_id = session.scalar(
            select(JobAttempt.provider_request_id)
            .where(
                JobAttempt.job_id == job.id,
                JobAttempt.provider_request_id.is_not(None),
            )
            .limit(1)
        )
        resume = known_provider_id is not None
        if resume:
            attempt.provider_request_id = known_provider_id
            session.commit()

        handler = self.handlers.get(job.kind)
        ctx = JobContext(job=job, attempt=attempt, payload=payload,
                         session=session, worker_id=self.worker_id, resume=resume)

        if handler is None:
            result = HandlerResult(
                status="failed", error_tail=f"无处理器：job kind {job.kind!r} 未注册"
            )
        else:
            try:
                result = handler(ctx)
            except Exception as exc:  # 处理器异常不炸 worker
                result = HandlerResult(status="failed", error_tail=f"{type(exc).__name__}: {exc}")

        self._finish(session, job, attempt, result)
        return job.id

    def _finish(self, session, job: StudioJob, attempt: JobAttempt,
                result: HandlerResult) -> str:
        """条件写终态；租约丢失即丢弃结果。"""
        now = self._now()
        session.rollback()  # 取最新状态
        if not self._holds_lease(session, job, now):
            session.rollback()
            return "lease_lost"

        fresh = session.get(StudioJob, job.id)
        if result.provider_request_id:
            attempt.provider_request_id = result.provider_request_id
        attempt.usage = result.usage
        attempt.cost = result.cost
        attempt.error_tail = result.error_tail
        attempt.finished_at = now

        if result.status == "succeeded":
            attempt.status = "succeeded"
            fresh.status = "succeeded"
            fresh.finished_at = now
            fresh.lease_owner = None
            fresh.lease_expires_at = None
            fresh.usage = result.usage
            event = "succeeded"
        elif result.status == "indeterminate":
            # 结果不确定：独立终态，job 停在 running（不自动重试，R08）
            attempt.status = "indeterminate"
            fresh.last_error = result.error_tail
            event = "indeterminate"
        else:
            attempt.status = "failed"
            fresh.status = "failed"
            fresh.finished_at = now
            fresh.last_error = result.error_tail
            fresh.lease_owner = None
            fresh.lease_expires_at = None
            event = "failed"

        session.add(
            JobEvent(
                id=make_ulid(), job_id=job.id, type=event, at=now,
                detail=f'{{"attempt_seq":{attempt.seq}}}',
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return "lease_lost"
        return result.status

    def resume(self, session, job_id: str) -> str:
        """恢复一个已知 attempt：**只轮询**（处理器以 ctx.resume=True 区分），
        绝不重新提交收费请求。"""
        now = self._now()
        job = session.get(StudioJob, job_id)
        if job is None:
            return "not_found"
        attempt = session.scalar(
            select(JobAttempt)
            .where(JobAttempt.job_id == job_id)
            .order_by(JobAttempt.seq.desc())
            .limit(1)
        )
        if attempt is None:
            return "no_attempt"
        handler = self.handlers.get(job.kind)
        if handler is None:
            return "no_handler"
        import json as _json

        try:
            payload = _json.loads(job.payload)
        except (TypeError, ValueError):
            payload = {}
        ctx = JobContext(job=job, attempt=attempt, payload=payload, session=session,
                         worker_id=self.worker_id,
                         resume=attempt.provider_request_id is not None)
        result = handler(ctx)
        return self._finish(session, job, attempt, result)

    def run_forever(self, poll_seconds: float = 0.5, max_iterations: int | None = None) -> int:
        """单进程轮询。`max_iterations` 便于确定性退出（测试/脚本用）。"""
        iterations = 0
        while True:
            if max_iterations is not None and iterations >= max_iterations:
                return iterations
            iterations += 1
            with self._session() as session:
                self.claim_and_run_one(session)
            if max_iterations is None:
                time.sleep(poll_seconds)
