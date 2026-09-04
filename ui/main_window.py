"""Main application window orchestrating discovery, scanning, and display."""

from __future__ import annotations

from collections.abc import Sequence
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
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QTableView,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.analytics import storage_capacity
from core.config import load_config, save_config
from core.deletion import (
    DeleteMode,
    DeletionResult,
    DeletionStatus,
    ToolDeletionResult,
)
from core.discovery import DiscoveryResult, Library, SteamRoot
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
from core.toolmap import ToolMapError, load_tool_mapping, tool_name_for
from core.tools import SYSTEM_TOOLS_DIR, Tool, enumerate_tools, used_by
from ui.dialogs import (
    confirm_final,
    confirm_selection,
    show_deletion_summary,
    tools_pending_note_for,
    unscanned_note_for,
)
from ui.overview import OverviewPage
from ui.settings import SettingsDialog
from ui.styles import (
    CHECK_WIDTH_PX,
    MESSAGE_ICON_SIZE_PX,
    MESSAGE_LAYOUT_SPACING_PX,
    MESSAGE_PAGE_MARGIN_PX,
    SEARCH_DEBOUNCE_MS,
    apply_app_style,
)
from ui.table import (
    OPEN_COLUMN,
    SIZE_COLUMN,
    SORTABLE_COLUMNS,
    PrefixTable,
    PrefixTableModel,
    shared_view_settings,
)
from ui.workers import (
    DeletionWorker,
    DiscoveryWorker,
    OpenFolderWorker,
    ScanWorker,
    ToolDeletionWorker,
    ToolSizeWorker,
)

_PAGE_MESSAGE = 0
_PAGE_CONTENT = 1

_PAGE_OVERVIEW = 0
_PAGE_PREFIXES = 1
_PAGE_TOOLS = 2

_INNER_PAGE_MESSAGE = 0
_INNER_PAGE_TABLE = 1

_NO_ROOTS_TEXT = (
    "Couldn't locate your Steam folder automatically.\n"
    "Use Locate Steam Folder below to point the app at your installation."
)
_NO_PREFIXES_TEXT = "Steam libraries were found, but no Proton prefixes exist yet."
_NO_ROWS_TEXT = "No prefixes match the current view."
_NO_TOOLS_ROWS_TEXT = "No tools match the current view."

_SCAN_WAIT_MS = 2000

_TYPE_LABELS = {
    PrefixType.STEAM: "Steam Games",
    PrefixType.NON_STEAM: "Non-Steam Games",
    PrefixType.ORPHANED: "Orphaned",
}
_SEARCH_TARGETS = {"name": "Name", "app_id": "AppID"}

_TOOL_CHECK_COLUMN = 0
_TOOL_NAME_COLUMN = 1
_TOOL_SIZE_COLUMN = 2
_TOOL_STATUS_COLUMN = 3
_TOOL_COLUMN_COUNT = 4

_TOOL_HEADER_LABELS = {
    _TOOL_NAME_COLUMN: "Name",
    _TOOL_SIZE_COLUMN: "Size",
    _TOOL_STATUS_COLUMN: "Status",
}

_TOOL_STATUS_USED = "Used"
_TOOL_STATUS_READ_ONLY = "Read-only"
_TOOL_STATUS_UNUSED = "Unused"

_TOOL_SORTABLE_COLUMNS = {
    _TOOL_NAME_COLUMN: "name",
    _TOOL_SIZE_COLUMN: "size",
    _TOOL_STATUS_COLUMN: "status",
}

_TOOL_STATUS_FILTERS = (_TOOL_STATUS_USED, _TOOL_STATUS_UNUSED, _TOOL_STATUS_READ_ONLY)

_TOOL_MODEL_ROOT_INDEX = QModelIndex()


def _tool_status(tool: Tool, used: set[str]) -> str:
    if str(tool.path) in used:
        return _TOOL_STATUS_USED
    if tool.read_only:
        return _TOOL_STATUS_READ_ONLY
    return _TOOL_STATUS_UNUSED


def _tool_facets(tool: Tool, used: set[str]) -> set[str]:
    """Status labels applying to a tool; used and read-only can overlap."""
    facets: set[str] = set()
    if str(tool.path) in used:
        facets.add(_TOOL_STATUS_USED)
    if tool.read_only:
        facets.add(_TOOL_STATUS_READ_ONLY)
    if not facets:
        facets.add(_TOOL_STATUS_UNUSED)
    return facets


class ToolTableModel(QAbstractTableModel):
    """Read-only tool rows with checkboxes limited to deletable tools.

    Only unused writable tools are checkable. Used and read-only tools
    render without a checkbox and cannot be selected for delete.
    """

    selection_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._tools: list[Tool] = []
        self._used_by: dict[str, list[int]] = {}
        self._names: dict[int, str] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._selected: set[str] = set()

    def set_items(
        self,
        tools: Sequence[Tool],
        used_by: dict[str, list[int]],
        names: dict[int, str],
    ) -> None:
        self.beginResetModel()
        self._tools = list(tools)
        self._used_by = dict(used_by)
        self._names = dict(names)
        self._pending = {str(tool.path) for tool in tools}
        self._failed.clear()
        self._selected.clear()
        self.endResetModel()
        self.selection_changed.emit()

    @property
    def used(self) -> set[str]:
        return set(self._used_by)

    @property
    def pending(self) -> set[str]:
        return set(self._pending)

    def set_tool_size(self, path: str, size_bytes: int, error: str | None) -> None:
        """Store one async size result and refresh its size cell."""
        self._pending.discard(path)
        if error is not None:
            self._failed.add(path)
        else:
            self._failed.discard(path)
        for row, tool in enumerate(self._tools):
            if str(tool.path) == path:
                self._tools[row] = replace(tool, size_bytes=size_bytes)
                self.dataChanged.emit(
                    self.index(row, _TOOL_SIZE_COLUMN),
                    self.index(row, _TOOL_SIZE_COLUMN),
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
        if self._tools:
            self.dataChanged.emit(
                self.index(0, _TOOL_SIZE_COLUMN),
                self.index(len(self._tools) - 1, _TOOL_SIZE_COLUMN),
            )

    def selected_tools(self) -> list[Tool]:
        return [tool for tool in self._tools if str(tool.path) in self._selected]

    def rows(self) -> list[Tool]:
        return list(self._tools)

    def is_selected(self, tool: Tool) -> bool:
        return str(tool.path) in self._selected

    def is_deletable(self, tool: Tool) -> bool:
        return str(tool.path) not in self.used and not tool.read_only

    def set_visible(self, tools: Sequence[Tool]) -> None:
        """Re-slice visible rows, dropping selections hidden by the filter."""
        self.beginResetModel()
        self._tools = list(tools)
        visible = {str(tool.path) for tool in tools}
        self._selected = {key for key in self._selected if key in visible}
        self.endResetModel()
        self.selection_changed.emit()

    def set_visible_deletable_selected(self, selected: bool) -> None:
        """Check or clear every deletable visible row."""
        for tool in self._tools:
            if self.is_deletable(tool):
                key = str(tool.path)
                if selected:
                    self._selected.add(key)
                else:
                    self._selected.discard(key)
        if self._tools:
            self.dataChanged.emit(
                self.index(0, _TOOL_CHECK_COLUMN),
                self.index(len(self._tools) - 1, _TOOL_CHECK_COLUMN),
            )
        self.selection_changed.emit()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _TOOL_MODEL_ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self._tools)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = _TOOL_MODEL_ROOT_INDEX
    ) -> int:
        return _TOOL_COLUMN_COUNT

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        if not 0 <= index.row() < len(self._tools):
            return None
        tool = self._tools[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_text(tool, column)
        if column == _TOOL_CHECK_COLUMN and role == Qt.ItemDataRole.CheckStateRole:
            if not self.is_deletable(tool):
                return None
            selected = str(tool.path) in self._selected
            state = Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
            return int(state.value)
        if role == Qt.ItemDataRole.ToolTipRole and column == _TOOL_NAME_COLUMN:
            return self._name_tooltip(tool)
        if role == Qt.ItemDataRole.ToolTipRole and column == _TOOL_CHECK_COLUMN:
            if not self.is_deletable(tool):
                return self._lock_reason(tool)
        if (
            column == _TOOL_CHECK_COLUMN
            and role == Qt.ItemDataRole.DecorationRole
            and not self.is_deletable(tool)
            and QApplication.instance() is not None
        ):
            icon: QIcon = QApplication.style().standardIcon(
                QStyle.StandardPixmap.SP_MessageBoxInformation
            )
            if icon.isNull():
                return None
            return icon
        if role == Qt.ItemDataRole.TextAlignmentRole and column == _TOOL_SIZE_COLUMN:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not 0 <= index.row() < len(self._tools):
            return base
        if index.column() == _TOOL_CHECK_COLUMN and self.is_deletable(self._tools[index.row()]):
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != _TOOL_CHECK_COLUMN:
            return False
        if not 0 <= index.row() < len(self._tools):
            return False
        tool = self._tools[index.row()]
        if not self.is_deletable(tool):
            return False
        checked = value in (Qt.CheckState.Checked, int(Qt.CheckState.Checked.value), True)
        key = str(tool.path)
        if checked:
            self._selected.add(key)
        else:
            self._selected.discard(key)
        self.dataChanged.emit(index, index)
        self.selection_changed.emit()
        return True

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if orientation != Qt.Orientation.Horizontal or role != Qt.ItemDataRole.DisplayRole:
            return None
        return _TOOL_HEADER_LABELS.get(section)

    def _display_text(self, tool: Tool, column: int) -> str:
        if column == _TOOL_NAME_COLUMN:
            return tool.name
        if column == _TOOL_SIZE_COLUMN:
            if str(tool.path) in self._pending:
                return "Scanning..."
            if str(tool.path) in self._failed:
                return "Unavailable"
            return format_size(tool.size_bytes)
        if column == _TOOL_STATUS_COLUMN:
            return _tool_status(tool, self.used)
        return ""

    def _name_tooltip(self, tool: Tool) -> str:
        lines: list[str] = []
        app_ids = self._used_by.get(str(tool.path), [])
        if app_ids:
            parts: list[str] = []
            for app_id in app_ids:
                name = self._names.get(app_id)
                if name:
                    parts.append(f"{name} ({app_id})")
                else:
                    parts.append(f"AppID {app_id}")
            lines.append("Used by: " + ", ".join(parts))
        if tool.read_only:
            lines.append("Read-only: this build cannot be removed from this app.")
        lines.append(str(tool.path))
        return "\n".join(lines)

    def _lock_reason(self, tool: Tool) -> str:
        # Tooltip-only HTML: DisplayRole never returns markup, so plain-text
        # cells cannot leak these tags. The bold tail is for tooltips only.
        tail = "<b>It cannot be removed from this app.</b>"
        if tool.read_only:
            system = SYSTEM_TOOLS_DIR.resolve(strict=False)
            if tool.path == system or system in tool.path.parents:
                return f"System-owned: this build belongs to the package manager. {tail}"
            return f"Steam-managed: this build belongs to Steam. {tail}"
        return f"In use: this build is selected by a game. {tail}"


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
        self._roots: list[SteamRoot] = []
        self._tool_mapping: dict[int, str] = {}
        self._tool_errors: list[ToolMapError] = []
        self._warning_count: int = 0
        self._all_tools: list[Tool] = []
        self._tools_search_text = ""
        self._tools_statuses: set[str] = {
            _TOOL_STATUS_USED,
            _TOOL_STATUS_UNUSED,
            _TOOL_STATUS_READ_ONLY,
        }
        self._tools_sort_key = "name"
        self._tools_sort_descending = False
        self._filter_types: set[PrefixType] = set(self._config.type_filter)
        self._search_text = ""
        self._search_target = "name"
        self._deleting = False
        self._delete_total = 0
        self._delete_done = 0
        self._deletion_results: list[DeletionResult] = []
        self._delete_mode: DeleteMode | None = None
        self._tools_deleting = False
        self._tools_delete_total = 0
        self._tools_delete_done = 0
        self._tools_deletion_results: list[ToolDeletionResult] = []
        self._tools_delete_mode: DeleteMode | None = None
        self._disk_capacity: int | None = None
        self._pending_filter_reset = False
        self._pending_select_all_visible = False
        self._pending_highlight_key: tuple[int, str] | None = None
        self._apply_window_font()

        self._model = PrefixTableModel(
            error_provider=self._scan_error_for, tool_provider=self._tool_for
        )
        self._table = PrefixTable(self._model)
        self._store = self._model.store
        self._tools_model = ToolTableModel()

        self._message_icon_label = QLabel()
        self._message_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_icon_label.setFixedSize(MESSAGE_ICON_SIZE_PX, MESSAGE_ICON_SIZE_PX)
        self._message_label = QLabel()
        self._message_label.setWordWrap(True)
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_page = QWidget()
        message_page = self._message_page
        self._locate_button = QPushButton("Locate Steam Folder...", message_page)
        self._locate_button.clicked.connect(self._on_locate_clicked)
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
        message_layout.addWidget(self._locate_button, alignment=Qt.AlignmentFlag.AlignHCenter)
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
        # SummaryCard label fonts are captured once at construction, so
        # live font changes leave the overview cards at their startup
        # sizes until restart; everything else follows immediately.
        self._overview.orphan_review_requested.connect(self._on_orphan_review_requested)
        self._overview.prefix_focus_requested.connect(self._on_prefix_focus_requested)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._overview, "Overview")
        self._tabs.addTab(prefixes_page, "Prefixes")
        self._tabs.addTab(self._build_tools_page(), "Tools")

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self._tabs)

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

    def _build_tools_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build_tools_filter_bar(page))
        self._tools_table = QTableView(page)
        self._tools_table.setModel(self._tools_model)
        header = shared_view_settings(self._tools_table)
        header.setSectionResizeMode(_TOOL_CHECK_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_TOOL_NAME_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_TOOL_SIZE_COLUMN, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_TOOL_STATUS_COLUMN, QHeaderView.ResizeMode.Interactive)
        header.setSortIndicator(_TOOL_NAME_COLUMN, Qt.SortOrder.AscendingOrder)
        header.sectionClicked.connect(self._on_tools_section_clicked)
        self._tools_table.setColumnWidth(_TOOL_CHECK_COLUMN, CHECK_WIDTH_PX)
        self._tools_header_checkbox = QCheckBox(header)
        self._tools_header_checkbox.setTristate(True)
        self._tools_header_checkbox.clicked.connect(self._on_tools_header_toggle)
        header.geometriesChanged.connect(self._reposition_tools_header_checkbox)
        self._tools_model.selection_changed.connect(self._on_tools_selection_changed)
        layout.addWidget(self._tools_table)
        layout.addWidget(self._build_tools_action_bar(page))
        return page

    def _build_tools_filter_bar(self, parent: QWidget) -> QWidget:
        bar = QWidget(parent)
        layout = QHBoxLayout(bar)
        self._tools_status_button = QToolButton(bar)
        self._tools_status_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._tools_status_button.setText("Filters")
        status_menu = QMenu(self._tools_status_button)
        self._tools_status_actions: dict[str, QAction] = {}
        for label in _TOOL_STATUS_FILTERS:
            action = status_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(label in self._tools_statuses)
            action.toggled.connect(
                lambda checked, text=label: self._on_tools_status_toggled(text, checked)
            )
            self._tools_status_actions[label] = action
        self._tools_status_button.setMenu(status_menu)
        self._tools_status_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        layout.addWidget(self._tools_status_button)
        search_frame = QFrame(bar)
        search_frame.setObjectName("toolsSearchFrame")
        search_layout = QHBoxLayout(search_frame)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(0)
        self._tools_search_box = QLineEdit(search_frame)
        self._tools_search_box.setClearButtonEnabled(True)
        self._tools_search_box.setPlaceholderText("Search tools")
        self._tools_search_box.textChanged.connect(self._on_tools_search_text_changed)
        search_layout.addWidget(self._tools_search_box, stretch=1)
        layout.addWidget(search_frame, stretch=1)
        self._tools_search_timer = QTimer(self)
        self._tools_search_timer.setSingleShot(True)
        self._tools_search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._tools_search_timer.timeout.connect(self._apply_tools_filter)
        self._update_tools_status_summary()
        return bar

    def _build_tools_action_bar(self, parent: QWidget) -> QWidget:
        bar = QWidget(parent)
        layout = QHBoxLayout(bar)
        layout.addStretch(1)
        self._delete_tools_button = QPushButton(bar)
        self._delete_tools_button.clicked.connect(self._on_delete_tools_clicked)
        layout.addWidget(self._delete_tools_button)
        self._update_delete_tools_button()
        return bar

    def _update_delete_tools_button(self) -> None:
        count = len(self._tools_model.selected_tools())
        self._delete_tools_button.setText(f"Delete Tools ({count})")
        self._delete_tools_button.setEnabled(
            count > 0 and not self._tools_deleting and not self._deleting
        )

    def _on_tools_selection_changed(self) -> None:
        self._update_delete_tools_button()
        self._update_tools_header_state()

    def _update_tools_header_state(self) -> None:
        rows = self._tools_model.rows()
        deletable = [tool for tool in rows if self._tools_model.is_deletable(tool)]
        box = self._tools_header_checkbox
        box.blockSignals(True)
        if not deletable:
            box.setCheckState(Qt.CheckState.Unchecked)
        else:
            selected = sum(1 for tool in deletable if self._tools_model.is_selected(tool))
            if selected == 0:
                box.setCheckState(Qt.CheckState.Unchecked)
            elif selected == len(deletable):
                box.setCheckState(Qt.CheckState.Checked)
            else:
                box.setCheckState(Qt.CheckState.PartiallyChecked)
        box.blockSignals(False)
        self._reposition_tools_header_checkbox()

    def _reposition_tools_header_checkbox(self) -> None:
        """Center the checkbox inside the tools header check section."""
        header = self._tools_table.horizontalHeader()
        hint = self._tools_header_checkbox.sizeHint()
        section_width = max(header.sectionSize(_TOOL_CHECK_COLUMN), hint.width())
        x = header.sectionPosition(_TOOL_CHECK_COLUMN)
        y = max((header.height() - hint.height()) // 2, 0)
        self._tools_header_checkbox.setGeometry(
            x + (section_width - hint.width()) // 2,
            y,
            hint.width(),
            hint.height(),
        )

    def _on_tools_header_toggle(self) -> None:
        rows = self._tools_model.rows()
        deletable = [tool for tool in rows if self._tools_model.is_deletable(tool)]
        if not deletable:
            return
        all_selected = all(self._tools_model.is_selected(tool) for tool in deletable)
        self._tools_model.set_visible_deletable_selected(not all_selected)

    def _tools_status_summary_text(self) -> str:
        # Both filter buttons read Filters and expose the facet in the
        # tooltip, matching the prefixes tab which uses Types: here.
        checked = [label for label in _TOOL_STATUS_FILTERS if label in self._tools_statuses]
        if not checked:
            return "Status: None"
        return "Status: " + " + ".join(checked)

    def _update_tools_status_summary(self) -> None:
        self._tools_status_button.setToolTip(self._tools_status_summary_text())

    def _on_tools_status_toggled(self, label: str, checked: bool) -> None:
        if checked:
            self._tools_statuses.add(label)
        else:
            self._tools_statuses.discard(label)
        self._update_tools_status_summary()
        self._apply_tools_filter()

    def _on_tools_search_text_changed(self, text: str) -> None:
        self._tools_search_text = text.strip()
        self._tools_search_timer.start()

    def _apply_tools_filter(self) -> None:
        """Re-slice all tools through status and search into the model."""
        text = self._tools_search_text.strip().casefold()
        used = self._tools_model.used
        visible = [
            tool
            for tool in self._all_tools
            if _tool_facets(tool, used) & self._tools_statuses
            and (not text or text in tool.name.casefold())
        ]
        if self._tools_sort_key == "size":
            visible.sort(key=lambda tool: tool.size_bytes, reverse=self._tools_sort_descending)
        elif self._tools_sort_key == "status":
            visible.sort(
                key=lambda tool: _tool_status(tool, used),
                reverse=self._tools_sort_descending,
            )
        else:
            visible.sort(key=lambda tool: tool.name.casefold(), reverse=self._tools_sort_descending)
        self._tools_model.set_visible(visible)
        if not visible and self._all_tools:
            self._status.showMessage(_NO_TOOLS_ROWS_TEXT, 5000)
        elif self._status.currentMessage() == _NO_TOOLS_ROWS_TEXT:
            self._status.clearMessage()

    def _on_tools_section_clicked(self, section: int) -> None:
        key = _TOOL_SORTABLE_COLUMNS.get(section)
        if key is None:
            return
        if key == self._tools_sort_key:
            self._tools_sort_descending = not self._tools_sort_descending
        else:
            self._tools_sort_key = key
            self._tools_sort_descending = key == "size"
        order = (
            Qt.SortOrder.DescendingOrder
            if self._tools_sort_descending
            else Qt.SortOrder.AscendingOrder
        )
        self._tools_table.horizontalHeader().setSortIndicator(section, order)
        self._apply_tools_filter()

    def _on_delete_tools_clicked(self) -> None:
        tools = self._tools_model.selected_tools()
        if not tools or self._tools_deleting or self._deleting:
            return
        names = [tool.name for tool in tools]
        total_text = format_size(sum(tool.size_bytes for tool in tools))
        note = tools_pending_note_for(tools, self._tools_model.pending)
        if not confirm_selection(self, names, total_text, note, item_noun="tools"):
            return
        mode = confirm_final(self, len(tools), total_text, item_noun="tool(s)")
        if mode is None:
            return
        self._tools_deleting = True
        self._tools_delete_total = len(tools)
        self._tools_delete_done = 0
        self._tools_deletion_results.clear()
        self._tools_delete_mode = mode
        self._update_delete_button()
        self._update_delete_tools_button()
        self._refresh_action.setEnabled(False)
        self._status.showMessage(f"Deleting 0/{self._tools_delete_total}")
        epoch = self._epoch
        worker = ToolDeletionWorker(tools, self._roots, mode, epoch)
        self._workers.add(worker)
        worker.signals.result_ready.connect(self._on_tool_deletion_result)
        worker.signals.finished.connect(lambda ep, w=worker: self._on_tool_deletion_finished(ep, w))
        QThreadPool.globalInstance().start(worker)

    def _on_tool_deletion_result(self, result: ToolDeletionResult, epoch: int) -> None:
        if epoch != self._epoch:
            return
        self._tools_deletion_results.append(result)
        self._tools_delete_done += 1
        self._status.showMessage(f"Deleting {self._tools_delete_done}/{self._tools_delete_total}")

    def _on_tool_deletion_finished(self, epoch: int, worker: QRunnable | None = None) -> None:
        self._workers.discard(worker)
        if epoch != self._epoch:
            self._tools_deleting = False
            if not self._deleting and not self._tools_deleting:
                self._refresh_action.setEnabled(True)
            self._update_delete_button()
            self._update_delete_tools_button()
            if self._status.currentMessage().startswith("Deleting"):
                self._status.clearMessage()
            return
        results = list(self._tools_deletion_results)
        self._tools_deleting = False
        if not self._deleting and not self._tools_deleting:
            self._refresh_action.setEnabled(True)
        self._update_delete_button()
        self._update_delete_tools_button()
        self._status.clearMessage()
        if any(result.status is not DeletionStatus.DELETED for result in results):
            show_deletion_summary(self, results)
        self.refresh()

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
        self._table.set_header_state(self._model.selection_state(exclude_runtime=True))
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
        self._delete_button.setEnabled(
            count > 0 and not self._deleting and not self._tools_deleting
        )

    def _on_delete_clicked(self) -> None:
        selected = self._store.selected()
        if not selected or self._deleting or self._tools_deleting:
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
        self._update_delete_tools_button()
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
            self._deleting = False
            if not self._deleting and not self._tools_deleting:
                self._refresh_action.setEnabled(True)
            self._update_delete_button()
            self._update_delete_tools_button()
            if self._status.currentMessage().startswith("Deleting"):
                self._status.clearMessage()
            return
        results = list(self._deletion_results)
        for result in results:
            if result.status is DeletionStatus.DELETED:
                invalidate(self._config.size_cache, result.prefix)
        save_config(self._config)
        self._deleting = False
        if not self._deleting and not self._tools_deleting:
            self._refresh_action.setEnabled(True)
        self._update_delete_button()
        self._update_delete_tools_button()
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
        settings_menu = QMenu("&Settings", self)
        menu_bar.addMenu(settings_menu)
        self._settings_action = settings_menu.addAction("&Settings...")
        self._settings_action.setShortcut("Ctrl+,")
        self._settings_action.triggered.connect(self._open_settings)

    def _apply_window_font(self) -> None:
        """Apply the configured font through the shared application path.

        Runtime changes must match what a restart would produce, so this
        delegates to ui.styles.apply_app_style. A window-level font loses
        to the application-wide font established at startup, which made
        accepted settings appear ineffective until the next launch.
        """
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_app_style(app, self._config)

    def _open_settings(self, *, focus_add_root: bool = False) -> None:
        """Show the settings dialog and persist accepted values."""
        dialog = SettingsDialog(
            self,
            self._config,
            discovered_roots=self._roots,
            libraries=self._libraries,
            focus_add_root=focus_add_root,
        )
        dialog.redetect_requested.connect(self.refresh)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._config.custom_roots = dialog.custom_roots()
        self._config.font_family = dialog.font_family()
        self._config.font_size = dialog.font_size()
        save_config(self._config)
        self._apply_window_font()
        self.refresh()

    def _on_locate_clicked(self) -> None:
        self._open_settings(focus_add_root=True)

    def refresh(self) -> None:
        """Start one full pipeline run; stale runs are dropped via the epoch."""
        if self._deleting or self._tools_deleting:
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
        self._roots = list(result.roots)
        self._config.steam_roots = [str(root.path) for root in result.roots]
        self._libraries = list(result.libraries)
        self._tool_mapping, self._tool_errors = load_tool_mapping(self._roots)
        self._model.set_tool_provider(self._tool_for)
        tools = enumerate_tools(self._roots, self._libraries)
        prefix_names: dict[int, str] = {}
        for prefix in prefixes:
            prefix_names.setdefault(prefix.app_id, prefix.name)
        self._tools_model.set_items(tools, used_by(tools, self._tool_mapping), prefix_names)
        self._all_tools = list(tools)
        self._apply_tools_filter()
        if tools:
            size_worker = ToolSizeWorker(tools, epoch)
            self._workers.add(size_worker)
            size_worker.signals.sized.connect(self._on_tool_sized)
            size_worker.signals.finished.connect(
                lambda ep, w=size_worker: self._on_tool_size_finished(ep, w)
            )
            try:
                QThreadPool.globalInstance().start(size_worker)
            except RuntimeError:
                self._workers.discard(size_worker)
                self._tools_model.mark_pending_unavailable()
        else:
            self._tools_model.mark_pending_unavailable()
        warning_count = len(result.errors) + len(self._tool_errors)
        self._warning_count = warning_count
        if warning_count:
            self._status.showMessage(f"warnings: {warning_count}", 5000)

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
            self._show_message(_NO_ROOTS_TEXT, locate=True)
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
        if self._warning_count:
            self._status.showMessage(f"warnings: {self._warning_count}", 5000)
        save_config(self._config)

    def _on_tool_sized(self, payload: object, epoch: int) -> None:
        if epoch != self._epoch or not isinstance(payload, tuple) or len(payload) != 3:
            return
        path, size_bytes, error = payload
        if (
            isinstance(path, str)
            and isinstance(size_bytes, int)
            and (error is None or isinstance(error, str))
        ):
            self._all_tools = [
                replace(tool, size_bytes=size_bytes) if str(tool.path) == path else tool
                for tool in self._all_tools
            ]
            self._tools_model.set_tool_size(path, size_bytes, error)
            if self._tools_sort_key == "size":
                self._apply_tools_filter()

    def _on_tool_size_finished(self, epoch: int, worker: QRunnable | None = None) -> None:
        self._workers.discard(worker)
        if epoch != self._epoch:
            return
        self._tools_model.mark_pending_unavailable()

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
        self._table.set_header_state(self._model.selection_state(exclude_runtime=True))

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
        """Switch between the Overview and Prefixes tabs."""
        self._tabs.setCurrentIndex(index)

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
            # Runtime components stay visible but are never auto-selected;
            # mass-deleting them from orphan review would remove Steam's
            # own tooling by accident.
            self._store.select_visible(self._model.rows(), exclude_runtime=True)
            self._model.refresh_all_check_states()
            self._table.set_header_state(self._model.selection_state(exclude_runtime=True))
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

    def _show_message(self, text: str, *, locate: bool = False) -> None:
        self._message_label.setText(text)
        self._locate_button.setVisible(locate)
        if locate:
            self._locate_button.setFocus()
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

    def _tool_for(self, prefix: Prefix) -> str | None:
        tool = tool_name_for(self._tool_mapping, prefix.app_id)
        return tool or None

    def closeEvent(self, event) -> None:
        QThreadPool.globalInstance().waitForDone(_SCAN_WAIT_MS)
        super().closeEvent(event)
