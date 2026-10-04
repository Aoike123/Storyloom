"""P1 新建项目（附录 01 §2）—— POST /api/studio/projects 行为验证。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件不自行改写 STUDIO_* env。db fixture 照抄 test_source_models.py。
"""
import re
import time
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.core.db import Base, Session, engine
from backend.studio.contracts.models import CommandRecord
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


# ── P3 读取单项目（GET /projects/{pid}）──────────────────────────────────────

def test_t11_no_token_get_401(db, client):
    r = client.get("/api/studio/projects/01010101010101010101010101")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t12_owner_reads_own_project(db, client):
    token = _register(client, "t12_karen")
    r1 = client.post(
        "/api/studio/projects",
        json={"name": "P3Proj", "description": "hello", "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r1.status_code == 201, r1.text
    created = r1.json()
    r2 = client.get(
        f"/api/studio/projects/{created['id']}", headers=_auth(token)
    )
    assert r2.status_code == 200, r2.text
    fetched = r2.json()
    for k in (
        "id",
        "name",
        "description",
        "visibility",
        "active_source_revision_id",
        "created_at",
        "updated_at",
    ):
        assert fetched[k] == created[k], f"字段 {k} 不一致: {fetched[k]!r} != {created[k]!r}"


def test_t13_nonexistent_pid_404(db, client):
    token = _register(client, "t13_laura")
    pid = "01" * 13
    r = client.get(f"/api/studio/projects/{pid}", headers=_auth(token))
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "project", "id": pid}


def test_t14_cross_owner_404(db, client):
    t1 = _register(client, "t14_mallory")
    t2 = _register(client, "t14_nancy")
    r1 = client.post(
        "/api/studio/projects",
        json={"name": "CrossOwner", "command_id": _cmd()},
        headers=_auth(t1),
    )
    assert r1.status_code == 201, r1.text
    pid = r1.json()["id"]
    r2 = client.get(f"/api/studio/projects/{pid}", headers=_auth(t2))
    assert r2.status_code == 404, r2.text
    err = r2.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "project", "id": pid}


def test_t15_no_side_effects(db, client):
    token = _register(client, "t15_olga")
    # 先建一个项目，使 projects 表有数据
    r1 = client.post(
        "/api/studio/projects",
        json={"name": "NoSide", "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r1.status_code == 201, r1.text

    # 基线计数
    proj_before = _project_count()
    with Session() as s:
        cmd_before = s.scalar(select(func.count(CommandRecord.id)))

    # 对不存在 pid 发 3 次 GET
    pid = "01" * 13
    for _ in range(3):
        r = client.get(f"/api/studio/projects/{pid}", headers=_auth(token))
        assert r.status_code == 404

    # 读命令无副作用：无新 command record，项目数不变
    proj_after = _project_count()
    with Session() as s:
        cmd_after = s.scalar(select(func.count(CommandRecord.id)))

    assert cmd_after == cmd_before, f"command_records 新增 {cmd_after - cmd_before} 行"
    assert proj_after == proj_before, f"projects 从 {proj_before} 变 {proj_after}"


# ── P2 项目列表（GET /projects?cursor=&limit=）──────────────────────────────

def _mk_projects(client, token, count, prefix):
    """建 count 个项目（间隔 ≥1ms 保证 ULID 严格递增），返回创建顺序的 id 列表。"""
    ids = []
    for i in range(count):
        r = client.post(
            "/api/studio/projects",
            json={"name": f"{prefix}{i}", "command_id": _cmd()},
            headers=_auth(token),
        )
        assert r.status_code == 201, (i, r.text)
        ids.append(r.json()["id"])
        time.sleep(0.002)
    return ids


def test_t16_no_token_list_401(db, client):
    r = client.get("/api/studio/projects")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t17_empty_account_empty_list(db, client):
    token = _register(client, "t17_paul")
    r = client.get("/api/studio/projects", headers=_auth(token))
    assert r.status_code == 200, r.text  # R12：空列表 ≠ 读失败
    assert r.json() == {"items": [], "next_cursor": None}


def test_t18_list_three_order_desc(db, client):
    token = _register(client, "t18_quinn")
    ids = _mk_projects(client, token, 3, "L8")
    assert len(set(ids)) == 3, "ULID 未互异"
    r = client.get("/api/studio/projects", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_cursor"] is None
    # 顺序 = 创建逆序（id DESC，新→旧）
    assert [it["id"] for it in body["items"]] == list(reversed(ids))
    # DTO 逐字段正确（7 字段与 P1/P3 相同）
    for k in (
        "id",
        "name",
        "description",
        "visibility",
        "active_source_revision_id",
        "created_at",
        "updated_at",
    ):
        for it in body["items"]:
            assert k in it, f"缺字段 {k}"
    assert [it["name"] for it in body["items"]] == [f"L8{i}" for i in (2, 1, 0)]
    for it in body["items"]:
        assert it["visibility"] == "private"
        assert it["active_source_revision_id"] is None
        assert isinstance(it["created_at"], (int, float))
        assert isinstance(it["updated_at"], (int, float))


def test_t19_pagination_keyset(db, client):
    token = _register(client, "t19_randy")
    ids = _mk_projects(client, token, 3, "P19")  # 创建序 id 递增
    # 第一页：limit=2 → 最新 2 条，next_cursor = 第 2 条的 id
    r1 = client.get("/api/studio/projects?limit=2", headers=_auth(token))
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert [it["id"] for it in b1["items"]] == [ids[2], ids[1]]
    assert b1["next_cursor"] == ids[1]
    # 第二页：cursor = ids[1] → 仅最旧那条，next_cursor = null
    r2 = client.get(
        f"/api/studio/projects?cursor={ids[1]}&limit=2", headers=_auth(token)
    )
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert [it["id"] for it in b2["items"]] == [ids[0]]
    assert b2["next_cursor"] is None


def test_t20_owner_isolation_no_leak(db, client):
    ta = _register(client, "t20_susan")
    tb = _register(client, "t20_trent")
    ids_a = _mk_projects(client, ta, 2, "A20")
    ids_b = _mk_projects(client, tb, 2, "B20")
    r = client.get("/api/studio/projects", headers=_auth(ta))
    assert r.status_code == 200, r.text
    got = [it["id"] for it in r.json()["items"]]
    assert got == list(reversed(ids_a))
    assert not (set(got) & set(ids_b)), "跨账号项目泄漏"


def test_t21_cursor_format_and_limit_clamp(db, client):
    token = _register(client, "t21_utter")
    # cursor 非 26 字符 Crockford 小写 → 422 validation_failed
    r = client.get("/api/studio/projects?cursor=xyz", headers=_auth(token))
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "cursor" and v0["rule"] == "format"
    assert v0["message"] == "cursor 必须是 26 字符 ULID。"
    # 空串 cursor 同样 422
    r0 = client.get("/api/studio/projects?cursor=", headers=_auth(token))
    assert r0.status_code == 422, r0.text
    # limit 越界 → clamp 到 [1, 200]，不报 422
    for bad_limit in (0, 500):
        rl = client.get(
            f"/api/studio/projects?limit={bad_limit}", headers=_auth(token)
        )
        assert rl.status_code == 200, (bad_limit, rl.text)
        assert len(rl.json()["items"]) <= 200
