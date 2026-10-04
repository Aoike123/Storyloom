"""S1 导入不可变原文（附录 01 §2）—— POST /api/studio/projects/{pid}/sources 行为验证。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件不自行改写 STUDIO_* env。db fixture 与 register/auth helper 照抄
tests/studio/test_projects.py 的实现；测试参数 (db, client) 序。
"""
import hashlib
import time
import uuid

import pytest
from sqlalchemy import select, text

from backend.core.db import Base, Session, engine
from backend.studio.sources.models import SourceRevision


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


def _create_project(client, token, name) -> str:
    """P1 建项目，返回项目 id。"""
    r = client.post(
        "/api/studio/projects",
        json={"name": name, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _import(client, token, pid, content, command_id=None):
    return client.post(
        f"/api/studio/projects/{pid}/sources",
        json={"content": content, "command_id": command_id or _cmd()},
        headers=_auth(token),
    )


def _revision_count(pid) -> int:
    with Session() as s:
        return len(
            s.execute(
                select(SourceRevision).where(SourceRevision.project_id == pid)
            )
            .scalars()
            .all()
        )


# T1 无 token → 401
def test_t1_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/01010101010101010101010101/sources",
        json={"content": "hello", "command_id": _cmd()},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


# T2 pid 不存在（26 字符 ULID）→ 404 not_found，details={kind:"project", id:pid}
def test_t2_nonexistent_pid_404(db, client):
    token = _register(client, "s1_t2_alice")
    pid = "01" * 13
    r = _import(client, token, pid, "hello")
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "project", "id": pid}


# T3 跨属主项目 → 404（同形状，不泄漏存在性）
def test_t3_cross_owner_404(db, client):
    t1 = _register(client, "s1_t3_bob")
    t2 = _register(client, "s1_t3_carol")
    pid = _create_project(client, t1, "T3Proj")
    r = _import(client, t2, pid, "hello")
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["details"] == {"kind": "project", "id": pid}


# T4 首版导入：raw 原样 / canonical 仅 CRLF→LF / 哈希与长度独立计算 / 自动激活
def test_t4_first_import_201(db, client):
    token = _register(client, "s1_t4_dave")
    pid = _create_project(client, token, "T4Proj")
    content = "  hello\r\nworld  "
    r = _import(client, token, pid, content)
    assert r.status_code == 201, r.text
    body = r.json()
    for k in (
        "id",
        "project_id",
        "previous_revision_id",
        "raw_content",
        "raw_hash",
        "canonical_content",
        "canonical_hash",
        "offset_policy",
        "char_length",
        "created_at",
        "is_active",
        "activation_hint",
        "duplicate_content",
    ):
        assert k in body, f"缺字段 {k}"
    assert body["project_id"] == pid
    # raw 逐字节原样：含 CRLF 与首尾空格
    assert body["raw_content"] == "  hello\r\nworld  "
    # canonical 仅 CRLF→LF；首尾空白保留
    assert body["canonical_content"] == "  hello\nworld  "
    # 哈希 = sha256(UTF-8)——测试内独立计算
    assert body["raw_hash"] == hashlib.sha256(
        "  hello\r\nworld  ".encode("utf-8")
    ).hexdigest()
    assert body["canonical_hash"] == hashlib.sha256(
        "  hello\nworld  ".encode("utf-8")
    ).hexdigest()
    # char_length = canonical 的 UTF-16 code unit 数（2+5+1+5+2）
    assert body["char_length"] == 15
    assert body["offset_policy"] == "lf-utf16-v1"
    assert body["previous_revision_id"] is None
    # 首个导入自动激活：is_active true、hint null
    assert body["is_active"] is True
    assert body["activation_hint"] is None
    assert body["duplicate_content"] is False
    assert isinstance(body["created_at"], (int, float))
    # GET 项目 → active 指针 == 新 id
    r2 = client.get(f"/api/studio/projects/{pid}", headers=_auth(token))
    assert r2.status_code == 200, r2.text
    assert r2.json()["active_source_revision_id"] == body["id"]


# T5 第二版：不激活；previous 链接第一版；activation_hint 指向 S4/S5
def test_t5_second_version_not_activated(db, client):
    token = _register(client, "s1_t5_erin")
    pid = _create_project(client, token, "T5Proj")
    r1 = _import(client, token, pid, "  hello\r\nworld  ")
    assert r1.status_code == 201, r1.text
    first = r1.json()
    time.sleep(0.002)  # 保证 ULID 严格递增
    r2 = _import(client, token, pid, "second version content")
    assert r2.status_code == 201, r2.text
    second = r2.json()
    # 已有活跃版本 → 新版本不激活
    assert second["is_active"] is False
    assert second["previous_revision_id"] == first["id"]
    assert second["activation_hint"] == {
        "kind": "source_activate",
        "target_revision_id": second["id"],
        "message": "已有活跃版本。切换到该版本需预览确认（S4/S5）。",
    }
    assert second["duplicate_content"] is False
    # GET 项目 → active 仍为第一版
    r3 = client.get(f"/api/studio/projects/{pid}", headers=_auth(token))
    assert r3.status_code == 200, r3.text
    assert r3.json()["active_source_revision_id"] == first["id"]


# T6 重复导入：canonical_hash 相同 → duplicate_content true；仍建新版本
def test_t6_duplicate_import_still_new_version(db, client):
    token = _register(client, "s1_t6_frank")
    pid = _create_project(client, token, "T6Proj")
    # v1：CRLF 原文
    r1 = _import(client, token, pid, "  hello\r\nworld  ")
    assert r1.status_code == 201, r1.text
    v1 = r1.json()
    assert v1["duplicate_content"] is False
    time.sleep(0.002)
    # v2：普通新内容
    r2 = _import(client, token, pid, "second version content")
    assert r2.status_code == 201, r2.text
    v2 = r2.json()
    time.sleep(0.002)
    # v3：T4 原文的纯 LF 变体 → canonical_hash 同 v1 → duplicate true
    r3 = _import(client, token, pid, "  hello\nworld  ")
    assert r3.status_code == 201, r3.text
    v3 = r3.json()
    assert v3["duplicate_content"] is True
    assert v3["canonical_hash"] == v1["canonical_hash"]
    assert v3["raw_content"] == "  hello\nworld  "  # raw 仍原样
    assert v3["previous_revision_id"] == v2["id"]
    assert v3["is_active"] is False
    # 重复导入仍建新版本：项目修订数 == 3（历史依据，R03）
    assert _revision_count(pid) == 3
    # active 指针仍为 v1
    r4 = client.get(f"/api/studio/projects/{pid}", headers=_auth(token))
    assert r4.json()["active_source_revision_id"] == v1["id"]


# T7 content 空 → 422 validation_failed [{field:"content", rule:"required"}]
def test_t7_empty_content_422(db, client):
    token = _register(client, "s1_t7_grace")
    pid = _create_project(client, token, "T7Proj")
    r = _import(client, token, pid, "")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "content"
    assert v0["rule"] == "required"
    assert v0["message"] == "原文不能为空。"


# T8 同 command_id 同 body 重放 → 200（非 201），id 相同，修订行数不变
def test_t8_replay_same_command_200(db, client):
    token = _register(client, "s1_t8_heidi")
    pid = _create_project(client, token, "T8Proj")
    payload = {"content": "  hello\r\nworld  ", "command_id": _cmd()}
    r1 = client.post(
        f"/api/studio/projects/{pid}/sources", json=payload, headers=_auth(token)
    )
    assert r1.status_code == 201, r1.text
    first = r1.json()
    before = _revision_count(pid)
    r2 = client.post(
        f"/api/studio/projects/{pid}/sources", json=payload, headers=_auth(token)
    )
    assert r2.status_code == 200, (r2.status_code, r2.text)  # 重放 = 200 非 201
    second = r2.json()
    assert second["id"] == first["id"]
    assert second == first  # 原样返回首次 result_payload
    assert _revision_count(pid) == before  # 零写零 commit


# T9 同 command_id 不同 content → 422 rule=reused
def test_t9_command_id_reused_422(db, client):
    token = _register(client, "s1_t9_ivan")
    pid = _create_project(client, token, "T9Proj")
    cmd = _cmd()
    r1 = client.post(
        f"/api/studio/projects/{pid}/sources",
        json={"content": "first content", "command_id": cmd},
        headers=_auth(token),
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        f"/api/studio/projects/{pid}/sources",
        json={"content": "different content", "command_id": cmd},
        headers=_auth(token),
    )
    assert r2.status_code == 422, r2.text
    err = r2.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "command_id"
    assert v0["rule"] == "reused"


# T10 不 trim 回归：首尾空格在 raw 与 canonical 中均保留
def test_t10_no_trim_regression(db, client):
    token = _register(client, "s1_t10_judy")
    pid = _create_project(client, token, "T10Proj")
    r = _import(client, token, pid, " x ")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["raw_content"] == " x "
    assert body["canonical_content"] == " x "
    assert body["raw_hash"] == body["canonical_hash"] == hashlib.sha256(
        b" x "
    ).hexdigest()
    assert body["char_length"] == 3
