import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select

from .authors import router as author_router
from .catalog import router as catalog_router
from .creative import router as creative_router
from .db import DATA, Record, Session, Task, TaskCapacityError, init_db, record_dict, task_dict
from .director import router as director_router
from .local_config import save_config
from .model_access import (
    ACCESS_HEADER,
    ModelAccessError,
    access_scope,
    public_demo_mode,
    resolve_access_token,
    router as model_access_router,
)
from .preproduction import router as preproduction_router
from .production import reader as reader_router
from .production import router as production_router
from .reader_branch import router as reader_branch_router
from .providers import settings
from .public_limits import request_retry_after
from .video_files import StorageError
from .video_storage import router as storage_router
from .zhihu_stories import router as story_router
from .zhihu_oauth import public_router as zhihu_callback_router, router as zhihu_login_router


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    title="叙间 · 本地工作台",
    lifespan=lifespan,
)
app.include_router(story_router)
app.include_router(zhihu_login_router)
app.include_router(zhihu_callback_router)
app.include_router(director_router)
app.include_router(production_router)
app.include_router(reader_router)
app.include_router(reader_branch_router)
app.include_router(catalog_router)
app.include_router(storage_router)

from .consistency import router as consistency_router

app.include_router(consistency_router)
app.include_router(preproduction_router)
app.include_router(creative_router)
app.include_router(author_router)
app.include_router(model_access_router)
app.mount("/media", StaticFiles(directory=DATA / "media"), name="media")


@app.exception_handler(StorageError)
async def storage_error(_request: Request, exc: StorageError):
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.exception_handler(TaskCapacityError)
async def task_capacity_error(_request: Request, exc: TaskCapacityError):
    return JSONResponse(
        {"detail": str(exc)},
        status_code=503,
        headers={"Cache-Control": "no-store", "Retry-After": "30"},
    )


@app.middleware("http")
async def local_only(request: Request, call_next):
    retry_after = request_retry_after(request)
    if retry_after is not None:
        return JSONResponse(
            {"detail": "请求太频繁，请稍后再试。"},
            status_code=429,
            headers={"Cache-Control": "no-store", "Retry-After": str(retry_after)},
        )
    try:
        access_id = resolve_access_token(request.headers[ACCESS_HEADER]) if ACCESS_HEADER in request.headers else ""
    except ModelAccessError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401, headers={"Cache-Control": "no-store"})
    if not access_id:
        # A signed-in Zhihu account pays from its bean wallet. An explicit access header still wins,
        # so an account whose beans ran out can switch to its own keys and keep working.
        from .zhihu_oauth import signed_in_access_id

        access_id = signed_in_access_id(request) or ""
    with access_scope(access_id):
        if request.method not in ["GET", "HEAD", "OPTIONS"]:
            origin = request.headers.get("origin")
            if origin and origin not in [
                "http://127.0.0.1:3000",
                "http://localhost:3000",
                "http://127.0.0.1:8000",
                "http://localhost:8000",
            ]:
                return JSONResponse({"detail": "仅允许本地工作台发起操作"}, status_code=403)
        path = request.url.path
        response = await call_next(request)
        if path.startswith("/api/"):
            streaming = response.headers.get("content-type", "").startswith("text/event-stream")
            response.headers["Cache-Control"] = "no-store, no-transform" if streaming else "no-store"
        elif path.startswith("/media/clips/") or path.startswith("/media/clip_uses/"):
            # These paths are content-addressed/immutable. Browser caching and range
            # reuse prevent a branch transition from downloading the same clip twice.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/media/"):
            response.headers["Cache-Control"] = "no-store"
        return response


@app.get("/api/health")
def health():
    with Session() as db:
        worker = db.get(Record, "worker_heartbeat")
        return {
            "ok": True,
            "worker_online": bool(worker and time.time() - worker.data["at"] < 120),
            "database": "PostgreSQL" if db.bind.dialect.name == "postgresql" else "SQLite 本地模式",
        }


@app.get("/api/diagnostics/{code}")
def failure_detail(code: str):
    """Full server-side failure detail. Visitors only ever see the short code, not the stack."""
    from .diagnostics import failure_report
    from .model_access import public_demo_mode

    if public_demo_mode():
        raise HTTPException(404, "公开演示模式不提供错误详情，请在本地工作台查看。")
    report = failure_report(code)
    if not report:
        raise HTTPException(404, "没有这条错误记录。")
    return report


@app.get("/api/node-skills")
def node_skills():
    from .skill_runtime import catalog

    return {"version": "1.0.0", "nodes": catalog()}


@app.get("/api/node-skills/{node}")
def node_skill_detail(node: str):
    from .providers import ProviderError
    from .skill_runtime import public, snapshot

    try:
        binding = snapshot(node)
    except ProviderError as exc:
        raise HTTPException(404, str(exc)) from None
    return {**public(binding), "effective_instructions": binding["system"]}


@app.get("/api/platform")
def platform():
    from .workflows import WORKFLOWS

    with Session() as db:
        works = [
            record_dict(row)
            for row in db.scalars(
                select(Record).where(Record.kind == "author_project").order_by(Record.created.desc())
            )
        ]
        published_count = db.scalar(
            select(func.count()).select_from(Record).where(Record.kind == "reader_release")
        )
        project_works = {work["director_id"]: work["id"] for work in works if work.get("director_id")}
        recommendation_works = {
            work["recommend_task"]: work["id"] for work in works if work.get("recommend_task")
        }
        tasks = []
        for task in db.scalars(select(Task).order_by(Task.created.desc())):
            work_id = task.payload.get("work_id") or recommendation_works.get(task.id)
            if not work_id:
                work_id = next(
                    (
                        project_works[task.payload[key]]
                        for key in ("creative_id", "director_id", "preproduction_id", "project_id")
                        if task.payload.get(key) in project_works
                    ),
                    None,
                )
            tasks.append({**task_dict(task), "work_id": work_id})
    return {
        "workflows": WORKFLOWS,
        "works": works,
        "tasks": tasks,
        "published_count": published_count,
        "health": health(),
    }


@app.get("/api/settings")
def get_settings():
    return settings()


class SettingsInput(BaseModel):
    values: dict[str, str]


@app.post("/api/settings")
def update_settings(body: SettingsInput):
    if public_demo_mode():
        raise HTTPException(404, "公网演示仅允许使用固定供应商，请前往模型使用方式页面。")
    with Session() as db:
        active = db.scalar(
            select(Task.id).where(Task.status.in_(["queued", "running", "waiting"])).limit(1)
        )
        if active:
            raise HTTPException(409, "请先等待或停止当前任务，再修改模型配置。")
    try:
        save_config(body.values)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except OSError:
        raise HTTPException(500, "本地配置保存失败，请检查文件权限。") from None
    return settings()
