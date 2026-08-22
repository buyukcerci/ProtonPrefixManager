"""Qt tests for the UI worker runnables."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import opener as opener_module
from core.discovery import RootSource
from core.models import Prefix, PrefixType
from core.opener import OpenStatus
from core.scanner import ScanEventKind, cache_key
from ui.workers import DiscoveryWorker, OpenFolderWorker, ScanWorker


def _fixture_home(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / ".local" / "share" / "Steam"
    compatdata = root / "steamapps" / "compatdata"
    (compatdata / "480").mkdir(parents=True)
    return tmp_path, compatdata


def test_discovery_worker_round_trip(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home, _ = _fixture_home(tmp_path)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    worker = DiscoveryWorker([], epoch=1)
    with qtbot.waitSignal(worker.signals.finished, timeout=10000) as blocker:
        QThreadPool_start(worker)
    payload, epoch = blocker.args
    result, prefixes = payload
    assert epoch == 1
    assert [root.source for root in result.roots] == [RootSource.NATIVE]
    assert [prefix.app_id for prefix in prefixes] == [480]


def QThreadPool_start(worker) -> None:  # noqa: N802 (local helper keeps tests terse)
    from PySide6.QtCore import QThreadPool

    QThreadPool.globalInstance().start(worker)


def test_discovery_worker_empty_home(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    worker = DiscoveryWorker([], epoch=2)
    with qtbot.waitSignal(worker.signals.finished, timeout=10000) as blocker:
        QThreadPool_start(worker)
    payload, epoch = blocker.args
    result, prefixes = payload
    assert epoch == 2
    assert result.roots == []
    assert result.libraries == []
    assert prefixes == []


def test_scan_worker_emits_events_and_fills_cache(qtbot, tmp_path: Path) -> None:
    prefix_dir = tmp_path / "compat" / "700"
    prefix_dir.mkdir(parents=True)
    (prefix_dir / "a.bin").write_bytes(b"x" * 30)
    subdir = prefix_dir / "sub"
    subdir.mkdir()
    (subdir / "b.bin").write_bytes(b"y" * 12)
    prefix = Prefix(
        app_id=700,
        name="Game",
        prefix_type=PrefixType.STEAM,
        path=prefix_dir,
        library=str(tmp_path),
    )
    cache: dict[str, dict] = {}
    events: list[tuple[object, int]] = []
    worker = ScanWorker([prefix], cache, epoch=3)
    worker.signals.scan_event.connect(lambda event, epoch: events.append((event, epoch)))
    with qtbot.waitSignal(worker.signals.finished, timeout=10000):
        QThreadPool_start(worker)
    kinds = [event.kind for event, _ in events]
    assert kinds == [ScanEventKind.STARTED, ScanEventKind.COMPLETED]
    completed_event, completed_epoch = events[-1]
    assert completed_epoch == 3
    scanned = completed_event.prefix
    assert isinstance(scanned, Prefix)
    assert scanned.size_bytes == 42
    assert scanned.scan_status is not None and scanned.scan_status.value == "scanned"
    entry = cache.get(cache_key(prefix))
    assert entry is not None and entry["size_bytes"] == 42


def test_open_folder_worker_existing_and_missing(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(opener_module.shutil, "which", lambda name: "/usr/bin/xdg-open")
    monkeypatch.setattr(opener_module, "_run_opener", lambda argv: None)
    results: list[object] = []

    existing = tmp_path / "real"
    existing.mkdir()
    worker = OpenFolderWorker(existing)
    worker.signals.finished.connect(results.append)
    with qtbot.waitSignal(worker.signals.finished, timeout=10000):
        QThreadPool_start(worker)

    missing_worker = OpenFolderWorker(tmp_path / "vanished")
    missing_worker.signals.finished.connect(results.append)
    with qtbot.waitSignal(missing_worker.signals.finished, timeout=10000):
        QThreadPool_start(missing_worker)

    opened, missed = results
    assert opened.status is OpenStatus.OPENED  # type: ignore[attr-defined]
    assert missed.status is OpenStatus.MISSING_PATH  # type: ignore[attr-defined]
