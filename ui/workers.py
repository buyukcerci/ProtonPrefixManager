"""QRunnable workers moving core pipelines off the UI thread.

Each worker carries the pipeline epoch it was started under; the window
ignores signals from stale epochs instead of killing running workers.
Signal payloads cross threads as plain Python objects via Signal(object).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from core.discovery import discover
from core.enumeration import enumerate_from_discovery
from core.models import Prefix
from core.opener import open_folder
from core.scanner import scan_prefixes


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
