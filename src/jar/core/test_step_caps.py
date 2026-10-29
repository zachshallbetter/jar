#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A task step's cap on a slider (jellybean#707): declared in code with ceilings, never run uncapped, one card to the
owner whose Approve label shows the amount it sends, the slider's number accepted only for its own cap, never an amount
the person didn't see, the cap in the step's context, and an over-cap result not accepted. No money paths. Fictional.
Run: python3 scripts/test_step_caps.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

tmp = Path(tempfile.mkdtemp(prefix="titan-caps-"))
os.environ["TASKS_DB"] = str(tmp / "tasks.db")
os.environ["PROACTIVE_DB"] = str(tmp / "proactive.db")
os.environ["AGENT_TASK_AUTONOMY"] = "1"
os.environ["AGENT_TASK_AUTONOMY_OWNERS"] = ""
sys.path.insert(0, str(Path(__file__).parent))

import scenarios as sc  # noqa: E402
import step_caps as C  # noqa: E402
import task_runner as tr  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── declaring ──")
caps, bad = C.parse("2: usd 40; 3: count 3; 4: usd 5000; 9: usd 10; huh", 4)
check("a step's cap, by its plan number", caps, {"s1": ("usd", 40.0), "s2": ("count", 3.0)})
check("…over the ceiling, outside the plan, unreadable: refused, said", len(bad), 3)

print("\n── the card ──")
cid = C.declare("t-cap", "alex", "s1", "Order the soccer cleats", "usd", 40)
row = C.need("t-cap", "s1")
title, detail, actions, data = C.card(row)
check("fixed wording; the slider's data", (title, detail.endswith("Nothing is bought or paid."),
      {k: data[k] for k in ("kind", "unit", "min", "max", "step", "default")}),
      ("Set a limit for “Order the soccer cleats”", True,
       {"kind": "approve_cap", "unit": "usd", "min": 5, "max": 80, "step": 5, "default": 40}))
check("Approve's label shows the amount its value sends", (actions[0]["label"], actions[0]["value"]),
      ("Approve up to $40", f"cap:{cid}:40"))
sent = []
check("asked once", (C.ask(row, lambda *a, **k: sent.append(k)), C.ask(C.need("t-cap", "s1"), lambda *a, **k: sent.append(k)),
                     len(sent), sent[0]["to"], sent[0]["source"]), (True, False, 1, ["alex"], "approve-cap"))

print("\n── the tap ──")
check("another person: refused", C.answer(f"cap:{cid}:40", "robin", "adult")[0], False)
check("a kid: refused", C.answer(f"cap:{cid}:40", "alex", "kid")[0], False)
check("no number: refused (never an amount they didn't see)", C.answer(f"cap:{cid}", "alex", "admin"),
      (False, "Update the app to set a limit: this version can't show the amount."))
check("out of range: refused", C.answer(f"cap:{cid}:500", "alex", "admin")[0], False)
check("Not now: waits", C.answer(f"nocap:{cid}", "alex", "admin")[0] and C.need("t-cap", "s1")["state"] != "approved", True)
ok, said = C.answer(f"cap:{cid}:47", "alex", "admin", {"id": "d-ip"})
r = C.need("t-cap", "s1")
check("the owner's number, snapped to the step, kept with who and which device", (ok, r["value"], r["set_by"], r["device"],
                                                                                   "Nothing was bought" in said),
      (True, 45.0, "alex", "d-ip", True))
check("…and only once", C.answer(f"cap:{cid}:60", "alex", "admin")[0], False)

print("\n── through a card (the slider's own number, only for its own cap) ──")
import proactive  # noqa: E402
cid2 = C.declare("t-cap", "alex", "s2", "Buy tickets", "count", 3)
C.ask(C.need("t-cap", "s2"), proactive.add_card)
card = next(c for c in proactive.list_cards(person="alex", role="admin") if c["source"] == "approve-cap")
appr = next(a for a in card["actions"] if a["label"].startswith("Approve"))
out = proactive.card_action(card["id"], appr["id"], person="alex", role="admin", value=f"cap:{cid}:5")
check("a number naming another cap is ignored: the button's own amount is approved", (out["ok"], C.need("t-cap", "s2")["value"]),
      (True, 3.0))
cid3 = C.declare("t-cap", "alex", "s3", "Buy more", "count", 2)
C.ask(C.need("t-cap", "s3"), proactive.add_card)
card = next(c for c in proactive.list_cards(person="alex", role="admin") if c["source"] == "approve-cap" and cid3 in str(c["actions"]))
appr = next(a for a in card["actions"] if a["label"].startswith("Approve"))
proactive.card_action(card["id"], appr["id"], person="alex", role="admin", value=f"cap:{cid3}:4")
check("the slider's number for its own cap is used", C.need("t-cap", "s3")["value"], 4.0)

print("\n── relayed from the Watch (jellybean#900): an explicit amount, never the suggestion unseen ──")


def cap_card(cid_):
    C.ask(C.need("t-cap", cid_steps[cid_]), proactive.add_card)
    c_ = next(c for c in proactive.list_cards(person="alex", role="admin") if c["source"] == "approve-cap" and cid_ in str(c["actions"]))
    return c_, next(a for a in c_["actions"] if a["label"].startswith("Approve")), next(a for a in c_["actions"]
                                                                                         if a["value"].startswith("nocap:"))


cid_steps = {}
cid4 = C.declare("t-cap", "alex", "s4", "Book the pool", "usd", 30)
cid_steps[cid4] = "s4"
card, appr, nocap = cap_card(cid4)
for v in (None, "", f"cap:{cid}:30", "30", f"nocap:{cid4}"):
    out = proactive.card_action(card["id"], appr["id"], person="alex", role="admin", value=v, via="watch")
    check(f"Approve from the Watch with value {v!r}: refused with the fixed line, nothing approved",
          (out["ok"], out["said"], C.need("t-cap", "s4")["state"]), (False, C.WATCH_NEEDS_AMOUNT, "asked"))
check("…the card is still there", any(c["id"] == card["id"] for c in proactive.list_cards(person="alex", role="admin")), True)
out = proactive.card_action(card["id"], appr["id"], person="alex", role="admin", value=f"cap:{cid4}:30", via="watch")
r = C.need("t-cap", "s4")
check("the amount the Watch's button showed, sent explicitly: approved, kept with via watch",
      (out["ok"], r["value"], r["via"]), (True, 30.0, "watch"))
cid5 = C.declare("t-cap", "alex", "s5", "Book the court", "usd", 20)
cid_steps[cid5] = "s5"
card, appr, nocap = cap_card(cid5)
out = proactive.card_action(card["id"], nocap["id"], person="alex", role="admin", via="watch")
check("Not now from the Watch still works", (out["ok"], C.need("t-cap", "s5")["state"] != "approved"), (True, True))
cid6 = C.declare("t-cap", "alex", "s6", "Book the field", "usd", 25)
cid_steps[cid6] = "s6"
card, appr, _ = cap_card(cid6)
out = proactive.card_action(card["id"], appr["id"], person="alex", role="admin", via="phone")
r = C.need("t-cap", "s6")
check("an unknown via is today's tap: the button's own amount, no via kept", (out["ok"], r["value"], r["via"]), (True, 25.0, None))
cid7 = C.declare("t-cap", "alex", "s7", "Book the gym", "usd", 15)
cid_steps[cid7] = "s7"
card, appr, _ = cap_card(cid7)
out = proactive.card_action(card["id"], appr["id"], person="alex", role="admin")
check("…and so is a missing one", (out["ok"], C.need("t-cap", "s7")["value"], C.need("t-cap", "s7")["via"]), (True, 15.0, None))
import sqlite3  # noqa: E402
old = tmp / "old_tasks.db"
con = sqlite3.connect(old)
con.execute("""CREATE TABLE caps (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, step_key TEXT NOT NULL,
        step_title TEXT NOT NULL, owner TEXT NOT NULL, unit TEXT NOT NULL, proposed REAL NOT NULL, min REAL NOT NULL,
        max REAL NOT NULL, step REAL NOT NULL, state TEXT NOT NULL, value REAL, set_by TEXT, device TEXT, at REAL,
        created_at REAL NOT NULL, UNIQUE (task_id, step_key))""")
con.commit(); con.close()
C._db(old).close()
con = sqlite3.connect(old)
check("an older caps table gains the via column", "via" in {r_[1] for r_ in con.execute("PRAGMA table_info(caps)")}, True)
con.close()

print("\n── the runner ──")
PT = ZoneInfo("America/Los_Angeles")


def at(h, day=2):
    return datetime(2026, 10, day, h, tzinfo=PT).timestamp()


cards, steps = [], []
step_text = {"t": "✓ Found cleats at $38.\n(1 step run; task is running)"}
RUN = dict(execute_step=lambda s, ctx, p: (steps.append(ctx), step_text["t"])[1],
           card=lambda t, d, **k: cards.append((t, d)), changes_fn=lambda *a, **k: [], world_fn=lambda *a: [],
           describe=lambda cs: "", grant_use=lambda gid, owner, tid: None, person_of=lambda o: {"id": o, "role": "admin"})
tr.opt_in("t-run", "alex", "admin", "Get the cleats", "daily", daily_cap=3, issue_grant=lambda o, s, n: {"id": "gr"},
          resolve=lambda n: {"items": [], "entities": []}, now=at(8))
cr = C.declare("t-run", "alex", "s0", "Order the cleats", "usd", 40)
asked = []
got = tr.run_due(at(10), current_step=lambda tid: ("s0", ""), cap_card=lambda *a, **k: asked.append(k), **RUN)
check("never run uncapped: it waits, one card asks", (got, steps, len(asked)), ([{"task_id": "t-run", "status": "needs_cap"}], [], 1))
C.answer(f"cap:{cr}:40", "alex", "admin", now=at(10))
got = tr.run_due(at(11), current_step=lambda tid: ("s0", ""), cap_card=lambda *a, **k: asked.append(k), **RUN)
check("approved: the step runs with its limit in its context", (got[0]["status"] != "needs_cap",
                                                                 "stay within up to $40" in (steps[-1] if steps else "")), (True, True))
step_text["t"] = "✓ Best pair is $55 with shipping.\n(1 step run; task is running)"
sc.update("t-run", state="active", next_due=at(12), db=None)
got = tr.run_due(at(13), current_step=lambda tid: ("s0", ""), cap_card=lambda *a, **k: asked.append(k), **RUN)
check("a result over the limit: not accepted, the task waits, a card names the amount",
      (got, cards[-1][0].startswith("Over the limit"), "$55" in cards[-1][1], sc.get("t-run")["state"]),
      ([{"task_id": "t-run", "status": "over_cap"}], True, True, "waiting"))
check("over(): counts too", C.over({"unit": "count", "value": 3}, "qty 5 of them, 2 tickets"), [5.0])

print("\n── nothing here moves money ──")
src = (Path(C.__file__)).read_text()
check("no record writes, purchases or payments", [w for w in ("/v1/nodes", "purchase(", "pay(", "transfer") if w in src], [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
