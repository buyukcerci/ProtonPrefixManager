"""Smoke test for the Qt-free core package."""

from __future__ import annotations

import core


def test_core_version_is_string() -> None:
    assert isinstance(core.__version__, str)
    assert core.__version__
