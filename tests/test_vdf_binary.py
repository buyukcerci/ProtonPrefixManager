"""Unit tests for core.vdf_binary."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.vdf_binary import (
    TYPE_DICT,
    TYPE_END,
    TYPE_INT32,
    TYPE_STRING,
    ShortcutsParseError,
    load_shortcuts_vdf,
    parse_shortcuts_vdf_bytes,
)


def _cstr(text: str) -> bytes:
    return text.encode("utf-8") + b"\x00"


def _int32(value: int) -> bytes:
    return value.to_bytes(4, "little", signed=True)


def build_shortcuts_vdf(entries: list[tuple[int, str]]) -> bytes:
    out = bytearray([TYPE_DICT])
    out += _cstr("shortcuts")
    for index, (app_id, name) in enumerate(entries):
        out.append(TYPE_DICT)
        out += _cstr(str(index))
        out.append(TYPE_INT32)
        out += _cstr("appid")
        out += _int32(app_id)
        out.append(TYPE_STRING)
        out += _cstr("AppName")
        out += _cstr(name)
        out.append(TYPE_STRING)
        out += _cstr("Exe")
        out += _cstr(f'"{name}.exe"')
        out.append(TYPE_END)
    out.append(TYPE_END)
    return bytes(out)


def _build_returnal_fixture() -> bytes:
    out = bytearray([TYPE_DICT])
    out += _cstr("shortcuts")
    out.append(TYPE_DICT)
    out += _cstr("0")
    out.append(TYPE_INT32)
    out += _cstr("appid")
    out += _int32(-1839808813)
    out.append(TYPE_STRING)
    out += _cstr("AppName")
    out += _cstr("Returnal.exe")
    out.append(TYPE_STRING)
    out += _cstr("Exe")
    out += _cstr("/home/user/.local/share/Steam/steamapps/common/Returnal/Returnal.exe")
    out.append(TYPE_STRING)
    out += _cstr("StartDir")
    out += _cstr("/home/user/.local/share/Steam/steamapps/common/Returnal/")
    out.append(TYPE_STRING)
    out += _cstr("icon")
    out += _cstr("")
    out.append(TYPE_STRING)
    out += _cstr("ShortcutPath")
    out += _cstr("")
    out.append(TYPE_STRING)
    out += _cstr("LaunchOptions")
    out += _cstr("%command%")
    for key in (
        "IsHidden",
        "AllowDesktopConfig",
        "AllowOverlay",
        "OpenVR",
        "Devkit",
        "DevkitGameID",
        "DevkitOverrideAppID",
    ):
        out.append(TYPE_INT32)
        out += _cstr(key)
        out += _int32(0)
    out.append(TYPE_INT32)
    out += _cstr("LastPlayTime")
    out += _int32(0)
    out.append(TYPE_STRING)
    out += _cstr("FlatpakAppID")
    out += _cstr("")
    out.append(TYPE_STRING)
    out += _cstr("sortas")
    out += _cstr("")
    out.append(TYPE_DICT)
    out += _cstr("tags")
    out.append(TYPE_END)
    out.append(TYPE_END)
    out.append(TYPE_END)
    return bytes(out)


def test_single_entry_round_trip() -> None:
    data = build_shortcuts_vdf([(123456, "My Game")])
    assert parse_shortcuts_vdf_bytes(data) == {123456: "My Game"}


def test_multiple_entries_all_extracted() -> None:
    data = build_shortcuts_vdf([(1, "First"), (2, "Second"), (3, "Third")])
    assert parse_shortcuts_vdf_bytes(data) == {1: "First", 2: "Second", 3: "Third"}


def test_empty_input_yields_empty_mapping() -> None:
    assert parse_shortcuts_vdf_bytes(b"") == {}


def test_empty_root_dict_yields_empty_mapping() -> None:
    assert parse_shortcuts_vdf_bytes(bytes([TYPE_DICT, TYPE_END])) == {}


def test_unicode_app_name_preserved() -> None:
    data = build_shortcuts_vdf([(42, "Bücherroote")])
    assert parse_shortcuts_vdf_bytes(data) == {42: "Bücherroote"}


def test_empty_app_name_entry_ignored() -> None:
    data = build_shortcuts_vdf([(7, "")])
    assert parse_shortcuts_vdf_bytes(data) == {}


def test_whitespace_only_app_name_entry_ignored() -> None:
    data = build_shortcuts_vdf([(7, "   ")])
    assert parse_shortcuts_vdf_bytes(data) == {}


def test_negative_appid_normalized_to_unsigned() -> None:
    data = build_shortcuts_vdf([(-1, "Huge Shortcut")])
    assert parse_shortcuts_vdf_bytes(data) == {2**32 - 1: "Huge Shortcut"}


def test_real_world_returnal_fixture() -> None:
    data = _build_returnal_fixture()
    assert parse_shortcuts_vdf_bytes(data) == {2455158483: "Returnal.exe"}


def test_legacy_double_wrapped_form_still_parses() -> None:
    out = bytearray([TYPE_DICT, TYPE_DICT])
    out += _cstr("shortcuts")
    out.append(TYPE_DICT)
    out += _cstr("0")
    out.append(TYPE_INT32)
    out += _cstr("appid")
    out += _int32(123)
    out.append(TYPE_STRING)
    out += _cstr("AppName")
    out += _cstr("Legacy Game")
    out.append(TYPE_END)
    out.append(TYPE_END)
    out.append(TYPE_END)
    assert parse_shortcuts_vdf_bytes(bytes(out)) == {123: "Legacy Game"}


def test_truncated_string_raises() -> None:
    data = build_shortcuts_vdf([(1, "Game")])
    truncated = data[: len(data) - 2]
    with pytest.raises(ShortcutsParseError):
        parse_shortcuts_vdf_bytes(truncated)


def test_truncated_int32_raises() -> None:
    data = build_shortcuts_vdf([(1, "Game")])
    cut = data.index(_int32(1)) + 2
    with pytest.raises(ShortcutsParseError):
        parse_shortcuts_vdf_bytes(data[:cut])


def test_missing_end_marker_raises() -> None:
    data = build_shortcuts_vdf([(1, "Game")])
    with pytest.raises(ShortcutsParseError):
        parse_shortcuts_vdf_bytes(data[:-1])


def test_invalid_type_byte_raises() -> None:
    data = (
        bytes([TYPE_DICT])
        + _cstr("shortcuts")
        + bytes([TYPE_DICT])
        + _cstr("0")
        + bytes([0x63])
        + _cstr("x")
    )
    with pytest.raises(ShortcutsParseError):
        parse_shortcuts_vdf_bytes(data)


def test_unexpected_root_marker_raises() -> None:
    with pytest.raises(ShortcutsParseError):
        parse_shortcuts_vdf_bytes(bytes([TYPE_STRING]))


def test_extra_trailing_bytes_after_root_ignored() -> None:
    data = build_shortcuts_vdf([(5, "Game")]) + b"\xde\xad\xbe\xef"
    assert parse_shortcuts_vdf_bytes(data) == {5: "Game"}


def test_entry_without_appid_skipped() -> None:
    data = (
        bytes([TYPE_DICT])
        + _cstr("shortcuts")
        + bytes([TYPE_DICT])
        + _cstr("0")
        + bytes([TYPE_STRING])
        + _cstr("AppName")
        + _cstr("No AppId Here")
        + bytes([TYPE_END])
        + bytes([TYPE_END])
    )
    assert parse_shortcuts_vdf_bytes(data) == {}


def test_string_appid_skipped() -> None:
    data = (
        bytes([TYPE_DICT])
        + _cstr("shortcuts")
        + bytes([TYPE_DICT])
        + _cstr("0")
        + bytes([TYPE_STRING])
        + _cstr("appid")
        + _cstr("not-a-number")
        + bytes([TYPE_END])
        + bytes([TYPE_END])
    )
    assert parse_shortcuts_vdf_bytes(data) == {}


def test_non_dict_values_in_block_ignored() -> None:
    data = (
        bytes([TYPE_DICT])
        + _cstr("shortcuts")
        + bytes([TYPE_STRING])
        + _cstr("note")
        + _cstr("just a string")
        + bytes([TYPE_END])
    )
    assert parse_shortcuts_vdf_bytes(data) == {}


def test_load_reads_file_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "shortcuts.vdf"
    path.write_bytes(build_shortcuts_vdf([(99, "Disk Game")]))
    assert load_shortcuts_vdf(path) == {99: "Disk Game"}


def test_load_propagates_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_shortcuts_vdf(tmp_path / "missing.vdf")


def test_invalid_utf8_string_raises() -> None:
    data = bytes([TYPE_DICT]) + b"\xff\xfe\x00"
    with pytest.raises(ShortcutsParseError):
        parse_shortcuts_vdf_bytes(data)
