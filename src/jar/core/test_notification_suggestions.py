#!/usr/bin/env python3
# gate: hermetic   (temp stores; the model stubbed; no card sent)
"""Which triaged mail reaches the suggestions pass (Coordination, 10-09, from the 10-08/09 coverage audit): personal,
action, record and now notifications always; newsletters and marketing only with an invite, from a sender the owner
has replied to in that thread, or naming one of their calendar titles. The rule (worth_suggesting), who counts as a
correspondent (mail_server.correspondents), the server path (run_suggestions) and the Mac path (dropped_worth), with
the three cases asked for: a booking confirmation (a candidate), a promo newsletter (none), a newsletter from a sender
the owner has written to (a candidate). Fictional people and senders only.
Run: python3 scripts/test_notification_suggestions.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-notif-sugg-"))
for k, v in {"MAIL_SUGGESTIONS_DB": "sugg.db", "MAIL_SERVER_DB": "mail_server.db", "MAIL_TRIAGE_DB": "triage.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_DB": "household.db", "LINKS_DB": "links.db",
             "LINK_SALT_FILE": "salt"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import mail_server as MSV  # noqa: E402
import mail_server_passes as P  # noqa: E402
import mail_suggestions as MS  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── the rule ──")
W = MS.worth_suggesting
check("personal, action, record, notification: always", [W(c, "x") for c in ("personal", "action", "record", "notification")],
      [True] * 4)
check("untriaged (triage off): as before", W(None, "x"), True)
check("code: never", W("code", "x"), False)
check("a newsletter on its own: no", W("newsletter", "Fall sale: 30% off everything"), False)
check("…with an invite in it: yes", W("newsletter", "Open house", has_ics=True), True)
check("…from a sender the owner replied to: yes", W("newsletter", "Board update", sender_known=True), True)
check("marketing naming one of the owner's calendar titles: yes", W("marketing", "Your Lakeside Clinic visit",
      cal_titles=["Lakeside Clinic checkup"]), True)
check("…a generic word in common isn't enough", W("marketing", "Meeting room deals", cal_titles=["Team meeting"]), False)

print("── who the owner has written to ──")
con = MSV._db(_TMP / "mail_server.db")


def row(uid, folder, sender, thread):
    con.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, sender, "
                "subject, sent_at) VALUES ('alex-icloud', ?, 1, ?, 'alex', ?, ?, ?, ?, 'x', '2026-10-01T10:00:00Z')",
                (folder, uid, f"cx-{uid}", f"<m{uid}@x>", thread, sender))


row(1, "INBOX", "Hale Arts <news@halearts.example>", "t-hale")
row(2, "Sent Messages", "Alex <alex@example.invalid>", "t-hale")
row(3, "INBOX", "Shop <deals@shop.example>", "t-shop")
con.commit()
known = MSV.correspondents(con, "alex-icloud")
check("a sender in a thread the owner replied in is known; a promo sender isn't; the owner isn't their own correspondent",
      ("news@halearts.example" in known, "deals@shop.example" in known), (True, False))
con.close()
check("…the Mac path's view of the same, by owner", "news@halearts.example" in MSV.correspondents_of("alex"), True)
check("an address from a From header", MSV.address_of("Hale Arts <News@HaleArts.example>"), "news@halearts.example")

print("── the server path (run_suggestions) ──")
BOOKING = ("Your reservation is confirmed", "Hi Alex, your table for 4 at Corner Bistro is confirmed for Saturday, "
           "October 10 at 7:00 PM. See you then! Reservation #48213.")
PROMO = ("Fall sale: 30% off everything", "Shop our fall sale this Saturday, October 10 from 9:00 AM. Doors open early, "
         "join us for the event!")
FROM_THEM = ("Board meeting moved", "The board meeting is now on Saturday, October 10 at 6:00 PM at the gallery. "
             "Please confirm you can attend.")
seen: list = []
real_run = MS.run
MS.run = lambda apply, sources, text_of, llm, events, con, now, roles, send=None, **k: (
    seen.extend(s["title"] for s in sources), {"candidates": len(sources), "suggested": []})[1]


def item(t, cat, **kw):
    return {"item_id": f"cx-{t[:6]}", "owner": "alex", "title": t[0], "text": t[1], "md": {}, "stamp": "s",
            "category": cat, **kw}


st = P.run_suggestions([item(BOOKING, "notification", ics=False, sender_known=False),
                        item(PROMO, "newsletter", ics=False, sender_known=False),
                        item(FROM_THEM, "newsletter", ics=False, sender_known=True)],
                       llm=lambda p: "{}", events=[], send=lambda *a, **k: None, roles={"alex": "admin"},
                       now=datetime(2026, 10, 7, 9, 0), db=_TMP / "sugg.db")
check("a booking confirmation (notification) is a candidate", BOOKING[0] in seen, True)
check("a promo newsletter isn't (no card can come of it)", PROMO[0] in seen, False)
check("a newsletter from a sender the owner replied to is a candidate", FROM_THEM[0] in seen, True)
check("…two candidates in all", st["candidates"], 2)
MS.run = real_run

print("── the Mac path (dropped_worth) ──")
src = [{"source_id": "a", "title": BOOKING[0], "triaged": "notification", "metadata": {"owner": "alex"}, "sender": ""},
       {"source_id": "b", "title": PROMO[0], "triaged": "newsletter", "metadata": {"owner": "alex"},
        "sender": "Shop <deals@shop.example>"},
       {"source_id": "c", "title": FROM_THEM[0], "triaged": "newsletter", "metadata": {"owner": "alex"},
        "sender": "Hale Arts <news@halearts.example>"},
       {"source_id": "d", "title": "Lakeside Clinic: new hours", "triaged": "marketing", "metadata": {"owner": "alex"},
        "sender": "Clinic <info@clinic.example>"}]
ev = [{"data": {"title": "Lakeside Clinic checkup", "provenance": {"owner": "alex"}}}]
got = [s["source_id"] for s in MS.dropped_worth(src, ev, known=lambda o: {"news@halearts.example"},
                                                text_of=lambda i: "")]
check("notification in; promo out; a correspondent's newsletter in; marketing naming a calendar title in", got,
      ["a", "c", "d"])
got = [s["source_id"] for s in MS.dropped_worth(src, [], known=lambda o: set(), text_of=lambda i: "")]
check("with no correspondents and no calendar: only the notification", got, ["a"])

print("── the event words a plan uses (10-09 audit) ──")
FAMILIES = {
    "a time of day": ("Members' evening", "Join us for the members' evening on Saturday, October 10 at 6:00 PM."),
    "a meal": ("Dinner Saturday", "Dinner with the Hales is Saturday, October 10 at 7:00 PM at their place."),
    "a gathering": ("Neighborhood gathering", "The block gathering is Saturday, October 10 at 3:00 PM in the park."),
    "a ceremony": ("Leo's ceremony", "The award ceremony is Friday, October 9 at 5:30 PM in the school hall."),
    "a performance": ("Ava's performance", "Ava's dance performance is Sunday, October 11 at 2:00 PM, doors at 1:30."),
    "a match": ("Tournament schedule", "Robin's first match in the tournament is Saturday, October 10 at 9:00 AM."),
    "a service": ("Memorial service", "The memorial service for Jordan's grandmother is Monday, October 12 at 11:00 AM."),
    "a rehearsal": ("Rehearsal moved", "Choir rehearsal moves to Thursday, October 15 at 7:00 PM."),
}
for fam, (t, b) in FAMILIES.items():
    check(f"{fam} is a candidate", MS.is_candidate(t, b), True)
check("a promotion with the same words still isn't (the promo and automated filters stand)",
      MS.is_candidate("Fall sale this evening", "Our fall sale is this evening at 6:00 PM: 30% off everything. "
                      "Unsubscribe from our emails anytime."), False)
check("…and a promo newsletter's party never reaches it at all (the sender and thread rule)",
      MS.worth_suggesting("newsletter", "Launch party tonight", has_ics=False, sender_known=False), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
