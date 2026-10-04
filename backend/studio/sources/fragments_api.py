"""Storyloom Studio — 片段域 HTTP 路由（/api/studio/projects/{pid}/fragments*，附录 02 §2）。

SOURCE-03..08 逐子卡填充（F1 POST /fragments 等）。路由层只做请求体
校验与 DTO 组装；业务逻辑在 fragment_service.py。鉴权：core.auth
.require_user（严格 Bearer/sl_auth，无匿名回退）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.auth import require_user
from ...core.db import Session

from . import fragment_service

router = APIRouter(tags=["studio-fragments"])


def _session():
    """请求级 DB session（每次请求创建并在结束后关闭）。"""
    db = Session()
    try:
        yield db
    finally:
        db.close()


class FragmentRange(BaseModel):
    start: int
    end: int


class CreateFragmentBody(BaseModel):
    source_revision_id: str
    range: FragmentRange
    name: str
    expected_range_set_revision: int | None = None
    command_id: str


@router.post("/projects/{pid}/fragments")
def create_fragment_route(
    pid: str,
    body: CreateFragmentBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F1 原子保存候选片段：201；幂等重放命中 → 200（原 result_payload，零写零 commit）。"""
    result = fragment_service.create_fragment(
        db,
        user,
        pid,
        body.source_revision_id,
        body.range.start,
        body.range.end,
        body.name,
        body.expected_range_set_revision,
        body.command_id,
    )
    replay = result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200 if replay else 201)


class ConfirmFragmentBody(BaseModel):
    expected_revision: int
    command_id: str


@router.post("/projects/{pid}/fragments/{fid}/confirm")
def confirm_fragment_route(
    pid: str,
    fid: str,
    body: ConfirmFragmentBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F6 确认片段：200 state=confirmed（F3 形状 DTO；不启动生产、不表示全文覆盖，
    R04）；幂等重放命中 → 200（原 result_payload，零写零 commit）。"""
    result = fragment_service.confirm_fragment(
        db,
        user,
        pid,
        fid,
        body.expected_revision,
        body.command_id,
    )
    result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200)
