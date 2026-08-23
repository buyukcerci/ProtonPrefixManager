"""Unit tests for core.scanner."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import scanner as scanner_module
from core.config import AppConfig, load_config, save_config
from core.discovery import Library
from core.enumeration import enumerate_prefixes
from core.models import Prefix, PrefixType, ScanStatus, format_size
from core.scanner import (
    CacheEntry,
    ScanEventKind,
    cache_key,
    invalidate,
    load_cached,
    refresh_needed,
    save_cached,
    scan_prefix,
    scan_prefixes,
)


def _write_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _make_prefix(base: Path, name: str = "pfx", app_id: int = 1) -> Prefix:
    prefix_dir = base / name
    prefix_dir.mkdir(parents=True, exist_ok=True)
    return Prefix(
        app_id=app_id,
        name=name,
        prefix_type=PrefixType.ORPHANED,
        path=prefix_dir.resolve(),
        library=str(base.resolve()),
    )


def _scanned_copy(prefix: Prefix, size_bytes: int) -> Prefix:
    return replace(
        prefix,
        size_bytes=size_bytes,
        scan_status=ScanStatus.SCANNED,
        last_scanned=datetime.now(UTC),
    )


def test_empty_folder_totals_zero(tmp_path: Path) -> None:
    assert scan_prefix(tmp_path / "pfx").size_bytes == 0


def test_single_file_exact_total(tmp_path: Path) -> None:
    target = tmp_path / "pfx"
    _write_file(target / "file.bin", 123)
    assert scan_prefix(target).size_bytes == 123


def test_nested_files_summed(tmp_path: Path) -> None:
    target = tmp_path / "pfx"
    _write_file(target / "a" / "one", 10)
    _write_file(target / "a" / "b" / "two", 20)
    _write_file(target / "a" / "b" / "c" / "three", 30)
    result = scan_prefix(target)
    assert result.size_bytes == 60
    assert format_size(result.size_bytes) == "60 B"


def test_dir_symlink_outside_ignored(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write_file(outside / "big.bin", 5000)
    target = tmp_path / "pfx"
    _write_file(target / "own.bin", 7)
    (target / "escape").symlink_to(outside)
    assert scan_prefix(target).size_bytes == 7


def test_file_symlink_skipped_not_counted(tmp_path: Path) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"y" * 500)
    target = tmp_path / "pfx"
    target.mkdir()
    (target / "link.bin").symlink_to(outside)
    assert scan_prefix(target).size_bytes == 0


def test_broken_symlink_ignored(tmp_path: Path) -> None:
    target = tmp_path / "pfx"
    target.mkdir()
    (target / "dangling").symlink_to(tmp_path / "nowhere")
    assert scan_prefix(target).size_bytes == 0


def test_missing_root_yields_error_result(tmp_path: Path) -> None:
    result = scan_prefix(tmp_path / "vanished")
    assert result.error is not None
    assert result.size_bytes == 0


def test_root_file_yields_error_result(tmp_path: Path) -> None:
    target = tmp_path / "plain-file"
    target.write_text("data", encoding="utf-8")
    assert scan_prefix(target).error is not None


@pytest.mark.skipif(os.geteuid() == 0, reason="permission checks do not apply to root")
def test_unreadable_subdir_skipped_without_error(tmp_path: Path) -> None:
    target = tmp_path / "pfx"
    _write_file(target / "readable.bin", 12)
    locked = target / "locked"
    _write_file(locked / "hidden.bin", 9999)
    locked.chmod(0o000)
    try:
        result = scan_prefix(target)
    finally:
        locked.chmod(0o755)
    assert result.error is None
    assert result.size_bytes == 12


@pytest.mark.skipif(os.geteuid() == 0, reason="permission checks do not apply to root")
def test_unreadable_root_yields_error_result(tmp_path: Path) -> None:
    target = tmp_path / "locked-root"
    _write_file(target / "inner.bin", 50)
    target.chmod(0o000)
    try:
        result = scan_prefix(target)
    finally:
        target.chmod(0o755)
    assert result.error is not None


def test_disappearing_file_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "pfx"
    keep = target / "keep.bin"
    vanish = target / "gone.bin"
    _write_file(keep, 40)
    _write_file(vanish, 9999)
    original_listdir = scanner_module._listdir

    def listdir_then_delete(path: Path) -> list:
        entries = original_listdir(path)
        if path == target.resolve():
            vanish.unlink()
        return entries

    monkeypatch.setattr(scanner_module, "_listdir", listdir_then_delete)
    result = scan_prefix(target.resolve())
    assert result.error is None
    assert result.size_bytes == 40


def test_cache_round_trip_via_appconfig(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path / "libs")
    config = AppConfig()
    save_cached(config.size_cache, _scanned_copy(prefix, 321))
    config_path = tmp_path / "config.json"
    save_config(config, config_path)
    loaded = load_config(config_path)
    restored = load_cached(loaded.size_cache, prefix)
    assert restored is not None
    assert restored.size_bytes == 321
    assert restored.scan_status is ScanStatus.SCANNED
    assert restored.last_scanned is not None


def test_invalid_cache_entry_treated_as_miss(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    bad_entries: list[object] = [
        {},
        {"size_bytes": "x"},
        {"size_bytes": -5},
        {"size_bytes": True},
        {"size_bytes": 5},
        {"nope": 1},
    ]
    for bad in bad_entries:
        cache: dict[str, dict] = {cache_key(prefix): bad}  # type: ignore[dict-item]
        assert load_cached(cache, prefix) is None


def test_corrupt_timestamp_treated_as_miss(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    cache: dict[str, dict] = {cache_key(prefix): {"size_bytes": 10, "last_scanned": "junk"}}
    assert load_cached(cache, prefix) is None


def test_invalidate_removes_entry(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    cache: dict[str, dict] = {}
    save_cached(cache, _scanned_copy(prefix, 15))
    assert cache_key(prefix) in cache
    invalidate(cache, prefix)
    assert cache_key(prefix) not in cache
    invalidate(cache, prefix)


def test_refresh_needed_on_miss_force_and_hit(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    cache: dict[str, dict] = {}
    assert refresh_needed(cache, prefix) is True
    save_cached(cache, _scanned_copy(prefix, 15))
    assert refresh_needed(cache, prefix) is False
    assert refresh_needed(cache, prefix, force=True) is True


def test_save_cached_requires_timestamp(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    with pytest.raises(ValueError):
        save_cached({}, prefix)


def test_event_sequence_completed(tmp_path: Path) -> None:
    prefix_a = _make_prefix(tmp_path, "a", app_id=1)
    prefix_b = _make_prefix(tmp_path, "b", app_id=2)
    _write_file(prefix_a.path / "f", 11)
    events = list(scan_prefixes([prefix_a, prefix_b]))
    assert [event.kind for event in events] == [
        ScanEventKind.STARTED,
        ScanEventKind.COMPLETED,
        ScanEventKind.STARTED,
        ScanEventKind.COMPLETED,
    ]
    completed = events[1]
    assert isinstance(completed.prefix, Prefix)
    assert completed.prefix.scan_status is ScanStatus.SCANNED
    assert completed.prefix.size_bytes == 11
    assert completed.size_bytes == 11
    assert completed.prefix.last_scanned is not None


def test_cache_hit_completes_without_walk(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    cache: dict[str, dict] = {}
    first = list(scan_prefixes([prefix], cache))
    assert first[-1].kind is ScanEventKind.COMPLETED
    _write_file(prefix.path / "extra.bin", 777)
    second = list(scan_prefixes([prefix], cache))
    assert second[-1].kind is ScanEventKind.COMPLETED
    assert isinstance(second[-1].prefix, Prefix)
    assert second[-1].prefix.size_bytes == 0


def test_force_refresh_rescans_despite_cache(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    cache: dict[str, dict] = {}
    list(scan_prefixes([prefix], cache))
    _write_file(prefix.path / "new.bin", 55)
    events = list(scan_prefixes([prefix], cache, force_refresh=True))
    assert events[-1].size_bytes == 55


def test_failed_root_emits_failed_event_and_no_cache(tmp_path: Path) -> None:
    missing = _make_prefix(tmp_path, "ghost")
    missing.path.rmdir()
    cache: dict[str, dict] = {}
    events = list(scan_prefixes([missing], cache))
    assert len(events) == 2
    assert events[-1].kind is ScanEventKind.FAILED
    failed_prefix = events[-1].prefix
    assert isinstance(failed_prefix, Prefix)
    assert failed_prefix.scan_status is ScanStatus.FAILED
    assert events[-1].error is not None
    assert cache == {}


def test_on_event_callback_receives_events(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    received: list = []
    returned = scan_prefixes([prefix], on_event=received.append)
    assert list(returned) == []
    assert [event.kind for event in received] == [ScanEventKind.STARTED, ScanEventKind.COMPLETED]


def test_generator_is_lazy_per_prefix(tmp_path: Path) -> None:
    prefix_a = _make_prefix(tmp_path, "a")
    prefix_b = _make_prefix(tmp_path, "b")
    generator = scan_prefixes([prefix_a, prefix_b])
    first = next(generator)
    assert first.kind is ScanEventKind.STARTED
    generator.close()


def test_large_fixture_correct_total_in_thread(tmp_path: Path) -> None:
    target = tmp_path / "huge"
    expected = 0
    for index in range(1000):
        size = index % 7 + 1
        _write_file(target / f"f{index}.bin", size)
        expected += size
    box: dict[str, int] = {}

    def worker() -> None:
        box["total"] = scan_prefix(target).size_bytes

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert box["total"] == expected


def test_enumerated_prefixes_flow_through_scan_and_cache(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    compatdata = library / "steamapps" / "compatdata" / "480"
    compatdata.mkdir(parents=True)
    _write_file(compatdata / "state" / "file", 25)
    lib = Library(path=library.resolve(), root=tmp_path.resolve())
    prefixes = enumerate_prefixes([lib])
    assert all(prefix.scan_status is ScanStatus.NOT_SCANNED for prefix in prefixes)
    cache: dict[str, dict] = {}
    events = list(scan_prefixes(prefixes, cache))
    scanned = events[-1].prefix
    assert isinstance(scanned, Prefix)
    assert scanned.scan_status is ScanStatus.SCANNED
    assert scanned.size_bytes == 25
    entry = CacheEntry.from_dict(cache.get(cache_key(scanned)))
    assert entry is not None
    assert entry.size_bytes == 25


# --- modified tracking -----------------------------------------------------


def test_scan_prefix_tracks_newest_file_mtime(tmp_path: Path) -> None:
    target = tmp_path / "pfx"
    _write_file(target / "old.bin", 10)
    _write_file(target / "new.bin", 20)
    newest = datetime(2026, 4, 1, 10, 30, 0, tzinfo=UTC)
    older = datetime(2025, 4, 1, 10, 30, 0, tzinfo=UTC)
    os.utime(target / "old.bin", (older.timestamp(), older.timestamp()))
    os.utime(target / "new.bin", (newest.timestamp(), newest.timestamp()))
    result = scan_prefix(target)
    assert result.modified is not None
    assert abs(result.modified.timestamp() - newest.timestamp()) < 1


def test_scan_prefix_modified_none_on_empty_tree(tmp_path: Path) -> None:
    assert scan_prefix(tmp_path / "pfx").modified is None


def test_cache_round_trip_carries_modified(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    modified = datetime(2026, 4, 1, 9, 0, 0, tzinfo=UTC)
    scanned = replace(
        _scanned_copy(prefix, size_bytes=64),
        modified=modified,
    )
    cache: dict[str, dict] = {}
    save_cached(cache, scanned)
    assert cache[cache_key(scanned)]["modified"] == modified.isoformat()
    loaded = load_cached(cache, prefix)
    assert loaded is not None
    assert loaded.modified == modified


def test_cache_round_trip_omits_modified_when_unknown(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    scanned = _scanned_copy(prefix, size_bytes=64)
    cache: dict[str, dict] = {}
    save_cached(cache, scanned)
    assert "modified" not in cache[cache_key(scanned)]
    loaded = load_cached(cache, prefix)
    assert loaded is not None
    assert loaded.modified is None


def test_cache_entries_without_modified_key_stay_valid(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path)
    scanned = _scanned_copy(prefix, size_bytes=32)
    cache: dict[str, dict] = {}
    save_cached(cache, scanned)
    legacy = dict(cache[cache_key(scanned)])
    legacy.pop("modified", None)  # simulate an entry written before the field existed
    cache[cache_key(scanned)] = legacy
    assert refresh_needed(cache, prefix) is False
    entry = CacheEntry.from_dict(legacy)
    assert entry is not None
    assert entry.modified is None


def test_cache_entry_treats_unusable_modified_as_none() -> None:
    """Any unusable "modified" value keeps the entry and its cached size."""
    for bad in (17, True, [], {}):
        parsed = CacheEntry.from_dict(
            {"size_bytes": 5, "last_scanned": "2026-01-01T00:00:00+00:00", "modified": bad}
        )
        assert parsed is not None
        assert parsed.size_bytes == 5
        assert parsed.modified is None
    parsed = CacheEntry.from_dict(
        {"size_bytes": 5, "last_scanned": "2026-01-01T00:00:00+00:00", "modified": "garbage"}
    )
    assert parsed is not None
    assert parsed.modified is None
    good = CacheEntry.from_dict(
        {
            "size_bytes": 5,
            "last_scanned": "2026-01-01T00:00:00+00:00",
            "modified": "2026-02-01T00:00:00+00:00",
        }
    )
    assert good is not None
    assert good.modified == datetime(2026, 2, 1, tzinfo=UTC)


def test_scan_events_carry_modified(tmp_path: Path) -> None:
    prefix = _make_prefix(tmp_path, name="pfx", app_id=77)
    newest = datetime(2026, 2, 2, 2, 2, 2, tzinfo=UTC)
    _write_file(prefix.path / "data.bin", 8)
    os.utime(prefix.path / "data.bin", (newest.timestamp(), newest.timestamp()))
    cache: dict[str, dict] = {}
    events = list(scan_prefixes([prefix], cache))
    completed = events[-1].prefix
    assert isinstance(completed, Prefix)
    assert completed.modified is not None
    assert abs(completed.modified.timestamp() - newest.timestamp()) < 1
