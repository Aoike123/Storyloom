"""Storyloom Studio — 资产域 HTTP 路由（/api/studio/projects/{pid}/assets*，附录 05 §2）。

ASSET-01-a 逐子卡填充路由（本卡只实现 AS1 POST /projects/{pid}/assets）。
路由层只做请求体形态校验与重放状态码切换；业务逻辑在 assets/service.py。
鉴权：core.auth.require_user（严格 Bearer，无匿名回退）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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
