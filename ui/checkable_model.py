"""Shared table model for rows with a checkbox and an asynchronously measured size.

The Tools and Shaders tabs show the same kind of table: rows keyed by a
path, a check column limited to deletable rows, a size column that reads
Scanning... or Unavailable until a background walk reports, and a
short-lived row highlight used when the overview jumps to a row. This
base holds that behaviour once; each tab's model adds its own columns,
tooltips and deletability rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QBrush, QPalette
from PySide6.QtWidgets import QApplication, QCheckBox, QHeaderView

from core.models import format_size
from ui.styles import HIGHLIGHT_DURATION_MS

SCANNING_TEXT = "Scanning..."
UNAVAILABLE_TEXT = "Unavailable"

_ROOT_INDEX = QModelIndex()


class SizedItem(Protocol):
    """A dataclass row with a path identity and a measured size."""

    @property
    def path(self) -> Path: ...

    @property
    def size_bytes(self) -> int: ...


ItemT = TypeVar("ItemT", bound=SizedItem)


class SizedCheckableModel(QAbstractTableModel, Generic[ItemT]):
    """Rows with per-row selection, async size states and a timed highlight.

    Subclasses set the column layout through the class attributes and
    implement _display_text; they may override is_deletable (default: every
    row) and _extra_data for tooltips, decorations and alignment. Rows are
    identified by str(item.path).
    """

    selection_changed = Signal()

    check_column = 0
    size_column = 1
    column_count = 2
    header_labels: dict[int, str] = {}

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[ItemT] = []
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._selected: set[str] = set()
        self._highlighted: str | None = None
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self.clear_highlight)

    # --- hooks ----------------------------------------------------------------

    @staticmethod
    def key_of(item: SizedItem) -> str:
        return str(item.path)

    def is_deletable(self, item: ItemT) -> bool:
        return True

    def _display_text(self, item: ItemT, column: int) -> str:
        raise NotImplementedError

    def _extra_data(self, item: ItemT, column: int, role: int) -> object:
        """Roles the base does not handle (tooltips, decoration, alignment)."""
        return None

    # --- rows and size states -------------------------------------------------

    def _reset_rows(self, items: Sequence[ItemT]) -> None:
        """Replace all rows: every row starts pending and nothing is selected."""
        self._rows = list(items)
        self._pending = {self.key_of(item) for item in items}
        self._failed.clear()
        self._selected.clear()

    def rows(self) -> list[ItemT]:
        return list(self._rows)

    @property
    def pending(self) -> set[str]:
        return set(self._pending)

    @property
    def failed(self) -> set[str]:
        return set(self._failed)

    def set_visible(self, items: Sequence[ItemT]) -> None:
        """Re-slice visible rows, dropping selections hidden by the filter.

        A highlight never survives a filter change: the stored key is
        cleared and its timer stopped alongside the selection pruning,
        and the old key still emits dataChanged afterwards so the view
        repaints the row without the highlight colors.
        """
        previous = self._highlighted
        self._highlighted = None
        self._highlight_timer.stop()
        self.beginResetModel()
        self._rows = list(items)
        visible = {self.key_of(item) for item in items}
        self._selected = {key for key in self._selected if key in visible}
        self.endResetModel()
        if previous is not None:
            self._emit_row_changed(previous)
        self.selection_changed.emit()

    def set_size(self, key: str, size_bytes: int, failed: bool) -> None:
        """Store one async size result and refresh its size cell."""
        self._pending.discard(key)
        if failed:
            self._failed.add(key)
        else:
            self._failed.discard(key)
        for row, item in enumerate(self._rows):
            if self.key_of(item) == key:
                updated: Any = replace(item, size_bytes=size_bytes)  # type: ignore[type-var]
                self._rows[row] = updated
                self.dataChanged.emit(
                    self.index(row, self.size_column), self.index(row, self.size_column)
                )
                return

    def mark_pending_unavailable(self) -> None:
        """Move leftover pending rows to failed so none stay Scanning.

        Called when the size worker finishes without reporting a row
        (worker never started, stopped early, or dropped a signal).
        Failed rows render as Unavailable, never as a false zero size.
        """
        if not self._pending:
            return
        self._failed.update(self._pending)
        self._pending.clear()
        if self._rows:
            self.dataChanged.emit(
                self.index(0, self.size_column),
                self.index(len(self._rows) - 1, self.size_column),
            )

    def size_text(self, item: ItemT) -> str:
        key = self.key_of(item)
        if key in self._pending:
            return SCANNING_TEXT
        if key in self._failed:
            return UNAVAILABLE_TEXT
        return format_size(item.size_bytes)

    # --- selection ------------------------------------------------------------

    def selected_items(self) -> list[ItemT]:
        return [item for item in self._rows if self.key_of(item) in self._selected]

    def is_selected(self, item: ItemT) -> bool:
        return self.key_of(item) in self._selected

    def set_visible_deletable_selected(self, selected: bool) -> None:
        """Check or clear every deletable visible row."""
        for item in self._rows:
            if self.is_deletable(item):
                key = self.key_of(item)
                if selected:
                    self._selected.add(key)
                else:
                    self._selected.discard(key)
        if self._rows:
            self.dataChanged.emit(
                self.index(0, self.check_column),
                self.index(len(self._rows) - 1, self.check_column),
            )
        self.selection_changed.emit()

    def toggle_visible_deletable(self) -> None:
        """Header checkbox click: select all deletable rows unless all already are."""
        deletable = [item for item in self._rows if self.is_deletable(item)]
        if deletable:
            all_selected = all(self.is_selected(item) for item in deletable)
            self.set_visible_deletable_selected(not all_selected)

    def header_check_state(self) -> Qt.CheckState:
        """Tri-state summary of the deletable visible rows."""
        deletable = [item for item in self._rows if self.is_deletable(item)]
        selected = sum(1 for item in deletable if self.is_selected(item))
        if not deletable or selected == 0:
            return Qt.CheckState.Unchecked
        if selected == len(deletable):
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked

    # --- highlight ------------------------------------------------------------

    def highlight_row(self, item: ItemT) -> None:
        """Mark one row as highlighted for a short window (no selection change).

        A highlight that moves from another row first clears that row's
        highlight state, so the old row stops painting highlighted
        instead of waiting for an unrelated repaint.
        """
        previous = self._highlighted
        key = self.key_of(item)
        if previous is not None and previous != key:
            self._emit_row_changed(previous)
        self._highlighted = key
        self._emit_row_changed(key)
        self._highlight_timer.start(HIGHLIGHT_DURATION_MS)

    def clear_highlight(self) -> None:
        key = self._highlighted
        if key is None:
            return
        self._highlighted = None
        self._emit_row_changed(key)

    def highlighted_key(self) -> str | None:
        return self._highlighted

    def _emit_row_changed(self, key: str) -> None:
        for row, item in enumerate(self._rows):
            if self.key_of(item) == key:
                self.dataChanged.emit(self.index(row, 0), self.index(row, self.column_count - 1))
                return

    # --- Qt model API ---------------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT_INDEX) -> int:  # noqa: N802
        return self.column_count

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        item = self._rows[index.row()]
        column = index.column()
        key = self.key_of(item)
        if key == self._highlighted:
            if role == Qt.ItemDataRole.BackgroundRole:
                return QBrush(QApplication.palette().color(QPalette.ColorRole.Highlight))
            if role == Qt.ItemDataRole.ForegroundRole:
                return QBrush(QApplication.palette().color(QPalette.ColorRole.HighlightedText))
        if role == Qt.ItemDataRole.DisplayRole:
            if column == self.size_column:
                return self.size_text(item)
            return self._display_text(item, column)
        if column == self.check_column and role == Qt.ItemDataRole.CheckStateRole:
            if not self.is_deletable(item):
                return None
            state = Qt.CheckState.Checked if key in self._selected else Qt.CheckState.Unchecked
            return int(state.value)
        if role == Qt.ItemDataRole.TextAlignmentRole and column == self.size_column:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return self._extra_data(item, column, role)

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not 0 <= index.row() < len(self._rows):
            return base
        if index.column() == self.check_column and self.is_deletable(self._rows[index.row()]):
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def setData(  # noqa: N802
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != self.check_column:
            return False
        if not 0 <= index.row() < len(self._rows):
            return False
        item = self._rows[index.row()]
        if not self.is_deletable(item):
            return False
        key = self.key_of(item)
        if value in (Qt.CheckState.Checked, int(Qt.CheckState.Checked.value), True):
            self._selected.add(key)
        else:
            self._selected.discard(key)
        self.dataChanged.emit(index, index)
        self.selection_changed.emit()
        return True

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if orientation != Qt.Orientation.Horizontal or role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.header_labels.get(section)


def sync_header_checkbox(
    box: QCheckBox, header: QHeaderView, state: Qt.CheckState, check_column: int
) -> None:
    """Set the header checkbox state without signals and centre it in its section."""
    box.blockSignals(True)
    box.setCheckState(state)
    box.blockSignals(False)
    position_header_checkbox(box, header, check_column)


def position_header_checkbox(box: QCheckBox, header: QHeaderView, check_column: int) -> None:
    """Centre the checkbox inside the header's check section."""
    hint = box.sizeHint()
    section_width = max(header.sectionSize(check_column), hint.width())
    x = header.sectionPosition(check_column)
    y = max((header.height() - hint.height()) // 2, 0)
    box.setGeometry(x + (section_width - hint.width()) // 2, y, hint.width(), hint.height())
