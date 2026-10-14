"""Storyloom Studio — 剧本域 HTTP 路由（/api/studio/projects/{pid}/script-objects*，附录 03 §2）。

SCRIPT-01-a / SCRIPT-02 逐子卡填充路由（SC1 POST /projects/{pid}/script-objects、
SC4 POST .../revise、SC6 POST .../fragments/{fid}/script-adopt[/apply]）。
路由层只做请求体形态校验与重放状态码切换；业务逻辑在 scripts/service.py。
鉴权：core.auth.require_user（严格 Bearer，无匿名回退）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.accounts import User
from ...core.auth import require_user
from ...core.db import Session
from . import service
from .service import _UNSET

router = APIRouter(tags=["studio-scripts"])


class CreateScriptObjectIn(BaseModel):
    """SC1 请求体（附录 03 §2）。

    fragment_id/kind/text/command_id 必填（缺失由请求体形态校验拦截）；
    speaker/adaptation_note/seq 可缺省或 null（seq 缺省 = 片段内下一个空位，
    由 service 裁定；显式占用冲突返回冻结的 validation_failed/seq.taken）。
    """

    fragment_id: str
    kind: str
    text: str
    speaker: str | None = None
    adaptation_note: str | None = None
    seq: int | None = None
    command_id: str


@router.post("/projects/{pid}/script-objects", status_code=201)
def create_script_object(
    pid: str, body: CreateScriptObjectIn, user: User = Depends(require_user)
):
    """SC1 新建剧本草稿对象（附录 03 §2）：成功 201；同 command_id 重放 200。"""
    with Session() as s:
        result = service.create_script_object(
            s,
            user,
            pid,
            body.fragment_id,
            body.kind,
            body.text,
            body.speaker,
            body.adaptation_note,
            body.seq,
            body.command_id,
        )
    if result.pop("_replay", False):
        # 重放命中：200 原样返回首次 result_payload（覆盖 201）
        return JSONResponse(status_code=200, content=result)
    return result


class ReviseScriptObjectIn(BaseModel):
    """SC4 请求体（附录 03 §2）。

    expected_revision/command_id 必填（缺失由请求体形态校验拦截）。
    text/speaker/adaptation_note 三者全缺席 → 422 validation_failed
    （field=payload, rule=empty）；**缺席 ≠ null**：字段缺席 = 保持原值，
    显式 null = 清空（speaker/adaptation_note），text 显式 null 视同缺席。
    本模型三个字段一律默认 None；「字段是否出现」由 `model_fields_set`
    判定后映射为 service 的 `_UNSET` 哨兵（服务层契约不变）。不使用
    `object()` 作 Pydantic 默认值：那会让 JSON schema 生成时抛
    `PydanticJsonSchemaWarning`（默认值不可序列化）并从 schema 剔除该键，
    改用 `model_fields_set` 语义更明确且无警告（接续单 5.2 裁定）。
    """

    text: str | None = None
    speaker: str | None = None
    adaptation_note: str | None = None
    expected_revision: int
    command_id: str


@router.post("/projects/{pid}/script-objects/{oid}/revise")
def revise_script_object(
    pid: str,
    oid: str,
    body: ReviseScriptObjectIn,
    user: User = Depends(require_user),
):
    """SC4 修订当前草稿（附录 03 §2）：成功 200（与 SC1 DTO 同形，revision=新值）；
    同 command_id 重放亦 200（弹内部标记后原样返回首次 result_payload）。
    只改当前草稿 revision，过期保存拒绝（409 revision_conflict）；不触碰采用、
    不启动任何生成。"""
    # 缺席（未在请求体出现）→ _UNSET（保持原值）；显式 null → None（清空）。
    present = body.model_fields_set
    speaker = body.speaker if "speaker" in present else _UNSET
    adaptation_note = body.adaptation_note if "adaptation_note" in present else _UNSET
    with Session() as s:
        result = service.revise_script_object(
            s,
            user,
            pid,
            oid,
            body.text,
            speaker,
            adaptation_note,
            body.expected_revision,
            body.command_id,
        )
    result.pop("_replay", None)  # 成功与重放同为 200，仅弹内部标记
    return result


class ScriptAdoptPreviewIn(BaseModel):
    """SC6 preview 请求体（附录 03 §2）：target_fragment_id 必与路径片段一致。"""

    target_fragment_id: str
    command_id: str


class ExpectedObjectRevisionIn(BaseModel):
    """SC6 apply 的对象快照项（附录 03 §1 object_revisions 格式）。"""

    object_id: str
    revision: int
    seq: int
    kind: str


class ScriptAdoptApplyIn(BaseModel):
    """SC6 apply 请求体（附录 03 §2）：

    expected_previous_adoption_id 可 null（首个采用）；expected_object_revisions
    为 preview 基准的对象快照（写事务内重读逐项全等比对）。
    """

    preview_id: str
    command_id: str
    expected_previous_adoption_id: str | None = None
    expected_object_revisions: list[ExpectedObjectRevisionIn]


@router.post("/projects/{pid}/fragments/{fid}/script-adopt")
def preview_script_adopt(
    pid: str, fid: str, body: ScriptAdoptPreviewIn, user: User = Depends(require_user)
):
    """SC6 预览采用剧本 revision：200 恰 4 字段 {preview_id, kind, baseline, impact}
    （00 §5.1；preview 是一次幂等写：只写 ChangePreview + CommandRecord，零业务副作用）；
    幂等重放命中 → 200 原 result_payload（零写零 commit）。"""
    with Session() as s:
        result = service.preview_script_adopt(
            s, user, pid, fid, body.target_fragment_id, body.command_id
        )
    result.pop("_replay", None)  # 成功与重放同为 200，仅弹内部标记
    return JSONResponse(status_code=200, content=result)


@router.post("/projects/{pid}/fragments/{fid}/script-adopt/apply")
def apply_script_adopt(
    pid: str, fid: str, body: ScriptAdoptApplyIn, user: User = Depends(require_user)
):
    """SC6 应用采用：200 新 ScriptAdoption DTO（链尾）；不自动生成分镜、不改任何
    已冻结镜头输入与既有 adoption/对象/版本行；幂等重放命中 → 200 原 result_payload。"""
    expected = [
        {
            "object_id": m.object_id,
            "revision": m.revision,
            "seq": m.seq,
            "kind": m.kind,
        }
        for m in body.expected_object_revisions
    ]
    with Session() as s:
        result = service.apply_script_adopt(
            s,
            user,
            pid,
            fid,
            body.preview_id,
            body.command_id,
            body.expected_previous_adoption_id,
            expected,
        )
    result.pop("_replay", None)  # 成功与重放同为 200，仅弹内部标记
    return JSONResponse(status_code=200, content=result)
