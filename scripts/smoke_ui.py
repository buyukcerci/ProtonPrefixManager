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
    sizes = {480: 4096, 700: 1024, 900: 0}
    for app_id, size in sizes.items():
        prefix = (
            base / "home" / ".local" / "share" / "Steam" / "steamapps" / "compatdata" / str(app_id)
        )
        prefix.mkdir(parents=True)
        if size:
            (prefix / "data.bin").write_bytes(b"x" * size)


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
        ok = all(
            row.size_bytes == expected
            for row, expected in zip(
                sorted(rows_final, key=lambda r: r.app_id), (4096, 1024, 0), strict=True
            )
        )
        window.close()
        if not ok:
            print("FAIL: scanned sizes did not match fixture expectations")
            return 1
    print("smoke OK: immediate rows, async size completion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
