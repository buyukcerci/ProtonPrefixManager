"""Tests for the deletion confirmation dialogs (never executed modally)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog

from core.deletion import DeleteMode, DeletionResult, DeletionStatus, FailureKind, RejectReason
from core.models import Prefix, PrefixType
from ui.dialogs import (
    confirm_final,
    confirm_selection,
    selection_body,
    summary_body,
)


def _prefix(app_id: int = 480, name: str = "Game") -> Prefix:
    return Prefix(
        app_id=app_id,
        name=name,
        prefix_type=PrefixType.ORPHANED,
        path=Path(f"/tmp/{app_id}"),
        library="/tmp",
    )


def test_selection_body_lists_up_to_eight_names_then_overflow() -> None:
    names = [f"game-{i}" for i in range(10)]
    body = selection_body(names, "4.5 KB", None)
    for name in names[:8]:
        assert name in body
    assert "game-8" not in body and "and 2 more" in body


def test_selection_body_total_size_and_note() -> None:
    note = "Some sizes have not been scanned yet"
    with_note = selection_body(["one"], "12 B", note)
    without_note = selection_body(["one"], "12 B", None)
    assert "Total size: 12 B" in with_note
    assert note in with_note
    assert note not in without_note


def test_confirm_selection_accept_and_reject(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = iter(
        [
            QDialog.DialogCode.Accepted,
            QDialog.DialogCode.Rejected,
        ]
    )

    def fake_exec(self: QDialog) -> QDialog.DialogCode:
        return next(outcomes)

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    assert confirm_selection(None, ["a"], "1 B", None) is True
    assert confirm_selection(None, ["a"], "1 B", None) is False


def test_confirm_final_defaults_to_trash_on_accept(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_exec(self: QDialog) -> QDialog.DialogCode:
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    assert confirm_final(None, 2, "1.5 KB") is DeleteMode.TRASH


def test_confirm_final_permanent_when_selected(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtWidgets import QRadioButton

    def fake_exec(self: QDialog) -> QDialog.DialogCode:
        for radio in self.findChildren(QRadioButton):
            if radio.text() == "Delete permanently":
                radio.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    assert confirm_final(None, 1, "10 B") is DeleteMode.PERMANENT


def test_confirm_final_irreversibility_wording_present(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    texts: list[str] = []
    original_labels = []

    def fake_exec(self: QDialog) -> QDialog.DialogCode:
        from PySide6.QtWidgets import QLabel

        original_labels.extend(label.text() for label in self.findChildren(QLabel))
        texts.extend(original_labels)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    assert confirm_final(None, 3, "9 B") is None
    assert any("cannot be undone" in text for text in texts)


def test_summary_body_lists_every_non_deleted_result() -> None:
    deleted_prefix = _prefix(1, "Gone")
    rejected_prefix = _prefix(2, "Ghost")
    failed_prefix = _prefix(3, "Busy")
    results = [
        DeletionResult(prefix=deleted_prefix, mode=DeleteMode.TRASH, status=DeletionStatus.DELETED),
        DeletionResult(
            prefix=rejected_prefix,
            mode=DeleteMode.TRASH,
            status=DeletionStatus.REJECTED,
            reject_reason=RejectReason.MISSING,
        ),
        DeletionResult(
            prefix=failed_prefix,
            mode=DeleteMode.TRASH,
            status=DeletionStatus.FAILED,
            failure_kind=FailureKind.PERMISSION,
            error="Permission denied",
        ),
    ]
    body = summary_body(results)
    assert "1 of 3 target(s) removed" in body
    assert "[2] Ghost" in body and "missing" in body
    assert "[3] Busy" in body and "Permission denied" in body
    assert "[1] Gone" not in body.split("removed.")[0]
