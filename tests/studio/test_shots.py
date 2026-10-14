"""SB1 新建场（附录 04 §2）—— POST /api/studio/projects/{pid}/scenes 行为验证。

裁定（主模型冻结，见任务卡）：studio_scenes 只有
id/project_id/name/seq/layout_cas_revision/created_at/updated_at，无任何
fragment/adoption 列；场内的片段归属由镜头的 script_adoption_id 承载（SB4
建立）。故 SB1 **只创建场行**，不接受片段入参、绝不生成 Shot/ShotRevision。

丢弃库由 tests/studio/conftest.py 在 session 级统一设定（engine 是进程单例）；
本文件不自行改写 STUDIO_* env。db fixture 照抄 test_projects.py。
"""
import re
import time
import uuid

import pytest
from sqlalchemy import func, select, text

from backend.core.db import Base, Session, engine
from backend.studio.contracts.models import CommandRecord
from backend.studio.jobs.models import StudioJob
from backend.studio.projects.models import StudioProject
from backend.studio.relations.models import (
    AssetBinding,
    AssetOccurrence,
    SourceRelation,
)
from backend.studio.scripts.models import ScriptAdoption
from backend.studio.sources.fragment_models import Fragment
from backend.studio.storyboard.models import Scene, Shot, ShotRevision


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


def _mk_project(client, token, name="SProj") -> str:
    r = client.post(
        "/api/studio/projects",
        json={"name": name, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_scene(client, token, pid, *, name="第一场", seq=None, command_id=None):
    """SB1 请求体：seq 缺省（None）时不发送该键 = 显式 null 语义（下一位）。"""
    body = {"name": name, "command_id": command_id or _cmd()}
    if seq is not None:
        body["seq"] = seq
    return client.post(
        f"/api/studio/projects/{pid}/scenes", json=body, headers=_auth(token)
    )


def _count(model) -> int:
    with Session() as s:
        return len(s.execute(select(model)).scalars().all())


def _violations(r) -> list[dict]:
    """422 响应体 violations（形态层 422 由 FastAPI 默认体承载，细节形状不同）。"""
    body = r.json()
    if "error" in body:
        assert body["error"]["code"] == "validation_failed", body
        return body["error"]["details"]["violations"]
    return body.get("detail", [])


def _first_field_rule(r) -> tuple:
    """首个 violation 的 (field, rule)。

    服务层 violations 形状 = {"field","rule","message"}（附录 00 §4）；
    形态层 422 由 FastAPI 默认体承载 = {"type","loc","msg","input"}，
    此处把 loc 尾段读作 field（rule 保留其 type，如 value_error/string_type）。
    """
    v0 = _violations(r)[0]
    if "field" in v0:
        return v0["field"], v0.get("rule")
    return (v0.get("loc") or [None])[-1], v0.get("type")


def _cmd_record_payload(command_id: str) -> dict:
    import json

    with Session() as s:
        rec = s.scalar(
            select(CommandRecord).where(CommandRecord.command_id == command_id)
        )
        assert rec is not None
        return json.loads(rec.result_payload)


# ── T1 鉴权与项目门禁 ──────────────────────────────────────────────────────


def test_t1_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/whatever/scenes",
        json={"name": "X", "command_id": _cmd()},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t2_project_not_found_and_other_owner_404(db, client):
    token = _register(client, "t2_alice")
    other = _register(client, "t2_bob")
    other_pid = _mk_project(client, other, name="BobProj")

    for pid in ("01nosuchproject0000000000a", other_pid):
        r = _create_scene(client, token, pid)
        assert r.status_code == 404, (pid, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        # 两路同形状（不泄漏存在性）
        assert err["details"] == {"kind": "project", "id": pid}
    # 404 短路先于字段校验：name 空 + 不存在项目仍是 404
    r = _create_scene(client, token, other_pid, name="   ")
    assert r.status_code == 404


# ── T3/T4 name 校验 ────────────────────────────────────────────────────────


def test_t3_name_required_422(db, client):
    token = _register(client, "t3_bob")
    pid = _mk_project(client, token)
    for bad in ("", "   ", "\t\n "):
        r = _create_scene(client, token, pid, name=bad)
        assert r.status_code == 422, (bad, r.text)
        assert _first_field_rule(r) == ("name", "required"), (bad, r.text)
    # 根本性类型错误（非字符串）由请求体形态层拦截，同 422 且 loc 指向 name
    for bad in (123, True, None):
        r = _create_scene(client, token, pid, name=bad)
        assert r.status_code == 422, (bad, r.text)
        field, _rule = _first_field_rule(r)
        assert field == "name", (bad, r.text)
    assert _count(Scene) == 0


def test_t4_name_max_length_422_and_trimmed_value_saved(db, client):
    token = _register(client, "t4_carol")
    pid = _mk_project(client, token)

    r = _create_scene(client, token, pid, name="a" * 121)
    assert r.status_code == 422, r.text
    field, rule = _first_field_rule(r)
    assert field == "name" and rule == "max_length"
    # trim 后 121（两侧空白不计入长度）
    r = _create_scene(client, token, pid, name="  " + "a" * 121 + "  ")
    assert r.status_code == 422, r.text
    assert _first_field_rule(r) == ("name", "max_length")

    # 保存的是 trim 后的值（两侧去空白，内部原样保留；边界 120 合法）
    r = _create_scene(client, token, pid, name=" 夜  雨  ", seq=1)
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "夜  雨"
    with Session() as s:
        row = s.scalar(select(Scene).where(Scene.seq == 1))
        assert row.name == "夜  雨"

    r = _create_scene(client, token, pid, name="  " + "b" * 120 + "  ")
    assert r.status_code == 201, r.text
    assert len(r.json()["name"]) == 120


# ── T5/T6/T7 seq 语义 ──────────────────────────────────────────────────────


def test_t5_default_seq_first_is_1_then_2(db, client):
    token = _register(client, "t5_dave")
    pid = _mk_project(client, token)
    r1 = _create_scene(client, token, pid)
    assert r1.status_code == 201, r1.text
    assert r1.json()["seq"] == 1
    r2 = _create_scene(client, token, pid)
    assert r2.status_code == 201, r2.text
    assert r2.json()["seq"] == 2
    # 显式 seq 也参与 max+1（与 SC1 先例一致）
    r3 = _create_scene(client, token, pid, seq=9)
    assert r3.status_code == 201 and r3.json()["seq"] == 9
    r4 = _create_scene(client, token, pid)
    assert r4.status_code == 201 and r4.json()["seq"] == 10
    assert _count(Scene) == 4


def test_t6_explicit_seq_honored_and_taken_422(db, client):
    token = _register(client, "t6_erin")
    pid = _mk_project(client, token)
    r1 = _create_scene(client, token, pid, name="占位", seq=3)
    assert r1.status_code == 201, r1.text
    occupant = r1.json()["object_ref"]["id"]

    r2 = _create_scene(client, token, pid, name="撞车", seq=3)
    assert r2.status_code == 422, r2.text
    err = r2.json()["error"]
    assert err["code"] == "validation_failed"
    v0 = err["details"]["violations"][0]
    assert v0["field"] == "seq" and v0["rule"] == "taken"
    assert occupant in v0["message"]  # message 给出已占用场的 id
    assert _count(Scene) == 1

    # 另一项目内同 seq 不冲突（唯一键是 (project_id, seq)）
    pid2 = _mk_project(client, token, name="SProj2")
    r3 = _create_scene(client, token, pid2, seq=3)
    assert r3.status_code == 201, r3.text


def test_t7_seq_invalid_422(db, client):
    token = _register(client, "t7_frank")
    pid = _mk_project(client, token)
    for bad in (0, -1):
        r = _create_scene(client, token, pid, seq=bad)
        assert r.status_code == 422, (bad, r.text)
        assert _first_field_rule(r) == ("seq", "invalid"), (bad, r.text)
    # bool / 小数 / 字符串：形态层 422（rule=invalid 已在服务层复核，见 api.py
    # field_validator），loc 仍指向 seq
    for bad in (1.5, True, "1"):
        r = _create_scene(client, token, pid, seq=bad)
        assert r.status_code == 422, (bad, r.text)
        field, _rule = _first_field_rule(r)
        assert field == "seq", (bad, r.text)
    # 显式 null 合法（= 下一位）；"seq": false 亦非法
    r = client.post(
        f"/api/studio/projects/{pid}/scenes",
        json={"name": "null seq", "seq": None, "command_id": _cmd()},
        headers=_auth(token),
    )
    assert r.status_code == 201 and r.json()["seq"] == 1
    r = _create_scene(client, token, pid, seq=False)
    assert r.status_code == 422, r.text
    assert _count(Scene) == 1


# ── T8/T9 幂等重放与 command_id 复用 ───────────────────────────────────────


def test_t8_replay_same_command_returns_200_original_body(db, client):
    token = _register(client, "t8_grace")
    pid = _mk_project(client, token)
    cmd = _cmd()
    r1 = _create_scene(client, token, pid, name=" 重放场 ", seq=5, command_id=cmd)
    assert r1.status_code == 201, r1.text
    payload = _cmd_record_payload(cmd)
    assert payload == r1.json()  # result_payload = 完整 201 体

    time.sleep(0.01)
    r2 = _create_scene(client, token, pid, name=" 重放场 ", seq=5, command_id=cmd)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()  # 原 body 原样
    assert _count(Scene) == 1  # 场行数不变
    with Session() as s:
        row = s.scalar(select(Scene))
        assert row.created_at == r1.json()["created_at"]
        assert row.updated_at == r1.json()["updated_at"]

    # 缺省 seq 的重放：不参与比对，返回首次分配的 seq（不重新分配）
    cmd2 = _cmd()
    a1 = _create_scene(client, token, pid, command_id=cmd2)
    assert a1.status_code == 201 and a1.json()["seq"] == 6
    a2 = _create_scene(client, token, pid, command_id=cmd2)
    assert a2.status_code == 200 and a2.json() == a1.json()
    assert _count(Scene) == 2


def test_t9_same_command_different_content_422_reused(db, client):
    token = _register(client, "t9_heidi")
    pid = _mk_project(client, token)
    cmd = _cmd()
    r1 = _create_scene(client, token, pid, name="A", seq=1, command_id=cmd)
    assert r1.status_code == 201, r1.text

    for kwargs in ({"name": "B", "seq": 1}, {"name": "A", "seq": 2}):
        r = _create_scene(client, token, pid, command_id=cmd, **kwargs)
        assert r.status_code == 422, (kwargs, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v0 = err["details"]["violations"][0]
        assert v0["field"] == "command_id" and v0["rule"] == "reused"
    assert _count(Scene) == 1


# ── T10 负向断言：本卡不生成镜头、不写其他域 ───────────────────────────────


def test_t10_negative_no_shots_no_other_domain_writes(db, client):
    token = _register(client, "t10_ivan")
    pid = _mk_project(client, token)

    def counts() -> dict:
        with Session() as s:
            return {
                "shots": s.scalar(select(func.count()).select_from(Shot)),
                "shot_revisions": s.scalar(
                    select(func.count()).select_from(ShotRevision)
                ),
                "source_relations": s.scalar(
                    select(func.count()).select_from(SourceRelation)
                ),
                "asset_occurrences": s.scalar(
                    select(func.count()).select_from(AssetOccurrence)
                ),
                "asset_bindings": s.scalar(
                    select(func.count()).select_from(AssetBinding)
                ),
                "script_adoptions": s.scalar(
                    select(func.count()).select_from(ScriptAdoption)
                ),
                "fragments": s.scalar(select(func.count()).select_from(Fragment)),
                "jobs": s.scalar(select(func.count()).select_from(StudioJob)),
            }

    before = counts()
    assert before["shots"] == 0 and before["shot_revisions"] == 0

    for seq in (None, 2, 7):
        r = _create_scene(client, token, pid, seq=seq)
        assert r.status_code == 201, r.text
    # 重放一次也不应产生任何负向表写入
    cmd = _cmd()
    assert _create_scene(client, token, pid, seq=4, command_id=cmd).status_code == 201
    assert _create_scene(client, token, pid, seq=4, command_id=cmd).status_code == 200

    after = counts()
    assert after == before, after
    for key in (
        "shots",
        "shot_revisions",
        "source_relations",
        "asset_occurrences",
        "asset_bindings",
        "script_adoptions",
        "fragments",
        "jobs",
    ):
        assert after[key] == before[key] == 0, (key, before, after)
    # 项目行未被修改（updated_at 不被本命令触碰）
    with Session() as s:
        proj = s.scalar(select(StudioProject).where(StudioProject.id == pid))
        assert proj.name == "SProj"
        assert proj.updated == proj.created
    assert _count(Scene) == 4


# ── T11 DTO 形状与展示字段 ────────────────────────────────────────────────


def test_t11_dto_exact_7_keys(db, client):
    token = _register(client, "t11_jack")
    pid = _mk_project(client, token)
    r = _create_scene(client, token, pid, name="  开场  ", seq=1)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {
        "object_ref",
        "name",
        "seq",
        "layout_cas_revision",
        "project_id",
        "created_at",
        "updated_at",
    }
    sid = body["object_ref"]["id"]
    assert body["object_ref"] == {"kind": "scene", "id": sid, "revision": None}
    assert re.fullmatch(r"[0-9a-z]{26}", sid) and not (set(sid) & set("ilou"))
    assert body["name"] == "开场"  # trimmed
    assert body["seq"] == 1
    assert body["layout_cas_revision"] == 1
    assert body["project_id"] == pid
    assert isinstance(body["created_at"], (int, float))
    assert isinstance(body["updated_at"], (int, float))


def test_t12_updated_at_advances_only_on_new_create(db, client):
    token = _register(client, "t12_kate")
    pid = _mk_project(client, token)
    r1 = _create_scene(client, token, pid, command_id=_cmd())
    assert r1.status_code == 201
    time.sleep(0.02)
    cmd2 = _cmd()
    r2 = _create_scene(client, token, pid, command_id=cmd2)
    assert r2.status_code == 201
    assert r2.json()["updated_at"] > r1.json()["updated_at"]  # 新建后前移
    time.sleep(0.02)
    r3 = _create_scene(client, token, pid, command_id=cmd2)
    assert r3.status_code == 200
    assert r3.json()["updated_at"] == r2.json()["updated_at"]  # 重放不变
    assert _count(Scene) == 2
