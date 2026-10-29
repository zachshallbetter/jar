#!/usr/bin/env python3
# gate: hermetic
"""titan-decider hands its freed load-time CPU heap back to the OS (titan-node#1544), without torch or a GPU:
  - VmRSS/VmSwap parse from a /proc/self/status-shaped file; a missing file gives {};
  - malloc_trim is a no-op off Linux, calls libc malloc_trim(0) on Linux, and never raises
    (libc without malloc_trim, a CDLL that fails to load, a trim that throws);
  - DECIDER_TRIM=0 skips the collect and the trim and prints nothing; otherwise one line, numbers only.
torch is replaced by an empty stub module before the import, so the real torch is never loaded.
Run: TZ=UTC python3 scripts/test_decider_trim.py
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.modules["torch"] = types.ModuleType("torch")   # the helpers never touch torch
import decider_server as D  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("status parsing")
with tempfile.TemporaryDirectory() as tmp:
    status = Path(tmp) / "status"
    status.write_text("Name:\tpython\nVmPeak:\t 9999 kB\nVmRSS:\t 1240000 kB\nVmData:\t 5 kB\nVmSwap:\t 2270000 kB\n")
    check("VmRSS and VmSwap only", D._mem_kb(str(status)), {"VmRSS": 1240000, "VmSwap": 2270000})
    status.write_text("Name:\tpython\nVmRSS:\t 12 kB\n")
    check("no VmSwap line -> just VmRSS", D._mem_kb(str(status)), {"VmRSS": 12})
    check("missing file -> {}", D._mem_kb(str(Path(tmp) / "nope")), {})

print("malloc_trim")
with mock.patch.object(D.sys, "platform", "darwin"), mock.patch.object(D.ctypes, "CDLL") as cdll:
    check("non-Linux: no trim", D._malloc_trim(), False)
    check("non-Linux: libc never loaded", cdll.call_count, 0)

libc = mock.Mock()
libc.malloc_trim.return_value = 1
with mock.patch.object(D.sys, "platform", "linux"), mock.patch.object(D.ctypes, "CDLL", return_value=libc):
    check("Linux: trimmed", D._malloc_trim(), True)
    check("Linux: malloc_trim(0)", libc.malloc_trim.call_args, mock.call(0))

no_trim = types.SimpleNamespace()   # musl-like libc: no malloc_trim attribute
with mock.patch.object(D.sys, "platform", "linux"), mock.patch.object(D.ctypes, "CDLL", return_value=no_trim):
    check("libc without malloc_trim -> False", D._malloc_trim(), False)
with mock.patch.object(D.sys, "platform", "linux"), mock.patch.object(D.ctypes, "CDLL", side_effect=OSError("no libc")):
    check("libc fails to load -> False", D._malloc_trim(), False)
boom = mock.Mock()
boom.malloc_trim.side_effect = RuntimeError("boom")
with mock.patch.object(D.sys, "platform", "linux"), mock.patch.object(D.ctypes, "CDLL", return_value=boom):
    check("trim raising -> False", D._malloc_trim(), False)

print("release after load")
mem = [{"VmRSS": 1240 * 1024, "VmSwap": 2270 * 1024}, {"VmRSS": 300 * 1024, "VmSwap": 0}]
with mock.patch.object(D, "_mem_kb", side_effect=lambda: mem.pop(0)), \
        mock.patch.object(D, "_malloc_trim", return_value=True) as trim, \
        mock.patch.object(D.gc, "collect") as collect:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D._release_host_memory({})
    check("collect ran", collect.call_count, 1)
    check("trim ran", trim.call_count, 1)
    check("one numbers-only line",
          buf.getvalue(), "[decider] host memory MB: rss 1240 -> 300, swap 2270 -> 0, trimmed=1\n")

with mock.patch.object(D, "_mem_kb", return_value={}), mock.patch.object(D, "_malloc_trim", return_value=False):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D._release_host_memory({})
    check("no /proc -> '?' not a crash",
          buf.getvalue(), "[decider] host memory MB: rss ? -> ?, swap ? -> ?, trimmed=0\n")

with mock.patch.object(D, "_malloc_trim") as trim, mock.patch.object(D.gc, "collect") as collect:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D._release_host_memory({"DECIDER_TRIM": "0"})
    check("DECIDER_TRIM=0: no trim", trim.call_count, 0)
    check("DECIDER_TRIM=0: no collect", collect.call_count, 0)
    check("DECIDER_TRIM=0: silent", buf.getvalue(), "")

check("real torch never imported", getattr(sys.modules["torch"], "__file__", None), None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
