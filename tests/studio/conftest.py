"""新 studio 后端测试的 conftest。

父级 tests/conftest.py 定义了 autouse 的 `database` 夹具（对旧 engine
drop/init 旧表）以及绑定旧 app 的 `client` 夹具。pytest 对同名夹具取最近
定义，因此在本目录重定义两者，使新测试独立于旧数据库与旧 app：

- `database`：no-op 覆盖，不触发旧库初始化；
- `client`：指向 backend.studio.app_factory 中的隔离新 app。

本文件不 import 任何旧 backend 模块，不设置指向旧 data/ 的环境变量。

session 级丢弃库：backend.core.db 的 engine 是进程单例（import 时按
STUDIO_* env 定值），因此本 conftest 在所有测试模块 import 之前统一
setdefault 一个 session 丢弃库目录/URL，并预创建空 sqlite 文件
（SQLite 对空文件按全新空库初始化）。各测试文件只允许做防御断言，
不得再各自改写 STUDIO_* env（那会导致单例 engine 绑定漂移）。
"""
import atexit
import os
import shutil
import uuid

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TMP = f"data/studio-test-{uuid.uuid4().hex[:8]}"
_TMP_ABS = os.path.join(_ROOT, _TMP)
os.makedirs(_TMP_ABS, exist_ok=True)
os.environ.setdefault("STUDIO_DATA_DIR", _TMP_ABS)
os.environ.setdefault("STUDIO_DATABASE_URL", f"sqlite:///{_TMP_ABS}/session.db")
# collection 期预创建空 sqlite 文件（test 执行期 mkdir 可能被 sandbox 拦截）
open(os.path.join(_TMP_ABS, "session.db"), "a").close()
atexit.register(shutil.rmtree, _TMP_ABS, ignore_errors=True)

# 模型注册表完整性：import 全部现有模型模块（import 即注册到 Base），
# 使任何 in-process 测试看到的 Base.metadata 完整且确定。
# 后续 DB 卡验收时主模型在此追加对应 import。
#
# 跨卡 FK 占位：SQLAlchemy 在 create_all 时按 Base.metadata 解析 FK 目标
# （NoReferencedTableError），比 SQLite DDL 层更严。studio_media_artifacts
# 属 DB-10 尚未交付 → 此处注册最小占位 Table；DB-10 验收时以真实
# MediaArtifact 声明类注册同名字表覆盖此条目（MetaData 按名替换），
# 届时移除本占位。占位存在期间 media_artifact_id 只允许 NULL。
from backend.core.db import Base  # noqa: E402
from sqlalchemy import Column, String, Table as _StubTable  # noqa: E402

if "studio_media_artifacts" not in Base.metadata.tables:
    _StubTable("studio_media_artifacts", Base.metadata, Column("id", String(32), primary_key=True))

import backend.core.accounts  # noqa: F401,E402
import backend.studio.projects.models  # noqa: F401,E402
import backend.studio.sources.models  # noqa: F401,E402
import backend.studio.sources.fragment_models  # noqa: F401,E402
import backend.studio.scripts.models  # noqa: F401,E402
import backend.studio.storyboard.models  # noqa: F401,E402
import backend.studio.assets.models  # noqa: F401,E402
import backend.studio.contracts.models  # noqa: F401,E402
import backend.studio.jobs.models  # noqa: F401,E402

import pytest


@pytest.fixture(autouse=True)
def database():
    """覆盖父级 autouse 夹具：新测试不初始化旧库。"""
    return


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from backend.studio.app_factory import app

    return TestClient(app)
