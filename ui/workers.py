"""QRunnable workers moving core pipelines off the UI thread.

Each worker carries the pipeline epoch it was started under; the window
ignores signals from stale epochs instead of killing running workers.
Signal payloads cross threads as plain Python objects via Signal(object).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal

from core.deletion import (
    DeleteMode,
    DeletionResult,
    DeletionStatus,
    FailureKind,
    RejectReason,
    ToolDeletionResult,
    delete_tools,
    iter_delete_prefixes,
)
from core.discovery import (
    DiscoveryError,
    DiscoveryErrorKind,
    DiscoveryResult,
    Library,
    SteamRoot,
    discover,
)
from core.enumeration import enumerate_from_discovery
from core.models import Prefix
from core.opener import OpenResult, OpenStatus, open_folder
from core.scanner import scan_prefix, scan_prefixes
from core.toolmap import contributing_roots, load_tool_mapping
from core.tools import Tool, used_by

_logger = logging.getLogger(__name__)


class DiscoverySignals(QObject):
    finished = Signal(object, int)


class DiscoveryWorker(QRunnable):
    """Runs discovery plus enumeration and reports the combined payload."""

    def __init__(self, custom_roots: Sequence[str], epoch: int) -> None:
        super().__init__()
        self._custom_roots = list(custom_roots)
        self.epoch = epoch
        self.signals = DiscoverySignals()

    def run(self) -> None:
        result = DiscoveryResult()
        prefixes: list[Prefix] = []
        try:
            result = discover(custom_roots=self._custom_roots)
            prefixes = enumerate_from_discovery(result)
        except Exception as exc:
            _logger.error("discovery worker failed: %s", exc)
            kind = (
                DiscoveryErrorKind.PERMISSION
                if isinstance(exc, PermissionError)
                else DiscoveryErrorKind.CRASH
            )
            result.errors.append(DiscoveryError(kind=kind, message=str(exc), path=None))
        finally:
            self.signals.finished.emit((result, prefixes), self.epoch)


class ScanSignals(QObject):
    scan_event = Signal(object, int)
    finished = Signal(int)


class ScanWorker(QRunnable):
    """Streams scan events for the given prefixes.

    The cache passed in is only read (as a snapshot); persisting results
    is the receiver's job, done on the GUI thread from the events.
    """

    def __init__(
        self,
        prefixes: Sequence[Prefix],
        cache: dict[str, dict],
        epoch: int,
        is_stale: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self._prefixes = list(prefixes)
        self._cache = cache
        self.epoch = epoch
        self._is_stale = is_stale
        self.signals = ScanSignals()

    def run(self) -> None:
        try:
            stop = self._is_stale
            for event in scan_prefixes(self._prefixes, self._cache, should_stop=stop):
                if stop is not None and stop():
                    break
                self.signals.scan_event.emit(event, self.epoch)
        except Exception as exc:
            _logger.error("scan worker failed: %s", exc)
        finally:
            self.signals.finished.emit(self.epoch)


class DeletionSignals(QObject):
    result_ready = Signal(object, int)
    finished = Signal(int)


class DeletionWorker(QRunnable):
    """Runs delete_prefixes and streams one event per target."""

    def __init__(
        self,
        prefixes: Sequence[Prefix],
        libraries: Sequence[Library],
        mode: DeleteMode,
        epoch: int,
    ) -> None:
        super().__init__()
        self._prefixes = list(prefixes)
        self._libraries = list(libraries)
        self._mode = mode
        self.epoch = epoch
        self._emitted_targets: set[str] = set()
        self.signals = DeletionSignals()

    def run(self) -> None:
        """Stream one result per target; finished always emits.

        Results come from iter_delete_prefixes as each target is handled,
        so when the batch raises partway through, only targets that were
        not reported yet get a synthetic FAILED result; targets already
        deleted keep their DELETED result. Aliases resolving to a reported
        target are not reported again, matching delete_prefixes' grouping.
        """
        try:
            for result in iter_delete_prefixes(self._prefixes, self._libraries, self._mode):
                self._emitted_targets.add(_target_key(result.prefix))
                self.signals.result_ready.emit(result, self.epoch)
        except Exception as exc:
            _logger.error("deletion worker failed: %s", exc)
            for prefix in self._prefixes:
                key = _target_key(prefix)
                if key in self._emitted_targets:
                    continue
                self._emitted_targets.add(key)
                self.signals.result_ready.emit(
                    DeletionResult(
                        prefix=prefix,
                        mode=self._mode,
                        status=DeletionStatus.FAILED,
                        failure_kind=FailureKind.OS,
                        error=str(exc),
                    ),
                    self.epoch,
                )
        finally:
            self.signals.finished.emit(self.epoch)


def _target_key(prefix: Prefix) -> str:
    """Resolved target path, the unit delete_prefixes groups and reports by."""
    try:
        return str(prefix.path.expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return str(prefix.path)


class OpenFolderSignals(QObject):
    finished = Signal(object)


class OpenFolderWorker(QRunnable):
    """Opens one folder off-thread; open_folder may block up to its timeout."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self.signals = OpenFolderSignals()

    def run(self) -> None:
        result = OpenResult(path=self._path, status=OpenStatus.LAUNCH_FAILED)
        try:
            result = open_folder(self._path)
        except Exception as exc:
            _logger.error("open folder worker failed: %s", exc)
            result = OpenResult(
                path=self._path,
                status=OpenStatus.LAUNCH_FAILED,
                error=str(exc),
            )
        finally:
            self.signals.finished.emit(result)


class ToolSizeSignals(QObject):
    sized = Signal(object, int)
    finished = Signal(int)


class ToolSizeWorker(QRunnable):
    """Walks each tool directory and reports one event per tool."""

    def __init__(
        self,
        tools: Sequence[Tool],
        epoch: int,
        is_stale: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self._tools = list(tools)
        self.epoch = epoch
        self._is_stale = is_stale
        self.signals = ToolSizeSignals()

    def run(self) -> None:
        try:
            for tool in self._tools:
                if self._is_stale is not None and self._is_stale():
                    break
                result = scan_prefix(tool.path, should_stop=self._is_stale)
                if self._is_stale is not None and self._is_stale():
                    break
                self.signals.sized.emit(
                    (str(tool.path), result.size_bytes, result.error), self.epoch
                )
        except Exception as exc:
            _logger.error("tool size worker failed: %s", exc)
        finally:
            self.signals.finished.emit(self.epoch)


class ToolDeletionSignals(QObject):
    result_ready = Signal(object, int)
    finished = Signal(int)


class ToolDeletionWorker(QRunnable):
    """Runs delete_tools and streams one event per target.

    The usage mapping is reloaded here at run time instead of trusting a
    used set captured when the click handler ran, so a tool selected by a
    game between the click and this run is still detected. A failed
    reload fails the batch closed: nothing is deleted and every target is
    reported as rejected with MAPPING_UNAVAILABLE. This fail-closed rule
    also applies when the reloaded mapping shrank compared to the scan-time
    mapping, or when the set of contributing roots shrank, preventing a
    vanished config from silently unlocking tools. The finished signal
    always emits, including when the batch raises unexpectedly; each
    target not yet reported is then reported as a failed result so the
    window never stays in the deleting state and no target is double-listed.
    """

    def __init__(
        self,
        tools: Sequence[Tool],
        roots: Sequence[SteamRoot | Path],
        mode: DeleteMode,
        epoch: int,
        libraries: Sequence[Library] = (),
        scan_mapping: Mapping[int, str] | None = None,
        scan_contributing_roots: Collection[Any] | None = None,
    ) -> None:
        super().__init__()
        self._tools = list(tools)
        self._roots = list(roots)
        self._mode = mode
        self.epoch = epoch
        self._libraries = list(libraries)
        self._scan_mapping = dict(scan_mapping) if scan_mapping is not None else None
        self._scan_contributing_roots = (
            {
                Path(str(r.path if hasattr(r, "path") else r)).resolve(strict=False)
                for r in scan_contributing_roots
            }
            if scan_contributing_roots is not None
            else None
        )
        self._emitted_paths: set[Path] = set()
        self.signals = ToolDeletionSignals()

    def run(self) -> None:
        try:
            self._run_batch()
        except Exception as exc:
            _logger.error("tool deletion batch failed: %s", exc)
            for tool in self._tools:
                if tool.path in self._emitted_paths:
                    continue
                self.signals.result_ready.emit(
                    ToolDeletionResult(
                        tool=tool,
                        mode=self._mode,
                        status=DeletionStatus.FAILED,
                        failure_kind=FailureKind.OS,
                        error=str(exc),
                    ),
                    self.epoch,
                )
                self._emitted_paths.add(tool.path)
        finally:
            self.signals.finished.emit(self.epoch)

    def _run_batch(self) -> None:
        mapping, mapping_errors = load_tool_mapping(self._roots)
        shrunk = False
        if self._scan_mapping is not None and any(
            app_id not in mapping for app_id in self._scan_mapping
        ):
            shrunk = True
        if self._scan_contributing_roots is not None:
            current_roots = contributing_roots(self._roots)
            if not self._scan_contributing_roots.issubset(current_roots):
                shrunk = True
        if mapping_errors or shrunk:
            for tool in self._tools:
                if tool.path in self._emitted_paths:
                    continue
                self.signals.result_ready.emit(
                    ToolDeletionResult(
                        tool=tool,
                        mode=self._mode,
                        status=DeletionStatus.REJECTED,
                        reject_reason=RejectReason.MAPPING_UNAVAILABLE,
                    ),
                    self.epoch,
                )
                self._emitted_paths.add(tool.path)
            return
        used_paths = set(used_by(self._tools, mapping))
        for result in delete_tools(
            self._tools, self._roots, used_paths, self._mode, libraries=self._libraries
        ):
            self.signals.result_ready.emit(result, self.epoch)
            self._emitted_paths.add(result.tool.path)
