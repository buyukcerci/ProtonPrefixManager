"""Confirmation dialogs for the deletion flow.

Each function builds its dialog, runs it modally, and returns a plain
value. Tests never execute dialogs; they assert on constructed content and
drive decisions by stubbing QDialog.exec at the class level.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from core.deletion import DeleteMode, DeletionResult, DeletionStatus
from core.models import Prefix, ScanStatus

MAX_LISTED_NAMES = 8


def selection_body(names: Sequence[str], total_text: str, unscanned_note: str | None) -> str:
    """Shared body text for confirm_selection, exposed for content tests."""
    listed = list(names[:MAX_LISTED_NAMES])
    overflow = len(names) - len(listed)
    lines = ["The following prefixes will be removed:", ""]
    lines.extend(listed)
    if overflow > 0:
        lines.append(f"and {overflow} more")
    lines.extend(["", f"Total size: {total_text}"])
    if unscanned_note:
        lines.append(unscanned_note)
    lines.extend(["", "Proceed?"])
    return "\n".join(lines)


def confirm_selection(
    parent: QWidget | None,
    names: Sequence[str],
    total_text: str,
    unscanned_note: str | None,
) -> bool:
    """First confirmation: what will be removed and how big it is."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Review selection")
    layout = QVBoxLayout(dialog)
    label = QLabel(selection_body(names, total_text, unscanned_note))
    label.setWordWrap(True)
    layout.addWidget(label)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.button(QDialogButtonBox.StandardButton.Yes).setDefault(True)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    result = dialog.exec()
    return result == QDialog.DialogCode.Accepted


def _radio_mode(trash: QRadioButton, permanent: QRadioButton) -> DeleteMode:
    return DeleteMode.PERMANENT if permanent.isChecked() else DeleteMode.TRASH


def confirm_final(parent: QWidget | None, count: int, size_text: str) -> DeleteMode | None:
    """Second confirmation: irreversibility wording plus the mode choice."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Confirm deletion")
    layout = QVBoxLayout(dialog)
    warning = QLabel(
        f"This cannot be undone.\n\n"
        f"{count} prefix(es) totaling {size_text} will be removed.\n"
        "Choose how they should be removed:"
    )
    warning.setWordWrap(True)
    layout.addWidget(warning)
    trash_radio = QRadioButton("Move to trash (recommended)")
    permanent_radio = QRadioButton("Delete permanently")
    trash_radio.setChecked(True)
    layout.addWidget(trash_radio)
    layout.addWidget(permanent_radio)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return _radio_mode(trash_radio, permanent_radio)


def summary_body(results: Sequence[DeletionResult]) -> str:
    """Shared body text for show_deletion_summary, exposed for content tests."""
    deleted = sum(1 for r in results if r.status is DeletionStatus.DELETED)
    problems = [r for r in results if r.status is not DeletionStatus.DELETED]
    lines = [
        f"Deletion finished: {deleted} of {len(results)} target(s) removed.",
    ]
    if problems:
        lines.append("")
        lines.append("Targets that were not removed:")
        for problem in problems:
            prefix = problem.prefix
            header = f"[{prefix.app_id}] {prefix.name}"
            if problem.status is DeletionStatus.REJECTED:
                reason = problem.reject_reason.value if problem.reject_reason else "rejected"
                lines.append(f"  {header}: rejected ({reason})")
            else:
                kind = problem.failure_kind.value if problem.failure_kind else "failed"
                detail = problem.error or "no details available"
                lines.append(f"  {header}: failed ({kind}): {detail}")
    return "\n".join(lines)


def show_deletion_summary(parent: QWidget | None, results: Sequence[DeletionResult]) -> None:
    """Informational summary of one deletion batch; failures are listed."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Deletion summary")
    layout = QVBoxLayout(dialog)
    label = QLabel(summary_body(results))
    label.setWordWrap(True)
    layout.addWidget(label)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    dialog.exec()


def unscanned_note_for(prefixes: Sequence[Prefix]) -> str | None:
    """Note shown when any selected prefix has not been scanned yet."""
    unscanned = [p for p in prefixes if p.scan_status is not ScanStatus.SCANNED]
    if not unscanned:
        return None
    return "Some sizes have not been scanned yet; the total may be understated."
