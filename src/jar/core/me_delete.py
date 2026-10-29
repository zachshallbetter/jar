#!/usr/bin/env python3
"""Delete my data, with a 7-day undo (#1080, decided by Coordination 10-06 on Zach's instruction; folds in #903).

An adult (or the admin) deletes their own chats, memories and KB notes. Nothing is gone at once: every row is moved
into a per-person tombstone (DELETE_DIR/<person>/tomb.db, 0700/0600), so recall and retrieval stop seeing it at
once, and Cancel puts all of it back exactly within 7 days. After that the nightly purge (scripts/me_delete.py
purge, titan-me-purge.timer, outside the eval windows) deletes the tombstone for good. No admin approval: an adult's data
is theirs. Kids are refused ("ask a parent"); a shared screen has nothing of its own.

What moves (only the caller's own rows, never anyone else's or the household's):
  memory   their chat messages (conv_id_for(person)), scenes (+ their .md files), facts, forgotten facts, dev notes
  kb       the KB notes they own (kb_privacy's rule, the same one the export uses) + their access rows; a synced
           item's ledger row is held (tombstoned) so their device doesn't send it back, and released on Cancel
  lineage  their branch rows in spaces (#903): as a branch and as a parent, since every row is theirs
  filings  where they filed their chats (spaces tags)
  titles   the model titles of their threads (chat_judge)
  claude   their Claude conversations (claude_chats)
  export   their download archive, deleted outright (it is a copy of all the above)
  world    (1b) in every bubble: the evidence they own that isn't the household's, the claims left with no other
           evidence (with their log and asks), and their mentions, note reviews and learned aliases. Household-
           audience facts stay: they're the household's, even when this person observed them
  places   (1b) their place events and held departures
  uploads  (1b) their uploaded files (voice notes, scans) and the uploads rows
Household facts, shared calendars and devices are never touched: devices stay paired.

  python3 scripts/me_delete.py purge [--dry-run]     the nightly job (counts only)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

KEEP_S = 7 * 86400
DELETE_DIR = Path(os.environ.get("ME_DELETE_DIR", "/ai/datasets/me_delete"))
CONFIRM = "delete"
_lock = threading.Lock()


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _root() -> Path:
    return Path(os.environ.get("ME_DELETE_DIR") or DELETE_DIR)


def _safe(person: str) -> str:
    return "".join(c for c in str(person) if c.isalnum() or c in "-_")[:64] or "_"


def _check(person: str | None, role: str | None) -> None:
    if not person or role == "household":
        raise Refused(403, "a shared screen has nothing of its own")
    if role == "kid":
        raise Refused(403, "ask a parent")
    if role not in ("admin", "adult"):
        raise Refused(403, "not allowed")


def _tomb(person: str, create: bool = False) -> sqlite3.Connection | None:
    d = _root() / _safe(person)
    p = d / "tomb.db"
    if not p.exists():
        if not create:
            return None
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rows (part TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
    """)
    return con


def _state(con: sqlite3.Connection) -> dict[str, Any]:
    return {r["k"]: json.loads(r["v"]) for r in con.execute("SELECT k, v FROM state")}


def _put(con: sqlite3.Connection, part: str, kind: str, rows: list[dict[str, Any]]) -> int:
    con.executemany("INSERT INTO rows (part, kind, data) VALUES (?,?,?)",
                    [(part, kind, json.dumps(r, default=str)) for r in rows])
    return len(rows)


def _get(con: sqlite3.Connection, part: str, kind: str) -> list[dict[str, Any]]:
    return [json.loads(r["data"]) for r in con.execute("SELECT data FROM rows WHERE part=? AND kind=?", (part, kind))]


# ── the parts ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _mail_moves_db(path: Path | None = None) -> sqlite3.Connection:
    import mail_moves
    return mail_moves._db(path)


def _mail_db_exists() -> bool:
    import mail_server
    return Path(os.environ.get("MAIL_SERVER_DB") or mail_server.DB).exists()


class Parts:
    """Where each kind of row lives. Every store is injectable so tests run on temporary copies."""

    def __init__(self, mem: Any, *, kb_sources: Callable[[], list[dict]] | None = None, call: Any = None,
                 kb_index: Callable[..., Any] | None = None, spaces_db: Path | None = None,
                 titles_db: Path | None = None, claude_db: Path | None = None, ledger: Path | None = None,
                 export_dir: Path | None = None, world: Callable[[], Any] | None = None,
                 places_db: Path | None = None):
        self.mem, self._kb_sources, self.call, self._kb_index = mem, kb_sources, call, kb_index
        self.spaces_db, self.titles_db, self.claude_db, self.ledger = spaces_db, titles_db, claude_db, ledger
        self.export_dir = export_dir
        self._world, self.places_db = world, places_db

    # memory ──
    def stash_memory(self, person: str, conv: str, tomb: sqlite3.Connection) -> dict[str, int]:
        db = self.mem._db
        db.row_factory = sqlite3.Row
        out = {}
        for kind, q, arg in (("messages", "SELECT * FROM messages WHERE conv_id=?", conv),
                             ("scenes", "SELECT * FROM scenes WHERE conv_id=?", conv),
                             ("facts", "SELECT * FROM facts WHERE person_id=?", person),
                             ("forgotten_facts", "SELECT * FROM forgotten_facts WHERE person_id=?", person),
                             ("dev_notes", "SELECT * FROM dev_notes WHERE person_id=?", person)):   # #1605
            rows = [dict(r) for r in db.execute(q, (arg,))]
            if kind == "scenes":
                d = tomb_dir(person) / "scenes"
                d.mkdir(exist_ok=True)
                for r in rows:
                    src = Path(r["md_path"])
                    if src.exists():
                        r["_file"] = str(d / src.name)
                        shutil.copy2(src, r["_file"])
            out[kind] = _put(tomb, "memory", kind, rows)
        return out

    def drop_memory(self, person: str, conv: str, tomb: sqlite3.Connection) -> None:
        with self.mem._lock:
            db = self.mem._db
            db.execute("DELETE FROM messages WHERE conv_id=?", (conv,))
            db.execute("DELETE FROM facts WHERE person_id=?", (person,))
            db.execute("DELETE FROM forgotten_facts WHERE person_id=?", (person,))
            db.execute("DELETE FROM dev_notes WHERE person_id=?", (person,))
            for r in _get(tomb, "memory", "scenes"):
                db.execute("DELETE FROM scenes WHERE id=?", (r["id"],))
                Path(r["md_path"]).unlink(missing_ok=True)
            db.commit()
            self.mem._bm25 = None      # recall stops seeing them at once

    def restore_memory(self, tomb: sqlite3.Connection) -> None:
        with self.mem._lock:
            db = self.mem._db
            for kind in ("messages", "scenes", "facts", "forgotten_facts", "dev_notes"):
                cols = {r[1] for r in db.execute(f"PRAGMA table_info({kind})")}
                for r in _get(tomb, "memory", kind):
                    if kind == "scenes" and r.get("_file") and Path(r["_file"]).exists():
                        Path(r["md_path"]).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(r["_file"], r["md_path"])
                    row = {k: v for k, v in r.items() if k in cols}
                    db.execute(f"INSERT OR REPLACE INTO {kind} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                               list(row.values()))
            db.commit()
            self.mem._bm25 = None

    # kb ──
    def _mine(self, person: str) -> list[dict[str, Any]]:
        import kb_privacy
        srcs = self._kb_sources() if self._kb_sources else []
        acl = kb_privacy.acl_rows(str(x.get("source_id")) for x in srcs)
        return [x for x in srcs if str(kb_privacy.acl_of(x.get("metadata"), str(x.get("source_id")), acl)[0] or "")
                .lower() == person.lower()]

    def stash_kb(self, person: str, tomb: sqlite3.Connection) -> dict[str, int]:
        import kb_privacy
        mine = self._mine(person)
        acl = kb_privacy.acl_rows(str(x.get("source_id")) for x in mine)
        rows = []
        for x in mine:
            sid = str(x.get("source_id"))
            got = kb_privacy._fetch(self.call, sid)
            if got is None:
                raise Refused(503, "the knowledge base couldn't be read in full; nothing was deleted")
            md, text = got
            rows.append({"source_id": sid, "title": x.get("title") or "", "source_type": x.get("source_type") or "note",
                         "text": text, "metadata": md, "acl": list(acl[sid]) if sid in acl else None})
        return {"notes": _put(tomb, "kb", "notes", rows)}

    def drop_kb(self, person: str, tomb: sqlite3.Connection) -> None:
        import urllib.parse
        import kb_privacy
        held = []
        for r in _get(tomb, "kb", "notes"):
            sid = r["source_id"]
            self.call("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}", "DELETE")
            kb_privacy.drop_acl(sid)
            if sid.startswith("cx-"):
                held.append(sid)
        if held:
            import connectors_runner
            _put(tomb, "kb", "held", [{"item_id": i} for i in connectors_runner.hold_items(held, self.ledger)])

    def restore_kb(self, tomb: sqlite3.Connection) -> None:
        import kb_privacy
        held = [r["item_id"] for r in _get(tomb, "kb", "held")]
        if held:
            import connectors_runner
            connectors_runner.release_items(held, self.ledger)
        for r in _get(tomb, "kb", "notes"):
            self._kb_index(r["source_id"], r["title"], r["text"], r["source_type"], r["metadata"])
            if r.get("acl"):
                owner, aud = r["acl"]
                kb_privacy.set_acl(r["source_id"], owner, aud, by=owner, why="delete cancelled")

    # lineage (#903), titles, claude chats ──
    def _stores(self) -> list[tuple[str, str, Callable[[], sqlite3.Connection], str]]:
        import chat_judge
        import claude_chats
        import clear_prefs
        import mail_triage
        import autonomy
        import capability_audit
        import governance
        import spaces

        def opener(fn: Callable[..., sqlite3.Connection], path: Path | None) -> Callable[[], sqlite3.Connection]:
            def open_() -> sqlite3.Connection:
                con = fn(path)
                con.row_factory = sqlite3.Row
                return con
            return open_
        return [("lineage", "lineage", opener(spaces._db, self.spaces_db), "person"),
                ("filings", "tags", opener(spaces._db, self.spaces_db), "person"),   # where they filed their chats
                ("theme", "theme_prefs", opener(spaces._db, self.spaces_db), "person"),   # their look (#1159)
                ("clearing", "clear_prefs", opener(clear_prefs._db, self.spaces_db), "person"),   # what clearing does (#1646)
                ("capabilities", "capability_tiers", opener(governance._db, self.spaces_db), "person"),   # what they turned off (#1628)
                ("capabilities", "capability_audit", opener(capability_audit._db, self.spaces_db), "person"),   # what titan did for them
                ("capabilities", "autonomy", opener(autonomy._db, self.spaces_db), "person"),   # what titan may do without asking
                ("titles", "titles", opener(chat_judge._titles_db, self.titles_db), "person"),
                ("claude", "convs", opener(claude_chats._db, self.claude_db), "owner"),
                ("claude", "msgs", opener(claude_chats._db, self.claude_db), "owner")] + (
                # what they taught triage (#1644), where mail triage keeps its store (titan; never created here)
                [("triage", "sender_rules", opener(mail_triage._db, None), "owner")]
                if Path(os.environ.get("MAIL_TRIAGE_DB") or mail_triage.DB).exists() else []) + (
                # where they file mail from each sender (#1644), where the mail server's store is
                [("mail", "filing_habits", opener(_mail_moves_db, None), "owner")] if _mail_db_exists() else [])

    def stash_stores(self, person: str, tomb: sqlite3.Connection) -> dict[str, int]:
        out: dict[str, int] = {}
        for part, table, open_, col in self._stores():
            con = open_()
            try:
                rows = [dict(r) for r in con.execute(f"SELECT * FROM {table} WHERE lower({col})=lower(?)", (person,))]
            except sqlite3.OperationalError:   # the store doesn't exist yet: nothing of theirs in it
                rows = []
            finally:
                con.close()
            out[f"{part}.{table}"] = _put(tomb, part, table, rows)
        return out

    def drop_stores(self, person: str) -> None:
        for _part, table, open_, col in self._stores():
            con = open_()
            try:
                con.execute(f"DELETE FROM {table} WHERE lower({col})=lower(?)", (person,))
                con.commit()
            except sqlite3.OperationalError:
                pass
            finally:
                con.close()

    def restore_stores(self, tomb: sqlite3.Connection) -> None:
        for part, table, open_, _col in self._stores():
            rows = _get(tomb, part, table)
            if not rows:
                continue
            con = open_()
            try:
                for r in rows:
                    verb = "INSERT" if table == "msgs" else "INSERT OR REPLACE"   # fts5 has no primary key
                    con.execute(f"{verb} INTO {table} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                                list(r.values()))
                con.commit()
            finally:
                con.close()

    # world (1b) ──
    _WORLD_PERSON = (("mentions", "person"), ("note_reviews", "person"), ("alias_learn", "speaker"), ("aliases", "speaker"))

    def _bubbles(self) -> list[tuple[str, sqlite3.Connection]]:
        if self._world is None:
            import world
            w = world.get()
        else:
            w = self._world()
        return [(b, w.world(b).con) for b in w.bubbles()]

    @staticmethod
    def _tables(con: sqlite3.Connection) -> set[str]:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def stash_world(self, person: str, tomb: sqlite3.Connection) -> dict[str, int]:
        n_ev = n_cl = 0
        for b, con in self._bubbles():
            con.row_factory = sqlite3.Row
            have = self._tables(con)
            ev = [dict(r) for r in con.execute("SELECT * FROM evidence WHERE lower(owner)=lower(?) AND "
                                               "lower(audience)<>'household'", (person,))]
            mine = {e["id"] for e in ev}
            orphan = sorted({e["claim_id"] for e in ev if not any(
                r["id"] not in mine for r in con.execute("SELECT id FROM evidence WHERE claim_id=?", (e["claim_id"],)))})
            q = ",".join("?" * len(orphan))
            claims = [dict(r) for r in con.execute(f"SELECT * FROM claims WHERE id IN ({q})", orphan)] if orphan else []
            logs = [dict(r) for r in con.execute(f"SELECT * FROM claim_log WHERE claim_id IN ({q})", orphan)] if orphan else []
            asks = [dict(r) for r in con.execute(f"SELECT * FROM asks WHERE claim_id IN ({q})", orphan)] \
                if orphan and "asks" in have else []
            _put(tomb, "world", "bubble", [{"bubble": b, "evidence": ev, "claims": claims, "claim_log": logs, "asks": asks,
                                            **{t: [dict(r) for r in con.execute(f"SELECT * FROM {t} WHERE lower({c})=lower(?)",
                                                                                (person,))]
                                               for t, c in self._WORLD_PERSON if t in have}}])
            n_ev, n_cl = n_ev + len(ev), n_cl + len(claims)
        return {"world_evidence": n_ev, "world_claims": n_cl}

    def drop_world(self, person: str, tomb: sqlite3.Connection) -> None:
        cons = dict(self._bubbles())
        for rec in _get(tomb, "world", "bubble"):
            con = cons.get(rec["bubble"])
            if con is None:
                continue
            have = self._tables(con)
            with con:
                for e in rec["evidence"]:
                    con.execute("DELETE FROM evidence WHERE id=?", (e["id"],))
                for c in rec["claims"]:
                    con.execute("DELETE FROM claim_log WHERE claim_id=?", (c["id"],))
                    if "asks" in have:
                        con.execute("DELETE FROM asks WHERE claim_id=?", (c["id"],))
                    con.execute("DELETE FROM claims WHERE id=?", (c["id"],))
                for t, col in self._WORLD_PERSON:
                    if t in have:
                        con.execute(f"DELETE FROM {t} WHERE lower({col})=lower(?)", (person,))

    @staticmethod
    def _insert(con: sqlite3.Connection, table: str, row: dict[str, Any], verb: str = "INSERT") -> int | None:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        row = {k: v for k, v in row.items() if k in cols}
        cur = con.execute(f"{verb} INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        return cur.lastrowid

    def restore_world(self, tomb: sqlite3.Connection) -> None:
        """Back exactly; a claim id taken meanwhile is never overwritten: the same fact re-learned is joined, any
        other collision gets a new id (and its evidence, log and asks follow it)."""
        cons = dict(self._bubbles())
        for rec in _get(tomb, "world", "bubble"):
            con = cons.get(rec["bubble"])
            if con is None:
                continue
            have = self._tables(con)
            with con:
                remap: dict[int, int] = {}
                for c in rec["claims"]:
                    same = con.execute("SELECT id FROM claims WHERE entity_id=? AND attr=? AND qual=? AND value_norm=?",
                                       (c["entity_id"], c["attr"], c["qual"], c["value_norm"])).fetchone()
                    if same:
                        remap[c["id"]] = same[0]
                        continue
                    if con.execute("SELECT 1 FROM claims WHERE id=?", (c["id"],)).fetchone():
                        remap[c["id"]] = self._insert(con, "claims", {k: v for k, v in c.items() if k != "id"})
                    else:
                        self._insert(con, "claims", c)
                        remap[c["id"]] = c["id"]
                for lg in rec["claim_log"]:
                    self._insert(con, "claim_log", {**{k: v for k, v in lg.items() if k != "id"},
                                                    "claim_id": remap.get(lg["claim_id"], lg["claim_id"])})
                if "asks" in have:
                    for a in rec["asks"]:
                        self._insert(con, "asks", {**a, "claim_id": remap.get(a["claim_id"], a["claim_id"])},
                                     "INSERT OR IGNORE")
                for e in rec["evidence"]:
                    e = {**e, "claim_id": remap.get(e["claim_id"], e["claim_id"])}
                    if con.execute("SELECT 1 FROM evidence WHERE id=?", (e["id"],)).fetchone():
                        e = {k: v for k, v in e.items() if k != "id"}
                    self._insert(con, "evidence", e, "INSERT OR IGNORE")
                for t, _col in self._WORLD_PERSON:
                    if t in have:
                        for r in rec.get(t) or []:
                            self._insert(con, t, r, "INSERT OR IGNORE")

    # places and uploads (1b) ──
    def _places(self) -> sqlite3.Connection:
        import places
        con = places._db(self.places_db)
        con.row_factory = sqlite3.Row
        return con

    def stash_places_uploads(self, person: str, tomb: sqlite3.Connection) -> dict[str, int]:
        import places as places_mod
        con = self._places()
        try:
            ev = [dict(r) for r in con.execute("SELECT * FROM place_events WHERE lower(person)=lower(?)", (person,))]
            held = [dict(r) for r in con.execute("SELECT * FROM held_departures WHERE lower(person)=lower(?)", (person,))]
            places_mod._modes(con)
            modes = [dict(r) for r in con.execute("SELECT * FROM location_modes WHERE lower(person)=lower(?)", (person,))]
        finally:
            con.close()
        _put(tomb, "places", "place_events", ev)
        _put(tomb, "places", "held_departures", held)
        _put(tomb, "places", "location_modes", modes)
        import uploads
        ucon = uploads._db()
        ucon.row_factory = sqlite3.Row
        try:
            ups = [dict(r) for r in ucon.execute("SELECT * FROM uploads WHERE lower(owner)=lower(?)", (person,))]
        finally:
            ucon.close()
        d = tomb_dir(person) / "uploads"
        for u in ups:
            src = uploads.UPLOADS_DIR / u["id"]
            if src.is_dir():
                shutil.copytree(src, d / u["id"])
                u["_kept"] = True
        _put(tomb, "uploads", "uploads", ups)
        return {"place_events": len(ev), "uploads": len(ups)}

    def drop_places_uploads(self, person: str, tomb: sqlite3.Connection) -> None:
        import places as places_mod
        con = self._places()
        try:
            con.execute("DELETE FROM place_events WHERE lower(person)=lower(?)", (person,))
            con.execute("DELETE FROM held_departures WHERE lower(person)=lower(?)", (person,))
            places_mod._modes(con)
            con.execute("DELETE FROM location_modes WHERE lower(person)=lower(?)", (person,))
            con.commit()
        finally:
            con.close()
        import uploads
        ucon = uploads._db()
        try:
            for u in _get(tomb, "uploads", "uploads"):
                ucon.execute("DELETE FROM uploads WHERE id=?", (u["id"],))
                shutil.rmtree(uploads.UPLOADS_DIR / u["id"], ignore_errors=True)
            ucon.commit()
        finally:
            ucon.close()

    def restore_places_uploads(self, person: str, tomb: sqlite3.Connection) -> None:
        con = self._places()
        try:
            import places as places_mod
            places_mod._modes(con)
            for t in ("place_events", "held_departures", "location_modes"):
                for r in _get(tomb, "places", t):
                    self._insert(con, t, r, "INSERT OR REPLACE")
            con.commit()
        finally:
            con.close()
        import uploads
        ucon = uploads._db()
        try:
            for u in _get(tomb, "uploads", "uploads"):
                kept = tomb_dir(person) / "uploads" / u["id"]
                if u.get("_kept") and kept.is_dir():
                    dest = uploads.UPLOADS_DIR / u["id"]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(kept, dest, dirs_exist_ok=True)
                    os.chmod(dest, 0o700)
                self._insert(ucon, "uploads", u, "INSERT OR REPLACE")
            ucon.commit()
        finally:
            ucon.close()

    def drop_export(self, person: str) -> None:
        import me_data
        d = (self.export_dir or me_data._root()) / me_data._safe(person)
        shutil.rmtree(d, ignore_errors=True)


def tomb_dir(person: str) -> Path:
    return _root() / _safe(person)


def _counts(c: dict[str, int]) -> dict[str, int]:
    return {"chats_messages": c.get("messages", 0), "memories": c.get("facts", 0) + c.get("forgotten_facts", 0) + c.get("dev_notes", 0),
            "notes": c.get("notes", 0), "claude_conversations": c.get("claude.convs", 0),
            "world_facts": c.get("world_evidence", 0), "place_events": c.get("place_events", 0),
            "uploads": c.get("uploads", 0)}


# ── the three actions ─────────────────────────────────────────────────────────────────────────────────────────────

def state(person: str | None, role: str | None, now: float | None = None) -> dict[str, Any]:
    _check(person, role)
    con = _tomb(person)
    if con is None:
        return {"state": "none"}
    try:
        s = _state(con)
    finally:
        con.close()
    if s.get("state") != "pending":
        return {"state": s.get("state", "none")}
    return {"state": "pending", "deleted_at": s["at"], "purge_after": s["purge_after"], "counts": s["counts"],
            "purge_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s["purge_after"]))}


def delete(person: str, role: str | None, parts: Parts, *, conv: str, confirm: Any, now: float | None = None) -> dict:
    """Stash everything first (committed), then remove it from the live stores. A failure while stashing leaves
    the live data untouched; a failure while removing leaves a pending tombstone that Cancel restores in full."""
    _check(person, role)
    if confirm != CONFIRM:
        raise Refused(400, 'say {"confirm": "delete"}')
    now = time.time() if now is None else now
    with _lock:
        con = _tomb(person, create=True)
        try:
            s = _state(con)
            if s.get("state") in ("pending", "deleting"):
                raise Refused(409, "already deleted; cancel within 7 days, or wait for the purge")
            con.execute("DELETE FROM rows")
            con.execute("INSERT OR REPLACE INTO state VALUES ('state', ?)", (json.dumps("stashing"),))
            con.commit()
            c: dict[str, int] = {}
            c.update(parts.stash_memory(person, conv, con))
            c.update(parts.stash_kb(person, con))
            c.update(parts.stash_stores(person, con))
            c.update(parts.stash_world(person, con))
            c.update(parts.stash_places_uploads(person, con))
            con.executemany("INSERT OR REPLACE INTO state VALUES (?, ?)",
                            [("state", json.dumps("deleting")), ("at", json.dumps(now)),
                             ("purge_after", json.dumps(now + KEEP_S)), ("counts", json.dumps(_counts(c)))])
            con.commit()
        except Exception:
            if _state(con).get("state") == "stashing":       # nothing was removed: forget the half-made stash
                con.close()
                shutil.rmtree(tomb_dir(person), ignore_errors=True)
                raise
            raise
        try:
            parts.drop_memory(person, conv, con)
            parts.drop_kb(person, con)
            parts.drop_stores(person)
            parts.drop_world(person, con)
            parts.drop_places_uploads(person, con)
            parts.drop_export(person)
        finally:
            con.execute("INSERT OR REPLACE INTO state VALUES ('state', ?)", (json.dumps("pending"),))
            con.commit()
            con.close()
    return state(person, role, now)


def cancel(person: str, role: str | None, parts: Parts, now: float | None = None) -> dict[str, Any]:
    _check(person, role)
    with _lock:
        con = _tomb(person)
        if con is None or _state(con).get("state") not in ("pending", "deleting"):
            if con:
                con.close()
            raise Refused(404, "nothing to cancel")
        try:
            s = _state(con)
            parts.restore_memory(con)
            parts.restore_kb(con)
            parts.restore_stores(con)
            parts.restore_world(con)
            parts.restore_places_uploads(person, con)
        finally:
            con.close()
        shutil.rmtree(tomb_dir(person), ignore_errors=True)
    return {"state": "restored", "counts": s.get("counts")}


def purge(now: float | None = None, *, dry_run: bool = False, ledger: Path | None = None) -> dict[str, Any]:
    """The nightly job: every tombstone past its 7 days is deleted for good (its synced items stay held, so their
    devices never send them again). Counts only."""
    now = time.time() if now is None else now
    out = {"due": 0, "purged": 0, "waiting": 0}
    root = _root()
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        db = d / "tomb.db"
        if not db.exists():
            continue
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        try:
            s = _state(con)
        finally:
            con.close()
        if s.get("state") != "pending" or float(s.get("purge_after") or 0) > now:
            out["waiting"] += 1
            continue
        out["due"] += 1
        if not dry_run:
            with _lock:
                shutil.rmtree(d, ignore_errors=True)
            out["purged"] += 1
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Delete my data: the nightly purge (#1080); counts only")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("purge")
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(purge(dry_run=a.dry_run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
