"""Argument-safe folder opening via xdg-open with fallback file managers.

Only directory paths that currently exist are ever launched, the resolved
absolute path is always passed as a single argv element (never through a
shell), and a real launcher failure (the opener ran and exited non-zero or
timed out) is surfaced instead of being retried silently on the next
candidate. An opener that cannot even be executed counts as unavailable,
so the next candidate is tried.
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
    INACCESSIBLE = "inaccessible"
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
    try:
        return path.is_dir()
    except OSError:
        return False


def open_folder(path: Path) -> OpenResult:
    """Open path with the first available opener, returning a structured result."""
    try:
        resolved = path.expanduser().resolve(strict=False)
        if not resolved.is_dir():
            return OpenResult(path=resolved, status=OpenStatus.MISSING_PATH)
    except FileNotFoundError:
        return OpenResult(path=path, status=OpenStatus.MISSING_PATH)
    except (OSError, RuntimeError) as exc:
        # RuntimeError: Python < 3.13 raises it from resolve() on a symlink loop.
        return OpenResult(path=path, status=OpenStatus.INACCESSIBLE, error=str(exc))
    candidates = [name for name in OPENERS if shutil.which(name)]
    if not candidates:
        return OpenResult(path=resolved, status=OpenStatus.NO_OPENER)
    exec_error: str | None = None
    for name in candidates:
        try:
            error = _run_opener([name, str(resolved)])
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired as exc:
            return OpenResult(path=resolved, status=OpenStatus.LAUNCH_FAILED, error=str(exc))
        except OSError as exc:
            # Could not exec this opener (EACCES, ENOEXEC, ...); try the next one.
            exec_error = f"{name}: {exc}"
            continue
        if error is None:
            return OpenResult(path=resolved, status=OpenStatus.OPENED)
        return OpenResult(path=resolved, status=OpenStatus.LAUNCH_FAILED, error=error)
    if exec_error is not None:
        return OpenResult(path=resolved, status=OpenStatus.LAUNCH_FAILED, error=exec_error)
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
