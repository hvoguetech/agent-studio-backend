"""A/C12 AC-7 - the open-core separation guard.

The MIT core must import NO cloud/plugin package. Walk the `forge` package source and fail if
a forbidden top-level module is imported anywhere. This is the structural guarantee behind the
open-core split (EPIC D / A/C13): the cloud backends (e.g. the Inngest backend) live in a
separate proprietary package and are reached only via the `forge.execution_backends`
entry-point, never a direct import from core.
"""

from __future__ import annotations

import ast
import pathlib

FORBIDDEN = {"inngest", "forge_execution_inngest"}
CORE = pathlib.Path(__file__).resolve().parents[1] / "forge"


def _imported_roots(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:  # skip relative imports
                yield node.module.split(".")[0]


def test_core_imports_no_cloud_packages():
    offenders: list[str] = []
    for path in CORE.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - shouldn't happen in the core
            continue
        for root in _imported_roots(tree):
            if root in FORBIDDEN:
                offenders.append(f"{path.relative_to(CORE.parent)} imports {root}")
    assert not offenders, "MIT core must not import cloud packages:\n" + "\n".join(offenders)
