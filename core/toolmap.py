"""Per-game Proton tool mapping read from Steam config files."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vdf

from core.models import parse_ascii_decimal
from core.vdf_read import MAX_VDF_BYTES, read_vdf_bounded

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
    case. AppID keys must be ASCII decimal digits. Values may be plain strings
    or mappings with a name entry. Names are stripped and empty names
    are dropped. Keys are normalized to int. Unrelated blocks are ignored.
    """
    try:
        data = vdf.loads(text)
        if not isinstance(data, dict):
            raise ToolMapParseError("top-level VDF value is not a mapping")
        if not data:
            raise ToolMapParseError("no parsable content")
        block = _find_tool_block(data)
        if block is None:
            return {}
        return _collect_mapping(block)
    except ToolMapParseError:
        raise
    except (TypeError, ValueError, SyntaxError, RecursionError) as exc:
        raise ToolMapParseError(str(exc)) from exc


def load_tool_mapping(
    roots: Sequence[Any],
) -> tuple[dict[int, str], list[ToolMapError]]:
    """Load and merge tool mappings from each root in order.

    Each root contributes <root>/config/config.vdf. Roots may be SteamRoot
    records or plain paths. Files are read with utf-8-sig through a
    bounded, no-follow reader. The first occurrence of an AppID wins, so
    roots keep discovery order. Missing files are skipped quietly as long
    as at least one root contributes a readable config: when the root set
    is non-empty and every config is missing, one structured error is
    recorded and the empty mapping is returned, so a deleted config.vdf
    cannot read as an empty known mapping. An empty root set stays a
    quiet empty result. Any read failure, including permission errors,
    oversized content, and non-regular files, records one structured
    error instead of yielding an empty mapping, so a failed load never
    reads as an empty tool map. Content that is not valid UTF-8 records
    a decoding error with its own message, so a decode failure stays
    distinguishable from a read failure. Malformed files are skipped with
    one structured error each and never hide other roots.
    """
    merged: dict[int, str] = {}
    errors: list[ToolMapError] = []
    found_any = False
    for root in roots:
        config_path = _config_path_for(root)
        try:
            text = read_vdf_bounded(config_path, MAX_VDF_BYTES)
        except FileNotFoundError:
            continue
        except UnicodeDecodeError:
            found_any = True
            errors.append(
                ToolMapError(
                    path=config_path,
                    message=f"{config_path} could not be decoded as UTF-8",
                )
            )
            continue
        except OSError as exc:
            found_any = True
            errors.append(ToolMapError(path=config_path, message=str(exc)))
            continue
        found_any = True
        if text is None:
            errors.append(
                ToolMapError(path=config_path, message=f"{config_path} could not be read")
            )
            continue
        try:
            parsed = parse_tool_mapping_text(text)
        except ToolMapParseError as exc:
            errors.append(ToolMapError(path=config_path, message=str(exc)))
            continue
        for app_id, tool in parsed.items():
            merged.setdefault(app_id, tool)
    if roots and not found_any:
        errors.append(ToolMapError(path=None, message="no compatibility tool mapping found"))
    return merged, errors


def tool_name_for(mapping: Mapping[int, str], app_id: int) -> str:
    """Return the tool name for an AppID or empty string when none is set."""
    return mapping.get(app_id, "")


def contributing_roots(roots: Sequence[Any]) -> set[Path]:
    """Return the set of root paths that contributed a readable config."""
    contributing: set[Path] = set()
    for root in roots:
        config_path = _config_path_for(root)
        try:
            text = read_vdf_bounded(config_path, MAX_VDF_BYTES)
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            continue
        if text is None:
            continue
        try:
            parse_tool_mapping_text(text)
        except ToolMapParseError:
            continue
        base = root.path if hasattr(root, "path") else root
        contributing.add(Path(str(base)).resolve(strict=False))
    return contributing


def _config_path_for(root: Any) -> Path:
    base = root.path if hasattr(root, "path") else root
    return Path(str(base)) / CONFIG_SUFFIX


def _find_tool_block(data: Mapping[str, Any]) -> dict[str, Any] | None:
    # Iterative preorder traversal with an explicit stack. Documents from
    # untrusted installs can nest arbitrarily deep, and a recursive walk
    # would raise RecursionError outside the parse error handling, so the
    # traversal must not use Python call stack depth. Reversed pushes keep
    # the visit order identical to the recursive form.
    stack: list[tuple[Any, Any]] = list(data.items())[::-1]
    while stack:
        key, value = stack.pop()
        if not isinstance(value, dict):
            continue
        if isinstance(key, str) and key.casefold() == TOOL_BLOCK_NAME:
            return value
        stack.extend(list(value.items())[::-1])
    return None


def _collect_mapping(block: Mapping[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for key, value in block.items():
        app_id = parse_ascii_decimal(key)
        if app_id is None:
            continue
        name = _tool_name_from(value)
        if not name:
            continue
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
