"""BASE-03-d — 新配置读取层 backend.core.config 验证。

env_guard（function 级）：记录并清除 STUDIO_ENV_FILE / LLM_MODEL / LLM_API_KEY /
LLM_FAST_MODEL，teardown 恢复原值（原本不存在则删除），不污染其他测试文件。

DSH sandbox 限制（同 test_accounts.py）：collection 期可创建目录/文件，
test 执行期 mkdir 被拦截。因此丢弃目录 data/studio-test-<uuid8>/ 与临时
env 文件 cfg.env 在模块 import 期创建，atexit 兜底 rmtree，
fixture 仅覆写既有 cfg.env 内容（不新建文件）。
"""
import atexit
import os
import shutil
import uuid as _uuid

import pytest

import backend.core.config as config

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TMP = f"data/studio-test-{_uuid.uuid4().hex[:8]}"
_TMP_ABS = os.path.join(_ROOT, _TMP)
_CFG_ABS = os.path.join(_TMP_ABS, "cfg.env")
os.makedirs(_TMP_ABS, exist_ok=True)
with open(_CFG_ABS, "w", encoding="utf-8") as _f:
    _f.write("")

# 进程退出时最终清理（atexit 兜底）
atexit.register(shutil.rmtree, _TMP_ABS, ignore_errors=True)

_ENV_NAMES = ("STUDIO_ENV_FILE", "LLM_MODEL", "LLM_API_KEY", "LLM_FAST_MODEL")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def env_guard():
    """记录并清除 4 个 env 名；yield 后还原（原本不存在则删除）。"""
    saved = {name: os.environ.get(name) for name in _ENV_NAMES}
    for name in _ENV_NAMES:
        os.environ.pop(name, None)
    yield
    for name in _ENV_NAMES:
        if saved[name] is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved[name]


def _write_cfg(lines: list[str]) -> str:
    with open(_CFG_ABS, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return _CFG_ABS


def _ensure_tmp():
    """确保丢弃目录存在（DSH sandbox 下执行期 mkdir 可能受限）。"""
    if not os.path.isdir(_TMP_ABS):
        os.makedirs(_TMP_ABS, exist_ok=True)


# ---------------------------------------------------------------------------
# T1 默认 + 纯读取（无副作用）
# ---------------------------------------------------------------------------

def test_defaults_and_no_side_effects(env_guard):
    pre_env = {k for k in os.environ if k.startswith("STUDIO_")}
    pre_env_file = (config.ROOT / ".env.studio").is_file()
    cfg = config.model_config()
    # 完全相等：DEFAULTS（guard 已清 4 键；shell 环境无其他 _KEYS 污染）
    assert cfg == config.DEFAULTS
    # import/调用不注入 STUDIO_* env
    assert {k for k in os.environ if k.startswith("STUDIO_")} == pre_env
    # 不创建默认文件
    assert (config.ROOT / ".env.studio").is_file() == pre_env_file
    assert not pre_env_file  # 测试前不存在，测试后仍不存在


# ---------------------------------------------------------------------------
# T2 文件层读取
# ---------------------------------------------------------------------------

def test_file_layer(env_guard):
    _ensure_tmp()
    path = _write_cfg(
        ["LLM_MODEL=dummy-model-x", "LLM_API_KEY=test-key-123", "IGNORED_KEY=zzz"]
    )
    os.environ["STUDIO_ENV_FILE"] = path
    cfg = config.model_config()
    assert cfg["LLM_MODEL"] == "dummy-model-x"
    assert cfg["LLM_API_KEY"] == "test-key-123"
    assert "IGNORED_KEY" not in cfg


# ---------------------------------------------------------------------------
# T3 os.environ 层（无文件）
# ---------------------------------------------------------------------------

def test_env_layer(env_guard):
    _ensure_tmp()
    os.environ["STUDIO_ENV_FILE"] = os.path.join(_TMP_ABS, "missing.env")
    os.environ["LLM_MODEL"] = "env-model-y"
    cfg = config.model_config()
    assert cfg["LLM_MODEL"] == "env-model-y"


# ---------------------------------------------------------------------------
# T4 优先级：文件 > os.environ
# ---------------------------------------------------------------------------

def test_priority_file_over_env(env_guard):
    _ensure_tmp()
    path = _write_cfg(["LLM_FAST_MODEL=file-fast"])
    os.environ["STUDIO_ENV_FILE"] = path
    os.environ["LLM_FAST_MODEL"] = "env-fast"
    os.environ["LLM_MODEL"] = "env-only"
    cfg = config.model_config()
    assert cfg["LLM_FAST_MODEL"] == "file-fast"  # 文件优先
    assert cfg["LLM_MODEL"] == "env-only"        # 文件无此键 → 取 env


# ---------------------------------------------------------------------------
# T5 文件不存在 → DEFAULTS，无异常
# ---------------------------------------------------------------------------

def test_missing_file_returns_defaults(env_guard):
    _ensure_tmp()
    os.environ["STUDIO_ENV_FILE"] = os.path.join(_TMP_ABS, "missing.env")
    assert config.model_config() == config.DEFAULTS
