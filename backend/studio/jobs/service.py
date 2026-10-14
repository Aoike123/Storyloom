"""Storyloom Studio — 任务域服务层（附录 09 §1/§3/§4 的作业提交）。

JOB-02 只实现**提交**：服务端解析并冻结 target/input，检查就绪与计费确认，
存 `input_digest` 与 `command_id`，产出 `job` 引用。
**不实现**执行/租约/重试/取消/重做（JOB-03/05/06/07）——本卡零 attempt、零事件
（除 `created`）、零供应商调用。

实现约束（冻结）：
- 写命令走 command_id 幂等（附录 00 §3，studio_command_records）；
- 属主校验：未登录 401；项目不存在/非属主一律 404 not_found（不泄漏存在性）；
- **冻结语义（卡面「冻结目标…未来新增对象不扩大任务」）**：提交时一次性把 target
  的 revision 与该时刻的 input 快照写入 payload 与 input_digest；之后对象升版或
  新对象出现**不得**改变已提交 job 的 payload/digest/ref_id；
- id 全部服务端 ULID（core.db.make_ulid）；
- 错误一律 raise StudioAPIError（冻结错误协议，附录 00 §4）。

**kind 枚举以 DB CheckConstraint 为准**：`studio_jobs.kind` 只有
`('shot_generate','asset_generate','transcode','export','release_publish')`。
附录 09 §3 的 kind 注册表更宽（含本地 kind），但宽于 DB 约束的值**无法入库**；
本卡取**可强制执行的窄集**，缺口记入账本 implementation_notes（附录与 DB 的
kind 集不一致属契约级问题，须主模型统一后才可扩）。
"""
import hashlib
import json
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ...infrastructure.providers.base import missing_env
from ..assets.models import Asset
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..edits.models import ConfirmedEdit, EditDraft
from ..media.models import MediaArtifact
from ..projects.models import StudioProject
from ..publishing.models import Release
from ..storyboard.models import Shot
from .models import JobEvent, StudioJob

__all__ = ["create_job", "canonical_payload", "input_digest"]

# DB CheckConstraint 冻结的 kind 集（见模块 docstring 的契约缺口说明）
_JOB_KINDS = (
    "shot_generate",
    "asset_generate",
    "transcode",
    "export",
    "release_publish",
)

# 计费边界（附录 09 §4）：这两类走**收费**供应商 → 需配置 + 需成本确认
_PAID_KINDS = ("shot_generate", "asset_generate")

# kind → 所需供应商**类别**（JOB-01 的 missing_env 以类别为入参，返回缺失的环境
# 变量名元组——含 PROVIDER 取值不受支持的情形，便于上层报出可操作的键名）
_PROVIDER_KIND_BY_JOB_KIND = {
    "asset_generate": "image",
    "shot_generate": "video",
}

# kind → target.kind（各 kind 的合法目标种类）
_TARGET_KIND_BY_JOB_KIND = {
    "shot_generate": "shot",
    "asset_generate": "asset",
    "transcode": "media_artifact",
    "export": "confirmed_edit",
    "release_publish": "release",
}


def canonical_payload(job_kind: str, target: dict, input_: dict) -> dict:
    """冻结后的规范输入（写 payload 列、算 input_digest 的唯一来源）。"""
    return {
        "kind": job_kind,
        "target": {
            "kind": target["kind"],
            "id": target["id"],
            "revision": target.get("revision"),
        },
        "input": input_,
    }


def input_digest(payload: dict) -> str:
    """输入指纹（确定性）：sort_keys + 紧凑分隔符 + ensure_ascii=False + sha256。"""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _violation(field: str, rule: str, message: str) -> StudioAPIError:
    return StudioAPIError.validation_failed(
        [{"field": field, "rule": rule, "message": message}]
    )


def _reused() -> StudioAPIError:
    return StudioAPIError.validation_failed(
        [
            {
                "field": "command_id",
                "rule": "reused",
                "message": "command_id 已被其他命令使用，请生成新的。",
            }
        ]
    )


def _require_project(session, user, pid) -> StudioProject:
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)
    return project


def _target_row(session, pid: str, kind: str, target_id: str):
    """按 target.kind 取目标行；不存在或跨项目 → None（由调用方映射 404 同形状）。

    `confirmed_edit` 无 project_id 列：经其 `edit_id` 反查 `studio_edit_drafts`
    的项目归属（DB-10 裁定：确认版本的归属随剪辑草稿）。
    """
    if kind == "shot":
        return session.scalar(
            select(Shot).where(Shot.id == target_id, Shot.project_id == pid)
        )
    if kind == "asset":
        return session.scalar(
            select(Asset).where(Asset.id == target_id, Asset.project_id == pid)
        )
    if kind == "media_artifact":
        return session.scalar(
            select(MediaArtifact).where(
                MediaArtifact.id == target_id, MediaArtifact.project_id == pid
            )
        )
    if kind == "release":
        return session.scalar(
            select(Release).where(Release.id == target_id, Release.project_id == pid)
        )
    if kind == "confirmed_edit":
        confirmed = session.scalar(
            select(ConfirmedEdit).where(ConfirmedEdit.id == target_id)
        )
        if confirmed is None:
            return None
        draft = session.scalar(
            select(EditDraft).where(
                EditDraft.edit_id == confirmed.edit_id, EditDraft.project_id == pid
            )
        )
        return confirmed if draft is not None else None
    return None


def _to_dto(job: StudioJob, payload: dict) -> dict:
    """作业 DTO（主模型冻结）。"""
    return {
        "object_ref": {"kind": "job", "id": job.id, "revision": None},
        "kind": job.kind,
        "status": job.status,
        "target": {
            "kind": job.ref_kind,
            "id": job.ref_id,
            "revision": (payload.get("target") or {}).get("revision"),
        },
        "input_digest": input_digest(payload),
        "payload": payload,
        "priority": job.priority,
        "attempts": job.attempts,
        "created_at": job.created_at,
        "submitted_by": job.submitted_by,
        "project_id": job.project_id,
    }


def _replay_payload(record: CommandRecord, payload: dict) -> dict | None:
    """重放命中判定：首次 payload 的 kind 与 target 与本次请求等价。"""
    stored = json.loads(record.result_payload) if record.result_payload else {}
    ref = stored.get("object_ref") or {}
    if not isinstance(ref.get("id"), str):
        return None
    if stored.get("kind") != payload["kind"]:
        return None
    stored_target = stored.get("target") or {}
    want = payload["target"]
    if stored_target.get("kind") != want["kind"] or stored_target.get("id") != want["id"]:
        return None
    if stored_target.get("revision") != want["revision"]:
        return None
    if (stored.get("payload") or {}).get("input") != payload["input"]:
        return None
    return dict(stored, _replay=True)


def create_job(
    session, user, pid: str, job_kind, target, input_, priority, confirm_cost, command_id
) -> dict:
    """提交作业（附录 09 §1/§3/§4）。校验序见模块与派发规格；返回作业 DTO。

    重放命中时附内部标记 ``_replay=True``（路由层 pop 后以 200 返回原 payload）。
    """
    _require_project(session, user, pid)

    # 2) kind 枚举（DB CheckConstraint 冻结窄集）
    if job_kind not in _JOB_KINDS:
        raise _violation(
            "kind", "enum", "kind 必须是 shot_generate/asset_generate/transcode/export/release_publish 之一。"
        )

    # 3) target 形状
    if not isinstance(target, dict):
        raise _violation("target", "invalid", "target 必须是 {kind,id,revision} 对象。")
    t_kind = target.get("kind")
    t_id = target.get("id")
    if not isinstance(t_kind, str) or not t_kind:
        raise _violation("target", "invalid", "target.kind 必须是非空字符串。")
    if not isinstance(t_id, str) or not t_id:
        raise _violation("target", "invalid", "target.id 必须是非空字符串。")

    # kind 与 target.kind 的配对（否则 「export」可指向任意对象）
    expected_target_kind = _TARGET_KIND_BY_JOB_KIND[job_kind]
    if t_kind != expected_target_kind:
        raise _violation(
            "target",
            "mismatch",
            f"kind={job_kind} 的 target.kind 必须是 {expected_target_kind}。",
        )

    # 3b) input 形状
    if not isinstance(input_, dict):
        raise _violation("input", "invalid", "input 必须是对象。")

    # 3c) priority
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise _violation("priority", "invalid", "priority 必须是整数。")

    # 4) 目标存在且属本项目（不存在/跨项目同形状 404，不泄漏存在性）
    if _target_row(session, pid, t_kind, t_id) is None:
        raise StudioAPIError.not_found(t_kind, t_id)

    # 5) 计费边界（附录 09 §4）
    provider_kind = _PROVIDER_KIND_BY_JOB_KIND.get(job_kind)
    missing = missing_env(provider_kind) if provider_kind else ()
    if missing:
        raise StudioAPIError.precondition_failed(
            "provider_not_configured",
            {"kind": "job_kind", "id": job_kind, "revision": None},
            f"该任务所需供应商未配置，缺少：{', '.join(missing)}。",
        )
    if job_kind in _PAID_KINDS and confirm_cost is not True:
        raise StudioAPIError.precondition_failed(
            "cost_not_confirmed",
            {"kind": "job_kind", "id": job_kind, "revision": None},
            "该任务为收费生成，须显式确认成本后再提交。",
        )

    # 冻结输入
    frozen = canonical_payload(job_kind, target, input_)

    # 6) 幂等（先于写）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        replay = _replay_payload(record, frozen)
        if replay is not None:
            return replay
        raise _reused()

    # 7) 写（单事务）：StudioJob + JobEvent(created) + CommandRecord
    now = time.time()
    job_id = make_ulid()
    job = StudioJob(
        id=job_id,
        project_id=pid,
        owner_id=user.id,
        kind=job_kind,
        ref_kind=t_kind,
        ref_id=t_id,
        payload=json.dumps(frozen, ensure_ascii=False),
        status="pending",
        priority=priority,
        created_at=now,
        submitted_by=user.id,
        attempts=0,
    )
    dto = _to_dto(job, frozen)
    session.commit()  # 结束只读事务；写阶段另起事务
    try:
        with session.begin():
            session.add(job)
            session.flush()  # 先持久化父行：UOW 不保证跨表 FK 插入序
            session.add(
                JobEvent(
                    id=make_ulid(),
                    job_id=job_id,
                    type="created",
                    at=now,
                    detail=json.dumps({"kind": job_kind}, ensure_ascii=False),
                )
            )
            session.add(
                CommandRecord(
                    id=make_ulid(),
                    owner_id=user.id,
                    project_id=pid,
                    command_id=command_id,
                    result_payload=json.dumps(dto, ensure_ascii=False),
                    created_at=now,
                )
            )
    except IntegrityError:
        # UQ(owner_id, command_id) 竞态：并发赢家已提交 → 返回其首次结果（重放语义）
        session.rollback()
        record = session.scalar(
            select(CommandRecord).where(
                CommandRecord.owner_id == user.id,
                CommandRecord.command_id == command_id,
            )
        )
        if record is not None:
            replay = _replay_payload(record, frozen)
            if replay is not None:
                return replay
        raise StudioAPIError.internal()

    return dto
