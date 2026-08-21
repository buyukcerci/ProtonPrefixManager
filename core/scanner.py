"""Safe prefix size scanning with a path-keyed size cache.

The scanner walks prefix directories without ever following symlinks,
counts regular files only, and isolates failures: an unreadable nested
directory contributes zero rather than aborting, while an inaccessible
prefix root produces a failed result instead of an exception. Cache
helpers are pure dictionary operations; persistence stays the
responsibility of AppConfig.save_config. Scanning runs in plain Python and
never mutates its inputs, so callers can display the prefix list
immediately and upsert refreshed copies as events arrive.
"""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from core.models import Prefix, ScanStatus


class ScanEventKind(StrEnum):
    """Kind of a scan progress event.

    PROGRESS is reserved for future per-file reporting and is not emitted
    by the current implementation.
    """

    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class CacheEntry:
    """Cached scan outcome for one prefix path."""

    size_bytes: int
    last_scanned: datetime

    def to_dict(self) -> dict[str, object]:
        return {"size_bytes": self.size_bytes, "last_scanned": self.last_scanned.isoformat()}

    @classmethod
    def from_dict(cls, data: object) -> CacheEntry | None:
        """Parse a raw cache mapping, returning None on any malformed value."""
        if not isinstance(data, dict):
            return None
        size_bytes = data.get("size_bytes")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
            return None
        raw_timestamp = data.get("last_scanned")
        if not isinstance(raw_timestamp, str):
            return None
        try:
            last_scanned = datetime.fromisoformat(raw_timestamp)
        except ValueError:
            return None
        return cls(size_bytes=size_bytes, last_scanned=last_scanned)


@dataclass(slots=True)
class ScanEvent:
    """Progress event emitted while scanning a sequence of prefixes.

    STARTED carries the original Prefix. COMPLETED and FAILED carry the
    refreshed copy (SCANNED with size and timestamp, or FAILED) so callers
    can upsert it directly; size_bytes mirrors the completed total.
    """

    kind: ScanEventKind
    prefix: Prefix | Path
    size_bytes: int | None = None
    error: str | None = None


@dataclass(slots=True)
class ScanResult:
    """Synchronous outcome of scanning a single directory tree."""

    path: Path
    size_bytes: int = 0
    error: str | None = None


def cache_key(target: Prefix | Path) -> str:
    """Normalized cache key matching Store._dedupe_key path semantics."""
    path = target.path if isinstance(target, Prefix) else target
    return str(path.resolve(strict=False)).rstrip("/")


def load_cached(cache: dict[str, dict], prefix: Prefix) -> Prefix | None:
    """Return a scanned copy of prefix when a valid cache entry exists."""
    entry = CacheEntry.from_dict(cache.get(cache_key(prefix)))
    if entry is None:
        return None
    return replace(
        prefix,
        size_bytes=entry.size_bytes,
        scan_status=ScanStatus.SCANNED,
        last_scanned=entry.last_scanned,
    )


def save_cached(cache: dict[str, dict], prefix: Prefix) -> None:
    """Store prefix's scan outcome under its cache key, mutating cache in place.

    The caller remains responsible for persisting via AppConfig.save_config.
    """
    if prefix.last_scanned is None:
        raise ValueError("cannot cache a prefix without last_scanned timestamp")
    cache[cache_key(prefix)] = {
        "size_bytes": prefix.size_bytes,
        "last_scanned": prefix.last_scanned.isoformat(),
    }


def invalidate(cache: dict[str, dict], target: Prefix | Path) -> None:
    """Remove the cache entry for target if present."""
    cache.pop(cache_key(target), None)


def refresh_needed(cache: dict[str, dict], prefix: Prefix, force: bool = False) -> bool:
    """True when force is set or no valid cache entry exists for prefix."""
    if force:
        return True
    return CacheEntry.from_dict(cache.get(cache_key(prefix))) is None


def scan_prefix(path: Path) -> ScanResult:
    """Recursively total regular-file bytes under path without following symlinks.

    Symlinked files and directories are skipped entirely, so a link pointing
    outside the prefix can never inflate the total. Unreadable or vanishing
    entries are ignored; only a failure on the root itself yields an error
    result instead of raising.
    """
    try:
        root_entries = _listdir(path)
    except OSError as exc:
        return ScanResult(path=path, size_bytes=0, error=str(exc))
    total = 0
    stack: list[list[os.DirEntry[str]]] = [root_entries]
    while stack:
        for entry in stack.pop():
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            mode = info.st_mode
            if stat_module.S_ISLNK(mode):
                continue
            if stat_module.S_ISDIR(mode):
                child = Path(entry.path)
                try:
                    stack.append(_listdir(child))
                except OSError:
                    continue
            elif stat_module.S_ISREG(mode):
                total += info.st_size
    return ScanResult(path=path, size_bytes=total, error=None)


def scan_prefixes(
    prefixes: Sequence[Prefix],
    cache: dict[str, dict] | None = None,
    *,
    on_event: Callable[[ScanEvent], None] | None = None,
    force_refresh: bool = False,
) -> Iterable[ScanEvent]:
    """Lazily scan prefixes in order, emitting STARTED then COMPLETED or FAILED.

    Iteration drives the work; nothing scans until the first next() call, so
    callers may interleave rendering between events. When on_event is given,
    events are delivered through it instead of being yielded. Valid cache
    hits are reported as completed without walking the disk unless
    force_refresh is set. The input sequence is never mutated; attach the
    refreshed Prefix from each terminal event via Store.upsert.
    """
    for prefix in prefixes:
        for event in _scan_one(prefix, cache, force_refresh=force_refresh):
            if on_event is not None:
                on_event(event)
            else:
                yield event


def _scan_one(
    prefix: Prefix,
    cache: dict[str, dict] | None,
    *,
    force_refresh: bool,
) -> Iterator[ScanEvent]:
    yield ScanEvent(kind=ScanEventKind.STARTED, prefix=prefix)
    if cache is not None and not force_refresh:
        cached = load_cached(cache, prefix)
        if cached is not None:
            yield ScanEvent(
                kind=ScanEventKind.COMPLETED, prefix=cached, size_bytes=cached.size_bytes
            )
            return
    result = scan_prefix(prefix.path)
    if result.error is not None:
        yield ScanEvent(
            kind=ScanEventKind.FAILED,
            prefix=replace(prefix, scan_status=ScanStatus.FAILED),
            error=result.error,
        )
        return
    scanned = replace(
        prefix,
        size_bytes=result.size_bytes,
        scan_status=ScanStatus.SCANNED,
        last_scanned=datetime.now(UTC),
    )
    if cache is not None:
        save_cached(cache, scanned)
    yield ScanEvent(kind=ScanEventKind.COMPLETED, prefix=scanned, size_bytes=scanned.size_bytes)


def _listdir(path: Path) -> list[os.DirEntry[str]]:
    with os.scandir(path) as iterator:
        return list(iterator)
