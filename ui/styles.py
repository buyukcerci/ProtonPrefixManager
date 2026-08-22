"""Central UI styling: spacing constants, the application stylesheet, fonts.

The stylesheet deliberately contains no color literals. Anything that needs
a color must come from the widget palette so the app never fights a dark
system theme; row striping is enabled through setAlternatingRowColors for
the same reason.
"""

from __future__ import annotations

from PySide6.QtGui import QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

from core.config import AppConfig

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
"""


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
    """Apply the shared stylesheet and the configured font to the app."""
    app.setStyleSheet(STYLESHEET)
    _ensure_visible_alternate_base(app)
    system = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    font = QFont(system)
    font.setFamilies([system.family(), *SYSTEM_FALLBACK_FAMILIES])
    if config.font_family and config.font_family in QFontDatabase.families():
        font = QFont(config.font_family)
    if config.font_size > 0:
        font.setPointSize(config.font_size)
    app.setFont(font)
