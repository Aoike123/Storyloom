"""Storyloom Studio — 新工作台 HTTP 路由（/api/studio 前缀，与旧业务路由隔离）。"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/studio", tags=["studio"])


@router.get("/health")
def health():
    return {"ok": True, "service": "storyloom-studio"}
