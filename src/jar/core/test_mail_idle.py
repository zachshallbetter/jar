#!/usr/bin/env python3
# gate: hermetic   (a scripted fake IMAP server; temp account files, locks and stores; nothing connects anywhere)
"""mail_idle.py (#955: IMAP IDLE with the timer as the fallback). IDLE and DONE on the read-only connection; SELECT,
STORE, EXPUNGE, COPY, MOVE and APPEND refused by the wrapper itself (nothing reaches the server); renewal inside 29
minutes; one connection per account (a lock); reconnect backoff of at least 60 s, doubling; sign-in refusals doubling
from 60 s and stopping after AUTH_STOP with one card to the account's owner; new mail coalesced into one sync a minute;
kids' mailboxes and polling providers skipped; the log names accounts by hash only; off unless MAIL_IDLE=1; and
mail_server's sync lock (a second sync waits). Fictional people only.
Run: python3 scripts/test_mail_idle.py
"""
from __future__ import annotations

import contextlib
import fcntl
import imaplib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mail-idle-"))
for k, v in {"MAIL_SERVER_DB": "mail_server.db", "MAIL_ACCOUNTS_DIR": "accounts", "MAIL_IDLE_LOCK_DIR": "locks",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_DB": "household.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("MAIL_IDLE", None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mail_idle as I  # noqa: E402
import mail_server as M  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


class FakeIMAP:
    """A scripted server: `lines` it will send (released by `feed`), every command it received in `got`."""

    def __init__(self, login_ok=True, idle_ok=True):
        self.got: list = []
        self.lines: list[bytes] = []
        self.login_ok, self.idle_ok, self.n = login_ok, idle_ok, 0
        self.lock = threading.Lock()

    def _new_tag(self):
        self.n += 1
        return f"T{self.n}".encode()

    def send(self, data):
        self.got.append(data)
        if data.endswith(b" IDLE\r\n"):
            self.idle_tag = data.split()[0]
            self.feed(b"+ idling\r\n" if self.idle_ok else self.idle_tag + b" BAD no idle\r\n")
        elif data == b"DONE\r\n":
            self.feed(self.idle_tag + b" OK IDLE terminated\r\n")

    def feed(self, *lines):
        with self.lock:
            self.lines.extend(lines)

    def readline(self):
        for _ in range(400):
            with self.lock:
                if self.lines:
                    return self.lines.pop(0)
            time.sleep(0.005)
        return b""

    def wait(self, step):
        end = time.monotonic() + step
        while time.monotonic() < end:
            with self.lock:
                if self.lines:
                    return True
            time.sleep(0.01)
        return False

    def login(self, user, password):
        self.got.append(("LOGIN",))
        if not self.login_ok:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] nope")

    def select(self, folder, readonly=False):
        self.got.append(("SELECT", folder, readonly))
        return "OK", [b"3"]

    def response(self, code):
        return code, [b"42"]

    def logout(self):
        self.got.append(("LOGOUT",))


def idler(fake):
    return I.Idler(fake, wait=fake.wait)


print("── IDLE on the read-only connection ──")
f = FakeIMAP()
c = idler(f)
stop = threading.Event()
threading.Timer(0.2, lambda: f.feed(b"* 4 EXISTS\r\n")).start()
check("new mail (EXISTS) ends the IDLE with True", c.idle(5, stop, step=0.05), True)
check("…IDLE, then DONE, nothing else sent", [x for x in f.got if isinstance(x, bytes)], [b"T1 IDLE\r\n", b"DONE\r\n"])
f2 = FakeIMAP()
t0 = time.monotonic()
check("no mail by the renewal: False, and DONE sent", (idler(f2).idle(0.3, stop, step=0.05), f2.got[-1]), (False, b"DONE\r\n"))
check("…renewed on time", time.monotonic() - t0 < 2, True)
f3 = FakeIMAP()
s3 = threading.Event()
threading.Timer(0.15, s3.set).start()
check("stop ends an IDLE", idler(f3).idle(30, s3, step=0.05), False)
f4 = FakeIMAP()


def late_exists(data, orig=f4.send):
    orig(data)
    if data == b"DONE\r\n":
        with f4.lock:
            f4.lines.insert(0, b"* 5 EXISTS\r\n")


f4.send = late_exists
check("EXISTS between DONE and the tagged OK still counts", idler(f4).idle(0.2, stop, step=0.05), True)
f5 = FakeIMAP(idle_ok=False)
try:
    idler(f5).idle(1, stop)
    got = "no error"
except imaplib.IMAP4.error:
    got = "error"
check("a server that refuses IDLE: an error (watch() reconnects later)", got, "error")
check("RENEW_S is inside RFC 2177's 29 minutes", I.RENEW_S < 29 * 60, True)

print("── the real reader: unbuffered socket, select, a local IMAP server ──")
import socket  # noqa: E402

srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
seen: list[bytes] = []


def serve():
    s_, _ = srv.accept()
    fh = s_.makefile("rb")
    s_.sendall(b"* OK [CAPABILITY IMAP4rev1 IDLE] ready\r\n")
    while True:
        line = fh.readline()
        if not line:
            return
        words = line.strip().split(b" ", 2)
        t_, cmd = (words[0], words[1].upper()) if len(words) > 1 else (b"", words[0].upper())
        seen.append(cmd)
        if cmd == b"CAPABILITY":
            s_.sendall(b"* CAPABILITY IMAP4rev1 IDLE\r\n" + t_ + b" OK done\r\n")
        elif cmd == b"LOGIN":
            s_.sendall(t_ + b" OK logged in\r\n")
        elif cmd == b"EXAMINE":
            s_.sendall(b"* 3 EXISTS\r\n* OK [UIDVALIDITY 42] ok\r\n" + t_ + b" OK [READ-ONLY] done\r\n")
        elif cmd == b"IDLE":
            idle_t = t_
            s_.sendall(b"+ idling\r\n")
            time.sleep(0.3)
            s_.sendall(b"* 4 EXISTS\r\n")
        elif cmd == b"DONE":
            s_.sendall(idle_t + b" OK IDLE terminated\r\n")
        elif cmd == b"LOGOUT":
            s_.sendall(b"* BYE\r\n" + t_ + b" OK bye\r\n")
            return
        else:
            s_.sendall(t_ + b" BAD no\r\n")


threading.Thread(target=serve, daemon=True).start()


class PlainIdle(I._Unbuffered, imaplib.IMAP4):
    pass


real = I.Idler(PlainIdle("127.0.0.1", srv.getsockname()[1], timeout=10))
real.login("robin", "not-real")
check("EXAMINE over a real socket gives UIDVALIDITY", real.examine("INBOX"), 42)
t0 = time.monotonic()
check("a real IDLE: the server's EXISTS arrives through select", real.idle(5, threading.Event(), step=0.1), True)
check("…promptly (not at the renewal)", time.monotonic() - t0 < 3, True)
real.logout()
check("the server saw CAPABILITY, LOGIN, EXAMINE, IDLE, DONE, LOGOUT and nothing else",
      [x.upper() for x in seen], [b"CAPABILITY", b"LOGIN", b"EXAMINE", b"IDLE", b"DONE", b"LOGOUT"])
srv.close()

print("── mailboxes stay EXAMINE; writes refused by the wrapper itself ──")
f6 = FakeIMAP()
c6 = idler(f6)
c6.examine("INBOX")
check("INBOX opened read-only (EXAMINE)", f6.got[-1], ("SELECT", '"INBOX"', True))
before = list(f6.got)
for verb in ("select", "store", "expunge", "copy", "move", "append", "uid", "delete"):
    try:
        getattr(c6, verb)("1", "x")
        res = "ran"
    except M.ReadOnlyViolation:
        res = "refused"
    check(f"{verb}: refused by the wrapper", res, "refused")
check("…and nothing reached the server", f6.got, before)

print("── watch(): sync on new mail, backoff, sign-in refusals ──")
ACCT = {"name": "robin-icloud", "owner": "robin", "user": "robin@example.invalid", "password": "app-pass-not-real",
        "host": "imap.example.invalid", "port": 993, "provider": "imap"}


class Fires:
    def __init__(self):
        self.n = 0

    def fire(self):
        self.n += 1

    def close(self):
        pass


def run_watch(connect, stop_after=None, **kw):
    st = threading.Event()
    sleeps: list[float] = []
    fires = Fires()

    def sleep(s):
        sleeps.append(s)
        if stop_after is not None and len(sleeps) >= stop_after:
            st.set()
        return st.is_set()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = I.watch(ACCT, st, connect=connect, trigger=fires, sleep=sleep, **kw)
    return out, sleeps, fires.n, err.getvalue()


fw = FakeIMAP()
st_w = threading.Event()


def connect_once(acct):
    threading.Timer(0.1, lambda: fw.feed(b"* 7 EXISTS\r\n")).start()
    threading.Timer(0.5, st_w.set).start()
    return idler(fw)


fires = Fires()
log = io.StringIO()
with contextlib.redirect_stderr(log):
    out = I.watch(ACCT, st_w, connect=connect_once, trigger=fires, renew_s=0.2)
check("new mail fires its account's sync; stop ends the watch", (out, fires.n >= 1), ("stopped", True))
check("…signed in, opened INBOX read-only, logged out", [x for x in fw.got if isinstance(x, tuple)],
      [("LOGIN",), ("SELECT", '"INBOX"', True), ("LOGOUT",)])

out, sleeps, n, log2 = run_watch(lambda a: (_ for _ in ()).throw(OSError("down")), stop_after=4)
check("a dropped connection: at least 60 s, doubling, capped", sleeps, [60.0, 120.0, 240.0, 480.0])
cards: list = []
out, sleeps, n, log3 = run_watch(lambda a: idler(FakeIMAP(login_ok=False)), card=cards.append)
check(f"sign-in refused {I.AUTH_STOP} times: stopped", out, "auth")
check("…waits doubled from 60 s between tries", sleeps, [120.0, 240.0])
check("…and one card, to the account's owner", [c_["owner"] for c_ in cards], ["robin"])
allogs = log.getvalue() + log2 + log3
check("the log names no account, address, password or host (a hash only)",
      [w for w in ("robin", "example.invalid", "app-pass", "imap.") if w in allogs], [])
check("…it does name the account by hash", I.tag("robin-icloud") in allogs, True)

print("── new mail coalesced: one sync a minute ──")
runs: list[float] = []
tr = I.Trigger("robin-icloud", run=lambda n_: runs.append(time.monotonic()), settle=0.1, gap=0.6)
t0 = time.monotonic()
for _ in range(5):
    tr.fire()
time.sleep(0.4)
check("a burst of five: one sync", len(runs), 1)
tr.fire()
time.sleep(0.9)
check("the next one waits out the gap", (len(runs), runs[1] - runs[0] >= 0.55 if len(runs) > 1 else None), (2, True))
tr.close()

print("── one connection per account; which accounts ──")
lk = I._lock("robin-icloud")
check("the first instance holds the account's lock", lk is not None, True)
check("…a second gets none", I._lock("robin-icloud"), None)
lk.close()
check("…and gets it once the first is gone", I._lock("robin-icloud") is not None, True)
d = _TMP / "accounts"
d.mkdir(exist_ok=True)


def account(name, body):
    p = d / f"{name}.json"
    p.write_text(json.dumps(body))
    p.chmod(0o600)


account("robin-icloud", {"owner": "robin", "provider": "icloud", "user": "robin@example.invalid", "password": "x"})
account("ava-icloud", {"owner": "ava", "provider": "icloud", "user": "ava@example.invalid", "password": "x"})
account("alex-gmail", {"owner": "alex", "provider": "gmail", "client_id": "c", "client_secret": "s"})
account("hale-imap", {"owner": "hale", "host": "mail.example.invalid", "port": 993, "user": "h", "password": "x"})
roles = {"robin": "adult", "ava": "kid", "alex": "admin", "hale": "adult"}.get
accts, skipped = I.watchable(d, roles)
check("IMAP accounts watched; a kid's refused; Gmail polls",
      ([a["name"] for a in accts], skipped), (["hale-imap", "robin-icloud"], {"polls": 1, "refused": 1}))

print("── off by default ──")
r = subprocess.run([sys.executable, str(HERE / "mail_idle.py"), "run"], capture_output=True, text=True, timeout=30,
                   env={**os.environ, "MAIL_IDLE": "0"})
check("MAIL_IDLE off: run exits at once, connecting to nothing", (r.returncode, "isn't on" in r.stderr), (0, True))

print("── mail_server: one sync at a time ──")
lockp = str(_TMP / "mail_server.db") + ".lock"
held = open(lockp, "a")
fcntl.flock(held, fcntl.LOCK_EX)
p = subprocess.Popen([sys.executable, str(HERE / "mail_server.py"), "sync"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env={**os.environ, "MAIL_SERVER": "0"})
time.sleep(1.5)
check("a second sync waits while one holds the lock", p.poll(), None)
held.close()
check("…and goes once it's free", p.wait(timeout=30) is not None, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
