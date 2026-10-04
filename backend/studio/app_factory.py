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

# ROOT-01 冻结的前端 dev 端口（账本 implementation 块）。
ALLOWED_ORIGIN = "http://127.0.0.1:3021"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 服务器启动时幂等建表：上方 import 已注册 users 等模型；后续域卡在
    # 顶部 import 各自的 router/service（import 即注册模型）后自动覆盖。
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
