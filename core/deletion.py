"""Safe prefix deletion restricted to discovered compatdata children.

A deletion may only target a directory that is a numeric-named direct
child of the ``steamapps/compatdata`` directory of one of the libraries
currently reported by discovery, whose name matches the record's AppID,
and which is not a symlink. Everything else is rejected without touching
the filesystem. Removal itself goes to the OS trash unless the caller
passes ``DeleteMode.PERMANENT`` explicitly; permanent is never a default
and never derived from stored settings.

Residual risk: a race remains between validation and execution. The
code acts on the single validated resolved path and re-checks the final
component for a symlink or a missing directory immediately before the
destructive call. A narrow window remains between that re-check and
removal, so the race is narrowed but not fully closed.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from send2trash import send2trash

from core import tools as tools_module
from core.discovery import COMPATDATA_DIR, STEAMAPPS_DIR, Library, SteamRoot
from core.models import Prefix
from core.tools import COMMON_DIR, COMPAT_TOOLS_DIR, Tool

_logger = logging.getLogger(__name__)


class DeleteMode(StrEnum):
    """How a validated prefix should be removed."""

    TRASH = "trash"
    PERMANENT = "permanent"


class DeletionStatus(StrEnum):
    """Outcome category for one deletion attempt."""

    DELETED = "deleted"
    REJECTED = "rejected"
    FAILED = "failed"


class RejectReason(StrEnum):
    """Why validation refused to touch a target.

    MAPPING_UNAVAILABLE is batch level: the usage mapping could not be
    reloaded at deletion time, so usage is unknown for every target and
    none of them may be touched. IDENTITY_MISMATCH is per target: the
    device and inode recorded for the tool at enumeration time no longer
    match the resolved target, so the record may describe a different
    directory than the one the path names at deletion time.
    """

    STALE_LIBRARY = "stale_library"
    NOT_IN_COMPATDATA = "not_in_compatdata"
    NOT_DIRECT_CHILD = "not_direct_child"
    NAME_MISMATCH = "name_mismatch"
    SYMLINK = "symlink"
    MISSING = "missing"
    READ_ONLY = "read_only"
    STALE_ROOT = "stale_root"
    NOT_IN_TOOLSDIR = "not_in_toolsdir"
    IN_USE = "in_use"
    MAPPING_UNAVAILABLE = "mapping_unavailable"
    IDENTITY_MISMATCH = "identity_mismatch"


class FailureKind(StrEnum):
    """Which layer produced a failed deletion."""

    PERMISSION = "permission"
    TRASH = "trash"
    OS = "os"


@dataclass(slots=True)
class DeletionResult:
    """Per-target outcome; prefix is the input record, unmodified."""

    prefix: Prefix
    mode: DeleteMode
    status: DeletionStatus
    reject_reason: RejectReason | None = None
    failure_kind: FailureKind | None = None
    error: str | None = None


def compatdata_path(library: Library) -> Path:
    """Resolved steamapps/compatdata directory of a library."""
    return (library.path / STEAMAPPS_DIR / COMPATDATA_DIR).resolve(strict=False)


def _is_strict_child(parent: Path, ancestor: Path) -> bool:
    """Return True when parent lies strictly below ancestor."""
    return parent != ancestor and parent.is_relative_to(ancestor)


def _unresolved_parent_has_symlink(raw: Path) -> bool:
    """True when a lexical parent of raw is a symlink.

    The main validation works on the resolved target, which normalizes
    away symlinked parents. This check runs on the unresolved input so a
    symlinked parent cannot hide behind resolution. Stat errors fail
    closed as no symlink so the resolved containment check still decides.
    """
    try:
        absolute = raw.expanduser()
        if not absolute.is_absolute():
            absolute = Path.cwd() / absolute
        return any(parent.is_symlink() for parent in absolute.parents)
    except OSError as exc:
        _logger.debug("symlink parent check failed for %s: %s", raw, exc)
        return False


def delete_prefixes(
    prefixes: Sequence[Prefix],
    libraries: Sequence[Library],
    mode: DeleteMode = DeleteMode.TRASH,
) -> list[DeletionResult]:
    """Delete each validated prefix, returning one result per unique target.

    Inputs are grouped by resolved target path and results keep
    first-seen order. When several records resolve to one target the
    flags merge conservatively: any alias whose name does not match the
    directory rejects the whole group, so a matching alias cannot win
    over a mismatched one. OS errors from a single destructive call
    never escape or abort the remaining targets; anything non-OSError raised
    by a backend propagates, as it would indicate a programming error.
    """
    compatdatas = _current_compatdatas(libraries)
    grouped: dict[str, list[tuple[Prefix, Path]]] = {}
    order: list[str] = []
    for prefix in prefixes:
        target = prefix.path.expanduser().resolve(strict=False)
        key = str(target)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append((prefix, target))
    results: list[DeletionResult] = []
    for key in order:
        aliases = [prefix for prefix, _ in grouped[key]]
        first = aliases[0]
        target = grouped[key][0][1]
        expanded = [alias.path.expanduser() for alias in aliases]
        if any(raw.is_symlink() or _unresolved_parent_has_symlink(raw) for raw in expanded):
            results.append(
                DeletionResult(
                    prefix=first,
                    mode=mode,
                    status=DeletionStatus.REJECTED,
                    reject_reason=RejectReason.SYMLINK,
                )
            )
            continue
        if any(
            target.name != str(alias.app_id) or raw.name != str(alias.app_id)
            for alias, raw in zip(aliases, expanded, strict=True)
        ):
            results.append(
                DeletionResult(
                    prefix=first,
                    mode=mode,
                    status=DeletionStatus.REJECTED,
                    reject_reason=RejectReason.NAME_MISMATCH,
                )
            )
            continue
        results.append(_delete_one(first, target, mode, compatdatas))
    return results


def _current_compatdatas(libraries: Sequence[Library]) -> list[Path]:
    """Resolved, deduplicated compatdata paths of the current library set."""
    result: list[Path] = []
    seen: set[str] = set()
    for library in libraries:
        path = compatdata_path(library)
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _final_check(target: Path) -> RejectReason | None:
    """Re-stat the final component immediately before removal.

    Fail closed: a symlink fails as SYMLINK, a non-directory fails as
    MISSING, and a stat error fails as MISSING without touching anything.
    """
    try:
        if target.is_symlink():
            return RejectReason.SYMLINK
        if not target.is_dir():
            return RejectReason.MISSING
    except OSError:
        return RejectReason.MISSING
    return None


def _delete_one(
    prefix: Prefix,
    target: Path,
    mode: DeleteMode,
    compatdatas: list[Path],
) -> DeletionResult:
    rejection = _validate(prefix, target, compatdatas, mode)
    if rejection is not None:
        return rejection
    final = _final_check(target)
    if final is not None:
        return DeletionResult(
            prefix=prefix,
            mode=mode,
            status=DeletionStatus.REJECTED,
            reject_reason=final,
        )
    try:
        if mode is DeleteMode.TRASH:
            send2trash(target)
        else:
            _remove_tree(target)
    except PermissionError as exc:
        return DeletionResult(
            prefix=prefix,
            mode=mode,
            status=DeletionStatus.FAILED,
            failure_kind=FailureKind.PERMISSION,
            error=str(exc),
        )
    except FileNotFoundError:
        return DeletionResult(
            prefix=prefix,
            mode=mode,
            status=DeletionStatus.REJECTED,
            reject_reason=RejectReason.MISSING,
        )
    except OSError as exc:
        detail = f"[errno {exc.errno}] {exc.strerror}" if exc.errno else str(exc)
        kind = FailureKind.TRASH if mode is DeleteMode.TRASH else FailureKind.OS
        return DeletionResult(
            prefix=prefix,
            mode=mode,
            status=DeletionStatus.FAILED,
            failure_kind=kind,
            error=detail,
        )
    return DeletionResult(prefix=prefix, mode=mode, status=DeletionStatus.DELETED)


def _validate(
    prefix: Prefix,
    target: Path,
    compatdatas: list[Path],
    mode: DeleteMode,
) -> DeletionResult | None:
    def reject(reason: RejectReason) -> DeletionResult:
        return DeletionResult(
            prefix=prefix,
            mode=mode,
            status=DeletionStatus.REJECTED,
            reject_reason=reason,
        )

    raw = prefix.path.expanduser()
    if raw.is_symlink():
        return reject(RejectReason.SYMLINK)

    if _unresolved_parent_has_symlink(raw):
        return reject(RejectReason.SYMLINK)

    if target.name != str(prefix.app_id) or raw.name != str(prefix.app_id):
        return reject(RejectReason.NAME_MISMATCH)

    parent = target.parent
    if parent not in compatdatas:
        if any(_is_strict_child(parent, compatdata) for compatdata in compatdatas):
            return reject(RejectReason.NOT_DIRECT_CHILD)
        implied = (Path(prefix.library).expanduser() / STEAMAPPS_DIR / COMPATDATA_DIR).resolve(
            strict=False
        )
        if parent == implied and implied not in compatdatas:
            return reject(RejectReason.STALE_LIBRARY)
        return reject(RejectReason.NOT_IN_COMPATDATA)

    if not target.is_dir():
        return reject(RejectReason.MISSING)
    return None


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path)


@dataclass(slots=True)
class ToolDeletionResult:
    """Per-target outcome; tool is the input record, unmodified."""

    tool: Tool
    mode: DeleteMode
    status: DeletionStatus
    reject_reason: RejectReason | None = None
    failure_kind: FailureKind | None = None
    error: str | None = None


def toolsdir_path(root: SteamRoot | Path) -> Path:
    """Resolved compatibilitytools.d directory of a Steam root."""
    base = root.path if isinstance(root, SteamRoot) else root
    return (Path(str(base)) / COMPAT_TOOLS_DIR).resolve(strict=False)


def commondir_path(library: Library) -> Path:
    """Resolved steamapps/common directory of a library."""
    return (library.path / STEAMAPPS_DIR / COMMON_DIR).resolve(strict=False)


def _readonly_tool_parents(libraries: Sequence[Library]) -> set[str]:
    """Resolved parent directories whose direct children are not deletable.

    System packages own the fixed system tools directory and Steam owns
    each library's steamapps/common directory, so a target under either
    counts as read-only even when a record claims otherwise. The system
    directory is read through the tools module so tests can relocate it.
    """
    parents = {str(tools_module.SYSTEM_TOOLS_DIR.resolve(strict=False))}
    for library in libraries:
        parents.add(str(commondir_path(library)))
    return parents


def delete_tools(
    tools: Sequence[Tool],
    roots: Sequence[SteamRoot | Path],
    used_paths: set[str],
    mode: DeleteMode = DeleteMode.TRASH,
    *,
    libraries: Sequence[Library] = (),
) -> list[ToolDeletionResult]:
    """Delete each validated tool, returning one result per unique target.

    Inputs are grouped by resolved target path and results keep
    first-seen order. When several records resolve to one target the
    flags merge conservatively: any alias that is read-only or in use
    rejects the whole group, so a writable unused alias cannot win over
    a locked one. Read-only is also enforced structurally: a target
    whose parent directory is the system tools directory or a library
    steamapps/common directory is refused as read-only regardless of
    the flag its records carry, so a mislabeled managed install cannot
    be deleted. The compatdata validator is untouched;
    tool targets follow the same guard shape against the currently
    discovered compatibilitytools.d directories. OS errors from a single
    destructive call never escape or abort the remaining targets. The
    used_paths set must be explicit: callers must re-resolve the set
    from the current mapping at deletion time so a
    stale UI snapshot cannot unlock a build selected by a game.

    Each record carrying the device and inode recorded at enumeration
    time is verified against the resolved target before removal: the
    stat must succeed, name a directory, and match that identity. Any
    alias in the group that mismatches rejects the whole group with
    IDENTITY_MISMATCH, so a record whose path was relocated by a swap
    during enumeration can only ever delete the directory it actually
    enumerated, never whatever the path names at deletion time. The
    current tools directories are verified without following symlinks:
    a path that is a symlink or not a directory counts as absent, so a
    link planted after enumeration cannot relocate the approval boundary
    onto the directory the link names.
    Filesystems like overlayfs or network mounts can report shifting
    device or inode values across operations; such false positives fail
    closed by rejecting the deletion.
    """
    toolsdirs = _current_toolsdirs(roots)
    readonly_parents = _readonly_tool_parents(libraries)
    used = {str(Path(entry).expanduser().resolve(strict=False)) for entry in used_paths}
    grouped: dict[str, list[tuple[Tool, Path]]] = {}
    order: list[str] = []
    for tool in tools:
        target = tool.path.expanduser().resolve(strict=False)
        key = str(target)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append((tool, target))
    results: list[ToolDeletionResult] = []
    for key in order:
        aliases = grouped[key]
        first, target = aliases[0]
        structurally_read_only = str(target.parent) in readonly_parents
        if structurally_read_only or any(alias.read_only for alias, _ in aliases):
            results.append(
                ToolDeletionResult(
                    tool=first,
                    mode=mode,
                    status=DeletionStatus.REJECTED,
                    reject_reason=RejectReason.READ_ONLY,
                )
            )
            continue
        if str(target) in used:
            results.append(
                ToolDeletionResult(
                    tool=first,
                    mode=mode,
                    status=DeletionStatus.REJECTED,
                    reject_reason=RejectReason.IN_USE,
                )
            )
            continue
        if any(
            alias.dev_ino is not None and not _identity_matches(target, alias.dev_ino)
            for alias, _ in aliases
        ):
            reason = RejectReason.MISSING if not target.is_dir() else RejectReason.IDENTITY_MISMATCH
            results.append(
                ToolDeletionResult(
                    tool=first,
                    mode=mode,
                    status=DeletionStatus.REJECTED,
                    reject_reason=reason,
                )
            )
            continue
        expanded_tools = [alias.path.expanduser() for alias, _ in aliases]
        if any(raw.is_symlink() or _unresolved_parent_has_symlink(raw) for raw in expanded_tools):
            results.append(
                ToolDeletionResult(
                    tool=first,
                    mode=mode,
                    status=DeletionStatus.REJECTED,
                    reject_reason=RejectReason.SYMLINK,
                )
            )
            continue
        if any(target.name != raw.name for raw in expanded_tools):
            results.append(
                ToolDeletionResult(
                    tool=first,
                    mode=mode,
                    status=DeletionStatus.REJECTED,
                    reject_reason=RejectReason.NAME_MISMATCH,
                )
            )
            continue
        results.append(_delete_one_tool(first, target, mode, toolsdirs, used, readonly_parents))
    return results


def _current_toolsdirs(roots: Sequence[SteamRoot | Path]) -> list[Path]:
    """Resolved, deduplicated compatibilitytools.d paths of the root set.

    Each tools directory is verified before it counts as an approval
    boundary: the lexical root path plus compatibilitytools.d must be a
    real directory when stated without following symlinks. A symlink or
    anything else at that path counts as absent, so a link planted after
    enumeration cannot relocate the boundary onto whatever it names;
    targets under a dropped path then fail the containment check. The
    returned paths stay resolved so containment compares like with like
    even when an ancestor of the root is a symlink.
    """
    result: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        base = root.path if isinstance(root, SteamRoot) else Path(str(root))
        raw = base / COMPAT_TOOLS_DIR
        key = str(raw)
        if key in seen:
            continue
        seen.add(key)
        if not _is_real_dir(raw):
            continue
        result.append(raw.resolve(strict=False))
    return result


def _is_real_dir(path: Path) -> bool:
    """True when path is a directory and not a symlink; errors fail closed."""
    try:
        return stat.S_ISDIR(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _identity_matches(target: Path, dev_ino: tuple[int, int]) -> bool:
    """True when target still refers to the identity recorded at enumeration.

    The target is the resolved tool directory, so the stat follows
    symlinks on purpose: a link planted over the recorded path resolves
    to whatever it names, and that must fail the comparison instead of
    inheriting the recorded identity. A stat failure counts as a
    mismatch so a vanished target never passes.
    Filesystems like overlayfs or network mounts can report shifting
    device or inode values across operations; such false positives fail
    closed by rejecting the deletion.
    """
    try:
        current = os.stat(target)
    except OSError:
        return False
    return stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == dev_ino


def _delete_one_tool(
    tool: Tool,
    target: Path,
    mode: DeleteMode,
    toolsdirs: list[Path],
    used: set[str],
    readonly_parents: set[str],
) -> ToolDeletionResult:
    rejection = _validate_tool(tool, target, toolsdirs, mode, used, readonly_parents)
    if rejection is not None:
        return rejection
    final = _final_check(target)
    if final is not None:
        return ToolDeletionResult(
            tool=tool,
            mode=mode,
            status=DeletionStatus.REJECTED,
            reject_reason=final,
        )
    try:
        if mode is DeleteMode.TRASH:
            send2trash(target)
        else:
            _remove_tree(target)
    except PermissionError as exc:
        return ToolDeletionResult(
            tool=tool,
            mode=mode,
            status=DeletionStatus.FAILED,
            failure_kind=FailureKind.PERMISSION,
            error=str(exc),
        )
    except FileNotFoundError:
        return ToolDeletionResult(
            tool=tool,
            mode=mode,
            status=DeletionStatus.REJECTED,
            reject_reason=RejectReason.MISSING,
        )
    except OSError as exc:
        detail = f"[errno {exc.errno}] {exc.strerror}" if exc.errno else str(exc)
        kind = FailureKind.TRASH if mode is DeleteMode.TRASH else FailureKind.OS
        return ToolDeletionResult(
            tool=tool,
            mode=mode,
            status=DeletionStatus.FAILED,
            failure_kind=kind,
            error=detail,
        )
    return ToolDeletionResult(tool=tool, mode=mode, status=DeletionStatus.DELETED)


def _validate_tool(
    tool: Tool,
    target: Path,
    toolsdirs: list[Path],
    mode: DeleteMode,
    used: set[str],
    readonly_parents: set[str],
) -> ToolDeletionResult | None:
    # Initial validation; _delete_one_tool re-checks the final component
    # again immediately before the destructive call.
    def reject(reason: RejectReason) -> ToolDeletionResult:
        return ToolDeletionResult(
            tool=tool,
            mode=mode,
            status=DeletionStatus.REJECTED,
            reject_reason=reason,
        )

    if tool.read_only or str(target.parent) in readonly_parents:
        return reject(RejectReason.READ_ONLY)

    if str(target) in used:
        return reject(RejectReason.IN_USE)

    raw = tool.path.expanduser()
    if raw.is_symlink():
        return reject(RejectReason.SYMLINK)

    if _unresolved_parent_has_symlink(raw):
        return reject(RejectReason.SYMLINK)

    if target.name != raw.name:
        return reject(RejectReason.NAME_MISMATCH)

    parent = target.parent
    if parent not in toolsdirs:
        if any(_is_strict_child(parent, toolsdir) for toolsdir in toolsdirs):
            return reject(RejectReason.NOT_DIRECT_CHILD)
        implied = toolsdir_path(tool.root)
        if parent == implied and implied not in toolsdirs:
            return reject(RejectReason.STALE_ROOT)
        return reject(RejectReason.NOT_IN_TOOLSDIR)

    if not target.is_dir():
        return reject(RejectReason.MISSING)
    return None
