"""Unit tests for core.deletion (all destructive calls mocked or tmp_path-bound)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import deletion as deletion_module
from core.deletion import (
    DeleteMode,
    DeletionStatus,
    FailureKind,
    RejectReason,
    compatdata_path,
    delete_prefixes,
)
from core.discovery import Library
from core.models import Prefix, PrefixType


def _make_library(base: Path, name: str = "lib") -> tuple[Library, Path]:
    library_path = base / name
    compatdata = library_path / "steamapps" / "compatdata"
    compatdata.mkdir(parents=True)
    return Library(path=library_path.resolve(), root=base.resolve()), compatdata


def _make_prefix(compatdata: Path, app_id: int = 480) -> Prefix:
    target = compatdata / str(app_id)
    target.mkdir(exist_ok=True)
    return Prefix(
        app_id=app_id,
        name="Game",
        prefix_type=PrefixType.STEAM,
        path=target,
        library=str(compatdata.parent.parent),
    )


@pytest.fixture()
def trash_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(deletion_module, "send2trash", fake_send2trash)
    return calls


def test_trash_happy_path_default_mode(
    tmp_path: Path, trash_calls: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    library, compatdata = _make_library(tmp_path)
    prefix = _make_prefix(compatdata)
    results = delete_prefixes([prefix], [library])
    resolved = compatdata / "480"
    assert trash_calls == [resolved]
    assert len(results) == 1
    assert results[0].status is DeletionStatus.DELETED
    assert results[0].mode is DeleteMode.TRASH
    assert results[0].reject_reason is None
    assert results[0].failure_kind is None


def test_permanent_happy_path_mocked_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library, compatdata = _make_library(tmp_path)
    prefix = _make_prefix(compatdata)
    calls: list[Path] = []
    monkeypatch.setattr(deletion_module, "_remove_tree", lambda path: calls.append(path))
    results = delete_prefixes([prefix], [library], mode=DeleteMode.PERMANENT)
    assert calls == [compatdata / "480"]
    assert results[0].status is DeletionStatus.DELETED
    assert results[0].mode is DeleteMode.PERMANENT


def test_permanent_real_removal_wiring(tmp_path: Path) -> None:
    library, compatdata = _make_library(tmp_path)
    prefix = _make_prefix(compatdata)
    target = compatdata / "480"
    results = delete_prefixes([prefix], [library], mode=DeleteMode.PERMANENT)
    assert results[0].status is DeletionStatus.DELETED
    assert not target.exists()


def test_arbitrary_path_rejected_and_untouched(tmp_path: Path, trash_calls: list[Path]) -> None:
    library, _ = _make_library(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    prefix = Prefix(
        app_id=1,
        name="Evil",
        prefix_type=PrefixType.ORPHANED,
        path=victim / "1",
        library=str(victim),
    )
    results = delete_prefixes([prefix], [library])
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.NOT_IN_COMPATDATA
    assert trash_calls == []
    assert victim.is_dir()


def test_traversal_collapses_outside_compatdata(tmp_path: Path, trash_calls: list[Path]) -> None:
    library, compatdata = _make_library(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    sneaky = Prefix(
        app_id=123,
        name="Sneaky",
        prefix_type=PrefixType.ORPHANED,
        path=compatdata / ".." / ".." / "victim" / ".." / "123",
        library=str(library.path),
    )
    results = delete_prefixes([sneaky], [library])
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.NOT_IN_COMPATDATA
    assert trash_calls == []
    assert victim.is_dir()


def test_nested_target_rejected(tmp_path: Path, trash_calls: list[Path]) -> None:
    library, compatdata = _make_library(tmp_path)
    nested_parent = compatdata / "480"
    nested_parent.mkdir()
    deep = Prefix(
        app_id=555,
        name="Deep",
        prefix_type=PrefixType.ORPHANED,
        path=nested_parent / "555",
        library=str(library.path),
    )
    (nested_parent / "555").mkdir()
    results = delete_prefixes([deep], [library])
    assert results[0].reject_reason is RejectReason.NOT_DIRECT_CHILD
    assert trash_calls == []


def test_symlink_escape_rejected_outside_intact(tmp_path: Path, trash_calls: list[Path]) -> None:
    library, compatdata = _make_library(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (compatdata / "999").symlink_to(outside)
    prefix = Prefix(
        app_id=999,
        name="Linked",
        prefix_type=PrefixType.ORPHANED,
        path=compatdata / "999",
        library=str(library.path),
    )
    results = delete_prefixes([prefix], [library])
    assert results[0].reject_reason is RejectReason.SYMLINK
    assert trash_calls == []
    assert outside.is_dir()


def test_stale_library_record_rejected(tmp_path: Path, trash_calls: list[Path]) -> None:
    old_library, old_compatdata = _make_library(tmp_path, "old-lib")
    prefix = _make_prefix(old_compatdata)
    current_library, _ = _make_library(tmp_path, "current-lib")
    results = delete_prefixes([prefix], [current_library])
    assert results[0].reject_reason is RejectReason.STALE_LIBRARY
    assert trash_calls == []
    assert (old_compatdata / "480").is_dir()


@pytest.mark.parametrize(
    ("app_id", "dirname"),
    [(480, "not-digits"), (481, "480")],
    ids=["non-digit-name", "appid-mismatch"],
)
def test_name_mismatch_rejected_both_ways(
    tmp_path: Path, trash_calls: list[Path], app_id: int, dirname: str
) -> None:
    library, compatdata = _make_library(tmp_path)
    target = compatdata / dirname
    target.mkdir()
    prefix = Prefix(
        app_id=app_id,
        name="Mismatched",
        prefix_type=PrefixType.ORPHANED,
        path=target,
        library=str(library.path),
    )
    results = delete_prefixes([prefix], [library])
    assert results[0].reject_reason is RejectReason.NAME_MISMATCH
    assert trash_calls == []


def test_missing_target_rejected(tmp_path: Path, trash_calls: list[Path]) -> None:
    library, compatdata = _make_library(tmp_path)
    ghost = Prefix(
        app_id=700,
        name="Ghost",
        prefix_type=PrefixType.ORPHANED,
        path=compatdata / "700",
        library=str(library.path),
    )
    results = delete_prefixes([ghost], [library])
    assert results[0].reject_reason is RejectReason.MISSING
    assert trash_calls == []


def test_partial_failure_reports_every_target(
    tmp_path: Path, trash_calls: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    library, compatdata = _make_library(tmp_path)
    prefixes = [_make_prefix(compatdata, app_id) for app_id in (1, 2, 3)]
    fixture_send2trash = deletion_module.send2trash

    def failing_on_two(path: Path) -> None:
        if path.name == "2":
            raise PermissionError(13, "Permission denied")
        fixture_send2trash(path)

    monkeypatch.setattr(deletion_module, "send2trash", failing_on_two)
    results = delete_prefixes(prefixes, [library])
    assert [result.status for result in results] == [
        DeletionStatus.DELETED,
        DeletionStatus.FAILED,
        DeletionStatus.DELETED,
    ]
    failed = results[1]
    assert failed.failure_kind is FailureKind.PERMISSION
    assert failed.error is not None and "Permission denied" in failed.error
    assert {result.prefix.app_id for result in results} == {1, 2, 3}


def test_generic_oserror_from_trash_reports_trash_kind(
    tmp_path: Path, trash_calls: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    library, compatdata = _make_library(tmp_path)
    prefix = _make_prefix(compatdata)

    def busy(path: Path) -> None:
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(deletion_module, "send2trash", busy)
    results = delete_prefixes([prefix], [library])
    assert results[0].status is DeletionStatus.FAILED
    assert results[0].failure_kind is FailureKind.TRASH
    assert results[0].error is not None and "busy" in results[0].error


def test_generic_oserror_from_permanent_reports_os_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library, compatdata = _make_library(tmp_path)
    prefix = _make_prefix(compatdata)

    def busy(path: Path) -> None:
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(deletion_module, "_remove_tree", busy)
    results = delete_prefixes([prefix], [library], mode=DeleteMode.PERMANENT)
    assert results[0].failure_kind is FailureKind.OS


def test_filenotfound_from_seam_is_missing_rejection(
    tmp_path: Path, trash_calls: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    library, compatdata = _make_library(tmp_path)
    prefix = _make_prefix(compatdata)

    def vanished(path: Path) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(deletion_module, "send2trash", vanished)
    results = delete_prefixes([prefix], [library])
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.MISSING


def test_duplicate_records_single_result(tmp_path: Path, trash_calls: list[Path]) -> None:
    library, compatdata = _make_library(tmp_path)
    prefix_a = _make_prefix(compatdata)
    prefix_b = _make_prefix(compatdata)
    results = delete_prefixes([prefix_a, prefix_b], [library])
    assert len(results) == 1
    assert trash_calls == [compatdata / "480"]


def test_empty_input_no_seam_calls(trash_calls: list[Path]) -> None:
    assert delete_prefixes([], []) == []
    assert trash_calls == []


def test_second_library_in_current_set_accepted(tmp_path: Path, trash_calls: list[Path]) -> None:
    first, _ = _make_library(tmp_path, "first")
    second, second_compatdata = _make_library(tmp_path, "second")
    prefix = _make_prefix(second_compatdata, app_id=880)
    results = delete_prefixes([prefix], [first, second])
    assert results[0].status is DeletionStatus.DELETED
    assert trash_calls == [second_compatdata / "880"]


def test_compatdata_path_helper(tmp_path: Path) -> None:
    library, compatdata = _make_library(tmp_path)
    assert compatdata_path(library) == compatdata.resolve()
