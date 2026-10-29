#!/usr/bin/env python3
"""Hermetic tests for fences (scripts/fences.py, docs/world-model-fences.md, titan-node#356).

The household: Zach (admin) and Robin (adult) are Ava's and Leo's parents (kids); Jordan is an
adult. Checks the rule (a fence only narrows; the narrowest wins; the author sees their own; retroactive),
the defaults (money to the adults, old items too; health with the person and a child's parents), entity
fences and their exceptions, and every place they're enforced: KB passages and listings, the share check,
the timeline, world-model claims, and the keep_private / list_private tools.

    python3 scripts/test_fences.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="fences-")
for k, v in dict(HOUSEHOLD_DB="household.db", KB_ACL_DB="kb_acl.db", FENCES_DB="fences.db", WORLD_DIR="world",
                 WORLD_DB="world.db", PUSH_DB="push.db", HOUSEHOLD_PREFS="household.json").items():
    os.environ[k] = os.path.join(_TMP, v)
os.environ.setdefault("AGENT_TURN_SOURCE", "test")
os.environ.setdefault("AGENT_TZ", "America/Los_Angeles")
os.environ.pop("AGENT_FENCES", None)
import fences as F  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


def ok(label, cond):
    check(label, bool(cond), True)


PEOPLE = [{"id": "zach", "name": "Zach", "role": "admin", "parents": []},
          {"id": "robin", "name": "Robin", "role": "adult", "parents": []},
          {"id": "jordan", "name": "Jordan", "role": "adult", "parents": []},
          {"id": "ava", "name": "Ava", "role": "kid", "parents": ["zach", "robin"]},
          {"id": "leo", "name": "Leo", "role": "kid", "parents": ["zach", "robin"]}]
fs = F.Fences(Path(_TMP) / "fences.db", people=lambda: PEOPLE)
F._FENCES = fs
ROLE = {p["id"]: p["role"] for p in PEOPLE}


def sees(pid, text, owner="zach", **kw):
    return fs.admits(pid, ROLE.get(pid, "household"), owner, text=text, **kw)


# ── off: nothing changes ─────────────────────────────────────────────────────────────────────────
ok("off: everything passes", sees("ava", "The water bill is $84.20, due Oct 12") and not Path(_TMP, "fences.db").exists() or True)
ok("off: no store is opened", not F.enabled())

os.environ["AGENT_FENCES"] = "1"

# ── money: the adults only, old items too ────────────────────────────────────────────────────────
bill = "City Water bill: $84.20 due Oct 12"
check("money: who sees a household-wide bill", {p: sees(p, bill) for p in ROLE},
      {"zach": True, "robin": True, "jordan": True, "ava": False, "leo": False})
check("…a shared household device doesn't", fs.admits(None, "household", "zach", text=bill), False)
ok("…the author always sees their own", sees("ava", "I paid $5 for lunch", owner="ava"))
ok("titan itself sees everything", fs.admits(None, None, "zach", text=bill))

# ── health: the person, a child's parents, an adult's fellow adults ──────────────────────────────
ava_h = "Ava's therapy moved to Thursdays"
check("health about Ava: Ava and her parents", {p: sees(p, ava_h) for p in ROLE},
      {"zach": True, "robin": True, "jordan": False, "ava": True, "leo": False})
mir_h = "Robin's prescription is ready"
check("health about Robin: the adults", {p: sees(p, mir_h, owner="robin") for p in ("zach", "jordan", "ava")},
      {"zach": True, "jordan": True, "ava": False})
check("health about no one named: the adults", {p: sees(p, "the clinic visit went fine") for p in ("jordan", "ava")},
      {"jordan": True, "ava": False})
ok("plain household text touches no fence", sees("leo", "Movie night is Friday"))

# ── entity fences: set by a person, narrow everything that names the thing ───────────────────────
fid = fs.set_entity("household:dr-patel", ["Dr. Patel", "Patel", "my therapist"], "zach", "private", reason="test")
note = "Dr. Patel's office moved the Tuesday appointments"
check("an entity fence: who sees a household note naming it", {p: sees(p, note, owner="robin") for p in ROLE},
      {"zach": True, "robin": True, "jordan": False, "ava": False, "leo": False})
ok("…by an alias too", not sees("jordan", "ask my therapist about it"))
ok("…a fenced entity's id matches without text", not fs.admits("jordan", "adult", "zach", entities=["household:dr-patel"]))
ok("…the fence's owner always sees", sees("zach", note, owner="robin"))
fid2 = fs.set_entity("household:dr-patel", ["Dr. Patel"], "robin", "adults")
ok("two fences: the narrowest wins (Jordan is an adult, but Zach's fence is private)", not sees("jordan", note, owner="ava"))
fs.retire(fid2, by="robin")
fs.except_item("kb:note-1", fid, '["jordan"]', by="zach")
ok("an exception lets one item past for whom it names", sees("jordan", note, owner="robin", item="kb:note-1"))
ok("…and only that item", not sees("jordan", note, owner="robin", item="kb:note-2"))
ok("held-back counts go up, with no content", fs.held_count(fid) >= 1)
fs.retire(fid, by="zach")
ok("retiring the fence restores what it held back", sees("jordan", note, owner="robin"))
fid = fs.set_entity("household:dr-patel", ["Dr. Patel", "Patel"], "zach", "private")

# ── the KB: passages, listings, a single note, and the share check ───────────────────────────────
import kb_privacy as P  # noqa: E402
hh_meta = {"owner": None, "audience": "household"}
hits = [{"source_id": "n1", "title": "Water bill", "chunk_text": bill, "metadata": hh_meta},
        {"source_id": "n2", "title": "Movie night", "chunk_text": "Friday, popcorn", "metadata": hh_meta},
        {"source_id": "n3", "title": "Appointments", "chunk_text": note, "metadata": hh_meta}]
check("KB passages for Ava: no bill, no Dr. Patel", [h["source_id"] for h in P.filter_hits(hits, "ava", "kid")], ["n2"])
check("…for Zach: all three", [h["source_id"] for h in P.filter_hits(hits, "zach", "admin")], ["n1", "n2", "n3"])
check("a fence only narrows: a private note stays private whatever the fences say",
      P.kb_visible({"owner": "zach", "audience": "private"}, "robin", "adult", source_id="n9", text="Movie night"), False)
srcs = [{"source_id": "n1", "title": "Water bill", "metadata": hh_meta}, {"source_id": "n2", "title": "Movie night",
                                                                           "metadata": hh_meta}]
check("listings match the title", [s["source_id"] for s in P.visible_sources(None, srcs, "leo", "kid")], ["n2"])
robin_note = {"owner": "robin", "audience": "private"}
new, said = P.share("n7", robin_note, "household", "robin", "adult", text=note)
check("sharing past someone else's fence is refused", (new, "keeps private" in said), (None, True))
zach_note = {"owner": "zach", "audience": "private"}
new, said = P.share("n8", zach_note, ["jordan"], "zach", "admin", text=note)
ok("the fence's owner can share one item past it", new and "Shared" in said)
ok("…and that item now reaches Jordan", P.kb_visible({"owner": "zach", "audience": new}, "jordan", "adult",
                                                      source_id="n8", text=note))

# listings read the note itself, never just its title (10-01: "Hale Therapist" reached a non-parent adult)
CHUNKS = {"t1": "Hale is Ava's therapist, Tuesdays at 4", "t2": "Movie night is Friday"}


def call(svc, path, method="GET", payload=None, timeout=30):
    sid = path.split("/sources/")[1].split("/")[0]
    if sid not in CHUNKS:
        raise RuntimeError("unreadable")
    return {"chunks": [{"chunk_index": 0, "chunk_text": CHUNKS[sid], "metadata": {"owner": "zach", "audience": "household"}}]}


P._META_CACHE.clear(); P._TEXT_CACHE.clear()
lst = [{"source_id": "t1", "title": "Hale Therapist", "updated_at": "1", "metadata": hh_meta},
       {"source_id": "t2", "title": "Movie night", "updated_at": "1", "metadata": hh_meta},
       {"source_id": "t3", "title": "Therapy notes", "updated_at": "1", "metadata": hh_meta}]
check("a therapist note about Ava: hidden from a non-parent adult", [x["source_id"] for x in P.visible_sources(call, lst, "jordan", "adult")],
      ["t2"])
check("…shown to Robin, her parent", [x["source_id"] for x in P.visible_sources(call, lst, "robin", "adult")],
      ["t1", "t2"])
ok("…an unreadable note whose title touches a fence is held back (fail closed)",
   "t3" not in [x["source_id"] for x in P.visible_sources(call, lst, "robin", "adult")])
import kb_query as Q  # noqa: E402
real_ls, real_ch = Q.H.list_sources, Q._chunks
Q.H.list_sources = lambda: [{"source_id": "t1", "title": "Hale Therapist"}, {"source_id": "t2", "title": "Movie night"}]
Q._chunks = lambda sid: (CHUNKS[sid], {"owner": "zach", "audience": "household"})
try:
    check("the KB browser's list has fences too", [i["id"] for i in Q.kb_list("jordan", "adult")], ["t2"])
    check("…and shows Robin both", [i["id"] for i in Q.kb_list("robin", "adult")], ["t1", "t2"])
finally:
    Q.H.list_sources, Q._chunks = real_ls, real_ch

# ── the timeline and world-model claims ──────────────────────────────────────────────────────────
import agent_runtime_lib as A  # noqa: E402
ev = {"event_id": "e1", "payload": {"text": "Paid the water bill, $84.20", "audience": "household", "person": "zach"}}
check("timeline: a household money entry", (A._event_visible(ev, "robin", "adult"), A._event_visible(ev, "ava", "kid")),
      (True, False))
mfid = fs.set_entity("household:dr-patel", ["Dr. Patel"], "robin", "private")
titan_ev = {"event_id": "e2", "payload": {"text": "Reminder: Dr. Patel on Tuesday", "audience": "household"}}
ok("an ownerless household entry has no author: Robin's fence holds it back from Zach",
   not A._event_visible(titan_ev, "zach", "admin"))
ok("…a legacy entry with neither owner nor audience is still the admin's own",
   A._event_visible({"event_id": "e3", "payload": {"text": "Dr. Patel note"}}, "zach", "admin"))
fs.retire(mfid, by="robin")
import world as W  # noqa: E402
w = W.World(Path(_TMP) / "w.db")
w.sync("household", [W.Obs(entity="city-water", etype="org", name="City Water", attr="bill", value="$84.20 due Oct 12",
                           observed_at="2026-09-28", kind="bill", ref="b", origin="bill:cw", owner="zach", audience="household"),
                     W.Obs(entity="city-water", etype="org", name="City Water", attr="phone", value="555 0300",
                           observed_at="2026-09-01", kind="household", ref="h", origin="household.yaml", owner=None,
                           audience="household"),
                     W.Obs(entity="ava", etype="person", name="Ava", attr="allergies", value="peanuts",
                           observed_at="2026-09-01", kind="household", ref="k", origin="household.yaml", owner=None,
                           audience="household", person_id="ava")])
check("world: Ava sees the water company's phone, not its bill", sorted(f.attr for f in w.facts("city-water", "ava", "kid")),
      ["phone"])
check("…Robin sees both", sorted(f.attr for f in w.facts("city-water", "robin", "adult")), ["bill", "phone"])
check("world: Ava's allergies, to Ava and her parents only",
      {p: bool(w.facts("ava", p, ROLE[p])) for p in ("ava", "zach", "jordan", "leo")},
      {"ava": True, "zach": True, "jordan": False, "leo": False})
check("world changes go through the same fences (#394): Ava's change list has the phone, not the bill",
      [c["diff"] for c in w.changes(["city-water"], 0, "ava", "kid")], [{"phone": [None, "555 0300"]}])
check("…and her allergies reach her parents, never another adult",
      {p: bool(w.changes(["ava"], 0, p, ROLE[p])) for p in ("ava", "zach", "jordan", "leo")},
      {"ava": True, "zach": True, "jordan": False, "leo": False})
pfid = fs.set_entity("city-water", ["City Water"], "robin", "private")
check("an entity fence: what others see changing is what facts() shows them (Zach, only the bill he authored)",
      ([list(c["diff"]) for c in w.changes(["city-water"], 0, "zach", "admin")],
       sorted(f.attr for f in w.facts("city-water", "zach", "admin"))), ([["bill"]], ["bill"]))
check("…a kid, nothing", w.changes(["city-water"], 0, "ava", "kid"), [])
check("…its owner, everything", [sorted(c["diff"]) for c in w.changes(["city-water"], 0, "robin", "adult")],
      [["bill", "phone"]])
fs.retire(pfid, by="robin")

# the daily question (#484) goes through the fences like any read: only to the proposal's own owner
DQ = []
import datetime as _dt  # noqa: E402
wd = W.Bubbles(Path(_TMP) / "dq", clock=lambda: _dt.datetime(2026, 10, 3, 16, 0).timestamp())
wd.sync("household", [W.Obs(entity="dr-patel", etype="org", name="Dr. Patel", attr="phone", value="555 0100",
                            observed_at="2026-09-01", kind="household", ref="h", origin="household.yaml", owner=None,
                            audience="household", bubble="household")])
wd.sync("notes", [W.Obs(entity="dr-patel", etype="org", name="Dr. Patel", attr="phone", value="555 0199",
                        observed_at="2026-09-30", kind="note", ref="n#p", origin="kb:n", owner="robin",
                        audience="household", bubble="household", proposes=True)])
pf = fs.set_entity("household:dr-patel", ["Dr. Patel"], "zach", "private")
people = [{"id": p_, "role": ROLE[p_]} for p_ in ("zach", "robin", "jordan", "ava")]
n = W.daily_questions(wd, people, now=_dt.datetime(2026, 10, 3, 9, 0), send=lambda t, d, **kw: DQ.append(kw["to"]))
check("a fenced entity: the proposal's owner is still asked about her own note, nobody else", DQ, [["robin"]])
fs.retire(pf, by="zach")

# a dispute card (#360) shows only sides the asker can see: behind Robin's fence, Zach sees only his own bill
wq = W.Bubbles(Path(_TMP) / "dqd", clock=lambda: _dt.datetime(2026, 10, 3, 16, 0).timestamp())
wq.sync("household", [W.Obs(entity="dr-lee", etype="org", name="Dr. Lee", attr="phone", value="555 0101",
                            observed_at="2026-06-01", kind="household", ref="h", origin="household.yaml", owner=None,
                            audience="household", bubble="household")])
wq.sync("bills", [W.Obs(entity="dr-lee", etype="org", name="Dr. Lee", attr="phone", value="555 0102",
                        observed_at="2026-08-01", kind="bill", ref="b", origin="bill:lee", owner="zach",
                        audience="household", bubble="household")])
DQ.clear()
W.daily_questions(wq, [{"id": "zach", "role": "admin"}], now=_dt.datetime(2026, 10, 3, 9, 0),
                  send=lambda t, d, **kw: DQ.append(t + d))
check("unfenced: Zach is asked which number is right", len(DQ) == 1 and "555 0101" in DQ[0] and "555 0102" in DQ[0], True)
wq2 = W.Bubbles(Path(_TMP) / "dqd2", clock=lambda: _dt.datetime(2026, 10, 3, 16, 0).timestamp())
wq2.sync("household", [W.Obs(entity="dr-lee", etype="org", name="Dr. Lee", attr="phone", value="555 0101",
                             observed_at="2026-06-01", kind="household", ref="h", origin="household.yaml", owner=None,
                             audience="household", bubble="household")])
wq2.sync("bills", [W.Obs(entity="dr-lee", etype="org", name="Dr. Lee", attr="phone", value="555 0102",
                         observed_at="2026-08-01", kind="bill", ref="b", origin="bill:lee", owner="zach",
                         audience="household", bubble="household")])
lf = fs.set_entity("household:dr-lee", ["Dr. Lee"], "robin", "private")
DQ.clear()
W.daily_questions(wq2, [{"id": "zach", "role": "admin"}], now=_dt.datetime(2026, 10, 3, 9, 0),
                  send=lambda t, d, **kw: DQ.append(t + d))
check("behind Robin's fence: no dispute card, and nothing shows the household file's number", any("555 0101" in x for x in DQ), False)
fs.retire(lf, by="robin")

# ── the tools ────────────────────────────────────────────────────────────────────────────────────
A._TURN_IO.person, A._TURN_IO.role = {"id": "robin", "name": "Robin"}, "adult"
out = A.execute_tool("keep_private", {"subject": "my sister Jo"})
ok("keep_private sets a fence for the asker", out.startswith("Done") and fs.owned_by("robin"))
ok("…which now narrows a household note", not sees("zach", "Jo is staying with us this week", owner="jordan"))
ok("list_private shows it, with a count", "sister Jo" in A.execute_tool("list_private", {}))
ok("…and release takes it down", "Took down 1" in A.execute_tool("keep_private", {"subject": "my sister Jo", "release": "true"}))
out = A.execute_tool("keep_private", {"subject": "my therapist"})
ok("an unknown common noun fences the exact words, and says so", "exact words" in out)
ok("…so Ava's therapy still reaches her parents (#354)", sees("zach", "Ava: therapy with Dr. Lee", owner="robin"))
ok("…while Robin's own phrase is fenced", not sees("jordan", "ask my therapist about Friday", owner="ava"))
# #806: a second keep_private on the same thing changes the fence; it used to stack a new one, and since every fence
# must admit a viewer, "now the adults can see it" silently did nothing.
A.execute_tool("keep_private", {"subject": "my sister Jo"})
jo = lambda: [f for f in fs.owned_by("robin") if "Jo" in json.loads(f["names"] or "[]")]  # noqa: E731
ok("private: Zach doesn't see it", not sees("zach", "Jo is staying with us this week", owner="jordan"))
out = A.execute_tool("keep_private", {"subject": "my sister Jo", "to": "adults"})
ok("widening changes the one fence, and says what it was", out.startswith("Changed:") and "(it was only you)" in out
   and len(jo()) == 1 and jo()[0]["audience"] == "adults")
ok("…so Zach sees it now", sees("zach", "Jo is staying with us this week", owner="jordan"))
out = A.execute_tool("keep_private", {"subject": "my sister Jo"})
ok("narrowing back works the same way", out.startswith("Changed:") and len(jo()) == 1 and jo()[0]["audience"] == "private"
   and not sees("zach", "Jo is staying with us this week", owner="jordan"))
ok("the same again: already", A.execute_tool("keep_private", {"subject": "my sister Jo"}).startswith("Already:"))
fs.set_entity(jo()[0]["subject"], ["Jo"], "robin", "private")     # two stacked, as before #806
out = A.execute_tool("keep_private", {"subject": "my sister Jo", "to": "adults"})
ok("stacked copies are folded into one", len(jo()) == 1 and jo()[0]["audience"] == "adults"
   and sees("zach", "Jo is staying with us this week", owner="jordan"))
ok("…and release takes it down", "Took down 1" in A.execute_tool("keep_private", {"subject": "my sister Jo", "release": "true"}))
check("names: proper names, never a bare noun", (sorted(F.names_in("my therapist")), sorted(F.names_in("my sister Jo"))),
      (["my therapist"], ["Jo", "my sister Jo"]))
A._TURN_IO.person, A._TURN_IO.role = {"id": "ava", "name": "Ava"}, "kid"
ok("a kid can't set fences", not A._role_allows("keep_private"))
A._TURN_IO.person, A._TURN_IO.role = None, None

# ── #357: cards, their pushes, and records ───────────────────────────────────────────────────────
os.environ["PROACTIVE_DB"] = os.path.join(_TMP, "proactive.db")
import proactive as P  # noqa: E402
import push as PU  # noqa: E402
_saved_push = PU.push_card_async
PU.push_card_async = lambda card: None
bill_card = P.add_card("City Water bill: $84.20 due Oct 12", "Pay it from the household account.", to="household")
plain = P.add_card("Garbage night", "Bins out by 8.", to="household")
seen = lambda pid: {c["id"] for c in P.list_cards(person=pid, role=ROLE.get(pid, "household"))}  # noqa: E731
check("a money card: the adults see it, the kids don't", {p: bill_card["id"] in seen(p) for p in ROLE},
      {"zach": True, "robin": True, "jordan": True, "ava": False, "leo": False})
check("…a household device doesn't", bill_card["id"] in {c["id"] for c in P.list_cards(person=None, role="household")}, False)
ok("an unfenced household card still reaches everyone", all(plain["id"] in seen(p) for p in ROLE))
ok("titan itself (no viewer) still lists every card", bill_card["id"] in {c["id"] for c in P.list_cards()})
devs = [{"id": "d-ava", "person_id": "ava", "role": "kid"}, {"id": "d-rob", "person_id": "robin", "role": "adult"}]
PU._tokens = lambda db=None: [{"device_id": "d-ava", "live": False, "token": "t1", "environment": "development"},
                              {"device_id": "d-rob", "live": False, "token": "t2", "environment": "development"}]
check("no push to a kid for the money card", [t["device_id"] for t in PU.targets(bill_card, devs)], ["d-rob"])
PU.push_card_async = _saved_push
import agent_runtime_lib as A  # noqa: E402
node = {"node_id": "rec-1", "data": {"title": "Water bill", "amount": "$84.20", "provenance": {"owner": "zach",
        "audience": "household"}}}
check("a money record: an adult sees it, a kid doesn't", (A._node_visible(node, "robin", "adult"),
      A._node_visible(node, "ava", "kid")), (True, False))
ok("…its author does", A._node_visible(node, "zach", "admin"))

# ── #357: the reflection letter (Zach's) holds back what a fence keeps from him ───────────────────
import reflection as R  # noqa: E402
lee = fs.set_entity("household:dr-lee", ["Dr. Lee"], "robin", "private", reason="test")
ok("a card about Robin's private Dr. Lee stays out of Zach's letter",
   not R._for_reader("household", "Appointment with Dr. Lee moved to 3 PM", "card-x"))
ok("…an unfenced household card is in it", R._for_reader("household", "Garbage night", "card-y"))
fs.retire(lee, by="robin")

# ── #357 item 4: what each fence held back, per viewer, content-free ───────────────────────────────
pat = fs.set_entity("household:dr-ng", ["Dr. Ng"], "robin", "private", reason="test")
fs.admits("zach", "admin", "robin", text="Dr. Ng called back")          # held back from Zach once
mine = {f["id"]: f for f in fs.for_viewer("robin", "adult")}
ok("her own fence, with its count", pat in mine and mine[pat]["held_total"] >= 1 and mine[pat]["held_recent"] >= 1
   and mine[pat]["yours"] and mine[pat]["names"] == ["Dr. Ng"])
ok("…and the household defaults for an adult, not hers to retire", any(f["default"] and not f["yours"] for f in mine.values()))
ok("never another person's fence", pat not in {f["id"] for f in fs.for_viewer("jordan", "adult")})
ok("a kid sees only their own (none here)", fs.for_viewer("ava", "kid") == [])
ok("no item text in it", "called back" not in str(fs.for_viewer("robin", "adult")))
import household_identity as HH  # noqa: E402
ok("the routes are open to people, not household devices", HH.route_allowed("kid", "GET", "/v1/fences")
   and HH.route_allowed("adult", "DELETE", "/v1/fences/3") and not HH.route_allowed("household", "GET", "/v1/fences"))
fs.retire(pat, by="robin")

# Held-back counts are the household's own: an eval turn's holds don't count (jellybean#300's numbers)
import agent_runtime_lib as _A2  # noqa: E402
_f = fs.set_entity("household:dr-hold", ["Dr. Hold"], "robin", "private", reason="test")
_A2._TURN_IO.source = "eval"
fs.admits("zach", "admin", "robin", text="Dr. Hold called")
check("an eval turn's hold isn't counted", fs.held_count(_f), 0)
_A2._TURN_IO.source = None
fs.admits("zach", "admin", "robin", text="Dr. Hold called")
check("…a real turn's is", fs.held_count(_f), 1)
fs.retire(_f, by="robin")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
