"""RETIRE-01 · 禁止新业务依赖旧业务——静态依赖守卫。

新命名空间（``backend/studio``、``backend/core``、``backend/infrastructure``）是
重构后的实现边界：它们必须与退役中的旧业务模块**零依赖**（R01/R11）。本模块
用 AST 解析（不做正则文本匹配，避免把注释/文档字符串误判为依赖）在**独立子进程**
可跑的纯函数里给出违规清单。

守卫规则（主模型冻结，见账本 RETIRE-01）：

- **禁止**：新命名空间直接 import 旧业务模块（见 ``FORBIDDEN_MODULES``）；
  该集合是保守子集——只收「只读盘点已明确属于退役范畴」的模块，不把
  「待提取基础设施」误伤（``providers``/``image_provider`` 等旧栈文件的**纯协议**
  由 JOB-01 在 ``backend/infrastructure/providers/`` 重写，不 import 旧文件）。
- **允许**（``ALLOWED_NEW_MODULES``）：新命名空间内部互相 import，以及
  ``backend.core.accounts`` / ``backend.core.db`` / ``backend.core.auth`` /
  ``backend.core.config`` 这四个基础模块（ROOT-01/DB-01 冻结的基础设施）。
- 任何其他 ``backend.*`` import 一律报告为违规（fail-closed：新增旧模块名不会被
  静默放行），并附可执行的 ``allowed_override`` 提示。

用法::

    .venv/bin/python -m scripts.check_legacy_deps            # 扫描默认根目录
    .venv/bin/python -m scripts.check_legacy_deps --json      # 机器可读

退出码：0 = 无违规；1 = 存在违规。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

# 新命名空间根（相对仓库根）
NEW_NAMESPACE_ROOTS = ("backend/studio", "backend/core", "backend/infrastructure")

# 新命名空间内部允许的包前缀
NEW_NAMESPACE_PREFIXES = ("backend.studio", "backend.core", "backend.infrastructure")

# 冻结的可复用基础设施模块（不是旧业务，允许依赖）
ALLOWED_NEW_MODULES = frozenset(
    {
        "backend.core.accounts",
        "backend.core.db",
        "backend.core.auth",
        "backend.core.config",
    }
)

# 旧业务模块（退役范畴）：新命名空间一律不得 import。
# 全部为 backend 顶层旧模块；逐项均有 ROOT-05 只读盘点依据。
FORBIDDEN_MODULES = frozenset(
    {
        "app",  # backend.app（旧路由装配）
        "authors",
        "consistency",
        "creative",
        "diagnostics",
        "director",
        "image_errors",
        "image_messages",
        "image_provider",
        "llm_stream",
        "preproduction",
        "production_nodes",
        "provider_usage",
        "providers",
        "reader_branch",
        "reference_image_model",
        "skill_runtime",
        "story_sources",
        "task_activity",
        "video_files",
        "video_storage",
        "worker",
        "workflows",
    }
)

# 旧业务专用 API 路径前缀：新命名空间不得把「旧 API」当作对外契约引用。
# 注意：`/api/studio/**` 是新契约，不在此列。
FORBIDDEN_LEGACY_PATH_PREFIXES = (
    "/api/author/",
    "/api/creative/",
    "/api/director/",
    "/api/preproduction/",
    "/api/production/",
    "/api/reader/",
)

_SKIP_DIR_NAMES = frozenset({"__pycache__", ".git", "node_modules", ".next"})


def _allowed_override(module: str) -> str:
    """给出可执行的放行提示（主模型只需改一处常量）。"""
    return (
        f"若 {module!r} 确属允许的新基础设施，请把它加入 "
        f"scripts/check_legacy_deps.py 的 ALLOWED_NEW_MODULES（并在账本记裁定）"
    )


def _classify(module: str) -> str | None:
    """返回违规原因；None 表示允许。

    判定顺序：允许的新基础设施 → 新命名空间内部 → 禁止的旧业务 → 其他 backend.*。
    """
    if module in ALLOWED_NEW_MODULES:
        return None
    if module.startswith(NEW_NAMESPACE_PREFIXES):
        return None
    # 旧模块在代码里既可写 `import creative`（裸名）也可写
    # `from backend.creative import ...`（点号名）——两者都必须命中同一规则，
    # 否则点号写法会退化成笼统的 fail-closed 提示，失去可操作的诊断信息。
    bare = module[len("backend.") :] if module.startswith("backend.") else module
    if bare in FORBIDDEN_MODULES:
        return (
            f"新命名空间不得 import 旧业务模块 {module!r}（R01/R11 退役边界）。"
            f"{_allowed_override(module)}"
        )
    if module.startswith("backend."):
        return (
            f"新命名空间 import 了未登记的新模块 {module!r}：fail-closed 拒绝，"
            f"须先由主模型确认其属于新边界。{_allowed_override(module)}"
        )
    return None


def _imported_modules(path: Path) -> list[tuple[int, str]]:
    """解析文件的 import 语句，返回 [(行号, 顶层模块名)]。

    语法错误 → 抛出 ``SyntaxError``（由调用方汇总为违规，而不是静默跳过）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            # 相对 import（level>0）在新命名空间内部，天然允许
            if node.level:
                continue
            if node.module:
                found.append((node.lineno, node.module))
    return found


def check_imports(root: Path) -> list[dict]:
    """扫描新命名空间的 import 依赖，返回违规清单（确定性排序）。"""
    violations: list[dict] = []
    for rel_root in NEW_NAMESPACE_ROOTS:
        base = root / rel_root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            try:
                imports = _imported_modules(path)
            except SyntaxError as exc:
                violations.append(
                    {
                        "file": str(path.relative_to(root)),
                        "line": exc.lineno or 0,
                        "module": "<syntax error>",
                        "reason": f"无法解析：{exc.msg}",
                    }
                )
                continue
            for lineno, module in imports:
                reason = _classify(module)
                if reason is not None:
                    violations.append(
                        {
                            "file": str(path.relative_to(root)),
                            "line": lineno,
                            "module": module,
                            "reason": reason,
                        }
                    )
    violations.sort(key=lambda v: (v["file"], v["line"], v["module"]))
    return violations


def check_legacy_api_strings(root: Path) -> list[dict]:
    """扫描新命名空间源码里的旧业务 API 路径字符串。

    只做「字符串字面量以旧前缀开头」的判定，不匹配注释/文档说明之外的松散出现。
    """
    violations: list[dict] = []
    for rel_root in NEW_NAMESPACE_ROOTS:
        base = root / rel_root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue  # 语法错误已由 check_imports 报告
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for prefix in FORBIDDEN_LEGACY_PATH_PREFIXES:
                        if node.value.startswith(prefix):
                            violations.append(
                                {
                                    "file": str(path.relative_to(root)),
                                    "line": node.lineno,
                                    "module": node.value,
                                    "reason": (
                                        f"新命名空间引用了旧业务 API 路径 "
                                        f"{node.value!r}（前缀 {prefix!r}）；"
                                        f"新契约一律走 /api/studio/**。"
                                    ),
                                }
                            )
    violations.sort(key=lambda v: (v["file"], v["line"], v["module"]))
    return violations


def collect_violations(root: Path) -> list[dict]:
    """全部守卫规则的合并违规清单。"""
    return check_imports(root) + check_legacy_api_strings(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RETIRE-01：检查新命名空间是否依赖旧业务模块/API。"
    )
    parser.add_argument(
        "--root",
        default=None,
        help="仓库根目录（默认 = 本文件所在 scripts/ 的上一级）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出违规清单")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent

    violations = collect_violations(root)
    if args.json:
        print(json.dumps(violations, ensure_ascii=False, indent=2))
    else:
        if not violations:
            print(f"RETIRE-01 通过：{root} 的新命名空间零旧业务依赖。")
        else:
            print(f"RETIRE-01 失败：发现 {len(violations)} 处旧业务依赖：")
            for v in violations:
                print(f"  {v['file']}:{v['line']} [{v['module']}] {v['reason']}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
