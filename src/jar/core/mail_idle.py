#!/usr/bin/env python3
"""Mail pushed, not polled (#955's design: "IMAP IDLE … with a polling fallback"): one read-only IDLE per IMAP account
(iCloud, other IMAP), and new mail in INBOX starts that account's sync (mail_server.py sync --account) within a minute.
titan-mail-server.timer's 30-minute run stays the fallback. Gmail and Outlook keep polling: their push needs an inbound
webhook or Pub/Sub, and titan takes no inbound connections.

Off by default: MAIL_IDLE=1 (Coordination, 10-09: kept off until #955's parallel week has its readout, so the recount
isn't confounded by a change in inflow timing).

Read-only, like the connector: the connection is mail_server's _ReadOnly (EXAMINE, never SELECT; no STORE, COPY, MOVE,
EXPUNGE, APPEND) plus IDLE and DONE, and nothing else. One connection per account, held by a lock, so a second
instance leaves that account alone. IDLE is renewed every RENEW_S (25 min), inside the 29 minutes RFC 2177 lets a
server drop it. A dropped connection comes back after at least BACKOFF_MIN_S, doubling to BACKOFF_MAX_S; a refused
sign-in doubles from 60 s too, and after AUTH_STOP refusals in a row that account stops and its owner (never the admin
on their behalf) gets one card. Credentials are the account's own 0600 file (mail_server.load_account); nothing here
prints them, an address, a subject, a sender or a folder other than INBOX. The log names an account by a short hash.

  python3 scripts/mail_idle.py run        watch every IMAP account until stopped (the titan-mail-idle unit)
  python3 scripts/mail_idle.py status     the accounts it would watch, by hash, and why any is skipped (counts only)
"""
from __future__ import annotations

import fcntl
import hashlib
import imaplib
import os
import select
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_server as M  # noqa: E402

RENEW_S = 25 * 60
BACKOFF_MIN_S = 60.0
BACKOFF_MAX_S = 30 * 60.0
AUTH_STOP = 3
SYNC_GAP_S = 60.0          # at most one sync an account a minute, however much mail arrives
SETTLE_S = 15.0            # a burst of mail lands before its sync starts
LOCK_DIR = Path(os.environ.get("MAIL_IDLE_LOCK_DIR", "/ai/datasets/connectors/mail_idle"))
PUSH_PROVIDERS = frozenset({"gmail", "outlook"})   # these poll; their push needs inbound connections


def enabled() -> bool:
    return os.environ.get("MAIL_IDLE", "0") == "1"


def tag(name: str) -> str:
    """An account in the log: a short hash of its file name, never the name (it can hold a person's name)."""
    return "acct#" + hashlib.sha256(name.encode()).hexdigest()[:8]


def say(line: str) -> None:
    print(f"[mail_idle] {line}", file=sys.stderr, flush=True)


class AuthFailed(Exception):
    pass


class Idler(M._ReadOnly):
    """mail_server's read-only connection plus IDLE (RFC 2177). `wait(step)` says whether a line can be read without
    blocking (the real one: SSL's own buffer, then select); the tests pass their own."""

    def __init__(self, imap: Any, wait: Callable[[float], bool] | None = None):
        super().__init__(imap)
        self._wait = wait or self._readable

    def _readable(self, step: float) -> bool:
        sock = self._m.sock
        if getattr(sock, "pending", lambda: 0)():
            return True
        return bool(select.select([sock], [], [], step)[0])

    def login(self, user: str, password: str) -> None:
        try:
            self._m.login(user, password)
        except imaplib.IMAP4.error as exc:
            raise AuthFailed("the server refused the sign-in") from exc

    def idle(self, renew_s: float, stop: threading.Event, step: float = 5.0) -> bool:
        """One IDLE: True when the server says INBOX has new mail (EXISTS), False at renew_s or on stop. Always ends with
        DONE and the tagged reply, so the connection is ready for the next one."""
        m = self._m
        t = m._new_tag()
        m.send(t + b" IDLE\r\n")
        first = m.readline()
        if not first.startswith(b"+"):
            raise imaplib.IMAP4.error("the server didn't accept IDLE")
        got, end = False, time.monotonic() + renew_s
        while not got and not stop.is_set() and time.monotonic() < end:
            if not self._wait(min(step, max(0.0, end - time.monotonic()))):
                continue
            line = m.readline()
            if not line:
                raise OSError("the connection closed")
            got = line.startswith(b"* ") and line.rstrip().upper().endswith(b" EXISTS")
        m.send(b"DONE\r\n")
        while True:
            line = m.readline()
            if not line:
                raise OSError("the connection closed")
            if line.startswith(t + b" "):
                if not line[len(t) + 1:].upper().startswith(b"OK"):
                    raise imaplib.IMAP4.error("IDLE ended badly")
                return got
            if line.startswith(b"* ") and line.rstrip().upper().endswith(b" EXISTS"):
                got = True


class _Unbuffered:
    """An unbuffered reader, so select() sees every byte the server sent and IDLE waits on select, never on a read
    timeout. The socket keeps its 60 s timeout for ordinary replies: one that times out means the connection is gone,
    and watch() reconnects. This connection never fetches a body."""

    def open(self, host: str = "", port: int = 0, timeout: float | None = None) -> None:
        super().open(host, port, timeout)   # type: ignore[misc]
        self.file = self.sock.makefile("rb", buffering=0)   # type: ignore[attr-defined]


class _IdleIMAP(_Unbuffered, imaplib.IMAP4_SSL):
    """IMAP over TLS for IDLE (_Unbuffered)."""


def connect_idle(acct: dict[str, Any]) -> Idler:
    imap = _IdleIMAP(acct["host"], acct["port"], ssl_context=ssl.create_default_context(), timeout=60)
    return Idler(imap)


def _sync(name: str) -> None:
    """That account's ordinary sync (the passes, the caps and the local model's turn all as the timer's)."""
    subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "mail_server.py"), "sync", "--account", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1500, check=False)


class Trigger:
    """New mail → one sync, coalesced: a burst settles SETTLE_S, and syncs are SYNC_GAP_S apart."""

    def __init__(self, name: str, run: Callable[[str], None] = _sync, settle: float = SETTLE_S, gap: float = SYNC_GAP_S):
        self.name, self._run, self.settle, self.gap = name, run, settle, gap
        self._ev, self._stop, self.runs, self._last = threading.Event(), threading.Event(), 0, -1e9
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def fire(self) -> None:
        self._ev.set()

    def close(self) -> None:
        self._stop.set()
        self._ev.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._ev.wait()
            if self._stop.is_set():
                return
            self._stop.wait(max(self.settle, self._last + self.gap - time.monotonic()))
            self._ev.clear()
            if self._stop.is_set():
                return
            self._last = time.monotonic()
            try:
                self._run(self.name)
                self.runs += 1
            except Exception as exc:  # noqa: BLE001 - the next mail or the timer tries again
                say(f"{tag(self.name)}: a sync failed ({type(exc).__name__})")


def _card_owner(acct: dict[str, Any]) -> None:
    """Sign-in refused AUTH_STOP times: the account's owner gets one card (keyed, so never twice)."""
    try:
        import proactive
        proactive.add_card("Mail stopped updating", "titan couldn't sign in to one of your mail accounts, so new mail "
                           "waits for the half-hourly check. Its app-specific password may need replacing.",
                           kind="info", urgency=1, source="mail", to=[acct["owner"]],
                           key=f"mail-idle-auth-{tag(acct['name'])}")
    except Exception as exc:  # noqa: BLE001
        say(f"{tag(acct['name'])}: the owner's card failed ({type(exc).__name__})")


def watch(acct: dict[str, Any], stop: threading.Event, *, connect: Callable[[dict[str, Any]], Idler] = connect_idle,
          trigger: Trigger | None = None, sleep: Callable[[float], bool] | None = None,
          renew_s: float = RENEW_S, card: Callable[[dict[str, Any]], None] = _card_owner) -> str:
    """One account until stop: → "stopped" | "auth" (gave up after AUTH_STOP refused sign-ins)."""
    sleep = sleep or stop.wait
    trig = trigger or Trigger(acct["name"])
    backoff, auth_fails = BACKOFF_MIN_S, 0
    try:
        while not stop.is_set():
            conn = None
            try:
                conn = connect(acct)
                conn.login(acct["user"], acct["password"])
                auth_fails = 0
                conn.examine("INBOX")
                backoff = BACKOFF_MIN_S
                while not stop.is_set():
                    if conn.idle(renew_s, stop):
                        trig.fire()
            except AuthFailed:
                auth_fails += 1
                if auth_fails >= AUTH_STOP:
                    say(f"{tag(acct['name'])}: sign-in refused {auth_fails} times; stopped, the owner is told")
                    card(acct)
                    return "auth"
                wait = min(BACKOFF_MIN_S * 2 ** auth_fails, BACKOFF_MAX_S)
                say(f"{tag(acct['name'])}: sign-in refused; again in {int(wait)} s")
                sleep(wait)
            except (OSError, imaplib.IMAP4.error, EOFError) as exc:
                say(f"{tag(acct['name'])}: connection lost ({type(exc).__name__}); again in {int(backoff)} s")
                sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
            finally:
                if conn is not None:
                    conn.logout()
        return "stopped"
    finally:
        if trigger is None:
            trig.close()


def _lock(name: str, lock_dir: Path | None = None) -> Any:
    """This account's lock, or None when another instance holds it (one connection per account)."""
    d = lock_dir or LOCK_DIR
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / (tag(name)[5:] + ".lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def watchable(directory: Path | None = None, role_of: Callable[[str], str | None] = M._role_of
              ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The IMAP accounts to watch, and counts of the skipped ones by reason (an unreadable or refused file, a kid's
    mailbox, a provider that polls)."""
    d = directory or M.ACCOUNTS_DIR
    out: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            acct = M.load_account(p, role_of)
        except M.AccountError:
            skipped["refused"] = skipped.get("refused", 0) + 1
            continue
        if acct.get("provider") in PUSH_PROVIDERS:
            skipped["polls"] = skipped.get("polls", 0) + 1
            continue
        out.append(acct)
    return out, skipped


def run(stop: threading.Event | None = None, **kw: Any) -> int:
    stop = stop or threading.Event()
    accts, skipped = watchable(kw.get("directory"), kw.get("role_of", M._role_of))
    locks, threads = [], []
    for a in accts:
        lk = _lock(a["name"], kw.get("lock_dir"))
        if lk is None:
            skipped["locked"] = skipped.get("locked", 0) + 1
            continue
        locks.append(lk)
        th = threading.Thread(target=watch, args=(a, stop), kwargs={k: v for k, v in kw.items()
                                                                    if k in ("connect", "renew_s", "card", "trigger")},
                              daemon=True)
        th.start()
        threads.append(th)
    say(f"watching {len(threads)} account(s); skipped {sum(skipped.values())} {dict(sorted(skipped.items()))}")
    for th in threads:
        th.join()
    for lk in locks:
        lk.close()
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        accts, skipped = watchable()
        print({"on": enabled(), "watch": [tag(a["name"]) for a in accts], "skipped": skipped})
        return 0
    if cmd != "run":
        print(__doc__.split("\n\n")[-1], file=sys.stderr)
        return 2
    if not enabled():
        say("MAIL_IDLE isn't on; nothing to watch")
        return 0
    if not M.enabled():
        say("MAIL_SERVER isn't on; nothing to watch")
        return 0
    stop = threading.Event()
    import signal
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return run(stop)


if __name__ == "__main__":
    sys.exit(main())
