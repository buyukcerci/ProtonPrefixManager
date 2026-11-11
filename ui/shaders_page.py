"""Shaders tab: per-game Steam shader caches with sizes and deletion.

The page owns its table model, filter bar, size worker and deletion
worker. The main window feeds it fresh rows after every discovery run and
listens for three things: busy_changed (a shader deletion started or
ended, so other delete buttons and Refresh can be locked), status_message
(text for the shared status bar) and deletion_finished (time to refresh).
Every worker result carries the window epoch it was started under and is
dropped when the window has moved on, matching the other tabs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QBrush, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStackedWidget,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.deletion import DeleteMode, DeletionStatus, ShaderDeletionResult
from core.discovery import Library
from core.models import PrefixType, format_size
from core.shaders import ShaderCache, shader_cache_key
from ui.dialogs import confirm_final, confirm_selection, show_deletion_summary
from ui.dialogs import tools_pending_note_for as pending_note_for
from ui.sanitize import sanitize_display, sanitize_tooltip
from ui.styles import (
    CHECK_WIDTH_PX,
    HIGHLIGHT_DURATION_MS,
    MESSAGE_ICON_SIZE_PX,
    MESSAGE_LAYOUT_SPACING_PX,
    MESSAGE_PAGE_MARGIN_PX,
    SEARCH_DEBOUNCE_MS,
    set_message_content,
)
from ui.table import shared_view_settings
from ui.workers import ShaderDeletionWorker, SizeWorker

CHECK_COLUMN = 0
NAME_COLUMN = 1
APP_ID_COLUMN = 2
SIZE_COLUMN = 3
STATUS_COLUMN = 4
_COLUMN_COUNT = 5

_HEADER_LABELS = {
    NAME_COLUMN: "Name",
    APP_ID_COLUMN: "AppID",
    SIZE_COLUMN: "Size",
    STATUS_COLUMN: "Status",
}
_SORTABLE_COLUMNS = {
    NAME_COLUMN: "name",
    APP_ID_COLUMN: "app_id",
    SIZE_COLUMN: "size",
    STATUS_COLUMN: "status",
}

STATUS_LABELS = {
    PrefixType.STEAM: "Installed",
    PrefixType.NON_STEAM: "Non-Steam",
    PrefixType.ORPHANED: "Orphaned",
}
_STATUS_ORDER = (PrefixType.STEAM, PrefixType.NON_STEAM, PrefixType.ORPHANED)

_INNER_PAGE_MESSAGE = 0
_INNER_PAGE_TABLE = 1

NO_ROWS_TEXT = "No shader caches match the current view."
NO_CACHES_TEXT = "No shader caches found."
RECOMPILE_NOTE = (
    "Steam rebuilds a shader cache when its game next runs, "
    "so that first launch may stutter while shaders compile."
)

_ROOT_INDEX = QModelIndex()


def matches_search(cache: ShaderCache, text: str) -> bool:
    """Case-insensitive match on name, or on AppID digits."""
    needle = text.strip().casefold()
    if not needle:
        return True
    return needle in cache.name.casefold() or needle in str(cache.app_id)


class ShaderTableModel(QAbstractTableModel):
    """Shader cache rows; every row is deletable since Steam can rebuild it."""

    selection_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[ShaderCache] = []
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._selected: set[str] = set()
        self._highlighted: str | None = None
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self.clear_highlight)

    # --- state --------------------------------------------------------------

    def reset_items(self, caches: Sequence[ShaderCache]) -> None:
        """New discovery results: every row starts pending, nothing selected."""
        self.beginResetModel()
        self._rows = list(caches)
        self._pending = {shader_cache_key(cache) for cache in caches}
        self._failed.clear()
        self._selected.clear()
        self._highlighted = None
        self._highlight_timer.stop()
        self.endResetModel()
        self.selection_changed.emit()

    def set_visible(self, caches: Sequence[ShaderCache]) -> None:
        """Re-slice visible rows, dropping selections the filter hides."""
        self.beginResetModel()
        self._rows = list(caches)
        visible = {shader_cache_key(cache) for cache in caches}
        self._selected &= visible
        self.endResetModel()
        self.selection_changed.emit()

    def set_size(self, key: str, size_bytes: int, failed: bool) -> None:
        self._pending.discard(key)
        if failed:
            self._failed.add(key)
        else:
            self._failed.discard(key)
        for row, cache in enumerate(self._rows):
            if shader_cache_key(cache) == key:
                self._rows[row] = replace(cache, size_bytes=size_bytes)
                self.dataChanged.emit(self.index(row, SIZE_COLUMN), self.index(row, SIZE_COLUMN))
                return

    def mark_pending_unavailable(self) -> None:
        """Rows the size worker never reported read Unavailable, not 0 B."""
        if not self._pending:
            return
        self._failed |= self._pending
        self._pending.clear()
        if self._rows:
            last = len(self._rows) - 1
            self.dataChanged.emit(self.index(0, SIZE_COLUMN), self.index(last, SIZE_COLUMN))

    @property
    def pending(self) -> set[str]:
        return set(self._pending)

    @property
    def failed(self) -> set[str]:
        return set(self._failed)

    def rows(self) -> list[ShaderCache]:
        return list(self._rows)

    def selected_caches(self) -> list[ShaderCache]:
        return [cache for cache in self._rows if shader_cache_key(cache) in self._selected]

    def is_selected(self, cache: ShaderCache) -> bool:
        return shader_cache_key(cache) in self._selected

    def set_all_selected(self, selected: bool) -> None:
        if selected:
            self._selected = {shader_cache_key(cache) for cache in self._rows}
        else:
            self._selected.clear()
        if self._rows:
            last = len(self._rows) - 1
            self.dataChanged.emit(self.index(0, CHECK_COLUMN), self.index(last, CHECK_COLUMN))
        self.selection_changed.emit()

    def highlight_row(self, cache: ShaderCache) -> None:
        previous = self._highlighted
        self._highlighted = shader_cache_key(cache)
        if previous is not None and previous != self._highlighted:
            self._emit_row_changed(previous)
        self._emit_row_changed(self._highlighted)
        self._highlight_timer.start(HIGHLIGHT_DURATION_MS)

    def clear_highlight(self) -> None:
        key = self._highlighted
        self._highlighted = None
        if key is not None:
            self._emit_row_changed(key)

    def highlighted_key(self) -> str | None:
        return self._highlighted

    def _emit_row_changed(self, key: str) -> None:
        for row, cache in enumerate(self._rows):
            if shader_cache_key(cache) == key:
                self.dataChanged.emit(self.index(row, 0), self.index(row, _COLUMN_COUNT - 1))
                return

    # --- Qt model API -------------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT_INDEX) -> int:  # noqa: N802
        return _COLUMN_COUNT

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        cache = self._rows[index.row()]
        column = index.column()
        key = shader_cache_key(cache)
        if key == self._highlighted and role in (
            Qt.ItemDataRole.BackgroundRole,
            Qt.ItemDataRole.ForegroundRole,
        ):
            palette = QApplication.palette()
            color_role = (
                QPalette.ColorRole.Highlight
                if role == Qt.ItemDataRole.BackgroundRole
                else QPalette.ColorRole.HighlightedText
            )
            return QBrush(palette.color(color_role))
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_text(cache, column)
        if role == Qt.ItemDataRole.CheckStateRole and column == CHECK_COLUMN:
            state = Qt.CheckState.Checked if key in self._selected else Qt.CheckState.Unchecked
            return int(state.value)
        if role == Qt.ItemDataRole.ToolTipRole and column == NAME_COLUMN:
            lines = [sanitize_display(cache.name, limit=None), str(cache.path)]
            if cache.is_orphan:
                lines.append("No installed game or shortcut uses this AppID.")
            return sanitize_tooltip("\n".join(lines))
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (SIZE_COLUMN, APP_ID_COLUMN):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def _display_text(self, cache: ShaderCache, column: int) -> str:
        if column == NAME_COLUMN:
            return sanitize_display(cache.name)
        if column == APP_ID_COLUMN:
            return str(cache.app_id)
        if column == SIZE_COLUMN:
            key = shader_cache_key(cache)
            if key in self._pending:
                return "Scanning..."
            if key in self._failed:
                return "Unavailable"
            return format_size(cache.size_bytes)
        if column == STATUS_COLUMN:
            return STATUS_LABELS[cache.cache_type]
        return ""

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == CHECK_COLUMN and 0 <= index.row() < len(self._rows):
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def setData(  # noqa: N802
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != CHECK_COLUMN:
            return False
        if not 0 <= index.row() < len(self._rows):
            return False
        key = shader_cache_key(self._rows[index.row()])
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
        return _HEADER_LABELS.get(section)


class ShadersPage(QWidget):
    """The Shaders tab."""

    busy_changed = Signal(bool)
    status_message = Signal(str, int)
    deletion_finished = Signal()
    data_changed = Signal()

    def __init__(
        self,
        epoch_provider: Callable[[], int],
        external_busy: Callable[[], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._epoch_provider = epoch_provider
        self._external_busy = external_busy
        self._all: list[ShaderCache] = []
        self._libraries: list[Library] = []
        self._workers: set[QRunnable] = set()
        self._statuses: set[PrefixType] = set(_STATUS_ORDER)
        self._search_text = ""
        self._sort_key = "size"
        self._sort_descending = True
        self._deleting = False
        self._delete_total = 0
        self._delete_results: list[ShaderDeletionResult] = []
        self.model = ShaderTableModel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build_filter_bar())
        layout.addWidget(self._build_stack())
        layout.addWidget(self._build_action_bar())
        self.model.selection_changed.connect(self._on_selection_changed)
        self._apply_filter()

    # --- construction -------------------------------------------------------

    def _build_filter_bar(self) -> QWidget:
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        self.status_button = QToolButton(bar)
        self.status_button.setText("Filters")
        self.status_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.status_button)
        self.status_actions: dict[PrefixType, QAction] = {}
        for cache_type in _STATUS_ORDER:
            action = menu.addAction(STATUS_LABELS[cache_type])
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(
                lambda checked, ct=cache_type: self._on_status_toggled(ct, checked)
            )
            self.status_actions[cache_type] = action
        self.status_button.setMenu(menu)
        layout.addWidget(self.status_button)
        search_frame = QFrame(bar)
        search_frame.setObjectName("shadersSearchFrame")
        search_layout = QHBoxLayout(search_frame)
        search_layout.setContentsMargins(0, 0, 0, 0)
        self.search_box = QLineEdit(search_frame)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setPlaceholderText("Search names or AppIDs")
        self.search_box.textChanged.connect(self._on_search_text_changed)
        search_layout.addWidget(self.search_box)
        layout.addWidget(search_frame, stretch=1)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._apply_filter)
        self._update_status_tooltip()
        return bar

    def _build_stack(self) -> QWidget:
        self.table = QTableView(self)
        self.table.setModel(self.model)
        header = shared_view_settings(self.table)
        header.setSectionResizeMode(CHECK_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(NAME_COLUMN, QHeaderView.ResizeMode.Stretch)
        for column in (APP_ID_COLUMN, SIZE_COLUMN, STATUS_COLUMN):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setSortIndicator(SIZE_COLUMN, Qt.SortOrder.DescendingOrder)
        header.sectionClicked.connect(self._on_section_clicked)
        self.table.setColumnWidth(CHECK_COLUMN, CHECK_WIDTH_PX)
        self.header_checkbox = QCheckBox(header)
        self.header_checkbox.setTristate(True)
        self.header_checkbox.clicked.connect(self._on_header_toggle)
        header.geometriesChanged.connect(self._reposition_header_checkbox)

        message_page = QWidget(self)
        message_layout = QVBoxLayout(message_page)
        margin = MESSAGE_PAGE_MARGIN_PX
        message_layout.setContentsMargins(margin, margin, margin, margin)
        message_layout.setSpacing(MESSAGE_LAYOUT_SPACING_PX)
        self._message_icon = QLabel(message_page)
        self._message_icon.setFixedSize(MESSAGE_ICON_SIZE_PX, MESSAGE_ICON_SIZE_PX)
        self._message_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label = QLabel(message_page)
        self.message_label.setWordWrap(True)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_layout.addStretch(1)
        message_layout.addWidget(self._message_icon, alignment=Qt.AlignmentFlag.AlignHCenter)
        message_layout.addWidget(self.message_label)
        message_layout.addStretch(1)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(message_page)
        self.stack.addWidget(self.table)
        return self.stack

    def _build_action_bar(self) -> QWidget:
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        self.summary_label = QLabel(bar)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)
        self.delete_button = QPushButton(bar)
        self.delete_button.clicked.connect(self._on_delete_clicked)
        layout.addWidget(self.delete_button)
        self.update_delete_button()
        return bar

    # --- data ---------------------------------------------------------------

    def set_data(self, caches: Sequence[ShaderCache], libraries: Sequence[Library]) -> None:
        """Take one discovery run's rows and start measuring their sizes."""
        self._all = list(caches)
        self._libraries = list(libraries)
        self.model.reset_items(self._all)
        self._apply_filter()
        self.data_changed.emit()
        if not self._all:
            return
        epoch = self._epoch_provider()
        worker = SizeWorker(self._all, epoch, is_stale=lambda: epoch != self._epoch_provider())
        self._workers.add(worker)
        worker.signals.sized.connect(self._on_sized)
        worker.signals.finished.connect(lambda ep, w=worker: self._on_size_finished(ep, w))
        try:
            QThreadPool.globalInstance().start(worker)
        except RuntimeError:
            self._workers.discard(worker)
            self.model.mark_pending_unavailable()

    def caches(self) -> list[ShaderCache]:
        return list(self._all)

    @property
    def pending(self) -> set[str]:
        return self.model.pending

    @property
    def failed(self) -> set[str]:
        return self.model.failed

    @property
    def is_deleting(self) -> bool:
        return self._deleting

    def _on_sized(self, payload: object, epoch: int) -> None:
        if epoch != self._epoch_provider() or not isinstance(payload, tuple) or len(payload) != 3:
            return
        key, size_bytes, error = payload
        if not isinstance(key, str) or not isinstance(size_bytes, int):
            return
        # A failed walk's partial total is not trustworthy; it counts as 0
        # and the row reads Unavailable.
        size = 0 if error is not None else size_bytes
        self._all = [
            replace(cache, size_bytes=size) if shader_cache_key(cache) == key else cache
            for cache in self._all
        ]
        self.model.set_size(key, size, failed=error is not None)
        if self._sort_key == "size":
            self._apply_filter()
        self._update_summary()
        self.data_changed.emit()

    def _on_size_finished(self, epoch: int, worker: QRunnable | None = None) -> None:
        self._workers.discard(worker)
        if epoch != self._epoch_provider():
            return
        self.model.mark_pending_unavailable()
        self.data_changed.emit()

    # --- filtering and sorting ----------------------------------------------

    def _apply_filter(self) -> None:
        visible = [
            cache
            for cache in self._all
            if cache.cache_type in self._statuses and matches_search(cache, self._search_text)
        ]
        reverse = self._sort_descending
        if self._sort_key == "size":
            visible.sort(key=lambda cache: cache.size_bytes, reverse=reverse)
        elif self._sort_key == "app_id":
            visible.sort(key=lambda cache: cache.app_id, reverse=reverse)
        elif self._sort_key == "status":
            visible.sort(key=lambda cache: STATUS_LABELS[cache.cache_type], reverse=reverse)
        else:
            visible.sort(key=lambda cache: cache.name.casefold(), reverse=reverse)
        self.model.set_visible(visible)
        if visible:
            self.stack.setCurrentIndex(_INNER_PAGE_TABLE)
        else:
            text = NO_ROWS_TEXT if self._all else NO_CACHES_TEXT
            set_message_content(self._message_icon, self.message_label, text)
            self.stack.setCurrentIndex(_INNER_PAGE_MESSAGE)
        self._update_summary()

    def _on_status_toggled(self, cache_type: PrefixType, checked: bool) -> None:
        if checked:
            self._statuses.add(cache_type)
        else:
            self._statuses.discard(cache_type)
        self._update_status_tooltip()
        self._apply_filter()

    def set_status_filter(self, statuses: set[PrefixType]) -> None:
        self._statuses = set(statuses)
        for cache_type, action in self.status_actions.items():
            action.blockSignals(True)
            action.setChecked(cache_type in self._statuses)
            action.blockSignals(False)
        self._update_status_tooltip()
        self._apply_filter()

    def _update_status_tooltip(self) -> None:
        checked = [STATUS_LABELS[ct] for ct in _STATUS_ORDER if ct in self._statuses]
        self.status_button.setToolTip("Status: " + (" + ".join(checked) or "None"))

    def _on_search_text_changed(self, text: str) -> None:
        self._search_text = text.strip()
        self._search_timer.start()

    def clear_search(self) -> None:
        self._search_text = ""
        self.search_box.blockSignals(True)
        self.search_box.clear()
        self.search_box.blockSignals(False)
        self._search_timer.stop()

    def _on_section_clicked(self, section: int) -> None:
        key = _SORTABLE_COLUMNS.get(section)
        if key is None:
            return
        if key == self._sort_key:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_key = key
            self._sort_descending = key == "size"
        order = (
            Qt.SortOrder.DescendingOrder if self._sort_descending else Qt.SortOrder.AscendingOrder
        )
        self.table.horizontalHeader().setSortIndicator(section, order)
        self._apply_filter()

    # --- handoffs from the overview -----------------------------------------

    def show_orphans(self) -> None:
        """Clear the search and show only orphaned caches."""
        self.clear_search()
        self.set_status_filter({PrefixType.ORPHANED})

    def focus_cache(self, cache: ShaderCache) -> None:
        """Make one cache visible, then highlight it and scroll to it."""
        self.clear_search()
        if cache.cache_type not in self._statuses:
            self.set_status_filter(set(_STATUS_ORDER))
        else:
            self._apply_filter()
        key = shader_cache_key(cache)
        for row, visible in enumerate(self.model.rows()):
            if shader_cache_key(visible) == key:
                self.model.highlight_row(visible)
                self.table.scrollTo(
                    self.model.index(row, NAME_COLUMN),
                    QAbstractItemView.ScrollHint.PositionAtCenter,
                )
                return

    # --- selection ----------------------------------------------------------

    def _on_selection_changed(self) -> None:
        self.update_delete_button()
        self._update_header_checkbox()

    def _update_header_checkbox(self) -> None:
        rows = self.model.rows()
        selected = sum(1 for cache in rows if self.model.is_selected(cache))
        box = self.header_checkbox
        box.blockSignals(True)
        if not rows or selected == 0:
            box.setCheckState(Qt.CheckState.Unchecked)
        elif selected == len(rows):
            box.setCheckState(Qt.CheckState.Checked)
        else:
            box.setCheckState(Qt.CheckState.PartiallyChecked)
        box.blockSignals(False)
        self._reposition_header_checkbox()

    def _reposition_header_checkbox(self) -> None:
        header = self.table.horizontalHeader()
        hint = self.header_checkbox.sizeHint()
        width = max(header.sectionSize(CHECK_COLUMN), hint.width())
        x = header.sectionPosition(CHECK_COLUMN) + (width - hint.width()) // 2
        y = max((header.height() - hint.height()) // 2, 0)
        self.header_checkbox.setGeometry(x, y, hint.width(), hint.height())

    def _on_header_toggle(self) -> None:
        rows = self.model.rows()
        if rows:
            self.model.set_all_selected(not all(self.model.is_selected(c) for c in rows))

    def _update_summary(self) -> None:
        total = sum(cache.size_bytes for cache in self._all)
        orphaned = sum(cache.size_bytes for cache in self._all if cache.is_orphan)
        if not self._all:
            self.summary_label.setText("")
            return
        noun = "cache" if len(self._all) == 1 else "caches"
        text = f"{len(self._all)} {noun}, {format_size(total)}"
        if orphaned:
            text += f" ({format_size(orphaned)} orphaned)"
        self.summary_label.setText(text)

    # --- deletion -----------------------------------------------------------

    def update_delete_button(self) -> None:
        count = len(self.model.selected_caches())
        self.delete_button.setText(f"Delete Shader Caches ({count})")
        self.delete_button.setEnabled(
            count > 0 and not self._deleting and not self._external_busy()
        )

    def _on_delete_clicked(self) -> None:
        caches = self.model.selected_caches()
        if not caches or self._deleting or self._external_busy():
            return
        total_text = format_size(sum(cache.size_bytes for cache in caches))
        notes = [
            note
            for note in (
                pending_note_for(caches, self.model.pending, self.model.failed, "shader cache"),
                RECOMPILE_NOTE,
            )
            if note
        ]
        names = [cache.name for cache in caches]
        if not confirm_selection(self, names, total_text, "\n".join(notes), "shader caches"):
            return
        mode = confirm_final(self, len(caches), total_text, item_noun="shader cache(s)")
        if mode is None:
            return
        self._start_deletion(caches, mode)

    def _start_deletion(self, caches: Sequence[ShaderCache], mode: DeleteMode) -> None:
        self._deleting = True
        self._delete_total = len(caches)
        self._delete_results = []
        self.update_delete_button()
        self.busy_changed.emit(True)
        self.status_message.emit(f"Deleting 0/{self._delete_total}", 0)
        epoch = self._epoch_provider()
        worker = ShaderDeletionWorker(caches, self._libraries, mode, epoch)
        self._workers.add(worker)
        worker.signals.result_ready.connect(self._on_deletion_result)
        worker.signals.finished.connect(lambda ep, w=worker: self._on_deletion_finished(ep, w))
        QThreadPool.globalInstance().start(worker)

    def _on_deletion_result(self, result: object, epoch: int) -> None:
        if epoch != self._epoch_provider() or not isinstance(result, ShaderDeletionResult):
            return
        self._delete_results.append(result)
        done = len(self._delete_results)
        self.status_message.emit(f"Deleting {done}/{self._delete_total}", 0)

    def _on_deletion_finished(self, epoch: int, worker: QRunnable | None = None) -> None:
        self._workers.discard(worker)
        results = list(self._delete_results)
        self._deleting = False
        self.update_delete_button()
        self.busy_changed.emit(False)
        self.status_message.emit("", 0)
        if epoch != self._epoch_provider():
            return
        if any(result.status is not DeletionStatus.DELETED for result in results):
            show_deletion_summary(self, results)
        self.deletion_finished.emit()

    def stop(self) -> None:
        """Window is closing: drop timers so nothing fires after teardown."""
        self._search_timer.stop()
        self.model.clear_highlight()
