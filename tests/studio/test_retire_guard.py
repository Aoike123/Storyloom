"""RETIRE-01 验收：静态依赖守卫（scripts/check_legacy_deps.py）。

分两组证据：

1. **真实仓库**：新命名空间（backend/studio、backend/core、backend/infrastructure）
   对旧业务模块与旧 API 路径**零依赖**——这是本卡的验收本体，且是后续所有
   新卡的回归守卫。
2. **守卫有效性**：用 tmp_path 构造合成分支，证明守卫**真的能抓到**旧业务
   import / 旧 API 字符串 / 语法错误，并对允许的基础设施与相对 import 放行。
   没有这一组，第 1 组的「通过」不能排除「守卫本身失效」。
"""
import importlib.util
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_GUARD_PATH = _ROOT / "scripts" / "check_legacy_deps.py"


def _load_guard():
    """按文件路径加载守卫模块（scripts/ 不是包）。"""
    spec = importlib.util.spec_from_file_location("check_legacy_deps", _GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


# ── 1. 真实仓库：零旧业务依赖（本卡验收本体） ──────────────────────────────


def test_real_tree_has_no_legacy_imports():
    """新命名空间不得 import 任何旧业务模块（R01/R11）。"""
    violations = guard.check_imports(_ROOT)
    assert violations == [], "新命名空间出现旧业务依赖：\n" + "\n".join(
        f"  {v['file']}:{v['line']} [{v['module']}] {v['reason']}" for v in violations
    )


def test_real_tree_has_no_legacy_api_strings():
    """新命名空间不得引用旧业务 API 路径（新契约一律 /api/studio/**）。"""
    violations = guard.check_legacy_api_strings(_ROOT)
    assert violations == [], "新命名空间引用了旧 API 路径：\n" + "\n".join(
        f"  {v['file']}:{v['line']} {v['module']}" for v in violations
    )


def test_real_tree_collect_is_clean_and_exit_code_zero():
    """合并守卫在真实仓库上无违规，且 CLI 语义为 0。"""
    assert guard.collect_violations(_ROOT) == []
    assert guard.main(["--root", str(_ROOT)]) == 0


def test_guard_scans_all_three_new_namespaces():
    """守卫覆盖 backend/studio、backend/core、backend/infrastructure 三根。"""
    assert guard.NEW_NAMESPACE_ROOTS == (
        "backend/studio",
        "backend/core",
        "backend/infrastructure",
    )


# ── 2. 守卫有效性：合成夹具 ────────────────────────────────────────────────


# 合成夹具以**扫描根**为基准建 `backend/studio/...` 等目录，使守卫命中的
# 命名空间根与真实仓库一致（守卫的根是相对仓库根的路径，如 "backend/studio"）。
# 本测试文件自身位于仓库的 tests/ 下、不在扫描根内，故不会被自己的
# `backend.core` import 污染断言。
def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


@pytest.mark.parametrize("module", sorted(guard.FORBIDDEN_MODULES))
def test_detects_every_forbidden_module_both_spellings(tmp_path, module):
    """每个旧业务模块都必须被抓住——裸名与 backend. 点号名两种写法都要命中。

    两种写法各用**独立扫描根**，否则第一次写入的裸名文件会在第二次扫描时
    被重复计入（守卫每次都重扫整个根）。
    """
    bare_root = tmp_path / "bare"
    dotted_root = tmp_path / "dotted"

    _write(bare_root, "backend/studio/x/a.py", f"import {module}\n")
    found = guard.check_imports(bare_root)
    assert [v["module"] for v in found] == [module]

    _write(dotted_root, "backend/studio/x/b.py", f"from backend.{module} import thing\n")
    found = guard.check_imports(dotted_root)
    assert [v["module"] for v in found] == [f"backend.{module}"]


def test_detects_from_import_of_legacy_submodule(tmp_path):
    """`from backend.creative import director` 也要命中（子模块不逃逸）。"""
    _write(tmp_path, "backend/core/x.py", "from backend.creative import director\n")
    found = guard.check_imports(tmp_path)
    assert len(found) == 1
    assert found[0]["module"] == "backend.creative"
    assert "旧业务模块" in found[0]["reason"]


def test_detects_legacy_api_path_string(tmp_path):
    """旧 API 路径字符串（新命名空间内）必须被报告。"""
    _write(
        tmp_path,
        "backend/studio/x.py",
        'URL = "/api/author/projects"\n',
    )
    found = guard.check_legacy_api_strings(tmp_path)
    assert len(found) == 1
    assert found[0]["module"] == "/api/author/projects"
    assert "旧业务 API 路径" in found[0]["reason"]


def test_allows_new_studio_api_path(tmp_path):
    """新契约路径 `/api/studio/**` 不得被误报。"""
    _write(tmp_path, "backend/studio/x.py", 'URL = "/api/studio/projects"\n')
    assert guard.check_legacy_api_strings(tmp_path) == []


def test_allows_documented_infrastructure(tmp_path):
    """冻结的四个基础设施模块必须放行（否则守卫会误伤全部新代码）。"""
    _write(
        tmp_path,
        "backend/studio/x.py",
        """
        from backend.core.db import Session, Base, make_ulid
        from backend.core.accounts import User, hash_password
        from backend.core.auth import require_user
        from backend.core.config import settings
        """,
    )
    assert guard.check_imports(tmp_path) == []


def test_allows_new_namespace_internal_and_relative_imports(tmp_path):
    """新命名空间内部 import 与相对 import 必须放行（新代码主体形态）。"""
    _write(
        tmp_path,
        "backend/studio/x.py",
        """
        from backend.studio.media.store import store_media
        from backend.infrastructure.media.probe import probe_media
        from . import service
        from ..projects.models import StudioProject
        """,
    )
    assert guard.check_imports(tmp_path) == []


def test_allows_third_party_and_stdlib(tmp_path):
    """第三方/标准库不得误报。"""
    _write(
        tmp_path,
        "backend/studio/x.py",
        """
        import json
        import os
        from pathlib import Path
        from fastapi import APIRouter
        from sqlalchemy import select
        import imageio_ffmpeg
        """,
    )
    assert guard.check_imports(tmp_path) == []


def test_fails_closed_on_unregistered_backend_module(tmp_path):
    """未登记的 backend.* 新模块 → fail-closed 报告，并给出放行提示。"""
    _write(tmp_path, "backend/studio/x.py", "from backend.brandnew import thing\n")
    found = guard.check_imports(tmp_path)
    assert len(found) == 1
    assert "fail-closed" in found[0]["reason"]
    assert "ALLOWED_NEW_MODULES" in found[0]["reason"]


def test_reports_syntax_error_instead_of_silently_passing(tmp_path):
    """语法错误的文件必须被报告，不能被静默跳过（否则守卫可被绕过）。"""
    _write(tmp_path, "backend/studio/broken.py", "def oops(:\n")
    found = guard.check_imports(tmp_path)
    assert len(found) == 1
    assert found[0]["module"] == "<syntax error>"
    assert "无法解析" in found[0]["reason"]


def test_violations_are_deterministically_sorted(tmp_path):
    """违规清单按 (文件, 行, 模块) 确定性排序（便于复验与 diff）。"""
    _write(tmp_path, "backend/studio/z.py", "import creative\nimport director\n")
    _write(tmp_path, "backend/studio/a.py", "import authors\n")
    found = guard.check_imports(tmp_path)
    assert [(v["file"], v["line"]) for v in found] == [
        ("backend/studio/a.py", 1),
        ("backend/studio/z.py", 1),
        ("backend/studio/z.py", 2),
    ]


def test_main_returns_one_when_violations_present(tmp_path):
    """CLI 退出码：有违规必须为 1（否则 CI/门槛无法拦截）。"""
    _write(tmp_path, "backend/studio/x.py", "import creative\n")
    assert guard.main(["--root", str(tmp_path)]) == 1


def test_skips_pycache_and_hidden_dirs(tmp_path):
    """__pycache__ 等目录不参与扫描（避免噪声与重复报告）。"""
    _write(tmp_path, "backend/studio/__pycache__/cached.py", "import creative\n")
    assert guard.check_imports(tmp_path) == []
