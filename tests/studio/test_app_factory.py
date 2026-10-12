"""BASE-01-b：新 API 装配入口（隔离 app + /api/studio 路由前缀）验证。"""

import json
import os
from pathlib import Path

# 禁止引用旧 backend 模块（绝对名或解析到旧模块的相对导入）。
# 注意：新核心包 ..core.* 是合法依赖，不在此列。
FORBIDDEN_TOKENS = (
    "backend.db",
    "backend.app",
    "backend.auth",
    "backend.providers",
    "backend.worker",
    "backend.environment",
    "from ..db",
    "from ..app",
    "from ..auth",
    "from ..providers",
    "from ..worker",
    "from ..environment",
)


def test_health(client):
    resp = client.get("/api/studio/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "service": "storyloom-studio"}


def _collect_route_paths(routes):
    """app.routes 的路径集合；兼容新版 FastAPI 用 _IncludedRouter 包装 include_router 的形式。"""
    paths = set()
    for route in routes:
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
        original = getattr(route, "original_router", None)
        if original is not None:
            paths |= _collect_route_paths(original.routes)
        sub = getattr(route, "routes", None)
        if sub is not None:
            paths |= _collect_route_paths(sub)
    return paths


def test_no_legacy_routes(client):
    from backend.studio.app_factory import app

    paths = _collect_route_paths(app.routes)
    assert "/api/studio/health" in paths
    for prefix in ("/api/auth", "/api/projects", "/api/story", "/media/"):
        assert not any(p.startswith(prefix) for p in paths), (
            f"隔离 app 不应注册 {prefix} 前缀路由: {paths}"
        )
    # 交叉核对公开 OpenAPI 路径表（稳定的公共 API），同样的前缀断言。
    for path in app.openapi()["paths"]:
        for prefix in ("/api/auth", "/api/projects", "/api/story", "/media/"):
            assert not path.startswith(prefix), f"OpenAPI 不应暴露 {prefix} 前缀: {path}"
    assert client.get("/api/auth/login").status_code == 404


def test_cors_whitelist(client):
    resp = client.get("/api/studio/health", headers={"Origin": "http://127.0.0.1:3021"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://127.0.0.1:3021"

    resp = client.get("/api/studio/health", headers={"Origin": "http://evil.example.com"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_no_legacy_imports():
    studio_dir = Path(__file__).resolve().parents[2] / "backend" / "studio"
    for name in ("app_factory.py", "router.py"):
        text = (studio_dir / name).read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            assert token not in text, f"{name} 含禁止引用 {token!r}"


def test_asgi_entry():
    import backend.studio.app_factory as m

    assert callable(m.app)


# 运行期装配入口必须注册的完整表集（各领域 models 模块 import 即注册）。
# 断言在干净子进程内进行，不依赖本进程的导入顺序。
_EXPECTED_TABLES = {
    "users",
    "studio_projects",
    "studio_source_revisions",
    "studio_source_relations",
    "studio_range_sets",
    "studio_fragments",
    "studio_fragment_revisions",
    "studio_production_scopes",
    "studio_change_previews",
    "studio_command_records",
    "studio_review_decisions",
    "studio_script_revisions",
    "studio_script_objects",
    "studio_script_adoptions",
    "studio_scenes",
    "studio_shots",
    "studio_shot_revisions",
    "studio_assets",
    "studio_asset_occurrences",
    "studio_asset_revisions",
    "studio_asset_bindings",
    "studio_conversations",
    "studio_messages",
    "studio_proposals",
    "studio_media_artifacts",
    "studio_edit_drafts",
    "studio_edit_instances",
    "studio_confirmed_edits",
    "studio_releases",
    "studio_jobs",
    "studio_job_attempts",
    "studio_job_events",
}

# 干净解释器中只导入运行期装配入口，再复现 uvicorn lifespan 的建表动作。
# 必须用子进程：本目录 conftest.py 在收集期已导入全部模型模块，同进程断言
# 会被掩盖（该掩盖正是本缺陷此前未被发现的原因）。
_REGISTRATION_PROBE = """
import json
import sys

sys.path.insert(0, {root!r})
import backend.studio.app_factory  # noqa: F401  仅经过运行期装配入口导入

from backend.core.db import Base, build_engine

engine = build_engine("sqlite://")
with engine.connect() as conn:
    Base.metadata.create_all(conn)
    created = {{
        row[0]
        for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
    }}
print(json.dumps(sorted(created)))
"""


def test_importing_app_registers_all_domain_models(tmp_path):
    """回归：app_factory 必须 import 全部领域模型模块。

    uvicorn 的 lifespan 调用 init_db() → Base.metadata.create_all(engine)。
    若某个领域 models 模块没被运行期导入，其表既不会建立，也会让引用它的
    领域（如 assets 的 FK → studio_assets / studio_shots）建表报
    NoReferencedTableError，真实服务器在全新数据根上直接启动失败。
    """
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(root), STUDIO_DATA_DIR=str(tmp_path))
    probe = _REGISTRATION_PROBE.format(root=str(root))
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"装配入口建表失败：{proc.stderr[-2000:]}"

    created = set(json.loads(proc.stdout.strip().splitlines()[-1]))
    missing = _EXPECTED_TABLES - created
    assert not missing, f"app_factory 未注册领域模型，缺表: {sorted(missing)}"
    unexpected = created - _EXPECTED_TABLES
    assert not unexpected, f"出现未登记的表（新领域须同步本清单与 conftest）: {sorted(unexpected)}"
