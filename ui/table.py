"""Table model over the prefix Store with tri-state selection header."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from PySide6.QtCore import (
    QAbstractTableModel,
    QDateTime,
    QLocale,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QBrush, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHeaderView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
)

from core.models import (
    Prefix,
    ScanStatus,
    SelectionState,
    Store,
    format_size,
    prefix_key,
)
from ui.styles import (
    APP_ID_DEFAULT_PX,
    CHECK_WIDTH_PX,
    HEADER_HEIGHT_RATIO,
    HIGHLIGHT_DURATION_MS,
    MIN_HEADER_SECTION_PX,
    MODIFIED_DEFAULT_PX,
    NAME_DEFAULT_PX,
    OPEN_WIDTH_PX,
    SIZE_DEFAULT_PX,
)

_MODEL_ROOT_INDEX = QModelIndex()

CHECK_COLUMN = 0
NAME_COLUMN = 1
APP_ID_COLUMN = 2
PATH_COLUMN = 3
SIZE_COLUMN = 4
MODIFIED_COLUMN = 5
OPEN_COLUMN = 6
COLUMN_COUNT = 7

SORTABLE_COLUMNS = {
    NAME_COLUMN: "name",
    APP_ID_COLUMN: "app_id",
    PATH_COLUMN: "path",
    SIZE_COLUMN: "size",
    MODIFIED_COLUMN: "modified",
}

_HEADER_LABELS = {
    NAME_COLUMN: "Name",
    APP_ID_COLUMN: "AppID",
    PATH_COLUMN: "Path",
    SIZE_COLUMN: "Size",
    MODIFIED_COLUMN: "Modified",
}

_OPEN_DISABLED_TOOLTIP = "path no longer exists"
_SIZE_FAILED_TOOLTIP = "size scan failed"
_RUNTIME_COMPONENT_TOOLTIP = (
    "Created by Steam for its own runtime; safe to delete, "
    "recreated by Steam on next launch if needed."
)
_MODIFIED_UNKNOWN_TEXT = "-"


def format_modified(value: datetime | None) -> str:
    """Render a timestamp in the system locale's short format, '-' when unknown."""
    if value is None:
        return _MODIFIED_UNKNOWN_TEXT
    moment = QDateTime.fromMSecsSinceEpoch(int(value.timestamp() * 1000))
    return QLocale.system().toString(moment, QLocale.FormatType.ShortFormat)


class PrefixTableModel(QAbstractTableModel):
    """Renders the Store's rows; all ordering and selection live in the Store."""

    header_state_changed = Signal(SelectionState)

    def __init__(
        self,
        error_provider: Callable[[Prefix], str | None] | None = None,
    ) -> None:
        super().__init__()
        self._store = Store()
        self._rows: list[Prefix] = []
        self._openable: list[bool] = []
        self._error_provider = error_provider
        self._highlighted_key: tuple[int, str] | None = None
        self._highlight_timer = QTimer()
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self.clear_highlight)

    @property
    def store(self) -> Store:
        return self._store

    def set_store(self, store: Store) -> None:
        self.beginResetModel()
        self._store = store
        self._rows = list(store.prefixes)
        self._openable = [prefix.path.is_dir() for prefix in self._rows]
        self.endResetModel()

    def set_rows(
        self,
        rows: Sequence[Prefix],
        *,
        rescan_openable: bool = True,
    ) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        if rescan_openable or len(self._openable) != len(self._rows):
            self._openable = [prefix.path.is_dir() for prefix in self._rows]
        self.endResetModel()

    def rows(self) -> list[Prefix]:
        return list(self._rows)

    def apply_sort(self, key: str, descending: bool = True) -> None:
        """Sort through the Store (raw bytes for size), then reset the view."""
        self._store.sort(key=key, descending=descending)
        self.set_rows(self._store.prefixes)

    def open_enabled(self, row: int) -> bool:
        return 0 <= row < len(self._openable) and self._openable[row]

    def selection_state(self, *, exclude_runtime: bool = False) -> SelectionState:
        return self._store.selection_state(self._rows, exclude_runtime=exclude_runtime)

    def toggle_visible_selection(self) -> None:
        if self.selection_state(exclude_runtime=True) is SelectionState.NONE:
            self._store.select_visible(self._rows, exclude_runtime=True)
        else:
            self._store.clear_selection()
        self.refresh_all_check_states()

    def refresh_all_check_states(self) -> None:
        if not self._rows:
            self.header_state_changed.emit(SelectionState.NONE)
            return
        self.dataChanged.emit(
            self.index(0, CHECK_COLUMN),
            self.index(len(self._rows) - 1, CHECK_COLUMN),
        )
        self.header_state_changed.emit(self.selection_state(exclude_runtime=True))

    def row_at(self, row: int) -> Prefix | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def highlight_row(self, prefix: Prefix) -> None:
        """Mark one row as highlighted for a short window (no selection change)."""
        self._highlighted_key = prefix_key(prefix)
        self._emit_rows_changed_for(self._highlighted_key)
        self._highlight_timer.start(HIGHLIGHT_DURATION_MS)

    def clear_highlight(self) -> None:
        key = self._highlighted_key
        if key is None:
            return
        self._highlighted_key = None
        self._emit_rows_changed_for(key)

    def highlighted_key(self) -> tuple[int, str] | None:
        return self._highlighted_key

    def _emit_rows_changed_for(self, key: tuple[int, str]) -> None:
        for row, row_prefix in enumerate(self._rows):
            if prefix_key(row_prefix) == key:
                self.dataChanged.emit(self.index(row, 0), self.index(row, COLUMN_COUNT - 1))
                return

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        prefix = self._rows[index.row()]
        column = index.column()
        if self._highlighted_key is not None and prefix_key(prefix) == self._highlighted_key:
            if role == Qt.ItemDataRole.BackgroundRole:
                palette = QApplication.palette()
                return QBrush(palette.color(QPalette.ColorRole.Highlight))
            if role == Qt.ItemDataRole.ForegroundRole:
                palette = QApplication.palette()
                return QBrush(palette.color(QPalette.ColorRole.HighlightedText))
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_text(prefix, index.row(), column)
        if column == CHECK_COLUMN and role == Qt.ItemDataRole.CheckStateRole:
            selected = self._store.is_selected(prefix)
            state = Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
            return int(state.value)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(prefix, index.row(), column)
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (SIZE_COLUMN, MODIFIED_COLUMN):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if column == OPEN_COLUMN and role == Qt.ItemDataRole.UserRole:
            return self.open_enabled(index.row())
        if (
            column == OPEN_COLUMN
            and role == Qt.ItemDataRole.DecorationRole
            and QApplication.instance() is not None
        ):
            icon: QIcon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
            return icon
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == CHECK_COLUMN:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != CHECK_COLUMN:
            return False
        prefix = self.row_at(index.row())
        if prefix is None:
            return False
        checked = value in (Qt.CheckState.Checked, int(Qt.CheckState.Checked.value), True)
        if checked:
            self._store.select(prefix)
        else:
            self._store.deselect(prefix)
        self.dataChanged.emit(index, index)
        self.header_state_changed.emit(self.selection_state(exclude_runtime=True))
        return True

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if orientation != Qt.Orientation.Horizontal or role != Qt.ItemDataRole.DisplayRole:
            return None
        return _HEADER_LABELS.get(section)

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _MODEL_ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = _MODEL_ROOT_INDEX) -> int:
        return COLUMN_COUNT

    def _display_text(self, prefix: Prefix, row: int, column: int) -> str:
        if column == NAME_COLUMN:
            return prefix.display_label
        if column == APP_ID_COLUMN:
            return str(prefix.app_id)
        if column == PATH_COLUMN:
            return str(prefix.path)
        if column == SIZE_COLUMN:
            if not self.open_enabled(row):
                return "-"
            if prefix.scan_status is ScanStatus.NOT_SCANNED:
                return "-"
            if prefix.scan_status is ScanStatus.SCANNING:
                return "Scanning..."
            if prefix.scan_status is ScanStatus.FAILED:
                return "Unavailable"
            return format_size(prefix.size_bytes)
        if column == MODIFIED_COLUMN:
            return format_modified(prefix.modified)
        return ""

    def _tooltip(self, prefix: Prefix, row: int, column: int) -> str | None:
        if column == NAME_COLUMN and prefix.is_runtime_component:
            return _RUNTIME_COMPONENT_TOOLTIP
        if column == PATH_COLUMN:
            return str(prefix.path)
        if column == OPEN_COLUMN and not self.open_enabled(row):
            return _OPEN_DISABLED_TOOLTIP
        if column == SIZE_COLUMN and prefix.scan_status is ScanStatus.FAILED:
            if self._error_provider is not None:
                error = self._error_provider(prefix)
                if error:
                    return error
            return _SIZE_FAILED_TOOLTIP
        return None


class PrefixTable(QTableView):
    """Table view wiring header clicks to Store-backed sorting."""

    sort_column_clicked = Signal(int)
    header_toggle_clicked = Signal()

    def __init__(self, model: PrefixTableModel) -> None:
        super().__init__()
        self._model = model
        self.setModel(model)
        self.setSelectionMode(QTableView.SelectionMode.NoSelection)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        header = self.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setMinimumSectionSize(MIN_HEADER_SECTION_PX)
        header.setSectionResizeMode(CHECK_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(APP_ID_COLUMN, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(NAME_COLUMN, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(PATH_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(SIZE_COLUMN, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(MODIFIED_COLUMN, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(OPEN_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.setMinimumHeight(int(self.fontMetrics().height() * HEADER_HEIGHT_RATIO))
        self.setColumnWidth(CHECK_COLUMN, CHECK_WIDTH_PX)
        self.setColumnWidth(OPEN_COLUMN, OPEN_WIDTH_PX)
        self.setColumnWidth(APP_ID_COLUMN, APP_ID_DEFAULT_PX)
        self.setColumnWidth(NAME_COLUMN, NAME_DEFAULT_PX)
        self.setColumnWidth(SIZE_COLUMN, SIZE_DEFAULT_PX)
        self.setColumnWidth(MODIFIED_COLUMN, MODIFIED_DEFAULT_PX)
        self.setItemDelegateForColumn(OPEN_COLUMN, OpenActionDelegate(self))
        header.sectionClicked.connect(self._on_section_clicked)
        self._header_checkbox = QCheckBox(header)
        self._header_checkbox.setTristate(True)
        self._header_checkbox.clicked.connect(self._on_header_checkbox)
        header.geometriesChanged.connect(self.reposition_header_checkbox)
        model.header_state_changed.connect(self.set_header_state)

    @property
    def header_checkbox(self) -> QCheckBox:
        """The overlaid tri-state select-all checkbox."""
        return self._header_checkbox

    def reposition_header_checkbox(self) -> None:
        """Center the checkbox's own sizeHint inside the first header section.

        Stretching the widget over the whole section lets the active style
        place the indicator anywhere; sizing to the hint keeps it centered
        under every desktop style.
        """
        header = self.horizontalHeader()
        hint = self._header_checkbox.sizeHint()
        section_width = max(header.sectionSize(CHECK_COLUMN), hint.width())
        x = header.sectionPosition(CHECK_COLUMN)
        y = max((header.height() - hint.height()) // 2, 0)
        self._header_checkbox.setGeometry(
            x + (section_width - hint.width()) // 2,
            y,
            hint.width(),
            hint.height(),
        )

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self.reposition_header_checkbox()

    def _on_section_clicked(self, section: int) -> None:
        if section in SORTABLE_COLUMNS:
            self.sort_column_clicked.emit(section)

    def _on_header_checkbox(self) -> None:
        self.header_toggle_clicked.emit()

    def set_header_state(self, state: SelectionState) -> None:
        states = {
            SelectionState.NONE: Qt.CheckState.Unchecked,
            SelectionState.SOME: Qt.CheckState.PartiallyChecked,
            SelectionState.ALL: Qt.CheckState.Checked,
        }
        self._header_checkbox.blockSignals(True)
        self._header_checkbox.setCheckState(states[state])
        self._header_checkbox.blockSignals(False)
        self.reposition_header_checkbox()


class OpenActionDelegate(QStyledItemDelegate):
    """Renders the open-folder icon greyed out when the path is gone."""

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if not index.data(Qt.ItemDataRole.UserRole):
            opt.state &= ~QStyle.StateFlag.State_Enabled
        super().paint(painter, opt, index)
