"""Overview page: summary cards, disk-context treemap, legend, largest list.

The page is a passive view: state flows in through update_data and user
intent flows out through signals (orphan review, prefix focus). All
colors are derived from the current palette at paint time so theme
changes apply on the next repaint without cached state.
"""

from __future__ import annotations

from collections.abc import Sequence

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
    classification_totals,
    has_known_size,
    top_largest,
    total_size,
    use_zoom_mode,
)
from core.models import Prefix, PrefixType, ScanStatus, format_size
from core.treemap import CellRect, layout
from ui.styles import (
    OVERVIEW_CARD_SPACING_PX,
    OVERVIEW_CONTENT_MARGIN_PX,
    OVERVIEW_DOT_SIZE_PX,
    OVERVIEW_LEGEND_SWATCH_PX,
    OVERVIEW_SECTION_SPACING_PX,
    OVERVIEW_TOP_ROWS,
    OVERVIEW_TREEMAP_MAX_HEIGHT_PX,
    OVERVIEW_TREEMAP_MIN_HEIGHT_PX,
    OVERVIEW_TREEMAP_MIN_LABEL_PX,
    SecondaryLabel,
    cell_fill_color,
    classification_color,
    readable_text_color,
)
from ui.table import format_modified

_REMAINDER_KEY = -1
_REMAINDER_LABEL = "Other disk usage"
_TREEMAP_EMPTY_TEXT = "No sized prefixes yet"
_ORPHAN_CARD_TOOLTIP = "Click to review orphaned prefixes on the Prefixes page"


def format_percent(value_bytes: int, basis_bytes: int) -> str:
    """Whole percents against a basis, with '<1%' for small nonzero shares."""
    if basis_bytes <= 0 or value_bytes <= 0:
        return "0%"
    percent = value_bytes * 100 / basis_bytes
    if percent < 1:
        return "<1%"
    return f"{round(percent)}%"


def display_name(prefix: Prefix) -> str:
    return prefix.name or f"Unknown (AppID: {prefix.app_id})"


class ClassificationSwatch(QWidget):
    """Small color patch painted from the palette on every repaint."""

    def __init__(self, circle: bool, size_px: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._circle = circle
        self.setFixedSize(size_px, size_px)
        self._prefix_type = PrefixType.STEAM

    def set_prefix_type(self, prefix_type: PrefixType) -> None:
        self._prefix_type = prefix_type
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        color = classification_color(self._prefix_type, self.palette())
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


class TreemapWidget(QWidget):
    """Hand-painted treemap over known-sized prefixes plus a neutral remainder."""

    prefix_focus_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prefixes: list[Prefix] = []
        self._remainder_bytes = 0
        self._highlight_type: PrefixType | None = None
        self._hover_key: int | None = None
        self._cells: list[tuple[int, CellRect]] = []
        self.setMinimumHeight(OVERVIEW_TREEMAP_MIN_HEIGHT_PX)
        self.setMaximumHeight(OVERVIEW_TREEMAP_MAX_HEIGHT_PX)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def set_data(self, prefixes: Sequence[Prefix], remainder_bytes: int) -> None:
        self._prefixes = sorted(prefixes, key=lambda p: (-p.size_bytes, p.app_id))
        self._remainder_bytes = max(0, remainder_bytes)
        self._hover_key = None
        self._cells = []
        self.update()

    def set_highlight_type(self, prefix_type: PrefixType | None) -> None:
        if self._highlight_type != prefix_type:
            self._highlight_type = prefix_type
            self.update()

    def has_content(self) -> bool:
        return any(prefix.size_bytes > 0 for prefix in self._prefixes)

    def key_at(self, position: QPointF) -> int | None:
        """Layout key under the position, None outside all cells."""
        if not self._cells and self._prefixes:
            self._compute_cells()
        for key, rect in self._cells:
            inside_x = rect.x <= position.x() < rect.x + rect.w
            inside_y = rect.y <= position.y() < rect.y + rect.h
            if inside_x and inside_y:
                return key
        return None

    def _compute_cells(self) -> None:
        area = CellRect(x=0.0, y=0.0, w=float(self.width()), h=float(self.height()))
        items: list[tuple[int, float]] = []
        if self._remainder_bytes > 0:
            items.append((_REMAINDER_KEY, float(self._remainder_bytes)))
        items.extend((index, float(p.size_bytes)) for index, p in enumerate(self._prefixes))
        self._cells = layout(items, area)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        if not self.has_content():
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, _TREEMAP_EMPTY_TEXT)
            painter.end()
            return
        self._compute_cells()
        palette = self.palette()
        background = palette.color(QPalette.ColorRole.Base)
        border = palette.color(QPalette.ColorRole.Window)
        metrics = painter.fontMetrics()
        label_color = _treemap_label_color(palette)
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
            prefix = self._prefixes[key]
            fill = cell_fill_color(prefix.prefix_type, prefix.app_id, palette)
            painter.fillRect(cell, fill)
            dimmed = (
                self._highlight_type is not None and prefix.prefix_type is not self._highlight_type
            )
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
                    display_name(prefix), Qt.TextElideMode.ElideRight, int(cell.width() - 4)
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
            prefix = self._prefixes[key]
            self.setToolTip(
                f"{display_name(prefix)}\n{format_size(prefix.size_bytes)}\n"
                f"{prefix.prefix_type.label()}"
            )
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setToolTip(_REMAINDER_LABEL if key == _REMAINDER_KEY else "")
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() is Qt.MouseButton.LeftButton:
            key = self.key_at(event.position())
            if key is not None and key != _REMAINDER_KEY:
                self.prefix_focus_requested.emit(self._prefixes[key])
        super().mousePressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._hover_key = None
        self.setToolTip("")
        self.update()
        super().leaveEvent(event)


class LegendRow(QFrame):
    """One legend entry; hovering it reports the classification and None on leave."""

    hovered = Signal(object)

    def __init__(self, prefix_type: PrefixType, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prefix_type = prefix_type
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._swatch = ClassificationSwatch(circle=False, size_px=OVERVIEW_LEGEND_SWATCH_PX)
        self._swatch.set_prefix_type(prefix_type)
        layout.addWidget(self._swatch)
        self._type_label = QLabel(prefix_type.label(), self)
        layout.addWidget(self._type_label)
        self._size_label = QLabel("0 B", self)
        layout.addWidget(self._size_label)
        self._percent_label = QLabel("(0%)", self)
        layout.addWidget(self._percent_label)
        layout.addStretch(1)

    def set_entry(self, entry: ClassificationTotals, basis_bytes: int) -> None:
        self._size_label.setText(format_size(entry.size_bytes))
        self._percent_label.setText(f"({format_percent(entry.size_bytes, basis_bytes)})")

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.hovered.emit(self._prefix_type)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.hovered.emit(None)
        super().leaveEvent(event)


class LegendWidget(QWidget):
    """Classification legend whose hover state drives treemap dimming."""

    classification_hovered = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._rows: dict[PrefixType, LegendRow] = {}
        for prefix_type in PrefixType:
            row = LegendRow(prefix_type, self)
            row.hovered.connect(self.classification_hovered.emit)
            self._rows[prefix_type] = row
            layout.addWidget(row)

    def set_totals(self, totals: Sequence[ClassificationTotals], basis_bytes: int) -> None:
        for entry in totals:
            self._rows[entry.prefix_type].set_entry(entry, basis_bytes)

    def row_for(self, prefix_type: PrefixType) -> LegendRow:
        return self._rows[prefix_type]


class ElidedLabel(QLabel):
    """Label that elides its text to its own width, tooltip keeps the full text.

    Hidden resizes do not deliver resize events, so the elided text is
    refreshed in paintEvent as well; the width check keeps that cheap.
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self.setToolTip(self._full_text)
        self._elided_width = -1

    def full_text(self) -> str:
        return self._full_text

    def _update_elided(self) -> None:
        width = self.width()
        if width <= 0 or width == self._elided_width:
            return
        self._elided_width = width
        self.setText(
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
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        rank_label = QLabel(f"{rank}.", self)
        rank_label.setMinimumWidth(20)
        layout.addWidget(rank_label)
        dot = ClassificationSwatch(circle=True, size_px=OVERVIEW_DOT_SIZE_PX, parent=self)
        dot.set_prefix_type(prefix.prefix_type)
        layout.addWidget(dot)
        self._name_label = ElidedLabel(display_name(prefix), self)
        layout.addWidget(self._name_label, stretch=1)
        modified_label = QLabel(format_modified(prefix.modified), self)
        layout.addWidget(modified_label)
        size_label = QLabel(format_size(prefix.size_bytes), self)
        layout.addWidget(size_label)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() is Qt.MouseButton.LeftButton:
            self.activated.emit(self._prefix)
        super().mousePressEvent(event)


class TopListWidget(QWidget):
    """Ranked list of the largest known prefixes."""

    prefix_focus_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)

    def set_prefixes(self, prefixes: Sequence[Prefix]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
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


class SummaryCard(QFrame):
    """Flat bordered card; the dominant variant gets a stronger frame."""

    clicked = Signal()

    def __init__(
        self,
        title: str,
        *,
        dominant: bool = False,
        clickable: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("overviewCard")
        self.setProperty("dominant", dominant)
        self._clickable = clickable
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
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
        layout.addWidget(self._value_label)
        self._detail_label = SecondaryLabel("", self)
        layout.addWidget(self._detail_label)
        if clickable:
            self.setToolTip(_ORPHAN_CARD_TOOLTIP)

    def set_values(self, value: str, detail: str) -> None:
        self._value_label.setText(value)
        self._detail_label.setText(detail)

    def value_text(self) -> str:
        return self._value_label.text()

    def detail_text(self) -> str:
        return self._detail_label.text()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._clickable and event.button() is Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


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


class OverviewPage(QScrollArea):
    """Disk usage at a glance: cards, treemap, legend, and largest prefixes."""

    orphan_review_requested = Signal()
    prefix_focus_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._orphan_count = 0
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(
            OVERVIEW_CONTENT_MARGIN_PX,
            OVERVIEW_CONTENT_MARGIN_PX,
            OVERVIEW_CONTENT_MARGIN_PX,
            OVERVIEW_CONTENT_MARGIN_PX,
        )
        layout.setSpacing(OVERVIEW_SECTION_SPACING_PX)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(OVERVIEW_CARD_SPACING_PX)
        self._prefixes_card = SummaryCard("PREFIXES", parent=self)
        self._orphan_card = SummaryCard("ORPHANED", dominant=True, clickable=True, parent=self)
        self._disk_card = SummaryCard("DISK USAGE", parent=self)
        self._orphan_card.clicked.connect(self._on_orphan_card_clicked)
        cards_row.addWidget(self._prefixes_card, stretch=1)
        cards_row.addWidget(self._orphan_card, stretch=1)
        cards_row.addWidget(self._disk_card, stretch=1)
        layout.addLayout(cards_row)

        self._caption_label = SecondaryLabel("", content)
        self._caption_label.setWordWrap(True)
        layout.addWidget(self._caption_label)

        self._treemap = TreemapWidget(content)
        self._treemap.prefix_focus_requested.connect(self.prefix_focus_requested.emit)
        layout.addWidget(self._treemap, stretch=1)

        self._legend = LegendWidget(content)
        self._legend.classification_hovered.connect(self._treemap.set_highlight_type)
        layout.addWidget(self._legend)

        heading = QLabel("Largest prefixes", content)
        heading_font = QFont(heading.font())
        heading_font.setBold(True)
        heading.setFont(heading_font)
        layout.addWidget(heading)

        self._top_list = TopListWidget(content)
        self._top_list.prefix_focus_requested.connect(self.prefix_focus_requested.emit)
        layout.addWidget(self._top_list)

        self._status_label = SecondaryLabel("", content)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self.setWidget(content)

    def _on_orphan_card_clicked(self) -> None:
        if self._orphan_count > 0:
            self.orphan_review_requested.emit()

    def update_data(self, prefixes: Sequence[Prefix], capacity_bytes: int | None) -> None:
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

    @property
    def treemap(self) -> TreemapWidget:
        return self._treemap

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
