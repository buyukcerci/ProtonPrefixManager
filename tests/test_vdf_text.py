"""Unit tests for core.vdf_text."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.vdf_text import (
    LibraryFoldersParseError,
    load_library_folder_paths,
    parse_library_folders_text,
)

NEW_FORMAT = """
"libraryfolders"
{
    "0"
    {
        "path" "/home/user/.local/share/Steam"
        "label" ""
        "contentid" "1234567890"
    }
    "1"
    {
        "path" "/mnt/games/SteamLibrary"
        "label" "Games"
        "contentid" "9876543210"
    }
}
"""

LEGACY_FORMAT = """
"LibraryFolders"
{
    "TimeNextStatsReport" "1700000000"
    "ContentStatsID" "1234567890123456789"
    "1" "/home/user/.local/share/Steam"
    "2" "/mnt/games/SteamLibrary"
}
"""


def test_new_format_extracts_paths_in_order() -> None:
    assert parse_library_folders_text(NEW_FORMAT) == [
        "/home/user/.local/share/Steam",
        "/mnt/games/SteamLibrary",
    ]


def test_legacy_format_extracts_paths_in_order() -> None:
    assert parse_library_folders_text(LEGACY_FORMAT) == [
        "/home/user/.local/share/Steam",
        "/mnt/games/SteamLibrary",
    ]


def test_folders_block_key_is_case_insensitive() -> None:
    text = '"LibraryFolders"\n{\n    "0"\n    {\n        "path" "/games"\n    }\n}\n'
    assert parse_library_folders_text(text) == ["/games"]


def test_new_format_ignores_non_dict_and_pathless_blocks() -> None:
    text = """
    "libraryfolders"
    {
        "0"
        {
            "label" "No Path Here"
        }
        "1" "not-a-block"
        "2"
        {
            "path" "/games"
        }
    }
    """
    assert parse_library_folders_text(text) == ["/games"]


def test_legacy_format_ignores_non_numeric_and_relative_values() -> None:
    text = '"LibraryFolders"\n{\n    "note" "/etc/passwd"\n    "1" "relative/path"\n}\n'
    assert parse_library_folders_text(text) == []


def test_empty_text_raises() -> None:
    with pytest.raises(LibraryFoldersParseError):
        parse_library_folders_text("")


def test_truncated_text_raises() -> None:
    truncated = '"libraryfolders"\n{\n    "0"\n    {\n        "path" '
    with pytest.raises(LibraryFoldersParseError):
        parse_library_folders_text(truncated)


def test_unicode_paths_are_preserved() -> None:
    text = '"libraryfolders"\n{\n    "0"\n    {\n        "path" "/spiele/Bücherroote"\n    }\n}\n'
    assert parse_library_folders_text(text) == ["/spiele/Bücherroote"]


def test_load_reads_file_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "libraryfolders.vdf"
    path.write_text(NEW_FORMAT, encoding="utf-8")
    assert load_library_folder_paths(path) == [
        "/home/user/.local/share/Steam",
        "/mnt/games/SteamLibrary",
    ]


def test_load_strips_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "libraryfolders.vdf"
    path.write_text(NEW_FORMAT, encoding="utf-8-sig")
    assert load_library_folder_paths(path) == [
        "/home/user/.local/share/Steam",
        "/mnt/games/SteamLibrary",
    ]


def test_load_propagates_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_library_folder_paths(tmp_path / "missing.vdf")
