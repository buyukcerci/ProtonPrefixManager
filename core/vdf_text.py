"""Text-format Valve Data File parsing for libraryfolders.vdf."""

from __future__ import annotations

from pathlib import Path

import vdf

from core.models import parse_ascii_decimal
from core.vdf_read import MAX_VDF_BYTES, read_vdf_bounded

LIBRARY_FOLDERS_KEY = "libraryfolders"
PATH_KEY = "path"


class LibraryFoldersParseError(ValueError):
    """Raised when libraryfolders.vdf content cannot be parsed."""


def parse_library_folders_text(text: str) -> list[str]:
    """Extract library paths from current and legacy libraryfolders.vdf layouts.

    Both layouts share a top-level "libraryfolders" key: the current layout
    nests numbered blocks containing a "path" entry, while the legacy layout
    maps numeric keys directly to path strings. Layouts are distinguished by
    structure, unrelated keys are ignored, and file order is preserved.
    Legacy string values are accepted only when they are absolute POSIX
    paths, matching the Linux Steam installs this project targets; Windows
    or UNC paths from foreign configs are intentionally not recognized.
    """
    try:
        data = vdf.loads(text)
    except (TypeError, ValueError, SyntaxError, RecursionError) as exc:
        raise LibraryFoldersParseError(str(exc)) from exc
    if not isinstance(data, dict):
        raise LibraryFoldersParseError("top-level VDF value is not a mapping")
    if not data:
        raise LibraryFoldersParseError("no parsable content")
    folders = _find_folders_block(data)
    return _collect_paths(folders if folders is not None else data)


def load_library_folder_paths(path: Path) -> list[str]:
    """Read the file at path and extract its library paths."""
    text = read_vdf_bounded(path, MAX_VDF_BYTES)
    if text is None:
        raise LibraryFoldersParseError("unreadable, non-regular, or oversized file")
    return parse_library_folders_text(text)


def _find_folders_block(data: dict) -> dict | None:
    for key, value in data.items():
        if (
            isinstance(key, str)
            and key.casefold() == LIBRARY_FOLDERS_KEY
            and isinstance(value, dict)
        ):
            return value
    return None


def _collect_paths(block: dict) -> list[str]:
    paths: list[str] = []
    for key, value in block.items():
        if isinstance(value, dict):
            entry = value.get(PATH_KEY)
            if isinstance(entry, str) and entry.strip():
                paths.append(entry.strip())
        elif (
            parse_ascii_decimal(key) is not None
            and isinstance(value, str)
            and value.strip().startswith("/")
        ):
            paths.append(value.strip())
    return paths
