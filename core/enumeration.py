"""Prefix enumeration and classification from discovered Steam libraries.

Walks each library's steamapps/compatdata directory, resolves a display name
per the priority chain appmanifest > shortcuts.vdf > unknown fallback, and
classifies every numeric AppID directory as STEAM, NON_STEAM, or ORPHANED.
A malformed or unreadable file never removes unrelated prefixes; it only
demotes the affected prefix to the next fallback (or skips that one source).
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import vdf

from core.discovery import COMPATDATA_DIR, STEAMAPPS_DIR, DiscoveryResult, Library, SteamRoot
from core.models import Prefix, PrefixType, ScanStatus, parse_ascii_decimal
from core.vdf_binary import ShortcutsParseError, load_shortcuts_vdf
from core.vdf_read import MAX_VDF_BYTES, read_vdf_bounded

USERDATA_DIR = "userdata"
CONFIG_DIR = "config"
SHORTCUTS_FILE = "shortcuts.vdf"

UNKNOWN_NAME_TEMPLATE = "Unknown (AppID: {app_id})"

# Steam installs its own runtime and Proton tooling as pseudo-apps with real
# manifests, so they enumerate as ordinary STEAM prefixes. The curated AppID
# list may lag Valve; the name pattern catches releases the list is missing.
# Pattern matches Proton 9.0 and Steam Linux Runtime 5.0, rejects
# ProtonUp-Qt and Proton-7.0, uses word boundary plus (?!-) to avoid
# Proton-Up false positives, case insensitive for lower case manifests.
RUNTIME_COMPONENT_APP_IDS = frozenset({226490, 962960, 1070560, 1391110, 2805730, 3240890})
RUNTIME_COMPONENT_NAME_PATTERN = re.compile(
    r"^(Proton|Steam Linux Runtime|Steam Runtime)\b(?!-)", re.IGNORECASE
)

# AppID 0 is Steam's internal probe prefix for shader pre-cache and
# D3D driver query; it is not a real game and is recreated on launch,
# so it is always ignored.
IGNORED_APP_IDS = frozenset({0})


def is_runtime_component(app_id: int, resolved_name: str | None) -> bool:
    """True when the AppID or resolved manifest name identifies a Steam component."""
    if app_id in RUNTIME_COMPONENT_APP_IDS:
        return True
    if resolved_name is None:
        return False
    stripped = resolved_name.strip()
    if not stripped:
        return False
    return bool(RUNTIME_COMPONENT_NAME_PATTERN.search(stripped))


def enumerate_from_discovery(result: DiscoveryResult) -> list[Prefix]:
    """Convenience wrapper feeding a DiscoveryResult straight into enumeration."""
    return enumerate_prefixes(result.libraries, result.roots)


def is_real_dir(path: Path) -> bool:
    """True when path is a directory and not a symlink; errors fail closed.

    A single lstat decides both conditions, so no swap can slip in
    between a symlink check and a directory check.
    """
    try:
        return stat.S_ISDIR(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        return False


def library_steamapps_subdir(library: Library, name: str) -> Path | None:
    """Lexical steamapps/<name> path of a library, or None when unusable.

    Both steamapps and the subdirectory must be real directories; a
    symlink at either level is refused rather than followed, so the
    library cannot relocate its contents (and the deletion boundary)
    elsewhere.
    """
    steamapps = library.path / STEAMAPPS_DIR
    subdir = steamapps / name
    if is_real_dir(steamapps) and is_real_dir(subdir):
        return subdir
    return None


def library_compatdata_dir(library: Library) -> Path | None:
    """Lexical steamapps/compatdata path of a library, or None when unusable."""
    return library_steamapps_subdir(library, COMPATDATA_DIR)


def symlinked_compatdata_paths(libraries: Sequence[Library]) -> list[Path]:
    """steamapps or compatdata paths that exist but are symlinks.

    Libraries with such a path are skipped by enumerate_prefixes and by
    the deletion boundary; this lets the UI report them instead of the
    library silently showing no prefixes. A missing directory is normal
    for a library without Proton games and is not reported.
    """
    result: list[Path] = []
    for library in libraries:
        link = compatdata_symlink(library.path)
        if link is not None:
            result.append(link)
    return result


def compatdata_symlink(library_path: Path) -> Path | None:
    """The steamapps or compatdata path under library_path that is a symlink, if any."""
    return steamapps_subdir_symlink(library_path, COMPATDATA_DIR)


def steamapps_subdir_symlink(library_path: Path, name: str) -> Path | None:
    """The steamapps or steamapps/<name> path under library_path that is a symlink."""
    steamapps = library_path / STEAMAPPS_DIR
    for path in (steamapps, steamapps / name):
        try:
            if stat.S_ISLNK(os.stat(path, follow_symlinks=False).st_mode):
                return path
        except OSError:
            return None
    return None


def enumerate_prefixes(
    libraries: Sequence[Library],
    roots: Sequence[SteamRoot] | None = None,
    *,
    shortcuts_map: Mapping[int, str] | None = None,
) -> list[Prefix]:
    """Enumerate prefixes under each library's compatdata in discovery order,
    skipping AppID 0 which is Steam's internal probe prefix.

    Compatdata directory entries must be ASCII decimal digits. Names resolve
    manifest first, then shortcut entries from every root's userdata
    directories, then the Unknown fallback. Results dedupe on (AppID,
    resolved path), keeping the first occurrence, using the same
    normalization as Store._dedupe_key. Symlinked compatdata children are
    intentionally skipped instead of followed, so prefixes relocated via
    symlinks are never listed outside their library. A library whose
    steamapps or compatdata directory is itself a symlink is skipped the
    same way; symlinked_compatdata_paths reports those so callers can warn.
    Unreadable or missing compatdata directories are skipped silently.
    """
    shortcuts = dict(shortcuts_map) if shortcuts_map is not None else _load_shortcuts(roots or ())
    prefixes: list[Prefix] = []
    seen: set[tuple[int, str]] = set()
    for library in libraries:
        compatdata = library_compatdata_dir(library)
        if compatdata is None:
            continue
        try:
            entries = sorted(compatdata.iterdir(), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if not is_real_dir(entry):
                continue
            app_id = parse_ascii_decimal(entry.name)
            if app_id is None:
                continue
            # Skip ignored probe prefixes (see IGNORED_APP_IDS).
            if app_id in IGNORED_APP_IDS:
                continue
            path = entry.resolve(strict=False)
            key = (app_id, str(path).rstrip("/"))
            if key in seen:
                continue
            seen.add(key)
            prefixes.append(_classify(app_id, path, library, shortcuts))
    return prefixes


@dataclass(frozen=True, slots=True)
class AppIdentity:
    """Display name and classification of one AppID within a library."""

    name: str
    app_type: PrefixType
    runtime_component: bool


def identify_app(app_id: int, library: Library, shortcuts: Mapping[int, str]) -> AppIdentity:
    """Resolve an AppID's name and type: manifest > shortcut > unknown.

    Shared by prefix and shader cache enumeration so both classify the
    same AppID the same way.
    """
    manifest_name = _manifest_name(library.path, app_id)
    shortcut_name = shortcuts.get(app_id)
    resolved_name = manifest_name if manifest_name is not None else shortcut_name
    runtime_component = is_runtime_component(app_id, resolved_name)
    if manifest_name is not None:
        return AppIdentity(manifest_name, PrefixType.STEAM, runtime_component)
    if shortcut_name is not None:
        return AppIdentity(shortcut_name, PrefixType.NON_STEAM, runtime_component)
    return AppIdentity(
        UNKNOWN_NAME_TEMPLATE.format(app_id=app_id), PrefixType.ORPHANED, runtime_component
    )


def load_shortcuts(roots: Sequence[SteamRoot]) -> dict[int, str]:
    """Merged shortcut names from every root; see _load_shortcuts."""
    return _load_shortcuts(roots)


def _classify(
    app_id: int,
    path: Path,
    library: Library,
    shortcuts: Mapping[int, str],
) -> Prefix:
    identity = identify_app(app_id, library, shortcuts)
    return Prefix(
        app_id=app_id,
        name=identity.name,
        prefix_type=identity.app_type,
        path=path,
        library=str(library.path),
        size_bytes=0,
        scan_status=ScanStatus.NOT_SCANNED,
        is_runtime_component=identity.runtime_component,
    )


def _manifest_name(library_path: Path, app_id: int) -> str | None:
    """Return the manifest display name, or None when absent/unreadable."""
    manifest = library_path / STEAMAPPS_DIR / f"appmanifest_{app_id}.acf"
    try:
        text = read_vdf_bounded(manifest, MAX_VDF_BYTES)
    except (OSError, UnicodeDecodeError):
        return None
    if text is None:
        return None
    try:
        data = vdf.loads(text)
    except (TypeError, ValueError, SyntaxError, RecursionError):
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
            except (ShortcutsParseError, OSError, RecursionError):
                continue
            for app_id, name in entries.items():
                merged.setdefault(app_id, name)
    return merged
