"""Tests for the central application styling helpers."""

from __future__ import annotations

import re

import pytest

from core.config import AppConfig
from ui.styles import STYLESHEET, _ensure_visible_alternate_base, apply_app_style


@pytest.fixture()
def restored_app_state(qapp):
    """Snapshot and restore the shared application font and stylesheet."""
    saved_font = qapp.font()
    saved_sheet = qapp.styleSheet()
    yield qapp
    qapp.setFont(saved_font)
    qapp.setStyleSheet(saved_sheet)


HEX_COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def test_stylesheet_is_nonempty_and_color_free() -> None:
    assert STYLESHEET.strip()
    assert not HEX_COLOR_PATTERN.search(STYLESHEET), "QSS must not hardcode colors"


def test_apply_app_style_sets_application_stylesheet(restored_app_state) -> None:
    app = restored_app_state
    apply_app_style(app, AppConfig())
    assert app.styleSheet() == STYLESHEET
    assert app.styleSheet().strip() != ""


def test_font_size_applied_from_config(restored_app_state) -> None:
    from PySide6.QtGui import QFontDatabase

    app = restored_app_state
    installed = QFontDatabase.families()
    family = installed[0] if installed else None
    config = AppConfig(font_family=family, font_size=13)
    apply_app_style(app, config)
    font = app.font()
    assert font.pointSize() == 13
    if family is not None:
        assert font.family() == family


def test_unknown_font_family_falls_back_silently(restored_app_state) -> None:
    app = restored_app_state
    before = app.font()
    config = AppConfig(font_family="Definitely-Not-Installed-Font-1234", font_size=12)
    apply_app_style(app, config)
    assert app.font().pointSize() == 12
    assert app.font().family() in (before.family(),)


def test_default_config_resolves_system_general_font(restored_app_state) -> None:
    from PySide6.QtGui import QFontDatabase

    app = restored_app_state
    apply_app_style(app, AppConfig())
    expected = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    assert app.font().family() == expected
    assert app.font().families()[0] == expected


@pytest.mark.parametrize(
    "literal", ["background-color: #ffffff", "color: #000;", "border: 1px solid #abc"]
)
def test_hex_colors_would_be_detected(literal: str) -> None:
    assert HEX_COLOR_PATTERN.search(literal)


def test_alternate_base_untouched_when_already_distinct(restored_app_state) -> None:
    from PySide6.QtGui import QPalette

    app = restored_app_state
    palette = app.palette()
    base = palette.color(QPalette.ColorRole.Base)
    if palette.color(QPalette.ColorRole.AlternateBase) == base:
        palette.setColor(QPalette.ColorRole.AlternateBase, base.lighter(120))
        app.setPalette(palette)

    saved_alternate = app.palette().color(QPalette.ColorRole.AlternateBase)
    _ensure_visible_alternate_base(app)
    assert app.palette().color(QPalette.ColorRole.AlternateBase) == saved_alternate
