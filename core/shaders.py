"""Steam shader cache enumeration.

Steam keeps one compiled-shader cache per game under each library's
``steamapps/shadercache/<AppID>`` directory, next to ``compatdata``. The
cache is rebuilt by Steam (or the game) when missing, so deleting one only
costs a recompile on the next launch; caches left behind by uninstalled
games are pure reclaimable space.

Enumeration follows the same rules as prefixes and tools: entry names
must be ASCII decimal AppIDs, AppID 0 is ignored, and symlinks are refused
rather than followed at every level. A library whose ``steamapps`` or
``shadercache`` directory is a symlink contributes no rows; a symlinked
or unreadable ``shadercache`` directory is reported as an
EnumerationWarning. Each library's ``shadercache`` is listed through a
pinned directory descriptor, and every row records the device and inode of
its entry so deletion can verify it still targets the enumerated
directory. Names and classification come from the same manifest > shortcut
> unknown chain prefixes use, so one AppID reads the same on both tabs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from core.discovery import STEAMAPPS_DIR, Library, SteamRoot
from core.enumeration import IGNORED_APP_IDS, identify_app, is_real_dir, load_shortcuts
from core.models import PrefixType, parse_ascii_decimal
from core.tools import EnumerationWarning, scan_pinned_dir

SHADERCACHE_DIR = "shadercache"


@dataclass(slots=True)
class ShaderCache:
    """One per-game shader cache directory.

    cache_type reuses PrefixType: STEAM when an app manifest names the
    game, NON_STEAM when only a shortcut does, ORPHANED when neither does,
    which usually means the game was uninstalled and the cache was left
    behind. path is resolved; library is the library path it was found in.
    dev_ino is the entry identity observed through the pinned descriptor,
    or None when the platform fallback walk produced the row.
    """

    app_id: int
    name: str
    cache_type: PrefixType
    path: Path
    library: str
    size_bytes: int = 0
    dev_ino: tuple[int, int] | None = None

    @property
    def is_orphan(self) -> bool:
        return self.cache_type is PrefixType.ORPHANED


def shader_cache_key(cache: ShaderCache) -> str:
    """Stable identity for a cache row across refreshed copies."""
    return str(cache.path)


def enumerate_shader_caches(
    libraries: Sequence[Library],
    roots: Sequence[SteamRoot] | None = None,
    *,
    shortcuts_map: Mapping[int, str] | None = None,
) -> tuple[list[ShaderCache], list[EnumerationWarning]]:
    """Enumerate shader caches in library order, deduped on resolved path.

    A library whose steamapps directory is missing or a symlink is skipped
    silently here; prefix enumeration already reports a symlinked
    steamapps. The shadercache directory itself is opened without
    following symlinks, so a symlink there is skipped and reported.
    """
    shortcuts = dict(shortcuts_map) if shortcuts_map is not None else load_shortcuts(roots or ())
    caches: list[ShaderCache] = []
    warnings: list[EnumerationWarning] = []
    seen: set[str] = set()
    seen_warnings: set[str] = set()
    for library in libraries:
        steamapps = library.path / STEAMAPPS_DIR
        if not is_real_dir(steamapps):
            continue
        shadercache = steamapps / SHADERCACHE_DIR

        def build(name: str, *, library: Library = library, base: Path = shadercache):
            app_id = parse_ascii_decimal(name)
            if app_id is None or app_id in IGNORED_APP_IDS:
                return None
            identity = identify_app(app_id, library, shortcuts)
            return ShaderCache(
                app_id=app_id,
                name=identity.name,
                cache_type=identity.app_type,
                path=(base / name).resolve(strict=False),
                library=str(library.path),
            )

        rows, dir_warnings = scan_pinned_dir(shadercache, SHADERCACHE_DIR, build)
        for row in rows:
            key = shader_cache_key(row)
            if key not in seen:
                seen.add(key)
                caches.append(row)
        for warning in dir_warnings:
            key = str(warning.path)
            if key not in seen_warnings:
                seen_warnings.add(key)
                warnings.append(warning)
    return caches, warnings
