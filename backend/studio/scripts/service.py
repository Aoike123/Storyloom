"""Storyloom Studio — 剧本域服务层（附录 03 §2 命令的服务端实现）。

SCRIPT-01-a / SCRIPT-02 逐子卡填充（本卡实现 SC1/SC4 与 SC6）：
SC1 新建剧本草稿对象（action/dialogue），记录出处，不采用。
SC4 修订当前草稿（追加 revision），不触碰采用。
SC6 采用片段剧本 revision（preview/apply），新增 ScriptAdoption（链尾），
不自动生成分镜、不改任何已冻结镜头输入。

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
import hashlib
import json
import time

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..projects.models import StudioProject
from ..reviews.impact import compute_script_adopt_impact
from ..reviews.models import ChangePreview, ReviewDecision
from ..sources.fragment_models import Fragment, FragmentRevision
from .models import ScriptAdoption, ScriptObject, ScriptRevision

__all__ = [
    "create_script_object",
    "revise_script_object",
    "preview_script_adopt",
    "apply_script_adopt",
    "_UNSET",
]

# 附录 03 §1 CHECK 冻结枚举：动作/对白
_KINDS = ("action", "dialogue")

# SC4 请求体「缺席 ≠ null」哨兵（api.py 的 Pydantic 模型默认值）：
# 字段缺席 → 保持原值；显式 null → 清空（speaker/adaptation_note）；
# text 显式 null 视同缺席（保持原值）。_UNSET 标记「该字段未在请求体出现」，
# 与显式 None（清空）区分。
_UNSET = object()


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


def _current_revision_no(session, obj: ScriptObject) -> int:
    """对象当前草稿 revision 号（= current_revision_id 行的 revision）。

    current_revision_id 为 NOT NULL FK，理论必命中；缺失 = 数据损坏，
    按冻结协议映射 internal_error，不泄露细节。
    """
    rev = session.get(ScriptRevision, obj.current_revision_id)
    if rev is None:  # 理论上不可达
        raise StudioAPIError.internal()
    return rev.revision


def _fragment_revise_gate(session, project, fragment_id: str) -> int:
    """SC4 片段门禁（附录 03 §2 SC4：只列「片段非 active」，不校验 confirmed）。

    校验顺序（冻结，按序短路；命中任一即 422 precondition_failed，
    details 形状 = {"reason", "blocked_by":{"kind","id","revision"}}）：
    1. 绑定版本 != 项目 active 正文版本 → reason=source_revision_inactive；
    2. 已退役 → reason=retired。
    **不校验 confirmed**（SC4 只列「片段非 active」，candidate/confirmed 均可修订）。

    返回片段当前 revision 号（写入新 ScriptRevision.source_fragment_revision 出处快照）。
    """
    frag = session.scalar(select(Fragment).where(Fragment.id == fragment_id))
    if frag is None:  # 对象行 fragment_id 是 FK，理论必命中；缺失 = 数据损坏
        raise StudioAPIError.internal()
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达（current_revision_id NOT NULL FK）
        raise StudioAPIError.internal()
    current = rv.revision
    blocked_by = {"kind": "fragment", "id": fragment_id, "revision": current}
    if frag.source_revision_id != project.active_source_revision_id:
        raise StudioAPIError.precondition_failed(
            "source_revision_inactive",
            blocked_by,
            "该片段绑定的正文版本不是项目当前活跃版本，不能修订。",
        )
    if frag.state == "retired":
        raise StudioAPIError.precondition_failed(
            "retired", blocked_by, "该片段已退役，不能修订。"
        )
    return current


def _validate_revise(text, speaker, adaptation_note, expected_revision) -> None:
    """SC4 请求字段校验（附录 03 §2 SC4）：

    - 三者全缺席（text 缺席/null、speaker 缺席、adaptation_note 缺席）→
      field=payload, rule=empty；
    - text 非 null 但去空白后为空 → field=text, rule=blank；
    - speaker 显式提供且 >80 → field=speaker, rule=too_long；
    - adaptation_note 显式提供且 >300 → field=adaptation_note, rule=too_long；
    - expected_revision 非正整数（含 bool/非 int/0/负）→ field=expected_revision,
      rule=positive。
    一次返回全部 violations（附录 00 §4 validation_failed 形状）。
    """
    violations: list[dict[str, str]] = []
    # 三者全缺席（text 显式 null 视同缺席）→ empty
    if text is None and speaker is _UNSET and adaptation_note is _UNSET:
        violations.append(
            {
                "field": "payload",
                "rule": "empty",
                "message": "至少提供一个要修订的字段（text/speaker/adaptation_note）。",
            }
        )
    # text 显式提供（非 null）但去空白后为空 → blank
    if text is not None and not text.strip():
        violations.append(
            {"field": "text", "rule": "blank", "message": "text 不能为空白。"}
        )
    # speaker 显式提供（非 _UNSET 且非 null）且 >80 → too_long
    if speaker is not _UNSET and speaker is not None and len(speaker) > 80:
        violations.append(
            {
                "field": "speaker",
                "rule": "too_long",
                "message": "speaker 长度不能超过 80。",
            }
        )
    # adaptation_note 显式提供（非 _UNSET 且非 null）且 >300 → too_long
    if (
        adaptation_note is not _UNSET
        and adaptation_note is not None
        and len(adaptation_note) > 300
    ):
        violations.append(
            {
                "field": "adaptation_note",
                "rule": "too_long",
                "message": "adaptation_note 长度不能超过 300。",
            }
        )
    # expected_revision 非正整数（bool 是 int 子类，显式排除；非 int / 0 / 负）
    if (
        expected_revision is None
        or isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        violations.append(
            {
                "field": "expected_revision",
                "rule": "positive",
                "message": "expected_revision 必须是正整数。",
            }
        )
    if violations:
        raise StudioAPIError.validation_failed(violations)


def _resolve(current_value, requested):
    """SC4 缺席/null 语义解析：缺席（_UNSET）取现值；显式 null 清空；显式值取请求值。"""
    if requested is _UNSET:
        return current_value
    if requested is None:
        return None
    return requested


def _dto_revise(
    object_id, kind, text, speaker, adaptation_note, seq, fragment_id,
    fragment_revision, revision,
) -> dict:
    """SC4 成功 DTO（与 SC1 同形，revision 号为新值）。"""
    return {
        "object_ref": {"kind": "script_object", "id": object_id, "revision": revision},
        "kind": kind,
        "text": text,
        "speaker": speaker,
        "adaptation_note": adaptation_note,
        "seq": seq,
        "fragment": {"kind": "fragment", "id": fragment_id, "revision": fragment_revision},
        "current_revision": revision,
    }


def _same_command_revise(payload, oid, text, speaker, adaptation_note) -> bool:
    """SC4 幂等比对（附录 00 §3）：同 command_id 重放时，result_payload 的
    object_ref.id 与已解析的 text/speaker/adaptation_note 逐项等于本次有效载荷
    （即与首次一致）= 重放。"""
    ref = payload.get("object_ref") or {}
    if ref.get("id") != oid:
        return False
    if payload.get("text") != text:
        return False
    if payload.get("speaker") != speaker:
        return False
    if payload.get("adaptation_note") != adaptation_note:
        return False
    return True


def revise_script_object(
    session,
    user,
    pid: str,
    oid: str,
    text,
    speaker,
    adaptation_note,
    expected_revision: int,
    command_id: str,
) -> dict:
    """SC4 修订当前草稿（附录 03 §2 SC4）：只改当前草稿 revision，过期保存拒绝；
    **不触碰采用**（零 ScriptAdoption 写入）、不启动任何生成。

    语义（冻结）：
    - 缺席 ≠ null：text/speaker/adaptation_note 字段缺席 = 保持原值；
      显式 null = 清空（speaker/adaptation_note）；text 显式 null 视同缺席。
    - 三者全缺席 → 422 validation_failed(field=payload, rule=empty)。
    - 新 ScriptRevision（revision=当前+1，缺席字段取现值，
      source_fragment_revision=保存时刻片段 revision）+ 对象行
      current_revision_id/speaker 镜像/updated_at + CommandRecord，同一事务。

    校验顺序（冻结，按序短路；**幂等先于 CAS**，与 sources/fragment_service.py
    F1/F4/F6/F7/F8 先例对齐——字面重试即使 expected_revision 仍为首次值也命中重放）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏存在性）；
    2. 对象不存在/跨项目 → 404 not_found(kind=script_object)（两情形同形状）；
    3. 对象 retired → 422 precondition_failed(reason=retired)；
    4. 字段校验（text 空白 / speaker>80 / adaptation_note>300 /
       expected_revision 非正整数；三者全缺席 → payload.empty）；
    5. 片段门禁（source_revision_inactive / retired；**不校验 confirmed**）；
    6. 解析缺席/null 语义（读当前草稿 revision，纯读，先于幂等/CAS）；
    7. 幂等（附录 00 §3，CAS 之前、写之前）：同 command_id 同内容 → 重放
       返回首次 result_payload（附内部标记 ``_replay=True``，本命令成功码即 200，
       重放亦 200），零写零 commit；不同内容 → 422 validation_failed
       （field=command_id, rule=reused）；
    8. CAS：expected_revision != 当前草稿 revision → 409 revision_conflict
       （object={kind:script_object,id}，expected/actual=真实当前值，附录 00 §4 形状）。

    成功体（与 SC1 DTO 同形，revision 号为新值）：
    ``{"object_ref": {"kind":"script_object","id":oid,"revision":新 revision},
    "kind", "text", "speaker", "adaptation_note", "seq",
    "fragment": {"kind":"fragment","id":fragment_id,"revision":片段 revision},
    "current_revision": 新 revision}``
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 对象（不存在/跨项目同形状 404，不泄漏存在性）
    obj = session.scalar(select(ScriptObject).where(ScriptObject.id == oid))
    if obj is None or obj.project_id != pid:
        raise StudioAPIError.not_found("script_object", oid)

    # 3) 对象 retired → 422 precondition_failed
    if obj.retired_at is not None:
        raise StudioAPIError.precondition_failed(
            "retired",
            {"kind": "script_object", "id": oid, "revision": _current_revision_no(session, obj)},
            "该对象已退役，不能修订。",
        )

    # 4) 字段校验（一次返回全部 violations）
    _validate_revise(text, speaker, adaptation_note, expected_revision)

    # 5) 片段门禁（source_revision_inactive / retired；不校验 confirmed）
    fragment_revision = _fragment_revise_gate(session, project, obj.fragment_id)

    # 6) 解析缺席/null 语义（纯读，先于幂等/CAS）：基于当前草稿 revision 的现值
    current = _current_revision_no(session, obj)
    current_rev = session.get(ScriptRevision, obj.current_revision_id)
    if current_rev is None:  # 理论上不可达
        raise StudioAPIError.internal()
    resolved_text = current_rev.text if text is None else text
    resolved_speaker = _resolve(current_rev.speaker, speaker)
    resolved_note = _resolve(current_rev.adaptation_note, adaptation_note)

    # 7) 幂等（读查询，先于 CAS 与写事务；与 F1/F4/F6/F7/F8 先例对齐）：
    #    同 command_id 同内容 → 重放（字面重试即使 expected_revision 仍为首次值
    #    也命中重放，不撞 CAS）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if _same_command_revise(payload, oid, resolved_text, resolved_speaker, resolved_note):
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

    # 8) CAS：expected_revision 必须等于当前草稿 revision（幂等之后）
    if expected_revision != current:
        raise StudioAPIError.revision_conflict("script_object", oid, expected_revision, current)

    session.commit()  # 结束读事务；写阶段另起事务

    # 9) 写事务：新 ScriptRevision + 对象行镜像 + CommandRecord，同一事务
    now = time.time()
    new_revision = current + 1
    revision_id = make_ulid()
    response = _dto_revise(
        obj.id, obj.kind, resolved_text, resolved_speaker, resolved_note,
        obj.seq, obj.fragment_id, fragment_revision, new_revision,
    )
    try:
        with session.begin():
            session.add(
                ScriptRevision(
                    id=revision_id,
                    object_id=obj.id,
                    revision=new_revision,
                    text=resolved_text,
                    speaker=resolved_speaker,
                    adaptation_note=resolved_note,
                    source_fragment_revision=fragment_revision,
                    created_at=now,
                )
            )
            # 对象行：current_revision_id 指向新 revision；speaker 镜像；updated_at
            obj.current_revision_id = revision_id
            obj.speaker = resolved_speaker
            obj.updated_at = now
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
        # UQ(object_id, revision) 或 UQ(owner_id, command_id) 并发竞态：
        # 赢家已提交 → 重读其 CommandRecord 映射重放语义；
        # 其他完整性冲突（本卡不应出现）→ 冻结 internal_error，不泄露细节
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


# ─────────────── SC6 采用剧本 revision（preview/apply，附录 03 §2 + 00 §5） ───────────────

# 预览 TTL 30 分钟（附录 07 RV1：服务层计算后写入；与 F7–F10 同值）
_PREVIEW_TTL = 1800

# 片段门禁 reason → 面向用户 message（preview 步骤 4~7，按序短路）
_GATE_MESSAGES = {
    "not_confirmed": "该片段尚未确认，不能采用剧本 revision。",
    "source_revision_inactive": (
        "该片段绑定的正文版本不是项目当前活跃版本，不能采用剧本 revision。"
    ),
    "no_objects": "该片段下没有剧本对象，不能采用。",
    "retired": "该片段已退役，不能采用剧本 revision。",
}

# apply 片段门禁重校验失配项形状（preview_stale.conflicts 项；规格未冻结该项
# 形状，取最接近既有 F7–F10 conflicts 的 {object, expected, actual} 三键形状：
# object 恒为相关对象 ref；not_confirmed/retired 的 expected/actual 为片段状态，
# source_revision_inactive 为正文版本 id，no_objects 为存在性标记）。
_GATE_CONFLICT_MSG = "片段状态已变化，请重新预览。"


def _adoption_tail(session, fragment_id: str):
    """片段当前采用 = 采用链链尾（附录 03 §1：按 created_at 取该 fragment 最新行；
    ULID 单调，同刻以 id 降序兜底）。无采用 → None。"""
    return session.scalars(
        select(ScriptAdoption)
        .where(ScriptAdoption.fragment_id == fragment_id)
        .order_by(ScriptAdoption.created_at.desc(), ScriptAdoption.id.desc())
        .limit(1)
    ).first()


def _fragment_current_revision(session, frag) -> int:
    """片段当前 revision 号（current_revision_id 为 NOT NULL FK，缺失 = 数据损坏）。"""
    rv = session.get(FragmentRevision, frag.current_revision_id)
    if rv is None:  # 理论上不可达
        raise StudioAPIError.internal()
    return rv.revision


def _has_script_objects(session, fragment_id: str) -> bool:
    return (
        session.scalar(
            select(ScriptObject.id)
            .where(ScriptObject.fragment_id == fragment_id)
            .limit(1)
        )
        is not None
    )


def _script_adopt_gate(session, project, frag, current_revision) -> list[tuple[str, dict]]:
    """SC6 片段门禁（preview 步骤 4~7 的**全部**失配项，不短路，按冻结顺序）。

    4. state != 'confirmed'（**retired 除外，置末**）→ not_confirmed；
    5. source_revision_id != project.active_source_revision_id → source_revision_inactive；
    6. 片段下无 ScriptObject → no_objects；
    7. state == 'retired' → retired（置末）。

    每项 details.blocked_by 形状冻结 = {"kind":"fragment","id":fid,"revision":current}。
    """
    blocked_by = {"kind": "fragment", "id": frag.id, "revision": current_revision}
    failures: list[tuple[str, dict]] = []
    if frag.state not in ("confirmed", "retired"):
        failures.append(("not_confirmed", blocked_by))
    if frag.source_revision_id != project.active_source_revision_id:
        failures.append(("source_revision_inactive", blocked_by))
    if not _has_script_objects(session, frag.id):
        failures.append(("no_objects", blocked_by))
    if frag.state == "retired":
        failures.append(("retired", blocked_by))
    return failures


def _gate_conflict(reason: str, frag, project) -> dict:
    """apply 片段门禁重校验的单个失配项（preview_stale.conflicts 项）。"""
    if reason == "not_confirmed":
        return {
            "object": {"kind": "fragment", "id": frag.id},
            "expected": "confirmed",
            "actual": frag.state,
        }
    if reason == "source_revision_inactive":
        return {
            "object": {"kind": "source", "id": project.active_source_revision_id},
            "expected": project.active_source_revision_id,
            "actual": frag.source_revision_id,
        }
    if reason == "no_objects":
        return {
            "object": {"kind": "fragment", "id": frag.id},
            "expected": "objects_present",
            "actual": "no_objects",
        }
    return {  # retired（置末）
        "object": {"kind": "fragment", "id": frag.id},
        "expected": "confirmed",
        "actual": "retired",
    }


def _baseline_conflicts(expected: list, actual: list) -> list[dict]:
    """expected_object_revisions 与权威快照逐项全等（object_id/revision/seq/kind）比对，
    返回**全部**失配项（规格未冻结该项形状：expected/actual 取整条快照项，
    object.id 取可解析一侧的 object_id）。"""
    conflicts: list[dict] = []
    for i in range(max(len(expected), len(actual))):
        e = expected[i] if i < len(expected) else None
        a = actual[i] if i < len(actual) else None
        if e != a:
            ref = a if isinstance(a, dict) else (e if isinstance(e, dict) else {})
            conflicts.append(
                {
                    "object": {
                        "kind": "script_object",
                        "id": ref.get("object_id") or "",
                    },
                    "expected": e,
                    "actual": a,
                }
            )
    return conflicts


def _script_object_snapshot(session, fragment_id: str) -> tuple[list[dict], str]:
    """片段全部对象按 seq 升序的权威快照 + content_hash（冻结算法）。

    快照项 = {"object_id","revision","seq","kind"}（= ScriptAdoption.object_revisions
    与 apply 200 DTO 的 object_revisions 格式，附录 03 §1）；
    content_hash = sha256(按 seq 拼接 f"{revision}:{sha256(text.encode('utf-8'))}")
    （附录 03 §1 冻结指纹）。不写任何行；revision 缺失（数据损坏）→ internal_error。
    """
    objs = session.scalars(
        select(ScriptObject)
        .where(ScriptObject.fragment_id == fragment_id)
        .order_by(ScriptObject.seq)
    ).all()
    snapshot: list[dict] = []
    parts: list[str] = []
    for o in objs:
        rev = session.get(ScriptRevision, o.current_revision_id)
        if rev is None:  # 理论上不可达
            raise StudioAPIError.internal()
        snapshot.append(
            {"object_id": o.id, "revision": rev.revision, "seq": o.seq, "kind": o.kind}
        )
        parts.append(
            f"{rev.revision}:{hashlib.sha256(rev.text.encode('utf-8')).hexdigest()}"
        )
    content_hash = hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()
    return snapshot, content_hash


def preview_script_adopt(
    session, user, pid, fid, target_fragment_id, command_id
) -> dict:
    """SC6 预览采用片段剧本 revision（附录 03 §2 SC6 + 00 §5；preview 本身是一次
    幂等写：只写 ChangePreview + CommandRecord，**零业务副作用**）。

    校验顺序（冻结，按序短路）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏）；
    2. 片段不存在/跨项目 → 404 not_found(kind=fragment)（不泄漏）；
    3. target_fragment_id != fid → 422 validation_failed(field=target_fragment_id,
       rule=mismatch)；
    4.~7. 片段门禁（_script_adopt_gate，按序短路：not_confirmed →
       source_revision_inactive → no_objects → retired），命中 → 422
       precondition_failed{reason, blocked_by={fragment,fid,current}}；
    8. 幂等（00 §3，先于写）：同 (owner, command_id) 且首次 preview 行的
       payload.target_fragment_id == fid → 200 原 result_payload（附内部标记
       ``_replay=True``，零写零 commit）；异内容 → 422 validation_failed
       (field=command_id, rule=reused)。首次 preview 的 target 只持久在
       ChangePreview.payload（200 体不含该字段），故从该行取证比对。

    baseline（冻结、确定性）= JSON 数组：片段全部对象按 seq 升序的
    {kind:'script_object', id, revision} + （若已有采用）末尾链尾
    {kind:'script_adoption', id, revision:null}。
    impact = compute_script_adopt_impact(session, pid, fid)（三类清单）。

    返回 200 体，**恰 4 字段**：``{preview_id, kind:'script_adopt', baseline,
    impact}``（记录 state=pending 不外露）；重放附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    # 3) target 必须与路径片段一致
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

    current = _fragment_current_revision(session, frag)

    # 4~7) 片段门禁（按序短路：命中首个即 422 precondition_failed）
    failures = _script_adopt_gate(session, project, frag, current)
    if failures:
        reason, blocked_by = failures[0]
        raise StudioAPIError.precondition_failed(
            reason, blocked_by, _GATE_MESSAGES[reason]
        )

    # 8) 幂等（冻结顺序：门禁之后、写之前）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        prev = (
            session.get(ChangePreview, payload["preview_id"])
            if payload.get("kind") == "script_adopt" and payload.get("preview_id")
            else None
        )
        prev_payload = (
            json.loads(prev.payload) if prev is not None and prev.payload else {}
        )
        if (
            prev is not None
            and prev.project_id == pid
            and prev_payload.get("target_fragment_id") == fid
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

    # 读事务快照：baseline（对象按 seq 升序 + 链尾 adoption ref）+ impact
    snapshot, _ = _script_object_snapshot(session, fid)
    baseline = [
        {"kind": "script_object", "id": o["object_id"], "revision": o["revision"]}
        for o in snapshot
    ]
    tail = _adoption_tail(session, fid)
    if tail is not None:
        baseline.append(
            {"kind": "script_adoption", "id": tail.id, "revision": None}
        )
    impact = compute_script_adopt_impact(session, pid, fid)
    session.commit()  # 结束读事务；写阶段另起事务

    # 9) 写事务：preview 是一次幂等写（单事务 + CommandRecord，无半次保存）
    now = time.time()
    preview_id = make_ulid()
    body = {
        "preview_id": preview_id,
        "kind": "script_adopt",
        "baseline": baseline,
        "impact": impact,
    }
    with session.begin():
        session.add(
            ChangePreview(
                id=preview_id,
                project_id=pid,
                owner_id=user.id,
                kind="script_adopt",
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


def apply_script_adopt(
    session,
    user,
    pid,
    fid,
    preview_id,
    command_id,
    expected_previous_adoption_id,
    expected_object_revisions,
) -> dict:
    """SC6 应用采用（附录 03 §2 SC6 + 00 §5）：新增一条 ScriptAdoption（链尾），
    **不自动生成分镜**、不改任何已冻结镜头输入、不改既有 adoption/对象/版本行。

    校验顺序（冻结，按序短路）：
    1. 项目 404；2. 片段 404（形状同 preview 步骤 1/2）；
    3. preview 不存在/跨项目 → 404 not_found(kind=preview)；kind != 'script_adopt'
       或 payload.target_fragment_id != fid → 422 validation_failed
       (field=target_fragment_id, rule=mismatch，同 F7 apply 步骤 3 形状)；
    4. 幂等（00 §3；**先于状态检查**——原命令重放不受预览终态阻断，F7/T43 惯例）：
       同 (owner, command_id) 且首次 payload object_ref.kind=='script_adoption'、
       fragment.id==fid、previous_adoption_id==expected_previous_adoption_id → 200
       原 result_payload（零写零 commit）；异内容 → 422 validation_failed
       (field=command_id, rule=reused)；
    5. preview 非 pending（或已过 TTL）→ 409 preview_stale{preview_id,
       conflicts:[{object:{preview,id}, expected:'pending', actual}]}：'expired' 或
       (pending 且 now>expires_at) → actual='expired' 并**惰性置 state='expired'**；
       'applied'/'rejected'/'superseded' → actual=该 state（终态不可逆，不再改写）；
    6. CAS：expected_previous_adoption_id != 当前链尾 id → 409 revision_conflict
       （object={kind:'script_adoption', id:链尾 id 或 ""}，expected/actual 真实值）；
    7. 片段门禁重校验（preview 步骤 4~7 全部；写事务内以权威值为准；任一失配 →
       409 preview_stale，conflicts 列出全部失配项）；
    8. baseline 比对（**写事务内重读为权威**）：expected_object_revisions 与当前
       片段全部对象按 seq 升序的快照逐项全等（object_id/revision/seq/kind）失配 →
       409 preview_stale{preview_id, conflicts:[全部失配项]} 且 preview 置
       state='superseded' + resolved_at；
    9. 写（**单事务，无半次保存**；写序照 F7 apply）：
       INSERT ScriptAdoption → INSERT ReviewDecision(decision='applied', preview_id,
       target_ref={kind:'script_adoption',id:新id,revision:null},
       baseline_digest=sha256(规范化 preview.baseline)) → UPDATE preview(state='applied',
       resolved_at, decision_id) → INSERT CommandRecord(result_payload=完整 200 体)。

    返回 200 体（新 adoption DTO，字段冻结）：
    ``{object_ref:{kind:'script_adoption',id,revision:null}, fragment:{kind:'fragment',
    id:fid,revision}, object_revisions:[{object_id,revision,seq,kind}], content_hash,
    previous_adoption_id, created_at}``；重放附 ``_replay=True``。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 片段：不存在或不属本项目 → 一律 404（不泄漏存在性）
    frag = session.scalar(
        select(Fragment).where(Fragment.id == fid, Fragment.project_id == pid)
    )
    if frag is None:
        raise StudioAPIError.not_found("fragment", fid)

    # 3) preview：不存在或跨项目 → 404；kind/target 失配 → 422 mismatch
    preview = session.scalar(
        select(ChangePreview).where(
            ChangePreview.id == preview_id, ChangePreview.project_id == pid
        )
    )
    if preview is None:
        raise StudioAPIError.not_found("preview", preview_id)
    pv_payload = json.loads(preview.payload) if preview.payload else {}
    if preview.kind != "script_adopt" or pv_payload.get("target_fragment_id") != fid:
        raise StudioAPIError.validation_failed(
            [
                {
                    "field": "target_fragment_id",
                    "rule": "mismatch",
                    "message": "target_fragment_id 必须与路径片段一致。",
                }
            ]
        )

    # 4) 幂等（00 §3；先于状态检查——原命令重放不受预览终态阻断，F7/T43）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        ref = payload.get("object_ref") or {}
        pfrag = payload.get("fragment") or {}
        if (
            ref.get("kind") == "script_adoption"
            and pfrag.get("id") == fid
            and payload.get("previous_adoption_id") == expected_previous_adoption_id
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

    # 6) CAS：链尾（读阶段；写事务内再复核，见下）
    tail = _adoption_tail(session, fid)
    tail_id = tail.id if tail is not None else None
    if expected_previous_adoption_id != tail_id:
        raise StudioAPIError.revision_conflict(
            "script_adoption",
            tail_id or "",
            expected_previous_adoption_id,
            tail_id,
            "片段当前采用已变化，请重新预览后重试。",
        )

    base = json.loads(preview.baseline) if preview.baseline else []
    session.commit()  # 结束读事务；写阶段另起事务

    # 7~9) 写事务：首条语句即写语句（no-op 自赋值抢 DB 级写锁，同 F7）→ 获锁后
    # 重读为权威 → 门禁重校验 → 链尾复核 → baseline 比对 → 写
    now = time.time()
    adoption_id = make_ulid()
    decision_id = make_ulid()
    if not session.in_transaction():
        session.begin()
    try:
        session.execute(
            update(Fragment)
            .where(Fragment.id == fid)
            .values(updated_at=Fragment.updated_at)
        )
        frag_now = session.get(Fragment, fid)
        current_now = _fragment_current_revision(session, frag_now)
        project_now = session.get(StudioProject, pid)

        # 7) 片段门禁重校验（preview 步骤 4~7 全部；全部收集）
        gate = _script_adopt_gate(session, project_now, frag_now, current_now)
        if gate:
            conflicts = [
                _gate_conflict(reason, frag_now, project_now) for reason, _ in gate
            ]
            session.rollback()
            err = StudioAPIError.preview_stale(preview_id, _GATE_CONFLICT_MSG)
            err.details["conflicts"] = conflicts
            raise err

        # 6b) 链尾复核（并发防护；CAS 真实值）
        tail_now = _adoption_tail(session, fid)
        tail_now_id = tail_now.id if tail_now is not None else None
        if expected_previous_adoption_id != tail_now_id:
            session.rollback()
            raise StudioAPIError.revision_conflict(
                "script_adoption",
                tail_now_id or "",
                expected_previous_adoption_id,
                tail_now_id,
                "片段当前采用已变化，请重新预览后重试。",
            )

        # 8) baseline 比对（写事务内重读为权威，失配项全部收集）
        snapshot, content_hash = _script_object_snapshot(session, fid)
        conflicts = _baseline_conflicts(expected_object_revisions, snapshot)
        if conflicts:
            session.execute(
                update(ChangePreview)
                .where(ChangePreview.id == preview_id)
                .values(state="superseded", resolved_at=now)
            )
            session.commit()
            err = StudioAPIError.preview_stale(
                preview_id, "预览基准已变化（对象快照失配），请重新预览。"
            )
            err.details["conflicts"] = conflicts
            raise err

        # 9) 执行（单事务，无半次保存；不改任何既有 adoption/对象/版本行）
        session.add(
            ScriptAdoption(
                id=adoption_id,
                project_id=pid,
                fragment_id=fid,
                object_revisions=json.dumps(snapshot, ensure_ascii=False),
                content_hash=content_hash,
                previous_adoption_id=tail_now_id,
                created_at=now,
            )
        )
        session.add(
            ReviewDecision(
                id=decision_id,
                project_id=pid,
                owner_id=user.id,
                preview_id=preview_id,
                target_ref=json.dumps(
                    {"kind": "script_adoption", "id": adoption_id, "revision": None},
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
        session.flush()  # ReviewDecision 先 INSERT（FK preview.decision_id 非 deferrable）
        session.execute(
            update(ChangePreview)
            .where(ChangePreview.id == preview_id)
            .values(state="applied", resolved_at=now, decision_id=decision_id)
        )
        body = {
            "object_ref": {
                "kind": "script_adoption",
                "id": adoption_id,
                "revision": None,
            },
            "fragment": {"kind": "fragment", "id": fid, "revision": current_now},
            "object_revisions": snapshot,
            "content_hash": content_hash,
            "previous_adoption_id": tail_now_id,
            "created_at": now,
        }
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
