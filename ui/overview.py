"""Overview page: summary cards, treemap, legend, largest lists.

The page is a passive view: state flows in through update_data and user
intent flows out through signals (orphan review, prefix focus, tool
review, tool focus). All colors are derived from the current palette at
paint time so theme changes apply on the next repaint without cached state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Generic, TypeVar

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.analytics import (
    ClassificationTotals,
    ToolCategoryTotals,
    classification_totals,
    has_known_size,
    tool_category_totals,
    top_largest,
    total_size,
    use_zoom_mode,
)
from core.models import Prefix, PrefixType, ScanStatus, format_size
from core.tools import Tool, ToolCategory, tool_category
from core.treemap import CellRect, layout_sized
from ui.sanitize import sanitize_display, sanitize_tooltip
from ui.styles import (
    OVERVIEW_CARD_SPACING_PX,
    OVERVIEW_CONTENT_MARGIN_PX,
    OVERVIEW_DOT_SIZE_PX,
    OVERVIEW_LEGEND_SWATCH_PX,
    OVERVIEW_PREFIX_CARD_WIDTH_PX,
    OVERVIEW_RANK_WIDTH_PX,
    OVERVIEW_SECTION_SPACING_PX,
    OVERVIEW_TOOL_CARD_WIDTH_PX,
    OVERVIEW_TOP_MODIFIED_MIN_PX,
    OVERVIEW_TOP_NAME_MAX_PX,
    OVERVIEW_TOP_ROW_SPACING_PX,
    OVERVIEW_TOP_ROWS,
    OVERVIEW_TOP_SIZE_MIN_PX,
    OVERVIEW_TREEMAP_MAX_HEIGHT_PX,
    OVERVIEW_TREEMAP_MIN_HEIGHT_PX,
    OVERVIEW_TREEMAP_MIN_LABEL_PX,
    SecondaryLabel,
    cell_fill_color,
    classification_color,
    readable_text_color,
    tool_cell_fill_color,
)
from ui.table import format_modified

_REMAINDER_KEY = -1
_REMAINDER_LABEL = "Other disk usage"
_TREEMAP_EMPTY_TEXT = "No sized prefixes yet"
_TOOLS_TREEMAP_EMPTY_TEXT = "No sized tools yet"
_ORPHAN_CARD_TOOLTIP = "Click to review orphaned prefixes on the Prefixes page"
_ORPHAN_CARD_EMPTY_TOOLTIP = "No orphaned prefixes"
_RECLAIMABLE_CARD_TOOLTIP = "Click to review unused tools on the Tools page"
_RECLAIMABLE_CARD_EMPTY_TOOLTIP = "No unused tools"
_TOOL_SCANNING_TEXT = "Scanning..."
_TOOL_UNAVAILABLE_TEXT = "Unavailable"


def format_percent(value_bytes: int, basis_bytes: int) -> str:
    """Whole percents against a basis, with '<1%' for small nonzero shares."""
    if basis_bytes <= 0 or value_bytes <= 0:
        return "0%"
    percent = value_bytes * 100 / basis_bytes
    if percent < 1:
        return "<1%"
    return f"{round(percent)}%"


def display_name(prefix: Prefix) -> str:
    label = prefix.display_label.strip() if prefix.display_label else ""
    return label or f"Unknown (AppID: {prefix.app_id})"


class ClassificationSwatch(QWidget):
    """Small color patch painted from the palette on every repaint.

    The category may be a prefix type or a tool category; both resolve
    through the shared hue anchors at paint time.
    """

    def __init__(self, circle: bool, size_px: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._circle = circle
        self.setFixedSize(size_px, size_px)
        self._category: PrefixType | ToolCategory = PrefixType.STEAM

    def set_category(self, category: PrefixType | ToolCategory) -> None:
        self._category = category
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        color = classification_color(self._category, self.palette())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._circle:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        else:
            painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Mid), 1))
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 2, 2)
        painter.end()


def _treemap_label_color(palette: QPalette) -> QColor:
    """One label color for every treemap cell, derived once per paint.

    Per-cell luminance thresholds fire at different points across hues
    (amber reads lighter than red or blue at equal lightness), which made
    labels inconsistent on one theme. A single reference, the average
    lightness of the three classification fills, keeps them uniform.
    """
    fills = [classification_color(prefix_type, palette) for prefix_type in PrefixType]
    average_lightness = round(sum(color.lightness() for color in fills) / len(fills))
    reference = QColor.fromHsl(fills[0].hslHue(), fills[0].hslSaturation(), average_lightness)
    return readable_text_color(reference)


ItemT = TypeVar("ItemT")


class TreemapWidget(QWidget, Generic[ItemT]):
    """Hand-painted treemap over sized items plus a neutral remainder cell.

    Layout, painting, hover, tooltips, and click dispatch live in this
    base. Subclasses provide the per-item presentation through the hook
    methods (sort order, size, label, tooltip, fill, dimming, label
    color) and expose their own focus signal from _activate. All colors
    are derived from the current palette at paint time.
    """

    def __init__(self, empty_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._empty_text = empty_text
        self._items: list[ItemT] = []
        self._remainder_bytes = 0
        self._hover_key: int | None = None
        self._cells: list[tuple[int, CellRect]] = []
        self.setMinimumHeight(OVERVIEW_TREEMAP_MIN_HEIGHT_PX)
        self.setMaximumHeight(OVERVIEW_TREEMAP_MAX_HEIGHT_PX)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def set_data(self, items: Sequence[ItemT], remainder_bytes: int) -> None:
        self._items = self._sorted_items(list(items))
        self._remainder_bytes = max(0, remainder_bytes)
        self._hover_key = None
        self._cells = []
        self.update()

    def has_content(self) -> bool:
        return any(self._item_size(item) > 0 for item in self._items)

    def key_at(self, position: QPointF) -> int | None:
        """Layout key under the position, None outside all cells."""
        if not self._cells and self._items:
            self._compute_cells()
        for key, rect in self._cells:
            inside_x = rect.x <= position.x() < rect.x + rect.w
            inside_y = rect.y <= position.y() < rect.y + rect.h
            if inside_x and inside_y:
                return key
        return None

    def _sorted_items(self, items: list[ItemT]) -> list[ItemT]:
        """Display order for the items; the base never sorts on its own."""
        raise NotImplementedError

    def _item_size(self, item: ItemT) -> int:
        """Size in bytes backing the cell area."""
        raise NotImplementedError

    def _item_label(self, item: ItemT) -> str:
        """Text drawn inside the cell."""
        raise NotImplementedError

    def _item_tooltip(self, item: ItemT) -> str:
        """Tooltip text while hovering the cell; the base escapes it before display."""
        raise NotImplementedError

    def _item_fill(self, item: ItemT, palette: QPalette) -> QColor:
        """Fill color for one cell, derived from the given palette."""
        raise NotImplementedError

    def _label_color(self, palette: QPalette) -> QColor:
        """Label color for the whole treemap, derived from the given palette."""
        raise NotImplementedError

    def _item_dimmed(self, item: ItemT) -> bool:
        """Whether the cell paints dimmed; subclasses without highlights keep this."""
        return False

    def _activate(self, item: ItemT) -> None:
        """Dispatch a click on the cell; subclasses emit their focus signal."""
        raise NotImplementedError

    def _min_cell_height(self) -> float:
        """Smallest cell height that fits one text line plus the padding.

        Mirrors the label condition in paintEvent, so every cell that
        survives layout is tall enough to draw its label.
        """
        return float(self.fontMetrics().height() + 4)

    def _compute_cells(self) -> None:
        area = CellRect(x=0.0, y=0.0, w=float(self.width()), h=float(self.height()))
        items: list[tuple[int, float]] = []
        if self._remainder_bytes > 0:
            items.append((_REMAINDER_KEY, float(self._remainder_bytes)))
        items.extend(
            (index, float(self._item_size(item))) for index, item in enumerate(self._items)
        )
        result = layout_sized(items, area, self._min_cell_height(), _REMAINDER_KEY)
        self._cells = result.cells

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        if not self.has_content():
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._empty_text)
            painter.end()
            return
        self._compute_cells()
        if not self._cells:
            # Every slice folded below the minimum height and no remainder
            # cell exists, so nothing drawable is left; show the empty state.
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._empty_text)
            painter.end()
            return
        palette = self.palette()
        background = palette.color(QPalette.ColorRole.Base)
        border = palette.color(QPalette.ColorRole.Window)
        metrics = painter.fontMetrics()
        label_color = self._label_color(palette)
        painter.fillRect(self.rect(), background)
        for key, rect in self._cells:
            cell = QRectF(rect.x, rect.y, rect.w, rect.h)
            if key == _REMAINDER_KEY:
                fill = palette.color(QPalette.ColorRole.Mid)
                painter.fillRect(cell, fill)
                painter.setPen(QPen(border, 1))
                painter.drawRect(cell)
                if cell.width() >= 80 and cell.height() >= metrics.height() + 4:
                    painter.setPen(QPen(readable_text_color(fill)))
                    painter.drawText(
                        cell.adjusted(2, 2, -2, -2),
                        Qt.AlignmentFlag.AlignCenter,
                        _REMAINDER_LABEL,
                    )
                continue
            item = self._items[key]
            fill = self._item_fill(item, palette)
            painter.fillRect(cell, fill)
            dimmed = self._item_dimmed(item)
            if dimmed:
                dim = QColor(background)
                dim.setAlpha(170)
                painter.fillRect(cell, dim)
            hovered = self._hover_key == key
            frame = palette.color(QPalette.ColorRole.Highlight) if hovered else border
            painter.setPen(QPen(frame, 2 if hovered else 1))
            painter.drawRect(cell)
            if (
                not dimmed
                and cell.width() >= OVERVIEW_TREEMAP_MIN_LABEL_PX
                and (cell.height() >= metrics.height() + 4)
            ):
                painter.setPen(QPen(label_color))
                elided = metrics.elidedText(
                    self._item_label(item), Qt.TextElideMode.ElideRight, int(cell.width() - 4)
                )
                painter.drawText(
                    cell.adjusted(2, 2, -2, -2),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    elided,
                )
        painter.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        key = self.key_at(event.position())
        if key != self._hover_key:
            self._hover_key = key
            self.update()
        if key is not None and key != _REMAINDER_KEY:
            # The tooltip boundary escapes markup: Qt renders tooltips as
            # rich text when the text looks tag-like, so unescaped names
            # could spoof the tooltip content. sanitize_tooltip keeps the
            # multiline layout intact by joining lines with <br>.
            self.setToolTip(sanitize_tooltip(self._item_tooltip(self._items[key])))
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setToolTip(_REMAINDER_LABEL if key == _REMAINDER_KEY else "")
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() is Qt.MouseButton.LeftButton:
            key = self.key_at(event.position())
            if key is not None and key != _REMAINDER_KEY:
                self._activate(self._items[key])
        super().mousePressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._hover_key = None
        self.setToolTip("")
        self.update()
        super().leaveEvent(event)


class PrefixTreemapWidget(TreemapWidget[Prefix]):
    """Treemap over sized prefixes with classification fills and dimming."""

    prefix_focus_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(_TREEMAP_EMPTY_TEXT, parent)
        self._highlight_type: PrefixType | None = None

    def set_highlight_type(self, prefix_type: PrefixType | None) -> None:
        if self._highlight_type != prefix_type:
            self._highlight_type = prefix_type
            self.update()

    def _sorted_items(self, items: list[Prefix]) -> list[Prefix]:
        return sorted(items, key=lambda prefix: (-prefix.size_bytes, prefix.app_id))

    def _item_size(self, item: Prefix) -> int:
        return item.size_bytes

    def _item_label(self, item: Prefix) -> str:
        return display_name(item)

    def _item_tooltip(self, item: Prefix) -> str:
        safe_name = sanitize_display(display_name(item), limit=None)
        return f"{safe_name}\n{format_size(item.size_bytes)}\n{item.prefix_type.label()}"

    def _item_fill(self, item: Prefix, palette: QPalette) -> QColor:
        return cell_fill_color(item.prefix_type, item.app_id, palette)

    def _label_color(self, palette: QPalette) -> QColor:
        return _treemap_label_color(palette)

    def _item_dimmed(self, item: Prefix) -> bool:
        return self._highlight_type is not None and item.prefix_type is not self._highlight_type

    def _activate(self, item: Prefix) -> None:
        self.prefix_focus_requested.emit(item)


class ToolTreemapWidget(TreemapWidget[Tool]):
    """Treemap over sized tools with category fills and dimming.

    Tool categories reuse the prefix hue anchors, so cells use the same
    colors as the prefix treemap plus a per-path lightness variation. The
    used path set classifies the cells; the page passes it together with
    the data because both come from the same refresh.
    """

    tool_focus_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(_TOOLS_TREEMAP_EMPTY_TEXT, parent)
        self._used_set: set[str] = set()
        self._usage_known = True
        self._highlight_category: ToolCategory | None = None

    def set_data(
        self,
        items: Sequence[Tool],
        remainder_bytes: int,
        used_set: set[str] | None = None,
        usage_known: bool = True,
    ) -> None:
        """Refresh the cells; used_set classifies them until the next call.

        None or an omitted used set classifies every writable tool as
        reclaimable, matching an unknown usage mapping. usage_known False
        instead classifies unmatched writable tools as Unknown, matching
        a failed mapping load. Per-tool name_unverified flags fail closed
        through tool_category the same way.
        """
        self._used_set = set(used_set or ())
        self._usage_known = usage_known
        super().set_data(items, remainder_bytes)

    def set_highlight_category(self, category: ToolCategory | None) -> None:
        if self._highlight_category != category:
            self._highlight_category = category
            self.update()

    def _sorted_items(self, items: list[Tool]) -> list[Tool]:
        return sorted(items, key=lambda tool: (-tool.size_bytes, tool.name.casefold()))

    def _item_size(self, item: Tool) -> int:
        return item.size_bytes

    def _item_label(self, item: Tool) -> str:
        return item.name

    def _item_tooltip(self, item: Tool) -> str:
        safe_name = sanitize_display(item.name, limit=None)
        return f"{safe_name}\n{format_size(item.size_bytes)}"

    def _item_fill(self, item: Tool, palette: QPalette) -> QColor:
        category = tool_category(item, self._used_set, usage_known=self._usage_known)
        return tool_cell_fill_color(category, str(item.path), palette)

    def _label_color(self, palette: QPalette) -> QColor:
        return _treemap_label_color(palette)

    def _item_dimmed(self, item: Tool) -> bool:
        category = tool_category(item, self._used_set, usage_known=self._usage_known)
        return self._highlight_category is not None and category is not self._highlight_category

    def _activate(self, item: Tool) -> None:
        self.tool_focus_requested.emit(item)


class LegendRow(QFrame):
    """One legend entry; hovering it reports the classification and None on leave."""

    hovered = Signal(object)

    def __init__(
        self,
        category: PrefixType | ToolCategory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._category = category
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._swatch = ClassificationSwatch(circle=False, size_px=OVERVIEW_LEGEND_SWATCH_PX)
        self._swatch.set_category(category)
        layout.addWidget(self._swatch)
        self._category_label = QLabel(category.label(), self)
        layout.addWidget(self._category_label)
        self._size_label = QLabel("0 B", self)
        layout.addWidget(self._size_label)
        self._percent_label = QLabel("(0%)", self)
        layout.addWidget(self._percent_label)
        layout.addStretch(1)

    def set_entry(self, size_bytes: int, basis_bytes: int) -> None:
        self._size_label.setText(format_size(size_bytes))
        self._percent_label.setText(f"({format_percent(size_bytes, basis_bytes)})")

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.hovered.emit(self._category)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.hovered.emit(None)
        super().leaveEvent(event)


class LegendWidget(QWidget):
    """Classification legend whose hover state drives treemap dimming.

    Rows are built for one classification domain: prefix types by default,
    tool categories when constructed with them.
    """

    classification_hovered = Signal(object)

    def __init__(
        self,
        categories: Sequence[PrefixType | ToolCategory] = tuple(PrefixType),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._rows: dict[PrefixType | ToolCategory, LegendRow] = {}
        for category in categories:
            row = LegendRow(category, self)
            row.hovered.connect(self.classification_hovered.emit)
            self._rows[category] = row
            layout.addWidget(row)

    def set_totals(self, totals: Sequence[ClassificationTotals], basis_bytes: int) -> None:
        for entry in totals:
            self._rows[entry.prefix_type].set_entry(entry.size_bytes, basis_bytes)

    def set_tool_totals(self, totals: Sequence[ToolCategoryTotals], basis_bytes: int) -> None:
        for entry in totals:
            self._rows[entry.category].set_entry(entry.size_bytes, basis_bytes)

    def row_for(self, category: PrefixType | ToolCategory) -> LegendRow:
        return self._rows[category]


class ElidedLabel(QLabel):
    """Label that elides its text to its own width, tooltip keeps the full text.

    Hidden resizes do not deliver resize events, so the elided text is
    refreshed in paintEvent as well; the width check keeps that cheap.
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full_text = ""
        self._elided_width = -1
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.set_full_text(text)

    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self, text: str) -> None:
        """Replace the content after construction, keeping elision in sync.

        The tooltip keeps the full plain-text value while the visible text
        re-elides to the current width on the next paint. This method is
        the only supported way to change the text; writing via setText
        directly leaves the stored full text behind.
        """
        clean = sanitize_display(text, limit=None)
        self._full_text = clean
        super().setText(clean)
        self.setToolTip(sanitize_tooltip(self._full_text))
        self._elided_width = -1
        self._update_elided()

    def setText(self, text: str) -> None:  # noqa: N802 (Qt override)
        self.set_full_text(text)

    def _update_elided(self) -> None:
        width = self.contentsRect().width()
        if width <= 0 or width == self._elided_width:
            return
        self._elided_width = width
        super().setText(
            self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideMiddle, width)
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._update_elided()
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._update_elided()
        super().paintEvent(event)


class TopListRow(QFrame):
    """One ranked entry in the largest-prefixes section."""

    activated = Signal(object)

    def __init__(self, rank: int, prefix: Prefix, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prefix = prefix
        self.setObjectName("topListRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"{rank}. {sanitize_display(display_name(prefix))}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(OVERVIEW_TOP_ROW_SPACING_PX)
        self._rank_label = SecondaryLabel(f"{rank}.", self)
        self._rank_label.setFixedWidth(OVERVIEW_RANK_WIDTH_PX)
        self._rank_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._rank_label)
        self._dot = ClassificationSwatch(circle=True, size_px=OVERVIEW_DOT_SIZE_PX, parent=self)
        self._dot.set_category(prefix.prefix_type)
        layout.addWidget(self._dot)
        self._name_label = ElidedLabel(sanitize_display(display_name(prefix)), self)
        self._name_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._name_label.setFixedWidth(OVERVIEW_TOP_NAME_MAX_PX)
        layout.addWidget(self._name_label)
        self._modified_label = SecondaryLabel(format_modified(prefix.modified), self)
        self._modified_label.setFixedWidth(OVERVIEW_TOP_MODIFIED_MIN_PX)
        self._modified_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._modified_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._modified_label)
        self._size_label = QLabel(format_size(prefix.size_bytes), self)
        self._size_label.setFixedWidth(OVERVIEW_TOP_SIZE_MIN_PX)
        self._size_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._size_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._size_label)
        layout.addStretch(1)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() is Qt.MouseButton.LeftButton:
            self.activated.emit(self._prefix)
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        key = event.key()
        if key in (
            int(Qt.Key.Key_Return),
            int(Qt.Key.Key_Enter),
            int(Qt.Key.Key_Space),
        ):
            self.activated.emit(self._prefix)
            event.accept()
            return
        super().keyPressEvent(event)


class TopListWidget(QFrame):
    """Ranked list of the largest known prefixes."""

    prefix_focus_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("topListCard")
        self.setFixedWidth(OVERVIEW_PREFIX_CARD_WIDTH_PX)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(2)
        self._header = self._create_header()
        self._layout.addWidget(self._header)

    def _create_header(self) -> QFrame:
        header = QFrame(self)
        header.setObjectName("topListHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(OVERVIEW_TOP_ROW_SPACING_PX)

        rank_header = SecondaryLabel("#", header)
        rank_header.setFixedWidth(OVERVIEW_RANK_WIDTH_PX)
        rank_header.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        font = QFont(rank_header.font())
        font.setBold(True)
        rank_header.setFont(font)
        layout.addWidget(rank_header)

        swatch_spacer = QWidget(header)
        swatch_spacer.setFixedWidth(OVERVIEW_DOT_SIZE_PX)
        layout.addWidget(swatch_spacer)

        name_header = SecondaryLabel("Name", header)
        name_header.setFixedWidth(OVERVIEW_TOP_NAME_MAX_PX)
        name_header.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        name_header.setFont(font)
        layout.addWidget(name_header)

        modified_header = SecondaryLabel("Last Modified", header)
        modified_header.setFixedWidth(OVERVIEW_TOP_MODIFIED_MIN_PX)
        modified_header.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        modified_header.setFont(font)
        layout.addWidget(modified_header)

        size_header = SecondaryLabel("Size", header)
        size_header.setFixedWidth(OVERVIEW_TOP_SIZE_MIN_PX)
        size_header.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        size_header.setFont(font)
        layout.addWidget(size_header)

        layout.addStretch(1)
        return header

    def set_prefixes(self, prefixes: Sequence[Prefix]) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(1)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for rank, prefix in enumerate(prefixes, start=1):
            row = TopListRow(rank, prefix, self)
            row.activated.connect(self.prefix_focus_requested.emit)
            self._layout.addWidget(row)

    def rows(self) -> list[TopListRow]:
        result: list[TopListRow] = []
        for index in range(self._layout.count()):
            item = self._layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, TopListRow):
                result.append(widget)
        return result


def tool_used_set(used: set[str] | Mapping[str, list[int]] | None) -> set[str]:
    """Normalize the used mapping or set to a set of tool paths."""
    if not used:
        return set()
    if isinstance(used, Mapping):
        return set(used.keys())
    return set(used)


def tool_size_text(tool: Tool, pending: set[str], failed: set[str]) -> str:
    """Size label for one tool, matching the tools table states."""
    key = str(tool.path)
    if key in pending:
        return _TOOL_SCANNING_TEXT
    if key in failed:
        return _TOOL_UNAVAILABLE_TEXT
    return format_size(tool.size_bytes)


def tool_top_largest(tools: Sequence[Tool], limit: int) -> list[Tool]:
    """Largest tools by size, ties broken on name for stable order."""
    ordered = sorted(tools, key=lambda tool: (-tool.size_bytes, tool.name.casefold()))
    return list(ordered[:limit])


def _tool_scan_status_text(tools: Sequence[Tool], pending: set[str], failed: set[str]) -> str:
    total = len(tools)
    if total == 0:
        return "No Proton tools found"
    sized = total - len(pending)
    text = f"{sized} of {total} tools sized"
    if pending:
        text += f", {len(pending)} scanning"
    if failed:
        text += f", {len(failed)} unavailable"
    return text


class ToolTopRow(QFrame):
    """One ranked entry in the largest-tools section."""

    activated = Signal(object)

    def __init__(
        self,
        rank: int,
        tool: Tool,
        size_text: str,
        used_set: set[str] | None = None,
        usage_known: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tool = tool
        self.setObjectName("topListRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"{rank}. {sanitize_display(tool.name)}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(OVERVIEW_TOP_ROW_SPACING_PX)
        self._rank_label = SecondaryLabel(f"{rank}.", self)
        self._rank_label.setFixedWidth(OVERVIEW_RANK_WIDTH_PX)
        self._rank_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._rank_label)
        self._dot = ClassificationSwatch(circle=True, size_px=OVERVIEW_DOT_SIZE_PX, parent=self)
        category = tool_category(tool, used_set or set(), usage_known=usage_known)
        self._dot.set_category(category)
        layout.addWidget(self._dot)
        self._name_label = ElidedLabel(sanitize_display(tool.name), self)
        self._name_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._name_label.setFixedWidth(OVERVIEW_TOP_NAME_MAX_PX)
        layout.addWidget(self._name_label)
        self._size_label = QLabel(size_text, self)
        self._size_label.setFixedWidth(OVERVIEW_TOP_SIZE_MIN_PX)
        self._size_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._size_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._size_label)
        layout.addStretch(1)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() is Qt.MouseButton.LeftButton:
            self.activated.emit(self._tool)
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        key = event.key()
        if key in (
            int(Qt.Key.Key_Return),
            int(Qt.Key.Key_Enter),
            int(Qt.Key.Key_Space),
        ):
            self.activated.emit(self._tool)
            event.accept()
            return
        super().keyPressEvent(event)


class ToolTopListWidget(QFrame):
    """Ranked list of the largest tools with table-matched size states."""

    tool_focus_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("topListCard")
        self.setFixedWidth(OVERVIEW_TOOL_CARD_WIDTH_PX)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(2)
        self._header = self._create_header()
        self._layout.addWidget(self._header)

    def _create_header(self) -> QFrame:
        header = QFrame(self)
        header.setObjectName("topListHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(OVERVIEW_TOP_ROW_SPACING_PX)

        rank_header = SecondaryLabel("#", header)
        rank_header.setFixedWidth(OVERVIEW_RANK_WIDTH_PX)
        rank_header.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        font = QFont(rank_header.font())
        font.setBold(True)
        rank_header.setFont(font)
        layout.addWidget(rank_header)

        swatch_spacer = QWidget(header)
        swatch_spacer.setFixedWidth(OVERVIEW_DOT_SIZE_PX)
        layout.addWidget(swatch_spacer)

        name_header = SecondaryLabel("Name", header)
        name_header.setFixedWidth(OVERVIEW_TOP_NAME_MAX_PX)
        name_header.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        name_header.setFont(font)
        layout.addWidget(name_header)

        size_header = SecondaryLabel("Size", header)
        size_header.setFixedWidth(OVERVIEW_TOP_SIZE_MIN_PX)
        size_header.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        size_header.setFont(font)
        layout.addWidget(size_header)

        layout.addStretch(1)
        return header

    def set_tools(
        self,
        tools: Sequence[Tool],
        pending: set[str],
        failed: set[str],
        used_set: set[str] | None = None,
        usage_known: bool = True,
    ) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(1)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for rank, tool in enumerate(tools, start=1):
            row = ToolTopRow(
                rank, tool, tool_size_text(tool, pending, failed), used_set, usage_known, self
            )
            row.activated.connect(self.tool_focus_requested.emit)
            self._layout.addWidget(row)

    def rows(self) -> list[ToolTopRow]:
        result: list[ToolTopRow] = []
        for index in range(self._layout.count()):
            item = self._layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, ToolTopRow):
                result.append(widget)
        return result


class SummaryCard(QFrame):
    """Flat bordered card; the dominant variant gets a stronger frame."""

    clicked = Signal()

    def __init__(
        self,
        title: str,
        *,
        dominant: bool = False,
        clickable: bool = False,
        tooltip: str | None = None,
        disabled_tooltip: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("overviewCard")
        self.setProperty("dominant", dominant)
        self._clickable = clickable
        self._title = title
        if clickable:
            if tooltip is None or disabled_tooltip is None:
                raise ValueError("clickable cards need tooltip and disabled_tooltip")
            self._enabled_tooltip = tooltip
            self._disabled_tooltip = disabled_tooltip
        else:
            self._enabled_tooltip = tooltip or ""
            self._disabled_tooltip = disabled_tooltip or ""
        self._clickable_enabled = True
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setAccessibleName(title)
            self.setToolTip(self._enabled_tooltip)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        title_label = SecondaryLabel(title, self)
        title_font = QFont(title_label.font())
        title_font.setBold(True)
        if title_font.pointSize() > 0:
            title_font.setPointSize(max(8, title_font.pointSize() - 1))
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        self._value_label = QLabel("-", self)
        value_font = QFont(self._value_label.font())
        value_font.setBold(dominant)
        if value_font.pointSize() > 0:
            value_font.setPointSize(value_font.pointSize() + (3 if dominant else 2))
        self._value_label.setFont(value_font)
        self._value_label.setTextFormat(Qt.TextFormat.PlainText)
        self._value_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._value_label.setMinimumWidth(0)
        layout.addWidget(self._value_label)
        self._detail_label = SecondaryLabel("", self)
        self._detail_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._detail_label.setMinimumWidth(0)
        layout.addWidget(self._detail_label)

    def set_clickable_enabled(self, enabled: bool) -> None:
        """Swap cursor and tooltip when the card has nothing to review."""
        if not self._clickable:
            return
        self._clickable_enabled = enabled
        if enabled:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(self._enabled_tooltip)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.setToolTip(self._disabled_tooltip)
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if self.hasFocus():
                self.clearFocus()

    def is_clickable_enabled(self) -> bool:
        return self._clickable_enabled

    def set_values(self, value: str, detail: str) -> None:
        self._value_label.setText(value)
        self._detail_label.setText(detail)

    def value_text(self) -> str:
        return self._value_label.text()

    def detail_text(self) -> str:
        return self._detail_label.text()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if (
            self._clickable
            and self._clickable_enabled
            and event.button() is Qt.MouseButton.LeftButton
        ):
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        key = event.key()
        if (
            self._clickable
            and self._clickable_enabled
            and key
            in (
                int(Qt.Key.Key_Return),
                int(Qt.Key.Key_Enter),
                int(Qt.Key.Key_Space),
            )
        ):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def _scan_status_text(prefixes: Sequence[Prefix]) -> str:
    total = len(prefixes)
    counts = {status: 0 for status in ScanStatus}
    for prefix in prefixes:
        counts[prefix.scan_status] += 1
    text = f"{counts[ScanStatus.SCANNED]} of {total} prefixes sized"
    if counts[ScanStatus.SCANNING]:
        text += f", {counts[ScanStatus.SCANNING]} scanning"
    if counts[ScanStatus.FAILED]:
        text += f", {counts[ScanStatus.FAILED]} failed"
    if counts[ScanStatus.NOT_SCANNED]:
        text += f", {counts[ScanStatus.NOT_SCANNED]} pending"
    return text


def _section_header(text: str, parent: QWidget) -> SecondaryLabel:
    label = SecondaryLabel(text, parent)
    font = QFont(label.font())
    font.setBold(True)
    if font.pointSize() > 0:
        font.setPointSize(font.pointSize() + 2)
    label.setFont(font)
    return label


class OverviewPage(QScrollArea):
    """Disk usage at a glance: cards, treemap, legend, and largest lists."""

    orphan_review_requested = Signal()
    prefix_focus_requested = Signal(object)
    tool_review_requested = Signal()
    tool_focus_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._orphan_count = 0
        self._reclaimable_count = 0
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(
            OVERVIEW_CONTENT_MARGIN_PX,
            OVERVIEW_CONTENT_MARGIN_PX,
            OVERVIEW_CONTENT_MARGIN_PX,
            OVERVIEW_CONTENT_MARGIN_PX,
        )
        layout.setSpacing(OVERVIEW_SECTION_SPACING_PX)

        layout.addWidget(_section_header("Prefixes", content))

        cards_row = QHBoxLayout()
        cards_row.setSpacing(OVERVIEW_CARD_SPACING_PX)
        self._prefixes_card = SummaryCard("PREFIXES", parent=self)
        self._orphan_card = SummaryCard(
            "ORPHANED",
            dominant=True,
            clickable=True,
            tooltip=_ORPHAN_CARD_TOOLTIP,
            disabled_tooltip=_ORPHAN_CARD_EMPTY_TOOLTIP,
            parent=self,
        )
        self._disk_card = SummaryCard("PREFIXES DISK USAGE", parent=self)
        self._orphan_card.clicked.connect(self._on_orphan_card_clicked)
        cards_row.addWidget(self._prefixes_card, stretch=1)
        cards_row.addWidget(self._orphan_card, stretch=1)
        cards_row.addWidget(self._disk_card, stretch=1)
        layout.addLayout(cards_row)

        self._caption_label = SecondaryLabel("", content)
        self._caption_label.setWordWrap(True)
        layout.addWidget(self._caption_label)

        self._treemap = PrefixTreemapWidget(content)
        self._treemap.prefix_focus_requested.connect(self.prefix_focus_requested.emit)
        layout.addWidget(self._treemap, stretch=1)

        self._legend = LegendWidget(parent=content)
        self._legend.classification_hovered.connect(self._treemap.set_highlight_type)
        layout.addWidget(self._legend)

        prefix_heading = QLabel("Largest prefixes", content)
        prefix_heading_font = QFont(prefix_heading.font())
        prefix_heading_font.setBold(True)
        prefix_heading.setFont(prefix_heading_font)
        layout.addWidget(prefix_heading)

        self._top_list = TopListWidget(content)
        self._top_list.prefix_focus_requested.connect(self.prefix_focus_requested.emit)
        layout.addWidget(self._top_list)

        self._status_label = SecondaryLabel("", content)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        separator = QFrame(content)
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

        layout.addWidget(_section_header("Tools", content))

        tools_cards_row = QHBoxLayout()
        tools_cards_row.setSpacing(OVERVIEW_CARD_SPACING_PX)
        self._tools_card = SummaryCard("TOOLS", parent=self)
        self._reclaimable_card = SummaryCard(
            "RECLAIMABLE",
            dominant=True,
            clickable=True,
            tooltip=_RECLAIMABLE_CARD_TOOLTIP,
            disabled_tooltip=_RECLAIMABLE_CARD_EMPTY_TOOLTIP,
            parent=self,
        )
        self._tools_disk_card = SummaryCard("TOOLS DISK USAGE", parent=self)
        self._reclaimable_card.clicked.connect(self._on_reclaimable_card_clicked)
        tools_cards_row.addWidget(self._tools_card, stretch=1)
        tools_cards_row.addWidget(self._reclaimable_card, stretch=1)
        tools_cards_row.addWidget(self._tools_disk_card, stretch=1)
        layout.addLayout(tools_cards_row)

        self._tools_caption_label = SecondaryLabel("", content)
        self._tools_caption_label.setWordWrap(True)
        layout.addWidget(self._tools_caption_label)

        self._tools_treemap = ToolTreemapWidget(content)
        self._tools_treemap.tool_focus_requested.connect(self.tool_focus_requested.emit)
        layout.addWidget(self._tools_treemap, stretch=1)

        self._tools_legend = LegendWidget(tuple(ToolCategory), content)
        self._tools_legend.classification_hovered.connect(
            self._tools_treemap.set_highlight_category
        )
        layout.addWidget(self._tools_legend)

        tools_heading = QLabel("Largest tools", content)
        tools_heading_font = QFont(tools_heading.font())
        tools_heading_font.setBold(True)
        tools_heading.setFont(tools_heading_font)
        layout.addWidget(tools_heading)

        self._tools_top_list = ToolTopListWidget(content)
        self._tools_top_list.tool_focus_requested.connect(self.tool_focus_requested.emit)
        layout.addWidget(self._tools_top_list)

        self._tools_status_label = SecondaryLabel("", content)
        self._tools_status_label.setWordWrap(True)
        layout.addWidget(self._tools_status_label)

        self.setWidget(content)

    def _on_orphan_card_clicked(self) -> None:
        if self._orphan_count > 0:
            self.orphan_review_requested.emit()

    def _on_reclaimable_card_clicked(self) -> None:
        if self._reclaimable_count > 0:
            self.tool_review_requested.emit()

    def update_data(
        self,
        prefixes: Sequence[Prefix],
        capacity_bytes: int | None,
        tools: Sequence[Tool] = (),
        used: set[str] | Mapping[str, list[int]] | None = None,
        pending: set[str] | None = None,
        failed: set[str] | None = None,
        usage_known: bool = True,
    ) -> None:
        known = [prefix for prefix in prefixes if has_known_size(prefix)]
        total = total_size(known)
        totals = classification_totals(known)
        self._orphan_count = sum(1 for prefix in prefixes if prefix.is_orphan)
        zoom = use_zoom_mode(total, capacity_bytes)

        self._prefixes_card.set_values(str(len(prefixes)), format_size(total))
        orphan_totals = {entry.prefix_type: entry for entry in totals}
        self._orphan_card.set_values(
            str(self._orphan_count),
            format_size(orphan_totals[PrefixType.ORPHANED].size_bytes),
        )
        self._orphan_card.set_clickable_enabled(self._orphan_count > 0)
        if capacity_bytes is not None:
            self._disk_card.set_values(
                f"{format_size(total)} of {format_size(capacity_bytes)}",
                f"{format_percent(total, capacity_bytes)} of disk",
            )
        else:
            self._disk_card.set_values(format_size(total), "disk size unknown")

        if not zoom and capacity_bytes is not None:
            self._caption_label.setText(
                f"Areas sized against the full disk ({format_size(capacity_bytes)})"
            )
        elif capacity_bytes is not None:
            self._caption_label.setText(
                f"Zoomed: prefixes fill {format_percent(total, capacity_bytes)} of the disk, "
                f"areas sized against {format_size(total)}"
            )
        else:
            self._caption_label.setText(
                f"Areas sized against total prefix size ({format_size(total)}); disk size unknown"
            )

        remainder = 0
        basis = total
        if not zoom and capacity_bytes is not None:
            basis = capacity_bytes
            if capacity_bytes > total:
                remainder = capacity_bytes - total
        self._treemap.set_data(known, remainder)
        self._legend.set_totals(totals, basis)
        self._top_list.set_prefixes(top_largest(known, OVERVIEW_TOP_ROWS))
        self._status_label.setText(_scan_status_text(prefixes))
        self._update_tools_section(list(tools), used, pending, failed, capacity_bytes, usage_known)

    def _update_tools_section(
        self,
        tools: list[Tool],
        used: set[str] | Mapping[str, list[int]] | None,
        pending: set[str] | None,
        failed: set[str] | None,
        capacity_bytes: int | None = None,
        usage_known: bool = True,
    ) -> None:
        used_set = tool_used_set(used)
        pending_set = set(pending or ())
        failed_set = set(failed or ())
        total_bytes = sum(tool.size_bytes for tool in tools)
        totals = tool_category_totals(tools, used_set, usage_known=usage_known)
        by_category = {entry.category: entry for entry in totals}
        reclaimable_totals = by_category[ToolCategory.RECLAIMABLE]
        self._reclaimable_count = reclaimable_totals.count

        self._tools_card.set_values(str(len(tools)), format_size(total_bytes))
        self._reclaimable_card.set_values(
            format_size(reclaimable_totals.size_bytes),
            f"{reclaimable_totals.count} unused tools",
        )
        self._reclaimable_card.set_clickable_enabled(reclaimable_totals.count > 0)
        if capacity_bytes is not None:
            self._tools_disk_card.set_values(
                f"{format_size(total_bytes)} of {format_size(capacity_bytes)}",
                f"{format_percent(total_bytes, capacity_bytes)} of disk",
            )
        else:
            self._tools_disk_card.set_values(format_size(total_bytes), "disk size unknown")
        top_tools = tool_top_largest(tools, OVERVIEW_TOP_ROWS)
        self._tools_top_list.set_tools(top_tools, pending_set, failed_set, used_set, usage_known)
        self._tools_status_label.setText(_tool_scan_status_text(tools, pending_set, failed_set))

        sized_tools = [tool for tool in tools if tool.size_bytes > 0]
        # Tools zoom independently of prefixes: their own total decides
        # against the same disk capacity, so a tools treemap can show a
        # remainder while the prefix treemap zooms, and the other way around.
        tools_zoom = use_zoom_mode(total_bytes, capacity_bytes)
        tools_remainder = 0
        if not tools_zoom and capacity_bytes is not None and capacity_bytes > total_bytes:
            tools_remainder = capacity_bytes - total_bytes
        if not tools_zoom and capacity_bytes is not None:
            self._tools_caption_label.setText(
                f"Areas sized against the full disk ({format_size(capacity_bytes)})"
            )
        elif capacity_bytes is not None:
            self._tools_caption_label.setText(
                f"Zoomed: tools fill {format_percent(total_bytes, capacity_bytes)} of the disk, "
                f"areas sized against {format_size(total_bytes)}"
            )
        else:
            self._tools_caption_label.setText(
                f"Areas sized against total tool size ({format_size(total_bytes)}); "
                "disk size unknown"
            )
        basis = total_bytes
        if not tools_zoom and capacity_bytes is not None:
            basis = capacity_bytes
        self._tools_treemap.set_data(sized_tools, tools_remainder, used_set, usage_known)
        self._tools_legend.set_tool_totals(totals, basis)

    def update_tools(
        self,
        tools: Sequence[Tool],
        used: set[str] | Mapping[str, list[int]] | None,
        pending: set[str] | None,
        failed: set[str] | None,
        capacity_bytes: int | None = None,
        usage_known: bool = True,
    ) -> None:
        """Refresh only the tools section, without rebuilding prefix visuals."""
        self._update_tools_section(list(tools), used, pending, failed, capacity_bytes, usage_known)

    @property
    def treemap(self) -> PrefixTreemapWidget:
        return self._treemap

    @property
    def tools_treemap(self) -> ToolTreemapWidget:
        return self._tools_treemap

    @property
    def legend(self) -> LegendWidget:
        return self._legend

    @property
    def top_list(self) -> TopListWidget:
        return self._top_list

    @property
    def caption_label(self) -> QLabel:
        return self._caption_label

    @property
    def tools_caption_label(self) -> QLabel:
        return self._tools_caption_label

    @property
    def status_label(self) -> QLabel:
        return self._status_label

    @property
    def prefixes_card(self) -> SummaryCard:
        return self._prefixes_card

    @property
    def orphan_card(self) -> SummaryCard:
        return self._orphan_card

    @property
    def disk_card(self) -> SummaryCard:
        return self._disk_card

    @property
    def tools_card(self) -> SummaryCard:
        return self._tools_card

    @property
    def reclaimable_card(self) -> SummaryCard:
        return self._reclaimable_card

    @property
    def tools_disk_card(self) -> SummaryCard:
        return self._tools_disk_card

    @property
    def tools_top_list(self) -> ToolTopListWidget:
        return self._tools_top_list

    @property
    def tools_legend(self) -> LegendWidget:
        return self._tools_legend

    @property
    def tools_status_label(self) -> QLabel:
        return self._tools_status_label
