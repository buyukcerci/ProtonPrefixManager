"""Tests for the main window: smoke behavior, empty states, epoch guard."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from core.config import load_config, save_config
from core.deletion import DeleteMode
from core.discovery import DiscoveryResult, Library, RootSource, SteamRoot
from core.models import Prefix, PrefixType, ScanStatus
from core.scanner import ScanEvent, ScanEventKind, save_cached
from ui.main_window import _NO_ROWS_TEXT, MainWindow
from ui.table import SIZE_COLUMN


@pytest.fixture()
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


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
    assert "No valid Steam root" in window._message_label.text()


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
    assert window._stack.currentIndex() == 0
    assert window._message_label.text() == _NO_ROWS_TEXT
    window._on_search_text_changed("")
    window._search_timer.timeout.emit()
    assert window._stack.currentIndex() == 1


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
    frame = window.findChild(QFrame, "searchFrame")
    assert frame is not None
    assert window._target_combo.parent() is frame
    assert window._search_box.parent() is frame


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
    assert window._stack.currentIndex() == 0
    assert window._message_label.text() == _NO_ROWS_TEXT
