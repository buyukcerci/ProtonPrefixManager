"""Minimal application window."""

from __future__ import annotations

from PySide6.QtWidgets import QMainWindow


class MainWindow(QMainWindow):
    """Root window placeholder for the prefix table."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Proton Prefix Manager")
        self.resize(900, 600)
