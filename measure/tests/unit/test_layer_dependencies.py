"""硬边界 1(CLAUDE.md):依赖方向单向 L4 report → L3 diagnosis → L2 engine → L1 adapters,只许上层依赖下层。

扫全部源码的 import 语句,下层 import 上层即失败。例外只许写进白名单并注明出处——白名单变长
本身就是评审信号。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAYER = {"adapters": 1, "engine": 2, "diagnosis": 3, "report": 4}
# 唯一许可的「下层用上层」:适配器可用 engine.models 里的契约类型(架构书附录 v0.4.3)
ALLOWED = {("adapters", "engine.models")}


def _imports(path: Path) -> Iterator[tuple[str, int]]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module, node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno


def _layered_imports() -> Iterator[tuple[str, str, int, str]]:
    """(所在包, 被 import 的模块, 行号, 文件相对路径),只列指向四层包的 import。"""
    for package in LAYER:
        for path in sorted((ROOT / package).rglob("*.py")):
            for module, lineno in _imports(path):
                if module.split(".")[0] in LAYER:
                    yield package, module, lineno, path.relative_to(ROOT).as_posix()


def test_lower_layers_never_import_upper_layers():
    scanned = list(_layered_imports())
    # 扫描器自检:白名单里的例外确实被扫到了,否则「零违规」可能只是没扫到东西
    assert any((package, module) in ALLOWED for package, module, _, _ in scanned)
    violations = [
        f"{where}:{lineno} → {module}"
        for package, module, lineno, where in scanned
        if LAYER[module.split(".")[0]] > LAYER[package] and (package, module) not in ALLOWED
    ]
    assert violations == []


def test_access_audit_stands_alone():
    """唯一例外 diagnosis/access_audit.py 自带爬取器、可独立运行:不得依赖 L1 / L2。"""
    path = ROOT / "diagnosis" / "access_audit.py"
    lower = [module for module, _ in _imports(path) if module.split(".")[0] in ("adapters", "engine")]
    assert lower == []
