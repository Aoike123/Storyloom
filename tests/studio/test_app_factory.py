"""BASE-01-b：新 API 装配入口（隔离 app + /api/studio 路由前缀）验证。"""

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
