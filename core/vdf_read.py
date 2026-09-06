"""Bounded, no-follow reads for untrusted VDF files.

Shared by the tool enumeration and the tool mapping loaders. Both read
files that can be planted or corrupted by an untrusted install, so a
symlink at path itself must never be followed, special files must never
block the caller, and content must never be pulled into memory without
a bound. The no-follow guarantee covers path itself only; intermediate
directories are not checked, so a symlinked intermediate directory can
still redirect the read. The regular-file and size checks apply to
whatever the open lands on.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

# Real VDF files handled here are a few kilobytes. The cap keeps a
# planted multi-gigabyte file from being slurped into memory; oversized
# content is discarded and the caller treats the read as failed.
MAX_VDF_BYTES = 1_048_576


def read_vdf_bytes_bounded(path: Path, max_bytes: int) -> bytes | None:
    """Read at most max_bytes of path without following a symlink at path.

    The open is no-follow and non-blocking, so a symlink or FIFO planted
    at path can neither bypass the size cap nor block the caller.
    O_NOFOLLOW protects the final path component only; intermediate
    directories are not checked, so a symlinked intermediate directory
    can still redirect the open. The descriptor is fstat'ed after the
    open and anything that is not a regular file is rejected, so FIFOs
    and device files never reach the read loop, and the size cap applies
    to whatever the open landed on. O_NONBLOCK is a no-op for regular
    files, so ordinary reads are unaffected. The read is capped: at most
    max_bytes plus one 65536-byte chunk is pulled into memory before the
    content is discarded as oversized. O_NOFOLLOW exists on Linux, which
    is the only platform this project targets.

    OSError raised by the open itself propagates so the caller can tell
    a missing file apart from an unreadable one. Every failure after the
    open returns None. The descriptor is always closed and close errors
    are swallowed.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > max_bytes:
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def read_vdf_bounded(path: Path, max_bytes: int) -> str | None:
    """Read at most max_bytes of path without following a symlink at path.

    The open is no-follow and non-blocking, so a symlink or FIFO planted
    at path can neither bypass the size cap nor block the caller.
    O_NOFOLLOW protects the final path component only; intermediate
    directories are not checked, so a symlinked intermediate directory
    can still redirect the open. The descriptor is fstat'ed after the
    open and anything that is not a regular file is rejected, so FIFOs
    and device files never reach the read loop, and the size cap applies
    to whatever the open landed on. O_NONBLOCK is a no-op for regular
    files, so ordinary reads are unaffected. The read is capped: at most
    max_bytes plus one 65536-byte chunk is pulled into memory before the
    content is discarded as oversized. O_NOFOLLOW exists on Linux, which
    is the only platform this project targets.

    OSError raised by the open itself propagates so the caller can tell
    a missing file apart from an unreadable one. Every failure after the
    open, except decoding, returns None. Content that is not valid UTF-8
    raises UnicodeDecodeError instead, so callers can report a decoding
    failure distinctly from an unreadable or oversized file. The
    descriptor is always closed and close errors are swallowed.
    """
    data = read_vdf_bytes_bounded(path, max_bytes)
    if data is None:
        return None
    return data.decode("utf-8-sig")
