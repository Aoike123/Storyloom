"""Storyloom Studio — 资产域服务层（附录 05 §2 命令的服务端实现）。

ASSET-01 逐子卡填充：AS1 POST /assets（ASSET-01-a）—— 建立**稳定身份**
（Asset）与其 revision=1 规格（AssetRevision）；AS10 POST
/assets/{aid}/costume-required（ASSET-01-b）—— character 的
costume_required 可**明确**置 false（自然体表角色，bare 模式无需图），
不用空图冒充服装。**不做**改名（AS3）、新版本（AS4/AS5/AS6）、审核
（AS7）、切换当前版本（AS8）、采用（AS9）。

实现约束（冻结）：
- 所有写命令走 command_id 幂等（附录 00 §3，studio_command_records）；
- 属主校验：未登录 401（路由层 require_user）；项目不存在/非属主一律
  404 not_found（不泄漏存在性，同 P1/S1 单点模式）；
- id 全部服务端 ULID（core.db.make_ulid）；客户端只提供 command_id；
- costume 的归属一致性（type=costume 必填且指向本项目 character；
  character/scene 必须 NULL）在服务层校验（assets/models.py 模块注）；
- 本卡 media_artifact_id 恒为 NULL（媒体/上传/生成属 ASSET-02 之后子卡）；
- 不写 occurrence/binding/关系表（附录 06 属关系域），不自动采用任何版本；
- 错误一律 raise StudioAPIError（冻结错误协议，不泄露内部细节）。

DTO 形状（主模型针对本卡冻结，与 AS1 之后的详情读一致）：
``{"object_ref":{kind,id,revision},"type","name","character":{kind,id}|null,
"current_revision":{revision,spec,media_artifact_id,review_status},
"versions":[{revision,spec,media_artifact_id,review_status,created_at}],
"occurrences":[],"bindings":[]}``
"""
import json
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ...core.db import make_ulid
from ..contracts.errors import StudioAPIError
from ..contracts.models import CommandRecord
from ..projects.models import StudioProject
from .models import Asset, AssetRevision

__all__ = ["create_asset", "set_costume_required"]

TYPES = ("character", "costume", "scene")
_BODY_TYPES = ("natural", "clothed")
_NAME_MAX = 120  # 附录 05 §1：name text(120)


def _to_dto(asset: Asset, rev: AssetRevision) -> dict:
    """AS1 成功体 = 与 §2 AS2 详情读一致的 DTO（本卡无 occurrence/binding）。

    spec 存文本（JSON），读时反序列化为原样 JSON object 返回。
    """
    spec_obj = json.loads(rev.spec) if rev.spec else {}
    character = (
        {"kind": "asset", "id": asset.character_id}
        if asset.character_id is not None
        else None
    )
    version = {
        "revision": rev.revision,
        "spec": spec_obj,
        "media_artifact_id": rev.media_artifact_id,
        "review_status": rev.review_status,
    }
    return {
        "object_ref": {"kind": "asset", "id": asset.id, "revision": rev.revision},
        "type": asset.type,
        "name": asset.name,
        "character": character,
        "current_revision": dict(version),
        "versions": [dict(version, created_at=rev.created_at)],
        # 出现/绑定是关系域（附录 06）的事实；新建身份必然为空，不查不写
        "occurrences": [],
        "bindings": [],
    }


def _missing_text(spec: dict, key: str) -> bool:
    """必填字符串键判定：缺失、非字符串或全空白 = 缺失。"""
    value = spec.get(key)
    return not isinstance(value, str) or not value.strip()


def _validate_spec(type_: str, spec: dict) -> list[dict[str, str]]:
    """按 type 校验 spec（附录 05 §1 冻结的必填键；自由扩展键允许）。

    必填键（附录 05 §1 字面为准；交接单曾放宽为"至少一个非空键"，
    2026-10-13 主模型裁定按附录收紧并回报）：
    - character：``appearance``(string) 必填非空；可选 body_type/height_cm/notes 按声明类型校验；
    - costume：``description``(string) 必填非空；
    - scene：``location``(string)、``description``(string) 均必填非空。

    缺席的必填键逐个报 violation（field=``spec.<key>``, rule=``required``），
    使调用方能一次看到全部缺口。
    """
    violations: list[dict[str, str]] = []
    if type_ == "character":
        if _missing_text(spec, "appearance"):
            violations.append(
                {
                    "field": "spec.appearance",
                    "rule": "required",
                    "message": "人物规格必含非空 appearance（字符串）。",
                }
            )
        body_type = spec.get("body_type")
        if body_type is not None and body_type not in _BODY_TYPES:
            violations.append(
                {
                    "field": "spec.body_type",
                    "rule": "invalid",
                    "message": "body_type 只能是 'natural' 或 'clothed'。",
                }
            )
        height_cm = spec.get("height_cm")
        if height_cm is not None and (
            isinstance(height_cm, bool) or not isinstance(height_cm, int)
        ):
            violations.append(
                {
                    "field": "spec.height_cm",
                    "rule": "invalid",
                    "message": "height_cm 必须是整数（厘米）。",
                }
            )
        notes = spec.get("notes")
        if notes is not None and not isinstance(notes, str):
            violations.append(
                {
                    "field": "spec.notes",
                    "rule": "invalid",
                    "message": "notes 必须是字符串。",
                }
            )
    elif type_ == "costume":
        if _missing_text(spec, "description"):
            violations.append(
                {
                    "field": "spec.description",
                    "rule": "required",
                    "message": "服装规格必含非空 description（字符串）。",
                }
            )
    elif type_ == "scene":
        if _missing_text(spec, "location"):
            violations.append(
                {
                    "field": "spec.location",
                    "rule": "required",
                    "message": "场景规格必含非空 location（字符串）。",
                }
            )
        if _missing_text(spec, "description"):
            violations.append(
                {
                    "field": "spec.description",
                    "rule": "required",
                    "message": "场景规格必含非空 description（字符串）。",
                }
            )
    return violations


def _resolve_character(session, pid: str, character_id):
    """costume 归属校验：目标须存在、属本项目、type=character。

    失败一律 422 precondition_failed（附录 00 §4 冻结形状：
    ``{"reason","blocked_by":{kind,id,revision}}``）；blocked_by.id 回传调用方
    给出的 character_id，不泄漏该 id 是否存在于别的项目/属主。
    """
    if not character_id:
        raise StudioAPIError.precondition_failed(
            "costume_requires_character",
            {"kind": "asset", "id": None, "revision": None},
            "服装必须归属一个人物身份：character_id 必填。",
        )
    target = session.scalar(select(Asset).where(Asset.id == character_id))
    if target is None:
        raise StudioAPIError.precondition_failed(
            "character_not_found",
            {"kind": "asset", "id": character_id, "revision": None},
            f"指定的所属人物不存在：{character_id}。",
        )
    if target.project_id != pid:
        raise StudioAPIError.precondition_failed(
            "character_cross_project",
            {"kind": "asset", "id": character_id, "revision": None},
            f"指定的所属人物不属于本项目：{character_id}。",
        )
    if target.type != "character":
        raise StudioAPIError.precondition_failed(
            "character_not_character",
            {"kind": "asset", "id": character_id, "revision": None},
            f"指定的资产不是人物身份（type={target.type}），不能作为服装归属：{character_id}。",
        )


def create_asset(
    session, user, pid, type_, name, character_id, spec, command_id
) -> dict:
    """AS1 新建资产（附录 05 §2，契约 v2.9）。

    事务边界（自管，同 P1/S1 模式）：
    - 项目属主校验、字段校验、spec 校验、归属前置、幂等命中均为读查询，先于写事务；
    - 重放路径零写零 commit，直接返回首次 result_payload（路由层以 200 返回）；
    - 写阶段（Asset + AssetRevision + CommandRecord）同一事务：FK 环
      （assets.current_revision_id ↔ asset_revisions.asset_id）由模型层
      DEFERRED 约束承载，两行插入序由 add 顺序决定。

    成功返回 §2 详情 DTO（revision=1，review_status='unreviewed'，无媒体）；
    重放命中时附带内部标记 ``_replay=True``（路由层 pop，不改响应体形状）。
    """
    # 1) 项目（属主校验：404 不泄漏存在性）
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 字段校验（先 type，再 name；name trim 后保存，同 P1 裁定）
    violations: list[dict[str, str]] = []
    if type_ not in TYPES:
        violations.append(
            {
                "field": "type",
                "rule": "invalid",
                "message": "type 只能是 'character'、'costume' 或 'scene'。",
            }
        )
    name_s = name.strip()
    if not name_s:
        violations.append(
            {"field": "name", "rule": "required", "message": "资产名不能为空。"}
        )
    if len(name_s) > _NAME_MAX:
        violations.append(
            {
                "field": "name",
                "rule": "max_length",
                "message": f"资产名最长 {_NAME_MAX} 个字符。",
            }
        )
    if violations:
        raise StudioAPIError.validation_failed(violations)

    # 3) spec 校验（按 type 的必填/非空键；自定义键允许）
    spec_violations = _validate_spec(type_, spec)
    if spec_violations:
        raise StudioAPIError.validation_failed(spec_violations)

    # 4) 归属前置（附录 05 §1 character_id 条件规则）
    if type_ == "character" or type_ == "scene":
        if character_id is not None:
            raise StudioAPIError.precondition_failed(
                "character_id_not_allowed",
                {"kind": "asset", "id": character_id, "revision": None},
                f"只有服装可以携带 character_id；{type_} 必须缺省或为 null。",
            )
        char_id = None
    else:  # costume
        _resolve_character(session, pid, character_id)
        char_id = character_id

    # 5) 幂等（附录 00 §3，同 P1/S1 模式）：读查询，先于写事务
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        stored_char = (payload.get("character") or {}).get("id")
        stored_spec = (payload.get("current_revision") or {}).get("spec")
        if (
            payload.get("type") == type_
            and payload.get("name") == name_s
            and stored_char == char_id
            and stored_spec == spec
        ):
            # 重放：返回首次 result_payload，零副作用
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
    session.commit()  # 结束读事务；写阶段另起事务

    # 6) 写事务：Asset + AssetRevision(1) + CommandRecord，同一事务
    now = time.time()
    asset_id = make_ulid()
    revision_id = make_ulid()
    asset = Asset(
        id=asset_id,
        project_id=pid,
        type=type_,
        name=name_s,
        character_id=char_id,
        costume_required=True,  # 附录 05 §1 默认 true（AS10 才可改）
        current_revision_id=revision_id,
        created_at=now,
        updated_at=now,
    )
    revision = AssetRevision(
        id=revision_id,
        asset_id=asset_id,
        revision=1,
        spec=json.dumps(spec, ensure_ascii=False),
        media_artifact_id=None,  # 本卡不生成/上传媒体（ASSET-02 起才有值）
        generation_input=None,
        review_status="unreviewed",  # 不自动确认（R07）
        created_at=now,
    )
    response = _to_dto(asset, revision)  # 首次成功的完整响应体（幂等 result_payload）
    try:
        with session.begin():
            session.add(asset)
            session.add(revision)
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
        # UQ(owner_id, command_id) 竞态：并发赢家已提交 → 返回其首次结果（重放语义）；
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


def _current_revision_no(session, asset: Asset) -> int:
    """资产当前 revision 号（= current_revision_id 行的 revision）。

    current_revision_id 为 NOT NULL FK，理论必命中；缺失 = 数据损坏，
    按冻结协议映射 internal_error，不泄露细节。
    """
    rev = session.get(AssetRevision, asset.current_revision_id)
    if rev is None:  # 理论上不可达
        raise StudioAPIError.internal()
    return rev.revision


def set_costume_required(
    session, user, pid, aid, value, expected_revision, command_id
) -> dict:
    """AS10 明确设置 character 的 costume_required（附录 05 §2，契约 v2.9）。

    语义：自然体表角色可**明确**置 false（bare 模式无需图），不用空图
    冒充服装；value 与现值相同也算成功（显式确认语义），照常落写。
    本命令只改 Asset 行的 costume_required/updated_at：**不新建
    AssetRevision、不动 current_revision_id、不动 review_status、
    不写 occurrence/binding**。

    校验顺序（冻结，按序短路）：
    1. 项目不存在/非属主 → 404 not_found(kind=project)（不泄漏存在性）；
    2. 资产不存在/跨项目 → 404 not_found(kind=asset)（两情形同形状）；
    3. 资产 type != 'character' → 422 precondition_failed
       （reason=not_character，blocked_by={kind:asset, id, revision=当前}）；
    4. expected_revision != 当前 revision → 409 revision_conflict
       （object={kind:asset,id}，expected/actual=真实当前值，附录 00 §4 形状）；
    5. 幂等（附录 00 §3，校验之后、写之前）：(owner, command_id) 记录存在
       且 result_payload 逐项等于本次有效载荷 → 重放：返回首次
       result_payload（附内部标记 ``_replay=True``，本命令成功码即 200，
       重放亦 200），零写零 commit；不一致 → 422 validation_failed
       （field=command_id, rule=reused）。

    写事务（单事务）：UPDATE Asset.costume_required/updated_at + INSERT
    CommandRecord（result_payload=完整 200 体），同一 ``session.begin()``；
    IntegrityError（UQ(owner_id, command_id) 并发竞态）→ 回滚重读赢家，
    映射重放语义。

    成功体（恰 2 字段）：
    ``{"object_ref": {"kind":"asset","id":aid,"revision":当前 revision},
    "costume_required": <bool>}``
    """
    # 1) 项目（属主校验，404 不泄漏存在性）
    project = session.scalar(select(StudioProject).where(StudioProject.id == pid))
    if project is None or project.owner_id != user.id:
        raise StudioAPIError.not_found("project", pid)

    # 2) 资产（不存在/跨项目同形状 404，不泄漏存在性）
    asset = session.scalar(select(Asset).where(Asset.id == aid))
    if asset is None or asset.project_id != pid:
        raise StudioAPIError.not_found("asset", aid)
    current = _current_revision_no(session, asset)

    # 3) 仅人物身份可设置 costume_required
    if asset.type != "character":
        raise StudioAPIError.precondition_failed(
            "not_character",
            {"kind": "asset", "id": aid, "revision": current},
            f"只有人物身份可以设置 costume_required（该资产 type={asset.type}）。",
        )

    # 4) CAS：expected_revision 必须等于当前 revision
    if expected_revision != current:
        raise StudioAPIError.revision_conflict("asset", aid, expected_revision, current)

    # 5) 幂等（读查询，先于写事务）：同 command_id 同载荷 → 重放
    record = session.scalar(
        select(CommandRecord).where(
            CommandRecord.owner_id == user.id,
            CommandRecord.command_id == command_id,
        )
    )
    if record is not None:
        payload = json.loads(record.result_payload) if record.result_payload else {}
        stored_ref = payload.get("object_ref") or {}
        if (
            stored_ref.get("kind") == "asset"
            and stored_ref.get("id") == aid
            and payload.get("costume_required") == value
            and isinstance(payload.get("costume_required"), bool)
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
    session.commit()  # 结束读事务；写阶段另起事务

    # 6) 写事务：UPDATE 资产行 + INSERT CommandRecord，同一事务
    now = time.time()
    response = {
        "object_ref": {"kind": "asset", "id": aid, "revision": current},
        "costume_required": value,
    }
    try:
        with session.begin():
            asset.costume_required = value
            asset.updated_at = now
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
        # UQ(owner_id, command_id) 竞态：并发赢家已提交 → 返回其首次结果（重放语义）；
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
