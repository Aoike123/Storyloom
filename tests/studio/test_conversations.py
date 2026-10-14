"""CHAT-01 · 会话和消息 API（附录 10 §1–§3）行为验证。

覆盖五路由：GET/POST /projects/{pid}/conversations、
GET /projects/{pid}/conversations/{cid}、
GET/POST /projects/{pid}/conversations/{cid}/messages。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定；db fixture 照抄
test_projects.py。属主隔离、冻结错误形状、幂等重放、禁止项（不写业务表 /
不建提案 / 不调模型不作网络调用）均有负向断言。
"""
import inspect
import json
import re
import time
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.core.db import Base, Session, engine, make_ulid
from backend.studio.contracts.models import CommandRecord
from backend.studio.conversations.models import Conversation, Message, Proposal
from backend.studio.edits.models import EditDraft
from backend.studio.jobs.models import StudioJob
from backend.studio.projects.models import StudioProject
from backend.studio.sources.fragment_models import (
    Fragment,
    FragmentRevision,
    RangeSet,
)
from backend.studio.sources.models import SourceRevision
from backend.studio.storyboard.models import Scene

CONVERSATION_KEYS = {
    "object_ref",
    "scope_kind",
    "scope_id",
    "title",
    "created_at",
    "updated_at",
}
MESSAGE_KEYS = {
    "object_ref",
    "conversation_id",
    "sender",
    "content",
    "refs",
    "job_id",
    "created_at",
}
# 「禁止写业务表」的负向断言对象（本卡一律只读）
BUSINESS_TABLES = (
    "studio_fragments",
    "studio_fragment_revisions",
    "studio_range_sets",
    "studio_scenes",
    "studio_shots",
    "studio_assets",
    "studio_edit_drafts",
    "studio_jobs",
    "studio_proposals",
)


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


# ────────────────────────────── helpers ──────────────────────────────


def _register_user(client, username) -> dict:
    r = client.post(
        "/api/studio/auth/register",
        json={"username": username, "password": "secret123", "email": "t@x"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _register(client, username) -> str:
    return _register_user(client, username)["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _cmd() -> str:
    return str(uuid.uuid4())


def _mk_project(client, token, name="P") -> str:
    r = client.post(
        "/api/studio/projects",
        json={"name": name, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mk_scene(pid, seq=1, name=None) -> str:
    """直接落一行 studio_scenes（仅测试夹具：服务层只读该表）。"""
    scene_id = make_ulid()
    with Session() as s:
        s.add(
            Scene(
                id=scene_id,
                project_id=pid,
                name=name or f"scene-{seq}",
                seq=seq,
            )
        )
        s.commit()
    return scene_id


def _mk_fragment(pid) -> str:
    """直接落 source_revision + range_set + fragment + fragment_revision。

    仅测试夹具：验证 scope_kind=fragment 的存在性查表（服务层只读）。
    """
    now = time.time()
    rev_id = make_ulid()
    rset_id = make_ulid()
    frag_id = make_ulid()
    frev_id = make_ulid()
    with Session() as s:
        s.add(
            SourceRevision(
                id=rev_id,
                project_id=pid,
                name="src",
                raw_content="abcdef",
                raw_hash="0" * 64,
                canonical_content="abcdef",
                canonical_hash="0" * 64,
                offset_policy="lf-utf16-v1",
                char_length=6,
                created=now,
            )
        )
        s.flush()
        s.add(
            RangeSet(
                id=rset_id,
                project_id=pid,
                source_revision_id=rev_id,
                cas_revision=1,
                created_at=now,
            )
        )
        s.flush()
        s.add(
            Fragment(
                id=frag_id,
                project_id=pid,
                source_revision_id=rev_id,
                range_set_id=rset_id,
                name="frag",
                summary=None,
                state="candidate",
                current_revision_id=frev_id,
                created_at=now,
                updated_at=now,
                retired_at=None,
            )
        )
        s.flush()
        s.add(
            FragmentRevision(
                id=frev_id,
                fragment_id=frag_id,
                revision=1,
                source_revision_id=rev_id,
                range_start=0,
                range_end=6,
                reason="created",
                predecessor_fragment_ids="[]",
                created_at=now,
            )
        )
        s.commit()
    return frag_id


def _mk_edit_draft(pid) -> str:
    """直接落一行 studio_edit_drafts（仅测试夹具：服务层只读该表）。"""
    edit_id = make_ulid()
    with Session() as s:
        s.add(
            EditDraft(
                id=make_ulid(),
                project_id=pid,
                edit_id=edit_id,
                edit_revision=1,
                manifest="{}",
                manifest_digest="0" * 64,
                cas_revision=1,
                updated_at=time.time(),
            )
        )
        s.commit()
    return edit_id


def _mk_job(pid, owner_id) -> str:
    """直接落一行 studio_jobs（仅测试夹具：服务层只读该表）。"""
    job_id = make_ulid()
    with Session() as s:
        s.add(
            StudioJob(
                id=job_id,
                project_id=pid,
                owner_id=owner_id,
                kind="transcode",
                payload="{}",
                status="pending",
            )
        )
        s.commit()
    return job_id


def _mk_conversation_row(pid, owner_id, scope_kind="project", scope_id=None) -> str:
    """直接落一行 studio_conversations（构造「他人会话」/「跨项目会话」）。"""
    cid = make_ulid()
    now = time.time()
    with Session() as s:
        s.add(
            Conversation(
                id=cid,
                project_id=pid,
                owner_id=owner_id,
                scope_kind=scope_kind,
                scope_id=scope_id or pid,
                title="",
                created_at=now,
                updated_at=now,
            )
        )
        s.commit()
    return cid


def _count(table: str) -> int:
    with Session() as s:
        return s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def _counts() -> dict:
    return {t: _count(t) for t in BUSINESS_TABLES}


def _open(client, token, pid, scope_kind, scope_id, cmd=None):
    return client.post(
        f"/api/studio/projects/{pid}/conversations",
        json={
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "command_id": cmd or _cmd(),
        },
        headers=_auth(token),
    )


def _open_ok(client, token, pid, scope_kind, scope_id, cmd=None) -> str:
    r = _open(client, token, pid, scope_kind, scope_id, cmd)
    assert r.status_code == 201, r.text
    return r.json()["object_ref"]["id"]


def _post_msg(client, token, pid, cid, content, sender="user", refs=None,
              job_id=None, cmd=None):
    payload = {
        "sender": sender,
        "content": content,
        "command_id": cmd or _cmd(),
    }
    if refs is not None:
        payload["refs"] = refs
    if job_id is not None:
        payload["job_id"] = job_id
    return client.post(
        f"/api/studio/projects/{pid}/conversations/{cid}/messages",
        json=payload,
        headers=_auth(token),
    )


def _assert_violation(body, field, rule):
    err = body["error"]
    assert err["code"] == "validation_failed", body
    v0 = err["details"]["violations"][0]
    assert v0["field"] == field and v0["rule"] == rule, body


def _assert_not_found(body, kind, ident):
    err = body["error"]
    assert err["code"] == "not_found", body
    assert err["details"] == {"kind": kind, "id": ident}, body


# ═══════════════ 路由 1：GET /projects/{pid}/conversations ═══════════════


def test_t01_list_no_token_401(db, client):
    r = client.get("/api/studio/projects/01010101010101010101010101/conversations")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t02_list_empty_account_empty_list(db, client):
    token = _register(client, "t02_alice")
    pid = _mk_project(client, token, "Empty")
    r = client.get(f"/api/studio/projects/{pid}/conversations", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"items": [], "next_cursor": None}


def test_t03_list_project_404_shapes(db, client):
    token = _register(client, "t03_bob")
    other = _register(client, "t03_bob2")
    # 不存在项目
    pid = "01" * 13
    r = client.get(f"/api/studio/projects/{pid}/conversations", headers=_auth(token))
    assert r.status_code == 404, r.text
    _assert_not_found(r.json(), "project", pid)
    # 他人项目
    pid2 = _mk_project(client, other, "Other")
    r2 = client.get(f"/api/studio/projects/{pid2}/conversations", headers=_auth(token))
    assert r2.status_code == 404, r2.text
    _assert_not_found(r2.json(), "project", pid2)


def test_t04_list_ascending_dto_shape(db, client):
    token = _register(client, "t04_carol")
    pid = _mk_project(client, token, "L4")
    ids = []
    scenes = []
    for i in range(3):
        sid = _mk_scene(pid, seq=i + 1)
        scenes.append(sid)
        ids.append(_open_ok(client, token, pid, "scene", sid))
        time.sleep(0.002)  # ULID 严格递增（毫秒时间戳）
    r = client.get(f"/api/studio/projects/{pid}/conversations", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_cursor"] is None
    assert [it["object_ref"]["id"] for it in body["items"]] == ids  # id 升序
    for it, sid in zip(body["items"], scenes):
        assert set(it.keys()) == CONVERSATION_KEYS
        assert it["object_ref"] == {
            "kind": "conversation",
            "id": it["object_ref"]["id"],
            "revision": None,
        }
        assert re.fullmatch(r"[0-9a-z]{26}", it["object_ref"]["id"])
        assert it["scope_kind"] == "scene" and it["scope_id"] == sid
        assert it["title"] == ""
        assert isinstance(it["created_at"], (int, float))
        assert isinstance(it["updated_at"], (int, float))


def test_t05_list_optional_filters(db, client):
    token = _register(client, "t05_dave")
    pid = _mk_project(client, token, "L5")
    s1 = _mk_scene(pid, seq=1)
    s2 = _mk_scene(pid, seq=2)
    c1 = _open_ok(client, token, pid, "scene", s1)
    c2 = _open_ok(client, token, pid, "scene", s2)
    base = f"/api/studio/projects/{pid}/conversations"
    r = client.get(f"{base}?scope_kind=scene&scope_id={s1}", headers=_auth(token))
    assert [it["object_ref"]["id"] for it in r.json()["items"]] == [c1]
    r2 = client.get(f"{base}?scope_kind=scene", headers=_auth(token))
    assert [it["object_ref"]["id"] for it in r2.json()["items"]] == [c1, c2]
    r3 = client.get(f"{base}?scope_kind=fragment", headers=_auth(token))
    assert r3.json() == {"items": [], "next_cursor": None}
    r4 = client.get(f"{base}?scope_id={s2}", headers=_auth(token))
    assert [it["object_ref"]["id"] for it in r4.json()["items"]] == [c2]


def test_t06_list_pagination_two_pages(db, client):
    token = _register(client, "t06_erin")
    pid = _mk_project(client, token, "L6")
    ids = []
    for i in range(3):
        sid = _mk_scene(pid, seq=i + 1)
        ids.append(_open_ok(client, token, pid, "scene", sid))
        time.sleep(0.002)
    base = f"/api/studio/projects/{pid}/conversations"
    b1 = client.get(f"{base}?limit=2", headers=_auth(token)).json()
    assert [it["object_ref"]["id"] for it in b1["items"]] == ids[:2]
    assert b1["next_cursor"] == ids[1]
    b2 = client.get(
        f"{base}?cursor={b1['next_cursor']}&limit=2", headers=_auth(token)
    ).json()
    assert [it["object_ref"]["id"] for it in b2["items"]] == ids[2:]
    assert b2["next_cursor"] is None


def test_t07_list_cursor_format_and_limit_clamp(db, client):
    token = _register(client, "t07_frank")
    pid = _mk_project(client, token, "L7")
    base = f"/api/studio/projects/{pid}/conversations"
    for bad in ("xyz", "", "0" * 25, "0" * 27, "ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        r = client.get(f"{base}?cursor={bad}", headers=_auth(token))
        assert r.status_code == 422, (bad, r.text)
        _assert_violation(r.json(), "cursor", "format")
    for bad_limit in (0, 500):
        rl = client.get(f"{base}?limit={bad_limit}", headers=_auth(token))
        assert rl.status_code == 200, (bad_limit, rl.text)
        assert len(rl.json()["items"]) <= 200


def test_t08_list_owner_isolation_no_leak(db, client):
    ta = _register(client, "t08_grace")
    tb_user = _register_user(client, "t08_heidi")
    pid_a = _mk_project(client, ta, "A8")
    pid_b = _mk_project(client, tb_user["token"], "B8")
    ca = _open_ok(client, ta, pid_a, "project", pid_a)
    cb = _open_ok(client, tb_user["token"], pid_b, "project", pid_b)
    # 他人属主但在 A 项目内的会话行（直接构造）：A 不可见
    foreign = _mk_conversation_row(pid_a, tb_user["user"]["id"])
    r = client.get(f"/api/studio/projects/{pid_a}/conversations", headers=_auth(ta))
    got = [it["object_ref"]["id"] for it in r.json()["items"]]
    assert got == [ca]
    assert cb not in got and foreign not in got
    # B 看自己的项目只看到自己的会话
    r2 = client.get(
        f"/api/studio/projects/{pid_b}/conversations",
        headers=_auth(tb_user["token"]),
    )
    assert [it["object_ref"]["id"] for it in r2.json()["items"]] == [cb]


def test_t09_list_no_side_effects(db, client):
    token = _register(client, "t09_ivan")
    pid = _mk_project(client, token, "L9")
    _open_ok(client, token, pid, "project", pid)
    with Session() as s:
        before = (
            s.scalar(select(func.count(Conversation.id))),
            s.scalar(select(func.count(Message.id))),
            s.scalar(select(func.count(CommandRecord.id))),
        )
    for _ in range(3):
        assert (
            client.get(
                f"/api/studio/projects/{pid}/conversations", headers=_auth(token)
            ).status_code
            == 200
        )
    with Session() as s:
        after = (
            s.scalar(select(func.count(Conversation.id))),
            s.scalar(select(func.count(Message.id))),
            s.scalar(select(func.count(CommandRecord.id))),
        )
    assert after == before, f"读命令产生副作用：{before} → {after}"


# ═══════════════ 路由 2：POST /projects/{pid}/conversations ═══════════════


def test_t10_open_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/01010101010101010101010101/conversations",
        json={"scope_kind": "project", "scope_id": "x", "command_id": _cmd()},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t11_open_create_201_dto_shape(db, client):
    token = _register(client, "t11_judy")
    pid = _mk_project(client, token, "O11")
    r = _open(client, token, pid, "project", pid)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == CONVERSATION_KEYS
    assert re.fullmatch(r"[0-9a-z]{26}", body["object_ref"]["id"])
    assert body["object_ref"] == {
        "kind": "conversation",
        "id": body["object_ref"]["id"],
        "revision": None,
    }
    assert body["scope_kind"] == "project" and body["scope_id"] == pid
    assert body["title"] == ""
    assert isinstance(body["created_at"], (int, float))
    assert isinstance(body["updated_at"], (int, float))


def test_t12_open_take_or_create_second_call_200_same_id(db, client):
    token = _register(client, "t12_karen")
    pid = _mk_project(client, token, "O12")
    cid = _open_ok(client, token, pid, "project", pid)
    conv_before = _count("studio_conversations")
    cmd_before = _count("studio_command_records")
    r2 = _open(client, token, pid, "project", pid)  # 新 command_id、同参数
    assert r2.status_code == 200, r2.text
    assert r2.json()["object_ref"]["id"] == cid
    assert _count("studio_conversations") == conv_before, "取或建不应新建行"
    assert _count("studio_command_records") == cmd_before, "已存在路径不应记录命令"


def test_t13_open_replay_same_command_id_200_identical(db, client):
    token = _register(client, "t13_laura")
    pid = _mk_project(client, token, "O13")
    cmd = _cmd()
    r1 = _open(client, token, pid, "project", pid, cmd=cmd)
    assert r1.status_code == 201, r1.text
    conv_before = _count("studio_conversations")
    cmd_before = _count("studio_command_records")
    r2 = _open(client, token, pid, "project", pid, cmd=cmd)
    assert r2.status_code == 200, (r2.status_code, r2.text)
    assert r2.json() == r1.json()
    assert _count("studio_conversations") == conv_before
    assert _count("studio_command_records") == cmd_before


def test_t14_open_command_id_reused_422(db, client):
    token = _register(client, "t14_mallory")
    pid1 = _mk_project(client, token, "O14a")
    pid2 = _mk_project(client, token, "O14b")
    cmd = _cmd()
    assert _open(client, token, pid1, "project", pid1, cmd=cmd).status_code == 201
    r2 = _open(client, token, pid2, "project", pid2, cmd=cmd)
    assert r2.status_code == 422, r2.text
    _assert_violation(r2.json(), "command_id", "reused")
    # 同项目内换作用域同样 reused
    s1 = _mk_scene(pid1, seq=1)
    s2 = _mk_scene(pid1, seq=2)
    cmd2 = _cmd()
    assert _open(client, token, pid1, "scene", s1, cmd=cmd2).status_code == 201
    r3 = _open(client, token, pid1, "scene", s2, cmd=cmd2)
    assert r3.status_code == 422, r3.text
    _assert_violation(r3.json(), "command_id", "reused")


def test_t15_open_scope_kind_enum_422(db, client):
    token = _register(client, "t15_nancy")
    pid = _mk_project(client, token, "O15")
    for bad in ("", "source", "scene_", "PROJECT"):
        r = _open(client, token, pid, bad, pid)
        assert r.status_code == 422, (bad, r.text)
        _assert_violation(r.json(), "scope_kind", "enum")


def test_t16_open_project_scope_mismatch_422(db, client):
    token = _register(client, "t16_olga")
    pid = _mk_project(client, token, "O16")
    other_pid = _mk_project(client, token, "O16b")
    r = _open(client, token, pid, "project", other_pid)
    assert r.status_code == 422, r.text
    _assert_violation(r.json(), "scope_id", "mismatch")
    r2 = _open(client, token, pid, "project", "0" * 26)
    assert r2.status_code == 422, r2.text
    _assert_violation(r2.json(), "scope_id", "mismatch")


def test_t17_open_missing_scope_404(db, client):
    token = _register(client, "t17_paul")
    pid = _mk_project(client, token, "O17")
    missing = "0" * 26
    for kind in ("fragment", "scene", "shot", "asset", "edit"):
        r = _open(client, token, pid, kind, missing)
        assert r.status_code == 404, (kind, r.text)
        _assert_not_found(r.json(), kind, missing)


def test_t18_open_fragment_of_other_project_404(db, client):
    ta = _register(client, "t18_quinn")
    tb = _register(client, "t18_randy")
    pid_a = _mk_project(client, ta, "A18")
    pid_b = _mk_project(client, tb, "B18")
    frag_b = _mk_fragment(pid_b)
    # A 拿 B 项目的片段 id 去自己的项目取或建 → 404（不泄漏存在性）
    r = _open(client, ta, pid_a, "fragment", frag_b)
    assert r.status_code == 404, r.text
    _assert_not_found(r.json(), "fragment", frag_b)
    # B 自己项目内合法 → 201
    r2 = _open(client, tb, pid_b, "fragment", frag_b)
    assert r2.status_code == 201, r2.text
    assert r2.json()["scope_kind"] == "fragment"


def test_t19_open_scope_kinds_happy(db, client):
    token = _register(client, "t19_susan")
    pid = _mk_project(client, token, "O19")
    frag = _mk_fragment(pid)
    scene = _mk_scene(pid, seq=1)
    edit = _mk_edit_draft(pid)
    for kind, sid in (("fragment", frag), ("scene", scene), ("edit", edit)):
        r = _open(client, token, pid, kind, sid)
        assert r.status_code == 201, (kind, r.text)
        assert r.json()["scope_kind"] == kind and r.json()["scope_id"] == sid
    # 同 scope 再次取 → 200 同一 id
    r2 = _open(client, token, pid, "fragment", frag)
    assert r2.status_code == 200, r2.text
    # 列表过滤生效
    r3 = client.get(
        f"/api/studio/projects/{pid}/conversations?scope_kind=edit",
        headers=_auth(token),
    )
    assert len(r3.json()["items"]) == 1


def test_t20_open_project_404_shapes(db, client):
    token = _register(client, "t20_trent")
    other = _register(client, "t20_uma")
    pid = "01" * 13
    r = _open(client, token, pid, "project", pid)
    assert r.status_code == 404, r.text
    _assert_not_found(r.json(), "project", pid)
    pid2 = _mk_project(client, other, "O20")
    r2 = _open(client, token, pid2, "project", pid2)
    assert r2.status_code == 404, r2.text
    _assert_not_found(r2.json(), "project", pid2)


def test_t21_open_missing_command_id_422(db, client):
    token = _register(client, "t21_utter")
    pid = _mk_project(client, token, "O21")
    r = client.post(
        f"/api/studio/projects/{pid}/conversations",
        json={"scope_kind": "project", "scope_id": pid},
        headers=_auth(token),
    )
    assert r.status_code == 422


def test_t22_open_writes_no_business_tables(db, client):
    token = _register(client, "t22_victor")
    pid = _mk_project(client, token, "O22")
    _mk_fragment(pid)
    _mk_scene(pid, seq=1)
    _mk_edit_draft(pid)
    before = _counts()
    assert _open(client, token, pid, "project", pid).status_code == 201
    assert _open(client, token, pid, "fragment", _frag_of(pid)).status_code == 201
    after = _counts()
    assert after == before, f"取或建写入了业务表：{before} → {after}"


def _frag_of(pid) -> str:
    with Session() as s:
        return s.scalar(select(Fragment.id).where(Fragment.project_id == pid))


# ═══════════════ 路由 3：GET /projects/{pid}/conversations/{cid} ═══════════════


def test_t23_detail_no_token_401(db, client):
    r = client.get(
        "/api/studio/projects/01010101010101010101010101/conversations/"
        + "0" * 26
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t24_detail_happy_with_messages_ascending(db, client):
    token = _register(client, "t24_wendy")
    pid = _mk_project(client, token, "D24")
    cid = _open_ok(client, token, pid, "project", pid)
    mids = []
    for i in range(3):
        r = _post_msg(client, token, pid, cid, f"m{i}")
        assert r.status_code == 201, r.text
        mids.append(r.json()["object_ref"]["id"])
        time.sleep(0.002)
    r = client.get(
        f"/api/studio/projects/{pid}/conversations/{cid}", headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == CONVERSATION_KEYS | {"messages"}
    assert body["object_ref"] == {"kind": "conversation", "id": cid, "revision": None}
    assert body["title"] == ""
    assert [m["object_ref"]["id"] for m in body["messages"]] == mids  # id 升序
    for m in body["messages"]:
        assert set(m.keys()) == MESSAGE_KEYS
        assert m["conversation_id"] == cid
        assert m["refs"] is None and m["job_id"] is None


def test_t25_detail_returns_last_50_only(db, client):
    token = _register(client, "t25_xena")
    pid = _mk_project(client, token, "D25")
    cid = _open_ok(client, token, pid, "project", pid)
    now = time.time()
    with Session() as s:
        for i in range(1, 61):
            # 确定性 26 字符 id：字典序 = 序号序
            s.add(
                Message(
                    id=f"{i:026d}",
                    conversation_id=cid,
                    sender="user",
                    content=f"m{i}",
                    refs=None,
                    job_id=None,
                    created_at=now + i,
                )
            )
        s.commit()
    r = client.get(
        f"/api/studio/projects/{pid}/conversations/{cid}", headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    got = [m["object_ref"]["id"] for m in r.json()["messages"]]
    assert got == [f"{i:026d}" for i in range(11, 61)], "应只返回最近 50 条且升序"
    assert got == sorted(got)


def test_t26_detail_404_shapes(db, client):
    ta = _register(client, "t26_yuri")
    tb_user = _register_user(client, "t26_zack")
    pid_a = _mk_project(client, ta, "A26")
    pid_a2 = _mk_project(client, ta, "A26b")
    pid_b = _mk_project(client, tb_user["token"], "B26")
    cid_a = _open_ok(client, ta, pid_a, "project", pid_a)
    # 不存在 cid
    missing = "0" * 26
    r = client.get(
        f"/api/studio/projects/{pid_a}/conversations/{missing}", headers=_auth(ta)
    )
    assert r.status_code == 404, r.text
    _assert_not_found(r.json(), "conversation", missing)
    # 同 owner 跨项目
    r2 = client.get(
        f"/api/studio/projects/{pid_a2}/conversations/{cid_a}", headers=_auth(ta)
    )
    assert r2.status_code == 404, r2.text
    _assert_not_found(r2.json(), "conversation", cid_a)
    # 他人属主但在 A 项目内的会话行
    foreign = _mk_conversation_row(pid_a, tb_user["user"]["id"])
    r3 = client.get(
        f"/api/studio/projects/{pid_a}/conversations/{foreign}", headers=_auth(ta)
    )
    assert r3.status_code == 404, r3.text
    _assert_not_found(r3.json(), "conversation", foreign)
    # 他人项目 → 项目 404（同形状，不泄漏）
    r4 = client.get(
        f"/api/studio/projects/{pid_b}/conversations/",
        headers=_auth(ta),
    )
    assert r4.status_code == 404, r4.text
    _assert_not_found(r4.json(), "project", pid_b)


def test_t27_detail_no_side_effects(db, client):
    token = _register(client, "t27_amy")
    pid = _mk_project(client, token, "D27")
    cid = _open_ok(client, token, pid, "project", pid)
    assert _post_msg(client, token, pid, cid, "hello").status_code == 201
    with Session() as s:
        before = (
            s.scalar(select(func.count(Conversation.id))),
            s.scalar(select(func.count(Message.id))),
            s.scalar(select(func.count(CommandRecord.id))),
        )
    for _ in range(3):
        r = client.get(
            f"/api/studio/projects/{pid}/conversations/{cid}", headers=_auth(token)
        )
        assert r.status_code == 200
    with Session() as s:
        after = (
            s.scalar(select(func.count(Conversation.id))),
            s.scalar(select(func.count(Message.id))),
            s.scalar(select(func.count(CommandRecord.id))),
        )
    assert after == before


# ═══════════ 路由 4：GET /projects/{pid}/conversations/{cid}/messages ═══════════


def test_t28_messages_no_token_401(db, client):
    r = client.get(
        "/api/studio/projects/01010101010101010101010101/conversations/"
        + "0" * 26
        + "/messages"
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t29_messages_ascending_and_pagination(db, client):
    token = _register(client, "t29_ben")
    pid = _mk_project(client, token, "M29")
    cid = _open_ok(client, token, pid, "project", pid)
    mids = []
    for i in range(3):
        r = _post_msg(client, token, pid, cid, f"m{i}")
        assert r.status_code == 201, r.text
        mids.append(r.json()["object_ref"]["id"])
        time.sleep(0.002)
    base = f"/api/studio/projects/{pid}/conversations/{cid}/messages"
    b1 = client.get(f"{base}?limit=2", headers=_auth(token))
    assert b1.status_code == 200, b1.text
    assert [m["object_ref"]["id"] for m in b1.json()["items"]] == mids[:2]
    assert b1.json()["next_cursor"] == mids[1]
    b2 = client.get(
        f"{base}?cursor={b1.json()['next_cursor']}&limit=2", headers=_auth(token)
    )
    assert [m["object_ref"]["id"] for m in b2.json()["items"]] == mids[2:]
    assert b2.json()["next_cursor"] is None


def test_t30_messages_cursor_format_and_limit_clamp(db, client):
    token = _register(client, "t30_cara")
    pid = _mk_project(client, token, "M30")
    cid = _open_ok(client, token, pid, "project", pid)
    base = f"/api/studio/projects/{pid}/conversations/{cid}/messages"
    for bad in ("xyz", "", "0" * 25, "!" * 26):
        r = client.get(f"{base}?cursor={bad}", headers=_auth(token))
        assert r.status_code == 422, (bad, r.text)
        _assert_violation(r.json(), "cursor", "format")
    for bad_limit in (0, 500):
        rl = client.get(f"{base}?limit={bad_limit}", headers=_auth(token))
        assert rl.status_code == 200, (bad_limit, rl.text)
        assert len(rl.json()["items"]) <= 200


def test_t31_messages_404_shapes(db, client):
    ta = _register(client, "t31_dan")
    tb_user = _register_user(client, "t31_eve")
    pid_a = _mk_project(client, ta, "A31")
    pid_a2 = _mk_project(client, ta, "A31b")
    pid_b = _mk_project(client, tb_user["token"], "B31")
    cid_a = _open_ok(client, ta, pid_a, "project", pid_a)
    missing = "0" * 26
    r = client.get(
        f"/api/studio/projects/{pid_a}/conversations/{missing}/messages",
        headers=_auth(ta),
    )
    assert r.status_code == 404, r.text
    _assert_not_found(r.json(), "conversation", missing)
    r2 = client.get(
        f"/api/studio/projects/{pid_a2}/conversations/{cid_a}/messages",
        headers=_auth(ta),
    )
    assert r2.status_code == 404, r2.text
    _assert_not_found(r2.json(), "conversation", cid_a)
    foreign = _mk_conversation_row(pid_a, tb_user["user"]["id"])
    r3 = client.get(
        f"/api/studio/projects/{pid_a}/conversations/{foreign}/messages",
        headers=_auth(ta),
    )
    assert r3.status_code == 404, r3.text
    _assert_not_found(r3.json(), "conversation", foreign)
    r4 = client.get(
        f"/api/studio/projects/{pid_b}/conversations/{cid_a}/messages",
        headers=_auth(ta),
    )
    assert r4.status_code == 404, r4.text
    _assert_not_found(r4.json(), "project", pid_b)


def test_t32_messages_no_side_effects(db, client):
    token = _register(client, "t32_finn")
    pid = _mk_project(client, token, "M32")
    cid = _open_ok(client, token, pid, "project", pid)
    assert _post_msg(client, token, pid, cid, "hello").status_code == 201
    with Session() as s:
        before = (
            s.scalar(select(func.count(Message.id))),
            s.scalar(select(func.count(CommandRecord.id))),
        )
    for _ in range(3):
        r = client.get(
            f"/api/studio/projects/{pid}/conversations/{cid}/messages",
            headers=_auth(token),
        )
        assert r.status_code == 200
    with Session() as s:
        after = (
            s.scalar(select(func.count(Message.id))),
            s.scalar(select(func.count(CommandRecord.id))),
        )
    assert after == before


# ═══════════ 路由 5：POST /projects/{pid}/conversations/{cid}/messages ═══════════


def test_t33_post_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/01010101010101010101010101/conversations/"
        + "0" * 26
        + "/messages",
        json={"sender": "user", "content": "x", "command_id": _cmd()},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t34_post_message_201_dto_and_updated_at(db, client):
    token = _register(client, "t34_gil")
    pid = _mk_project(client, token, "P34")
    cid = _open_ok(client, token, pid, "project", pid)
    before = client.get(
        f"/api/studio/projects/{pid}/conversations/{cid}", headers=_auth(token)
    ).json()
    r = _post_msg(client, token, pid, cid, "你好 world", sender="assistant")
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == MESSAGE_KEYS
    assert re.fullmatch(r"[0-9a-z]{26}", body["object_ref"]["id"])
    assert body["object_ref"] == {
        "kind": "message",
        "id": body["object_ref"]["id"],
        "revision": None,
    }
    assert body["conversation_id"] == cid
    assert body["sender"] == "assistant"
    assert body["content"] == "你好 world"
    assert body["refs"] is None
    assert body["job_id"] is None
    assert isinstance(body["created_at"], (int, float))
    after = client.get(
        f"/api/studio/projects/{pid}/conversations/{cid}", headers=_auth(token)
    ).json()
    assert after["updated_at"] >= before["updated_at"], "追加消息应推进会话 updated_at"
    assert after["updated_at"] == body["created_at"]
    assert [m["object_ref"]["id"] for m in after["messages"]] == [
        body["object_ref"]["id"]
    ]


def test_t35_post_refs_roundtrip(db, client):
    token = _register(client, "t35_hana")
    pid = _mk_project(client, token, "P35")
    frag = _mk_fragment(pid)
    cid = _open_ok(client, token, pid, "fragment", frag)
    refs = [
        {"kind": "fragment", "id": frag, "label": "第一段"},
        {"kind": "scene", "id": "s1", "label": ""},
    ]
    r = _post_msg(client, token, pid, cid, "引用两处", refs=refs)
    assert r.status_code == 201, r.text
    assert r.json()["refs"] == refs  # 出参解析为数组、逐项一致
    # 入库为 JSON 文本
    mid = r.json()["object_ref"]["id"]
    with Session() as s:
        stored = s.scalar(select(Message.refs).where(Message.id == mid))
    assert isinstance(stored, str)
    assert json.loads(stored) == refs
    # 详情/列表同样解析为数组
    d = client.get(
        f"/api/studio/projects/{pid}/conversations/{cid}", headers=_auth(token)
    ).json()
    assert d["messages"][0]["refs"] == refs
    l = client.get(
        f"/api/studio/projects/{pid}/conversations/{cid}/messages",
        headers=_auth(token),
    ).json()
    assert l["items"][0]["refs"] == refs


def test_t36_post_refs_null_and_empty(db, client):
    token = _register(client, "t36_ines")
    pid = _mk_project(client, token, "P36")
    cid = _open_ok(client, token, pid, "project", pid)
    r1 = _post_msg(client, token, pid, cid, "no refs")
    assert r1.status_code == 201 and r1.json()["refs"] is None
    with Session() as s:
        assert s.scalar(select(Message.refs).where(Message.id == r1.json()["object_ref"]["id"])) is None
    r2 = _post_msg(client, token, pid, cid, "empty refs", refs=[])
    assert r2.status_code == 201, r2.text
    assert r2.json()["refs"] == []


def test_t37_post_refs_invalid_422(db, client):
    token = _register(client, "t37_jack")
    pid = _mk_project(client, token, "P37")
    cid = _open_ok(client, token, pid, "project", pid)
    bad_refs = [
        "not-a-list",
        {"kind": "fragment", "id": "x", "label": ""},
        [1],
        ["x"],
        [{}],
        [{"kind": "", "id": "x", "label": ""}],
        [{"kind": "fragment", "id": "", "label": ""}],
        [{"kind": "fragment", "id": "x"}],
        [{"kind": "fragment", "id": "x", "label": 1}],
        [{"kind": "fragment", "id": "x", "label": None}],
    ]
    for refs in bad_refs:
        r = _post_msg(client, token, pid, cid, "x", refs=refs)
        assert r.status_code == 422, (refs, r.text)
        _assert_violation(r.json(), "refs", "invalid")
    assert _count("studio_messages") == 0


def test_t38_post_sender_enum_422(db, client):
    token = _register(client, "t38_kim")
    pid = _mk_project(client, token, "P38")
    cid = _open_ok(client, token, pid, "project", pid)
    for bad in ("", "system", "User", "agent"):
        r = _post_msg(client, token, pid, cid, "content", sender=bad)
        assert r.status_code == 422, (bad, r.text)
        _assert_violation(r.json(), "sender", "enum")
    # 校验序：sender 先于 content（sender 非法 + content 空 → 报 sender）
    r = _post_msg(client, token, pid, cid, "   ", sender="system")
    _assert_violation(r.json(), "sender", "enum")


def test_t39_post_content_required_and_too_long(db, client):
    token = _register(client, "t39_liam")
    pid = _mk_project(client, token, "P39")
    cid = _open_ok(client, token, pid, "project", pid)
    for bad in ("", "   ", "\n\t "):
        r = _post_msg(client, token, pid, cid, bad)
        assert r.status_code == 422, (bad, r.text)
        _assert_violation(r.json(), "content", "required")
    r2 = _post_msg(client, token, pid, cid, "a" * 8001)
    assert r2.status_code == 422, r2.text
    _assert_violation(r2.json(), "content", "too_long")
    r3 = _post_msg(client, token, pid, cid, "a" * 8000)
    assert r3.status_code == 201, r3.text
    assert len(r3.json()["content"]) == 8000
    assert _count("studio_messages") == 1


def test_t40_post_job_id_exists_and_project_scoped(db, client):
    token = _register(client, "t40_mia")
    pid = _mk_project(client, token, "P40")
    pid2 = _mk_project(client, token, "P40b")
    cid = _open_ok(client, token, pid, "project", pid)
    uid = client.get("/api/studio/auth/me", headers=_auth(token)).json()["user"]["id"]
    job = _mk_job(pid, uid)
    r = _post_msg(client, token, pid, cid, "with job", job_id=job)
    assert r.status_code == 201, r.text
    assert r.json()["job_id"] == job
    # 详情回读
    d = client.get(
        f"/api/studio/projects/{pid}/conversations/{cid}", headers=_auth(token)
    ).json()
    assert d["messages"][0]["job_id"] == job
    # 不存在 job → 404
    missing = "0" * 26
    r2 = _post_msg(client, token, pid, cid, "no job", job_id=missing)
    assert r2.status_code == 404, r2.text
    _assert_not_found(r2.json(), "job", missing)
    # 同 owner 跨项目的 job → 404
    job2 = _mk_job(pid2, uid)
    r3 = _post_msg(client, token, pid, cid, "cross job", job_id=job2)
    assert r3.status_code == 404, r3.text
    _assert_not_found(r3.json(), "job", job2)
    # 他人项目的 job → 404
    tb = _register(client, "t40_mia2")
    pid_b = _mk_project(client, tb, "P40c")
    uid_b = client.get("/api/studio/auth/me", headers=_auth(tb)).json()["user"]["id"]
    job_b = _mk_job(pid_b, uid_b)
    r4 = _post_msg(client, token, pid, cid, "foreign job", job_id=job_b)
    assert r4.status_code == 404, r4.text
    _assert_not_found(r4.json(), "job", job_b)


def test_t41_post_replay_200_identical_zero_side_effects(db, client):
    token = _register(client, "t41_nina")
    pid = _mk_project(client, token, "P41")
    cid = _open_ok(client, token, pid, "project", pid)
    cmd = _cmd()
    refs = [{"kind": "fragment", "id": "f1", "label": "L"}]
    r1 = _post_msg(client, token, pid, cid, "hello", refs=refs, cmd=cmd)
    assert r1.status_code == 201, r1.text
    with Session() as s:
        before = (
            s.scalar(select(func.count(Message.id))),
            s.scalar(select(func.count(CommandRecord.id))),
            s.scalar(select(func.count(Proposal.id))),
        )
    r2 = _post_msg(client, token, pid, cid, "hello", refs=refs, cmd=cmd)
    assert r2.status_code == 200, (r2.status_code, r2.text)
    assert r2.json() == r1.json()
    with Session() as s:
        after = (
            s.scalar(select(func.count(Message.id))),
            s.scalar(select(func.count(CommandRecord.id))),
            s.scalar(select(func.count(Proposal.id))),
        )
    assert after == before, "重放不得新增消息/命令/提案"


def test_t42_post_command_id_reused_422(db, client):
    token = _register(client, "t42_omar")
    pid = _mk_project(client, token, "P42")
    cid = _open_ok(client, token, pid, "project", pid)
    cmd = _cmd()
    assert _post_msg(client, token, pid, cid, "first", cmd=cmd).status_code == 201
    r2 = _post_msg(client, token, pid, cid, "second", cmd=cmd)
    assert r2.status_code == 422, r2.text
    _assert_violation(r2.json(), "command_id", "reused")
    # 改 refs 亦为异内容
    r3 = _post_msg(
        client, token, pid, cid, "first", refs=[{"kind": "f", "id": "i", "label": ""}],
        cmd=cmd,
    )
    assert r3.status_code == 422, r3.text
    _assert_violation(r3.json(), "command_id", "reused")
    assert _count("studio_messages") == 1


def test_t43_post_404_shapes(db, client):
    ta = _register(client, "t43_pia")
    tb_user = _register_user(client, "t43_quinn")
    pid_a = _mk_project(client, ta, "A43")
    pid_a2 = _mk_project(client, ta, "A43b")
    pid_b = _mk_project(client, tb_user["token"], "B43")
    cid_a = _open_ok(client, ta, pid_a, "project", pid_a)
    missing = "0" * 26
    # 不存在会话
    r = _post_msg(client, ta, pid_a, missing, "x")
    assert r.status_code == 404, r.text
    _assert_not_found(r.json(), "conversation", missing)
    # 同 owner 跨项目会话
    r2 = _post_msg(client, ta, pid_a2, cid_a, "x")
    assert r2.status_code == 404, r2.text
    _assert_not_found(r2.json(), "conversation", cid_a)
    # 他人属主会话（同项目）
    foreign = _mk_conversation_row(pid_a, tb_user["user"]["id"])
    r3 = _post_msg(client, ta, pid_a, foreign, "x")
    assert r3.status_code == 404, r3.text
    _assert_not_found(r3.json(), "conversation", foreign)
    # 他人项目 → 项目 404
    r4 = _post_msg(client, ta, pid_b, cid_a, "x")
    assert r4.status_code == 404, r4.text
    _assert_not_found(r4.json(), "project", pid_b)
    # 不存在项目
    r5 = _post_msg(client, ta, missing, cid_a, "x")
    assert r5.status_code == 404, r5.text
    _assert_not_found(r5.json(), "project", missing)


def test_t44_post_missing_command_id_422(db, client):
    token = _register(client, "t44_rosa")
    pid = _mk_project(client, token, "P44")
    cid = _open_ok(client, token, pid, "project", pid)
    r = client.post(
        f"/api/studio/projects/{pid}/conversations/{cid}/messages",
        json={"sender": "user", "content": "x"},
        headers=_auth(token),
    )
    assert r.status_code == 422


def test_t45_post_writes_no_business_tables_no_proposals(db, client):
    token = _register(client, "t45_sam")
    pid = _mk_project(client, token, "P45")
    frag = _mk_fragment(pid)
    cid = _open_ok(client, token, pid, "fragment", frag)
    before = _counts()
    r = _post_msg(client, token, pid, cid, "hi", refs=[{"kind": "fragment", "id": frag, "label": "L"}])
    assert r.status_code == 201, r.text
    after = _counts()
    assert after == before, f"追加消息写入了业务表：{before} → {after}"
    assert _count("studio_proposals") == 0, "本卡不得建提案"
    # 只允许这两张本域表增长
    assert _count("studio_messages") == 1
    assert _count("studio_conversations") == 1


def test_t46_no_llm_and_no_network_source_scan(db, client):
    import backend.studio.conversations.api as api_mod
    import backend.studio.conversations.service as svc

    src = inspect.getsource(svc) + "\n" + inspect.getsource(api_mod)
    for banned in (
        "requests",
        "httpx",
        "urllib",
        "openai",
        "anthropic",
        "socket",
        "aiohttp",
        "Proposal",
    ):
        assert banned not in src, f"实现不得引用 {banned}（不调模型/不发网络/不建提案）"
    assert not hasattr(svc, "Proposal")
    assert not hasattr(api_mod, "Proposal")


def test_t47_schema_route_paths_mounted(db, client):
    """router.py「存在即挂载」自动接线：五路由均在 /api/studio 前缀下。"""
    paths = _studio_app().openapi()["paths"]
    expected = {
        "/api/studio/projects/{pid}/conversations": {"get", "post"},
        "/api/studio/projects/{pid}/conversations/{cid}": {"get"},
        "/api/studio/projects/{pid}/conversations/{cid}/messages": {"get", "post"},
    }
    for p, methods in expected.items():
        assert p in paths, f"路由未挂载：{p}"
        assert methods <= set(paths[p]), f"路由缺方法 {p}: {paths.get(p)}"


def _studio_app():
    from backend.studio.app_factory import app as studio_app

    return studio_app
