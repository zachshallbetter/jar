#!/usr/bin/env python3
"""
Tests for naming whose event it is: Jellybean ends each list_calendar_events line with
" [calendar: <title>]" (", shared" when it isn't the user's own), and titan marks a calendar named
for another household member as theirs, so their events stay in the agenda under their name.
Hermetic: the household and the Mac are faked.

Run: python3 scripts/test_calendar_owners.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
# A throwaway household store (#1230): calendar reads are cut by its claims, and an unreadable one fails closed.
import tempfile as _tf  # noqa: E402
os.environ.setdefault("HOUSEHOLD_DB", os.path.join(_tf.mkdtemp(prefix="titan-hh-"), "household.db"))

import agent_runtime_lib as lib  # noqa: E402

PEOPLE = [{"id": "zach", "name": "Zach Hale"}, {"id": "robin", "name": "Robin"},
          {"id": "leo", "name": "Leo"}, {"id": "quinn", "name": "Quinn"}]

DAY = ("• Tue Oct 6, 11:00 AM — Team sync [calendar: Work]\n"
       "• Tue Oct 6, 3:00 PM — Quinn's appointment [calendar: Robin, shared]\n"
       "• Tue Oct 6, 5:15 PM — Ballet [calendar: robin@icloud.com, shared]\n"
       "• Tue Oct 6, 6:00 PM — Pickup [calendar: Family, shared]\n"
       "• Tue Oct 6, 7:00 PM — Dinner [calendar: Zach]\n"
       "• Tue Oct 6, 8:00 PM — Movie [calendar: Robin & Zach, shared]")

# One Family calendar, each person's part of it.
FAMILY = ("• Wed Oct 7, 8:00 AM — Zach CTO sync [calendar: Family]\n"
          "• Wed Oct 7, 2:50 PM — Pickup Leo from school [calendar: Family, shared]\n"
          "• Wed Oct 7, 5:15 PM — Robin ballet [calendar: Family, shared]\n"
          "• Wed Oct 7, 6:00 PM — Leo's skate night, Quinn's recital [calendar: Family, shared]\n"
          "• Wed Oct 7, 8:00 PM — Movie night [calendar: Family, shared]")


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def run():
    ok = True
    saved = (lib._household_people, lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed,
             lib._calendar_claim)
    lib._household_people = lambda: PEOPLE
    lib._calendar_claim = lambda title: None
    try:
        print("\n── Zach asking ──")
        lib._TURN_IO.person = {"id": "zach", "name": "Zach"}
        lines = lib._attribute_calendars(DAY).split("\n")
        ok &= check("his own calendar is untouched", lines[0], "• Tue Oct 6, 11:00 AM — Team sync [calendar: Work]")
        ok &= check("Robin's calendar is hers, and the event is about Quinn",
                    lines[1], "• Tue Oct 6, 3:00 PM — Quinn's appointment [calendar: Robin, shared — Robin's, about Quinn]")
        ok &= check("… by her address too", lines[2].endswith("[calendar: robin@icloud.com, shared — Robin's]"), True)
        ok &= check("a shared calendar named for nobody stays as it is", lines[3], "• Tue Oct 6, 6:00 PM — Pickup [calendar: Family, shared]")
        ok &= check("a calendar with his own name isn't someone else's", lines[4], "• Tue Oct 6, 7:00 PM — Dinner [calendar: Zach]")
        ok &= check("two people named: nobody claimed", lines[5], "• Tue Oct 6, 8:00 PM — Movie [calendar: Robin & Zach, shared]")
        ok &= check("an event title naming a kid doesn't make the calendar theirs", "Quinn's" in lines[1].split("[")[1], False)

        print("\n── Settings › Household says whose a calendar is ──")
        claims = {"hamilbetter@gmail.com": {"id": "robin", "name": "Robin", "role": "adult"},
                  "ava": {"id": "ava", "name": "Ava", "role": "kid"}, "work": {"id": "zach", "name": "Zach", "role": "admin"}}
        lib._calendar_claim = lambda title: claims.get(title.lower())
        ok &= check("an address no name matches, claimed in settings",
                    lib._attribute_calendars("• x [calendar: hamilbetter@gmail.com, shared]"),
                    "• x [calendar: hamilbetter@gmail.com, shared — Robin's]")
        ok &= check("the asker's own claimed calendar isn't marked", lib._attribute_calendars("• x [calendar: Work]"),
                    "• x [calendar: Work]")
        ok &= check("a claim beats name matching", lib._calendar_owner("Robin", "zach"), "Robin")
        lib._calendar_claim = lambda title: None

        print("\n── one shared calendar, each person's part ──")
        lines = lib._attribute_calendars(FAMILY).split("\n")
        ok &= check("the asker's own part", lines[0].endswith("[calendar: Family — about you]"), True)
        ok &= check("a kid's name in the title", lines[1].endswith("[calendar: Family, shared — about Leo]"), True)
        ok &= check("Robin's part", lines[2].endswith("[calendar: Family, shared — about Robin]"), True)
        ok &= check("two people", lines[3].endswith("[calendar: Family, shared — about Leo and Quinn]"), True)
        ok &= check("nobody named: the household's, untouched", lines[4], "• Wed Oct 7, 8:00 PM — Movie night [calendar: Family, shared]")
        ok &= check("the time before the title never counts", lib._event_about("• Mon — Standup [calendar: X]", "zach"), [])

        print("\n── a place note, before or after the calendar ──")
        before = "• Wed Oct 7, 12:00 PM — Dentist [at: Pearl Dental] [calendar: Robin, shared]"
        ok &= check("[at:] before: calendar still read", lib._attribute_calendars(before),
                    "• Wed Oct 7, 12:00 PM — Dentist [at: Pearl Dental] [calendar: Robin, shared — Robin's]")
        after = "• Wed Oct 7, 12:00 PM — Dentist [calendar: Robin, shared] [at: Pearl Dental]"
        ok &= check("[at:] after: the rest of the line is kept", lib._attribute_calendars(after),
                    "• Wed Oct 7, 12:00 PM — Dentist [calendar: Robin, shared — Robin's] [at: Pearl Dental]")
        house = "• Wed Oct 7, 6:00 PM — Dinner [at: Robin's house] [calendar: Family, shared]"
        ok &= check("a place naming someone doesn't make it about them", lib._attribute_calendars(house), house)
        kid = "• Wed Oct 7, 6:00 PM — Leo's skate night [at: Oaks Park Rink] [calendar: Family, shared]"
        ok &= check("the title still counts", lib._attribute_calendars(kid).endswith("[calendar: Family, shared — about Leo]"), True)

        print("\n── Robin asking ──")
        lib._TURN_IO.person = {"id": "robin", "name": "Robin"}
        lines = lib._attribute_calendars(DAY).split("\n")
        ok &= check("her own calendar isn't marked as someone else's", lines[1].endswith("[calendar: Robin, shared — about Quinn]"), True)
        ok &= check("Zach's is his", lines[4].endswith("[calendar: Zach — Zach's]"), True)
        ok &= check("on Robin's phone, her part is hers",
                    lib._attribute_calendars(FAMILY).split("\n")[2].endswith("[calendar: Family, shared — about you]"), True)

        print("\n── older Jellybean, and the device path ──")
        lib._TURN_IO.person = None
        old = "• Tue Oct 6, 3:00 PM — Quinn's appointment\n• Tue Oct 6, 6:00 PM — Pickup [2 copies on Home]"
        ok &= check("lines without the note are left alone", lib._attribute_calendars(old), old)
        ok &= check("no person: the admin (Zach) is asking",
                    lib._attribute_calendars("• x [calendar: Robin, shared]"), "• x [calendar: Robin, shared — Robin's]")
        lib._device_bridge_enabled = lambda: True
        lib._device_tool_allowed = lambda n: True
        lib._device_request = lambda name, args: DAY
        out = lib.execute_tool("list_calendar_events", {"start": "2026-10-06", "end": "2026-10-06"})
        ok &= check("list_calendar_events results are attributed", "— Robin's]" in out, True)
        lib._device_request = lambda name, args: "• Tue Oct 6 — Note [calendar: Robin, shared]"
        ok &= check("other tools aren't touched", lib.execute_tool("list_reminders", {}), "• Tue Oct 6 — Note [calendar: Robin, shared]")
        prompt = lib.build_tool_system_prompt()
        ok &= check("the prompt says to keep their events under their name", "never as the user's own" in prompt, True)
        ok &= check("… and to ask before touching someone else's", "ask before touching someone else's event" in prompt, True)
        skill = open(os.path.join(os.path.dirname(__file__), "skills", "calendar", "SKILL.md")).read()
        ok &= check("the calendar skill no longer forbids deletes", "Never delete events" in skill, False)
        ok &= check("… and covers shared calendars", ("## Whose event it is" in skill, "A shared calendar changes for everyone" in skill),
                    (True, True))
    finally:
        (lib._household_people, lib._device_request, lib._device_bridge_enabled, lib._device_tool_allowed,
         lib._calendar_claim) = saved
        lib._TURN_IO.person = None

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
