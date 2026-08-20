"""Enforce the no-roadmap rule: no roadmap/phase wording in committed files."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = re.compile(r"\b(?:roadmap|phase|milestone)\b", re.IGNORECASE)

SCAN_PATHS = [
    ROOT / "main.py",
    ROOT / "README.md",
    ROOT / "pyproject.toml",
    ROOT / "conftest.py",
    *sorted((ROOT / "core").rglob("*.py")),
    *sorted((ROOT / "ui").rglob("*.py")),
]


def test_no_roadmap_references_in_source() -> None:
    offenders: dict[str, list[int]] = {}
    for path in SCAN_PATHS:
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                offenders.setdefault(str(path.relative_to(ROOT)), []).append(lineno)
    assert not offenders, f"roadmap/phase wording found: {offenders}"
