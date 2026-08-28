"""Settings dialog for Steam roots and application appearance.

The dialog never reads global state: the caller passes the current config
plus the last discovery result, and reads validated values back after the
dialog is accepted.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QFontDatabase, QValidator
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig
from core.discovery import Library, SteamRoot

DEFAULT_FONT_SIZE = 10
MIN_FONT_SIZE = 7
MAX_FONT_SIZE = 20
SYSTEM_DEFAULT_LABEL = "(System Default)"


class _FontSizeSpin(QSpinBox):
    """Spin box that clamps typed values into range instead of reverting.

    QSpinBox's built-in correction reverts out-of-range typed input to the
    previous value, so a user typing "1" silently keeps the old size. Both
    bounds are resolved here: below range clamps to the minimum, above
    range to the maximum.
    """

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:
        if self._numeric_part(text).isdigit():
            return QValidator.State.Acceptable, text, pos
        return super().validate(text, pos)

    def valueFromText(self, text: str) -> int:
        part = self._numeric_part(text)
        if not part.isdigit():
            return self.value()
        return min(max(int(part), self.minimum()), self.maximum())

    def _numeric_part(self, text: str) -> str:
        return text.removesuffix(self.suffix()).strip()


class SettingsDialog(QDialog):
    """Edit custom Steam roots and the application font."""

    redetect_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None,
        config: AppConfig,
        *,
        discovered_roots: Sequence[SteamRoot] = (),
        libraries: Sequence[Library] = (),
        focus_add_root: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(640, 520)
        self.setMinimumSize(560, 420)
        self._font_family_value: str | None = config.font_family
        self._custom_roots: list[str] = list(config.custom_roots)
        self._focus_add_root = focus_add_root

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs)

        self._tabs.addTab(
            self._build_locations_page(discovered_roots, libraries), "Steam Locations"
        )
        self._tabs.addTab(self._build_appearance_page(config), "Appearance")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_locations_page(
        self, discovered_roots: Sequence[SteamRoot], libraries: Sequence[Library]
    ) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel("Detected Steam roots"))
        self._resolved_list = QListWidget(page)
        self._resolved_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._resolved_list.setToolTip(
            "Found automatically; these cannot be edited here and disappear when absent on disk."
        )
        if discovered_roots:
            for root in discovered_roots:
                self._resolved_list.addItem(f"{root.path}  ({root.source.value})")
        else:
            self._resolved_list.addItem("None found")
        layout.addWidget(self._resolved_list)

        layout.addWidget(QLabel("Libraries"))
        self._libraries_list = QListWidget(page)
        self._libraries_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._populate_libraries(discovered_roots, libraries)
        layout.addWidget(self._libraries_list)

        layout.addWidget(QLabel("Custom Steam roots"))
        master_detail = QHBoxLayout()
        self._custom_list = QListWidget(page)
        self._custom_list.setMinimumHeight(96)
        self._reload_custom_items()
        master_detail.addWidget(self._custom_list, stretch=1)
        buttons_column = QVBoxLayout()
        self._add_button = QPushButton("Add...", page)
        self._add_button.clicked.connect(self._on_add_clicked)
        self._change_button = QPushButton("Change...", page)
        self._change_button.clicked.connect(self._on_change_clicked)
        self._remove_button = QPushButton("Remove", page)
        self._remove_button.clicked.connect(self._on_remove_clicked)
        self._redetect_button = QPushButton("Re-detect", page)
        self._redetect_button.setToolTip("Run discovery again; manually added roots are kept.")
        self._redetect_button.clicked.connect(self._on_redetect_clicked)
        for button in (
            self._add_button,
            self._change_button,
            self._remove_button,
            self._redetect_button,
        ):
            buttons_column.addWidget(button)
        buttons_column.addStretch(1)
        master_detail.addLayout(buttons_column)
        layout.addLayout(master_detail)
        return page

    def _build_appearance_page(self, config: AppConfig) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        self._font_search = QLineEdit(page)
        self._font_search.setPlaceholderText("Search fonts...")
        self._font_search.setClearButtonEnabled(True)
        self._font_search.textChanged.connect(self._on_font_search_changed)
        layout.addWidget(self._font_search)

        self._font_list = QListWidget(page)
        self._font_list.addItem(SYSTEM_DEFAULT_LABEL)
        for family in sorted(QFontDatabase.families(), key=str.casefold):
            self._font_list.addItem(family)
        self.set_selected_family(self._font_family_value)
        self._font_list.currentItemChanged.connect(self._on_family_selected)
        layout.addWidget(self._font_list, stretch=1)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Font size"))
        self._font_size_spin = _FontSizeSpin(page)
        self._font_size_spin.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self._font_size_spin.setCorrectionMode(
            QAbstractSpinBox.CorrectionMode.CorrectToNearestValue
        )
        self._font_size_spin.setKeyboardTracking(False)
        self._font_size_spin.setSuffix(" pt")
        self._font_size_spin.setToolTip(f"Allowed range: {MIN_FONT_SIZE} to {MAX_FONT_SIZE} pt")
        self._font_size_spin.setValue(config.font_size)
        size_row.addWidget(self._font_size_spin)
        self._reset_font_button = QPushButton("Reset to default", page)
        self._reset_font_button.setToolTip("Restores defaults; takes effect after OK.")
        self._reset_font_button.clicked.connect(self._on_reset_font_clicked)
        size_row.addWidget(self._reset_font_button)
        size_row.addStretch(1)
        layout.addLayout(size_row)
        return page

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Seed keyboard focus on Add once the dialog is on screen."""
        if self._focus_add_root:
            self._tabs.setCurrentIndex(0)
            QTimer.singleShot(0, self._add_button.setFocus)
        super().showEvent(event)

    def _populate_libraries(self, roots: Sequence[SteamRoot], libraries: Sequence[Library]) -> None:
        """List each unique library once; discovery registers the root itself
        as its own first library, so filtering by path identity prevents the
        duplicated line the plain grouping produced."""
        source_by_root = {str(root.path): root.source for root in roots}
        seen: set[str] = set()
        entries: list[str] = []

        def add(library: Library) -> None:
            key = str(library.path)
            if key in seen:
                return
            seen.add(key)
            owner = source_by_root.get(str(library.root))
            suffix = f"   (root: {owner.value})" if owner is not None else ""
            entries.append(f"{key}{suffix}")

        for root in roots:
            for library in libraries:
                if str(library.root) == str(root.path):
                    add(library)
        for library in libraries:
            add(library)

        if entries:
            for entry in entries:
                self._libraries_list.addItem(entry)
        else:
            self._libraries_list.addItem("No libraries found.")

    def _reload_custom_items(self) -> None:
        self._custom_list.clear()
        for entry in self._custom_roots:
            self._custom_list.addItem(entry)

    def _selected_row(self) -> int | None:
        row = self._custom_list.currentRow()
        return row if 0 <= row < len(self._custom_roots) else None

    def _warn(self, message: str) -> None:
        QMessageBox.warning(self, "Settings", message)

    def _pick_directory(self, *, current: str | None = None) -> str | None:
        start = current if current and Path(current).is_dir() else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose a Steam root folder", start)
        if not chosen:
            return None
        resolved = Path(chosen).expanduser().resolve(strict=False)
        if not resolved.is_dir():
            self._warn(f"{resolved} does not exist or is not a directory.")
            return None
        normalized = str(resolved)
        if normalized in self._custom_roots:
            self._warn(f"{normalized} is already in the list.")
            return None
        return normalized

    def _on_add_clicked(self) -> None:
        entry = self._pick_directory()
        if entry is None:
            return
        self._custom_roots.append(entry)
        self._reload_custom_items()
        self._custom_list.setCurrentRow(len(self._custom_roots) - 1)

    def _on_change_clicked(self) -> None:
        row = self._selected_row()
        if row is None:
            self._warn("Select a custom root to change first.")
            return
        entry = self._pick_directory(current=self._custom_roots[row])
        if entry is None:
            return
        self._custom_roots[row] = entry
        self._reload_custom_items()
        self._custom_list.setCurrentRow(row)

    def _on_remove_clicked(self) -> None:
        row = self._selected_row()
        if row is None:
            self._warn("Select a custom root to remove first.")
            return
        del self._custom_roots[row]
        self._reload_custom_items()

    def _on_redetect_clicked(self) -> None:
        self.redetect_requested.emit()
        self.reject()

    def _on_family_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None = None
    ) -> None:
        if current is None:
            return
        self._font_family_value = None if current.text() == SYSTEM_DEFAULT_LABEL else current.text()

    def _on_font_search_changed(self, text: str) -> None:
        needle = text.strip().casefold()
        for row in range(self._font_list.count()):
            item = self._font_list.item(row)
            if row == 0:
                item.setHidden(False)
                continue
            item.setHidden(bool(needle) and needle not in item.text().casefold())

    def _on_reset_font_clicked(self) -> None:
        self._font_search.clear()
        self._font_family_value = None
        self._font_size_spin.setValue(DEFAULT_FONT_SIZE)
        self.set_selected_family(None)

    def set_selected_family(self, value: str | None) -> None:
        """Preselect a family row without touching the pending family value.

        None selects the System Default row; a configured family that no
        longer exists in the font database falls back there too, so a stale
        name never leaves the wrong row highlighted.
        """
        row = self._family_row(value) if value is not None else -1
        if row < 0:
            row = 0
        self._font_list.blockSignals(True)
        self._font_list.setCurrentRow(row)
        self._font_list.blockSignals(False)

    def _family_row(self, family: str | None) -> int:
        if family is None:
            return -1
        for row in range(self._font_list.count()):
            if self._font_list.item(row).text() == family:
                return row
        return -1

    def _validation_problem(self) -> str | None:
        for entry in self._custom_roots:
            path = Path(entry).expanduser().resolve(strict=False)
            if not path.is_dir():
                return f"{entry} does not exist or is not a directory."
        return None

    def accept(self) -> None:
        problem = self._validation_problem()
        if problem is not None:
            self._warn(problem)
            return
        super().accept()

    def custom_roots(self) -> list[str]:
        """Validated custom roots at acceptance time."""
        return list(self._custom_roots)

    def font_family(self) -> str | None:
        """Selected family, or None when left on the system default."""
        return self._font_family_value

    def font_size(self) -> int:
        """Selected point size within the allowed range."""
        return self._font_size_spin.value()
