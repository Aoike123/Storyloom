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


class RenameFragmentBody(BaseModel):
    name: str
    expected_revision: int
    command_id: str


@router.post("/projects/{pid}/fragments/{fid}/rename")
def rename_fragment_route(
    pid: str,
    fid: str,
    body: RenameFragmentBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F4 片段就地改名：200 DTO（F3 形状，ID 不变；改名不产生 revision、
    不动 range_set cas）；幂等重放命中 → 200（原 result_payload，零写零 commit）。"""
    result = fragment_service.rename_fragment(
        db,
        user,
        pid,
        fid,
        body.name,
        body.expected_revision,
        body.command_id,
    )
    result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200)


class RetirePreviewBody(BaseModel):
    target_fragment_id: str
    command_id: str


class RetireApplyBody(BaseModel):
    preview_id: str
    command_id: str
    expected_revision: int
    expected_range_set_revision: int


@router.post("/projects/{pid}/fragments/{fid}/retire")
def preview_retire_route(
    pid: str,
    fid: str,
    body: RetirePreviewBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F7 预览退役片段：200 恰 4 字段 {preview_id, kind, baseline, impact}
    （00 §5.1；preview 是一次幂等写，记录 state=pending 不外露）；
    幂等重放命中 → 200（原 result_payload，零写零 commit）。"""
    result = fragment_service.preview_fragment_retire(
        db,
        user,
        pid,
        fid,
        body.target_fragment_id,
        body.command_id,
    )
    result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200)


@router.post("/projects/{pid}/fragments/{fid}/retire/apply")
def apply_retire_route(
    pid: str,
    fid: str,
    body: RetireApplyBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F7 应用退役片段：200（F3 形状 DTO，state='retired'、retired_at=now；
    不删除任何产物——R11 失效≠删除；退役不动 range_set cas）；
    幂等重放命中 → 200（原 result_payload，零写零 commit）。"""
    result = fragment_service.apply_fragment_retire(
        db,
        user,
        pid,
        fid,
        body.preview_id,
        body.command_id,
        body.expected_revision,
        body.expected_range_set_revision,
    )
    result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200)


# ───────────────────── F8 片段边界变更（preview/apply，附录 02 §2 F8 + 00 §5） ─────────────────────


class BoundaryPreviewBody(BaseModel):
    target_fragment_id: str
    new_range: FragmentRange
    command_id: str


class BoundaryApplyBody(BaseModel):
    preview_id: str
    command_id: str
    expected_revision: int
    expected_range_set_revision: int


@router.post("/projects/{pid}/fragments/{fid}/boundary")
def preview_boundary_route(
    pid: str,
    fid: str,
    body: BoundaryPreviewBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F8 预览片段边界变更：200 恰 4 字段 {preview_id, kind, baseline, impact}
    （00 §5.1；kind=fragment_boundary；preview 是一次幂等写，记录 state=pending
    不外露；range/重叠/版本非 active 均在 preview 阶段即校验——F8 行冻结）；
    幂等重放命中 → 200（原 result_payload，零写零 commit）。"""
    result = fragment_service.preview_fragment_boundary(
        db,
        user,
        pid,
        fid,
        body.target_fragment_id,
        {"start": body.new_range.start, "end": body.new_range.end},
        body.command_id,
    )
    result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200)


@router.post("/projects/{pid}/fragments/{fid}/boundary/apply")
def apply_boundary_route(
    pid: str,
    fid: str,
    body: BoundaryApplyBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F8 应用片段边界变更：200（F3 形状 DTO：新 FragmentRevision
    reason='boundary'、ID 不变、revision+1、range=new_range；range_set 是 CAS
    写点，cas+1——附录 02 §3）；幂等重放命中 → 200（原 result_payload，
    零写零 commit）。"""
    result = fragment_service.apply_fragment_boundary(
        db,
        user,
        pid,
        fid,
        body.preview_id,
        body.command_id,
        body.expected_revision,
        body.expected_range_set_revision,
    )
    result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200)


class SplitPreviewBody(BaseModel):
    target_fragment_id: str
    split_point: int
    left_name: str | None = None
    right_name: str | None = None
    command_id: str


class SplitApplyBody(BaseModel):
    preview_id: str
    command_id: str
    expected_revision: int
    expected_range_set_revision: int


@router.post("/projects/{pid}/fragments/{fid}/split")
def preview_split_route(
    pid: str,
    fid: str,
    body: SplitPreviewBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F9 预览片段拆分：200 恰 4 字段 {preview_id, kind, baseline, impact}
    （00 §5.1；kind=fragment_split；preview 是一次幂等写；命名派生（缺省
    派生 "<原 name>（左）"/"<原 name>（右）"）与 split_point 五判定均在
    preview 阶段即校验——F9 行冻结）；幂等重放命中 → 200（原 result_payload，
    零写零 commit）。"""
    result = fragment_service.preview_fragment_split(
        db,
        user,
        pid,
        fid,
        body.target_fragment_id,
        body.split_point,
        body.left_name,
        body.right_name,
        body.command_id,
    )
    result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200)


@router.post("/projects/{pid}/fragments/{fid}/split/apply")
def apply_split_route(
    pid: str,
    fid: str,
    body: SplitApplyBody,
    db=Depends(_session),
    user=Depends(require_user),
):
    """F9 应用片段拆分：200（F9 冻结形状 {left, right, original}：原片段退役
    不新建其 revision，左/右新片段各得一行 revision=1（reason='split'、
    predecessor=[原 fid]）、state='candidate'；range_set cas+1——附录 02 §3）；
    幂等重放命中 → 200（原 result_payload，零写零 commit）。"""
    result = fragment_service.apply_fragment_split(
        db,
        user,
        pid,
        fid,
        body.preview_id,
        body.command_id,
        body.expected_revision,
        body.expected_range_set_revision,
    )
    result.pop("_replay", False)
    return JSONResponse(content=result, status_code=200)
