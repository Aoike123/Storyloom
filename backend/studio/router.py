"""Storyloom Studio — 新工作台 HTTP 路由（/api/studio 前缀，与旧业务路由隔离）。

各域子路由（projects/sources/…）在此 include；子路由自身不带 /api/studio
前缀，只写域内路径（如 /projects、/projects/{pid}/sources）。

域子路由分两种情况：

1. 已交付域（projects/sources）直接 import；
2. 尚未交付域（storyboard/scripts/assets/media/edits/conversations）由主模型
   预先接线：模块文件已存在时才 import 并 include。这样并行派发的域卡各自只写
   自己的 api.py/service.py，不必争改本文件；域卡交付后，路由在下次启动时自动
   挂载。

情况 2 的“存在才接线”用文件存在性判断，不用 try/except：模块存在但导入失败
（语法错、循环导入、缺失契约名）必须原样失败，不得被接线逻辑吞掉。
"""

import importlib
import importlib.util
from pathlib import Path

from fastapi import APIRouter

from .projects.api import router as projects_router
from .sources.api import router as sources_router
from .sources.changes_api import router as changes_router
from .sources.fragments_api import router as fragments_router
from .sources.scopes_api import router as scopes_router

router = APIRouter(prefix="/api/studio", tags=["studio"])


@router.get("/health")
def health():
    return {"ok": True, "service": "storyloom-studio"}


router.include_router(projects_router)
router.include_router(sources_router)
router.include_router(fragments_router)
router.include_router(changes_router)
router.include_router(scopes_router)


def _include_if_present(module_path: str) -> bool:
    """挂载待交付域的子路由；模块文件不存在时跳过。

    参数为相对本包的域模块路径（如 "scripts.api"）。返回是否已挂载。
    """
    dotted = f"{__package__}.{module_path}"
    relative = Path(*module_path.split(".")).with_suffix(".py")
    if not (Path(__file__).parent / relative).is_file():
        return False
    if importlib.util.find_spec(dotted) is None:
        return False
    module = importlib.import_module(dotted)
    router.include_router(module.router)
    return True


# 尚未交付域：文件存在即挂载（并行派发期间由主模型统一接线，域卡不改本文件）。
for _pending in (
    "storyboard.api",
    "scripts.api",
    "assets.api",
    "media.api",
    "edits.api",
    "conversations.api",
):
    _include_if_present(_pending)
