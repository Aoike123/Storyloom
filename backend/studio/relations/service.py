"""REL-02 资产出现引用命令（附录 06 §1/§2 OC1–OC3）。

只做**出现**（`AssetOccurrence`）：资产在原文片段中的出处（R05/R07）。
出现是出处——不复制资产、不建片段、不承担归属；`studio_asset_bindings`
（生成输入）与 `studio_source_relations`（额外出处）不在本卡。

冻结语义：
- 出现范围与片段范围各司其职：`range` 为片段区间内的同一套 UTF-16 半开绝对
  偏移（不引入第二套坐标），可空（成对可空 = "无精确范围"桶，同桶唯一）；
- 片段只校验「绑定版本 == 项目当前 active 正文版本」（**不校验 confirmed**，
  与 SC4 先例一致）；
- 所有写命令幂等（附录 00 §3）：同 command_id 同内容 → 200 首次 payload；异内容
  → 422 `validation_failed` field=`command_id` rule=`reused`；零写零 commit；
- 删除类：同 command_id 重放 → 200 首次 payload；删掉后换新 command_id → 404
  （附录 06 §2 注：客户端以 404 收敛）。
"""
import json
import re
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ..assets.models import Asset, AssetRevision
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..projects.models import StudioProject
from ..sources.fragment_models import Fragment, FragmentRevision
from .models import AssetOccurrence

__all__ = ["create_occurrence", "list_occurrences", "delete_occurrence"]

# keyset 游标（逐字对齐 P2/P1 域惯例）
_CURSOR_RE = re.compile(r"^[0-9a-z]{26}$")


def _project(session, user, pid: str) -> StudioProject:
    """项目属主校验（不存在/非属主同形状 404，不泄漏存在性）。"""
    p = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if p is None or p.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)
    return p


def _revision_no(session, model, current_revision_id) -> int:
    """可变聚合当前 revision 号；`current_revision_id` 为 NOT NULL FK，
    缺失 = 数据损坏 → 冻结 internal_error（不泄露细节）。"""
    rev = session.get(model, current_revision_id)
    if rev is None:  # 理论上不可达
        raise StudioAPIError.internal()
    return rev.revision


def _range_dict(start, end):
    if start is None and end is None:
        return None
    return {"start": start, "end": end}


def _to_dto(occ: AssetOccurrence) -> dict:
    """OC 成功 DTO。

    `object_ref.revision` 取 `source_fragment_revision`：与既有
    `reviews/impact.py` 对 `asset_occurrence` 的发射保持一致，不新造第二套
    revision 约定；资产/片段各自的 revision 另有显式 ref。
    """
    return {
        "object_ref": {
            "kind": "asset_occurrence",
            "id": occ.id,
            "revision": occ.source_fragment_revision,
        },
        "asset": {
            "kind": "asset",
            "id": occ.asset_id,
            "revision": occ.asset_revision,
        },
        "fragment": {
            "kind": "fragment",
            "id": occ.source_fragment_id,
            "revision": occ.source_fragment_revision,
        },
        "range": _range_dict(occ.range_start, occ.range_end),
        "created_at": occ.created_at,
    }


def _parse_range(frag_rev: FragmentRevision, range_):
    """范围校验（附录 00 §4 `range_invalid`；坐标系 = 片段的 UTF-16 半开区间）。

    - 缺一/非整数 → rule=`non_integer`；
    - `start >= end` → rule=`empty`；
    - 越出片段区间 → rule=`out_of_bounds`。
    """
    if range_ is None:
        return None, None
    if not isinstance(range_, dict):
        raise StudioAPIError.range_invalid(
            "non_integer", "range 必须是 {start, end} 半开区间。"
        )
    start = range_.get("start")
    end = range_.get("end")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
    ):
        raise StudioAPIError.range_invalid(
            "non_integer", "range.start 与 range.end 必须是整数。"
        )
    if start >= end:
        raise StudioAPIError.range_invalid(
            "empty", "range 不能为空区间（end 必须大于 start）。"
        )
    if start < frag_rev.range_start or end > frag_rev.range_end:
        raise StudioAPIError.range_invalid(
            "out_of_bounds",
            "range 必须落在该片段区间内（出现范围与片段范围各司其职）。",
        )
    return start, end


def _duplicate_stmt(asset_id: str, source_fragment_id: str, start, end):
    """唯一桶查询：与表达式索引 `coalesce(range_start,-1), coalesce(range_end,-1)`
    同语义（NULL 范围 = 同一「无精确范围」桶）。"""
    stmt = select(AssetOccurrence).where(
        AssetOccurrence.asset_id == asset_id,
        AssetOccurrence.source_fragment_id == source_fragment_id,
    )
    if start is None:
        return stmt.where(
            AssetOccurrence.range_start.is_(None), AssetOccurrence.range_end.is_(None)
        )
    return stmt.where(
        AssetOccurrence.range_start == start, AssetOccurrence.range_end == end
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


def create_occurrence(
    session,
    user,
    pid: str,
    asset_id: str,
    source_fragment_id: str,
    range_,
    command_id: str,
) -> dict:
    """OC1 建立出现（附录 06 §2）。校验序（冻结，按序短路）：

    1. 项目 404；2. 资产 404（不存在/跨项目）；3. 片段 404（不存在/跨项目）；
    4. range 形状与界（`range_invalid`）；5. 片段绑定版本非 active → 422
       `precondition_failed` reason=`source_revision_inactive`（不校验 confirmed）；
    6. 幂等（同 command_id 同内容 → 200 首次 payload；异内容 → 422 reused）；
    7. 唯一桶冲突 → 409 `duplicate`；
    8. 写事务：INSERT AssetOccurrence（`asset_revision`=资产当前 revision、
       `source_fragment_revision`=片段当前 revision）+ CommandRecord。

    返回 201 体；重放时附内部标记 ``_replay=True``（路由转 200）。
    """
    project = _project(session, user, pid)

    asset = session.scalar(select(Asset).where(Asset.id == asset_id))
    if asset is None or asset.project_id != pid:
        raise StudioAPIError.not_found("asset", asset_id)

    frag = session.scalar(select(Fragment).where(Fragment.id == source_fragment_id))
    if frag is None or frag.project_id != pid:
        raise StudioAPIError.not_found("fragment", source_fragment_id)

    frag_rev = session.get(FragmentRevision, frag.current_revision_id)
    if frag_rev is None:  # 理论上不可达
        raise StudioAPIError.internal()
    start, end = _parse_range(frag_rev, range_)
    frag_rev_no = frag_rev.revision

    if frag.source_revision_id != project.active_source_revision_id:
        raise StudioAPIError.precondition_failed(
            "source_revision_inactive",
            {"kind": "fragment", "id": source_fragment_id, "revision": frag_rev_no},
            "该片段绑定的正文版本不是项目当前活跃版本，不能建立出现。",
        )

    asset_rev_no = _revision_no(session, AssetRevision, asset.current_revision_id)
    normalized = _range_dict(start, end)

    # 幂等（00 §3）：读查询，先于唯一检查与写
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if (
            (payload.get("asset") or {}).get("id") == asset_id
            and (payload.get("fragment") or {}).get("id") == source_fragment_id
            and payload.get("range") == normalized
        ):
            return dict(payload, _replay=True)
        raise _reused()

    if session.scalar(_duplicate_stmt(asset_id, source_fragment_id, start, end)) is not None:
        raise StudioAPIError.duplicate(
            "asset_occurrence:"
            f"{asset_id}:{source_fragment_id}:"
            f"{start if start is not None else '-'}:{end if end is not None else '-'}",
            message="该资产在此片段的同一范围已有出现记录。",
        )

    session.commit()  # 结束读事务；写阶段另起事务
    now = time.time()
    occ = AssetOccurrence(
        id=make_ulid(),
        project_id=pid,
        asset_id=asset_id,
        asset_revision=asset_rev_no,
        source_fragment_id=source_fragment_id,
        source_fragment_revision=frag_rev_no,
        range_start=start,
        range_end=end,
        created_at=now,
    )
    response = _to_dto(occ)
    try:
        with session.begin():
            session.add(occ)
            session.add(
                CommandRecord(
                    id=make_ulid(),
                    owner_id=user.id,
                    project_id=pid,
                    command_id=command_id,
                    result_payload=json.dumps(response, ensure_ascii=False),
                    created_at=now,
                )
            )
    except IntegrityError:
        # 并发竞态：赢家已提交 → 重读其 CommandRecord 映射重放语义；
        # 其他完整性冲突（本卡不应出现）→ 冻结 internal_error
        session.rollback()
        record = session.scalar(
            select(CommandRecord).where(
                CommandRecord.owner_id == user.id,
                CommandRecord.command_id == command_id,
            )
        )
        if record is not None:
            payload = json.loads(record.result_payload) if record.result_payload else {}
            return dict(payload, _replay=True)
        raise StudioAPIError.internal()

    return response


def list_occurrences(
    session,
    user,
    pid: str,
    asset_id: str | None = None,
    source_fragment_id: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict:
    """OC2 出现列表（附录 00 §6 查询 DTO；纯读，无副作用、无 command_id）。

    keyset 分页逐字对齐 P2/P1 域惯例：`limit` 缺省 50、clamp [1,200]（越界不报
    422）；`cursor` 非 26 字符 ULID → 422 `validation_failed`(field=cursor,
    rule=format)；排序 `id DESC`（新→旧，域倒序惯例）；只列本项目行。
    """
    _project(session, user, pid)

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

    stmt = select(AssetOccurrence).where(AssetOccurrence.project_id == pid)
    if asset_id is not None:
        stmt = stmt.where(AssetOccurrence.asset_id == asset_id)
    if source_fragment_id is not None:
        stmt = stmt.where(AssetOccurrence.source_fragment_id == source_fragment_id)
    if cursor is not None:
        stmt = stmt.where(AssetOccurrence.id < cursor)

    rows = session.scalars(
        stmt.order_by(AssetOccurrence.id.desc()).limit(limit + 1)
    ).all()
    items = [_to_dto(r) for r in rows[:limit]]
    next_cursor = (
        items[-1]["object_ref"]["id"] if len(rows) > limit and items else None
    )
    return {"items": items, "next_cursor": next_cursor}


def delete_occurrence(
    session, user, pid: str, occurrence_id: str, command_id: str
) -> dict:
    """OC3 删除出现（附录 06 §2；无 preview、无其他业务副作用）。

    1. 项目 404；2. 幂等（先于存在性检查）：同 command_id 且首次 payload 指向
       同一 occurrence → 200 原 payload；异内容 → 422 reused；
    3. 行不存在/跨项目 → 404 `not_found`(kind=asset_occurrence)；
    4. 单事务 DELETE 行 + CommandRecord。

    删掉后换**新** command_id 再来 → 404（客户端以 404 收敛）。
    """
    _project(session, user, pid)

    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if (
            (payload.get("object_ref") or {}).get("id") == occurrence_id
            and payload.get("deleted") is True
        ):
            return dict(payload, _replay=True)
        raise _reused()

    occ = session.scalar(
        select(AssetOccurrence).where(
            AssetOccurrence.id == occurrence_id,
            AssetOccurrence.project_id == pid,
        )
    )
    if occ is None:
        raise StudioAPIError.not_found("asset_occurrence", occurrence_id)

    response = {
        "object_ref": {
            "kind": "asset_occurrence",
            "id": occ.id,
            "revision": occ.source_fragment_revision,
        },
        "deleted": True,
    }
    session.commit()  # 结束读事务；写阶段另起事务
    now = time.time()
    try:
        with session.begin():
            session.delete(occ)
            session.add(
                CommandRecord(
                    id=make_ulid(),
                    owner_id=user.id,
                    project_id=pid,
                    command_id=command_id,
                    result_payload=json.dumps(response, ensure_ascii=False),
                    created_at=now,
                )
            )
    except IntegrityError:
        session.rollback()
        record = session.scalar(
            select(CommandRecord).where(
                CommandRecord.owner_id == user.id,
                CommandRecord.command_id == command_id,
            )
        )
        if record is not None:
            payload = json.loads(record.result_payload) if record.result_payload else {}
            return dict(payload, _replay=True)
        raise StudioAPIError.internal()

    return response
