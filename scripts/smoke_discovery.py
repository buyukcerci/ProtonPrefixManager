"""Smoke-test discovery against the local Steam installation."""

from __future__ import annotations

import sys

from core.discovery import discover


def main() -> int:
    result = discover(custom_roots=sys.argv[1:])

    print(f"roots ({len(result.roots)}):")
    for root in result.roots:
        print(f"  [{root.source.value}] {root.path}")

    print(f"libraries ({len(result.libraries)}):")
    for lib in result.libraries:
        print(f"  {lib.path} (root: {lib.root})")

    print(f"errors ({len(result.errors)}):")
    for error in result.errors:
        location = f" @ {error.path}" if error.path else ""
        print(f"  [{error.kind.value}] {error.message}{location}")

    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
