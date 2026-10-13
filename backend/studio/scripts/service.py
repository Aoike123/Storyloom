"""Storyloom Studio — 剧本域服务层（附录 03 §2 命令的服务端实现）。

SCRIPT-01-a 逐子卡填充（本卡只实现其唯一命令）：
SC1 新建剧本草稿对象（action/dialogue），记录出处，不采用。

实现约束（冻结）：
- 建立对象 = 真实写入 ScriptObject + ScriptRevision（revision=1，
  随内容冻结 speaker 与 source_fragment_revision 出处快照），同一事务；
- **不采用**：不写 ScriptAdoption、不改任何采用状态、不启动生成、
  不写任何 stage/状态列（附录 03 §1/§3：草稿与采用是两个事实）；
- 所属片段只来自对象行上的 fragment_id（版本更新不复制业务归属）；
- 所有写命令走 command_id 幂等（附录 00 §3，studio_command_records）；
- 属主校验：未登录 401（core.auth.require_user）；项目不存在/非属主一律
  404 not_found（不泄漏存在性，SOURCE-01 验收裁定：读写统一 404）；
- id 全部服务端 ULID（core.db.make_ulid），客户端永不生成实体 ID；
- 错误一律 raise StudioAPIError（冻结错误协议，附录 00 §4）。
"""
import json
import time

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..projects.models import StudioProject
from ..sources.fragment_models import Fragment, FragmentRevision
from .models import ScriptObject, ScriptRevision

__all__ = ["create_script_object"]

# 附录 03 §1 CHECK 冻结枚举：动作/对白
_KINDS = ("action", "dialogue")


def _fragment_gate(session, project, fragment_id: str) -> int:
    """SC1 片段前置（附录 03 §2：片段须 confirmed 且绑定 active 正文版本）。

    校验顺序（冻结，按序短路；命中任一即 422 precondition_failed，
    details 形状 = {"reason", "blocked_by":{"kind","id","revision"}}）：
    1. 片段不存在**或**不属于本项目 → reason=fragment_not_found
       （两路同形状，不泄漏存在性）；
    2. 绑定版本 != 项目 active 正文版本 → reason=source_revision_inactive
       （同 fragment_service/scopes.py 既有裁定，含已确认但待复核的片段）；
    3. 已退役 → reason=retired；
    4. 持久 state != 'confirmed' → reason=not_confirmed。

    返回片段当前 revision 号（= 响应 fragment.revision，并写入
    ScriptRevision.source_fragment_revision 出处快照）。
    """
    frag = session.scalar(select(Fragment).where(Fragment.id == fragment_id))
    if frag is None or frag.project_id != project.id:
        raise StudioAPIError.precondition_failed(
            "fragment_not_found",
            {"kind": "fragment", "id": fragment_id, "revision": None},
            "该片段不存在于当前项目，不能新建剧本对象。",
        )

    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    current = rv.revision
    blocked_by = {"kind": "fragment", "id": fragment_id, "revision": current}

    if frag.source_revision_id != project.active_source_revision_id:
        raise StudioAPIError.precondition_failed(
            "source_revision_inactive",
            blocked_by,
            "该片段绑定的正文版本不是项目当前活跃版本，不能新建剧本对象。",
        )
    if frag.state == "retired":
        raise StudioAPIError.precondition_failed(
            "retired", blocked_by, "该片段已退役，不能新建剧本对象。"
        )
    if frag.state != "confirmed":
        raise StudioAPIError.precondition_failed(
            "not_confirmed", blocked_by, "该片段尚未确认，不能新建剧本对象。"
        )
    return current


def _validate(kind: str, text: str, seq: int | None) -> None:
    """SC1 请求字段校验（附录 03 §2）：text 去空白后为空 / kind 非枚举 /
    seq 非正整数 → 422 validation_failed（一次返回全部 violations）。"""
    violations: list[dict[str, str]] = []
    if not isinstance(text, str) or not text.strip():
        violations.append(
            {"field": "text", "rule": "required", "message": "动作/对白文本不能为空。"}
        )
    if kind not in _KINDS:
        violations.append(
            {
                "field": "kind",
                "rule": "enum",
                "message": "kind 只能是 'action' 或 'dialogue'。",
            }
        )
    # bool 是 int 子类，显式排除（True/False 不是合法 seq）
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 1):
        violations.append(
            {"field": "seq", "rule": "positive", "message": "seq 必须是正整数。"}
        )
    if violations:
        raise StudioAPIError.validation_failed(violations)


def _dto(
    object_id: str,
    kind: str,
    text: str,
    speaker: str | None,
    adaptation_note: str | None,
    seq: int,
    fragment_id: str,
    fragment_revision: int,
) -> dict:
    """SC1 成功/重放响应体（契约 v2.9 冻结字段集）。"""
    return {
        "object_ref": {"kind": "script_object", "id": object_id, "revision": 1},
        "kind": kind,
        "text": text,
        "speaker": speaker,
        "adaptation_note": adaptation_note,
        "seq": seq,
        "fragment": {
            "kind": "fragment",
            "id": fragment_id,
            "revision": fragment_revision,
        },
        "current_revision": 1,
    }


def _same_command(
    payload: dict,
    fragment_id: str,
    kind: str,
    text: str,
    speaker: str | None,
    adaptation_note: str | None,
    seq: int | None,
) -> bool:
    """重放内容比对（附录 00 §3）：同一逻辑命令（同 command_id）重试时不变。

    seq 缺省（None）表示"片段内下一个空位"由服务端裁定，重放时不参与比对
    （首次已分配的 seq 由 result_payload 原样返回）。
    """
    frag = payload.get("fragment") or {}
    if frag.get("id") != fragment_id:
        return False
    if payload.get("kind") != kind:
        return False
    if payload.get("text") != text:
        return False
    if payload.get("speaker") != speaker:
        return False
    if payload.get("adaptation_note") != adaptation_note:
        return False
    if seq is not None and payload.get("seq") != seq:
        return False
    return True


def create_script_object(
    session,
    user,
    pid,
    fragment_id,
    kind,
    text,
    speaker,
    adaptation_note,
    seq,
    command_id,
) -> dict:
    """SC1 新建剧本草稿对象（附录 03 §2，契约 v2.9）。

    校验顺序（冻结，按序短路；与 sources/service.py、scopes.py 同约定）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏存在性）；
    2. 字段校验 → 422 validation_failed（text/kind/seq，见 _validate）；
    3. 片段前置 → 422 precondition_failed（见 _fragment_gate）；
    4. 幂等（附录 00 §3）：(owner_id, command_id) 记录存在且内容一致
       → 重放：返回首次 result_payload（附内部标记 ``_replay=True`` 由路由转
       200），零写零 commit；不一致 → 422 validation_failed
       （field=command_id, rule=reused）；
    5. seq 解析（主模型冻结）：缺省或 null → 片段内 max(seq)+1（无则 1）；
       显式给出且同 (fragment_id, seq) 已占用 → 422 validation_failed
       （field=seq, rule=taken，message 给出已占用对象 id）。

    写事务（单事务，无半次保存；不采用、不生成、不写状态列）：
    ScriptObject（current_revision_id 指向同事务新建的 revision，FK 环
    DEFERRED 提交时校验）+ ScriptRevision（revision=1，
    source_fragment_revision=片段当前 revision 出处快照）+ CommandRecord
    （result_payload = 完整 201 体），同一 ``session.begin()``；
    IntegrityError（并发同 seq 竞态）→ 回滚并映射 422 rule=taken。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 字段校验
    _validate(kind, text, seq)

    # 3) 片段前置（confirmed + 绑定 active 正文版本 + 未退役）
    fragment_revision = _fragment_gate(session, project, fragment_id)

    # 4) 幂等（读查询，先于写事务）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if _same_command(
            payload, fragment_id, kind, text, speaker, adaptation_note, seq
        ):
            # 重放：返回首次 result_payload，零写零 commit
            return dict(payload, _replay=True)
        # 同 command_id 不同内容（冻结码表无专用码，validation_failed 承载）
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "command_id",
                    "rule": "reused",
                    "message": "command_id 已被其他命令使用，请生成新的。",
                }
            ]
        )

    # 5) seq 解析：缺省/null = 片段内下一个空位；显式给出须未占用
    if seq is None:
        max_seq = session.scalar(
            select(func.max(ScriptObject.seq)).where(
                ScriptObject.fragment_id == fragment_id
            )
        )
        seq = (max_seq or 0) + 1
    else:
        occupied = session.scalar(
            select(ScriptObject.id).where(
                ScriptObject.fragment_id == fragment_id,
                ScriptObject.seq == seq,
            )
        )
        if occupied is not None:
            raise StudioAPIError.validation_failed(
                [
                    {
                        "field": "seq",
                        "rule": "taken",
                        "message": f"片段内 seq={seq} 已被对象 {occupied} 占用。",
                    }
                ]
            )

    session.commit()  # 结束读事务；写阶段另起事务

    now = time.time()
    object_id = make_ulid()
    revision_id = make_ulid()
    response = _dto(
        object_id,
        kind,
        text,
        speaker,
        adaptation_note,
        seq,
        fragment_id,
        fragment_revision,
    )
    try:
        with session.begin():
            # 写序：object → revision（FK 环 DEFERRED 同事务，提交时校验）
            # → CommandRecord；无任何采用/生成/状态列写入
            session.add(
                ScriptObject(
                    id=object_id,
                    project_id=pid,
                    fragment_id=fragment_id,
                    kind=kind,
                    seq=seq,
                    speaker=speaker,
                    current_revision_id=revision_id,
                    created_at=now,
                    updated_at=now,
                    retired_at=None,
                )
            )
            session.add(
                ScriptRevision(
                    id=revision_id,
                    object_id=object_id,
                    revision=1,
                    text=text,
                    speaker=speaker,
                    adaptation_note=adaptation_note,
                    source_fragment_revision=fragment_revision,
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
                    result_payload=json.dumps(response, ensure_ascii=False),
                    created_at=now,
                )
            )
    except IntegrityError:
        # 上下文管理器已回滚。UQ(fragment_id, seq) 冲突 = 并发赢家已占该 seq：
        # 重读占用者并映射 422 validation_failed(field=seq, rule=taken)
        session.rollback()
        occupied = session.scalar(
            select(ScriptObject.id).where(
                ScriptObject.fragment_id == fragment_id,
                ScriptObject.seq == seq,
            )
        )
        if occupied is not None and occupied != object_id:
            raise StudioAPIError.validation_failed(
                [
                    {
                        "field": "seq",
                        "rule": "taken",
                        "message": f"片段内 seq={seq} 已被对象 {occupied} 占用。",
                    }
                ]
            )
        raise StudioAPIError.internal()

    return response
