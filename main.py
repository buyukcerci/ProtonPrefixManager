"""Proton Prefix Manager entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from core.config import load_config
from ui.main_window import MainWindow
from ui.styles import apply_app_style


def main() -> int:
    app = QApplication(sys.argv)
    apply_app_style(app, load_config())
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
