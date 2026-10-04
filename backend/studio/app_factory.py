"""Storyloom Studio — 新工作台后端装配入口。

创建隔离的 ASGI app：只挂载 /api/studio 前缀路由，不注册任何旧业务路由，
不 import 任何旧 backend 模块。CORS 白名单冻结为 ROOT-01 的前端端口 3021。

uvicorn 启动方式：.venv/bin/python -m uvicorn backend.studio.app_factory:app
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .router import router

# ROOT-01 冻结的前端 dev 端口（账本 implementation 块）。
ALLOWED_ORIGIN = "http://127.0.0.1:3021"


def create_app() -> FastAPI:
    app = FastAPI(title="Storyloom Studio")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
