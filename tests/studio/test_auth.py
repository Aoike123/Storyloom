"""BASE-03-c — 严格鉴权 + 冻结错误协议（附录 00 §4）验证。

模块级 env 在任何 backend.* import 之前以 setdefault 设置：若同会话
其他 studio 测试模块（如 test_accounts.py）已设定并绑定丢弃库，则复用
其值，避免 env 竞争。测试 app 为本文件内新建的本地 FastAPI app
（install_error_handlers + auth_router），不碰 backend.studio.app_factory。

DSH sandbox 限制：collection 期可创建文件/目录，test 执行期 mkdir 被
拦截；目录在 collection 期创建，执行期仅按需补建库文件（参照
test_accounts.py 策略），最终清理由 atexit hook 执行。
"""
import atexit
import os
import shutil
import uuid as _uuid

# ---------------------------------------------------------------------------
# 模块级 env（在任何 backend.* import 之前）
# ---------------------------------------------------------------------------
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TMP = f"data/studio-test-{_uuid.uuid4().hex[:8]}"
_TMP_ABS = os.path.join(_ROOT, _TMP)
os.makedirs(_TMP_ABS, exist_ok=True)  # 目录创建于 collection 期
os.environ.setdefault("STUDIO_DATA_DIR", _TMP_ABS)
os.environ.setdefault("STUDIO_DATABASE_URL", f"sqlite:///{os.path.join(_TMP_ABS, 'auth.db')}")

# setdefault 之后，记录本进程实际生效的丢弃库位置（可能复用他模块的库）。
_DATA_DIR = os.environ["STUDIO_DATA_DIR"]
_DATABASE_URL = os.environ["STUDIO_DATABASE_URL"]
assert _DATABASE_URL.startswith("sqlite:///")
_DB_FILE = _DATABASE_URL[len("sqlite:///"):]

# 进程退出时清理本模块创建的空目录（env 复用时该目录仅为占位）。
atexit.register(shutil.rmtree, _TMP_ABS, ignore_errors=True)

import re  # noqa: E402

import pytest  # noqa: E402
import starlette.requests  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.core import auth as auth_mod  # noqa: E402
from backend.core.auth import assert_owner  # noqa: E402
from backend.core.db import Base, engine  # noqa: E402
from backend.studio.contracts.errors import StudioAPIError, install_error_handlers  # noqa: E402


# ---------------------------------------------------------------------------
# 本地测试 app 与 fixture
# ---------------------------------------------------------------------------

def make_app():
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(auth_mod.router)
    return app


def _ensure_db_file() -> None:
    """确保丢弃库目录与文件存在（DSH sandbox 下 test 执行期 mkdir 可能受限）。"""
    if not os.path.isdir(_DATA_DIR):
        os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.isfile(_DB_FILE):
        import sqlite3

        conn = sqlite3.connect(_DB_FILE)
        conn.execute("CREATE TABLE _init(x)")
        conn.commit()
        conn.execute("DROP TABLE _init")
        conn.commit()
        conn.close()


@pytest.fixture
def client():
    _ensure_db_file()
    Base.metadata.drop_all(engine)  # setup 前 drop，保证干净
    Base.metadata.create_all(engine)
    yield TestClient(make_app())
    engine.dispose()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _register(client, username, password, email=""):
    return client.post(
        "/api/studio/auth/register",
        json={"username": username, "password": password, "email": email},
    )


def _login(client, username, password):
    return client.post(
        "/api/studio/auth/login", json={"username": username, "password": password}
    )


def _me(client, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/api/studio/auth/me", headers=headers or None)


def assert_frozen_error(resp, code: str, status: int, message: str | None = None) -> dict:
    """冻结错误体形状（附录 00 §4）：顶层仅 {"error"}，error 恰为
    {code, message, details}，code 与 HTTP 状态码符合白名单映射。"""
    assert resp.status_code == status, f"期望 {status}，实际 {resp.status_code}: {resp.text}"
    body = resp.json()
    assert set(body.keys()) == {"error"}, f"顶层应仅有一个 error 键: {body}"
    err = body["error"]
    assert set(err.keys()) == {"code", "message", "details"}, f"error 形状不符: {err}"
    assert err["code"] == code, f"期望 code={code}，实际 {err['code']}"
    assert "details" in err
    if message is not None:
        assert err["message"] == message, f"期望 message={message!r}，实际 {err['message']!r}"
    return err


def _raw_request(token: str) -> starlette.requests.Request:
    return starlette.requests.Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    )


# ---------------------------------------------------------------------------
# T1 — register 成功
# ---------------------------------------------------------------------------

def test_t1_register_success(client) -> None:
    resp = _register(client, "alice", "secret-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    user = body["user"]
    assert re.fullmatch(r"user_[0-9a-f]{16}", user["id"]), f"id 格式不符: {user['id']}"
    assert user["username"] == "alice"
    assert user["email"] == ""
    assert isinstance(user["created"], float)
    assert body["token"], "token 应非空"


# ---------------------------------------------------------------------------
# T2 — register 重复用户名
# ---------------------------------------------------------------------------

def test_t2_register_duplicate_username(client) -> None:
    _register(client, "alice", "secret-1")
    resp = _register(client, "alice", "other-1")
    err = assert_frozen_error(resp, "validation_failed", 422)
    assert err["details"]["violations"][0] == {
        "field": "username",
        "rule": "unique",
        "message": "该用户名已存在。",
    }


# ---------------------------------------------------------------------------
# T3 — register 弱输入
# ---------------------------------------------------------------------------

def test_t3_register_weak_input(client) -> None:
    resp = _register(client, "x" * 61, "12345")  # username 超长 + password <6
    err = assert_frozen_error(resp, "validation_failed", 422)
    violations = err["details"]["violations"]
    assert violations, "violations 应非空"
    assert {v["field"] for v in violations} == {"username", "password"}
    for v in violations:
        assert v["rule"], "每个 violation 应带 rule"
        assert v["message"], "每个 violation 应带 message"


# ---------------------------------------------------------------------------
# T4 — login 正确 + token 轮换
# ---------------------------------------------------------------------------

def test_t4_login_rotates_token(client) -> None:
    reg = _register(client, "bob", "secret-2")
    assert reg.status_code == 201
    token1 = reg.json()["token"]

    resp = _login(client, "bob", "secret-2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    token2 = body["token"]
    assert token2 and token2 != token1, "login 应轮换 token"
    assert body["user"]["username"] == "bob"

    # 旧 token 立即失效
    assert_frozen_error(_me(client, token=token1), "unauthenticated", 401)

    # 新 token 有效
    resp2 = _me(client, token=token2)
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["user"]["username"] == "bob"
    assert body2["token"] == token2


# ---------------------------------------------------------------------------
# T5 — login 错误凭证（不区分用户不存在/密码错误）
# ---------------------------------------------------------------------------

def test_t5_login_wrong_credentials(client) -> None:
    _register(client, "carol", "secret-3")
    assert_frozen_error(
        _login(client, "carol", "wrong-pass"),
        "unauthenticated",
        401,
        message="用户名或密码错误。",
    )
    assert_frozen_error(
        _login(client, "nobody-here", "whatever1"),
        "unauthenticated",
        401,
        message="用户名或密码错误。",
    )


# ---------------------------------------------------------------------------
# T6 — /me 无凭证 401 + cookie 通道
# ---------------------------------------------------------------------------

def test_t6_me_no_credentials_and_cookie_channel(client) -> None:
    # 无凭证 → 401 冻结体
    assert_frozen_error(_me(client), "unauthenticated", 401)

    # cookie 通道：register 后仅带 sl_auth cookie（无 Authorization 头）
    reg = _register(client, "dave", "secret-4")
    assert reg.status_code == 201
    token = reg.json()["token"]
    resp = client.get("/api/studio/auth/me", cookies={"sl_auth": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["username"] == "dave"
    assert body["token"] == token


# ---------------------------------------------------------------------------
# T7 — assert_owner 越权 / 匹配（直接构造 starlette Request）
# ---------------------------------------------------------------------------

def test_t7_assert_owner(client) -> None:
    reg_alice = _register(client, "erin", "secret-5")
    assert reg_alice.status_code == 201
    alice = reg_alice.json()
    alice_id = alice["user"]["id"]
    alice_token = alice["token"]

    reg_other = _register(client, "frank", "secret-6")
    assert reg_other.status_code == 201
    other_token = reg_other.json()["token"]

    # 越权：other 的 token，owner 为 alice → forbidden 冻结错误
    with pytest.raises(StudioAPIError) as ei:
        assert_owner(_raw_request(other_token), alice_id)
    assert ei.value.code == "forbidden"
    assert ei.value.details == {"kind": "project", "id": alice_id}
    assert ei.value.status == 403

    # owner 匹配 → 返回该 User
    u = assert_owner(_raw_request(alice_token), alice_id)
    assert u.id == alice_id
    assert u.username == "erin"


# ---------------------------------------------------------------------------
# T8 — 源码无旁路身份（contextvars / active_user / request_ctx）
# ---------------------------------------------------------------------------

def test_t8_no_contextvar_bypass() -> None:
    src = open(os.path.join(_ROOT, "backend", "core", "auth.py"), encoding="utf-8").read()
    for banned in ("contextvars", "active_user", "request_ctx"):
        assert banned not in src, f"backend/core/auth.py 含旁路身份残留: {banned}"
