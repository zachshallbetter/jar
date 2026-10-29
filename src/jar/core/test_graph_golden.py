#!/usr/bin/env python3
"""The relational FE graph's shared fixture is consistent (#1399): graph_golden/household.json and 5w.json.

- Every id a case, link, hook, proposal, scenario or fence names exists in the fixture.
- Only invented names: none of the household's real first names appear (fixtures rule).
- `expected` (P1's per-viewer node sets) agrees with a small reference model of titan's rules: audience
  (private, adults, household, a list), fences, parents seeing a kid's items, shared screens seeing
  household items only. P1's real projection is checked against `expected` by its own test; this one
  makes sure `expected` itself is right before anything is built on it.

Run: python3 scripts/test_graph_golden.py
"""
# gate: hermetic
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

G = Path(__file__).resolve().parent / "graph_golden"
H = json.loads((G / "household.json").read_text())
W = json.loads((G / "5w.json").read_text())
passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": {detail}"))


people = {p["id"]: p for p in H["people"]}
items = {i["id"]: i for i in H["items"]}
places = {p["id"]: p for p in H["places"]}
place_events = {e["id"]: e for e in H["place_events"]}
scenarios = {s["task_id"]: s for s in H["scenarios"]}
entities = {f"{b}:{e['id']}": e for b, w in H["world"].items() for e in w["entities"]}
records = {f"rec-{i}" for i, it in items.items() if it["kind"] == "mail" and "-bill-" in i}   # filed from the bill mail
known = set(people) | set(items) | set(places) | set(place_events) | set(scenarios) | set(entities) | records

print("── ids resolve ──")
refs = [(f"link {l['a']}–{l['b']}", x) for l in H["links"] + H["link_statements"] for x in (l["a"], l["b"])]
refs += [(f"hook on {h['item']}", h["item"]) for h in H["hooks"]]
refs += [(f"fence on {f['item']}", f["item"]) for f in H["fences"]]
refs += [(f"scenario {s['task_id']}", x) for s in H["scenarios"] for x in s["deps"]["items"] + s["deps"]["entities"]]
refs += [(f"proposal {p['sig_id']}", x) for p in H["event_proposals"] for x in p["participants"]]
for c in W["cases"]:
    e = c["expect"]
    for k in ("via", "lacks"):
        refs += [(f"case {c['id']} {k}", x) for x in e.get(k, []) if re.fullmatch(r"(cx|dev|chat|pe|pl|task)-[\w-]+", x)]
    for slot in ("who", "where"):
        v = e.get(slot)
        if isinstance(v, dict):
            # ids only: a slot may also expect free text (an unpromoted place is answered from its item's words)
            refs += [(f"case {c['id']} {slot}", x) for x in v.get("is", []) + v.get("has", [])
                     if re.fullmatch(r"(p_\w+|pl-[\w-]+|\w+:[\w-]+)", x)]
missing = [(w, x) for w, x in refs if x not in known]
check(f"all {len(refs)} referenced ids exist", not missing, str(missing[:5]))
viewers = {c["as"][0] for c in W["cases"]}
check("every case asks as a household member or the shared screen", viewers <= set(people) | {"screen"}, str(viewers - set(people)))
check(f"{len(W['cases'])} cases, each with an expectation", all(c.get("expect") for c in W["cases"]) and len(W["cases"]) >= 40)

print("── invented names only ──")
REAL = ("zach", "miranda", "sabrina", "conor", "kate", "sam ", "sam-", "swaia")
blob = ((G / "household.json").read_text() + (G / "5w.json").read_text()).lower()
hits = [n for n in REAL if n in blob]
check("no real household or contact names", not hits, str(hits))

print("── `expected` matches the reference visibility model ──")
fenced = {(f["item"], v) for f in H["fences"] for v in f["hidden_from"]}


def sees(viewer: str, owner: str | None, audience) -> bool:
    if viewer == "screen":
        return audience == "household"
    role = people[viewer]["role"]
    if owner == viewer:
        return True
    if audience == "household":
        return True
    if audience == "adults":
        return role in ("admin", "adult")
    if isinstance(audience, list):
        return viewer in audience
    # private: the owner, and a kid's parents for the kid's own items
    return owner in people and viewer in people[owner].get("parents", [])


def visible_items(viewer):
    return sorted(i for i, it in items.items() if sees(viewer, it["owner"], it["audience"]) and (i, viewer) not in fenced)


def visible_people(viewer):
    out = set(people)
    for b, w in H["world"].items():
        for e in w["entities"]:
            if e["type"] == "person" and not e.get("person_id") and sees(viewer, e["owner"], e["audience"]):
                out.add(f"{b}:{e['id']}")
    return sorted(out)


def visible_places(viewer):
    return sorted(p for p, pl in places.items()
                  if sees(viewer, pl["owner"], pl["audience"]) and not (pl["kind"] == "candidate" and pl["sightings"] < 3))


for viewer, exp in H["expected"].items():
    if viewer == "about":
        continue
    check(f"{viewer}: items", sorted(exp["item"]) == visible_items(viewer),
          f"expected-only {sorted(set(exp['item']) - set(visible_items(viewer)))}, model-only {sorted(set(visible_items(viewer)) - set(exp['item']))}")
    check(f"{viewer}: people", sorted(exp["person"]) == visible_people(viewer),
          f"{sorted(set(exp['person']) ^ set(visible_people(viewer)))}")
    check(f"{viewer}: places (a candidate seen under 3 times is never a place)", sorted(exp["place"]) == visible_places(viewer),
          f"{sorted(set(exp['place']) ^ set(visible_places(viewer)))}")

print("── cases never expect what the viewer can't see ──")
leaks = []
for c in W["cases"]:
    v = c["as"][0]
    for x in c["expect"].get("via", []):
        if x in items and x not in visible_items(v):
            leaks.append((c["id"], x))
check("every via item is visible to its case's viewer", not leaks, str(leaks))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
