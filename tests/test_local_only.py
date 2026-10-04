"""The workbench is purely local: no accounts, no sessions, no cookies.

These tests pin that the local endpoints respond directly and never issue a session cookie — a
cookie appearing anywhere would mean an identity concept has crept back in.
"""

LOCAL_GET_ROUTES = (
    "/api/health",
    "/api/platform",
    "/api/node-skills",
    "/api/stories",
    "/api/reader/catalog",
    "/api/author/projects",
    "/api/models",
)


def test_local_routes_respond_without_issuing_a_session(client):
    for path in LOCAL_GET_ROUTES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "set-cookie" not in response.headers, path
    assert not client.cookies


def test_model_status_is_read_only_and_key_free(client):
    payload = client.get("/api/models").json()
    assert set(payload) == {
        "llm_configured",
        "image_configured",
        "video_configured",
        "models",
        "config_file",
    }
    assert {"llm", "image", "video"} <= set(payload["models"])
    assert "key" not in __import__("json").dumps(payload).lower()


# The mutation guard cares about *where* the page runs, not which port the developer
# happened to pick: any loopback origin is the local workbench; anything else is not.

IMPORT_TEXT = {"content": "雨停在凌晨三点。猫跟着她走了三条街，最后跟她回了家。"}


def test_loopback_origins_pass_on_any_port(client):
    for origin in [
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3100",
        "http://localhost:9999",
        "http://[::1]:4000",
    ]:
        response = client.post(
            "/api/stories/import", json=dict(IMPORT_TEXT), headers={"origin": origin}
        )
        assert response.status_code == 200, origin


def test_foreign_origins_are_rejected(client):
    for origin in [
        "https://evil.example.com",
        "http://192.168.1.20:3000",
        "https://127.0.0.1.evil.com",
        "not-a-url",
    ]:
        response = client.post(
            "/api/stories/import", json=dict(IMPORT_TEXT), headers={"origin": origin}
        )
        assert response.status_code == 403, origin
        assert response.json()["detail"] == "仅允许本地工作台发起操作"


def test_requests_without_an_origin_still_pass(client):
    # curl, scripts and server-to-server calls carry no Origin header at all.
    response = client.post("/api/stories/import", json=dict(IMPORT_TEXT))
    assert response.status_code == 200


def test_reads_are_never_blocked_by_origin(client):
    response = client.get(
        "/api/reader/catalog", headers={"origin": "https://evil.example.com"}
    )
    assert response.status_code == 200
