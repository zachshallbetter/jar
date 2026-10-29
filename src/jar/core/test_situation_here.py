#!/usr/bin/env python3
# gate: hermetic   (the golden household plus a few rows of its own, in temp stores; no model, no network)
"""#1396: situation() answers "who did we come with" from where the viewer is, and one owner's items that name the same
place on the same household day link (graph_edges.same_place_day_edges).

The world is the golden household (graph_golden/household.json) with rows added through the same loader
(graph_golden_load, GRAPH_GOLDEN_FIXTURE): a Larkspur Hall plan (a proposal whose participants are the family), a second
owner's own texts and mail naming the same place, a mixed-audience pair, a pair someone judged unrelated, a hub of eight
items, and a day the household zone and UTC read differently. Checked per viewer, and for every edge state."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
H = json.loads((HERE / "graph_golden" / "household.json").read_text())
W = copy.deepcopy(H)
os.environ["TZ"] = "UTC"            # the host's zone: the household day must never be read from it
os.environ["AGENT_TZ"] = "America/Los_Angeles"


def text(iid, owner, at, audience="private"):
    return {"id": iid, "source": "texts", "kind": "text", "title": "Text", "observed_at": at, "owner": owner,
            "audience": audience, "fields": {}, "body": "-"}


def mail(iid, owner, at):
    return {"id": iid, "source": "mail", "kind": "mail", "title": "Note", "observed_at": at, "owner": owner,
            "audience": "private", "fields": {"from_addr": "someone@example.org"}, "body": "-"}


def place_hook(iid):
    return {"item": iid, "kind": "mention:place", "raw": "pl-larkspur"}


D = "2026-10-10T"
new = [
    text("rb-1", "p_robin", D + "10:00:00-07:00"), text("rb-2", "p_robin", D + "11:00:00-07:00"),
    mail("rb-mail-late", "p_robin", "2026-10-11T03:30:00+00:00"),     # 10-10 20:30 in the household, 10-11 in UTC
    mail("rb-mail-early", "p_robin", "2026-10-10T06:00:00+00:00"),    # 10-09 23:00 in the household, 10-10 in UTC
    text("rb-adults", "p_robin", "2026-10-07T12:00:00-07:00", "adults"), text("rb-house", "p_robin", "2026-10-07T12:30:00-07:00", "household"),
    text("rb-no-1", "p_robin", "2026-10-06T13:00:00-07:00"), text("rb-no-2", "p_robin", "2026-10-06T13:30:00-07:00"),
    text("jd-1", "p_jordan", D + "15:00:00-07:00"),
]
hub = [text(f"hub-{i}", "p_alex", "2026-10-05T09:%02d:00-07:00" % i) for i in range(9)]      # nine on one day: a hub
W["items"] += new + hub
W["hooks"] += [place_hook(i["id"]) for i in new + hub]
W["hooks"] += [{"item": "jd-1", "kind": "party:phone", "raw": "+15550100001", "dir": "from"},
               {"item": "dev-text-hale-1", "kind": "date:mention", "raw": "2026-10-12", "time": "16:00"}]   # the invite names a time
W["link_statements"] += [{"a": "rb-no-1", "b": "rb-no-2", "related": False, "by": "p_robin", "source": "chat"}]
W["places"] = [dict(p, audience="household") if p["id"] == "pl-larkspur" else p for p in W["places"]]
W["place_events"] += [
    {"id": "pe-jordan-1", "person": "p_jordan", "event": "arrived", "place": "Larkspur Hall", "at": D + "15:56:00-07:00", "device": "dev-j"},
    {"id": "pe-robin-1", "person": "p_robin", "event": "arrived", "place": "Larkspur Hall", "at": D + "15:55:00-07:00", "device": "dev-r"},
    {"id": "pe-robin-2", "person": "p_robin", "event": "left", "place": "Larkspur Hall", "at": D + "15:59:00-07:00", "device": "dev-r"}]
# the plan: a proposal for the afternoon whose participants are the family (the Mac's event proposal, jellybean#1189)
W["event_proposals"] = H["event_proposals"] + [{
    "owner": "p_alex", "sig_id": "b1c2d3e4f5a60789", "thread_ref": "1a2b3c4d5e6f7089", "title": "Larkspur Hall",
    "start": D + "16:00:00-07:00", "end": None, "place": "Larkspur Hall", "participants": ["p_alex", "p_robin", "p_ava", "p_leo"],
    "others": 1, "with_name": "Robin", "confidence": 0.86, "state": "new"}]
fix = Path(tempfile.mkdtemp(prefix="titan-here-fixture-")) / "world.json"
fix.write_text(json.dumps(W))
os.environ["GRAPH_GOLDEN_FIXTURE"] = str(fix)
sys.path.insert(0, str(HERE))
import graph_golden_load as GL  # noqa: E402  (first: points every store at a temp dir and loads the fixture)
import graph_edges as GE  # noqa: E402
import graph_sync as G  # noqa: E402
import fe_proactive  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402
from situation import situation  # noqa: E402

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"\n       got  {got!r}\n       want {want!r}"))


db = GL.build()
ROLE = {"p_alex": "admin", "p_robin": "adult", "p_jordan": "adult", "p_ava": "kid", "p_leo": "kid"}
ALL = ("rejected", "verified", "accepted", "candidate")


class Graph:
    zone, now = GL.H["zone"], GL.H["now"]

    def visible_nodes(self, viewer, role, path=None):
        return G.visible_nodes(viewer, role, db)

    def visible_edges(self, viewer, role, path=None, **kw):
        return G.visible_edges(viewer, role, db, **kw)

    def same_as(self, viewer, role, node_id=None, path=None):
        return []


g = Graph()


def derived(viewer, states=ALL):
    """The same-place-same-day edges this viewer can see, as 'a b state'."""
    return sorted(f"{e['a']} {e['b']} {e['state']}" for e in G.visible_edges(viewer, ROLE[viewer], db, states=states)
                  if e["type"] == "related" and e["why"].startswith("both name "))


print("── the edge: one owner, one place, one household day ──")
check("Alex: his two Hale texts, and the one pair of Robin's he can see in full (adults + household); not Robin's "
      "private texts, and not his hub of nine",
      derived("p_alex"), ["dev-text-hale-1 dev-text-hale-2 accepted", "rb-adults rb-house accepted"])
check("Robin: her morning texts, and the mail that is 10-10 in the household (not in UTC); not the 10-09 one, not the "
      "judged pair, and nothing of Alex's",
      derived("p_robin"),
      ["rb-1 rb-2 accepted", "rb-1 rb-mail-late accepted", "rb-2 rb-mail-late accepted", "rb-adults rb-house accepted"])
check("Jordan (an adult): only the adults+household pair he can see in full; his own text links to nothing",
      derived("p_jordan"), ["rb-adults rb-house accepted"])
check("a kid sees none of it", (derived("p_ava"), derived("p_leo")), ([], []))
check("the pair Robin said are unrelated stays rejected, never accepted",
      [f"{e['a']} {e['b']} {e['state']}" for e in G.visible_edges("p_robin", "adult", db, states=ALL)
       if {e["a"], e["b"]} == {"rb-no-1", "rb-no-2"}], ["rb-no-1 rb-no-2 rejected"])
every = G.visible_edges(None, "household", db, states=ALL)
check("the screen sees none", [e for e in every if e["type"] == "related" and e["why"].startswith("both name ")], [])

n1_nodes = G.build(records=GL.bill_records, today=datetime.fromisoformat(GL.H["now"]).date())[0]
print("── owner and audience are the narrowest of the sources ──")
con = G._ro_graph(db)
rows = {(r["a"], r["b"]): (r["owner"], r["audience"], r["plane"], r["state"]) for r in con.execute(
    "SELECT * FROM edges WHERE type='related' AND why LIKE 'both name %'")}
con.close()
check("a private pair: the owner's, private, measured", rows[("dev-text-hale-1", "dev-text-hale-2")],
      ("p_alex", "private", "measured", "accepted"))
check("an adults text with a household text: adults, never household", rows[("rb-adults", "rb-house")][:3],
      ("p_robin", "adults", "measured"))
owner_of = {n["id"]: n["owner"] for n in n1_nodes}
check("no edge crosses owners", [k for k in rows if owner_of[k[0]] != owner_of[k[1]]], [])

print("── every edge state: a candidate or rejected mention never links ──")
n1, x1, e1, f1 = G.build(records=GL.bill_records, today=datetime.fromisoformat(GL.H["now"]).date())
ctx = GE.Ctx(n1, x1, {}, [], [])


def mention(item, state, kind_src="hooks"):
    return GE.Edge(item, "mentions", "pl-larkspur", state=state, rung="observed", conf=0.9, why="names Larkspur Hall",
                   src=[GE.prov("hooks", f"{item}:mention:place", "p_robin", "private", item=item)])


def link(states):
    es = [mention("rb-1", states[0]), mention("rb-2", states[1])]
    return [(e["a"], e["b"], e["state"], e["plane"]) for e in GE.same_place_day_edges(ctx, None, es)]


for s1 in ("accepted", "verified", "candidate"):
    for s2 in ("accepted", "verified", "candidate"):
        want = [("rb-1", "rb-2", "accepted", "measured")] if "candidate" not in (s1, s2) else []
        check(f"mentions {s1} + {s2}", link((s1, s2)), want)
check("an existing link in any state (a rejected one) is kept, not overridden",
      [(e["a"], e["b"]) for e in GE.same_place_day_edges(
          ctx, None, [mention("rb-1", "accepted"), mention("rb-2", "accepted"),
                      GE.Edge("rb-1", "related", "rb-2", plane="measured", state="rejected", rung="observed", conf=0,
                              why="no", src=[GE.prov("link_statements", "rb-1|rb-2", "p_robin", "private")])])], [])
check("different owners never link, whatever the states",
      [(e["a"], e["b"]) for e in GE.same_place_day_edges(ctx, None, [mention("rb-1", "verified"), mention("jd-1", "verified")])], [])

print("── situation(): 'who did we come with' from where you are ──")
alex = situation("Who did we come with?", "p_alex", "admin", g, zone=g.zone, now=GL.H["now"])
check("Alex at Larkspur Hall, the plan names the family: Robin, Ava and Leo, not Alex, not the host",
      (sorted(alex["who"]["is"]), alex["where"]["is"], alex["what"]["is"]),
      (["p_ava", "p_leo", "p_robin"], ["pl-larkspur"], ["invited by Hale Dorsey"]))
check("…said with its chain: the arrival, the plan's attends edges, the invite", {"pe-alex-2"} <= set(alex["via"]) and
      "dev-text-hale-1" in alex["via"], True)
check("'who's here with us?' is the same question", situation("Who's here with us?", "p_alex", "admin", g, zone=g.zone,
                                                              now=GL.H["now"])["who"], alex["who"])
named = situation("Who did we come with to Larkspur Hall?", "p_alex", "admin", g, zone=g.zone, now=GL.H["now"])
check("a place named in the question keeps the existing reading (no 'here' anchor)", "invited by Hale Dorsey" in
      json.dumps(named.get("what")), False)
late = situation("Who did we come with?", "p_alex", "admin", g, zone=g.zone, now="2026-10-10T22:30:00-07:00")
check("hours later he is no longer 'here': unknown, never a guess", late["who"], "unknown")
nowhere = situation("Who did we come with?", "p_leo", "kid", g, zone=g.zone, now=GL.H["now"])
check("a viewer with no arrival gets unknown", nowhere["who"], "unknown")
left = situation("Who did we come with?", "p_robin", "adult", g, zone=g.zone, now=GL.H["now"])
check("Robin arrived and left: not here, unknown", left["who"], "unknown")
jordan = situation("Who's here with us?", "p_jordan", "adult", g, zone=g.zone, now=GL.H["now"])
check("Jordan, at the Hall with a text from Robin: Robin only, from what he can see",
      (jordan["who"]["is"], jordan["where"]["is"]), (["p_robin"], ["pl-larkspur"]))
blob = json.dumps(jordan).lower()
check("…and nothing of Alex's plan, texts, host or kids reaches him",
      [x for x in ("p_ava", "p_leo", "hale", "dev-text-hale", "b1c2d3e4f5a60789", "event:ep-", "p_alex") if x in blob], [])
for who, role in (("p_robin", "adult"), ("p_ava", "kid"), ("p_leo", "kid")):
    b = situation("Who did we come with?", who, role, g, zone=g.zone, now=GL.H["now"])
    check(f"{who}: no id of Alex's private rows", [x for x in ("hale", "dev-text-hale", "b1c2d3e4f5a60789", "pe-alex") if x
                                                  in json.dumps(b).lower()], [])

print("── fe_proactive: the invite and the family thread cluster, so the card names the family ──")
plans = fe_proactive.detect("p_alex", "admin", g, zone=ZoneInfo(g.zone), now=datetime.fromisoformat(GL.H["now"]))
check("one plan from the invite and the family thread (Robin's reply joined by the thread and the inline reply, #1650), "
      "the card naming the family (Robin, and Ava and Leo through the texts' mention:person hooks, #1396) as well as "
      "the host (the original failure named only Alex)",
      [(p["items"], p["body"]["participants"], p["body"]["others"]) for p in plans],
      [(["dev-text-hale-1", "dev-text-hale-2", "dev-text-hale-3"], ["p_alex", "p_ava", "p_leo", "p_robin"], 1)])

print(f"\n{'FAIL' if fails else 'ok'}: {fails} failed")
sys.exit(1 if fails else 0)
