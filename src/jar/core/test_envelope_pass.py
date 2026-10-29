#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Item envelopes in the knowledge base (titan-node#390; Zach, 10-02: "Yes, for new items"). The pass turns a day-old
mail or scan into its envelope: only old-enough new items (older ones with --migrate), the original backed up first and
restorable, the world model's observations from the original kept, and the original's fences carried so a summary
that dropped the matched name is still held back. Fake KB, fake model, a real fences store in a temp file.
Run: python3 scripts/test_envelope_pass.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-envpass-"))
os.environ["AGENT_FENCES"] = "1"
os.environ["FENCES_DB"] = str(_TMP / "fences.db")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["ENVELOPE_BACKUP_DIR"] = str(_TMP / "envelope-backups")   # never the real backups (#905 prunes them)
os.environ["WORLD_DB"] = str(_TMP / "world.db")            # #951: hooks index here, never titan's world.db
os.environ["LINKS_DB"] = str(_TMP / "links.db")            # …the link index too
os.environ["LINK_TOPICS"] = "0"                            # …and never the live embeddings model
os.environ["LINK_SALT_FILE"] = str(_TMP / "link_salt")     # …and never the house salt
os.environ.pop("ITEM_ENVELOPES", None)
sys.path.insert(0, str(Path(__file__).parent))

import envelope_pass as P  # noqa: E402
import fences as F  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = 1_791_000_000.0


def iso(hours_ago):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW - hours_ago * 3600, timezone.utc).isoformat()


TEXT = ("From: Lilac Counseling\nSubject: Ava's appointment\n\nAva's next session is Thursday, October 8 at 4 PM. "
        "Call Lilac Counseling at (509) 555-0142 to reschedule.")
KB = {
    "m-new": (TEXT, {"owner": "zach", "audience": "private", "synced_at": iso(30)}),
    "m-fresh": ("Dinner Friday?", {"owner": "zach", "audience": "private", "synced_at": iso(2)}),
    "m-old": ("Old receipt from 2026-09-01 for 12.50.", {"owner": "zach", "audience": "private", "synced_at": iso(240)}),
    "m-done": ("x", {"owner": "zach", "audience": "private", "synced_at": iso(30), "envelope": {"digest": "sha256:x"}}),
    "n-1": ("A note", {"owner": "zach", "audience": "private", "synced_at": iso(30)}),
}
SOURCES = [{"source_id": "m-new", "source_type": "connector:mail", "title": "Appointment"},
           {"source_id": "m-fresh", "source_type": "connector:mail", "title": "Dinner"},
           {"source_id": "m-old", "source_type": "connector:mail", "title": "Receipt"},
           {"source_id": "m-done", "source_type": "connector:mail", "title": "Done"},
           {"source_id": "n-1", "source_type": "note", "title": "A note"}]
written = {}


def read(sid):
    return KB[sid]


def write(sid, title, text, st, md):
    written[sid] = (title, text, st, md)


def chat(prompt, url):
    return {"summary": "Lilac confirms Thursday, October 8 at 4 PM."}, "qwen3-8b"


def observe(sid, title, text, md):
    return [W.Obs(entity="lilac-counseling", etype="org", name="Lilac Counseling", attr="phone", value="(509) 555-0142",
                  observed_at="2026-10-02", kind="mail", ref=f"{sid}#phone", origin=f"mail:{sid}")]


FOLKS = [{"id": "zach", "name": "Zach", "role": "admin", "parents": []},
         {"id": "jordan", "name": "Jordan", "role": "adult", "parents": []},
         {"id": "ava", "name": "Ava", "role": "kid", "parents": ["zach"]}]
fs = F.Fences(_TMP / "fences.db", people=lambda: FOLKS)
fid = fs.set_entity("ava", ["Ava"], owner="zach", audience=["zach", "ava"])

print("── which items ──")
r = P.run(apply=False, migrate=False, limit=10, now=NOW, sources=SOURCES, read=read, write=write, observe=observe, fences=fs)
check("no ENVELOPE_SINCE: no item is new (10-02: by age alone it took all 325 existing)", r["candidates"], 0)
ON = NOW - 48 * 3600   # envelopes turned on two days ago: the 30-hour-old mail is new, the 240-hour-old one existing
r = P.run(apply=False, migrate=False, limit=10, now=NOW, sources=SOURCES, read=read, write=write, observe=observe, fences=fs,
          turned_on=ON)
check("dry run: only the day-old mail synced after they were turned on; nothing written", (r["candidates"], written), (1, {}))
r = P.run(apply=False, migrate=False, limit=10, now=NOW, sources=SOURCES, read=read, write=write, observe=observe, fences=fs,
          turned_on=NOW - 1 * 3600)
check("turned on an hour ago: the day-old mail is an existing copy, not new", r["candidates"], 0)
r = P.run(apply=False, migrate=True, limit=10, now=NOW, sources=SOURCES, read=read, write=write, observe=observe, fences=fs)
check("--migrate: older ones too (not a 2-hour-old one, not a note, not one already done)", r["candidates"], 2)

print("── age from when titan first had it, not the latest sync (10-03 live check) ──")
# m-fresh was re-synced 2 hours ago but titan first had it 30 hours ago (after the turn-on): it's old enough now
first = {"m-new": NOW - 30 * 3600, "m-fresh": NOW - 30 * 3600, "m-old": NOW - 240 * 3600}
r = P.run(apply=False, migrate=False, limit=10, now=NOW, sources=SOURCES, read=read, write=write, observe=observe, fences=fs,
          turned_on=ON, first_seen=first.get)
check("a re-synced new mail counts from its first sync (2 h by synced_at, 30 h by first seen)", r["candidates"], 2)
first2 = {"m-new": NOW - 72 * 3600}   # an existing copy (first had before the turn-on), re-synced since
r = P.run(apply=False, migrate=False, limit=10, now=NOW, sources=SOURCES[:1], read=read, write=write, observe=observe,
          fences=fs, turned_on=ON, first_seen=first2.get)
check("an existing copy re-synced after the turn-on is still an existing copy (only --migrate)", r["candidates"], 0)
check("an item the ledger doesn't know: today's rule (synced_at)", P.run(apply=False, migrate=False, limit=10, now=NOW,
      sources=SOURCES, read=read, write=write, observe=observe, fences=fs, turned_on=ON, first_seen=lambda sid: None)["candidates"], 1)
import sqlite3 as _sq  # noqa: E402
led = _TMP / "ledger-first.db"
c = _sq.connect(led)
c.execute("CREATE TABLE versions (item_id TEXT, at REAL)")
c.execute("CREATE TABLE items (item_id TEXT, observed_at TEXT)")
c.executemany("INSERT INTO versions VALUES (?,?)", [("m-a", 100.0), ("m-a", 500.0), ("m-b", 900.0)])
c.executemany("INSERT INTO items VALUES (?,?)", [("m-a", None), ("m-b", "1970-01-01T00:05:00Z")])
c.commit(); c.close()
fsn = P.first_seen_from_ledger(led)
check("first seen: the earliest version, or the item's own date when earlier (pruned versions)",
      (fsn("m-a"), fsn("m-b"), fsn("m-x")), (100.0, 300.0, None))

print("── one envelope ──")
bk = _TMP / "backup.jsonl"
r = P.run(apply=True, migrate=False, limit=10, now=NOW, sources=SOURCES, read=read, write=write, observe=observe,
          fences=fs, chat=lambda p, u: chat(p, u), backup=bk, turned_on=ON)
check("applied: one, from the model, fenced, its observation kept",
      {k: r[k] for k in ("done", "from_model", "fenced", "obs_kept")}, {"done": 1, "from_model": 1, "fenced": 1, "obs_kept": 1})
title, text, st, md = written["m-new"]
check("the KB now holds the summary, not the mail", ("555-0142" in text, "October 8 at 4 PM" in text, st),
      (False, True, "connector:mail"))
check("…its metadata kept, plus the envelope", (md["owner"], md["audience"], md["envelope"]["digest"].startswith("sha256:"),
                                              md["envelope"]["interpretation"]["label"]), ("zach", "private", True, "lossy-disclosed"))
check("the backup holds the original (mode 600)", (json.loads(bk.read_text())["text"] == TEXT, oct(bk.stat().st_mode & 0o777)),
      (True, "0o600"))

print("── the fence holds though the summary dropped the name ──")
check("the envelope's text doesn't name Ava (title and summary)", "Ava" in text, False)
check("…a fresh store wouldn't fence the summary alone", F.Fences(_TMP / "other.db", people=lambda: FOLKS).admits(
      "jordan", "adult", "zach", text=text, item="m-new"), True)
check("…but the carried fence holds it back from Jordan", fs.admits("jordan", "adult", "zach", text=text, item="m-new"), False)
check("…and lets Ava's parent through", fs.admits("zach", "admin", None, text=text, item="m-new"), True)

print("── the world model reads the original's observations, not the summary ──")
into: dict = {}
check("carried_obs: an envelope's observations, by kind", (W.carried_obs(md, into), [o.value for o in into.get("mail", [])]),
      (True, ["(509) 555-0142"]))
check("…a plain item isn't an envelope", W.carried_obs({"owner": "zach"}, {}), False)

print("── undo and the switch ──")
written.clear()
check("--restore puts the original back, without the envelope", (P.restore(bk, write=write), written["m-new"][1] == TEXT,
                                                                "envelope" in written["m-new"][3]), (1, True, False))
import subprocess  # noqa: E402
out = subprocess.run([sys.executable, str(Path(P.__file__)), "--apply"], capture_output=True, text=True,
                     env={**os.environ, "ITEM_ENVELOPES": "0"}).stdout
check("--apply with ITEM_ENVELOPES off does nothing", "item envelopes are off" in out, True)
check("the mail timer runs it last", "envelope_pass.py --apply" in (Path(P.__file__).parent.parent / "server-setup"
                                                                   / "titan-mail-bills.service").read_text(), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
