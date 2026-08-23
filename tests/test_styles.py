"""Tests for the central application styling helpers."""

from __future__ import annotations

import re

import pytest
from PySide6.QtGui import QColor, QPalette

from core.config import AppConfig
from core.models import PrefixType
from ui.styles import (
    MUTED_TEXT_ALPHA,
    STYLESHEET,
    SecondaryLabel,
    _ensure_visible_alternate_base,
    apply_app_style,
    cell_fill_color,
    classification_color,
    readable_text_color,
)


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


# --- classification color derivation ----------------------------------------


def _light_palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Window, QColor(239, 239, 239))
    return palette


def _dark_palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
    palette.setColor(QPalette.ColorRole.Window, QColor(45, 45, 45))
    return palette


def test_classification_colors_pairwise_distinct_in_both_themes() -> None:
    for palette in (_light_palette(), _dark_palette()):
        colors = {
            prefix_type: classification_color(prefix_type, palette) for prefix_type in PrefixType
        }
        values = list(colors.values())
        for index, first in enumerate(values):
            for second in values[index + 1 :]:
                assert first.rgb() != second.rgb(), "classifications must not share a color"
        assert all(color.alpha() == 255 for color in values)


def test_classification_colors_stay_in_their_hue_families() -> None:
    for palette in (_light_palette(), _dark_palette()):
        assert 190 <= classification_color(PrefixType.STEAM, palette).hslHue() <= 240
        assert 25 <= classification_color(PrefixType.NON_STEAM, palette).hslHue() <= 60
        orphan_hue = classification_color(PrefixType.ORPHANED, palette).hslHue()
        assert orphan_hue <= 15 or orphan_hue >= 350 or orphan_hue == -1


def test_classification_colors_lift_for_dark_and_deepen_for_light() -> None:
    light = classification_color(PrefixType.STEAM, _light_palette())
    dark = classification_color(PrefixType.STEAM, _dark_palette())
    assert dark.lightness() > light.lightness()


def test_cell_fill_color_varies_lightness_and_is_stable() -> None:
    palette = _light_palette()
    base = cell_fill_color(PrefixType.STEAM, 100, palette)
    other = cell_fill_color(PrefixType.STEAM, 101, palette)
    assert base.hslHue() == other.hslHue()  # classification identity preserved
    again = cell_fill_color(PrefixType.STEAM, 100, palette)
    assert base.rgb() == again.rgb()  # stable per app_id
    variations = {cell_fill_color(PrefixType.STEAM, app_id, palette).rgb() for app_id in range(20)}
    assert len(variations) > 1


def test_readable_text_color_contrasts_with_fill() -> None:
    def luminance(color: QColor) -> float:
        return 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()

    for fill in (QColor(255, 255, 255), QColor(230, 220, 200), QColor(244, 208, 63)):
        text = readable_text_color(fill)
        assert abs(luminance(text) - luminance(fill)) > 80
    for fill in (QColor(0, 0, 0), QColor(38, 122, 206), QColor(206, 43, 38)):
        text = readable_text_color(fill)
        assert abs(luminance(text) - luminance(fill)) > 80


# --- muted secondary text ------------------------------------------------------


@pytest.fixture()
def restored_app_palette(qapp):
    """Snapshot and restore the shared application palette."""
    saved = QPalette(qapp.palette())
    yield qapp
    qapp.setPalette(saved)


def _window_text(app) -> QColor:
    return app.palette().color(QPalette.ColorRole.WindowText)


def test_secondary_label_mutes_window_text(qtbot, restored_app_palette) -> None:
    label = SecondaryLabel("caption text")
    qtbot.addWidget(label)
    muted = label.muted_color()
    source = _window_text(restored_app_palette)
    assert muted.red() == source.red()
    assert muted.green() == source.green()
    assert muted.blue() == source.blue()
    assert muted.alpha() == MUTED_TEXT_ALPHA


def test_secondary_label_follows_theme_changes(qtbot, restored_app_palette) -> None:
    app = restored_app_palette
    label = SecondaryLabel("caption text")
    qtbot.addWidget(label)

    dark = QPalette()
    dark.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
    dark.setColor(QPalette.ColorRole.WindowText, QColor(235, 235, 235))
    app.setPalette(dark)

    muted = label.muted_color()
    assert (muted.red(), muted.green(), muted.blue()) == (235, 235, 235)
    assert muted.alpha() == MUTED_TEXT_ALPHA
    # the alpha blend against the dark base must stay clearly readable
    base = app.palette().color(QPalette.ColorRole.Base)
    blended = (muted.red() * muted.alpha() + base.red() * (255 - muted.alpha())) / 255
    assert abs(blended - base.red()) > 60
    pixmap = label.grab()
    assert not pixmap.isNull()
