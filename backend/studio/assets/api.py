"""Storyloom Studio — 资产域 HTTP 路由（/api/studio/projects/{pid}/assets*，附录 05 §2）。

ASSET-01 逐子卡填充路由：
- AS1（ASSET-01-a）POST /projects/{pid}/assets；
- AS10（ASSET-01-b）POST /projects/{pid}/assets/{aid}/costume-required；
- AS4（ASSET-01-d）POST /projects/{pid}/assets/{aid}/spec。
路由层只做请求体形态校验与重放状态码切换；业务逻辑在 assets/service.py。
鉴权：core.auth.require_user（严格 Bearer，无匿名回退）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool

from ...core.accounts import User
from ...core.auth import require_user
from ...core.db import Session
from . import service

router = APIRouter(tags=["studio-assets"])


class CreateAssetIn(BaseModel):
    """AS1 请求体（附录 05 §2）。

    type/name/spec/command_id 必填（缺失由请求体形态校验拦截）；
    character_id 仅 type='costume' 必填，character/scene 必须缺省或 null
    （条件规则在 service 内判定，返回冻结的 precondition_failed）。
    """

    type: str
    name: str
    character_id: str | None = None
    spec: dict
    command_id: str


@router.post("/projects/{pid}/assets", status_code=201)
def create_asset(pid: str, body: CreateAssetIn, user: User = Depends(require_user)):
    """AS1 新建资产身份（附录 05 §2）：成功 201；同 command_id 重放 200。"""
    with Session() as s:
        result = service.create_asset(
            s,
            user,
            pid,
            body.type,
            body.name,
            body.character_id,
            body.spec,
            body.command_id,
        )
    if result.pop("_replay", False):
        # 重放命中：200 原样返回首次 result_payload（覆盖 201）
        return JSONResponse(status_code=200, content=result)
    return result


class CostumeRequiredIn(BaseModel):
    """AS10 请求体（附录 05 §2）。

    value 为**严格布尔**（StrictBool：字符串 "false"、整数 1、null 均被
    请求体形态校验拦截为 422，不会进入 service）；expected_revision 为调用方
    所见的资产当前 revision 号（CAS）；command_id 必填（幂等键）。三字段均
    必填，缺失由请求体形态校验拦截。
    """

    value: StrictBool
    expected_revision: int
    command_id: str


@router.post("/projects/{pid}/assets/{aid}/costume-required")
def set_costume_required(
    pid: str,
    aid: str,
    body: CostumeRequiredIn,
    user: User = Depends(require_user),
):
    """AS10 明确设置 character 的 costume_required（附录 05 §2）。

    成功 200（恰 2 字段：object_ref + costume_required）；同 command_id
    重放亦 200（本命令成功码即 200，弹内部标记后原样返回首次
    result_payload）。
    """
    with Session() as s:
        result = service.set_costume_required(
            s,
            user,
            pid,
            aid,
            body.value,
            body.expected_revision,
            body.command_id,
        )
    result.pop("_replay", None)  # 成功与重放同为 200，仅弹内部标记
    return result


class ReviseSpecIn(BaseModel):
    """AS4 请求体（附录 05 §2，契约 v2.9）。

    spec（object）、expected_revision（int）、command_id 必填；name/type/
    character_id **不可经此命令修改**——请求体不接受这三个字段，出现即
    422 validation_failed（rule=forbidden，field 精确到字段名；service 层
    判定，因现有代码无 unknown_field 先例，按主模型裁定用 forbidden）。
    本模型以可选字段接收这三者（缺省 None），交由 service 做"出现即拒绝"
    判定；null 视为未提供（不做修改），与"不可修改"语义一致。
    """

    spec: dict
    expected_revision: int
    command_id: str
    name: str | None = None
    type: str | None = None
    character_id: str | None = None


@router.post("/projects/{pid}/assets/{aid}/spec")
def revise_spec(
    pid: str,
    aid: str,
    body: ReviseSpecIn,
    user: User = Depends(require_user),
):
    """AS4 修订资产规格（附录 05 §2）：只改 spec 并产生新 revision。

    成功 200（新 revision=当前+1，继承上一 revision 的 media/generation，
    review_status 回到 unreviewed，自动成为当前）；同 command_id 重放亦
    200（本命令成功码即 200，弹内部标记后原样返回首次 result_payload）。
    """
    with Session() as s:
        result = service.revise_spec(
            s,
            user,
            pid,
            aid,
            body.spec,
            body.expected_revision,
            body.command_id,
            name=body.name,
            type_=body.type,
            character_id=body.character_id,
        )
    result.pop("_replay", None)  # 成功与重放同为 200，仅弹内部标记
    return result
