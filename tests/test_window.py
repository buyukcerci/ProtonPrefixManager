"""Tests for the main window: smoke behavior, empty states, epoch guard."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.discovery import DiscoveryResult, Library, RootSource, SteamRoot
from core.models import Prefix, PrefixType, ScanStatus
from core.scanner import ScanEvent, ScanEventKind, save_cached
from ui.main_window import MainWindow
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
