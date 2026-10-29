#!/usr/bin/env python3
# gate: hermetic
"""Text hooks with a time, a place and a direction (#1436, titan side): a paired Mac posts body-free hooks through
/v1/hooks (hooks.put_device), and the relational graph projects them. Over the golden household (graph_golden_load:
Alex, Robin, Jordan, Ava, Leo in temp stores, never titan's):
  - a text's date:mention with a time of day → the item's attrs.when (household-zone local);
  - mention:place (a registry place id) → mentions → place, accepted, observed, "the text names <place>";
  - a directed party:phone → from / to the person, accepted (an undirected one stays a candidate from);
  - a named place plus the event's day → about the calendar event (measured);
  - every one of them is the text owner's only: never another adult, a kid or the household screen.
Run: python3 scripts/test_graph_text_hooks.py"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_golden_load as L  # noqa: E402  (sets every store path to a temp dir and loads the fixture)

import graph_sync as G  # noqa: E402
import hooks  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


ROBIN_PHONE, JORDAN_PHONE = "+1 555 010 0001", "(555) 010-0003"     # the household world's claims (fixture)
con = hooks.connect()
# Robin's text to Alex: "Saturday 10:00 at Pine Park?" (the body stays on the Mac; only its hooks come)
ask = hooks.put_device("p_alex", "admin", "d_alex_mac", {
    "item_id": "guid-plan-ask", "source": "text", "at": "2026-10-09", "hooks": [
        {"kind": "party:phone", "value": ROBIN_PHONE, "dir": "from", "line": 0},
        {"kind": "date:mention", "value": "2026-10-10T10:00", "line": 0},
        {"kind": "mention:place", "value": "pl-pine-park", "line": 0}]}, con)["item_id"]
# Alex's reply to Robin and Jordan: "Yes, see you then"
reply = hooks.put_device("p_alex", "admin", "d_alex_mac", {
    "item_id": "guid-plan-reply", "source": "text", "at": "2026-10-09", "hooks": [
        {"kind": "party:phone:to", "value": ROBIN_PHONE},
        {"kind": "party:phone", "value": JORDAN_PHONE, "dir": "to"}]}, con)["item_id"]
# Alex's text about Ava's dentist visit: "Summit Dental on the 13th at 3"
dentist = hooks.put_device("p_alex", "admin", "d_alex_mac", {
    "item_id": "guid-dentist", "source": "text", "at": "2026-10-09", "hooks": [
        {"kind": "date:mention", "value": "2026-10-13T15:00:00"},
        {"kind": "mention:place", "value": "pl-summit"}]}, con)["item_id"]
con.close()

path = L.build()
ALEX = ("p_alex", "admin")
seen = G.visible_nodes(*ALEX, path)

print("── when: a text's stated time of day, on its item ──")
check("the ask's item carries its time (household-zone local)", seen[ask]["attrs"].get("when"), ["2026-10-10T10:00"])
check("seconds are dropped", seen[dentist]["attrs"].get("when"), ["2026-10-13T15:00"])
check("a text with no stated time has no when", "when" in seen[reply]["attrs"], False)
check("the golden texts' day-only date hooks give no when", "when" in seen["dev-text-plan-1"]["attrs"], False)


def edges(viewer, role, a, **kw):
    return sorted((e["type"], e["b"], e["state"], e["rung"], e["why"])
                  for e in G.visible_edges(viewer, role, path, a=a, states=("verified", "accepted", "candidate"), **kw))


print("── who and where, as edges ──")
check("the ask: from Robin (accepted: the Mac said she sent it), names Pine Park (observed), about no event",
      edges(*ALEX, ask), [("from", "p_robin", "accepted", "attributed", "sent by Robin Quarry"),
                          ("mentions", "pl-pine-park", "accepted", "observed", "the text names Pine Park")])
check("the reply: to Robin and to Jordan (accepted)", edges(*ALEX, reply),
      [("to", "p_jordan", "accepted", "attributed", "sent to Jordan Hale"),
       ("to", "p_robin", "accepted", "attributed", "sent to Robin Quarry")])
check("an undirected text party stays a candidate from (dev-text-plan-1)",
      [e[:3] for e in edges(*ALEX, "dev-text-plan-1", types=["from"])], [("from", "p_robin", "candidate")])
check("the dentist text: names Summit Dental, and the place plus the day make it about Ava's dentist visit (measured)",
      edges(*ALEX, dentist),
      [("about", "event:cx-calendar-dentist", "accepted", "attributed", "same day and Summit Dental as Ava dentist"),
       ("mentions", "pl-summit", "accepted", "observed", "the text names Summit Dental")])
about = [e for e in G.visible_edges(*ALEX, path, a=dentist) if e["type"] == "about"]
check("…the about edge is measured, never typed", [e["plane"] for e in about], ["measured"])

print("── the text owner's only ──")
for viewer, role, who in (("p_robin", "adult", "Robin, another adult (and the sender)"), ("p_jordan", "adult", "Jordan"),
                          ("p_ava", "kid", "Ava, a kid"), (None, "household", "the household screen")):
    got = [e for e in G.visible_edges(viewer, role, path, states=("verified", "accepted", "candidate"))
           if e["a"] in (ask, reply, dentist)]
    check(f"{who} sees none of its edges", got, [])
    check(f"…nor its item or when", sorted(set(G.visible_nodes(viewer, role, path)) & {ask, reply, dentist}), [])
raw = sqlite3.connect(path)
prov = sorted({(r[0], r[1]) for r in raw.execute(
    "SELECT p.owner, p.audience FROM edge_prov p JOIN edges e ON e.id=p.edge_id WHERE e.a IN (?,?,?) AND p.source='hooks'",
    (ask, reply, dentist))})
check("every hook source is Alex's, private", prov, [("p_alex", "private")])

print("── nothing raw in the graph or the hooks ──")
blob = "\n".join(str(tuple(r)) for t in ("nodes", "edges", "edge_prov") for r in raw.execute(f"SELECT * FROM {t}"))
lnk = sqlite3.connect(L.TMP / "links.db")
blob += "\n".join(str(tuple(r)) for r in lnk.execute("SELECT * FROM hooks WHERE item_id IN (?,?,?)", (ask, reply, dentist)))
check("no phone number, device id or text word stored", [v for v in ("5550100001", "555 010", "0100003", "guid-",
                                                                     "Saturday", "see you") if v in blob], [])
check("the place is kept as its registry id, never its words",
      sorted(r[0] for r in lnk.execute("SELECT key FROM hooks WHERE kind='mention:place'")), ["pl-larkspur", "pl-larkspur", "pl-pine-park", "pl-summit"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
