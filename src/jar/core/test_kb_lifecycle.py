#!/usr/bin/env python3
# gate: hermetic
"""The KB lifecycle (docs/kb-partitions.md §3, #833): mail 180 days after its envelope, a past one-off event 30 days, a
series 30 days after its last occurrence, a saved page 90 days; facts and system never. A kept item or one a live world
claim cites stays; an item whose age is unknown stays. Applying backs up (0600), deletes the KB copy and its sharing
row, marks the ledger kb=0, and tombstones nothing. Throwaway stores, made-up items, no runtime.
Run: python3 scripts/test_kb_lifecycle.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kblife-"))
for k, v in {"KB_ACL_DB": "kb_acl.db", "CONNECTORS_LEDGER": "ledger.db", "HOUSEHOLD_DB": "household.db",
             "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "WORLD_DIR": "world",
             "KB_LIFECYCLE_LOG": "life.jsonl", "KB_LIFECYCLE_BACKUP_DIR": "backups"}.items():
    os.environ[k] = str(_TMP / v)
for k in ("MAIL_TRIAGE", "ITEM_REFERENCES", "KB_LIFECYCLE", "WORLD_DB"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import connectors_runner as cx  # noqa: E402
import kb_lifecycle as L  # noqa: E402
import kb_privacy as P  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")


NOW = time.time()


def iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


calls: list[tuple] = []


def rt(service, path, method, payload=None, *a, **k):
    calls.append((service, path, method))
    return {}


def mail(sid, title, days_ago):
    return {"source_id": sid, "title": title, "text": f"{title}: a note from Robin Hale.", "observed_at": iso(days_ago)}


M_OLD = mail("m-old", "Gutter quote", 200)
cx.ingest("mail", [M_OLD, mail("m-new", "Dinner Friday?", 10),
                   mail("m-kept", "Warranty for the dryer", 300), mail("m-cited", "Vet's new number", 250),
                   mail("m-dead-claim", "Old plumber number", 260)], runtime=rt, owner="zach")


def ev(sid, title, start_days_ago):
    s = iso(start_days_ago)
    return {"source_id": sid, "title": title, "text": f"{title}", "fields": {"calendar": "Family", "start": s, "end": s}}


cx.ingest("calendar", [ev("e-past", "Parent conference", 45), ev("e-recent", "Book fair", 5),
                       ev("ser-1@" + iso(70), "Soccer", 70), ev("ser-1@" + iso(40), "Soccer", 40),
                       ev("ser-2@" + iso(14), "Piano", 14), ev("ser-2@" + iso(-7), "Piano", -7)], runtime=rt, owner="zach")
SER3 = "ser-3@" + iso(-30)   # once: two iso() calls a second apart named different items (gate flake, 10-07)
cx.ingest("calendar", [ev(SER3, "Holiday", -30)], runtime=rt, owner="zach")
cx.tombstone_item(cx.item_id("calendar", SER3))   # forgotten; its series note was left behind
iid = lambda src, sid: cx.item_id(src, sid)  # noqa: E731

# The KB as the runtime lists it, and each source's metadata (as the writers stamp it).
SRC, META = [], {}


def add(sid, st, md, title=""):
    SRC.append({"source_id": sid, "source_type": st, "title": title or sid})
    META[sid] = md


for s in ("m-old", "m-new", "m-kept", "m-cited", "m-dead-claim"):
    add(iid("mail", s), "connector:mail", {"source": "mail", "owner": "zach", "audience": "private"})
add(iid("calendar", "e-past"), "connector:calendar", {"source": "calendar", "owner": "zach", "audience": "private"})
add(iid("calendar", "e-recent"), "connector:calendar", {"source": "calendar", "owner": "zach", "audience": "private"})
add(iid("calendar", "series:ser-1"), "connector:calendar", {"source": "calendar", "kind": "series", "series": "ser-1"})
add(iid("calendar", "series:ser-2"), "connector:calendar", {"source": "calendar", "kind": "series", "series": "ser-2"})
add(iid("calendar", "series:ser-3"), "connector:calendar", {"source": "calendar", "kind": "series", "series": "ser-3"})
add("web-aaa", "web", {"url": "https://example.org/a", "saved": (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")})
add("web-bbb", "web", {"url": "https://example.org/b", "saved": (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")})
add("web-nodate", "web", {"url": "https://example.org/c"})
add("note-trash", "note", {"owner": "zach", "audience": "household", "saved": "2020-01-01"})
add("note-runbook-x", "knowledge_record", {"origin": "kb_seed_runbook.py", "owner": None, "audience": "admins"})

# A world store: a current claim cites one old mail; a superseded claim cites another.
(_TMP / "world").mkdir()
wcon = sqlite3.connect(str(_TMP / "world" / "household.db"))
wcon.executescript("CREATE TABLE claims (id INTEGER PRIMARY KEY, status TEXT); "
                   "CREATE TABLE evidence (id INTEGER PRIMARY KEY, claim_id INTEGER, ref TEXT);")
wcon.execute("INSERT INTO claims VALUES (1, 'current'), (2, 'superseded')")
wcon.execute("INSERT INTO evidence VALUES (1, 1, ?), (2, 2, ?)", (iid("mail", "m-cited"), iid("mail", "m-dead-claim")))
wcon.commit(); wcon.close()
L.keep(iid("mail", "m-kept"), by="robin")

print("── what's due ──")
pins = L.kept() | L.cited()
items, counts = L.plan(SRC, lambda sid: META[sid], L.Ledger(), now=NOW, pins=pins)
due_ids = sorted(i["source_id"] for i in items)
want = sorted([iid("mail", "m-old"), iid("mail", "m-dead-claim"), iid("calendar", "e-past"),
               iid("calendar", "series:ser-1"), iid("calendar", "series:ser-3"), "web-aaa"])
check("mail past 180 d, the past event, the ended series, an orphaned series (future, but forgotten), the old page",
      due_ids, want)
check("a recent mail, a recent event, a series still running, a recent page: not yet",
      sum(v for k, v in counts.items() if k.endswith("not yet")), 4)
check("kept by a person, cited by a live claim: kept", counts.get("mail: due, but kept (pinned or cited)"), 2)
check("a superseded claim's evidence expires", iid("mail", "m-dead-claim") in due_ids, True)
check("a page with no saved date: kept, counted", counts.get("web: age unknown, kept"), 1)
check("facts and system never", (counts.get("zone facts"), counts.get("zone system"), any(
      i["source_id"] in ("note-trash", "note-runbook-x") for i in items)), (1, 1, False))

print("── a dry run changes nothing ──")
calls.clear()
real_sources, real_meta, real_text = None, P.source_meta, P.source_text
import agent_runtime_lib as lib  # noqa: E402
real_sources = lib.kb_sources
try:
    lib.kb_sources = lambda call=None, **k: SRC
    P.source_meta = lambda call, sid, updated_at="": META[sid]
    P.source_text = lambda call, sid, updated_at="": f"text of {sid}"
    for s in SRC:
        P.set_acl(s["source_id"], "zach", "private", by="test")
    r = L.run(False, call=rt, now=NOW)
    check("dry run: counts, no deletes, no backup", (r["counts"]["expiring"], [c for c in calls if c[2] == "DELETE"],
                                                     list((_TMP / "backups").glob("*")) if (_TMP / "backups").exists() else []),
          (6, [], []))

    print("── applying ──")
    r = L.run(True, call=rt, now=NOW)
    deleted = sorted(c[1].rsplit("/", 1)[1] for c in calls if c[2] == "DELETE")
    check("the six KB copies deleted", deleted, want)
    bak = Path(r["backup"])
    lines = [json.loads(x) for x in bak.read_text().splitlines()]
    check("backed up first, 0600, with text and metadata", (oct(bak.stat().st_mode & 0o777), len(lines),
          all(x["text"] and x["metadata"] for x in lines)), ("0o600", 6, True))
    check("their sharing rows go; the rest stay", (P.acl_rows(want), sorted(P.acl_rows([s["source_id"] for s in SRC]))),
          ({}, sorted(set(s["source_id"] for s in SRC) - set(want))))
    lc = sqlite3.connect(os.environ["CONNECTORS_LEDGER"])
    check("ledger: kb=0 for the expired mail and event, no tombstones added (the one is the fixture's forget)",
          ([r[0] for r in lc.execute("SELECT kb FROM items WHERE item_id IN (?,?)", (iid("mail", "m-old"), iid("calendar", "e-past")))],
           lc.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0]), ([0, 0], 1))
    lc.close()
    calls.clear()
    st = cx.ingest("mail", [M_OLD], runtime=rt, owner="zach")
    check("an unchanged re-sync of an expired mail isn't re-indexed", (st.get("unchanged"), [c for c in calls if c[1] == "/v1/retrieval/index"]),
          (1, []))

    print("── the nightly applies only when turned on ──")
    calls.clear()
    L.main(["--nightly", "--no-log"])
    check("--nightly without KB_LIFECYCLE=apply: a dry run", [c for c in calls if c[2] == "DELETE"], [])
    L.main(["--nightly"])
    check("…and logs counts only (no titles)", "Gutter" in (_TMP / "life.jsonl").read_text(), False)
finally:
    lib.kb_sources, P.source_meta, P.source_text = real_sources, real_meta, real_text

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
