"""Storyloom Studio — 片段域服务层（附录 02 §2 F1–F10 的服务端实现）。

SOURCE-03..08 逐子卡填充（F1 原子保存候选 → F4/F5 改名/摘要 → F6 确认 →
F7 退役 → F8 边界 → F9 拆分 → F10 合并）。

实现约束（冻结）：
- 重叠校验 = 写事务内全量比对（范围集合 CAS 的唯一写入点：
  F1/F8/F9/F10 的 apply 改 cas_revision，+1/写；其余命令不动 cas）；
- 候选与确认共同参与重叠校验（R04），边界相接合法，退役者不参与；
- 任何写命令要求 fragment 绑定版本 == 项目 active，否则
  precondition_failed(reason=source_revision_inactive)；
- 父+子同 flush 必须显式 flush 父行（UOW 不保证跨表插入序，01-a 裁定）;
- 幂等/属主 404 不泄漏/错误协议同 projects/service.py 头注。
"""
import hashlib
import json
import re
import time

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..projects.models import StudioProject
from ..reviews.impact import compute_impact
from ..reviews.models import ChangePreview, ReviewDecision
from .fragment_models import Fragment, FragmentRevision, RangeSet
from .models import SourceRevision
from .ranges import ranges_overlap, utf16_len, validate_range

__all__ = [
    "create_fragment",
    "confirm_fragment",
    "rename_fragment",
    "preview_fragment_retire",
    "apply_fragment_retire",
    "preview_fragment_boundary",
    "apply_fragment_boundary",
    "preview_fragment_split",
    "apply_fragment_split",
    "preview_fragment_merge",
    "apply_fragment_merge",
    "get_fragment",
    "list_fragments",
]

# F1 写事务内的 CAS 冲突 message（面向用户，给出定位信息）
_CAS_MSG = "范围集合版本已变化，请刷新片段列表后重试。"


def create_fragment(
    session,
    user,
    pid,
    source_revision_id,
    range_start,
    range_end,
    name,
    expected_range_set_revision,
    command_id,
) -> dict:
    """F1 原子保存候选片段（附录 02 §2 F1，DB-02 集合 CAS 唯一写入点）。

    校验顺序（冻结，按序短路）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. source_revision 非本项目 → 404 not_found(kind=source)；存在但 ≠ 项目
       active → 422 precondition_failed(reason=source_revision_inactive，
       blocked_by.id = active id 或 null，裁定)；
    3. name：strip 后空 → required；>120 → max_length；保存 trimmed（裁定，同 P1）；
    4. range 对 active 修订 canonical 文本跑 validate_range：
       empty/out_of_bounds/surrogate_split→proxy_split/blank；TypeError→non_integer
       → 422 range_invalid（message 含 canonical 长度）；
    5. 幂等（附录 00 §3，同 P1/S1 模式）：(owner_id, command_id) 记录存在 →
       请求体一致（source_revision_id+range+name）= 重放：返回首次
       result_payload 并附内部标记 ``_replay=True``（路由转 200，零写零 commit）；
       不一致 → 422 validation_failed(rule=reused)；
    6. range_set CAS 预检（读事务内）：expected=null 且已有行 → 409；
       expected=N 且（无行或 cas≠N）→ 409 revision_conflict；
    7. 写事务：首条语句即写语句以获取 DB 级写锁——无行则 INSERT 建集合
       （cas=1；UQ 冲突=并发赢家 → IntegrityError → 重读赢家映射 409），
       有行则 no-op UPDATE 后重读行并重验 CAS（WAL：获锁写事务可见此前全部
       已提交数据）→ 全量重叠比对（state≠retired，当前版本范围，严格 < 判定，
       边界相接合法）→ 冲突 409 range_overlap（全部冲突，message 点名首个）→
       无冲突则 range_set cas+1、Fragment(candidate)、FragmentRevision(1,
       created)、CommandRecord 同事务写入。任何异常 → 回滚，无半次保存。

    返回 201 体（重放时附 ``_replay=True``）。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 来源修订：须为本项目修订；存在但非 active → precondition_failed
    sr = session.scalar(
        select(SourceRevision).where(
            SourceRevision.id == source_revision_id,
            SourceRevision.project_id == pid,
        )
    )
    if sr is None:
        raise StudioAPIError.not_found("source", source_revision_id)
    if p.active_source_revision_id != source_revision_id:
        raise StudioAPIError.precondition_failed(
            "source_revision_inactive",
            {"kind": "source", "id": p.active_source_revision_id, "revision": None},
            "该正文版本不是项目当前活跃版本，不能基于它创建片段。",
        )

    # 3) name：trim 后校验，保存 trimmed（裁定，同 P1）
    name_s = name.strip()
    violations: list[dict[str, str]] = []
    if not name_s:
        violations.append({"field": "name", "rule": "required", "message": "片段名不能为空。"})
    if len(name_s) > 120:
        violations.append({"field": "name", "rule": "max_length", "message": "片段名最长 120 个字符。"})
    if violations:
        raise StudioAPIError.validation_failed(violations)

    # 4) range：active 修订 canonical 文本上的权威判定
    canonical = sr.canonical_content
    n = utf16_len(canonical)
    try:
        verdict = validate_range(canonical, range_start, range_end)
    except TypeError:  # 非 int（含 bool）入参
        verdict = "non_integer"
    if verdict != "ok":
        rule = "proxy_split" if verdict == "surrogate_split" else verdict
        messages = {
            "empty": f"范围为空：end 必须大于 start（正文长度 {n} 个 UTF-16 单位）。",
            "out_of_bounds": f"范围越界：正文长度 {n} 个 UTF-16 单位。",
            "proxy_split": f"范围边界劈开了代理对（正文长度 {n} 个 UTF-16 单位）。",
            "blank": f"范围内容全为空白（正文长度 {n} 个 UTF-16 单位）。",
            "non_integer": f"范围端点必须为整数（正文长度 {n} 个 UTF-16 单位）。",
        }
        raise StudioAPIError.range_invalid(rule, messages[rule])

    # 5) 幂等（附录 00 §3，同 P1/S1 模式）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        fid0 = (payload.get("object_ref") or {}).get("id")
        frag0 = session.scalar(select(Fragment).where(Fragment.id == fid0)) if fid0 else None
        if (
            frag0 is not None
            and frag0.source_revision_id == source_revision_id
            and payload.get("name") == name_s
            and payload.get("range") == {"start": range_start, "end": range_end}
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

    # 6) range_set CAS 预检（(pid, source_revision_id) 唯一集合行）
    S = session.scalar(
        select(RangeSet).where(
            RangeSet.project_id == pid,
            RangeSet.source_revision_id == source_revision_id,
        )
    )
    s_id = S.id if S is not None else None
    cas = S.cas_revision if S is not None else None
    if expected_range_set_revision is None and cas is not None:
        raise StudioAPIError.revision_conflict(
            "range_set", s_id, None, cas, _CAS_MSG
        )
    if expected_range_set_revision is not None and (cas is None or cas != expected_range_set_revision):
        raise StudioAPIError.revision_conflict(
            "range_set",
            (s_id if S is not None else None),
            expected_range_set_revision,
            cas,
            _CAS_MSG,
        )
    session.commit()  # 结束读事务；写阶段另起事务

    # 7) 写事务：首条语句即写语句（DB 级写锁）→ CAS 重验 → 重叠全量比对 → 写
    now = time.time()
    frag_id = make_ulid()
    rev_id = make_ulid()
    body = {
        "object_ref": {"kind": "fragment", "id": frag_id, "revision": 1},
        "name": name_s,
        "summary": None,
        "state": "candidate",
        "range": {"start": range_start, "end": range_end},
        "created_at": now,
    }
    try:
        with session.begin():
            if s_id is None:
                # 无行：INSERT 即抢占写锁（cas=1 惰性创建，同事务）；
                # 并发赢家已提交 → UQ(project_id, source_revision_id) 冲突
                new_set = RangeSet(
                    id=make_ulid(),
                    project_id=pid,
                    source_revision_id=source_revision_id,
                    cas_revision=1,
                    created_at=now,
                )
                session.add(new_set)
                session.flush()
                set_id = new_set.id
            else:
                # 行存在：no-op UPDATE 抢占写锁（阻塞至并发写提交）；
                # WAL 下获锁写事务可见此前全部已提交数据 → 重读行并重验 CAS
                session.execute(
                    update(RangeSet)
                    .where(RangeSet.id == s_id)
                    .values(cas_revision=RangeSet.cas_revision)
                )
                row = session.execute(
                    select(RangeSet.id, RangeSet.cas_revision).where(RangeSet.id == s_id)
                ).first()
                if row is None:  # 理论上不可达（集合行无删除命令）
                    raise StudioAPIError.internal()
                set_id, cas_now = row
                if cas_now != expected_range_set_revision:
                    raise StudioAPIError.revision_conflict(
                        "range_set", set_id, expected_range_set_revision, cas_now, _CAS_MSG
                    )
                session.execute(
                    update(RangeSet)
                    .where(RangeSet.id == set_id)
                    .values(cas_revision=expected_range_set_revision + 1)
                )

            # 重叠校验：同集合全部未退役片段，当前版本范围，严格 < 判定
            frags = session.scalars(
                select(Fragment).where(
                    Fragment.range_set_id == set_id,
                    Fragment.state != "retired",
                )
            ).all()
            conflicts: list[dict[str, object]] = []
            for f in sorted(frags, key=lambda x: x.id):
                rv = session.get(FragmentRevision, f.current_revision_id)
                if ranges_overlap((range_start, range_end), (rv.range_start, rv.range_end)):
                    conflicts.append(
                        {
                            "fragment_id": f.id,
                            "name": f.name,
                            "start": rv.range_start,
                            "end": rv.range_end,
                        }
                    )
            if conflicts:
                first = conflicts[0]
                raise StudioAPIError.range_overlap(
                    conflicts,
                    f"该范围与已有片段「{first['name']}」（{first['fragment_id']}）重叠。",
                )

            # 写序：先 add+flush range_set（父行），再 fragment 与 revision
            # （deferred FK 环同事务先后皆可），最后 CommandRecord
            if s_id is None:
                new_set.cas_revision += 1  # 首次写后 = 2；此后每次 F1 成功 +1
                session.flush()
            fragment = Fragment(
                id=frag_id,
                project_id=pid,
                source_revision_id=source_revision_id,
                range_set_id=set_id,
                name=name_s,
                summary=None,
                state="candidate",
                current_revision_id=rev_id,
                created_at=now,
                updated_at=now,
                retired_at=None,
            )
            revision = FragmentRevision(
                id=rev_id,
                fragment_id=frag_id,
                revision=1,
                source_revision_id=source_revision_id,
                range_start=range_start,
                range_end=range_end,
                reason="created",
                predecessor_fragment_ids="[]",
                created_at=now,
            )
            session.add(fragment)
            session.add(revision)
            session.flush()
            command = CommandRecord(
                id=make_ulid(),
                owner_id=user.id,
                project_id=pid,
                command_id=command_id,
                result_payload=json.dumps(body, ensure_ascii=False),
                created_at=now,
            )
            session.add(command)
        # with 块结束 → commit；任何异常 → 回滚，无半次保存（附录 00 §3）
    except IntegrityError:
        # 上下文管理器已回滚。UQ 冲突 = 并发赢家已建/改该集合：
        # 重读赢家行，映射 revision_conflict（调用方可携新 expected 重试）
        winner = session.scalar(
            select(RangeSet).where(
                RangeSet.project_id == pid,
                RangeSet.source_revision_id == source_revision_id,
            )
        )
        if winner is not None:
            raise StudioAPIError.revision_conflict(
                "range_set", winner.id, expected_range_set_revision, winner.cas_revision, _CAS_MSG
            )
        raise
    return body


def confirm_fragment(
    session,
    user,
    pid,
    fid,
    expected_revision,
    command_id,
) -> dict:
    """F6 确认片段（附录 02 §2 F6；不启动生产、不表示全文覆盖，R04）。

    校验顺序（冻结，按序短路）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. 片段不存在**或**不属本项目 → 一律 404 not_found(kind=fragment)（不泄漏）；
    3. fragment.source_revision_id != project.active_source_revision_id →
       422 precondition_failed(reason=source_revision_inactive，
       blocked_by.id = active id 或 null，同 F1)；
    4. 幂等（附录 00 §3，同 F1 模式）：(owner_id, command_id) 记录存在 →
       result_payload 的 object_ref 与本请求一致（id==fid 且
       revision==expected_revision，即与首次一致）= 重放：返回首次
       result_payload 并附内部标记 ``_replay=True``（路由转 200，零写零 commit）；
       不一致 → 422 validation_failed(rule=reused)；
    5. CAS：expected_revision != current（fragment.current_revision_id 所指
       FragmentRevision 行的 revision 号）→ 409 revision_conflict
       (object={fragment,fid}, expected, actual)；
    6. 状态：fragment.state != "candidate" → 422 precondition_failed
       (reason=not_candidate，blocked_by={fragment,fid,revision=current})；
       “重复确认”= 新 command_id 再确认已确认片段 → 本条 422；
       同 command_id 重放走第 4 步幂等 200。

    写事务（单事务，无半次保存；不动 range_set cas——附录 02 §3：F4–F7
    不改范围布局；不写任何 ProductionScope——确认 ≠ 全文覆盖）：
    写序 fragment 更新（已存在行，UPDATE 即写语句）→ ReviewDecision
    (decision=applied, preview_id=None, target_ref, baseline_digest =
    sha256(规范化 JSON baseline，含范围集合 CAS 快照)) → CommandRecord
    （result_payload = 完整 200 体），同一 ``session.begin()``。
    任何异常 → 回滚，无半次保存。

    返回 200 体（F3 形状片段 DTO：state=confirmed，revision_is_active=true；
    本路径绑定版本 == active → 有效状态 = 持久状态）；重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    # 3) 绑定版本须为项目 active（同 F1）
    if frag.source_revision_id != p.active_source_revision_id:
        raise StudioAPIError.precondition_failed(
            "source_revision_inactive",
            {"kind": "source", "id": p.active_source_revision_id, "revision": None},
            "该正文版本不是项目当前活跃版本，不能基于它确认片段。",
        )

    # 4) 幂等（附录 00 §3，同 F1 模式）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        ref = payload.get("object_ref") or {}
        if ref.get("id") == fid and ref.get("revision") == expected_revision:
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

    # 5) CAS：current = current_revision_id 所指版本行的 revision 号
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    current = rv.revision
    if expected_revision != current:
        raise StudioAPIError.revision_conflict(
            "fragment",
            fid,
            expected_revision,
            current,
            "片段版本已变化，请刷新片段详情后重试。",
        )

    # 6) 状态：只有 candidate 可确认
    if frag.state != "candidate":
        raise StudioAPIError.precondition_failed(
            "not_candidate",
            {"kind": "fragment", "id": fid, "revision": current},
            "该片段当前状态不是 candidate，不能确认。",
        )

    # 读事务快照（DTO 字段与 baseline；写阶段不再回读）
    S = session.scalar(select(RangeSet).where(RangeSet.id == frag.range_set_id))
    cas = S.cas_revision if S is not None else None
    name_s = frag.name
    summary_s = frag.summary
    src_id = frag.source_revision_id
    range_set_id = frag.range_set_id
    created_at = frag.created_at
    retired_at = frag.retired_at
    rng_start = rv.range_start
    rng_end = rv.range_end
    predecessor_ids = json.loads(rv.predecessor_fragment_ids or "[]")
    session.commit()  # 结束读事务；写阶段另起事务

    # 确认基准 = 片段版本 + 绑定版本 + 集合 CAS 快照（主模型裁定）；
    # 规范化 JSON 的 sha256
    baseline = {
        "fragment_id": fid,
        "fragment_revision": current,
        "source_revision_id": src_id,
        "range_set_id": range_set_id,
        "cas_revision": cas,
    }
    baseline_digest = hashlib.sha256(
        json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    # 写事务：单事务，无半次保存；写序 fragment → ReviewDecision → CommandRecord
    now = time.time()
    body = {
        "object_ref": {"kind": "fragment", "id": fid, "revision": current},
        "name": name_s,
        "summary": summary_s,
        "state": "confirmed",
        "revision_is_active": True,
        "range": {"start": rng_start, "end": rng_end},
        "source_revision_id": src_id,
        "predecessor_ids": predecessor_ids,
        "created_at": created_at,
        "updated_at": now,
        "retired_at": retired_at,
    }
    with session.begin():
        # fragment：已存在行 UPDATE（首条语句即写语句；不动 range_set cas，
        # 不写任何 ProductionScope——确认 ≠ 全文覆盖）
        session.execute(
            update(Fragment)
            .where(Fragment.id == fid)
            .values(state="confirmed", updated_at=now)
        )
        session.add(
            ReviewDecision(
                id=make_ulid(),
                project_id=pid,
                owner_id=user.id,
                preview_id=None,
                target_ref=json.dumps(
                    {"kind": "fragment", "id": fid, "revision": current}, ensure_ascii=False
                ),
                decision="applied",
                baseline_digest=baseline_digest,
                created_at=now,
            )
        )
        session.flush()
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


def rename_fragment(
    session,
    user,
    pid,
    fid,
    name,
    expected_revision,
    command_id,
) -> dict:
    """F4 片段就地改名（附录 02 §2 F4；ID 不变、改名不产生 revision、
    不动 range_set cas——附录 02 §1/§3）。

    校验顺序（冻结，按序短路；与 F1 同序惯例）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. 片段不存在**或**不属本项目 → 一律 404 not_found(kind=fragment)（不泄漏）；
    3. fragment.source_revision_id != project.active_source_revision_id →
       422 precondition_failed(reason=source_revision_inactive，
       blocked_by.id = active id 或 null，同 F1；附录 02 §1：任何写命令
       要求 active)；
    4. name：strip 后空 → 422 validation_failed(rule=required)；>120 →
       rule=max_length；保存 trimmed（同 F1）。**name 校验先于 CAS**（冻结顺序）；
    5. 幂等（附录 00 §3，同 F1/F6 模式）：(owner_id, command_id) 记录存在 →
       result_payload 的 object_ref.id == fid 且 name == trimmed（与首次一致）
       = 重放：返回首次 result_payload 并附内部标记 ``_replay=True``（路由转
       200，零写零 commit）；不一致 → 422 validation_failed(rule=reused)；
    6. CAS：expected_revision != current（fragment.current_revision_id 所指
       FragmentRevision 行的 revision 号）→ 409 revision_conflict
       (object={fragment,fid}, expected, actual)。

    写事务（单事务，无半次保存；不动 range_set cas——附录 02 §3：F4–F7
    不改范围布局；不新建 FragmentRevision——改名不产生 revision，实体 ID
    与版本分离）：
    fragment 就地 UPDATE（name=trimmed、updated_at=now；首条语句即写语句）
    → CommandRecord（result_payload = 完整 200 体），同一 ``session.begin()``。
    任何异常 → 回滚，无半次保存。

    返回 200 体（F3 形状片段 DTO，与 F6 确认返回同形：state = 持久状态；
    本路径绑定版本 == active → 有效状态 = 持久状态，revision_is_active=true）；
    重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    # 3) 绑定版本须为项目 active（附录 02 §1：任何写命令要求 active，同 F1）
    if frag.source_revision_id != p.active_source_revision_id:
        raise StudioAPIError.precondition_failed(
            "source_revision_inactive",
            {"kind": "source", "id": p.active_source_revision_id, "revision": None},
            "该正文版本不是项目当前活跃版本，不能基于它改名片段。",
        )

    # 4) name：trim 后校验，保存 trimmed（冻结顺序：先于幂等与 CAS，同 F1）
    name_s = name.strip()
    violations: list[dict[str, str]] = []
    if not name_s:
        violations.append({"field": "name", "rule": "required", "message": "片段名不能为空。"})
    if len(name_s) > 120:
        violations.append({"field": "name", "rule": "max_length", "message": "片段名最长 120 个字符。"})
    if violations:
        raise StudioAPIError.validation_failed(violations)

    # 5) 幂等（附录 00 §3，同 F1/F6 模式）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        ref = payload.get("object_ref") or {}
        if ref.get("id") == fid and payload.get("name") == name_s:
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

    # 6) CAS：current = current_revision_id 所指版本行的 revision 号
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    current = rv.revision
    if expected_revision != current:
        raise StudioAPIError.revision_conflict(
            "fragment",
            fid,
            expected_revision,
            current,
            "片段版本已变化，请刷新片段详情后重试。",
        )

    # 读事务快照（DTO 字段；写阶段不再回读）
    summary_s = frag.summary
    state_s = frag.state
    src_id = frag.source_revision_id
    created_at = frag.created_at
    retired_at = frag.retired_at
    rng_start = rv.range_start
    rng_end = rv.range_end
    predecessor_ids = json.loads(rv.predecessor_fragment_ids or "[]")
    session.commit()  # 结束读事务；写阶段另起事务

    # 写事务：单事务，无半次保存；fragment 就地 UPDATE（首条语句即写语句；
    # 不新建 FragmentRevision——改名不产生 revision；不动 range_set cas——
    # 不改范围布局）→ CommandRecord
    now = time.time()
    body = {
        "object_ref": {"kind": "fragment", "id": fid, "revision": current},
        "name": name_s,
        "summary": summary_s,
        "state": state_s,
        "revision_is_active": True,
        "range": {"start": rng_start, "end": rng_end},
        "source_revision_id": src_id,
        "predecessor_ids": predecessor_ids,
        "created_at": created_at,
        "updated_at": now,
        "retired_at": retired_at,
    }
    with session.begin():
        session.execute(
            update(Fragment)
            .where(Fragment.id == fid)
            .values(name=name_s, updated_at=now)
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


# ─────────────────────── F7 片段退役（preview/apply，附录 02 §2 v2.8 + 00 §5） ───────────────────────

# 预览 TTL 30 分钟（附录 07：服务层计算后写入）
_PREVIEW_TTL = 1800

# F7/F8/F9/F10 预览共用的跨域 impact 计算（_raw_rows/_successor_group/compute_impact）
# 已迁至 backend/studio/reviews/impact.py；本模块经 `from ..reviews.impact import
# compute_impact` 复用（附录 02 §2 impact 计算段；确定性/缺表降级语义不变）。


def preview_fragment_retire(
    session,
    user,
    pid,
    fid,
    target_fragment_id,
    command_id,
) -> dict:
    """F7 预览退役片段（附录 02 §2 F7（v2.8）+ 00 §5；preview 本身是一次幂等写）。

    校验顺序（冻结，按序短路）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. 片段不存在或跨项目 → 404 not_found(kind=fragment)（不泄漏）；
    3. target_fragment_id != fid → 422 validation_failed(rule=mismatch，裁定)；
    4. 绑定版本 ≠ 项目 active → 422 precondition_failed(reason=source_revision_inactive，
       blocked_by={source, active id 或 null, null})；
    5. state == 'retired' → 422 precondition_failed(reason=already_retired，
       blocked_by={fragment, fid, current})（**422**，v2.8 修正）；
    6. 幂等（00 §3）：(owner, command_id) 记录存在 → 与首次一致（kind=fragment_retire
       且 baseline.fragment.id==fid）= 重放：返回首次 200 体并附内部标记 ``_replay=True``
       （路由转 200，零写零 commit）；不一致 → 422 validation_failed(rule=reused)；
    7. 创建 ChangePreview（单事务 + CommandRecord）：kind='fragment_retire'、
       payload={target_fragment_id: fid}、baseline 冻结（fragment 当前 revision +
       range_set id/cas）、impact（三类清单，_compute_impact）、state='pending'、
       expires_at=now+1800（30 分钟 TTL，服务层计算后写入，附录 07）。

    返回 200 体，**恰 4 字段**（00 §5.1 冻结）：
    ``{preview_id, kind, baseline, impact}``（记录 state=pending 不外露）；
    重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    # 3) target 必须与路径片段一致（裁定）
    if target_fragment_id != fid:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "target_fragment_id",
                    "rule": "mismatch",
                    "message": "target_fragment_id 必须与路径片段一致。",
                }
            ]
        )

    # 4) 绑定版本须为项目 active（附录 02 §1：任何写命令要求 active）
    if frag.source_revision_id != p.active_source_revision_id:
        raise StudioAPIError.precondition_failed(
            "source_revision_inactive",
            {"kind": "source", "id": p.active_source_revision_id, "revision": None},
            "该正文版本不是项目当前活跃版本，不能基于它退役片段。",
        )

    # 5) 已退役 → 422（v2.8 修正：409→422）
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    current = rv.revision
    if frag.state == "retired":
        raise StudioAPIError.precondition_failed(
            "already_retired",
            {"kind": "fragment", "id": fid, "revision": current},
            "该片段已退役，不能重复退役。",
        )

    # 6) 幂等（00 §3）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        base = payload.get("baseline") or {}
        if (
            payload.get("kind") == "fragment_retire"
            and (base.get("fragment") or {}).get("id") == fid
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

    # 读事务快照（baseline 冻结；impact 计算）
    S = session.scalar(select(RangeSet).where(RangeSet.id == frag.range_set_id))
    baseline = {
        "fragment": {"kind": "fragment", "id": fid, "revision": current},
        "range_set": {
            "id": S.id if S is not None else None,
            "cas": S.cas_revision if S is not None else None,
        },
    }
    impact = compute_impact(session, pid, fid)
    session.commit()  # 结束读事务；写阶段另起事务

    # 7) 写事务：preview 是一次幂等写（单事务 + CommandRecord，无半次保存）
    now = time.time()
    preview_id = make_ulid()
    body = {
        "preview_id": preview_id,
        "kind": "fragment_retire",
        "baseline": baseline,
        "impact": impact,
    }
    with session.begin():
        session.add(
            ChangePreview(
                id=preview_id,
                project_id=pid,
                owner_id=user.id,
                kind="fragment_retire",
                payload=json.dumps({"target_fragment_id": fid}, ensure_ascii=False),
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


def apply_fragment_retire(
    session,
    user,
    pid,
    fid,
    preview_id,
    command_id,
    expected_revision,
    expected_range_set_revision,
) -> dict:
    """F7 应用退役片段（附录 02 §2 F7 + 00 §5；退役只置状态，不删除任何产物——
    R11 失效≠删除）。

    校验顺序（按序短路）：
    1. 项目 404；2. 片段 404（不泄漏）；
    3. preview 不存在或跨项目 → 404 not_found(kind=preview)；kind≠'fragment_retire'
       或 payload 的 target≠fid → 422 validation_failed(rule=mismatch，同 A3 形状)；
    4. 幂等（00 §3；置于状态检查之前——原命令重放不受预览终态阻断，T43 裁定）：
       (owner, command_id) 记录存在 → 与首次一致（object_ref.id==fid 且
       revision==expected_revision 且 state='retired'）= 重放 200 原 result_payload
       （零写零 commit）；不一致 → 422 validation_failed(rule=reused)；
    5. preview 非 pending：'expired' 或 (pending 且 now>expires_at) → 409
       preview_stale{preview_id, conflicts:[{object:{preview,id}, expected:'pending',
       actual:'expired'}]} 且惰性置 state='expired'（00 §5.4）；
       'applied'/'rejected'/'superseded' → 409 preview_stale 同形（actual=state，
       终态不可逆，不再改写）；
    6. CAS（在 baseline 比对之前，冲突先报）：expected_revision≠current → 409
       revision_conflict(object={fragment,fid})；expected_range_set_revision≠current
       cas → 409 revision_conflict(object={range_set,id})（仅失配/失鲜检测——退役
       不 +1，附录 02 §3：F4–F7 不动 cas）；
    7. baseline 比对（写事务内重读为权威，失配项全部收集，00 §5.2）：fragment
       revision / range_set cas 失配 → 409 preview_stale{preview_id,
       conflicts:[全部]} 且预览置 'superseded'+resolved_at；
    8. 执行（单事务，无半次保存；写序 fragment 更新 → ReviewDecision INSERT →
       preview 更新 → CommandRecord——ReviewDecision 的 INSERT 必须先于
       preview.decision_id 写入，该 FK 非 deferrable）：
       fragment(state='retired', retired_at=updated_at=now) → ReviewDecision
       (decision='applied', preview_id, target_ref, baseline_digest=sha256(规范化
       preview.baseline)) → preview(state='applied', resolved_at, decision_id) →
       CommandRecord(result_payload=完整 200 体)。

    返回 200 体（F3 形状片段 DTO：state='retired'、retired_at=now）；
    重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    # 3) preview：不存在或跨项目 → 404（裁定 kind=preview）
    preview = session.scalar(
        select(ChangePreview).where(
            ChangePreview.id == preview_id, ChangePreview.project_id == pid
        )
    )
    if preview is None:
        raise StudioAPIError.not_found("preview", preview_id)
    pv_payload = json.loads(preview.payload) if preview.payload else {}
    if preview.kind != "fragment_retire" or pv_payload.get("target_fragment_id") != fid:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "target_fragment_id",
                    "rule": "mismatch",
                    "message": "target_fragment_id 必须与路径片段一致。",
                }
            ]
        )

    # 4) 幂等（00 §3；先于状态检查——原命令重放不受预览终态阻断，T43）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        ref = payload.get("object_ref") or {}
        if (
            ref.get("id") == fid
            and ref.get("revision") == expected_revision
            and payload.get("state") == "retired"
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

    # 6) 读阶段：当前 revision + cas（CAS 检查在 baseline 比对之前，冲突先报）
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    current = rv.revision
    S = session.scalar(select(RangeSet).where(RangeSet.id == frag.range_set_id))
    set_id = S.id if S is not None else None
    cas = S.cas_revision if S is not None else None
    if expected_revision != current:
        raise StudioAPIError.revision_conflict(
            "fragment",
            fid,
            expected_revision,
            current,
            "片段版本已变化，请刷新片段详情后重试。",
        )
    if expected_range_set_revision != cas:
        raise StudioAPIError.revision_conflict(
            "range_set",
            set_id,
            expected_range_set_revision,
            cas,
            "范围集合版本已变化，请刷新片段列表后重试。",
        )

    # 读事务快照（DTO 字段 + baseline；写阶段不再回读这些字段）
    name_s = frag.name
    summary_s = frag.summary
    src_id = frag.source_revision_id
    created_at = frag.created_at
    rng_start = rv.range_start
    rng_end = rv.range_end
    predecessor_ids = json.loads(rv.predecessor_fragment_ids or "[]")
    revision_is_active = src_id == p.active_source_revision_id
    base = json.loads(preview.baseline) if preview.baseline else {}
    session.commit()  # 结束读事务；写阶段另起事务

    # 7) 写事务：首条语句即写语句（no-op 自赋值抢 DB 级写锁，同 F1）→ 获锁后
    # 重读 → baseline 比对（00 §5.2 写事务内为权威，失配项全部收集）
    now = time.time()
    decision_id = make_ulid()
    body = {
        "object_ref": {"kind": "fragment", "id": fid, "revision": current},
        "name": name_s,
        "summary": summary_s,
        "state": "retired",
        "revision_is_active": revision_is_active,
        "range": {"start": rng_start, "end": rng_end},
        "source_revision_id": src_id,
        "predecessor_ids": predecessor_ids,
        "created_at": created_at,
        "updated_at": now,
        "retired_at": now,
    }
    if not session.in_transaction():
        session.begin()
    try:
        session.execute(
            update(Fragment)
            .where(Fragment.id == fid)
            .values(updated_at=Fragment.updated_at)
        )
        # 获锁后重读（WAL：写事务可见此前全部已提交数据）
        frag_now = session.get(Fragment, fid)
        rv_now = session.get(FragmentRevision, frag_now.current_revision_id)
        S_now = session.scalar(
            select(RangeSet).where(RangeSet.id == frag_now.range_set_id)
        )
        cur_rev = rv_now.revision
        cur_cas = S_now.cas_revision if S_now is not None else None
        conflicts: list[dict] = []
        if base["fragment"]["revision"] != cur_rev:
            conflicts.append(
                {
                    "object": {"kind": "fragment", "id": fid},
                    "expected": base["fragment"]["revision"],
                    "actual": cur_rev,
                }
            )
        if (
            base["range_set"]["id"] != (S_now.id if S_now is not None else None)
            or base["range_set"]["cas"] != cur_cas
        ):
            conflicts.append(
                {
                    "object": {"kind": "range_set", "id": base["range_set"]["id"]},
                    "expected": base["range_set"]["cas"],
                    "actual": cur_cas,
                }
            )
        if conflicts:
            # baseline 已变：预览 → superseded（惰性终态，独立提交）后 409
            session.execute(
                update(ChangePreview)
                .where(ChangePreview.id == preview_id)
                .values(state="superseded", resolved_at=now)
            )
            session.commit()
            err = StudioAPIError.preview_stale(
                preview_id,
                "预览基准已变化（片段版本或范围集合版本），请重新预览。",
            )
            err.details["conflicts"] = conflicts
            raise err

        # 8) 执行（单事务，无半次保存；退役只置状态——R11 失效≠删除；
        # range_set 不加 1——附录 02 §3：F4–F7 不动 cas，释放的空间经 state
        # 过滤动态可见）
        session.execute(
            update(Fragment)
            .where(Fragment.id == fid)
            .values(state="retired", retired_at=now, updated_at=now)
        )
        session.add(
            ReviewDecision(
                id=decision_id,
                project_id=pid,
                owner_id=user.id,
                preview_id=preview_id,
                target_ref=json.dumps(
                    {"kind": "fragment", "id": fid, "revision": cur_rev},
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


# ───────────────────── F8 片段边界变更（preview/apply，附录 02 §2 F8 + 00 §5） ─────────────────────


def preview_fragment_boundary(
    session,
    user,
    pid,
    fid,
    target_fragment_id,
    new_range,
    command_id,
) -> dict:
    """F8 预览片段边界变更（附录 02 §2 F8 + 00 §5；preview 本身是一次幂等写）。

    校验顺序（冻结，按序短路；幂等位置 404/mismatch 之后、其余之前——SOURCE-05-b 裁定 1）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. 片段不存在或跨项目 → 404 not_found(kind=fragment)（不泄漏）；
    3. target_fragment_id != fid → 422 validation_failed(rule=mismatch，F7 同款文案)；
    4. 绑定版本 ≠ 项目 active → 422 precondition_failed(reason=source_revision_inactive，
       blocked_by={source, active id 或 null, null})（"版本非 active"在 preview 阶段即拒）；
    5. 幂等（00 §3）：(owner, command_id) 记录存在 → 与首次一致（target 与 new_range
       均相同）= 重放 200 原 result_payload（零写零 commit，附 ``_replay``）；
       不一致 → 422 validation_failed(rule=reused)；
    6. range 合法性：new_range 对绑定修订 canonical 文本跑 validate_range（五判定，
       同 F1 verdict 映射：surrogate_split→proxy_split；TypeError→non_integer）→
       422 range_invalid（message 含 canonical 文本长度，同 F1 文案惯例）；
    7. 重叠（preview 阶段即查，F8 行冻结）：与 range_set 中除本片段外所有非退役
       片段的当前版本范围逐一 ranges_overlap 严格判定（相接合法）→ 409 range_overlap
       {conflicts:[全部，按 id 排序], message 点名首个}（F1 同款形状）；
    8. 创建 ChangePreview + CommandRecord（单事务，F7 同款）：kind='fragment_boundary'、
       payload={target_fragment_id: fid, new_range:{start,end}}、baseline 与 F7 同形
       （fragment 当前 revision + range_set id/cas）、impact=compute_impact(...)、
       state='pending'、expires_at=now+1800。

    返回 200 体，**恰 4 字段**（00 §5.1 冻结）：
    ``{preview_id, kind, baseline, impact}``；重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    # 3) target 必须与路径片段一致（F7 同款文案）
    if target_fragment_id != fid:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "target_fragment_id",
                    "rule": "mismatch",
                    "message": "target_fragment_id 必须与路径片段一致。",
                }
            ]
        )

    # 4) 绑定版本须为项目 active（F8 行冻结：preview 阶段即拒）
    if frag.source_revision_id != p.active_source_revision_id:
        raise StudioAPIError.precondition_failed(
            "source_revision_inactive",
            {"kind": "source", "id": p.active_source_revision_id, "revision": None},
            "该正文版本不是项目当前活跃版本，不能基于它修改片段边界。",
        )

    # 5) 幂等（00 §3；404/mismatch 之后、其余之前）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if payload.get("kind") == "fragment_boundary":
            pv = session.get(ChangePreview, payload.get("preview_id"))
            pv_payload = (
                json.loads(pv.payload) if (pv is not None and pv.payload) else {}
            )
            orig_range = pv_payload.get("new_range") or {}
            if (
                pv_payload.get("target_fragment_id") == fid
                and orig_range.get("start") == new_range["start"]
                and orig_range.get("end") == new_range["end"]
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

    # 读事务快照：canonical（range 合法性）+ current revision / range_set（baseline）
    sr = session.scalar(
        select(SourceRevision).where(SourceRevision.id == frag.source_revision_id)
    )
    canonical = sr.canonical_content
    n = utf16_len(canonical)
    range_start = new_range["start"]
    range_end = new_range["end"]

    # 6) range 合法性（validate_range 五判定，同 F1 verdict 映射）
    try:
        verdict = validate_range(canonical, range_start, range_end)
    except TypeError:  # 非 int（含 bool）入参
        verdict = "non_integer"
    if verdict != "ok":
        rule = "proxy_split" if verdict == "surrogate_split" else verdict
        messages = {
            "empty": f"范围为空：end 必须大于 start（正文长度 {n} 个 UTF-16 单位）。",
            "out_of_bounds": f"范围越界：正文长度 {n} 个 UTF-16 单位。",
            "proxy_split": f"范围边界劈开了代理对（正文长度 {n} 个 UTF-16 单位）。",
            "blank": f"范围内容全为空白（正文长度 {n} 个 UTF-16 单位）。",
            "non_integer": f"范围端点必须为整数（正文长度 {n} 个 UTF-16 单位）。",
        }
        raise StudioAPIError.range_invalid(rule, messages[rule])

    # 7) 重叠（preview 阶段即查；除本片段外全部非退役片段，当前版本范围，严格判定）
    frags = session.scalars(
        select(Fragment).where(
            Fragment.range_set_id == frag.range_set_id,
            Fragment.state != "retired",
        )
    ).all()
    conflicts: list[dict[str, object]] = []
    for f in sorted(frags, key=lambda x: x.id):
        if f.id == fid:
            continue  # 排除本片段（F8 行冻结：只与他段比）
        rv = session.get(FragmentRevision, f.current_revision_id)
        if ranges_overlap((range_start, range_end), (rv.range_start, rv.range_end)):
            conflicts.append(
                {
                    "fragment_id": f.id,
                    "name": f.name,
                    "start": rv.range_start,
                    "end": rv.range_end,
                }
            )
    if conflicts:
        first = conflicts[0]
        raise StudioAPIError.range_overlap(
            conflicts,
            f"该范围与已有片段「{first['name']}」（{first['fragment_id']}）重叠。",
        )

    # 读事务快照（baseline 冻结 + impact 计算）
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    current = rv.revision
    S = session.scalar(select(RangeSet).where(RangeSet.id == frag.range_set_id))
    baseline = {
        "fragment": {"kind": "fragment", "id": fid, "revision": current},
        "range_set": {
            "id": S.id if S is not None else None,
            "cas": S.cas_revision if S is not None else None,
        },
    }
    impact = compute_impact(session, pid, fid)
    session.commit()  # 结束读事务；写阶段另起事务

    # 8) 写事务：preview 是一次幂等写（单事务 + CommandRecord，无半次保存）
    now = time.time()
    preview_id = make_ulid()
    body = {
        "preview_id": preview_id,
        "kind": "fragment_boundary",
        "baseline": baseline,
        "impact": impact,
    }
    with session.begin():
        session.add(
            ChangePreview(
                id=preview_id,
                project_id=pid,
                owner_id=user.id,
                kind="fragment_boundary",
                payload=json.dumps(
                    {
                        "target_fragment_id": fid,
                        "new_range": {"start": range_start, "end": range_end},
                    },
                    ensure_ascii=False,
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


def apply_fragment_boundary(
    session,
    user,
    pid,
    fid,
    preview_id,
    command_id,
    expected_revision,
    expected_range_set_revision,
) -> dict:
    """F8 应用片段边界变更（附录 02 §2 F8 + 00 §5；边界变更产生新 FragmentRevision
    reason='boundary'、ID 不变、revision+1；range_set 是 CAS 写点，cas+1——附录 02 §3：
    F1/F8/F9/F10 的 apply 是 range_set.cas_revision 唯一写点）。

    校验顺序（按序短路；幂等先于状态检查——SOURCE-05-b 裁定 1）：
    1. 项目 404；2. 片段 404（不泄漏）；
    3. preview 不存在或跨项目 → 404 not_found(kind=preview)；kind≠'fragment_boundary'
       或 payload 的 target≠fid → 422 validation_failed(rule=mismatch，F7 同款)；
    4. 幂等（00 §3；先于状态检查）：(owner, command_id) 记录存在 → 与首次一致
       （object_ref.id==fid 且 revision==expected_revision+1，即边界 apply 的新版本）
       = 重放 200 原 result_payload（零写零 commit）；不一致 → 422 reused；
    5. preview 非 pending：'expired' 或 (pending 且 now>expires_at) → 409 preview_stale
       {preview_id, conflicts:[{object:{preview,id}, expected:'pending',
       actual:'expired'}]} 且惰性置 'expired'（00 §5.4）；'applied'/'rejected'/
       'superseded' → 409 preview_stale 同形（actual=state，终态不可逆）；
    6. CAS（在 baseline 比对之前，冲突先报）：expected_revision≠current → 409
       revision_conflict(object={fragment,fid})；expected_range_set_revision≠current
       cas → 409 revision_conflict(object={range_set,id})；
    7. baseline 比对（写事务内重读为权威，失配项全部收集，00 §5.2）：fragment
       revision / range_set cas 失配 → 409 preview_stale{preview_id, conflicts:[全部]}
       且预览置 'superseded'+resolved_at；
    8. 执行（单事务，无半次保存；写序满足 FK：INSERT 新 FragmentRevision →
       UPDATE fragment → UPDATE range_set(cas+1) → INSERT ReviewDecision → UPDATE
       preview → INSERT CommandRecord）：
       新 FragmentRevision(revision=current+1, range=new_range, reason='boundary',
       predecessor=[]) → fragment(current_revision_id=新行, updated_at=now，state 不变)
       → range_set(cas+1) → ReviewDecision(decision='applied', preview_id, target_ref,
       baseline_digest=sha256(规范化 preview.baseline)) → preview(state='applied',
       resolved_at, decision_id) → CommandRecord(result_payload=完整 200 体)。

    返回 200 体（F3 形状片段 DTO：object_ref.revision=current+1、name/summary 不变、
    state=持久状态、revision_is_active=true、range={new_range}、predecessor_ids=[]、
    updated_at=now）；重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    # 3) preview：不存在或跨项目 → 404（kind=preview）
    preview = session.scalar(
        select(ChangePreview).where(
            ChangePreview.id == preview_id, ChangePreview.project_id == pid
        )
    )
    if preview is None:
        raise StudioAPIError.not_found("preview", preview_id)
    pv_payload = json.loads(preview.payload) if preview.payload else {}
    if preview.kind != "fragment_boundary" or pv_payload.get("target_fragment_id") != fid:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "target_fragment_id",
                    "rule": "mismatch",
                    "message": "target_fragment_id 必须与路径片段一致。",
                }
            ]
        )

    # 4) 幂等（00 §3；先于状态检查——原命令重放不受预览终态阻断）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        ref = payload.get("object_ref") or {}
        if ref.get("id") == fid and ref.get("revision") == expected_revision + 1:
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

    # 6) 读阶段：当前 revision + cas（CAS 检查在 baseline 比对之前，冲突先报）
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    current = rv.revision
    S = session.scalar(select(RangeSet).where(RangeSet.id == frag.range_set_id))
    set_id = S.id if S is not None else None
    cas = S.cas_revision if S is not None else None
    if expected_revision != current:
        raise StudioAPIError.revision_conflict(
            "fragment",
            fid,
            expected_revision,
            current,
            "片段版本已变化，请刷新片段详情后重试。",
        )
    if expected_range_set_revision != cas:
        raise StudioAPIError.revision_conflict(
            "range_set",
            set_id,
            expected_range_set_revision,
            cas,
            "范围集合版本已变化，请刷新片段列表后重试。",
        )

    # 读事务快照（DTO 字段 + baseline + new_range；写阶段不再回读这些字段）
    name_s = frag.name
    summary_s = frag.summary
    state_s = frag.state
    src_id = frag.source_revision_id
    created_at = frag.created_at
    retired_at = frag.retired_at
    predecessor_ids = []  # 边界变更新 revision 无 predecessor（区别于 split/merge）
    revision_is_active = src_id == p.active_source_revision_id
    new_range = pv_payload.get("new_range") or {}
    new_start = new_range.get("start")
    new_end = new_range.get("end")
    base = json.loads(preview.baseline) if preview.baseline else {}
    session.commit()  # 结束读事务；写阶段另起事务

    # 7) 写事务：首条语句即写语句（no-op 自赋值抢 DB 级写锁，同 F7）→ 获锁后
    # 重读 → baseline 比对（00 §5.2 写事务内为权威，失配项全部收集）
    now = time.time()
    decision_id = make_ulid()
    new_rev_id = make_ulid()
    body = {
        "object_ref": {"kind": "fragment", "id": fid, "revision": current + 1},
        "name": name_s,
        "summary": summary_s,
        "state": state_s,
        "revision_is_active": revision_is_active,
        "range": {"start": new_start, "end": new_end},
        "source_revision_id": src_id,
        "predecessor_ids": predecessor_ids,
        "created_at": created_at,
        "updated_at": now,
        "retired_at": retired_at,
    }
    if not session.in_transaction():
        session.begin()
    try:
        # no-op 自赋值抢 DB 级写锁（SQLite 单写者：获锁后阻塞全部并发写）
        session.execute(
            update(Fragment)
            .where(Fragment.id == fid)
            .values(updated_at=Fragment.updated_at)
        )
        # 获锁后重读（WAL：写事务可见此前全部已提交数据）
        frag_now = session.get(Fragment, fid)
        rv_now = session.get(FragmentRevision, frag_now.current_revision_id)
        S_now = session.scalar(
            select(RangeSet).where(RangeSet.id == frag_now.range_set_id)
        )
        cur_rev = rv_now.revision
        cur_cas = S_now.cas_revision if S_now is not None else None
        conflicts: list[dict] = []
        if base["fragment"]["revision"] != cur_rev:
            conflicts.append(
                {
                    "object": {"kind": "fragment", "id": fid},
                    "expected": base["fragment"]["revision"],
                    "actual": cur_rev,
                }
            )
        if (
            base["range_set"]["id"] != (S_now.id if S_now is not None else None)
            or base["range_set"]["cas"] != cur_cas
        ):
            conflicts.append(
                {
                    "object": {"kind": "range_set", "id": base["range_set"]["id"]},
                    "expected": base["range_set"]["cas"],
                    "actual": cur_cas,
                }
            )
        if conflicts:
            # baseline 已变：预览 → superseded（惰性终态，独立提交）后 409
            session.execute(
                update(ChangePreview)
                .where(ChangePreview.id == preview_id)
                .values(state="superseded", resolved_at=now)
            )
            session.commit()
            err = StudioAPIError.preview_stale(
                preview_id,
                "预览基准已变化（片段版本或范围集合版本），请重新预览。",
            )
            err.details["conflicts"] = conflicts
            raise err

        # 8) 执行（单事务，无半次保存）；写序满足 FK：新 revision 行先落（flush），
        # fragment 再指向它（deferred FK 环同事务先后皆可，显式 flush 更稳）。
        session.add(
            FragmentRevision(
                id=new_rev_id,
                fragment_id=fid,
                revision=current + 1,
                source_revision_id=src_id,
                range_start=new_start,
                range_end=new_end,
                reason="boundary",
                predecessor_fragment_ids="[]",
                created_at=now,
            )
        )
        session.flush()  # 先落新 revision 行（fragment 指向它之前须已存在）
        # fragment：current_revision_id → 新 revision（ID 不变，revision+1；
        # state 不变——边界变更不改变确认/候选状态；有效状态由 active 比较派生）
        session.execute(
            update(Fragment)
            .where(Fragment.id == fid)
            .values(current_revision_id=new_rev_id, updated_at=now)
        )
        # range_set：CAS 写点——cas+1（附录 02 §3：F1/F8/F9/F10 的 apply 唯一写 cas）
        session.execute(
            update(RangeSet)
            .where(RangeSet.id == frag_now.range_set_id)
            .values(cas_revision=cur_cas + 1)
        )
        session.add(
            ReviewDecision(
                id=decision_id,
                project_id=pid,
                owner_id=user.id,
                preview_id=preview_id,
                target_ref=json.dumps(
                    {"kind": "fragment", "id": fid, "revision": current + 1},
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


# ───────────────────── F9 片段拆分（preview/apply，附录 02 §2 F9 + 00 §5） ─────────────────────


def preview_fragment_split(
    session,
    user,
    pid,
    fid,
    target_fragment_id,
    split_point,
    left_name,
    right_name,
    command_id,
) -> dict:
    """F9 预览片段拆分（附录 02 §2 F9 + 00 §5；preview 本身是一次幂等写）。

    校验顺序（冻结，按序短路；幂等位置 404/mismatch 之后、其余之前——
    SOURCE-05-b 裁定 1）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. 片段不存在或跨项目 → 404 not_found(kind=fragment)（不泄漏）；
    3. target_fragment_id != fid → 422 validation_failed(rule=mismatch，F7 同款文案)；
    4. 绑定版本 ≠ 项目 active → 422 precondition_failed(reason=source_revision_inactive，
       blocked_by={source, active id 或 null, null})；已 retired → 422
       precondition_failed(reason=already_retired, blocked_by={fragment, fid, current})；
    5. 幂等（00 §3）：(owner, command_id) 记录存在 → 经 ChangePreview.payload 取
       首次 {target_fragment_id, split_point, left_name, right_name} 比对
       （名称按派生/trim 后有效值，同 F8 确认 2）= 重放 200 原 result_payload
       （零写零 commit，附 ``_replay``）；不一致 → 422 validation_failed(rule=reused)；
    6. 命名派生（幂等之后、range 校验之前）：left_name/right_name 缺省 → 派生
       "<原 name>（左）"/"<原 name>（右）"；派生（或给定，strip 后）须非空且
       ≤120 → 否则 422 validation_failed(rule=required/max_length)；保存 trimmed；
    7. split_point 合法性 → 422 range_invalid（冻结五判定：== 端点 → empty
       （某半空）；越出当前 range → out_of_bounds；否则对两半 validate_range，
       surrogate_split→proxy_split 映射、其余同名；message 含 canonical 长度、
       当前 range 与 split_point）；
    8. 创建 ChangePreview + CommandRecord（单事务，F7/F8 同形）：kind='fragment_split'、
       payload={target_fragment_id, split_point, left_name, right_name}（trimmed
       或派生）、baseline 同形（fragment 当前 revision + range_set id/cas）、
       impact=compute_impact(...)、state='pending'、expires_at=now+1800。

    返回 200 体，**恰 4 字段**（00 §5.1 冻结）：
    ``{preview_id, kind, baseline, impact}``；重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    # 3) target 必须与路径片段一致（F7 同款文案）
    if target_fragment_id != fid:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "target_fragment_id",
                    "rule": "mismatch",
                    "message": "target_fragment_id 必须与路径片段一致。",
                }
            ]
        )

    # 4) 绑定版本须为项目 active；已退役 → 422（F7 同形，"拆分"文案）
    if frag.source_revision_id != p.active_source_revision_id:
        raise StudioAPIError.precondition_failed(
            "source_revision_inactive",
            {"kind": "source", "id": p.active_source_revision_id, "revision": None},
            "该正文版本不是项目当前活跃版本，不能基于它拆分片段。",
        )
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    current = rv.revision
    if frag.state == "retired":
        raise StudioAPIError.precondition_failed(
            "already_retired",
            {"kind": "fragment", "id": fid, "revision": current},
            "该片段已退役，不能再次拆分。",
        )

    # 命名派生（纯计算；校验短路置于幂等之后——步骤 6）：
    # 给定名 → strip；缺省 → 派生 "<原 name>（左）"/"<原 name>（右）"
    left_s = left_name.strip() if left_name is not None else f"{frag.name}（左）"
    right_s = right_name.strip() if right_name is not None else f"{frag.name}（右）"

    # 5) 幂等（00 §3；404/mismatch 之后、其余之前）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if payload.get("kind") == "fragment_split":
            pv = session.get(ChangePreview, payload.get("preview_id"))
            pv_payload = (
                json.loads(pv.payload) if (pv is not None and pv.payload) else {}
            )
            # 与首次比对（同 F8 确认 2）：target/split_point 与派生/trim 后有效名
            # （存储 payload 即 trimmed/派生值）
            if (
                pv_payload.get("target_fragment_id") == fid
                and pv_payload.get("split_point") == split_point
                and pv_payload.get("left_name") == left_s
                and pv_payload.get("right_name") == right_s
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

    # 6) 命名派生校验（幂等之后、range 校验之前）：非空且 ≤120
    if not left_s:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "left_name",
                    "rule": "required",
                    "message": "left_name 去除首尾空白后不能为空。",
                }
            ]
        )
    if len(left_s) > 120:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "left_name",
                    "rule": "max_length",
                    "message": f"left_name 长度不得超过 120 个字符（当前 {len(left_s)}）。",
                }
            ]
        )
    if not right_s:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "right_name",
                    "rule": "required",
                    "message": "right_name 去除首尾空白后不能为空。",
                }
            ]
        )
    if len(right_s) > 120:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "right_name",
                    "rule": "max_length",
                    "message": f"right_name 长度不得超过 120 个字符（当前 {len(right_s)}）。",
                }
            ]
        )

    # 读事务快照：canonical（range 合法性）+ 当前 range（两半判定 + baseline）
    sr = session.scalar(
        select(SourceRevision).where(SourceRevision.id == frag.source_revision_id)
    )
    canonical = sr.canonical_content
    n = utf16_len(canonical)
    rng_start = rv.range_start
    rng_end = rv.range_end

    # 7) split_point 合法性（冻结五判定）：== 端点 → empty；越界 → out_of_bounds；
    #    否则对两半 validate_range（surrogate_split→proxy_split，其余同名）
    if split_point == rng_start or split_point == rng_end:
        raise StudioAPIError.range_invalid(
            "empty",
            f"split_point 等于片段范围端点，无法形成非空半段：split_point={split_point}，"
            f"当前范围 [{rng_start}, {rng_end})（正文长度 {n} 个 UTF-16 单位）。",
        )
    if split_point < rng_start or split_point > rng_end:
        raise StudioAPIError.range_invalid(
            "out_of_bounds",
            f"split_point 超出片段范围：split_point={split_point}，"
            f"当前范围 [{rng_start}, {rng_end})（正文长度 {n} 个 UTF-16 单位）。",
        )
    left_verdict = validate_range(canonical, rng_start, split_point)
    right_verdict = validate_range(canonical, split_point, rng_end)
    if left_verdict != "ok":
        which, verdict = "左半段", left_verdict
    elif right_verdict != "ok":
        which, verdict = "右半段", right_verdict
    else:
        verdict = None
    if verdict is not None:
        rule = "proxy_split" if verdict == "surrogate_split" else verdict
        messages = {
            "empty": f"{which}为空：split_point={split_point}，当前范围 [{rng_start}, {rng_end})（正文长度 {n} 个 UTF-16 单位）。",
            "out_of_bounds": f"{which}越界：split_point={split_point}，当前范围 [{rng_start}, {rng_end})（正文长度 {n} 个 UTF-16 单位）。",
            "proxy_split": f"{which}边界劈开了代理对：split_point={split_point}，当前范围 [{rng_start}, {rng_end})（正文长度 {n} 个 UTF-16 单位）。",
            "blank": f"{which}内容全为空白：split_point={split_point}，当前范围 [{rng_start}, {rng_end})（正文长度 {n} 个 UTF-16 单位）。",
            "non_integer": f"split_point 必须为整数：split_point={split_point}，当前范围 [{rng_start}, {rng_end})（正文长度 {n} 个 UTF-16 单位）。",
        }
        raise StudioAPIError.range_invalid(rule, messages[rule])

    # 读事务快照（baseline 冻结 + impact 计算）
    S = session.scalar(select(RangeSet).where(RangeSet.id == frag.range_set_id))
    baseline = {
        "fragment": {"kind": "fragment", "id": fid, "revision": current},
        "range_set": {
            "id": S.id if S is not None else None,
            "cas": S.cas_revision if S is not None else None,
        },
    }
    impact = compute_impact(session, pid, fid)
    session.commit()  # 结束读事务；写阶段另起事务

    # 8) 写事务：preview 是一次幂等写（单事务 + CommandRecord，无半次保存）
    now = time.time()
    preview_id = make_ulid()
    body = {
        "preview_id": preview_id,
        "kind": "fragment_split",
        "baseline": baseline,
        "impact": impact,
    }
    with session.begin():
        session.add(
            ChangePreview(
                id=preview_id,
                project_id=pid,
                owner_id=user.id,
                kind="fragment_split",
                payload=json.dumps(
                    {
                        "target_fragment_id": fid,
                        "split_point": split_point,
                        "left_name": left_s,
                        "right_name": right_s,
                    },
                    ensure_ascii=False,
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


def apply_fragment_split(
    session,
    user,
    pid,
    fid,
    preview_id,
    command_id,
    expected_revision,
    expected_range_set_revision,
) -> dict:
    """F9 应用片段拆分（附录 02 §2 F9 + 00 §5；原片段退役不新建其 revision，
    左/右新片段各得一行 revision=1（reason='split'、predecessor=[原 fid]）、
    state='candidate'；range_set 是 CAS 唯一写点之一，cas+1——附录 02 §3：
    F1/F8/F9/F10 的 apply 是 range_set.cas_revision 唯一写点）。

    校验顺序（按序短路；幂等先于状态检查——SOURCE-05-b 裁定 1）：
    1. 项目 404；2. 片段 404（不泄漏）；
    3. preview 不存在或跨项目 → 404 not_found(kind=preview)；kind≠'fragment_split'
       或 payload 的 target≠fid → 422 validation_failed(rule=mismatch，F7 同款)；
    4. 幂等（00 §3；先于状态检查）：(owner, command_id) 记录存在 → 与首次一致
       （original.object_ref.id==fid 且 original.object_ref.revision==expected_revision
       ——拆分不推进原 revision；且 200 体 left/right 段引用的片段行在 DB 中存在）
       = 重放 200 原 result_payload（零写零 commit）；不一致 → 422 reused；
    5. preview 非 pending：'expired' 或 (pending 且 now>expires_at) → 409 preview_stale
       {preview_id, conflicts:[{object:{preview,id}, expected:'pending',
       actual:'expired'}]} 且惰性置 'expired'（00 §5.4）；'applied'/'rejected'/
       'superseded' → 409 preview_stale 同形（actual=state，终态不可逆，不再改写）；
    6. CAS（在 baseline 比对之前，冲突先报）：expected_revision≠current → 409
       revision_conflict(object={fragment,fid})；expected_range_set_revision≠current
       cas → 409 revision_conflict(object={range_set,id})；
    7. baseline 比对（写事务内重读为权威，失配项全部收集，00 §5.2）：fragment
       revision / range_set id+cas 失配 → 409 preview_stale{preview_id, conflicts:[全部]}
       且预览置 'superseded'+resolved_at（独立提交）；
    8. 执行（单事务，无半次保存；写序冻结）：UPDATE 原片段 retired → INSERT 左
       revision（flush）→ INSERT 左 fragment → INSERT 右 revision（flush）→
       INSERT 右 fragment → UPDATE range_set（cas+1）→ INSERT ReviewDecision
       （flush）→ UPDATE preview（applied）→ INSERT CommandRecord。

    返回 200 体（F9 冻结形状：{left, right, original}；left/right 各
    object_ref(revision=1)+range；original 为 object_ref(revision=current)
    +state='retired'）；重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    # 3) preview：不存在或跨项目 → 404（kind=preview）
    preview = session.scalar(
        select(ChangePreview).where(
            ChangePreview.id == preview_id, ChangePreview.project_id == pid
        )
    )
    if preview is None:
        raise StudioAPIError.not_found("preview", preview_id)
    pv_payload = json.loads(preview.payload) if preview.payload else {}
    if preview.kind != "fragment_split" or pv_payload.get("target_fragment_id") != fid:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "target_fragment_id",
                    "rule": "mismatch",
                    "message": "target_fragment_id 必须与路径片段一致。",
                }
            ]
        )

    # 4) 幂等（00 §3；先于状态检查——原命令重放不受预览终态阻断）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        oref = (payload.get("original") or {}).get("object_ref") or {}
        left_ref = (payload.get("left") or {}).get("object_ref") or {}
        right_ref = (payload.get("right") or {}).get("object_ref") or {}
        if (
            oref.get("id") == fid
            and oref.get("revision") == expected_revision
            and left_ref.get("id")
            and right_ref.get("id")
            and session.get(Fragment, left_ref.get("id")) is not None
            and session.get(Fragment, right_ref.get("id")) is not None
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

    # 6) 读阶段：当前 revision + cas（CAS 检查在 baseline 比对之前，冲突先报）
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    current = rv.revision
    S = session.scalar(select(RangeSet).where(RangeSet.id == frag.range_set_id))
    set_id = S.id if S is not None else None
    cas = S.cas_revision if S is not None else None
    if expected_revision != current:
        raise StudioAPIError.revision_conflict(
            "fragment",
            fid,
            expected_revision,
            current,
            "片段版本已变化，请刷新片段详情后重试。",
        )
    if expected_range_set_revision != cas:
        raise StudioAPIError.revision_conflict(
            "range_set",
            set_id,
            expected_range_set_revision,
            cas,
            "范围集合版本已变化，请刷新片段列表后重试。",
        )

    # 读事务快照（执行字段 + baseline；写阶段不再回读这些字段）
    src_id = frag.source_revision_id
    rng_start = rv.range_start
    rng_end = rv.range_end
    sp = pv_payload.get("split_point")
    left_name_s = pv_payload.get("left_name")
    right_name_s = pv_payload.get("right_name")
    base = json.loads(preview.baseline) if preview.baseline else {}
    session.commit()  # 结束读事务；写阶段另起事务

    # 7) 写事务：首条语句即写语句（no-op 自赋值抢 DB 级写锁，同 F7/F8）→ 获锁后
    # 重读 → baseline 比对（00 §5.2 写事务内为权威，失配项全部收集）
    now = time.time()
    decision_id = make_ulid()
    left_id = make_ulid()
    right_id = make_ulid()
    left_rev_id = make_ulid()
    right_rev_id = make_ulid()
    body = {
        "left": {
            "object_ref": {"kind": "fragment", "id": left_id, "revision": 1},
            "range": {"start": rng_start, "end": sp},
        },
        "right": {
            "object_ref": {"kind": "fragment", "id": right_id, "revision": 1},
            "range": {"start": sp, "end": rng_end},
        },
        "original": {
            "object_ref": {"kind": "fragment", "id": fid, "revision": current},
            "state": "retired",
        },
    }
    if not session.in_transaction():
        session.begin()
    try:
        # no-op 自赋值抢 DB 级写锁（SQLite 单写者：获锁后阻塞全部并发写）
        session.execute(
            update(Fragment)
            .where(Fragment.id == fid)
            .values(updated_at=Fragment.updated_at)
        )
        # 获锁后重读（WAL：写事务可见此前全部已提交数据）
        frag_now = session.get(Fragment, fid)
        rv_now = session.get(FragmentRevision, frag_now.current_revision_id)
        S_now = session.scalar(
            select(RangeSet).where(RangeSet.id == frag_now.range_set_id)
        )
        cur_rev = rv_now.revision
        cur_cas = S_now.cas_revision if S_now is not None else None
        conflicts: list[dict] = []
        if base["fragment"]["revision"] != cur_rev:
            conflicts.append(
                {
                    "object": {"kind": "fragment", "id": fid},
                    "expected": base["fragment"]["revision"],
                    "actual": cur_rev,
                }
            )
        if (
            base["range_set"]["id"] != (S_now.id if S_now is not None else None)
            or base["range_set"]["cas"] != cur_cas
        ):
            conflicts.append(
                {
                    "object": {"kind": "range_set", "id": base["range_set"]["id"]},
                    "expected": base["range_set"]["cas"],
                    "actual": cur_cas,
                }
            )
        if conflicts:
            # baseline 已变：预览 → superseded（惰性终态，独立提交）后 409
            session.execute(
                update(ChangePreview)
                .where(ChangePreview.id == preview_id)
                .values(state="superseded", resolved_at=now)
            )
            session.commit()
            err = StudioAPIError.preview_stale(
                preview_id,
                "预览基准已变化（片段版本或范围集合版本），请重新预览。",
            )
            err.details["conflicts"] = conflicts
            raise err

        # 8) 执行（单事务，无半次保存）；写序冻结：UPDATE 原片段 retired →
        # 左 revision（flush）→ 左 fragment → 右 revision（flush）→ 右 fragment
        # → range_set cas+1 → ReviewDecision（flush）→ preview applied →
        # CommandRecord。新 revision 行先落（deferred FK 环同事务先后皆可，
        # 显式 flush 更稳）。
        session.execute(
            update(Fragment)
            .where(Fragment.id == fid)
            .values(state="retired", retired_at=now, updated_at=now)
        )
        session.add(
            FragmentRevision(
                id=left_rev_id,
                fragment_id=left_id,
                revision=1,
                source_revision_id=src_id,
                range_start=rng_start,
                range_end=sp,
                reason="split",
                predecessor_fragment_ids=json.dumps([fid], ensure_ascii=False),
                created_at=now,
            )
        )
        session.flush()  # 先落左 revision 行（fragment 指向它之前须已存在）
        session.add(
            Fragment(
                id=left_id,
                project_id=frag_now.project_id,
                source_revision_id=src_id,
                range_set_id=frag_now.range_set_id,
                name=left_name_s,
                summary=None,
                state="candidate",
                current_revision_id=left_rev_id,
                created_at=now,
                updated_at=now,
                retired_at=None,
            )
        )
        session.add(
            FragmentRevision(
                id=right_rev_id,
                fragment_id=right_id,
                revision=1,
                source_revision_id=src_id,
                range_start=sp,
                range_end=rng_end,
                reason="split",
                predecessor_fragment_ids=json.dumps([fid], ensure_ascii=False),
                created_at=now,
            )
        )
        session.flush()  # 先落右 revision 行
        session.add(
            Fragment(
                id=right_id,
                project_id=frag_now.project_id,
                source_revision_id=src_id,
                range_set_id=frag_now.range_set_id,
                name=right_name_s,
                summary=None,
                state="candidate",
                current_revision_id=right_rev_id,
                created_at=now,
                updated_at=now,
                retired_at=None,
            )
        )
        # range_set：CAS 唯一写点之一——cas+1（附录 02 §3：F1/F8/F9/F10 的 apply 唯一写 cas）
        session.execute(
            update(RangeSet)
            .where(RangeSet.id == frag_now.range_set_id)
            .values(cas_revision=cur_cas + 1)
        )
        session.add(
            ReviewDecision(
                id=decision_id,
                project_id=pid,
                owner_id=user.id,
                preview_id=preview_id,
                target_ref=json.dumps(
                    {"kind": "fragment", "id": fid, "revision": cur_rev},
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


# ───────────────────── F10 片段合并（preview/apply，附录 02 §2 F10 + 00 §5） ─────────────────────


def _merge_impacts(ia: dict, ib: dict) -> dict:
    """F10 预览 impact 并集（compute_impact 单 fid × 2，本文件内合并 helper）：
    三键（affected/needs_review/preservable）各自合并，按 object_ref (kind, id)
    去重（保留先见条目；needs_review 同去重保留 reason），按 object_ref.id 排序。"""
    merged: dict[str, list] = {"affected": [], "needs_review": [], "preservable": []}
    for key in merged:
        seen: set = set()
        for entry in (ia.get(key) or []) + (ib.get(key) or []):
            ref = entry.get("object_ref") or {}
            ident = (ref.get("kind"), ref.get("id"))
            if ident in seen:
                continue
            seen.add(ident)
            merged[key].append(entry)
        merged[key].sort(key=lambda x: x["object_ref"]["id"])
    return merged


def preview_fragment_merge(
    session,
    user,
    pid,
    fragment_ids,
    merged_name,
    command_id,
) -> dict:
    """F10 预览片段合并（附录 02 §2 F10 + 00 §5；preview 本身是一次幂等写）。

    校验顺序（冻结，按序短路；幂等位置 404/形状校验之后、其余之前——
    SOURCE-05-b 裁定 1）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. fragment_ids 长度 ≠ 2 → 422 validation_failed(field=fragment_ids,
       rule=cardinality)；两元素相同 → 422 validation_failed(rule=duplicate)；
    3. 按请求序加载 a=fragment_ids[0]、b=fragment_ids[1]：任一不存在或跨项目 →
       404 not_found(kind=fragment, id=该 id)（不泄漏）；
    4. 绑定版本 ≠ 项目 active（a→b 序首个失败即报）→ 422 precondition_failed
       (reason=source_revision_inactive，blocked_by={source, active id 或 null, null})；
    5. 已 retired（a→b 序）→ 422 precondition_failed(reason=already_retired,
       blocked_by={fragment, id, current})（F7 同形，"合并"文案）；
    6. 命名派生（纯计算）：merged_name 给定 → strip；缺省 → 派生
       f"{左name}＋{右name}"（左/右按当前 revision 的 range_start 几何排序；
       连接符为全角加号 U+FF0B）；
    7. 幂等（00 §3）：(owner, command_id) 记录存在 → 经 ChangePreview.payload
       比对 fragment_ids（**请求序精确比较**，[a,b]≠[b,a]）与 merged_name
       （派生/trim 后有效值）= 重放 200 原 result_payload（零写零 commit，
       附 ``_replay``）；不一致 → 422 validation_failed(rule=reused)；
    8. 命名校验：strip 后非空 → 否则 422 validation_failed(rule=required)；
       >120 → rule=max_length（message 含当前长度，F9 同款文案）；
    9. 相接校验：左.range_end == 右.range_start（半开区间相接）→ 否则 422
       precondition_failed(reason=not_contiguous，blocked_by={fragment, 右 id,
       右当前 revision})，message 含两片范围数值定位（**顺序按请求序报告**）；
    10. 重叠校验（"合并范围再走重叠校验"——F10 行冻结）：merged_range=
       [左 start, 右 end)；除 a、b 外同 range_set 内非退役片段的当前 revision
       与 merged_range 严格重叠（相接合法）→ 409 range_overlap（conflicts
       收集全部冲突、按 id 排序，形状同 F8 preview）；
    11. 创建 ChangePreview + CommandRecord（单事务，F9 同形）：kind=
       'fragment_merge'、payload={fragment_ids（请求序）, left_id, right_id,
       merged_name（trimmed/派生有效值）}、baseline={fragment_a, fragment_b
       （各 kind/id/revision）, range_set(id/cas)}、impact=compute_impact(a) 与
       compute_impact(b) 的并集（_merge_impacts）、state='pending'、
       expires_at=now+1800。

    返回 200 体，**恰 4 字段**（00 §5.1 冻结）：
    ``{preview_id, kind, baseline, impact}``；重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 形状：恰两个；两元素不同（按序短路）
    if len(fragment_ids) != 2:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "fragment_ids",
                    "rule": "cardinality",
                    "message": "fragment_ids 必须恰好包含两个片段 ID。",
                }
            ]
        )
    a_id, b_id = fragment_ids[0], fragment_ids[1]
    if a_id == b_id:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "fragment_ids",
                    "rule": "duplicate",
                    "message": "fragment_ids 中的两个片段 ID 不能相同。",
                }
            ]
        )

    # 3) 按请求序加载：任一不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag_a = session.scalar(
        select(Fragment).where(Fragment.id == a_id, Fragment.project_id == pid)
    )
    if frag_a is None:
        raise StudioAPIError.not_found("fragment", a_id)
    frag_b = session.scalar(
        select(Fragment).where(Fragment.id == b_id, Fragment.project_id == pid)
    )
    if frag_b is None:
        raise StudioAPIError.not_found("fragment", b_id)

    # 4) 绑定版本须为项目 active（a→b 序首个失败即报；F9 同款文案，"合并"语境）
    for f in (frag_a, frag_b):
        if f.source_revision_id != p.active_source_revision_id:
            raise StudioAPIError.precondition_failed(
                "source_revision_inactive",
                {"kind": "source", "id": p.active_source_revision_id, "revision": None},
                "该正文版本不是项目当前活跃版本，不能基于它合并片段。",
            )

    # 5) 已退役 → 422（a→b 序；F7 同形，"合并"文案）
    rv_a = session.get(FragmentRevision, frag_a.current_revision_id)
    rv_b = session.get(FragmentRevision, frag_b.current_revision_id)
    if rv_a is None or rv_b is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    for f, rv in ((frag_a, rv_a), (frag_b, rv_b)):
        if f.state == "retired":
            raise StudioAPIError.precondition_failed(
                "already_retired",
                {"kind": "fragment", "id": f.id, "revision": rv.revision},
                "该片段已退役，不能再次合并。",
            )

    # 几何左/右（按当前 revision 的 range_start 排序；同起点按 id 字典序保底
    # 确定性——同集合非退役片段经重叠校验不会同起点）
    if (rv_a.range_start, frag_a.id) <= (rv_b.range_start, frag_b.id):
        left_frag, left_rv = frag_a, rv_a
        right_frag, right_rv = frag_b, rv_b
    else:
        left_frag, left_rv = frag_b, rv_b
        right_frag, right_rv = frag_a, rv_a

    # 6) 命名派生（纯计算；校验短路置于幂等之后——步骤 8）：
    #    给定 → strip；缺省 → 派生 f"{左name}＋{右name}"（全角加号 U+FF0B）
    merged_s = (
        merged_name.strip()
        if merged_name is not None
        else f"{left_frag.name}＋{right_frag.name}"
    )

    # 7) 幂等（00 §3；404/形状校验之后、其余之前）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if payload.get("kind") == "fragment_merge":
            pv = session.get(ChangePreview, payload.get("preview_id"))
            pv_payload = (
                json.loads(pv.payload) if (pv is not None and pv.payload) else {}
            )
            # 与首次比对：fragment_ids 请求序精确比较（[a,b]≠[b,a]）与
            # 派生/trim 后有效 merged_name（存储 payload 即有效值）
            if (
                pv_payload.get("fragment_ids") == [a_id, b_id]
                and pv_payload.get("merged_name") == merged_s
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

    # 8) 命名派生校验（幂等之后、相接校验之前）：非空且 ≤120（F9 同款文案）
    if not merged_s:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "merged_name",
                    "rule": "required",
                    "message": "merged_name 去除首尾空白后不能为空。",
                }
            ]
        )
    if len(merged_s) > 120:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "merged_name",
                    "rule": "max_length",
                    "message": f"merged_name 长度不得超过 120 个字符（当前 {len(merged_s)}）。",
                }
            ]
        )

    # 9) 相接校验（半开区间相接：左.range_end == 右.range_start）；
    #    message 两片范围按**请求序**报告
    if left_rv.range_end != right_rv.range_start:
        raise StudioAPIError.precondition_failed(
            "not_contiguous",
            {
                "kind": "fragment",
                "id": right_frag.id,
                "revision": right_rv.revision,
            },
            f"两个片段的范围不相接，无法合并：[{rv_a.range_start}, {rv_a.range_end}) "
            f"与 [{rv_b.range_start}, {rv_b.range_end})。",
        )

    # 10) 重叠校验（合并范围再走重叠校验；除 a、b 外同集合非退役片段，
    #     当前版本范围，严格判定，相接合法——形状同 F8 preview）
    merged_start, merged_end = left_rv.range_start, right_rv.range_end
    frags = session.scalars(
        select(Fragment).where(
            Fragment.range_set_id == frag_a.range_set_id,
            Fragment.state != "retired",
        )
    ).all()
    conflicts: list[dict[str, object]] = []
    for f in sorted(frags, key=lambda x: x.id):
        if f.id in (a_id, b_id):
            continue  # 排除待合并的两片（F8 同款：只与他段比）
        rv = session.get(FragmentRevision, f.current_revision_id)
        if ranges_overlap((merged_start, merged_end), (rv.range_start, rv.range_end)):
            conflicts.append(
                {
                    "fragment_id": f.id,
                    "name": f.name,
                    "start": rv.range_start,
                    "end": rv.range_end,
                }
            )
    if conflicts:
        first = conflicts[0]
        raise StudioAPIError.range_overlap(
            conflicts,
            f"该范围与已有片段「{first['name']}」（{first['fragment_id']}）重叠。",
        )

    # 读事务快照（baseline 冻结 + impact 并集计算）
    S = session.scalar(select(RangeSet).where(RangeSet.id == frag_a.range_set_id))
    baseline = {
        "fragment_a": {"kind": "fragment", "id": a_id, "revision": rv_a.revision},
        "fragment_b": {"kind": "fragment", "id": b_id, "revision": rv_b.revision},
        "range_set": {
            "id": S.id if S is not None else None,
            "cas": S.cas_revision if S is not None else None,
        },
    }
    impact = _merge_impacts(
        compute_impact(session, pid, a_id), compute_impact(session, pid, b_id)
    )
    session.commit()  # 结束读事务；写阶段另起事务

    # 11) 写事务：preview 是一次幂等写（单事务 + CommandRecord，无半次保存）
    now = time.time()
    preview_id = make_ulid()
    body = {
        "preview_id": preview_id,
        "kind": "fragment_merge",
        "baseline": baseline,
        "impact": impact,
    }
    with session.begin():
        session.add(
            ChangePreview(
                id=preview_id,
                project_id=pid,
                owner_id=user.id,
                kind="fragment_merge",
                payload=json.dumps(
                    {
                        "fragment_ids": [a_id, b_id],
                        "left_id": left_frag.id,
                        "right_id": right_frag.id,
                        "merged_name": merged_s,
                    },
                    ensure_ascii=False,
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


def apply_fragment_merge(
    session,
    user,
    pid,
    preview_id,
    command_id,
    expected_revision_a,
    expected_revision_b,
    expected_range_set_revision,
) -> dict:
    """F10 应用片段合并（附录 02 §2 F10 + 00 §5；a/b 退役不新建其 revision，
    merged 新 ID 得一行 revision=1（reason='merge'、predecessor=[a,b] 请求序）、
    state='candidate'；range_set 是 CAS 唯一写点之一，cas+1——附录 02 §3：
    F1/F8/F9/F10 的 apply 是 range_set.cas_revision 唯一写点）。

    校验顺序（按序短路；幂等先于状态检查——SOURCE-05-b 裁定 1，
    SOURCE-07 确认 2 的双原片变体）：
    1. 项目 404；preview 不存在或跨项目 → 404 not_found(kind=preview)；
    2. preview.kind ≠ 'fragment_merge' → 422 validation_failed(field=preview_id,
       rule=mismatch)；
    3. 从 preview.payload 取 fragment_ids/left_id/right_id/merged_name；加载
       a、b 行（域内无删除，理论必存在；缺失 → internal_error）；
    4. 幂等（00 §3；先于状态检查）：(owner, command_id) 记录存在 → 其
       result_payload 满足 predecessors[0].object_ref.id==a_id 且
       revision==expected_revision_a、predecessors[1].object_ref.id==b_id 且
       revision==expected_revision_b、且 merged.object_ref.id 对应 Fragment 行
       在 DB 存在 = 重放 200 原 result_payload（零写零 commit）；不一致 →
       422 reused；
    5. preview 非 pending：'expired' 或 (pending 且 now>expires_at) → 409
       preview_stale{preview_id, conflicts:[{object:{preview,id},
       expected:'pending', actual:'expired'}]} 且惰性置 'expired'（00 §5.4，
       独立提交）；'applied'/'rejected'/'superseded' → 409 同形不改写；
    6. CAS（在 baseline 比对之前，冲突先报；a→b→range_set 序）：
       expected_revision_a≠a.current → 409 revision_conflict(object={fragment,
       a_id})；b 同形；expected_range_set_revision≠current cas → 409
       revision_conflict(object={range_set,id})；
    7. baseline 比对（写事务内重读为权威，失配项全部收集，00 §5.2）：
       fragment_a/fragment_b revision、range_set id+cas 失配 → 409
       preview_stale{preview_id, conflicts:[全部]} 且预览置
       'superseded'+resolved_at（独立提交）；
    8. 执行（单事务，无半次保存；写序冻结）：UPDATE a retired → UPDATE b
       retired → INSERT merged revision（flush）→ INSERT merged fragment →
       UPDATE range_set（cas+1）→ INSERT ReviewDecision（flush）→ UPDATE
       preview（applied）→ INSERT CommandRecord。

    返回 200 体（F10 冻结形状，**恰 2 字段**：{merged:{object_ref(revision=1),
    range, name}, predecessors:[{object_ref, state:'retired'}×2 请求序]}）；
    重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # preview：不存在或跨项目 → 404（kind=preview）
    preview = session.scalar(
        select(ChangePreview).where(
            ChangePreview.id == preview_id, ChangePreview.project_id == pid
        )
    )
    if preview is None:
        raise StudioAPIError.not_found("preview", preview_id)

    # 2) kind 必须是片段合并预览
    if preview.kind != "fragment_merge":
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "preview_id",
                    "rule": "mismatch",
                    "message": "preview_id 对应的预览不是片段合并预览。",
                }
            ]
        )

    # 3) 从 payload 取参；加载 a、b 行（域内无删除，理论必存在）
    pv_payload = json.loads(preview.payload) if preview.payload else {}
    ids = pv_payload.get("fragment_ids") or []
    if len(ids) != 2:  # 理论上不可达（preview 由本服务创建）
        raise StudioAPIError.internal()
    a_id, b_id = ids[0], ids[1]
    left_id = pv_payload.get("left_id")
    merged_name_s = pv_payload.get("merged_name")
    frag_a = session.get(Fragment, a_id)
    frag_b = session.get(Fragment, b_id)
    if frag_a is None or frag_b is None:  # 理论上不可达（域内无删除）
        raise StudioAPIError.internal()

    # 4) 幂等（00 §3；先于状态检查——原命令重放不受预览终态阻断；
    #    SOURCE-07 确认 2 的双原片变体：predecessors[0/1] 逐一比对 +
    #    merged 行在 DB 存在）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        preds = payload.get("predecessors") or []
        ref0 = (preds[0].get("object_ref") if len(preds) > 0 else {}) or {}
        ref1 = (preds[1].get("object_ref") if len(preds) > 1 else {}) or {}
        merged_ref = (payload.get("merged") or {}).get("object_ref") or {}
        if (
            ref0.get("id") == a_id
            and ref0.get("revision") == expected_revision_a
            and ref1.get("id") == b_id
            and ref1.get("revision") == expected_revision_b
            and merged_ref.get("id")
            and session.get(Fragment, merged_ref.get("id")) is not None
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

    # 6) 读阶段：当前 revision + cas（CAS 检查在 baseline 比对之前，冲突先报；
    #    a→b→range_set 序）
    rv_a = session.get(FragmentRevision, frag_a.current_revision_id)
    rv_b = session.get(FragmentRevision, frag_b.current_revision_id)
    if rv_a is None or rv_b is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    cur_a = rv_a.revision
    cur_b = rv_b.revision
    S = session.scalar(select(RangeSet).where(RangeSet.id == frag_a.range_set_id))
    set_id = S.id if S is not None else None
    cas = S.cas_revision if S is not None else None
    if expected_revision_a != cur_a:
        raise StudioAPIError.revision_conflict(
            "fragment",
            a_id,
            expected_revision_a,
            cur_a,
            "片段版本已变化，请刷新片段详情后重试。",
        )
    if expected_revision_b != cur_b:
        raise StudioAPIError.revision_conflict(
            "fragment",
            b_id,
            expected_revision_b,
            cur_b,
            "片段版本已变化，请刷新片段详情后重试。",
        )
    if expected_range_set_revision != cas:
        raise StudioAPIError.revision_conflict(
            "range_set",
            set_id,
            expected_range_set_revision,
            cas,
            "范围集合版本已变化，请刷新片段列表后重试。",
        )

    # 读事务快照（baseline；写阶段不再回读这些字段）
    base = json.loads(preview.baseline) if preview.baseline else {}
    session.commit()  # 结束读事务；写阶段另起事务

    # 7) 写事务：首条语句即写语句（no-op 自赋值抢 DB 级写锁，对 a、b 两行，
    #    按 id 字典序保证确定性，同 F7/F8/F9）→ 获锁后重读 → baseline 比对
    #    （00 §5.2 写事务内为权威，失配项全部收集）
    now = time.time()
    decision_id = make_ulid()
    merged_id = make_ulid()
    merged_rev_id = make_ulid()
    if not session.in_transaction():
        session.begin()
    try:
        # no-op 自赋值抢 DB 级写锁（SQLite 单写者：获锁后阻塞全部并发写）
        for lock_id in sorted((a_id, b_id)):
            session.execute(
                update(Fragment)
                .where(Fragment.id == lock_id)
                .values(updated_at=Fragment.updated_at)
            )
        # 获锁后重读（WAL：写事务可见此前全部已提交数据）
        a_now = session.get(Fragment, a_id)
        b_now = session.get(Fragment, b_id)
        rv_a_now = session.get(FragmentRevision, a_now.current_revision_id)
        rv_b_now = session.get(FragmentRevision, b_now.current_revision_id)
        S_now = session.scalar(
            select(RangeSet).where(RangeSet.id == a_now.range_set_id)
        )
        cur_rev_a = rv_a_now.revision
        cur_rev_b = rv_b_now.revision
        cur_cas = S_now.cas_revision if S_now is not None else None
        conflicts: list[dict] = []
        if base["fragment_a"]["revision"] != cur_rev_a:
            conflicts.append(
                {
                    "object": {"kind": "fragment", "id": a_id},
                    "expected": base["fragment_a"]["revision"],
                    "actual": cur_rev_a,
                }
            )
        if base["fragment_b"]["revision"] != cur_rev_b:
            conflicts.append(
                {
                    "object": {"kind": "fragment", "id": b_id},
                    "expected": base["fragment_b"]["revision"],
                    "actual": cur_rev_b,
                }
            )
        if (
            base["range_set"]["id"] != (S_now.id if S_now is not None else None)
            or base["range_set"]["cas"] != cur_cas
        ):
            conflicts.append(
                {
                    "object": {"kind": "range_set", "id": base["range_set"]["id"]},
                    "expected": base["range_set"]["cas"],
                    "actual": cur_cas,
                }
            )
        if conflicts:
            # baseline 已变：预览 → superseded（惰性终态，独立提交）后 409
            session.execute(
                update(ChangePreview)
                .where(ChangePreview.id == preview_id)
                .values(state="superseded", resolved_at=now)
            )
            session.commit()
            err = StudioAPIError.preview_stale(
                preview_id,
                "预览基准已变化（片段版本或范围集合版本），请重新预览。",
            )
            err.details["conflicts"] = conflicts
            raise err

        # 8) 执行（单事务，无半次保存）；写序冻结：UPDATE a retired → UPDATE b
        # retired → INSERT merged revision（flush）→ INSERT merged fragment →
        # range_set cas+1 → ReviewDecision（flush）→ preview applied →
        # CommandRecord。merged range=[左 start, 右 end)（左/右按 payload
        # left_id/right_id；baseline 已验 revision 未变，范围即 preview 时值）。
        if left_id == a_id:
            left_rv_now = rv_a_now
            right_rv_now = rv_b_now
        else:
            left_rv_now = rv_b_now
            right_rv_now = rv_a_now
        m_start = left_rv_now.range_start
        m_end = right_rv_now.range_end
        body = {
            "merged": {
                "object_ref": {"kind": "fragment", "id": merged_id, "revision": 1},
                "range": {"start": m_start, "end": m_end},
                "name": merged_name_s,
            },
            "predecessors": [
                {
                    "object_ref": {
                        "kind": "fragment",
                        "id": a_id,
                        "revision": cur_rev_a,
                    },
                    "state": "retired",
                },
                {
                    "object_ref": {
                        "kind": "fragment",
                        "id": b_id,
                        "revision": cur_rev_b,
                    },
                    "state": "retired",
                },
            ],
        }
        session.execute(
            update(Fragment)
            .where(Fragment.id == a_id)
            .values(state="retired", retired_at=now, updated_at=now)
        )
        session.execute(
            update(Fragment)
            .where(Fragment.id == b_id)
            .values(state="retired", retired_at=now, updated_at=now)
        )
        session.add(
            FragmentRevision(
                id=merged_rev_id,
                fragment_id=merged_id,
                revision=1,
                source_revision_id=a_now.source_revision_id,
                range_start=m_start,
                range_end=m_end,
                reason="merge",
                predecessor_fragment_ids=json.dumps([a_id, b_id], ensure_ascii=False),
                created_at=now,
            )
        )
        session.flush()  # 先落 merged revision 行（fragment 指向它之前须已存在）
        session.add(
            Fragment(
                id=merged_id,
                project_id=a_now.project_id,
                source_revision_id=a_now.source_revision_id,
                range_set_id=a_now.range_set_id,
                name=merged_name_s,
                summary=None,
                state="candidate",
                current_revision_id=merged_rev_id,
                created_at=now,
                updated_at=now,
                retired_at=None,
            )
        )
        # range_set：CAS 唯一写点之一——cas+1（附录 02 §3：F1/F8/F9/F10 的 apply 唯一写 cas）
        session.execute(
            update(RangeSet)
            .where(RangeSet.id == a_now.range_set_id)
            .values(cas_revision=cur_cas + 1)
        )
        session.add(
            ReviewDecision(
                id=decision_id,
                project_id=pid,
                owner_id=user.id,
                preview_id=preview_id,
                target_ref=json.dumps(
                    {"kind": "fragment", "id": merged_id, "revision": 1},
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


# ───────────────────── F2/F3 片段纯读（附录 02 §2 F2/F3；SOURCE-03-a） ─────────────────────

# cursor 格式（附录 01 §2 P2 裁定，逐字对齐 projects/service.py list_projects）：
# 必须是 26 字符 Crockford base32 小写
_CURSOR_RE = re.compile(r"^[0-9a-z]{26}$")

# F2 ?state= 合法值：持久三态 + 派生值 pending_review（附录 02 §1；
# pending_review 不落库，只列 active 版本时该过滤恒为空集）
_FRAGMENT_STATES = ("candidate", "confirmed", "retired", "pending_review")


def _fragment_dto(project, frag, rv) -> dict:
    """片段 DTO（附录 02 §2 F3 行冻结字段形状；F2 列表项与 F3 详情共用）。

    state = 有效状态（读时推导，不落库，附录 02 §1）：retired 恒 'retired'；
    绑定版本 == 项目 active → 持久 state；否则持久 state ∈ (candidate,
    confirmed) → 'pending_review'。revision_is_active 一并回。
    """
    revision_is_active = frag.source_revision_id == project.active_source_revision_id
    if frag.state == "retired":
        state = "retired"
    elif revision_is_active:
        state = frag.state
    else:
        state = "pending_review"
    return {
        "object_ref": {"kind": "fragment", "id": frag.id, "revision": rv.revision},
        "name": frag.name,
        "summary": frag.summary,
        "state": state,
        "revision_is_active": revision_is_active,
        "range": {"start": rv.range_start, "end": rv.range_end},
        "source_revision_id": frag.source_revision_id,
        "predecessor_ids": json.loads(rv.predecessor_fragment_ids or "[]"),
        "created_at": frag.created_at,
        "updated_at": frag.updated_at,
        "retired_at": frag.retired_at,
    }


def get_fragment(session, user, pid, fid) -> dict:
    """F3 读取片段详情（附录 02 §2 F3；纯读：不写任何表、不 commit、无 command_id）。

    校验顺序（冻结，按序短路）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏存在性，
       读写统一 404，SOURCE-01 验收裁定）；
    2. 片段不存在或不属本项目 → 一律 404 not_found(kind=fragment)（不泄漏，
       两路同形状）。

    任何版本片段都可读（含绑定非 active 版本与已 retired）；返回
    _fragment_dto 的 F3 行冻结 DTO（state=有效状态）。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    return _fragment_dto(p, frag, rv)


def list_fragments(session, user, pid, cursor: str | None, state: str | None, limit: int) -> dict:
    """F2 片段列表（附录 02 §2 F2，附录 00 §6 查询 DTO；纯读：不写任何表、
    不 commit、无 command_id）。

    校验顺序（冻结，按序短路）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. ?state= 非法值 → 422 validation_failed(field=state, rule=invalid，
       message 列合法值）；合法值含派生 pending_review（只列 active 版本时
       派生 state 恒等于持久 state，故该过滤恒为空集，返回 200 空列表）；
    3. keyset 参数（逐字对齐 P2 list_projects）：limit 缺省 50，clamp 到
       [1, 200]，越界不报 422；cursor 非 ^[0-9a-z]{26}$ → 422
       validation_failed(field=cursor, rule=format)；
    4. 只列绑定项目当前 active 版本的片段；无 active →
       {"items": [], "next_cursor": None}（空列表 ≠ 读失败，R12）。

    查询：id DESC（新→旧；P2 裁定的域倒序惯例，附录未定方向），取 limit+1
    判溢出，next_cursor = 溢出时本页末项 id 否则 None；items 元素 =
    _fragment_dto 的完整 DTO。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) state 过滤值（合法值含派生 pending_review）
    if state is not None and state not in _FRAGMENT_STATES:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "state",
                    "rule": "invalid",
                    "message": "state 必须是 candidate/confirmed/retired/pending_review 之一。",
                }
            ]
        )

    # 3) keyset 参数（逐字对齐 P2 list_projects）
    limit = max(1, min(int(limit), 200))
    if cursor is not None and not _CURSOR_RE.match(cursor):
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "cursor",
                    "rule": "format",
                    "message": "cursor 必须是 26 字符 ULID。",
                }
            ]
        )

    # 4) 只列绑定当前 active 版本的片段；无 active → 空列表（≠读失败）
    if p.active_source_revision_id is None:
        return {"items": [], "next_cursor": None}
    stmt = select(Fragment).where(
        Fragment.project_id == pid,
        Fragment.source_revision_id == p.active_source_revision_id,
    )
    if state is not None:
        stmt = stmt.where(Fragment.state == state)
    if cursor is not None:
        stmt = stmt.where(Fragment.id < cursor)
    rows = session.scalars(
        stmt.order_by(Fragment.id.desc()).limit(limit + 1)
    ).all()
    items = []
    for frag in rows[:limit]:
        rv = session.get(FragmentRevision, frag.current_revision_id)
        if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
            raise StudioAPIError.internal()
        items.append(_fragment_dto(p, frag, rv))
    next_cursor = (
        items[-1]["object_ref"]["id"] if len(rows) > limit and items else None
    )
    return {"items": items, "next_cursor": next_cursor}
