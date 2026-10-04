"""P1 新建项目（附录 01 §2）—— POST /api/studio/projects 行为验证。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件不自行改写 STUDIO_* env。db fixture 照抄 test_source_models.py。
"""
import re
import uuid

import pytest
from sqlalchemy import select, text

from backend.core.db import Base, Session, engine
from backend.studio.projects.models import StudioProject


@pytest.fixture
def db():
    conn = engine.connect()
    try:
        # FK 环（projects↔source_revisions）：drop 前临时关 FK
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        Base.metadata.drop_all(conn, checkfirst=True)
        conn.execute(text("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(conn)
        conn.commit()
    finally:
        conn.close()
    yield
    engine.dispose()


def _register(client, username) -> str:
    r = client.post(
        "/api/studio/auth/register",
        json={"username": username, "password": "secret123", "email": "t@x"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _cmd() -> str:
    return str(uuid.uuid4())


def _project_count() -> int:
    with Session() as s:
        return len(s.execute(select(StudioProject)).scalars().all())


def test_t1_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects", json={"name": "X", "command_id": _cmd()}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t2_create_201(db, client):
    token = _register(client, "t2_alice")
    r = client.post(
        "/api/studio/projects",
        json={"name": "  My Project  ", "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    for k in (
        "id",
        "name",
        "description",
        "visibility",
        "active_source_revision_id",
        "created_at",
        "updated_at",
    ):
        assert k in body, f"缺字段 {k}"
    assert re.fullmatch(r"[0-9a-z]{26}", body["id"]) and not (
        set(body["id"]) & set("ilou")
    ), f"id 非 26 字符 Crockford 小写: {body['id']}"
    assert body["name"] == "My Project"  # trimmed
    assert body["description"] == ""
    assert body["visibility"] == "private"
    assert body["active_source_revision_id"] is None
    assert isinstance(body["created_at"], (int, float))
    assert isinstance(body["updated_at"], (int, float))


def test_t3_name_required(db, client):
    token = _register(client, "t3_bob")
    for bad in ("", "   "):
        r = client.post(
            "/api/studio/projects",
            json={"name": bad, "command_id": _cmd()},
            headers=_auth(token),
        )
        assert r.status_code == 422, (bad, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v0 = err["details"]["violations"][0]
        assert v0["field"] == "name" and v0["rule"] == "required"


def test_t4_name_max_length(db, client):
    token = _register(client, "t4_carol")
    r = client.post(
        "/api/studio/projects",
        json={"name": "a" * 81, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "name" and v0["rule"] == "max_length"
    r2 = client.post(
        "/api/studio/projects",
        json={"name": "b" * 80, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r2.status_code == 201, r2.text


def test_t5_owner_duplicate_name_409(db, client):
    rreg = client.post(
        "/api/studio/auth/register",
        json={"username": "t5_dave", "password": "secret123", "email": "t@x"},
    )
    token = rreg.json()["token"]
    owner_id = rreg.json()["user"]["id"]
    r1 = client.post(
        "/api/studio/projects",
        json={"name": "Dup", "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/api/studio/projects",
        json={"name": "Dup", "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r2.status_code == 409, r2.text
    err = r2.json()["error"]
    assert err["code"] == "duplicate"
    assert err["details"]["scope_ref"] == "projects:" + owner_id + ":name:" + "Dup"


def test_t6_two_owners_same_name(db, client):
    t1 = _register(client, "t6_erin")
    t2 = _register(client, "t6_frank")
    r1 = client.post(
        "/api/studio/projects",
        json={"name": "Same", "command_id": _cmd()},
        headers=_auth(t1),
    )
    r2 = client.post(
        "/api/studio/projects",
        json={"name": "Same", "command_id": _cmd()},
        headers=_auth(t2),
    )
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text


def test_t7_replay_same_command_id_200(db, client):
    token = _register(client, "t7_grace")
    payload = {"name": "Replay", "description": "d1", "command_id": _cmd()}
    r1 = client.post(
        "/api/studio/projects", json=payload, headers=_auth(token)
    )
    assert r1.status_code == 201, r1.text
    first = r1.json()
    r2 = client.post(
        "/api/studio/projects", json=payload, headers=_auth(token)
    )
    assert r2.status_code == 200, (r2.status_code, r2.text)  # 重放 = 200 非 201
    second = r2.json()
    assert second["id"] == first["id"]
    assert second == first  # 原样返回首次 result_payload
    assert _project_count() == 1  # 无副作用：仅 1 行


def test_t8_command_id_reused_422(db, client):
    token = _register(client, "t8_heidi")
    cmd = _cmd()
    r1 = client.post(
        "/api/studio/projects",
        json={"name": "First", "command_id": cmd},
        headers=_auth(token),
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/api/studio/projects",
        json={"name": "Different", "command_id": cmd},
        headers=_auth(token),
    )
    assert r2.status_code == 422, r2.text
    err = r2.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "command_id" and v0["rule"] == "reused"


def test_t9_description_kept_raw(db, client):
    token = _register(client, "t9_ivan")
    r = client.post(
        "/api/studio/projects",
        json={"name": "Desc", "description": "  keep  ", "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["description"] == "  keep  "


def test_t10_missing_command_id_422(db, client):
    token = _register(client, "t10_judy")
    r = client.post(
        "/api/studio/projects", json={"name": "NoCmd"}, headers=_auth(token)
    )
    assert r.status_code == 422
