"""Storyloom Studio — 来源域 HTTP 路由（/api/studio/projects/{pid}/sources*，附录 01 §2）。

SOURCE-01-d/e 逐子卡填充路由（S1 POST、S3 GET /{revisionId}）；
S2/S4/S5/S6 属 SOURCE-03+ 后续卡。路由层只做请求体校验与 DTO 组装；
业务逻辑在 sources/service.py。鉴权：core.auth.require_user。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.auth import require_user
from ...core.db import Session
from .service import get_source_revision, import_source

router = APIRouter(tags=["studio-sources"])


class ImportSourceBody(BaseModel):
    """S1 请求体（附录 01 §2）：content 原样保存（不 trim）；command_id 幂等键。"""

    content: str
    command_id: str


def _session():
    """请求级 DB session（每次请求创建并在结束后关闭）。"""
    db = Session()
    try:
        yield db
    finally:
        db.close()


@router.post("/projects/{pid}/sources", status_code=201)
def import_source_route(
    pid: str,
    body: ImportSourceBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """S1 导入不可变原文（附录 01 §2）：201 新修订 DTO；幂等重放命中 → 200 原结果。"""
    result = import_source(db, user, pid, body.content, body.command_id)
    if result.pop("_replay", None):
        return JSONResponse(status_code=200, content=result)
    return result


@router.get("/projects/{pid}/sources/{revision_id}")
def get_source_revision_route(
    pid: str,
    revision_id: str,
    db=Depends(_session),
    user=Depends(require_user),
):
    """S3 读取来源版本（附录 01 §2）：200 完整修订 DTO；404 project/source。纯读。"""
    return get_source_revision(db, user, pid, revision_id)
