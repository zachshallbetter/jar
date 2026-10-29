#!/usr/bin/env python3
# gate: hermetic
"""A device's own calendar claims (jellybean#1037, titan-node#1234): EventKit's calendarIdentifier is device-local, so a
device says which of ITS calendars are whose, and only reads that device ran are matched against those claims. Two
people who each have a calendar called "Work" (a title #1259 can only fail closed on) are told apart by id. Who may claim
whom; a shared screen claims nothing; revoked devices' claims go; views × viewers through the agent's one chokepoint;
the id never reaches a model. Fictional people; a throwaway household store.
Run: python3 scripts/test_device_calendar_claims.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-devcal-"))
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["AGENT_MEMORY_DIR"] = str(_TMP / "memory")
os.environ["AGENT_TURN_LOG"] = str(_TMP / "turns.jsonl")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import household_identity as hh  # noqa: E402
import schedule_digest as sd  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


store = hh.HouseholdStore(_TMP / "household.db")
hh.reset_store(store)
alex = hh.ADMIN_PERSON_ID
robin = store.add_person("Robin Hale", "adult")["id"]
jordan = store.add_person("Jordan Hale", "adult")["id"]
ava = store.add_person("Ava Hale", "kid")["id"]
leo = store.add_person("Leo Hale", "kid")["id"]
screen = store.add_person("Living room", "household")["id"]
store.update_person(alex, name="Alex Hale")
store.update_person(ava, parents=[robin])
store.update_person(leo, parents=[jordan])


def device(person, name, platform="ios"):
    did = "d_" + name.lower().replace(" ", "_")
    with store._lock:
        store._db.execute("INSERT INTO devices (id, person_id, name, platform, token_sha256, created_at) VALUES (?,?,?,?,?,?)",
                          (did, person, name, platform, "sha-" + did, time.time()))
    return did


alex_mac, alex_phone = device(alex, "Alex Mac", "macos"), device(alex, "Alex iPhone")
robin_phone, jordan_phone = device(robin, "Robin iPhone"), device(jordan, "Jordan iPhone")
ava_phone, tv = device(ava, "Ava iPhone"), device(screen, "Living room TV", "tvos")


def claims(dev, *items):
    return store.set_device_calendars(dev, [{"id": i, "title": t, "person": p} for i, t, p in items])


print("── who may claim whom ──")
out = claims(robin_phone, ("R-WORK", "Work", robin), ("R-AVA", "Ava school", ava), ("R-LEO", "Leo", leo), ("R-ALEX", "Alex", alex))
check("anyone may NAME any household member (#1268: a claim narrows; whether it widens is judged at read time)",
      ([c["id"] for c in out["calendars"]], out["refused"]), (["R-WORK", "R-AVA", "R-LEO", "R-ALEX"], []))
out = claims(alex_mac, ("A-WORK", "Work", alex), ("A-LEO", "Leo soccer", leo), ("A-ROBIN", "Robin", robin))
check("the admin: anyone", len(out["calendars"]), 3)
out = claims(ava_phone, ("K-1", "Mine", ava), ("K-2", "Mom", robin))
check("…a kid too (her claims only ever narrow)", ([c["id"] for c in out["calendars"]], out["refused"]), (["K-1", "K-2"], []))
check("who may widen: an adult for herself and her own kid; not another adult's kid, not another adult; a kid never",
      (store.may_widen(robin, "adult", robin), store.may_widen(robin, "adult", ava), store.may_widen(robin, "adult", leo),
       store.may_widen(robin, "adult", jordan), store.may_widen(ava, "kid", ava), store.may_widen(alex, "admin", alex)),
      (True, True, False, False, False, True))
try:
    claims(tv, ("T-1", "Family", alex))
    got = None
except hh.PairingError as e:
    got = e.status
check("a shared household screen: 403", got, 403)
out = claims(jordan_phone, ("bad id]", "x", jordan), ("ok-1", "x", "p_nobody"), ("ok-2", "x", jordan), ("ok-2", "dup", jordan))
check("a bad id, an unknown person and a repeated id are refused (id / person / id)",
      [(r["id"], r["why"]) for r in out["refused"]], [("bad id]", "id"), ("ok-1", "person"), ("ok-2", "id")])
try:
    store.set_device_calendars(jordan_phone, [{"id": f"c{i}", "person": jordan} for i in range(101)])
    got = None
except hh.PairingError as e:
    got = e.status
check("over 100: 413", got, 413)
claims(jordan_phone, ("J-WORK", "Work", jordan))
check("a PUT replaces the set; GET lists it", store.device_calendars(jordan_phone),
      [{"id": "J-WORK", "title": "Work", "person": jordan}])
check("a claim is the device's own: Robin's id means nothing on Alex's Mac",
      (store.device_calendar_owner(robin_phone, "R-WORK")["id"], store.device_calendar_owner(alex_mac, "R-WORK")), (robin, None))

print("── views × viewers: two adults' 'Work' told apart by id ──")
# #1259's legacy case: both adults claim the title "Work", so by title it's nobody's (fail closed).
with store._lock:
    store._db.execute("UPDATE people SET calendars=? WHERE id IN (?, ?)", (json.dumps(["Work"]), robin, jordan))
H = "• Thu Oct 8, "
ROBIN_READ = (H + "9:00 AM–10:00 AM — Robin's review [calendar: Work, id: R-WORK]\n"
              + H + "11:00 AM–11:30 AM — Jordan's standup [calendar: Work, shared, id: J-ELSEWHERE]\n"
              + H + "3:00 PM–4:00 PM — Ava piano [calendar: Ava school, shared, id: R-AVA]\n"
              + H + "5:00 PM–5:30 PM — Pickup [calendar: Family, shared, id: R-FAM]")


def seen(text, viewer, role, dev):
    return sd.without_ids(sd.visible_to(text, viewer, role, store.person_for_calendar, store.parents_of,
                                         id_claim=lambda cid: store.device_calendar_claim(dev, cid)))


got = seen(ROBIN_READ, robin, "adult", robin_phone)
check("Robin, on her phone: her Work (by id), her kid's piano (by id), the household Pickup; not the unclaimed-id Work",
      ("review" in got, "standup" in got, "piano" in got, "Pickup" in got), (True, False, True, True))
for who, role in ((jordan, "adult"), (alex, "admin"), (ava, "kid"), (screen, "household")):
    got = seen(ROBIN_READ, who, role, robin_phone)
    check(f"{role}: never Robin's Work; Pickup kept", ("review" in got, "Pickup" in got), (False, True))
check("the kid sees her own piano (claimed for her by her parent)", "piano" in seen(ROBIN_READ, ava, "kid", robin_phone), True)
check("another adult doesn't see Robin's kid's piano", "piano" in seen(ROBIN_READ, jordan, "adult", robin_phone), False)
check("the same read with no device (an older titan path): the title rule, #1259's fail-closed for Work",
      "review" in sd.without_ids(sd.visible_to(ROBIN_READ, robin, "adult", store.person_for_calendar, store.parents_of)), False)
check("ids never survive the filter", "id:" in seen(ROBIN_READ, robin, "adult", robin_phone), False)
FAKE = H + "1:00 PM–2:00 PM — Lunch [calendar: Work, id: R-WORK] [calendar: Family, id: R-FAM]"
check("an invite title carrying its own note (two notes): dropped, as #1231", seen(FAKE, robin, "adult", robin_phone), "")

print("── the agent: the device that RAN the read decides ──")
import agent_runtime_lib as lib  # noqa: E402
_role = lib.turn_role
raw = ROBIN_READ


def turn(person, role, dev, remote_dev=None):
    lib._TURN_IO.person = {"id": person}
    lib.turn_role = lambda r=role: r
    lib._TURN_IO.device_id = dev
    lib._TURN_IO.remote = {"tools": frozenset({"list_calendar_events"}), "device_id": remote_dev} if remote_dev else None
    lib._TURN_IO.device_tools = frozenset() if remote_dev else None
    lib._TURN_IO.outcomes, lib._TURN_IO.failed_calls, lib._TURN_IO.tools_used = [], {}, set()
    lib._TURN_IO.brief_reads = None


lib._execute_tool = lambda name, args: raw if name == "list_calendar_events" else "ok"
turn(robin, "adult", robin_phone)
shown = lib.execute_tool("list_calendar_events", {"start": "2026-10-08T00:00:00", "end": "2026-10-08T23:59:59"})
rec = " ".join(str(o.get("full") or o.get("text")) for o in lib._TURN_IO.outcomes)
check("Robin's turn on her phone: her Work shown, the id stripped from the read and the outcome record",
      ("review" in shown, "id:" in shown, "R-WORK" in rec), (True, False, False))
# Alex's phone turn whose calendar read ran on Alex's Mac (cross_device): the Mac's claims count, not the phone's.
raw = H + "8:00 AM–9:00 AM — Alex's 1:1 [calendar: Work, id: A-WORK]\n" + H + "6:00 PM–7:00 PM — Leo soccer [calendar: Leo soccer, shared, id: A-LEO]"
with store._lock:
    store._db.execute("UPDATE people SET calendars=? WHERE id=?", (json.dumps(["Work"]), alex))   # Work: three claimants
turn(alex, "admin", alex_phone, remote_dev=alex_mac)
check("the read ran on Alex's Mac", lib._read_device_id(), alex_mac)
shown = lib.execute_tool("list_calendar_events", {"start": "2026-10-08T00:00:00", "end": "2026-10-08T23:59:59"})
check("…so the Mac's id claims decide: Alex's own Work kept although the title is claimed thrice",
      ("1:1" in shown, "id:" in shown), (True, False))
turn(alex, "admin", alex_phone)
check("the same events read on the phone (no claims there): the title rule, Work dropped",
      "1:1" in lib.execute_tool("list_calendar_events", {"start": "2026-10-08T00:00:00", "end": "2026-10-08T23:59:59"}), False)
turn(alex, "admin", alex_phone, remote_dev=alex_mac)
line = raw.split("\n")[1]
check("the label comes from the device's claim: Leo's soccer is Leo's", "Leo's" in lib._attribute_calendars(line), True)
check("…and the label keeps the id for the filter to read", "id: A-LEO" in lib._attribute_calendars(line), True)

print("── #1268 (JEV): a claim narrows always, widens only for an adult's own or her kids' ──")
with store._lock:
    store._db.execute("UPDATE people SET calendars=? WHERE id=?", (json.dumps(["Work", "Robin personal"]), robin))
claims(ava_phone, ("K-MOM", "Robin personal", ava), ("K-FAM", "Family", ava))
MOM = (H + "10:00 AM–11:00 AM — Therapy [calendar: Robin personal, shared, id: K-MOM]\n"
       + H + "5:00 PM–5:30 PM — Pickup [calendar: Family, shared, id: K-FAM]")
got = seen(MOM, ava, "kid", ava_phone)
check("a kid's device claiming her parent's calendar as hers: still hidden from the kid (both rules must allow)",
      ("Therapy" in got, "Pickup" in got), (False, True))
claims(robin_phone, ("R-WORK", "Work", robin), ("R-JW", "Work", jordan), ("R-JS", "Jordan stuff", jordan))
JW = (H + "11:00 AM–11:30 AM — Jordan's standup [calendar: Work, shared, id: R-JW]\n"
      + H + "1:00 PM–2:00 PM — Jordan's lunch [calendar: Jordan stuff, shared, id: R-JS]")
got = seen(JW, robin, "adult", robin_phone)
check("Robin labels Jordan's shared calendars as Jordan's: hidden from Robin, even an unclaimed-by-title one (narrows)",
      ("standup" in got, "lunch" in got), (False, False))
check("…and the household screen never sees them either", seen(JW, screen, "household", robin_phone), "")
check("an adult's self-claim widens: Robin's own Work, a title two people claim", "review" in seen(ROBIN_READ, robin, "adult", robin_phone), True)
store.update_person(robin, role="kid")
check("the claimer's role is judged at read time (JEV): Robin as a kid now, her self-claim no longer widens",
      "review" in seen(ROBIN_READ, robin, "kid", robin_phone), False)
store.update_person(robin, role="adult")

print("── a revoked device's claims go ──")
store.revoke_device(robin_phone)
check("revoked: no claims, no owner", (store.device_calendars(robin_phone), store.device_calendar_owner(robin_phone, "R-WORK")),
      ([], None))
check("…so its Work falls back to the title rule (fail closed)", "review" in seen(ROBIN_READ, robin, "adult", robin_phone), False)

lib.turn_role = _role
lib._TURN_IO.person = lib._TURN_IO.device_id = lib._TURN_IO.remote = lib._TURN_IO.device_tools = None
lib._TURN_IO.outcomes = None
src = (Path(__file__).parent / "agent_api_server.py").read_text()
check("the API carries the Mac's device id for cross-device reads", src.count('"device_id": remote["mac"].get("device_id")'), 2)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
