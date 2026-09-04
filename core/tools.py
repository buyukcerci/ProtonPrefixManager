"""Installed Proton tool enumeration and used state.

Custom tools come from each root's compatibilitytools.d directory and
Steam-managed tools come from each library's steamapps/common directory
under names starting with Proton or SteamLinuxRuntime. System-wide tools
from the fixed system tool directory enumerate as read-only rows as
well. Custom tools are writable and all other sources are read-only. A tool
counts as used when its directory name or
display name matches a value in the CompatToolMapping mapping. Games
that run on the Steam default with no override cannot be resolved to a
tool name, so default-served tools may read as unused. Built-in defaults
live in the read-only set, so the read-only rule keeps them safe
regardless. Duplicate rows collapse on resolved path with the first
occurrence winning, matching prefix enumeration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import vdf

from core.discovery import STEAMAPPS_DIR, Library, SteamRoot

COMPAT_TOOLS_DIR = "compatibilitytools.d"
COMMON_DIR = "common"
TOOL_FILE = "compatibilitytool.vdf"
MANAGED_PREFIXES = ("Proton", "SteamLinuxRuntime")
SYSTEM_TOOLS_DIR = Path("/usr/share/steam/compatibilitytools.d")


class CompatToolParseError(ValueError):
    """Raised when compatibilitytool.vdf text cannot be parsed."""


@dataclass(slots=True)
class Tool:
    """One installed Proton build with its install location and size."""

    name: str
    path: Path
    root: Path
    read_only: bool
    size_bytes: int = 0


def parse_compat_tool_text(text: str) -> str:
    """Return the display name from compatibilitytool.vdf text.

    The result may be empty when the file omits it. Unparsable text
    raises CompatToolParseError and the caller falls back to the
    directory name.
    """
    try:
        data = vdf.loads(text)
    except (TypeError, ValueError, SyntaxError) as exc:
        raise CompatToolParseError(str(exc)) from exc
    if not isinstance(data, dict):
        raise CompatToolParseError("top-level VDF value is not a mapping")
    if not data:
        raise CompatToolParseError("no parsable content")
    block = _find_compat_block(data)
    if block is None:
        return ""
    return _read_tool_entry(block)


def enumerate_tools(
    roots: Sequence[SteamRoot],
    libraries: Sequence[Library],
) -> list[Tool]:
    """Enumerate custom, managed, and system Proton tools in discovery order.

    Missing directories are a normal empty state for that root, library,
    or the system tool dir. Symlinks and non-directories are skipped
    silently. Rows dedupe on resolved path with the first occurrence
    winning.
    """
    tools: list[Tool] = []
    seen: set[str] = set()
    for root in roots:
        for tool in _custom_tools(root):
            key = str(tool.path)
            if key in seen:
                continue
            seen.add(key)
            tools.append(tool)
    for library in libraries:
        for tool in _managed_tools(library):
            key = str(tool.path)
            if key in seen:
                continue
            seen.add(key)
            tools.append(tool)
    for tool in _system_tools():
        key = str(tool.path)
        if key in seen:
            continue
        seen.add(key)
        tools.append(tool)
    return tools


def used_by(
    tools: Sequence[Tool],
    mapping: Mapping[int, str],
) -> dict[str, list[int]]:
    """Map resolved tool paths to the sorted AppIDs selecting them.

    Matching is exact on stripped strings against both the install
    directory name and the display name. Tools with no selecting AppID
    are absent from the result. Mapping values hold display names only,
    so exact per-path resolution is impossible. Distinct installs that
    share one selected display name all stay locked.
    """
    by_value: dict[str, list[int]] = {}
    for app_id, value in mapping.items():
        stripped = value.strip()
        if stripped:
            by_value.setdefault(stripped, []).append(app_id)
    result: dict[str, list[int]] = {}
    for tool in tools:
        app_ids: set[int] = set()
        # Same-name installs share one mapping value, so all of them stay locked.
        for candidate in (tool.path.name, tool.name.strip()):
            if candidate:
                app_ids.update(by_value.get(candidate, ()))
        if app_ids:
            result[str(tool.path)] = sorted(app_ids)
    return result


def _custom_tools(root: SteamRoot) -> list[Tool]:
    tools: list[Tool] = []
    try:
        entries = sorted((root.path / COMPAT_TOOLS_DIR).iterdir(), key=lambda entry: entry.name)
    except OSError:
        return tools
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        tools.append(_custom_tool(entry, root.path))
    return tools


def _custom_tool(entry: Path, root: Path, *, read_only: bool = False) -> Tool:
    name = entry.name
    try:
        text = (entry / TOOL_FILE).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        text = None
    if text is not None:
        try:
            display = parse_compat_tool_text(text)
        except CompatToolParseError:
            pass
        else:
            name = display or entry.name
    return Tool(
        name=name,
        path=entry.resolve(strict=False),
        root=root,
        read_only=read_only,
    )


def _managed_tools(library: Library) -> list[Tool]:
    tools: list[Tool] = []
    try:
        entries = sorted(
            (library.path / STEAMAPPS_DIR / COMMON_DIR).iterdir(),
            key=lambda entry: entry.name,
        )
    except OSError:
        return tools
    for entry in entries:
        if not entry.name.startswith(MANAGED_PREFIXES):
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        tools.append(
            Tool(
                name=entry.name,
                path=entry.resolve(strict=False),
                root=library.root,
                read_only=True,
            )
        )
    return tools


def _system_tools() -> list[Tool]:
    """Enumerate the fixed system tool dir as read-only rows."""
    try:
        entries = sorted(SYSTEM_TOOLS_DIR.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    tools: list[Tool] = []
    # Stored root is resolved to match the resolved tool path.
    system_root = SYSTEM_TOOLS_DIR.resolve(strict=False)
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        tools.append(_custom_tool(entry, system_root, read_only=True))
    return tools


def _find_compat_block(data: Mapping[str, object]) -> dict[str, object] | None:
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if isinstance(key, str) and _normalize(key) == "compattools":
            return value
        found = _find_compat_block(value)
        if found is not None:
            return found
    return None


def _normalize(key: str) -> str:
    return key.casefold().replace("_", "").replace(" ", "")


def _read_tool_entry(block: Mapping[str, object]) -> str:
    for value in block.values():
        if isinstance(value, dict):
            return _str_field(value, "display_name")
    return ""


def _str_field(entry: Mapping[str, object], field: str) -> str:
    for key, value in entry.items():
        if isinstance(key, str) and key.casefold() == field and isinstance(value, str):
            return value.strip()
    return ""
