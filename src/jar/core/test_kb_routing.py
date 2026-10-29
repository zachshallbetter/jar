#!/usr/bin/env python3
# gate: hermetic
"""KB routing, phase 3 (docs/kb-partitions.md §6, #834): code picks a question's zones (zones.plan); the model's
zone hint only narrows; a role never gets a zone it may not see; the per-turn context keeps to the planned zones and a
search falls back to every allowed zone once. The routing bank (zones_routing_bank.json, made-up names; the cases
marked `real` are shaped on real titan questions) must route ≥ 90% to the right first zone. No runtime.
Run: python3 scripts/test_kb_routing.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kbroute-"))
for k, v in {"KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory",
             "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl", "DAILY_LOG_DIR": "daily",
             "HOUSEHOLD_PREFS": "household.json", "CONNECTORS_LEDGER": "ledger.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("KB_ZONES", None)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import zones  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


print("── the routing bank ──")
bank = json.loads((HERE / "zones_routing_bank.json").read_text())
hits = [c for c in bank if zones.plan(c["q"], "admin", "zach")[0][0] == c["first"]]
for c in bank:
    if c not in hits:
        print(f"    miss: {c['first']} → {zones.plan(c['q'], 'admin', 'zach')[0][0]}: {c['q']}")
check(f"≥ 90% to the right first zone ({len(hits)}/{len(bank)})", len(hits) / len(bank) >= 0.9, True)
check("the bank covers every zone", sorted({c["first"] for c in bank}), sorted(zones.ZONES))

print("── a role never gets a zone it may not see ──")
check("a kid asking about mail: facts only, with the whole budget", zones.plan("Did Robin email me?", "kid"), [("facts", 5)])
check("a shared screen asking about the runbook: facts only", zones.plan("What does the runbook say?", "household"),
      [("facts", 6)])
check("engineering: system only", zones.plan("How do I deploy titan?", "engineering"), [("system", 4)])
check("the admin on mail: mail first, facts after", zones.plan("Any receipts from Hale Outdoors?", "admin"),
      [("sync", 4), ("facts", 1)])

print("── the model's hint only narrows ──")
check("a hint inside the plan narrows to it", zones.plan("When is the recital?", "admin", hint="facts"), [("facts", 5)])
check("a hint outside the plan is ignored", zones.plan("When is the recital?", "admin", hint="system"),
      [("sync", 3), ("facts", 2)])
check("a kid's 'system' hint adds nothing", zones.plan("What's the wifi?", "kid", hint="system"), [("facts", 6)])
check("a pasted link searches saved pages first (10-04 turn review)",
      zones.plan("https://github.com/example/runtime take a look", "admin")[0][0], "web")
check("an ordinary question keeps one saved page in reach", zones.plan("What's the wifi?", "admin"),
      [("facts", 3), ("sync", 2), ("web", 1)])
check("a search also keeps one runbook passage in reach for an admin (ops questions without titan's words, 10-05)",
      zones.plan("How do scheduled actions work?", "admin", search=True), [("facts", 3), ("sync", 2), ("web", 1), ("system", 1)])
check("…never for a role that can't see the system zone (a kid, a shared screen)",
      [[z for z, _ in zones.plan("How do scheduled actions work?", r, search=True)] for r in ("kid", "household")],
      [["facts"], ["facts"]])
check("…an adult may (the item audience still decides: runbook items are the admins' unless shared)",
      "system" in [z for z, _ in zones.plan("How do scheduled actions work?", "adult", search=True)], True)
check("…and the per-turn context is unchanged", zones.plan("How do scheduled actions work?", "admin"),
      [("facts", 3), ("sync", 2), ("web", 1)])

print("── titan's words and mail words both: a search covers both (#1225) ──")
both_q = "Did the canary email me after the deploy?"
check("admin search: runbook first, then mail, then facts", zones.plan(both_q, "admin", search=True),
      [("system", 2), ("sync", 2), ("facts", 1)])
check("…the per-turn context keeps the first match (the runbook)", zones.plan(both_q, "admin"), [("system", 3), ("facts", 1)])
check("…a kid's or a shared screen's plan is unchanged (facts only)",
      (zones.plan(both_q, "kid", "p_ava", search=True), zones.plan(both_q, "household", "screen", search=True)),
      ([("facts", 5)], [("facts", 5)]))
check("…titan's words alone, or mail words alone: as before",
      (zones.plan("How do I deploy titan?", "admin", search=True), zones.plan("Any mail from the school?", "admin", search=True)),
      ([("system", 3), ("facts", 1)], [("sync", 4), ("facts", 1)]))

print("── arranging ranked hits by the plan ──")


def h(sid, md=None):
    return {"source_id": sid, "metadata": md or {}}


ranked = [h("note-runbook-a", {"origin": "kb_seed_runbook.py"}), h("cx-mail-1"), h("note-1"), h("cx-calendar-1"),
          h("web-1", {"url": "u"}), h("cx-mail-2"), h("note-2")]
ids = lambda xs: [x["source_id"] for x in xs]  # noqa: E731
check("mail plan: the planned zones in rank order, each at most its budget",
      ids(zones.arrange(ranked, [("sync", 2), ("facts", 1)], 5)), ["cx-mail-1", "note-1", "cx-calendar-1"])
check("a better-ranked household note isn't pushed below the calendar (the late-start miss, 10-04)",
      ids(zones.arrange([h("note-late"), h("cx-calendar-9"), h("cx-calendar-8")], [("sync", 3), ("facts", 2)], 3)),
      ["note-late", "cx-calendar-9", "cx-calendar-8"])
check("never a zone outside the plan", any(x.startswith(("web-", "note-runbook")) for x in
                                           ids(zones.arrange(ranked, [("sync", 2), ("facts", 1)], 7))), False)
only_web = [h("web-9", {"url": "u"}), h("note-9")]
check("a search with nothing in its zones falls back to everything visible, once",
      ids(zones.arrange(only_web, [("sync", 3)], 3)), ["web-9", "note-9"])
check("the per-turn context doesn't fall back", zones.arrange(only_web, [("sync", 3)], 3, fallback=False), [])
runbook = [h(f"note-runbook-{i}", {"origin": "kb_seed_runbook.py"}) for i in range(4)] + [h("web-3", {"url": "u"})]
search_plan = [("facts", 3), ("sync", 2), ("web", 1), ("system", 1)]
check("a search: budget the other zones leave unspent goes to the next-ranked planned hits, after the budgeted ones (#1225)",
      ids(zones.arrange(runbook, search_plan, 9)), ["note-runbook-0", "web-3", "note-runbook-1", "note-runbook-2", "note-runbook-3"])
check("…so an extra never moves a budgeted pick down (the calendar answer stays in the top 3)",
      ids(zones.arrange([h("note-a"), h("note-b"), h("note-c"), h("cx-calendar-1")], [("sync", 3), ("facts", 2)], 9))[:3],
      ["note-a", "note-b", "cx-calendar-1"])
check("…never past the plan's total", len(zones.arrange(runbook * 3, search_plan, 99)), 7)
check("…never a zone outside the plan (a kid's plan is facts only)",
      ids(zones.arrange(runbook + [h("note-5")], [("facts", 3)], 9)), ["note-5"])
check("the per-turn context keeps each zone's budget", ids(zones.arrange(runbook, search_plan, 9, fallback=False)),
      ["note-runbook-0", "web-3"])

print("── wired into titan's search, only with KB_ZONES=1 ──")
import agent_runtime_lib as lib  # noqa: E402
import kb_privacy  # noqa: E402
real_q = kb_privacy.query
try:
    kb_privacy.query = lambda call, text, k, person, role, timeout=30: [dict(x, score=1.0, chunk_text="recital mail note")
                                                                        for x in ranked]
    lib._TURN_IO.person, lib._TURN_IO.role = {"id": "zach", "name": "Alex"}, "admin"
    off = ids(lib._zone_order("Did the school email about the recital?", ranked, "zach", "admin", False))
    check("off: the runtime's order, untouched", off, ids(ranked))
    os.environ["KB_ZONES"] = "1"
    on = ids(lib._zone_order("Did the school email about the recital?", ranked, "zach", "admin", False))
    check("on: a mail question keeps the runtime's rank among mail and notes; sync's unspent slot goes to the next note "
          "(#1225)", on, ["cx-mail-1", "note-1", "cx-calendar-1", "cx-mail-2", "note-2"])
    check("on: no runbook or saved page for a mail question", any(x.startswith(("note-runbook", "web-")) for x in on), False)
    check("on: the context leaves titan's runbook out of a household question",
          "note-runbook-a" in ids(lib._zone_order("When is the recital?", ranked, "zach", "admin", True)), False)
    check("on: a titan question gets it", ids(lib._zone_order("How do I deploy titan?", ranked, "zach", "admin", True))[0],
          "note-runbook-a")
    check("on: the hint narrows the explicit search",
          set(ids(lib._zone_order("When is the recital?", ranked, "zach", "admin", False, zone="facts"))), {"note-1", "note-2"})
finally:
    kb_privacy.query = real_q
    os.environ.pop("KB_ZONES", None)

print("── the model sees the zone hint only with KB_ZONES=1 ──")
probe = "import agent_runtime_lib as l; print('zone' in l._TOOL_REGISTRY['kb_retrieve']['parameters'])"
env = dict(os.environ)
check("off: kb_retrieve's parameters unchanged (no prompt change)",
      subprocess.run([sys.executable, "-c", probe], cwd=HERE, env=env, capture_output=True, text=True).stdout.strip(), "False")
check("on: kb_retrieve takes a zone",
      subprocess.run([sys.executable, "-c", probe], cwd=HERE, env={**env, "KB_ZONES": "1"}, capture_output=True,
                     text=True).stdout.strip(), "True")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
