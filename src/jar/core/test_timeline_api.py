#!/usr/bin/env python3
"""GET/POST /v1/timeline, hidden calendars on /v1/me, and coordination ignoring them. Hermetic.
Run: python3 scripts/test_timeline_api.py"""
from __future__ import annotations
import os, sys, tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
_TMP = Path(tempfile.mkdtemp(prefix="titan-timeline-api-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402
passed = failed = 0
def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")

EVS = [{"event_id": "e1", "timestamp": "2026-09-30T10:00:00", "payload": {"text": "Zach downloaded taxes.pdf", "person": "zach", "audience": "private"}},
       {"event_id": "e2", "timestamp": "2026-09-30T11:00:00", "payload": {"text": "Trash went out", "person": "zach", "audience": "household"}},
       {"event_id": "e3", "timestamp": "2026-09-30T12:00:00", "payload": {"text": "Robin's private note", "person": "p_mir", "audience": "private"}},
       {"event_id": "e4", "timestamp": "2026-09-29T09:00:00", "payload": {"text": "legacy entry"}}]
lib._timeline_events = lambda stream="household-timeline": list(EVS)
print("── what each person sees ──")
z = lib.timeline_for("zach", "admin")
check("Zach: his own, household, legacy (his); newest first", [e["id"] for e in z], ["e2", "e1", "e4"])
check("…'mine' marks his own", [e["mine"] for e in z], [True, True, False])
m = lib.timeline_for("p_mir", "adult")
check("Robin: hers and the household's, never Zach's private", [e["id"] for e in m], ["e3", "e2"])
check("a kid: household only", [e["id"] for e in lib.timeline_for("p_kid", "kid")], ["e2"])
check("a shared device: household only", [e["id"] for e in lib.timeline_for("household", "household")], ["e2"])
check("before= pages back", [e["id"] for e in lib.timeline_for("zach", "admin", before="2026-09-30T10:30:00")], ["e1", "e4"])
check("limit", len(lib.timeline_for("zach", "admin", limit=1)), 1)

print("\n── the feed: notes on the timeline ──")
import kb_privacy as kbp  # noqa: E402
SRC = [{"source_id": "note-wifi", "title": "Wi-Fi for guests", "source_type": "note"},
       {"source_id": "note-filter", "title": "Furnace filter", "source_type": "note"},
       {"source_id": "note-rob", "title": "Gift ideas", "source_type": "note"},
       {"source_id": "runbook-1", "title": "Titan runbook", "source_type": "note"},
       {"source_id": "conn-1", "title": "Scanned receipt", "source_type": "connector:scans"}]
lib._feed_sources = lambda: SRC
T0 = datetime(2026, 9, 30, 18, 0).timestamp()
for sid, owner, aud, why, dt in (("note-wifi", "zach", "household", "share", 0), ("note-filter", "zach", "private", "write", 60),
                                 ("note-rob", "p_mir", "private", "write", 120), ("runbook-1", None, "admins", "write", 180),
                                 ("conn-1", "zach", "private", "write", 240)):
    kbp.set_acl(sid, owner, aud, by=owner, why=why)
con = kbp._connect(True)
for i, sid in enumerate(["note-wifi", "note-filter", "note-rob", "runbook-1", "conn-1"]):
    con.execute("UPDATE acl SET set_at=? WHERE source_id=?", (T0 + i * 60, sid))
con.commit(); con.close()
EVS.append({"event_id": "e6", "timestamp": "2026-09-30T19:30:00", "payload": {"text": "Changed the furnace filter (16x25x1)", "person": "zach", "audience": "private"}})
zf = lib.timeline_for("zach", "admin", notes=True)
notes = [e for e in zf if e.get("kind") == "note"]
check("Zach's feed: his saved and shared notes; never Robin's private, titan's runbook or connector imports",
      sorted(e["kb_id"] for e in notes), ["note-filter", "note-wifi"])
check("…in words", sorted(e["text"] for e in notes), ["Saved a note: \u201cFurnace filter\u201d", "Shared \u201cWi-Fi for guests\u201d with everyone at home"])
check("…an entry that names a note links it", next(e for e in zf if e["id"] == "e6").get("links"), [{"kb_id": "note-filter", "title": "Furnace filter"}])
check("…the timeline alone (notes off) is unchanged", [e for e in lib.timeline_for("zach", "admin") if e.get("kind") == "note"], [])
mf = lib.timeline_for("p_mir", "adult", notes=True)
check("Robin's feed: her own note and the shared Wi-Fi; not Zach's private filter",
      sorted(e["kb_id"] for e in mf if e.get("kind") == "note"), ["note-rob", "note-wifi"])
check("…and his entry's link doesn't show her his private note", [e for e in mf if e["id"] == "e6"], [])
kbp.set_acl("note-wifi", "zach", "private", by="zach", why="unshare")
check("made private again: gone from her feed", sorted(e["kb_id"] for e in lib.timeline_for("p_mir", "adult", notes=True)
                                                   if e.get("kind") == "note"), ["note-rob"])
check("…and his says so", next(e["text"] for e in lib.timeline_for("zach", "admin", notes=True) if e.get("kb_id") == "note-wifi"),
      "Made \u201cWi-Fi for guests\u201d private")
check("a kid sees no one's private notes", [e for e in lib.timeline_for("p_kid", "kid", notes=True) if e.get("kind") == "note"], [])
kbp.set_acl("note-old", "zach", "private", by="kb_backfill_owner", why="backfill:saved", keep=True)
SRC.append({"source_id": "note-old", "title": "Old saved note", "source_type": "note"})
check("a backfilled row is no event (its time is the backfill's)",
      [e for e in lib.timeline_for("zach", "admin", notes=True) if e.get("kb_id") == "note-old"], [])
SRC.append({"source_id": "web-1", "title": "Recipe page", "source_type": "web"})
kbp.set_acl("web-1", "zach", "private", by="zach", why="write")
check("a saved web page is in the feed", any(e.get("kb_id") == "web-1" for e in lib.timeline_for("zach", "admin", notes=True)), True)
check("only returned entries are linked (limit applies first)", len(lib.timeline_for("zach", "admin", notes=True, limit=2)), 2)
EVS.pop()

print("\n── the routes ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api.db"); hh.reset_store(st)
c = TestClient(api.app); M = {"X-Agent-Token": MASTER}
s = c.post("/v1/pair/start", json={"new_person": {"name": "Robin", "role": "adult"}}, headers=M).json()
MIR = {"X-Agent-Token": c.post("/v1/pair/claim", json={"code": s["code"], "device_name": "Robin's iPhone", "platform": "ios"}).json()["token"]}
mid = s["person"]["id"]
EVS.append({"event_id": "e5", "timestamp": "2026-09-30T13:00:00", "payload": {"text": "Robin's errand", "person": mid, "audience": "private"}})
r = c.get("/v1/timeline", headers=MIR).json()["events"]
check("GET /v1/timeline as Robin: her own + household", [e["id"] for e in r], ["e5", "e2"])
rn = c.get("/v1/timeline?notes=true", headers=MIR).json()["events"]
check("GET /v1/timeline?notes=1: the feed (her note is a different id here: her paired person id)",
      all(e.get("kind") != "note" or e["person"] == mid or e["audience"] != "private" for e in rn), True)
check("dev stream: admin only", c.get("/v1/timeline?which=dev", headers=MIR).status_code, 403)
check("POST with no text → 400", c.post("/v1/timeline", json={}, headers=MIR).status_code, 400)
os.environ["AGENT_TURN_SOURCE"] = "test"
r = c.post("/v1/timeline", json={"text": "Picked up dry cleaning"}, headers=MIR)
check("POST goes through log_event (a test turn writes nothing)", (r.status_code, "Test turn" in r.json().get("result", "")), (400, True))

print("\n── hidden calendars ──")
check("GET /v1/me: none hidden yet, no places named", {k: v for k, v in c.get("/v1/me", headers=MIR).json()["settings"].items()
                                                         if k != "household_calendars"},
      {"calendars_ignored": [], "places": [], "facets": {}, "live_activity_detail": False, "call_me": None,
       "connectors_off": []})
r = c.patch("/v1/me", json={"calendars_ignored": ["US Holidays", " us holidays ", "Birthdays"]}, headers=MIR)
check("PATCH /v1/me sets them (deduped)", r.json()["settings"]["calendars_ignored"], ["US Holidays", "Birthdays"])
check("…and every device of hers reads them back", c.get("/v1/me", headers=MIR).json()["settings"]["calendars_ignored"], ["US Holidays", "Birthdays"])
check("a bad shape → 400", c.patch("/v1/me", json={"calendars_ignored": "Birthdays"}, headers=MIR).status_code, 400)

print("\n── named places (#284: \"when I get home…\" knows Work before her first arrival there) ──")
r = c.patch("/v1/me", json={"places": [" Work ", "School", "work", "", "Grandma's house"]}, headers=MIR)
check("PATCH /v1/me places: trimmed, de-duplicated, names only", r.json()["settings"], {"places": ["Work", "School", "Grandma's house"]})
check("…calendars untouched by a places-only PATCH", c.get("/v1/me", headers=MIR).json()["settings"]["calendars_ignored"],
      ["US Holidays", "Birthdays"])
check("…read back on GET /v1/me", c.get("/v1/me", headers=MIR).json()["settings"]["places"], ["Work", "School", "Grandma's house"])

print("\n── facet map (#354: her calendars and vendors → work, personal…) ──")
r = c.patch("/v1/me", json={"facets": {"vendor:Railway  Corp": "work", "Calendar:Work": "work"}}, headers=MIR)
check("PATCH /v1/me facets: keys normalized", r.json()["settings"], {"facets": {"vendor:railway corp": "work", "calendar:work": "work"}})
check("…read back", c.get("/v1/me", headers=MIR).json()["settings"]["facets"], {"vendor:railway corp": "work", "calendar:work": "work"})
check("a facet that isn't one → 400", c.patch("/v1/me", json={"facets": {"vendor:x": "home"}}, headers=MIR).status_code, 400)
check("a key that isn't calendar/vendor → 400", c.patch("/v1/me", json={"facets": {"x": "work"}}, headers=MIR).status_code, 400)
check("at most 20, each ≤ 40 chars", (lambda p: (len(p), max(map(len, p))))(
      c.patch("/v1/me", json={"places": [f"Place {i} " + "x" * 50 for i in range(30)]}, headers=MIR).json()["settings"]["places"]), (20, 40))
c.patch("/v1/me", json={"places": ["Work", "School"]}, headers=MIR)
check("a bad shape → 400", c.patch("/v1/me", json={"places": "Work"}, headers=MIR).status_code, 400)
check("neither setting → 400", c.patch("/v1/me", json={"other": 1}, headers=MIR).status_code, 400)
import places as _pl  # noqa: E402
check("places.known: home, her named places, then places from events", _pl.known(mid, named=st.places(mid)), ["home", "Work", "School"])
check("…and another person's named places aren't hers", "Work" in _pl.known("zach", named=st.places("zach")), False)
import household_coordination as hc  # noqa: E402
tz = ZoneInfo("America/Los_Angeles")
people = [{"id": "zach", "name": "Zach", "role": "admin", "calendars_ignored": ["US Holidays"]},
          {"id": mid, "name": "Robin", "role": "adult", "calendars_ignored": []}]
nodes = [{"data": {"title": "Columbus Day thing", "start": "2026-10-01T10:00:00-07:00", "end": "2026-10-01T11:00:00-07:00",
                   "calendar": "US Holidays", "provenance": {"owner": "zach"}}},
         {"data": {"title": "Dentist", "start": "2026-10-01T10:00:00-07:00", "end": "2026-10-01T11:00:00-07:00",
                   "calendar": "Home", "provenance": {"owner": "zach"}}},
         {"data": {"title": "Her holiday", "start": "2026-10-01T14:00:00-07:00", "end": "2026-10-01T15:00:00-07:00",
                   "calendar": "US Holidays", "provenance": {"owner": mid}}}]
titles = sorted(e.title for e in hc.events_for(date(2026, 10, 1), nodes, people, tz))
check("coordination ignores a calendar its syncer hid, not someone else's", titles, ["Dentist", "Her holiday"])
print("\n── titan's own canary note is never a feed item (10-04) ──")
lib._feed_sources = lambda: SRC + [{"source_id": "note-agent-canary", "title": "Agent Canary", "source_type": "note"}]
kbp.set_acl("note-agent-canary", "zach", "private", by="zach", why="write")
check("the canary's note (saved as the admin every 10 min) isn't in his feed",
      "note-agent-canary" in [e.get("kb_id") for e in lib.timeline_for("zach", "admin", notes=True)], False)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
