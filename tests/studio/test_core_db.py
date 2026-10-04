"""BASE-03-a — 新后端数据库连接与 Base 验证。

用子进程隔离 import 期 env：模块级 engine / DATABASE_URL 在 import 时定值，
本进程不可重复 import 以测试不同 env 组合。
"""
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 在 run_py 中默认剔除的新系统专用 env 变量
_EXCLUDED = ("STUDIO_DATA_DIR", "STUDIO_DATABASE_URL")


def run_py(code: str, env_over: dict | None = None) -> str:
    """在干净子进程中运行 code；默认剔除 STUDIO_* env，env_over 可重新注入。

    返回子进程 stdout；returncode != 0 时抛 AssertionError（附 stderr）。
    """
    env = {k: v for k, v in os.environ.items() if k not in _EXCLUDED}
    if env_over:
        env.update(env_over)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"subprocess rc={result.returncode}\nstderr:\n{result.stderr}"
    )
    return result.stdout


# ---------------------------------------------------------------------------
# T1 — 默认路径与引擎
# ---------------------------------------------------------------------------

def test_t1_default_path_and_engine() -> None:
    """STUDIO_DATA_DIR / STUDIO_DATABASE_URL 均未设 → 默认 data/studio/story.db；
    WAL、foreign_keys 生效；Base 为空时 sqlite_master 无表。"""
    code = (
        "import backend.core.db as db\n"
        "assert db.DATABASE_URL.endswith('data/studio/story.db'), db.DATABASE_URL\n"
        "assert str(db.engine.url) == db.DATABASE_URL\n"
        "with db.Session() as s:\n"
        "    assert s.execute(__import__('sqlalchemy').text('SELECT 1')).scalar() == 1\n"
        "import sqlalchemy as sa\n"
        "with db.engine.connect() as c:\n"
        "    assert c.execute(sa.text('PRAGMA journal_mode')).scalar() == 'wal'\n"
        "    assert c.execute(sa.text('PRAGMA foreign_keys')).scalar() == 1\n"
        "with db.engine.connect() as c:\n"
        "    tables = {r[0] for r in c.execute(sa.text(\n"
        "        \"SELECT name FROM sqlite_master WHERE type='table'\"))}\n"
        "print('TABLES:', sorted(tables))\n"
    )
    stdout = run_py(code)
    assert "TABLES: []" in stdout, stdout


# ---------------------------------------------------------------------------
# T2 — env 覆盖
# ---------------------------------------------------------------------------

def test_t2_env_override() -> None:
    """STUDIO_DATA_DIR + STUDIO_DATABASE_URL 显式覆盖 → 用 tmp 路径；
    init_db() 后 custom.db 存在、story.db 不存在。"""
    tmp = ROOT / "data" / f"studio-test-{uuid.uuid4().hex[:8]}"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        custom_url = f"sqlite:///{tmp}/custom.db"
        env_over = {
            "STUDIO_DATA_DIR": str(tmp),
            "STUDIO_DATABASE_URL": custom_url,
        }
        code = (
            "import backend.core.db as db\n"
            "print('URL:', db.DATABASE_URL)\n"
            "print('ENGINE_URL:', str(db.engine.url))\n"
            "db.init_db()\n"
            "db.engine.dispose()\n"
            "print('DONE')\n"
        )
        stdout = run_py(code, env_over=env_over)
        assert f"URL: {custom_url}" in stdout, stdout
        assert f"ENGINE_URL: {custom_url}" in stdout, stdout
        assert "DONE" in stdout, stdout
        # init_db 建文件（sqlite 文件在首次 connect 时创建）
        assert (tmp / "custom.db").exists(), f"custom.db 缺失: {list(tmp.iterdir())}"
        assert not (tmp / "story.db").exists(), f"story.db 不应存在于 {tmp}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# T3 — 无旧模块引用
# ---------------------------------------------------------------------------

def test_t3_no_legacy_references() -> None:
    """backend/core/db.py 源码不得出现旧模块或旧模型名称子串。"""
    src = (ROOT / "backend" / "core" / "db.py").read_text()
    for forbidden in ("backend.db", "from ..", "environment", "Record", "Task"):
        assert forbidden not in src, (
            f"backend/core/db.py 含禁止子串 {forbidden!r}"
        )


# ---------------------------------------------------------------------------
# T4 — 会话配置
# ---------------------------------------------------------------------------

def test_t4_session_config() -> None:
    """Session.kw['expire_on_commit'] is False。"""
    code = (
        "import backend.core.db as db\n"
        "assert db.Session.kw.get('expire_on_commit') is False, db.Session.kw\n"
        "print('OK')\n"
    )
    stdout = run_py(code)
    assert "OK" in stdout, stdout
