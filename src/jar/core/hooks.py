#!/usr/bin/env python3
"""Link hooks (titan-node#951 (a)): what an item could be linked by, read in code from the item's original.

Zach, 10-05: threading must be automatic and general ("making it manual defeats the purpose"). Every item (a mail, a
scan, a record, later a text or a calendar item) gets hooks; the linker (#951 (b)) joins items that share them.

  identifiers  id:<label>   a labelled code (order / case / ticket / confirmation / reference / claim / request /
                            RMA / invoice / booking / account / tracking …), UPS and USPS tracking shapes, a labelled
                            IMEI, a derived record's source item. The key ignores the label: "order #X" in a text and
                            "Order X" in a mail are one identifier.
  parties      party:domain the sender's registrable domain (not a shared mailbox provider like gmail.com)
               party:addr   the sender's address        party:phone  a phone number or short code
               party:to     each To/Cc address (#1399's `to` edges), none when a mail has more than MAX_RECIPIENTS
                            (a list or a blast); read by the graph, never a linking key (the household's own
                            addresses are on nearly every mail: recipients would join unrelated mail)
               party:entity a world-model entity named in the title or sender, through its owner's aliases
               mention:entity  one named only in the body: never a party
               mention:place   a registry place a device's text names (its id from places.db, #1436); never free text
               mention:person  a household member a device's text names, by household person id only (#1396); never a
                               name, never anyone outside the household
               party:vendor a record's vendor, normalised (obligations.account_of)
  threads      thread:device a device's own opaque id for a text's conversation, 16 lowercase hex, kept as it is: the
                            same per-conversation thread_ref the Mac puts on an event proposal (jellybean#1189, from
                            Messages' chat_message_join), so a kept plan's texts and every other text of that
                            conversation carry one value. Never joined by the linker (it pairs id: and party: kinds
                            only); graph_edges links one owner's texts with one value on one household day (#1650)
               reply:device a text's inline reply (#1650): the device's own item id of the text it answers (Messages'
                            reply_to_guid / thread_originator_guid, mapped by the device to its own ids). titan mints
                            it as it mints the item's own id, under the poster and their device, and keeps it only
                            when that item is already one of the poster's: never another person's item
  time, money  date:item, date:mention (ISO day) · amount (dollars and cents)

A device's party hook may say its direction (#1436): {"dir": "from" | "to"} (or the kind party:phone:from …), kept in
the locator. A date:mention it sends may carry a time of day ("2026-10-11T10:00", household-zone local): the key stays
the day (what the linker matches) and {"time": "10:00"} goes in the locator.

Privacy: identifiers, addresses and phones are kept only as an HMAC-SHA256 under a per-house salt that never leaves
titan (LINK_SALT_FILE, /ai/secrets/link_salt, 0600; created on first use). Linking needs only equality, so no code,
number, address or phone is stored or shown. Domains, entity ids, amounts and dates are kept as they are. Each hook
carries a locator ({part, line}) so an edge can join ranges, not whole items (rule 4).

Hooks come from originals, never summaries (#954): envelope_pass indexes them before it summarises, and the backfill
reads the migrated mails' originals from their 0600 backups (before the 30-day shred).

  python3 scripts/hooks.py --backfill            dry run: counts per hook kind and source, never a value
  python3 scripts/hooks.py --backfill --apply    index them in links.db
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

SALT_FILE = Path(os.environ.get("LINK_SALT_FILE", "/ai/secrets/link_salt"))
# Its own file, beside the world model's bubbles (never inside WORLD_DIR: every .db there is read as a bubble).
LINKS_DB = Path(os.environ.get("LINKS_DB", "/ai/datasets/connectors/links.db"))
BACKUP_DIR = Path(os.environ.get("ENVELOPE_BACKUP_DIR", "/ai/backups/envelopes"))
VERSION = 2   # 2: party:to (#1399)
PUBLIC_MAIL = frozenset("gmail.com googlemail.com icloud.com me.com mac.com yahoo.com outlook.com hotmail.com live.com "
                        "msn.com aol.com proton.me protonmail.com gmx.com hey.com fastmail.com".split())
_LABELS = {"order": "order", "case": "case", "ticket": "ticket", "confirmation": "confirmation", "conf": "confirmation",
           "reference": "reference", "ref": "reference", "claim": "claim", "request": "request", "rma": "rma",
           "invoice": "invoice", "booking": "booking", "reservation": "reservation", "account": "account",
           "acct": "account", "tracking": "tracking", "trade-in": "trade-in", "return": "return", "policy": "policy",
           "member": "member", "membership": "member", "transaction": "transaction", "receipt": "receipt"}
_ID_RX = re.compile(r"(?i)\b(" + "|".join(sorted(map(re.escape, _LABELS), key=len, reverse=True)) +
                    r")\b\s*(?:number|no\.?|num\.?|#|id)?\s*[:#]?\s*#?\s*([A-Z0-9][A-Z0-9-]{4,23}[A-Z0-9])\b")
_UPS = re.compile(r"\b1Z[0-9A-Z]{16}\b")
_USPS = re.compile(r"\b9[1-5]\d{18,20}\b")
_IMEI = re.compile(r"(?i)\b(?:imei|meid)\b\D{0,12}(\d{14,15})\b")
_PHONE = re.compile(r"(?<![\w-])(?:\+?1[\s.-]?)?\(?([2-9]\d{2})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?![\w-])")
_AMOUNT = re.compile(r"(?<![\w.])\$\s?((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})(?!\d)")
_OFFER = re.compile(r"(?i)\b(?:save|saving|only|just|from|starting(?: at)?|as low as|under|up to|get|extra|deals?|"
                    r"for as little as|less than)\s*$")
_OFFER_AFTER = re.compile(r"(?i)^\s*(?:off|discount|savings?|credit toward|or less|and up|each|per (?:month|item))\b")
_MON = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_D_MDY = re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
                    r"(?:,?\s+(\d{4}))?\b")
_D_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b")
_D_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_HEADER = re.compile(r"^(From|To|Cc|Date|Subject|Message-ID|References|In-Reply-To):\s*(.*)$", re.I)
_ADDR = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
MAX_RECIPIENTS = 10   # more To/Cc than this: a list or a blast, so no party:to at all (#1399)


@dataclass
class Hook:
    kind: str
    key: str
    at: str | None = None                 # the item's own day (ISO), for timing
    locator: dict[str, Any] = field(default_factory=dict)


def _salt() -> bytes:
    if not SALT_FILE.exists():
        SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(SALT_FILE), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    return SALT_FILE.read_bytes()


def keyed(kind: str, value: str) -> str:
    """The HMAC an identifier, address or phone is kept as: equality only, never the value."""
    return hmac.new(_salt(), f"{kind}\0{value}".encode(), hashlib.sha256).hexdigest()[:24]


def registrable(domain: str) -> str:
    parts = domain.lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "gov", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _day(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _near_year(m: int, d: int, around: date | None) -> str | None:
    """A month and day without a year: the year that puts it nearest the item's own date."""
    import local_time
    base = around or local_time.today()
    best = None
    for y in (base.year - 1, base.year, base.year + 1):
        s = _day(y, m, d)
        if s and (best is None or abs((date.fromisoformat(s) - base).days) < abs((date.fromisoformat(best) - base).days)):
            best = s
    return best


def mentioned_dates(text: str, around: date | None) -> list[tuple[str, int]]:
    out = []
    for i, line in enumerate(text.splitlines()):
        for m in _D_ISO.finditer(line):
            s = _day(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if s:
                out.append((s, i))
        for m in _D_MDY.finditer(line):
            mon = _MON.get(m.group(1).lower()[:3])
            if not mon:
                continue
            s = _day(int(m.group(3)), mon, int(m.group(2))) if m.group(3) else _near_year(mon, int(m.group(2)), around)
            if s:
                out.append((s, i))
        for m in _D_NUM.finditer(line):
            y = int(m.group(3))
            s = _day(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))
            if s:
                out.append((s, i))
    return out


def extract(item: dict[str, Any], entities: Any = None) -> list[Hook]:
    """Hooks for one item. item: {text, title, at (ISO day), source, derived_from, vendor, amount, dates…}.
    entities: a callable(text) → entity ids the item names (the owner's world model), or None."""
    text = str(item.get("text") or "")
    title = str(item.get("title") or "")
    lines = ([title] if title else []) + text.splitlines()
    at = str(item.get("at") or "")[:10] or None
    out: list[Hook] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, key: str, part: str, line: int) -> None:
        if key and (kind, key) not in seen:
            seen.add((kind, key))
            out.append(Hook(kind, key, at, {"part": part, "line": line}))

    body_from = 0
    recipients: dict[str, int] = {}   # address → the header line it was on (To/Cc, folded lines too)
    last = None
    for i, ln in enumerate(lines):
        if last in ("to", "cc") and ln[:1] in (" ", "\t") and ln.strip() and body_from == 0:
            for m in _ADDR.finditer(ln):
                recipients.setdefault(m.group(0).lower(), i)
            continue
        h = _HEADER.match(ln.strip())
        if not h:
            last = None
            if i > 0 and ln.strip() == "" and body_from == 0:
                body_from = i
            continue
        name, val = h.group(1).lower(), h.group(2)
        last = name
        if name in ("to", "cc") and body_from == 0:
            for m in _ADDR.finditer(val):
                recipients.setdefault(m.group(0).lower(), i)
        if name == "from":
            m = _ADDR.search(val)
            if m:
                dom = registrable(m.group(1))
                add("party:addr", keyed("addr", m.group(0).lower()), "header", i)
                if dom not in PUBLIC_MAIL:
                    add("party:domain", dom, "header", i)
        elif name in ("message-id", "references", "in-reply-to"):
            for mid in re.findall(r"<([^<>\s]+)>", val):
                add("id:message", keyed("id", mid.lower()), "header", i)
        elif name == "date" and not at:
            ds = mentioned_dates(val, None)
            at = ds[0][0] if ds else at
    if 0 < len(recipients) <= MAX_RECIPIENTS:
        for addr, line in recipients.items():
            add("party:to", keyed("addr", addr), "header", line)
    around = date.fromisoformat(at) if at else None
    for o in out:
        o.at = at
    for i, ln in enumerate(lines):
        part = "title" if title and i == 0 else ("header" if _HEADER.match(ln.strip()) else "body")
        for m in _ID_RX.finditer(ln):
            val = m.group(2).upper()
            if sum(c.isdigit() for c in val) >= 2:
                add(f"id:{_LABELS[m.group(1).lower()]}", keyed("id", val.replace("-", "")), part, i)
        for rx in (_UPS, _USPS):
            for m in rx.finditer(ln):
                add("id:tracking", keyed("id", m.group(0).upper()), part, i)
        for m in _IMEI.finditer(ln):
            add("id:imei", keyed("id", m.group(1)), part, i)
        for m in _PHONE.finditer(ln):
            if not _ID_RX.search(ln[max(0, m.start() - 30):m.start()]):   # not a number inside a labelled code
                add("party:phone", keyed("phone", "".join(m.groups())), part, i)
        for m in _AMOUNT.finditer(ln):
            if _OFFER.search(ln[max(0, m.start() - 24):m.start()]) or _OFFER_AFTER.match(ln[m.end():m.end() + 16]):
                continue   # an offer ("$2 off", "save $5", "from $9.99"), not a transaction: links nothing (10-05)
            add("amount", m.group(1).replace(",", ""), part, i)
    for d, i in mentioned_dates("\n".join(lines), around):
        if d != at:
            add("date:mention", d, "body", i)
    if at:
        add("date:item", at, "item", 0)
    sender = str(item.get("sender") or "").strip()   # a text's sender: a phone or short code (the Mac sends it raw)
    if sender:
        digits = re.sub(r"\D", "", sender)
        if digits:
            add("party:phone", keyed("phone", digits[-10:]), "header", 0)
    if item.get("derived_from"):                     # a record filed from a mail is that mail's manifestation
        add("id:source", keyed("id", str(item["derived_from"])), "item", 0)
    if item.get("vendor"):
        import obligations
        add("party:vendor", obligations.account_of(item["vendor"]), "item", 0)
    if item.get("amount") not in (None, ""):
        try:
            add("amount", f"{float(item['amount']):.2f}", "item", 0)
        except (TypeError, ValueError):
            pass
    for k in ("due_date", "paid_date"):
        if item.get(k):
            add("date:mention", str(item[k])[:10], "item", 0)
    if entities is not None:
        # Where an entity is named matters (#951, 10-05: newsletters that only mention a company in their body were
        # threaded with its order mails). Named in the title or the sender: a party. Only in the body: a mention,
        # never a party (kept for topic similarity later).
        head = "\n".join([title] + [ln for ln in lines if re.match(r"(?i)^(from|subject):", ln.strip())])
        try:
            in_head = {str(e) for e in (entities(head) or [])}
            for eid in sorted(in_head):
                add("party:entity", eid, "header", 0)
            for eid in entities(f"{title}\n{text}") or []:
                if str(eid) not in in_head:
                    add("mention:entity", str(eid), "body", 0)
        except Exception:  # noqa: BLE001 - no world model here: no entity hooks
            pass
    return out


# ── the index (links.db) ──────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS hooks (item_id TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
    at TEXT, locator TEXT NOT NULL DEFAULT '{}', owner TEXT, audience TEXT, made_at REAL NOT NULL,
    version INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS idx_hooks_key ON hooks(kind, key);
CREATE INDEX IF NOT EXISTS idx_hooks_item ON hooks(item_id);
CREATE INDEX IF NOT EXISTS idx_hooks_source ON hooks(source, item_id);
"""

# idx_hooks_source (10-10 review): the by-source reads (a conversation's hooks, a source's items) scanned the table.
# (source, item_id), not (source) alone: with no ANALYZE stats, a bare source index won the planner over idx_hooks_item
# and idx_hooks_key for the selective reads (a source's two items; a day's text hooks) and made them slower.

# The links.db files this process has already set up, by (path, device, inode): every request opened links.db through
# connect() and re-ran all of it (the schema script with its implicit commit, a stat, a chmod, a WAL pragma). Once per
# process and file now. A file deleted and made again can get the same inode back (ext4 reuses them at once: the PR
# gate on titan caught it), so a remembered file is trusted only while its schema is there (_has_schema: one lookup in
# sqlite_master, no write, no commit), and a file connect() makes itself is always set up. _READY: SCHEMA ran
# (ensure); _OPENED: connect() also set the mode and WAL (ensure alone, on another opener's connection, doesn't).
_READY: set[tuple[str, int, int]] = set()
_OPENED: set[tuple[str, int, int]] = set()
_READY_LOCK = threading.Lock()


def _file_key(path: str) -> tuple[str, int, int] | None:
    """(path, device, inode) of an on-disk database, None for an in-memory or temporary one (always set up)."""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (path, st.st_dev, st.st_ino)


_SCHEMA_NAMES = ("hooks", "idx_hooks_key", "idx_hooks_item", "idx_hooks_source")


def _has_schema(con: sqlite3.Connection, names: tuple[str, ...] = _SCHEMA_NAMES) -> bool:
    q = f"SELECT COUNT(*) FROM sqlite_master WHERE name IN ({','.join('?' * len(names))})"
    return con.execute(q, names).fetchone()[0] == len(names)


def _main_path(con: sqlite3.Connection) -> str:
    return next((r[2] for r in con.execute("PRAGMA database_list") if r[1] == "main"), "") or ""


def ensure(con: sqlite3.Connection) -> None:
    key = _file_key(_main_path(con))
    if key is not None and key in _READY and _has_schema(con):
        return
    con.executescript(SCHEMA)
    if key is not None:
        with _READY_LOCK:
            _READY.add(key)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """The link index (hooks, and the linker's edges): private by file mode (0640), WAL. The mode, WAL and schema are
    set once per process and file (_OPENED), not on every open."""
    p = Path(path or LINKS_DB)
    made = not p.exists()
    if made:
        p.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o640))
    con = sqlite3.connect(str(p), check_same_thread=False)
    key = _file_key(_main_path(con))
    if made and key is not None:
        with _READY_LOCK:
            _READY.discard(key)
            _OPENED.discard(key)
    elif key is not None and key in _OPENED and _has_schema(con):
        return con
    if p.stat().st_mode & 0o007:
        os.chmod(p, 0o640)
    con.execute("PRAGMA journal_mode=WAL")
    ensure(con)
    if key is not None:
        with _READY_LOCK:
            _OPENED.add(key)
    return con


def _thread_carry(con: sqlite3.Connection, item_id: str, posted: list[Hook]) -> list[Hook]:
    """What index()'s replace must keep of an item's hooks when a device posts it again (jellybean#1189, Box's review
    of jellybean#1218): a thread:device is additive. A post of only thread:device hooks (a kept plan's texts tied to
    its thread) keeps every other hook the text already has; any other post keeps the thread:device it already has.
    Hooks of a kind the post carries itself are replaced as before (it's the device's latest word on them)."""
    brought = {h.kind for h in posted}
    kinds = brought - {"date:item"}
    if not kinds:
        return []
    thread_only = kinds == {"thread:device"}
    keep = []
    ensure(con)
    for kind, key, at, loc in con.execute("SELECT kind, key, at, locator FROM hooks WHERE item_id=?", (item_id,)):
        if kind == "id:source" or kind in brought:
            continue                                   # index() adds id:source; a kind the post brings is its own
        if thread_only or kind == "thread:device":
            keep.append(Hook(kind, key, at, json.loads(loc or "{}")))
    return keep


def index(con: sqlite3.Connection, item_id: str, source: str, hooks: Iterable[Hook], owner: str | None,
          audience: str | None) -> int:
    """Replace one item's hooks. → how many."""
    ensure(con)
    con.execute("DELETE FROM hooks WHERE item_id=?", (item_id,))
    hooks = list(hooks)
    # the item's own id, so a record filed from it (id:source, its derived_from) finds it
    hooks.append(Hook("id:source", keyed("id", item_id), hooks[0].at if hooks else None, {"part": "item", "line": 0}))
    rows = [(item_id, source, h.kind, h.key, h.at, json.dumps(h.locator), owner, audience, time.time(), VERSION)
            for h in hooks]
    con.executemany("INSERT INTO hooks VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def entity_finder(owner: str | None, role: str | None = None):
    """The owner's world-model entities named in a text, or None when there's no world model."""
    try:
        import world
        w = world.get()
        try:   # the household's own people are the recipients, never a counterparty: they link nothing
            import household_identity as hh
            members = {str(p.get("name") or "").strip().lower() for p in hh.get_store().people()} - {""}
        except Exception:  # noqa: BLE001
            members = set()

        def find(text: str) -> list[str]:
            out = []
            for eid in w.find(text, owner, role or "admin") or []:
                row = w.entity(eid)
                ent = dict(row) if row is not None else {}   # a sqlite3.Row has no .get
                name = str(ent.get("name") or "").strip().lower()
                if name and (name in members or name.split()[0] in members) and str(ent.get("type") or "person") == "person":
                    continue
                out.append(eid)
            return out
        return find
    except Exception:  # noqa: BLE001
        return None


class HookError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


_DEVICE_KINDS = {"party:phone", "party:addr", "party:domain", "amount", "date:mention", "date:item", "mention:place",
                 "mention:person", "thread:device", "reply:device"} | \
    {f"id:{v}" for v in set(_LABELS.values()) | {"tracking", "imei", "message"}}
_DIRECTED = ("party:phone", "party:addr")                    # a text's sender or recipient: the device knows which
_DIRS = ("from", "to")
_DAY_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?")
_PLACE_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_THREAD_REF = re.compile(r"[0-9a-f]{16}")                    # event_proposals' thread_ref: already an opaque hash


def _day_time(val: str) -> tuple[str, str | None] | None:
    """A device's mentioned date: an ISO day, or a household-zone local day and time of day (HH:MM; seconds dropped;
    never an offset) → (day, "HH:MM" or None); None when it isn't one."""
    m = _DAY_TIME.fullmatch(val)
    if not m:
        return None
    day = _day(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if not day:
        return None
    if m.group(4) is None:
        return day, None
    hh, mm, ss = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return day, f"{hh:02d}:{mm:02d}"


def _places_for(person: str, role: str | None) -> set[str]:
    """The registry place ids this person may see (places.db `places`, read-only): what a mention:place may name.
    Empty when there's no registry yet. Another person's private place is refused like an unknown one."""
    import audience as A
    import places
    p = Path(os.environ.get("PLACES_DB") or places.DB_PATH)
    if not p.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute("SELECT * FROM places").fetchall()]
        finally:
            con.close()
    except sqlite3.Error:
        return set()                                         # no registry table yet
    return {str(r["id"]) for r in rows if not r.get("archived_at")      # a removed place (#1510) is no longer named
            and A.visible(r.get("audience") or "household", person, role, owner=r.get("owner"))}


HOUSEHOLD_ROLES = ("admin", "adult", "kid")                  # a member; a shared screen ("household") is a device


def _members() -> set[str]:
    """The household's own person ids (household_identity `people`, roles admin, adult, kid): what a mention:person may
    name. Never a shared screen, never a contact or anyone outside the house. Empty when the registry can't be read,
    so every mention:person is dropped rather than trusted."""
    try:
        import household_identity as hh
        return {str(p["id"]) for p in hh.get_store().people() if p.get("role") in HOUSEHOLD_ROLES and p.get("id")}
    except Exception:  # noqa: BLE001
        return set()


def put_device(person: str, role: str | None, device_id: str | None, body: dict[str, Any],
               con: sqlite3.Connection | None = None, accounts: Any = None) -> dict[str, Any]:
    """POST /v1/hooks: a person's own device sends one of its items' hooks as raw values over the paired channel
    (a text's order number, its sender's short code, a date, an amount). The salt never leaves titan: titan keys the
    identifiers, addresses and phones here, on arrival, and never stores, logs or returns a raw value. Never a body.
    body: {item_id (the device's own id), source ("text", "calendar"…), at (ISO day), hooks: [{kind, value, line?, dir?}]}
    (#1436: a party:phone/party:addr may say dir "from" or "to", or come as party:phone:from …; a date:mention may carry
    a local time, "2026-10-11T10:00"; a mention:place's value is a registry place id the poster may see; a
    mention:person's value is a household person id, one per hook, the device's own resolution of a member its text
    names (#1396): never a name, never a contact; a thread:device is the text's conversation, the same 16 lowercase
    hex thread_ref the device puts on that conversation's event proposals (jellybean#1189, #1650); a reply:device is
    the device's own item_id of the text this one answers inline, one per item, #1650). A hook that doesn't hold (an
    unknown kind, a bad direction, a date that isn't one, a place not in the registry or not the poster's to see, a
    person who isn't a household member's id, a reply to an item that isn't already the poster's (or to itself, or a
    second parent), an empty or unreadable value) drops that hook only, counted in the reply under a fixed label:
    {item_id, hooks, dropped: {unknown_kind | direction | date | place | person | reply | value: n}}, never a value or
    a kind.
    Every hook is the poster's own (owner the poster, audience private) on the poster's own item: the item id is
    minted from the poster, their device and the device's id, so no device can put a hook on another person's item.
    Only a malformed body is refused (400),
    or {kind: "event_signal", …}: a text's reschedule or cancel of the poster's own event (event_signals.py),
    or {kind: "obligation_signal", …}: a text's payment or failed payment on one of the poster's own obligations
    (obligation_signals.py; accounts() → their {account_ref: account}),
    or {kind: "event_proposal", …}: a plan agreed over texts, an "Add to calendar?" card to the poster (event_proposals.py),
    or {kind: "meeting_summary", …}: a transcript on the poster's Mac, read once for their notes (meeting_notes.py)."""
    if not device_id:
        raise HookError(403, "hooks come from the owner's own device")
    if role == "household":
        raise HookError(403, "a shared device has no personal items")
    if isinstance(body, dict) and body.get("kind") == "meeting_summary":
        # a transcript on the poster's Mac (#1364 B′): counts only; titan reads it once later, for the poster's notes
        import meeting_notes
        try:
            return meeting_notes.put(person, body, con or connect(), role=role)
        except meeting_notes.SummaryError as exc:
            raise HookError(exc.status, str(exc)) from None
    if isinstance(body, dict) and body.get("kind") == "event_proposal":
        # a plan agreed over texts (#1363): a derived fact, strictly shaped, never text; a card asks the poster only
        import event_proposals
        try:
            return event_proposals.put(person, body, con or connect())
        except event_proposals.ProposalError as exc:
            raise HookError(400, str(exc)) from None
    if isinstance(body, dict) and body.get("kind") == "event_signal":
        # a text that moves or cancels one of the poster's own events: a derived fact, strictly shaped, never text
        import event_signals
        try:
            return event_signals.put(person, body, con or connect())
        except event_signals.SignalError as exc:
            raise HookError(400, str(exc)) from None
    if isinstance(body, dict) and body.get("kind") == "obligation_signal":
        import obligation_signals
        try:
            return obligation_signals.put(person, role, body, con or connect(), accounts or (lambda: {}))
        except obligation_signals.SignalError as exc:
            raise HookError(exc.status, str(exc)) from None
    if not isinstance(body, dict) or any(k in body for k in ("text", "body", "content", "message")):
        raise HookError(400, "hooks only: never an item's text")
    native = str(body.get("item_id") or "").strip()
    source = re.sub(r"[^a-z]", "", str(body.get("source") or "").lower())[:20]
    raw = body.get("hooks")
    if not native or not source or not isinstance(raw, list) or len(raw) > 200:
        raise HookError(400, "item_id, source and a list of hooks are required")
    at = str(body.get("at") or "")[:10]
    at = at if re.fullmatch(r"\d{4}-\d{2}-\d{2}", at) else None
    out: list[Hook] = []
    known: set[str] | None = None
    members: set[str] | None = None
    named: set[str] = set()
    parents: list[tuple[str, dict[str, Any]]] = []
    dropped: dict[str, int] = {}
    iid = f"dev-{keyed('item', f'{person}|{device_id}|{native}')[:16]}"   # the device's own id, never stored raw

    def drop(reason: str) -> None:                           # a fixed label, never the hook's value or kind
        dropped[reason] = dropped.get(reason, 0) + 1

    for h in raw:
        h = h if isinstance(h, dict) else {}
        kind = str(h.get("kind") or "")
        d = h.get("dir")
        m = re.fullmatch(r"(party:(?:phone|addr)):(from|to)", kind)
        if m:
            if d not in (None, m.group(2)):
                drop("direction")                            # two directions: neither is trusted
                continue
            kind, d = m.group(1), m.group(2)
        if kind not in _DEVICE_KINDS:
            drop("unknown_kind")                             # e.g. a newer device's kind: this hook only
            continue
        if d is not None and (kind not in _DIRECTED or d not in _DIRS):
            drop("direction")
            continue
        val = str(h.get("value") or "").strip()
        if not val:
            drop("value")
            continue
        loc: dict[str, Any] = {"part": "body", "line": int(h.get("line") or 0)} \
            if str(h.get("line") or "0").isdigit() else {"part": "body"}
        if d is not None:
            loc.update(part="header", dir=d)                 # its sender or recipient, like a mail's From/To header
        if kind.startswith("id:"):
            key = keyed("id", re.sub(r"[\s-]", "", val.upper()))
        elif kind == "party:phone":
            digits = re.sub(r"\D", "", val)
            if not digits:
                drop("value")
                continue
            key = keyed("phone", digits[-10:])
        elif kind == "party:addr":
            key = keyed("addr", val.lower())
        elif kind == "party:domain":
            key = registrable(val.split("@")[-1])
            if key in PUBLIC_MAIL:
                continue
        elif kind == "amount":
            try:
                key = f"{float(val.replace('$', '').replace(',', '')):.2f}"
            except ValueError:
                drop("value")
                continue
        elif kind == "mention:place":
            if known is None:
                known = _places_for(person, role)
            if not _PLACE_ID.fullmatch(val) or val not in known:
                drop("place")                                # unknown, a name, or not the poster's to see: alike
                continue
            key = val
        elif kind == "mention:person":
            if members is None:
                members = _members()
            if val not in members:
                drop("person")                               # a name, a contact, an unknown id, a screen: alike, unkept
                continue
            if val in named:
                continue                                     # one row per member per item
            named.add(val)
            key = val
        elif kind == "thread:device":
            if not _THREAD_REF.fullmatch(val):
                drop("value")                                # never a chat id or handle: 16 lowercase hex only
                continue
            key = val
        elif kind == "reply:device":
            # the parent, minted exactly as an item's own id: under this poster and this device, so it can only ever
            # name the poster's own item; kept below only if that item is already indexed as the poster's
            parents.append((f"dev-{keyed('item', f'{person}|{device_id}|{val}')[:16]}", loc))
            continue
        elif kind == "date:mention":
            dt = _day_time(val)
            if dt is None:
                drop("date")
                continue
            key = dt[0]                                       # the day: what the linker matches
            if dt[1]:
                loc["time"] = dt[1]
        else:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val[:10]):
                drop("date")
                continue
            key = val[:10]
        out.append(Hook(kind, key, at, loc))
    if at:
        out.append(Hook("date:item", at, at, {"part": "item", "line": 0}))
    con = con or connect()
    if parents:
        distinct = {p for p, _ in parents}                   # the same parent twice is one
        parent, ploc = parents[0]
        ensure(con)
        if len(distinct) > 1:
            dropped["reply"] = dropped.get("reply", 0) + len(parents)   # two parents: neither is trusted
        elif parent != iid and con.execute("SELECT 1 FROM hooks WHERE item_id=? AND owner=? LIMIT 1",
                                           (parent, person)).fetchone():
            out.append(Hook("reply:device", parent, at, {"part": "header", "line": ploc.get("line", 0)}))
        else:
            drop("reply")                                    # not (yet) the poster's own item, or itself: unkept
    out += _thread_carry(con, iid, out)
    n = index(con, iid, source, out, person, "private")
    return {"item_id": iid, "hooks": n, "dropped": dict(sorted(dropped.items()))}


_CON: sqlite3.Connection | None = None


def index_original(item_id: str, source: str, title: str, text: str, md: dict[str, Any]) -> int:
    """envelope_pass's step: index an item's hooks from its original, before the summary replaces it. Never raises:
    a failure here costs the item its hooks, not its envelope. → how many (0 on failure)."""
    global _CON
    try:
        import kb_privacy
        owner, aud = kb_privacy.acl_of(md or {}, item_id)
        if _CON is None:
            _CON = connect()
        n = index(_CON, item_id, source, extract(_mail_item(item_id, title, text, md or {}), entity_finder(owner)),
                  owner, aud)
        try:   # its topic vector, from the same original (#951 (e)); no embeddings model: no topic
            import topics
            topics.index_text(_CON, item_id, title, text)
        except Exception:  # noqa: BLE001
            pass
        return n
    except Exception as exc:  # noqa: BLE001
        print(f"[hooks] {item_id}: not indexed ({type(exc).__name__})", file=sys.stderr)
        return 0


# ── the backfill ──────────────────────────────────────────────────────────────────────────────────

def _mail_item(sid: str, title: str, text: str, md: dict[str, Any]) -> dict[str, Any]:
    return {"item_id": sid, "title": title, "text": text,
            "at": str(md.get("observed_at") or md.get("synced_at") or "")[:10] or None}


def backfill_items() -> list[tuple[str, str, dict[str, Any], str | None, str | None]]:
    """(item_id, source, item, owner, audience) for: the migrated mails from their originals (the envelope backups),
    mail and scans still whole in the KB, and the bill and receipt records."""
    import kb_privacy
    out: dict[str, tuple[str, str, dict[str, Any], str | None, str | None]] = {}
    def stamp(f: Path) -> str:
        m = re.search(r"(\d{8}-\d{6})", f.name)
        return m.group(1) if m else f.name
    for f in sorted(BACKUP_DIR.glob("envelopes-*.jsonl"), key=stamp):   # every backup holds originals; oldest wins
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            sid = str(r.get("source_id") or "")
            if sid and sid not in out:
                md = r.get("metadata") or {}
                owner, aud = kb_privacy.acl_of(md, sid)
                src = "scan" if "scan" in str(r.get("source_type") or "") else "mail"
                out[sid] = (sid, src, _mail_item(sid, str(r.get("title") or ""), str(r.get("text") or ""), md), owner, aud)
    try:
        import kb_hygiene
        import kb_query
        for s in kb_hygiene.list_sources():
            sid = str(s.get("source_id") or "")
            st = str(s.get("source_type") or "")
            if sid in out or st not in ("connector:mail", "connector:scans"):
                continue
            text, md = kb_query._chunks(sid)
            if (md or {}).get("envelope"):
                continue                                         # a summary: its original is in the backups or gone
            owner, aud = kb_privacy.acl_of(md or {}, sid)
            out[sid] = (sid, "scan" if "scans" in st else "mail", _mail_item(sid, str(s.get("title") or ""), text or "",
                                                                             md or {}), owner, aud)
    except Exception as exc:  # noqa: BLE001
        print(f"(KB not read: {exc})", file=sys.stderr)
    try:
        import agent_runtime_lib as lib
        for t in ("bill", "receipt"):
            for n in lib.record_nodes(t)[0]:
                d = n.get("data") or {}
                prov = d.get("provenance") or {}
                nid = str(n.get("node_id") or "")
                out[nid] = (nid, "record", {"item_id": nid, "title": str(d.get("title") or ""), "text": "",
                                            "at": str(d.get("mail_date") or d.get("paid_date") or d.get("due_date") or "")[:10],
                                            "vendor": d.get("vendor"), "amount": d.get("amount"),
                                            "due_date": d.get("due_date"), "paid_date": d.get("paid_date"),
                                            "derived_from": prov.get("source_id")}, prov.get("owner"), prov.get("audience"))
    except Exception as exc:  # noqa: BLE001
        print(f"(records not read: {exc})", file=sys.stderr)
    return list(out.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Link hooks (#951): index what items could be linked by")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if not a.backfill:
        ap.error("--backfill is the only command")
    items = backfill_items()
    by_src: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    con = None
    if a.apply:
        con = connect()
    for iid, src, item, owner, aud in items:
        hs = extract(item, entity_finder(owner))
        by_src[src] = by_src.get(src, 0) + 1
        for h in hs:
            by_kind[h.kind] = by_kind.get(h.kind, 0) + 1
        if con is not None:
            index(con, iid, src, hs, owner, aud)
    embedded = 0
    if con is not None:   # topic vectors from the same originals, in batches (#951 (e)); none if the model is down
        import topics
        texts = [(iid, topics.topic_text(str(item.get("title") or ""),
                                         str(item.get("text") or "") or f"{item.get('vendor') or ''}"))
                 for iid, _src, item, _o, _a in items]
        for k in range(0, len(texts), 32):
            batch = [t for t in texts[k:k + 32] if t[1]]
            vecs = topics.embed([t for _, t in batch]) or []
            for (iid, _), v in zip(batch, vecs):
                topics.put(con, iid, v)
                embedded += 1
    print(json.dumps({"apply": a.apply, "items": len(items), "by_source": by_src,
                      "hooks_by_kind": dict(sorted(by_kind.items())), "topics_embedded": embedded}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
