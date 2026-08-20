"""Enforce the Qt-free core boundary: no module under core/ may import Qt."""

from __future__ import annotations

import ast
from pathlib import Path

import core

FORBIDDEN = ("PySide6", "shiboken6")

CORE_DIR = Path(core.__file__).parent


def _imported_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_core_does_not_import_qt() -> None:
    offenders: dict[str, set[str]] = {}
    for path in CORE_DIR.rglob("*.py"):
        if not path.is_file():
            continue
        imported = _imported_names(path.read_text(encoding="utf-8"))
        bad = {name for name in imported if name.split(".")[0] in FORBIDDEN}
        if bad:
            offenders[str(path.relative_to(CORE_DIR))] = bad
    assert not offenders, f"core/ imports Qt modules: {offenders}"
