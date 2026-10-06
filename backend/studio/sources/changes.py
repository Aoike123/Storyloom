"""Storyloom Studio — 正文变更域服务层（附录 01 §2 S4/S5 的服务端实现）。

SOURCE-09 交付 S4 preview（kind=source_activate 的激活影响预览）；
SOURCE-10 交付 S5 apply（显式应用新正文：仅切换 active_source_revision_id）。

实现约束（冻结）：
- 正文更新只支持整篇重导入 + 激活预览切换（decisions.md 2026-10-04）；
  不提供局部文本 diff 编辑命令，impact 一律按整篇语义计算——范围永不
  自动重映射（无论新旧文本 diff 是插字/删选区/移段/重复字串，不计算、
  不声称任何新范围）；
- preview 本身是一次幂等写（00 §5）；除 ChangePreview + CommandRecord
  外零业务副作用（不切换 active 指针、不动任何片段/版本行）；
- 幂等/属主 404 不泄漏/错误协议同 fragment_service.py 头注；
- 跨域 impact 计算只可 import backend/studio/reviews/impact.py 的
  compute_impact（不可改）；跨片段并集 helper 在本文件内。
"""
import hashlib
import json
import time

from sqlalchemy import select, update

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..projects.models import StudioProject
from ..projects.service import _to_dto
from ..reviews.impact import compute_impact
from ..reviews.models import ChangePreview, ReviewDecision
from .fragment_models import Fragment, FragmentRevision
from .models import SourceRevision

__all__ = ["preview_source_activate", "apply_source_activate"]

# 预览 TTL 30 分钟（附录 07：服务层计算后写入；与 fragment_service._PREVIEW_TTL 同值）
_PREVIEW_TTL = 1800


def _revision_fragments(session, pid, source_revision_id) -> list:
    """绑定指定正文版本的全部非退役 Fragment（按 id 排序，确定性）。"""
    frags = session.scalars(
        select(Fragment).where(
            Fragment.project_id == pid,
            Fragment.source_revision_id == source_revision_id,
            Fragment.state != "retired",
        )
    ).all()
    return sorted(frags, key=lambda f: f.id)


def _current_revision(session, frag) -> int:
    """片段当前 revision 号（current_revision_id 所指 FragmentRevision 行）。"""
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    return rv.revision


def _union_entries(parts: list[list]) -> list:
    """S4 预览 impact 并集（本文件内 helper；impact.py 不可改）：
    按 object_ref (kind, id) 去重（保留先见条目），按 object_ref.id 排序。"""
    merged: list = []
    seen: set = set()
    for entries in parts:
        for entry in entries:
            ref = entry.get("object_ref") or {}
            ident = (ref.get("kind"), ref.get("id"))
            if ident in seen:
                continue
            seen.add(ident)
            merged.append(entry)
    merged.sort(key=lambda x: x["object_ref"]["id"])
    return merged


def preview_source_activate(
    session,
    user,
    pid,
    kind,
    target_revision_id,
    command_id,
) -> dict:
    """S4 预览正文版本激活（附录 01 §2 S4 + 00 §5；preview 本身是一次幂等写）。

    校验顺序（冻结，按序短路；幂等位置=上述校验之后、写之前——SOURCE-05-b 裁定 1）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. kind != "source_activate" → 422 validation_failed(field=kind, rule=invalid)
       （整篇语义：不提供局部文本 diff 编辑命令）；
    3. target 不存在或跨项目 → 422 validation_failed(field=target_revision_id,
       rule=not_found)（S4 行冻结 422——body 参数不走路径参数的 404 约定；
       "不存在"与"属别的项目"同形状，不泄漏存在性）；
    4. target == project.active_source_revision_id → 422 precondition_failed
       (reason=already_active，blocked_by={source_revision, target, None})；
    5. 幂等（00 §3）：(owner_id, command_id) 记录存在 → 其 result_payload.kind
       =='source_activate' 且对应 ChangePreview.payload 的 target_revision_id
       ==本次 target = 重放：返回首次 result_payload（零写零 commit，附内部
       标记 ``_replay=True``）；否则 422 validation_failed(field=command_id,
       rule=reused)（文案同 F 系既有）；
    6. 读事务快照 + impact 计算（附录 01 §2 激活语义 bullets 的可执行冻结；
       记 O=原 active id、T=target id）：
       - affected[]：绑定 T 的全部非退役 Fragment（当前 revision 号）各一条
         {object_ref:{kind:"fragment",id,revision}}，再并集每个 T 片段的
         compute_impact(session,pid,f).affected（片段自身+其引用对象链）；
       - needs_review[]：绑定 O 的全部非退役 Fragment → {object_ref:{kind:
         "fragment",id,revision}, reason:"cross_version_mapping"}（整篇语义：
         范围永不自动重映射——一律入此，不得计算或声称任何新范围）；并集
         每个 O 片段的 compute_impact(...).needs_review（job 项，reason=
         "frozen_input_contains_fragment"）；
       - preservable[]：并集每个 O 片段的 compute_impact(...).preservable；
       三清单均按 object_ref (kind,id) 去重（保留先见条目）、按 id 排序；
       空 → 空列表，键不省略；空表/缺表降级语义由 compute_impact 保证；
       写事务单事务：INSERT ChangePreview（kind='source_activate'，payload=
       {"target_revision_id":T}，baseline={"active_revision_id":O,
       "target_revision_id":T,"target_canonical_hash":T 行 canonical_hash}，
       impact，state='pending'，decision_id=None，created_at=now，
       resolved_at=None，expires_at=now+1800）+ INSERT CommandRecord
       （result_payload=body），无半次保存。

    返回 200 体，**恰 4 字段**（00 §5.1 冻结）：
    ``{preview_id, kind, baseline, impact}``（记录 state=pending 不外露）；
    重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) kind 仅支持 source_activate（整篇语义，无局部文本 diff 编辑命令）
    if kind != "source_activate":
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "kind",
                    "rule": "invalid",
                    "message": "kind 仅支持 source_activate。",
                }
            ]
        )

    # 3) target 须存在且属本项目（S4 行冻结 422；与"不存在"同形状，不泄漏）
    target = session.scalar(
        select(SourceRevision).where(
            SourceRevision.id == target_revision_id,
            SourceRevision.project_id == pid,
        )
    )
    if target is None:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "target_revision_id",
                    "rule": "not_found",
                    "message": "指定正文版本不存在。",
                }
            ]
        )

    # 4) target 已是当前活跃版本 → 422 precondition_failed
    if p.active_source_revision_id == target_revision_id:
        raise StudioAPIError.precondition_failed(
            "already_active",
            {"kind": "source_revision", "id": target_revision_id, "revision": None},
            "该正文版本已是当前活跃版本。",
        )

    # 5) 幂等（00 §3；裁定 1 位置=上述校验之后、写之前）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if payload.get("kind") == "source_activate":
            pv = session.get(ChangePreview, payload.get("preview_id"))
            pv_payload = (
                json.loads(pv.payload) if (pv is not None and pv.payload) else {}
            )
            if pv_payload.get("target_revision_id") == target_revision_id:
                # 重放：返回首次 result_payload，零写零 commit
                return dict(payload, _replay=True)
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "command_id",
                    "rule": "reused",
                    "message": "command_id 已被其他命令使用，请生成新的。",
                }
            ]
        )

    # 6) 读事务快照 + impact 计算（整篇语义；O=原 active id、T=target id。
    #    O 必非空——target 存在即项目已有版本，首版导入已自动激活）
    old_id = p.active_source_revision_id
    old_frags = _revision_fragments(session, pid, old_id)
    new_frags = _revision_fragments(session, pid, target_revision_id)

    # affected[]：T 片段各一条（当前 revision 号）＋ 每个 T 片段的引用对象链
    affected_parts: list[list] = [
        [
            {
                "object_ref": {
                    "kind": "fragment",
                    "id": f.id,
                    "revision": _current_revision(session, f),
                }
            }
            for f in new_frags
        ]
    ]
    for f in new_frags:
        affected_parts.append(compute_impact(session, pid, f.id)["affected"])

    # needs_review[]：O 片段各一条（cross_version_mapping，不假称任何新范围）
    # ＋ 每个 O 片段的冻结输入 job；preservable[]：每个 O 片段的历史产物
    needs_parts: list[list] = [
        [
            {
                "object_ref": {
                    "kind": "fragment",
                    "id": f.id,
                    "revision": _current_revision(session, f),
                },
                "reason": "cross_version_mapping",
            }
            for f in old_frags
        ]
    ]
    preservable_parts: list[list] = []
    for f in old_frags:
        imp = compute_impact(session, pid, f.id)
        needs_parts.append(imp["needs_review"])
        preservable_parts.append(imp["preservable"])

    impact = {
        "affected": _union_entries(affected_parts),
        "needs_review": _union_entries(needs_parts),
        "preservable": _union_entries(preservable_parts),
    }
    baseline = {
        "active_revision_id": old_id,
        "target_revision_id": target_revision_id,
        "target_canonical_hash": target.canonical_hash,
    }
    session.commit()  # 结束读事务；写阶段另起事务

    # 写事务：preview 是一次幂等写（单事务 + CommandRecord，无半次保存；
    # 除本两表外零业务副作用——不切换 active 指针、不动任何片段/版本行）
    now = time.time()
    preview_id = make_ulid()
    body = {
        "preview_id": preview_id,
        "kind": "source_activate",
        "baseline": baseline,
        "impact": impact,
    }
    with session.begin():
        session.add(
            ChangePreview(
                id=preview_id,
                project_id=pid,
                owner_id=user.id,
                kind="source_activate",
                payload=json.dumps(
                    {"target_revision_id": target_revision_id}, ensure_ascii=False
                ),
                baseline=json.dumps(baseline, ensure_ascii=False, sort_keys=True),
                impact=json.dumps(impact, ensure_ascii=False, sort_keys=True),
                state="pending",
                decision_id=None,
                created_at=now,
                resolved_at=None,
                expires_at=now + _PREVIEW_TTL,
            )
        )
        session.add(
            CommandRecord(
                id=make_ulid(),
                owner_id=user.id,
                project_id=pid,
                command_id=command_id,
                result_payload=json.dumps(body, ensure_ascii=False),
                created_at=now,
            )
        )
    return body


# ───────────────────── S5 应用正文版本激活（附录 01 §2 S5 + 00 §5） ─────────────────────


def apply_source_activate(
    session,
    user,
    pid,
    preview_id,
    command_id,
    expected_active_revision_id,
) -> dict:
    """S5 应用正文版本激活（附录 01 §2 S5 + 00 §5；apply 仅切换
    active_source_revision_id——激活不删除任何版本、不移动任何范围）。

    校验顺序（冻结，按序短路；幂等先于状态检查——SOURCE-05-b 裁定 1）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. preview 不存在或跨项目 → 404 not_found(kind=preview)；
       kind≠'source_activate' → 422 validation_failed(field=preview_id,
       rule=mismatch)；
    3. 从 preview.payload 取 target_revision_id；baseline=json 解析
       preview.baseline；
    4. 幂等（00 §3）：(owner_id, command_id) 记录存在 → 其 result_payload
       （项目 DTO）满足 id==pid 且 active_source_revision_id==target 且
       expected_active_revision_id==baseline["active_revision_id"] = 重放：
       返回首次 result_payload（零写零 commit，附 ``_replay=True``）；
       否则 422 validation_failed(field=command_id, rule=reused)（文案同
       F 系既有）；
    5. preview 非 pending：'expired' 或 (pending 且 now>expires_at) → 409
       preview_stale{preview_id, conflicts:[{object:{preview,id},
       expected:'pending', actual:'expired'}]} 且惰性置 state='expired'
       （独立提交，F 系同形，00 §5.4）；'applied'/'rejected'/'superseded'
       → 409 同形（终态不可逆，不再改写）；
    6. CAS 先报（在 baseline 比对之前）：expected_active_revision_id ≠ 当前
       project.active_source_revision_id → 409 revision_conflict(object=
       {project,pid})；
    7. 写事务：首语句 no-op 自赋值抢锁（UPDATE studio_projects SET
       updated_at=updated_at WHERE id=pid，同 F 系）→ WAL 重读 project →
       baseline 比对：当前 active ≠ baseline["active_revision_id"] → preview
       置 superseded+resolved_at（独立提交）→ 409 preview_stale（conflicts
       恰 1：{object:{project,pid}, expected:baseline.active, actual:当前}）；
       target 版本不可变（无 UPDATE 路径），无需重比 hash；
    8. 执行（单事务，无半次保存；写序冻结：project 更新 → ReviewDecision
       INSERT → flush → preview 更新 → CommandRecord——ReviewDecision 的
       INSERT 必须先于 preview.decision_id 写入，该 FK 非 deferrable）：
       project(active_source_revision_id=target, updated_at=now) →
       ReviewDecision(decision='applied', preview_id, target_ref=
       {source_revision, target, None}, baseline_digest=sha256(规范化
       preview.baseline)) → preview(state='applied', resolved_at,
       decision_id) → CommandRecord(result_payload=切换后项目 DTO——对
       session 内已更新的 project 对象构造，保证 CommandRecord 内即最终形状)。

    返回 200 体（附录 01 P3 全字段形状项目 DTO，active_source_revision_id==
    target）；重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) preview：不存在或跨项目 → 404（不泄漏）；kind 不符 → 422 mismatch
    preview = session.scalar(
        select(ChangePreview).where(
            ChangePreview.id == preview_id, ChangePreview.project_id == pid
        )
    )
    if preview is None:
        raise StudioAPIError.not_found("preview", preview_id)
    if preview.kind != "source_activate":
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "preview_id",
                    "rule": "mismatch",
                    "message": "preview_id 对应的预览不是正文激活预览。",
                }
            ]
        )

    # 3) preview 冻结参数：payload 取 target；baseline 解析（幂等比对/digest 共用）
    pv_payload = json.loads(preview.payload) if preview.payload else {}
    target_revision_id = pv_payload.get("target_revision_id")
    base = json.loads(preview.baseline) if preview.baseline else {}

    # 4) 幂等（00 §3；先于状态检查——原命令重放不受预览终态阻断，裁定 1）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if (
            payload.get("id") == pid
            and payload.get("active_source_revision_id") == target_revision_id
            and expected_active_revision_id == base.get("active_revision_id")
        ):
            # 重放：返回首次 result_payload，零写零 commit
            return dict(payload, _replay=True)
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "command_id",
                    "rule": "reused",
                    "message": "command_id 已被其他命令使用，请生成新的。",
                }
            ]
        )

    # 5) 状态检查：非 pending（或 pending 已过 TTL）→ 409 preview_stale
    now = time.time()
    expired = preview.state == "expired" or (
        preview.state == "pending" and now > preview.expires_at
    )
    if preview.state != "pending" or expired:
        session.commit()  # 结束读事务（惰性置位需独立事务）
        if expired and preview.state == "pending":
            with session.begin():
                session.execute(
                    update(ChangePreview)
                    .where(ChangePreview.id == preview_id)
                    .values(state="expired", resolved_at=now)
                )
        actual = "expired" if expired else preview.state
        err = StudioAPIError.preview_stale(
            preview_id, "预览已过期或状态已变化，请重新预览。"
        )
        err.details["conflicts"] = [
            {
                "object": {"kind": "preview", "id": preview_id},
                "expected": "pending",
                "actual": actual,
            }
        ]
        raise err

    # 6) CAS 先报（在 baseline 比对之前，冲突先报）：expected ≠ 当前 active
    if expected_active_revision_id != p.active_source_revision_id:
        raise StudioAPIError.revision_conflict(
            "project",
            pid,
            expected_active_revision_id,
            p.active_source_revision_id,
            "当前正文版本已变化，请刷新后重试。",
        )
    session.commit()  # 结束读事务；写阶段另起事务

    # 7) 写事务：首条语句即写语句（no-op 自赋值抢 DB 级写锁，同 F 系）→ 获锁后
    # WAL 重读 → baseline 比对（00 §5.2 写事务内为权威）；target 版本不可变
    # （无 UPDATE 路径），无需重比 hash
    now = time.time()
    decision_id = make_ulid()
    if not session.in_transaction():
        session.begin()
    try:
        session.execute(
            update(StudioProject)
            .where(StudioProject.id == pid)
            .values(updated=StudioProject.updated)
        )
        # 获锁后重读（WAL：写事务可见此前全部已提交数据）
        p_now = session.get(StudioProject, pid)
        cur_active = p_now.active_source_revision_id
        if cur_active != base["active_revision_id"]:
            # baseline 已变：预览 → superseded（惰性终态，独立提交）后 409
            session.execute(
                update(ChangePreview)
                .where(ChangePreview.id == preview_id)
                .values(state="superseded", resolved_at=now)
            )
            session.commit()
            err = StudioAPIError.preview_stale(
                preview_id, "预览基准已变化（当前正文版本），请重新预览。"
            )
            err.details["conflicts"] = [
                {
                    "object": {"kind": "project", "id": pid},
                    "expected": base["active_revision_id"],
                    "actual": cur_active,
                }
            ]
            raise err

        # 8) 执行（单事务，无半次保存；激活仅切换 active 指针——不删除任何版本、
        # 不移动任何范围、不重生成/迁移任何任务或媒体）
        p_now.active_source_revision_id = target_revision_id
        p_now.updated = now
        session.flush()  # 写序：project UPDATE 先于 ReviewDecision INSERT
        session.add(
            ReviewDecision(
                id=decision_id,
                project_id=pid,
                owner_id=user.id,
                preview_id=preview_id,
                target_ref=json.dumps(
                    {
                        "kind": "source_revision",
                        "id": target_revision_id,
                        "revision": None,
                    },
                    ensure_ascii=False,
                ),
                decision="applied",
                baseline_digest=hashlib.sha256(
                    json.dumps(base, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ).hexdigest(),
                created_at=now,
            )
        )
        session.flush()  # ReviewDecision 先 INSERT（FK preview.decision_id→decisions.id 非 deferrable）
        session.execute(
            update(ChangePreview)
            .where(ChangePreview.id == preview_id)
            .values(state="applied", resolved_at=now, decision_id=decision_id)
        )
        body = _to_dto(p_now)  # 切换后最终形状（CommandRecord 内即最终形状）
        session.add(
            CommandRecord(
                id=make_ulid(),
                owner_id=user.id,
                project_id=pid,
                command_id=command_id,
                result_payload=json.dumps(body, ensure_ascii=False),
                created_at=now,
            )
        )
        session.commit()
    except StudioAPIError:
        raise
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        raise
    return body
