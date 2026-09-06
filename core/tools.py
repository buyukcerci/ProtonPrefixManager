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
occurrence winning, matching prefix enumeration. When a tool's
compatibilitytool.vdf exists but cannot be read or parsed, the display
name is unknown, so the tool is marked unverified and never classifies
as reclaimable. Each root's compatibilitytools.d and each library's
steamapps/common directory is enumerated through a pinned directory
descriptor, so the listed entries always come from the directory inode
the descriptor was opened on, never from whatever the path names later.
A symlink or a planted file at the directory path is skipped entirely
and reported as an EnumerationWarning, so content visible only through a
planted link never enumerates as a deletable tool, and a path swap
before or during enumeration discards that root's rows and reports the
same warning kind. Each row from a descriptor-based scan also carries
the device and inode of its own entry in Tool.dev_ino, so deletion can
verify the target still is the directory that was enumerated.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import vdf

from core.discovery import STEAMAPPS_DIR, Library, SteamRoot
from core.vdf_read import MAX_VDF_BYTES, read_vdf_bounded

COMPAT_TOOLS_DIR = "compatibilitytools.d"
COMMON_DIR = "common"
TOOL_FILE = "compatibilitytool.vdf"
MANAGED_PREFIXES = ("Proton", "SteamLinuxRuntime")
SYSTEM_TOOLS_DIR = Path("/usr/share/steam/compatibilitytools.d")

# Linux is the only platform this project targets and it provides both
# flags. Without either one the scanner falls back to the plain path
# walk, which cannot pin the directory inode; see _scan_dir_fd.
_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FD_ENUM_SUPPORTED = hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")


class CompatToolParseError(ValueError):
    """Raised when compatibilitytool.vdf text cannot be parsed."""


class ToolCategory(StrEnum):
    """Four-way use state of an installed tool.

    The outcomes are exhaustive and mutually exclusive: a used path wins
    over every other state, an unused writable tool is reclaimable, an
    unused read-only tool is read-only, and when the tool mapping could
    not be loaded the remaining tools are unknown instead of reclaimable
    so a failed load never marks an in-use tool deletable.
    """

    USED = "used"
    RECLAIMABLE = "reclaimable"
    READ_ONLY = "read_only"
    UNKNOWN = "unknown"

    def label(self) -> str:
        labels = {
            ToolCategory.USED: "Used",
            ToolCategory.RECLAIMABLE: "Reclaimable",
            ToolCategory.READ_ONLY: "Read-only",
            ToolCategory.UNKNOWN: "Unknown",
        }
        return labels[self]


@dataclass(slots=True)
class Tool:
    """One installed Proton build with its install location and size.

    name_unverified marks a tool whose compatibilitytool.vdf exists but
    could not be read or parsed. Its real display name is then unknown,
    so a mapping value selecting that display name would match nothing.
    Classification treats such a writable unmatched tool as Unknown
    instead of Reclaimable; the flag rides on the Tool record, so every
    consumer of tool_category, is_reclaimable, and the analytics totals
    fails closed without extra plumbing.

    dev_ino holds the device and inode of the entry as observed through
    the pinned enumeration descriptor, or None when the row did not come
    from a descriptor-based scan. Deletion verifies the resolved target
    against this identity, so a record whose path was relocated by a
    swap during enumeration can never delete more than the directory it
    actually enumerated. The value rides on the record through dedupe;
    the first occurrence wins. Filesystems like overlayfs or network
    mounts can report shifting device or inode values across operations;
    such false positives fail closed by rejecting the deletion.
    """

    name: str
    path: Path
    root: Path
    read_only: bool
    size_bytes: int = 0
    name_unverified: bool = False
    dev_ino: tuple[int, int] | None = None


@dataclass(slots=True)
class EnumerationWarning:
    """One skipped enumeration source, reported instead of being silent.

    Emitted when a tools directory or a library common directory is a
    symlink or otherwise cannot be opened safely and is therefore skipped
    entirely, and when the directory path no longer refers to the inode
    the enumeration descriptor was opened on, meaning the directory was
    swapped or removed before or during enumeration. The path is the
    skipped directory as given, before any resolution.
    """

    message: str
    path: Path


def parse_compat_tool_text(text: str) -> str:
    """Return the display name from compatibilitytool.vdf text.

    The result may be empty when the file omits it. Unparsable text
    raises CompatToolParseError and the caller falls back to the
    directory name.
    """
    try:
        data = vdf.loads(text)
    except (TypeError, ValueError, SyntaxError, RecursionError) as exc:
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
) -> tuple[list[Tool], list[EnumerationWarning]]:
    """Enumerate custom, managed, and system Proton tools in discovery order.

    Missing directories are a normal empty state for that root, library,
    or the system tool dir. Each custom tools directory and each library
    common directory is enumerated through a pinned directory descriptor:
    the returned entries always come from the directory inode the
    descriptor was opened on, and the path must still refer to that same
    inode after enumeration. A symlink or a planted file at the directory
    path is skipped entirely and reported as an EnumerationWarning, so
    content visible only through a planted link never enumerates as a
    deletable tool, and a swap of the directory path before or during
    enumeration discards that root's rows and reports the same warning
    kind. Entry-level symlinks and non-directories are skipped silently.
    Rows dedupe on resolved path with the first occurrence winning,
    including its name_unverified flag and its recorded entry identity;
    warnings dedupe on the skipped path.
    """
    tools: list[Tool] = []
    warnings: list[EnumerationWarning] = []
    seen_tools: set[str] = set()
    seen_warnings: set[str] = set()

    def merge(
        new_tools: Sequence[Tool],
        new_warnings: Sequence[EnumerationWarning],
    ) -> None:
        for tool in new_tools:
            key = str(tool.path)
            if key not in seen_tools:
                seen_tools.add(key)
                tools.append(tool)
        for warning in new_warnings:
            key = str(warning.path)
            if key not in seen_warnings:
                seen_warnings.add(key)
                warnings.append(warning)

    for root in roots:
        root_tools, root_warnings = _custom_tools(root)
        merge(root_tools, root_warnings)
    for library in libraries:
        library_tools, library_warnings = _managed_tools(library)
        merge(library_tools, library_warnings)
    merge(_system_tools(), ())
    return tools, warnings


def tool_category(
    tool: Tool,
    used: set[str],
    *,
    usage_known: bool,
) -> ToolCategory:
    """Classify one tool against the used path set.

    Used wins over every other state so a locked tool never shows as
    reclaimable, matching the reclaimable rule below. Read-only state is
    intrinsic to the install, so it still shows when the mapping load
    failed. Everything writable and unmatched is unknown while the usage
    mapping is unavailable, or when the tool's own display name could
    not be read: the mapping may then select that display name without
    this record being able to see it.
    """
    if str(tool.path) in used:
        return ToolCategory.USED
    if tool.read_only:
        return ToolCategory.READ_ONLY
    if not usage_known or tool.name_unverified:
        return ToolCategory.UNKNOWN
    return ToolCategory.RECLAIMABLE


def is_reclaimable(
    tool: Tool,
    used: set[str],
    *,
    usage_known: bool,
) -> bool:
    """A tool counts as reclaimable when it is unused, writable, and known.

    Used and read-only overlap: a used tool stays locked even when it is
    also read-only, so the reclaimable set is strictly unused and
    writable. When the mapping load failed, usage_known is False and no
    tool counts as reclaimable. The same holds for a tool whose own
    compatibilitytool.vdf could not be read, because a mapping value may
    select the display name that record could not resolve. The breakdown
    labels mirror this: Used counts every used path, Read-only counts
    unused read-only paths, Unknown counts the remaining tools of a
    failed load plus unverified tools, Unused counts this helper.
    """
    return tool_category(tool, used, usage_known=usage_known) is ToolCategory.RECLAIMABLE


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


def _custom_tools(root: SteamRoot) -> tuple[list[Tool], list[EnumerationWarning]]:
    toolsdir = root.path / COMPAT_TOOLS_DIR

    def build(name: str) -> Tool:
        return _custom_tool(toolsdir / name, root.path)

    return _scan_tool_dir(toolsdir, "tools", build)


def _read_tool_vdf(vdf_path: Path) -> tuple[str | None, bool]:
    """Read compatibilitytool.vdf through the shared bounded reader.

    Returns the decoded text and whether the display name is unverified.
    A missing file is a normal state: the directory name is the real
    name and the text is None with unverified False. An existing file
    whose content cannot be read, decoded, or parsed yields text None
    with unverified True, so the caller knows the display name is
    unknown rather than absent.
    """
    try:
        text = read_vdf_bounded(vdf_path, MAX_VDF_BYTES)
    except FileNotFoundError:
        return None, False
    except (OSError, UnicodeDecodeError):
        return None, True
    if text is None:
        return None, True
    return text, False


def _custom_tool(entry: Path, root: Path, *, read_only: bool = False) -> Tool:
    name = entry.name
    vdf_path = entry / TOOL_FILE
    text, unverified = _read_tool_vdf(vdf_path)
    if text is not None:
        try:
            display = parse_compat_tool_text(text)
        except CompatToolParseError:
            unverified = True
        else:
            name = display or entry.name
    return Tool(
        name=name,
        path=entry.resolve(strict=False),
        root=root,
        read_only=read_only,
        name_unverified=unverified,
    )


def _scan_tool_dir(
    dir_path: Path,
    kind: str,
    build: Callable[[str], Tool | None],
) -> tuple[list[Tool], list[EnumerationWarning]]:
    """Enumerate the direct children of one tools or common directory.

    kind names the directory in warning messages, for example "tools" or
    "common". build receives one entry name and returns a Tool, or None
    to leave the entry out. A missing directory is a normal empty state
    with no warning.
    """
    if _FD_ENUM_SUPPORTED:
        return _scan_dir_fd(dir_path, kind, build)
    return _scan_dir_path(dir_path, kind, build)


def _scan_dir_fd(
    dir_path: Path,
    kind: str,
    build: Callable[[str], Tool | None],
) -> tuple[list[Tool], list[EnumerationWarning]]:
    """Enumerate dir_path through a pinned directory descriptor.

    The directory is opened once with O_NOFOLLOW and O_DIRECTORY, so a
    symlink or a planted file at dir_path fails the open instead of being
    followed, and every later lookup happens against the descriptor. The
    entry names come from listdir on that descriptor and each entry type
    comes from os.stat with dir_fd and follow_symlinks=False, so the
    enumerated rows are always children of the opened inode even when the
    dir_path component is swapped mid-loop. Every returned row carries
    the device and inode of its own entry as observed through the
    descriptor in Tool.dev_ino, which deletion later uses to verify the
    target still is the directory that was enumerated. After the loop,
    dir_path must still refer to the same device and inode the descriptor
    was opened on; any mismatch or stat failure means the directory was
    replaced or removed before or during enumeration, so every row from
    this directory is discarded and one EnumerationWarning is reported
    for dir_path. Entry-level symlinks and non-directories are skipped
    silently. An open failure is reported as an EnumerationWarning except
    for a missing directory, which stays a silent empty result. The
    warning message distinguishes a symlinked directory from an unreadable
    one by statting dir_path without following links, because opening a
    symlinked directory with O_NOFOLLOW fails with ENOTDIR rather than
    ELOOP on the kernels this project targets; the failed open remains
    the authoritative decision to skip either way. A listing failure on
    the opened descriptor is reported the same way. The descriptor is
    always closed and close errors are swallowed.
    """
    try:
        fd = os.open(dir_path, _DIR_OPEN_FLAGS)
    except FileNotFoundError:
        return [], []
    except OSError:
        if _path_is_symlink(dir_path):
            message = f"skipped symlinked {kind} directory: {dir_path}"
        else:
            message = f"skipped unreadable {kind} directory: {dir_path}"
        return [], [EnumerationWarning(message=message, path=dir_path)]
    try:
        identity = os.fstat(fd)
        try:
            names = sorted(os.listdir(fd))
        except OSError:
            message = f"skipped unreadable {kind} directory: {dir_path}"
            return [], [EnumerationWarning(message=message, path=dir_path)]
        tools: list[Tool] = []
        for name in names:
            try:
                entry_stat = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                # The entry vanished after the listing; skip it silently.
                continue
            if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
                continue
            tool = build(name)
            if tool is not None:
                # The identity comes from the descriptor's own entry stat,
                # not from a re-stat through the mutable path, so it stays
                # bound to the inode the listing saw even when dir_path is
                # swapped while the build callback runs.
                tool.dev_ino = (entry_stat.st_dev, entry_stat.st_ino)
                tools.append(tool)
        if not _same_inode(dir_path, identity):
            message = f"{kind} directory changed during enumeration: {dir_path}"
            return [], [EnumerationWarning(message=message, path=dir_path)]
        return tools, []
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _path_is_symlink(path: Path) -> bool:
    """True when path is a symlink; stat errors read as not a symlink.

    This only classifies the warning message. The failed open decides
    whether the directory is skipped, and it is never retried.
    """
    try:
        return stat.S_ISLNK(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _same_inode(dir_path: Path, identity: os.stat_result) -> bool:
    """True when dir_path still refers to the opened directory's inode."""
    try:
        current = os.stat(dir_path, follow_symlinks=False)
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)


def _scan_dir_path(
    dir_path: Path,
    kind: str,
    build: Callable[[str], Tool | None],
) -> tuple[list[Tool], list[EnumerationWarning]]:
    """Path-based fallback for platforms without the directory open flags.

    This walk cannot pin the directory inode, so a symlinked dir_path is
    only caught by the pre-open check and a swap mid-loop goes unnoticed.
    Rows built here carry no entry identity, so deletion falls back to
    the path-based checks for them. An unreadable directory warns like
    the fd variant does; only a missing directory stays a silent empty
    state. Linux provides the flags, so this variant never runs on the
    platforms this project targets.
    """
    if dir_path.is_symlink():
        message = f"skipped symlinked {kind} directory: {dir_path}"
        return [], [EnumerationWarning(message=message, path=dir_path)]
    try:
        entries = sorted(dir_path.iterdir(), key=lambda entry: entry.name)
    except FileNotFoundError:
        return [], []
    except OSError:
        message = f"skipped unreadable {kind} directory: {dir_path}"
        return [], [EnumerationWarning(message=message, path=dir_path)]
    tools: list[Tool] = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        tool = build(entry.name)
        if tool is not None:
            tools.append(tool)
    return tools, []


def _managed_tools(library: Library) -> tuple[list[Tool], list[EnumerationWarning]]:
    commondir = library.path / STEAMAPPS_DIR / COMMON_DIR

    def build(name: str) -> Tool | None:
        if not name.startswith(MANAGED_PREFIXES):
            return None
        return Tool(
            name=name,
            path=(commondir / name).resolve(strict=False),
            root=library.root,
            read_only=True,
        )

    return _scan_tool_dir(commondir, "common", build)


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
    # Iterative preorder traversal with an explicit stack. Documents from
    # untrusted installs can nest arbitrarily deep, and a recursive walk
    # would raise RecursionError outside the parse error handling, so the
    # traversal must not use Python call stack depth. Reversed pushes keep
    # the visit order identical to the recursive form.
    stack = list(data.items())[::-1]
    while stack:
        key, value = stack.pop()
        if not isinstance(value, dict):
            continue
        if isinstance(key, str) and _normalize(key) == "compattools":
            return value
        stack.extend(list(value.items())[::-1])
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
