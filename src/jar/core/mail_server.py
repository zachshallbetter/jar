#!/usr/bin/env python3
"""Mail read from the mail server itself (titan-node#955, step 1: iCloud IMAP, read-only, the admin's account).

Zach, 10-05: "the server is the store". titan keeps, per mail, its headers, its thread and its envelope (envelopes.py:
the summary, fences, digest), plus a reference to where the mail lives (account, folder, UIDVALIDITY/UID,
Message-ID). A body is fetched from the server just in time to derive the envelope from the ORIGINAL (#954: never
from a summary), then dropped: no table here has a body column, and nothing writes one to disk.

Read-only, by construction:
  - a folder is opened with EXAMINE (imaplib's select(readonly=True)), never SELECT;
  - every fetch is BODY.PEEK (no \\Seen flag set), never BODY[] or RFC822;
  - _ReadOnly lets through only LOGIN, EXAMINE, UID SEARCH, UID FETCH and LOGOUT. Anything else (STORE, COPY, MOVE,
    EXPUNGE, APPEND, ...) raises before it reaches the server. Archive/move is #955 step 5, with the confirmation
    floor and undo.

Accounts: one 0600 JSON file per account in MAIL_ACCOUNTS_DIR (default /ai/secrets/mail), which the person creates
themselves (the HA-token pattern): {"owner": "<person id>", "provider": "icloud", "user": "<address>",
"password": "<app-specific password>", "folders": ["INBOX"]}. It's never in an env file, argv or chat, and a file
anyone else can read is refused. Kids' mailboxes are refused.

Threads (#951): thread_id is a known parent's thread, else the first Message-ID in References, else In-Reply-To, else
the mail's own Message-ID. A bill's mails group by header, not by guessing.

What's kept out: a sign-in or verification code mail (mail_triage.is_code) keeps no subject and gets no envelope.

Off by default. MAIL_SERVER=1 lets `sync` connect. MAIL_SERVER_KB=1 also writes each personal or action thread's
envelope to the knowledge base (#446 v2, 10-09): one owner-only entry per thread (its subject, participants by
display name, dates, each mail's derived summary and claims), never a body and never a mail's own entry; a mail's
envelope (summary, world observations, fences: envelope_pass.envelope_one) stays in this store. A question about the
thread's own words fetches the body from the server on demand. Until the parallel week (#955 step 6) shows the counts
match the Mac's sync, envelopes stay in this store only, so the KB doesn't hold two copies of a mail.

Egress (#409): reading the person's own server is noted as `mail` (a no-op outside a turn; search_mail, step 4, runs
inside one).

  python3 scripts/mail_server.py sync [--account NAME]     headers, threads, then envelopes for new mail
  python3 scripts/mail_server.py status                   counts per account and folder (no subjects, no addresses)
  python3 scripts/mail_server.py cutover-check --owner ID  each server account names exactly one Mac account the ledger
                                                          has seen (MAIL_SERVER_MAC_ACCOUNT); exit 1 refuses the cut-over
  python3 scripts/mail_server.py passes-status [--hours 24]  the server passes' outcomes, counts only (#955)
  python3 scripts/mail_server.py rethread                 join linked mails already stored into their threads
  python3 scripts/mail_server.py hooks-backfill           link hooks (#951) for mail derived before they existed
  python3 scripts/mail_server.py hooks-backfill --revector [--limit N]   hooked mail with no topic vector, again
  python3 scripts/mail_server.py rederive [--days 90]      mark the window for derivation again (cut-over; no connection)
  python3 scripts/mail_server.py redact-store             mask stored subjects, senders and envelopes (#979; a 0600
                                                          backup first, shredded at 30 days)
"""
from __future__ import annotations

import argparse
import email
import email.message
import email.policy
import email.utils
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import sqlite3
import ssl
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

ACCOUNTS_DIR = Path(os.environ.get("MAIL_ACCOUNTS_DIR", "/ai/secrets/mail"))
# A person's standing keep rules (#955, Zach 10-10: a project's mail), 0600 (a looser file is ignored, with a log line),
# their own mailboxes only: {"<person id>": [{"phrases": [...], "threads": [...], "project": "<its name>", "facet": "work"}]}
# (one rule as a plain object too). A rule's project (the owner's own world-model project entity, by name, resolved on
# titan: no id is ever written down) and facet file its kept threads: filing only, never who sees them. Words, not sender domains: KB's 10-10
# counts showed a domain rule catching hundreds of unrelated mails for each one it should (2484 vs 4 on the biggest).
KEEP_FILE = Path(os.environ.get("MAIL_KEEP_FILE", "/ai/config/mail_keep.json"))
DB = Path(os.environ.get("MAIL_SERVER_DB", "/ai/datasets/connectors/mail_server.db"))
PROVIDERS = {"icloud": ("imap.mail.me.com", 993)}   # "gmail" goes through the Gmail API (mail_gmail.py)
HEADER_FIELDS = "MESSAGE-ID IN-REPLY-TO REFERENCES FROM SUBJECT DATE"
HEADERS_MAX = int(os.environ.get("MAIL_SERVER_HEADERS_MAX", "2000"))   # new headers per folder per run, newest first
DERIVE_MAX = int(os.environ.get("MAIL_SERVER_DERIVE_MAX", "50"))       # envelopes per run (the local model's time)
# Envelopes only for mail sent in the last N days (Coordination, 10-05): older mail keeps its headers and thread, and
# gets link hooks (no model) at HOOKS_MAX a run.
DERIVE_DAYS = float(os.environ.get("MAIL_SERVER_DERIVE_DAYS", "90"))
# The passes (triage, bills, suggestions) read only mail sent this recently (#955 cut-over, 10-09): older mail derives
# as before, with no card, so turning the passes on never queues weeks of old suggestions ahead of today's (the
# suggestion cards' own budget is mail_suggestions.PER_DAY a person a day).
PASSES_DAYS = float(os.environ.get("MAIL_SERVER_PASSES_DAYS", "14"))
HOOKS_MAX = int(os.environ.get("MAIL_SERVER_HOOKS_MAX", "200"))
BATCH = 100
BODY_MAX = 20000
SOURCE_TYPE = "connector:mail"
_ID_RX = re.compile(r"<[^<>\s@]+@[^<>\s@]+>")   # <local@domain>; "<>", "<@x>", a bare domain aren't ids


def enabled() -> bool:
    return os.environ.get("MAIL_SERVER", "0") == "1"


def kb_enabled() -> bool:
    return os.environ.get("MAIL_SERVER_KB", "0") == "1"


class AccountError(ValueError):
    """An account file that can't be used. The message names the file, never what's in it."""


# ── accounts ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _role_of(person: str) -> str | None:
    import household_identity as hh
    for p in hh.get_store().people():
        if str(p.get("id")) == person:
            return str(p.get("role") or "")
    return None


def env_folders(name: str, directory: Path | None = None) -> list[str] | None:
    """MAIL_SERVER_FOLDERS, not a secret: which folders this server account reads. "auto" (INBOX plus the archive and
    sent folders the server marks or names) or a comma list ("INBOX,Archive,Sent Messages"); "<server account>=<…>;…"
    for several; a bare value only when there's exactly one server account. None when unset or ambiguous."""
    raw = os.environ.get("MAIL_SERVER_FOLDERS", "").strip()
    if not raw:
        return None
    if "=" in raw:
        pairs = dict(p.split("=", 1) for p in raw.split(";") if "=" in p)
        val = str(pairs.get(name, "")).strip()
    else:
        d = directory or ACCOUNTS_DIR
        val = raw if d.is_dir() and len(list(d.glob("*.json"))) == 1 else ""
    if not val:
        return None
    if val.lower() == "auto":
        return ["auto"]
    out = [" ".join(f.split()) for f in val.split(",") if f.strip()]
    return out or None


def env_mac_account(name: str, directory: Path | None = None) -> str:
    """MAIL_SERVER_MAC_ACCOUNT, not a secret: the Mac's mail account name (as the ledger's mail items carry it, e.g.
    "iCloud") this server account replaces. "<server account>=<Mac name>[,…]" for several; a bare "<Mac name>" only
    when there's exactly one server account. "" when unset or ambiguous."""
    raw = os.environ.get("MAIL_SERVER_MAC_ACCOUNT", "").strip()
    if not raw:
        return ""
    if "=" in raw:
        pairs = dict(p.split("=", 1) for p in raw.split(",") if "=" in p)
        return " ".join(str(pairs.get(name, "")).split())
    d = directory or ACCOUNTS_DIR
    return " ".join(raw.split()) if d.is_dir() and len(list(d.glob("*.json"))) == 1 else ""


def cutover_check(owner: str, directory: Path | None = None, ledger: Path | None = None,
                  role_of: Callable[[str], str | None] = _role_of) -> tuple[bool, list[str]]:
    """Before MAIL_SERVER_REPLACES_MAC=1: each of the owner's server accounts names exactly one Mac mail account the
    ledger has seen for them (case and spaces aside). → (ok, one line per account: counts and names, no addresses)."""
    import connectors_runner as R
    lp = Path(ledger or R.LEDGER_PATH)
    if not lp.exists():
        return False, ["no connectors ledger here: nothing to match the Mac's accounts against"]
    con = sqlite3.connect(f"file:{lp}?mode=ro", uri=True)
    try:
        seen = {}
        for (a,) in con.execute("SELECT account FROM items WHERE source='mail' AND owner=?", (owner,)):
            k = " ".join(str(a or "").split()).lower()
            seen[k] = seen.get(k, 0) + 1
    finally:
        con.close()
    ok, lines, mine = True, [], []
    d = directory or ACCOUNTS_DIR
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            a = load_account(p, role_of)
        except AccountError:
            lines.append(f"{p.stem}: its account file can't be used here")
            ok = False
            continue
        if a["owner"] == owner:
            mine.append(a)
    if not mine:
        return False, lines + [f"{owner}: no server account"]
    for a in mine:
        want = " ".join(str(a.get("mac_account") or "").split()).lower()
        hits = [k for k in seen if k == want] if want else []
        good = len(hits) == 1 and seen[hits[0]] > 0
        ok &= good
        lines.append(f"{a['name']}: " + (f"replaces the Mac's '{a['mac_account']}' ({seen[hits[0]]} items seen)" if good else
                                          "no mac_account set (MAIL_SERVER_MAC_ACCOUNT)" if not want else
                                          f"'{a['mac_account']}' matches no Mac mail account seen (seen: {len(seen)})"))
    return ok, lines


def load_account(path: Path, role_of: Callable[[str], str | None] = _role_of) -> dict[str, Any]:
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise AccountError(f"{path}: must be a regular 0600 file owned by this user")
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raise AccountError(f"{path}: not JSON") from None
    owner, provider = str(raw.get("owner") or ""), str(raw.get("provider") or "")
    need = {"gmail": ("client_id", "client_secret"), "outlook": ("client_id",)}.get(provider, ("user", "password"))
    if not owner or not all(raw.get(k) for k in need):
        raise AccountError(f"{path}: needs owner and {' and '.join(need)}")
    role = role_of(owner)
    if role is None:
        raise AccountError(f"{path}: owner isn't a household member")
    if role == "kid":
        raise AccountError(f"{path}: no kids' mailboxes (#955)")
    # "auto": INBOX plus the archive and sent folders the server marks (resolved on connect, _folders)
    folders = ["auto"] if raw.get("folders") == "auto" else [str(f) for f in (raw.get("folders") or ["INBOX"])]
    # MAIL_SERVER_FOLDERS (not a secret) overrides the file's list, so folders can be added without opening a person's
    # 0600 credentials file (the 10-09 rebuild, decision (c): Sent and Archive)
    folders = env_folders(path.stem, directory=path.parent) or folders
    # the Mac's name for this account, for the cut-over (#955 step 6): the file's own field, else MAIL_SERVER_MAC_ACCOUNT
    # (Coordination, 10-09: never edit a person's 0600 credentials file to add it; the name is in titan's ledger anyway)
    mac = str(raw.get("mac_account") or "").strip() or env_mac_account(path.stem, directory=path.parent)
    if provider == "gmail":
        # the Gmail API (mail_gmail.py); the refresh token comes from the person's own sign-in (mail_gmail.py auth)
        return {"name": path.stem, "owner": owner, "provider": "gmail", "folders": folders, "mac_account": mac,
                **{k: str(raw.get(k) or "") for k in ("client_id", "client_secret", "refresh_token")}}
    if provider == "outlook":
        # Microsoft Graph (mail_outlook.py); the refresh token comes from the person's own device-code sign-in
        import mail_outlook
        tenant = str(raw.get("tenant") or "common")
        if not mail_outlook._TENANT_RX.match(tenant):
            raise AccountError(f"{path}: tenant must be common, consumers, organizations or a tenant id/domain")
        return {"name": path.stem, "owner": owner, "provider": "outlook", "folders": folders, "mac_account": mac,
                "client_id": str(raw["client_id"]), "tenant": tenant, "refresh_token": str(raw.get("refresh_token") or "")}
    host, port = PROVIDERS.get(provider, (raw.get("host"), raw.get("port") or 993))
    if not host:
        raise AccountError(f"{path}: unknown provider and no host")
    # a generic IMAP server (#955 "all providers"): a bare host name and a port, always TLS, read-only like the rest
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", str(host)):
        raise AccountError(f"{path}: host must be a bare host name (no scheme, path or port)")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise AccountError(f"{path}: port must be a number") from None
    if not 0 < port < 65536:
        raise AccountError(f"{path}: port out of range")
    return {"name": path.stem, "owner": owner, "provider": provider or "imap", "host": str(host), "port": int(port),
            "user": str(raw["user"]), "password": str(raw["password"]), "folders": folders, "mac_account": mac}


def replaces_mac() -> bool:
    return os.environ.get("MAIL_SERVER_REPLACES_MAC", "0") == "1"


def mac_accounts(owner: str, directory: Path | None = None,
                 role_of: Callable[[str], str | None] = _role_of) -> set[str]:
    """The Mac's mail account names this person's connected server accounts replace (their files' "mac_account"),
    folded; empty unless MAIL_SERVER_REPLACES_MAC=1. A bad account file replaces nothing."""
    if not replaces_mac():
        return set()
    out = set()
    for p in sorted((directory or ACCOUNTS_DIR).glob("*.json")) if (directory or ACCOUNTS_DIR).is_dir() else []:
        try:
            a = load_account(p, role_of)
        except (AccountError, OSError):
            continue
        if a["owner"] == owner and a.get("mac_account"):
            out.add(" ".join(a["mac_account"].split()).lower())
    return out


def accounts(directory: Path | None = None, role_of: Callable[[str], str | None] = _role_of) -> list[dict[str, Any]]:
    d = directory or ACCOUNTS_DIR
    return [load_account(p, role_of) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


# ── the read-only connection ────────────────────────────────────────────────────────────────────────────────────────

class ReadOnlyViolation(RuntimeError):
    pass


class _ReadOnly:
    """An IMAP connection that can only read. Every call that could change the mailbox raises here."""

    def __init__(self, imap: imaplib.IMAP4):
        self._m = imap

    def login(self, user: str, password: str) -> None:
        self._m.login(user, password)

    def examine(self, folder: str) -> int:
        typ, _ = self._m.select(_quote(folder), readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"EXAMINE failed for a folder ({typ})")
        _, v = self._m.response("UIDVALIDITY")
        return int((v or [b"0"])[0] or 0)

    def folders(self) -> list[tuple[set[str], str]]:
        """LIST: every folder's flags and name (read-only)."""
        typ, data = self._m.list()
        if typ != "OK":
            raise imaplib.IMAP4.error("LIST failed")
        out = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            m = re.match(rb'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
            if m:
                name = m.group("name").decode("utf-8", "replace").strip()
                if name.startswith('"') and name.endswith('"'):
                    name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                out.append(({f.lower() for f in m.group("flags").decode().split()}, name))
        return out

    def uids(self, *criteria: str) -> list[int]:
        typ, data = self._m.uid("SEARCH", None, *(criteria or ("ALL",)))
        if typ != "OK":
            raise imaplib.IMAP4.error("UID SEARCH failed")
        return sorted(int(x) for x in b" ".join(d for d in data if d).split())

    def fetch(self, uids: Iterable[int], what: str) -> dict[int, bytes]:
        spec = what.upper()
        if "PEEK" not in spec or "RFC822" in spec:
            raise ReadOnlyViolation("only BODY.PEEK fetches (a plain fetch marks the mail read)")
        ids = ",".join(str(u) for u in uids)
        if not ids:
            return {}
        typ, data = self._m.uid("FETCH", ids, f"(UID {what})")
        if typ != "OK":
            raise imaplib.IMAP4.error("UID FETCH failed")
        out: dict[int, bytes] = {}
        for part in data:
            if isinstance(part, tuple) and len(part) >= 2:
                m = re.search(rb"\bUID (\d+)", part[0])
                if m:
                    out[int(m.group(1))] = part[1]
        return out

    def logout(self) -> None:
        try:
            self._m.logout()
        except Exception:  # noqa: BLE001 - already gone
            pass

    def __getattr__(self, name: str) -> Any:
        raise ReadOnlyViolation(f"{name} isn't allowed: the mail connector is read-only (#955 step 1)")


MARK_FLAGS = {"read": ("+FLAGS.SILENT", "\\Seen"), "unread": ("-FLAGS.SILENT", "\\Seen"),
              "flag": ("+FLAGS.SILENT", "\\Flagged"), "unflag": ("-FLAGS.SILENT", "\\Flagged")}


class _Mover:
    """The one connection that may change a mailbox (#955 step 5): SELECT a folder, find mails by Message-ID, UID MOVE
    them to another folder, and (#1642) set or clear exactly two flags, read (\\Seen) and flagged (\\Flagged). Nothing
    else: no other STORE (never \\Deleted), no COPY, no EXPUNGE, no DELETE, no APPEND. Used only by mail_moves'
    executor after the owner's tap; a server without MOVE (RFC 6851) is refused."""

    def __init__(self, imap: imaplib.IMAP4):
        self._m = imap

    def login(self, user: str, password: str) -> None:
        self._m.login(user, password)
        if "MOVE" not in {c.upper() for c in (self._m.capabilities or ())}:
            raise ReadOnlyViolation("this server has no MOVE: titan won't move mail with copy-and-delete")

    def select(self, folder: str) -> None:
        typ, _ = self._m.select(_quote(folder))
        if typ != "OK":
            raise imaplib.IMAP4.error(f"SELECT failed for a folder ({typ})")

    def find(self, message_id: str) -> list[int]:
        typ, data = self._m.uid("SEARCH", None, "HEADER", "Message-ID", _quote(message_id))
        if typ != "OK":
            raise imaplib.IMAP4.error("UID SEARCH failed")
        return sorted(int(x) for x in b" ".join(d for d in data if d).split())

    def move(self, uids: list[int], folder: str) -> None:
        if not uids:
            return
        typ, _ = self._m.uid("MOVE", ",".join(str(u) for u in uids), _quote(folder))
        if typ != "OK":
            raise imaplib.IMAP4.error("UID MOVE failed")

    def mark(self, uids: list[int], how: str) -> None:
        """read / unread / flag / unflag: one UID STORE of \\Seen or \\Flagged, silently. Any other word is refused."""
        if how not in MARK_FLAGS:
            raise ReadOnlyViolation(f"{how!r} isn't a mark: only read, unread, flag and unflag")
        if not uids:
            return
        op, flag = MARK_FLAGS[how]
        typ, _ = self._m.uid("STORE", ",".join(str(int(u)) for u in uids), op, f"({flag})")
        if typ != "OK":
            raise imaplib.IMAP4.error("UID STORE failed")

    def logout(self) -> None:
        try:
            self._m.logout()
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, name: str) -> Any:
        raise ReadOnlyViolation(f"{name} isn't allowed: a move only selects, finds, moves and marks (#955 step 5)")


def open_writer(acct: dict[str, Any], connect: Callable[[dict[str, Any]], Any] = None) -> _Mover:
    """The write connection, for mail_moves only."""
    if acct.get("provider") == "gmail":
        raise ReadOnlyViolation("moving Gmail needs the gmail.modify scope (step 5b): not yet")
    _note_egress()
    conn = _Mover((connect or _connect_tls)(acct))
    conn.login(acct["user"], acct["password"])
    return conn


def archive_folder(conn: Any) -> str | None:
    """The account's archive folder: the one LIST marks \\Archive, else one named like an archive."""
    listed = conn.folders()
    return next((n for f, n in listed if "\\archive" in f), None) or next(
        (n for want in _ARCHIVE_NAMES for f, n in listed if n.lower() == want and "\\noselect" not in f), None)


def find_folder(conn: Any, name: str) -> str | None:
    """One of the mailbox's own folders by name, any case (#1646 "move to a mailbox"): a selectable one, never INBOX.
    None when there's no such folder (titan never creates one)."""
    want = " ".join(str(name or "").split()).lower()
    if not want or want == "inbox":
        return None
    return next((n for f, n in conn.folders() if n.lower() == want and "\\noselect" not in f), None)


def folder_names(conn: Any, limit: int = 12) -> list[str]:
    """The mailbox's selectable folders, INBOX aside, for saying which ones there are."""
    return [n for f, n in conn.folders() if "\\noselect" not in f and n.upper() != "INBOX"][:limit]


def trash_folder(conn: Any) -> str | None:
    """The account's trash (#1646): the one LIST marks \\Trash, else one named like a trash. Mail moved there is
    recoverable for the server's own retention; nothing here ever expunges it."""
    listed = conn.folders()
    return next((n for f, n in listed if "\\trash" in f and "\\noselect" not in f), None) or next(
        (n for want in _TRASH_NAMES for f, n in listed if n.lower() == want and "\\noselect" not in f), None)


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _connect_tls(acct: dict[str, Any]) -> Any:
    """The transport: an IMAP connection over TLS, or for Gmail its HTTPS API (mail_gmail.urllib_http)."""
    if acct.get("provider") == "gmail":
        import mail_gmail
        return mail_gmail.urllib_http
    if acct.get("provider") == "outlook":
        import mail_outlook
        return mail_outlook.urllib_http
    return imaplib.IMAP4_SSL(acct["host"], acct["port"], ssl_context=ssl.create_default_context(),
                             timeout=acct.get("timeout") or 60)


def open_account(acct: dict[str, Any], connect: Callable[[dict[str, Any]], imaplib.IMAP4] = _connect_tls) -> _ReadOnly:
    _note_egress()
    if acct.get("provider") == "gmail":
        import mail_gmail
        if not acct.get("refresh_token"):
            raise mail_gmail.GmailError(f"{acct['name']}: not signed in yet (mail_gmail.py auth)")
        gm = mail_gmail.Gmail(acct, connect(acct))
        gm.login()
        return gm  # type: ignore[return-value]
    if acct.get("provider") == "outlook":
        import mail_outlook
        if not acct.get("refresh_token"):
            raise mail_outlook.OutlookError(f"{acct['name']}: not signed in yet (mail_outlook.py auth)")
        ol = mail_outlook.Outlook(acct, connect(acct))
        ol.login()
        return ol  # type: ignore[return-value]
    conn = _ReadOnly(connect(acct))
    conn.login(acct["user"], acct["password"])
    return conn


_ARCHIVE_NAMES = ("archive", "archives", "all mail")
_TRASH_NAMES = ("trash", "deleted messages", "deleted items")   # never "bin": too easily a person's own folder
_SENT_NAMES = ("sent messages", "sent", "sent items", "sent mail")


def _folders(conn: Any, acct: dict[str, Any]) -> list[str]:
    """The account's folders; "auto" is INBOX plus the archive and sent folders (RFC 6154 flags \\Archive/\\Sent,
    else their usual names). Gmail's "auto" is all mail."""
    if acct["folders"] != ["auto"]:
        return acct["folders"]
    if acct.get("provider") == "gmail":
        return ["ALL"]
    if acct.get("provider") == "outlook":
        return ["INBOX", "Archive", "Sent"]   # mail_outlook's well-known folders
    listed = conn.folders()
    out = ["INBOX"]
    for flag, names in (("\\archive", _ARCHIVE_NAMES), ("\\sent", _SENT_NAMES)):
        pick = next((n for f, n in listed if flag in f), None) or next(
            (n for want in names for f, n in listed if n.lower() == want and "\\noselect" not in f), None)
        if pick and pick not in out:
            out.append(pick)
    return out


def _note_egress() -> None:
    import egress
    egress.note("mail")


# ── the store (headers, threads, references, envelopes: never a body) ───────────────────────────────────────────────

def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS folders (account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
            synced_at REAL NOT NULL, PRIMARY KEY (account, folder));
        CREATE TABLE IF NOT EXISTS messages (account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
            uid INTEGER NOT NULL, owner TEXT NOT NULL, item_id TEXT NOT NULL, message_id TEXT NOT NULL,
            in_reply_to TEXT, refs TEXT NOT NULL DEFAULT '[]', thread_id TEXT NOT NULL, sender TEXT, subject TEXT,
            sent_at TEXT, code INTEGER NOT NULL DEFAULT 0, envelope TEXT, derived_at REAL, kb INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (account, folder, uidvalidity, uid));
        CREATE INDEX IF NOT EXISTS messages_mid ON messages (account, message_id);
        CREATE INDEX IF NOT EXISTS messages_thread ON messages (account, thread_id);
    """)
    if "hooked_at" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
        con.execute("ALTER TABLE messages ADD COLUMN hooked_at REAL")   # link hooks indexed (#951), with or without an envelope
    if "category" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
        con.execute("ALTER TABLE messages ADD COLUMN category TEXT")    # mail_triage's, with MAIL_SERVER_PASSES (#955 step 6)
    if "passes" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
        # why the server passes did or didn't run on a mail when it was derived (passes-status): ran | mac (the Mac's
        # passes have it) | old (sent before the window) | ledger (the Mac's ledger was unreadable) | off | code (a
        # sign-in code, kept nowhere). NULL: derived before this column.
        con.execute("ALTER TABLE messages ADD COLUMN passes TEXT")
    if "kept_at" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
        # when a keep rule first considered this mail (#955 10-10): set on every kept-thread mail derived, so a mail
        # derived before its rule existed is re-derived under it once, never again
        con.execute("ALTER TABLE messages ADD COLUMN kept_at REAL")
    return con


def item_id(owner: str, account: str, message_id: str) -> str:
    return "cx-mailsrv-" + hashlib.sha1(f"{owner}\0{account}\0{message_id}".encode()).hexdigest()[:16]


def _ids(value: Any) -> list[str]:
    return _ID_RX.findall(str(value or ""))


def _when(value: Any) -> str | None:
    try:
        d = email.utils.parsedate_to_datetime(str(value))
    except Exception:  # noqa: BLE001 - a malformed Date is no date (IndexError, OverflowError seen in real mail)
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _header(h: Any, name: str, decode: bool = False) -> str:
    """One header as text, never raising. compat32 hands back the raw string (policy.default's structured parse
    raised on real mail's Message-IDs, 10-05); decode=True turns RFC 2047 words (=?utf-8?...?=) into text."""
    try:
        v = h.get(name)
        if v is None:
            return ""
        if decode:
            import email.header
            return str(email.header.make_header(email.header.decode_header(str(v))))
        return str(v)
    except Exception:  # noqa: BLE001 - a header that won't decode is kept as it came, or dropped
        try:
            return str(h.get(name) or "")
        except Exception:  # noqa: BLE001
            return ""


def parse_headers(raw: bytes) -> dict[str, Any]:
    """Headers and thread links from one mail's header block. Never raises: a malformed Message-ID is no id (the
    caller gives the mail a synthetic one), malformed In-Reply-To/References are no links."""
    try:
        h = email.message_from_bytes(raw, policy=email.policy.compat32)
    except Exception:  # noqa: BLE001
        h = email.message.Message()
    return {"message_id": (_ids(_header(h, "Message-ID")) or [""])[0],
            "in_reply_to": (_ids(_header(h, "In-Reply-To")) or [None])[0],
            "refs": _ids(_header(h, "References")), "sender": _redact(" ".join(_header(h, "From", True).split()))[:300],
            # a serial, IMEI, account number or code in a subject is masked before it's stored or shown (#979)
            "subject": _redact(" ".join(_header(h, "Subject", True).split()))[:300], "sent_at": _when(_header(h, "Date"))}


def _redact(text: str) -> str:
    import redact
    return redact.redact_personal(text)


def thread_of(con: sqlite3.Connection, account: str, h: dict[str, Any]) -> str:
    """A known parent's thread, else the root of References, else the parent, else the mail itself."""
    for parent in ([h["in_reply_to"]] if h["in_reply_to"] else []) + list(reversed(h["refs"])):
        row = con.execute("SELECT thread_id FROM messages WHERE account=? AND message_id=? LIMIT 1",
                          (account, parent)).fetchone()
        if row:
            return str(row[0])
    return (h["refs"] or [h["in_reply_to"] or h["message_id"]])[0]


def rethread(con: sqlite3.Connection, account: str) -> int:
    """Join every mail the headers link (Message-ID, In-Reply-To, References) into one thread, whatever order they
    arrived in. A sync reads newest first, so a reply can be stored before its parent and start a thread of its own
    (10-05 live: 2 of 43 replies whose parent was in the store sat apart from it). Each linked group's thread is the
    root of its earliest mail. → how many mails changed thread."""
    rows = con.execute("SELECT rowid, message_id, in_reply_to, refs, thread_id, sent_at FROM messages WHERE account=?",
                       (account,)).fetchall()
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in rows:
        for other in [r["in_reply_to"]] + json.loads(r["refs"] or "[]"):
            if other:
                a, b = find(r["message_id"]), find(other)
                if a != b:
                    parent[a] = b
    first: dict[str, tuple] = {}
    for r in rows:
        refs = json.loads(r["refs"] or "[]")
        root = (refs or [r["in_reply_to"] or r["message_id"]])[0]
        key = (r["sent_at"] or "9999", r["message_id"])
        g = find(r["message_id"])
        if g not in first or key < first[g][0]:
            first[g] = (key, root)
    changed = 0
    for r in rows:
        want = first[find(r["message_id"])][1]
        if want != r["thread_id"]:
            con.execute("UPDATE messages SET thread_id=? WHERE rowid=?", (want, r["rowid"]))
            changed += 1
    return changed


# ── just-in-time body ───────────────────────────────────────────────────────────────────────────────────────────────

_TAG_RX = re.compile(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>")


def body_text(raw: bytes) -> str:
    """The readable text of one fetched message: its plain part, else its HTML without tags. Never raises."""
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001 - malformed headers: walk it the old way
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        parts = [p for p in msg.walk() if p.get_content_maintype() == "text"]
        part = next((p for p in parts if p.get_content_subtype() == "plain"), parts[0] if parts else None)
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:  # noqa: BLE001 - an unknown charset, a compat32 part
        try:
            text = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
        except (LookupError, TypeError):
            text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_subtype() == "html":
        text = html.unescape(_TAG_RX.sub(" ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", str(text))).strip()[:BODY_MAX]


CALENDAR_MAX = 16384   # bytes of calendar parts kept for invite detection (an invite's VEVENT is a few KB)


def calendar_text(raw: bytes) -> str:
    """A message's calendar parts (text/calendar, or an .ics / application/ics attachment), as text, for mail_invites:
    body_text keeps only the plain or HTML part, so an invite whose details live in its calendar part (a forwarded
    invite, most calendar systems' REQUEST mail) never reached invite detection (#955, 10-09). Never raises; "" when
    there's none."""
    out: list[str] = []
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            name = str(part.get_filename() or "").lower()
            if ctype in ("text/calendar", "application/ics") or name.endswith(".ics"):
                data = part.get_payload(decode=True) or b""
                out.append(data.decode(part.get_content_charset() or "utf-8", "replace"))
    except Exception:  # noqa: BLE001 - a malformed part: no calendar text, never a failed derive
        return ""
    return "\n".join(t for t in out if "BEGIN:VEVENT" in t)[:CALENDAR_MAX]


def _metadata(acct: dict[str, Any], row: sqlite3.Row) -> dict[str, Any]:
    return {"owner": acct["owner"], "audience": "private", "account": acct["name"], "source": "mail",
            "kind": "mail", "source_type": SOURCE_TYPE, "observed_at": row["sent_at"],
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "upstream_id": row["message_id"], "item_id": row["item_id"],
            "reference": {"account": acct["name"], "folder": row["folder"], "uidvalidity": row["uidvalidity"],
                          "uid": row["uid"], "message_id": row["message_id"]},
            "thread": {"thread_id": row["thread_id"], "in_reply_to": row["in_reply_to"]}}


def with_headers(row: Any, text: str, to: list[str] | None = None) -> str:
    """The original as the link index reads it (#951): From, To (its recipients, #1399), Date, Message-ID and the
    thread headers, then the body. Our own synthetic ids (<uid-…>) aren't the mail's, so they're left out."""
    refs = json.loads(row["refs"] or "[]") if row["refs"] else []
    mid = row["message_id"] if not str(row["message_id"]).startswith("<uid-") else ""
    head = [f"From: {row['sender'] or ''}"] + ([f"To: {', '.join(to)}"] if to else []) + [f"Date: {row['sent_at'] or ''}"]
    head += [f"Message-ID: {mid}"] if mid else []
    head += [f"In-Reply-To: {row['in_reply_to']}"] if row["in_reply_to"] else []
    head += [f"References: {' '.join(refs)}"] if refs else []
    return "\n".join(head) + "\n\n" + text


# Which mail reaches the link index (#951, owner call 10-05): only what triage keeps. 06:00-21:00 UTC on 10-05 the
# backfill hooked every mail (~500 an hour, links.db 966 -> 7,678 items), and threads/open_obligations slowed to
# 30-50 min. A model verdict, when triage ran: personal, action and record (records are the bills, orders and
# receipts obligation threads are made of). Without one (MAIL_SERVER_PASSES off, or older mail, which never sees
# the model): the rules below; bulk mail is out unless its subject is a reply or names something worth keeping.
HOOK_CATS = frozenset({"personal", "action", "record"})
# What threads are made of beyond triage's own keep cues (mail_triage._KEEP_CUES): orders, shipments, receipts,
# returns, trade-ins, statements. Hooks only; triage's verdicts don't change.
_HOOK_CUES = re.compile(r"(?i)\b(order\w*|ship\w*|receipt|purchase\w*|return\w*|trade-?in|statement|subscription|"
                        r"renewal|charged?|credit|claim)\b")


def recipients(raw: bytes) -> list[str]:
    """The To and Cc addresses of one mail, lowercased, for its party:to hooks (#1399). Read from the raw headers at
    derive time and handed straight to the hook index, which keys them: never stored here. Never raises."""
    try:
        head = email.message_from_bytes(raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0] + b"\n\n",
                                        policy=email.policy.compat32)
        pairs = email.utils.getaddresses([str(v) for k in ("To", "Cc") for v in (head.get_all(k) or [])])
    except Exception:  # noqa: BLE001
        return []
    return list(dict.fromkeys(a.strip().lower() for _, a in pairs if "@" in a))


def bulk_headers(raw: bytes) -> bool:
    """A mailing-list or machine-sent message by its own headers (List-Unsubscribe, List-Id, Precedence bulk/list/
    junk, Auto-Submitted other than no). Headers only; never raises."""
    try:
        head = email.message_from_bytes(raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0] + b"\n\n",
                                        policy=email.policy.compat32)
    except Exception:  # noqa: BLE001
        return False
    if head.get("List-Unsubscribe") or head.get("List-Id"):
        return True
    if str(head.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk"):
        return True
    auto = str(head.get("Auto-Submitted") or "").strip().lower()
    return bool(auto) and auto != "no"


def hook_worthy(category: str | None, subject: str, sender: str, bulk: bool) -> bool:
    """Whether a mail's link hooks go into the index: the model's verdict when there is one, else the rules."""
    import mail_triage as mt
    if category:
        return category in HOOK_CATS
    subj = str(subject or "")
    if mt._REPLY_RX.search(subj) or mt._KEEP_CUES.search(subj) or _HOOK_CUES.search(subj):
        return True                      # a person's thread, or a bill / order / appointment / delivery
    return not (bulk or mt._AUTOMATED.search(str(sender or "")))


def _hooks(row: Any, text: str, md: dict[str, Any], to: list[str] | None = None) -> int:
    """Index the mail's link hooks from its original with its headers (hooks.py; never raises). `to`: its To/Cc
    addresses (recipients()), keyed by the hook index, never kept."""
    import hooks
    return hooks.index_original(row["item_id"], "mail", row["subject"] or "", with_headers(row, text, to), md)


def _kb_write(sid: str, title: str, text: str, source_type: str, md: dict[str, Any]) -> None:
    import agent_runtime_lib as lib
    lib._kb_index(sid, title, text, source_type, md)


def _kb_delete(sid: str) -> None:
    """Remove a mail's KB source. Already gone (404) is done, not an error: on 10-13 one source deleted elsewhere made
    every sync stop here, so no mail synced for a day and a half."""
    import agent_runtime_lib as lib
    import urllib.error
    import urllib.parse
    try:
        lib._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}", "DELETE")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise


THREAD_SUMMARIES = 8          # a thread's envelope keeps its latest mails' summaries
THREAD_CLAIMS = 12


def claims_of(obs: list[dict[str, Any]]) -> list[str]:
    """A mail's world observations as short claim lines ("Hale Water: amount due $48.20"), at most THREAD_CLAIMS."""
    out = []
    for o in obs or []:
        who = str(o.get("name") or o.get("entity") or "").strip()
        attr, val = str(o.get("attr") or "").replace("_", " "), str(o.get("value") or "").strip()
        if who and attr and val:
            out.append(f"{who}: {attr} {val}"[:160])
    return out[:THREAD_CLAIMS]


def _sender_name(sender: str | None) -> str:
    """A participant as the thread envelope names them: the display name only, never an address."""
    m = re.match(r'\s*"?([^<"]*)"?\s*<', sender or "")
    name = " ".join((m.group(1) if m else "").split())
    return name if name and "@" not in name else ""


def thread_sid(acct: dict[str, Any], thread_id: str) -> str:
    return "cx-mailthread-" + hashlib.sha1(f"{acct['owner']}|{acct['name']}|{thread_id}".encode()).hexdigest()[:16]


def threads_of(item_ids: list[str], db: Path | None = None) -> dict[str, str]:
    """Each server mail's thread, as its thread's KB entry id (thread_sid), for the items surface (#1497: a thread is
    one member and opens there). Ids only, from this store's own thread ids (never subjects); an id that isn't a
    server mail is left out. {} when the store can't be read."""
    ids = [i for i in dict.fromkeys(item_ids) if str(i).startswith("cx-mailsrv-")]
    if not ids:
        return {}
    out: dict[str, str] = {}
    try:
        con = sqlite3.connect(f"file:{Path(db or DB)}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            for iid, owner, account, tid in con.execute(
                    f"SELECT item_id, owner, account, thread_id FROM messages WHERE item_id IN ({','.join('?' * len(part))})",
                    part):
                if tid and owner and account:
                    out[iid] = thread_sid({"owner": owner, "name": account}, tid)
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return out


def write_thread(con: sqlite3.Connection, acct: dict[str, Any], thread_id: str, *,
                 write: Callable[..., Any] = None, scope: dict[str, str] | None = None,
                 resolve: Callable[[str, str], str | None] | None = None) -> str | None:
    """#446 v2 (Coordination, 10-09): one KB entry per personal or action thread, owner-only, built from what titan
    already keeps: its subject, participants (display names), dates, each mail's derived summary and claims. Never a
    body: a question about the thread's own words fetches it from the server on demand. It enters a prompt as mail
    (untrusted markers, #473: metadata source "mail"). → its source id, or None when the thread has no kept mail."""
    rows = con.execute("SELECT sender, subject, sent_at, envelope, item_id FROM messages WHERE account=? AND "
                       "thread_id=? AND kb=1 AND code=0 AND envelope IS NOT NULL ORDER BY sent_at",
                       (acct["name"], thread_id)).fetchall()
    if not rows:
        return None
    sid = thread_sid(acct, thread_id)
    names = list(dict.fromkeys(n for n in (_sender_name(r[0]) for r in rows) if n))
    title = next((r[1] for r in reversed(rows) if r[1]), "") or "(no subject)"
    first, last = str(rows[0][2] or "")[:10], str(rows[-1][2] or "")[:10]
    lines, claims = [], []
    for sender, _subj, sent, env_json, _iid in rows[-THREAD_SUMMARIES:]:
        env = json.loads(env_json or "{}")
        summary = " ".join(str((env.get("interpretation") or {}).get("summary") or "").split())
        if summary:
            who = _sender_name(sender)
            lines.append(f"• {str(sent or '')[:10]}{' ' + who if who else ''}: {summary}")
        claims += [c for c in env.get("claims") or [] if c not in claims]
    text = "\n".join([title, "Participants: " + (", ".join(names) or "unnamed"),
                      f"{first}{' to ' + last if last != first else ''}, {len(rows)} mail{'s' if len(rows) != 1 else ''}",
                      *lines, *(["Claims: " + "; ".join(claims[:THREAD_CLAIMS])] if claims else [])])
    md = {"owner": acct["owner"], "audience": "private", "account": acct["name"], "source": "mail",
          "kind": "mail_thread", "source_type": SOURCE_TYPE, "observed_at": rows[-1][2], "item_id": sid,
          "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "thread": {"thread_id": thread_id, "mails": len(rows), "first": first, "last": last},
          "references": [r[4] for r in rows]}   # the mails' own rows: the references a body is fetched through
    # where it files (#955 10-10): the owner's facet and their project entity, never who sees it (still theirs alone)
    if (scope or {}).get("facet"):
        md["facet"] = scope["facet"]
    if (scope or {}).get("project_id"):
        md["project"] = scope["project_id"]          # KB's mapping names the owner's project entity already
    elif (scope or {}).get("project"):
        pid = (resolve or project_id)(acct["owner"], scope["project"])
        if pid:
            md["project"] = pid
    (write or _kb_write)(sid, title, text, SOURCE_TYPE, md)
    return sid


FACETS = ("personal", "social", "work", "school")          # household_identity.FACETS ("home" is the household's)


def scope_of(project: Any, facet: Any) -> dict[str, str]:
    """A checked {project, facet} (either may be absent): a project's name (one line, at most 80 characters) and one
    of a person's own facets."""
    out: dict[str, str] = {}
    if isinstance(project, str) and project.strip() and len(project.strip()) <= 80 and "\n" not in project:
        out["project"] = project.strip()
    if isinstance(facet, str) and facet.strip().lower() in FACETS:
        out["facet"] = facet.strip().lower()
    return out


def project_id(owner: str, name: str, w: Any = None) -> str | None:
    """The owner's own project entity by its exact name (world model, #1489): "<their bubble>:<entity>", or None when
    there isn't one yet (then nothing is filed). Never another bubble's, never an entity that isn't a project."""
    try:
        import world as W
        bubble = W.bubble_name(owner)
        w = w if w is not None else W.get()
        for cid in w.exact(name, owner, _role_of(owner) or "adult"):
            if not cid.startswith(bubble + ":"):
                continue
            ent = w.entity(cid)
            if ent is not None and "type" in ent.keys() and ent["type"] == "project":   # the entities column is "type"
                return cid
    except Exception:  # noqa: BLE001 - no world, or no such entity yet: file nothing
        return None
    return None


def load_keep(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Each owner's keep rules: words (whole words in a mail's subject, any case) and thread ids, each rule with the
    project and facet its threads file under. A matching mail's whole thread is derived and kept in THEIR KB whatever
    its age or triage. {} when there's no file, or it isn't 0600 (never trusted then: the file says what someone keeps)."""
    p = Path(path or KEEP_FILE)
    try:
        if p.stat().st_mode & 0o077:
            print("[mail_server] keep rules ignored: the file isn't 0600", file=sys.stderr)
            return {}
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for owner, rules in (raw.items() if isinstance(raw, dict) else []):
        for rule in (rules if isinstance(rules, list) else [rules]):
            if not isinstance(rule, dict):
                continue
            out.setdefault(str(owner), []).append({
                "words": {str(w).lower().strip() for w in (rule.get("phrases") or rule.get("words") or [])
                          if len(str(w).strip()) >= 3},
                "threads": {str(t).strip() for t in rule.get("threads") or [] if str(t).strip()},
                "scope": scope_of(rule.get("project"), rule.get("facet"))})
    return out


def _keep_word(rule: dict[str, set[str]], subject: str | None) -> bool:
    low = str(subject or "").lower()
    return any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", low) for w in rule["words"])


def keep_threads(con: sqlite3.Connection, account: str, owner: str | None,
                 keep: dict[str, list[dict[str, Any]]] | None) -> dict[str, dict[str, str]]:
    """The threads this owner's keep rules hold in this account, each with its rule's {project, facet}: every thread
    with a mail whose subject carries one of a rule's words, and the threads a rule names. The first rule that holds a
    thread files it. Another person's rules never apply."""
    rules = (keep or {}).get(str(owner or "")) or []
    rules = [{"words": set(), "threads": set(), "scope": {}, **r}   # copies: the caller's rules are never changed
             for r in (rules if isinstance(rules, list) else [rules]) if isinstance(r, dict)]
    got: dict[str, dict[str, str]] = {}
    for rule in rules:
        for t in rule["threads"]:
            got.setdefault(t, rule["scope"])
    if any(r["words"] for r in rules):
        for tid, subj in con.execute("SELECT thread_id, subject FROM messages WHERE account=? AND code=0", (account,)):
            if not tid or tid in got:
                continue
            for rule in rules:
                if rule["words"] and _keep_word(rule, subj):
                    got[tid] = rule["scope"]
                    break
    return got


def kept(threads: Any, thread_id: str | None) -> bool:
    return bool(threads) and bool(thread_id) and str(thread_id) in threads


def account_scope(acct: dict[str, Any], uses: Callable[[str], dict[str, Any]] | None = None) -> dict[str, str]:
    """Where an account's mail files, from KB's mapping (#1490: the owner's own facet map and project claims, keyed by
    titan's account label, never an address): {facet, project_id}. An unmapped account files nothing; a mapping never
    changes who sees anything, and only the account's owner's own mapping is read."""
    try:
        if uses is None:
            import world as W
            if not hasattr(W, "mail_uses"):
                return {}
            uses = W.mail_uses
        label = " ".join(str(acct["name"]).split()).lower()[:80]   # the mapping's own label form (#1492)
        got = (uses(str(acct["owner"])) or {}).get(label) or {}
    except Exception:  # noqa: BLE001 - no world, no mapping: file nothing
        return {}
    out = scope_of(None, got.get("facet"))
    if isinstance(got.get("project"), str) and got["project"].strip():
        out["project_id"] = got["project"].strip()
    return out


def thread_scope(acct_scope: dict[str, str], keep: Any, thread_id: str | None) -> dict[str, str]:
    """Where a thread files: its keep rule's project and facet, else its account's (KB's mapping)."""
    if isinstance(keep, dict) and thread_id in keep and keep[thread_id]:
        return dict(keep[thread_id])
    return dict(acct_scope or {})


_MACHINE_RX = re.compile(r"^(?:no-?reply|do-?not-?reply|notifications?|alerts?|mailer-daemon|bounces?|postmaster|"
                         r"automated|builds?|ci|github|jira|calendar-notification)\b")


def automated(sender: str | None, bulk: bool) -> bool:
    """A machine's mail, not a person's: list or bulk headers (bulk_headers), or a no-reply / notifications address.
    The keep rule (Coordination 10-10) keeps a person's mail in a kept thread; a build ping in it is triaged as ever."""
    return bool(bulk) or bool(_MACHINE_RX.match(address_of(sender).partition("@")[0]))


def address_of(sender: str | None) -> str:
    """A From header's address, lowercased ("" when there's none)."""
    m = re.search(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", str(sender or ""))
    return m.group(0).lower() if m else ""


def correspondents_of(owner: str, db: Path | None = None) -> set[str]:
    """correspondents() over every server account this person owns (the Mac path's mail is judged against these)."""
    p = Path(db or DB)
    if not p.exists():
        return set()
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return set().union(*[correspondents(con, a) for (a,) in
                             con.execute("SELECT DISTINCT account FROM messages WHERE owner=?", (owner,))])
    finally:
        con.close()


AWAIT_MIN_DAYS, AWAIT_MAX_DAYS = 2, 21


def awaiting_reply(owner: str, *, db: Path | None = None, now: float | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """#1644 "needs a reply": mail in this person's inbox from someone they write to (correspondents: a person, not a
    mailer), AWAIT_MIN_DAYS to AWAIT_MAX_DAYS old, that's still the newest mail in its thread: nothing after it, from
    them or anyone. Their own mail only, read-only, newest first: [{sender, subject, sent_at, days}]."""
    p = Path(db or DB)
    if not p.exists() or not owner:
        return []
    now = now or time.time()
    lo = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - AWAIT_MAX_DAYS * 86400))
    hi = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - AWAIT_MIN_DAYS * 86400))
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out: list[dict[str, Any]] = []
    try:
        for (account,) in con.execute("SELECT DISTINCT account FROM messages WHERE owner=?", (owner,)).fetchall():
            people = correspondents(con, account)
            if not people:
                continue
            rows = con.execute(
                "SELECT m.sender, m.subject, m.sent_at, m.thread_id FROM messages m WHERE m.account=? AND m.owner=? AND "
                "upper(m.folder)='INBOX' AND m.code=0 AND m.sent_at BETWEEN ? AND ? AND NOT EXISTS (SELECT 1 FROM messages "
                "n WHERE n.account=m.account AND n.thread_id=m.thread_id AND n.sent_at > m.sent_at) ORDER BY m.sent_at DESC",
                (account, owner, lo, hi)).fetchall()
            for r in rows:
                if address_of(r["sender"]) not in people:
                    continue
                t = datetime.strptime(r["sent_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                out.append({"sender": _sender_name(r["sender"]) or address_of(r["sender"]), "subject": (r["subject"] or "")[:120],
                            "sent_at": r["sent_at"], "days": int((now - t) // 86400)})
    finally:
        con.close()
    return sorted(out, key=lambda x: x["sent_at"], reverse=True)[:limit]


def correspondents(con: sqlite3.Connection, account: str) -> set[str]:
    """Senders the owner has written to: an address with a mail in a thread that also holds one from the owner's Sent
    folder (a reply). Addresses stay in memory for the pass; nothing new is stored."""
    sent = [f for (f,) in con.execute("SELECT DISTINCT folder FROM messages WHERE account=?", (account,))
            if f.lower() in _SENT_NAMES or "sent" in f.lower()]
    if not sent:
        return set()
    marks = ",".join("?" * len(sent))
    rows = con.execute(f"SELECT DISTINCT m.sender FROM messages m WHERE m.account=? AND m.folder NOT IN ({marks}) AND "
                       f"m.thread_id IN (SELECT thread_id FROM messages WHERE account=? AND folder IN ({marks}))",
                       (account, *sent, account, *sent)).fetchall()
    return {a for (snd,) in rows if (a := address_of(snd))}


def derive(con: sqlite3.Connection, conn: _ReadOnly, acct: dict[str, Any], folder: str, rows: list[sqlite3.Row], *,
           keep: dict[str, dict[str, str]] | None = None, keep_cut: str | None = None,
           resolve: Callable[[str, str], str | None] | None = None,
           uses: Callable[[str], dict[str, Any]] | None = None,
           kb: bool, chat: Any = None, url: str | None = None, fences: Any = None,
           write: Callable[..., Any] | None = None, observe: Callable[..., list] | None = None,
           passes: dict[str, Any] | None = None) -> int:
    """Fetch each row's body (BODY.PEEK), derive its envelope from the original, keep the envelope, drop the body.
    With MAIL_SERVER_PASSES=1 (mail_server_passes.py) each mail is triaged first and only personal and action mail's
    envelope may reach the KB; bills and suggestions read the batch from the original before the texts go.
    `passes`: injected functions for tests (llm, bills, suggestions)."""
    import envelopes
    import envelope_pass
    import mail_invites
    import mail_triage
    import mail_server_passes as P
    on = P.enabled()
    hooks_in = passes or {}
    # #955 cut-over: while the Mac's mail sync still runs its own passes, the server's take only what the Mac can't:
    # mail it never synced, and an invite that shows only in its calendar part (the Mac sends plain text). Then a
    # mail never gets two cards or two bills. Everything, once the server replaces the Mac (MAIL_SERVER_REPLACES_MAC).
    mac = (hooks_in["mac_ids"] if "mac_ids" in hooks_in else (_mac_ids() if on and not replaces_mac() else set()))
    off_why = "off"
    if mac is None:   # the Mac's ledger couldn't be read: no server pass this run (never a duplicate), as if off
        on, mac, off_why = False, set(), "ledger"
        print("[mail_server] the Mac's ledger couldn't be read: passes skipped this run", file=sys.stderr)
    passes_cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - PASSES_DAYS * 86400))
    batch: list[dict[str, Any]] = []
    threads_touched: set[str] = set()
    known: set[str] | None = None   # correspondents(), read once a batch when a mail needs it
    raws = conn.fetch([r["uid"] for r in rows], "BODY.PEEK[]")
    n = 0
    for r in rows:
        raw = raws.get(r["uid"])
        if raw is None:
            continue
        text = body_text(raw)
        cal = calendar_text(raw)   # an invite's calendar part, for invite detection (body_text keeps none)
        bulk = bulk_headers(raw)
        to = recipients(raw)
        del raw
        md = _metadata(acct, r)
        if mail_triage.is_code(r["subject"] or "", text):
            con.execute("UPDATE messages SET code=1, subject=?, envelope=NULL, derived_at=?, passes='code' WHERE "
                        "account=? AND folder=? AND uidvalidity=? AND uid=?",
                        (mail_triage.MASKED_TITLE, time.time(), acct["name"], folder, r["uidvalidity"], r["uid"]))
            continue
        item = {"source_id": r["item_id"], "source_type": SOURCE_TYPE, "title": r["subject"] or "", "metadata": md}
        # the summariser and the envelope get the mail with serials, IMEIs, account numbers and codes masked (#979);
        # the link hooks below read the original (they keep only keyed hashes)
        masked = _redact(text)
        cat = None
        kb_this = kb
        keep_this = kept(keep, r["thread_id"]) and not automated(r["sender"], bulk)
        if kept(keep, r["thread_id"]):
            con.execute("UPDATE messages SET kept_at=? WHERE account=? AND folder=? AND uidvalidity=? AND uid=?",
                        (time.time(), acct["name"], folder, r["uidvalidity"], r["uid"]))
        if r["derived_at"] is not None:
            # derived before its keep rule existed: a person's mail now goes to their KB (no bills or cards again:
            # they ran then); a machine's stays exactly as it was
            if not keep_this:
                del text, cal, masked
                continue
            again_this = True
        else:
            again_this = False
        # here only by the keep rule (past the derive window) yet a machine's mail: enveloped locally, never the KB
        keep_machine = bool(keep_cut and r["sent_at"] and r["sent_at"] < keep_cut and not keep_this)
        on_this = on and (not r["sent_at"] or r["sent_at"] >= passes_cut)
        why = "ran" if on_this else (off_why if not on else "old")
        if keep_this and why in ("old", "off"):
            why = "keep"   # past the passes' window, here by the owner's keep rule: the KB only, no bills or cards
        if keep_machine:
            kb_this, on_this, why = False, False, "keep-bulk"
        if again_this:
            on_this, why = False, "keep"   # the KB only: its bills and cards ran when it was first derived
        if on_this and mac:
            import mail_compare
            if mail_compare._norm(r["message_id"]) in mac:
                # the Mac's passes have this mail; only an invite they can't see (its calendar part) is the server's
                on_this = bool(cal) and not mail_invites.detect(item["title"], text) and \
                    mail_invites.detect(item["title"], f"{text}\n{cal}")
                why = "ran" if on_this else "mac"
                # the Mac's copy covers it: never into the KB from here too, untriaged (10-09: with MAIL_SERVER_KB=1 in
                # the parallel week, every Mac-known newsletter and notification would have landed in the KB twice)
                if not on_this:
                    kb_this = False
        if on_this:
            cat = P.classify(item["title"], masked, hooks_in.get("llm"))
            if cat == "code":     # the model's word for it: kept nowhere, like the rule's
                con.execute("UPDATE messages SET code=1, subject=?, envelope=NULL, derived_at=?, category='code', passes='ran' "
                            "WHERE account=? AND folder=? AND uidvalidity=? AND uid=?",
                            (mail_triage.MASKED_TITLE, time.time(), acct["name"], folder, r["uidvalidity"], r["uid"]))
                del text, masked
                continue
            kb_this = kb and (cat in mail_triage.KEEP or keep_this)   # personal and action only, or the owner's keep rule
            if known is None:
                known = correspondents(con, acct["name"])
            mine = {"item_id": r["item_id"], "owner": acct["owner"], "title": item["title"],
                    "text": masked + (f"\n{_redact(cal)}" if cal else ""), "md": md,
                    "stamp": f"server:{r['sent_at'] or r['message_id']}", "category": cat,
                    "ics": bool(cal), "sender_known": address_of(r["sender"]) in known}
            if cat in mail_triage.DROPPED and not keep_this:   # a kept mail is never held back as a notification
                P.hold_for_digest(mine, cat, path=hooks_in.get("triage_db"))
            batch.append(mine)
        if kb_this:
            # #446 v2 (10-09): the KB holds one envelope per thread, never a mail's own entry (and never a body): the
            # mail's envelope (summary, observations, fences) is kept here and its thread's entry is rewritten below
            held: dict[str, Any] = {}
            env = envelope_pass.envelope_one(item, masked, md, write=lambda *a: held.update(md=a[4]),
                                             observe=observe if observe is not None else envelope_pass._world_observer(),
                                             fences=fences, url=url, chat=chat)
            env["claims"] = claims_of((held.get("md") or {}).get("envelope", {}).get("obs") or [])
            threads_touched.add(r["thread_id"])
        else:
            env = envelopes.build({**item, "text": masked}, url=url, chat=chat, fences=fences)
        # after envelope_one's own body-only pass, so the index keeps this one: the headers link threads and senders.
        # Only mail triage keeps (hook_worthy); hooked_at is set either way, so a skipped mail isn't fetched again.
        env["hooks"] = _hooks(r, text, md, to) if hook_worthy(cat, r["subject"], r["sender"], bulk) else 0
        con.execute("UPDATE messages SET hooked_at=? WHERE account=? AND folder=? AND uidvalidity=? AND uid=?",
                    (time.time(), acct["name"], folder, r["uidvalidity"], r["uid"]))
        del text, cal
        con.execute("UPDATE messages SET envelope=?, derived_at=?, kb=?, category=?, passes=? WHERE account=? AND folder=? "
                    "AND uidvalidity=? AND uid=?", (json.dumps(env, default=str), time.time(), int(kb_this), cat, why,
                                                    acct["name"], folder, r["uidvalidity"], r["uid"]))
        n += 1
    acct_scope = account_scope(acct, uses) if threads_touched else {}   # KB's mapping, read once a batch
    for tid in sorted(threads_touched):
        try:
            write_thread(con, acct, tid, write=write or _kb_write, scope=thread_scope(acct_scope, keep, tid), resolve=resolve)
        except Exception as exc:  # noqa: BLE001 - a thread entry not written is retried with its next mail
            print(f"[mail_server] a thread envelope wasn't written ({type(exc).__name__})", file=sys.stderr)
    if batch:
        try:
            # a payment confirmation reaches bills whatever triage said; suggestions from what a person would read
            (hooks_in.get("bills") or P.run_bills)([b for b in batch if b["category"] != "code"])
            import mail_suggestions as MS   # the rule itself (calendar titles included) is run_suggestions'
            (hooks_in.get("suggestions") or P.run_suggestions)(
                [b for b in batch if b["category"] in MS.SUGGEST_ALWAYS | MS.SUGGEST_IF_KNOWN])
        except Exception as exc:  # noqa: BLE001 - a pass that fails never loses the envelopes
            print(f"[mail_server] derivation passes failed ({type(exc).__name__})", file=sys.stderr)
        finally:
            for b in batch:
                b["text"] = ""
            batch.clear()
    return n


# ── a mail's body, on demand (#446 v2: the server is the store; its row is the reference) ─────────────────────────

def references_for(person: str | None, db: Path | None = None, limit: int = 2000) -> list[dict[str, Any]]:
    """The person's own server mail as references, for open_item: the store's rows (never a copy of them), newest
    first. Only theirs: server mail is private to its owner, so nobody else can open or even find it here."""
    if not person:
        return []
    con = _db(db)
    try:
        rows = con.execute("SELECT item_id, owner, account, folder, uidvalidity, uid, message_id, subject, sent_at, "
                           "thread_id FROM messages WHERE owner=? AND code=0 AND envelope IS NOT NULL ORDER BY sent_at "
                           "DESC LIMIT ?", (person, limit)).fetchall()
    finally:
        con.close()
    return [{"id": r["item_id"], "owner": r["owner"], "audience": "private", "device_id": f"server:{r['account']}",
             "kind": "mail", "native_id": r["message_id"], "content_hash": None, "title": r["subject"] or "",
             "dates": {"received": r["sent_at"]} if r["sent_at"] else {},
             "server": {"account": r["account"], "folder": r["folder"], "uidvalidity": r["uidvalidity"],
                        "uid": r["uid"], "thread_id": r["thread_id"], "sent_at": r["sent_at"]}} for r in rows]


def read_body(person: str, ref: dict[str, Any], *, max_chars: int = 8000,
              connect: Callable[[dict[str, Any]], imaplib.IMAP4] = _connect_tls, directory: Path | None = None,
              role_of: Callable[[str], str | None] = _role_of) -> dict[str, Any]:
    """One mail's text, read now from the person's own server (BODY.PEEK: it stays unread there), for this turn only:
    {"found": True, "text"} | {"found": False} | {"error": "no_mailbox" | "unreachable"}. Never stored, never logged."""
    srv = ref.get("server") or {}
    acct = next((a for a in accounts(directory, role_of) if a["owner"] == person and a["name"] == srv.get("account")),
                None)
    if acct is None:
        return {"error": "no_mailbox"}
    try:
        conn = open_account({**acct, "timeout": SEARCH_TIMEOUT}, connect)
    except (imaplib.IMAP4.error, OSError):
        return {"error": "unreachable"}
    try:
        validity = conn.examine(srv.get("folder") or "INBOX")
        mid = str(ref.get("native_id") or "")
        if validity == srv.get("uidvalidity") and srv.get("uid"):
            uids = [int(srv["uid"])]
        elif mid and not mid.startswith("<uid-"):
            uids = conn.uids("HEADER", "Message-ID", _quote(mid))[:1]
        else:
            uids = []
        raw = conn.fetch(uids, "BODY.PEEK[]").get(uids[0]) if uids else None
        if raw is None:
            return {"found": False}
        text = body_text(raw)
        del raw
        return {"found": True, "text": text[:max_chars]}
    except (imaplib.IMAP4.error, OSError):
        return {"error": "unreachable"}
    finally:
        conn.logout()


# ── search (#955 step 4: search_mail) ───────────────────────────────────────────────────────────────────────────────

SEARCH_TIMEOUT = 20          # seconds per connection inside a turn
SEARCH_PEEK = 65536          # bytes of each matching mail read for its snippet (its text part comes first)
_WORD_RX = re.compile(r"[A-Za-z0-9@._'$-]+")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class NoMailbox(LookupError):
    """The asker has no mailbox connected to titan."""


def criteria(query: str, sender: str | None = None, days: int | None = None, now: float | None = None) -> list[str]:
    """IMAP SEARCH keys: every word in the text (AND), the sender, and how far back. ASCII words only (a non-ASCII
    one needs a CHARSET literal; none of the household's searches have needed one)."""
    keys: list[str] = []
    for w in _WORD_RX.findall(query or "")[:6]:
        keys += ["TEXT", _quote(w)]
    for w in _WORD_RX.findall(sender or "")[:2]:
        keys += ["FROM", _quote(w)]
    if days:
        d = time.gmtime((now or time.time()) - int(days) * 86400)
        keys += ["SINCE", f"{d.tm_mday}-{_MONTHS[d.tm_mon - 1]}-{d.tm_year}"]
    return keys


def _has(hay: str, word: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", hay, re.I) is not None


def relevance(query: str, head: str, text: str) -> int:
    """How well a mail the server returned answers the query, from what was read (headers, the start of the body):
    3 the whole phrase in the subject or sender, 2 the whole phrase in the text, 1 every word, 0 some words,
    -1 none of them as whole words (IMAP TEXT matches inside words: "trade" mail for "trade-in", 10-05).
    With no words (a sender-only search) every hit is 0."""
    words = _WORD_RX.findall(query or "")
    if not words:
        return 0
    phrase = " ".join(words)
    flat_head, flat = " ".join(head.split()), " ".join(text.split())
    if _has(flat_head, phrase):
        return 3
    if _has(flat, phrase):
        return 2
    present = [w for w in words if _has(flat_head, w) or _has(flat, w)]
    return 1 if len(present) == len(words) else (0 if present else -1)


def snippet(text: str, query: str, width: int = 220) -> str:
    """The stretch of the mail around the first word of the query that's in it, else its start; redacted."""
    flat = _redact(" ".join(text.split()))   # before the cut: a cut can drop the "IMEI:" that marks a number
    at = -1
    words = _WORD_RX.findall(query or "")
    for w in ([" ".join(words)] if len(words) > 1 else []) + words:
        at = flat.lower().find(w.lower())
        if at >= 0:
            break
    start = max(0, at - width // 3) if at >= 0 else 0
    out = flat[start:start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(flat) else "")


def search(person: str, query: str, *, sender: str | None = None, days: int | None = None, limit: int = 8,
           connect: Callable[[dict[str, Any]], imaplib.IMAP4] = _connect_tls, directory: Path | None = None,
           role_of: Callable[[str], str | None] = _role_of, now: float | None = None) -> dict[str, Any]:
    """Search the asker's OWN mailboxes on their servers (never anyone else's). → {"results": [{date, sender,
    subject, snippet, account, folder}], newest first, "errors": [account names that couldn't be searched]}.
    A sign-in or verification code mail is never returned."""
    import mail_triage
    keys = criteria(query, sender, days, now)
    if not keys or keys[-2] == "SINCE" and len(keys) == 2:
        raise ValueError("a word to look for, or a sender, is required")
    mine = [a for a in accounts(directory, role_of) if a["owner"] == person]
    if not mine:
        raise NoMailbox(person)
    found: list[dict[str, Any]] = []
    errors: list[str] = []
    for acct in mine:
        try:
            conn = open_account({**acct, "timeout": SEARCH_TIMEOUT}, connect)
        except (imaplib.IMAP4.error, OSError):
            errors.append(acct["name"])
            continue
        try:
            for folder in _folders(conn, acct):
                conn.examine(folder)
                uids = conn.uids(*keys)[-limit * 3:]     # the server's recall; ranked below
                heads = conn.fetch(uids, f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})]")
                bodies = conn.fetch(uids, f"BODY.PEEK[]<0.{SEARCH_PEEK}>")
                for uid in uids:
                    if uid not in heads:
                        continue
                    h = parse_headers(heads[uid])
                    text = body_text(bodies.get(uid) or b"")
                    if mail_triage.is_code(h["subject"], text):
                        continue
                    rank = relevance(query, f"{h['subject']}\n{h['sender']}", text)
                    if rank < 0:
                        continue      # the server matched a piece of a word ("trade" for "trade-in"): not a hit
                    found.append({"date": h["sent_at"] or "", "sender": h["sender"], "subject": h["subject"],
                                  "snippet": snippet(text, query), "account": acct["name"], "folder": folder,
                                  "rank": rank})
                    del text
        except (imaplib.IMAP4.error, OSError):
            errors.append(acct["name"])
        finally:
            conn.logout()
    found.sort(key=lambda r: (r.pop("rank"), r["date"]), reverse=True)
    return {"results": found[:limit], "errors": errors}


def _hook_only(con: sqlite3.Connection, conn: Any, acct: dict[str, Any], folder: str, rows: list, batch: int = 50) -> int:
    """Link hooks for mail outside the envelope window: one BODY.PEEK each, the body dropped after, no model. Only
    mail hook_worthy keeps is indexed (no verdict here: the rules); every one is marked, so none is fetched twice.
    Returns how many were indexed."""
    n = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        raws = conn.fetch([r["uid"] for r in chunk], "BODY.PEEK[]")
        for r in chunk:
            raw = raws.pop(r["uid"], None)
            if raw is None:
                continue
            text = body_text(raw)
            bulk = bulk_headers(raw)
            to = recipients(raw)
            del raw
            import mail_triage
            if mail_triage.is_code(r["subject"] or "", text):   # a code mail keeps nothing, hooks included
                con.execute("UPDATE messages SET code=1, subject=? WHERE account=? AND folder=? AND uidvalidity=? AND uid=?",
                            (mail_triage.MASKED_TITLE, acct["name"], folder, r["uidvalidity"], r["uid"]))
                continue
            if hook_worthy(r["category"] if "category" in r.keys() else None, r["subject"], r["sender"], bulk):
                _hooks(r, text, _metadata(acct, r), to)
                n += 1
            del text
            con.execute("UPDATE messages SET hooked_at=? WHERE account=? AND folder=? AND uidvalidity=? AND uid=?",
                        (time.time(), acct["name"], folder, r["uidvalidity"], r["uid"]))
    return n


# ── one-off: hooks for mail derived before #951 reached the server connector ──────────────────────────────────────

REVECTOR_MAX = int(os.environ.get("MAIL_SERVER_REVECTOR_MAX", "500"))


def _topic_ids() -> set[str]:
    """The items that already have a topic vector (links.db `topics`)."""
    import hooks
    import topics
    c = hooks.connect()
    try:
        c.executescript(topics.SCHEMA)
        return {r[0] for r in c.execute("SELECT item_id FROM topics")}
    finally:
        c.close()


def hooks_backfill(*, connect: Callable[[dict[str, Any]], Any] = _connect_tls, db: Path | None = None,
                   directory: Path | None = None, role_of: Callable[[str], str | None] = _role_of,
                   batch: int = 50, revector: bool = False, limit: int | None = None) -> dict[str, Any]:
    """Index the link hooks of every already-derived mail: one BODY.PEEK per mail, read-only, the body dropped after.
    Nothing else in the store changes. → counts per account.
    revector: instead, every hooked mail (derived or hooks-only) that has no topic vector yet (links.db `topics`, #951
    (e)): read again, its hooks and its vector recomputed from the body; at most `limit` (REVECTOR_MAX) a run."""
    if not enabled():
        raise RuntimeError("MAIL_SERVER isn't on")
    out: dict[str, Any] = {}
    con = _db(db)
    try:
        for acct in accounts(directory, role_of):
            if revector:
                have = _topic_ids()
                rows = [r for r in con.execute(
                    "SELECT m.* FROM messages m JOIN folders f ON f.account=m.account AND f.folder=m.folder AND "
                    "f.uidvalidity=m.uidvalidity WHERE m.account=? AND m.hooked_at IS NOT NULL AND m.code=0 "
                    "ORDER BY m.sent_at DESC", (acct["name"],)).fetchall() if r["item_id"] not in have]
                rows = rows[:REVECTOR_MAX if limit is None else limit]
            else:
                rows = con.execute("SELECT m.* FROM messages m JOIN folders f ON f.account=m.account AND f.folder=m.folder "
                                   "AND f.uidvalidity=m.uidvalidity WHERE m.account=? AND m.derived_at IS NOT NULL AND "
                                   "m.code=0 ORDER BY m.folder, m.uid", (acct["name"],)).fetchall()
            n = {"mails": 0, "hooks": 0, "unread": 0}
            if not rows:
                out[acct["name"]] = n
                continue
            conn = open_account(acct, connect)
            try:
                for folder in sorted({r["folder"] for r in rows}):
                    conn.examine(folder)
                    mine = [r for r in rows if r["folder"] == folder]
                    for i in range(0, len(mine), batch):
                        chunk = mine[i:i + batch]
                        raws = conn.fetch([r["uid"] for r in chunk], "BODY.PEEK[]")
                        for r in chunk:
                            raw = raws.pop(r["uid"], None)
                            if raw is None:
                                n["unread"] += 1
                                continue
                            text = body_text(raw)
                            bulk = bulk_headers(raw)
                            to = recipients(raw)
                            del raw
                            if hook_worthy(r["category"], r["subject"], r["sender"], bulk):   # #951: kept mail only
                                n["hooks"] += _hooks(r, text, _metadata(acct, r), to)
                            else:
                                n["skipped"] = n.get("skipped", 0) + 1
                            con.execute("UPDATE messages SET hooked_at=? WHERE rowid=(SELECT rowid FROM messages WHERE "
                                        "account=? AND folder=? AND uidvalidity=? AND uid=?)",
                                        (time.time(), r["account"], r["folder"], r["uidvalidity"], r["uid"]))
                            n["mails"] += 1
                            del text
            finally:
                conn.logout()
            out[acct["name"]] = n
    finally:
        con.close()
    return out


# ── one-off: derive the recent window again (the cut-over, #955 step 6) ─────────────────────────────────────────────

def rederive(days: float = DERIVE_DAYS, *, db: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Mark the mail in the envelope window for derivation again (derived_at cleared), so the timer re-reads each
    original through the passes (MAIL_SERVER_PASSES) and the KB (MAIL_SERVER_KB) at DERIVE_MAX a run. Each envelope
    stays until its new one replaces it; code mails are never re-read. No connection. → counts only."""
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now or time.time()) - days * 86400))
    con = _db(db)
    try:
        n = con.execute("UPDATE messages SET derived_at=NULL WHERE derived_at IS NOT NULL AND code=0 AND "
                        "(sent_at IS NULL OR sent_at >= ?)", (cutoff,)).rowcount
        con.commit()
        return {"marked": n, "since": cutoff[:10]}
    finally:
        con.close()


# ── the cut-over's dry run (#955, 10-09): what turning the passes on would do, counts only ─────────────────────────

def _mac_ids() -> set[str] | None:
    """The Message-IDs the Mac's mail sync has (the connector ledger, source "mail"; tombstoned rows too: the Mac's
    passes saw them before the cleanup). None when the ledger can't be read: then no server pass runs this time,
    rather than one that might duplicate the Mac's cards."""
    import connectors_runner
    import mail_compare
    try:
        con = sqlite3.connect(f"file:{connectors_runner.LEDGER_PATH}?mode=ro", uri=True)
        try:
            return {mail_compare._norm(r[0]) for r in con.execute("SELECT source_id FROM items WHERE source='mail'")}
        finally:
            con.close()
    except sqlite3.Error:
        return None


def dryrun_passes(days: float = 14, *, model: bool = False, connect: Callable[[dict[str, Any]], Any] = _connect_tls,
                  db: Path | None = None, directory: Path | None = None, mac_ids: set[str] | None = None,
                  llm: Callable[[str], str] | None = None, now: float | None = None) -> dict[str, Any]:
    """Read-only: the last `days` of server mail fetched BODY.PEEK (nothing kept, nothing written, no card, no model
    unless `model`), counted by what the passes would do, split by whether the Mac's sync also has the mail:
      invite_plain   an invite by rule from the plain text (what the Mac's text, and today's derive, can see)
      invite_cal     an invite only once its calendar part is read (calendar_text): what both paths miss today
      card_ready     an invite whose day and time read (a card would be offered; mail_suggestions still checks it)
      bills          mail_bills.is_candidate
      suggestions    mail_suggestions.is_candidate (with the calendar part)
      kinds          mail_triage's category per mail, with `model` only (a local model call each)
    → counts only: never an id, subject, sender or body."""
    import mail_bills
    import mail_compare
    import mail_invites
    import mail_suggestions
    import mail_triage
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now or time.time()) - days * 86400))
    mac = (_mac_ids() or set()) if mac_ids is None else mac_ids
    out: dict[str, Any] = {"days": days, "mails": 0, "fetched": 0}
    counts: dict[str, Any] = {"mac": {}, "server_only": {}}
    con = _db(db)
    try:
        for acct in accounts(directory):
            try:
                conn = open_account(acct, connect)
            except (imaplib.IMAP4.error, OSError) as exc:
                out.setdefault("errors", []).append(type(exc).__name__)
                continue
            try:
                for folder in _folders(conn, acct):
                    conn.examine(folder)
                    rows = con.execute("SELECT m.* FROM messages m JOIN folders f ON f.account=m.account AND "
                                       "f.folder=m.folder AND f.uidvalidity=m.uidvalidity WHERE m.account=? AND "
                                       "m.folder=? AND m.code=0 AND m.sent_at >= ? ORDER BY m.uid DESC",
                                       (acct["name"], folder, cutoff)).fetchall()
                    out["mails"] += len(rows)
                    for i in range(0, len(rows), 50):
                        chunk = rows[i:i + 50]
                        raws = conn.fetch([r["uid"] for r in chunk], "BODY.PEEK[]")
                        for r in chunk:
                            raw = raws.pop(r["uid"], None)
                            if raw is None:
                                continue
                            out["fetched"] += 1
                            text, cal = body_text(raw), calendar_text(raw)
                            del raw
                            title = r["subject"] or ""
                            side = counts["mac" if mail_compare._norm(r["message_id"]) in mac else "server_only"]
                            side["mails"] = side.get("mails", 0) + 1
                            plain = mail_invites.detect(title, text)
                            full = plain or (bool(cal) and mail_invites.detect(title, f"{text}\n{cal}"))
                            side["has_calendar_part"] = side.get("has_calendar_part", 0) + bool(cal)
                            side["invite_plain"] = side.get("invite_plain", 0) + bool(plain)
                            side["invite_cal"] = side.get("invite_cal", 0) + bool(full and not plain)
                            if full:
                                f = mail_invites.read(title, f"{text}\n{cal}")
                                side["card_ready"] = side.get("card_ready", 0) + bool(f and f.get("date"))
                            side["bills"] = side.get("bills", 0) + bool(mail_bills.is_candidate(title, text))
                            side["suggestions"] = side.get("suggestions", 0) + bool(
                                mail_suggestions.is_candidate(title, f"{text}\n{cal}" if cal else text))
                            if model:
                                k = mail_triage.classify(title, _redact(text), llm) or "unread"
                                side.setdefault("kinds", {})[k] = side["kinds"].get(k, 0) + 1
                            del text, cal
            finally:
                conn.logout()
    finally:
        con.close()
    out["by_side"] = counts
    return out


# ── one-off: mask what was stored before redaction reached the connector (#979) ────────────────────────────────────

def _redact_strings(v: Any) -> Any:
    if isinstance(v, str):
        return _redact(v)
    if isinstance(v, list):
        return [_redact_strings(x) for x in v]
    if isinstance(v, dict):
        return {k: _redact_strings(x) for k, x in v.items()}
    return v


def redact_store(*, db: Path | None = None, backup_dir: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Re-redact the stored subjects, senders and envelopes in place, after a 0600 copy of the whole store in the
    backups folder (mail-server-<stamp>.db; backup_retention shreds it at 30 days). No connection. → counts only."""
    import kb_lifecycle
    folder = Path(backup_dir or kb_lifecycle.BACKUP_DIR)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = folder / f"mail-server-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now or time.time()))}.db"
    os.close(os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    con = _db(db)
    try:
        out = sqlite3.connect(str(dest))
        try:
            con.backup(out)
        finally:
            out.close()
        n = {"mails": 0, "subjects": 0, "senders": 0, "envelopes": 0}
        for r in con.execute("SELECT rowid, subject, sender, envelope FROM messages").fetchall():
            n["mails"] += 1
            subj, sender = _redact(r["subject"] or ""), _redact(r["sender"] or "")
            env = json.dumps(_redact_strings(json.loads(r["envelope"])), default=str) if r["envelope"] else None
            changed = {k: v for k, v, old in (("subject", subj, r["subject"] or ""), ("sender", sender, r["sender"] or ""),
                                              ("envelope", env, r["envelope"])) if v != old}
            for k in changed:
                n[k + "s"] += 1
            if changed:
                con.execute(f"UPDATE messages SET {', '.join(k + '=?' for k in changed)} WHERE rowid=?",
                            (*changed.values(), r["rowid"]))
        con.commit()
        con.execute("VACUUM")    # the masked values' old pages go too
    finally:
        con.close()
    return {**n, "backup": dest.name}


# ── sync ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def sync_folder(con: sqlite3.Connection, conn: _ReadOnly, acct: dict[str, Any], folder: str) -> dict[str, int]:
    """Headers and threads for new mail; removed mail goes. Envelopes carry over a UIDVALIDITY change by Message-ID."""
    name = acct["name"]
    validity = conn.examine(folder)
    prev = con.execute("SELECT uidvalidity FROM folders WHERE account=? AND folder=?", (name, folder)).fetchone()
    stale = prev is not None and int(prev[0]) != validity
    on_server = conn.uids()
    known = {int(r[0]) for r in con.execute(
        "SELECT uid FROM messages WHERE account=? AND folder=? AND uidvalidity=?", (name, folder, validity))}
    unseen = sorted(set(on_server) - known, reverse=True)
    new, capped = unseen[:HEADERS_MAX], len(unseen) > HEADERS_MAX
    stats = {"new": 0, "moved": 0, "removed": 0}
    for i in range(0, len(new), BATCH):
        chunk = sorted(new[i:i + BATCH])
        for uid, raw in sorted(conn.fetch(chunk, f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})]").items()):
            h = parse_headers(raw)
            mid = h["message_id"] or f"<uid-{validity}-{uid}@{folder}>"   # no usable Message-ID: one of our own
            if stale:
                # the same mail under the server's new numbering: keep its envelope, change its address
                cur = con.execute("UPDATE messages SET uidvalidity=?, uid=? WHERE account=? AND folder=? AND "
                                  "message_id=? AND uidvalidity<>?", (validity, uid, name, folder, mid, validity))
                if cur.rowcount:
                    stats["moved"] += 1
                    continue
            import mail_triage
            code = mail_triage.is_code(h["subject"], "")
            con.execute("""INSERT OR REPLACE INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id,
                           in_reply_to, refs, thread_id, sender, subject, sent_at, code)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (name, folder, validity, uid, acct["owner"], item_id(acct["owner"], name, mid), mid,
                         h["in_reply_to"], json.dumps(h["refs"]), thread_of(con, name, {**h, "message_id": mid}),
                         h["sender"], mail_triage.MASKED_TITLE if code else h["subject"], h["sent_at"], int(code)))
            stats["new"] += 1
    live = set(on_server)
    for r in con.execute("SELECT uid, uidvalidity, item_id, kb FROM messages WHERE account=? AND folder=?",
                         (name, folder)).fetchall():
        # mail under the old numbering waits until every header has been read again (it may yet carry over)
        if (int(r["uidvalidity"]) != validity and not capped) or (int(r["uidvalidity"]) == validity
                                                                   and int(r["uid"]) not in live):
            if r["kb"]:
                _kb_delete(r["item_id"])
            con.execute("DELETE FROM messages WHERE account=? AND folder=? AND uid=? AND uidvalidity=?",
                        (name, folder, r["uid"], r["uidvalidity"]))
            stats["removed"] += 1
    if stats["new"] or stats["moved"]:
        stats["rethreaded"] = rethread(con, name)
    con.execute("INSERT OR REPLACE INTO folders (account, folder, uidvalidity, synced_at) VALUES (?,?,?,?)",
                (name, folder, validity, time.time()))
    con.commit()
    return stats


def model_busy(url: str | None = None) -> bool:
    """Every slot of the local model (llama-server's /slots) is answering something. Unknown (no /slots, no
    answer) counts as not busy: the summariser falls back to the title on its own."""
    import envelopes
    import urllib.request
    try:
        with urllib.request.urlopen(f"{(url or envelopes.SUMMARISER_URL).rstrip('/')}/slots", timeout=3) as r:
            slots = json.loads(r.read())
    except Exception:  # noqa: BLE001
        return False
    return isinstance(slots, list) and bool(slots) and all(isinstance(x, dict) and x.get("is_processing") for x in slots)


def _newest_first(con: sqlite3.Connection, account: str, folders: list[str], cutoff: str, left: int,
                  keep: dict[str, dict[str, str]] | None = None
                  ) -> dict[str, list[sqlite3.Row]]:
    """The next `left` mails to derive across all the account's folders, newest first (#1410): a thread's Sent reply
    comes with its INBOX side, not after every Archive mail. Undated mail is in the window but goes last. Only mail
    under each folder's current numbering (an old UID may now be another mail). → rows by folder, newest first."""
    if left <= 0 or not folders:
        return {}
    marks = ",".join("?" * len(folders))
    rows = con.execute("SELECT m.* FROM messages m JOIN folders f ON f.account=m.account AND f.folder=m.folder AND "
                       "f.uidvalidity=m.uidvalidity WHERE m.account=? AND m.folder IN (" + marks + ") AND "
                       "m.derived_at IS NULL AND m.code=0 AND (m.sent_at IS NULL OR m.sent_at >= ?) "
                       "ORDER BY m.sent_at IS NULL, m.sent_at DESC, m.uid DESC LIMIT ?",
                       (account, *folders, cutoff, left)).fetchall()
    if keep:
        # the owner's keep rule (#955): their matching mail past the window joins, newest first, in the same budget
        old = con.execute("SELECT m.* FROM messages m JOIN folders f ON f.account=m.account AND f.folder=m.folder AND "
                          "f.uidvalidity=m.uidvalidity WHERE m.account=? AND m.folder IN (" + marks + ") AND "
                          "m.derived_at IS NULL AND m.code=0 AND m.sent_at < ? ORDER BY m.sent_at DESC, m.uid DESC",
                          (account, *folders, cutoff)).fetchall()
        rows = list(rows) + [r for r in old if kept(keep, r["thread_id"])][:left]
        # mail a rule's thread had before the rule existed: derived then, triaged out of the KB (kb=0), never yet
        # considered under a rule (kept_at). A person's is re-derived under it once; a no-reply's isn't fetched at all.
        again = con.execute("SELECT m.* FROM messages m JOIN folders f ON f.account=m.account AND f.folder=m.folder AND "
                            "f.uidvalidity=m.uidvalidity WHERE m.account=? AND m.folder IN (" + marks + ") AND "
                            "m.derived_at IS NOT NULL AND m.kb=0 AND m.code=0 AND m.kept_at IS NULL "
                            "ORDER BY m.sent_at DESC, m.uid DESC", (account, *folders)).fetchall()
        rows += [r for r in again if kept(keep, r["thread_id"]) and not automated(r["sender"], False)][:left]
        dated = sorted((r for r in rows if r["sent_at"] is not None), key=lambda r: r["sent_at"], reverse=True)
        rows = (dated + [r for r in rows if r["sent_at"] is None])[:left]
    out: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        out.setdefault(r["folder"], []).append(r)
    return out


def sync(*, account: str | None = None, connect: Callable[[dict[str, Any]], imaplib.IMAP4] = _connect_tls,
         db: Path | None = None, directory: Path | None = None, role_of: Callable[[str], str | None] = _role_of,
         kb: bool | None = None, chat: Any = None, url: str | None = None, fences: Any = None,
         write: Callable[..., Any] | None = None, observe: Callable[..., list] | None = None,
         derive_max: int | None = None, busy: Callable[[], bool] | None = None, now: float | None = None,
         derive_days: float | None = None, hooks_max: int | None = None,
         passes: dict[str, Any] | None = None) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("MAIL_SERVER isn't on")
    kb = kb_enabled() if kb is None else kb
    days = DERIVE_DAYS if derive_days is None else derive_days
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now or time.time()) - days * 86400))
    out: dict[str, Any] = {}
    con = _db(db)
    try:
        for acct in accounts(directory, role_of):
            if account and acct["name"] != account:
                continue
            try:
                conn = open_account(acct, connect)
            except (imaplib.IMAP4.error, OSError) as exc:
                out[acct["name"]] = {"error": str(exc)[:200]}   # one mailbox down never stops the others
                continue
            try:
                per: dict[str, Any] = {}
                left = DERIVE_MAX if derive_max is None else derive_max
                # headers always; envelopes wait for a free local model (the timer runs every 30 min)
                if left > 0 and (busy or (lambda: model_busy(url)))():
                    left = 0
                    per["skipped"] = "the local model is busy: envelopes wait for the next run"
                    print(f"[mail_server] {acct['name']}: local model busy, envelopes skipped this run", file=sys.stderr)
                folders = _folders(conn, acct)
                stats = {}
                for folder in folders:   # headers first, every folder
                    stats[folder] = sync_folder(con, conn, acct, folder)
                    con.commit()
                keep = keep_threads(con, acct["name"], acct.get("owner"), load_keep())   # this owner's, this account
                todo = _newest_first(con, acct["name"], folders, cutoff, left, keep)
                examined = folders[-1] if folders else None
                for folder in folders:
                    st = stats[folder]
                    rows = todo.get(folder) or []
                    if rows and folder != examined:
                        examined = folder
                        if conn.examine(folder) != rows[0]["uidvalidity"]:
                            rows = []   # renumbered since its headers were read: its mail waits for the next run
                    st["derived"] = derive(con, conn, acct, folder, rows, keep=keep, keep_cut=cutoff, kb=kb, chat=chat, url=url,
                                           fences=fences, write=write, observe=observe, passes=passes) if rows else 0
                    con.commit()
                    # older mail: hooks only (undated mail counts as recent: no date doesn't mean old), read from the original, no model (#951)
                    old = con.execute("SELECT m.* FROM messages m JOIN folders f ON f.account=m.account AND "
                                      "f.folder=m.folder AND f.uidvalidity=m.uidvalidity WHERE m.account=? AND m.folder=? "
                                      "AND m.hooked_at IS NULL AND m.code=0 AND m.sent_at < ? "
                                      "ORDER BY m.uid DESC LIMIT ?",
                                      (acct["name"], folder, cutoff, HOOKS_MAX if hooks_max is None else hooks_max)).fetchall()
                    if old and folder != examined:
                        conn.examine(folder)
                        examined = folder
                    st["hook_read"] = len(old)   # older mail fetched for hooks this run (each once: then marked)
                    st["hooked"] = _hook_only(con, conn, acct, folder, old) if old else 0   # of those, indexed
                    con.commit()
                    per[folder] = st
                out[acct["name"]] = per
            finally:
                conn.logout()
        try:   # the archive plans' audit goes after 30 days (#955 step 5)
            import mail_moves
            import mail_unsubscribe
            mail_moves.prune(db)
            mail_unsubscribe.prune(db)
        except Exception:  # noqa: BLE001
            pass
    finally:
        con.close()
    return out


def status(db: Path | None = None) -> list[dict[str, Any]]:
    con = _db(db)
    try:
        return [dict(r) for r in con.execute(
            "SELECT account, folder, COUNT(*) AS mails, SUM(derived_at IS NOT NULL) AS envelopes, SUM(code) AS codes, "
            "COUNT(DISTINCT thread_id) AS threads, SUM(kb) AS in_kb FROM messages GROUP BY account, folder")]
    finally:
        con.close()



def _ro(path: Path) -> sqlite3.Connection | None:
    """A store opened read-only, or None when it doesn't exist yet (a report never creates one)."""
    if not Path(path).exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def passes_status(hours: float = 24, db: Path | None = None, sug_db: Path | None = None,
                  now: float | None = None) -> dict[str, Any]:
    """Whether the server passes (#955 step 6) are doing their job, in counts only: no subject, sender, title or id.
    Mail categorized (all time and in the window), envelopes derived in the window, the suggestion cards made from
    server mail by state, duplicates (two cards for one mail, or two for one event change), invite cards, server mail
    the suggestions pass has seen, why the passes ran or skipped per mail (ran, mac, old, ledger, off, code), and household days over mail_suggestions.PER_DAY (a card's day is set when it's
    sent). A change card replaced by a later one is withdrawn, so only held and sent ones count as duplicates. Read-only."""
    import mail_suggestions
    now = time.time() if now is None else now
    cut = now - hours * 3600
    out: dict[str, Any] = {"hours": hours, "categorized": {}, "categorized_in_window": {}, "derived_in_window": 0,
                           "passes_in_window": {},
                           "cards_in_window": {}, "duplicate_mail_cards": 0, "duplicate_change_cards": 0,
                           "invite_cards_in_window": 0, "seen_server_in_window": 0, "days_over_budget": 0}
    con = _ro(Path(db or DB))
    if con is not None:
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
            if "category" in cols:
                out["categorized"] = {r[0]: r[1] for r in con.execute(
                    "SELECT category, COUNT(*) FROM messages WHERE category IS NOT NULL GROUP BY category ORDER BY category")}
                out["categorized_in_window"] = {r[0]: r[1] for r in con.execute(
                    "SELECT category, COUNT(*) FROM messages WHERE category IS NOT NULL AND derived_at >= ? "
                    "GROUP BY category ORDER BY category", (cut,))}
            out["derived_in_window"] = con.execute("SELECT COUNT(*) FROM messages WHERE derived_at >= ?", (cut,)).fetchone()[0]
            if "passes" in cols:   # why the passes ran or not, per mail derived in the window (null: derived before)
                out["passes_in_window"] = {(r[0] or "unrecorded"): r[1] for r in con.execute(
                    "SELECT passes, COUNT(*) FROM messages WHERE derived_at >= ? GROUP BY passes ORDER BY passes", (cut,))}
        finally:
            con.close()
    con = _ro(Path(sug_db or mail_suggestions.DB))
    if con is not None:
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "cards" in tables:
                ccols = {r[1] for r in con.execute("PRAGMA table_info(cards)")}
                srv = "mail_id LIKE 'cx-mailsrv-%' AND ts >= ?"
                out["cards_in_window"] = {r[0]: r[1] for r in con.execute(
                    f"SELECT state, COUNT(*) FROM cards WHERE {srv} GROUP BY state ORDER BY state", (cut,))}
                out["duplicate_mail_cards"] = con.execute(
                    f"SELECT COUNT(*) FROM (SELECT mail_id FROM cards WHERE {srv} GROUP BY mail_id HAVING COUNT(*) > 1)",
                    (cut,)).fetchone()[0]
                if {"event", "target"} <= ccols:
                    out["duplicate_change_cards"] = con.execute(
                        "SELECT COUNT(*) FROM (SELECT 1 FROM cards WHERE ts >= ? AND event IS NOT NULL AND state IN ('held', 'sent') "
                        "GROUP BY owner, event, target HAVING COUNT(*) > 1)", (cut,)).fetchone()[0]
                out["invite_cards_in_window"] = con.execute(
                    f"SELECT COUNT(*) FROM cards WHERE key LIKE 'mailsug-inv-%' AND {srv}", (cut,)).fetchone()[0]
                out["days_over_budget"] = con.execute(
                    "SELECT COUNT(*) FROM (SELECT 1 FROM cards WHERE ts >= ? AND day IS NOT NULL "
                    "GROUP BY owner, day HAVING COUNT(*) > ?)", (cut, mail_suggestions.PER_DAY)).fetchone()[0]
            if "seen" in tables:
                out["seen_server_in_window"] = con.execute(
                    "SELECT COUNT(*) FROM seen WHERE mail_id LIKE 'cx-mailsrv-%' AND ts >= ?", (cut,)).fetchone()[0]
        finally:
            con.close()
    return out

# ── one writer at a time ──────────────────────────────────────────────────────────────────────────────────────────

WRITERS = frozenset({"sync", "rederive", "hooks-backfill", "rethread", "redact-store"})   # read-only commands never lock


def lock_path() -> Path:
    return Path(os.environ.get("MAIL_SERVER_LOCK") or str(DB) + ".lock")


class store_lock:
    """The store's one-writer lock (#1411): the timer's sync, mail_idle's, a manual run and the other writing commands
    take turns; the second waits (a queued timer run then derives only what's left, never lost). The file is 0600 in the
    store's own directory and names only the holder's command and pid (never an argument). The kernel drops it whenever
    the holder exits: an exception, systemd's SIGTERM or a SIGKILL. The waiting side logs the holder's command and how
    long it waited, so a long wait shows in the journal."""

    def __init__(self, cmd: str, path: Path | None = None, *, log: Callable[[str], None] | None = None) -> None:
        self.cmd, self.path = cmd, path or lock_path()
        self.log = log or (lambda line: print(line, file=sys.stderr))
        self.fd: int | None = None
        self.waited = 0.0

    def __enter__(self) -> "store_lock":
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass   # someone else's file: the lock still works; its mode is theirs to fix
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = self._holder(fd)
            self.log(f"[mail_server] {self.cmd}: waiting for the store lock (held by {holder})")
            t0 = time.monotonic()
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.waited = time.monotonic() - t0
            self.log(f"[mail_server] {self.cmd}: got the store lock after {self.waited:.0f}s (it was held by {holder})")
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{self.cmd} {os.getpid()}\n".encode(), 0)
        self.fd = fd
        return self

    @staticmethod
    def _holder(fd: int) -> str:
        try:
            got = os.pread(fd, 80, 0).decode(errors="replace").split()
        except OSError:
            got = []
        name = got[0] if got and got[0] in WRITERS else "another run"
        return f"{name}, pid {got[1]}" if len(got) > 1 and got[1].isdigit() else name

    def __exit__(self, *exc: Any) -> None:
        if self.fd is not None:
            try:
                os.ftruncate(self.fd, 0)
            except OSError:
                pass
            os.close(self.fd)   # closing drops the flock
            self.fd = None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--account")
    sub.add_parser("status")
    cc = sub.add_parser("cutover-check", help="before MAIL_SERVER_REPLACES_MAC=1: each server account names one Mac account seen")
    cc.add_argument("--owner", required=True)
    ps = sub.add_parser("passes-status", help="the server passes' outcomes in counts only (#955): categories, cards, duplicates")
    ps.add_argument("--hours", type=float, default=24)
    rd = sub.add_parser("rederive", help="derive the last N days again (cut-over: through the passes and the KB)")
    rd.add_argument("--days", type=float, default=DERIVE_DAYS)
    sub.add_parser("redact-store", help="mask stored subjects, senders and envelopes (#979), after a 0600 backup")
    hb = sub.add_parser("hooks-backfill", help="index link hooks for mail derived before #951 (one read-only fetch each)")
    hb.add_argument("--revector", action="store_true", help="hooked mail with no topic vector: hooks and vector again")
    hb.add_argument("--limit", type=int, help="at most this many (default MAIL_SERVER_REVECTOR_MAX, 500)")
    sub.add_parser("rethread", help="join linked mails already in the store into their threads (no connection)")
    dr = sub.add_parser("dryrun-passes", help="read-only: what the passes would do to the last N days, counts only")
    dr.add_argument("--days", type=float, default=14)
    dr.add_argument("--model", action="store_true", help="also triage each mail with the local model (counts only)")
    a = ap.parse_args()
    if a.cmd == "dryrun-passes":
        print(json.dumps(dryrun_passes(a.days, model=a.model), indent=1))
        return 0
    if a.cmd in WRITERS:
        with store_lock(a.cmd):
            return _write_cmd(a)
    if a.cmd == "status":
        print(json.dumps(status(), indent=1))
        return 0
    if a.cmd == "cutover-check":
        ok, lines = cutover_check(a.owner)
        print("\n".join(lines) + ("\nok: the cut-over can go ahead" if ok else "\nREFUSED: fix the above before the cut-over"))
        return 0 if ok else 1
    if a.cmd == "passes-status":
        print(json.dumps(passes_status(a.hours), indent=1))
        return 0
    return 2


def _write_cmd(a: argparse.Namespace) -> int:
    """The commands that write the store, each run under store_lock (one at a time)."""
    if a.cmd == "rederive":
        print(json.dumps(rederive(a.days)))
        return 0
    if a.cmd == "redact-store":
        print(json.dumps(redact_store()))
        return 0
    if a.cmd == "rethread":
        con = _db()
        try:
            n = {acct: rethread(con, acct) for (acct,) in con.execute("SELECT DISTINCT account FROM messages")}
            con.commit()
        finally:
            con.close()
        print(json.dumps({"rethreaded": n}))
        return 0
    try:
        if a.cmd == "hooks-backfill":
            print(json.dumps(hooks_backfill(revector=a.revector, limit=a.limit)))
        else:   # sync (the timer's and one mail_idle.py starts on new mail, #955)
            print(json.dumps(sync(account=a.account), indent=1))
    except (AccountError, RuntimeError, imaplib.IMAP4.error, OSError) as exc:
        print(f"mail_server: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
