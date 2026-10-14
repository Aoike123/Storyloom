"""Storyloom Studio — 剪辑草稿域服务层（EDIT-01：项目剪辑草稿读写）。

来源：附录 08 §2（EditManifest v1）/§3（v1 支持范围）、附录 11 §2。
本卡只做**草稿读写**：GET 当前草稿 + POST 保存新 edit_revision（CAS + 幂等）。

本卡明确不做（后续卡）：
- 不写 ``studio_confirmed_edits``（确认属 EDIT-04/08）与 ``studio_releases``；
- 不改 ``MediaArtifact``（媒体行只读，仅校验 media_id 存在且属本项目）；
- 不删旧草稿行：版本链只追加，每次保存 = 新 (edit_id, edit_revision) 行；
- 不引入第二套时间单位：source_in_ms/source_out_ms/timeline_start_ms 一律整数毫秒。

实现约束（冻结，同 projects/service.py 头注）：
- 属主校验：项目不存在/非属主一律 404 not_found（不泄漏存在性）；
- ``edit_id`` 非 null 时草稿必须存在且属本项目，否则 404 not_found（跨项目同形状）；
- 幂等走 studio_command_records（owner_id, command_id），重放 200 零写；
- manifest_digest 由服务端计算（忽略客户端传入值）；
- 错误一律 raise StudioAPIError（冻结错误协议）。

规格留白（最保守解释，见交付报告）：
- ``manifest.instances[i].id`` 在单一 manifest 内必须唯一（否则会撞
  ``UNIQUE(draft_id, instance_id)``）；本服务按 rule=``invalid`` 提前报 422，
  而不是让 DB 抛 IntegrityError 变 500；
- ``instances[i]`` 非 object 时规格未给字段路径，按 ``manifest.instances[i]``
  rule=``invalid`` 单条报错（其余逐项校验对该项跳过）；
- GET 无 edit_id 入参：以本项目最新草稿行（``id`` DESC，ULID 时间有序）的
  ``edit_id`` 为"当前剪辑"，再取该 edit_id 的 max(edit_revision) 行为当前草稿行。
"""
import hashlib
import json
import math
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..media.models import MediaArtifact
from ..projects.models import StudioProject
from .models import ConfirmedEdit, EditDraft, EditInstance

__all__ = ["get_edit", "save_draft", "manifest_digest"]

# 附录 08 §2 冻结：唯一 editmanifest v1 schema 标识。
MANIFEST_SCHEMA = "storyloom.editmanifest/v1"
# 附录 08 §3 冻结：速度支持范围。
SPEED_MIN = 0.25
SPEED_MAX = 4.0


def _violation(field: str, rule: str, message: str) -> dict[str, str]:
    return {"field": field, "rule": rule, "message": message}


def _is_int(v) -> bool:
    """JSON 整数判定：bool 是 int 子类，但不是合法整数（拒绝 true/false）。"""
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def manifest_digest(manifest) -> str:
    """服务端 manifest_digest（附录 08 §2，冻结公式；忽略客户端传入值）。

    sha256(json.dumps(manifest, sort_keys=True, separators=(",",":"),
                      ensure_ascii=False).encode("utf-8")).hexdigest()
    """
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _instance_dto(row: EditInstance) -> dict:
    """InstanceDto（冻结形状，附录 11 §2）。

    object_ref.id 取稳定逻辑身份 ``instance_id``（= manifest 内 instance id）；
    EditInstance 行主键 id 是每个草稿版本重建的内部代理，不作对外引用。
    """
    return {
        "object_ref": {
            "kind": "edit_instance",
            "id": row.instance_id,
            "revision": None,
        },
        "instance_id": row.instance_id,
        "media_id": row.media_id,
        "source_in_ms": row.source_in_ms,
        "source_out_ms": row.source_out_ms,
        "speed": row.speed,
        "timeline_start_ms": row.timeline_start_ms,
        "track_id": row.track_id,
    }


def _current_draft(session, pid: str, edit_id: str) -> EditDraft | None:
    """某 edit_id 在本项目内的当前草稿行 = max(edit_revision)，无则 None。"""
    return session.scalar(
        select(EditDraft)
        .where(EditDraft.project_id == pid, EditDraft.edit_id == edit_id)
        .order_by(EditDraft.edit_revision.desc())
        .limit(1)
    )


def get_edit(session, user, pid) -> dict:
    """GET /api/studio/projects/{pid}/edit（附录 11 §2，纯读）。

    - 项目不存在/非属主 → 404 not_found(kind=project, id=pid)；
    - 项目无任何草稿行 → 空态：edit_id/edit_revision/cas_revision/manifest/
      manifest_digest 全 null、instances=[]、confirmed=null（不造样例实例、
      不造假时长）；
    - 有草稿 → 当前草稿行（本项目最新 edit_id 的 max edit_revision）的
      manifest/digest/cas_revision + 该行 studio_edit_instances（按插入序）；
    - confirmed 取该 edit_id 的 max(edit_revision) 确认行，无则 null。
    """
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    latest = session.scalar(
        select(EditDraft)
        .where(EditDraft.project_id == pid)
        .order_by(EditDraft.id.desc())
        .limit(1)
    )
    if latest is None:
        # 空态就是空态：不造样例实例、不造样例时长（R12/EDIT-01 冻结合同）
        return {
            "edit_id": None,
            "edit_revision": None,
            "cas_revision": None,
            "manifest": None,
            "manifest_digest": None,
            "instances": [],
            "confirmed": None,
        }

    edit_id = latest.edit_id
    current = _current_draft(session, pid, edit_id)
    manifest = json.loads(current.manifest)
    rows = session.scalars(
        select(EditInstance).where(EditInstance.draft_id == current.id)
    ).all()
    # 行序对齐当前草稿 manifest 的 instances 顺序（ULID 同毫秒内不保证插入序，
    # 而 manifest 本身有序且是唯一编辑依据）；manifest 未登记的残留行排在其后。
    by_instance = {r.instance_id: r for r in rows}
    ordered: list[EditInstance] = []
    for item in manifest.get("instances", []) if isinstance(manifest, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            row = by_instance.pop(item["id"], None)
            if row is not None:
                ordered.append(row)
    ordered.extend(by_instance[k] for k in sorted(by_instance))
    confirmed = session.scalar(
        select(ConfirmedEdit)
        .where(ConfirmedEdit.edit_id == edit_id)
        .order_by(ConfirmedEdit.edit_revision.desc())
        .limit(1)
    )
    return {
        "edit_id": edit_id,
        "edit_revision": current.edit_revision,
        "cas_revision": current.cas_revision,
        "manifest": manifest,
        "manifest_digest": current.manifest_digest,
        "instances": [_instance_dto(r) for r in ordered],
        "confirmed": (
            None
            if confirmed is None
            else {
                "object_ref": {
                    "kind": "confirmed_edit",
                    "id": confirmed.edit_id,
                    "revision": confirmed.edit_revision,
                },
                "manifest_digest": confirmed.manifest_digest,
                "output_spec": json.loads(confirmed.output_spec),
            }
        ),
    }


def _validate_manifest(session, pid: str, manifest) -> None:
    """EditManifest v1 校验（附录 08 §2/§3，冻结；全部 violations 一次报 422）。

    非 object / schema 不符 → manifest.schema invalid；
    canvas.width/height/fps 非正整数 → manifest.canvas.<k> invalid；
    instances 非数组 → manifest.instances invalid；
    逐项（field 带下标精确路径）：id/media_id 非空字符串 required、
    track_id==0 invalid、timeline_start_ms/source_in_ms 非负整数 invalid、
    source_out_ms>source_in_ms invalid、0.25≤speed≤4.0 invalid、
    media_id 必须存在且属本项目 not_found。
    """
    if not isinstance(manifest, dict):
        raise StudioAPIError.validation_failed(
            [
                _violation(
                    "manifest.schema",
                    "invalid",
                    "manifest 必须是 storyloom.editmanifest/v1 对象。",
                )
            ]
        )

    violations: list[dict[str, str]] = []
    if manifest.get("schema") != MANIFEST_SCHEMA:
        violations.append(
            _violation(
                "manifest.schema",
                "invalid",
                f"schema 必须是 {MANIFEST_SCHEMA}。",
            )
        )

    canvas = manifest.get("canvas")
    for key in ("width", "height", "fps"):
        value = canvas.get(key) if isinstance(canvas, dict) else None
        if not (_is_int(value) and value > 0):
            violations.append(
                _violation(
                    f"manifest.canvas.{key}",
                    "invalid",
                    f"canvas.{key} 必须是正整数。",
                )
            )

    instances = manifest.get("instances")
    if not isinstance(instances, list):
        violations.append(
            _violation("manifest.instances", "invalid", "instances 必须是数组。")
        )
        raise StudioAPIError.validation_failed(violations)

    seen_ids: set[str] = set()
    for i, item in enumerate(instances):
        path = f"manifest.instances[{i}]"
        if not isinstance(item, dict):
            violations.append(
                _violation(path, "invalid", "实例必须是对象。")
            )
            continue

        instance_id = item.get("id")
        instance_id_ok = isinstance(instance_id, str) and instance_id.strip() != ""
        if not instance_id_ok:
            violations.append(
                _violation(f"{path}.id", "required", "实例 id 不能为空。")
            )
        elif instance_id in seen_ids:
            # 规格留白：同 manifest 内 id 必须唯一（DB UNIQUE(draft_id, instance_id)）
            violations.append(
                _violation(f"{path}.id", "invalid", "同一 manifest 内实例 id 不能重复。")
            )
        else:
            seen_ids.add(instance_id)

        media_id = item.get("media_id")
        media_id_ok = isinstance(media_id, str) and media_id.strip() != ""
        if not media_id_ok:
            violations.append(
                _violation(f"{path}.media_id", "required", "media_id 不能为空。")
            )

        track_id = item.get("track_id")
        if not (_is_int(track_id) and track_id == 0):
            violations.append(
                _violation(f"{path}.track_id", "invalid", "v1 仅支持 track_id=0（单轨）。")
            )

        for key in ("timeline_start_ms", "source_in_ms"):
            value = item.get(key)
            if not (_is_int(value) and value >= 0):
                violations.append(
                    _violation(
                        f"{path}.{key}", "invalid", f"{key} 必须是非负整数（毫秒）。"
                    )
                )

        source_in = item.get("source_in_ms")
        source_out = item.get("source_out_ms")
        if not (_is_int(source_in) and _is_int(source_out) and source_out > source_in):
            violations.append(
                _violation(
                    f"{path}.source_out_ms",
                    "invalid",
                    "source_out_ms 必须是大于 source_in_ms 的整数（毫秒）。",
                )
            )

        speed = item.get("speed")
        if not (
            _is_number(speed)
            and math.isfinite(float(speed))
            and SPEED_MIN <= float(speed) <= SPEED_MAX
        ):
            violations.append(
                _violation(
                    f"{path}.speed", "invalid", "speed 必须在 0.25–4.0 之间。"
                )
            )

        # media_id 存在性：仅对形状合法的 media_id 查库（形状非法时已报 required）
        if media_id_ok:
            media_row = session.scalar(
                select(MediaArtifact.id).where(
                    MediaArtifact.id == media_id,
                    MediaArtifact.project_id == pid,
                )
            )
            if media_row is None:
                violations.append(
                    _violation(
                        f"{path}.media_id",
                        "not_found",
                        "媒体不存在或不属于当前项目。",
                    )
                )

    if violations:
        raise StudioAPIError.validation_failed(violations)


def _payload_matches(payload: dict, edit_id, manifest, expected_revision) -> bool:
    """重放命中判定：首次 result_payload 与本次请求是否同一 payload（附录 00 §3）。

    从首次响应体可无损还原请求侧内容：
    - manifest：原样存于响应体；
    - 目标 edit_id：object_ref.id（首次保存时请求侧为 null，响应用服务端新 id）；
    - expected_revision：首次保存（edit_revision==1，edit_id 为 null）必为 null，
      其余 = cas_revision - 1（每次保存 cas 与 edit_revision 同步 +1）。
    """
    if payload.get("manifest") != manifest:
        return False
    ref = payload.get("object_ref") or {}
    if edit_id is not None and ref.get("id") != edit_id:
        return False
    stored_revision = payload.get("edit_revision")
    if stored_revision == 1:
        return expected_revision is None
    return expected_revision == (payload.get("cas_revision", 0) - 1)


def save_draft(session, user, pid, edit_id, manifest, expected_revision, command_id) -> dict:
    """POST /api/studio/projects/{pid}/edit/draft（附录 11 §2）。

    校验序（按序短路，冻结）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)；
    2. edit_id 非 null 时草稿必须存在且属本项目 → 否则 404 not_found(kind=edit)；
    3. manifest 校验：全部 violations 一次报（422 validation_failed）；
    4. 幂等：同 (owner_id, command_id) 且同 payload → 重放（返回首次
       result_payload，附内部标记 ``_replay=True`` 由路由转 200，零写）；
       异内容 → 422 validation_failed(field=command_id, rule=reused)；
    5. CAS：新建（edit_id 为 null）时 expected_revision 必须为 null，否则 422
       field=expected_revision rule=invalid；已有草稿时 expected_revision 必须
       == 当前 cas_revision，否则 409 revision_conflict(expected/actual)；
    6. 写（单事务，无半次保存）：INSERT EditDraft(edit_revision=prev+1,
       cas_revision=prev+1) + 新草稿行的 EditInstance 行（id=make_ulid(),
       instance_id=manifest.instances[i].id, draft_id=新行 id）+ CommandRecord。
       首次保存 edit_id=make_ulid()、edit_revision=1、cas_revision=1。

    返回 201 体 DraftDto（冻结形状）；重放时附 ``_replay=True``。
    """
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) edit_id 非 null：草稿必须存在且属本项目（跨项目同形状 404，不泄漏）
    current: EditDraft | None = None
    if edit_id is not None:
        rows = session.scalars(
            select(EditDraft).where(EditDraft.edit_id == edit_id)
        ).all()
        owned = [r for r in rows if r.project_id == pid]
        if not owned:
            raise StudioAPIError.not_found("edit", edit_id)
        current = max(owned, key=lambda r: r.edit_revision)

    # 3) manifest 校验（全部 violations 一次报）
    _validate_manifest(session, pid, manifest)

    # 4) 幂等（附录 00 §3）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        if _payload_matches(payload, edit_id, manifest, expected_revision):
            # 重放：返回首次 result_payload 解析出的 DTO，零写零 commit
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

    # 5) CAS
    if current is None:
        if expected_revision is not None:
            raise StudioAPIError.validation_failed(
                [
                    {
                        "field": "expected_revision",
                        "rule": "invalid",
                        "message": "新建草稿时 expected_revision 必须为 null。",
                    }
                ]
            )
        new_edit_id = make_ulid()
        new_revision = 1
        new_cas = 1
    else:
        actual = current.cas_revision
        if expected_revision != actual:
            raise StudioAPIError.revision_conflict(
                "edit", edit_id, expected_revision, actual
            )
        new_edit_id = edit_id
        new_revision = current.edit_revision + 1
        new_cas = actual + 1

    digest = manifest_digest(manifest)
    manifest_text = json.dumps(manifest, ensure_ascii=False)
    now = time.time()
    draft_id = make_ulid()

    instance_rows = [
        EditInstance(
            id=make_ulid(),
            draft_id=draft_id,
            instance_id=item["id"],
            media_id=item["media_id"],
            source_in_ms=int(item["source_in_ms"]),
            source_out_ms=int(item["source_out_ms"]),
            speed=float(item["speed"]),
            timeline_start_ms=int(item["timeline_start_ms"]),
            track_id=int(item["track_id"]),
        )
        for item in manifest["instances"]
    ]

    response = {
        "object_ref": {
            "kind": "edit",
            "id": new_edit_id,
            "revision": new_revision,
        },
        "edit_revision": new_revision,
        "cas_revision": new_cas,
        "manifest": manifest,
        "manifest_digest": digest,
        "instances": [_instance_dto(r) for r in instance_rows],
        "updated_at": now,
    }

    draft = EditDraft(
        id=draft_id,
        project_id=pid,
        edit_id=new_edit_id,
        edit_revision=new_revision,
        manifest=manifest_text,
        manifest_digest=digest,
        cas_revision=new_cas,
        updated_at=now,
    )

    session.commit()  # 结束读事务（幂等/CAS 预检）；写阶段另起单一事务
    try:
        with session.begin():
            session.add(draft)
            session.flush()  # 先持久化父行：UOW 不保证跨表 FK 插入序
            for row in instance_rows:
                session.add(row)
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
        # 兜底（并发竞态/唯一约束）：回滚后以 CAS 语义报冲突，不留半次保存
        session.rollback()
        winner = _current_draft(session, pid, new_edit_id)
        raise StudioAPIError.revision_conflict(
            "edit",
            new_edit_id,
            expected_revision,
            None if winner is None else winner.cas_revision,
        )

    return response
