"""Smoke-test size scanning and cache against the local Steam installation."""

from __future__ import annotations

from datetime import datetime

from core.discovery import discover
from core.enumeration import enumerate_from_discovery
from core.models import Prefix
from core.scanner import ScanEventKind, cache_key, scan_prefixes


def main() -> int:
    result = discover()
    prefixes = enumerate_from_discovery(result)
    print(f"prefixes found: {len(prefixes)}")
    if not prefixes:
        return 0

    cache: dict[str, dict] = {}
    walked_sizes, failures = _scan_pass(prefixes, cache, label="pass 1 (walk)")
    cached_sizes, cached_failures = _scan_pass(prefixes, cache, label="pass 2 (cache)")

    if walked_sizes != cached_sizes:
        print("MISMATCH between walked and cached sizes")
        return 1
    if failures or cached_failures:
        print(f"failures: {len(failures)} walked / {len(cached_failures)} cached")
    print(f"cache entries: {len(cache)}")
    return 1 if failures else 0


def _scan_pass(
    prefixes: list[Prefix],
    cache: dict[str, dict],
    *,
    label: str,
) -> tuple[dict[str, int], list[str]]:
    sizes: dict[str, int] = {}
    failures: list[str] = []
    start = datetime.now()
    for event in scan_prefixes(prefixes, cache):
        if event.kind is ScanEventKind.STARTED:
            continue
        prefix = event.prefix
        assert isinstance(prefix, Prefix)
        name = f"[{prefix.app_id}] {prefix.name}"
        if event.kind is ScanEventKind.COMPLETED:
            sizes[cache_key(prefix)] = prefix.size_bytes
            print(f"{label}: {name} = {prefix.size_bytes} bytes ({prefix.scan_status.value})")
        else:
            failures.append(name)
            print(f"{label}: {name} FAILED - {event.error}")
    elapsed = (datetime.now() - start).total_seconds()
    print(f"{label}: done in {elapsed:.2f}s")
    return sizes, failures


if __name__ == "__main__":
    raise SystemExit(main())
