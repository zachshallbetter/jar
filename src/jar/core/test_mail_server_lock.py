#!/usr/bin/env python3
# gate: hermetic   (temp store and lock; subprocesses of mail_server.py on a temp DB; no IMAP, no model)
"""#1411: the mail store's one-writer lock. The file is 0600 and names only the holder's command and pid; a second
writer waits and logs who it waited for and how long; the lock goes when the holder raises, is SIGTERM'd (systemd stop)
or SIGKILL'd; every writing command takes it and the read-only ones don't.
Run: python3 scripts/test_mail_server_lock.py
"""
from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailsrv-lock-"))
os.environ["MAIL_SERVER_DB"] = str(_TMP / "store" / "mail_server.db")
os.environ["MAIL_SERVER_LOCK"] = str(_TMP / "store" / "mail_server.db.lock")
os.environ["MAIL_SERVER"] = "0"
os.environ["LINKS_DB"] = str(_TMP / "links.db")
os.environ["LINK_SALT_FILE"] = str(_TMP / "link_salt")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import mail_server as M  # noqa: E402

LOCK = Path(os.environ["MAIL_SERVER_LOCK"])
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def free_now() -> bool:
    """Whether the lock can be taken at once (without waiting)."""
    import fcntl
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


HOLD = """
import sys, time
sys.path.insert(0, {here!r})
import mail_server as M
with M.store_lock("sync"):
    print("held", flush=True)
    time.sleep(60)
"""


def holder() -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", HOLD.format(here=str(HERE))], stdout=subprocess.PIPE, text=True,
                         env=os.environ.copy())
    assert p.stdout is not None and p.stdout.readline().strip() == "held"
    return p


print("── the file ──")
with M.store_lock("sync") as lk:
    check("created 0600 in the store's own directory", (mode(LOCK), LOCK.parent == Path(os.environ["MAIL_SERVER_DB"]).parent),
          (0o600, True))
    check("it names the holder's command and pid only", LOCK.read_text(), f"sync {os.getpid()}\n")
    check("a second one can't take it meanwhile", free_now(), False)
check("released on exit, and emptied", (free_now(), LOCK.read_text()), (True, ""))
os.chmod(LOCK, 0o644)
with M.store_lock("rethread"):
    pass
check("an existing 0644 file (the live one) becomes 0600", mode(LOCK), 0o600)
check("the default path is the store's own, beside the DB", str(M.lock_path()), os.environ["MAIL_SERVER_LOCK"])

print("── a second writer waits, and says for whom and how long ──")
got = threading.Event()


def first():
    with M.store_lock("sync"):
        got.set()
        time.sleep(1.5)


t = threading.Thread(target=first)
t.start()
got.wait(5)
lines = []
t0 = time.monotonic()
with M.store_lock("hooks-backfill", log=lines.append) as lk:
    waited = time.monotonic() - t0
t.join()
check("it waited for the holder", waited >= 1.0, True)
check("two lines: waiting (for sync, its pid), then got it after Ns",
      (len(lines), lines[0], lines[1].startswith("[mail_server] hooks-backfill: got the store lock after "),
       lines[1].endswith(f"(it was held by sync, pid {os.getpid()})")),
      (2, f"[mail_server] hooks-backfill: waiting for the store lock (held by sync, pid {os.getpid()})", True, True))
check("…the seconds are whole and real", 1 <= round(lk.waited) <= 3, True)
LOCK.write_text("--password=hunter2 /etc/x\n")   # never echoed: only a known command name is
lines = []
import fcntl  # noqa: E402
fd = os.open(LOCK, os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
threading.Timer(0.5, lambda: os.close(fd)).start()
with M.store_lock("sync", log=lines.append):
    pass
check("a holder whose file says anything else is just \"another run\" (nothing from the file echoed)",
      ("hunter2" in " ".join(lines), lines[0].endswith("(held by another run)")), (False, True))

print("── released however the holder ends ──")
try:
    with M.store_lock("sync"):
        raise RuntimeError("boom")
except RuntimeError:
    pass
check("an exception", free_now(), True)
for sig, name in ((signal.SIGTERM, "SIGTERM (systemd stop)"), (signal.SIGKILL, "SIGKILL")):
    p = holder()
    check(f"{name}: held while it runs", free_now(), False)
    p.send_signal(sig)
    p.wait(10)
    check(f"{name}: free the moment it's gone", free_now(), True)

print("── every writing command takes it; read-only ones don't ──")
for cmd in ("rethread", "rederive"):
    p = holder()
    w = subprocess.Popen([sys.executable, str(HERE / "mail_server.py"), cmd], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=os.environ.copy())
    time.sleep(1.5)
    check(f"{cmd} waits while a sync holds it", w.poll(), None)
    p.terminate()
    p.wait(10)
    out, err = w.communicate(timeout=30)
    check(f"…then runs (rc 0), its wait in the log", (w.returncode, f"{cmd}: waiting for the store lock (held by sync" in err),
          (0, True))
p = holder()
for cmd in (["status"], ["passes-status"]):
    r = subprocess.run([sys.executable, str(HERE / "mail_server.py"), *cmd], capture_output=True, text=True, timeout=20,
                       env=os.environ.copy())
    check(f"{cmd[0]} (read-only) answers while a sync holds it", (r.returncode, "store lock" in r.stderr), (0, False))
p.terminate()
p.wait(10)
check("the writers are the store's writing commands", sorted(M.WRITERS),
      ["hooks-backfill", "redact-store", "rederive", "rethread", "sync"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
