"""Tests for the pure aggregation helpers in core.analytics."""

from __future__ import annotations

from pathlib import Path

from core.analytics import (
    classification_totals,
    cumulative_share,
    has_known_size,
    largest_prefix,
    storage_capacity,
    top_largest,
    total_count,
    total_size,
    use_zoom_mode,
)
from core.models import Prefix, PrefixType, ScanStatus


def _prefix(
    app_id: int,
    size: int = 0,
    prefix_type: PrefixType = PrefixType.STEAM,
    status: ScanStatus = ScanStatus.SCANNED,
) -> Prefix:
    return Prefix(
        app_id=app_id,
        name=f"Game {app_id}",
        prefix_type=prefix_type,
        path=Path(f"/p/{app_id}"),
        library="/lib",
        size_bytes=size,
        scan_status=status,
    )


def test_totals_and_counts_including_zero_sizes() -> None:
    prefixes = [
        _prefix(1, size=100, prefix_type=PrefixType.STEAM),
        _prefix(2, size=0, prefix_type=PrefixType.STEAM),
        _prefix(3, size=50, prefix_type=PrefixType.ORPHANED),
    ]
    assert total_size(prefixes) == 150
    assert total_count(prefixes) == 3
    totals = {entry.prefix_type: entry for entry in classification_totals(prefixes)}
    assert totals[PrefixType.STEAM].count == 2
    assert totals[PrefixType.STEAM].size_bytes == 100
    assert totals[PrefixType.NON_STEAM].count == 0
    assert totals[PrefixType.NON_STEAM].size_bytes == 0
    assert totals[PrefixType.ORPHANED].count == 1
    assert totals[PrefixType.ORPHANED].size_bytes == 50


def test_per_classification_zeros_present_on_empty_input() -> None:
    totals = classification_totals([])
    assert len(totals) == 3
    types = {entry.prefix_type for entry in totals}
    assert types == {PrefixType.STEAM, PrefixType.NON_STEAM, PrefixType.ORPHANED}
    assert all(entry.count == 0 and entry.size_bytes == 0 for entry in totals)


def test_largest_prefix_selection_and_none_on_empty() -> None:
    assert largest_prefix([]) is None
    prefixes = [_prefix(1, size=10), _prefix(2, size=30), _prefix(3, size=20)]
    largest = largest_prefix(prefixes)
    assert largest is not None and largest.app_id == 2


def test_top_largest_orders_by_size_then_app_id() -> None:
    prefixes = [
        _prefix(5, size=10),
        _prefix(2, size=30),
        _prefix(9, size=30),
        _prefix(1, size=20),
        _prefix(4, size=1),
        _prefix(6, size=2),
        _prefix(7, size=3),
    ]
    top = top_largest(prefixes, 5)
    assert [prefix.app_id for prefix in top] == [2, 9, 1, 5, 7]
    assert top_largest([], 5) == []
    assert len(top_largest(prefixes, 100)) == len(prefixes)


def test_cumulative_share_runs_over_largest_entries() -> None:
    prefixes = [
        _prefix(1, size=50),
        _prefix(2, size=30),
        _prefix(3, size=20),
    ]
    assert cumulative_share(prefixes, 2) == [0.5, 0.8]
    assert cumulative_share(prefixes, 5) == [0.5, 0.8, 1.0]
    assert cumulative_share([], 5) == []
    assert cumulative_share([_prefix(1, size=0)], 5) == []


def test_has_known_size_matrix() -> None:
    assert has_known_size(_prefix(1, size=10, status=ScanStatus.SCANNED)) is True
    assert has_known_size(_prefix(1, size=0, status=ScanStatus.SCANNED)) is True
    assert has_known_size(_prefix(1, size=10, status=ScanStatus.SCANNING)) is False
    assert has_known_size(_prefix(1, size=10, status=ScanStatus.NOT_SCANNED)) is False
    assert has_known_size(_prefix(1, size=10, status=ScanStatus.FAILED)) is True
    assert has_known_size(_prefix(1, size=0, status=ScanStatus.FAILED)) is False


def test_storage_capacity_counts_each_filesystem_once(tmp_path: Path) -> None:
    import shutil

    capacity = storage_capacity([tmp_path, tmp_path, tmp_path / "nested" / "missing"])
    assert capacity.total_bytes == shutil.disk_usage(tmp_path).total


def test_storage_capacity_reports_unknown_when_nothing_measurable(tmp_path: Path) -> None:
    assert storage_capacity([]).total_bytes is None
    assert storage_capacity([tmp_path / "nope"]).total_bytes is None


def test_use_zoom_mode_strict_threshold() -> None:
    capacity = 1000
    assert use_zoom_mode(49, capacity) is True  # strictly below 5 percent
    assert use_zoom_mode(50, capacity) is False  # exactly 5 percent stays full disk
    assert use_zoom_mode(51, capacity) is False
    assert use_zoom_mode(0, capacity) is True
    assert use_zoom_mode(50, None) is True  # unknown capacity forces zoom
    assert use_zoom_mode(50, 0) is True  # non-positive capacity forces zoom
