import hashlib

import pytest

from backend import model_access
from backend.db import Record, Session, Task, uid
from backend.environment import model_config


OWN_KEYS = {
    "deepseek": "deepseek-test-key",
    "siliconflow": "siliconflow-test-key",
    "minimax": "minimax-test-key",
}


def test_byok_is_encrypted_locked_and_inherited(client, monkeypatch):
    monkeypatch.setenv("MODEL_ACCESS_SECRET", "test-secret-" + "a" * 48)
    response = client.post("/api/model-access/sessions", json={"mode": "own", "keys": OWN_KEYS})
    assert response.status_code == 200
    session = response.json()
    access_id = "model_access_" + hashlib.sha256(session["token"].encode()).hexdigest()

    with Session() as db:
        saved = db.get(Record, access_id).data["credentials"]
        assert not any(key in saved for key in OWN_KEYS.values())

    with model_access.access_scope(access_id):
        cfg = model_config()
        assert cfg["LLM_API_KEY"] == OWN_KEYS["deepseek"]
        assert cfg["LLM_BASE_URL"] == "https://api.deepseek.com"
        assert cfg["LLM_MODEL"] == "deepseek-v4-flash"
        assert cfg["IMAGE_MODEL"] == "Tongyi-MAI/Z-Image-Turbo"
        assert cfg["VIDEO_MODEL"] == "MiniMax-H3-Max"
        assert cfg["VIDEO_ENDPOINT"] == "https://api.minimax.cn/v2/video_generation"
        with Session.begin() as db:
            task = Task(id=uid("bound"), kind="author_flow")
            db.add(task)
            db.flush()
            assert task.session_id == access_id


def test_empty_public_pool_cannot_be_selected(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_POOL_ENABLED", "true")
    for name in (
        "PUBLIC_POOL_LLM_DAILY_BUDGET_CNY",
        "PUBLIC_POOL_IMAGE_DAILY_BUDGET_CNY",
        "PUBLIC_POOL_VIDEO_DAILY_BUDGET_CNY",
    ):
        monkeypatch.setenv(name, "0")
    for name in ("LLM_API_KEY", "IMAGE_API_KEY", "VIDEO_API_KEY"):
        monkeypatch.setenv(name, "operator-test-key")
    monkeypatch.setattr(model_access, "_deepseek_balance", lambda *_args, **_kwargs: {"state": "verified", "available": True})

    status = client.get("/api/model-access/status").json()["pool"]
    assert status["available"] is False
    assert {item["kind"]: item["remaining_cny"] for item in status["providers"]} == {
        "llm": "0.00", "image": "0.00", "video": "0.00",
    }
    response = client.post("/api/model-access/sessions", json={"mode": "public"})
    assert response.status_code == 409


def test_public_pool_reservations_are_independent_by_key(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_POOL_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_POOL_LLM_DAILY_BUDGET_CNY", "0.50")
    monkeypatch.setenv("PUBLIC_POOL_IMAGE_DAILY_BUDGET_CNY", "1.00")
    monkeypatch.setenv("PUBLIC_POOL_VIDEO_DAILY_BUDGET_CNY", "12.00")
    monkeypatch.setenv("PUBLIC_POOL_LLM_RESERVE_CNY", "0.30")
    monkeypatch.setenv("PUBLIC_POOL_IMAGE_RESERVE_CNY", "0.50")
    monkeypatch.setenv("PUBLIC_POOL_VIDEO_RESERVE_CNY", "6.00")
    for name in ("LLM_API_KEY", "IMAGE_API_KEY", "VIDEO_API_KEY"):
        monkeypatch.setenv(name, "operator-test-key")
    monkeypatch.setattr(model_access, "_deepseek_balance", lambda *_args, **_kwargs: {"state": "verified", "available": True})

    created = client.post("/api/model-access/sessions", json={"mode": "public"}).json()
    access_id = model_access.resolve_access_token(created["token"])
    with model_access.access_scope(access_id):
        assert model_access.authorize_call("llm")["reserved_cny"] == "0.30"
        status = model_access.public_pool_status(refresh=False)
        providers = {item["kind"]: item for item in status["providers"]}
        assert providers["llm"]["remaining_cny"] == "0.20"
        assert providers["llm"]["available"] is False
        assert providers["image"]["remaining_cny"] == "1.00"
        assert providers["image"]["available"] is True
        assert providers["video"]["remaining_cny"] == "12.00"
        assert providers["video"]["available"] is True
        assert model_access.access_paid_states() == {"all": False, "llm": False, "image": True, "video": True}
        with pytest.raises(model_access.ModelAccessError, match="额度"):
            model_access.authorize_call("llm")
        assert model_access.authorize_call("image")["reserved_cny"] == "0.50"
    assert model_access.public_pool_status(refresh=False)["available"] is False

    with Session() as db:
        rows = db.query(Record).filter(Record.kind == model_access.POOL_KIND).all()
        assert {(row.data["kind"], row.data["reserved_cny"]) for row in rows} == {
            ("llm", "0.30"), ("image", "0.50"),
        }


def test_provider_failure_only_blocks_its_own_shared_key(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_POOL_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_POOL_LLM_DAILY_BUDGET_CNY", "1.00")
    monkeypatch.setenv("PUBLIC_POOL_IMAGE_DAILY_BUDGET_CNY", "1.00")
    monkeypatch.setenv("PUBLIC_POOL_VIDEO_DAILY_BUDGET_CNY", "12.00")
    for name in ("LLM_API_KEY", "IMAGE_API_KEY", "VIDEO_API_KEY"):
        monkeypatch.setenv(name, "operator-test-key")
    monkeypatch.setattr(model_access, "_deepseek_balance", lambda *_args, **_kwargs: {"state": "verified", "available": True})

    created = client.post("/api/model-access/sessions", json={"mode": "public"}).json()
    access_id = model_access.resolve_access_token(created["token"])
    with model_access.access_scope(access_id):
        model_access.mark_public_provider_unavailable("image", 402)
        assert model_access.access_paid_states() == {"all": False, "llm": True, "image": False, "video": True}
        assert model_access.authorize_call("llm")["pool_kind"] == "llm"
        with pytest.raises(model_access.ModelAccessError, match="暂停"):
            model_access.authorize_call("image")
