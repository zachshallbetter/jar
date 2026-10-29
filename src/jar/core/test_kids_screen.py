#!/usr/bin/env python3
# gate: hermetic
"""The kids' screen (jellybean#422) and a device's room (jellybean#424): each kid's household chores for today, the
checkmark (the one write a shared screen gets), the streak, where they sleep only with custody allowed; a device
naming its own room only.

Run: python3 scripts/test_kids_screen.py
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kids-test-"))
os.environ["WORLD_DIR"] = str(_TMP / "world")
os.environ["KIDS_CHORES_DB"] = str(_TMP / "kids.db")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["AGENT_TZ"] = "America/Los_Angeles"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kids_screen as K  # noqa: E402
import world as W  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


PT = ZoneInfo("America/Los_Angeles")
PEOPLE = [{"id": "zach", "name": "Zach", "role": "admin"},
          {"id": "p_ava", "name": "Ava", "role": "kid", "other_parent": "their dad"},
          {"id": "p_leo", "name": "Leo", "role": "kid", "other_parent": "their dad"}]
w = W.Bubbles(_TMP / "world")
for name, sched, who in (("Feed the cat", "every day 7 AM", "Ava"), ("Recycling to the curb", "Wednesday nights", "Leo"),
                         ("Make the bed", "daily", "Leo")):
    W.set_fact(w, name, "schedule", sched, "zach", "admin", etype="activity", people=PEOPLE, household=True)
    W.set_fact(w, name, "assignee", who, "zach", "admin", etype="activity", people=PEOPLE)
W.set_fact(w, "Take out garbage", "schedule", "Wednesday nights", "zach", "admin", etype="activity", people=PEOPLE, household=True)

WED = datetime(2026, 10, 7, 18, 0, tzinfo=PT)
away_leo = lambda kid, at: ({"state": "away", "until": datetime(2026, 10, 8, 9, tzinfo=PT), "then": "here"}   # noqa: E731
                              if kid == "p_leo" else {"state": "here", "until": None})
chores_none = lambda d: K.kid_chores(w, d, PEOPLE, None, zone=PT)   # noqa: E731
chores_cust = lambda d: K.kid_chores(w, d, PEOPLE, away_leo, zone=PT)   # noqa: E731

print("\n── whose chores, today ──")
today = chores_none(WED.date())
check("Ava's: hers only", [c["text"] for c in today["p_ava"]], ["Feed the cat"])
check("Leo's: timed first", [(c["text"], c["at"]) for c in today["p_leo"]],
      [("Recycling to the curb", "9 PM"), ("Make the bed", None)])
check("an unassigned household chore isn't anyone's", any(c["text"] == "Take out garbage" for v in today.values() for c in v), False)
check("Thursday: no recycling", [c["text"] for c in chores_none(date(2026, 10, 8))["p_leo"]], ["Make the bed"])
check("with custody: a kid away has none that day", chores_cust(WED.date())["p_leo"], [])

print("\n── the checkmark: the one write ──")
cat = today["p_ava"][0]["id"]
check("a household screen checks it off", K.mark(cat, True, WED, PEOPLE, chores_none, "household", "household", "tv1")[0], 200)
check("again: the same (idempotent)", K.mark(cat, True, WED, PEOPLE, chores_none, "household", "household", "tv1")[1]["done"], True)
check("a kid can't check off someone else's", K.mark(cat, True, WED, PEOPLE, chores_none, "p_leo", "kid", "ipad")[0], 403)
check("an adult's device isn't how (household, the kid, the admin)", K.mark(cat, True, WED, PEOPLE, chores_none, "p_mir", "adult", "x")[0], 403)
check("not due today: 404", K.mark("ch-nope", True, WED, PEOPLE, chores_none, "household", "household", "tv1")[0], 404)
con = K._db()
log = con.execute("SELECT chore, kid, device, person FROM log").fetchall()
con.close()
check("every call is logged, with device and person", log[0], (cat, "p_ava", "tv1", "household"))

print("\n── the screen ──")
s = K.build(WED, PEOPLE, chores_none)
ava = next(k for k in s["kids"] if k["name"] == "Ava")
check("done shows; nothing next once all done", ([c["done"] for c in ava["chores"]], ava["next"]), ([True], None))
leo = next(k for k in s["kids"] if k["name"] == "Leo")
check("next undone, with time", leo["next"], "Recycling to the curb · 9 PM")
check("no custody: no sleeps line", "sleeps" in leo, False)
s2 = K.build(WED, PEOPLE, chores_cust, state_at=away_leo)
c2 = next(k for k in s2["kids"] if k["name"] == "Leo")
check("custody allowed: where they sleep, and the change", c2["sleeps"], "At their dad's tonight; home from 9 AM tomorrow")
check("never an adult on it", [k["name"] for k in s["kids"]], ["Ava", "Leo"])

print("\n── the streak ──")
for back in (1, 2, 3):
    d = WED - timedelta(days=back)
    for c in chores_none(d.date())["p_ava"]:
        K.mark(c["id"], True, d, PEOPLE, chores_none, "household", "household", "tv1")
check("four days in a row (today included, all done)", K.streak("p_ava", WED.date(), lambda d: chores_none(d)["p_ava"]), 4)
check("today not done yet doesn't break it", K.streak("p_leo", WED.date(), lambda d: chores_none(d)["p_leo"]), 0)

print("\n── the ritual a chore belongs to (titan-node#790) ──")
said = W.set_fact(w, "Make the bed", "ritual", "Mornings, before school", "zach", "admin", people=PEOPLE)
check("an adult sets it, in their words", (said[0], said[2]), (True, "ok"))
check("…and it's stored as the TV reads it", next(c.get("ritual") for c in chores_none(WED.date())["p_leo"]
                                                   if c["text"] == "Make the bed"), "morning")
W.set_fact(w, "Feed the cat", "ritual", "bedtime", "zach", "admin", people=PEOPLE)
W.set_fact(w, "Feed the cat", "ritual", "before bed", "zach", "admin", people=PEOPLE)
check("a later value replaces it (one ritual per chore)",
      [c.get("ritual") for c in chores_none(WED.date())["p_ava"]], ["bedtime"])
check("neither morning nor bedtime: refused", W.set_fact(w, "Make the bed", "ritual", "lunchtime", "zach", "admin",
                                                          people=PEOPLE)[2], "invalid")
check("both at once: refused", W.set_fact(w, "Make the bed", "ritual", "morning or night", "zach", "admin",
                                          people=PEOPLE)[2], "invalid")
check("a kid's say-so waits for a grown-up", W.set_fact(w, "Recycling to the curb", "ritual", "morning", "p_leo", "kid",
                                                         people=PEOPLE)[2], "proposed")
leo_r = {c["text"]: c.get("ritual") for c in chores_none(WED.date())["p_leo"]}
check("…so the TV doesn't see it yet; without one, the key is absent (the time split stays)",
      (leo_r["Recycling to the curb"], "ritual" in next(c for c in chores_none(WED.date())["p_leo"]
                                                         if c["text"] == "Recycling to the curb")), (None, False))
s3 = K.build(WED, PEOPLE, chores_none)
check("GET /v1/home/kids carries it on each chore",
      {c["text"]: c.get("ritual") for k in s3["kids"] for c in k["chores"]},
      {"Feed the cat": "bedtime", "Recycling to the curb": None, "Make the bed": "morning"})
check("the chore's keys: only id, text, at, done and ritual",
      {k_ for k in s3["kids"] for c in k["chores"] for k_ in c} <= {"id", "text", "at", "done", "ritual"}, True)
check("asking 'is it part of the morning?' finds the ritual", W.attr_for("activity", "part of the morning"), "ritual")

print("\n── a device names its own room (jellybean#424) ──")
import household_identity as hh  # noqa: E402
st = hh.get_store()
st._db.execute("INSERT INTO devices (id, person_id, name, platform, token_sha256, created_at) VALUES ('tv1','zach','TV','tvos','t1',0)")
check("set", st.set_device_room("tv1", "Living room"), "Living room")
check("shows in people & devices", next(d for p in st.people() for d in p["devices"] if d["id"] == "tv1")["room"], "Living room")
try:
    st.set_device_room("tv1", "<script>")
    check("odd text refused", False, True)
except hh.PairingError:
    check("odd text refused", True, True)
check("null clears", st.set_device_room("tv1", None), None)
check("routes: the TV may PATCH its own row and check off chores; an adult can't check off",
      (hh.route_allowed("household", "PATCH", "/v1/devices/self"), hh.route_allowed("household", "POST", "/v1/home/kids/chores/ch-x/done"),
       hh.route_allowed("adult", "POST", "/v1/home/kids/chores/ch-x/done"), hh.route_allowed("kid", "GET", "/v1/home/kids")),
      (True, True, False, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
