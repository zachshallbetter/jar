#!/usr/bin/env python3
# gate: hermetic   (temp world files only)
"""jellybean#593 D6: "neither of these is right" for a disputed world fact. The person's own statements are withdrawn,
every other claim they can see is rejected as their statement (claim log, NEITHER_REASON), and the value becomes
unknown. "none" is never written. Claims they can't see are untouched. A re-sync of the same old evidence doesn't bring
a rejected value back; a firm source observing it again later does. Adults only.
Run: python3 scripts/test_world_neither.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ["HOUSEHOLD_DB"] = str(Path(tempfile.mkdtemp()) / "household.db")
os.environ["FENCES_DB"] = str(Path(tempfile.mkdtemp()) / "fences.db")
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = [datetime(2026, 10, 3, 12, tzinfo=timezone.utc).timestamp()]
w = W.World(Path(tempfile.mkdtemp()) / "world.db", clock=lambda: NOW[0])


def say(value, kind, at, ref, audience="household", owner=None, by=None, qual=None):
    """A source's item through sync (as the nightly connectors do: a re-sync updates the row, never re-adds it);
    a person's statement through state."""
    o = W.Obs(entity="biscuit", etype="pet", name="Biscuit", attr="vet", value=value, observed_at=at, kind=kind,
              ref=ref, origin=f"{kind}:{ref}", audience=audience, owner=owner, by=by, qual=qual or {})
    if kind == "user":
        w.state(o, by=by)
    else:
        w.sync(kind, [o], by="sync", remove_missing=False)


def vets(person, role="adult"):
    return sorted(v.value for f in w.facts("biscuit", person, role) if f.attr == "vet" for v in f.values)


def status(value):
    r = w.con.execute("SELECT status FROM claims WHERE attr='vet' AND value_norm=?", (W.norm_value("vet", value),)).fetchone()
    return r["status"] if r else None


say("Pearl Vet", "household", "2026-06-01", "hh-1")
say("Elm Clinic", "bill", "2026-09-01", "bill-1")
say("Hidden Vet", "note", "2026-09-15", "note-1", audience="private", owner="p_robin")   # another adult's own note
check("the setup: a dispute Alex sees", vets("p_alex"), ["Elm Clinic", "Pearl Vet"])
check("…and Robin's private note isn't Alex's to see", "Hidden Vet" in vets("p_alex"), False)

print("── neither ──")
ok, said, why = W.neither_fact(w, "biscuit", "vet", None, "p_alex", "adult")
check("done", (ok, why), (True, ""))
check("the value is unknown for Alex", vets("p_alex"), [])
check("both disputed claims are rejected", (status("Pearl Vet"), status("Elm Clinic")), ("rejected", "rejected"))
log = w.con.execute("SELECT by, reason FROM claim_log WHERE to_state='rejected'").fetchall()
check("the claim log has it as Alex's statement", sorted({(r["by"], r["reason"]) for r in log}), [("p_alex", W.NEITHER_REASON)])
check("Robin's private claim is untouched", status("Hidden Vet") not in ("rejected", "retracted"), True)
check("nothing is written as 'none'",
      w.con.execute("SELECT COUNT(*) FROM claims WHERE value_norm IN ('none','no','nothing')").fetchone()[0], 0)

print("── it holds ──")
NOW[0] += 3600
say("Pearl Vet", "household", "2026-06-01", "hh-1")      # the nightly re-sync of the same old evidence
say("Elm Clinic", "bill", "2026-09-01", "bill-1")
check("a re-sync of the same old evidence doesn't bring them back", vets("p_alex"), [])
say("Elm Clinic", "bill", "2026-10-05", "bill-2")         # a new bill, observed after Alex said neither
check("a firm source observing it again later does", vets("p_alex"), ["Elm Clinic"])

print("── their own statement is withdrawn, not rejected ──")
say("Oak Vet", "user", "2026-10-06", "u-1", by="p_alex")
before = vets("p_alex")
ok, _, _ = W.neither_fact(w, "biscuit", "vet", None, "p_alex", "adult")
check("Alex's own value was on record", "Oak Vet" in before, True)
check("…and is retracted, its evidence gone", (status("Oak Vet"),
      w.con.execute("SELECT COUNT(*) FROM evidence WHERE kind='user' AND by='p_alex'").fetchone()[0]), ("retracted", 0))
check("the value their statement had overridden comes back: they only said no to what they saw", vets("p_alex"),
      ["Elm Clinic"])

print("── who may ──")
check("a kid can't", W.neither_fact(w, "biscuit", "vet", None, "p_kid", "kid")[2], "denied")
check("nothing on record to reject", W.neither_fact(w, "biscuit", "birthday", None, "p_alex", "adult")[2], "nothing")
check("an unknown entity", W.neither_fact(w, "nobody", "vet", None, "p_alex", "adult")[2], "unknown")

print("── the page gives each fact its qual, so a client can send it back ──")
say("Night Vet", "household", "2026-10-07", "hh-q", qual={"when": "nights"})   # observed_at after the clock: still judged by when it was added
page = W.entity_page(w, "biscuit", "p_alex", "adult")
quals = sorted((f["attr"], tuple(sorted(f["qual"].items()))) for f in page["facts"] if f["attr"] == "vet")
check("every fact carries qual ({} when none)", all(isinstance(f.get("qual"), dict) for f in page["facts"]), True)
check("…the qualified vet shows its own", (("vet", (("when", "nights"),)) in quals), True)
q = next(f["qual"] for f in page["facts"] if f["attr"] == "vet" and f["qual"])
ok, _, _ = W.neither_fact(w, "biscuit", "vet", q, "p_alex", "adult")
check("neither with the page's qual reaches the qualified value", (ok, status("Night Vet")), (True, "rejected"))
check("…and leaves the unqualified one alone", vets("p_alex"), ["Elm Clinic"])

print("── the API takes it on the facts route ──")
src = (Path(__file__).with_name("agent_api_server.py")).read_text()
check("{attribute, neither: true} on POST /v1/world/entities/{id}/facts",
      ('body.get("neither") is True' in src, "_world.neither_fact" in src), (True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
