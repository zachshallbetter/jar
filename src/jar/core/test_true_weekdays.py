#!/usr/bin/env python3
"""A weekday that doesn't match its date is fixed in place (truth eval 10-01, local: "Saturday, October 2, 2026" for
a Friday; #1611: no "Correction:" paragraph under the answer). Hermetic: the household clock is pinned to Thu Oct 1,
2026."""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as lib  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


lib._local_now = lambda: datetime(2026, 10, 1, 21, 0)
W = lib._with_true_weekdays

out = W("Saturday, October 2, 2026 has Ballet at 5:15 PM.")
check("a wrong weekday is fixed in place, nothing appended", out == "Friday, October 2, 2026 has Ballet at 5:15 PM.", out)
lib.begin_turn_info()
W("Saturday, October 2 at 9.")
check("…and counted for the turn log", lib._TURN_INFO.info.get("date_corrections") == 1, str(lib._TURN_INFO.info))
check("a right weekday is left alone", W("Friday, October 2 at 9 AM: Dentist.") == "Friday, October 2 at 9 AM: Dentist.")
check("abbreviations: Fri Oct 2 is right", W("• Fri Oct 2, 9:00 AM — Dentist") == "• Fri Oct 2, 9:00 AM — Dentist")
check("abbreviations: Sat Oct 2 is wrong, and stays abbreviated", W("Sat Oct 2: soccer") == "Fri Oct 2: soccer")
check("case is kept", W("SATURDAY, OCTOBER 2") == "FRIDAY, OCTOBER 2" and W("on saturday, october 2") == "on friday, october 2")
check("date-first form", W("October 13, 2026 is a Wednesday.") == "October 13, 2026 is a Tuesday.")
check("ordinals and no comma", W("Monday October 5th: the Spokane trip.") == "Monday October 5th: the Spokane trip.")
check("no year: the date nearest today (Jan 4 is 2027, a Monday)", W("Monday, January 4: school starts.") == "Monday, January 4: school starts.")
check("an explicit past year is honoured", W("Tuesday, July 4, 2023 was the parade.").endswith("July 4, 2023 is a Tuesday.") is False
      and W("Tuesday, July 4, 2023 was the parade.") == "Tuesday, July 4, 2023 was the parade.")
out = W("Saturday, October 2 and again Saturday, October 2.")
check("each mention fixed", out == "Friday, October 2 and again Friday, October 2.", out)
out = W("Thursday, October 2 and Monday, October 6.")
check("two wrong dates, both fixed", out == "Friday, October 2 and Tuesday, October 6.", out)
check("no Correction line ever", "Correction" not in W("Thursday, October 2 and Monday, October 6."))
check("an impossible date is ignored", W("Monday, February 30 is not real.") == "Monday, February 30 is not real.")
check("a month name without a weekday is ignored", W("On October 2 we have ballet.") == "On October 2 we have ballet.")
check("'Mayday' or 'Sunday May' phrasing doesn't crash", isinstance(W("Sunday May 3 is fine?"), str))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all weekday checks passed'}")
sys.exit(1 if failures else 0)
