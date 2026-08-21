"""Prefix enumeration and classification from discovered Steam libraries.

Walks each library's steamapps/compatdata directory, resolves a display name
per the priority chain appmanifest > shortcuts.vdf > unknown fallback, and
classifies every numeric AppID directory as STEAM, NON_STEAM, or ORPHANED.
A malformed or unreadable file never removes unrelated prefixes; it only
demotes the affected prefix to the next fallback (or skips that one source).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import vdf

from core.discovery import COMPATDATA_DIR, STEAMAPPS_DIR, DiscoveryResult, Library, SteamRoot
from core.models import Prefix, PrefixType, ScanStatus
from core.vdf_binary import ShortcutsParseError, load_shortcuts_vdf

USERDATA_DIR = "userdata"
CONFIG_DIR = "config"
SHORTCUTS_FILE = "shortcuts.vdf"

UNKNOWN_NAME_TEMPLATE = "Unknown (AppID: {app_id})"


def enumerate_from_discovery(result: DiscoveryResult) -> list[Prefix]:
    """Convenience wrapper feeding a DiscoveryResult straight into enumeration."""
    return enumerate_prefixes(result.libraries, result.roots)


def enumerate_prefixes(
    libraries: Sequence[Library],
    roots: Sequence[SteamRoot] | None = None,
    *,
    shortcuts_map: Mapping[int, str] | None = None,
) -> list[Prefix]:
    """Enumerate prefixes under each library's compatdata in discovery order.

    Names resolve manifest first, then shortcut entries from every root's
    userdata directories, then the Unknown fallback. Results dedupe on
    (AppID, resolved path), keeping the first occurrence, using the same
    normalization as Store._dedupe_key. Symlinked compatdata children are
    intentionally skipped instead of followed, so prefixes relocated via
    symlinks are never listed outside their library. Unreadable or missing
    compatdata directories are skipped silently for v1.
    """
    shortcuts = dict(shortcuts_map) if shortcuts_map is not None else _load_shortcuts(roots or ())
    prefixes: list[Prefix] = []
    seen: set[tuple[int, str]] = set()
    for library in libraries:
        compatdata = library.path / STEAMAPPS_DIR / COMPATDATA_DIR
        try:
            entries = sorted(compatdata.iterdir(), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if not entry.name.isdigit():
                continue
            app_id = int(entry.name)
            path = entry.resolve(strict=False)
            key = (app_id, str(path).rstrip("/"))
            if key in seen:
                continue
            seen.add(key)
            prefixes.append(_classify(app_id, path, library, shortcuts))
    return prefixes


def _classify(
    app_id: int,
    path: Path,
    library: Library,
    shortcuts: Mapping[int, str],
) -> Prefix:
    manifest_name = _manifest_name(library.path, app_id)
    shortcut_name = shortcuts.get(app_id)
    if manifest_name is not None:
        prefix_type, name = PrefixType.STEAM, manifest_name
    elif shortcut_name is not None:
        prefix_type, name = PrefixType.NON_STEAM, shortcut_name
    else:
        prefix_type = PrefixType.ORPHANED
        name = UNKNOWN_NAME_TEMPLATE.format(app_id=app_id)
    return Prefix(
        app_id=app_id,
        name=name,
        prefix_type=prefix_type,
        path=path,
        library=str(library.path),
        size_bytes=0,
        scan_status=ScanStatus.NOT_SCANNED,
    )


def _manifest_name(library_path: Path, app_id: int) -> str | None:
    """Return the manifest display name, or None when absent/unreadable."""
    manifest = library_path / STEAMAPPS_DIR / f"appmanifest_{app_id}.acf"
    try:
        text = manifest.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        data = vdf.loads(text)
    except (TypeError, ValueError, SyntaxError):
        return None
    if not isinstance(data, dict):
        return None
    state = data.get("AppState")
    if not isinstance(state, dict):
        return None
    name = state.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def _load_shortcuts(roots: Sequence[SteamRoot]) -> dict[int, str]:
    """Merge shortcut maps from every root's userdata directories, first wins.

    Files are visited in root order with per-directory sorted traversal, so
    the first readable occurrence of an AppID provides its display name.
    Corrupt or unreadable shortcuts files are skipped without affecting the
    remaining sources.
    """
    merged: dict[int, str] = {}
    for root in roots:
        userdata = root.path / USERDATA_DIR
        try:
            files = sorted(userdata.glob(f"*/{CONFIG_DIR}/{SHORTCUTS_FILE}"))
        except OSError:
            continue
        for shortcuts_path in files:
            try:
                entries = load_shortcuts_vdf(shortcuts_path)
            except (ShortcutsParseError, OSError):
                continue
            for app_id, name in entries.items():
                merged.setdefault(app_id, name)
    return merged
