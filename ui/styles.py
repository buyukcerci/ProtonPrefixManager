"""Central UI styling: spacing constants, the application stylesheet, fonts.

The stylesheet deliberately contains no color literals. Anything that needs
a color must come from the widget palette so the app never fights a dark
system theme; row striping is enabled through setAlternatingRowColors for
the same reason.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QLabel

from core.config import AppConfig
from core.models import PrefixType

SYSTEM_FALLBACK_FAMILIES = ("Noto Sans", "Cantarell", "Fira Sans", "DejaVu Sans")

ITEM_PADDING_PX = 4
HEADER_PADDING_PX = 4
HEADER_H_PADDING_PX = 8
HEADER_HEIGHT_RATIO = 1.6
MIN_HEADER_SECTION_PX = 28
MESSAGE_ICON_SIZE_PX = 64
MESSAGE_LAYOUT_SPACING_PX = 16
MESSAGE_PAGE_MARGIN_PX = 32
SEARCH_DEBOUNCE_MS = 300

CHECK_WIDTH_PX = 28
OPEN_WIDTH_PX = 32
APP_ID_MIN_PX = 70
APP_ID_DEFAULT_PX = 90
NAME_MIN_PX = 120
NAME_DEFAULT_PX = 240
SIZE_MIN_PX = 90
SIZE_DEFAULT_PX = 110
MODIFIED_MIN_PX = 100
MODIFIED_DEFAULT_PX = 140
HIGHLIGHT_DURATION_MS = 2000

OVERVIEW_CARD_SPACING_PX = 12
OVERVIEW_SECTION_SPACING_PX = 16
OVERVIEW_CONTENT_MARGIN_PX = 12
OVERVIEW_TREEMAP_MIN_HEIGHT_PX = 220
OVERVIEW_TREEMAP_MAX_HEIGHT_PX = 400
OVERVIEW_TREEMAP_MIN_LABEL_PX = 34
OVERVIEW_LEGEND_SWATCH_PX = 12
OVERVIEW_DOT_SIZE_PX = 10
OVERVIEW_TOP_ROWS = 5

STYLESHEET = f"""
QTableView::item {{
    padding: {ITEM_PADDING_PX}px;
}}
QHeaderView::section {{
    padding: {HEADER_PADDING_PX}px {HEADER_H_PADDING_PX}px;
    font-weight: 600;
}}
QFrame#searchFrame {{
    border: 1px solid palette(mid);
    border-radius: 4px;
}}
QFrame#searchFrame QComboBox {{
    border: none;
    background: transparent;
    padding: 2px 4px;
}}
QFrame#searchFrame QLineEdit {{
    border: none;
    background: transparent;
    padding: 2px 4px;
}}
QFrame#overviewCard {{
    border: 1px solid palette(mid);
    border-radius: 4px;
    background: palette(base);
}}
QFrame#overviewCard[dominant="true"] {{
    border: 2px solid palette(mid);
}}
"""

MUTED_TEXT_ALPHA = 160

# Hue anchors for the three classifications: Steam is blue, Non-Steam is
# amber/orange, Orphaned is red. Saturation and lightness are derived from
# the theme palette at runtime so dark themes get lifted shades and light
# themes deeper shades. These are chart data colors; every other surface
# stays palette-relative.
_HUE_ANCHORS: dict[PrefixType, int] = {
    PrefixType.STEAM: 210,
    PrefixType.NON_STEAM: 42,
    PrefixType.ORPHANED: 2,
}


def classification_color(prefix_type: PrefixType, palette: QPalette) -> QColor:
    """Runtime-derived fill color for one classification under the given palette."""
    hue = _HUE_ANCHORS[prefix_type]
    dark_theme = palette.color(QPalette.ColorRole.Base).lightness() <= 127
    if dark_theme:
        return QColor.fromHsl(hue, 140, 165)
    return QColor.fromHsl(hue, 170, 125)


def cell_fill_color(prefix_type: PrefixType, app_id: int, palette: QPalette) -> QColor:
    """Classification color with a stable lightness variation keyed by app_id.

    The variation keeps same-colored neighbors distinguishable without
    changing the classification identity of the hue.
    """
    color = classification_color(prefix_type, palette)
    offset = (app_id % 5) - 2
    lightness = min(230, max(30, color.lightness() + offset * 6))
    return QColor.fromHsl(color.hslHue(), color.hslSaturation(), lightness)


def readable_text_color(fill: QColor) -> QColor:
    """Black or white, whichever contrasts with the given fill."""
    luminance = (0.299 * fill.red() + 0.587 * fill.green() + 0.114 * fill.blue()) / 255
    return QColor(0, 0, 0) if luminance > 0.55 else QColor(255, 255, 255)


def _ensure_visible_alternate_base(app: QApplication) -> None:
    """Derive AlternateBase from Base when the style leaves them identical.

    Some styles ship equal Base and AlternateBase colors, which makes row
    striping invisible. The derived shade stays palette-relative so it works
    on light and dark themes alike.
    """
    palette = app.palette()
    base = palette.color(QPalette.ColorRole.Base)
    alternate = palette.color(QPalette.ColorRole.AlternateBase)
    if base == alternate:
        shifted = base.darker(106) if base.lightness() > 127 else base.lighter(140)
        palette.setColor(QPalette.ColorRole.AlternateBase, shifted)
        app.setPalette(palette)


def apply_app_style(app: QApplication, config: AppConfig) -> None:
    """Apply the configured font and the shared stylesheet to the app.

    The font is installed before the stylesheet on purpose: stylesheet
    polish resolves widget fonts, so the repolish triggered by
    setStyleSheet must run under the final font. Setting the stylesheet
    first leaves running widgets rendering the previous size until some
    later repolish.
    """
    system = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    font = QFont(system)
    font.setFamilies([system.family(), *SYSTEM_FALLBACK_FAMILIES])
    if config.font_family and config.font_family in QFontDatabase.families():
        font = QFont(config.font_family)
    if config.font_size > 0:
        font.setPointSize(config.font_size)
    app.setFont(font)
    app.setStyleSheet(STYLESHEET)
    _ensure_visible_alternate_base(app)


class SecondaryLabel(QLabel):
    """Quiet text: WindowText at reduced opacity, derived at paint time.

    Muting via palette(mid) reads poorly on dark themes where mid sits
    close to the window color, so the muted shade derives from the
    application's WindowText instead. The color is recomputed on every
    repaint straight from the current application palette, so theme
    changes mid-session apply without cached state.
    """

    def muted_color(self) -> QColor:
        app = QApplication.instance()
        source = app.palette() if isinstance(app, QApplication) else self.palette()
        muted = QColor(source.color(QPalette.ColorRole.WindowText))
        muted.setAlpha(MUTED_TEXT_ALPHA)
        return muted

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setPen(self.muted_color())
        flags = int(self.alignment())
        if self.wordWrap():
            flags |= int(Qt.TextFlag.TextWordWrap)
        painter.drawText(self.rect(), flags, self.text())
        painter.end()
