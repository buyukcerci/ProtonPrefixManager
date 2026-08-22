"""Main application window orchestrating discovery, scanning, and display."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QRunnable, Qt, QThreadPool
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from core.config import load_config, save_config
from core.discovery import DiscoveryResult
from core.models import Prefix, ScanStatus, Store
from core.opener import OpenStatus, can_open
from core.scanner import ScanEvent, ScanEventKind, cache_key, load_cached, refresh_needed
from ui.styles import (
    MESSAGE_ICON_SIZE_PX,
    MESSAGE_LAYOUT_SPACING_PX,
    MESSAGE_PAGE_MARGIN_PX,
)
from ui.table import (
    OPEN_COLUMN,
    SIZE_COLUMN,
    SORTABLE_COLUMNS,
    PrefixTable,
    PrefixTableModel,
)
from ui.workers import DiscoveryWorker, OpenFolderWorker, ScanWorker

_PAGE_MESSAGE = 0
_PAGE_TABLE = 1

_NO_ROOTS_TEXT = (
    "No valid Steam root was found.\n"
    "Adding a custom Steam root will arrive with the settings options."
)
_NO_PREFIXES_TEXT = "Steam libraries were found, but no Proton prefixes exist yet."
_NO_ROWS_TEXT = "No prefixes match the current view."

_SCAN_WAIT_MS = 2000


class MainWindow(QMainWindow):
    """Root window hosting the prefix table and its background pipelines."""

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

        self._model = PrefixTableModel(error_provider=self._scan_error_for)
        self._table = PrefixTable(self._model)
        self._store = self._model.store

        self._message_icon_label = QLabel()
        self._message_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_icon_label.setFixedSize(MESSAGE_ICON_SIZE_PX, MESSAGE_ICON_SIZE_PX)
        self._message_label = QLabel()
        self._message_label.setWordWrap(True)
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_page = QWidget()
        message_layout = QVBoxLayout(message_page)
        message_layout.setContentsMargins(
            MESSAGE_PAGE_MARGIN_PX,
            MESSAGE_PAGE_MARGIN_PX,
            MESSAGE_PAGE_MARGIN_PX,
            MESSAGE_PAGE_MARGIN_PX,
        )
        message_layout.setSpacing(MESSAGE_LAYOUT_SPACING_PX)
        message_layout.addStretch(1)
        message_layout.addWidget(self._message_icon_label)
        message_layout.addWidget(self._message_label)
        message_layout.addStretch(2)

        self._stack = QStackedWidget()
        self._stack.addWidget(message_page)
        self._stack.addWidget(self._table)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)
        self.setCentralWidget(container)

        self._build_menu()
        self._status = QStatusBar()
        self.setStatusBar(self._status)

        self._table.sort_column_clicked.connect(self._on_sort_column_clicked)
        self._table.header_toggle_clicked.connect(self._on_header_toggle)
        self._table.clicked.connect(self._on_table_clicked)

        self._apply_initial_sort_indicator()
        if auto_start:
            self.refresh()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        file_menu = QMenu("&File", self)
        menu_bar.addMenu(file_menu)
        refresh_action = file_menu.addAction("&Refresh")
        refresh_action.setShortcut("Ctrl+R")
        refresh_action.triggered.connect(self.refresh)
        quit_action = file_menu.addAction("&Quit")
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)

    def refresh(self) -> None:
        """Start one full pipeline run; stale runs are dropped via the epoch."""
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

        store = Store()
        store.merge(prefixes)
        for prefix in prefixes:
            cached = load_cached(self._config.size_cache, prefix)
            if cached is not None:
                store.upsert(cached)
        self._store = store
        self._model.set_store(store)

        if not result.roots:
            save_config(self._config)
            self._show_message(_NO_ROOTS_TEXT)
            return
        if not store.prefixes:
            save_config(self._config)
            self._show_message(_NO_PREFIXES_TEXT)
            return
        self._stack.setCurrentIndex(_PAGE_TABLE)
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
            self._model.set_rows(self._store.prefixes, rescan_openable=False)
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
            self._model.set_rows(self._store.prefixes)

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
        self._model.apply_sort(self._sort_key, descending=self._sort_descending)

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

    def _update_progress_text(self) -> None:
        if self._scan_total <= 0:
            return
        self._status.showMessage(f"Scanning sizes... {self._scan_done}/{self._scan_total}")

    def _scan_error_for(self, prefix: Prefix) -> str | None:
        return self._scan_errors.get(cache_key(prefix))

    def closeEvent(self, event) -> None:
        QThreadPool.globalInstance().waitForDone(_SCAN_WAIT_MS)
        super().closeEvent(event)
