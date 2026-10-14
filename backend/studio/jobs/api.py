"""Storyloom Studio — 任务域 HTTP 路由（JOB-02：幂等提交作业，附录 09 §1/§3/§4）。

只实现提交：`POST /projects/{pid}/jobs`。执行/租约/重试/取消归 JOB-03/05/06/07。
路由层只做请求体形态校验与重放状态码切换；业务逻辑在 jobs/service.py。
鉴权：core.auth.require_user（严格 Bearer/sl_auth，无匿名回退）。
"""
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.accounts import User
from ...core.auth import require_user
from ...core.db import Session
from . import service

router = APIRouter(tags=["studio-jobs"])


class TargetIn(BaseModel):
    """作业目标引用（附录 00 冻结 ObjectRef 形状；revision 可空）。"""

    kind: str
    id: str
    revision: int | None = None


class CreateJobIn(BaseModel):
    """JOB-02 请求体（附录 09 §3/§4）。

    ``input`` 声明为 ``Any``：非 object 必须走 service 的冻结 422 形状
    （field=input rule=invalid），不能被请求体形态校验提前拦成 FastAPI 默认 422。
    ``confirm_cost`` 为收费 kind 的显式计费确认（§4）。
    """

    kind: str
    target: TargetIn
    input: Any = None
    priority: int = 0
    confirm_cost: bool = False
    command_id: str


@router.post("/projects/{pid}/jobs", status_code=201)
def create_job(pid: str, body: CreateJobIn, user: User = Depends(require_user)):
    """提交作业（附录 09 §1/§3/§4）：成功 201；同 command_id 重放 200 原体。"""
    with Session() as s:
        result = service.create_job(
            s,
            user,
            pid,
            body.kind,
            body.target.model_dump(),
            body.input,
            body.priority,
            body.confirm_cost,
            body.command_id,
        )
    if result.pop("_replay", False):
        # 重放命中：200 原样返回首次 result_payload（覆盖 201）
        return JSONResponse(status_code=200, content=result)
    return result
