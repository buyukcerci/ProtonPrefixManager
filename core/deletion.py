"""Safe prefix deletion restricted to discovered compatdata children.

A deletion may only target a directory that is a numeric-named direct
child of the ``steamapps/compatdata`` directory of one of the libraries
currently reported by discovery, whose name matches the record's AppID,
and which is not a symlink. Everything else is rejected without touching
the filesystem. Removal itself goes to the OS trash unless the caller
passes ``DeleteMode.PERMANENT`` explicitly; permanent is never a default
and never derived from stored settings.

Residual risk: a race remains between validation and execution — the
directory could in principle be swapped in that window. Mitigations are
acting on the single validated resolved path and re-checking the final
component for symlinks immediately before the destructive call.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from send2trash import send2trash

from core.discovery import COMPATDATA_DIR, STEAMAPPS_DIR, Library, SteamRoot
from core.models import Prefix
from core.tools import COMPAT_TOOLS_DIR, Tool


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
    """Why validation refused to touch a target."""

    STALE_LIBRARY = "stale_library"
    NOT_IN_COMPATDATA = "not_in_compatdata"
    NOT_DIRECT_CHILD = "not_direct_child"
    NAME_MISMATCH = "name_mismatch"
    SYMLINK = "symlink"
    MISSING = "missing"
    READ_ONLY = "read_only"
    STALE_ROOT = "stale_root"
    NOT_IN_TOOLSDIR = "not_in_toolsdir"


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


def delete_prefixes(
    prefixes: Sequence[Prefix],
    libraries: Sequence[Library],
    mode: DeleteMode = DeleteMode.TRASH,
) -> list[DeletionResult]:
    """Delete each validated prefix, returning one result per unique target.

    Inputs are deduped by resolved target path (first occurrence wins) and
    results keep first-seen order. OS errors from a single destructive call
    never escape or abort the remaining targets; anything non-OSError raised
    by a backend propagates, as it would indicate a programming error.
    """
    compatdatas = _current_compatdatas(libraries)
    results: list[DeletionResult] = []
    seen: set[str] = set()
    for prefix in prefixes:
        target = prefix.path.expanduser().resolve(strict=False)
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        results.append(_delete_one(prefix, target, mode, compatdatas))
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


def _delete_one(
    prefix: Prefix,
    target: Path,
    mode: DeleteMode,
    compatdatas: list[Path],
) -> DeletionResult:
    rejection = _validate(prefix, target, compatdatas, mode)
    if rejection is not None:
        return rejection
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

    if prefix.path.expanduser().is_symlink():
        return reject(RejectReason.SYMLINK)

    if not target.name.isdigit() or int(target.name) != prefix.app_id:
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


def delete_tools(
    tools: Sequence[Tool],
    roots: Sequence[SteamRoot | Path],
    mode: DeleteMode = DeleteMode.TRASH,
) -> list[ToolDeletionResult]:
    """Delete each validated tool, returning one result per unique target.

    Inputs are deduped by resolved target path (first occurrence wins) and
    results keep first-seen order. The compatdata validator is untouched;
    tool targets follow the same guard shape against the currently
    discovered compatibilitytools.d directories. OS errors from a single
    destructive call never escape or abort the remaining targets.
    """
    toolsdirs = _current_toolsdirs(roots)
    results: list[ToolDeletionResult] = []
    seen: set[str] = set()
    for tool in tools:
        target = tool.path.expanduser().resolve(strict=False)
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        results.append(_delete_one_tool(tool, target, mode, toolsdirs))
    return results


def _current_toolsdirs(roots: Sequence[SteamRoot | Path]) -> list[Path]:
    """Resolved, deduplicated compatibilitytools.d paths of the root set."""
    result: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        path = toolsdir_path(root)
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _delete_one_tool(
    tool: Tool,
    target: Path,
    mode: DeleteMode,
    toolsdirs: list[Path],
) -> ToolDeletionResult:
    rejection = _validate_tool(tool, target, toolsdirs, mode)
    if rejection is not None:
        return rejection
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
) -> ToolDeletionResult | None:
    # Single check before the destructive call; no second check runs at execution time.
    def reject(reason: RejectReason) -> ToolDeletionResult:
        return ToolDeletionResult(
            tool=tool,
            mode=mode,
            status=DeletionStatus.REJECTED,
            reject_reason=reason,
        )

    if tool.read_only:
        return reject(RejectReason.READ_ONLY)

    if tool.path.expanduser().is_symlink():
        return reject(RejectReason.SYMLINK)

    if target.name != tool.path.expanduser().name:
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
