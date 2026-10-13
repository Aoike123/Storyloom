"""REL-02 资产出现引用命令（附录 06 §2 OC1–OC3）—— /relations/occurrences。

覆盖：建立出现（201、DTO 形状、资产/片段 revision 快照）、同资产多片段出现共享
同一资产身份、无精确范围桶唯一与精确范围并存、range_invalid 三 rule、跨项目/幽灵
对象 404 同形状、片段绑定版本失效 422、幂等重放 200 零写与 reused 422、列表过滤/
倒序/keyset 分页、删除 200 与重放/404 收敛、零业务副作用（不复制资产、不建片段、
不写绑定）。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件不改写 STUDIO_* env。db fixture 照抄 tests/studio/test_assets.py。
"""
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.core.db import Base, Session, engine
from backend.studio.assets.models import Asset, AssetRevision
from backend.studio.contracts.models import CommandRecord
from backend.studio.relations.models import AssetBinding, AssetOccurrence
from backend.studio.scripts.models import ScriptObject
from backend.studio.sources.fragment_models import Fragment, FragmentRevision, RangeSet

TEXT = "0123456789012345678901234567890123456789"  # 40 code units


@pytest.fixture
def db():
    conn = engine.connect()
    try:
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


def _mk_project(client, token, name="Proj") -> str:
    r = client.post(
        "/api/studio/projects",
        json={"name": name, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mk_asset(client, token, pid, name="林") -> str:
    r = client.post(
        f"/api/studio/projects/{pid}/assets",
        json={
            "type": "character",
            "name": name,
            "spec": {"appearance": "短发"},
            "command_id": _cmd(),
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["object_ref"]["id"]


def _mk_source(client, token, pid, content=TEXT):
    r = client.post(
        f"/api/studio/projects/{pid}/sources",
        json={"content": content, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mk_fragment(client, token, pid, rev_id, start=0, end=10, name="F", expected=None):
    r = client.post(
        f"/api/studio/projects/{pid}/fragments",
        json={
            "source_revision_id": rev_id,
            "range": {"start": start, "end": end},
            "name": name,
            "expected_range_set_revision": expected,
            "command_id": _cmd(),
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["object_ref"]["id"]


def _cas(pid) -> int:
    """项目当前 range_set CAS（第二个片段起必须用真实当前值）。"""
    with Session() as s:
        return s.scalar(select(RangeSet.cas_revision).where(RangeSet.project_id == pid))


def _confirm(client, token, pid, fid):
    r = client.post(
        f"/api/studio/projects/{pid}/fragments/{fid}/confirm",
        json={"expected_revision": 1, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text


def _setup(client, token, name="Proj", *, confirm=True):
    """项目 + active 正文 + 片段（可选确认）+ 人物资产。"""
    pid = _mk_project(client, token, name)
    rev = _mk_source(client, token, pid)
    fid = _mk_fragment(client, token, pid, rev["id"])
    if confirm:
        _confirm(client, token, pid, fid)
    aid = _mk_asset(client, token, pid)
    return pid, rev, fid, aid


def _activate(client, token, pid, target_revision_id, expected_active_revision_id):
    rp = client.post(
        f"/api/studio/projects/{pid}/source-changes/preview",
        json={
            "kind": "source_activate",
            "target_revision_id": target_revision_id,
            "command_id": _cmd(),
        },
        headers=_auth(token),
    )
    assert rp.status_code == 200, rp.text
    ra = client.post(
        f"/api/studio/projects/{pid}/source-changes/apply",
        json={
            "preview_id": rp.json()["preview_id"],
            "command_id": _cmd(),
            "expected_active_revision_id": expected_active_revision_id,
        },
        headers=_auth(token),
    )
    assert ra.status_code == 200, ra.text


def _occ(client, token, pid, aid, fid, range_=None, command_id=None):
    body = {
        "asset_id": aid,
        "source_fragment_id": fid,
        "command_id": command_id or _cmd(),
    }
    if range_ is not None:
        body["range"] = range_
    return client.post(
        f"/api/studio/projects/{pid}/relations/occurrences",
        json=body,
        headers=_auth(token),
    )


def _list(client, token, pid, **params):
    return client.get(
        f"/api/studio/projects/{pid}/relations/occurrences",
        params=params,
        headers=_auth(token),
    )


def _del(client, token, pid, oid, command_id=None):
    return client.request(
        "DELETE",
        f"/api/studio/projects/{pid}/relations/occurrences/{oid}",
        json={"command_id": command_id or _cmd()},
        headers=_auth(token),
    )


def _counts() -> dict:
    with Session() as s:
        return {
            "occurrences": s.scalar(select(func.count(AssetOccurrence.id))),
            "bindings": s.scalar(select(func.count(AssetBinding.id))),
            "assets": s.scalar(select(func.count(Asset.id))),
            "revisions": s.scalar(select(func.count(AssetRevision.id))),
            "fragments": s.scalar(select(func.count(Fragment.id))),
            "frag_revs": s.scalar(select(func.count(FragmentRevision.id))),
            "objects": s.scalar(select(func.count(ScriptObject.id))),
            "commands": s.scalar(select(func.count(CommandRecord.id))),
        }


# ── OC1 建立出现 ─────────────────────────────────────────────────────────────

def test_t1_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/" + "01" * 13 + "/relations/occurrences",
        json={"asset_id": "a", "source_fragment_id": "f", "command_id": _cmd()},
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t2_create_201_dto_and_db(db, client):
    token = _register(client, "r2_t2")
    pid, rev, fid, aid = _setup(client, token)
    before = _counts()
    r = _occ(client, token, pid, aid, fid)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["object_ref"]["kind"] == "asset_occurrence"
    assert body["object_ref"]["revision"] == 1  # = source_fragment_revision
    assert body["asset"] == {"kind": "asset", "id": aid, "revision": 1}
    assert body["fragment"] == {"kind": "fragment", "id": fid, "revision": 1}
    assert body["range"] is None
    assert isinstance(body["created_at"], float)

    with Session() as s:
        row = s.get(AssetOccurrence, body["object_ref"]["id"])
        assert row is not None
        assert row.project_id == pid
        assert row.asset_id == aid and row.asset_revision == 1
        assert row.source_fragment_id == fid and row.source_fragment_revision == 1
        assert row.range_start is None and row.range_end is None
    after = _counts()
    assert after["occurrences"] == before["occurrences"] + 1
    assert after["commands"] == before["commands"] + 1
    assert after["assets"] == before["assets"]  # 不复制资产
    assert after["revisions"] == before["revisions"]
    assert after["fragments"] == before["fragments"]  # 不建片段
    assert after["frag_revs"] == before["frag_revs"]
    assert after["objects"] == before["objects"]
    assert after["bindings"] == 0  # 不写生成绑定


def test_t3_idempotent_replay_and_reused(db, client):
    token = _register(client, "r2_t3")
    pid, rev, fid, aid = _setup(client, token)
    cmd = _cmd()
    r1 = _occ(client, token, pid, aid, fid, range_={"start": 2, "end": 5}, command_id=cmd)
    assert r1.status_code == 201, r1.text
    before = _counts()
    r2 = _occ(client, token, pid, aid, fid, range_={"start": 2, "end": 5}, command_id=cmd)
    assert r2.status_code == 200, r2.text  # 重放覆盖 201
    assert r2.json() == r1.json()
    assert _counts() == before  # 零写
    r3 = _occ(client, token, pid, aid, fid, range_={"start": 3, "end": 5}, command_id=cmd)
    assert r3.status_code == 422, r3.text
    v = r3.json()["error"]["details"]["violations"][0]
    assert v["field"] == "command_id" and v["rule"] == "reused"
    assert _counts() == before


def test_t4_same_asset_occurrences_share_identity(db, client):
    token = _register(client, "r2_t4")
    pid, rev, fid, aid = _setup(client, token)
    fid2 = _mk_fragment(
        client, token, pid, rev["id"], start=10, end=20, name="F2",
        expected=_cas(pid),
    )
    r1 = _occ(client, token, pid, aid, fid)
    r2 = _occ(client, token, pid, aid, fid2)
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)
    assert r1.json()["asset"]["id"] == r2.json()["asset"]["id"] == aid
    with Session() as s:
        rows = s.scalars(
            select(AssetOccurrence).where(AssetOccurrence.asset_id == aid)
        ).all()
        assert len(rows) == 2
        assert len({r.asset_id for r in rows}) == 1  # 共享同一资产身份
        assert s.scalar(select(func.count(Asset.id))) == 1  # 未复制资产
        assert s.scalar(select(func.count(Fragment.id))) == 2  # 未新建片段


def test_t5_unique_bucket_and_ranges(db, client):
    token = _register(client, "r2_t5")
    pid, rev, fid, aid = _setup(client, token)
    r = _occ(client, token, pid, aid, fid)
    assert r.status_code == 201, r.text
    # 同桶（无精确范围）重复 → 409 duplicate
    before = _counts()
    dup = _occ(client, token, pid, aid, fid)
    assert dup.status_code == 409, dup.text
    err = dup.json()["error"]
    assert err["code"] == "duplicate"
    assert err["details"]["scope_ref"].startswith("asset_occurrence:")
    assert _counts() == before
    # 有精确范围 → 另一桶，可再建
    r2 = _occ(client, token, pid, aid, fid, range_={"start": 2, "end": 5})
    assert r2.status_code == 201, r2.text
    # 同范围再建 → 409
    dup2 = _occ(client, token, pid, aid, fid, range_={"start": 2, "end": 5})
    assert dup2.status_code == 409, dup2.text
    with Session() as s:
        assert s.scalar(select(func.count(AssetOccurrence.id))) == 2


def test_t6_range_invalid_rules(db, client):
    token = _register(client, "r2_t6")
    pid, rev, fid, aid = _setup(client, token)
    before = _counts()
    cases = [
        ({"start": "a", "end": 5}, "non_integer"),
        ({"start": 5, "end": 5}, "empty"),
        ({"start": 0, "end": 11}, "out_of_bounds"),
        ({"start": -1, "end": 5}, "out_of_bounds"),
    ]
    for range_, rule in cases:
        r = _occ(client, token, pid, aid, fid, range_=range_)
        assert r.status_code == 422, (range_, r.text)
        err = r.json()["error"]
        assert err["code"] == "range_invalid", (range_, err)
        assert err["details"]["rule"] == rule, (range_, err)
    assert _counts() == before
    # 边界：[0,10) 与 [1,10) 合法
    assert _occ(client, token, pid, aid, fid, range_={"start": 0, "end": 10}).status_code == 201


def test_t7_asset_404_shapes(db, client):
    token = _register(client, "r2_t7")
    pid, rev, fid, aid = _setup(client, token)
    pid_b, rev_b, fid_b, aid_b = _setup(client, token, "ProjB")
    ghost = "01" * 13
    for target in (ghost, aid_b):
        r = _occ(client, token, pid, target, fid)
        assert r.status_code == 404, (target, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"] == {"kind": "asset", "id": target}


def test_t8_fragment_404_shapes(db, client):
    token = _register(client, "r2_t8")
    pid, rev, fid, aid = _setup(client, token)
    pid_b, rev_b, fid_b, aid_b = _setup(client, token, "ProjB")
    ghost = "02" * 13
    for target in (ghost, fid_b):
        r = _occ(client, token, pid, aid, target)
        assert r.status_code == 404, (target, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"] == {"kind": "fragment", "id": target}


def test_t9_project_404_shapes(db, client):
    token = _register(client, "r2_t9")
    other = _register(client, "r2_t9b")
    pid, rev, fid, aid = _setup(client, token)
    pid_other = _mk_project(client, other, "Other")
    ghost = "03" * 13
    for target_pid in (ghost, pid_other):
        r = _occ(client, token, target_pid, aid, fid)
        assert r.status_code == 404, (target_pid, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"] == {"kind": "project", "id": target_pid}


def test_t10_inactive_fragment_422(db, client):
    token = _register(client, "r2_t10")
    pid, rev, fid, aid = _setup(client, token)
    rev2 = _mk_source(client, token, pid, "abcdefghijklmnopqrst" * 2)
    _activate(client, token, pid, rev2["id"], rev["id"])
    before = _counts()
    r = _occ(client, token, pid, aid, fid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "precondition_failed"
    assert err["details"]["reason"] == "source_revision_inactive"
    assert err["details"]["blocked_by"] == {"kind": "fragment", "id": fid, "revision": 1}
    assert _counts() == before


def test_t11_list_filters_order_pagination(db, client):
    token = _register(client, "r2_t11")
    pid, rev, fid, aid = _setup(client, token)
    fid2 = _mk_fragment(
        client, token, pid, rev["id"], start=10, end=20, name="F2",
        expected=_cas(pid),
    )
    ids = []
    for fid_, range_ in ((fid, None), (fid, {"start": 2, "end": 5}), (fid2, None)):
        r = _occ(client, token, pid, aid, fid_, range_=range_)
        assert r.status_code == 201, r.text
        ids.append(r.json()["object_ref"]["id"])

    r = _list(client, token, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["object_ref"]["id"] for i in body["items"]] == sorted(ids, reverse=True)
    assert body["next_cursor"] is None

    r = _list(client, token, pid, source_fragment_id=fid)
    assert r.status_code == 200
    assert {i["fragment"]["id"] for i in r.json()["items"]} == {fid}
    assert len(r.json()["items"]) == 2

    # keyset 分页：limit=1 → 逐页倒序，next_cursor = 本页末项 id
    page1 = _list(client, token, pid, limit=1).json()
    assert len(page1["items"]) == 1 and page1["next_cursor"] == page1["items"][0]["object_ref"]["id"]
    page2 = _list(client, token, pid, limit=1, cursor=page1["next_cursor"]).json()
    assert len(page2["items"]) == 1
    assert page2["items"][0]["object_ref"]["id"] != page1["items"][0]["object_ref"]["id"]

    bad = _list(client, token, pid, cursor="nope")
    assert bad.status_code == 422
    v = bad.json()["error"]["details"]["violations"][0]
    assert v["field"] == "cursor" and v["rule"] == "format"

    # 项目隔离：他人项目 → 404
    other = _register(client, "r2_t11b")
    pid_other = _mk_project(client, other, "Other")
    assert _list(client, token, pid_other).status_code == 404


def test_t12_delete_and_replay(db, client):
    token = _register(client, "r2_t12")
    pid, rev, fid, aid = _setup(client, token)
    created = _occ(client, token, pid, aid, fid)
    assert created.status_code == 201, created.text
    oid = created.json()["object_ref"]["id"]
    cmd = _cmd()
    before = _counts()
    r = _del(client, token, pid, oid, command_id=cmd)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert r.json()["object_ref"]["id"] == oid
    with Session() as s:
        assert s.get(AssetOccurrence, oid) is None
    mid = _counts()
    assert mid["occurrences"] == before["occurrences"] - 1
    assert mid["commands"] == before["commands"] + 1
    # 同 command_id 重放 → 200 原 payload，零写
    r2 = _del(client, token, pid, oid, command_id=cmd)
    assert r2.status_code == 200 and r2.json() == r.json(), r2.text
    assert _counts() == mid
    # 换新 command_id → 404 收敛
    r3 = _del(client, token, pid, oid)
    assert r3.status_code == 404, r3.text
    assert r3.json()["error"]["details"] == {"kind": "asset_occurrence", "id": oid}
    # 跨项目 → 404
    other = _register(client, "r2_t12b")
    pid_other = _mk_project(client, other, "Other")
    assert _del(client, token, pid_other, oid).status_code == 404
