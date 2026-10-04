"""Storyloom Studio — 项目域 HTTP 路由（/api/studio/projects*，附录 01 §2）。

SOURCE-01-a..c 逐子卡填充路由（P1 POST /projects、P3 GET /projects/{pid}、
P2 GET /projects）。路由层只做请求体校验与 DTO 组装；业务逻辑在
projects/service.py。鉴权：core.auth.require_user（严格 Bearer/sl_auth，
无匿名回退）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.accounts import User
from ...core.auth import require_user
from ...core.db import Session
from . import service

router = APIRouter(tags=["studio-projects"])


class CreateProjectIn(BaseModel):
    """P1 请求体（附录 01 §2）：name、description 可选、command_id 必填。"""

    name: str
    description: str = ""
    command_id: str


@router.post("/projects", status_code=201)
def create_project(body: CreateProjectIn, user: User = Depends(require_user)):
    with Session() as s:
        result = service.create_project(
            s, user, body.name, body.description, body.command_id
        )
    if result.pop("_replay", False):
        # 重放命中：200 原样返回首次 result_payload（覆盖 201）
        return JSONResponse(status_code=200, content=result)
    return result
