"""Sanitize untrusted display strings for safe UI rendering.

Control characters from game or tool names must not change dialog
layout or accessibility output. Visible text uses the shortened form
while tooltips keep the full escaped value.
"""

from __future__ import annotations

import html
import unicodedata

MAX_DISPLAY_CHARS = 120


def sanitize_display(value: str, limit: int | None = MAX_DISPLAY_CHARS) -> str:
    """Collapse whitespace and truncate to a short single line."""
    text = value.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = "".join(char for char in text if unicodedata.category(char) not in ("Cc", "Cf"))
    collapsed = " ".join(text.split())
    if limit is not None and len(collapsed) > limit:
        if limit <= 3:
            return collapsed[:limit]
        return collapsed[: limit - 3] + "..."
    return collapsed


TOOLTIP_SHELL_START = "<p style='white-space:pre'>"
TOOLTIP_SHELL_END = "</p>"


def sanitize_tooltip(value: str) -> str:
    """Sanitize a string and escape markup for safe tooltip rendering.

    The return value is a minimal rich-text document: escaped lines
    joined with <br> inside a white-space:pre paragraph shell. Wrapping
    the content in a real tag forces Qt to decode entities, so a benign
    name such as "Rock & Roll" renders with its ampersand instead of the
    literal "&amp;" sequence, and unescaped markup in untrusted names can
    never become live tags because every line is escaped first. Lines are
    sanitized singly so multiline tooltips keep one row per line. Quotes
    are left alone because tooltip values never appear inside attributes.
    Empty input returns the empty string so callers never raise an empty
    tooltip box.
    """
    if not sanitize_display(value, limit=None):
        return ""
    escaped = "<br>".join(
        html.escape(sanitize_display(line, limit=None), quote=False) for line in value.split("\n")
    )
    return TOOLTIP_SHELL_START + escaped + TOOLTIP_SHELL_END
