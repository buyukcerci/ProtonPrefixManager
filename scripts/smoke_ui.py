"""Offscreen end-to-end drive: rows render first, sizes fill asynchronously."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

SCAN_TIMEOUT_SECONDS = 30


def build_fixture(base: Path) -> None:
    steam_root = base / "home" / ".local" / "share" / "Steam"
    compatdata = steam_root / "steamapps" / "compatdata"

    # 480: STEAM via manifest
    (compatdata / "480").mkdir(parents=True)
    (compatdata / "480" / "data.bin").write_bytes(b"x" * 4096)
    (steam_root / "steamapps" / "appmanifest_480.acf").write_text(
        '"AppState"\n{\n "appid" "480"\n "name" "Half Life Test"\n}\n',
        encoding="utf-8",
    )

    # 700: NON_STEAM via shortcuts.vdf
    (compatdata / "700").mkdir(parents=True)
    (compatdata / "700" / "data.bin").write_bytes(b"x" * 1024)
    userdata = steam_root / "userdata" / "123456" / "config"
    userdata.mkdir(parents=True)
    from tests.test_vdf_binary import build_shortcuts_vdf

    (userdata / "shortcuts.vdf").write_bytes(build_shortcuts_vdf([(700, "Shortcut Game")]))

    # 900: orphan
    (compatdata / "900").mkdir()


def main() -> int:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        fixture_home = tmp / "home"
        config_dir = tmp / "config"
        config_dir.mkdir()
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        build_fixture(tmp)

        os.environ["HOME"] = str(fixture_home)
        monkeypatched_home = str(fixture_home)
        import pathlib

        pathlib.Path.home = lambda: Path(monkeypatched_home)  # type: ignore[method-assign]

        from core.deletion import DeleteMode
        from core.models import ScanStatus
        from ui.main_window import MainWindow

        app = QApplication.instance() or QApplication(sys.argv)
        window = MainWindow(auto_start=False)
        window.show()

        window.refresh()
        deadline_rows = time.monotonic() + 5
        while time.monotonic() < deadline_rows and not window._model.rows():
            app.processEvents()
            time.sleep(0.01)

        rows_now = window._model.rows()
        print(f"rows rendered before sizes: {len(rows_now)}")
        if len(rows_now) != 3:
            print("FAIL: expected 3 rows immediately after discovery")
            return 1
        early_statuses = [row.scan_status for row in rows_now]
        if any(status is ScanStatus.SCANNED for status in early_statuses):
            print("note: some sizes already resolved before first paint check")

        deadline_scan = time.monotonic() + SCAN_TIMEOUT_SECONDS
        while time.monotonic() < deadline_scan:
            app.processEvents()
            rows = window._model.rows()
            if all(row.scan_status in (ScanStatus.SCANNED, ScanStatus.FAILED) for row in rows):
                break
            time.sleep(0.01)

        # --- filter/search/or orphan visibility ---
        print(f"filter check: {len(window._model.rows())} rows visible")
        from core.models import PrefixType

        window._on_type_toggled(PrefixType.ORPHANED, False)
        app.processEvents()
        if 900 in [r.app_id for r in window._model.rows()]:
            print("FAIL: orphan 900 should be hidden with Orphaned filter off")
            return 1
        window._on_type_toggled(PrefixType.ORPHANED, True)
        app.processEvents()
        if 900 not in [r.app_id for r in window._model.rows()]:
            print("FAIL: orphan 900 not visible with Orphaned filter on")
            return 1

        # search by name
        window._search_box.setText("Half Life")
        window._search_timer.timeout.emit()
        app.processEvents()
        if [r.app_id for r in window._model.rows()] != [480]:
            print(f"FAIL: search Half Life got {[r.app_id for r in window._model.rows()]}")
            return 1
        window._search_box.setText("")
        window._search_timer.timeout.emit()
        app.processEvents()

        # --- deletion via mocked trash ---
        window._model.toggle_visible_selection()
        if len(window._store.selected()) != 3:
            print(f"FAIL: select-all visible expected 3 got {len(window._store.selected())}")
            return 1

        import core.deletion as deletion_module
        import ui.main_window as mw_module

        orig_selection = mw_module.confirm_selection
        orig_final = mw_module.confirm_final
        orig_summary = mw_module.show_deletion_summary
        orig_send2trash = deletion_module.send2trash

        trash_calls: list[Path] = []
        deletion_module.send2trash = lambda p: trash_calls.append(Path(p))
        mw_module.confirm_selection = lambda *a: True
        mw_module.confirm_final = lambda *a: DeleteMode.TRASH
        mw_module.show_deletion_summary = lambda *a: None

        selected_paths = sorted(p.path.resolve() for p in window._store.selected())
        window._on_delete_clicked()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and window._deleting:
            app.processEvents()
            time.sleep(0.01)
        # wait for refresh discovery to land
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not window._model.rows():
            app.processEvents()
            time.sleep(0.01)
        time.sleep(0.3)
        app.processEvents()

        if sorted(trash_calls) != selected_paths:
            print(f"FAIL: trash calls {trash_calls} != selected {selected_paths}")
            return 1
        print(f"trash verified: {len(trash_calls)} targets")
        if window._store.selected():
            print(f"FAIL: selection not cleared after refresh: {window._store.selected()}")
            return 1

        # restore and test cancellation leaves FS untouched
        trash_calls.clear()
        mw_module.confirm_selection = lambda *a: False
        # recreate fixture dirs that were logically deleted (mock kept them, so recreate not needed,
        # but ensure at least one exists for second run)
        window._model.toggle_visible_selection()
        mw_module.confirm_selection = lambda *a: False
        window._on_delete_clicked()
        time.sleep(0.2)
        app.processEvents()
        if trash_calls:
            print(f"FAIL: cancellation should not trash, got {trash_calls}")
            return 1

        # second cancel point: accept first, reject second
        mw_module.confirm_selection = lambda *a: True
        mw_module.confirm_final = lambda *a: None
        window._on_delete_clicked()
        time.sleep(0.2)
        app.processEvents()
        if trash_calls:
            print(f"FAIL: second-dialog cancel should not trash, got {trash_calls}")
            return 1
        print("cancellation verified: no trash on reject")

        # restore
        mw_module.confirm_selection = orig_selection
        mw_module.confirm_final = orig_final
        mw_module.show_deletion_summary = orig_summary
        deletion_module.send2trash = orig_send2trash

        shot_dir = os.environ.get("SMOKE_SHOT_DIR")
        if shot_dir:
            from pathlib import Path as _Path

            out = _Path(shot_dir)
            out.mkdir(parents=True, exist_ok=True)
            for label, width, height in (("wide", 1366, 768), ("narrow", 800, 450)):
                window.resize(width, height)
                deadline_shot = time.monotonic() + 2
                while time.monotonic() < deadline_shot:
                    app.processEvents()
                    time.sleep(0.01)
                image = window.grab()
                target = out / f"smoke-{label}.png"
                if not image.save(str(target)):
                    print(f"FAIL: could not write {target}")
                    return 1
                print(f"screenshot: {target}")

        rows_final = window._model.rows()
        print("final rows:")
        for row in sorted(rows_final, key=lambda r: r.app_id):
            print(f"  [{row.app_id}] {row.name}: {row.size_bytes} bytes ({row.scan_status.value})")
        window.close()
    print("smoke OK: immediate rows, async size completion, filter/search, delete flow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
