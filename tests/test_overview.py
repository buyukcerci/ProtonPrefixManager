"""Qt tests for the overview page: cards, treemap, legend, largest list."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QPoint, Qt

from core.models import Prefix, PrefixType, ScanStatus, format_size
from core.tools import Tool, ToolCategory
from ui.overview import OverviewPage, format_percent
from ui.sanitize import sanitize_tooltip


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


def _tool(name: str = "Proton 9.0", size: int = 0) -> Tool:
    return Tool(
        name=name,
        path=Path(f"/tools/{name}"),
        root=Path("/root"),
        read_only=False,
        size_bytes=size,
    )


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
    assert treemap.toolTip() == sanitize_tooltip("Game 5\n700 B\nSteam")
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


# --- tools caption --------------------------------------------------------------


def test_tools_caption_full_disk_mode(qtbot) -> None:
    page = _page(qtbot)
    page.update_data([], capacity_bytes=1_500, tools=[_tool(size=200)])
    caption = page.tools_caption_label
    assert caption.wordWrap() is True
    assert caption.text() == f"Areas sized against the full disk ({format_size(1_500)})"


def test_tools_caption_zoom_mode(qtbot) -> None:
    page = _page(qtbot)
    page.update_data([], capacity_bytes=1_000_000, tools=[_tool(size=200)])
    assert page.tools_caption_label.text().startswith("Zoomed: tools fill <1% of the disk")
    assert page.tools_caption_label.text().endswith(f"areas sized against {format_size(200)}")


def test_tools_caption_unknown_capacity(qtbot) -> None:
    page = _page(qtbot)
    page.update_data([], capacity_bytes=None, tools=[_tool(size=200)])
    assert page.tools_caption_label.text() == (
        f"Areas sized against total tool size ({format_size(200)}); disk size unknown"
    )


# --- tools treemap -------------------------------------------------------------


def test_tools_treemap_click_emits_tool_focus(qtbot) -> None:
    page = _page(qtbot)
    tool = _tool(size=700)
    page.update_data([], capacity_bytes=None, tools=[tool])
    treemap = page.tools_treemap
    treemap.resize(400, 300)
    captured: list[object] = []
    treemap.tool_focus_requested.connect(captured.append)
    assert treemap.has_content() is True
    _move_to(treemap, 200, 150)
    assert treemap.toolTip() == sanitize_tooltip(f"Proton 9.0\n{format_size(700)}")
    assert treemap.cursor().shape() == Qt.CursorShape.PointingHandCursor
    qtbot.mouseClick(treemap, Qt.MouseButton.LeftButton, pos=QPoint(200, 150))
    assert len(captured) == 1
    assert isinstance(captured[0], Tool)
    assert captured[0].name == "Proton 9.0"


def test_tools_treemap_click_reaches_page_signal(qtbot) -> None:
    page = _page(qtbot)
    tool = _tool(size=700)
    page.update_data([], capacity_bytes=None, tools=[tool])
    treemap = page.tools_treemap
    treemap.resize(400, 300)
    captured: list[object] = []
    page.tool_focus_requested.connect(captured.append)
    qtbot.mouseClick(treemap, Qt.MouseButton.LeftButton, pos=QPoint(200, 150))
    assert len(captured) == 1
    assert isinstance(captured[0], Tool)


def test_tools_treemap_remainder_is_not_clickable(qtbot) -> None:
    page = _page(qtbot)
    tool = _tool(size=200)
    page.update_data([], capacity_bytes=1_500, tools=[tool])
    treemap = page.tools_treemap
    treemap.resize(400, 300)
    captured: list[object] = []
    treemap.tool_focus_requested.connect(captured.append)
    _move_to(treemap, 200, 150)
    qtbot.mouseClick(treemap, Qt.MouseButton.LeftButton, pos=QPoint(200, 150))
    assert captured == []
    assert treemap.toolTip() == "Other disk usage"
    assert treemap._remainder_bytes == 1_300


def test_tools_treemap_remainder_zero_when_capacity_unknown_or_zoomed(qtbot) -> None:
    page = _page(qtbot)
    tool = _tool(size=200)
    page.update_data([], capacity_bytes=None, tools=[tool])
    assert page.tools_treemap._remainder_bytes == 0
    # 200 B against 1 MB stays in zoom mode, so areas size against the tool total
    page.update_data([], capacity_bytes=1_000_000, tools=[tool])
    assert page.tools_treemap._remainder_bytes == 0


def test_tools_treemap_zoom_decides_independently_from_prefixes(qtbot) -> None:
    page = _page(qtbot)
    tool = _tool(size=200)
    # 50 B of prefixes zoom under a 1500 B disk while 200 B of tools do not
    page.update_data([_prefix(1, size=50)], capacity_bytes=1_500, tools=[tool])
    assert page.treemap._remainder_bytes == 0
    assert page.tools_treemap._remainder_bytes == 1_300


def test_tools_treemap_tiny_tool_folds_into_remainder(qtbot) -> None:
    page = _page(qtbot)
    big = _tool("Proton 9.0", size=1_000)
    tiny = _tool("Proton Tiny", size=1)
    page.update_data([], capacity_bytes=2_000, tools=[big, tiny])
    treemap = page.tools_treemap
    treemap.resize(400, 60)
    treemap._compute_cells()
    keys = [key for key, _ in treemap._cells]
    assert 1 not in keys  # the tiny tool's slice folded under the minimum height
    assert -1 in keys  # the remainder absorbed it
    captured: list[object] = []
    treemap.tool_focus_requested.connect(captured.append)
    _move_to(treemap, 100, 30)
    qtbot.mouseClick(treemap, Qt.MouseButton.LeftButton, pos=QPoint(100, 30))
    assert captured == []
    assert treemap.toolTip() == "Other disk usage"


def test_tools_treemap_empty_state_without_sized_tools(qtbot) -> None:
    page = _page(qtbot)
    page.update_data([], capacity_bytes=None, tools=[_tool(size=0)])
    assert page.tools_treemap.has_content() is False
    assert page.tools_treemap._empty_text == "No sized tools yet"
    pixmap = page.tools_treemap.grab()
    assert not pixmap.isNull()


# --- tools classification visuals ----------------------------------------------


def test_tools_treemap_cells_use_category_colors(qtbot) -> None:
    from PySide6.QtGui import QPalette

    from ui.styles import tool_cell_fill_color

    page = _page(qtbot)
    used_tool = _tool("Proton 9.0", size=700)
    reclaimable_tool = _tool("GE-Proton 10.0", size=500)
    page.update_data(
        [],
        capacity_bytes=None,
        tools=[used_tool, reclaimable_tool],
        used={str(used_tool.path)},
    )
    treemap = page.tools_treemap
    palette = QPalette()
    assert treemap._item_fill(used_tool, palette) == tool_cell_fill_color(
        ToolCategory.USED, str(used_tool.path), palette
    )
    assert treemap._item_fill(reclaimable_tool, palette) == tool_cell_fill_color(
        ToolCategory.RECLAIMABLE, str(reclaimable_tool.path), palette
    )
    # category hues differ even when the lightness variation lines up
    assert (
        treemap._item_fill(used_tool, palette).hslHue()
        != treemap._item_fill(reclaimable_tool, palette).hslHue()
    )


def test_name_unverified_writable_tool_renders_unknown(qtbot) -> None:
    from PySide6.QtGui import QPalette

    from ui.styles import tool_cell_fill_color

    page = _page(qtbot)
    tool = Tool(
        name="Mystery Build",
        path=Path("/tools/mystery"),
        root=Path("/root"),
        read_only=False,
        size_bytes=500,
        name_unverified=True,
    )
    page.update_tools([tool], used=set(), pending=set(), failed=set(), capacity_bytes=None)
    palette = QPalette()
    treemap = page.tools_treemap
    assert treemap._item_fill(tool, palette) == tool_cell_fill_color(
        ToolCategory.UNKNOWN, str(tool.path), palette
    )
    rows = page.tools_top_list.rows()
    assert len(rows) == 1
    assert rows[0]._dot._category is ToolCategory.UNKNOWN


def test_tools_treemap_dimming_follows_highlight_category(qtbot) -> None:
    page = _page(qtbot)
    used_tool = _tool("Proton 9.0", size=700)
    reclaimable_tool = _tool("GE-Proton 10.0", size=500)
    page.update_data(
        [],
        capacity_bytes=None,
        tools=[used_tool, reclaimable_tool],
        used={str(used_tool.path)},
    )
    treemap = page.tools_treemap
    assert treemap._item_dimmed(used_tool) is False
    assert treemap._item_dimmed(reclaimable_tool) is False
    treemap.set_highlight_category(ToolCategory.USED)
    assert treemap._item_dimmed(used_tool) is False
    assert treemap._item_dimmed(reclaimable_tool) is True
    treemap.set_highlight_category(None)
    assert treemap._item_dimmed(reclaimable_tool) is False


def test_tools_legend_rows_labels_and_totals(qtbot) -> None:
    page = _page(qtbot)
    used_tool = _tool("Proton 9.0", size=200)
    reclaimable_tool = _tool("GE-Proton 10.0", size=300)
    page.update_data(
        [],
        capacity_bytes=1_500,
        tools=[used_tool, reclaimable_tool],
        used={str(used_tool.path)},
    )
    labels = [
        page.tools_legend.row_for(category)._category_label.text() for category in ToolCategory
    ]
    assert labels == ["Used", "Reclaimable", "Read-only", "Unknown"]
    assert page.tools_legend.row_for(ToolCategory.USED)._size_label.text() == format_size(200)
    assert page.tools_legend.row_for(ToolCategory.RECLAIMABLE)._size_label.text() == format_size(
        300
    )
    assert page.tools_legend.row_for(ToolCategory.READ_ONLY)._size_label.text() == format_size(0)
    # full-disk mode: shares are of the disk, matching the treemap remainder
    assert page.tools_legend.row_for(ToolCategory.USED)._percent_label.text() == "(13%)"
    assert page.tools_legend.row_for(ToolCategory.RECLAIMABLE)._percent_label.text() == "(20%)"
    # zoom mode: shares are of the tool total
    page.update_data(
        [],
        capacity_bytes=10_000_000,
        tools=[used_tool, reclaimable_tool],
        used={str(used_tool.path)},
    )
    assert page.tools_legend.row_for(ToolCategory.USED)._percent_label.text() == "(40%)"
    assert page.tools_legend.row_for(ToolCategory.RECLAIMABLE)._percent_label.text() == "(60%)"


def test_tools_legend_hover_drives_treemap_highlight(qtbot) -> None:
    page = _page(qtbot)
    page.update_data([], capacity_bytes=None, tools=[_tool(size=700)])
    page.tools_legend.row_for(ToolCategory.USED).hovered.emit(ToolCategory.USED)
    assert page.tools_treemap._highlight_category is ToolCategory.USED
    page.tools_legend.row_for(ToolCategory.USED).hovered.emit(None)
    assert page.tools_treemap._highlight_category is None


def test_tool_top_rows_have_category_dots(qtbot) -> None:
    from ui.overview import ClassificationSwatch

    page = _page(qtbot)
    used_tool = _tool("Proton 9.0", size=500)
    reclaimable_tool = _tool("GE-Proton 10.0", size=700)
    page.update_data(
        [],
        capacity_bytes=None,
        tools=[used_tool, reclaimable_tool],
        used={str(used_tool.path)},
    )
    rows = page.tools_top_list.rows()
    assert len(rows) == 2
    for row in rows:
        assert isinstance(row._dot, ClassificationSwatch)
        row_layout = row.layout()
        assert row_layout is not None
        assert row_layout.indexOf(row._dot) < row_layout.indexOf(row._name_label)
    # largest first, so the reclaimable tool ranks ahead of the used one
    assert rows[0]._dot._category is ToolCategory.RECLAIMABLE
    assert rows[1]._dot._category is ToolCategory.USED


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
    assert rows[0]._name_label.toolTip() == sanitize_tooltip("Game 2")
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
    assert label.toolTip() == sanitize_tooltip(long_name)


def test_unknown_names_fall_back_to_app_id(qtbot) -> None:
    page = _page(qtbot)
    page.update_data([_prefix(11, size=50, name="")], capacity_bytes=None)
    row = page.top_list.rows()[0]
    assert row._name_label.full_text() == "Unknown (AppID: 11)"
    assert row._name_label.toolTip() == sanitize_tooltip("Unknown (AppID: 11)")


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


def test_treemap_heights_capped_at_400_on_tall_windows(qtbot) -> None:
    from ui.styles import OVERVIEW_TREEMAP_MAX_HEIGHT_PX

    page = _page(qtbot)
    page.update_data([_prefix(1, size=500), _prefix(2, size=400)], None)
    # both treemaps share the free vertical space, so the window must be
    # tall enough for each of them to reach the shared cap
    page.resize(1920, 2600)
    page.show()
    qtbot.waitExposed(page)
    qtbot.wait(50)
    assert page.treemap.height() == OVERVIEW_TREEMAP_MAX_HEIGHT_PX
    assert page.tools_treemap.height() == OVERVIEW_TREEMAP_MAX_HEIGHT_PX


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


def test_treemap_label_color_is_uniform_across_classifications(qtbot) -> None:
    from PySide6.QtGui import QPalette

    from ui.overview import _treemap_label_color

    palette = QPalette()
    color = _treemap_label_color(palette)
    # one color for the whole treemap regardless of classification hue
    assert color in (Qt.GlobalColor.black, Qt.GlobalColor.white) or color.isValid()


def test_display_name_tags_runtime_components() -> None:
    from dataclasses import replace

    from ui.overview import display_name

    game = Prefix(
        app_id=4000,
        name="Alice",
        prefix_type=PrefixType.STEAM,
        path=Path("/games/steamapps/compatdata/4000"),
        library="/games",
    )
    runtime = replace(game, app_id=962960, name="Proton 9.0", is_runtime_component=True)
    assert display_name(game) == "Alice"
    assert display_name(runtime) == "Proton 9.0 (Steam component)"


def test_top_list_card_and_row_styling(qtbot) -> None:
    from PySide6.QtWidgets import QFrame

    from core.tools import Tool

    page = _page(qtbot)
    tool = Tool(name="Proton 9.0", path=Path("/tools/9.0"), root=Path("/root"), read_only=False)
    page.update_data([_prefix(1, size=100)], capacity_bytes=None)
    page.update_tools([tool], used=set(), pending=set(), failed=set(), capacity_bytes=None)

    assert isinstance(page.top_list, QFrame)
    assert page.top_list.objectName() == "topListCard"
    assert isinstance(page.tools_top_list, QFrame)
    assert page.tools_top_list.objectName() == "topListCard"

    prefix_rows = page.top_list.rows()
    assert len(prefix_rows) == 1
    assert isinstance(prefix_rows[0], QFrame)
    assert prefix_rows[0].objectName() == "topListRow"

    tool_rows = page.tools_top_list.rows()
    assert len(tool_rows) == 1
    assert isinstance(tool_rows[0], QFrame)
    assert tool_rows[0].objectName() == "topListRow"


def test_top_list_headers(qtbot) -> None:
    from PySide6.QtWidgets import QFrame, QLabel

    page = _page(qtbot)
    page.update_data([], capacity_bytes=None)

    prefix_header = page.top_list.findChild(QFrame, "topListHeader")
    assert prefix_header is not None
    prefix_header_labels = [label.text() for label in prefix_header.findChildren(QLabel)]
    assert prefix_header_labels == ["#", "Name", "Last Modified", "Size"]

    tool_header = page.tools_top_list.findChild(QFrame, "topListHeader")
    assert tool_header is not None
    tool_header_labels = [label.text() for label in tool_header.findChildren(QLabel)]
    assert tool_header_labels == ["#", "Name", "Size"]

    page.update_data([_prefix(1, size=100)], capacity_bytes=None)
    page.update_data([_prefix(2, size=200)], capacity_bytes=None)
    headers = [
        page.top_list._layout.itemAt(i).widget()
        for i in range(page.top_list._layout.count())
        if page.top_list._layout.itemAt(i).widget() is not None
        and page.top_list._layout.itemAt(i).widget().objectName() == "topListHeader"
    ]
    assert len(headers) == 1
    assert len(page.top_list.rows()) == 1


def test_top_list_columns_do_not_overlap(qtbot) -> None:
    from core.tools import Tool

    page = _page(qtbot)
    tool = Tool(
        name="Proton Experimental",
        path=Path("/tools/exp"),
        root=Path("/root"),
        read_only=False,
    )
    modified = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    page.update_data([_prefix(1, size=5000, modified=modified)], capacity_bytes=None)
    page.update_tools([tool], used=set(), pending=set(), failed=set(), capacity_bytes=None)
    page.resize(1200, 800)
    page.show()
    qtbot.waitExposed(page)

    prefix_row = page.top_list.rows()[0]
    assert prefix_row._rank_label.geometry().right() < prefix_row._name_label.geometry().left()
    assert prefix_row._name_label.geometry().right() < prefix_row._modified_label.geometry().left()
    assert prefix_row._modified_label.geometry().right() < prefix_row._size_label.geometry().left()

    tool_row = page.tools_top_list.rows()[0]
    assert tool_row._rank_label.geometry().right() < tool_row._name_label.geometry().left()
    assert tool_row._name_label.geometry().right() < tool_row._size_label.geometry().left()


def test_overview_section_order(qtbot) -> None:
    page = _page(qtbot)
    layout = page.widget().layout()
    assert layout is not None

    treemap_idx = -1
    separator_idx = -1
    prefix_list_idx = -1
    tools_treemap_idx = -1
    tools_list_idx = -1

    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item is None:
            continue
        widget = item.widget()
        if widget is page.treemap:
            treemap_idx = i
        elif (
            widget is not None
            and widget.objectName() == ""
            and widget.__class__.__name__ == "QFrame"
        ):
            separator_idx = i
        elif widget is page.top_list:
            prefix_list_idx = i
        elif widget is page.tools_treemap:
            tools_treemap_idx = i
        elif widget is page.tools_top_list:
            tools_list_idx = i

    assert 0 <= treemap_idx < separator_idx
    assert prefix_list_idx < separator_idx < tools_treemap_idx < tools_list_idx


def test_top_lists_fixed_widths(qtbot) -> None:
    from ui.styles import OVERVIEW_PREFIX_CARD_WIDTH_PX, OVERVIEW_TOOL_CARD_WIDTH_PX

    page = _page(qtbot)
    page.show()
    qtbot.waitExposed(page)

    assert page.top_list.width() == OVERVIEW_PREFIX_CARD_WIDTH_PX
    assert page.top_list.maximumWidth() == OVERVIEW_PREFIX_CARD_WIDTH_PX
    assert page.tools_top_list.width() == OVERVIEW_TOOL_CARD_WIDTH_PX
    assert page.tools_top_list.maximumWidth() == OVERVIEW_TOOL_CARD_WIDTH_PX
