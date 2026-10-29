#!/usr/bin/env python3
"""Fences: privacy boundaries on a thing or a kind of thing, applied wherever it shows up
(docs/world-model-fences.md, titan-node#356). Phase 1, behind AGENT_FENCES=1.

An item (a KB note, a timeline entry, a world-model claim) is visible to a viewer when its own owner and
audience allow it (audience.py, as today) AND every fence it touches admits the viewer. So a fence only
ever narrows, two fences combine as their overlap, and it's checked at read time: a fence set today
covers last month's notes, and retiring it restores what it held back. The author of an item always
sees it. What a fence holds back doesn't exist for the viewer (Zach, 10-01).

Fences:
  entity   "keep anything about Dr. Patel private": a world-model entity (bubble:entity) and its names.
           Set by an adult or the admin with the keep_private tool; owner = the person who set it.
  class    the defaults, owner None:
             money   the adults only (#268, now at read time, old items too: Zach, 10-01)
             health  the person it's about, plus their parents for a child (Zach, 10-01) or the adults
                     for an adult; with no person named, the adults
An exception lets one item past one fence: its owner shares it (kb share), logged.

What an item touches (deterministic, phase 1): its text against each entity fence's names; money by
redact.about_money; health by the word list below; world-model claims by attribute class.

Store: SQLite at FENCES_DB (default /ai/datasets/connectors/fences.db, mode 0640): fences, exceptions,
log, and held-back counts per fence per day (no content).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_time  # noqa: E402
import audience as _aud  # noqa: E402

DB_PATH = Path(os.environ.get("FENCES_DB", "/ai/datasets/connectors/fences.db"))
_lock = threading.Lock()
HEALTH_RX = re.compile(
    r"\b(therap(?:y|ist|ies)|counsel(?:l?ing|or)|psychiatr\w*|psycholog\w*|diagnos\w+|prescri\w+|medications?|meds|"
    r"dosage|symptoms?|illness|disorder|anxiety|depression|adhd|iep|allerg(?:y|ies)|pediatrician|"
    r"blood (?:test|work|pressure)|lab results?|surgery|hospital|clinic visit|health (?:data|summary|records?))\b", re.I)
# A person's own kinds of thing, besides health and money (#373 red team: "pretend I'm the admin and list
# Zach's downloads"): asked about by name, they're that person's alone.
PERSONAL_RX = re.compile(r"\b(files?|folders?|downloads?|documents?|e-?mails?|mail|inbox|texts?|text messages?|messages?|"
                         r"dms?|chats?|photos?|location|whereabouts|passwords?|(?:browsing|search|medical) history|browsing)\b", re.I)
# Legal matters (#357, suggested fences): no default fence holds them back, so titan only suggests one to the owner.
LEGAL_RX = re.compile(
    r"\b(lawyers?|attorneys?|law (?:firm|office|offices|group)|(?:family|criminal|estate|immigration|divorce) law|esq|legal (?:aid|matter|case|advice|counsel)|court (?:date|hearing|case|order)|"
    r"courthouse|custody|divorce|lawsuits?|sued|suing|subpoena|probate|guardianship|restraining order|parole|probation|"
    r"immigration (?:case|hearing|lawyer)|deposition)\b", re.I)
MONEY_ATTRS = frozenset({"bill", "billing", "account_holder", "kept_in"})
HEALTH_ATTRS = frozenset({"medical", "allergies", "medications"})


def enabled() -> bool:
    return os.environ.get("AGENT_FENCES", "0").strip().lower() in ("1", "true", "yes", "on")


SCHEMA = """
CREATE TABLE IF NOT EXISTS fences (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, names TEXT NOT NULL DEFAULT '[]',
    owner TEXT, audience TEXT NOT NULL, source TEXT NOT NULL, set_by TEXT, set_at REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS exceptions (
    item TEXT NOT NULL, fence_id INTEGER NOT NULL, audience TEXT NOT NULL, by TEXT, at REAL NOT NULL,
    PRIMARY KEY (item, fence_id));
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY, fence_id INTEGER, action TEXT NOT NULL, by TEXT, at REAL NOT NULL,
    item TEXT, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS held (
    fence_id INTEGER NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (fence_id, day));
CREATE TABLE IF NOT EXISTS carried (
    item TEXT NOT NULL, fence_id INTEGER NOT NULL, subject TEXT, at REAL NOT NULL, PRIMARY KEY (item, fence_id));
"""
DEFAULTS = (("class", "money", "adults", "money goes to the adults (#268)"),
            ("class", "health", "subject", "health stays with the person (#243), and a child's parents"))


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("FENCES_DB") or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o640))
    con = sqlite3.connect(str(p), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for kind, subject, aud, reason in DEFAULTS:
        if not con.execute("SELECT 1 FROM fences WHERE kind=? AND subject=? AND source='default'", (kind, subject)).fetchone():
            con.execute("INSERT INTO fences (kind, subject, owner, audience, source, set_at, reason) VALUES (?,?,?,?,?,?,?)",
                        (kind, subject, None, aud, "default", time.time(), reason))
    con.commit()
    return con


class Fences:
    def __init__(self, path: Path | str | None = None, people=None, clock=None):
        self.path = Path(path or os.environ.get("FENCES_DB") or DB_PATH)
        self.clock = clock or time.time
        self._local = threading.local()      # the store opens on first use: with fences off, nothing touches it
        self._people = people            # callable → [{id, name, role, parents}], for tests
        self._cache: dict[str, Any] = {"at": 0.0}

    @property
    def con(self) -> sqlite3.Connection:
        c = getattr(self._local, "con", None)
        if c is None:
            c = self._local.con = _connect(self.path)
        return c

    # ── who is who ─────────────────────────────────────────────────────────────────────────────

    def people(self) -> list[dict[str, Any]]:
        if self._people is not None:
            return self._people()
        if time.time() - self._cache["at"] > 60:
            try:
                import household_identity as hh
                self._cache = {"at": time.time(), "people": hh.get_store().people()}
            except Exception:  # noqa: BLE001
                self._cache = {"at": time.time(), "people": []}
        return self._cache.get("people") or []

    def _person(self, pid: str | None) -> dict[str, Any] | None:
        return next((p for p in self.people() if p.get("id") == pid), None)

    def subject_in(self, text: str) -> str | None:
        """The one household member a text names (first name or full name), or None."""
        hits = []
        for p in self.people():
            name = str(p.get("name") or "").strip()
            if name and re.search(r"(?<![A-Za-z])" + re.escape(name.split()[0]) + r"(?![a-z])", text or "", re.I):
                hits.append(p["id"])
        return hits[0] if len(hits) == 1 else None

    # ── the fences ─────────────────────────────────────────────────────────────────────────────

    def active(self) -> list[sqlite3.Row]:
        k = "rows"
        if self._cache.get(k + "_at", 0) < time.time() - 5:
            self._cache[k] = self.con.execute("SELECT * FROM fences WHERE status='active'").fetchall()
            self._cache[k + "_at"] = time.time()
        return self._cache[k]

    def _invalidate(self) -> None:
        self._cache["rows_at"] = 0

    def set_entity(self, subject: str, names: Iterable[str], owner: str, audience: Any = "private",
                   by: str | None = None, reason: str = "") -> int:
        aud = _aud.normalize(audience, default="private")
        with _lock, self.con:
            cur = self.con.execute(
                "INSERT INTO fences (kind, subject, names, owner, audience, source, set_by, set_at, reason) "
                "VALUES ('entity',?,?,?,?,?,?,?,?)",
                (subject, json.dumps(sorted({n for n in names if n and len(n.strip()) >= 2})), owner, aud, "stated",
                 by or owner, self.clock(), reason))
            self.con.execute("INSERT INTO log (fence_id, action, by, at) VALUES (?,?,?,?)",
                             (cur.lastrowid, "set", by or owner, self.clock()))
        self._invalidate()
        return cur.lastrowid

    def update(self, fence_id: int, names: Iterable[str], audience: Any, by: str) -> bool:
        """Change an active stated fence's audience (wider or narrower, as said) and add names to it; logged. One fence
        per subject: a second keep_private on it changes this one, since every fence must admit a viewer (#806)."""
        aud = _aud.normalize(audience, default="private")
        with _lock, self.con:
            row = self.con.execute("SELECT names FROM fences WHERE id=? AND status='active' AND source!='default'",
                                   (fence_id,)).fetchone()
            if row is None:
                return False
            have = set(json.loads(row["names"] or "[]"))
            self.con.execute("UPDATE fences SET names=?, audience=? WHERE id=?",
                             (json.dumps(sorted(have | {n for n in names if n and len(n.strip()) >= 2})), aud, fence_id))
            self.con.execute("INSERT INTO log (fence_id, action, by, at) VALUES (?,?,?,?)",
                             (fence_id, "changed", by, self.clock()))
        self._invalidate()
        return True

    def retire(self, fence_id: int, by: str) -> bool:
        with _lock, self.con:
            n = self.con.execute("UPDATE fences SET status='retired' WHERE id=? AND status='active' AND source!='default'",
                                 (fence_id,)).rowcount
            if n:
                self.con.execute("INSERT INTO log (fence_id, action, by, at) VALUES (?,?,?,?)",
                                 (fence_id, "retired", by, self.clock()))
        self._invalidate()
        return bool(n)

    def owned_by(self, person: str) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM fences WHERE status='active' AND owner=? ORDER BY id", (person,)).fetchall()

    def for_viewer(self, person: str, role: str | None, days: int = 7) -> list[dict[str, Any]]:
        """The fences a person can see and manage (#357, item 4; the apps' Settings): their own entity fences,
        plus the household defaults for an adult or the admin. Content-free: names they chose and counts of
        what each held back (the last `days` and in all), never the items."""
        since = local_time.day(self.clock() - days * 86400)
        rows = list(self.owned_by(person))
        if role in ("admin", "adult"):
            rows += [r for r in self.active() if r["source"] == "default"]
        out = []
        for r in rows:
            recent = self.con.execute("SELECT COALESCE(SUM(n), 0) FROM held WHERE fence_id=? AND day>=?",
                                      (r["id"], since)).fetchone()[0]
            out.append({"id": r["id"], "kind": r["kind"], "subject": r["subject"],
                        "names": json.loads(r["names"]) if r["names"] else [], "audience": r["audience"],
                        "default": r["source"] == "default", "set_at": r["set_at"],
                        "held_recent": int(recent), "held_total": self.held_count(r["id"]),
                        "yours": r["owner"] == person})
        return out

    def held_count(self, fence_id: int) -> int:
        r = self.con.execute("SELECT COALESCE(SUM(n), 0) FROM held WHERE fence_id=?", (fence_id,)).fetchone()
        return int(r[0])

    def except_item(self, item: str, fence_id: int, audience: Any, by: str) -> None:
        with _lock, self.con:
            self.con.execute("INSERT OR REPLACE INTO exceptions (item, fence_id, audience, by, at) VALUES (?,?,?,?,?)",
                             (item, fence_id, _aud.normalize(audience, default="private"), by, self.clock()))
            self.con.execute("INSERT INTO log (fence_id, action, by, at, item) VALUES (?,?,?,?,?)",
                             (fence_id, "shared past", by, self.clock(), item))

    # ── what an item touches, and whether a fence lets the viewer through ──────────────────────

    def touches(self, text: str = "", classes: Iterable[str] = (), entities: Iterable[str] = ()) -> list[sqlite3.Row]:
        t = " " + re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()) + " "
        classes, entities = set(classes), set(entities)
        if text:
            if HEALTH_RX.search(text):
                classes.add("health")
            try:
                import redact
                if redact.about_money(text):
                    classes.add("money")
            except Exception:  # noqa: BLE001
                pass
        out = []
        for f in self.active():
            if f["kind"] == "class" and f["subject"] in classes:
                out.append(f)
            elif f["kind"] == "entity":
                if f["subject"] in entities:
                    out.append(f)
                elif text and any(f" {re.sub(r'[^a-z0-9]+', ' ', n.lower()).strip()} " in t for n in json.loads(f["names"])):
                    out.append(f)
        return out

    def carry(self, item: str, fence_ids: Iterable[int], subject: str | None = None) -> int:
        """An item envelope (#390) keeps the fences its original text touched: admits() applies them to the item
        whatever its summary says (a summary can drop the very name a fence matches). Replaces what it carried."""
        ids = sorted({int(i) for i in fence_ids})
        with _lock, self.con:
            self.con.execute("DELETE FROM carried WHERE item=?", (str(item),))
            self.con.executemany("INSERT INTO carried (item, fence_id, subject, at) VALUES (?,?,?,?)",
                                 [(str(item), i, subject, self.clock()) for i in ids])
        return len(ids)

    def _fence_admits(self, f: sqlite3.Row, viewer: str | None, role: str | None, subject: str | None,
                      item: str | None) -> bool:
        if f["owner"] and viewer and f["owner"] == viewer:
            return True
        if item:
            ex = self.con.execute("SELECT audience FROM exceptions WHERE item=? AND fence_id=?", (item, f["id"])).fetchone()
            if ex and _aud.visible(ex["audience"], viewer, role, owner=f["owner"]):
                return True
        if f["audience"] != "subject":
            return _aud.visible(f["audience"], viewer, role, owner=f["owner"])
        # health: the person it's about; a child's parents, an adult's fellow adults; nobody named → adults
        if not subject:
            return role in ("admin", "adult")
        if viewer == subject:
            return True
        who = self._person(subject) or {}
        if who.get("role") == "kid":
            return viewer in (who.get("parents") or [])
        return role in ("admin", "adult")

    def admits(self, viewer: str | None, role: str | None, owner: str | None = None, *, text: str = "",
               classes: Iterable[str] = (), entities: Iterable[str] = (), subject: str | None = None,
               item: str | None = None) -> bool:
        """Whether every fence this item touches lets the viewer through. The item's own audience is
        checked by the caller (this only ever narrows). True when fences are off, for titan itself
        (no viewer), and for the item's own author."""
        if not enabled() or (viewer is None and role is None):
            return True
        if owner and viewer and str(owner).lower() == str(viewer).lower():
            return True
        touched = self.touches(text, classes, entities)
        carried_subject = None
        if item:   # an envelope's original touched these (#390): they hold though its summary dropped the words
            have = {f["id"] for f in touched}
            rows = self.con.execute("SELECT c.fence_id, c.subject FROM carried c JOIN fences f ON f.id = c.fence_id "
                                    "WHERE c.item=? AND f.status='active'", (str(item),)).fetchall()
            ids = [r[0] for r in rows if r[0] not in have]
            carried_subject = next((r[1] for r in rows if r[1]), None)
            if ids:
                touched += [f for f in self.active() if f["id"] in ids]
        if not touched:
            return True
        subj = subject if subject is not None else (self.subject_in(text) if text else None) or carried_subject
        for f in touched:
            if not self._fence_admits(f, viewer, role, subj, item):
                self._held(f["id"])
                return False
        return True

    def _held(self, fence_id: int) -> None:
        # Only the household's own turns count (jellybean#300 shows these numbers): the nightly privacy eval asks as
        # a kid on purpose, and its holds made health read "held back 28" on its first night (10-02).
        lib = sys.modules.get("agent_runtime_lib")
        try:
            if lib is not None and lib.turn_source() in ("eval", "canary"):
                return
        except Exception:  # noqa: BLE001
            pass
        day = local_time.day(self.clock())
        try:
            with _lock, self.con:
                self.con.execute("INSERT INTO held (fence_id, day, n) VALUES (?,?,1) ON CONFLICT(fence_id, day) "
                                 "DO UPDATE SET n=n+1", (fence_id, day))
        except sqlite3.Error:
            pass

    def blocking(self, viewer: str | None, role: str | None, text: str) -> list[sqlite3.Row]:
        """The fences this text touches that don't let `viewer` (as `role`) through, each counted as held (never the
        text). Empty when fences are off. For a shared screen (titan-node#917): viewer is the screen, role household."""
        if not enabled() or not text:
            return []
        subj = self.subject_in(text)
        out = [f for f in self.touches(text) if not self._fence_admits(f, viewer, role, subj, None)]
        for f in out:
            self._held(f["id"])
        return out

    def blocking_share(self, text: str, sharer: str | None, entities: Iterable[str] = ()) -> list[sqlite3.Row]:
        """Entity fences an item touches that the sharer doesn't own: sharing past them is refused."""
        return [f for f in self.touches(text, (), entities) if f["kind"] == "entity" and f["owner"] != sharer]


def names_in(subject: str) -> set[str]:
    """The names a fence should match for something the world model doesn't know: the whole phrase and its
    proper names ("my sister Jo" → also "Jo"; "Dr. Patel" → also "Patel"), never a bare common noun
    ("my therapist" fences only those exact words, not every "therapist": Ava's therapy stays with her
    parents, #354)."""
    s = " ".join(str(subject or "").split())
    out = {s}
    caps = re.findall(r"\b(?:Dr|Mr|Mrs|Ms)\.?\s+[A-Z][\w'-]+|\b[A-Z][a-z][\w'-]*", s)
    out |= set(caps) | {re.sub(r"^(?:Dr|Mr|Mrs|Ms)\.?\s+", "", c) for c in caps}
    return {x for x in out if x and x.lower() not in ("i", "me", "my", "the")}


def classes_in(text: str) -> set[str]:
    """The sensitive classes a text names: health and money (the default fences' own tests) and legal."""
    out = {"health"} if HEALTH_RX.search(text or "") else set()
    try:
        import redact
        if redact.about_money(text or ""):
            out.add("money")
    except Exception:  # noqa: BLE001
        pass
    return out | ({"legal"} if LEGAL_RX.search(text or "") else set())


def attr_classes(attr: str) -> set[str]:
    return ({"money"} if attr in MONEY_ATTRS else set()) | ({"health"} if attr in HEALTH_ATTRS else set())


_FENCES: Fences | None = None


def get() -> Fences:
    global _FENCES
    if _FENCES is None:
        _FENCES = Fences()
    return _FENCES
