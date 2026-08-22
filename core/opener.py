"""Argument-safe folder opening via xdg-open with fallback file managers.

Only directory paths that currently exist are ever launched, the resolved
absolute path is always passed as a single argv element (never through a
shell), and a real launcher failure is surfaced instead of being retried
silently on the next candidate.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

OPENERS = ("xdg-open", "nautilus", "dolphin", "thunar", "nemo")

OPENER_TIMEOUT_SECONDS = 10


class OpenStatus(StrEnum):
    """Outcome category for one folder-open attempt."""

    OPENED = "opened"
    MISSING_PATH = "missing_path"
    NO_OPENER = "no_opener"
    LAUNCH_FAILED = "launch_failed"


@dataclass(slots=True)
class OpenResult:
    """Outcome of opening one folder."""

    path: Path
    status: OpenStatus
    error: str | None = None


def can_open(path: Path) -> bool:
    """True when path is an existing directory and can be handed to an opener."""
    return path.is_dir()


def open_folder(path: Path) -> OpenResult:
    """Open path with the first available opener, returning a structured result."""
    resolved = path.expanduser().resolve(strict=False)
    if not resolved.is_dir():
        return OpenResult(path=resolved, status=OpenStatus.MISSING_PATH)
    candidates = [name for name in OPENERS if shutil.which(name)]
    if not candidates:
        return OpenResult(path=resolved, status=OpenStatus.NO_OPENER)
    for name in candidates:
        try:
            error = _run_opener([name, str(resolved)])
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired as exc:
            return OpenResult(path=resolved, status=OpenStatus.LAUNCH_FAILED, error=str(exc))
        if error is None:
            return OpenResult(path=resolved, status=OpenStatus.OPENED)
        return OpenResult(path=resolved, status=OpenStatus.LAUNCH_FAILED, error=error)
    return OpenResult(path=resolved, status=OpenStatus.NO_OPENER)


def _run_opener(argv: list[str]) -> str | None:
    """Run the opener argv list without a shell; None on success."""
    completed = subprocess.run(
        argv, check=False, timeout=OPENER_TIMEOUT_SECONDS, capture_output=True
    )
    if completed.returncode == 0:
        return None
    stderr = completed.stderr.decode(errors="replace").strip()
    return f"exit {completed.returncode}: {stderr}" if stderr else f"exit {completed.returncode}"
