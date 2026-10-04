"""新 studio 后端测试的 conftest。

父级 tests/conftest.py 定义了 autouse 的 `database` 夹具（对旧 engine
drop/init 旧表）以及绑定旧 app 的 `client` 夹具。pytest 对同名夹具取最近
定义，因此在本目录重定义两者，使新测试独立于旧数据库与旧 app：

- `database`：no-op 覆盖，不触发旧库初始化；
- `client`：指向 backend.studio.app_factory 中的隔离新 app。

本文件不 import 任何旧 backend 模块，不设置指向旧 data/ 的环境变量。
"""

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
