"""Storyloom Studio — 剪辑草稿域 HTTP 路由（EDIT-01：项目剪辑草稿读写）。

附录 08 §2/§3、附录 11 §2：
- GET  ``/api/studio/projects/{pid}/edit``        → 200 当前草稿（空态全 null）；
- POST ``/api/studio/projects/{pid}/edit/draft``  → 201 新 revision（重放 200）。

路由层只做请求体形态校验与重放状态码切换；业务逻辑在 edits/service.py。
本卡不做确认（ConfirmedEdit）/发布（Release）/导出。
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

router = APIRouter(tags=["studio-edits"])


class SaveDraftIn(BaseModel):
    """保存草稿请求体（附录 11 §2）。

    ``manifest`` 故意声明为 ``Any``：非 object / schema 不符必须走 service 的
    冻结 422 violation 形状（field=manifest.schema rule=invalid），不能被
    请求体形态校验提前拦成 FastAPI 默认 422。``edit_id=null`` = 新建剪辑；
    ``expected_revision=null`` = 新建草稿的 CAS 期望（已有草稿时必须等于
    当前 cas_revision，否则 409 revision_conflict）。
    """

    edit_id: str | None = None
    manifest: Any = None
    expected_revision: int | None = None
    command_id: str


@router.get("/projects/{pid}/edit")
def get_edit(pid: str, user: User = Depends(require_user)):
    """读取当前剪辑草稿（纯读，无副作用；项目无草稿 → 空态）。"""
    with Session() as s:
        return service.get_edit(s, user, pid)


@router.post("/projects/{pid}/edit/draft", status_code=201)
def save_draft(
    pid: str,
    body: SaveDraftIn,
    user: User = Depends(require_user),
):
    """保存新草稿版本：成功 201；同 command_id 重放 200（覆盖 201）。"""
    with Session() as s:
        result = service.save_draft(
            s,
            user,
            pid,
            body.edit_id,
            body.manifest,
            body.expected_revision,
            body.command_id,
        )
    if result.pop("_replay", False):
        # 重放命中：200 原样返回首次 result_payload（覆盖 201）
        return JSONResponse(status_code=200, content=result)
    return result
