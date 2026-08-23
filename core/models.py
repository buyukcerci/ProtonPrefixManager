"""Domain models and pure store logic."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, StrEnum
from pathlib import Path


class PrefixType(StrEnum):
    """Classification of a prefix: Steam game, non-Steam shortcut, or orphaned."""

    STEAM = "steam"
    NON_STEAM = "non_steam"
    ORPHANED = "orphaned"

    def label(self) -> str:
        labels = {
            PrefixType.STEAM: "Steam",
            PrefixType.NON_STEAM: "Non-Steam",
            PrefixType.ORPHANED: "Orphaned",
        }
        return labels[self]


class ScanStatus(StrEnum):
    """Progress of the asynchronous size scan for a single prefix."""

    NOT_SCANNED = "not_scanned"
    SCANNING = "scanning"
    SCANNED = "scanned"
    FAILED = "failed"


class SelectionState(Enum):
    """Tri-state for the table header checkbox."""

    NONE = 0
    SOME = 1
    ALL = 2


@dataclass(slots=True)
class Prefix:
    """A single Proton prefix found under a Steam library."""

    app_id: int
    name: str
    prefix_type: PrefixType
    path: Path
    library: str
    size_bytes: int = 0
    scan_status: ScanStatus = ScanStatus.NOT_SCANNED
    last_scanned: datetime | None = None
    modified: datetime | None = None

    @property
    def is_orphan(self) -> bool:
        """Orphan status is represented by the Orphaned classification."""
        return self.prefix_type is PrefixType.ORPHANED


def format_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string using 1024-based units."""
    if size_bytes <= 0:
        return "0 B"
    value = float(size_bytes)
    result = f"{int(value)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        if value < 1024:
            break
        value /= 1024
        result = f"{value:.1f} {unit}"
    return result


def prefix_key(prefix: Prefix) -> tuple[int, str]:
    """Stable identity for a prefix across refreshed copies."""
    return (prefix.app_id, str(prefix.path).rstrip("/"))


_dedupe_key = prefix_key


class Store:
    """Pure collection of prefixes with deduplication, filtering, sorting, and selection."""

    def __init__(self) -> None:
        self._prefixes: list[Prefix] = []
        self._selected: set[tuple[int, str]] = set()

    @property
    def prefixes(self) -> list[Prefix]:
        return list(self._prefixes)

    def upsert(self, prefix: Prefix) -> None:
        key = _dedupe_key(prefix)
        for index, existing in enumerate(self._prefixes):
            if _dedupe_key(existing) == key:
                self._prefixes[index] = prefix
                return
        self._prefixes.append(prefix)

    def merge(self, prefixes: Iterable[Prefix]) -> None:
        for prefix in prefixes:
            self.upsert(prefix)

    def filter(
        self,
        types: Iterable[PrefixType] | None = None,
        search_text: str = "",
        search_target: str = "name",
    ) -> list[Prefix]:
        allowed = set(types) if types is not None else set(PrefixType)
        text = search_text.strip().casefold()
        result: list[Prefix] = []
        for prefix in self._prefixes:
            if prefix.prefix_type not in allowed:
                continue
            if text:
                if search_target == "app_id":
                    if text not in str(prefix.app_id):
                        continue
                elif text not in prefix.name.casefold():
                    continue
            result.append(prefix)
        return result

    def sort(self, key: str = "size", descending: bool = True) -> None:
        if key == "size":
            self._prefixes.sort(key=lambda p: p.size_bytes, reverse=descending)
        elif key == "app_id":
            self._prefixes.sort(key=lambda p: p.app_id, reverse=descending)
        elif key == "name":
            self._prefixes.sort(key=lambda p: p.name.casefold(), reverse=descending)
        elif key == "path":
            self._prefixes.sort(key=lambda p: str(p.path), reverse=descending)
        elif key == "modified":
            self._sort_by_modified(descending)
        else:
            raise ValueError(f"unknown sort key: {key}")

    def _sort_by_modified(self, descending: bool) -> None:
        """Sort on raw timestamps; unknown values always sort last.

        Equal timestamps tie-break on size (larger first) then app_id so
        the order stays deterministic in both directions.
        """
        ranked: list[tuple[float, int, int, Prefix]] = []
        unknown: list[Prefix] = []
        for prefix in self._prefixes:
            modified = prefix.modified
            if modified is None:
                unknown.append(prefix)
            else:
                ranked.append((modified.timestamp(), prefix.size_bytes, prefix.app_id, prefix))
        if descending:
            ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        else:
            ranked.sort(key=lambda item: (item[0], -item[1], item[2]))
        self._prefixes[:] = [item[3] for item in ranked] + unknown

    def select(self, prefix: Prefix) -> None:
        self._selected.add(_dedupe_key(prefix))

    def deselect(self, prefix: Prefix) -> None:
        self._selected.discard(_dedupe_key(prefix))

    def select_visible(self, prefixes: Iterable[Prefix]) -> None:
        for prefix in prefixes:
            self._selected.add(_dedupe_key(prefix))

    def clear_selection(self) -> None:
        self._selected.clear()

    def is_selected(self, prefix: Prefix) -> bool:
        return _dedupe_key(prefix) in self._selected

    def selected(self) -> list[Prefix]:
        return [prefix for prefix in self._prefixes if self.is_selected(prefix)]

    def selection_state(self, visible: Iterable[Prefix] | None = None) -> SelectionState:
        rows = list(visible) if visible is not None else self._prefixes
        if not rows:
            return SelectionState.NONE
        selected_count = sum(1 for prefix in rows if self.is_selected(prefix))
        if selected_count == 0:
            return SelectionState.NONE
        if selected_count == len(rows):
            return SelectionState.ALL
        return SelectionState.SOME
