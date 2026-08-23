"""Pure aggregation helpers backing the overview page.

Everything here works on plain Prefix records and paths with no Qt and
no mutation of its inputs, so the overview can be recomputed cheaply on
every scan event.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from core.models import Prefix, PrefixType, ScanStatus


@dataclass(slots=True)
class ClassificationTotals:
    """Count and total size of one classification."""

    prefix_type: PrefixType
    count: int
    size_bytes: int


@dataclass(slots=True)
class DiskCapacity:
    """Total capacity of the filesystems involved, None when unmeasurable."""

    total_bytes: int | None


def classification_totals(prefixes: Sequence[Prefix]) -> list[ClassificationTotals]:
    """Per-classification counts and sizes, always including all three types."""
    buckets: dict[PrefixType, ClassificationTotals] = {
        prefix_type: ClassificationTotals(prefix_type, 0, 0) for prefix_type in PrefixType
    }
    for prefix in prefixes:
        entry = buckets[prefix.prefix_type]
        entry.count += 1
        entry.size_bytes += prefix.size_bytes
    return [buckets[PrefixType.STEAM], buckets[PrefixType.NON_STEAM], buckets[PrefixType.ORPHANED]]


def total_size(prefixes: Sequence[Prefix]) -> int:
    return sum(prefix.size_bytes for prefix in prefixes)


def total_count(prefixes: Sequence[Prefix]) -> int:
    return len(prefixes)


def has_known_size(prefix: Prefix) -> bool:
    """True once a scan or a cache entry provided a usable size.

    A failed scan still counts when it retained a previously cached size;
    pending and in-flight scans do not.
    """
    if prefix.scan_status is ScanStatus.SCANNED:
        return True
    return prefix.scan_status is ScanStatus.FAILED and prefix.size_bytes > 0


def largest_prefix(prefixes: Sequence[Prefix]) -> Prefix | None:
    if not prefixes:
        return None
    return max(prefixes, key=lambda prefix: prefix.size_bytes)


def top_largest(prefixes: Sequence[Prefix], count: int = 5) -> list[Prefix]:
    """Largest prefixes by size, ties broken by app_id, at most count entries."""
    ranked = sorted(prefixes, key=lambda prefix: (-prefix.size_bytes, prefix.app_id))
    return ranked[:count]


def cumulative_share(prefixes: Sequence[Prefix], count: int = 5) -> list[float]:
    """Running share of the known total for the largest prefixes.

    Returns an empty list when nothing is sized; each value is the sum of
    the sizes of the first n largest entries divided by the total.
    """
    total = total_size(prefixes)
    if total <= 0:
        return []
    running = 0
    shares: list[float] = []
    for prefix in top_largest(prefixes, count):
        running += prefix.size_bytes
        shares.append(running / total)
    return shares


def storage_capacity(paths: Sequence[Path]) -> DiskCapacity:
    """Total size of the distinct filesystems backing the given paths.

    Paths on the same device (st_dev) count once. Unmeasurable paths are
    skipped; when nothing could be measured the result says so via None.
    """
    seen_devices: set[int] = set()
    total = 0
    measured = False
    for raw in paths:
        path = Path(raw)
        try:
            device = path.stat().st_dev
            stats = os.statvfs(path)
        except OSError:
            continue
        if device in seen_devices:
            continue
        seen_devices.add(device)
        capacity = stats.f_blocks * stats.f_frsize
        if capacity <= 0:
            continue
        measured = True
        total += capacity
    return DiskCapacity(total_bytes=total if measured else None)


def use_zoom_mode(total_prefix_bytes: int, capacity_bytes: int | None) -> bool:
    """Strict rule: zoom if prefixes fill strictly under 5 percent of the disk.

    An unknown or non-positive capacity forces zoom mode so cells never
    collapse against an unusable reference total.
    """
    if capacity_bytes is None or capacity_bytes <= 0:
        return True
    return total_prefix_bytes * 20 < capacity_bytes
