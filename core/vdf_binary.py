"""Binary VDF parsing for userdata shortcuts.vdf.

Implements the minimal subset of Valve's binary VDF format needed to read
non-Steam shortcut entries: nested dictionaries (0x00), null-terminated
UTF-8 strings (0x01), little-endian Int32 values (0x02), and dictionary end
markers (0x08). Any other type byte fails closed with ShortcutsParseError.
The TYPE_* constants expose this wire format and double as the fixture
builder contract used by the test suite.
"""

from __future__ import annotations

from pathlib import Path

TYPE_DICT = 0x00
TYPE_STRING = 0x01
TYPE_INT32 = 0x02
TYPE_END = 0x08

_SHORTCUTS_KEY = "shortcuts"
_APP_ID_KEY = "appid"
_APP_NAME_KEY = "AppName"

_UINT32_MODULUS = 2**32


class ShortcutsParseError(ValueError):
    """Raised when binary shortcuts.vdf content cannot be parsed."""


def parse_shortcuts_vdf_bytes(data: bytes) -> dict[int, str]:
    """Extract an appid -> AppName mapping from raw binary VDF bytes.

    The root dictionary's "shortcuts" child is preferred when present;
    otherwise the root itself is treated as the shortcuts collection. Entries
    missing a usable appid or carrying an empty AppName are skipped. Shortcut
    appids are stored as signed Int32 by Steam but appear as their unsigned
    decimal form on disk, so negative values are normalized accordingly.
    Trailing bytes after the root dictionary are ignored.
    """
    if not data:
        return {}
    if data[0] != TYPE_DICT:
        raise ShortcutsParseError(f"unexpected root marker byte 0x{data[0]:02x}")
    root, _ = _parse_dict(data, 1)
    block = root.get(_SHORTCUTS_KEY)
    if not isinstance(block, dict):
        block = root
    return _collect_shortcuts(block)


def load_shortcuts_vdf(path: Path) -> dict[int, str]:
    """Read the binary VDF file at path and extract its shortcut mapping."""
    return parse_shortcuts_vdf_bytes(path.read_bytes())


def _parse_dict(data: bytes, pos: int) -> tuple[dict[str, object], int]:
    entries: dict[str, object] = {}
    while True:
        if pos >= len(data):
            raise ShortcutsParseError("unterminated dictionary: missing end marker")
        type_byte = data[pos]
        if type_byte == TYPE_END:
            return entries, pos + 1
        if type_byte not in (TYPE_DICT, TYPE_STRING, TYPE_INT32):
            raise ShortcutsParseError(f"unsupported type byte 0x{type_byte:02x}")
        name, pos = _read_string(data, pos + 1)
        value: object
        if type_byte == TYPE_DICT:
            value, pos = _parse_dict(data, pos)
        elif type_byte == TYPE_STRING:
            value, pos = _read_string(data, pos)
        else:
            if len(data) - pos < 4:
                raise ShortcutsParseError(f"truncated int32 value for {name!r}")
            value = int.from_bytes(data[pos : pos + 4], "little", signed=True)
            pos += 4
        entries[name] = value


def _read_string(data: bytes, pos: int) -> tuple[str, int]:
    end = data.find(b"\x00", pos)
    if end == -1:
        raise ShortcutsParseError("unterminated string: missing null terminator")
    try:
        text = data[pos:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShortcutsParseError(f"invalid UTF-8 string data: {exc}") from exc
    return text, end + 1


def _collect_shortcuts(block: dict[str, object]) -> dict[int, str]:
    shortcuts: dict[int, str] = {}
    for entry in block.values():
        if not isinstance(entry, dict):
            continue
        app_id = entry.get(_APP_ID_KEY)
        if not isinstance(app_id, int):
            continue
        name = entry.get(_APP_NAME_KEY)
        if not isinstance(name, str) or not name.strip():
            continue
        if app_id < 0:
            app_id += _UINT32_MODULUS
        shortcuts[app_id] = name.strip()
    return shortcuts
