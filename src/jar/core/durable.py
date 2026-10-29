#!/usr/bin/env python3
"""Writes that survive a power cut (titan-node#407: a household box loses power; titan rarely does).

A file rewritten in place (open "w", Path.write_text) is truncated first and filled after: a power cut between the two
leaves it empty or half-written. `atomic_write` writes a temp file in the same directory, fsyncs it, renames it over
the target (atomic on POSIX), then fsyncs the directory so the rename itself is on disk. A cut at any point leaves either
the old file whole or the new one whole.

SQLite needs nothing from here: its own journal makes each transaction atomic, as long as no store turns that off.
test_power_safety.py checks that no store turns SQLite's sync or journal off, and that the household state writers
(memory, device health, box setup) use atomic_write.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _umask_at_import() -> int:
    m = os.umask(0)   # the only way to read it portably; once, at import, before any thread writes files
    os.umask(m)
    return m


_UMASK = _umask_at_import()


def current_umask() -> int:
    """The process umask now, read without changing it (Linux: /proc/self/status), else the one at import. Never set
    to 0 and back on a live process: another thread creating a file meanwhile would get no umask at all."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError):
        pass
    return _UMASK


def atomic_write(path: str | os.PathLike, data: str | bytes, mode: int | None = None, encoding: str = "utf-8") -> None:
    """Replace `path` with `data` atomically and durably. `mode`: the file's permissions (default: the old file's, else
    what open() would give a new file, 0o666 less the umask: a service under umask 027 gets 0640, never a wider 0644);
    a secret passes 0o600. The temp file never outlives a failure."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = p.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o666 & ~current_umask()
    raw = data.encode(encoding) if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(p.parent)


def fsync_dir(d: str | os.PathLike) -> None:
    """Make a rename or a new file in `d` durable (a no-op where directories can't be opened, e.g. some platforms)."""
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
