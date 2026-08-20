# Proton Prefix Manager

A lightweight desktop utility to list, inspect, and safely remove Proton (Wine)
prefixes created by Steam — reclaiming disk space that accumulates silently under
`steamapps/compatdata/`.

This is a work in progress.

## Development

Requires Python 3.11+ (developed on 3.14; PySide6 ships `abi3` wheels).

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/python main.py                      # run the app
.venv/bin/pytest                              # run the test suite

.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy core/ ui/ main.py
```

The `core/` package must never import PySide6 — enforced by
`tests/test_core_no_qt.py`.
