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


# ── SB3 场改名（附录 04 §2）─────────────────────────────────────────────────
#
# 冻结语义：场就地改名、无版本（object_ref.revision 恒 null，不产生 revision
# 行）；layout_cas_revision 是镜头/场布局重排的 CAS（SB7 专属），改名不得改；
# seq / created_at / id / project_id 不变；只改 name 与 updated_at。
# 成功与重放都是 200（本命令成功码即 200，不使用 JSONResponse 覆盖 201）。


def _rename_scene(client, token, pid, sid, *, name="新场名", command_id=None):
    """SB3 请求体：{name, command_id}（无 seq —— 改名不改顺序）。"""
    return client.post(
        f"/api/studio/projects/{pid}/scenes/{sid}/rename",
        json={"name": name, "command_id": command_id or _cmd()},
        headers=_auth(token),
    )


def _scene_row(sid) -> dict | None:
    """场行逐字段快照（用于"仅 name/updated_at 变化"的逐字段断言）。"""
    with Session() as s:
        row = s.get(Scene, sid)
        if row is None:
            return None
        return {
            "id": row.id,
            "project_id": row.project_id,
            "name": row.name,
            "seq": row.seq,
            "layout_cas_revision": row.layout_cas_revision,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def test_t13_rename_no_token_401(db, client):
    r = client.post(
        "/api/studio/projects/whatever/scenes/whatever/rename",
        json={"name": "X", "command_id": _cmd()},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


def test_t14_rename_project_404_both_shapes(db, client):
    token = _register(client, "t14_alice")
    other = _register(client, "t14_bob")
    other_pid = _mk_project(client, other, name="BobProj")
    r_other = _create_scene(client, other, other_pid, name="Bob场")
    assert r_other.status_code == 201, r_other.text
    other_sid = r_other.json()["object_ref"]["id"]

    for pid in ("01nosuchproject0000000000a", other_pid):
        r = _rename_scene(client, token, pid, other_sid)
        assert r.status_code == 404, (pid, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        # 两路同形状（不泄漏存在性）
        assert err["details"] == {"kind": "project", "id": pid}
    # 404 短路先于字段校验：name 空 + 仅项目门禁失败仍是 404（不改他人场）
    r = _rename_scene(client, token, other_pid, other_sid, name="   ")
    assert r.status_code == 404
    assert r.json()["error"]["details"] == {"kind": "project", "id": other_pid}
    assert _scene_row(other_sid)["name"] == "Bob场"


def test_t15_rename_scene_404_both_shapes(db, client):
    token = _register(client, "t15_alice")
    pid = _mk_project(client, token)
    r_own = _create_scene(client, token, pid, name="己场")
    assert r_own.status_code == 201
    own_sid = r_own.json()["object_ref"]["id"]

    other = _register(client, "t15_bob")
    other_pid = _mk_project(client, other, name="BobProj")
    r_bob = _create_scene(client, other, other_pid, name="Bob场")
    assert r_bob.status_code == 201
    bob_sid = r_bob.json()["object_ref"]["id"]

    # 场不存在 / 场属他人项目：两情形同形状（不泄漏存在性）
    for sid in ("01nosuchscene00000000000a", bob_sid):
        r = _rename_scene(client, token, pid, sid)
        assert r.status_code == 404, (sid, r.text)
        err = r.json()["error"]
        assert err["code"] == "not_found"
        assert err["details"] == {"kind": "scene", "id": sid}
    # 404 短路先于 name 校验：场不存在时空白 name 仍是 404（不是 422）
    r = _rename_scene(client, token, pid, "01nosuchscene00000000000z", name="   ")
    assert r.status_code == 404
    assert r.json()["error"]["details"] == {
        "kind": "scene",
        "id": "01nosuchscene00000000000z",
    }
    # 对照：己场 + 空白 name 才走到字段校验 422
    r = _rename_scene(client, token, pid, own_sid, name="   ")
    assert r.status_code == 422
    assert _first_field_rule(r) == ("name", "required")
    assert _scene_row(bob_sid)["name"] == "Bob场"


def test_t16_rename_name_required_max_length_and_trim_saved(db, client):
    token = _register(client, "t16_carol")
    pid = _mk_project(client, token)
    sid = _create_scene(client, token, pid, name="原名", seq=1).json()["object_ref"]["id"]

    for bad in ("", "   ", "\t\n "):
        r = _rename_scene(client, token, pid, sid, name=bad)
        assert r.status_code == 422, (bad, r.text)
        assert _first_field_rule(r) == ("name", "required"), (bad, r.text)
    # 根本性类型错误（非字符串）由请求体形态层拦截，loc 仍指向 name
    for bad in (123, True, None):
        r = _rename_scene(client, token, pid, sid, name=bad)
        assert r.status_code == 422, (bad, r.text)
        assert _first_field_rule(r)[0] == "name", (bad, r.text)
    # trim 后 121（两侧空白不计入长度）
    for bad in ("a" * 121, "  " + "a" * 121 + "  "):
        r = _rename_scene(client, token, pid, sid, name=bad)
        assert r.status_code == 422, r.text
        assert _first_field_rule(r) == ("name", "max_length")
    # 全部非法请求后场行未被触碰
    assert _scene_row(sid)["name"] == "原名"

    # 保存的是 trim 后的值（两侧去空白，内部原样保留）；边界 120 合法
    r = _rename_scene(client, token, pid, sid, name=" 夜  雨 ")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "夜  雨"
    assert _scene_row(sid)["name"] == "夜  雨"

    r = _rename_scene(client, token, pid, sid, name="  " + "b" * 120 + "  ")
    assert r.status_code == 200, r.text
    assert len(r.json()["name"]) == 120


def test_t17_rename_only_name_and_updated_at_change(db, client):
    token = _register(client, "t17_dave")
    pid = _mk_project(client, token)
    r1 = _create_scene(client, token, pid, name="旧名", seq=1)
    sid = r1.json()["object_ref"]["id"]
    r2 = _create_scene(client, token, pid, name="别动我", seq=2)
    other_sid = r2.json()["object_ref"]["id"]
    before = _scene_row(sid)
    other_before = _scene_row(other_sid)
    assert _count(Scene) == 2

    time.sleep(0.02)
    r = _rename_scene(client, token, pid, sid, name="  新名  ")
    assert r.status_code == 200, r.text
    body = r.json()
    # DTO 与 SB1 同形状，恰 7 键；revision 恒 null（改名无版本）
    assert set(body) == {
        "object_ref",
        "name",
        "seq",
        "layout_cas_revision",
        "project_id",
        "created_at",
        "updated_at",
    }
    assert body["object_ref"] == {"kind": "scene", "id": sid, "revision": None}
    assert body["name"] == "新名"
    assert body["project_id"] == pid
    assert body["seq"] == before["seq"]
    assert body["layout_cas_revision"] == before["layout_cas_revision"]
    assert body["created_at"] == before["created_at"]
    assert body["updated_at"] > before["updated_at"]  # 前移

    after = _scene_row(sid)
    assert after["name"] == "新名"
    assert after["updated_at"] == body["updated_at"]
    # id / project_id / seq / created_at / layout_cas_revision 逐字段不变
    for key in ("id", "project_id", "seq", "layout_cas_revision", "created_at"):
        assert after[key] == before[key], (key, before, after)
    # 另一场行逐字段不变；场行数不变
    assert _scene_row(other_sid) == other_before
    assert _count(Scene) == 2


def test_t18_rename_replay_200_original_body(db, client):
    token = _register(client, "t18_grace")
    pid = _mk_project(client, token)
    sid = _create_scene(client, token, pid, name="旧", seq=1).json()["object_ref"]["id"]
    cmd = _cmd()

    time.sleep(0.02)
    r1 = _rename_scene(client, token, pid, sid, name=" 重放名 ", command_id=cmd)
    assert r1.status_code == 200, r1.text
    assert _cmd_record_payload(cmd) == r1.json()  # result_payload = 完整 200 体

    time.sleep(0.02)
    r2 = _rename_scene(client, token, pid, sid, name=" 重放名 ", command_id=cmd)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()  # 原 body 原样
    assert _scene_row(sid)["updated_at"] == r1.json()["updated_at"]  # 重放不再前移
    assert _scene_row(sid)["name"] == "重放名"
    assert _count(Scene) == 1


def test_t19_rename_same_command_different_content_422_reused(db, client):
    token = _register(client, "t19_heidi")
    pid = _mk_project(client, token)
    sid_a = _create_scene(client, token, pid, name="甲", seq=1).json()["object_ref"]["id"]
    sid_b = _create_scene(client, token, pid, name="乙", seq=2).json()["object_ref"]["id"]
    cmd = _cmd()
    assert _rename_scene(client, token, pid, sid_a, name="A", command_id=cmd).status_code == 200

    # 异 name（同场）/ 异目标场（同 name）：均 422 reused
    for sid, name in ((sid_a, "B"), (sid_b, "A")):
        r = _rename_scene(client, token, pid, sid, name=name, command_id=cmd)
        assert r.status_code == 422, (sid, name, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_failed"
        v0 = err["details"]["violations"][0]
        assert v0["field"] == "command_id" and v0["rule"] == "reused"
    # 两个场行都未被二次改动
    assert _scene_row(sid_a)["name"] == "A"
    assert _scene_row(sid_b)["name"] == "乙"
    assert _count(Scene) == 2


def test_t20_rename_negative_no_shots_no_other_domain_writes(db, client):
    token = _register(client, "t20_ivan")
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

    sid_a = _create_scene(client, token, pid, name="一号", seq=1).json()["object_ref"]["id"]
    sid_b = _create_scene(client, token, pid, name="二号", seq=2).json()["object_ref"]["id"]
    other_before = _scene_row(sid_b)

    cmd = _cmd()
    assert _rename_scene(client, token, pid, sid_a, name="改名后", command_id=cmd).status_code == 200
    # 重放一次也不应产生任何负向表写入
    assert _rename_scene(client, token, pid, sid_a, name="改名后", command_id=cmd).status_code == 200
    assert _rename_scene(client, token, pid, sid_a, name="再来一次").status_code == 200

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
    # 另一场行逐字段不变；被改场行的 seq/layout_cas_revision 不变
    assert _scene_row(sid_b) == other_before
    assert _scene_row(sid_a)["name"] == "再来一次"
    assert _scene_row(sid_a)["seq"] == 1
    assert _scene_row(sid_a)["layout_cas_revision"] == 1
    assert _count(Scene) == 2
    # 项目行未被本命令触碰
    with Session() as s:
        proj = s.scalar(select(StudioProject).where(StudioProject.id == pid))
        assert proj.name == "SProj"
        assert proj.updated == proj.created
