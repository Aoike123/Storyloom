"""Storyloom Studio — 正文变更域 HTTP 路由（/api/studio/projects/{pid}/source-changes*，附录 01 §2）。

SOURCE-09 填充 S4 preview（POST /projects/{pid}/source-changes/preview，
kind=source_activate）；S5 apply 属 SOURCE-10（后续卡，不在本文件本次
范围）。路由层只做请求体校验与 DTO 组装；业务逻辑在 changes.py。
鉴权：core.auth.require_user（严格 Bearer/sl_auth，无匿名回退）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.auth import require_user
from ...core.db import Session

from . import changes

router = APIRouter(tags=["studio-source-changes"])


def _session():
    """请求级 DB session（每次请求创建并在结束后关闭）。"""
    db = Session()
    try:
        yield db
    finally:
        db.close()


class ActivatePreviewBody(BaseModel):
    """S4 请求体（附录 01 §2）：kind 冻结 "source_activate"；command_id 幂等键。"""

    kind: str
    target_revision_id: str
    command_id: str


@router.post("/projects/{pid}/source-changes/preview")
def preview_source_activate_route(
    pid: str,
    body: ActivatePreviewBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """S4 预览正文版本激活：200 恰 4 字段 {preview_id, kind, baseline, impact}
    （00 §5.1；kind=source_activate；preview 是一次幂等写，记录 state=pending
    不外露）；幂等重放命中 → 200（原 result_payload，零写零 commit）。"""
    result = changes.preview_source_activate(
        db,
        user,
        pid,
        body.kind,
        body.target_revision_id,
        body.command_id,
    )
    result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200)
