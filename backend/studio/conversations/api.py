"""Storyloom Studio — 会话域 HTTP 路由（/api/studio/projects/{pid}/conversations*，附录 10 §1–§3）。

CHAT-01 五路由：会话列表 / 取或建 / 详情 / 消息列表 / 追加消息。路由层只做
请求体校验与 DTO 组装；业务逻辑在 conversations/service.py。鉴权：
core.auth.require_user（严格 Bearer/sl_auth，无匿名回退）。
"""
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.accounts import User
from ...core.auth import require_user
from ...core.db import Session
from . import service

router = APIRouter(tags=["studio-conversations"])


class OpenConversationIn(BaseModel):
    """路由 2 请求体：{scope_kind, scope_id, command_id}。"""

    scope_kind: str
    scope_id: str
    command_id: str


class PostMessageIn(BaseModel):
    """路由 5 请求体：{sender, content, refs?, job_id?, command_id}。

    refs 用 Any 承接：其形状判定属冻结错误协议（422 field=refs rule=invalid），
    在 service 层完成，避免 FastAPI 默认校验体破坏冻结错误形状。
    """

    sender: str
    content: str
    refs: Any = None
    job_id: str | None = None
    command_id: str


@router.get("/projects/{pid}/conversations")
def list_conversations(
    pid: str,
    user: User = Depends(require_user),
    scope_kind: str | None = None,
    scope_id: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
):
    """路由 1 会话列表：200 {items, next_cursor}；纯读，id 升序、keyset 分页。"""
    with Session() as s:
        return service.list_conversations(s, user, pid, scope_kind, scope_id, cursor, limit)


@router.post("/projects/{pid}/conversations", status_code=201)
def open_conversation(
    pid: str, body: OpenConversationIn, user: User = Depends(require_user)
):
    """路由 2 会话取或建：新建 201；命中重放或已存在四元组行 → 200 原 DTO。"""
    with Session() as s:
        result = service.open_conversation(
            s, user, pid, body.scope_kind, body.scope_id, body.command_id
        )
    if result.pop("_replay", False):
        return JSONResponse(status_code=200, content=result)
    return result


@router.get("/projects/{pid}/conversations/{cid}")
def get_conversation(pid: str, cid: str, user: User = Depends(require_user)):
    """路由 3 会话详情：200 ConversationDto + 最近 50 条消息（id 升序）；纯读。"""
    with Session() as s:
        return service.get_conversation(s, user, pid, cid)


@router.get("/projects/{pid}/conversations/{cid}/messages")
def list_messages(
    pid: str,
    cid: str,
    user: User = Depends(require_user),
    cursor: str | None = None,
    limit: int = 50,
):
    """路由 4 消息列表：200 {items, next_cursor}；纯读，id 升序、keyset 分页。"""
    with Session() as s:
        return service.list_messages(s, user, pid, cid, cursor, limit)


@router.post("/projects/{pid}/conversations/{cid}/messages", status_code=201)
def post_message(
    pid: str,
    cid: str,
    body: PostMessageIn,
    user: User = Depends(require_user),
):
    """路由 5 追加消息：201 MessageDto；幂等重放 → 200 原 MessageDto。"""
    with Session() as s:
        result = service.post_message(
            s,
            user,
            pid,
            cid,
            body.sender,
            body.content,
            body.refs,
            body.job_id,
            body.command_id,
        )
    if result.pop("_replay", False):
        return JSONResponse(status_code=200, content=result)
    return result
