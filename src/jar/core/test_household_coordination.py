#!/usr/bin/env python3
"""
Tests for the household coordination card (scripts/household_coordination.py): whose each synced event
is, one person's overlapping events, a kid's event when every grown-up is busy, and the cards (a person's
overlap to them, the driver question to the adults with private events as "busy"; keyed by day, with the
conflicts as data). Hermetic: synced events and the household are faked.

Run: python3 scripts/test_household_coordination.py
"""
import json
import os
import sys
from datetime import date
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("AGENT_TZ", "America/Los_Angeles")

import household_coordination as hc  # noqa: E402

PEOPLE = [{"id": "zach", "name": "Zach", "role": "admin", "age": None, "calendars": []},
          {"id": "robin", "name": "Robin", "role": "adult", "age": None, "calendars": ["hamilbetter@gmail.com"]},
          {"id": "leo", "name": "Leo", "role": "kid", "age": 6, "calendars": []},
          {"id": "ava", "name": "Ava", "role": "kid", "age": 12, "calendars": []},
          {"id": "jordan", "name": "Jordan", "role": "adult", "age": 22, "calendars": []}]
DAY = date(2026, 10, 7)   # a Wednesday; Pacific is UTC-7


def node(title, start, end, calendar, owner="mine", all_day=False, location=None):
    d = {"title": title, "start": start, "end": end, "calendar": calendar, "calendar_owner": owner, "all_day": all_day}
    if location:
        d["location"] = location
    return {"node_id": title, "data": d}


NODES = [
    node("Zach CTO", "2026-10-07T21:00:00Z", "2026-10-07T22:00:00Z", "Zach"),                       # 2:00–3:00 PM
    node("Pickup Leo from school", "2026-10-07T21:50:00Z", "2026-10-07T22:15:00Z", "Zach",
         location="Sunnyside Elementary"),                                                           # 2:50 PM
    node("Pickup Leo from school", "2026-10-07T21:50:00Z", "2026-10-07T22:15:00Z", "Family", "shared"),  # the copy
    node("Ballet", "2026-10-08T00:15:00Z", "2026-10-08T01:15:00Z", "Ava"),                          # 5:15 PM, someone's free
    node("Ava soccer", "2026-10-08T01:00:00Z", "2026-10-08T02:00:00Z", "Ava"),                     # 6:00 PM
    node("Dinner with Tim", "2026-10-08T00:45:00Z", "2026-10-08T02:00:00Z", "Zach"),                 # 5:45–7:00 PM
    node("Book club", "2026-10-08T00:30:00Z", "2026-10-08T02:00:00Z", "hamilbetter@gmail.com", "shared"),  # Robin
    node("Work shift", "2026-10-07T23:00:00Z", "2026-10-08T03:00:00Z", "Jordan", "shared"),         # 4:00–8:00 PM
    node("Kids with Mom", "2026-10-05T22:00:00Z", "2026-10-09T16:00:00Z", "Family", "shared"),       # a state, 3 days
    node("Ava's birthday", "2026-10-07T00:00:00Z", "2026-10-08T00:00:00Z", "Birthdays", all_day=True),
    node("Movie night", "2026-10-08T03:00:00Z", "2026-10-08T05:00:00Z", "Family", "shared"),         # the household's
]
# the calendars the adults designated as the household's (world.household_calendars); "shared" alone isn't
HOUSEHOLD = {"hamilbetter@gmail.com", "family"}


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def run():
    ok = True
    tz = hc._tz()
    print("\n── whose each event is ──")
    # jellybean#247 (Zach, 10-01): Zach hid "Ava" and "Zach" on his Mac. Ava's calendar, assigned to her, still
    # counts; his own hidden calendar doesn't.
    hid = [dict(p) for p in PEOPLE]
    hid[0] = {**hid[0], "calendars_ignored": ["Ava", "Zach"]}
    hid[3] = {**hid[3], "calendars": ["Ava"]}
    seen = {e.title for e in hc.events_for(DAY, NODES, hid, tz, HOUSEHOLD)}
    ok &= check("hidden on Zach's Mac: Ava's assigned calendar still counts, his own hidden one doesn't (#247)",
                ("Ava soccer" in seen, "Ballet" in seen, "Zach CTO" in seen), (True, True, False))
    evs = {e.title: e for e in hc.events_for(DAY, NODES, PEOPLE, tz, HOUSEHOLD)}
    ok &= check("the pickup counts once", sorted(evs), sorted(["Zach CTO", "Pickup Leo from school", "Ballet",
                                                               "Ava soccer", "Dinner with Tim", "Book club",
                                                               "Work shift", "Movie night"]))
    ok &= check("the user's own calendar, naming no one", evs["Zach CTO"].people, ["Zach"])
    ok &= check("a pickup on Zach's calendar: Zach's, about Leo", evs["Pickup Leo from school"].people, ["Zach", "Leo"])
    ok &= check("a calendar named for a kid", evs["Ballet"].people, ["Ava"])
    ok &= check("a calendar Settings gives to Robin", evs["Book club"].people, ["Robin"])
    ok &= check("a shared calendar naming no one: the household's", evs["Movie night"].people, [])
    ok &= check("local times", evs["Zach CTO"].start.strftime("%H:%M"), "14:00")
    ok &= check("a place comes along", evs["Pickup Leo from school"].location, "Sunnyside Elementary")

    print("\n── what to sort out ──")
    found = hc.conflicts(list(evs.values()), PEOPLE)
    needs = sorted((c["need"], c["who"]) for c in found)
    ok &= check("one overlap and one driver", needs, [("driver", "Ava"), ("overlap", "Zach")])
    over = next(c for c in found if c["need"] == "overlap")
    ok &= check("the overlap says both", over["text"], "Zach: Zach CTO (2:00 PM–3:00 PM) runs into Pickup Leo from school (2:50 PM).")
    ok &= check("… with refs for the phone", (over["event"]["title"], over["with"]["title"], over["with"]["at"]),
                ("Zach CTO", "Pickup Leo from school", "Sunnyside Elementary"))
    drv = next(c for c in found if c["need"] == "driver")
    # The card goes to every adult: a grown-up's event on their own calendar is just "busy"; one on a
    # shared calendar is named (audience.py).
    ok &= check("the driver question names who's busy, titles only from household calendars", drv["busy"],
                {"Zach": "busy", "Robin": "Book club", "Jordan": "busy"})
    ok &= check("… Jordan's shift, on a calendar only shared *with* Zach, stays 'busy'", "Work shift" in drv["text"], False)
    ok &= check("… and the text never names a private event", ("Dinner with Tim" in drv["text"], "Zach is busy" in drv["text"]),
                (False, True))
    ok &= check("… nor its data", drv["with"]["title"] in ("busy", "Book club"), True)
    shared_nodes = [dict(n, data={**n["data"], "provenance": {"owner": "zach", "audience": "adults"}})
                    if n["data"]["title"] == "Dinner with Tim" else n for n in NODES]
    drv2 = next(c for c in hc.conflicts(hc.events_for(DAY, shared_nodes, PEOPLE, tz, HOUSEHOLD), PEOPLE) if c["need"] == "driver")
    ok &= check("an event shared with the adults is named", drv2["busy"]["Zach"], "Dinner with Tim")
    # A colleague-organized meeting: the Mac calls its calendar "shared", but it's Zach's work, never the
    # household's, so Robin's card says he's busy and never what with.
    # (on Zach's own calendar; the Mac marks it "shared" because someone else organized it)
    work = [node("Acme roadmap review", "2026-10-08T00:45:00Z", "2026-10-08T02:00:00Z", "Zach", "shared")
            if n["data"]["title"] == "Dinner with Tim" else n for n in NODES]
    drv3 = next(c for c in hc.conflicts(hc.events_for(DAY, work, PEOPLE, tz, HOUSEHOLD), PEOPLE) if c["need"] == "driver")
    ok &= check("a colleague-organized meeting never reaches the adults' card",
                ("Acme" in drv3["text"], "Acme" in json.dumps(drv3)), (False, False))
    ok &= check("… nor with the default list (only the seeded household calendar widens)",
                "Acme" in json.dumps([c for c in hc.conflicts(hc.events_for(DAY, work, PEOPLE, tz), PEOPLE)]), False)
    ok &= check("… and asks, using the kid's name", drv["text"].endswith("Who's taking Ava?"), True)
    ok &= check("Ballet isn't flagged: a grown-up is free then", any(c["event"]["title"] == "Ballet" for c in found), False)

    print("\n── the card ──")
    sent = []

    def send(title, detail, **kw):
        sent.append((title, detail, kw))
        return {"id": "c1"}
    r = hc.run(DAY, nodes=NODES, people=PEOPLE, send=send)
    # A person's own overlap goes to them only; the driver question to the adults; same day key.
    ok &= check("two cards: Zach's overlap, the adults' driver question", len(sent), 2)
    by_to = {json.dumps(kw["to"]): (title, detail, kw) for title, detail, kw in sent}
    ok &= check("audiences", sorted(by_to), sorted(['"adults"', '["zach"]']))
    ok &= check("both keyed by day", {kw["key"] for _, _, kw in sent}, {"coordination-2026-10-07"})
    title, detail, kw = by_to['["zach"]']
    ok &= check("Zach's card: titled by the day", title, "Wednesday: 1 thing to sort out")
    ok &= check("… his overlap only", ("Zach CTO" in detail, "Ava" in detail), (True, False))
    title, detail, kw = by_to['"adults"']
    ok &= check("the adults' card: the driver question only", ("Who's taking Ava?" in detail, "Zach CTO" in detail),
                (True, False))
    ok &= check("no data field when add_card has none", "data" in kw, False)
    sent.clear()

    def send_data(title, detail, data=None, **kw):
        sent.append(data)
        return {"id": "c2"}
    hc.run(DAY, nodes=NODES, people=PEOPLE, send=send_data)
    ok &= check("structured conflicts when add_card takes data, per card",
                sorted(c["need"] for d in sent for c in d["conflicts"]), ["driver", "overlap"])
    ok &= check("… without the text", "text" in sent[0]["conflicts"][0], False)
    many = [{"who": "Zach", "event": {"title": "x" * 300, "start": "s"}, "with": {"title": "y" * 300, "start": "s"},
             "need": "overlap", "text": "t"}] * 12
    data = hc.card_data(many)
    ok &= check("data fits a card's 4 KB, saying how many didn't",
                (len(json.dumps(data)) <= hc.DATA_MAX_BYTES, data["more"] + len(data["conflicts"])), (True, 12))
    sent.clear()
    calm = [n for n in NODES if n["data"]["title"] not in ("Zach CTO", "Dinner with Tim")]
    r = hc.run(DAY, nodes=calm, people=PEOPLE, send=send)
    ok &= check("a calm day sends nothing", (r["conflicts"], sent), ([], []))
    r = hc.run(DAY, nodes=NODES, people=PEOPLE, send=send, dry_run=True)
    ok &= check("a dry run sends nothing", (len(r["conflicts"]), sent), (2, []))

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
