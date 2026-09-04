"""QRunnable workers moving core pipelines off the UI thread.

Each worker carries the pipeline epoch it was started under; the window
ignores signals from stale epochs instead of killing running workers.
Signal payloads cross threads as plain Python objects via Signal(object).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from core.deletion import DeleteMode, delete_prefixes, delete_tools
from core.discovery import Library, SteamRoot, discover
from core.enumeration import enumerate_from_discovery
from core.models import Prefix
from core.opener import open_folder
from core.scanner import scan_prefix, scan_prefixes
from core.tools import Tool


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
        result = discover(custom_roots=self._custom_roots)
        prefixes = enumerate_from_discovery(result)
        self.signals.finished.emit((result, prefixes), self.epoch)


class ScanSignals(QObject):
    scan_event = Signal(object, int)
    finished = Signal(int)


class ScanWorker(QRunnable):
    """Streams scan events for the given prefixes, mutating the cache dict."""

    def __init__(
        self,
        prefixes: Sequence[Prefix],
        cache: dict[str, dict],
        epoch: int,
    ) -> None:
        super().__init__()
        self._prefixes = list(prefixes)
        self._cache = cache
        self.epoch = epoch
        self.signals = ScanSignals()

    def run(self) -> None:
        for event in scan_prefixes(self._prefixes, self._cache):
            self.signals.scan_event.emit(event, self.epoch)
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
        self.signals = DeletionSignals()

    def run(self) -> None:
        for result in delete_prefixes(self._prefixes, self._libraries, self._mode):
            self.signals.result_ready.emit(result, self.epoch)
        self.signals.finished.emit(self.epoch)


class OpenFolderSignals(QObject):
    finished = Signal(object)


class OpenFolderWorker(QRunnable):
    """Opens one folder off-thread; open_folder may block up to its timeout."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self.signals = OpenFolderSignals()

    def run(self) -> None:
        self.signals.finished.emit(open_folder(self._path))


class ToolSizeSignals(QObject):
    sized = Signal(object, int)
    finished = Signal(int)


class ToolSizeWorker(QRunnable):
    """Walks each tool directory and reports one event per tool."""

    def __init__(self, tools: Sequence[Tool], epoch: int) -> None:
        super().__init__()
        self._tools = list(tools)
        self.epoch = epoch
        self.signals = ToolSizeSignals()

    def run(self) -> None:
        for tool in self._tools:
            result = scan_prefix(tool.path)
            self.signals.sized.emit((str(tool.path), result.size_bytes, result.error), self.epoch)
        self.signals.finished.emit(self.epoch)


class ToolDeletionSignals(QObject):
    result_ready = Signal(object, int)
    finished = Signal(int)


class ToolDeletionWorker(QRunnable):
    """Runs delete_tools and streams one event per target."""

    def __init__(
        self,
        tools: Sequence[Tool],
        roots: Sequence[SteamRoot | Path],
        mode: DeleteMode,
        epoch: int,
    ) -> None:
        super().__init__()
        self._tools = list(tools)
        self._roots = list(roots)
        self._mode = mode
        self.epoch = epoch
        self.signals = ToolDeletionSignals()

    def run(self) -> None:
        for result in delete_tools(self._tools, self._roots, self._mode):
            self.signals.result_ready.emit(result, self.epoch)
        self.signals.finished.emit(self.epoch)
