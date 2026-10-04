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
import json
import time

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..projects.models import StudioProject
from .fragment_models import Fragment, FragmentRevision, RangeSet
from .models import SourceRevision
from .ranges import ranges_overlap, utf16_len, validate_range

__all__ = ["create_fragment"]

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
