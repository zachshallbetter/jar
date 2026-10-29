#!/usr/bin/env python3
# gate: hermetic
"""The cards deck as a ResolvedSurface (#1060 migration step 2: `attention`, GET /v1/proactive?as=surface).
S1: a fixed fictional deck, viewer, `now` and household day resolve to exactly the golden vectors in
scripts/evalsets/surfaces/cards.*.json (`--write` rewrites them; review the diff). Rules: groups and order, priority,
each verb's exposure from its route's own rules, nothing done before today, no route invented. S2 through the real
route: never-see lists per viewer, and the profile's negatives 1, 2, 7 and 9 (docs/surfaces.md §8). Fictional people
(Alex the admin, Robin an adult, Ava a kid, a living-room screen); temporary stores; no model.
Run: python3 scripts/test_surfaces_cards.py [--write]"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-cards-surface-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "WORLD_DIR": "world", "SURFACES_DB": "surfaces.db",
             "ITEM_REFS_DB": "item_refs.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "SPACES_DB": "spaces.db", "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt",
             "SCHEDULED_DB": "scheduled.db", "TASKS_DB": "tasks.db", "PUSH_DB": "push.db", "FENCES_DB": "fences.db",
             "PLACES_DB": "places.db", "SITTER_DB": "sitter.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TZ"] = "America/Los_Angeles"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE"):
    os.environ.pop(k, None)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
VECTORS = HERE / "evalsets" / "surfaces"
WRITE = "--write" in sys.argv
import proactive  # noqa: E402
import surfaces as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def vector(name, got):
    p = VECTORS / f"{name}.json"
    if WRITE:
        p.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    want = json.loads(p.read_text()) if p.exists() else None
    check(f"S1 golden vector {name}", got, want)


NOW = 1791331200.0                 # 2026-10-07T00:00:00Z
DAY = NOW - 7 * 3600 - 17 * 3600   # the household day (PDT) began 2026-10-06T07:00:00Z
SAFE = proactive.SAFETY_SOURCES


def card(cid, title, *, source="calendar", urgency=1, updated=NOW - 600, acked=None, approval=False, can_mute=True,
         group=None, actions=None, item=None, prompt=None, detail=None, snoozed=None, kind="info", data=None,
         expires=None):
    c = {"id": cid, "title": title, "source": source, "kind": kind, "urgency": urgency, "created_at": updated - 60,
         "updated_at": updated, "acked_at": acked, "approval": approval, "can_mute": can_mute, "actions": actions or [],
         "action_prompt": prompt, "detail": detail, "expires_at": expires, "data": data}
    if group:
        c["group"] = group
    if item:
        c["item"] = item
    if snoozed:
        c["snoozed_from"] = snoozed
    return c


CAP = {"kind": "approve_cap", "cap_id": "k-7", "unit": "usd", "min": 10, "max": 80, "step": 5, "default": 40,
       "format": "Up to ${value}"}
DECK = [card("c-cap", "Raise the grocery cap?", source="approve-cap", urgency=2, approval=True, kind="approve_cap",
             data=CAP, actions=[{"id": "approve", "label": "Approve", "kind": "reply", "value": "cap:k-7:40"},
                                {"id": "decline", "label": "Not now", "kind": "reply", "value": "decline"}]),
        card("c-voice", "Voice note from Robin", source="voice-note", urgency=1, updated=NOW - 200, kind="voice_note",
             data={"kind": "voice_note", "file_id": "0a1b2c3d4e5f60718293a4b5", "seconds": 12}, expires=NOW + 86400,
             actions=[{"id": "keep", "label": "Keep", "kind": "reply", "value": ""}]),
        card("c-lock", "Lock up for the night?", source="lock-up", urgency=2, approval=True, can_mute=False,
             actions=[{"id": "a0", "label": "Lock up", "kind": "reply"}]),
        card("c-pickup", "Leo's pickup moved to 3:30", urgency=1, updated=NOW - 120, item={"id": "routine:r-9"},
             detail="School pickup, Thursday"),
        card("c-trip", "Leave by 5:10 for the recital", urgency=3, updated=NOW - 900, prompt="Plan my drive to the recital"),
        card("c-eng", "Retrieval runtime restarted", source="loop:checks", urgency=1, group="engineering"),
        card("c-back", "Call the plumber", urgency=1, updated=NOW - 300, snoozed="2026-10-05"),
        card("c-done", "Trash goes out tonight", urgency=1, acked=NOW - 3600),
        card("c-old", "Yesterday's bill reminder", urgency=1, acked=DAY - 600)]

print("── S1: golden vectors (fixed deck, viewer, now, household day) ──")
admin = S.cards(DECK, "alex", "admin", NOW, DAY, safety_sources=SAFE)
vector("cards.admin", admin)
vector("cards.screen", S.cards([c for c in DECK if c["id"] in ("c-pickup", "c-lock")], "living-room", "household",
                               NOW, DAY, safety_sources=SAFE))
vector("cards.kid.empty", S.cards([], "ava", "kid", NOW, DAY, safety_sources=SAFE))

print("── the rules, on the golden deck ──")
G = {g["id"]: g for g in admin["groups"]}
ids = lambda gid: [m["subject"]["id"] for m in G[gid]["members"]]  # noqa: E731
OPS = {o["id"]: o for o in admin["operations"]}
check("profile attention, purpose act, jb-1", (admin["profile"], admin["purpose"], admin["profileVersion"]),
      ("attention", "act", "jb-1"))
check("groups: needs a decision, cards, engineering, done today", [(g["id"], g["role"]) for g in admin["groups"]],
      [("g-needs-a-decision", "attention"), ("g-cards", "supporting"), ("g-engineering", "supporting"),
       ("g-done-today", "history")])
check("needs a decision: the approval cards", sorted(ids("g-needs-a-decision")), ["c-cap", "c-lock"])
check("cards: most urgent first, then newest", ids("g-cards"), ["c-trip", "c-pickup", "c-voice", "c-back"])
check("done today only (a card done before the household day began isn't on the deck)", ids("g-done-today"), ["c-done"])
check("urgency → SSS priority (3 → 0 required)", {m["subject"]["id"]: m["priority"] for g in admin["groups"]
                                                  for m in g["members"]}["c-trip"], 0)
check("a card back from Later says so", [m["slots"].get("status") for m in G["g-cards"]["members"]
                                         if m["subject"]["id"] == "c-back"], [{"text": "card.back_from_later", "tone": "info"}])
check("a done card carries no operation", [m["slots"]["actions"] for m in G["g-done-today"]["members"]], [[]])
check("open only for a card about an item this viewer may see (#1235)",
      sorted(o["subject"]["id"] for o in admin["operations"] if o["verb"] == "open"), ["c-pickup"])
check("…and it opens the item from Now", OPS["op-open-c-pickup"]["params"], {"view": "now", "item": "routine:r-9"})
check("ask uses the card's own prompt when it has one, else the app's 'Tell me more' words",
      (OPS["op-ask-c-trip"]["params"], OPS["op-ask-c-pickup"]["params"]),
      ({"prompt": "Plan my drive to the recital"}, {"prompt_key": "card.tell_me_more"}))
check("a card's own buttons, on its action route, the approval's first one primary",
      [(o["verb"], o["params"]["action_id"], o.get("variant"), o["label"]["text"]) for o in admin["operations"]
       if o["subject"]["id"] == "c-cap" and o["contract"] == "jellybean.card.action@1"],
      [("reply", "approve", "primary", "Approve"), ("reply", "decline", None, "Not now")])
check("a safety card can't be muted or put off (unavailable, with a reason)",
      [(OPS[f"op-{v}-c-lock"]["exposure"], OPS[f"op-{v}-c-lock"]["reason"]) for v in ("mute", "later")],
      [("unavailable", {"key": "card.safety"})] * 2)
screen = S.cards([c for c in DECK if c["id"] == "c-pickup"], "living-room", "household", NOW, DAY, safety_sources=SAFE)
check("a shared screen never gets Later or Stop these (withheld), still Done and Ask",
      sorted((o["verb"], o["exposure"]) for o in screen["operations"]),
      sorted([("ask", "available"), ("done", "available"), ("later", "withheld"), ("mute", "withheld"),
              ("open", "available")]))
check("each member carries its plain card verbatim (the apps' per-card parsers read it)",
      {m["subject"]["id"]: m["card"] for g in admin["groups"] for m in g["members"]},
      {c["id"]: c for c in DECK if c["id"] != "c-old"})
check("top-level now: the long-poll cursor, the plain payload's own", admin["now"], NOW)
check("a card button says its kind and value (action)", OPS["op-act-c-cap-decline"]["action"],
      {"id": "decline", "kind": "reply", "value": "decline"})
check("Approve on an approve_cap card declares its slider (input)", OPS["op-act-c-cap-approve"]["input"],
      {"kind": "range", "sends": "value", "value_format": "cap:{cap_id}:{value}", "cap_id": "k-7", "unit": "usd",
       "min": 10, "max": 80, "step": 5, "default": 40, "format": "Up to ${value}"})
check("…and no other button does", sorted(i for i, o in OPS.items() if "input" in o), ["op-act-c-cap-approve"])
check("a voice note keeps its data, kind inside it, and its expiry",
      [(m["card"]["data"]["kind"], m["card"]["expires_at"]) for g in admin["groups"] for m in g["members"]
       if m["subject"]["id"] == "c-voice"], [("voice_note", NOW + 86400)])
check("Ask in the app's words when the card has no prompt of its own", OPS["op-ask-c-pickup"]["label"],
      {"key": "card.tell_me_more", "text": "Tell me more"})
check("no restore (it has no route yet: docs/surfaces.md §2.6)", [o for o in admin["operations"] if o["verb"] == "restore"], [])
check("sizeClass after projection", (admin["sizeClass"], S.cards([], "ava", "kid", NOW, DAY)["sizeClass"]), ("dozens", "few"))
check("deterministic: the same inputs, the same surface (S2-9)", S.cards(DECK, "alex", "admin", NOW, DAY, safety_sources=SAFE),
      admin)

print("── S2: through the real route ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, platform="ios"):
    s_ = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": f"{name}'s device",
                                         "platform": platform}).json()["token"]
    return {"X-Agent-Token": tok}, next(p["id"] for p in st.people() if p["name"] == name)


ROBIN, robin = pair("Robin Quarry", "adult")
AVA, ava = pair("Ava Quarry", "kid")
TV, _ = pair("Living room", "household", "tvos")
ADMIN = hh.ADMIN_PERSON_ID
proactive.add_card("Dentist moved to Friday", "For the admin only", urgency=1, source="calendar", to=[ADMIN])
proactive.add_card("Bins out tonight", "Everyone", urgency=1, source="routine", to="household")
rcard = proactive.add_card("Robin's gym class at 7", "Private to Robin", urgency=2, source="calendar", to=[robin])


def surface(h):
    r = c.get("/v1/proactive?as=surface", headers=h)
    return r.status_code, r.json()


def titles(s):
    return sorted(m["slots"]["title"] for g in s["groups"] for m in g["members"])


NEVER = {"admin": ["Robin's gym class"], "robin": ["Dentist moved"], "ava": ["Dentist moved", "Robin's gym class"],
         "screen": ["Dentist moved", "Robin's gym class"]}
for nm, h in (("admin", M), ("robin", ROBIN), ("ava", AVA), ("screen", TV)):
    code, s = surface(h)
    check(f"{nm}: answers as jb-1 attention", (code, s.get("profileVersion"), s.get("profile")), (200, "jb-1", "attention"))
    check(f"{nm}: never-see list (whole JSON)", [x for x in NEVER[nm] if x in json.dumps(s)], [])
check("each viewer's titles are their own and the household's",
      [titles(surface(h)[1]) for h in (M, ROBIN, AVA)],
      [["Bins out tonight", "Dentist moved to Friday"], ["Bins out tonight", "Robin's gym class at 7"], ["Bins out tonight"]])
before = [m["subject"]["id"] for g in surface(M)[1]["groups"] for m in g["members"]]
proactive.add_card("Robin's swim at 6", "Private to Robin", urgency=3, source="calendar", to=[robin])
check("S2-1: a card for Robin never changes the order of the admin's deck",
      [m["subject"]["id"] for g in surface(M)[1]["groups"] for m in g["members"]], before)
fp = surface(AVA)[1]["fingerprint"]
proactive.add_card("Dentist moved to Saturday", "For the admin only", urgency=2, source="calendar", to=[ADMIN],
                   key="dentist-move")
check("S2-2: a hidden card changing never changes Ava's fingerprint or counts",
      (surface(AVA)[1]["fingerprint"], surface(AVA)[1]["sizeClass"]), (fp, "few"))
rid = rcard["id"] if isinstance(rcard, dict) else rcard
ack = c.post(f"/v1/proactive/{rid}/ack", headers=AVA)
check("S2-7: done on a card that isn't yours does nothing", (ack.status_code != 200 or not ack.json().get("acked"),
                                                            "Robin's gym class at 7" in titles(surface(ROBIN)[1])), (True, True))
check("S2-9: two calls with the same state, the same surface", surface(ROBIN)[1]["fingerprint"], surface(ROBIN)[1]["fingerprint"])
paths = {r.path for r in api.app.routes if hasattr(r, "path")}
pat = lambda p: re.sub(r"\{[^}]+\}", "X", p)  # noqa: E731
used = {pat(re.sub(r"/c-[^/]+|/p[0-9a-f-]{6,}[^/]*", "/{id}", o["route"]["path"]))
        for s in (admin, surface(M)[1]) for o in s["operations"]}
check("every operation names a route the app actually has (none invented)", sorted(u for u in used if u not in {pat(p) for p in paths}), [])
plain = {x["id"]: x for x in c.get("/v1/proactive?include_acked=true", headers=ROBIN).json()["cards"]}
rs = surface(ROBIN)[1]
check("merged payload: a member's card is exactly the plain route's card for the same viewer",
      {m["subject"]["id"]: m["card"] for g in rs["groups"] for m in g["members"]},
      {k: v for k, v in plain.items() if k in {m["subject"]["id"] for g in rs["groups"] for m in g["members"]}})
check("…and the surface has every plain card", sorted(plain), sorted(m["subject"]["id"] for g in rs["groups"] for m in g["members"]))
check("the route's now is the time it answered (a number, for /v1/cards/wait?since=)", isinstance(rs.get("now"), float), True)
print("── the trace's excluded: this viewer's own ineligible cards, counts only (§4) ──")


def excl(h):
    return {x["rule"]: x["count"] for x in surface(h)[1]["explanation"]["excluded"]}


hcard = proactive.add_card("Recycling pickup moved", "Everyone", urgency=1, source="recycling", to="household")
hid = hcard["id"] if isinstance(hcard, dict) else hcard
mcard = proactive.add_card("Alex's lab results", "Private", urgency=1, source="lab-results", to=[ADMIN])
mid_ = mcard["id"] if isinstance(mcard, dict) else mcard
before = {"admin": surface(M)[1], "ava": surface(AVA)[1], "screen": surface(TV)[1]}
check("nothing put off yet: no excluded rows for a viewer", excl(ROBIN), {})
r1 = c.post(f"/v1/proactive/{hid}/mute", headers=ROBIN)
check("muting dismisses the card it was on (done for Robin), so nothing new is counted yet",
      (r1.status_code, excl(ROBIN).get("muted")), (200, None))
proactive.add_card("Recycling pickup moved again", "Everyone", urgency=1, source="recycling", to="household")
check("the next household card of that kind: left out of Robin's deck, counted in Robin's trace (muted 1)",
      ("Recycling pickup moved again" in json.dumps(surface(ROBIN)[1]), excl(ROBIN).get("muted")), (False, 1))
check("…a shared card Robin muted is still on everyone else's deck, their trace unchanged",
      [(nm, "Recycling pickup moved again" in json.dumps(surface(h)[1]), surface(h)[1]["explanation"]["excluded"])
       for nm, h in (("admin", M), ("ava", AVA))], [("admin", True, before["admin"]["explanation"]["excluded"]),
                                                     ("ava", True, before["ava"]["explanation"]["excluded"])])
check("…and Robin's mute never shows in another viewer's trace", [excl(h).get("muted") for h in (M, AVA, TV)],
      [None, None, None])
r2 = c.post(f"/v1/proactive/{mid_}/snooze", json={"until": time.time() + 3600}, headers=M)
check("the admin snoozes their private card: the admin's trace counts it (snoozed 1)",
      (r2.status_code, excl(M).get("snoozed")), (200, 1))
check("DERIVED DETAILS: a private card's snooze never shows in anyone else's count",
      [excl(h).get("snoozed") for h in (ROBIN, AVA, TV)], [None, None, None])
check("…nor its title anywhere in their surfaces", ["Alex's lab results" in json.dumps(surface(h)[1]) for h in (ROBIN, AVA, TV)],
      [False, False, False])
check("the trace is counts and rules only: never an id or a title",
      sorted({k for h in (M, ROBIN) for x in surface(h)[1]["explanation"]["excluded"] for k in x}), ["count", "rule"])
check("a viewer's own count moves their own fingerprint", surface(M)[1]["fingerprint"] != before["admin"]["fingerprint"], True)
old = c.get("/v1/proactive", headers=ROBIN).json()
check("old clients keep today's shape", sorted(old), ["background", "cards", "now"])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
