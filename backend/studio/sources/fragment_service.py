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
import time

from sqlalchemy import bindparam, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..jobs.models import StudioJob
from ..media.models import MediaArtifact
from ..projects.models import StudioProject
from ..relations.models import AssetOccurrence, SourceRelation
from ..reviews.models import ChangePreview, ReviewDecision
from ..scripts.models import ScriptAdoption, ScriptObject, ScriptRevision
from .fragment_models import Fragment, FragmentRevision, RangeSet
from .models import SourceRevision
from .ranges import ranges_overlap, utf16_len, validate_range

__all__ = [
    "create_fragment",
    "confirm_fragment",
    "rename_fragment",
    "preview_fragment_retire",
    "apply_fragment_retire",
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

# 跨域只读查询（表名冻结常量；表可能尚未交付进当前 DB → 缺表时该规则降级为空，不 500）
_SHOT_SQL = (
    "SELECT s.id, r.revision FROM studio_shots s "
    "JOIN studio_shot_revisions r ON r.id = s.current_revision_id "
    "WHERE s.project_id = :pid AND s.script_adoption_id IN :adoptions"
)
_EDIT_INSTANCE_SQL = "SELECT id FROM studio_edit_instances WHERE media_id IN :ids"
_RELEASE_SQL = (
    "SELECT id FROM studio_releases WHERE project_id = :pid "
    "AND poster_artifact_id IN :ids"
)


def _raw_rows(session, sql, params) -> list:
    """跨域只读查询（F7 impact 证据链）：params 中 list/set/tuple 值自动展开为 IN 列表；
    表不存在（当前 DB 未交付该表）→ 返回空列表（该 impact 规则降级），绝不 500。"""
    expanded = dict(params)
    for k, v in list(expanded.items()):
        if isinstance(v, (set, frozenset)):
            expanded[k] = sorted(v)
        elif isinstance(v, tuple):
            expanded[k] = list(v)
    q = text(sql)
    for k, v in expanded.items():
        if isinstance(v, list):
            q = q.bindparams(bindparam(k, expanding=True))
    try:
        return session.execute(q, expanded).all()
    except OperationalError:
        return []


def _successor_group(session, pid, fid) -> set:
    """后继组（主模型裁定）：{fid} ∪ 传递闭包——任何 FragmentRevision 行的
    predecessor_fragment_ids 含组内 id 的 fragment 全部入组（split/merge 后继链）。"""
    preds: dict[str, set] = {}
    for f in session.scalars(select(Fragment).where(Fragment.project_id == pid)).all():
        s: set = set()
        for r in session.scalars(
            select(FragmentRevision).where(FragmentRevision.fragment_id == f.id)
        ).all():
            s.update(json.loads(r.predecessor_fragment_ids or "[]"))
        preds[f.id] = s
    group = {fid}
    changed = True
    while changed:
        changed = False
        for f_id, s in preds.items():
            if f_id not in group and (s & group):
                group.add(f_id)
                changed = True
    return group


def _compute_impact(session, pid, fid) -> dict:
    """F7 preview impact（附录 02 §2 impact 计算，主模型裁定；确定性 = 各列表按
    object_ref.id 排序；空表 → 空列表，键不得省略）：
    - affected[]：直接引用后继组的 SourceRelation/AssetOccurrence（含引用时记录的
      fragment revision）/ScriptObject/ScriptAdoption（revision=null）/Shot（当前
      revision 号，经其采用的 adoption 关联）；
    - needs_review[]：冻结输入 payload 文本含任一 group id 且 status ∈ (pending,
      running) 的 StudioJob（附录 "queued" = 模型值 pending）；
    - preservable[]：证据链 MediaArtifact(source_job_id∈job 组) → EditInstance
      (media_id) → Release(poster_artifact_id)（跨域表未交付时降级为空）。
    """
    group = _successor_group(session, pid, fid)

    affected: list[dict] = []
    for r in session.scalars(
        select(SourceRelation).where(
            SourceRelation.project_id == pid,
            SourceRelation.source_fragment_id.in_(group),
        )
    ).all():
        affected.append(
            {
                "object_ref": {
                    "kind": "source_relation",
                    "id": r.id,
                    "revision": r.source_fragment_revision,
                }
            }
        )
    for a in session.scalars(
        select(AssetOccurrence).where(
            AssetOccurrence.project_id == pid,
            AssetOccurrence.source_fragment_id.in_(group),
        )
    ).all():
        affected.append(
            {
                "object_ref": {
                    "kind": "asset_occurrence",
                    "id": a.id,
                    "revision": a.source_fragment_revision,
                }
            }
        )
    for o in session.scalars(
        select(ScriptObject).where(ScriptObject.fragment_id.in_(group))
    ).all():
        orev = session.get(ScriptRevision, o.current_revision_id)
        affected.append(
            {
                "object_ref": {
                    "kind": "script_object",
                    "id": o.id,
                    "revision": orev.revision if orev is not None else None,
                }
            }
        )
    adoption_ids: set = set()
    for ad in session.scalars(
        select(ScriptAdoption).where(ScriptAdoption.fragment_id.in_(group))
    ).all():
        adoption_ids.add(ad.id)
        affected.append(
            {"object_ref": {"kind": "script_adoption", "id": ad.id, "revision": None}}
        )
    if adoption_ids:
        for row in _raw_rows(
            session, _SHOT_SQL, {"pid": pid, "adoptions": sorted(adoption_ids)}
        ):
            affected.append(
                {"object_ref": {"kind": "shot", "id": row[0], "revision": row[1]}}
            )
    affected.sort(key=lambda x: x["object_ref"]["id"])

    # job 组 = payload（冻结输入）文本含任一 group id 的全部项目 job（任意状态）；
    # needs_review = 该组 ∩ {pending, running}
    jobs = session.scalars(select(StudioJob).where(StudioJob.project_id == pid)).all()
    job_group = [j for j in jobs if any(g in (j.payload or "") for g in group)]
    needs_review: list[dict] = []
    for j in job_group:
        if j.status in ("pending", "running"):
            needs_review.append(
                {
                    "object_ref": {"kind": "job", "id": j.id, "revision": None},
                    "reason": "frozen_input_contains_fragment",
                }
            )
    needs_review.sort(key=lambda x: x["object_ref"]["id"])

    preservable: list[dict] = []
    if job_group:
        jids = [j.id for j in job_group]
        arts = session.scalars(
            select(MediaArtifact).where(
                MediaArtifact.project_id == pid,
                MediaArtifact.source_job_id.in_(jids),
            )
        ).all()
        for m in arts:
            preservable.append(
                {"object_ref": {"kind": "media_artifact", "id": m.id, "revision": None}}
            )
        aids = [m.id for m in arts]
        if aids:
            for row in _raw_rows(session, _EDIT_INSTANCE_SQL, {"ids": aids}):
                preservable.append(
                    {
                        "object_ref": {
                            "kind": "edit_instance",
                            "id": row[0],
                            "revision": None,
                        }
                    }
                )
            for row in _raw_rows(session, _RELEASE_SQL, {"pid": pid, "ids": aids}):
                preservable.append(
                    {"object_ref": {"kind": "release", "id": row[0], "revision": None}}
                )
    preservable.sort(key=lambda x: x["object_ref"]["id"])

    return {"affected": affected, "needs_review": needs_review, "preservable": preservable}


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
    impact = _compute_impact(session, pid, fid)
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
