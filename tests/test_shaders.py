"""Unit tests for core.shaders and shader cache deletion."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import deletion as deletion_module
from core.deletion import (
    DeleteMode,
    DeletionStatus,
    RejectReason,
    delete_shader_caches,
    iter_delete_shader_caches,
)
from core.discovery import Library
from core.models import PrefixType
from core.shaders import ShaderCache, enumerate_shader_caches
from tests.test_enumeration import _make_manifest, _make_shortcut_root


def _library(base: Path, name: str = "lib", app_ids: tuple[int, ...] = ()) -> Library:
    library_path = base / name
    (library_path / "steamapps" / "shadercache").mkdir(parents=True)
    for app_id in app_ids:
        (library_path / "steamapps" / "shadercache" / str(app_id)).mkdir()
    return Library(path=library_path.resolve(), root=base.resolve())


@pytest.fixture()
def trash_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []
    monkeypatch.setattr(deletion_module, "send2trash", calls.append)
    return calls


# --- enumeration -----------------------------------------------------------


def test_enumerates_and_classifies(tmp_path: Path) -> None:
    lib = _library(tmp_path, app_ids=(730, 999, 123))
    _make_manifest(lib.path, 730, "Counter-Strike 2")
    root = _make_shortcut_root(tmp_path / "root", {"1": [(123, "My Shortcut")]})
    caches, warnings = enumerate_shader_caches([lib], [root])
    assert warnings == []
    by_id = {cache.app_id: cache for cache in caches}
    assert by_id[730].name == "Counter-Strike 2"
    assert by_id[730].cache_type is PrefixType.STEAM
    assert by_id[123].cache_type is PrefixType.NON_STEAM
    assert by_id[123].name == "My Shortcut"
    assert by_id[999].cache_type is PrefixType.ORPHANED
    assert by_id[999].is_orphan
    assert by_id[730].path == lib.path / "steamapps" / "shadercache" / "730"
    assert by_id[730].library == str(lib.path)
    assert by_id[730].dev_ino is not None


def test_skips_non_numeric_zero_files_and_symlinks(tmp_path: Path) -> None:
    lib = _library(tmp_path, app_ids=(0, 480))
    shadercache = lib.path / "steamapps" / "shadercache"
    (shadercache / "notanapp").mkdir()
    (shadercache / "555").write_text("file, not a dir")
    outside = tmp_path / "outside" / "777"
    outside.mkdir(parents=True)
    (shadercache / "777").symlink_to(outside, target_is_directory=True)
    caches, _ = enumerate_shader_caches([lib], shortcuts_map={})
    assert [cache.app_id for cache in caches] == [480]


def test_missing_shadercache_is_silent(tmp_path: Path) -> None:
    lib_path = tmp_path / "lib"
    (lib_path / "steamapps").mkdir(parents=True)
    lib = Library(path=lib_path, root=tmp_path)
    assert enumerate_shader_caches([lib], shortcuts_map={}) == ([], [])


def test_symlinked_shadercache_is_skipped_with_warning(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "480").mkdir(parents=True)
    lib_path = tmp_path / "lib"
    (lib_path / "steamapps").mkdir(parents=True)
    (lib_path / "steamapps" / "shadercache").symlink_to(outside, target_is_directory=True)
    lib = Library(path=lib_path, root=tmp_path)
    caches, warnings = enumerate_shader_caches([lib], shortcuts_map={})
    assert caches == []
    assert len(warnings) == 1
    assert warnings[0].path == lib_path / "steamapps" / "shadercache"
    assert "symlinked" in warnings[0].message


def test_symlinked_steamapps_is_skipped(tmp_path: Path) -> None:
    real = _library(tmp_path, "real", app_ids=(480,))
    lib_path = tmp_path / "linked"
    lib_path.mkdir()
    (lib_path / "steamapps").symlink_to(real.path / "steamapps", target_is_directory=True)
    lib = Library(path=lib_path, root=tmp_path)
    assert enumerate_shader_caches([lib], shortcuts_map={}) == ([], [])


def test_dedupes_on_resolved_path(tmp_path: Path) -> None:
    lib = _library(tmp_path, app_ids=(480,))
    caches, _ = enumerate_shader_caches([lib, lib], shortcuts_map={})
    assert len(caches) == 1


# --- deletion --------------------------------------------------------------


def _cache(lib: Library, app_id: int, *, with_identity: bool = True) -> ShaderCache:
    path = lib.path / "steamapps" / "shadercache" / str(app_id)
    path.mkdir(parents=True, exist_ok=True)
    st = os.stat(path)
    return ShaderCache(
        app_id=app_id,
        name=f"Game {app_id}",
        cache_type=PrefixType.ORPHANED,
        path=path,
        library=str(lib.path),
        dev_ino=(st.st_dev, st.st_ino) if with_identity else None,
    )


def test_trash_happy_path(tmp_path: Path, trash_calls: list[Path]) -> None:
    lib = _library(tmp_path)
    cache = _cache(lib, 480)
    results = delete_shader_caches([cache], [lib])
    assert [r.status for r in results] == [DeletionStatus.DELETED]
    assert results[0].mode is DeleteMode.TRASH
    assert trash_calls == [cache.path]


def test_permanent_removes_tree(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    cache = _cache(lib, 480)
    (cache.path / "fozpipelinesv6").mkdir()
    (cache.path / "fozpipelinesv6" / "blob").write_bytes(b"x" * 10)
    results = delete_shader_caches([cache], [lib], DeleteMode.PERMANENT)
    assert results[0].status is DeletionStatus.DELETED
    assert not cache.path.exists()


def test_rejects_name_mismatch(tmp_path: Path, trash_calls: list[Path]) -> None:
    lib = _library(tmp_path)
    cache = _cache(lib, 480)
    cache.app_id = 481
    results = delete_shader_caches([cache], [lib])
    assert results[0].reject_reason is RejectReason.NAME_MISMATCH
    assert trash_calls == []


def test_rejects_target_outside_shadercache(tmp_path: Path, trash_calls: list[Path]) -> None:
    lib = _library(tmp_path)
    stray = tmp_path / "elsewhere" / "480"
    stray.mkdir(parents=True)
    cache = ShaderCache(480, "Game", PrefixType.ORPHANED, stray, str(tmp_path / "elsewhere"))
    results = delete_shader_caches([cache], [lib])
    assert results[0].reject_reason is RejectReason.NOT_IN_SHADERCACHE
    assert trash_calls == []


def test_rejects_nested_target(tmp_path: Path, trash_calls: list[Path]) -> None:
    lib = _library(tmp_path)
    nested = lib.path / "steamapps" / "shadercache" / "480" / "480"
    nested.mkdir(parents=True)
    cache = ShaderCache(480, "Game", PrefixType.ORPHANED, nested, str(lib.path))
    results = delete_shader_caches([cache], [lib])
    assert results[0].reject_reason is RejectReason.NOT_DIRECT_CHILD
    assert trash_calls == []


def test_rejects_stale_library(tmp_path: Path, trash_calls: list[Path]) -> None:
    lib = _library(tmp_path, "old")
    current = _library(tmp_path, "current")
    cache = _cache(lib, 480)
    results = delete_shader_caches([cache], [current])
    assert results[0].reject_reason is RejectReason.STALE_LIBRARY
    assert trash_calls == []


def test_rejects_symlinked_shadercache_boundary(tmp_path: Path, trash_calls: list[Path]) -> None:
    outside = tmp_path / "outside"
    target = outside / "480"
    target.mkdir(parents=True)
    lib_path = tmp_path / "lib"
    (lib_path / "steamapps").mkdir(parents=True)
    (lib_path / "steamapps" / "shadercache").symlink_to(outside, target_is_directory=True)
    lib = Library(path=lib_path.resolve(), root=tmp_path.resolve())
    cache = ShaderCache(480, "Game", PrefixType.ORPHANED, target.resolve(), str(lib.path))
    results = delete_shader_caches([cache], [lib])
    assert results[0].reject_reason is RejectReason.SYMLINK
    assert trash_calls == []
    assert target.is_dir()


def test_rejects_symlinked_record_path(tmp_path: Path, trash_calls: list[Path]) -> None:
    lib = _library(tmp_path)
    real = _cache(lib, 480)
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    (link_dir / "480").symlink_to(real.path, target_is_directory=True)
    cache = ShaderCache(480, "Game", PrefixType.ORPHANED, link_dir / "480", str(lib.path))
    results = delete_shader_caches([cache], [lib])
    assert results[0].reject_reason is RejectReason.SYMLINK
    assert trash_calls == []


def test_rejects_identity_mismatch_after_swap(tmp_path: Path, trash_calls: list[Path]) -> None:
    lib = _library(tmp_path)
    cache = _cache(lib, 480)
    cache.path.rmdir()
    cache.path.mkdir()  # same name, new inode
    results = delete_shader_caches([cache], [lib])
    assert results[0].reject_reason is RejectReason.IDENTITY_MISMATCH
    assert trash_calls == []


def test_rejects_missing_target(tmp_path: Path, trash_calls: list[Path]) -> None:
    lib = _library(tmp_path)
    cache = _cache(lib, 480, with_identity=False)
    cache.path.rmdir()
    results = delete_shader_caches([cache], [lib])
    assert results[0].reject_reason is RejectReason.MISSING
    assert trash_calls == []


def test_aliases_report_once(tmp_path: Path, trash_calls: list[Path]) -> None:
    lib = _library(tmp_path)
    cache = _cache(lib, 480)
    alias = ShaderCache(480, "Alias", PrefixType.STEAM, cache.path, cache.library, 0, cache.dev_ino)
    results = delete_shader_caches([cache, alias], [lib])
    assert len(results) == 1
    assert len(trash_calls) == 1


def test_permission_error_is_reported_and_batch_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = _library(tmp_path)
    first = _cache(lib, 480)
    second = _cache(lib, 481)
    calls: list[Path] = []

    def fake_trash(path: Path) -> None:
        calls.append(path)
        if path == first.path:
            raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(deletion_module, "send2trash", fake_trash)
    results = delete_shader_caches([first, second], [lib])
    assert [r.status for r in results] == [DeletionStatus.FAILED, DeletionStatus.DELETED]
    assert results[0].error is not None and "Permission denied" in results[0].error


def test_iter_yields_before_later_targets(tmp_path: Path, trash_calls: list[Path]) -> None:
    lib = _library(tmp_path)
    results = iter_delete_shader_caches([_cache(lib, 480), _cache(lib, 481)], [lib])
    assert next(results).status is DeletionStatus.DELETED
    assert len(trash_calls) == 1


def test_prefix_boundary_does_not_admit_shader_caches(
    tmp_path: Path, trash_calls: list[Path]
) -> None:
    """A shader cache path never passes through the prefix deletion path."""
    from core.deletion import delete_prefixes
    from core.models import Prefix

    lib = _library(tmp_path)
    (lib.path / "steamapps" / "compatdata").mkdir()
    cache = _cache(lib, 480)
    prefix = Prefix(480, "Game", PrefixType.ORPHANED, cache.path, str(lib.path))
    results = delete_prefixes([prefix], [lib])
    assert results[0].status is DeletionStatus.REJECTED
    assert trash_calls == []


# --- worker ----------------------------------------------------------------


def test_shader_deletion_worker_keeps_deleted_results_on_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.deletion import ShaderDeletionResult
    from ui import workers as workers_module
    from ui.workers import ShaderDeletionWorker

    lib = _library(tmp_path)
    first = _cache(lib, 480)
    second = _cache(lib, 481)

    def crashing(*args: object, **kwargs: object):
        yield ShaderDeletionResult(first, DeleteMode.TRASH, DeletionStatus.DELETED)
        raise RuntimeError("boom")

    monkeypatch.setattr(workers_module, "iter_delete_shader_caches", crashing)
    worker = ShaderDeletionWorker([first, second], [lib], DeleteMode.TRASH, epoch=7)
    results: list[object] = []
    finished: list[int] = []
    worker.signals.result_ready.connect(lambda result, epoch: results.append(result))
    worker.signals.finished.connect(finished.append)
    worker.run()
    assert finished == [7]
    assert [r.status for r in results] == [DeletionStatus.DELETED, DeletionStatus.FAILED]
    assert results[1].cache is second
    assert "boom" in (results[1].error or "")


def test_summary_body_names_shader_caches(tmp_path: Path) -> None:
    from core.deletion import ShaderDeletionResult
    from ui.dialogs import summary_body

    lib = _library(tmp_path)
    cache = _cache(lib, 480)
    body = summary_body(
        [
            ShaderDeletionResult(
                cache, DeleteMode.TRASH, DeletionStatus.REJECTED, RejectReason.SYMLINK
            )
        ]
    )
    assert "[480] Game 480 (shader cache): rejected (symlink)" in body
