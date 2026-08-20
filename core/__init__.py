"""Qt-free core package: discovery, parsing, classification, and deletion safety."""

from __future__ import annotations

from importlib import metadata

try:
    __version__ = metadata.version("proton-prefix-manager")
except metadata.PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["__version__"]
