"""Storyloom Studio — 完整制作范围域 HTTP 路由
（/api/studio/projects/{pid}/production-scopes*，附录 02 §2 F11/F12）。

SOURCE-11 填充：F11 POST /production-scopes（完整制作范围提交）、
F12 GET /production-scopes（历史倒序）与 GET /production-scopes/current
（当前范围）。路由层只做请求体校验与 DTO 组装；业务逻辑在 scopes.py。
鉴权：core.auth.require_user（严格 Bearer/sl_auth，无匿名回退）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.auth import require_user
from ...core.db import Session

from . import scopes

router = APIRouter(tags=["studio-production-scopes"])


def _session():
    """请求级 DB session（每次请求创建并在结束后关闭）。"""
    db = Session()
    try:
        yield db
    finally:
        db.close()


class ScopeCreateBody(BaseModel):
    """F11 请求体（附录 02 §2）：fragment_ids 省略/null = 全部已确认片段。"""

    fragment_ids: list[str] | None = None
    command_id: str


@router.post("/projects/{pid}/production-scopes")
def create_scope_route(
    pid: str,
    body: ScopeCreateBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F11 完整制作范围提交：201 范围 DTO（恰 6 字段；全部列名片段须 confirmed
    且绑当前 active 版本；不动 range_set cas）；幂等重放命中 → 200
    （原 result_payload，零写零 commit）。"""
    result = scopes.create_production_scope(
        db,
        user,
        pid,
        body.fragment_ids,
        body.command_id,
    )
    replay = result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200 if replay else 201)


@router.get("/projects/{pid}/production-scopes")
def list_scopes_route(
    pid: str,
    cursor: str | None = None,
    limit: int = 50,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F12 范围历史列表：200 {items, next_cursor}（限当前 active 版本，
    id DESC 倒序，keyset 分页；limit clamp/cursor 格式校验在 service 内）。"""
    result = scopes.list_production_scopes(db, user, pid, cursor, limit)
    return JSONResponse(content=result, status_code=200)


@router.get("/projects/{pid}/production-scopes/current")
def current_scope_route(
    pid: str,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F12 当前制作范围：200 范围 DTO（active 版本 id 最大一行）；
    无 active 或该版本无 scope 行 → 404。"""
    result = scopes.get_current_production_scope(db, user, pid)
    return JSONResponse(content=result, status_code=200)
