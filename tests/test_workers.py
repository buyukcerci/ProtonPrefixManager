"""Qt tests for the UI worker runnables."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import opener as opener_module
from core.deletion import DeleteMode
from core.discovery import RootSource
from core.models import Prefix, PrefixType
from core.opener import OpenStatus
from core.scanner import ScanEventKind, cache_key
from core.tools import Tool
from ui.workers import (
    DeletionWorker,
    DiscoveryWorker,
    OpenFolderWorker,
    ScanWorker,
    ToolDeletionWorker,
    ToolSizeWorker,
)


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


def test_deletion_worker_streams_results(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core import deletion as deletion_module
    from core.discovery import Library

    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(deletion_module, "send2trash", fake_send2trash)
    library_path = tmp_path / "lib"
    compatdata = library_path / "steamapps" / "compatdata"
    prefixes = []
    for app_id in (10, 20):
        target = compatdata / str(app_id)
        target.mkdir(parents=True)
        prefixes.append(
            Prefix(
                app_id=app_id,
                name=f"G{app_id}",
                prefix_type=PrefixType.ORPHANED,
                path=target,
                library=str(library_path),
            )
        )
    library = Library(path=library_path.resolve(), root=tmp_path.resolve())

    worker = DeletionWorker(prefixes, [library], DeleteMode.TRASH, epoch=9)
    events: list[tuple[object, int]] = []
    worker.signals.result_ready.connect(lambda r, e: events.append((r, e)))
    with qtbot.waitSignal(worker.signals.finished, timeout=10000) as blocker:
        QThreadPool_start(worker)

    finished_epoch = blocker.args[0]
    assert finished_epoch == 9
    assert [epoch for _, epoch in events] == [9, 9]
    results = [r for r, _ in events]
    assert all(r.status.value == "deleted" for r in results)
    assert calls == [prefixes[0].path.resolve(), prefixes[1].path.resolve()]


def test_deletion_worker_isolates_permission_failure(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core import deletion as deletion_module
    from core.deletion import DeletionStatus
    from core.discovery import Library

    def failing(path: Path) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(deletion_module, "send2trash", failing)
    library_path = tmp_path / "lib"
    compatdata = library_path / "steamapps" / "compatdata"
    prefixes = []
    for app_id in (1, 2):
        target = compatdata / str(app_id)
        target.mkdir(parents=True)
        prefixes.append(
            Prefix(
                app_id=app_id,
                name=f"G{app_id}",
                prefix_type=PrefixType.ORPHANED,
                path=target,
                library=str(library_path),
            )
        )
    library = Library(path=library_path.resolve(), root=tmp_path.resolve())

    worker = DeletionWorker(prefixes, [library], DeleteMode.TRASH, epoch=4)
    events: list[tuple[object, int]] = []
    worker.signals.result_ready.connect(lambda r, e: events.append((r, e)))
    with qtbot.waitSignal(worker.signals.finished, timeout=10000):
        QThreadPool_start(worker)

    statuses = [r.status for r, _ in events]
    assert DeletionStatus.FAILED in statuses
    failed = next(r for r, _ in events if r.status is DeletionStatus.FAILED)
    assert failed.failure_kind is not None and failed.error is not None


def test_tool_size_worker_stale_break(qtbot, tmp_path: Path) -> None:
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "f.bin").write_bytes(b"x" * 8)
    tool = Tool(name="T", path=tool_dir, root=tmp_path, read_only=False)
    worker = ToolSizeWorker([tool], epoch=1, is_stale=lambda: True)
    emitted: list = []
    worker.signals.sized.connect(lambda payload, epoch: emitted.append((payload, epoch)))
    worker.run()
    assert emitted == []


def test_scan_worker_stale_break(qtbot, tmp_path: Path) -> None:
    prefix_dir = tmp_path / "pfx"
    prefix_dir.mkdir()
    (prefix_dir / "f.bin").write_bytes(b"x" * 4)
    prefix = Prefix(
        app_id=1,
        name="pfx",
        prefix_type=PrefixType.ORPHANED,
        path=prefix_dir,
        library=str(tmp_path),
    )
    worker = ScanWorker([prefix], {}, epoch=1, is_stale=lambda: True)
    emitted: list = []
    worker.signals.scan_event.connect(lambda event, epoch: emitted.append((event, epoch)))
    worker.run()
    assert emitted == []


def test_tool_deletion_worker_reloads_mapping_and_rejects_in_use(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core import deletion as deletion_module
    from core.deletion import DeletionStatus, RejectReason
    from ui import workers as workers_module

    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(deletion_module, "send2trash", fake_send2trash)
    root = tmp_path / "Steam"
    target = root / "compatibilitytools.d" / "OldBuild"
    target.mkdir(parents=True)
    tool = Tool(name="OldBuild", path=target, root=root, read_only=False)

    # The worker reloads the usage mapping at run time instead of
    # trusting a set captured at click time. The reloaded mapping selects
    # the target, so the deletion must be rejected as in use.
    monkeypatch.setattr(workers_module, "load_tool_mapping", lambda roots: ({480: "OldBuild"}, []))

    worker = ToolDeletionWorker([tool], [root], DeleteMode.TRASH, epoch=5)
    events: list[tuple[object, int]] = []
    worker.signals.result_ready.connect(lambda r, e: events.append((r, e)))
    worker.run()

    assert calls == []
    assert target.is_dir()
    assert [epoch for _, epoch in events] == [5]
    results = [r for r, _ in events]
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.IN_USE


def test_tool_deletion_worker_fails_closed_on_mapping_error(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core import deletion as deletion_module
    from core.deletion import DeletionStatus, RejectReason
    from core.toolmap import ToolMapError
    from ui import workers as workers_module

    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(deletion_module, "send2trash", fake_send2trash)
    root = tmp_path / "Steam"
    first = root / "compatibilitytools.d" / "OldBuild"
    second = root / "compatibilitytools.d" / "Other"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    tools = [
        Tool(name="OldBuild", path=first, root=root, read_only=False),
        Tool(name="Other", path=second, root=root, read_only=False),
    ]

    # A failed mapping reload at run time fails the whole batch closed:
    # nothing is deleted and every target is rejected with the explicit
    # mapping unavailable reason.
    monkeypatch.setattr(
        workers_module,
        "load_tool_mapping",
        lambda roots: ({}, [ToolMapError(path=None, message="broken config")]),
    )

    worker = ToolDeletionWorker(tools, [root], DeleteMode.TRASH, epoch=6)
    events: list[tuple[object, int]] = []
    worker.signals.result_ready.connect(lambda r, e: events.append((r, e)))
    worker.run()

    assert calls == []
    assert first.is_dir()
    assert second.is_dir()
    results = [r for r, _ in events]
    assert len(results) == 2
    assert all(r.status is DeletionStatus.REJECTED for r in results)
    assert all(r.reject_reason is RejectReason.MAPPING_UNAVAILABLE for r in results)


def test_tool_deletion_worker_fails_closed_when_config_vanishes(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core import deletion as deletion_module
    from core.deletion import DeletionStatus, RejectReason

    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(deletion_module, "send2trash", fake_send2trash)
    root = tmp_path / "Steam"
    target = root / "compatibilitytools.d" / "OldBuild"
    target.mkdir(parents=True)
    config_path = root / "config" / "config.vdf"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('"CompatToolMapping"\n{\n}\n', encoding="utf-8")
    tool = Tool(name="OldBuild", path=target, root=root, read_only=False)

    worker = ToolDeletionWorker([tool], [root], DeleteMode.TRASH, epoch=7)
    # The mapping file exists when the batch is armed and is deleted before
    # the worker reloads it. The reload must fail closed instead of reading
    # the vanished file as an empty known mapping.
    config_path.unlink()
    events: list[tuple[object, int]] = []
    worker.signals.result_ready.connect(lambda r, e: events.append((r, e)))
    worker.run()

    assert calls == []
    assert target.is_dir()
    results = [r for r, _ in events]
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.MAPPING_UNAVAILABLE


def test_tool_deletion_worker_reports_unexpected_failure(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core import deletion as deletion_module
    from core.deletion import DeletionStatus, FailureKind
    from ui import workers as workers_module

    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(deletion_module, "send2trash", fake_send2trash)
    root = tmp_path / "Steam"
    target = root / "compatibilitytools.d" / "OldBuild"
    target.mkdir(parents=True)
    config_path = root / "config" / "config.vdf"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('"CompatToolMapping"\n{\n}\n', encoding="utf-8")
    tool = Tool(name="OldBuild", path=target, root=root, read_only=False)

    def broken_batch(*args: object, **kwargs: object) -> list[object]:
        raise RuntimeError("unexpected backend crash")

    monkeypatch.setattr(workers_module, "delete_tools", broken_batch)
    worker = ToolDeletionWorker([tool], [root], DeleteMode.TRASH, epoch=8)
    events: list[tuple[object, int]] = []
    finished: list[int] = []
    worker.signals.result_ready.connect(lambda r, e: events.append((r, e)))
    worker.signals.finished.connect(finished.append)
    worker.run()

    # An unexpected batch error must not strand the window: finished
    # emits anyway and the target is reported as a failed result.
    assert finished == [8]
    results = [r for r, _ in events]
    assert len(results) == 1
    assert results[0].status is DeletionStatus.FAILED
    assert results[0].failure_kind is FailureKind.OS
    assert results[0].error is not None
    assert calls == []
    assert target.is_dir()


def test_tool_deletion_worker_fails_closed_when_mapping_shrinks_app_id(
    tmp_path: Path,
) -> None:
    from core.deletion import DeletionStatus, RejectReason

    root = tmp_path / "Steam"
    target = root / "compatibilitytools.d" / "OldBuild"
    target.mkdir(parents=True)
    config_path = root / "config" / "config.vdf"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('"CompatToolMapping"\n{\n}\n', encoding="utf-8")
    tool = Tool(name="OldBuild", path=target, root=root, read_only=False)

    worker = ToolDeletionWorker(
        [tool],
        [root],
        DeleteMode.TRASH,
        epoch=9,
        scan_mapping={480: "OldBuild"},
    )
    events: list[tuple[object, int]] = []
    worker.signals.result_ready.connect(lambda r, e: events.append((r, e)))
    worker.run()

    results = [r for r, _ in events]
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.MAPPING_UNAVAILABLE


def test_tool_deletion_worker_allows_mapping_value_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from core import deletion as deletion_module
    from core.deletion import DeletionStatus, RejectReason

    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(deletion_module, "send2trash", fake_send2trash)
    root = tmp_path / "Steam"
    target = root / "compatibilitytools.d" / "OldBuild"
    target.mkdir(parents=True)
    config_path = root / "config" / "config.vdf"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('"CompatToolMapping"\n{\n"480" "OtherTool"\n}\n', encoding="utf-8")
    tool = Tool(name="OldBuild", path=target, root=root, read_only=False)

    worker = ToolDeletionWorker(
        [tool],
        [root],
        DeleteMode.TRASH,
        epoch=10,
        scan_mapping={480: "OldBuild"},
    )
    events: list[tuple[object, int]] = []
    worker.signals.result_ready.connect(lambda r, e: events.append((r, e)))
    worker.run()

    results = [r for r, _ in events]
    assert len(results) == 1
    assert results[0].status is DeletionStatus.DELETED
    assert results[0].reject_reason is not RejectReason.MAPPING_UNAVAILABLE
    assert calls == [target]


def test_tool_deletion_worker_fails_closed_when_contributing_roots_shrink(
    tmp_path: Path,
) -> None:
    from core.deletion import DeletionStatus, RejectReason

    root1 = tmp_path / "Steam1"
    root2 = tmp_path / "Steam2"
    target = root1 / "compatibilitytools.d" / "OldBuild"
    target.mkdir(parents=True)
    config2 = root2 / "config" / "config.vdf"
    config2.parent.mkdir(parents=True)
    config2.write_text('"CompatToolMapping"\n{\n}\n', encoding="utf-8")
    tool = Tool(name="OldBuild", path=target, root=root1, read_only=False)

    worker = ToolDeletionWorker(
        [tool],
        [root1, root2],
        DeleteMode.TRASH,
        epoch=10,
        scan_contributing_roots=[root1, root2],
    )
    events: list[tuple[object, int]] = []
    worker.signals.result_ready.connect(lambda r, e: events.append((r, e)))
    worker.run()

    results = [r for r, _ in events]
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.MAPPING_UNAVAILABLE


def test_tool_deletion_worker_unexpected_failure_avoids_duplicate_emission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.deletion import DeletionStatus, FailureKind, ToolDeletionResult
    from ui import workers as workers_module

    root = tmp_path / "Steam"
    target1 = root / "compatibilitytools.d" / "Build1"
    target2 = root / "compatibilitytools.d" / "Build2"
    target1.mkdir(parents=True)
    target2.mkdir(parents=True)
    config_path = root / "config" / "config.vdf"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('"CompatToolMapping"\n{\n}\n', encoding="utf-8")
    tool1 = Tool(name="Build1", path=target1, root=root, read_only=False)
    tool2 = Tool(name="Build2", path=target2, root=root, read_only=False)

    def partially_failing_delete(*args: object, **kwargs: object):
        yield ToolDeletionResult(tool=tool1, mode=DeleteMode.TRASH, status=DeletionStatus.DELETED)
        raise RuntimeError("crash after first tool")

    monkeypatch.setattr(workers_module, "delete_tools", partially_failing_delete)
    worker = ToolDeletionWorker([tool1, tool2], [root], DeleteMode.TRASH, epoch=11)
    events: list[tuple[object, int]] = []
    finished: list[int] = []
    worker.signals.result_ready.connect(lambda r, e: events.append((r, e)))
    worker.signals.finished.connect(finished.append)
    worker.run()

    assert finished == [11]
    results = [r for r, _ in events]
    assert len(results) == 2
    assert results[0].tool.name == "Build1"
    assert results[0].status is DeletionStatus.DELETED
    assert results[1].tool.name == "Build2"
    assert results[1].status is DeletionStatus.FAILED
    assert results[1].failure_kind is FailureKind.OS


def test_discovery_worker_finished_barrier_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.discovery import DiscoveryErrorKind
    from ui import workers as workers_module

    def failing_discover(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated discovery crash")

    monkeypatch.setattr(workers_module, "discover", failing_discover)
    worker = DiscoveryWorker([], epoch=12)
    finished_events: list[tuple[object, int]] = []
    worker.signals.finished.connect(lambda payload, ep: finished_events.append((payload, ep)))
    worker.run()

    assert len(finished_events) == 1
    (result, prefixes), epoch = finished_events[0]
    assert epoch == 12
    assert prefixes == []
    assert len(result.errors) == 1
    assert result.errors[0].kind is DiscoveryErrorKind.CRASH
    assert "simulated discovery crash" in result.errors[0].message


def test_scan_worker_finished_barrier_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ui import workers as workers_module

    def failing_scan(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated scan crash")

    monkeypatch.setattr(workers_module, "scan_prefixes", failing_scan)
    worker = ScanWorker([], {}, epoch=13)
    finished: list[int] = []
    worker.signals.finished.connect(finished.append)
    worker.run()
    assert finished == [13]
