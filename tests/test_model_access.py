"""Who pays for a call: a signed-in account's beans, or the visitor's own keys.

The anonymous shared pool is gone, so these cover the two remaining modes and the guidance a
browser gets when it has neither.
"""
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
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("LLM_FAST_MODEL", "deepseek-v4-pro")
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
        assert cfg["LLM_MODEL"] == "deepseek-v4-pro"
        assert cfg["IMAGE_MODEL"] == "Tongyi-MAI/Z-Image-Turbo"
        assert cfg["VIDEO_MODEL"] == "MiniMax-H3-Max"
        assert cfg["VIDEO_ENDPOINT"] == "https://api.minimax.cn/v2/video_generation"
        with Session.begin() as db:
            task = Task(id=uid("bound"), kind="author_flow")
            db.add(task)
            db.flush()
            assert task.session_id == access_id


def test_only_some_keys_may_be_supplied(client, monkeypatch):
    """A visitor may bring just the providers a step needs."""
    monkeypatch.setenv("MODEL_ACCESS_SECRET", "test-secret-" + "a" * 48)
    response = client.post("/api/model-access/sessions",
                           json={"mode": "own", "keys": {"deepseek": "only-this-key-value"}})
    assert response.status_code == 200
    access_id = "model_access_" + hashlib.sha256(response.json()["token"].encode()).hexdigest()
    with model_access.access_scope(access_id):
        cfg = model_config()
        assert cfg["LLM_API_KEY"] == "only-this-key-value"
        # The operator's keys must never fill the gaps for a visitor session.
        assert cfg["IMAGE_API_KEY"] == "" and cfg["VIDEO_API_KEY"] == ""


def test_the_shared_pool_is_gone(client, monkeypatch):
    monkeypatch.setenv("STORYLOOM_DEMO_MODE", "public")
    status = client.get("/api/model-access/status").json()
    assert "pool" not in status

    refused = client.post("/api/model-access/sessions", json={"mode": "public"})
    assert refused.status_code == 409
    assert "共享体验池已停用" in refused.json()["detail"]
    assert "算力豆" in refused.json()["detail"]
    assert "自己的 API Key" in refused.json()["detail"]


def test_a_browser_without_a_payer_is_told_both_ways_forward(client, monkeypatch):
    monkeypatch.setenv("STORYLOOM_DEMO_MODE", "public")
    assert model_access.has_payer() is False
    message = model_access.payer_requirement_message()
    assert "知乎账号登录" in message and "算力豆" in message and "自己的 API Key" in message
    with pytest.raises(model_access.ModelAccessError, match="登录"):
        model_access.authorize_call("llm")


def test_local_mode_keeps_working_without_a_payer(client, monkeypatch):
    """A developer running the app locally is not asked to sign in."""
    monkeypatch.setenv("STORYLOOM_DEMO_MODE", "local")
    assert model_access.has_payer() is True
    assert model_access.payer_requirement_message() is None
    assert model_access.authorize_call("llm") == {"mode": "local"}


def test_a_refused_operator_key_is_remembered_for_the_day(client, monkeypatch):
    """A dead operator key must stop retrying for every visitor instead of failing each time."""
    monkeypatch.setenv("STORYLOOM_DEMO_MODE", "public")
    monkeypatch.setenv("MODEL_ACCESS_SECRET", "test-secret-" + "a" * 48)
    created = model_access.create_account_session("session-key-check", "525")
    with model_access.access_scope(created["access_id"]):
        model_access.note_operator_key_rejection("image", 402)
        assert model_access._operator_key_blocked("image") is True
    # A visitor's own rejected key is their own business and must not disable the account path.
    with Session.begin() as db:
        row = db.get(Record, model_access._operator_key_id(model_access._day(), "image"))
        row.data = {**row.data, "blocked": {}}
    own = client.post("/api/model-access/sessions",
                      json={"mode": "own", "keys": {"deepseek": "visitor-own-key-value"}}).json()
    own_id = "model_access_" + hashlib.sha256(own["token"].encode()).hexdigest()
    with model_access.access_scope(own_id):
        model_access.note_operator_key_rejection("image", 401)
        assert model_access._operator_key_blocked("image") is False


def test_an_account_with_a_blocked_operator_key_is_told_to_bring_its_own(client, monkeypatch):
    monkeypatch.setenv("STORYLOOM_DEMO_MODE", "public")
    monkeypatch.setenv("BEANS_INITIAL_GRANT", "100")
    created = model_access.create_account_session("session-blocked", "525")
    with Session.begin() as db:
        db.add(Record(id=model_access._operator_key_id(model_access._day(), "llm"),
                      kind=model_access.OPERATOR_KEY_KIND,
                      data={"day": model_access._day(), "kind": "llm",
                            "blocked": {"at": 0, "status": 401}}))
    with model_access.access_scope(created["access_id"]):
        with pytest.raises(model_access.ModelAccessError, match="自己的 API Key"):
            model_access.authorize_call("llm", task_id="t")
