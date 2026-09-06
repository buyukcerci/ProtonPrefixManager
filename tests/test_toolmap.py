"""Unit tests for core.toolmap."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from core.discovery import RootSource, SteamRoot
from core.toolmap import (
    ToolMapParseError,
    contributing_roots,
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


def test_unicode_digit_key_does_not_crash_parser() -> None:
    text = '"CompatToolMapping"\n{\n    "¹" "proton_9"\n    "480" "proton_exp"\n}\n'
    assert parse_tool_mapping_text(text) == {480: "proton_exp"}


def test_non_ascii_decimal_digit_keys_are_dropped() -> None:
    text = '"CompatToolMapping"\n{\n    "１２３" "proton_fullwidth"\n    "480" "proton_exp"\n}\n'
    assert parse_tool_mapping_text(text) == {480: "proton_exp"}


def test_oversized_digit_key_exceeding_int_conversion_limit_is_dropped() -> None:
    old_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        huge_key = "9" * 4500
        text = '"CompatToolMapping"\n{\n'
        text += f'    "{huge_key}" "proton_huge"\n    "480" "proton_exp"\n}}\n'
        assert parse_tool_mapping_text(text) == {480: "proton_exp"}
    finally:
        sys.set_int_max_str_digits(old_limit)


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


def test_recursion_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import toolmap as toolmap_module

    def boom(text: str) -> dict[str, object]:
        raise RecursionError("nesting too deep")

    monkeypatch.setattr(toolmap_module.vdf, "loads", boom)
    with pytest.raises(ToolMapParseError):
        parse_tool_mapping_text('"CompatToolMapping"\n{\n}')


def test_deep_nesting_finds_block_without_recursing(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import toolmap as toolmap_module

    # The block finder walks the parsed structure; a document nested past
    # the Python recursion limit must still resolve instead of raising
    # RecursionError outside the parse error handling.
    deep: dict[str, object] = {"CompatToolMapping": {"480": {"name": "proton_9"}}}
    for _ in range(6000):
        deep = {"level": deep}

    def fake_loads(text: str) -> dict[str, object]:
        return deep

    monkeypatch.setattr(toolmap_module.vdf, "loads", fake_loads)
    assert parse_tool_mapping_text("{}") == {480: "proton_9"}


def test_first_tool_block_wins_over_later_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import toolmap as toolmap_module

    # The block finder walks the parsed structure in preorder, so a
    # later sibling that carries a same-named nested block must not
    # shadow the first match found earlier in insertion order.
    data: dict[str, object] = {
        "CompatToolMapping": {"480": "first"},
        "Other": {"CompatToolMapping": {"480": "second"}},
    }

    def fake_loads(text: str) -> dict[str, object]:
        return data

    monkeypatch.setattr(toolmap_module.vdf, "loads", fake_loads)
    assert parse_tool_mapping_text("{}") == {480: "first"}


def test_deeper_earlier_tool_block_wins_over_shallow_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import toolmap as toolmap_module

    data: dict[str, object] = {
        "Outer": {"CompatToolMapping": {"480": "deep"}},
        "CompatToolMapping": {"480": "shallow"},
    }

    def fake_loads(text: str) -> dict[str, object]:
        return data

    monkeypatch.setattr(toolmap_module.vdf, "loads", fake_loads)
    assert parse_tool_mapping_text("{}") == {480: "deep"}


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


def test_load_no_configs_anywhere_records_error(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    # A non-empty root set where every config is missing must fail closed
    # with a structured error instead of reading as an empty known map.
    mapping, errors = load_tool_mapping([_make_root(first), _make_root(second)])
    assert mapping == {}
    assert len(errors) == 1
    assert errors[0].path is None
    assert "no compatibility tool mapping found" in errors[0].message


def test_load_one_missing_config_of_two_stays_quiet(tmp_path: Path) -> None:
    present = tmp_path / "present"
    absent = tmp_path / "absent"
    _write_config(present, VALID_FLAT)
    absent.mkdir()
    mapping, errors = load_tool_mapping([_make_root(present), _make_root(absent)])
    assert errors == []
    assert mapping == {480: "proton_9", 730: "proton_experimental"}


def test_load_empty_roots_stays_quiet() -> None:
    mapping, errors = load_tool_mapping([])
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
    # A decode failure names the encoding so it stays distinguishable
    # from a plain read failure in the same error list.
    assert "could not be decoded as UTF-8" in errors[0].message
    assert "could not be read" not in errors[0].message
    mapping, errors = load_tool_mapping([_make_root(bad), _make_root(good)])
    assert mapping == {480: "proton_9", 730: "proton_experimental"}
    assert len(errors) == 1


def test_load_permission_denied_records_error(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    root = tmp_path / "root"
    config_path = _write_config(root, VALID_FLAT)
    good = tmp_path / "good"
    _write_config(good, VALID_FLAT)
    config_path.chmod(0)
    try:
        mapping, errors = load_tool_mapping([_make_root(root)])
        # An unreadable mapping must fail closed with a recorded error,
        # not come back as an empty known mapping.
        assert mapping == {}
        assert len(errors) == 1
        assert errors[0].path == config_path
        assert errors[0].message
        # Other roots are still read after an unreadable one.
        mapping, errors = load_tool_mapping([_make_root(root), _make_root(good)])
        assert mapping == {480: "proton_9", 730: "proton_experimental"}
        assert len(errors) == 1
    finally:
        config_path.chmod(0o644)


def test_load_config_as_file_records_error(tmp_path: Path) -> None:
    # When "config" is a regular file, opening config/config.vdf raises
    # NotADirectoryError, which must be recorded like a permission error
    # instead of being skipped as a missing file.
    root = tmp_path / "root"
    root.mkdir()
    (root / "config").write_text("not a directory", encoding="utf-8")
    mapping, errors = load_tool_mapping([root])
    assert mapping == {}
    assert len(errors) == 1
    assert errors[0].path == root / "config" / "config.vdf"


def test_load_oversized_file_records_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core import toolmap as toolmap_module

    root = tmp_path / "root"
    config_path = _write_config(root, VALID_FLAT)
    monkeypatch.setattr(toolmap_module, "MAX_VDF_BYTES", 4)
    mapping, errors = load_tool_mapping([_make_root(root)])
    assert mapping == {}
    assert len(errors) == 1
    assert errors[0].path == config_path
    assert "could not be read" in errors[0].message


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


def test_contributing_roots_finds_valid_configs(tmp_path: Path) -> None:
    root1 = tmp_path / "root1"
    root2 = tmp_path / "root2"
    _write_config(root1, VALID_FLAT)
    _write_config(root2, VALID_NESTED)
    found = contributing_roots([_make_root(root1), root2])
    assert found == {root1.resolve(), root2.resolve()}


def test_contributing_roots_skips_missing_or_malformed(tmp_path: Path) -> None:
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    missing = tmp_path / "missing"
    _write_config(good, VALID_FLAT)
    _write_config(bad, '"broken"\n{\n')
    missing.mkdir()
    found = contributing_roots([good, bad, missing])
    assert found == {good.resolve()}


def test_load_mapping_and_contributing_roots_handles_unicode_digit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write_config(root, '"CompatToolMapping"\n{\n    "¹" "proton_9"\n    "480" "proton_exp"\n}\n')
    mapping, errors = load_tool_mapping([root])
    assert errors == []
    assert mapping == {480: "proton_exp"}
    contributing = contributing_roots([root])
    assert contributing == {root.resolve()}
