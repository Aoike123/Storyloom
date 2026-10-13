"""Storyloom Studio — 剧本域 HTTP 路由（/api/studio/projects/{pid}/script-objects*，附录 03 §2）。

SCRIPT-01-a 逐子卡填充路由（本卡只实现 SC1 POST /projects/{pid}/script-objects）。
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
