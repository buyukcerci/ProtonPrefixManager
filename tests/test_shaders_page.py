"""Tests for the Shaders tab and its wiring into the main window."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from core import deletion as deletion_module
from core import tools as tools_module
from core.deletion import DeleteMode
from core.discovery import Library
from core.models import PrefixType
from core.shaders import ShaderCache
from ui import shaders_page as page_module
from ui.shaders_page import (
    CHECK_COLUMN,
    NO_CACHES_TEXT,
    NO_ROWS_TEXT,
    RECOMPILE_NOTE,
    SIZE_COLUMN,
    STATUS_COLUMN,
    ShadersPage,
    matches_search,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(tools_module, "SYSTEM_TOOLS_DIR", tmp_path / "system-tools")


def _library(base: Path) -> Library:
    (base / "lib" / "steamapps" / "shadercache").mkdir(parents=True, exist_ok=True)
    return Library(path=(base / "lib").resolve(), root=base.resolve())


def _cache(
    lib: Library, app_id: int, cache_type: PrefixType, name: str, size: int = 0
) -> ShaderCache:
    path = lib.path / "steamapps" / "shadercache" / str(app_id)
    path.mkdir(exist_ok=True)
    if size:
        (path / "blob").write_bytes(b"x" * size)
    st = os.stat(path)
    return ShaderCache(
        app_id, name, cache_type, path, str(lib.path), dev_ino=(st.st_dev, st.st_ino)
    )


def _page(qtbot, *, busy: bool = False) -> ShadersPage:
    page = ShadersPage(epoch_provider=lambda: 0, external_busy=lambda: busy)
    qtbot.addWidget(page)
    return page


def _visible_ids(page: ShadersPage) -> list[int]:
    return [cache.app_id for cache in page.model.rows()]


def test_matches_search_on_name_and_app_id(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    cache = _cache(lib, 730, PrefixType.STEAM, "Counter-Strike 2")
    assert matches_search(cache, "")
    assert matches_search(cache, "counter")
    assert matches_search(cache, "73")
    assert not matches_search(cache, "dota")


def test_empty_state_messages(qtbot, tmp_path: Path) -> None:
    page = _page(qtbot)
    page.set_data([], [])
    assert page.message_label.text() == NO_CACHES_TEXT
    lib = _library(tmp_path)
    page.set_data([_cache(lib, 1, PrefixType.STEAM, "A")], [lib])
    page.set_status_filter(set())
    assert page.message_label.text() == NO_ROWS_TEXT


def test_sizes_arrive_and_sort_largest_first(qtbot, tmp_path: Path) -> None:
    lib = _library(tmp_path)
    small = _cache(lib, 1, PrefixType.STEAM, "Small", size=10)
    big = _cache(lib, 2, PrefixType.ORPHANED, "Big", size=5000)
    page = _page(qtbot)
    with qtbot.waitSignal(page.data_changed, timeout=10000, raising=True) as _:
        page.set_data([small, big], [lib])
    assert page.model.index(0, SIZE_COLUMN).data() == "Scanning..."
    qtbot.waitUntil(lambda: not page.pending, timeout=10000)
    assert _visible_ids(page) == [2, 1]
    assert page.model.index(0, SIZE_COLUMN).data().endswith("KB")
    assert page.model.index(0, STATUS_COLUMN).data() == "Orphaned"
    assert "orphaned" in page.summary_label.text()


def test_failed_size_reads_unavailable(qtbot, tmp_path: Path) -> None:
    lib = _library(tmp_path)
    cache = _cache(lib, 1, PrefixType.STEAM, "A")
    page = _page(qtbot)
    page._all = [cache]
    page.model.reset_items([cache])
    page._apply_filter()
    page._on_sized((str(cache.path), 999, "permission denied"), 0)
    assert page.model.index(0, SIZE_COLUMN).data() == "Unavailable"
    assert page.caches()[0].size_bytes == 0


def test_stale_epoch_size_is_ignored(qtbot, tmp_path: Path) -> None:
    lib = _library(tmp_path)
    cache = _cache(lib, 1, PrefixType.STEAM, "A")
    page = _page(qtbot)
    page._all = [cache]
    page.model.reset_items([cache])
    page._on_sized((str(cache.path), 999, None), 5)
    assert page.caches()[0].size_bytes == 0


def test_status_filter_and_search(qtbot, tmp_path: Path) -> None:
    lib = _library(tmp_path)
    caches = [
        _cache(lib, 1, PrefixType.STEAM, "Alpha"),
        _cache(lib, 2, PrefixType.NON_STEAM, "Beta"),
        _cache(lib, 3, PrefixType.ORPHANED, "Gamma"),
    ]
    page = _page(qtbot)
    page.set_data(caches, [lib])
    page.show_orphans()
    assert _visible_ids(page) == [3]
    page.set_status_filter({PrefixType.STEAM, PrefixType.NON_STEAM})
    page.search_box.setText("bet")
    page._search_timer.timeout.emit()
    assert _visible_ids(page) == [2]


def test_filter_drops_hidden_selection(qtbot, tmp_path: Path) -> None:
    lib = _library(tmp_path)
    page = _page(qtbot)
    page.set_data(
        [_cache(lib, 1, PrefixType.STEAM, "A"), _cache(lib, 2, PrefixType.ORPHANED, "B")], [lib]
    )
    page.model.set_all_selected(True)
    assert len(page.model.selected_caches()) == 2
    page.show_orphans()
    assert [c.app_id for c in page.model.selected_caches()] == [2]


def test_checkbox_and_header_toggle(qtbot, tmp_path: Path) -> None:
    lib = _library(tmp_path)
    page = _page(qtbot)
    page.set_data(
        [_cache(lib, 1, PrefixType.STEAM, "A"), _cache(lib, 2, PrefixType.STEAM, "B")], [lib]
    )
    index = page.model.index(0, CHECK_COLUMN)
    assert page.model.setData(
        index, int(Qt.CheckState.Checked.value), Qt.ItemDataRole.CheckStateRole
    )
    assert page.delete_button.text() == "Delete Shader Caches (1)"
    assert page.delete_button.isEnabled()
    assert page.header_checkbox.checkState() == Qt.CheckState.PartiallyChecked
    page._on_header_toggle()
    assert len(page.model.selected_caches()) == 2
    page._on_header_toggle()
    assert page.model.selected_caches() == []


def test_delete_button_respects_external_busy(qtbot, tmp_path: Path) -> None:
    lib = _library(tmp_path)
    page = _page(qtbot, busy=True)
    page.set_data([_cache(lib, 1, PrefixType.STEAM, "A")], [lib])
    page.model.set_all_selected(True)
    assert not page.delete_button.isEnabled()


def test_delete_flow_trashes_and_signals(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _library(tmp_path)
    cache = _cache(lib, 1, PrefixType.ORPHANED, "Gone")
    trashed: list[Path] = []
    monkeypatch.setattr(deletion_module, "send2trash", trashed.append)
    notes: list[str] = []

    def fake_selection(parent, names, total, note, item_noun="prefixes"):
        notes.append(note)
        assert item_noun == "shader caches"
        return True

    monkeypatch.setattr(page_module, "confirm_selection", fake_selection)
    monkeypatch.setattr(page_module, "confirm_final", lambda *a, **k: DeleteMode.TRASH)
    summaries: list[object] = []
    monkeypatch.setattr(page_module, "show_deletion_summary", lambda p, r: summaries.append(r))
    page = _page(qtbot)
    page.set_data([cache], [lib])
    qtbot.waitUntil(lambda: not page.pending, timeout=10000)
    page.model.set_all_selected(True)
    busy: list[bool] = []
    page.busy_changed.connect(busy.append)
    with qtbot.waitSignal(page.deletion_finished, timeout=10000):
        page._on_delete_clicked()
    assert busy == [True, False]
    assert trashed == [cache.path]
    assert RECOMPILE_NOTE in notes[0]
    assert summaries == []
    assert not page.is_deleting


def test_delete_cancelled_does_nothing(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _library(tmp_path)
    monkeypatch.setattr(page_module, "confirm_selection", lambda *a, **k: False)
    page = _page(qtbot)
    page.set_data([_cache(lib, 1, PrefixType.STEAM, "A")], [lib])
    page.model.set_all_selected(True)
    page._on_delete_clicked()
    assert not page.is_deleting


def test_focus_cache_resets_filter_and_highlights(qtbot, tmp_path: Path) -> None:
    lib = _library(tmp_path)
    target = _cache(lib, 1, PrefixType.STEAM, "Alpha")
    page = _page(qtbot)
    page.set_data([target, _cache(lib, 2, PrefixType.ORPHANED, "Beta")], [lib])
    page.show_orphans()
    page.search_box.setText("zzz")
    page.focus_cache(target)
    assert page.search_box.text() == ""
    assert 1 in _visible_ids(page)
    assert page.model.highlighted_key() == str(target.path)


# --- main window wiring ----------------------------------------------------


def test_window_discovery_fills_shaders_tab(qtbot, tmp_path: Path) -> None:
    from tests.test_window import _synthetic_payload
    from ui.main_window import _PAGE_SHADERS, MainWindow

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    result, prefixes = _synthetic_payload(tmp_path)
    shadercache = result.libraries[0].path / "steamapps" / "shadercache"
    (shadercache / "730").mkdir(parents=True)
    window._on_discovery_finished((result, prefixes), window._epoch)
    assert window._tabs.tabText(_PAGE_SHADERS) == "Shaders"
    assert [c.app_id for c in window._shaders_page.caches()] == [730]


def test_window_warns_about_symlinked_shadercache(qtbot, tmp_path: Path) -> None:
    from tests.test_window import _synthetic_payload
    from ui.main_window import MainWindow

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    result, prefixes = _synthetic_payload(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = result.libraries[0].path / "steamapps" / "shadercache"
    link.symlink_to(outside, target_is_directory=True)
    window._on_discovery_finished((result, prefixes), window._epoch)
    assert any(w.path == link for w in window._enumeration_warnings)
    assert window._shaders_page.caches() == []


def test_window_locks_other_deletes_while_shaders_delete(qtbot, tmp_path: Path) -> None:
    from ui.main_window import MainWindow

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    window._shaders_page._deleting = True
    window._shaders_page.busy_changed.emit(True)
    assert window._any_deleting()
    assert not window._refresh_action.isEnabled()
    epoch = window._epoch
    window.refresh()
    assert window._epoch == epoch
    window._shaders_page._deleting = False
    window._shaders_page.busy_changed.emit(False)
    assert window._refresh_action.isEnabled()


def test_window_prefix_delete_locks_shader_delete(qtbot, tmp_path: Path) -> None:
    from ui.main_window import MainWindow

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    lib = _library(tmp_path)
    window._shaders_page.set_data([_cache(lib, 1, PrefixType.STEAM, "A")], [lib])
    window._shaders_page.model.set_all_selected(True)
    assert window._shaders_page.delete_button.isEnabled()
    window._deleting = True
    window._update_delete_tools_button()
    assert not window._shaders_page.delete_button.isEnabled()
    window._deleting = False


# --- overview section and prefix hint --------------------------------------


def test_overview_shader_section_totals_and_orphan_card(qtbot, tmp_path: Path) -> None:
    from ui.overview import OverviewPage

    lib = _library(tmp_path)
    installed = replace_size(_cache(lib, 1, PrefixType.STEAM, "Alpha"), 3000)
    orphan = replace_size(_cache(lib, 2, PrefixType.ORPHANED, "Beta"), 1000)
    page = OverviewPage()
    qtbot.addWidget(page)
    assert not page.shader_orphan_card.is_clickable_enabled()
    page.update_shaders([installed, orphan], set(), set(), 1_000_000)
    assert page.shaders_card.value_text() == "2"
    assert page.shader_orphan_card.detail_text() == "1 orphaned cache"
    assert page.shader_orphan_card.is_clickable_enabled()
    assert [row._item for row in page.shaders_top_list.rows()] == [installed, orphan]
    assert page.shaders_status_label.text() == "2 of 2 shader caches sized"
    page.update_shaders([installed], {str(installed.path)}, set(), None)
    assert page.shaders_status_label.text() == "0 of 1 shader caches sized, 1 scanning"
    assert page.shaders_disk_card.detail_text() == "disk size unknown"
    reviews: list[bool] = []
    page.shader_review_requested.connect(lambda: reviews.append(True))
    page._on_shader_orphan_card_clicked()
    assert reviews == []  # no orphans in the latest update


def replace_size(cache: ShaderCache, size: int) -> ShaderCache:
    from dataclasses import replace

    return replace(cache, size_bytes=size)


def test_window_overview_handoffs(qtbot, tmp_path: Path) -> None:
    from ui.main_window import _PAGE_SHADERS, MainWindow

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    lib = _library(tmp_path)
    keep = _cache(lib, 1, PrefixType.STEAM, "Alpha")
    orphan = _cache(lib, 2, PrefixType.ORPHANED, "Beta")
    window._shaders_page.set_data([keep, orphan], [lib])
    qtbot.waitUntil(lambda: not window._shaders_page.pending, timeout=10000)
    assert window._overview.shaders_card.value_text() == "2"
    window._overview.shader_review_requested.emit()
    assert window._tabs.currentIndex() == _PAGE_SHADERS
    assert _visible_ids(window._shaders_page) == [2]
    window._overview.shader_focus_requested.emit(keep)
    assert window._shaders_page.model.highlighted_key() == str(keep.path)
    assert 1 in _visible_ids(window._shaders_page)


def test_prefix_size_tooltip_mentions_shader_cache(qtbot, tmp_path: Path) -> None:
    from core.models import Prefix, ScanStatus, Store
    from ui.main_window import MainWindow
    from ui.table import SIZE_COLUMN as PREFIX_SIZE_COLUMN

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    lib = _library(tmp_path)
    window._shaders_page.set_data([_cache(lib, 730, PrefixType.STEAM, "CS2", size=4096)], [lib])
    qtbot.waitUntil(lambda: not window._shaders_page.pending, timeout=10000)
    compat = tmp_path / "compat"
    store = Store()
    for app_id in (730, 440):
        (compat / str(app_id)).mkdir(parents=True)
        store.merge(
            [
                Prefix(
                    app_id,
                    f"Game {app_id}",
                    PrefixType.STEAM,
                    compat / str(app_id),
                    str(lib.path),
                    size_bytes=10,
                    scan_status=ScanStatus.SCANNED,
                )
            ]
        )
    window._model.set_store(store)
    tooltips = {
        window._model.row_at(row).app_id: window._model.index(row, PREFIX_SIZE_COLUMN).data(
            Qt.ItemDataRole.ToolTipRole
        )
        for row in range(window._model.rowCount())
    }
    assert tooltips[730] is not None and "shader cache" in tooltips[730]
    assert tooltips[440] is None
