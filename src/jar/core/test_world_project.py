#!/usr/bin/env python3
# gate: hermetic
"""A `project` in someone's professional world (Zach 10-10): an entity type with role, client, status and note, made
through set_fact like any fact, filed under the person's own `work` facet (set_fact's `facet`: an adult, their own
bubble only, never changing who sees it). Views × viewers: its owner sees it; another adult, a kid and a shared screen
see nothing. Fictional household (Alex admin, Robin adult, Ava kid, a living-room screen); temporary stores.
Run: python3 scripts/test_world_project.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-project-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "WORLD_DIR": "world", "FENCES_DB": "fences.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.setdefault("AGENT_TZ", "America/Los_Angeles")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


ALEX, ROBIN, AVA, SCREEN = "alex", "robin", "ava", "screen-1"
PEOPLE = [{"id": ALEX, "name": "Alex Quarry", "role": "admin"}, {"id": ROBIN, "name": "Robin Quarry", "role": "adult"},
          {"id": AVA, "name": "Ava Quarry", "role": "kid", "parents": [ALEX, ROBIN]},
          {"id": SCREEN, "name": "Living room", "role": "household"}]
B = W.Bubbles(_TMP / "world")
SRC = {"ref": "chat:alex:2026-10-10T18:00", "label": "Alex, 10-10", "observed_at": "2026-10-10T18:00:00-07:00"}

print("── the type ──")
check("project is a type with role, client, status, mailbox (#1490) and note", sorted(W.ATTRS["project"]), ["client", "mailbox", "note", "role", "status"])
done, said, why = W.set_fact(B, "Harbor Works", "role", "client project", ALEX, "admin", etype="project", people=PEOPLE,
                             source=SRC, facet="work")
check("Alex records it: done", (done, why), (True, "ok"))
cid = (B.find("Harbor Works", ALEX, "admin") or [None])[0]
ent = dict(B.entity(cid)) if cid else {}
check("in Alex's own bubble, a project, filed under work, private to him",
      (B.split(cid)[0] if cid else None, ent.get("type"), ent.get("facet"), ent.get("audience")),
      (W.bubble_name(ALEX), "project", "work", "private"))
W.set_fact(B, "Harbor Works", "client", "Harbor Co", ALEX, "admin", people=PEOPLE, source=SRC)
W.set_fact(B, "Harbor Works", "status", "in progress", ALEX, "admin", people=PEOPLE, source=SRC)
facts = {f.attr: f.values[0].value for f in B.facts(cid, ALEX, "admin")}
check("its facts, for Alex", facts, {"role": "client project", "client": "Harbor Co", "status": "in progress"})
ev = B.world(W.bubble_name(ALEX)).con.execute("SELECT DISTINCT ev.label, ev.audience FROM evidence ev JOIN claims c ON "
                                              "c.id=ev.claim_id WHERE c.entity_id=?", (B.split(cid)[1],)).fetchall()
check("…as Alex said them, with that provenance, private", sorted(tuple(r) for r in ev), [("Alex, 10-10", "private")])

print("── views × viewers ──")
for who, role in ((ROBIN, "adult"), (AVA, "kid"), (None, "household")):
    check(f"{who or 'the screen'} finds nothing and reads nothing",
          (B.find("Harbor Works", who, role), [f.attr for f in B.facts(cid, who, role)]), ([], []))

print("── set_fact's facet ──")
W.set_fact(B, "Pine Studio", "role", "a client", ALEX, "admin", etype="project", people=PEOPLE)
pine = B.find("Pine Studio", ALEX, "admin")[0]
before = dict(B.entity(pine))["audience"]
done, _s, why = W.set_fact(B, "Pine Studio", "status", "paused", ALEX, "admin", people=PEOPLE, facet="work")
check("an existing project of his, refiled under work later", (done, dict(B.entity(pine))["facet"]), (True, "work"))
check("…its claims follow", {r[0] for r in B.world(W.bubble_name(ALEX)).con.execute(
    "SELECT facet FROM claims WHERE entity_id=?", (B.split(pine)[1],))}, {"work"})
check("…and who sees it never changes", dict(B.entity(pine))["audience"], before)
check("not a facet: refused", W.set_fact(B, "Pine Studio", "status", "x", ALEX, "admin", people=PEOPLE, facet="home")[2],
      "invalid")
check("a kid can't file things", W.set_fact(B, "Ava's club", "role", "drawing", AVA, "kid", etype="project", people=PEOPLE,
                                            facet="school")[2], "denied")
W.set_fact(B, "The bins", "note", "Tuesday", ALEX, "admin", etype="activity", people=PEOPLE, household=True)
check("never another bubble's (a household entity)", W.set_fact(B, "The bins", "note", "Wednesday", ALEX, "admin",
                                                                 people=PEOPLE, facet="personal")[2], "denied")
check("a shared screen sets nothing", W.set_fact(B, "Harbor Works", "status", "done", None, "household", people=PEOPLE,
                                                facet="work")[2], "denied")

print("── a named type picks the thing ──")
W.set_fact(B, "Harbor", "phone", "555-0100", ALEX, "admin", etype="org", people=PEOPLE)
org = B.find("Harbor", ALEX, "admin")[0]
check("the tool's call: a project named like his org asks first, never writes onto the org",
      (W.set_fact(B, "Harbor project", "role", "client project", ALEX, "admin", etype="project", people=PEOPLE,
                  near_check=True)[2], sorted(f.attr for f in B.facts(org, ALEX, "admin"))), ("near", ["phone"]))
done, _s, why = W.set_fact(B, "Harbor project", "role", "client project", ALEX, "admin", etype="project", people=PEOPLE,
                           facet="work")
hp = [i for i in B.find("Harbor project", ALEX, "admin") if dict(B.entity(i))["type"] == "project"]
check("…said again (it's new): its own project, the org untouched", (done, why, len(hp),
      sorted(f.attr for f in B.facts(org, ALEX, "admin")), dict(B.entity(org))["type"]), (True, "ok", 1, ["phone"], "org"))
check("no type named: the match it finds, as before",
      W.set_fact(B, "Harbor", "hours", "9-5", ALEX, "admin", people=PEOPLE)[2], "ok")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
