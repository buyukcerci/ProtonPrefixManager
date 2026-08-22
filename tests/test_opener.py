"""Unit tests for core.opener (subprocess boundary fully mocked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core import opener as opener_module
from core.opener import OpenStatus, can_open, open_folder


@pytest.fixture()
def which(monkeypatch: pytest.MonkeyPatch):
    registry: dict[str, str] = {}

    def lookup(name: str) -> str | None:
        return registry.get(name)

    monkeypatch.setattr(opener_module.shutil, "which", lookup)
    return registry


@pytest.fixture()
def run_opener(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    outcomes: list[Exception | str | None] = [None]

    def fake_run(argv: list[str]) -> str | None:
        calls.append(argv)
        outcome = outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(opener_module, "_run_opener", fake_run)
    return calls, outcomes


def test_launches_xdg_open_with_exact_argv(tmp_path: Path, which, run_opener) -> None:
    calls, _ = run_opener
    which["xdg-open"] = "/usr/bin/xdg-open"
    target = tmp_path / "prefix"
    target.mkdir()
    result = open_folder(target)
    assert result.status is OpenStatus.OPENED
    assert result.error is None
    assert calls == [["xdg-open", str(target.resolve())]]


def test_relative_path_resolved_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which, run_opener
) -> None:
    calls, _ = run_opener
    which["xdg-open"] = "/usr/bin/xdg-open"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "folder").mkdir()
    result = open_folder(Path("folder"))
    assert result.status is OpenStatus.OPENED
    assert calls == [["xdg-open", str(tmp_path / "folder")]]


def test_missing_path_not_launched(tmp_path: Path, run_opener) -> None:
    calls, _ = run_opener
    result = open_folder(tmp_path / "vanished")
    assert result.status is OpenStatus.MISSING_PATH
    assert result.path == (tmp_path / "vanished").resolve()
    assert calls == []


def test_no_opener_available(tmp_path: Path, which, run_opener) -> None:
    _, _ = run_opener
    target = tmp_path / "prefix"
    target.mkdir()
    result = open_folder(target)
    assert result.status is OpenStatus.NO_OPENER


def test_fallback_to_second_opener(tmp_path: Path, which, run_opener) -> None:
    calls, _ = run_opener
    which["nautilus"] = "/usr/bin/nautilus"
    target = tmp_path / "prefix"
    target.mkdir()
    result = open_folder(target)
    assert result.status is OpenStatus.OPENED
    assert calls == [["nautilus", str(target.resolve())]]


def test_vanished_binary_tries_next_candidate(
    tmp_path: Path, which, monkeypatch: pytest.MonkeyPatch
) -> None:
    which["xdg-open"] = "/usr/bin/xdg-open"
    which["dolphin"] = "/usr/bin/dolphin"
    calls: list[list[str]] = []

    def seam(argv: list[str]) -> str | None:
        calls.append(argv)
        if argv[0] == "xdg-open":
            raise FileNotFoundError(2, "No such file or directory")
        return None

    monkeypatch.setattr(opener_module, "_run_opener", seam)
    result = open_folder(tmp_path)
    assert result.status is OpenStatus.OPENED
    assert [argv[0] for argv in calls] == ["xdg-open", "dolphin"]


def test_nonzero_exit_is_launch_failed(tmp_path: Path, which, run_opener) -> None:
    _, outcomes = run_opener
    which["xdg-open"] = "/usr/bin/xdg-open"
    outcomes[0] = "exit 1: cannot open"
    result = open_folder(tmp_path)
    assert result.status is OpenStatus.LAUNCH_FAILED
    assert result.error == "exit 1: cannot open"


def test_timeout_is_launch_failed(tmp_path: Path, which, run_opener) -> None:
    _, outcomes = run_opener
    which["xdg-open"] = "/usr/bin/xdg-open"
    outcomes[0] = subprocess.TimeoutExpired(cmd="xdg-open", timeout=10)
    result = open_folder(tmp_path)
    assert result.status is OpenStatus.LAUNCH_FAILED
    assert result.error is not None


def test_can_open_true_false(tmp_path: Path) -> None:
    directory = tmp_path / "dir"
    directory.mkdir()
    assert can_open(directory) is True
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    assert can_open(file_path) is False
    assert can_open(tmp_path / "missing") is False


def test_run_opener_seam_real_subprocess_success() -> None:
    assert opener_module._run_opener(["true"]) is None


def test_run_opener_seam_real_subprocess_failure() -> None:
    error = opener_module._run_opener(["false"])
    assert error is not None and "exit 1" in error
