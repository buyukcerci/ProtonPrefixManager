"""Qt tests for the prefix table model and view wiring."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from core.models import Prefix, PrefixType, ScanStatus, SelectionState, Store, prefix_key
from ui.styles import MIN_HEADER_SECTION_PX
from ui.table import (
    APP_ID_COLUMN,
    CHECK_COLUMN,
    MODIFIED_COLUMN,
    NAME_COLUMN,
    OPEN_COLUMN,
    PATH_COLUMN,
    SIZE_COLUMN,
    SORTABLE_COLUMNS,
    PrefixTable,
    PrefixTableModel,
    format_modified,
)


def _make_prefix(
    base: Path, name: str, app_id: int, *, size: int = 0, missing: bool = False
) -> Prefix:
    path = base / name
    if not missing:
        path.mkdir(parents=True)
        marker = path / "data.bin"
        marker.write_bytes(b"x" * size)
    return Prefix(
        app_id=app_id,
        name=f"Game {app_id}",
        prefix_type=PrefixType.STEAM,
        path=path,
        library=str(base),
        size_bytes=size,
        scan_status=ScanStatus.SCANNED if size else ScanStatus.NOT_SCANNED,
    )


@pytest.fixture()
def populated(tmp_path: Path) -> tuple[PrefixTableModel, Store, list[Prefix]]:
    big = _make_prefix(tmp_path, "big", 1000, size=1536)
    small = _make_prefix(tmp_path, "small", 900, size=900)
    store = Store()
    store.merge([big, small])
    model = PrefixTableModel()
    model.set_store(store)
    return model, store, [big, small]


def test_column_order_and_labels(populated) -> None:
    model, _, _ = populated
    assert model.columnCount() == 7
    labels = [model.headerData(c, Qt.Orientation.Horizontal) for c in range(7)]
    assert labels == [None, "Name", "AppID", "Path", "Size", "Modified", None]
    assert model.rowCount() == 2


def test_size_sort_uses_raw_bytes(populated) -> None:
    model, _, prefixes = populated
    by_bytes = sorted(prefixes, key=lambda p: p.size_bytes, reverse=True)
    model.apply_sort("size", descending=True)
    rows = model.rows()
    assert rows == by_bytes
    display_first = model.index(0, SIZE_COLUMN).data()
    display_second = model.index(1, SIZE_COLUMN).data()
    assert (display_first, display_second) == ("1.5 KB", "900 B")


def test_app_id_sort_is_numeric(tmp_path: Path) -> None:
    ten = _make_prefix(tmp_path / "t", "ten", 10)
    nine = _make_prefix(tmp_path / "n", "nine", 9)
    store = Store()
    store.merge([ten, nine])
    model = PrefixTableModel()
    model.set_store(store)
    model.apply_sort("app_id", descending=False)
    assert [prefix.app_id for prefix in model.rows()] == [9, 10]


def test_tri_state_transitions(qtbot, populated) -> None:
    model, _, _ = populated
    states: list[SelectionState] = []
    model.header_state_changed.connect(states.append)
    assert model.selection_state() is SelectionState.NONE

    index = model.index(0, 0)
    model.setData(index, int(Qt.CheckState.Checked.value), Qt.ItemDataRole.CheckStateRole)
    assert model.selection_state() is SelectionState.SOME

    second = model.index(1, 0)
    model.setData(second, True, Qt.ItemDataRole.CheckStateRole)
    assert model.selection_state() is SelectionState.ALL
    assert states[-1] is SelectionState.ALL


def test_header_toggle_selects_then_clears(populated) -> None:
    model, store, prefixes = populated
    model.toggle_visible_selection()
    assert all(store.is_selected(prefix) for prefix in prefixes)
    assert model.selection_state() is SelectionState.ALL
    model.toggle_visible_selection()
    assert all(not store.is_selected(prefix) for prefix in prefixes)
    assert model.selection_state() is SelectionState.NONE


def test_row_checkbox_updates_store_membership(populated) -> None:
    model, store, prefixes = populated
    index = model.index(0, CHECK_COLUMN)
    model.setData(index, int(Qt.CheckState.Checked.value), Qt.ItemDataRole.CheckStateRole)
    assert store.is_selected(prefixes[0])
    model.setData(index, int(Qt.CheckState.Unchecked.value), Qt.ItemDataRole.CheckStateRole)
    assert not store.is_selected(prefixes[0])


def test_open_action_availability(tmp_path: Path) -> None:
    existing = _make_prefix(tmp_path / "a", "exists", 1)
    ghost = _make_prefix(tmp_path / "b", "ghost", 2, missing=True)
    store = Store()
    store.merge([existing, ghost])
    model = PrefixTableModel()
    model.set_store(store)
    model.apply_sort("app_id", descending=False)

    assert model.open_enabled(0) is True
    assert model.open_enabled(1) is False

    user_flag_existing = model.data(model.index(0, OPEN_COLUMN), Qt.ItemDataRole.UserRole)
    user_flag_missing = model.data(model.index(1, OPEN_COLUMN), Qt.ItemDataRole.UserRole)
    assert user_flag_existing is True
    assert user_flag_missing is False


def test_failed_scan_shows_unavailable_with_error_tooltip(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path, "pfx", 5)
    errors = {str(prefix.path): "[errno 16] Device or resource busy"}
    model = PrefixTableModel(error_provider=lambda p: errors.get(str(p.path)))
    scanned_failed = Prefix(
        app_id=5,
        name=prefix.name,
        prefix_type=PrefixType.ORPHANED,
        path=prefix.path,
        library=prefix.library,
        scan_status=ScanStatus.FAILED,
    )
    model.set_rows([scanned_failed])
    text = model.data(model.index(0, SIZE_COLUMN), Qt.ItemDataRole.DisplayRole)
    tooltip = model.data(model.index(0, SIZE_COLUMN), Qt.ItemDataRole.ToolTipRole)
    assert text == "Unavailable"
    assert tooltip == "[errno 16] Device or resource busy"


def test_scanning_status_text(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path, "pfx", 6)
    scanning = Prefix(
        app_id=6,
        name=prefix.name,
        prefix_type=PrefixType.ORPHANED,
        path=prefix.path,
        library=prefix.library,
        scan_status=ScanStatus.SCANNING,
    )
    model = PrefixTableModel()
    model.set_rows([scanning])
    from PySide6.QtCore import Qt

    assert model.data(model.index(0, SIZE_COLUMN), Qt.ItemDataRole.DisplayRole) == "Scanning..."


def test_responsive_column_configuration(qtbot, populated) -> None:
    from PySide6.QtWidgets import QHeaderView

    model, _, _ = populated
    table = PrefixTable(model)
    qtbot.addWidget(table)
    header = table.horizontalHeader()

    assert header.sectionResizeMode(PATH_COLUMN) is QHeaderView.ResizeMode.Stretch
    assert header.sectionResizeMode(0) is QHeaderView.ResizeMode.Fixed
    assert header.sectionResizeMode(OPEN_COLUMN) is QHeaderView.ResizeMode.Fixed
    for column in (APP_ID_COLUMN, NAME_COLUMN, SIZE_COLUMN, MODIFIED_COLUMN):
        mode = header.sectionResizeMode(column)
        assert mode is QHeaderView.ResizeMode.Interactive
        assert (
            table.columnWidth(column)
            >= {
                APP_ID_COLUMN: 70,
                NAME_COLUMN: 120,
                SIZE_COLUMN: 90,
                MODIFIED_COLUMN: 100,
            }[column]
        )

    assert header.minimumSectionSize() == MIN_HEADER_SECTION_PX


def test_alternating_row_colors_enabled(qtbot, populated) -> None:
    model, _, _ = populated
    table = PrefixTable(model)
    qtbot.addWidget(table)
    assert table.alternatingRowColors() is True


# --- modified column and row highlight ---------------------------------------


def test_modified_column_formats_locale_or_placeholder(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    known = _make_prefix(tmp_path, "known", 1000, size=10)
    known = replace(known, modified=datetime(2026, 7, 1, 10, 0, 0, tzinfo=UTC))
    unknown = _make_prefix(tmp_path / "u", "unknown", 1001, size=10)
    model = PrefixTableModel()
    model.set_rows([known, unknown])

    shown = model.data(model.index(0, MODIFIED_COLUMN), Qt.ItemDataRole.DisplayRole)
    assert shown == format_modified(known.modified)
    assert shown != "-"
    assert model.data(model.index(1, MODIFIED_COLUMN), Qt.ItemDataRole.DisplayRole) == "-"
    alignment = model.data(model.index(0, MODIFIED_COLUMN), Qt.ItemDataRole.TextAlignmentRole)
    assert alignment == int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


def test_modified_column_is_sortable() -> None:
    assert SORTABLE_COLUMNS[MODIFIED_COLUMN] == "modified"


def test_highlight_row_brush_then_clear(qtbot, populated) -> None:
    model, store, prefixes = populated
    table = PrefixTable(model)
    qtbot.addWidget(table)
    model.set_rows(store.prefixes)
    target = prefixes[0]
    index = model.index(0, NAME_COLUMN)

    model.highlight_row(target)
    assert model.highlighted_key() == prefix_key(target)
    assert model.data(index, Qt.ItemDataRole.BackgroundRole) is not None
    assert model.data(index, Qt.ItemDataRole.ForegroundRole) is not None

    model.clear_highlight()
    assert model.highlighted_key() is None
    assert model.data(index, Qt.ItemDataRole.BackgroundRole) is None
    assert model.data(index, Qt.ItemDataRole.ForegroundRole) is None


def test_header_checkbox_centered_in_first_section(qtbot, populated) -> None:
    model, _, _ = populated
    table = PrefixTable(model)
    qtbot.addWidget(table)
    table.resize(600, 300)
    table.show()
    qtbot.waitExposed(table)

    header = table.horizontalHeader()
    checkbox = table.header_checkbox
    section_x = header.sectionPosition(CHECK_COLUMN)
    section_center_x = section_x + header.sectionSize(CHECK_COLUMN) // 2
    checkbox_center = checkbox.geometry().center()

    assert abs(checkbox_center.x() - section_center_x) <= 2
    assert abs(checkbox_center.y() - header.height() // 2) <= 2


def test_runtime_component_name_shows_suffix_and_tooltip(tmp_path: Path, qtbot) -> None:
    runtime = _make_prefix(tmp_path, "runtime", 962960)
    runtime = replace(runtime, name="Proton 9.0", is_runtime_component=True)
    store = Store()
    store.merge([runtime])
    model = PrefixTableModel()
    model.set_store(store)
    table = PrefixTable(model)
    qtbot.addWidget(table)

    assert model.index(0, NAME_COLUMN).data() == "Proton 9.0 (Steam component)"
    tooltip = model._tooltip(runtime, 0, NAME_COLUMN)
    assert tooltip is not None
    assert "safe to delete" in tooltip

    plain = _make_prefix(tmp_path / "other", "plain", 4000)
    assert model._tooltip(plain, 0, NAME_COLUMN) is None


def test_header_shows_all_when_runtime_excluded(tmp_path: Path) -> None:
    runtime = _make_prefix(tmp_path, "runtime", 962960)
    runtime = replace(runtime, name="Proton 9.0", is_runtime_component=True)
    game = _make_prefix(tmp_path / "g", "game", 4000)
    store = Store()
    store.merge([runtime, game])
    model = PrefixTableModel()
    model.set_store(store)
    assert model.selection_state(exclude_runtime=True) is SelectionState.NONE
    store.select_visible(model.rows(), exclude_runtime=True)
    assert model.selection_state(exclude_runtime=True) is SelectionState.ALL
    assert model.selection_state(exclude_runtime=False) is SelectionState.SOME
    store.clear_selection()
    assert model.selection_state(exclude_runtime=True) is SelectionState.NONE
