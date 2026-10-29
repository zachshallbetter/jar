#!/usr/bin/env python3
# gate: hermetic   (temp files and temp SQLite databases only; kills only its own child processes)
"""Power-loss safety for a household box (titan-node#407).

- durable.atomic_write: the old file or the new one, whole, at every point: a failure before the rename leaves the old
  file and no temp; it fsyncs the file and the directory; it keeps the old file's permissions or takes `mode`.
- The household state writers (memory's persona and notes, device health's state, box setup's markers) use it, so none
  is rewritten in place.
- No store turns SQLite's own crash safety off (synchronous=OFF, journal_mode=OFF or MEMORY).
- Crash runs: a child writing in a loop is killed with SIGKILL at random moments, many times; every atomic file is one
  whole version, and every SQLite database (rollback journal, WAL with FULL, WAL with NORMAL) passes integrity_check.
  This is a PROCESS crash: the OS page cache survives a kill, so an un-fsynced write still lands. Power loss is covered
  by the audit (the fsyncs, and SQLite's journal left on), not by these runs.
Run: python3 scripts/test_power_safety.py
"""
from __future__ import annotations

import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import durable  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="titan-power-"))
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── atomic_write ──")
f = _TMP / "state.json"
durable.atomic_write(f, '{"v": 1}')
check("writes the file", f.read_text(), '{"v": 1}')
os.chmod(f, 0o600)
durable.atomic_write(f, '{"v": 2}')
check("replaces it, keeping its permissions", (f.read_text(), f.stat().st_mode & 0o777), ('{"v": 2}', 0o600))
durable.atomic_write(_TMP / "secret", b"k", mode=0o600)
check("a new file takes the mode given; bytes are written as is", ((_TMP / "secret").read_bytes(),
      (_TMP / "secret").stat().st_mode & 0o777), (b"k", 0o600))
UMASK_CHILD = r'''
import os, sys
os.umask(0o027)                      # a service's umask, set before durable is imported (as at a unit's start)
sys.path.insert(0, sys.argv[1])
import durable
new, old = sys.argv[2], sys.argv[3]
durable.atomic_write(new, "x")
durable.atomic_write(old, "y")
print(oct(os.stat(new).st_mode & 0o777), oct(os.stat(old).st_mode & 0o777))
'''
fresh, kept = _TMP / "umask-new.md", _TMP / "umask-old.md"
kept.write_text("x")
os.chmod(kept, 0o600)
got = subprocess.run([sys.executable, "-c", UMASK_CHILD, str(HERE), str(fresh), str(kept)], capture_output=True, text=True).stdout.split()
check("under umask 027 a new file is 0640 (as open() would make it, never a wider 0644); an existing 0600 stays 0600",
      got, ["0o640", "0o600"])
check("current_umask reads the umask without changing it", (durable.current_umask(), durable.current_umask()),
      (durable._UMASK, durable._UMASK) if not Path("/proc/self/status").exists() else (durable.current_umask(),) * 2)
_replace = os.replace
os.replace = lambda *a: (_ for _ in ()).throw(OSError("disk full"))
try:
    durable.atomic_write(f, '{"v": 3}')
    raised = False
except OSError:
    raised = True
os.replace = _replace
check("a failure before the rename: raised, the old file whole, no temp left behind",
      (raised, f.read_text(), [p.name for p in _TMP.iterdir() if p.name.endswith(".tmp")]), (True, '{"v": 2}', []))
synced: list[int] = []
_fsync = os.fsync
os.fsync = lambda fd: synced.append(fd) or _fsync(fd)
durable.atomic_write(f, '{"v": 4}')
os.fsync = _fsync
check("it fsyncs the file and then its directory", len(synced) >= 2, True)

print("── the household state writers use it ──")
for name in ("agent_memory.py", "device_health.py", "box_setup.py"):
    src = (HERE / name).read_text()
    check(f"{name}: no in-place rewrite (write_text), atomic_write instead",
          ("write_text(" in src, "durable.atomic_write(" in src), (False, True))

print("── no store turns SQLite's crash safety off ──")
bad = []
for p in sorted(HERE.glob("*.py")):
    if p.name.startswith("test_"):
        continue
    for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
        if re.search(r"synchronous\s*=\s*(OFF|0)\b|journal_mode\s*=\s*(OFF|MEMORY)\b", line, re.I):
            bad.append(f"{p.name}:{i}")
check("no synchronous=OFF and no journal_mode=OFF/MEMORY in any script", bad, [])
wal = [p.name for p in HERE.glob("*.py") if not p.name.startswith("test_") and "journal_mode=WAL" in p.read_text(errors="replace")]
check("the WAL stores are the known ones (synchronous left FULL, or NORMAL: no corruption, at most the last commit)",
      sorted(wal), ["agent_memory.py", "hooks.py", "household_identity.py", "world.py"])

print("── crash runs (SIGKILL at random moments; a process crash, not a power cut) ──")
KILLS = int(os.environ.get("POWER_SAFETY_KILLS", "15"))
CHILD_FILE = r'''
import sys
sys.path.insert(0, sys.argv[1])
import durable
path, i = sys.argv[2], 0
while True:
    i += 1
    durable.atomic_write(path, (("A" if i % 2 else "B") * 200000) + "\n")
'''
CHILD_DB = r'''
import sqlite3, sys
path, journal, sync = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(path)
con.execute(f"PRAGMA journal_mode={journal}")
con.execute(f"PRAGMA synchronous={sync}")
con.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, body TEXT, n INTEGER)")
con.commit()
i = 0
while True:
    i += 1
    with con:
        con.executemany("INSERT INTO t (body, n) VALUES (?, ?)", [("x" * 500, i)] * 20)
        con.execute("UPDATE t SET n = n + 1 WHERE id % 7 = ?", (i % 7,))
        if i % 5 == 0:
            con.execute("DELETE FROM t WHERE id IN (SELECT id FROM t ORDER BY id LIMIT 10)")
'''
rng = random.Random(407)


def crash(argv: list[str]) -> None:
    p = subprocess.Popen([sys.executable, "-c", *argv], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(rng.uniform(0.15, 0.6))
    p.send_signal(signal.SIGKILL)
    p.wait()


af = _TMP / "crash.txt"
versions = set()
for _ in range(KILLS):
    crash([CHILD_FILE, str(HERE), str(af)])
    t = af.read_text() if af.exists() else ""
    versions.add("whole" if t in ("A" * 200000 + "\n", "B" * 200000 + "\n") else "torn" if t else "missing-or-empty")
check(f"atomic file, {KILLS} kills: always one whole version", versions, {"whole"})
check("…and no temp files pile up beyond the one a kill can strand",
      len([p for p in _TMP.iterdir() if p.name.startswith(".crash.txt.")]) <= KILLS, True)

for journal, sync in (("DELETE", "FULL"), ("WAL", "FULL"), ("WAL", "NORMAL")):
    db = _TMP / f"crash-{journal}-{sync}.db"
    results = set()
    for _ in range(KILLS):
        crash([CHILD_DB, str(db), journal, sync])
        con = sqlite3.connect(db)
        results.add(con.execute("PRAGMA integrity_check").fetchone()[0])
        con.close()
    check(f"SQLite {journal}/{sync}, {KILLS} kills: integrity_check ok every time", results, {"ok"})

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
