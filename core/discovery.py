"""Steam root and library discovery."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from core.vdf_text import LibraryFoldersParseError, load_library_folder_paths

STEAMAPPS_DIR = "steamapps"
COMPATDATA_DIR = "compatdata"
LIBRARY_FOLDERS_FILE = "libraryfolders.vdf"
FLATPAK_APP_ID = "com.valvesoftware.Steam"


class RootSource(StrEnum):
    """Where a discovered Steam root came from."""

    NATIVE = "native"
    FLATPAK = "flatpak"
    SNAP = "snap"
    CUSTOM = "custom"


class DiscoveryErrorKind(StrEnum):
    """Category of a failure encountered during discovery."""

    PERMISSION = "permission"
    PARSE = "parse"
    MISSING = "missing"


@dataclass(slots=True)
class SteamRoot:
    """A validated Steam installation root directory."""

    path: Path
    source: RootSource


@dataclass(slots=True)
class Library:
    """A single Steam library folder containing steamapps data."""

    path: Path
    root: Path


@dataclass(slots=True)
class DiscoveryError:
    """A structured failure reported instead of being silently ignored."""

    kind: DiscoveryErrorKind
    message: str
    path: Path | None = None


@dataclass(slots=True)
class DiscoveryResult:
    """Outcome of one full discovery run."""

    roots: list[SteamRoot] = field(default_factory=list)
    libraries: list[Library] = field(default_factory=list)
    errors: list[DiscoveryError] = field(default_factory=list)

    @property
    def found_roots(self) -> bool:
        """True when at least one valid Steam root was found."""
        return bool(self.roots)


def default_candidates(home: Path | None = None) -> list[SteamRoot]:
    """Ordered probe list of well-known Steam root locations."""
    base = home if home is not None else Path.home()
    flatpak_base = base / ".var" / "app" / FLATPAK_APP_ID
    snap_base = base / "snap" / "steam" / "common"
    locations: list[tuple[Path, RootSource]] = [
        (base / ".local" / "share" / "Steam", RootSource.NATIVE),
        (base / ".steam" / "steam", RootSource.NATIVE),
        (base / ".steam" / "debian-installation", RootSource.NATIVE),
        (flatpak_base / ".local" / "share" / "Steam", RootSource.FLATPAK),
        (flatpak_base / ".steam" / "steam", RootSource.FLATPAK),
        (snap_base / ".local" / "share" / "Steam", RootSource.SNAP),
    ]
    return [SteamRoot(path=path, source=source) for path, source in locations]


def validate_root(path: Path) -> bool:
    """True when path looks like a usable Steam root."""
    steamapps = path / STEAMAPPS_DIR
    try:
        return (steamapps / COMPATDATA_DIR).is_dir() or (steamapps / LIBRARY_FOLDERS_FILE).is_file()
    except OSError:
        return False


def discover(
    custom_roots: Sequence[str | Path] = (),
    *,
    home: Path | None = None,
) -> DiscoveryResult:
    """Probe known locations plus custom roots and enumerate their libraries.

    All candidates are evaluated so multiple Steam installations merge into a
    single result. Invalid candidates are dropped quietly; permission and
    parse failures are reported as structured errors.
    """
    result = DiscoveryResult()
    seen_roots: set[Path] = set()
    seen_libraries: set[Path] = set()

    candidates = default_candidates(home=home)
    for entry in custom_roots:
        candidates.append(SteamRoot(path=_normalize(Path(entry)), source=RootSource.CUSTOM))

    for candidate in candidates:
        normalized = _normalize(candidate.path)
        if normalized in seen_roots or not normalized.is_dir():
            continue
        if not validate_root(normalized):
            continue
        seen_roots.add(normalized)
        root = SteamRoot(path=normalized, source=candidate.source)
        result.roots.append(root)
        _collect_libraries(root, result, seen_libraries)

    return result


def _normalize(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _collect_libraries(root: SteamRoot, result: DiscoveryResult, seen: set[Path]) -> None:
    """Add the root library plus every library listed in its VDF.

    An absent libraryfolders.vdf is not an error: the root still contributes
    its own steamapps directory. This differs from library paths referenced
    inside the VDF, which are reported as MISSING errors when they do not
    exist on disk.
    """
    _add_library(root.path, root, result, seen)
    vdf_path = root.path / STEAMAPPS_DIR / LIBRARY_FOLDERS_FILE
    try:
        entries = load_library_folder_paths(vdf_path)
    except (FileNotFoundError, NotADirectoryError):
        return
    except PermissionError as exc:
        result.errors.append(
            DiscoveryError(kind=DiscoveryErrorKind.PERMISSION, message=str(exc), path=vdf_path)
        )
        return
    except (LibraryFoldersParseError, UnicodeDecodeError, OSError) as exc:
        result.errors.append(
            DiscoveryError(kind=DiscoveryErrorKind.PARSE, message=str(exc), path=vdf_path)
        )
        return
    for entry in entries:
        library_path = _normalize(Path(entry))
        if not library_path.exists():
            result.errors.append(
                DiscoveryError(
                    kind=DiscoveryErrorKind.MISSING,
                    message=f"library path does not exist: {library_path}",
                    path=library_path,
                )
            )
            continue
        _add_library(library_path, root, result, seen)


def _add_library(path: Path, root: SteamRoot, result: DiscoveryResult, seen: set[Path]) -> None:
    if path in seen:
        return
    seen.add(path)
    result.libraries.append(Library(path=path, root=root.path))
