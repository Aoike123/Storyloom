"""Storyloom Studio — 新工作台后端装配入口。

创建隔离的 ASGI app：只挂载 /api/studio 前缀路由，不注册任何旧业务路由，
不 import 任何旧 backend 模块。CORS 白名单冻结为 ROOT-01 的前端端口 3021。

建表（init_db）放在 lifespan 启动事件：只在真实服务器（uvicorn）启动时
幂等建表；import 本模块或裸用 TestClient 不触发，避免测试执行期的副作用。

uvicorn 启动方式：.venv/bin/python -m uvicorn backend.studio.app_factory:app
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..core.auth import router as auth_router
from ..core.db import init_db
from .contracts.errors import install_error_handlers
from .router import router

# 领域模型注册（import 即注册到 Base.metadata）。lifespan 的 init_db() 按
# Base.metadata 建表：未被运行期导入的模型模块既不会建表，也会让引用它的
# 领域在全新数据根上以 NoReferencedTableError 直接启动失败。此清单必须与
# tests/studio/conftest.py 的模型注册表保持同一集合，并有回归测试守护。
from .assets import models as _assets_models  # noqa: F401
from .conversations import models as _conversations_models  # noqa: F401
from .edits import models as _edits_models  # noqa: F401
from .jobs import models as _jobs_models  # noqa: F401
from .media import models as _media_models  # noqa: F401
from .publishing import models as _publishing_models  # noqa: F401
from .relations import models as _relations_models  # noqa: F401
from .reviews import models as _reviews_models  # noqa: F401
from .scripts import models as _scripts_models  # noqa: F401
from .storyboard import models as _storyboard_models  # noqa: F401

# ROOT-01 冻结的前端 dev 端口（账本 implementation 块）。
ALLOWED_ORIGIN = "http://127.0.0.1:3021"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 服务器启动时幂等建表。上方领域模型注册加各域 router/service 的导入
    # （import 即注册）共同决定 Base.metadata 的完整性。
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Storyloom Studio", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 冻结错误协议：所有 StudioAPIError → {"error":{code,message,details}}
    install_error_handlers(app)
    app.include_router(router)
    # 账号系统（D09）：register/login/me，严格鉴权
    app.include_router(auth_router)
    return app


app = create_app()
