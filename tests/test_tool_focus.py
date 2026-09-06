"""Tool focus handoff, deferred scroll, and row highlight behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from core import tools as tools_module
from core.tools import Tool


def _window(qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from ui.main_window import MainWindow

    monkeypatch.setattr(tools_module, "SYSTEM_TOOLS_DIR", tmp_path / "system-tools")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    window = MainWindow(auto_start=False)
    qtbot.addWidget(window)
    return window


def test_clear_tools_search_and_focus_no_match(qtbot, monkeypatch, tmp_path) -> None:
    from core.tools import Tool

    window = _window(qtbot, monkeypatch, tmp_path)
    window._tools_search_box.setText("stale query")
    window._tools_search_text = "stale query"
    window._clear_tools_search()
    assert window._tools_search_text == ""
    assert window._tools_search_box.text() == ""
    ghost = Tool(name="Ghost", path=tmp_path / "ghost", root=tmp_path, read_only=False)
    # The tools table uses NoSelection, so focus must not depend on view
    # selection; focusing a missing tool stays a no-op.
    window._on_tool_focus_requested(ghost)
    assert window._tools_search_text == ""


def test_deferred_tool_focus_scroll_reaches_target_row(qtbot, monkeypatch, tmp_path) -> None:
    from ui.main_window import _TOOL_NAME_COLUMN

    window = _window(qtbot, monkeypatch, tmp_path)
    first = Tool(name="First", path=tmp_path / "first", root=tmp_path, read_only=False)
    second = Tool(name="Second", path=tmp_path / "second", root=tmp_path, read_only=False)
    window._all_tools = [first, second]
    window._tools_model.set_items([first, second], {}, {})
    scrolled: list[object] = []
    monkeypatch.setattr(
        window._tools_table, "scrollTo", lambda index, hint=None: scrolled.append(index)
    )
    window._on_tool_focus_requested(first)
    qtbot.wait(10)
    assert len(scrolled) == 1
    index = scrolled[0]
    assert index.row() == 0
    assert index.column() == _TOOL_NAME_COLUMN
    assert index.isValid()


def test_deferred_tool_focus_scroll_skips_reset_model(qtbot, monkeypatch, tmp_path) -> None:
    window = _window(qtbot, monkeypatch, tmp_path)
    first = Tool(name="First", path=tmp_path / "first", root=tmp_path, read_only=False)
    second = Tool(name="Second", path=tmp_path / "second", root=tmp_path, read_only=False)
    window._all_tools = [first, second]
    window._tools_model.set_items([first, second], {}, {})
    scrolled: list[object] = []
    monkeypatch.setattr(
        window._tools_table, "scrollTo", lambda index, hint=None: scrolled.append(index)
    )
    window._on_tool_focus_requested(first)
    # A reset between the focus call and the deferred scroll removes the
    # row, so the scroll must not land on whatever replaced it.
    window._tools_model.set_visible([second])
    qtbot.wait(10)
    assert scrolled == []


def test_highlight_switch_clears_previous_row(qtbot) -> None:
    from ui.main_window import _TOOL_NAME_COLUMN, ToolTableModel

    first = Tool(name="First", path=Path("/tools/first"), root=Path("/root"), read_only=False)
    second = Tool(name="Second", path=Path("/tools/second"), root=Path("/root"), read_only=False)
    model = ToolTableModel()
    model.set_items([first, second], {}, {})
    model.highlight_row(first)
    first_index = model.index(0, _TOOL_NAME_COLUMN)
    assert model.data(first_index, Qt.ItemDataRole.BackgroundRole) is not None

    emitted: list[tuple[int, int]] = []
    model.dataChanged.connect(lambda top, bottom: emitted.append((top.row(), bottom.row())))
    model.highlight_row(second)
    second_index = model.index(1, _TOOL_NAME_COLUMN)
    assert model.highlighted_path() == str(second.path)
    assert model.data(second_index, Qt.ItemDataRole.BackgroundRole) is not None
    # The previously highlighted row stops painting highlighted instead
    # of waiting for an unrelated repaint.
    assert model.data(first_index, Qt.ItemDataRole.BackgroundRole) is None
    # Both rows reported the state change, not only the new target.
    assert (0, 0) in emitted
    assert (1, 1) in emitted
