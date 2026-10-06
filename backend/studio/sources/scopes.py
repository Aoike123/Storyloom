"""Storyloom Studio — 完整制作范围域服务层（附录 02 §2 F11/F12 的服务端实现）。

SOURCE-11 填充：F11 完整制作范围提交 + F12 范围读（历史列表 / 当前范围）。

实现约束（冻结）：
- 范围绑定具体正文版本（source_revision_id = 提交时项目 active）；历史只追加；
- F11 不动 range_set.cas（附录 02 §3：F4–F7/F11 不改范围布局）；
- 候选片段不进范围：给定路径逐一拒绝（not_confirmed），缺省路径只取 confirmed；
- 覆盖语义：covered = canonical 全部**非空白码元**被选中片段当前 revision 范围
  并集覆盖；excluded_ranges = 范围外、按空白切割的最大连续非空白段
  （UTF-16 半开，start 升序）；
- 幂等/属主 404 不泄漏/错误协议同 fragment_service.py 头注。
"""
import json
import re
import time

from sqlalchemy import select

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..projects.models import StudioProject
from .fragment_models import Fragment, FragmentRevision
from .models import ProductionScope, SourceRevision
from .ranges import utf16_len

__all__ = [
    "create_production_scope",
    "list_production_scopes",
    "get_current_production_scope",
]

# 附录 01 §2 P2 裁定同款游标格式（26 字符 Crockford base32 小写）
_CURSOR_RE = re.compile(r"^[0-9a-z]{26}$")

# 冻结空白集（附录 00 §7，v2.5）：= JS ``\s`` 码点集。ranges.py 的判定 helper
# （_is_whitespace/_WHITESPACE_CODEPOINTS）为模块私有未导出，本文件按其冻结集
# 原样实现（SOURCE-11 冻结：不另发明集合；Python 不用 isspace()——其与 JS \s
# 在 U+0085/U+001C-1F/U+FEFF 等码点不一致）。集中成员均属 BMP，故逐 UTF-16
# code unit 判定与逐码点判定等价（代理对两半均不在集内 → 恒为非空白）。
_WHITESPACE_CODEPOINTS = frozenset(
    {0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20}
    | {0x00A0, 0x1680}
    | set(range(0x2000, 0x200B))
    | {0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF}
)


def _to_dto(scope: ProductionScope) -> dict:
    """范围 DTO（冻结形状，恰 6 字段；revision 恒 None——范围行只追加不改版）。"""
    return {
        "object_ref": {"kind": "production_scope", "id": scope.id, "revision": None},
        "source_revision_id": scope.source_revision_id,
        "fragment_ids": json.loads(scope.fragment_ids or "[]"),
        "covered": bool(scope.covered),
        "excluded_ranges": json.loads(scope.excluded_ranges or "[]"),
        "created_at": scope.created_at,
    }


def _compute_coverage(canonical: str, ranges: list[tuple[int, int]]) -> tuple[bool, list[dict]]:
    """覆盖计算（附录 02 §1 表语义 + 00 §7 v2.5 空白集）。

    - covered = canonical 全部非空白码元都被 ranges 并集覆盖；
    - excluded_ranges = 并集之外、按空白切割的最大连续非空白段
      （UTF-16 半开 [{start,end}]，start 升序；空白码元不属于任何段，
      故每段至少含一个非空白码元）。
    """
    n = utf16_len(canonical)
    data = canonical.encode("utf-16-be")
    units = [data[i] << 8 | data[i + 1] for i in range(0, len(data), 2)]
    blank = [u in _WHITESPACE_CODEPOINTS for u in units]
    hit = bytearray(n)
    for s, e in ranges:
        hit[s:e] = b"\x01" * (e - s)
    covered = all(hit[i] or blank[i] for i in range(n))
    excluded: list[dict] = []
    i = 0
    while i < n:
        if not hit[i] and not blank[i]:
            j = i + 1
            while j < n and not hit[j] and not blank[j]:
                j += 1
            excluded.append({"start": i, "end": j})
            i = j
        else:
            i += 1
    return covered, excluded


def create_production_scope(session, user, pid, fragment_ids, command_id) -> dict:
    """F11 完整制作范围提交（附录 02 §2 F11；R04 的"完整制作范围"明确确认）。

    校验顺序（冻结，按序短路）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. project.active_source_revision_id 为空 → 422 precondition_failed
       (reason=no_active_source，blocked_by={source, None, None})；
    3. fragment_ids 给定但为空数组 → 422 validation_failed(field=fragment_ids,
       rule=required)；含重复 id → 422 rule=duplicate；
    4. 给定路径：按请求序逐一加载——不存在/跨项目 → 404 not_found(kind=fragment)；
       绑定版本 ≠ active → 422 precondition_failed(reason=source_revision_inactive，
       blocked_by={fragment, id, 当前 revision})；持久 state ≠ confirmed → 422
       precondition_failed(reason=not_confirmed，message 含片段名与当前持久 state)；
    5. 缺省路径（None）：当前 active 版本全部 state='confirmed' 片段按 id 排序；
       零个 → 422 validation_failed(field=fragment_ids, rule=required)；
    6. 幂等（附录 00 §3，同 F1 模式=校验之后、写之前）：(owner, command_id) 记录
       存在 → result_payload 的 source_revision_id==当前 active 且 fragment_ids==
       本次有效列表 = 重放：返回首次 result_payload 并附内部标记 ``_replay=True``
       （路由转 200，零写零 commit）；不一致 → 422 validation_failed(rule=reused)；
    7. duplicate_scope：同 (project_id, source_revision_id=active) 已有 scope 行其
       fragment_ids 集合（各自排序后相等）与本次相同 → 422 duplicate_scope
       (details.scope_ref={kind,id,revision=None})——历史只追加不重复；
    8. 计算（读事务内，active canonical）：covered 与 excluded_ranges
       （见 _compute_coverage；片段范围取自其当前 FragmentRevision 行）；
    9. 写事务（单事务，无半次保存）：INSERT ProductionScope + INSERT
       CommandRecord(result_payload=完整 201 体)，同一 ``session.begin()``；
       不动 range_set.cas（附录 02 §3）。任何异常 → 回滚。

    返回 201 体（范围 DTO，恰 6 字段）；重放时附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 须有 active 正文版本
    active_id = p.active_source_revision_id
    if not active_id:
        raise StudioAPIError.precondition_failed(
            "no_active_source",
            {"kind": "source", "id": None, "revision": None},
            "项目还没有正文版本，请先导入。",
        )

    # 3) fragment_ids 形状：空数组 / 重复 id（仅给定路径）
    if fragment_ids is not None:
        if len(fragment_ids) == 0:
            raise StudioAPIError.validation_failed(
                [
                    {
                        "field": "fragment_ids",
                        "rule": "required",
                        "message": "fragment_ids 不能为空数组；省略或传 null 表示全部已确认片段。",
                    }
                ]
            )
        if len(set(fragment_ids)) != len(fragment_ids):
            raise StudioAPIError.validation_failed(
                [
                    {
                        "field": "fragment_ids",
                        "rule": "duplicate",
                        "message": "fragment_ids 含重复片段 ID。",
                    }
                ]
            )

    # 4)+5) 解析有效片段集：给定=按请求序逐一校验（重复已在第 3 步拒绝，
    # 有效列表即请求原序 list）；缺省=active 版本全部 confirmed 按 id 排序
    selected: list[tuple[Fragment, FragmentRevision]] = []
    if fragment_ids is not None:
        for fid in fragment_ids:
            frag = session.scalar(
                select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
            )
            if frag is None:
                # 不存在或不属本项目 → 一律 404（不泄漏存在性）
                raise StudioAPIError.not_found("fragment", fid)
            rv = session.get(FragmentRevision, frag.current_revision_id)
            if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
                raise StudioAPIError.internal()
            if frag.source_revision_id != active_id:
                raise StudioAPIError.precondition_failed(
                    "source_revision_inactive",
                    {"kind": "fragment", "id": fid, "revision": rv.revision},
                    f"片段「{frag.name}」绑定的正文版本不是项目当前活跃版本。",
                )
            if frag.state != "confirmed":
                raise StudioAPIError.precondition_failed(
                    "not_confirmed",
                    {"kind": "fragment", "id": fid, "revision": rv.revision},
                    f"片段「{frag.name}」当前状态为 {frag.state}，只有已确认（confirmed）片段才能纳入制作范围。",
                )
            selected.append((frag, rv))
        effective_ids = list(fragment_ids)
    else:
        frags = session.scalars(
            select(Fragment)
            .where(
                Fragment.project_id == pid,
                Fragment.source_revision_id == active_id,
                Fragment.state == "confirmed",
            )
            .order_by(Fragment.id)
        ).all()
        if not frags:
            raise StudioAPIError.validation_failed(
                [
                    {
                        "field": "fragment_ids",
                        "rule": "required",
                        "message": "项目当前没有已确认的片段。",
                    }
                ]
            )
        for frag in frags:
            rv = session.get(FragmentRevision, frag.current_revision_id)
            if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
                raise StudioAPIError.internal()
            selected.append((frag, rv))
        effective_ids = [frag.id for frag, _ in selected]

    # 6) 幂等（附录 00 §3，同 F1 模式）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if (
            payload.get("source_revision_id") == active_id
            and payload.get("fragment_ids") == effective_ids
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

    # 7) duplicate_scope：同 (project, active) 已有同覆盖集合行 → 422（只追加不重复）
    want = sorted(effective_ids)
    existing = session.scalars(
        select(ProductionScope).where(
            ProductionScope.project_id == pid,
            ProductionScope.source_revision_id == active_id,
        )
    ).all()
    for row in sorted(existing, key=lambda r: r.id):
        if sorted(json.loads(row.fragment_ids or "[]")) == want:
            raise StudioAPIError.duplicate_scope(
                {"kind": "production_scope", "id": row.id, "revision": None},
                message="相同片段覆盖的制作范围已提交过，历史只追加不重复。",
            )

    # 8) 覆盖计算（读事务内，active canonical；范围取片段当前 FragmentRevision 行）
    sr = session.scalar(select(SourceRevision).where(SourceRevision.id == active_id))
    if sr is None:  # 理论上不可达（active 指针 FK）
        raise StudioAPIError.internal()
    covered, excluded = _compute_coverage(
        sr.canonical_content, [(rv.range_start, rv.range_end) for _, rv in selected]
    )
    session.commit()  # 结束读事务；写阶段另起事务

    # 9) 写事务：单事务，无半次保存；不动 range_set.cas（附录 02 §3：F11 不改
    # 范围布局）。写序 ProductionScope → CommandRecord
    now = time.time()
    scope_id = make_ulid()
    body = {
        "object_ref": {"kind": "production_scope", "id": scope_id, "revision": None},
        "source_revision_id": active_id,
        "fragment_ids": effective_ids,
        "covered": covered,
        "excluded_ranges": excluded,
        "created_at": now,
    }
    with session.begin():
        session.add(
            ProductionScope(
                id=scope_id,
                project_id=pid,
                source_revision_id=active_id,
                fragment_ids=json.dumps(effective_ids, ensure_ascii=False),
                covered=covered,
                excluded_ranges=json.dumps(excluded, ensure_ascii=False),
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


def list_production_scopes(session, user, pid, cursor, limit) -> dict:
    """F12 范围历史列表（附录 02 §2 F12 + 00 §6 查询 DTO）。

    纯读：不写任何表、不 commit、不接受 command_id（附录 00 §3）。
    项目不存在/非属主 → 404（不泄漏）。**限当前 active 版本**（无 active →
    ``{"items":[],"next_cursor":None}``）。

    keyset 分页（P2 同款游标语义）：
    - cursor = 上一页最后一条 id（ULID 时间序），取 id < cursor 的下一页；
    - 排序 id DESC（新→旧）；
    - limit 缺省 50，clamp 到 [1, 200]，越界不报 422；
    - cursor 必须匹配 ^[0-9a-z]{26}$，否则 422 validation_failed（同 P2）。
    """
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

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

    if not p.active_source_revision_id:
        return {"items": [], "next_cursor": None}

    stmt = select(ProductionScope).where(
        ProductionScope.project_id == pid,
        ProductionScope.source_revision_id == p.active_source_revision_id,
    )
    if cursor is not None:
        stmt = stmt.where(ProductionScope.id < cursor)
    rows = session.scalars(stmt.order_by(ProductionScope.id.desc()).limit(limit + 1)).all()
    items = [_to_dto(r) for r in rows[:limit]]
    next_cursor = (
        items[-1]["object_ref"]["id"] if len(rows) > limit and items else None
    )
    return {"items": items, "next_cursor": next_cursor}


def get_current_production_scope(session, user, pid) -> dict:
    """F12 当前制作范围（附录 02 §1：同 project+revision 下最新一行，id 最大）。

    纯读：不写任何表、不 commit。项目不存在/非属主 → 404（不泄漏）；
    无 active 或该版本无 scope 行 → 404 not_found(kind=production_scope, id=pid)。
    """
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    if not p.active_source_revision_id:
        raise StudioAPIError.not_found("production_scope", pid)
    row = session.scalars(
        select(ProductionScope)
        .where(
            ProductionScope.project_id == pid,
            ProductionScope.source_revision_id == p.active_source_revision_id,
        )
        .order_by(ProductionScope.id.desc())
        .limit(1)
    ).first()
    if row is None:
        raise StudioAPIError.not_found("production_scope", pid)
    return _to_dto(row)
