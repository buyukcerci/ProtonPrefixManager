"""Tests for the main window: smoke behavior, empty states, epoch guard."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QWidget

from core import tools as tools_module
from core.config import AppConfig, load_config, save_config
from core.deletion import DeleteMode
from core.discovery import DiscoveryResult, Library, RootSource, SteamRoot
from core.models import Prefix, PrefixType, ScanStatus, format_size, prefix_key
from core.scanner import ScanEvent, ScanEventKind, save_cached
from core.tools import Tool
from ui.main_window import (
    _NO_PREFIXES_TEXT,
    _NO_ROWS_TEXT,
    _PAGE_OVERVIEW,
    _PAGE_PREFIXES,
    _PAGE_TOOLS,
    _TOOL_CHECK_COLUMN,
    _TOOL_NAME_COLUMN,
    _TOOL_SIZE_COLUMN,
    _TOOL_STATUS_COLUMN,
    MainWindow,
)
from ui.settings import SettingsDialog
from ui.styles import apply_app_style
from ui.table import SIZE_COLUMN


@pytest.fixture()
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _isolated_system_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_module, "SYSTEM_TOOLS_DIR", tmp_path / "system-tools")


def _synthetic_payload(
    base: Path,
    app_ids: tuple[int, ...] = (480,),
) -> tuple[DiscoveryResult, list[Prefix]]:
    root = base / "Steam"
    compatdata = root / "steamapps" / "compatdata"
    for app_id in app_ids:
        (compatdata / str(app_id)).mkdir(parents=True, exist_ok=True)
    library = Library(path=root.resolve(), root=base.resolve())
    result = DiscoveryResult(
        roots=[SteamRoot(path=root.resolve(), source=RootSource.NATIVE)],
        libraries=[library],
    )
    prefixes = [
        Prefix(
            app_id=app_id,
            name=f"Game {app_id}",
            prefix_type=PrefixType.ORPHANED,
            path=compatdata / str(app_id),
            library=str(root),
        )
        for app_id in app_ids
    ]
    return result, prefixes


def test_main_window_constructs(qtbot) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    assert window.windowTitle() == "Proton Prefix Manager"


def test_main_window_shows_and_closes(qtbot) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    window.show()
    assert window.isVisible()
    window.close()
    assert not window.isVisible()


def test_zero_roots_switches_to_message_page(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    window._on_discovery_finished((DiscoveryResult(), []), window._epoch)
    assert window._stack.currentIndex() == 0
    assert "Couldn't locate your Steam folder" in window._message_label.text()
    assert not window._locate_button.isHidden()


def test_roots_with_prefixes_switch_to_table(qtbot, isolated_env: Path, tmp_path: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    payload = _synthetic_payload(isolated_env, app_ids=(700,))
    window._on_discovery_finished(payload, window._epoch)
    assert window._stack.currentIndex() == 1
    assert [row.app_id for row in window._model.rows()] == [700]


def test_cached_sizes_render_without_rescan(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    payload = _synthetic_payload(isolated_env, app_ids=(710,))
    prefix = payload[1][0]
    scanned = replace(
        prefix,
        size_bytes=256,
        scan_status=ScanStatus.SCANNED,
        last_scanned=datetime.now(UTC),
    )
    save_cached(window._config.size_cache, scanned)

    window._on_discovery_finished(payload, window._epoch)
    assert window._stack.currentIndex() == 1
    row = window._model.rows()[0]
    assert row.scan_status is ScanStatus.SCANNED
    assert row.size_bytes == 256


def test_refresh_epoch_drops_stale_events(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)

    window.refresh()
    stale_epoch = window._epoch
    window.refresh()
    current_epoch = window._epoch
    assert current_epoch == stale_epoch + 1

    stale_payload = _synthetic_payload(isolated_env / "stale", app_ids=(1,))
    window._on_discovery_finished(stale_payload, stale_epoch)
    assert window._model.rows() == []

    fresh_payload = _synthetic_payload(isolated_env / "fresh", app_ids=(555,))
    prefix = fresh_payload[1][0]
    scanned = replace(
        prefix,
        size_bytes=64,
        scan_status=ScanStatus.SCANNED,
        last_scanned=datetime.now(UTC),
    )
    save_cached(window._config.size_cache, scanned)
    window._on_discovery_finished(fresh_payload, current_epoch)
    assert [row.app_id for row in window._model.rows()] == [555]

    stale_scanned = replace(scanned, size_bytes=999)
    window._on_scan_event(
        ScanEvent(kind=ScanEventKind.COMPLETED, prefix=stale_scanned, size_bytes=999),
        stale_epoch,
    )
    assert window._model.rows()[0].size_bytes == 64

    live_scanned = replace(scanned, size_bytes=128)
    window._on_scan_event(
        ScanEvent(kind=ScanEventKind.COMPLETED, prefix=live_scanned, size_bytes=128),
        current_epoch,
    )
    assert window._model.rows()[0].size_bytes == 128


def test_warning_count_restored_after_scan(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    result, prefixes = _synthetic_payload(isolated_env, app_ids=(700,))
    config_path = result.roots[0].path / "config" / "config.vdf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(b"\xff\xfe broken")
    window._on_discovery_finished((result, prefixes), window._epoch)
    assert window._warning_count == 1
    assert window._status.currentMessage().startswith("Scanning sizes")
    window._on_scan_finished(window._epoch)
    assert window._status.currentMessage() == "warnings: 1"


def test_clean_scan_finishes_with_empty_status(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    result, prefixes = _synthetic_payload(isolated_env, app_ids=(701,))
    window._on_discovery_finished((result, prefixes), window._epoch)
    assert window._warning_count == 0
    assert window._status.currentMessage().startswith("Scanning sizes")
    window._on_scan_finished(window._epoch)
    assert window._status.currentMessage() == ""


def test_default_config_sorts_size_descending(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    assert window._sort_key == "size"
    assert window._sort_descending is True

    payload = _synthetic_payload(isolated_env / "sort", app_ids=(1, 2))
    for prefix, size in zip(payload[1], (100, 500), strict=True):
        save_cached(
            window._config.size_cache,
            replace(
                prefix,
                size_bytes=size,
                scan_status=ScanStatus.SCANNED,
                last_scanned=datetime.now(UTC),
            ),
        )
    window._on_discovery_finished(payload, window._epoch)
    assert [row.size_bytes for row in window._model.rows()] == [500, 100]


def test_sort_direction_round_trips_to_config(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    assert window._config.sort_ascending is False

    window._on_sort_column_clicked(SIZE_COLUMN)
    assert window._sort_descending is False
    assert window._config.sort_ascending is True


def test_sort_indicator_shown_and_tracks_changes(qtbot, isolated_env: Path, tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    from ui.table import NAME_COLUMN

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    payload = _synthetic_payload(isolated_env / "indicator", app_ids=(1, 2))
    window._on_discovery_finished(payload, window._epoch)

    header = window._table.horizontalHeader()
    assert header.isSortIndicatorShown() is True
    assert header.sortIndicatorSection() == SIZE_COLUMN

    window._on_sort_column_clicked(NAME_COLUMN)
    assert header.sortIndicatorSection() == NAME_COLUMN
    assert header.sortIndicatorOrder() is Qt.SortOrder.DescendingOrder

    window._on_sort_column_clicked(NAME_COLUMN)
    assert header.sortIndicatorOrder() is Qt.SortOrder.AscendingOrder


# --- filters and search ---------------------------------------------------


def _filter_fixture(qtbot, isolated_env):
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    payload = _synthetic_payload(isolated_env / "f", app_ids=(10, 20))
    # give the middle prefix an orphan sibling too
    result, prefixes = payload
    compatdir = prefixes[0].path.parent
    prefixes[0] = replace(prefixes[0], prefix_type=PrefixType.STEAM, name="Steam One")
    prefixes[1] = replace(prefixes[1], prefix_type=PrefixType.NON_STEAM, name="NonSteam Two")
    orphan_dir = compatdir / "777"
    orphan_dir.mkdir()
    orphan = Prefix(
        app_id=777,
        name="Unknown (AppID: 777)",
        prefix_type=PrefixType.ORPHANED,
        path=orphan_dir,
        library=prefixes[0].library,
    )
    prefixes.append(orphan)
    window._on_discovery_finished((result, prefixes), window._epoch)
    return window, prefixes


def test_type_filter_reslices_rows_orphan_always_visible(qtbot, isolated_env: Path) -> None:
    window, prefixes = _filter_fixture(qtbot, isolated_env)

    window._on_type_toggled(PrefixType.STEAM, False)  # non-steam + orphans
    assert [row.app_id for row in window._model.rows()] == [20, 777]
    window._on_type_toggled(PrefixType.NON_STEAM, False)  # orphans only
    assert [row.app_id for row in window._model.rows()] == [777]
    window._on_type_toggled(PrefixType.STEAM, True)  # steam + orphans
    assert [row.app_id for row in window._model.rows()] == [10, 777]
    window._on_type_toggled(PrefixType.NON_STEAM, True)  # everything
    assert sorted(row.app_id for row in window._model.rows()) == [10, 20, 777]


def test_empty_filter_state_persists_and_labels_none(qtbot, isolated_env: Path) -> None:
    window, _ = _filter_fixture(qtbot, isolated_env)
    window._on_type_toggled(PrefixType.STEAM, False)
    window._on_type_toggled(PrefixType.NON_STEAM, False)
    window._on_type_toggled(PrefixType.ORPHANED, False)
    assert window._type_button.text() == "Filters"
    assert window._type_button.toolTip() == "Types: None"
    assert window._config.type_filter == []
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / "cfg.json"
        save_config(window._config, cp)
        assert load_config(cp).type_filter == []
    window._on_type_toggled(PrefixType.STEAM, True)
    assert window._config.type_filter == [PrefixType.STEAM]
    assert window._type_button.toolTip() == "Types: Steam"


def test_no_match_shows_filtered_message_page(qtbot, isolated_env: Path) -> None:
    window, _ = _filter_fixture(qtbot, isolated_env)
    window._target_combo.setCurrentIndex(0)
    window._on_search_text_changed("zzz-does-not-exist")
    window._search_timer.timeout.emit()
    assert window._inner_stack.currentIndex() == 0
    assert window._inner_message_label.text() == _NO_ROWS_TEXT
    window._on_search_text_changed("")
    window._search_timer.timeout.emit()
    assert window._inner_stack.currentIndex() == 1


def test_search_debounces_then_filters_by_target(qtbot, isolated_env: Path) -> None:
    window, _ = _filter_fixture(qtbot, isolated_env)

    window._search_box.setText("two")
    assert sorted(row.app_id for row in window._model.rows()) == [10, 20, 777]

    with qtbot.waitSignal(window.search_applied):
        window._search_timer.timeout.emit()
    assert sorted(row.app_id for row in window._model.rows()) == [20]

    window._target_combo.setCurrentIndex(1)  # AppID target
    window._search_box.setText("10")
    with qtbot.waitSignal(window.search_applied):
        window._search_timer.timeout.emit()
    assert [row.app_id for row in window._model.rows()] == [10]

    window._search_box.setText("")
    with qtbot.waitSignal(window.search_applied):
        window._search_timer.timeout.emit()
    assert len(window._model.rows()) == 3


def test_selection_persists_across_narrowing_and_counts_hidden(qtbot, isolated_env: Path) -> None:
    window, _ = _filter_fixture(qtbot, isolated_env)
    window._model.toggle_visible_selection()
    assert len(window._store.selected()) == 3
    assert window._delete_button.text() == "Delete Prefixes (3)"

    window._on_search_text_changed("One")
    with qtbot.waitSignal(window.search_applied):
        window._search_timer.timeout.emit()
    assert len(window._model.rows()) == 1
    assert len(window._store.selected()) == 3
    assert window._delete_button.text() == "Delete Prefixes (3)"

    index = window._model.index(0, 0)
    window._model.setData(index, int(Qt.CheckState.Unchecked.value), Qt.ItemDataRole.CheckStateRole)
    assert len(window._store.selected()) == 2

    window._on_search_text_changed("")
    with qtbot.waitSignal(window.search_applied):
        window._search_timer.timeout.emit()
    assert len(window._model.rows()) == 3
    window._model.toggle_visible_selection()  # header clear empties everything
    assert len(window._store.selected()) == 0


# --- deletion flow --------------------------------------------------------


@pytest.fixture()
def deletion_setup(qtbot, isolated_env: Path, monkeypatch: pytest.MonkeyPatch):
    """Fixture library with three prefixes plus mocked dialog/seam plumbing."""
    import json

    config_root = isolated_env / "config"
    (config_root / "proton-prefix-manager").mkdir(parents=True)
    (config_root / "proton-prefix-manager" / "config.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)

    base = isolated_env
    root = isolated_env / ".local" / "share" / "Steam"
    compatdata = root / "steamapps" / "compatdata"
    prefixes = []
    for app_id in (11, 12, 13):
        target = compatdata / str(app_id)
        target.mkdir(parents=True)
        prefixes.append(
            Prefix(
                app_id=app_id,
                name=f"P{app_id}",
                prefix_type=PrefixType.ORPHANED,
                path=target,
                library=str(root),
            )
        )
    library = Library(path=root.resolve(), root=base.resolve())
    result = DiscoveryResult(
        roots=[SteamRoot(path=root.resolve(), source=RootSource.NATIVE)],
        libraries=[library],
    )
    window._on_discovery_finished((result, prefixes), window._epoch)
    assert len(window._model.rows()) == 3

    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr("core.deletion.send2trash", fake_send2trash)

    dialog_calls: list[str] = []
    monkeypatch.setattr(
        "ui.main_window.confirm_selection",
        lambda parent, names, total, note: dialog_calls.append("selection") or True,
    )
    monkeypatch.setattr(
        "ui.main_window.confirm_final",
        lambda parent, count, size: dialog_calls.append("final") or DeleteMode.TRASH,
    )
    summaries: list[object] = []
    monkeypatch.setattr(
        "ui.main_window.show_deletion_summary", lambda parent, results: summaries.append(results)
    )
    invalidated: list[Path] = []

    def recording_invalidate(cache, target):
        from core.scanner import invalidate as real_invalidate

        path = target.path if hasattr(target, "path") else target
        invalidated.append(Path(str(path)))
        real_invalidate(cache, target)

    monkeypatch.setattr("ui.main_window.invalidate", recording_invalidate)
    return (
        window,
        prefixes,
        library,
        calls,
        dialog_calls,
        summaries,
        invalidated,
    )


def test_delete_flow_trashes_selection_refreshes(qtbot, deletion_setup) -> None:
    window, prefixes, library, calls, dialog_calls, summaries, invalidated = deletion_setup
    initial_epoch = window._epoch
    window._model.toggle_visible_selection()

    window._on_delete_clicked()
    qtbot.waitUntil(lambda: not window._deleting, timeout=15000)
    qtbot.waitUntil(lambda: window._epoch > initial_epoch, timeout=15000)
    qtbot.waitUntil(
        lambda: sorted(row.app_id for row in window._model.rows()) == [11, 12, 13],
        timeout=15000,
    )

    assert sorted(path.name for path in calls) == ["11", "12", "13"]
    assert sorted(path.name for path in invalidated) == ["11", "12", "13"]
    assert dialog_calls == ["selection", "final"]
    assert summaries == []
    assert window._store.selected() == []
    assert window._stack.currentIndex() == 1


def test_delete_flow_failure_shows_summary(
    qtbot, deletion_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, prefixes, library, calls, dialog_calls, summaries, invalidated = deletion_setup
    victim = prefixes[1]

    def failing(path: Path) -> None:
        if path.name == str(victim.app_id):
            raise PermissionError(13, "Permission denied")
        calls.append(path)

    monkeypatch.setattr("core.deletion.send2trash", failing)

    window._model.toggle_visible_selection()
    window._on_delete_clicked()
    qtbot.waitUntil(lambda: not window._deleting and len(summaries) == 1, timeout=15000)

    reported = list(summaries[0])
    failed = next(r for r in reported if r.status.value == "failed")
    assert failed.prefix.app_id == victim.app_id
    assert sorted(path.name for path in calls) == ["11", "13"]
    assert sorted(path.name for path in invalidated) == ["11", "13"]


def test_cancel_at_first_dialog_touches_nothing(qtbot, deletion_setup, monkeypatch) -> None:
    window, prefixes, _, calls, dialog_calls, summaries, _ = deletion_setup
    monkeypatch.setattr("ui.main_window.confirm_selection", lambda *a: False)
    window._model.toggle_visible_selection()
    window._on_delete_clicked()
    assert dialog_calls == []
    assert calls == []
    assert window._deleting is False


def test_cancel_at_second_dialog_touches_nothing(qtbot, deletion_setup, monkeypatch) -> None:
    window, prefixes, _, calls, dialog_calls, summaries, _ = deletion_setup
    monkeypatch.setattr(
        "ui.main_window.confirm_final",
        lambda parent, c, s: dialog_calls.append("final") or None,
    )
    window._model.toggle_visible_selection()
    window._on_delete_clicked()
    assert dialog_calls == ["selection", "final"]
    assert calls == []
    assert window._deleting is False


def test_filter_state_survives_post_delete_refresh(qtbot, deletion_setup) -> None:
    window, prefixes, library, calls, dialog_calls, summaries, invalidated = deletion_setup
    window._target_combo.setCurrentIndex(1)
    window._search_box.setText("12")
    with qtbot.waitSignal(window.search_applied):
        window._search_timer.timeout.emit()
    assert [row.app_id for row in window._model.rows()] == [12]

    window._model.toggle_visible_selection()
    window._on_delete_clicked()
    qtbot.waitUntil(lambda: not window._deleting, timeout=15000)
    qtbot.waitUntil(
        lambda: sorted(row.app_id for row in window._model.rows()) == [12],
        timeout=15000,
    )
    assert sorted(path.name for path in calls) == ["12"]
    assert window._search_text == "12"
    assert window._search_box.text() == "12"
    assert window._delete_button.text() == "Delete Prefixes (0)"


def test_empty_state_icon_centered(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    window._on_discovery_finished((DiscoveryResult(), []), window._epoch)
    app = window._message_page
    icon = window._message_icon_label
    # geometry center within a few pixels after layout
    assert abs(icon.geometry().center().x() - app.rect().center().x()) <= 5


def test_search_placeholder_reflects_target(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    assert window._search_box.placeholderText() == "Search names"
    window._target_combo.setCurrentIndex(1)
    assert window._search_box.placeholderText() == "Search AppIDs"
    window._target_combo.setCurrentIndex(0)
    assert window._search_box.placeholderText() == "Search names"


def test_search_frame_grouped_control_exists(qtbot, isolated_env: Path) -> None:
    from PySide6.QtWidgets import QFrame

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    frame = window._search_box.parent()
    assert isinstance(frame, QFrame)
    assert frame.objectName() == "searchFrame"
    assert window._target_combo.parent() is frame


def test_orphan_toggle_hides_and_shows(qtbot, isolated_env: Path) -> None:
    window, _ = _filter_fixture(qtbot, isolated_env)
    window._on_type_toggled(PrefixType.ORPHANED, False)
    assert all(p.prefix_type is not PrefixType.ORPHANED for p in window._model.rows())
    assert 777 not in [p.app_id for p in window._model.rows()]
    window._on_type_toggled(PrefixType.ORPHANED, True)
    assert 777 in [p.app_id for p in window._model.rows()]


def test_all_filters_off_yields_empty_view(qtbot, isolated_env: Path) -> None:
    window, _ = _filter_fixture(qtbot, isolated_env)
    for pt in (PrefixType.STEAM, PrefixType.NON_STEAM, PrefixType.ORPHANED):
        window._on_type_toggled(pt, False)
    assert window._model.rows() == []
    assert window._inner_stack.currentIndex() == 0
    assert window._inner_message_label.text() == _NO_ROWS_TEXT


# --- navigation and overview -------------------------------------------------


def test_main_window_has_settings_style_tabs(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    assert window._tabs.count() == 3
    assert window._tabs.tabText(0) == "Overview"
    assert window._tabs.tabText(1) == "Prefixes"
    assert window._tabs.tabText(2) == "Tools"
    assert window._tabs.currentIndex() == _PAGE_OVERVIEW
    assert window._delete_tools_button.text() == "Delete Tools (0)"
    assert not window._delete_tools_button.isEnabled()


def test_navigation_switches_pages(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    assert window._tabs.currentIndex() == _PAGE_OVERVIEW
    window._tabs.setCurrentIndex(_PAGE_PREFIXES)
    assert window._tabs.currentIndex() == _PAGE_PREFIXES
    window._tabs.setCurrentIndex(_PAGE_TOOLS)
    assert window._tabs.currentIndex() == _PAGE_TOOLS
    window._tabs.setCurrentIndex(_PAGE_OVERVIEW)
    assert window._tabs.currentIndex() == _PAGE_OVERVIEW


def test_discovery_populates_tools_tab(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    result, prefixes = _synthetic_payload(isolated_env, app_ids=(700,))
    root = result.roots[0].path
    toolsdir = root / "compatibilitytools.d" / "GE-Proton9-1"
    toolsdir.mkdir(parents=True)
    (toolsdir / "compatibilitytool.vdf").write_text(
        '"compatibilitytools"\n{\n"compat_tools"\n{\n"GE-Proton9-1"\n{\n'
        '"install_path" "."\n"display_name" "GE-Proton 9-1"\n}\n}\n}\n',
        encoding="utf-8",
    )
    (toolsdir / "payload.bin").write_bytes(b"x" * 64)
    stale = root / "compatibilitytools.d" / "OldBuild"
    stale.mkdir(parents=True)
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.vdf").write_text(
        '"InstallConfigStore"\n{\n"Software"\n{\n"Valve"\n{\n"Steam"\n{\n'
        '"CompatToolMapping"\n{\n"700"\n{\n"name" "GE-Proton9-1"\n}\n}\n'
        "}\n}\n}\n}\n",
        encoding="utf-8",
    )
    window._on_discovery_finished((result, prefixes), window._epoch)
    assert window._tabs.count() == 3
    window._set_page(_PAGE_TOOLS)
    assert window._tabs.currentIndex() == _PAGE_TOOLS
    model = window._tools_model
    assert [tool.name for tool in model._tools] == ["GE-Proton 9-1", "OldBuild"]
    assert model.used == {str(model._tools[0].path)}
    expected = (toolsdir / "compatibilitytool.vdf").stat().st_size + (
        toolsdir / "payload.bin"
    ).stat().st_size
    qtbot.waitUntil(lambda: model._tools[0].size_bytes == expected, timeout=15000)
    assert window._delete_tools_button.text() == "Delete Tools (0)"
    assert not window._delete_tools_button.isEnabled()


def test_used_tool_tooltip_lists_games(qtbot, isolated_env: Path) -> None:
    from PySide6.QtCore import Qt

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    result, prefixes = _synthetic_payload(isolated_env, app_ids=(700,))
    root = result.roots[0].path
    used_dir = root / "compatibilitytools.d" / "UsedBuild"
    used_dir.mkdir(parents=True)
    free_dir = root / "compatibilitytools.d" / "FreeBuild"
    free_dir.mkdir(parents=True)
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.vdf").write_text(
        '"CompatToolMapping"\n{\n"700" "UsedBuild"\n"999" "UsedBuild"\n}\n',
        encoding="utf-8",
    )
    window._on_discovery_finished((result, prefixes), window._epoch)
    model = window._tools_model
    rows = {tool.name: row for row, tool in enumerate(model._tools)}
    tooltip = model.data(
        model.index(rows["UsedBuild"], _TOOL_NAME_COLUMN), Qt.ItemDataRole.ToolTipRole
    )
    assert tooltip is not None
    assert "Game 700 (700)" in tooltip
    assert "AppID 999" in tooltip
    assert str(used_dir.resolve()) in tooltip
    plain = model.data(
        model.index(rows["FreeBuild"], _TOOL_NAME_COLUMN), Qt.ItemDataRole.ToolTipRole
    )
    assert plain == str(free_dir.resolve())


def test_tool_sizes_fill_after_list(qtbot, isolated_env: Path) -> None:
    from PySide6.QtCore import Qt

    from core.tools import Tool
    from ui.main_window import ToolTableModel

    model = ToolTableModel()
    tool = Tool(
        name="Build",
        path=isolated_env / "Build",
        root=isolated_env,
        read_only=False,
    )
    model.set_items([tool], {}, {})
    pending = model.data(model.index(0, _TOOL_SIZE_COLUMN), Qt.ItemDataRole.DisplayRole)
    assert pending == "Scanning..."
    model.set_tool_size(str(tool.path), 128, None)
    filled = model.data(model.index(0, _TOOL_SIZE_COLUMN), Qt.ItemDataRole.DisplayRole)
    assert filled == "128 B"


def test_stale_tool_size_event_dropped(qtbot, isolated_env: Path) -> None:
    from PySide6.QtCore import Qt

    from core.tools import Tool

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    tool = Tool(
        name="Build",
        path=isolated_env / "Build",
        root=isolated_env,
        read_only=False,
    )
    window._tools_model.set_items([tool], {}, {})
    window._on_tool_sized((str(tool.path), 128, None), window._epoch + 99)
    pending = window._tools_model.data(
        window._tools_model.index(0, _TOOL_SIZE_COLUMN), Qt.ItemDataRole.DisplayRole
    )
    assert pending == "Scanning..."
    window._on_tool_sized((str(tool.path), 128, None), window._epoch)
    filled = window._tools_model.data(
        window._tools_model.index(0, _TOOL_SIZE_COLUMN), Qt.ItemDataRole.DisplayRole
    )
    assert filled == "128 B"


def test_locked_rows_have_no_checkbox_and_reason() -> None:
    from core.tools import Tool
    from ui.main_window import _TOOL_CHECK_COLUMN, SYSTEM_TOOLS_DIR, ToolTableModel

    model = ToolTableModel()
    system = Tool(
        name="Sys",
        path=SYSTEM_TOOLS_DIR / "Sys",
        root=SYSTEM_TOOLS_DIR,
        read_only=True,
    )
    managed = Tool(
        name="Managed",
        path=Path("/lib/steamapps/common/Proton 9.0"),
        root=Path("/lib"),
        read_only=True,
    )
    used = Tool(
        name="Used",
        path=Path("/root/compatibilitytools.d/Used"),
        root=Path("/root"),
        read_only=False,
    )
    free = Tool(
        name="Free",
        path=Path("/root/compatibilitytools.d/Free"),
        root=Path("/root"),
        read_only=False,
    )
    model.set_items([system, managed, used, free], {str(used.path): [7]}, {})
    for row in range(3):
        assert (
            model.data(model.index(row, _TOOL_CHECK_COLUMN), Qt.ItemDataRole.CheckStateRole) is None
        )
    assert model.data(model.index(3, _TOOL_CHECK_COLUMN), Qt.ItemDataRole.CheckStateRole) == int(
        Qt.CheckState.Unchecked.value
    )
    reasons = [
        model.data(model.index(row, _TOOL_CHECK_COLUMN), Qt.ItemDataRole.ToolTipRole)
        for row in range(3)
    ]
    assert "package manager" in reasons[0]
    assert "Steam" in reasons[1]
    assert "In use" in reasons[2]
    for reason in reasons:
        assert "<b>" in reason
        assert reason.endswith("<b>It cannot be removed from this app.</b>")
    assert model.data(model.index(3, _TOOL_CHECK_COLUMN), Qt.ItemDataRole.ToolTipRole) is None


def test_locked_check_cell_shows_info_glyph(qtbot) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon

    from core.tools import Tool
    from ui.main_window import (
        _TOOL_CHECK_COLUMN,
        SYSTEM_TOOLS_DIR,
        ToolTableModel,
    )

    qtbot.wait(1)  # QApplication must exist for style icons.
    model = ToolTableModel()
    system = Tool(
        name="Sys",
        path=SYSTEM_TOOLS_DIR / "Sys",
        root=SYSTEM_TOOLS_DIR,
        read_only=True,
    )
    free = Tool(
        name="Free",
        path=Path("/root/compatibilitytools.d/Free"),
        root=Path("/root"),
        read_only=False,
    )
    model.set_items([system, free], {}, {})
    glyph = model.data(model.index(0, _TOOL_CHECK_COLUMN), Qt.ItemDataRole.DecorationRole)
    assert isinstance(glyph, QIcon)
    assert not glyph.isNull()
    hover = model.data(model.index(0, _TOOL_CHECK_COLUMN), Qt.ItemDataRole.ToolTipRole)
    assert "<b>It cannot be removed from this app.</b>" in hover
    assert model.data(model.index(1, _TOOL_CHECK_COLUMN), Qt.ItemDataRole.DecorationRole) is None


def test_both_search_bars_use_search_frame(qtbot, isolated_env: Path) -> None:
    from PySide6.QtWidgets import QFrame

    from ui.styles import STYLESHEET

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    prefix_frame = window._search_box.parent()
    assert isinstance(prefix_frame, QFrame)
    assert prefix_frame.objectName() == "searchFrame"
    tools_frame = window._tools_search_box.parent()
    assert isinstance(tools_frame, QFrame)
    assert tools_frame.objectName() == "toolsSearchFrame"
    assert tools_frame is not prefix_frame
    assert "QFrame#searchFrame" in STYLESHEET
    assert "QFrame#toolsSearchFrame" in STYLESHEET


def _tools_fixture(qtbot, isolated_env: Path):
    """Window with three custom tools, one selected by game 700."""
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    result, prefixes = _synthetic_payload(isolated_env, app_ids=(700,))
    root = result.roots[0].path
    for dirname in ("AlphaBuild", "BetaBuild", "GammaBuild"):
        (root / "compatibilitytools.d" / dirname).mkdir(parents=True)
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.vdf").write_text(
        '"CompatToolMapping"\n{\n"700" "BetaBuild"\n}\n', encoding="utf-8"
    )
    window._on_discovery_finished((result, prefixes), window._epoch)
    return window


def test_tools_search_narrows_by_name(qtbot, isolated_env: Path) -> None:
    window = _tools_fixture(qtbot, isolated_env)
    assert sorted(tool.name for tool in window._tools_model.rows()) == [
        "AlphaBuild",
        "BetaBuild",
        "GammaBuild",
    ]
    window._tools_search_box.setText("alp")
    window._tools_search_timer.timeout.emit()
    assert [tool.name for tool in window._tools_model.rows()] == ["AlphaBuild"]
    window._tools_search_box.setText("BUILD")
    window._tools_search_timer.timeout.emit()
    assert sorted(tool.name for tool in window._tools_model.rows()) == [
        "AlphaBuild",
        "BetaBuild",
        "GammaBuild",
    ]
    window._tools_search_box.setText("zzz-no-match")
    window._tools_search_timer.timeout.emit()
    assert window._tools_model.rows() == []


def test_tools_status_filter_combinations(qtbot, isolated_env: Path) -> None:
    window = _tools_fixture(qtbot, isolated_env)
    assert window._tools_status_button.text() == "Filters"
    window._tools_status_actions["Used"].setChecked(False)
    assert sorted(tool.name for tool in window._tools_model.rows()) == [
        "AlphaBuild",
        "GammaBuild",
    ]
    window._tools_status_actions["Unused"].setChecked(False)
    assert window._tools_model.rows() == []
    window._tools_status_actions["Used"].setChecked(True)
    assert [tool.name for tool in window._tools_model.rows()] == ["BetaBuild"]
    assert window._tools_status_button.toolTip() == "Status: Used + Read-only"


def test_tools_sort_orders(qtbot, isolated_env: Path) -> None:
    from core.tools import Tool

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    tools = [
        Tool(
            name=name,
            path=isolated_env / name,
            root=isolated_env,
            read_only=False,
            size_bytes=size,
        )
        for name, size in (
            ("Bravo", 100),
            ("Alpha", 300),
            ("Charlie", 200),
        )
    ]
    window._tools_model.set_items(tools, {}, {})
    window._all_tools = list(tools)
    window._apply_tools_filter()
    assert [tool.name for tool in window._tools_model.rows()] == [
        "Alpha",
        "Bravo",
        "Charlie",
    ]
    window._on_tools_section_clicked(_TOOL_SIZE_COLUMN)
    assert [tool.name for tool in window._tools_model.rows()] == [
        "Alpha",
        "Charlie",
        "Bravo",
    ]
    window._on_tools_section_clicked(_TOOL_SIZE_COLUMN)
    assert [tool.name for tool in window._tools_model.rows()] == [
        "Bravo",
        "Charlie",
        "Alpha",
    ]
    window._on_tools_section_clicked(_TOOL_STATUS_COLUMN)
    assert [tool.name for tool in window._tools_model.rows()] == [
        "Bravo",
        "Alpha",
        "Charlie",
    ]


def test_tools_header_selects_only_deletable(qtbot, isolated_env: Path) -> None:
    from PySide6.QtCore import Qt

    from core.tools import Tool

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    tools = [
        Tool(
            name="Used",
            path=isolated_env / "Used",
            root=isolated_env,
            read_only=False,
        ),
        Tool(
            name="FreeOne",
            path=isolated_env / "FreeOne",
            root=isolated_env,
            read_only=False,
        ),
        Tool(
            name="FreeTwo",
            path=isolated_env / "FreeTwo",
            root=isolated_env,
            read_only=False,
        ),
    ]
    used_path = str(tools[0].path)
    window._tools_model.set_items(tools, {used_path: [700]}, {})
    window._all_tools = list(tools)
    window._apply_tools_filter()
    box = window._tools_header_checkbox
    assert box.checkState() is Qt.CheckState.Unchecked
    box.click()
    selected = window._tools_model.selected_tools()
    assert sorted(tool.name for tool in selected) == ["FreeOne", "FreeTwo"]
    assert window._delete_tools_button.text() == "Delete Tools (2)"
    assert box.checkState() is Qt.CheckState.Checked
    box.click()
    assert window._tools_model.selected_tools() == []
    assert window._delete_tools_button.text() == "Delete Tools (0)"
    assert box.checkState() is Qt.CheckState.Unchecked
    window._tools_model.setData(
        window._tools_model.index(1, _TOOL_CHECK_COLUMN),
        int(Qt.CheckState.Checked.value),
        Qt.ItemDataRole.CheckStateRole,
    )
    assert box.checkState() is Qt.CheckState.PartiallyChecked


def test_tools_table_shares_prefix_view_settings(qtbot, isolated_env: Path) -> None:
    from PySide6.QtWidgets import QHeaderView, QTableView

    from ui.styles import MIN_HEADER_SECTION_PX

    window = _tools_fixture(qtbot, isolated_env)
    window.show()
    qtbot.waitExposed(window)
    for view in (window._table, window._tools_table):
        assert view.alternatingRowColors() is True
        assert view.verticalHeader().isVisible() is False
        assert view.selectionMode() is QTableView.SelectionMode.NoSelection
        assert view.textElideMode() is Qt.TextElideMode.ElideMiddle
        header = view.horizontalHeader()
        assert header.isSortIndicatorShown() is True
        assert header.minimumSectionSize() == MIN_HEADER_SECTION_PX
    tools_header = window._tools_table.horizontalHeader()
    assert tools_header.sectionResizeMode(_TOOL_CHECK_COLUMN) is QHeaderView.ResizeMode.Fixed
    assert tools_header.sectionResizeMode(_TOOL_NAME_COLUMN) is QHeaderView.ResizeMode.Stretch
    window._set_page(_PAGE_TOOLS)
    assert window._tabs.currentIndex() == _PAGE_TOOLS
    assert window._tools_table.isVisible()
    window._set_page(_PAGE_PREFIXES)
    assert window._tabs.currentIndex() == _PAGE_PREFIXES


def _sized_tool(name: str, base: Path, size: int = 0) -> Tool:
    from core.tools import Tool

    return Tool(name=name, path=base / name, root=base, read_only=False, size_bytes=size)


def test_hidden_row_size_resolves_on_reshow(qtbot, isolated_env: Path) -> None:
    from PySide6.QtCore import Qt

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    alpha = _sized_tool("Alpha", isolated_env)
    beta = _sized_tool("Beta", isolated_env)
    window._tools_model.set_items([alpha, beta], {}, {})
    window._all_tools = [alpha, beta]
    window._apply_tools_filter()
    window._tools_search_box.setText("alpha")
    window._tools_search_timer.timeout.emit()
    assert [tool.name for tool in window._tools_model.rows()] == ["Alpha"]
    window._on_tool_sized((str(beta.path), 64, None), window._epoch)
    window._tools_search_box.setText("")
    window._tools_search_timer.timeout.emit()
    assert [tool.name for tool in window._tools_model.rows()] == ["Alpha", "Beta"]
    assert str(beta.path) not in window._tools_model.pending
    size = window._tools_model.data(
        window._tools_model.index(1, _TOOL_SIZE_COLUMN), Qt.ItemDataRole.DisplayRole
    )
    assert size == "64 B"


def test_hidden_selection_cleared_on_filter(qtbot, isolated_env: Path) -> None:
    from PySide6.QtCore import Qt

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    alpha = _sized_tool("Alpha", isolated_env)
    beta = _sized_tool("Beta", isolated_env)
    window._tools_model.set_items([alpha, beta], {}, {})
    window._all_tools = [alpha, beta]
    window._apply_tools_filter()
    index = window._tools_model.index(1, _TOOL_CHECK_COLUMN)
    assert window._tools_model.setData(
        index, int(Qt.CheckState.Checked.value), Qt.ItemDataRole.CheckStateRole
    )
    assert window._delete_tools_button.text() == "Delete Tools (1)"
    window._tools_search_box.setText("alpha")
    window._tools_search_timer.timeout.emit()
    assert window._tools_model.selected_tools() == []
    assert window._delete_tools_button.text() == "Delete Tools (0)"
    window._tools_search_box.setText("")
    window._tools_search_timer.timeout.emit()
    assert window._tools_model.selected_tools() == []
    assert window._delete_tools_button.text() == "Delete Tools (0)"


def test_system_lock_reason_with_symlinked_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import Qt

    from core.tools import Tool
    from ui.main_window import ToolTableModel

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    tool = Tool(name="Build", path=(link / "Build").resolve(), root=link, read_only=True)
    monkeypatch.setattr("ui.main_window.SYSTEM_TOOLS_DIR", link)
    model = ToolTableModel()
    model.set_items([tool], {}, {})
    reason = model.data(model.index(0, _TOOL_CHECK_COLUMN), Qt.ItemDataRole.ToolTipRole)
    assert "package manager" in reason


def test_removed_tool_shows_unavailable(tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    from core.tools import Tool
    from ui.main_window import ToolTableModel
    from ui.workers import ToolSizeWorker

    tool = Tool(name="Gone", path=tmp_path / "Gone", root=tmp_path, read_only=False)
    worker = ToolSizeWorker([tool], 0)
    payloads: list[object] = []
    worker.signals.sized.connect(lambda payload, epoch: payloads.append(payload))
    worker.run()
    assert len(payloads) == 1
    payload = payloads[0]
    assert isinstance(payload, tuple) and payload[2] is not None
    model = ToolTableModel()
    model.set_items([tool], {}, {})
    model.set_tool_size(str(tool.path), 0, payload[2])
    assert model.pending == set()
    assert (
        model.data(model.index(0, _TOOL_SIZE_COLUMN), Qt.ItemDataRole.DisplayRole) == "Unavailable"
    )


def test_used_readonly_row_matches_either_facet(qtbot, isolated_env: Path) -> None:
    from core.tools import Tool

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    managed = Tool(
        name="Proton 9.0",
        path=isolated_env / "Proton 9.0",
        root=isolated_env,
        read_only=True,
    )
    window._tools_model.set_items([managed], {str(managed.path): [700]}, {700: "Game"})
    window._all_tools = [managed]
    window._apply_tools_filter()
    assert [tool.name for tool in window._tools_model.rows()] == ["Proton 9.0"]
    window._tools_status_actions["Unused"].setChecked(False)
    window._tools_status_actions["Read-only"].setChecked(False)
    assert [tool.name for tool in window._tools_model.rows()] == ["Proton 9.0"]
    window._tools_status_actions["Used"].setChecked(False)
    window._tools_status_actions["Read-only"].setChecked(True)
    assert [tool.name for tool in window._tools_model.rows()] == ["Proton 9.0"]
    window._tools_status_actions["Read-only"].setChecked(False)
    window._tools_status_actions["Unused"].setChecked(True)
    assert window._tools_model.rows() == []


def test_tools_selection_gates_delete_button(qtbot, isolated_env: Path) -> None:
    from PySide6.QtCore import Qt

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    result, prefixes = _synthetic_payload(isolated_env, app_ids=(700,))
    root = result.roots[0].path
    used_dir = root / "compatibilitytools.d" / "UsedBuild"
    used_dir.mkdir(parents=True)
    free_dir = root / "compatibilitytools.d" / "FreeBuild"
    free_dir.mkdir(parents=True)
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.vdf").write_text(
        '"CompatToolMapping"\n{\n"700" "UsedBuild"\n}\n', encoding="utf-8"
    )
    window._on_discovery_finished((result, prefixes), window._epoch)
    model = window._tools_model
    rows = {tool.name: row for row, tool in enumerate(model._tools)}
    used_index = model.index(rows["UsedBuild"], _TOOL_CHECK_COLUMN)
    assert not (model.flags(used_index) & Qt.ItemFlag.ItemIsUserCheckable)
    assert model.setData(used_index, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole) is False
    free_index = model.index(rows["FreeBuild"], _TOOL_CHECK_COLUMN)
    assert model.flags(free_index) & Qt.ItemFlag.ItemIsUserCheckable
    assert model.setData(free_index, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole) is True
    assert window._delete_tools_button.text() == "Delete Tools (1)"
    assert window._delete_tools_button.isEnabled()
    assert (
        model.setData(free_index, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole) is True
    )
    assert window._delete_tools_button.text() == "Delete Tools (0)"
    assert not window._delete_tools_button.isEnabled()


def test_filter_and_action_bars_only_visible_on_prefixes_page(qtbot, isolated_env: Path) -> None:
    window, _ = _filter_fixture(qtbot, isolated_env)
    window.show()
    qtbot.waitExposed(window)
    window._set_page(_PAGE_OVERVIEW)
    assert window._tabs.currentIndex() == _PAGE_OVERVIEW
    assert not window._search_box.isVisible()
    assert not window._delete_button.isVisible()
    window._set_page(_PAGE_PREFIXES)
    assert window._search_box.isVisible()
    assert window._delete_button.isVisible()


def test_prefix_focus_handoff_resets_filters_and_highlights(qtbot, isolated_env: Path) -> None:
    window, prefixes = _filter_fixture(qtbot, isolated_env)
    window._set_type_filter({PrefixType.STEAM})
    window._on_search_text_changed("zzz-no-match")
    window._search_timer.timeout.emit()
    assert window._model.rows() == []

    target = prefixes[2]  # the orphan
    window._on_prefix_focus_requested(target)

    assert window._tabs.currentIndex() == _PAGE_PREFIXES
    assert window._filter_types == set(PrefixType)
    assert window._config.type_filter == [
        PrefixType.STEAM,
        PrefixType.NON_STEAM,
        PrefixType.ORPHANED,
    ]
    assert window._search_text == ""
    assert window._search_box.text() == ""
    assert 777 in [row.app_id for row in window._model.rows()]
    assert window._model.highlighted_key() == prefix_key(target)
    assert window._store.selected() == []  # highlighted, never selected


def test_orphan_review_handoff_filters_and_selects_visible(qtbot, isolated_env: Path) -> None:
    window, prefixes = _filter_fixture(qtbot, isolated_env)
    window._set_type_filter(set(PrefixType))
    window._on_orphan_review_requested()

    assert window._tabs.currentIndex() == _PAGE_PREFIXES
    assert window._filter_types == {PrefixType.ORPHANED}
    assert window._config.type_filter == [PrefixType.ORPHANED]
    assert [row.app_id for row in window._model.rows()] == [777]
    selected = window._store.selected()
    assert len(selected) == 1 and selected[0].app_id == 777
    assert window._delete_button.text() == "Delete Prefixes (1)"


def test_orphan_review_auto_select_skips_runtime_components(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    base = isolated_env / "runtime"
    root = base / "Steam"
    compatdata = root / "steamapps" / "compatdata"
    orphan_dir = compatdata / "777"
    runtime_dir = compatdata / "962960"
    orphan_dir.mkdir(parents=True)
    runtime_dir.mkdir(parents=True)
    library = Library(path=root.resolve(), root=base.resolve())
    result = DiscoveryResult(
        roots=[SteamRoot(path=root.resolve(), source=RootSource.NATIVE)],
        libraries=[library],
    )
    orphan = Prefix(
        app_id=777,
        name="Unknown (AppID: 777)",
        prefix_type=PrefixType.ORPHANED,
        path=orphan_dir,
        library=str(root),
    )
    runtime = Prefix(
        app_id=962960,
        name="Unknown (AppID: 962960)",
        prefix_type=PrefixType.ORPHANED,
        path=runtime_dir,
        library=str(root),
        is_runtime_component=True,
    )
    window._on_discovery_finished((result, [orphan, runtime]), window._epoch)

    window._on_orphan_review_requested()

    assert window._tabs.currentIndex() == _PAGE_PREFIXES
    assert window._filter_types == {PrefixType.ORPHANED}
    assert sorted(row.app_id for row in window._model.rows()) == [777, 962960]
    selected = window._store.selected()
    assert [prefix.app_id for prefix in selected] == [777]
    assert window._delete_button.text() == "Delete Prefixes (1)"


def test_overview_recomputes_from_discovery_and_scan_events(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    payload = _synthetic_payload(isolated_env / "ov", app_ids=(30, 31))
    for prefix, size in zip(payload[1], (512, 256), strict=True):
        save_cached(
            window._config.size_cache,
            replace(
                prefix,
                size_bytes=size,
                scan_status=ScanStatus.SCANNED,
                last_scanned=datetime.now(UTC),
            ),
        )
    window._on_discovery_finished(payload, window._epoch)

    assert window._disk_capacity is not None
    assert window._overview.prefixes_card.value_text() == "2"
    assert window._overview.prefixes_card.detail_text() == format_size(768)
    assert window._overview.status_label.text() == "2 of 2 prefixes sized"

    rescanned = replace(
        payload[1][1],
        size_bytes=128,
        scan_status=ScanStatus.SCANNED,
        last_scanned=datetime.now(UTC),
    )
    window._on_scan_event(
        ScanEvent(kind=ScanEventKind.COMPLETED, prefix=rescanned, size_bytes=128),
        window._epoch,
    )
    assert window._overview.prefixes_card.detail_text() == format_size(640)


def test_overview_empty_states_share_message_page(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    window._set_page(_PAGE_OVERVIEW)
    window._on_discovery_finished((DiscoveryResult(), []), window._epoch)
    assert window._stack.currentIndex() == 0
    assert "Couldn't locate your Steam folder" in window._message_label.text()
    window._set_page(_PAGE_PREFIXES)
    window._on_discovery_finished((DiscoveryResult(), []), window._epoch)
    assert window._stack.currentIndex() == 0
    assert "Couldn't locate your Steam folder" in window._message_label.text()


# --- settings and first-launch locate flow --------------------------------


def _fake_settings_dialog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: QDialog.DialogCode = QDialog.DialogCode.Accepted,
    roots: list[str] | None = None,
    family: str | None = None,
    size: int = 10,
    emit_redetect: bool = False,
) -> list[QWidget]:
    created: list[QWidget] = []

    class FakeSettingsDialog(QWidget):
        redetect_requested = Signal()

        def __init__(
            self,
            parent,
            config,
            *,
            discovered_roots=(),
            libraries=(),
            focus_add_root=False,
        ) -> None:
            super().__init__(parent)
            self.config = config
            self.kwargs = {
                "discovered_roots": discovered_roots,
                "libraries": libraries,
                "focus_add_root": focus_add_root,
            }
            created.append(self)

        def custom_roots(self) -> list[str]:
            return list(roots) if roots is not None else list(self.config.custom_roots)

        def font_family(self) -> str | None:
            return family

        def font_size(self) -> int:
            return size

        def exec(self) -> QDialog.DialogCode:
            if emit_redetect:
                self.redetect_requested.emit()
            return result

    monkeypatch.setattr("ui.main_window.SettingsDialog", FakeSettingsDialog)
    return created


def test_settings_action_exists_with_shortcut(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    assert window._settings_action.text() == "&Settings..."
    assert window._settings_action.shortcut().toString() == "Ctrl+,"


def test_accepted_settings_update_config_font_and_refresh(
    qtbot, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    family = QFontDatabase.families()[0]
    created = _fake_settings_dialog(monkeypatch, roots=["/games/Steam"], family=family, size=12)
    epoch_before = window._epoch
    original_font = QApplication.font()
    try:
        window._open_settings()

        assert len(created) == 1
        assert window._config.custom_roots == ["/games/Steam"]
        assert window._config.font_family == family
        assert window._config.font_size == 12
        assert load_config().custom_roots == ["/games/Steam"]
        assert window._epoch > epoch_before
        applied = QApplication.font()
        assert applied.family() == family
        assert applied.pointSize() == 12
    finally:
        QApplication.setFont(original_font)


def test_redetect_signal_refreshes_and_keeps_custom_roots(
    qtbot, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    window._config.custom_roots = ["/kept/root"]
    created = _fake_settings_dialog(
        monkeypatch, result=QDialog.DialogCode.Rejected, emit_redetect=True
    )
    epoch_before = window._epoch

    window._open_settings()

    assert len(created) == 1
    assert window._config.custom_roots == ["/kept/root"]
    assert window._epoch > epoch_before


def test_cancelled_settings_touch_nothing(
    qtbot, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    _fake_settings_dialog(monkeypatch, result=QDialog.DialogCode.Rejected)
    epoch_before = window._epoch

    window._open_settings()

    assert window._config.custom_roots == []
    assert window._config.font_family is None
    assert window._config.font_size == 10
    assert window._epoch == epoch_before


def test_settings_dialog_receives_last_discovery_state(
    qtbot, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    payload = _synthetic_payload(isolated_env / "settings", app_ids=(900,))
    window._on_discovery_finished(payload, window._epoch)
    created = _fake_settings_dialog(monkeypatch)

    window._open_settings()

    dialog = created[0]
    assert dialog.kwargs["discovered_roots"] == payload[0].roots
    assert dialog.kwargs["libraries"] == window._libraries


def test_locate_button_opens_seeded_dialog_and_persists_root(
    qtbot, isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    window._on_discovery_finished((DiscoveryResult(), []), window._epoch)
    assert window._locate_button.isVisible()
    root_dir = tmp_path / "picked"
    root_dir.mkdir(parents=True)
    created = _fake_settings_dialog(monkeypatch, roots=[str(root_dir.resolve())])
    epoch_before = window._epoch
    original_font = QApplication.font()
    try:
        window._locate_button.click()

        assert created[0].kwargs["focus_add_root"] is True
        assert window._config.custom_roots == [str(root_dir.resolve())]
        assert load_config().custom_roots == [str(root_dir.resolve())]
        assert window._epoch > epoch_before
    finally:
        QApplication.setFont(original_font)


def test_prefixes_message_hides_locate_button(qtbot, isolated_env: Path) -> None:
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    payload = _synthetic_payload(isolated_env / "noprefix", app_ids=(800,))
    result = payload[0]
    window._on_discovery_finished((result, []), window._epoch)
    assert window._message_label.text() == _NO_PREFIXES_TEXT
    assert window._locate_button.isHidden()


def test_startup_font_from_config_applied_to_app(
    qtbot, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    family = QFontDatabase.families()[0]
    config_dir = isolated_env / "config" / "proton-prefix-manager"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        f'{{"version": 1, "font_family": "{family}", "font_size": 14}}', encoding="utf-8"
    )
    original_font = QApplication.font()
    try:
        window = MainWindow(auto_start=False)
        qtbot.addWidget(window)
        applied = QApplication.font()
        assert applied.family() == family
        assert applied.pointSize() == 14
    finally:
        QApplication.setFont(original_font)


def test_accepted_font_change_resolves_children_immediately(
    qtbot, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    original_font = QApplication.font()
    try:
        apply_app_style(app, AppConfig(font_size=10))
        window = MainWindow(auto_start=False)
        qtbot.addWidget(window)

        def scripted_exec(self: SettingsDialog) -> QDialog.DialogCode:
            self._font_size_spin.lineEdit().setText("14")
            self._font_size_spin.interpretText()
            buttons = self.findChild(QDialogButtonBox)
            ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
            ok_button.click()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr("ui.main_window.SettingsDialog.exec", scripted_exec)

        window._open_settings()

        assert QApplication.font().pointSize() == 14
        assert window._message_label.font().pointSize() == 14
    finally:
        QApplication.setFont(original_font)


@pytest.fixture()
def tools_deletion_setup(qtbot, isolated_env: Path, monkeypatch: pytest.MonkeyPatch):
    """Window with one deletable tool plus mocked dialog and trash plumbing."""
    from core import deletion as deletion_module
    from core.deletion import DeleteMode

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    result, prefixes = _synthetic_payload(isolated_env, app_ids=(700,))
    root = result.roots[0].path
    target = root / "compatibilitytools.d" / "OldBuild"
    target.mkdir(parents=True)
    (target / "payload.bin").write_bytes(b"x" * 32)
    window._on_discovery_finished((result, prefixes), window._epoch)
    assert [tool.name for tool in window._tools_model.rows()] == ["OldBuild"]

    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(deletion_module, "send2trash", fake_send2trash)

    dialog_calls: list[str] = []
    monkeypatch.setattr(
        "ui.main_window.confirm_selection",
        lambda parent, names, total, note, item_noun="prefixes": (
            dialog_calls.append("selection") or True
        ),
    )
    monkeypatch.setattr(
        "ui.main_window.confirm_final",
        lambda parent, count, size, item_noun="prefix(es)": (
            dialog_calls.append("final") or DeleteMode.TRASH
        ),
    )
    summaries: list[object] = []
    monkeypatch.setattr(
        "ui.main_window.show_deletion_summary", lambda parent, results: summaries.append(results)
    )
    return window, target, calls, dialog_calls, summaries


def test_tools_delete_runs_off_thread_then_refreshes(qtbot, tools_deletion_setup) -> None:
    from PySide6.QtCore import Qt

    window, target, calls, dialog_calls, summaries = tools_deletion_setup
    initial_epoch = window._epoch
    index = window._tools_model.index(0, _TOOL_CHECK_COLUMN)
    assert window._tools_model.setData(
        index, int(Qt.CheckState.Checked.value), Qt.ItemDataRole.CheckStateRole
    )
    window._on_delete_tools_clicked()
    assert window._tools_deleting is True
    assert window._delete_tools_button.isEnabled() is False
    qtbot.waitUntil(lambda: not window._tools_deleting, timeout=15000)
    qtbot.waitUntil(lambda: window._epoch > initial_epoch, timeout=15000)
    qtbot.waitUntil(lambda: window._tools_model.rows() == [], timeout=15000)
    assert calls == [target.resolve(strict=False)]
    assert dialog_calls == ["selection", "final"]
    assert summaries == []
