#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Bulk and promotional mail stays out of world notes and mail suggestions (titan-node#650: "Google's phone is…" came
from a Google Ads email, a mail suggestion from a VALORANT crypto promo), without losing a school's newsletter event
or a booking confirmation from a no-reply address. Synthetic mails only.
Run: python3 scripts/test_promo_mail.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-promo-"))
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
sys.path.insert(0, str(Path(__file__).parent))

import mail_suggestions as ms  # noqa: E402
import promo_mail as pm  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def mail(frm, subject, body, extra=""):
    return f"From: {frm}\nSubject: {subject}\nDate: Fri, 2 Oct 2026 09:00\n{extra}\n{body}"


ADS = mail("Google Ads <ads-noreply@google.com>", "Your new ad credit is waiting",
           "Call our team at 1-866-555-0100 to claim $500 in ad credit. Unsubscribe.")
VALORANT = mail("VALORANT <news@email.riotgames.com>", "Join the crypto drop Friday at 7 PM",
                "Win exclusive skins in our crypto giveaway this Friday at 7 PM. Manage your preferences.")
LIST = mail("Lincoln Elementary <info@lincoln.k12.example>", "Fall recital Thursday at 6 PM",
            "The fall recital is Thursday, October 8 at 6 PM in the gym. Unsubscribe from this list.")
BOOKING = mail("Dr. Ames Dental <no-reply@amesdental.example>", "Appointment confirmed: Friday 10:15 AM",
               "Your cleaning is confirmed for Friday, October 9 at 10:15 AM. Call 509-555-0111 to reschedule.")
FRIEND = mail("Ann Lee <ann@example.com>", "Dinner Saturday at 7?", "Want to do dinner Saturday at 7 at our place?")

print("── what's promotional ──")
check("the Google Ads mail: its sender", pm.why(ADS), "sender")
check("the VALORANT promo: a marketing subdomain", pm.why(VALORANT), "sender")
check("List-Unsubscribe header", pm.why(mail("Store <orders@shop.example>", "x", "y", "List-Unsubscribe: <mailto:u@x>")),
      "list-unsubscribe")
check("Precedence: bulk", pm.why(mail("A <a@b.example>", "x", "y", "Precedence: bulk")), "precedence")
check("a mail service's domain", pm.why(mail("Brand <hi@brand.mcsv.net>", "x", "y")), "sender")
check("a newsletter footer", pm.why(mail("Club <club@example.org>", "x", "You're receiving this email because you joined.")),
      "footer")
check("a school list (footer)", pm.why(LIST), "footer")
for name, m in (("a booking from no-reply", BOOKING), ("a friend", FRIEND)):
    check(f"not promotional: {name}", pm.why(m), None)
check("info@ / hello@ / team@ alone aren't marketing", [pm.why(mail(f"X <{a}@x.example>", "s", "b")) for a in
      ("info", "hello", "team", "events")], [None] * 4)
check("headers given apart win", pm.why("just a body", {"From": "Deals <deals@x.example>"}), "sender")

print("── mail suggestions: marketing out, a school's event kept ──")
check("the VALORANT promo isn't a candidate", ms.is_candidate("Join the crypto drop Friday at 7 PM", VALORANT), False)
check("the school's recital (a list, no selling) still is", ms.is_candidate("Fall recital Thursday at 6 PM", LIST), True)
check("…a list with selling words isn't", ms.is_candidate("Sale event Friday at 7 PM", mail(
      "Store <store@shop.example>", "Sale event Friday at 7 PM", "40% off this Friday at 7 PM. Unsubscribe.")), False)
check("a booking confirmation still is", ms.is_candidate("Appointment confirmed: Friday 10:15 AM", BOOKING), True)

# World notes: test_world.py checks that a marketer's or a list's mail proposes nothing (it has the world fixture).

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
