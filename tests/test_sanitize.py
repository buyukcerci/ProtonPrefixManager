"""Sanitizer and tooltip escaping tests across the prefix and tool views."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.models import Prefix, PrefixType
from core.tools import Tool
from ui.overview import ElidedLabel, SummaryCard
from ui.sanitize import sanitize_display, sanitize_tooltip
from ui.styles import SecondaryLabel


def test_sanitize_display_strips_controls_and_bidi() -> None:
    dirty = "a\x00b\x1bc\x7fd\u200be\u200ff\u202eg"
    assert sanitize_display(dirty) == "abcdefg"
    assert sanitize_display("a\nb\rc\td") == "a b c d"
    assert sanitize_display("abcdef", limit=2) == "ab"
    assert sanitize_display("abcdef", limit=3) == "abc"
    long_text = "a" * 200
    assert sanitize_display(long_text, limit=None) == long_text
    assert len(sanitize_display(long_text)) == 120


def test_prefix_table_tooltip_injection_escaped() -> None:
    from ui.table import PrefixTableModel

    evil = '<b>bold</b> "quoted" & amp'
    prefix = Prefix(
        app_id=1,
        name=evil,
        prefix_type=PrefixType.STEAM,
        path=Path("/tmp/x"),
        library="/lib",
    )
    model = PrefixTableModel(tool_provider=lambda p: evil)
    tip = model._tooltip(prefix, 0, 1)
    assert tip is not None
    assert "<b>" not in tip
    assert "&lt;b&gt;" in tip
    assert "&amp;" in tip
    path_tip = model._tooltip(prefix, 0, 3)
    assert path_tip is not None
    assert path_tip == sanitize_tooltip(str(prefix.path))


def test_sanitize_tooltip_shell_and_qt_entity_decoding(qtbot) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import Qt as QtGuiQt
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import QLabel

    from ui.sanitize import TOOLTIP_SHELL_END, TOOLTIP_SHELL_START

    tip = sanitize_tooltip("Rock & Roll <special>")
    assert tip.startswith(TOOLTIP_SHELL_START)
    assert tip.endswith(TOOLTIP_SHELL_END)
    # Entities appear only for characters with a special meaning.
    assert "&amp;" in tip
    assert "&lt;special&gt;" in tip
    assert "Rock & Roll" not in tip
    # The shell is a real tag, so Qt always decodes the entities.
    assert QtGuiQt.mightBeRichText(tip) is True
    document = QTextDocument()
    document.setHtml(tip)
    assert document.toPlainText() == "Rock & Roll <special>"
    label = QLabel()
    qtbot.addWidget(label)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setText(tip)
    assert label.text() == tip


def test_sanitize_tooltip_multiline_uses_shell_constants() -> None:
    from ui.sanitize import TOOLTIP_SHELL_END, TOOLTIP_SHELL_START

    assert sanitize_tooltip("a\nb") == TOOLTIP_SHELL_START + "a<br>b" + TOOLTIP_SHELL_END


def test_sanitize_tooltip_empty_input_returns_empty() -> None:
    assert sanitize_tooltip("") == ""
    assert sanitize_tooltip("\n \t ") == ""


def test_tool_name_tooltip_escapes_markup() -> None:
    from ui.main_window import ToolTableModel
    from ui.sanitize import TOOLTIP_SHELL_END

    evil = "<img src=x onerror=alert(1)>"
    tool = Tool(name="Build", path=Path("/tools/build"), root=Path("/root"), read_only=False)
    model = ToolTableModel()
    model.set_items([tool], {str(tool.path): [5]}, {5: evil})
    tip = model._name_tooltip(tool)
    assert "<img" not in tip
    assert "&lt;img src=x onerror=alert(1)&gt; (5)" in tip
    assert tip.endswith("/tools/build" + TOOLTIP_SHELL_END)


def test_secondary_label_elide_has_tooltip(qtbot) -> None:
    label = SecondaryLabel("some long value text here")
    qtbot.addWidget(label)
    label.setText("another value")
    assert label.toolTip() == sanitize_tooltip("another value")
    label.setText("")
    assert label.toolTip() == ""


def test_elided_label_tooltip_refresh_and_set_text(qtbot) -> None:
    label = ElidedLabel("first")
    qtbot.addWidget(label)
    label.setText("second <b>value</b>")
    assert label.full_text() == sanitize_display("second <b>value</b>", limit=None)
    assert label.toolTip() == sanitize_tooltip("second <b>value</b>")
    assert "&lt;" in label.toolTip()
    assert "<b>" not in label.toolTip()
    label.set_full_text("third")
    assert label.full_text() == sanitize_display("third", limit=None)
    assert label.toolTip() == sanitize_tooltip("third")


def test_summary_card_clear_focus(qtbot) -> None:
    card = SummaryCard(
        "TITLE", clickable=True, tooltip="Review items", disabled_tooltip="Nothing to review"
    )
    qtbot.addWidget(card)
    card.show()
    card.set_clickable_enabled(True)
    card.setFocus()
    card.set_clickable_enabled(False)
    assert card.focusPolicy().value == 0
    assert not card.hasFocus()


def test_treemap_tooltip_escapes_markup(qtbot) -> None:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from ui.overview import OverviewPage

    page = OverviewPage()
    qtbot.addWidget(page)
    tool = Tool(
        name="<img src=x>",
        path=Path("/tools/evil"),
        root=Path("/root"),
        read_only=False,
        size_bytes=700,
    )
    page.update_tools([tool], used=set(), pending=set(), failed=set(), capacity_bytes=None)
    treemap = page.tools_treemap
    treemap.resize(400, 300)
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(200, 150),
        QPointF(200, 150),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    treemap.mouseMoveEvent(event)
    tip = treemap.toolTip()
    assert "<img" not in tip
    assert "&lt;img" in tip


@pytest.mark.parametrize("text", ["plain", "with spaces", ""])
def test_sanitize_display_plain_text_unchanged(text: str) -> None:
    assert sanitize_display(text) == text
