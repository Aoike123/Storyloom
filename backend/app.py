import ipaddress
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from .auth import request_ctx, router as auth_router
from .authors import router as author_router
from .creative import router as creative_router
from .db import DATA, Record, Session, Task, init_db, record_dict, task_dict
from .director import router as director_router
from .preproduction import router as preproduction_router
from .production import reader as reader_router
from .production import router as production_router
from .reader_branch import router as reader_branch_router
from .video_files import StorageError
from .video_storage import router as storage_router


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
app.include_router(auth_router)
app.include_router(director_router)
app.include_router(production_router)
app.include_router(reader_router)
app.include_router(reader_branch_router)
app.include_router(storage_router)

from .consistency import router as consistency_router

app.include_router(consistency_router)
app.include_router(preproduction_router)
app.include_router(creative_router)
app.include_router(author_router)
app.mount("/media", StaticFiles(directory=DATA / "media"), name="media")


@app.exception_handler(StorageError)
async def storage_error(_request: Request, exc: StorageError):
    return JSONResponse({"detail": str(exc)}, status_code=422)


def _loopback_origin(origin: str) -> bool:
    """The workbench guard cares about *where* the page runs, not which port the
    developer happened to pick: any loopback host is the local workbench."""
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@app.middleware("http")
async def local_only(request: Request, call_next):
    if request.method not in ["GET", "HEAD", "OPTIONS"]:
        origin = request.headers.get("origin")
        if origin and not _loopback_origin(origin):
            return JSONResponse({"detail": "仅允许本地工作台发起操作"}, status_code=403)
    path = request.url.path
    response = await call_next(request)
    if path.startswith("/api/"):
        streaming = response.headers.get("content-type", "").startswith(
            "text/event-stream"
        )
        response.headers["Cache-Control"] = (
            "no-store, no-transform" if streaming else "no-store"
        )
    elif path.startswith("/media/clips/") or path.startswith("/media/clip_uses/"):
        # These paths are content-addressed/immutable. Browser caching and range
        # reuse prevent a branch transition from downloading the same clip twice.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/media/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Expose the current request to deep helpers (the author owner check) via a contextvar."""
    token = request_ctx.set(request)
    try:
        return await call_next(request)
    finally:
        request_ctx.reset(token)


@app.get("/api/health")
def health():
    with Session() as db:
        worker = db.get(Record, "worker_heartbeat")
        return {
            "ok": True,
            "worker_online": bool(worker and time.time() - worker.data["at"] < 120),
            "database": (
                "PostgreSQL"
                if db.bind.dialect.name == "postgresql"
                else "SQLite 本地模式"
            ),
        }


@app.get("/api/diagnostics/{code}")
def failure_detail(code: str):
    """Full server-side failure detail. The interface only ever shows the short code, not the stack."""
    from .diagnostics import failure_report

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


@app.get("/api/models")
def models():
    """Read-only local model availability for the workbench status chip; never exposes keys."""
    from .providers import settings

    cfg = settings()
    return {
        "llm_configured": cfg["llm_configured"],
        "image_configured": cfg["image_configured"],
        "video_configured": cfg["video_configured"],
        "models": {
            "llm": cfg["llm_model"],
            "image": cfg["image_model"],
            "video": cfg["video_model"],
        },
        "config_file": cfg["config_file"],
    }


@app.get("/api/platform")
def platform():
    from .workflows import WORKFLOWS

    with Session() as db:
        works = [
            record_dict(row)
            for row in db.scalars(
                select(Record)
                .where(Record.kind == "author_project")
                .order_by(Record.created.desc())
            )
        ]
        published_count = db.scalar(
            select(func.count())
            .select_from(Record)
            .where(Record.kind == "reader_release")
        )
        project_works = {
            work["director_id"]: work["id"] for work in works if work.get("director_id")
        }
        recommendation_works = {
            work["recommend_task"]: work["id"]
            for work in works
            if work.get("recommend_task")
        }
        tasks = []
        for task in db.scalars(select(Task).order_by(Task.created.desc())):
            work_id = task.payload.get("work_id") or recommendation_works.get(task.id)
            if not work_id:
                work_id = next(
                    (
                        project_works[task.payload[key]]
                        for key in (
                            "creative_id",
                            "director_id",
                            "preproduction_id",
                            "project_id",
                        )
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
