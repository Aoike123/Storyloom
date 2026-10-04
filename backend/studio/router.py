"""Storyloom Studio — 新工作台 HTTP 路由（/api/studio 前缀，与旧业务路由隔离）。

各域子路由（projects/sources/…）在此 include；子路由自身不带 /api/studio
前缀，只写域内路径（如 /projects、/projects/{pid}/sources）。
"""

from fastapi import APIRouter

from .projects.api import router as projects_router
from .sources.api import router as sources_router
from .sources.fragments_api import router as fragments_router

router = APIRouter(prefix="/api/studio", tags=["studio"])


@router.get("/health")
def health():
    return {"ok": True, "service": "storyloom-studio"}


router.include_router(projects_router)
router.include_router(sources_router)
router.include_router(fragments_router)
