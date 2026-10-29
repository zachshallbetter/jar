#!/usr/bin/env python3
# gate: hermetic
"""The family meeting's agenda (jellybean#425): the week day by day as the household may see it, the kids' hand-offs,
what needs a driver, and last week's household notes. Built in code; a private event is "busy", never its title.

Run: python3 scripts/test_family_meeting.py
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")
os.environ["HOUSEHOLD_PREFS"] = os.path.join(tempfile.mkdtemp(), "household.json")
os.environ["FAMILY_MEETING_DB"] = os.path.join(tempfile.mkdtemp(), "family_meeting.db")
os.environ["PROACTIVE_DB"] = os.path.join(tempfile.mkdtemp(), "proactive.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import family_meeting as F  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


PT = ZoneInfo("America/Los_Angeles")
PEOPLE = [{"id": "alex", "name": "Alex", "role": "admin", "calendars": ["Alex"]},
          {"id": "robin", "name": "Robin", "role": "adult", "calendars": ["Robin"]},
          {"id": "ava", "name": "Ava", "role": "kid", "calendars": ["Ava"], "other_parent": "their dad"}]


def node(title, cal, start, end, aud="private"):
    return {"node_id": title + start, "data": {"title": title, "calendar": cal, "start": start, "end": end,
                                               "provenance": {"owner": "alex", "audience": aud}}}


WEEK = date(2026, 10, 5)   # a Monday
nodes = [
    node("Garbage night", "Home", "2026-10-07T21:00:00-07:00", "2026-10-07T21:15:00-07:00"),
    node("Ballet", "Ava", "2026-10-05T17:15:00-07:00", "2026-10-05T18:30:00-07:00"),
    node("Board meeting", "Alex", "2026-10-05T16:30:00-07:00", "2026-10-05T18:00:00-07:00"),
    node("Therapy", "Robin", "2026-10-05T16:45:00-07:00", "2026-10-05T18:00:00-07:00"),
]
HOUSE = {"home"}


def stays(kid, at):
    if kid == "ava" and at < datetime(2026, 10, 8, 9, 0, tzinfo=PT):
        return {"state": "away", "until": datetime(2026, 10, 8, 9, 0, tzinfo=PT), "then": "here"}
    return {"state": "here", "until": datetime(2026, 10, 15, 9, 0, tzinfo=PT), "then": "away"}


timeline = [
    {"timestamp": "2026-10-01T18:00:00Z", "payload": {"text": "Plumber fixed the kitchen sink", "audience": "household"}},
    {"timestamp": "2026-10-01T19:00:00Z", "payload": {"text": "Booked the dentist", "audience": "private", "person": "robin"}},
    {"timestamp": "2026-10-02T19:00:00Z", "payload": {"text": "Downloaded x.pdf", "audience": "household", "source": "downloads"}},
    {"timestamp": "2026-09-20T19:00:00Z", "payload": {"text": "Old household note", "audience": "household"}},
]
a = F.agenda(WEEK, nodes, PEOPLE, PT, HOUSE, stays, timeline=timeline, custody_line="Kids: …")
mon, wed, thu = a["days"][0], a["days"][2], a["days"][3]

print("\n── the week, as the household may see it ──")
check("seven days from Monday", (len(a["days"]), a["days"][0]["label"], a["title"]), (7, "Mon Oct 5", "Week of Oct 5"))
check("a person's own events are 'busy', with whose they are", [e["text"] for e in mon["events"]],
      ["4:30 PM Alex: busy", "4:45 PM Robin: busy", "5:15 PM Ava: busy"])
check("a household-calendar event shows its title", any("Garbage night" in e["text"] for e in wed["events"]), True)

print("\n── what needs sorting out ──")
check("Ava's event with both grown-ups busy: a driver question, no private titles",
      [s["text"] for s in a["sort_out"]], ["Ava has something at 5:15 PM, and nobody's free to drive yet. Who's taking Ava?"])
check("its day", a["sort_out"][0]["day"], "Mon Oct 5")
check("no one's own overlap on the shared screen", all(s["need"] == "driver" for s in a["sort_out"]), True)

print("\n── the kids' hand-offs ──")
check("Thursday 9 AM: Ava home", thu["handoffs"], [{"at": "9 AM", "text": "9 AM Ava home"}])
check("no hand-off on a day without one", mon["handoffs"], [])
check("the custody line rides along", a["kids"], "Kids: …")

print("\n── last week: the household's own notes ──")
check("household notes only, not a person's, not a connector's, not older", a["last_week"], ["Plumber fixed the kitchen sink"])

print("\n── a shared screen leaves the kids' stays out until Alex says ──")
os.environ.pop("FAMILY_SCREEN_CUSTODY", None)
check("off by default", F.custody_on_shared(), False)
os.environ["FAMILY_SCREEN_CUSTODY"] = "1"
check("on when set", F.custody_on_shared(), True)
os.environ.pop("FAMILY_SCREEN_CUSTODY", None)

print("\n── which week a meeting plans ──")
check("Sunday plans tomorrow's week", F.week_of(date(2026, 10, 4)), date(2026, 10, 5))
check("Monday plans this week", F.week_of(date(2026, 10, 5)), date(2026, 10, 5))
check("Wednesday plans the coming one", F.week_of(date(2026, 10, 7)), date(2026, 10, 12))

print("\n── the route is everyone's (household items only) ──")
import household_identity as hh  # noqa: E402
check("a shared device and a kid may read it", (hh.route_allowed("household", "GET", "/v1/family-meeting"),
                                               hh.route_allowed("kid", "GET", "/v1/family-meeting")), (True, True))

print("\n── the phones answer: who's driving ──")
import proactive  # noqa: E402
check("one card per open question, to the adults", F.ask_cards(a), 1)
cards = proactive.list_cards(include_acked=True)
c = cards[0]
check("the card: whose, the question, I will / Not me", (c["title"], c["to"], [x["label"] for x in c["actions"]], c["source"]),
      ("Mon Oct 5: who's taking Ava?", "adults", ["I will", "Not me"], "family-meeting"))
nope = proactive.card_action(c["id"], c["actions"][1]["id"], person="robin", role="adult")
check("Robin: not me, for her only", (nope["ok"], nope["said"], proactive.visible_to(c["to"], "alex", "admin")),
      (True, "Noted: not you.", True))
kid = F.answer(c["actions"][0]["value"], "ava", "kid")
check("a kid can't take a drive", kid[:2], (False, False))
yes = proactive.card_action(c["id"], c["actions"][0]["id"], person="alex", role="admin")
check("Alex: I will, settled for every adult", (yes["ok"], yes["said"]), (True, "Got it: you're driving."))
check("the card is done for everyone", next(x for x in proactive.list_cards(include_acked=True) if x["id"] == c["id"])["acked_at"] is not None, True)
check("a second taker is told", F.answer(c["actions"][0]["value"], "robin", "adult")[2], "Someone's already taking that one.")
a2 = F.agenda(WEEK, nodes, PEOPLE, PT, HOUSE, stays, timeline=timeline, answers=F.answers_for(WEEK.isoformat()))
s2 = a2["sort_out"][0]
check("the agenda shows who's driving, and who passed", (s2["text"], s2["taken_by"], s2["passed"]),
      ("Alex's taking Ava (something at 5:15 PM).", "Alex", ["Robin"]))
check("a settled question isn't asked again", F.ask_cards(a2, send=lambda *x, **k: None), 0)
check("a stray button value", F.answer("delete everything", "alex", "admin")[0], False)

print("\n── Sunday 6 PM: the meeting starts ──")
sent = []
r = F.start_meeting(F.agenda(WEEK, nodes, PEOPLE, PT, HOUSE, stays, timeline=timeline),
                    send=lambda *a_, **k: sent.append((a_, k)) or {"id": f"pc-{len(sent)}"})
meeting = [x for x in sent if x[1].get("to") == "household"]
check("one household card, the questions to the adults", (len(meeting), r["asked"], [x[1]["to"] for x in sent][0]), (1, 1, "adults"))
check("the card says only counts the household may see", meeting[0][0], ("Family meeting: the week of Oct 5",
      "2 days with plans, 1 thing to sort out (the grown-ups have the questions on their phones)."))
check("its button opens the week", meeting[0][1]["actions"][0]["label"], "Open the week")
check("…and a client with the meeting screen opens it; others still ask (jellybean#425)",
      (meeting[0][1]["actions"][0]["kind"], meeting[0][1]["actions"][0]["open"]), ("prompt", "family-meeting"))
import proactive  # noqa: E402
import home_screen  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
clean = proactive._clean_actions(meeting[0][1]["actions"] + [{"label": "Odd", "kind": "prompt", "open": "settings"}])
check("the hint is kept for a known screen, dropped for any other", ([a.get("open") for a in clean], clean[0]["kind"]),
      (["family-meeting", None], "prompt"))
card = {"id": "pc-9", "title": "Family meeting", "kind": "info", "actions": clean}
check("the TV's Home shelf carries it", [a.get("open") for a in home_screen.shelf_block([card])[0]["actions"]],
      ["family-meeting", None])
check("so do a reply's action blocks", [a.get("open") for a in lib.action_blocks([card])[0]["actions"]], ["family-meeting", None])
import pathlib  # noqa: E402
timer = (pathlib.Path(__file__).resolve().parent.parent / "server-setup" / "titan-family-meeting.timer").read_text()
check("the timer: Sundays at 6 PM household time, no catch-up", ("OnCalendar=Sun *-*-* 18:00 America/Los_Angeles" in timer,
                                                                "Persistent=false" in timer), (True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
