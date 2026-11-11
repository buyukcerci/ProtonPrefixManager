"""QRunnable workers moving core pipelines off the UI thread.

Each worker carries the pipeline epoch it was started under; the window
ignores signals from stale epochs instead of killing running workers.
Signal payloads cross threads as plain Python objects via Signal(object).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from PySide6.QtCore import QObject, QRunnable, Signal

from core.deletion import (
    DeleteMode,
    DeletionResult,
    DeletionStatus,
    FailureKind,
    RejectReason,
    ShaderDeletionResult,
    ToolDeletionResult,
    delete_tools,
    iter_delete_prefixes,
    iter_delete_shader_caches,
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
from core.shaders import ShaderCache, enumerate_shader_caches
from core.toolmap import contributing_roots, load_tool_mapping
from core.tools import Tool, used_by

_logger = logging.getLogger(__name__)


class _HasPath(Protocol):
    @property
    def path(self) -> Path: ...


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


class ShaderEnumerationSignals(QObject):
    finished = Signal(object, int)


class ShaderEnumerationWorker(QRunnable):
    """Lists shader caches off the UI thread.

    Enumeration reads every cache directory, its manifest and each root's
    shortcuts.vdf, which is too slow for the GUI thread on large
    libraries. Emits (caches, warnings); on an unexpected error the
    payload is empty so the window still leaves its loading state.
    """

    def __init__(
        self, libraries: Sequence[Library], roots: Sequence[SteamRoot], epoch: int
    ) -> None:
        super().__init__()
        self._libraries = list(libraries)
        self._roots = list(roots)
        self.epoch = epoch
        self.signals = ShaderEnumerationSignals()

    def run(self) -> None:
        payload: tuple[list[Any], list[Any]] = ([], [])
        try:
            caches, warnings = enumerate_shader_caches(self._libraries, self._roots)
            payload = (list(caches), list(warnings))
        except Exception as exc:
            _logger.error("shader enumeration worker failed: %s", exc)
        finally:
            self.signals.finished.emit(payload, self.epoch)


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


_ItemT = TypeVar("_ItemT", Prefix, ShaderCache)
_ResultT = TypeVar("_ResultT", DeletionResult, ShaderDeletionResult)


class _StreamingDeletionWorker(QRunnable, Generic[_ItemT, _ResultT]):
    """Shared run loop for the prefix and shader cache deletion workers.

    Subclasses supply the core iterator, how to read the deleted record
    off a result, and how to build a synthetic FAILED result. The iterator
    is looked up when run() executes, not bound at class definition, so a
    module attribute patched after import is the one that runs.
    """

    _log_label = "deletion worker"

    def __init__(
        self,
        items: Sequence[_ItemT],
        libraries: Sequence[Library],
        mode: DeleteMode,
        epoch: int,
    ) -> None:
        super().__init__()
        self._items: list[_ItemT] = list(items)
        self._libraries = list(libraries)
        self._mode = mode
        self.epoch = epoch
        self._emitted_targets: set[str] = set()
        self.signals = DeletionSignals()

    def _iter_results(self) -> Iterable[_ResultT]:
        raise NotImplementedError

    def _record_of(self, result: _ResultT) -> _ItemT:
        raise NotImplementedError

    def _failed_result(self, item: _ItemT, exc: Exception) -> _ResultT:
        raise NotImplementedError

    def run(self) -> None:
        """Stream one result per target; finished always emits.

        Results come from the core iterator as each target is handled, so
        when the batch raises partway through, only targets that were not
        reported yet get a synthetic FAILED result; targets already deleted
        keep their DELETED result. Aliases resolving to a reported target
        are not reported again, matching the core functions' grouping.
        """
        try:
            for result in self._iter_results():
                self._emitted_targets.add(_target_key(self._record_of(result)))
                self.signals.result_ready.emit(result, self.epoch)
        except Exception as exc:
            _logger.error("%s failed: %s", self._log_label, exc)
            for item in self._items:
                key = _target_key(item)
                if key in self._emitted_targets:
                    continue
                self._emitted_targets.add(key)
                self.signals.result_ready.emit(self._failed_result(item, exc), self.epoch)
        finally:
            self.signals.finished.emit(self.epoch)


class DeletionWorker(_StreamingDeletionWorker[Prefix, DeletionResult]):
    """Runs iter_delete_prefixes and streams one event per target."""

    def __init__(
        self,
        prefixes: Sequence[Prefix],
        libraries: Sequence[Library],
        mode: DeleteMode,
        epoch: int,
    ) -> None:
        super().__init__(prefixes, libraries, mode, epoch)

    def _iter_results(self) -> Iterable[DeletionResult]:
        return iter_delete_prefixes(self._items, self._libraries, self._mode)

    def _record_of(self, result: DeletionResult) -> Prefix:
        return result.prefix

    def _failed_result(self, item: Prefix, exc: Exception) -> DeletionResult:
        return DeletionResult(
            prefix=item,
            mode=self._mode,
            status=DeletionStatus.FAILED,
            failure_kind=FailureKind.OS,
            error=str(exc),
        )


class ShaderDeletionWorker(_StreamingDeletionWorker[ShaderCache, ShaderDeletionResult]):
    """Runs iter_delete_shader_caches and streams one event per target."""

    _log_label = "shader cache deletion worker"

    def __init__(
        self,
        caches: Sequence[ShaderCache],
        libraries: Sequence[Library],
        mode: DeleteMode,
        epoch: int,
    ) -> None:
        super().__init__(caches, libraries, mode, epoch)

    def _iter_results(self) -> Iterable[ShaderDeletionResult]:
        return iter_delete_shader_caches(self._items, self._libraries, self._mode)

    def _record_of(self, result: ShaderDeletionResult) -> ShaderCache:
        return result.cache

    def _failed_result(self, item: ShaderCache, exc: Exception) -> ShaderDeletionResult:
        return ShaderDeletionResult(
            cache=item,
            mode=self._mode,
            status=DeletionStatus.FAILED,
            failure_kind=FailureKind.OS,
            error=str(exc),
        )


def _target_key(item: _HasPath) -> str:
    """Resolved target path, the unit the delete functions group and report by."""
    try:
        return str(item.path.expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return str(item.path)


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


class SizeSignals(QObject):
    sized = Signal(object, int)
    finished = Signal(int)


class SizeWorker(QRunnable):
    """Walks each item's directory and reports one (path, size, error) event per item.

    Items only need a path, so tools and shader caches share this worker.
    """

    def __init__(
        self,
        items: Sequence[_HasPath],
        epoch: int,
        is_stale: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self._items = list(items)
        self.epoch = epoch
        self._is_stale = is_stale
        self.signals = SizeSignals()

    def run(self) -> None:
        try:
            for item in self._items:
                if self._is_stale is not None and self._is_stale():
                    break
                result = scan_prefix(item.path, should_stop=self._is_stale)
                if self._is_stale is not None and self._is_stale():
                    break
                self.signals.sized.emit(
                    (str(item.path), result.size_bytes, result.error), self.epoch
                )
        except Exception as exc:
            _logger.error("size worker failed: %s", exc)
        finally:
            self.signals.finished.emit(self.epoch)


# The Tools tab predates shader caches; keep its original name importable.
ToolSizeWorker = SizeWorker


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
