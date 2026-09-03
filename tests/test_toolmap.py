"""Unit tests for core.toolmap."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.discovery import RootSource, SteamRoot
from core.toolmap import (
    ToolMapParseError,
    load_tool_mapping,
    parse_tool_mapping_text,
    tool_name_for,
)

VALID_NESTED = """
"InstallConfigStore"
{
    "Software"
    {
        "Valve"
        {
            "Steam"
            {
                "CompatToolMapping"
                {
                    "480"
                    {
                        "name" "proton_9"
                        "config" ""
                        "priority" "250"
                    }
                    "730"
                    {
                        "name" "proton_experimental"
                        "config" ""
                        "priority" "250"
                    }
                }
            }
        }
    }
}
"""

VALID_FLAT = """
"CompatToolMapping"
{
    "480" "proton_9"
    "730" "proton_experimental"
}
"""


def test_valid_nested_mapping() -> None:
    assert parse_tool_mapping_text(VALID_NESTED) == {
        480: "proton_9",
        730: "proton_experimental",
    }


def test_valid_flat_mapping() -> None:
    assert parse_tool_mapping_text(VALID_FLAT) == {
        480: "proton_9",
        730: "proton_experimental",
    }


def test_missing_block_returns_empty() -> None:
    text = '"Software"\n{\n    "Valve" "other"\n}\n'
    assert parse_tool_mapping_text(text) == {}


def test_empty_block_returns_empty() -> None:
    text = '"CompatToolMapping"\n{\n}\n'
    assert parse_tool_mapping_text(text) == {}


def test_block_name_match_is_case_insensitive() -> None:
    text = '"COMPATTOOLMAPPING"\n{\n    "480" "proton_9"\n}\n'
    assert parse_tool_mapping_text(text) == {480: "proton_9"}


def test_non_digit_keys_are_dropped() -> None:
    text = (
        '"CompatToolMapping"\n{\n'
        '    "note" "proton_9"\n'
        '    "12x" "proton_9"\n'
        '    "480" "proton_9"\n'
        "}\n"
    )
    assert parse_tool_mapping_text(text) == {480: "proton_9"}


def test_empty_tool_name_is_dropped() -> None:
    text = '"CompatToolMapping"\n{\n    "480" "   "\n    "730" "proton_9"\n}\n'
    assert parse_tool_mapping_text(text) == {730: "proton_9"}


def test_empty_nested_name_is_dropped() -> None:
    text = (
        '"CompatToolMapping"\n{\n'
        '    "480"\n    {\n        "name" "  "\n    }\n'
        '    "730"\n    {\n        "name" "proton_9"\n    }\n'
        "}\n"
    )
    assert parse_tool_mapping_text(text) == {730: "proton_9"}


def test_tool_names_are_stripped() -> None:
    text = '"CompatToolMapping"\n{\n    "480" "  proton_9  "\n}\n'
    assert parse_tool_mapping_text(text) == {480: "proton_9"}


def test_leading_zeros_normalize_to_int() -> None:
    text = '"CompatToolMapping"\n{\n    "00480" "proton_9"\n}\n'
    assert parse_tool_mapping_text(text) == {480: "proton_9"}


def test_unrelated_blocks_are_ignored() -> None:
    text = (
        '"Other"\n{\n    "480" "wrong_tool"\n}\n"CompatToolMapping"\n{\n    "480" "proton_9"\n}\n'
    )
    assert parse_tool_mapping_text(text) == {480: "proton_9"}


def test_malformed_text_raises() -> None:
    with pytest.raises(ToolMapParseError):
        parse_tool_mapping_text('"CompatToolMapping"\n{\n    "480" ')


def test_empty_text_raises() -> None:
    with pytest.raises(ToolMapParseError):
        parse_tool_mapping_text("")


def _write_config(root: Path, text: str) -> Path:
    config_path = root / "config" / "config.vdf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")
    return config_path


def _make_root(path: Path) -> SteamRoot:
    return SteamRoot(path=path, source=RootSource.NATIVE)


def test_load_merges_first_wins(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_config(first, VALID_FLAT)
    _write_config(
        second,
        '"CompatToolMapping"\n{\n    "480" "proton_8"\n    "999" "proton_9"\n}\n',
    )
    mapping, errors = load_tool_mapping([_make_root(first), _make_root(second)])
    assert errors == []
    assert mapping == {480: "proton_9", 730: "proton_experimental", 999: "proton_9"}


def test_load_missing_file_is_empty_without_errors(tmp_path: Path) -> None:
    mapping, errors = load_tool_mapping([_make_root(tmp_path / "absent")])
    assert mapping == {}
    assert errors == []


def test_load_malformed_file_keeps_other_roots(tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    good = tmp_path / "good"
    _write_config(bad, '"CompatToolMapping"\n{\n    "480" ')
    _write_config(good, VALID_FLAT)
    mapping, errors = load_tool_mapping([_make_root(bad), _make_root(good)])
    assert mapping == {480: "proton_9", 730: "proton_experimental"}
    assert len(errors) == 1
    assert errors[0].path is not None


def test_load_undecodable_file_records_error_and_keeps_other_roots(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad"
    good = tmp_path / "good"
    config_path = bad / "config" / "config.vdf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(b"\xff\xfe\x00invalid\xff bytes")
    _write_config(good, VALID_FLAT)
    mapping, errors = load_tool_mapping([_make_root(bad)])
    assert mapping == {}
    assert len(errors) == 1
    assert errors[0].path == config_path
    mapping, errors = load_tool_mapping([_make_root(bad), _make_root(good)])
    assert mapping == {480: "proton_9", 730: "proton_experimental"}
    assert len(errors) == 1


def test_load_accepts_plain_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write_config(root, VALID_FLAT)
    mapping, errors = load_tool_mapping([root])
    assert errors == []
    assert mapping[480] == "proton_9"


def test_load_reads_utf8_bom(tmp_path: Path) -> None:
    root = tmp_path / "root"
    config_path = root / "config" / "config.vdf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(VALID_FLAT, encoding="utf-8-sig")
    mapping, errors = load_tool_mapping([root])
    assert errors == []
    assert mapping[480] == "proton_9"


def test_lookup_returns_empty_string_when_missing() -> None:
    assert tool_name_for({480: "proton_9"}, 480) == "proton_9"
    assert tool_name_for({480: "proton_9"}, 999) == ""
    assert tool_name_for({}, 480) == ""
