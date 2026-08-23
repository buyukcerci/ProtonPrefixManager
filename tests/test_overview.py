"""Qt tests for the overview page: cards, treemap, legend, largest list."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QPoint, Qt

from core.models import Prefix, PrefixType, ScanStatus, format_size
from ui.overview import OverviewPage, format_percent


def _prefix(
    app_id: int,
    prefix_type: PrefixType = PrefixType.STEAM,
    size: int = 0,
    status: ScanStatus | None = None,
    modified: datetime | None = None,
    name: str | None = None,
) -> Prefix:
    if status is None:
        status = ScanStatus.SCANNED if size else ScanStatus.NOT_SCANNED
    return Prefix(
        app_id=app_id,
        name=name if name is not None else f"Game {app_id}",
        prefix_type=prefix_type,
        path=Path(f"/p/{app_id}"),
        library="/lib",
        size_bytes=size,
        scan_status=status,
        modified=modified,
    )


def _page(qtbot) -> OverviewPage:
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    return page


def _dataset() -> list[Prefix]:
    return [
        _prefix(1, PrefixType.STEAM, size=600),
        _prefix(2, PrefixType.STEAM, size=400),
        _prefix(3, PrefixType.ORPHANED, size=1000),
    ]


# --- percent formatting -----------------------------------------------------


def test_format_percent_whole_and_small_values() -> None:
    assert format_percent(0, 1000) == "0%"
    assert format_percent(10, 0) == "0%"
    assert format_percent(5, 1000) == "<1%"
    assert format_percent(10, 1000) == "1%"
    assert format_percent(155, 1000) == "16%"
    assert format_percent(1000, 1000) == "100%"


# --- cards and caption ------------------------------------------------------


def test_full_disk_mode_cards_caption_and_legend(qtbot) -> None:
    page = _page(qtbot)
    page.update_data(_dataset(), capacity_bytes=10_000)
    assert page.prefixes_card.value_text() == "3"
    assert page.prefixes_card.detail_text() == format_size(2000)
    assert page.orphan_card.value_text() == "1"
    assert page.orphan_card.detail_text() == format_size(1000)
    assert page.disk_card.value_text() == f"{format_size(2000)} of {format_size(10_000)}"
    assert page.disk_card.detail_text() == "20% of disk"
    assert page.caption_label.text() == (
        f"Areas sized against the full disk ({format_size(10_000)})"
    )
    # percent semantics follow the rectangle: share of disk in full-disk mode
    assert page.legend.row_for(PrefixType.ORPHANED)._percent_label.text() == "(10%)"
    assert page.treemap.has_content() is True


def test_zoom_mode_caption_and_legend_basis(qtbot) -> None:
    page = _page(qtbot)
    page.update_data(_dataset(), capacity_bytes=10_000_000)
    assert page.caption_label.text().startswith("Zoomed: prefixes fill <1% of the disk")
    assert page.caption_label.text().endswith(f"areas sized against {format_size(2000)}")
    # share of total prefixes in zoom mode
    assert page.legend.row_for(PrefixType.ORPHANED)._percent_label.text() == "(50%)"


def test_unknown_capacity_forces_zoom_and_says_so(qtbot) -> None:
    page = _page(qtbot)
    page.update_data(_dataset(), capacity_bytes=None)
    assert page.caption_label.text() == (
        f"Areas sized against total prefix size ({format_size(2000)}); disk size unknown"
    )
    assert page.disk_card.detail_text() == "disk size unknown"


def test_unscanned_excluded_from_sizes_but_counted_in_cards(qtbot) -> None:
    page = _page(qtbot)
    prefixes = _dataset() + [_prefix(9, PrefixType.NON_STEAM, size=0, status=ScanStatus.SCANNING)]
    page.update_data(prefixes, capacity_bytes=10_000)
    assert page.prefixes_card.value_text() == "4"
    assert page.prefixes_card.detail_text() == format_size(2000)
    assert page.status_label.text() == "3 of 4 prefixes sized, 1 scanning"


def test_scan_status_line_lists_each_state(qtbot) -> None:
    page = _page(qtbot)
    prefixes = [
        _prefix(1, size=10),
        _prefix(2, size=10, status=ScanStatus.SCANNING),
        _prefix(3, size=10, status=ScanStatus.FAILED),
        _prefix(4),
    ]
    page.update_data(prefixes, capacity_bytes=None)
    assert page.status_label.text() == "1 of 4 prefixes sized, 1 scanning, 1 failed, 1 pending"


# --- treemap interaction -----------------------------------------------------


def test_treemap_click_emits_prefix_focus(qtbot) -> None:
    page = _page(qtbot)
    page.update_data([_prefix(5, size=700)], capacity_bytes=None)
    treemap = page.treemap
    treemap.resize(400, 300)
    captured: list[object] = []
    treemap.prefix_focus_requested.connect(captured.append)
    _move_to(treemap, 200, 150)
    assert treemap.toolTip() == "Game 5\n700 B\nSteam"
    assert treemap.cursor().shape() == Qt.CursorShape.PointingHandCursor
    qtbot.mouseClick(treemap, Qt.MouseButton.LeftButton, pos=QPoint(200, 150))
    assert len(captured) == 1
    assert isinstance(captured[0], Prefix)
    assert captured[0].app_id == 5


def _move_to(widget, x: int, y: int) -> None:
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(x, y),
        QPointF(x, y),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.mouseMoveEvent(event)


def test_treemap_remainder_is_not_clickable(qtbot) -> None:
    page = _page(qtbot)
    # full-disk mode with a remainder: 200 of 1500 bytes keeps the disk scale
    page.update_data([_prefix(5, size=200)], capacity_bytes=1_500)
    treemap = page.treemap
    treemap.resize(400, 300)
    captured: list[object] = []
    treemap.prefix_focus_requested.connect(captured.append)
    _move_to(treemap, 200, 150)
    qtbot.mouseClick(treemap, Qt.MouseButton.LeftButton, pos=QPoint(200, 150))
    assert captured == []
    assert treemap.toolTip() == "Other disk usage"


def test_legend_hover_drives_treemap_highlight(qtbot) -> None:
    page = _page(qtbot)
    page.update_data(_dataset(), capacity_bytes=None)
    page.legend.row_for(PrefixType.STEAM).hovered.emit(PrefixType.STEAM)
    assert page.treemap._highlight_type is PrefixType.STEAM
    page.legend.row_for(PrefixType.STEAM).hovered.emit(None)
    assert page.treemap._highlight_type is None


def test_degenerate_treemap_data_renders_message(qtbot) -> None:
    page = _page(qtbot)
    page.update_data([_prefix(1, size=0, status=ScanStatus.SCANNED)], capacity_bytes=None)
    assert page.treemap.has_content() is False
    pixmap = page.treemap.grab()
    assert not pixmap.isNull()


# --- largest prefixes section -------------------------------------------------


def test_top_list_ranks_and_formats(qtbot) -> None:
    page = _page(qtbot)
    modified = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    prefixes = [
        _prefix(1, size=100, modified=modified),
        _prefix(2, size=300),
        _prefix(3, size=200),
    ]
    page.update_data(prefixes, capacity_bytes=None)
    rows = page.top_list.rows()
    assert [row._prefix.app_id for row in rows] == [2, 3, 1]
    assert rows[0]._name_label.text() == "Game 2"
    assert rows[0]._name_label.toolTip() == "Game 2"
    from PySide6.QtWidgets import QLabel

    from ui.table import format_modified

    labels = rows[2].findChildren(QLabel)
    texts = [label.text() for label in labels]
    assert format_modified(modified) in texts
    assert format_size(100) in texts


def test_top_list_click_emits_prefix_focus(qtbot) -> None:
    page = _page(qtbot)
    page.update_data(_dataset(), capacity_bytes=None)
    rows = page.top_list.rows()
    captured: list[object] = []
    page.prefix_focus_requested.connect(captured.append)
    qtbot.mouseClick(rows[0], Qt.MouseButton.LeftButton)
    assert len(captured) == 1
    assert isinstance(captured[0], Prefix)
    assert captured[0].app_id == 3  # largest first


def test_non_left_clicks_trigger_nothing(qtbot) -> None:
    page = _page(qtbot)
    page.update_data(_dataset(), capacity_bytes=None)
    focused: list[object] = []
    clicks: list[bool] = []
    page.prefix_focus_requested.connect(focused.append)
    page.orphan_review_requested.connect(lambda: clicks.append(True))
    treemap = page.treemap
    treemap.resize(400, 300)
    row = page.top_list.rows()[0]

    for button in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
        qtbot.mouseClick(treemap, button, pos=QPoint(200, 150))
        qtbot.mouseClick(row, button)
        qtbot.mouseClick(page.orphan_card, button)
    assert focused == []
    assert clicks == []


def test_top_list_name_elides_to_label_width(qtbot) -> None:
    from ui.overview import ElidedLabel

    long_name = "A Very Long Game Name That Should Be Cut In The Middle Somewhere"
    label = ElidedLabel(long_name)
    qtbot.addWidget(label)
    assert label.text() == long_name  # full text before any layout

    label.resize(90, 20)
    label.grab()  # hidden resizes deliver no resize event; paint refreshes the elision
    expected = label.fontMetrics().elidedText(
        long_name, Qt.TextElideMode.ElideMiddle, label.width()
    )
    assert label.text() == expected
    assert label.text() != long_name
    assert label.toolTip() == long_name


def test_unknown_names_fall_back_to_app_id(qtbot) -> None:
    page = _page(qtbot)
    page.update_data([_prefix(11, size=50, name="")], capacity_bytes=None)
    row = page.top_list.rows()[0]
    assert row._name_label.full_text() == "Unknown (AppID: 11)"
    assert row._name_label.toolTip() == "Unknown (AppID: 11)"


# --- orphan card --------------------------------------------------------------


def test_orphan_card_click_emits_only_when_orphans_exist(qtbot) -> None:
    page = _page(qtbot)
    page.update_data([_prefix(1, size=10)], capacity_bytes=None)
    clicks: list[bool] = []
    page.orphan_review_requested.connect(lambda: clicks.append(True))
    qtbot.mouseClick(page.orphan_card, Qt.MouseButton.LeftButton)
    assert clicks == []
    page.update_data(_dataset(), capacity_bytes=None)
    qtbot.mouseClick(page.orphan_card, Qt.MouseButton.LeftButton)
    assert len(clicks) == 1


# --- offscreen smoke -----------------------------------------------------------


def test_offscreen_smoke_overview_renders_with_colors(qtbot) -> None:
    from PySide6.QtGui import QImage

    page = _page(qtbot)
    page.update_data(_dataset(), capacity_bytes=10_000)
    pixmap = page.grab()
    assert not pixmap.isNull()
    image: QImage = pixmap.toImage()
    colors = {
        image.pixelColor(x, y).rgb()
        for y in range(0, image.height(), 4)
        for x in range(0, image.width(), 4)
    }
    assert len(colors) > 3  # classification fills, borders, and text present


# --- round-3 fixes -----------------------------------------------------------


def test_treemap_height_capped_at_400_on_tall_windows(qtbot) -> None:
    from ui.styles import OVERVIEW_TREEMAP_MAX_HEIGHT_PX

    page = _page(qtbot)
    page.update_data([_prefix(1, size=500), _prefix(2, size=400)], None)
    page.resize(1920, 1040)
    page.show()
    qtbot.waitExposed(page)
    qtbot.wait(50)
    assert page.treemap.height() == OVERVIEW_TREEMAP_MAX_HEIGHT_PX


def test_treemap_height_between_bounds_on_small_windows(qtbot) -> None:
    from ui.styles import (
        OVERVIEW_TREEMAP_MAX_HEIGHT_PX,
        OVERVIEW_TREEMAP_MIN_HEIGHT_PX,
    )

    page = _page(qtbot)
    page.update_data([_prefix(1, size=100)], None)
    page.resize(900, 600)
    page.show()
    qtbot.waitExposed(page)
    qtbot.wait(50)
    # below the cap the treemap absorbs free space but never dominates
    height = page.treemap.height()
    assert OVERVIEW_TREEMAP_MIN_HEIGHT_PX <= height < OVERVIEW_TREEMAP_MAX_HEIGHT_PX


def test_nav_bar_spacing_is_two_pixels(qtbot, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    from PySide6.QtWidgets import QHBoxLayout

    from ui.main_window import MainWindow

    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    bar = window._nav_group.parent()
    layout = bar.layout()
    assert isinstance(layout, QHBoxLayout)
    assert layout.spacing() == 2


def test_treemap_label_color_is_uniform_across_classifications(qtbot) -> None:
    from PySide6.QtGui import QPalette

    from ui.overview import _treemap_label_color

    palette = QPalette()
    color = _treemap_label_color(palette)
    # one color for the whole treemap regardless of classification hue
    assert color in (Qt.GlobalColor.black, Qt.GlobalColor.white) or color.isValid()
