#!/usr/bin/env python3
# gate: hermetic
"""KB zones, phase 1 (docs/kb-partitions.md, #833/#834; Zach 10-04): every item's zone in code, stamped on every write;
kids and shared household devices see household facts only, never mail, calendar, saved pages or the runbook, whatever
an item's audience says; every item can get an explicit sharing row (kb_backfill_owner --explicit). Throwaway stores,
made-up people, no runtime.
Run: python3 scripts/test_kb_zones.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kbzones-"))
for k, v in {"KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory",
             "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl", "DAILY_LOG_DIR": "daily",
             "HOUSEHOLD_PREFS": "household.json", "CONNECTORS_LEDGER": "ledger.db", "FENCES_DB": "fences.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("AGENT_FENCES", None)
sys.path.insert(0, str(Path(__file__).parent))
import kb_privacy as P  # noqa: E402
import zones  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


# The metadata shapes titan's KB holds (sampled 10-04), with made-up values.
MAIL = ("cx-mail-10d1f5c6", {"kind": "email", "source": "mail", "owner": "zach", "audience": "private"})
CAL = ("cx-calendar-1fd7", {"kind": "series", "source": "calendar", "owner": "zach", "audience": "private"})
RUNBOOK = ("note-runbook-datastores", {"domain": "titan", "origin": "kb_seed_runbook.py", "owner": None, "audience": "admins"})
DECISION = ("note-decision-x", {"domain": "decisions", "origin": "kb_seed_decisions.py", "owner": None, "audience": "admins"})
WEB = ("web-f88de6d2", {"owner": "zach", "audience": "private", "url": "https://example.org/a"})
NOTE = ("note-garbage-schedule", {"origin": "agent-ingest_text", "owner": "zach", "audience": "household"})
HH = ("hh-trash", {"origin": "household.yaml", "owner": None, "audience": "household"})

print("── zone_of: from what the item is ──")
check("mail, calendar → sync", (zones.zone_of(MAIL[1], MAIL[0]), zones.zone_of(CAL[1], CAL[0])), ("sync", "sync"))
check("runbook, decisions → system", (zones.zone_of(RUNBOOK[1], RUNBOOK[0]), zones.zone_of(DECISION[1], DECISION[0])),
      ("system", "system"))
check("a saved page → web", zones.zone_of(WEB[1], WEB[0]), "web")
check("a saved note, household.yaml → facts", (zones.zone_of(NOTE[1], NOTE[0]), zones.zone_of(HH[1], HH[0])), ("facts", "facts"))
check("by source_type alone", [zones.zone_of({}, "x", t) for t in ("connector:scans", "knowledge_record", "web", "note")],
      ["sync", "system", "web", "facts"])
check("a stamped zone can't lift a mail out of sync", zones.zone_of({**MAIL[1], "zone": "facts"}, MAIL[0]), "sync")
check("…nor move a note into system", zones.zone_of({**NOTE[1], "zone": "system"}, NOTE[0]), "facts")

print("── who may ever see a zone ──")
grid = {r: [z for z in zones.ZONES if zones.allowed(z, r, "p")] for r in ("admin", "adult", "kid", "household", "engineering")}
check("the zone × role grid", grid, {"admin": ["facts", "sync", "web", "system"], "adult": ["facts", "sync", "web", "system"],
                                     "kid": ["facts"], "household": ["facts"], "engineering": ["system"]})
check("titan itself sees every zone", all(zones.allowed(z, None, None) for z in zones.ZONES), True)
check("an unknown role: facts only", [z for z in zones.ZONES if zones.allowed(z, "visitor", "p")], ["facts"])

print("── the read check: zone before audience ──")
shared_mail = {**MAIL[1], "audience": "household"}     # someone shared a mail to the household
shared_page = {**WEB[1], "audience": "household"}
for who, role in (("ava", "kid"), (None, "household")):
    check(f"{role}: a mail shared to the household stays out", P.kb_visible(shared_mail, who, role, source_id=MAIL[0]), False)
    check(f"{role}: a saved page shared to the household stays out", P.kb_visible(shared_page, who, role, source_id=WEB[0]), False)
    check(f"{role}: a household note is there", P.kb_visible(NOTE[1], who, role, source_id=NOTE[0]), True)
    check(f"{role}: the runbook isn't", P.kb_visible(RUNBOOK[1], who, role, source_id=RUNBOOK[0]), False)
check("an adult: the mail shared to the household is there", P.kb_visible(shared_mail, "robin", "adult", source_id=MAIL[0]), True)
check("an adult: someone else's private mail isn't", P.kb_visible(MAIL[1], "robin", "adult", source_id=MAIL[0]), False)
check("an adult: the runbook isn't", P.kb_visible(RUNBOOK[1], "robin", "adult", source_id=RUNBOOK[0]), False)
P.set_acl("note-runbook-gpus", None, '["robin", "zach"]', by="zach")
check("…unless the admin shares that section with them", P.kb_visible(RUNBOOK[1], "robin", "adult", source_id="note-runbook-gpus"), True)
check("a shared runbook section still never reaches a kid", P.kb_visible(RUNBOOK[1], "ava", "kid", source_id="note-runbook-gpus"), False)
check("the admin: their mail and the runbook", (P.kb_visible(MAIL[1], "zach", "admin", source_id=MAIL[0]),
                                                 P.kb_visible(RUNBOOK[1], "zach", "admin", source_id=RUNBOOK[0])), (True, True))
check("engineering: the runbook only", (P.kb_visible(RUNBOOK[1], None, "engineering", source_id=RUNBOOK[0]),
                                         P.kb_visible(NOTE[1], None, "engineering", source_id=NOTE[0])), (True, False))
P.set_acl(MAIL[0], "zach", "household", by="test")
check("a sharing row saying household doesn't open sync to a kid", P.kb_visible(MAIL[1], "ava", "kid", source_id=MAIL[0]), False)
check("a missing row and no metadata: the admin only (Zach's private)",
      (P.acl_of({}, "note-legacy"), P.kb_visible({}, "robin", "adult", source_id="note-legacy"),
       P.kb_visible({}, "zach", "admin", source_id="note-legacy")), (("zach", "private"), False, True))

print("── every write is stamped with its zone ──")
import agent_runtime_lib as lib  # noqa: E402
for sid, md, st, want in ((MAIL[0], MAIL[1], "connector:mail", "sync"), (NOTE[0], NOTE[1], "note", "facts"),
                          (RUNBOOK[0], RUNBOOK[1], "knowledge_record", "system"), (WEB[0], WEB[1], "web", "web")):
    out = lib._stamped("retrieval", "/v1/retrieval/index", "POST", {"source_id": sid, "source_type": st, "metadata": dict(md)})
    check(f"{st} → {want}", out["metadata"]["zone"], want)
check("other writes untouched", lib._stamped("record", "/v1/nodes", "POST", {"node_id": "n", "data": {}}),
      {"node_id": "n", "data": {}})

print("── an explicit row for every item (kb_backfill_owner --explicit) ──")
import kb_backfill_owner as B  # noqa: E402
SRC = [{"source_id": MAIL[0], "source_type": "connector:mail"}, {"source_id": CAL[0], "source_type": "connector:calendar"},
       {"source_id": WEB[0], "source_type": "web"}, {"source_id": "note-old", "source_type": "note"}]
META = {CAL[0]: CAL[1], WEB[0]: WEB[1], "note-old": {}}
real_meta, real_sources = P.source_meta, lib.kb_sources
try:
    P.source_meta = lambda call, sid, updated_at="": META.get(sid, {})
    lib.kb_sources = lambda call=None, **k: SRC
    todo, counts = B.plan(None)
    check("default: only items whose metadata says nothing", [t[0] for t in todo], ["note-old"])
    todo, counts = B.plan(None, explicit=True)
    check("explicit: every item without a row, from its metadata", sorted((t[0], t[1], t[2]) for t in todo),
          sorted([(CAL[0], "zach", "private"), (WEB[0], "zach", "private"), ("note-old", "zach", "private")]))
    check("counts by zone, every source", {k: v for k, v in counts.items() if k.startswith("zone ")},
          {"zone sync": 2, "zone web": 1, "zone facts": 1})
    B.main(["--explicit", "--apply"], call=lambda *a, **k: {})
    check("applied: every item has a row now", sorted(P.acl_rows([s["source_id"] for s in SRC])),
          sorted(s["source_id"] for s in SRC))
    check("…and the table was backed up first, 0600",
          [oct(p.stat().st_mode & 0o777) for p in _TMP.glob("kb_acl.before-backfill-*.db")], ["0o600"])
finally:
    P.source_meta, lib.kb_sources = real_meta, real_sources

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
