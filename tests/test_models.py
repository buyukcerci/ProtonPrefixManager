"""Unit tests for core.models."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from core.models import (
    Prefix,
    PrefixType,
    ScanStatus,
    SelectionState,
    Store,
    format_size,
)

LIB = "/steam"


def _prefix(
    app_id: int,
    name: str = "Game",
    prefix_type: PrefixType = PrefixType.STEAM,
    size_bytes: int = 0,
) -> Prefix:
    return Prefix(
        app_id=app_id,
        name=name,
        prefix_type=prefix_type,
        path=Path(f"{LIB}/steamapps/compatdata/{app_id}"),
        library=LIB,
        size_bytes=size_bytes,
    )


def test_prefix_defaults() -> None:
    prefix = _prefix(app_id=730)
    assert prefix.size_bytes == 0
    assert prefix.scan_status is ScanStatus.NOT_SCANNED
    assert prefix.last_scanned is None
    assert prefix.is_orphan is False


def test_prefix_orphan_derived() -> None:
    orphan = _prefix(app_id=999, prefix_type=PrefixType.ORPHANED)
    assert orphan.is_orphan is True
    assert orphan.prefix_type is PrefixType.ORPHANED


def test_prefix_type_labels() -> None:
    assert PrefixType.STEAM.label() == "Steam"
    assert PrefixType.NON_STEAM.label() == "Non-Steam"
    assert PrefixType.ORPHANED.label() == "Orphaned"


def test_prefix_scan_fields() -> None:
    scanned = datetime(2026, 1, 1, 12, 0, 0)
    prefix = Prefix(
        app_id=1,
        name="Game",
        prefix_type=PrefixType.STEAM,
        path=Path("/p"),
        library=LIB,
        size_bytes=512,
        scan_status=ScanStatus.SCANNED,
        last_scanned=scanned,
    )
    assert prefix.scan_status is ScanStatus.SCANNED
    assert prefix.last_scanned == scanned


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (-5, "0 B"),
        (500, "500 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024**2, "5.0 MB"),
        (1024**3, "1.0 GB"),
    ],
)
def test_format_size(size: int, expected: str) -> None:
    assert format_size(size) == expected


def test_store_sort_by_size_default_descending() -> None:
    store = Store()
    store.merge(
        [
            _prefix(app_id=1, size_bytes=100),
            _prefix(app_id=2, size_bytes=1000),
            _prefix(app_id=3, size_bytes=10),
        ]
    )
    store.sort()
    assert [p.app_id for p in store.prefixes] == [2, 1, 3]


def test_store_sort_by_size_ascending() -> None:
    store = Store()
    store.merge([_prefix(app_id=1, size_bytes=100), _prefix(app_id=2, size_bytes=1000)])
    store.sort("size", descending=False)
    assert [p.app_id for p in store.prefixes] == [1, 2]


def test_store_sort_by_name_case_insensitive() -> None:
    store = Store()
    store.merge(
        [
            _prefix(app_id=1, name="bravo"),
            _prefix(app_id=2, name="Alpha"),
            _prefix(app_id=3, name="charlie"),
        ]
    )
    store.sort("name", descending=False)
    assert [p.name for p in store.prefixes] == ["Alpha", "bravo", "charlie"]


def test_store_sort_by_app_id_numeric() -> None:
    store = Store()
    store.merge([_prefix(app_id=10), _prefix(app_id=2), _prefix(app_id=9)])
    store.sort("app_id", descending=False)
    assert [p.app_id for p in store.prefixes] == [2, 9, 10]


def test_store_sort_unknown_key_raises() -> None:
    store = Store()
    with pytest.raises(ValueError):
        store.sort("bogus")


def test_store_filter_by_type_strict_subset() -> None:
    store = Store()
    store.merge(
        [
            _prefix(app_id=1, prefix_type=PrefixType.STEAM),
            _prefix(app_id=2, prefix_type=PrefixType.NON_STEAM),
            _prefix(app_id=3, prefix_type=PrefixType.ORPHANED),
        ]
    )
    assert [p.app_id for p in store.filter(types=[PrefixType.STEAM])] == [1]
    assert [p.app_id for p in store.filter(types=[PrefixType.NON_STEAM])] == [2]
    assert [p.app_id for p in store.filter(types=[PrefixType.ORPHANED])] == [3]
    result = sorted(p.app_id for p in store.filter(types=[PrefixType.STEAM, PrefixType.ORPHANED]))
    assert result == [1, 3]


def test_store_filter_empty_type_set_yields_no_rows() -> None:
    store = Store()
    store.merge(
        [
            _prefix(app_id=1, prefix_type=PrefixType.STEAM),
            _prefix(app_id=2, prefix_type=PrefixType.NON_STEAM),
            _prefix(app_id=3, prefix_type=PrefixType.ORPHANED),
        ]
    )
    assert store.filter(types=set()) == []


def test_store_filter_search_narrows_orphans_when_included() -> None:
    store = Store()
    store.merge(
        [
            _prefix(app_id=111, name="Ghost One", prefix_type=PrefixType.ORPHANED),
            _prefix(app_id=222, name="Ghost Two", prefix_type=PrefixType.ORPHANED),
        ]
    )
    by_name = store.filter(types=[PrefixType.ORPHANED], search_text="ghost two")
    by_app_id = store.filter(search_text="111", search_target="app_id")
    assert [p.app_id for p in by_name] == [222]
    assert [p.app_id for p in by_app_id] == [111]

    excluded = store.filter(types=[PrefixType.STEAM], search_text="ghost")
    assert excluded == []


def test_store_filter_all_types_when_none() -> None:
    store = Store()
    store.merge([_prefix(app_id=1), _prefix(app_id=2, prefix_type=PrefixType.ORPHANED)])
    assert len(store.filter()) == 2


def test_store_filter_by_name_substring_case_insensitive() -> None:
    store = Store()
    store.merge([_prefix(app_id=1, name="Counter-Strike"), _prefix(app_id=2, name="Portal")])
    result = store.filter(search_text="COUNTER")
    assert [p.app_id for p in result] == [1]


def test_store_filter_by_app_id_substring() -> None:
    store = Store()
    store.merge([_prefix(app_id=10), _prefix(app_id=110), _prefix(app_id=7)])
    result = store.filter(search_text="10", search_target="app_id")
    assert [p.app_id for p in result] == [10, 110]


def test_store_filter_combines_type_and_search() -> None:
    store = Store()
    store.merge(
        [
            _prefix(app_id=1, name="Alpha", prefix_type=PrefixType.STEAM),
            _prefix(app_id=2, name="Alpha", prefix_type=PrefixType.NON_STEAM),
            _prefix(app_id=3, name="Beta", prefix_type=PrefixType.STEAM),
        ]
    )
    result = store.filter(types=[PrefixType.STEAM], search_text="alpha")
    assert [p.app_id for p in result] == [1]


def test_store_upsert_replaces_same_key() -> None:
    store = Store()
    store.upsert(_prefix(app_id=1, size_bytes=0))
    store.upsert(_prefix(app_id=1, size_bytes=100))
    assert len(store.prefixes) == 1
    assert store.prefixes[0].size_bytes == 100


def test_store_upsert_same_app_id_different_path() -> None:
    store = Store()
    store.upsert(_prefix(app_id=1))
    store.upsert(_prefix(app_id=1))
    assert len(store.prefixes) == 1


def test_store_merge_deduplicates() -> None:
    store = Store()
    store.merge([_prefix(app_id=1), _prefix(app_id=2)])
    store.merge([_prefix(app_id=2), _prefix(app_id=3)])
    assert [p.app_id for p in store.prefixes] == [1, 2, 3]


def test_store_selection_state_none() -> None:
    store = Store()
    store.merge([_prefix(app_id=1), _prefix(app_id=2)])
    assert store.selection_state() is SelectionState.NONE


def test_store_selection_state_some() -> None:
    store = Store()
    store.merge([_prefix(app_id=1), _prefix(app_id=2)])
    store.select(store.prefixes[0])
    assert store.selection_state() is SelectionState.SOME


def test_store_selection_state_all() -> None:
    store = Store()
    store.merge([_prefix(app_id=1), _prefix(app_id=2)])
    store.select_visible(store.prefixes)
    assert store.selection_state() is SelectionState.ALL


def test_store_selection_state_visible_subset() -> None:
    store = Store()
    store.merge([_prefix(app_id=1), _prefix(app_id=2), _prefix(app_id=3)])
    visible = store.prefixes[:2]
    store.select_visible(visible)
    assert store.selection_state(visible) is SelectionState.ALL
    assert store.selection_state() is SelectionState.SOME


def test_store_selection_state_empty_store() -> None:
    store = Store()
    assert store.selection_state() is SelectionState.NONE


def test_store_selection_clear() -> None:
    store = Store()
    store.merge([_prefix(app_id=1), _prefix(app_id=2)])
    store.select_visible(store.prefixes)
    store.clear_selection()
    assert store.selection_state() is SelectionState.NONE


def test_store_selected_and_deselect() -> None:
    store = Store()
    store.merge([_prefix(app_id=1), _prefix(app_id=2)])
    first = store.prefixes[0]
    store.select(first)
    assert [p.app_id for p in store.selected()] == [1]
    store.deselect(first)
    assert store.selected() == []
