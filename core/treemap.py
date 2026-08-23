"""Squarified treemap layout, pure and deterministic.

The layout maps keyed values to rectangles inside a bounding rect. Keys
are carried through unchanged so callers can map each rectangle back to
its own item; values are treated as relative areas.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

K = TypeVar("K")


@dataclass(slots=True)
class CellRect:
    x: float
    y: float
    w: float
    h: float


def layout(items: Sequence[tuple[K, float]], area: CellRect) -> list[tuple[K, CellRect]]:
    """Squarified treemap for the given keyed values inside area.

    Zero and negative values are skipped; remaining values are sorted
    descending internally, so the returned order follows size, not input
    order. Each rectangle lies inside area, rectangles do not overlap,
    and each area is proportional to its value.
    """
    entries = [(key, float(value)) for key, value in items if value > 0]
    if not entries or area.w <= 0 or area.h <= 0:
        return []
    entries.sort(key=lambda item: item[1], reverse=True)
    total = sum(value for _, value in entries)
    scale = area.w * area.h / total
    out: list[tuple[K, CellRect]] = []
    _squarify([(key, value * scale) for key, value in entries], area, out)
    return out


def _squarify(items: list[tuple[K, float]], area: CellRect, out: list[tuple[K, CellRect]]) -> None:
    """Fill area row by row, iteratively; one loop turn per placed item.

    The loop mirrors the classic recursive formulation exactly: grow the
    current row while the worst aspect ratio does not worsen, otherwise
    place the row, shrink the area, and start the next row. Iterative
    rather than recursive because libraries reach thousands of prefixes.
    """
    index = 0
    row: list[tuple[K, float]] = []
    while index < len(items):
        if not row:
            row.append(items[index])
            index += 1
            continue
        candidate = items[index]
        if _worst(row + [candidate], area) <= _worst(row, area):
            row.append(candidate)
            index += 1
        else:
            area = _layout_row(row, area, out)
            row = []
    if row:
        _layout_row(row, area, out)


def _worst(row: Sequence[tuple[K, float]], area: CellRect) -> float:
    """Worst (largest) aspect ratio the row would produce inside area."""
    if not row:
        return float("inf")
    total = sum(value for _, value in row)
    if area.w >= area.h:
        height = total / area.w if area.w else 0
        if height <= 0:
            return float("inf")
        worst = 0.0
        for _, size in row:
            width = size / height
            if width <= 0:
                return float("inf")
            worst = max(worst, max(width / height, height / width))
        return worst
    width = total / area.h if area.h else 0
    if width <= 0:
        return float("inf")
    worst = 0.0
    for _, size in row:
        height = size / width
        if height <= 0:
            return float("inf")
        worst = max(worst, max(width / height, height / width))
    return worst


def _layout_row(
    row: Sequence[tuple[K, float]],
    area: CellRect,
    out: list[tuple[K, CellRect]],
) -> CellRect:
    """Place one row along the shorter side of area and return the remainder."""
    total = sum(value for _, value in row)
    if area.w >= area.h:
        height = total / area.w if area.w else 0
        x = area.x
        for key, size in row:
            width = size / height if height else 0
            out.append((key, CellRect(x=x, y=area.y, w=width, h=height)))
            x += width
        return CellRect(x=area.x, y=area.y + height, w=area.w, h=area.h - height)
    width = total / area.h if area.h else 0
    y = area.y
    for key, size in row:
        height = size / width if width else 0
        out.append((key, CellRect(x=area.x, y=y, w=width, h=height)))
        y += height
    return CellRect(x=area.x + width, y=area.y, w=area.w - width, h=area.h)
