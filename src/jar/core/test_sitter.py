#!/usr/bin/env python3
# gate: hermetic
"""Sitter and guest mode on a household TV (jellybean#428, titan-node#797): adults keep a sheet (its own 0600 store);
an adult turns it on, or the TV asks and an adult at home confirms on their phone; while it's on the TV gets the
sheet and nothing else of the household (every other route 403 sitter_mode); it ends at its time or by an adult,
never the TV alone; the Wi-Fi password never reaches a prompt and the secret vault masks it. Throwaway stores; made-up
people; Home's today is stubbed. Run: python3 scripts/test_sitter.py"""
from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-sitter-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "TASK_LOG_DB": "task_log.db", "UPLOADS_DB": "uploads.db", "UPLOADS_DIR": "uploads",
             "TV_UNLOCK_DB": "tv_unlock.db", "SITTER_DB": "sitter.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import titan_alert  # noqa: E402
titan_alert.alert = lambda *a, **k: None   # never a real alert from a test

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive  # noqa: E402
import sitter  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}
PASSWORD = "Bramble-Otter-4471"


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Hale", "adult", "Alex's iPhone")
ROBIN, robin = pair("Robin Hale", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
TV, _ = pair("Living room", "household", "Living room TV", platform="tvos")
IPAD, _ = pair("Kitchen", "household", "Kitchen iPad")

# Home's household-visible today, as home_screen would build it (the real one needs the record runtime).
TODAY = [{"at": "4 PM", "title": "Piano lesson: Ava"}, {"at": "6 PM", "title": "Leo's dentist"},
         {"at": "7 PM", "title": "Book club"}, {"at": "8 PM", "title": "Avalanche documentary"}]
sitter._kids_today = lambda names: sitter.match_kids(TODAY, ["Ava", "Leo"])

SHEET = {"contacts": [{"name": "Dr. Jordan", "role": "Pediatrician", "phone": "555-0101"},
                      {"name": "Robin", "role": "Parent", "phone": "555-0102"}, {"role": "no name"}],
         "basics": [{"title": "First-aid kit", "text": "Hall closet, top shelf."}, {"title": "", "text": "dropped"}],
         "kids": [{"name": "Ava", "bedtime": "8:30 PM", "notes": "Allergic to peanuts."}, {"name": "Leo", "bedtime": "7:30 PM"}],
         "wifi": {"network": "HaleHouse", "password": PASSWORD}, "ssn": "nope"}

print("── the sheet ──")
check("a kid can't read it", c.get("/v1/home/sitter/sheet", headers=AVA).status_code, 403)
check("nor can the TV", c.get("/v1/home/sitter/sheet", headers=TV).status_code, 403)
r = c.put("/v1/home/sitter/sheet", json=SHEET, headers=ALEX)
check("an adult saves it", r.status_code, 200)
s = c.get("/v1/home/sitter/sheet", headers=ROBIN).json()
check("…known fields only, empties dropped", (len(s["contacts"]), len(s["basics"]), "ssn" in s), (2, 1, False))
check("…the adults' own view has the password", s["wifi"]["password"], PASSWORD)
check("…kept in its own 0600 file", stat.S_IMODE(os.stat(_TMP / "sitter.db").st_mode), 0o600)
edit = {k: v for k, v in SHEET.items() if k != "wifi"}
edit["basics"] = SHEET["basics"] + [{"title": "Bedtime", "text": "Lights out at 8:30."}]
s = c.put("/v1/home/sitter/sheet", json=edit, headers=ALEX).json()
check("a save without wifi keeps the stored password", (s["wifi"]["password"], len(s["basics"])), (PASSWORD, 2))
s = c.put("/v1/home/sitter/sheet", json={**edit, "wifi": None}, headers=ALEX).json()
check("…an explicit null clears it", "wifi" in s, False)
c.put("/v1/home/sitter/sheet", json=SHEET, headers=ALEX)

print("── off: the TV is a TV ──")
check("the TV sees it off", c.get("/v1/home/sitter", headers=TV).json(), {"state": "off"})
check("…and its other routes as usual", c.get("/v1/tv-unlocks/self", headers=TV).status_code, 200)

print("── an adult turns it on ──")
r = c.post("/v1/home/sitter", json={"until": time.time() + 3 * 3600}, headers=ALEX)
check("on", (r.status_code, r.json()["state"], r.json()["kind"]), (200, "on", "sitter"))
v = c.get("/v1/home/sitter", headers=TV).json()
check("the TV gets the sheet", (v["state"], [x["name"] for x in v["contacts"]], v["wifi"]["password"]),
      ("on", ["Dr. Jordan", "Robin"], PASSWORD))
check("…the kids' notes as a parent wrote them", v["kids"][0].get("notes"), "Allergic to peanuts.")
check("…the kids' day: only items naming a kid", {k["name"]: [i["what"] for i in k["items"]] for k in v["kids_today"]},
      {"Ava": ["Piano lesson: Ava"], "Leo": ["Leo's dentist"]})
for method, path in (("GET", "/v1/schedule"), ("GET", "/v1/home"), ("GET", "/v1/proactive"), ("GET", "/v1/home/kids"),
                     ("POST", "/v1/prompt"), ("GET", "/v1/home/house"), ("POST", "/v1/tv-unlocks/request")):
    r = c.request(method, path, headers=TV, json={"prompt": "hi"} if method == "POST" else None)
    check(f"the TV's {method} {path}: 403 sitter_mode", (r.status_code, r.json().get("code")), (403, "sitter_mode"))
check("another household device isn't affected", (c.get("/v1/proactive", headers=IPAD).status_code, c.get("/v1/proactive", headers=TV).status_code), (200, 403))
check("the TV can't end it", c.request("DELETE", "/v1/home/sitter", headers=TV).status_code, 403)
check("nor can a kid", c.request("DELETE", "/v1/home/sitter", headers=AVA).status_code, 403)
check("an adult ends it", c.request("DELETE", "/v1/home/sitter", headers=ROBIN).json(), {"ended": 1})
check("…the TV is a TV again", (c.get("/v1/home/sitter", headers=TV).json()["state"],
                                c.get("/v1/tv-unlocks/self", headers=TV).status_code), ("off", 200))

print("── guest mode ──")
c.post("/v1/home/sitter", json={"kind": "guest"}, headers=ALEX)
v = c.get("/v1/home/sitter", headers=TV).json()
check("basics and Wi-Fi only", sorted(v), sorted(["state", "kind", "ends_at", "basics", "wifi"]))
_t = time.time()   # the 15-minute floor wins within 15 minutes of midnight (the 10-08 23:48Z deploy hit it)
check("…ends by midnight", v["ends_at"] <= max(sitter._midnight(_t), _t + 15 * 60) + 1, True)
c.request("DELETE", "/v1/home/sitter", headers=ALEX)

print("── the TV asks; an adult at home confirms ──")
c.post("/v1/presence/place", json={"id": "pe-1", "event": "arrived", "place": "home",
                                   "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, headers=ROBIN)
r = c.post("/v1/home/sitter", json={}, headers=TV)
check("the TV asks", (r.status_code, r.json()["state"], r.json()["asked"]), (202, "pending", 1))
sid = r.json()["id"]
check("…pending on the TV, no sheet", c.get("/v1/home/sitter", headers=TV).json()["state"], "pending")
check("…a household iPad can't ask", c.post("/v1/home/sitter", json={}, headers=IPAD).status_code, 403)
card = next(x for x in proactive.list_cards(person=robin, role="adult") if (x.get("data") or {}).get("session_id") == sid)
check("Robin gets the card", ([a["id"] for a in card["actions"]], card["data"]["kind"]), (["start", "decline"], "sitter_request"))
check("…the card holds no sheet", PASSWORD in str(card) or "Dr. Jordan" in str(card), False)
check("a kid can't approve", sitter.answer(f"start:{sid}", ava, "kid")[0], False)
check("an adult who wasn't asked can't", sitter.answer(f"start:{sid}", alex, "adult")[0], False)
res = c.post(f"/v1/proactive/{card['id']}/action", json={"action_id": "start"}, headers=ROBIN).json()
check("Robin turns it on from the card", res.get("ok"), True)
check("…on", c.get("/v1/home/sitter", headers=TV).json()["state"], "on")
check("…blocked", c.get("/v1/schedule", headers=TV).status_code, 403)

print("── it ends at its time ──")
dev = sitter.audit(1)[0]["device_id"]
later = sitter.audit(1)[0]["ends_at"] + 1
check("past ends_at: off", sitter.state(dev, now=later)["state"], "off")
check("…not blocked", sitter.blocked(dev, "GET", "/v1/schedule", now=later), False)
check("the audit has who and how", {(r["kind"], r["end_reason"]) for r in sitter.audit(1) if r["started_by"]},
      {("sitter", "ended"), ("guest", "ended"), ("sitter", "expired")})

print("── the password stays out of prompts ──")
v = lib._SecretVault()
check("the vault masks it wherever it appears", PASSWORD in v.shield(f"Try {PASSWORD} on HaleHouse"), False)
check("…and gives it back on titan", v.unshield(v.shield(PASSWORD)), PASSWORD)
check("the tool prompt never has it", (PASSWORD in lib.build_tool_system_prompt(), "sitter" in lib.build_tool_system_prompt().lower()),
      (False, False))
importers = sorted(p.name for p in HERE.glob("*.py") if not p.name.startswith("test_")
                   and re.search(r"^\s*import sitter\b|^\s*from sitter import", p.read_text(), re.M))
check("only the routes, the card hook and the vault read the sheet",
      importers, ["agent_api_server.py", "agent_runtime_lib.py", "proactive.py"])

print("── midnight is the household's, not titan's UTC (10-08) ──")
import local_time  # noqa: E402
from datetime import datetime  # noqa: E402
_tz = os.environ.get("AGENT_TZ")
os.environ["AGENT_TZ"] = "America/Denver"
_five_pm = datetime.fromisoformat("2026-10-08T23:00:00+00:00").timestamp()   # 5:00 PM in Denver
check("at 5 PM in the house, midnight is 7 hours away (06:00Z), not UTC's 00:00Z",
      sitter._midnight(_five_pm), datetime.fromisoformat("2026-10-09T06:00:00+00:00").timestamp())
check("a clock time with no zone is the household's", sitter._ends("2026-10-08T21:00:00", _five_pm),
      datetime.fromisoformat("2026-10-09T03:00:00+00:00").timestamp())
_late = datetime.fromisoformat("2026-10-09T05:55:00+00:00").timestamp()      # 11:55 PM in Denver
check("five minutes before the house's midnight: the 15-minute floor", sitter._ends(None, _late), _late + 15 * 60)
# the approval card's "Until": the household's clock, even when titan's own is UTC (JEV's review of #1285)
_host_tz = os.environ.get("TZ")
os.environ["TZ"] = "UTC"
time.tzset()
try:
    _cards = []
    _ends_at = datetime.fromisoformat("2026-10-09T03:30:00+00:00").timestamp()   # 9:30 PM in Denver, 03:30 UTC
    sitter.request({"id": "tv-clock", "name": "Den TV", "platform": "tvos"}, [{"id": "p_alex", "role": "admin"}],
                   until=_ends_at, now=_five_pm, add_card=lambda *a, **k: _cards.append((a, k)) or {"id": "c-1"},
                   where=lambda pid: None)
    check("the approving adult reads the house's time (9:30 PM in Denver), not titan's UTC (3:30 AM)",
          _cards[0][0][1].split(".")[0] if _cards else None, "Until 9:30 PM")
    import tv_unlock  # noqa: E402
    check("tv_unlock's 'Until' too (the same clock)", local_time.clock(_ends_at, "%-I:%M"), "9:30")
finally:
    if _host_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _host_tz
    time.tzset()
if _tz is None:
    os.environ.pop("AGENT_TZ", None)
else:
    os.environ["AGENT_TZ"] = _tz

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
