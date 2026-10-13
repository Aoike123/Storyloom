"""Storyloom Studio — 关系域 HTTP 路由（REL-02：资产出现引用命令 OC1–OC3）。

附录 06 §2：`/api/studio/projects/{pid}/relations/occurrences`。
路由层只做请求体形态校验与重放状态码切换；业务逻辑在 relations/service.py。
只交付出现族；RL（来源关系）/BD（绑定）不在本卡。
鉴权：core.auth.require_user（严格 Bearer，无匿名回退）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.accounts import User
from ...core.auth import require_user
from ...core.db import Session
from . import service

router = APIRouter(tags=["studio-relations"])


class CreateOccurrenceIn(BaseModel):
    """OC1 请求体（附录 06 §2）。

    `asset_id`/`source_fragment_id`/`command_id` 必填（缺失由请求体形态校验
    拦截）；`range` 可空（成对可空 = "无精确范围"桶），形状与界由 service 判定
    并返回冻结的 `range_invalid`。
    """

    asset_id: str
    source_fragment_id: str
    range: dict | None = None
    command_id: str


@router.post("/projects/{pid}/relations/occurrences", status_code=201)
def create_occurrence(
    pid: str,
    body: CreateOccurrenceIn,
    user: User = Depends(require_user),
):
    """OC1 建立出现：成功 201；同 command_id 重放 200（覆盖 201）。"""
    with Session() as s:
        result = service.create_occurrence(
            s,
            user,
            pid,
            body.asset_id,
            body.source_fragment_id,
            body.range,
            body.command_id,
        )
    if result.pop("_replay", False):
        return JSONResponse(status_code=200, content=result)
    return result


@router.get("/projects/{pid}/relations/occurrences")
def list_occurrences(
    pid: str,
    asset_id: str | None = None,
    source_fragment_id: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    user: User = Depends(require_user),
):
    """OC2 出现列表（纯读；`{items, next_cursor}`，id DESC）。"""
    with Session() as s:
        return service.list_occurrences(
            s, user, pid, asset_id, source_fragment_id, cursor, limit
        )


class DeleteOccurrenceIn(BaseModel):
    """OC3 请求体：删除类命令同样带 `command_id`（00 §3 幂等）。"""

    command_id: str


@router.delete("/projects/{pid}/relations/occurrences/{occurrence_id}")
def delete_occurrence(
    pid: str,
    occurrence_id: str,
    body: DeleteOccurrenceIn,
    user: User = Depends(require_user),
):
    """OC3 删除出现：200；不存在/跨项目 404；同 command_id 重放 200。"""
    with Session() as s:
        result = service.delete_occurrence(s, user, pid, occurrence_id, body.command_id)
    result.pop("_replay", None)  # 成功与重放同为 200，仅弹内部标记
    return result
