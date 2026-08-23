"""Main application window orchestrating discovery, scanning, and display."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.analytics import storage_capacity
from core.config import load_config, save_config
from core.deletion import DeleteMode, DeletionResult, DeletionStatus
from core.discovery import DiscoveryResult, Library
from core.models import Prefix, PrefixType, ScanStatus, Store, format_size, prefix_key
from core.opener import OpenStatus, can_open
from core.scanner import (
    ScanEvent,
    ScanEventKind,
    cache_key,
    invalidate,
    load_cached,
    refresh_needed,
)
from ui.dialogs import (
    confirm_final,
    confirm_selection,
    show_deletion_summary,
    unscanned_note_for,
)
from ui.overview import OverviewPage
from ui.styles import (
    MESSAGE_ICON_SIZE_PX,
    MESSAGE_LAYOUT_SPACING_PX,
    MESSAGE_PAGE_MARGIN_PX,
    SEARCH_DEBOUNCE_MS,
)
from ui.table import (
    OPEN_COLUMN,
    SIZE_COLUMN,
    SORTABLE_COLUMNS,
    PrefixTable,
    PrefixTableModel,
)
from ui.workers import DeletionWorker, DiscoveryWorker, OpenFolderWorker, ScanWorker

_PAGE_MESSAGE = 0
_PAGE_CONTENT = 1

_PAGE_OVERVIEW = 0
_PAGE_PREFIXES = 1

_INNER_PAGE_MESSAGE = 0
_INNER_PAGE_TABLE = 1

_NO_ROOTS_TEXT = (
    "No valid Steam root was found.\n"
    "Adding a custom Steam root will arrive with the settings options."
)
_NO_PREFIXES_TEXT = "Steam libraries were found, but no Proton prefixes exist yet."
_NO_ROWS_TEXT = "No prefixes match the current view."

_SCAN_WAIT_MS = 2000

_TYPE_LABELS = {
    PrefixType.STEAM: "Steam Games",
    PrefixType.NON_STEAM: "Non-Steam Games",
    PrefixType.ORPHANED: "Orphaned",
}
_SEARCH_TARGETS = {"name": "Name", "app_id": "AppID"}


class MainWindow(QMainWindow):
    """Root window hosting the prefix table and its background pipelines."""

    search_applied = Signal()

    def __init__(self, auto_start: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("Proton Prefix Manager")
        self.resize(900, 600)
        self.setMinimumSize(760, 440)

        self._config = load_config()
        self._epoch = 0
        self._workers: set[QRunnable] = set()
        self._scan_total = 0
        self._scan_done = 0
        self._scan_errors: dict[str, str] = {}
        self._sort_key = self._config.sort_column
        self._sort_descending = not self._config.sort_ascending
        self._libraries: list[Library] = []
        self._filter_types: set[PrefixType] = set(self._config.type_filter)
        self._search_text = ""
        self._search_target = "name"
        self._deleting = False
        self._delete_total = 0
        self._delete_done = 0
        self._deletion_results: list[DeletionResult] = []
        self._delete_mode: DeleteMode | None = None
        self._disk_capacity: int | None = None
        self._pending_filter_reset = False
        self._pending_select_all_visible = False
        self._pending_highlight_key: tuple[int, str] | None = None

        self._model = PrefixTableModel(error_provider=self._scan_error_for)
        self._table = PrefixTable(self._model)
        self._store = self._model.store

        self._message_icon_label = QLabel()
        self._message_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_icon_label.setFixedSize(MESSAGE_ICON_SIZE_PX, MESSAGE_ICON_SIZE_PX)
        self._message_label = QLabel()
        self._message_label.setWordWrap(True)
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_page = QWidget()
        message_page = self._message_page
        message_layout = QVBoxLayout(message_page)
        message_layout.setContentsMargins(
            MESSAGE_PAGE_MARGIN_PX,
            MESSAGE_PAGE_MARGIN_PX,
            MESSAGE_PAGE_MARGIN_PX,
            MESSAGE_PAGE_MARGIN_PX,
        )
        message_layout.setSpacing(MESSAGE_LAYOUT_SPACING_PX)
        message_layout.addStretch(1)
        message_layout.addWidget(self._message_icon_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        message_layout.addWidget(self._message_label)
        message_layout.addStretch(1)
        message_palette = self._message_page.palette()
        message_palette.setColor(
            message_palette.ColorRole.WindowText,
            message_palette.color(message_palette.ColorRole.WindowText),
        )
        message_palette.setColor(
            message_palette.ColorRole.Window,
            message_palette.color(message_palette.ColorRole.Window),
        )
        self._message_page.setPalette(message_palette)
        self._message_label.setPalette(message_palette)
        self._message_icon_label.setPalette(message_palette)

        self._inner_message_icon_label = QLabel()
        self._inner_message_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._inner_message_icon_label.setFixedSize(MESSAGE_ICON_SIZE_PX, MESSAGE_ICON_SIZE_PX)
        self._inner_message_label = QLabel()
        self._inner_message_label.setWordWrap(True)
        self._inner_message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._inner_message_page = QWidget()
        inner_message_layout = QVBoxLayout(self._inner_message_page)
        inner_message_layout.setContentsMargins(
            MESSAGE_PAGE_MARGIN_PX,
            MESSAGE_PAGE_MARGIN_PX,
            MESSAGE_PAGE_MARGIN_PX,
            MESSAGE_PAGE_MARGIN_PX,
        )
        inner_message_layout.setSpacing(MESSAGE_LAYOUT_SPACING_PX)
        inner_message_layout.addStretch(1)
        inner_message_layout.addWidget(
            self._inner_message_icon_label, alignment=Qt.AlignmentFlag.AlignHCenter
        )
        inner_message_layout.addWidget(self._inner_message_label)
        inner_message_layout.addStretch(1)
        inner_message_palette = self._inner_message_page.palette()
        self._inner_message_page.setPalette(inner_message_palette)
        self._inner_message_label.setPalette(inner_message_palette)
        self._inner_message_icon_label.setPalette(inner_message_palette)

        self._inner_stack = QStackedWidget()
        self._inner_stack.addWidget(self._inner_message_page)
        self._inner_stack.addWidget(self._table)

        prefixes_page = QWidget()
        prefixes_layout = QVBoxLayout(prefixes_page)
        prefixes_layout.setContentsMargins(0, 0, 0, 0)
        prefixes_layout.addWidget(self._build_filter_bar(prefixes_page))
        prefixes_layout.addWidget(self._inner_stack)
        prefixes_layout.addWidget(self._build_action_bar(prefixes_page))

        self._overview = OverviewPage()
        self._overview.orphan_review_requested.connect(self._on_orphan_review_requested)
        self._overview.prefix_focus_requested.connect(self._on_prefix_focus_requested)

        self._pages = QStackedWidget()
        self._pages.addWidget(self._overview)
        self._pages.addWidget(prefixes_page)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self._build_nav_bar(content))
        content_layout.addWidget(self._pages)

        self._stack = QStackedWidget()
        self._stack.addWidget(message_page)
        self._stack.addWidget(content)
        self.setCentralWidget(self._stack)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._apply_filters)

        self._build_menu()
        self._status = QStatusBar()
        self.setStatusBar(self._status)

        self._table.sort_column_clicked.connect(self._on_sort_column_clicked)
        self._table.header_toggle_clicked.connect(self._on_header_toggle)
        self._table.clicked.connect(self._on_table_clicked)
        self._model.header_state_changed.connect(lambda *_: self._update_delete_button())

        self._apply_initial_sort_indicator()
        if auto_start:
            self.refresh()

    def _build_nav_bar(self, parent: QWidget) -> QWidget:
        bar = QWidget(parent)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._nav_group = QButtonGroup(bar)
        self._nav_group.setExclusive(True)
        self._overview_button = QToolButton(bar)
        self._overview_button.setText("Overview")
        self._overview_button.setCheckable(True)
        self._prefixes_button = QToolButton(bar)
        self._prefixes_button.setText("Prefixes")
        self._prefixes_button.setCheckable(True)
        self._nav_group.addButton(self._overview_button, _PAGE_OVERVIEW)
        self._nav_group.addButton(self._prefixes_button, _PAGE_PREFIXES)
        self._overview_button.setChecked(True)
        layout.addWidget(self._overview_button)
        layout.addWidget(self._prefixes_button)
        layout.addStretch(1)
        self._nav_group.idToggled.connect(self._on_nav_toggled)
        return bar

    def _on_nav_toggled(self, identifier: int, checked: bool) -> None:
        if checked:
            self._pages.setCurrentIndex(identifier)

    def _build_filter_bar(self, parent: QWidget) -> QWidget:
        bar = QWidget(parent)
        layout = QHBoxLayout(bar)
        self._type_button = QToolButton(bar)
        self._type_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._type_button.setText("Filters")
        type_menu = QMenu(self._type_button)
        self._type_actions: dict[PrefixType, QAction] = {}
        for prefix_type in (PrefixType.STEAM, PrefixType.NON_STEAM, PrefixType.ORPHANED):
            action = type_menu.addAction(_TYPE_LABELS[prefix_type])
            action.setCheckable(True)
            action.setChecked(prefix_type in self._filter_types)
            action.toggled.connect(
                lambda checked, pt=prefix_type: self._on_type_toggled(pt, checked)
            )
            self._type_actions[prefix_type] = action
        self._type_button.setMenu(type_menu)
        self._type_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        layout.addWidget(self._type_button)

        search_frame = QFrame(bar)
        search_frame.setObjectName("searchFrame")
        search_layout = QHBoxLayout(search_frame)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(0)

        self._target_combo = QComboBox(search_frame)
        self._target_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for value, label in _SEARCH_TARGETS.items():
            self._target_combo.addItem(label, userData=value)
        self._target_combo.currentIndexChanged.connect(self._on_search_target_changed)
        search_layout.addWidget(self._target_combo)

        self._search_box = QLineEdit(search_frame)
        self._search_box.setClearButtonEnabled(True)
        self._search_box.textChanged.connect(self._on_search_text_changed)
        search_layout.addWidget(self._search_box, stretch=1)

        layout.addWidget(search_frame, stretch=1)

        self._update_type_summary()
        self._update_search_placeholder()
        return bar

    def _build_action_bar(self, parent: QWidget) -> QWidget:
        bar = QWidget(parent)
        layout = QHBoxLayout(bar)
        layout.addStretch(1)
        self._delete_button = QPushButton(bar)
        self._delete_button.clicked.connect(self._on_delete_clicked)
        layout.addWidget(self._delete_button)
        self._update_delete_button()
        return bar

    def _type_summary_text(self) -> str:
        checked = [
            pt
            for pt in (PrefixType.STEAM, PrefixType.NON_STEAM, PrefixType.ORPHANED)
            if pt in self._filter_types
        ]
        if not checked:
            return "Types: None"
        labels = [_TYPE_LABELS[pt].removesuffix(" Games") for pt in checked]
        return "Types: " + " + ".join(labels)

    def _update_type_summary(self) -> None:
        self._type_button.setToolTip(self._type_summary_text())

    def _update_search_placeholder(self) -> None:
        placeholders = {"name": "Search names", "app_id": "Search AppIDs"}
        self._search_box.setPlaceholderText(placeholders.get(self._search_target, "Search"))

    def _on_type_toggled(self, prefix_type: PrefixType, checked: bool) -> None:
        if checked:
            self._filter_types.add(prefix_type)
        else:
            self._filter_types.discard(prefix_type)
        self._config.type_filter = [
            pt
            for pt in (PrefixType.STEAM, PrefixType.NON_STEAM, PrefixType.ORPHANED)
            if pt in self._filter_types
        ]
        self._update_type_summary()
        self._apply_filters()
        save_config(self._config)

    def _on_search_text_changed(self, text: str) -> None:
        self._search_text = text.strip()
        self._search_timer.start()

    def _on_search_target_changed(self, index: int) -> None:
        value = self._target_combo.itemData(index)
        if isinstance(value, str):
            self._search_target = value
            self._update_search_placeholder()
            self._apply_filters()

    def _apply_filters(self, *, rescan_openable: bool = True) -> None:
        """Re-slice the Store through the current filters into the model."""
        visible = self._store.filter(
            types=self._filter_types,
            search_text=self._search_text,
            search_target=self._search_target,
        )
        self._model.set_rows(visible, rescan_openable=rescan_openable)
        self._table.set_header_state(self._model.selection_state())
        if visible:
            self._inner_stack.setCurrentIndex(_INNER_PAGE_TABLE)
            self._stack.setCurrentIndex(_PAGE_CONTENT)
        elif self._store.prefixes and self._libraries:
            self._show_inner_message(_NO_ROWS_TEXT)
            self._inner_stack.setCurrentIndex(_INNER_PAGE_MESSAGE)
            self._stack.setCurrentIndex(_PAGE_CONTENT)
        self.search_applied.emit()
        self._update_delete_button()

    def _selected_summary(self) -> tuple[list[str], str]:
        selected = self._store.selected()
        names = [prefix.name for prefix in selected]
        total = sum(prefix.size_bytes for prefix in selected)
        return names, format_size(total)

    def _update_delete_button(self) -> None:
        count = len(self._store.selected())
        self._delete_button.setText(f"Delete Prefixes ({count})")
        self._delete_button.setEnabled(count > 0 and not self._deleting)

    def _on_delete_clicked(self) -> None:
        selected = self._store.selected()
        if not selected or self._deleting:
            return
        names, total_text = self._selected_summary()
        if not confirm_selection(self, names, total_text, unscanned_note_for(selected)):
            return
        mode = confirm_final(self, len(selected), total_text)
        if mode is None:
            return
        self._deleting = True
        self._delete_total = len(selected)
        self._delete_done = 0
        self._deletion_results.clear()
        self._delete_mode = mode
        self._update_delete_button()
        self._refresh_action.setEnabled(False)
        self._status.showMessage(f"Deleting 0/{self._delete_total}")
        epoch = self._epoch
        worker = DeletionWorker(selected, self._libraries, mode, epoch)
        self._workers.add(worker)
        worker.signals.result_ready.connect(self._on_deletion_result)
        worker.signals.finished.connect(lambda ep, w=worker: self._on_deletion_finished(ep, w))
        QThreadPool.globalInstance().start(worker)

    def _on_deletion_result(self, result, epoch: int) -> None:
        if epoch != self._epoch:
            return
        self._deletion_results.append(result)
        self._delete_done += 1
        self._status.showMessage(f"Deleting {self._delete_done}/{self._delete_total}")

    def _on_deletion_finished(self, epoch: int, worker: QRunnable | None = None) -> None:
        self._workers.discard(worker)
        if epoch != self._epoch:
            return
        results = list(self._deletion_results)
        for result in results:
            if result.status is DeletionStatus.DELETED:
                invalidate(self._config.size_cache, result.prefix)
        save_config(self._config)
        self._deleting = False
        self._refresh_action.setEnabled(True)
        self._update_delete_button()
        self._status.clearMessage()
        if any(r.status is not DeletionStatus.DELETED for r in results):
            show_deletion_summary(self, results)
        self.refresh()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        file_menu = QMenu("&File", self)
        menu_bar.addMenu(file_menu)
        self._refresh_action = file_menu.addAction("&Refresh")
        self._refresh_action.setShortcut("Ctrl+R")
        self._refresh_action.triggered.connect(self.refresh)
        quit_action = file_menu.addAction("&Quit")
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)

    def refresh(self) -> None:
        """Start one full pipeline run; stale runs are dropped via the epoch."""
        if self._deleting:
            return
        self._epoch += 1
        epoch = self._epoch
        self._scan_errors.clear()
        self._scan_total = 0
        self._scan_done = 0
        worker = DiscoveryWorker(self._config.custom_roots, epoch)
        self._workers.add(worker)
        worker.signals.finished.connect(
            lambda payload, ep, w=worker: self._on_discovery_finished(payload, ep, w)
        )
        QThreadPool.globalInstance().start(worker)

    def _on_discovery_finished(
        self,
        payload: tuple[DiscoveryResult, list[Prefix]],
        epoch: int,
        worker: QRunnable | None = None,
    ) -> None:
        self._workers.discard(worker)
        if epoch != self._epoch:
            return
        result, prefixes = payload
        if result.errors:
            self._status.showMessage(f"discovery: {len(result.errors)} warnings", 5000)
        self._config.steam_roots = [str(root.path) for root in result.roots]
        self._libraries = list(result.libraries)

        store = Store()
        store.merge(prefixes)
        for prefix in prefixes:
            cached = load_cached(self._config.size_cache, prefix)
            if cached is not None:
                store.upsert(cached)
        self._store = store
        self._model.set_store(store)
        self._disk_capacity = storage_capacity(
            [library.path for library in self._libraries]
        ).total_bytes
        self._overview.update_data(store.prefixes, self._disk_capacity)

        if not result.roots:
            save_config(self._config)
            self._show_message(_NO_ROOTS_TEXT)
            return
        if not store.prefixes:
            save_config(self._config)
            self._show_message(_NO_PREFIXES_TEXT)
            return
        self._apply_sort()

        pending = [
            prefix
            for prefix in store.prefixes
            if prefix.scan_status is ScanStatus.NOT_SCANNED
            and refresh_needed(self._config.size_cache, prefix)
        ]
        if not pending:
            save_config(self._config)
            return
        self._scan_total = len(pending)
        self._update_progress_text()
        scan_worker = ScanWorker(pending, self._config.size_cache, epoch)
        self._workers.add(scan_worker)
        scan_worker.signals.scan_event.connect(self._on_scan_event)
        scan_worker.signals.finished.connect(
            lambda ep, w=scan_worker: self._on_scan_finished(ep, w)
        )
        QThreadPool.globalInstance().start(scan_worker)

    def _on_scan_event(self, event: ScanEvent, epoch: int) -> None:
        if epoch != self._epoch or not isinstance(event, ScanEvent):
            return
        if event.kind is ScanEventKind.STARTED:
            assert isinstance(event.prefix, Prefix)
            scanning = replace(event.prefix, scan_status=ScanStatus.SCANNING)
            self._store.upsert(scanning)
            self._apply_filters(rescan_openable=False)
            return
        assert isinstance(event.prefix, Prefix)
        self._store.upsert(event.prefix)
        if event.kind is ScanEventKind.FAILED and event.error:
            self._scan_errors[cache_key(event.prefix)] = event.error
        self._scan_done += 1
        self._update_progress_text()
        if self._sort_key == "size":
            self._apply_sort()
        else:
            self._apply_filters(rescan_openable=False)
        self._overview.update_data(self._store.prefixes, self._disk_capacity)

    def _on_scan_finished(self, epoch: int, worker: QRunnable | None = None) -> None:
        self._workers.discard(worker)
        if epoch != self._epoch:
            return
        if self._status.currentMessage().startswith("Scanning sizes"):
            self._status.clearMessage()
        save_config(self._config)

    def _on_sort_column_clicked(self, section: int) -> None:
        key = SORTABLE_COLUMNS.get(section)
        if key is None:
            return
        if key == self._sort_key:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_key = key
            self._sort_descending = True
        self._config.sort_column = self._sort_key
        self._config.sort_ascending = not self._sort_descending
        self._apply_initial_sort_indicator()
        self._apply_sort()
        save_config(self._config)

    def _on_header_toggle(self) -> None:
        self._model.toggle_visible_selection()
        self._table.set_header_state(self._model.selection_state())

    def _set_type_filter(self, types: set[PrefixType]) -> None:
        """Replace the active type filter, syncing menu checks and config."""
        self._filter_types = set(types)
        self._config.type_filter = [
            prefix_type for prefix_type in PrefixType if prefix_type in self._filter_types
        ]
        for prefix_type, action in self._type_actions.items():
            action.blockSignals(True)
            action.setChecked(prefix_type in self._filter_types)
            action.blockSignals(False)
        self._update_type_summary()
        save_config(self._config)

    def _set_page(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        for button, page in (
            (self._overview_button, _PAGE_OVERVIEW),
            (self._prefixes_button, _PAGE_PREFIXES),
        ):
            button.blockSignals(True)
            button.setChecked(page == index)
            button.blockSignals(False)

    def _on_prefix_focus_requested(self, prefix: object) -> None:
        if not isinstance(prefix, Prefix):
            return
        self._pending_filter_reset = True
        self._pending_select_all_visible = False
        self._pending_highlight_key = prefix_key(prefix)
        self._set_page(_PAGE_PREFIXES)
        self._run_pending_handoff()

    def _on_orphan_review_requested(self) -> None:
        self._pending_filter_reset = False
        self._pending_select_all_visible = True
        self._pending_highlight_key = None
        self._set_type_filter({PrefixType.ORPHANED})
        self._set_page(_PAGE_PREFIXES)
        self._run_pending_handoff()

    def _run_pending_handoff(self) -> None:
        """Carry handoff intent through the window, never through route parameters."""
        if self._pending_filter_reset:
            self._pending_filter_reset = False
            self._set_type_filter(set(PrefixType))
            self._search_text = ""
            self._search_box.blockSignals(True)
            self._search_box.clear()
            self._search_box.blockSignals(False)
            self._search_timer.stop()
            self._update_search_placeholder()
        if self._pending_select_all_visible:
            self._pending_select_all_visible = False
            self._apply_filters()
            self._store.select_visible(self._model.rows())
            self._model.refresh_all_check_states()
            self._table.set_header_state(self._model.selection_state())
        key = self._pending_highlight_key
        if key is not None:
            self._pending_highlight_key = None
            self._apply_filters()
            for row, row_prefix in enumerate(self._model.rows()):
                if prefix_key(row_prefix) == key:
                    self._table.scrollTo(self._model.index(row, 0))
                    self._model.highlight_row(row_prefix)
                    break
        self._update_delete_button()

    def _on_table_clicked(self, index) -> None:
        if index.column() != OPEN_COLUMN:
            return
        prefix = self._model.row_at(index.row())
        if prefix is None or not can_open(prefix.path):
            return
        worker = OpenFolderWorker(prefix.path)
        self._workers.add(worker)
        worker.signals.finished.connect(lambda result, w=worker: self._on_folder_opened(result, w))
        QThreadPool.globalInstance().start(worker)

    def _on_folder_opened(self, result, worker: QRunnable | None = None) -> None:
        self._workers.discard(worker)
        if result.status is OpenStatus.OPENED:
            self._status.showMessage(f"opened {result.path}", 3000)
        else:
            detail = f": {result.error}" if result.error else ""
            self._status.showMessage(
                f"could not open {result.path} ({result.status.value}{detail})", 5000
            )

    def _apply_sort(self) -> None:
        self._store.sort(key=self._sort_key, descending=self._sort_descending)
        self._apply_filters()

    def _apply_initial_sort_indicator(self) -> None:
        header = self._table.horizontalHeader()
        section_by_key = {key: sec for sec, key in SORTABLE_COLUMNS.items()}
        section = section_by_key.get(self._sort_key, SIZE_COLUMN)
        order = (
            Qt.SortOrder.DescendingOrder if self._sort_descending else Qt.SortOrder.AscendingOrder
        )
        header.setSortIndicator(section, order)

    def _show_message(self, text: str) -> None:
        self._message_label.setText(text)
        if QApplication.instance() is not None:
            icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
            self._message_icon_label.setPixmap(
                icon.pixmap(MESSAGE_ICON_SIZE_PX, MESSAGE_ICON_SIZE_PX)
            )
        self._stack.setCurrentIndex(_PAGE_MESSAGE)

    def _show_inner_message(self, text: str) -> None:
        self._inner_message_label.setText(text)
        if QApplication.instance() is not None:
            icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
            self._inner_message_icon_label.setPixmap(
                icon.pixmap(MESSAGE_ICON_SIZE_PX, MESSAGE_ICON_SIZE_PX)
            )

    def _update_progress_text(self) -> None:
        if self._scan_total <= 0:
            return
        self._status.showMessage(f"Scanning sizes... {self._scan_done}/{self._scan_total}")

    def _scan_error_for(self, prefix: Prefix) -> str | None:
        return self._scan_errors.get(cache_key(prefix))

    def closeEvent(self, event) -> None:
        QThreadPool.globalInstance().waitForDone(_SCAN_WAIT_MS)
        super().closeEvent(event)
