"""Tests for the squarified treemap layout in core.treemap."""

from __future__ import annotations

import random
from collections.abc import Sequence

from core.treemap import CellRect, layout, layout_sized

AREA_TOLERANCE = 0.02  # areas proportional to values within 2 percent relative
EPSILON = 1e-6


def _area(cell: CellRect) -> float:
    return cell.w * cell.h


def test_single_value_fills_area() -> None:
    area = CellRect(x=0, y=0, w=100, h=100)
    cells = layout([("only", 100)], area)
    assert len(cells) == 1
    key, cell = cells[0]
    assert key == "only"
    assert cell.x == 0 and cell.y == 0
    assert cell.w == 100 and cell.h == 100


def test_two_values_split_proportionally_and_keep_keys() -> None:
    area = CellRect(x=0, y=0, w=100, h=100)
    cells = dict(layout([("small", 40), ("big", 60)], area))
    assert set(cells) == {"small", "big"}
    total = area.w * area.h
    assert abs(_area(cells["big"]) - total * 0.6) <= total * 0.6 * AREA_TOLERANCE
    assert abs(_area(cells["small"]) - total * 0.4) <= total * 0.4 * AREA_TOLERANCE


def test_areas_stay_proportional_for_many_values() -> None:
    area = CellRect(x=0, y=0, w=320, h=240)
    values = {f"item{index}": random.randint(10, 5000) for index in range(25)}
    cells = dict(layout(list(values.items()), area))
    total_value = sum(values.values())
    total_area = area.w * area.h
    for key, value in values.items():
        expected = total_area * value / total_value
        assert abs(_area(cells[key]) - expected) <= expected * AREA_TOLERANCE


def test_cells_tile_bounds_without_gaps_or_overlaps() -> None:
    area = CellRect(x=5, y=10, w=200, h=100)
    values = [(index, random.randint(10, 100)) for index in range(12)]
    cells = layout(values, area)
    assert len(cells) == 12
    covered = 0.0
    for _, cell in cells:
        assert cell.x >= area.x - EPSILON
        assert cell.y >= area.y - EPSILON
        assert cell.x + cell.w <= area.x + area.w + EPSILON
        assert cell.y + cell.h <= area.y + area.h + EPSILON
        covered += _area(cell)
    assert abs(covered - area.w * area.h) <= area.w * area.h * AREA_TOLERANCE
    for index, (_, first) in enumerate(cells):
        for _, second in cells[index + 1 :]:
            overlap_w = min(first.x + first.w, second.x + second.w) - max(first.x, second.x)
            overlap_h = min(first.y + first.h, second.y + second.h) - max(first.y, second.y)
            assert not (overlap_w > EPSILON and overlap_h > EPSILON), "cells overlap"


def test_zero_and_negative_values_skipped() -> None:
    area = CellRect(x=0, y=0, w=100, h=100)
    cells = dict(layout([("a", 50), ("zero", 0), ("neg", -10), ("b", 50)], area))
    assert set(cells) == {"a", "b"}
    assert abs(_area(cells["a"]) + _area(cells["b"]) - area.w * area.h) <= EPSILON


def test_degenerate_inputs_return_defined_results() -> None:
    area = CellRect(x=0, y=0, w=100, h=100)
    assert layout([], area) == []
    assert layout([("a", 0), ("b", 0)], area) == []
    assert layout([("a", 10)], CellRect(x=0, y=0, w=0, h=100)) == []
    assert layout([("a", 10)], CellRect(x=0, y=0, w=100, h=0)) == []


def test_large_item_count_lays_out_without_recursion() -> None:
    area = CellRect(x=0, y=0, w=800, h=600)
    items = [(index, (index * 7919) % 1000 + 1) for index in range(5000)]
    cells = layout(items, area)
    assert len(cells) == 5000
    covered = sum(cell.w * cell.h for _, cell in cells)
    assert abs(covered - area.w * area.h) <= area.w * area.h * AREA_TOLERANCE


def _reference_squarify(items: Sequence[float], area: CellRect) -> list[CellRect]:
    """Recursive formulation of the same algorithm, for output equivalence."""
    rects: list[CellRect] = []

    def worst(row: Sequence[float], bounds: CellRect) -> float:
        return _reference_worst(row, bounds)

    def place(row: Sequence[float], bounds: CellRect) -> CellRect:
        total = sum(row)
        if bounds.w >= bounds.h:
            height = total / bounds.w if bounds.w else 0
            x = bounds.x
            for size in row:
                width = size / height if height else 0
                rects.append(CellRect(x=x, y=bounds.y, w=width, h=height))
                x += width
            return CellRect(x=bounds.x, y=bounds.y + height, w=bounds.w, h=bounds.h - height)
        width = total / bounds.h if bounds.h else 0
        y = bounds.y
        for size in row:
            height = size / width if width else 0
            rects.append(CellRect(x=bounds.x, y=y, w=width, h=height))
            y += height
        return CellRect(x=bounds.x + width, y=bounds.y, w=bounds.w - width, h=bounds.h)

    def recurse(values: Sequence[float], row: list[float], bounds: CellRect) -> None:
        if not values:
            if row:
                place(row, bounds)
            return
        if not row:
            recurse(values[1:], [values[0]], bounds)
            return
        if worst(row + [values[0]], bounds) <= worst(row, bounds):
            recurse(values[1:], row + [values[0]], bounds)
        else:
            recurse(values, [], place(row, bounds))

    total = sum(items)
    scale = area.w * area.h / total if total else 0.0
    recurse([value * scale for value in items], [], area)
    return rects


def _reference_worst(row: Sequence[float], area: CellRect) -> float:
    if not row:
        return float("inf")
    total = sum(row)
    if area.w >= area.h:
        height = total / area.w if area.w else 0
        if height <= 0:
            return float("inf")
        worst = 0.0
        for size in row:
            width = size / height
            if width <= 0:
                return float("inf")
            worst = max(worst, max(width / height, height / width))
        return worst
    width = total / area.h if area.h else 0
    if width <= 0:
        return float("inf")
    worst = 0.0
    for size in row:
        height = size / width
        if height <= 0:
            return float("inf")
        worst = max(worst, max(width / height, height / width))
    return worst


def test_layout_matches_recursive_reference_geometry() -> None:
    random.seed(20260822)
    for count in range(1, 40):
        area = CellRect(x=3.0, y=7.0, w=float(100 + count * 13), h=float(80 + count * 7))
        values = [random.randint(1, 5000) for _ in range(count)]
        keyed = [(index, float(value)) for index, value in enumerate(values)]
        actual = [rect for _, rect in layout(keyed, area)]
        expected = _reference_squarify(sorted(values, reverse=True), area)
        assert len(actual) == len(expected)
        for mine, reference in zip(actual, expected, strict=True):
            for field in ("x", "y", "w", "h"):
                assert abs(getattr(mine, field) - getattr(reference, field)) < 1e-9


# --- min height folding -------------------------------------------------------


def test_sized_layout_cells_meet_min_height_when_possible() -> None:
    area = CellRect(x=0, y=0, w=600, h=80)
    items = [(index, float(value)) for index, value in enumerate([500, 400, 300, 2, 1])]
    result = layout_sized(items, area, 20.0, exempt_key=-1)
    assert result.cells
    for key, cell in result.cells:
        if key == -1:
            continue
        assert cell.h >= 20.0 - EPSILON
    covered = sum(cell.w * cell.h for _, cell in result.cells)
    assert abs(covered - area.w * area.h) <= area.w * area.h * AREA_TOLERANCE


def test_sized_layout_folds_small_cells_into_exempt_value() -> None:
    area = CellRect(x=0, y=0, w=400, h=100)
    items = [("big", 900.0), ("tiny1", 1.0), ("tiny2", 1.0)]
    result = layout_sized(items, area, 30.0, exempt_key="big")
    assert result.folded_keys == ["tiny1", "tiny2"]
    assert abs(result.folded_value - 2.0) <= EPSILON
    assert [key for key, _ in result.cells] == ["big"]
    big_cell = result.cells[0][1]
    assert abs(_area(big_cell) - area.w * area.h) <= EPSILON


def test_sized_layout_without_exempt_folds_everything() -> None:
    area = CellRect(x=0, y=0, w=100, h=10)
    result = layout_sized([("a", 5.0), ("b", 5.0)], area, 20.0)
    assert result.cells == []
    assert result.folded_keys == ["a", "b"]
    assert abs(result.folded_value - 10.0) <= EPSILON


def test_sized_layout_exempt_cell_is_never_folded() -> None:
    area = CellRect(x=0, y=0, w=100, h=40)
    result = layout_sized([("disk", 1.0)], area, 50.0, exempt_key="disk")
    assert [key for key, _ in result.cells] == ["disk"]
    assert result.folded_keys == []
    assert result.folded_value == 0.0


def test_sized_layout_is_deterministic() -> None:
    area = CellRect(x=2.0, y=3.0, w=320.0, h=200.0)
    items = [(index, float((index * 7919) % 500 + 1)) for index in range(30)]
    first = layout_sized(items, area, 18.0, exempt_key=-1)
    second = layout_sized(items, area, 18.0, exempt_key=-1)
    assert first == second


def test_sized_layout_min_height_taller_than_area_folds_all_but_exempt() -> None:
    area = CellRect(x=0, y=0, w=200, h=30)
    items = [("disk", 700.0), ("a", 200.0), ("b", 100.0)]
    result = layout_sized(items, area, 60.0, exempt_key="disk")
    assert [key for key, _ in result.cells] == ["disk"]
    assert set(result.folded_keys) == {"a", "b"}
    assert abs(result.folded_value - 300.0) <= EPSILON
    disk_cell = result.cells[0][1]
    assert abs(_area(disk_cell) - area.w * area.h) <= EPSILON


def test_sized_layout_degenerate_inputs_and_zero_min_height() -> None:
    area = CellRect(x=0, y=0, w=100, h=100)
    empty = layout_sized([], area, 10.0)
    assert empty.cells == []
    assert empty.folded_keys == []
    assert empty.folded_value == 0.0
    assert layout_sized([("a", 10.0)], CellRect(x=0, y=0, w=100, h=0), 10.0).cells == []
    plain = layout([("a", 1.0), ("b", 2.0)], area)
    assert layout_sized([("a", 1.0), ("b", 2.0)], area, 0.0).cells == plain
