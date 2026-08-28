"""Tests for the settings dialog: roots editing, validation, font controls."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from core.config import AppConfig
from core.discovery import Library, RootSource, SteamRoot
from ui.settings import (
    DEFAULT_FONT_SIZE,
    MAX_FONT_SIZE,
    MIN_FONT_SIZE,
    SYSTEM_DEFAULT_LABEL,
    SettingsDialog,
)


@pytest.fixture()
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


def _dialog(
    config: AppConfig | None = None,
    *,
    roots: tuple[SteamRoot, ...] = (),
    libraries: tuple[Library, ...] = (),
) -> SettingsDialog:
    return SettingsDialog(None, config or AppConfig(), discovered_roots=roots, libraries=libraries)


def _fake_picker(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> list[str]:
    """Stub the folder picker; the recorded requests assert picker usage."""
    requests: list[str] = []

    def fake_get_existing_directory(*args: object, **kwargs: object) -> str:
        if len(args) >= 3 and isinstance(args[2], str):
            requests.append(args[2])
        return outcome

    monkeypatch.setattr("ui.settings.QFileDialog.getExistingDirectory", fake_get_existing_directory)
    return requests


def _captured_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(
        "ui.settings.QMessageBox.warning",
        lambda parent, title, text: messages.append(text),
    )
    return messages


def test_dialog_prefills_from_config(qtbot) -> None:
    root_dir = Path("/tmp")
    dialog = _dialog(AppConfig(custom_roots=[str(root_dir)], font_size=13))
    qtbot.addWidget(dialog)
    assert dialog.custom_roots() == [str(root_dir)]
    assert dialog._custom_list.count() == 1
    assert dialog._font_size_spin.value() == 13
    assert dialog._font_list.count() > 0
    buttons = dialog.findChild(QDialogButtonBox)
    assert buttons is not None
    ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
    cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
    assert ok_button is not None and ok_button.isEnabled()
    assert cancel_button is not None and cancel_button.isEnabled()


def _select_family(dialog: SettingsDialog, family: str) -> None:
    row = dialog._family_row(family)
    assert row >= 0, f"{family} not in font list"
    dialog._font_list.setCurrentRow(row)


def test_font_family_prefilled_when_configured(qtbot) -> None:
    family = QFontDatabase.families()[0]
    dialog = _dialog(AppConfig(font_family=family))
    qtbot.addWidget(dialog)
    assert dialog._font_list.currentItem().text() == family
    assert dialog.font_family() == family


def test_font_family_none_stays_none_until_user_changes(qtbot) -> None:
    dialog = _dialog(AppConfig())
    qtbot.addWidget(dialog)
    assert dialog.font_family() is None
    font_list = dialog._font_list
    assert font_list.currentItem().text() == SYSTEM_DEFAULT_LABEL
    other = (font_list.currentRow() + 1) % font_list.count()
    font_list.setCurrentRow(other)
    assert dialog.font_family() == font_list.currentItem().text()


def test_add_rejects_nonexistent_directory_with_warning(
    qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = _dialog()
    qtbot.addWidget(dialog)
    warnings = _captured_warnings(monkeypatch)
    _fake_picker(monkeypatch, "/definitely/not/a/real/dir")
    dialog._add_button.click()
    assert dialog.custom_roots() == []
    assert len(warnings) == 1
    assert "not a directory" in warnings[0] or "does not exist" in warnings[0]


def test_add_appends_existing_directory(qtbot, tmp_path: Path, monkeypatch) -> None:
    dialog = _dialog()
    qtbot.addWidget(dialog)
    target = tmp_path / "steam"
    target.mkdir()
    _fake_picker(monkeypatch, str(target))
    dialog._add_button.click()
    expected = str(target.resolve())
    assert dialog.custom_roots() == [expected]
    assert dialog._custom_list.item(0).text() == expected


def test_add_expands_tilde_and_relative_entries(
    qtbot, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = _dialog()
    qtbot.addWidget(dialog)
    target = isolated_home / "tildesteam"
    target.mkdir()
    _fake_picker(monkeypatch, "~/tildesteam")
    dialog._add_button.click()
    assert dialog.custom_roots() == [str(target.resolve())]


def test_add_rejects_duplicate_of_existing_entry(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "already"
    existing.mkdir()
    dialog = _dialog(AppConfig(custom_roots=[str(existing.resolve())]))
    qtbot.addWidget(dialog)
    warnings = _captured_warnings(monkeypatch)
    _fake_picker(monkeypatch, str(existing))
    dialog._add_button.click()
    assert dialog.custom_roots() == [str(existing.resolve())]
    assert len(warnings) == 1
    assert "already" in warnings[0]


def test_change_replaces_selected_entry(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    dialog = _dialog(AppConfig(custom_roots=[str(first.resolve())]))
    qtbot.addWidget(dialog)
    dialog._custom_list.setCurrentRow(0)
    _fake_picker(monkeypatch, str(second))
    dialog._change_button.click()
    assert dialog.custom_roots() == [str(second.resolve())]


def test_remove_deletes_selected_entry_only(qtbot, tmp_path: Path) -> None:
    one = str((tmp_path / "one").resolve())
    two = str((tmp_path / "two").resolve())
    dialog = _dialog(AppConfig(custom_roots=[one, two]))
    qtbot.addWidget(dialog)
    dialog._custom_list.setCurrentRow(0)
    dialog._remove_button.click()
    assert dialog.custom_roots() == [two]


def test_redetect_emits_signal_keeps_roots_and_closes(qtbot, tmp_path: Path) -> None:
    kept = str((tmp_path / "kept").resolve())
    dialog = _dialog(AppConfig(custom_roots=[kept]))
    qtbot.addWidget(dialog)
    with qtbot.waitSignal(dialog.redetect_requested):
        dialog._redetect_button.click()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.custom_roots() == [kept]


def test_ok_persists_values_after_accept(qtbot, tmp_path: Path) -> None:
    family = QFontDatabase.families()[0]
    config = AppConfig(custom_roots=[], font_family=None, font_size=10)
    dialog = _dialog(config)
    qtbot.addWidget(dialog)
    entry = str(tmp_path.resolve())
    dialog._custom_roots.append(entry)
    dialog._reload_custom_items()
    _select_family(dialog, family)
    dialog._font_size_spin.setValue(14)
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.custom_roots() == [entry]
    assert dialog.font_family() == family
    assert dialog.font_size() == 14


def test_ok_with_invalid_root_warns_and_keeps_dialog_open(
    qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = _dialog()
    qtbot.addWidget(dialog)
    dialog.show()
    warnings = _captured_warnings(monkeypatch)
    dialog._custom_roots.append("/gone/with/the/wind")
    dialog._reload_custom_items()
    dialog.accept()
    assert len(warnings) == 1
    assert "/gone/with/the/wind" in warnings[0]
    assert dialog.isVisible()


def test_font_size_below_range_clamps_to_minimum(qtbot) -> None:
    dialog = _dialog(AppConfig(font_size=10))
    qtbot.addWidget(dialog)
    spin = dialog._font_size_spin
    spin.lineEdit().setText("1")
    spin.interpretText()
    assert spin.value() == MIN_FONT_SIZE


def test_font_size_above_range_clamps_to_maximum(qtbot) -> None:
    dialog = _dialog(AppConfig(font_size=10))
    qtbot.addWidget(dialog)
    spin = dialog._font_size_spin
    spin.lineEdit().setText("99")
    spin.interpretText()
    assert spin.value() == MAX_FONT_SIZE


def test_font_size_spin_suffix_tooltip_and_tracking(qtbot) -> None:
    dialog = _dialog(AppConfig())
    qtbot.addWidget(dialog)
    spin = dialog._font_size_spin
    assert spin.suffix() == " pt"
    assert f"Allowed range: {MIN_FONT_SIZE} to {MAX_FONT_SIZE} pt" in spin.toolTip()
    assert not spin.keyboardTracking()


def test_reset_restores_default_font_values(qtbot) -> None:
    family = QFontDatabase.families()[0]
    dialog = _dialog(AppConfig(font_family=family, font_size=17))
    qtbot.addWidget(dialog)
    dialog._reset_font_button.click()
    assert dialog.font_family() is None
    assert dialog.font_size() == DEFAULT_FONT_SIZE
    assert dialog._font_size_spin.value() == DEFAULT_FONT_SIZE
    assert dialog._font_list.currentItem().text() == SYSTEM_DEFAULT_LABEL


def test_resolved_roots_and_libraries_displayed_readonly(qtbot) -> None:
    root_path = Path("/games/Steam")
    library_path = Path("/mnt/games/steamapps")
    roots = (SteamRoot(path=root_path, source=RootSource.FLATPAK),)
    libraries = (Library(path=library_path, root=root_path),)
    dialog = _dialog(roots=roots, libraries=libraries)
    qtbot.addWidget(dialog)
    assert dialog._resolved_list.count() == 1
    item_text = dialog._resolved_list.item(0).text()
    assert str(root_path) in item_text
    assert RootSource.FLATPAK.value in item_text
    libraries_text = dialog._libraries_list.item(0).text()
    assert str(library_path) in libraries_text
    assert f"root: {RootSource.FLATPAK.value}" in libraries_text


def test_libraries_list_shows_each_library_once(qtbot) -> None:
    root_path = Path("/games/Steam")
    other_library = Path("/mnt/games/steamapps")
    roots = (SteamRoot(path=root_path, source=RootSource.NATIVE),)
    libraries = (
        Library(path=root_path, root=root_path),
        Library(path=other_library, root=root_path),
    )
    dialog = _dialog(roots=roots, libraries=libraries)
    qtbot.addWidget(dialog)
    assert dialog._libraries_list.count() == 2
    texts = [dialog._libraries_list.item(i).text() for i in range(2)]
    assert sum(str(root_path) in text for text in texts) == 1
    assert any(str(other_library) in text for text in texts)


def test_no_libraries_shows_placeholder_row(qtbot) -> None:
    dialog = _dialog()
    qtbot.addWidget(dialog)
    assert dialog._libraries_list.count() == 1
    assert "No libraries found" in dialog._libraries_list.item(0).text()


def test_font_search_filters_and_preserves_selection(qtbot) -> None:
    dialog = _dialog()
    qtbot.addWidget(dialog)
    font_list = dialog._font_list
    total = font_list.count()
    selected_text = font_list.currentItem().text()
    query = font_list.item(1).text().casefold()[:4]

    dialog._font_search.setText(query)

    visible = [row for row in range(font_list.count()) if not font_list.isRowHidden(row)]
    assert 0 < len(visible) < total
    assert all(query in font_list.item(row).text().casefold() or row == 0 for row in visible)
    assert font_list.currentItem().text() == selected_text


def test_system_default_row_visible_under_arbitrary_filter(qtbot) -> None:
    dialog = _dialog()
    qtbot.addWidget(dialog)
    dialog._font_search.setText("zzzz-no-such-font-family")
    font_list = dialog._font_list
    assert not font_list.item(0).isHidden()
    assert font_list.item(0).text() == SYSTEM_DEFAULT_LABEL
    assert all(font_list.isRowHidden(row) for row in range(1, font_list.count()))


def test_selecting_system_default_sets_pending_none(qtbot) -> None:
    family = QFontDatabase.families()[0]
    dialog = _dialog(AppConfig(font_family=family))
    qtbot.addWidget(dialog)
    _select_family(dialog, family)
    assert dialog.font_family() == family
    dialog._font_list.setCurrentRow(0)
    assert dialog.font_family() is None


def test_tab_titles_exist(qtbot) -> None:
    dialog = _dialog()
    qtbot.addWidget(dialog)
    tabs = [dialog._tabs.tabText(i) for i in range(dialog._tabs.count())]
    assert tabs == ["Steam Locations", "Appearance"]


def test_reset_then_clearing_selection_leaves_family_none(qtbot) -> None:
    family = QFontDatabase.families()[0]
    dialog = _dialog(AppConfig(font_family=family))
    qtbot.addWidget(dialog)
    dialog._reset_font_button.click()
    assert dialog.font_family() is None
    dialog._font_list.clearSelection()
    assert dialog.font_family() is None


def test_no_discovered_roots_shows_placeholder_row(qtbot) -> None:
    dialog = _dialog()
    qtbot.addWidget(dialog)
    assert dialog._resolved_list.count() == 1
    assert "None found" in dialog._resolved_list.item(0).text()


def test_focus_add_root_seeds_keyboard_focus_on_add_button(qtbot) -> None:
    dialog = SettingsDialog(None, AppConfig(), focus_add_root=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    qtbot.waitUntil(lambda: dialog._add_button.hasFocus())
    assert dialog._tabs.currentIndex() == 0
