"""Storyloom Studio — 分镜域服务层（附录 04 §2 命令的服务端实现）。

本模块交付 SB1 新建场与 SB3 场改名（附录 04 §2，架构契约冻结）：
`POST /api/studio/projects/{pid}/scenes` 只创建场行（studio_scenes），
`POST /api/studio/projects/{pid}/scenes/{sid}/rename` 在场行上就地改名，
两者都**不生成任何镜头**（studio_shots / studio_shot_revisions 零写）。

实现约束（冻结）：
- 场的片段归属不落在场行上：studio_scenes 无 fragment/adoption 列（附录 04
  §1 冻结），场内的片段出处由镜头的 studio_shots.script_adoption_id 承载
  （SB4 建立）。因此本命令**不接受也不需要任何片段入参**；
- 不写 studio_source_relations / studio_asset_occurrences /
  studio_asset_bindings / studio_script_adoptions；不改 studio_fragments /
  studio_projects 任何行；不写任何生成任务（StudioJob）；
- 所有写命令走 command_id 幂等（附录 00 §3，studio_command_records）；
- 属主校验：未登录 401（core.auth.require_user）；项目不存在/非属主一律
  404 not_found（不泄漏存在性，与 projects/sources 同裁定）；
- id 全部服务端 ULID（core.db.make_ulid），客户端永不生成实体 ID；
- 错误一律 raise StudioAPIError（冻结错误协议，附录 00 §4），不 raise
  HTTPException。seq 占用取全域一致先例 422 validation_failed(field=seq,
  rule=taken)（与 SC1 的 (fragment_id, seq) 唯一冲突同型，非 duplicate 409
  身份冲突语义）。
"""
import json
import time

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..projects.models import StudioProject
from .models import Scene

__all__ = ["create_scene", "rename_scene"]

# 附录 04 §1：场名 text(120)；§2 SB1 请求体 name 去空白后保存
_NAME_MAX_LENGTH = 120

# 内部标记：命中幂等重放时附在返回值上，由路由层 pop 后转 200（附录 00 §3）
_REPLAY = "_replay"


def _taken_violation(seq: int, occupant_id: str) -> StudioAPIError:
    """seq 占用冲突（422 validation_failed / field=seq / rule=taken）。

    message 给出已占用场的 id（照抄 scripts/service.py SC1 同型语义）。
    """
    return StudioAPIError.validation_failed(
        [
            {
                "field": "seq",
                "rule": "taken",
                "message": f"项目内 seq={seq} 已被场 {occupant_id} 占用。",
            }
        ]
    )


def _validate(name, seq) -> str:
    """SB1 请求字段校验（附录 04 §2）：name 去空白后为空 / 超 120，
    显式 seq 非正整数（bool 显式排除）→ 422 validation_failed。

    返回 trim 后的 name（**保存 trim 后的值**）。

    请求体形态（seq 为 JSON bool / 小数 / 字符串等根本性类型错误）在路由层
    Pydantic 已拦截为 422（见 storyboard/api.py 的 field_validator）；进入本
    函数的 seq 要么是 None，要么是 int（bool 已排除），此处仍按 SC1 先例
    显式排除 bool，保证服务层可独立校验。
    """
    violations: list[dict[str, str]] = []
    if not isinstance(name, str) or not name.strip():
        violations.append(
            {"field": "name", "rule": "required", "message": "场名不能为空。"}
        )
    name_s = name.strip() if isinstance(name, str) else name
    if isinstance(name_s, str) and len(name_s) > _NAME_MAX_LENGTH:
        violations.append(
            {
                "field": "name",
                "rule": "max_length",
                "message": f"场名最长 {_NAME_MAX_LENGTH} 个字符。",
            }
        )
    # bool 是 int 子类，显式排除（True/False 不是合法 seq，参 SC1）
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 1):
        violations.append(
            {"field": "seq", "rule": "invalid", "message": "seq 必须是正整数。"}
        )
    if violations:
        raise StudioAPIError.validation_failed(violations)
    return name_s


def _dto(scene: Scene) -> dict:
    """SB1 成功/重放响应体（附录 04 §2；主模型冻结，恰好 7 键）。

    revision 恒 null：场就地改名、无版本（附录 04 §1）；
    seq 与 layout_cas_revision 都是真实列值。
    """
    return {
        "object_ref": {"kind": "scene", "id": scene.id, "revision": None},
        "name": scene.name,
        "seq": scene.seq,
        "layout_cas_revision": scene.layout_cas_revision,
        "project_id": scene.project_id,
        "created_at": scene.created_at,
        "updated_at": scene.updated_at,
    }


def _replay_payload(session, record: CommandRecord, pid, name_s, seq) -> dict | None:
    """幂等命中判定（附录 00 §3）：命中返回首次 result_payload（附 _replay）。

    一致 = 记录存在 + 首次 payload 的 object_ref.id 存在（且该场仍属本项目）
    + name == trim 后 name；显式给出 seq 时还须与首次分配的 seq 全等
    （seq 缺省/null = 服务端裁定的"下一个空位"，不参与比对，参 SC1
    _same_command 注释）。不一致返回 None，由调用方映射 422 reused。
    """
    payload = json.loads(record.result_payload) if record.result_payload else {}
    ref = payload.get("object_ref") or {}
    scene_id = ref.get("id")
    if not isinstance(scene_id, str):
        return None
    scene = session.get(Scene, scene_id)
    if scene is None or scene.project_id != pid:
        return None
    if payload.get("name") != name_s:
        return None
    if seq is not None and payload.get("seq") != seq:
        return None
    return dict(payload, **{_REPLAY: True})


def _reused() -> StudioAPIError:
    """同 command_id 异内容（冻结码表无专用码，validation_failed 承载；
    参 SC1/P1 既有裁定）。"""
    return StudioAPIError.validation_failed(
        [
            {
                "field": "command_id",
                "rule": "reused",
                "message": "command_id 已被其他命令使用，请生成新的。",
            }
        ]
    )


def create_scene(session, user, pid, name, seq, command_id) -> dict:
    """SB1 新建场（附录 04 §2）。

    校验顺序（冻结，按序短路）：
    1. 项目不存在/非属主 → 404 not_found(kind=project, id=pid)（不泄漏存在性）；
    2. name 校验 → 422 validation_failed(field=name, rule=required/max_length)，
       保存 trim 后的值；
    3. 显式 seq 必须正整数（非 null；bool 显式排除）→ 422
       validation_failed(field=seq, rule=invalid)；
    4. 幂等（附录 00 §3，**先于 seq 占用检查与写**）：(owner_id, command_id)
       命中且内容一致 → 重放：返回首次 result_payload（附内部标记
       ``_replay=True`` 由路由转 200），零写零 commit；不一致 → 422
       validation_failed(field=command_id, rule=reused)；
    5. seq 解析：缺省/null → 本项目内 max(seq)+1（无场时为 1）；显式给出且
       (project_id, seq) 已占用 → 422 validation_failed(field=seq, rule=taken，
       message 给出已占用场的 id)。

    写事务（单事务）：INSERT Scene(id=ULID, layout_cas_revision=1,
    created_at=updated_at=now) + INSERT CommandRecord（result_payload = 完整
    201 体）；IntegrityError（并发同 seq 竞态 / UQ owner+command）→ 回滚；
    若命中已存在 CommandRecord → 按重放语义返回其 payload；否则重读同 seq
    占用者 → 422 rule=taken。

    **不生成镜头**、不写采用/资产/来源关系、不改片段与项目行、不启动任何
    生成任务（附录 04 §2 SB1：只建场）。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2)+3) 字段校验（name trim + 长度；seq 正整数）
    name_s = _validate(name, seq)

    # 4) 幂等（读查询，先于 seq 占用检查与写事务）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        replay = _replay_payload(session, record, pid, name_s, seq)
        if replay is not None:
            return replay
        raise _reused()

    # 5) seq 解析：缺省/null = 本项目内下一个空位；显式给出须未占用
    if seq is None:
        max_seq = session.scalar(
            select(func.max(Scene.seq)).where(Scene.project_id == pid)
        )
        seq = (max_seq or 0) + 1
    else:
        occupied = session.scalar(
            select(Scene.id).where(Scene.project_id == pid, Scene.seq == seq)
        )
        if occupied is not None:
            raise _taken_violation(seq, occupied)

    session.commit()  # 结束读事务；写阶段另起事务

    now = time.time()
    scene = Scene(
        id=make_ulid(),
        project_id=pid,
        name=name_s,  # 保存 trim 后的值
        seq=seq,
        layout_cas_revision=1,
        created_at=now,
        updated_at=now,
    )
    response = _dto(scene)
    try:
        with session.begin():
            session.add(scene)
            session.flush()  # 让 UQ(project_id, seq) / UQ(owner_id, command_id) 立刻暴露
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
        # 上下文管理器已回滚。两种竞态：
        # a) UQ(owner_id, command_id)：并发同 command 赢家已写入 → 按重放语义
        #    返回其 payload（内容一致时；异内容映射 reused）；
        # b) UQ(project_id, seq)：并发赢家已占该 seq → 重读占用者映射 422 taken。
        session.rollback()
        raced = session.scalar(
            select(CommandRecord).where(
                CommandRecord.owner_id == user.id,
                CommandRecord.command_id == command_id,
            )
        )
        if raced is not None:
            replay = _replay_payload(session, raced, pid, name_s, seq)
            if replay is not None:
                return replay
            raise _reused()
        occupied = session.scalar(
            select(Scene.id).where(Scene.project_id == pid, Scene.seq == seq)
        )
        if occupied is not None and occupied != scene.id:
            raise _taken_violation(seq, occupied)
        raise StudioAPIError.internal()

    return response


def _rename_replay_payload(record: CommandRecord, sid, name_s) -> dict | None:
    """SB3 幂等命中判定（附录 00 §3）：命中返回首次 result_payload（附 _replay）。

    一致 = 记录存在 + 首次 payload 的 object_ref.id == sid（本命令的改名目标）
    + name == trim 后 name。不一致返回 None，由调用方映射 422 reused。

    与 SB1 ``_replay_payload`` 的差异：SB1 需回读场行确认 sid 仍属本项目；
    SB3 的校验序第 2 步已判定 sid 属本项目（不存在/跨项目一律 404），故此处
    不重复回读场行（命中即同一行，无存在性可泄漏）。
    """
    payload = json.loads(record.result_payload) if record.result_payload else {}
    ref = payload.get("object_ref") or {}
    if ref.get("id") != sid:
        return None
    if payload.get("name") != name_s:
        return None
    return dict(payload, **{_REPLAY: True})


def rename_scene(session, user, pid, sid, name, command_id) -> dict:
    """SB3 场就地改名（附录 04 §2）。

    校验顺序（冻结，按序短路）：
    1. 项目不存在/非属主 → 404 not_found(kind=project, id=pid)（不泄漏存在性）；
    2. 场不存在**或**跨项目 → 一律 404 not_found(kind=scene, id=sid)（两情形
       同形状，不泄漏存在性）；
    3. name：trim 后为空 → 422 validation_failed(field=name, rule=required)；
       trim 后 > 120 → field=name, rule=max_length；**保存 trim 后的值**
       （复用 SB1 ``_validate`` 的裁定与错误形状；无 seq 分支）；
    4. 幂等（附录 00 §3，**先于写事务**）：(owner_id, command_id) 命中且首次
       result_payload 的 object_ref.id == sid 且 name 一致 → 200 重放（原 body
       附内部标记 ``_replay=True``，零写零 commit）；异内容 → 422
       validation_failed(field=command_id, rule=reused)；
    5. 写（**单事务**）：UPDATE 场 name（trim 后）与 updated_at=now +
       INSERT CommandRecord（result_payload = 完整 200 体）；IntegrityError
       （UQ owner+command 竞态）→ 回滚后命中 CommandRecord 走重放语义，否则
       internal_error。

    语义要点（附录 04 §1）：**场就地改名、无版本**——object_ref.revision 恒
    null，**不产生任何 revision 行**（studio_scenes 本就无 revision 列）；
    ``layout_cas_revision`` 是镜头/场布局重排的 CAS（SB7 专属），**改名不得
    改它**；``seq`` 与 ``created_at`` 不变，仅 name 与 updated_at 变化。
    不生成镜头、不写采用/资产/来源关系、不改其他场行、不写任何生成任务。
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 场：不存在或跨项目 → 一律 404（同形状，不泄漏存在性）
    scene = session.scalar(select(Scene).where(Scene.id == sid, Scene.project_id == pid))
    if scene is None:
        raise StudioAPIError.not_found("scene", sid)

    # 3) name 校验（复用 SB1 裁定：seq=None 时不触发 seq 分支；保存 trim 后的值）
    name_s = _validate(name, None)

    # 4) 幂等（读查询，先于写事务）
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        replay = _rename_replay_payload(record, sid, name_s)
        if replay is not None:
            return replay
        raise _reused()

    session.commit()  # 结束读事务；写阶段另起事务

    now = time.time()
    try:
        with session.begin():
            scene.name = name_s  # 保存 trim 后的值
            scene.updated_at = now
            session.flush()  # 让 UQ(owner_id, command_id) 在块内立刻暴露
            response = _dto(scene)
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
        # 上下文管理器已回滚。唯一可能的竞态是 UQ(owner_id, command_id)：
        # 并发同 command 的赢家已写入 → 命中则按重放语义返回其 payload。
        session.rollback()
        raced = session.scalar(
            select(CommandRecord).where(
                CommandRecord.owner_id == user.id,
                CommandRecord.command_id == command_id,
            )
        )
        if raced is not None:
            replay = _rename_replay_payload(raced, sid, name_s)
            if replay is not None:
                return replay
            raise _reused()
        raise StudioAPIError.internal()

    return response
