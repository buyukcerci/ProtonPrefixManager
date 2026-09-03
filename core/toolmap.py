"""Per-game Proton tool mapping read from Steam config files."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vdf

TOOL_BLOCK_NAME = "compattoolmapping"
CONFIG_SUFFIX = Path("config") / "config.vdf"


class ToolMapParseError(ValueError):
    """Raised when config.vdf text cannot be parsed."""


@dataclass(slots=True)
class ToolMapError:
    """A structured load failure for one config file."""

    path: Path | None
    message: str


def parse_tool_mapping_text(text: str) -> dict[int, str]:
    """Parse VDF text and return AppID to tool name for the mapping block.

    Only the CompatToolMapping block is read. Block name match ignores
    case. AppID keys must be decimal digits. Values may be plain strings
    or mappings with a name entry. Names are stripped and empty names
    are dropped. Keys are normalized to int. Unrelated blocks are ignored.
    """
    try:
        data = vdf.loads(text)
    except (TypeError, ValueError, SyntaxError) as exc:
        raise ToolMapParseError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ToolMapParseError("top-level VDF value is not a mapping")
    if not data:
        raise ToolMapParseError("no parsable content")
    block = _find_tool_block(data)
    if block is None:
        return {}
    return _collect_mapping(block)


def load_tool_mapping(
    roots: Sequence[Any],
) -> tuple[dict[int, str], list[ToolMapError]]:
    """Load and merge tool mappings from each root in order.

    Each root contributes <root>/config/config.vdf. Roots may be SteamRoot
    records or plain paths. Files are read with utf-8-sig. The first
    occurrence of an AppID wins, so roots keep discovery order. Missing
    files and permission failures are skipped quietly. Undecodable and
    malformed files are skipped with one structured error each and never
    hide other roots.
    """
    merged: dict[int, str] = {}
    errors: list[ToolMapError] = []
    for root in roots:
        config_path = _config_path_for(root)
        try:
            text = config_path.read_text(encoding="utf-8-sig")
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(ToolMapError(path=config_path, message=str(exc)))
            continue
        try:
            parsed = parse_tool_mapping_text(text)
        except (ToolMapParseError, UnicodeDecodeError, OSError) as exc:
            errors.append(ToolMapError(path=config_path, message=str(exc)))
            continue
        for app_id, tool in parsed.items():
            merged.setdefault(app_id, tool)
    return merged, errors


def tool_name_for(mapping: Mapping[int, str], app_id: int) -> str:
    """Return the tool name for an AppID or empty string when none is set."""
    return mapping.get(app_id, "")


def _config_path_for(root: Any) -> Path:
    base = root.path if hasattr(root, "path") else root
    return Path(str(base)) / CONFIG_SUFFIX


def _find_tool_block(data: Mapping[str, Any]) -> dict[str, Any] | None:
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if isinstance(key, str) and key.casefold() == TOOL_BLOCK_NAME:
            return value
        found = _find_tool_block(value)
        if found is not None:
            return found
    return None


def _collect_mapping(block: Mapping[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for key, value in block.items():
        if not isinstance(key, str) or not key.isdigit():
            continue
        name = _tool_name_from(value)
        if not name:
            continue
        app_id = int(key)
        result.setdefault(app_id, name)
    return result


def _tool_name_from(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for entry_key, entry_value in value.items():
            if (
                isinstance(entry_key, str)
                and entry_key.casefold() == "name"
                and isinstance(entry_value, str)
                and entry_value.strip()
            ):
                return entry_value.strip()
    return ""
