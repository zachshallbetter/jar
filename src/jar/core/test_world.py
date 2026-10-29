#!/usr/bin/env python3
"""Hermetic tests for the world model (scripts/world.py, docs/world-model-claims.md).

The golden files (scripts/world_golden/*.jsonl) replay evidence step by step with a fixed clock and no
model, and check each claim's status and the answer each viewer gets. Then unit checks: the truth
rules, confidence and staleness, expiry, the subject rule, household.yaml parsing (round-tripped
through kb_import's own renderer), aliases and who sees what, the adapters, idempotent sync, and an
admin retraction that never shows the value.

    python3 scripts/test_world.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AGENT_TZ", "America/Los_Angeles")
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


def ok(label, cond):
    check(label, bool(cond), True)


class Clock:
    def __init__(self, at="2026-10-01T12:00"):
        self.set(at)

    def set(self, at):
        import local_time
        z = local_time.zone()
        self.t = datetime.fromisoformat(at).replace(tzinfo=z).timestamp()

    def __call__(self):
        return self.t


def fresh(at="2026-10-01T12:00"):
    clock = Clock(at)
    d = tempfile.mkdtemp()
    return W.World(Path(d) / "world.db", clock=clock), clock


def obs(**kw):
    kw["aliases"] = tuple(tuple(a) for a in kw.get("aliases", ()))
    return W.Obs(**kw)


def claim_status(w, entity, attr, value):
    r = w.con.execute("SELECT status, valid_to FROM claims WHERE entity_id=? AND attr=? AND value_norm=?",
                      (entity, attr, W.norm_value(attr, value))).fetchone()
    return (r["status"], r["valid_to"]) if r else (None, None)


# ── golden replays ───────────────────────────────────────────────────────────────────────────────

for gold in sorted((Path(__file__).parent / "world_golden").glob("*.jsonl")):
    w, clock = fresh()
    for n, line in enumerate(gold.read_text().splitlines(), 1):
        step = json.loads(line)
        clock.set(step["at"])
        tag = f"{gold.stem} {n} ({step['step']})"
        if step["do"] == "sync":
            rep = w.sync(step["source"], [obs(**o) for o in step["obs"]])
        else:
            rep = w.state(obs(**step["obs"]), by=step["by"])
        exp = step["expect"]
        if "changes" in exp:
            check(f"{tag}: status changes", rep.get("status_changes"), exp["changes"])
        ent = (step["obs"][0] if isinstance(step["obs"], list) and step["obs"] else step["obs"]) if step["obs"] else None
        eid = ent["entity"] if ent else "maple-animal-clinic"
        for value, st in exp.get("claims", {}).items():
            check(f"{tag}: claim {value}", claim_status(w, eid, "phone", value)[0], st)
        for value, vt in exp.get("valid_to", {}).items():
            check(f"{tag}: valid_to {value}", claim_status(w, eid, "phone", value)[1], vt)
        for a in exp.get("answers", []):
            person, role = a["as"]
            facts = {f.attr: f for f in w.facts(eid, person, role)}
            f = facts.get("phone")
            check(f"{tag}: {person} answer state", W.answer_state(f), a["state"])
            block, _ = W.context(w, a["q"], person, role)
            if "has" in a:
                ok(f"{tag}: {person} sees {a['has']}", a["has"] in block)
            if "lacks" in a:
                ok(f"{tag}: {person} never sees {a['lacks']}", a["lacks"] not in block)
            if "word" in a:
                ok(f"{tag}: {person} certainty {a['word']}", f and any(v.word == a["word"] for v in f.values))
        for value, states in exp.get("log", {}).items():
            cid = w.con.execute("SELECT id FROM claims WHERE value_norm=?", (W.norm_value("phone", value),)).fetchone()["id"]
            got = [r["to_state"] for r in w.con.execute("SELECT to_state FROM claim_log WHERE claim_id=? ORDER BY id", (cid,))]
            check(f"{tag}: log of {value}", got, states)


# ── the truth rules (pure) ──────────────────────────────────────────────────────────────────────

def ev(v, at, prior, origin=None, subject=False):
    return W.Ev(value_norm=v, observed_at=at, prior=prior, kind="x", origin=origin or f"o-{v}-{at}", subject=subject)


single, multi = W.Attr(), W.Attr(multi=True)
check("one value is current", W.resolve(single, [ev("a", "2026-01-01", .9)]), {"a": "current"})
check("newer and stronger supersedes",
      W.resolve(single, [ev("a", "2026-01-01", .9), ev("b", "2026-02-01", .95)]), {"a": "superseded", "b": "current"})
check("newer but weaker disputes",
      W.resolve(single, [ev("a", "2026-01-01", .9), ev("b", "2026-02-01", .85)]), {"a": "disputed", "b": "disputed"})
check("three values: the weak middle one is beaten, the strong old one disputes the newest",
      W.resolve(single, [ev("a", "2026-01-01", .95), ev("b", "2026-02-01", .6), ev("c", "2026-03-01", .85)]),
      {"a": "disputed", "b": "superseded", "c": "disputed"})
check("same day, same authority: disputed, never a coin toss",
      W.resolve(single, [ev("a", "2026-01-01", .9), ev("b", "2026-01-01", .9)]), {"a": "disputed", "b": "disputed"})
check("multi values live side by side",
      W.resolve(multi, [ev("peanuts", "2026-01-01", .9), ev("dust", "2026-03-01", .9)]),
      {"peanuts": "current", "dust": "current"})
check("a newer, stronger none ends the values",
      W.resolve(multi, [ev("peanuts", "2026-01-01", .9), ev(W.NONE, "2026-03-01", .95)]),
      {"peanuts": "superseded", W.NONE: "current"})
check("the subject outranks a newer source about themselves only when they're newer too",
      W.resolve(single, [ev("lincoln", "2026-08-01", .95, subject=True), ev("roosevelt", "2026-09-01", .9)]),
      {"lincoln": "disputed", "roosevelt": "disputed"})
check("the subject's newer word wins",
      W.resolve(single, [ev("roosevelt", "2026-08-01", .9), ev("lincoln", "2026-09-01", .5, subject=True)]),
      {"roosevelt": "superseded", "lincoln": "current"})
check("order doesn't matter",
      W.resolve(single, [ev("b", "2026-02-01", .85), ev("a", "2026-01-01", .9)]), {"a": "disputed", "b": "disputed"})

# confidence, decay, independence
from datetime import date  # noqa: E402
phone = W.ATTRS["org"]["phone"]
ok("fresh household phone is confident", W.confidence(phone, [ev("a", "2026-09-01", .9)], date(2026, 10, 1)) > .85)
ok("a three-year-old phone has halved",
   abs(W.confidence(phone, [ev("a", "2023-10-01", .9)], date(2026, 10, 1)) - .45) < .01)
check("one origin counts once",
      round(W.confidence(phone, [ev("a", "2026-09-01", .9, "f"), ev("a", "2026-09-02", .9, "f")], date(2026, 10, 1)), 3),
      round(W.confidence(phone, [ev("a", "2026-09-02", .9, "f")], date(2026, 10, 1)), 3))
ok("two origins corroborate", W.confidence(phone, [ev("a", "2026-09-01", .9, "f"), ev("a", "2026-09-01", .85, "b")],
                                           date(2026, 10, 1)) > .98)
check("birthdays never decay", W.confidence(W.ATTRS["person"]["birthday"], [ev("a", "2000-01-01", .9)], date(2026, 10, 1)), .9)
ok("school facts expire after Aug 31", W.expired(W.ATTRS["person"]["grade"], [ev("6th", "2025-09-10", .9)], date(2026, 9, 2)))
ok("school facts hold during the year", not W.expired(W.ATTRS["person"]["grade"], [ev("7th", "2026-09-10", .9)], date(2026, 10, 1)))
check("certainty words", (W.certainty([W.Ev("a", "2026-01-01", .9, "household", "h")]),
                          W.certainty([W.Ev("a", "2026-01-01", .85, "bill", "b")]),
                          W.certainty([W.Ev("a", "2026-01-01", .9, "household", "h"), W.Ev("a", "2026-01-01", .85, "bill", "b")])),
      ("stated", "recorded", "corroborated"))

# ── the answer contract ─────────────────────────────────────────────────────────────────────────

w, clock = fresh("2026-10-01T12:00")
w.sync("household", [
    obs(entity="leo", etype="person", name="Leo", attr="allergies", value=W.NONE, observed_at="2026-08-03",
        kind="household", ref="hh-people-leo#allergies", origin="household.yaml", label="the household file",
        owner=None, audience="household"),
    obs(entity="city-water", etype="org", name="City Water", attr="address", value="18 Alder St", observed_at="2023-01-05",
        kind="household", ref="hh-utilities-water#address", origin="household.yaml", label="the household file",
        owner=None, audience="household", aliases=(("water", ""),)),
])
f = {x.attr: x for x in w.facts("leo")}
check("known none", W.answer_state(f.get("allergies")), "known none")
ok("known none says so", "none on record" in W.answer_line("Leo", "person", "allergies", f.get("allergies"), w.today()))
f = {x.attr: x for x in w.facts("city-water")}
check("stale after years", W.answer_state(f.get("address")), "stale")
ok("stale says it may have changed", "may have changed" in W.answer_line("City Water", "org", "address", f["address"], w.today()))
check("unknown", W.answer_state(f.get("phone")), "unknown")
ok("unknown: an organisation's public fact only labelled as general knowledge (#363)",
   "general knowledge, not on record" in W.answer_line("City Water", "org", "phone", None, w.today()))
ok("unknown: a person's never guessed", "never fill it in" in W.answer_line("Ava", "person", "phone", None, w.today()))
block, ids = W.context(w, "What's the water company's phone number?", "zach", "admin")
check("a question finds the entity by its role", ids, ["city-water"])
ok("and the unknown answer line is in the block", "phone: not on record" in block)
check("no entity named: no block", W.context(w, "what's the weather like?", "zach", "admin"), ("", []))

# ── household.yaml, round-tripped through kb_import's own renderer ─────────────────────────────

try:
    import kb_import
    doc = {
        "people": [{"name": "Ava Hale", "relation": "daughter", "birthday": "2014-05-02", "allergies": ["peanuts", "dust"],
                    "school": "Lincoln Middle", "grade": "7th"}],
        "pets": [{"name": "Biscuit", "species": "dog", "vet": "Maple Animal Clinic"}],
        "contacts": [{"role": "vet", "name": "Maple Animal Clinic", "phone": "(555) 0142", "address": "18 Alder St",
                      "for": "Biscuit"}],
        "utilities": [{"service": "water", "provider": "City Water", "account_number": "12345", "kept_in": "1Password",
                       "billing": "autopay on the 12th"}],
        "schedules": [{"name": "Trash", "when": "Tuesday night", "who": "Zach"}],
    }
    notes, problems = kb_import.build(doc, include_secrets=False)
    check("importer is happy", problems, [])
    rows = [(sid, n["text"], None, "household") for sid, n in notes.items()]
    out = W.household_observations(rows)
    got = {(o.entity, o.attr, str(o.value)) for o in out}
    for want in [("ava-hale", "birthday", "2014-05-02"), ("ava-hale", "allergies", "peanuts"),
                 ("ava-hale", "allergies", "dust"), ("ava-hale", "grade", "7th"),
                 ("biscuit", "vet", "Maple Animal Clinic"), ("maple-animal-clinic", "phone", "(555) 0142"),
                 ("maple-animal-clinic", "serves", "Biscuit"), ("city-water", "kept_in", "1Password"),
                 ("city-water", "billing", "autopay on the 12th"), ("trash", "schedule", "Tuesday night")]:
        ok(f"parsed {want}", want in got)
    ok("the account number never becomes a claim", not any("12345" in str(o.value) for o in out))
    phone_co = W.household_observations([(sid, n["text"], None, "household") for sid, n in kb_import.build(
        {"utilities": [{"service": "phone", "provider": "T-Mobile"}]}, include_secrets=False)[0].items()])
    aliases = {a for o in phone_co for a, _ in o.aliases}
    ok("a 'phone' service never becomes the bare alias 'phone'", "phone" not in aliases and "phone company" in aliases)
    w2, _ = fresh()
    w2.sync("household", out)
    check("ava by first name", w2.find("when is Ava's birthday?", "zach", "admin"), ["ava-hale"])
    check("'daughter' is Zach's word", w2.find("what grade is my daughter in?", "zach", "admin"), ["ava-hale"])
    check("…not Robin's", w2.find("what grade is my daughter in?", "p_robin", "adult"), [])
    check("the vet by role", w2.find("what's the vet's number?", "p_kid", "kid"), ["maple-animal-clinic"])
    blk, _ = W.context(w2, "who is Biscuit's vet?", "zach", "admin")
    ok("pet's vet answer", "Biscuit, vet: Maple Animal Clinic" in blk)
    blk, _ = W.context(w2, "when does the trash go out?", "zach", "admin")
    ok("schedule answer with a computed next date", "Trash, when: Tuesday night" in blk and "next: " in blk)
    check("idempotent", w2.sync("household", W.household_observations(rows)),
          {"observations": len(out), "added": 0, "removed": 0, "status_changes": 0})
except ImportError as exc:  # yaml missing on a bare machine
    print(f"skip household round trip: {exc}")

# ── who sees what ───────────────────────────────────────────────────────────────────────────────

w, clock = fresh()
w.sync("bills", [obs(entity="dr-patel", etype="org", name="Dr. Patel", attr="phone", value="555 0100",
                     observed_at="2026-09-01", kind="bill", ref="b1", origin="bill:dr-patel", label="the September bill",
                     owner="zach", audience="private", aliases=(("therapist", ""),))])
check("Zach finds his private entity", w.find("what's my therapist's number?", "zach", "admin"), ["dr-patel"])
check("Robin doesn't", w.find("what's the therapist's number?", "p_robin", "adult"), [])
check("a kid doesn't", w.find("who is dr patel?", "p_kid", "kid"), [])
check("about: nothing on record for her", W.about(w, "Dr. Patel", "p_robin", "adult"), "Nothing on record about “Dr. Patel”.")
cid = w.con.execute("SELECT id FROM claims WHERE entity_id='dr-patel'").fetchone()["id"]
ok("admin retracts by id", w.retract(cid, by="admin", reason="asked by Zach"))
r = w.con.execute("SELECT * FROM claim_log WHERE claim_id=? ORDER BY id DESC", (cid,)).fetchone()
check("retraction is logged by admin", (r["to_state"], r["by"]), ("retracted", "admin"))
check("and the value is gone from answers", w.facts("dr-patel", "zach", "admin"), [])

# ── adapters ────────────────────────────────────────────────────────────────────────────────────

prov = lambda n: (n["data"]["provenance"].get("owner"), n["data"]["provenance"].get("audience"))  # noqa: E731
nodes = [
    {"node_id": "bill-mail-1", "node_type": "bill", "data": {"vendor": "City Water", "amount": 84.2, "currency": "USD",
     "due_date": "2026-10-12", "mail_date": "2026-09-28", "payment_status": "due", "provenance": {"owner": "zach", "audience": "private"}}},
    {"node_id": "bill-mail-2", "node_type": "bill", "data": {"vendor": "City Water", "amount": 84.2, "currency": "USD",
     "paid_date": "2026-10-10", "payment_status": "paid", "provenance": {"owner": "zach", "audience": "private"}}},
    {"node_id": "bill-mail-3", "node_type": "bill", "data": {"vendor": "PG&E", "amount": 120, "due_date": "2026-10-20",
     "mail_date": "2026-09-30", "provenance": {"owner": "zach", "audience": "private"}}},
]
bo = W.bill_observations(nodes, prov)
check("a paid notice ends the matching due one", sorted(o.value for o in bo), ["$120.00 due Oct 20", "$84.20 paid Oct 10"])
check("bills are private", {o.audience for o in bo}, {"private"})
two = [
    {"node_id": "bill-z", "node_type": "bill", "data": {"vendor": "Lumen Power", "amount": 61.5, "due_date": "2026-10-14",
     "mail_date": "2026-09-29", "payment_status": "due", "provenance": {"owner": "zach", "audience": "private"}}},
    {"node_id": "rcpt-r", "node_type": "receipt", "data": {"vendor": "Lumen Power", "amount": 61.5, "paid_date": "2026-10-02",
     "payment_status": "paid", "provenance": {"owner": "p_robin", "audience": "private"}}},
]
check("#1055: another person's receipt never ends someone's due bill (paid is per owner)",
      sorted((o.owner, o.value) for o in W.bill_observations(two, prov)),
      [("p_robin", "$61.50 paid Oct 2"), ("zach", "$61.50 due Oct 14")])

appts = []
for d in ("2026-09-03", "2026-09-10", "2026-09-17", "2026-09-24"):
    appts.append({"node_id": f"cx-{d}", "node_type": "appointment", "data": {
        "title": "Ballet", "calendar": "Kids", "start": f"{d}T23:30:00Z", "end": f"{d}T00:30:00Z".replace(d, d[:-2] + str(int(d[-2:]) + 1).zfill(2)),
        "location": "Studio 4", "provenance": {"owner": "zach", "audience": "private", "observed_at": "2026-08-20"}}})
appts.append({"node_id": "cx-x", "node_type": "appointment", "data": {"title": "Dentist", "calendar": "Kids",
              "start": "2026-10-09T17:00:00Z", "provenance": {"owner": "zach", "audience": "private"}}})
import local_time  # noqa: E402
co = W.calendar_observations(appts, prov, local_time.zone())
check("a weekly series becomes a schedule", [(o.attr, o.value) for o in co if o.attr == "schedule"],
      [("schedule", "every Thursday, 4:30 PM–5:30 PM")])
ok("a one-off appointment is not a claim", not any(o.entity == "dentist" for o in co))
# only a calendar the adults designated as the household's widens; the Mac's "shared" never does
def series(title, cal, owner_kind, weekday_dates, at="T03:00:00Z", synced_by="zach"):
    return [{"node_id": f"cx-{title}-{d}", "node_type": "appointment", "data": {
        "title": title, "calendar": cal, "calendar_owner": owner_kind, "start": f"{d}{at}",
        "provenance": {"owner": synced_by, "audience": "private", "observed_at": "2026-09-01"}}} for d in weekday_dates]
weds = ["2026-09-03", "2026-09-10", "2026-09-17", "2026-09-24"]
mixed = (series("Take out garbage", "hamilbetter@gmail.com", "shared", weds)
         + series("Ava Therapy", "Ava", "mine", weds, at="T22:00:00Z")
         + series("Team Standup", "Acme Engineering", "shared", weds, at="T16:00:00Z")    # a colleague's calendar
         + series("Board Sync", "Work", "shared", weds, at="T18:00:00Z")                  # organized by others
         + series("Old Club", "Hamilton", "shared", weds, at="T01:00:00Z"))
prefs = Path(tempfile.mkdtemp()) / "household.json"
check("unset: the seed", W.household_calendars(prefs), {"hamilbetter@gmail.com"})
so = W.calendar_observations(mixed, prov, local_time.zone(), hidden={"zach": {"hamilton"}},
                             household=W.household_calendars(prefs))
check("only the designated household calendar widens", {o.entity: o.audience for o in so if o.attr == "schedule"},
      {"take-out-garbage": "household", "ava-therapy": "private", "team-standup": "private", "board-sync": "private"})
ok("a calendar the syncer hid is skipped", not any(o.entity == "old-club" for o in so))
# jellybean#247 (Zach, 10-01): hidden on Zach's Mac means hidden from his own views only
check("hidden_here: hidden and unassigned → out; assigned to someone else or household → in; assigned to the syncer → out",
      (W.hidden_here("Hamilton", "zach", {"zach": {"hamilton"}}, {}, set()),
       W.hidden_here("Ava", "zach", {"zach": {"ava"}}, {"ava": "p_ava"}, set()),
       W.hidden_here("Family", "zach", {"zach": {"family"}}, {}, {"family"}),
       W.hidden_here("Work", "zach", {"zach": {"work"}}, {"work": "zach"}, set()),
       W.hidden_here("Ava", "zach", {}, {}, set())), (True, False, False, True, False))
so2 = W.calendar_observations(mixed, prov, local_time.zone(), hidden={"zach": {"ava", "hamilton"}},
                              household=W.household_calendars(prefs), assigned={"ava": "p_ava"})
ok("a hidden calendar assigned to Ava still reaches titan for her", any(o.entity == "ava-therapy" for o in so2)
   and not any(o.entity == "old-club" for o in so2))
ok("a household series never names the calendar by its title (an email)",
   all("@" not in o.label for o in so) and any(o.label == "the shared household calendar" for o in so))
try:
    W.set_household_calendars(["Kids"], "kid", prefs)
    ok("a kid can't choose household calendars", False)
except PermissionError:
    ok("a kid can't choose household calendars", True)
check("an adult sets the list", W.set_household_calendars(["Family", "hamilbetter@gmail.com"], "adult", prefs),
      ["Family", "hamilbetter@gmail.com"])
check("…and it's read back", W.household_calendars(prefs), {"family", "hamilbetter@gmail.com"})
W.set_household_calendars([], "admin", prefs)
check("set to empty: nothing is the household's (the seed doesn't come back)", W.household_calendars(prefs), set())
check("…so garbage night stays private", {o.audience for o in W.calendar_observations(
    mixed, prov, local_time.zone(), household=W.household_calendars(prefs)) if o.entity == "take-out-garbage"}, {"private"})
w, clock = fresh()
w.sync("calendar", so)
w.sync("bills", [o for o in bo])
check("a kid finds garbage night", w.find("when does the garbage go out?", "p_kid", "kid"), ["take-out-garbage"])
blk, _ = W.context(w, "When is take out garbage?", "p_kid", "kid")
ok("…with its schedule", "every Wednesday" in blk)
check("…but not Ava's therapy", w.find("when is ava therapy?", "p_kid", "kid"), [])
check("…nor a colleague's standup", w.find("when is team standup?", "p_kid", "kid"), [])
check("…nor Zach's bills", w.find("what's the city water bill?", "p_kid", "kid"), [])
check("Zach still sees his", sorted(w.find("garbage, ava therapy, team standup and city water", "zach", "admin")),
      ["ava-therapy", "city-water", "take-out-garbage", "team-standup"])
from datetime import date as _d  # noqa: E402
check("weekly", W.cadence({2: [_d(2026, 9, 2), _d(2026, 9, 9), _d(2026, 9, 16)]}), 7)
check("weekly with a skipped week", W.cadence({2: [_d(2026, 9, 2), _d(2026, 9, 16), _d(2026, 9, 23)]}), 7)
check("every other week", W.cadence({2: [_d(2026, 9, 2), _d(2026, 9, 16), _d(2026, 9, 30)]}), 14)
check("every four weeks is neither", W.cadence({2: [_d(2026, 9, 2), _d(2026, 9, 30), _d(2026, 10, 28)]}), None)
check("weekdays that disagree: no claim", W.cadence({0: [_d(2026, 9, 7), _d(2026, 9, 14)], 2: [_d(2026, 9, 2), _d(2026, 9, 16)]}), None)
fortnight = series("Take out recycling", "hamilbetter@gmail.com", "shared", ["2026-09-03", "2026-09-17", "2026-10-01"])
check("recycling every two weeks is told as every other Wednesday",
      [o.value for o in W.calendar_observations(fortnight, prov, local_time.zone(), household={"hamilbetter@gmail.com"})
       if o.attr == "schedule"], ["every other Wednesday, 8:00 PM"])
w, clock = fresh()
w.sync("calendar", W.calendar_observations(fortnight, prov, local_time.zone(), household={"hamilbetter@gmail.com"}))
rf = {f.attr: f for f in w.facts("take-out-recycling")}["schedule"]
check("its anchor is the last seen Wednesday", W.series_anchor(rf.values[0]).isoformat(), "2026-09-30")
line = W.answer_line("Take out recycling", "activity", "schedule", rf, w.today(), datetime(2026, 10, 2, 9, 0))
ok("every other week: the next one is two weeks on, not next Wednesday (10-01: kid heard 'next Wednesday')",
   "every other Wednesday" in line and "next: Wed Oct 14 8:00 PM" in line and "Oct 7" not in line)
ok("…and the reply is asked for both how often and when next", "how often" in line)
check("on the week it falls: today", W._next_words("every other Wednesday, 8:00 PM", datetime(2026, 10, 14, 9, 0),
                                                   _d(2026, 9, 30)), "next: today 8:00 PM (computed)")
check("no anchor: no date guessed", W._next_words("every other Wednesday, 8:00 PM", datetime(2026, 10, 2, 9, 0)), "")
ok("weekly lines aren't changed", W._next_words("every Wednesday, 8:00 PM", datetime(2026, 10, 2, 9, 0)).startswith("next: Wed Oct 7"))
monthly = series("Book club", "Zach", "mine", ["2026-07-08", "2026-08-05", "2026-09-02"])
check("a monthly series makes no claim", W.calendar_observations(monthly, prov, local_time.zone()), [])
w, clock = fresh()
w.sync("bills", [obs(entity="amazon-com", etype="org", name="Amazon.com", attr="bill", value=v, observed_at=at, kind="bill",
                     ref=f"b{i}", origin="bill:amazon-com", label="a bill", owner="zach", audience="private")
                 for i, (v, at) in enumerate([("$21.79 payment failed Sep 25", "2026-09-25"), ("$16.96 due Oct 10", "2026-09-26"),
                                              ("$17.43 payment failed Sep 17", "2026-09-17")])])
line = W.answer_line("Amazon.com", "org", "bill", {f.attr: f for f in w.facts("amazon-com")}["bill"], w.today())
ok("bills: the newest is named first", line.startswith("Amazon.com, bills: newest $16.96 due Oct 10"))
import world_eval as E  # noqa: E402
ok("eval: 'today at 3:00' answers a weekly schedule", E.mentions("It's today from 3:00 PM to 4:00 PM.", "every Thursday, 3:00 PM–4:00 PM", "schedule"))
ok("eval: an every-other series must be said to be one",
   not E.mentions("Every Wednesday at 8:00 PM.", "every other Wednesday, 8:00 PM", "schedule")
   and E.mentions("Every other Wednesday at 8:00 PM.", "every other Wednesday, 8:00 PM", "schedule"))
check("parse a schedule", W.parse_schedule("every Thursday, 4:30 PM–5:30 PM"), ([3], "16:30"))
check("weekdays", W.parse_schedule("weekdays 8:05 am"), ([0, 1, 2, 3, 4], "08:05"))
check("Wednesday and Saturday are read (the day regex missed them)",
      W.parse_schedule("every Wednesday and Saturday, 8:00 PM"), ([2, 5], "20:00"))
check("every other week is not guessed", W.parse_schedule("every other Tuesday"), None)
nxt = W.next_occurrence([3], "16:30", datetime(2026, 10, 1, 17, 0))
check("next Thursday after this one's start", nxt.date().isoformat(), "2026-10-08")

# partial listings never retract
w, clock = fresh()
w.sync("bills", bo)
rep = w.sync("bills", [], remove_missing=False)
check("partial listing: nothing removed", rep["removed"], 0)
rep = w.sync("bills", [])
check("full listing: removed", rep["removed"], 2)

# memory facts about a paged entity leave the prompt
w, clock = fresh()
w.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                         value="(555) 0142", observed_at="2026-09-01", kind="household", ref="r", origin="household.yaml",
                         owner=None, audience="household", aliases=(("vet", ""),))])
recall = "[MEMORY — relevant facts about this user]\n• The vet's number is (555) 0199 (noted Jun 3)\n• Zach prefers texts"
check("memory about the vet is dropped", W.drop_facts_about(recall, w, ["maple-animal-clinic"]),
      "[MEMORY — relevant facts about this user]\n• Zach prefers texts")
check("nothing left: no block", W.drop_facts_about("[MEMORY]\n• The vet is Maple Animal Clinic", w, ["maple-animal-clinic"]), "")

# ── bubbles (docs/world-model-bubbles.md, step 1) ──────────────────────────────────────────────

clock = Clock("2026-10-01T12:00")
B = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=clock)
hh_obs = obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone", value="(555) 0142",
             observed_at="2026-09-01", kind="household", ref="hh-vet#phone", origin="household.yaml",
             label="the household file", owner=None, audience="household", aliases=(("vet", ""),),
             bubble="household", facet="home")
B.sync("household", [hh_obs])
B.sync("bills", [o for o in W.bill_observations(nodes, prov)])
check("private bills land in their owner's bubble", sorted(B.bubbles()), ["household", "zach"])
check("…as personal", {r["facet"] for _, r in B.world("zach").entities()}, {"personal"})
check("household things stay in the household", [e for e, _ in B.world("household").entities()], ["maple-animal-clinic"])
check("ids say whose", B.find("what's the vet's number?", "zach", "admin"), ["household:maple-animal-clinic"])
check("Zach finds his own bill vendor in his bubble", B.find("the city water bill", "zach", "admin"), ["zach:city-water"])
check("a kid doesn't", B.find("the city water bill", "ava", "kid"), [])
blk, ids = W.context(B, "What's the vet's number?", "ava", "kid")
ok("answers read across bubbles", "Answer: Maple Animal Clinic, phone: (555) 0142" in blk)

# Ava's friend Sam and Leo's friend Sam: two entities in two bubbles, never a dispute
sams = [{"node_id": f"contact-sam-{k}", "node_type": "contact", "data": {"name": "Sam", "phone": ph, "relation": "friend",
         "provenance": {"owner": k, "audience": "private", "saved": "2026-09-20"}}} for k, ph in (("ava", "555 0111"), ("leo", "555 0222"))]
B.sync("contacts", W.contact_observations(sams, prov))
check("two Sams, two bubbles", sorted(e for e, _ in B.entities() if e.endswith(":sam")), ["ava:sam", "leo:sam"])
check("Ava finds her Sam", B.find("what's sam's number?", "ava", "kid"), ["ava:sam"])
f = {x.attr: x for x in B.facts("ava:sam", "ava", "kid")}
check("…with her Sam's number, not a dispute", (W.answer_state(f.get("phone")), f["phone"].values[0].value), ("known", "555 0111"))
check("Leo finds his", B.find("what's sam's number?", "leo", "kid"), ["leo:sam"])
ok("an unmapped contact waits in its owner's inbox", B.entity("ava:sam")["facet"] == "inbox")

# a calendar the people store assigns to Ava: her bubble, and she can see it beside whoever synced it
ballet = series("Ballet + Modern", "Ava", "mine", ["2026-09-04", "2026-09-11", "2026-09-18", "2026-09-25"], at="T23:00:00Z")
B.sync("calendar", W.calendar_observations(ballet, prov, local_time.zone(), assigned={"ava": "ava"}))
check("Ava's calendar lands in Ava's bubble", B.find("when is ballet?", "ava", "kid"), ["ava:ballet-modern"])
check("Zach, who synced it, still sees it", B.find("when is ballet?", "zach", "admin"), ["ava:ballet-modern"])
check("Leo doesn't", B.find("when is ballet?", "leo", "kid"), [])
ok("its page names the facet", "inbox" in W.page(B, "ava:ballet-modern", "ava", "kid"))

# parents see their kids' schedules (Zach, 10-01); the kid sees what a parent put on their calendar
B3 = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=clock)
fam = {"ava": ["zach", "robin"], "leo": ["zach", "robin"]}
therapy = series("Ava Therapy", "Ava", "mine", ["2026-09-03", "2026-09-17", "2026-10-01"], at="T22:00:00Z")
B3.sync("calendar", W.calendar_observations(ballet + therapy, prov, local_time.zone(), assigned={"ava": "ava"}, parents=fam))
check("Robin sees Ava's ballet", B3.find("when is ballet?", "robin", "adult"), ["ava:ballet-modern"])
check("…and her therapy, a schedule", B3.find("when is ava therapy?", "robin", "adult"), ["ava:ava-therapy"])
check("Ava sees what a parent put on her calendar", B3.find("when is ava therapy?", "ava", "kid"), ["ava:ava-therapy"])
check("Leo doesn't", B3.find("when is ava therapy?", "leo", "kid"), [])
check("Jordan, an adult but not a parent, doesn't", B3.find("when is ballet?", "jordan", "adult"), [])
own = [{"node_id": f"cx-swim-{d}", "node_type": "appointment", "data": {"title": "Swim", "calendar": "Leo's phone",
        "calendar_owner": "mine", "start": f"{d}T01:00:00Z", "provenance": {"owner": "leo", "audience": "private"}}}
       for d in ("2026-09-02", "2026-09-09", "2026-09-16")]
co = W.calendar_observations(own, prov, local_time.zone(), parents=fam)
check("a kid's own calendar series: parents see it too", sorted(json.loads(co[0].audience)), ["leo", "robin", "zach"])
ok("…and it stays in the kid's bubble", co[0].bubble == "leo")
co2 = W.calendar_observations(own, prov, local_time.zone())
check("no parents set: private to the kid", co2[0].audience, "private")

# routing changes move an item: shared with the household now, so it leaves Zach's bubble
moved = [W.Obs(**{**o.__dict__, "audience": "household", "bubble": "household", "facet": "home"}) for o in W.bill_observations(nodes, prov)
         if o.entity == "city-water"]
B.sync("bills", moved)
check("moved to the household", B.find("the city water bill", "ava", "kid"), ["household:city-water"])
check("…and gone from Zach's bubble", [st for st, in B.world("zach").con.execute(
    "SELECT status FROM claims WHERE entity_id='city-water'")], ["retracted"])
check("…and Zach no longer finds an empty copy in his bubble", B.find("the city water bill", "zach", "admin"),
      ["household:city-water"])
ok("a moved-away entity isn't listed", "zach:city-water" not in [e for e, _ in B.entities()])
ok("the CLI summary counts every bubble", all(x in B.summary() for x in ("household:", "zach:", "ava:")))
import stat  # noqa: E402
modes = {q.name: stat.S_IMODE(q.stat().st_mode) for q in B.root.glob("*.db")}
check("bubble files are private to owner and group", {n: m & 0o007 for n, m in modes.items()},
      {n: 0 for n in modes})
check("…and so is the directory", stat.S_IMODE(B.root.stat().st_mode) & 0o007, 0)
import world_eval as E2  # noqa: E402
ok("world_eval reads bubbles", any(c.name.startswith("household:maple-animal-clinic.phone") for c in E2.lookups(B)))
W._NAMES.update({"ava": "Ava"})
_kb = {"name": "Ballet + Modern", "type": "activity"}
_kt = {"name": "Ava Therapy", "type": "activity"}
_tg = {"name": "Take out garbage", "type": "activity"}
check("natural questions per kind of activity (#364)",
      (E2.question_for("household:take-out-garbage", _tg, "schedule", "x"),
       E2.question_for("household:take-out-garbage", _tg, "place", "x"),
       E2.question_for("ava:ballet-modern", _kb, "schedule", "x"),
       E2.question_for("ava:ballet-modern", _kb, "pickup", "x"),
       E2.question_for("ava:ava-therapy", _kt, "place", "x"),
       E2.question_for("ava:ava-therapy", _kt, "pickup", "x")),
      ("When does the garbage go out?", None, "When is Ava's Ballet + Modern?", "Who picks up from Ava's Ballet + Modern?",
       "Where is Ava Therapy?", None))
ok("…and bubble ids in privacy cases", all(":" in e for e, _, _ in E2.private_entities(B)))
ok("no 'Where is <activity>?' not-on-record questions (#364)",
   not any(c.name.endswith(".place.unknown") for c in E2.lookups(B)))
line = E2.log_line("nightly", "abc123", "gemini", E2.lookups(B, 4), {False: 2, True: 3}, 7, 8, 0, 1, 2, 2, 140.4,
                   {"household": [3, 4]})
check("the nightly line for the scorecard", {k: line[k] for k in ("source", "score", "privacy_ok", "privacy_errors",
                                                                   "household_kid_ok", "by_bubble")},
      {"source": "nightly", "score": round(3 / len(E2.lookups(B, 4)), 3), "privacy_ok": True, "privacy_errors": 1,
       "household_kid_ok": True, "by_bubble": {"household": [3, 4]}})
check("…a leak makes privacy_ok false", E2.log_line("nightly", "r", "gemini", [], {False: 0, True: 0}, 7, 8, 1, 0, 0, 0,
                                                   0, {})["privacy_ok"], False)

# ── a correction said in chat is newer than the record: it stays, and the world block flags it ────

w, clock = fresh("2026-10-01T12:00")
w.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                         value="(555) 0142", observed_at="2026-09-01", kind="household", ref="r", origin="household.yaml",
                         owner=None, audience="household", aliases=(("vet", ""),))])
recall = ("[MEMORY — relevant facts about this user]\n• The vet's number is (555) 0199 (noted Jun 3)\n"
          "• The vet's number changed to (555) 0777 (noted today)\n• Zach prefers texts")
kept, newer = W.split_recalled(recall, w, ["maple-animal-clinic"])
ok("an older memory fact about the paged entity is dropped", "0199" not in kept)
ok("a newer one stays", "0777" in kept and "prefers texts" in kept)
check("…and is reported for the world block", list(newer), ["maple-animal-clinic"])
blk, _ = W.context(w, "what's the vet's number?", "zach", "admin", newer=newer)
ok("the world block flags it as newer than the record", "Newer than titan's record for Maple Animal Clinic" in blk and "0777" in blk)
check("dates read back", (W.noted_date("• x (noted yesterday)", date(2026, 10, 1)), W.noted_date("• x (noted Mar 3, 2025)", date(2026, 10, 1)),
                         W.noted_date("• x (noted Sep 29)", date(2026, 10, 1)), W.noted_date("• x", date(2026, 10, 1))),
      (date(2026, 9, 30), date(2025, 3, 3), date(2026, 9, 29), None))

# ── who could see a claim survives its retraction; changes since T per viewer ────────────────────

w, clock = fresh("2026-10-01T12:00")
w.sync("bills", [obs(entity="city-water", etype="org", name="City Water", attr="bill", value="$84.20 due Oct 12",
                     observed_at="2026-09-28", kind="bill", ref="b1", origin="bill:cw", owner="zach", audience="private")])
w.sync("household", [obs(entity="city-water", etype="org", name="City Water", attr="phone", value="555 0300",
                         observed_at="2026-09-01", kind="household", ref="h1", origin="household.yaml", owner=None,
                         audience="household")])
t0 = clock.t
clock.set("2026-10-02T12:00")
w.sync("bills", [])
check("the bill is retracted", [st for st, in w.con.execute("SELECT status FROM claims WHERE attr='bill'")], ["retracted"])
ok("its history still knows who could see it", all(r["vis"] and "zach" in r["vis"] for r in w.log("city-water") if r["attr"] == "bill"))
ok("Zach sees the bill's history", any(r["attr"] == "bill" for r in w.log("city-water", "zach", "admin")))
ok("a kid never does", not any(r["attr"] == "bill" for r in w.log("city-water", "ava", "kid")))
ok("…but sees the household phone's", any(r["attr"] == "phone" for r in w.log("city-water", "ava", "kid")))
ch = w.changes(["city-water"], t0 + 1, "zach", "admin")
check("changes since T: one row in the connector ledger's shape (#394), for Zach",
      [(c["item_id"], c["source"], c["change"], c["title"], c["diff"]) for c in ch],
      [("city-water", "world", "changed", "City Water", {"bill": ["$84.20 due Oct 12", None]})])
ok("…with when it moved and when it was last observed", ch[0]["at"] >= t0 + 1 and ch[0]["observed_at"] == "2026-09-01")
check("…and nothing for a kid (the bill was never theirs)", w.changes(["city-water"], t0 + 1, "ava", "kid"), [])
check("since the start, the kid sees it appear with only the household phone",
      [(c["change"], c["diff"]) for c in w.changes(["city-water"], 0, "ava", "kid")],
      [("created", {"phone": [None, "555 0300"]})])
check("…and Zach sees both", w.changes(["city-water"], 0, "zach", "admin")[0]["diff"],
      {"phone": [None, "555 0300"]})   # the bill came and went inside the window: nothing to tell
w.sync("household", [])
check("everything retracted: removed", [(c["change"], c["diff"]) for c in w.changes(["city-water"], t0 + 1, "ava", "kid")],
      [("removed", {"phone": ["555 0300", None]})])
w, clock = fresh("2026-10-01T12:00")
w.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                         value="555 0142", observed_at="2026-09-01", kind="household", ref="r", origin="household.yaml",
                         owner=None, audience="household")])
t1 = clock.t
clock.set("2026-10-02T12:00")
w.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                         value="555 0199", observed_at="2026-10-02", kind="household", ref="r", origin="household.yaml",
                         owner=None, audience="household")])
check("a value replaced: [before, after]", w.changes(["maple-animal-clinic"], t1 + 1, "ava", "kid")[0]["diff"],
      {"phone": ["555 0142", "555 0199"]})
check("nothing since now", w.changes(["maple-animal-clinic"], clock.t + 1, "ava", "kid"), [])
B2 = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=clock)
B2.sync("household", [hh_obs])
check("Bubbles.changes uses bubble ids", {c["item_id"] for c in B2.changes(["household:maple-animal-clinic"], 0)},
      {"household:maple-animal-clinic"})

# ── the world API (docs/world-api.md, #365): list and pages per viewer ────────────────────────────

clock = Clock("2026-10-01T12:00")
BA = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=clock)
BA.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                          value="(555) 0199", observed_at="2026-06-03", kind="household", ref="hh-contacts-vet-maple#phone",
                          origin="household.yaml", label="the household file", owner=None, audience="household",
                          aliases=(("vet", ""),), bubble="household", facet="home"),
                      obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="serves",
                          value="Biscuit", observed_at="2026-06-03", kind="household", ref="hh-contacts-vet-maple#serves",
                          origin="household.yaml", label="the household file", owner=None, audience="household",
                          value_entity="biscuit", bubble="household", facet="home"),
                      obs(entity="biscuit", etype="pet", name="Biscuit", attr="species", value="dog", observed_at="2026-06-03",
                          kind="household", ref="hh-pets-biscuit#species", origin="household.yaml", owner=None,
                          audience="household", bubble="household", facet="home")])
BA.sync("bills", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone", value="555-0142",
                      observed_at="2026-08-02", kind="bill", ref="bill-1", origin="bill:maple", label="the August bill",
                      owner="zach", audience="private", bubble="household", facet="home"),
                  obs(entity="city-water", etype="org", name="City Water", attr="bill", value="$84.20 due Oct 12",
                      observed_at="2026-09-28", kind="bill", ref="bill-2", origin="bill:cw", label="the September bill",
                      owner="zach", audience="private", bubble="zach", facet="personal")])
labels = {"zach": "Zach", "ava": "Ava"}
L = W.entity_list(BA, "zach", "admin", labels=labels)
check("Zach's list: his bubble first, then household", [r["id"] for r in L["entities"]],
      ["zach:city-water", "household:biscuit", "household:maple-animal-clinic"])
check("…grouped and counted", L["counts"], {"people": 1, "places": 2, "things": 0})
row = next(r for r in L["entities"] if r["id"] == "household:maple-animal-clinic")
check("…with flags, a summary and a full timestamp", (row["flags"], row["group"], row["bubble_label"], row["updated"]),
      ({"disputed": 1, "stale": 0}, "places", "Household", "2026-08-02T00:00:00-07:00"))
check("a kid's list: household only", [r["id"] for r in W.entity_list(BA, "ava", "kid")["entities"]],
      ["household:biscuit", "household:maple-animal-clinic"])
check("group filter", [r["id"] for r in W.entity_list(BA, "zach", "admin", group="people")["entities"]], ["household:biscuit"])
qr = W.entity_list(BA, "ava", "kid", q="vet")["entities"]
check("q matches an alias, and says so", [(r["id"], r["matched"]) for r in qr], [("household:maple-animal-clinic", "alias")])
check("truncated when the limit cuts", W.entity_list(BA, "zach", "admin", limit=1)["truncated"], True)

print("── freshness: confirmed, facts inline, Still right? (Settings v5) ──")
BF = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=clock)
BF.sync("household", [obs(entity="biscuit", etype="pet", name="Biscuit", attr="species", value="dog", observed_at="2026-06-03",
                          kind="household", ref="hh-pets-biscuit#species", origin="household.yaml", owner=None,
                          audience="household", bubble="household", facet="home")])
BF.sync("bills", [obs(entity="city-water", etype="org", name="City Water", attr="bill", value="$84.20 due Oct 12",
                      observed_at="2026-09-28", kind="bill", ref="bill-2", origin="bill:cw", label="the September bill",
                      owner="zach", audience="private", bubble="zach", facet="personal")])
HF = W.entity_list(BF, "ava", "kid", bubble_only="household", with_facts=True)["entities"]
check("bubble=household: only household entities, even for the admin",
      [r["bubble"] for r in W.entity_list(BF, "zach", "admin", bubble_only="household")["entities"]], ["household"])
check("facts inline, each with state, stale and confirmed",
      [(f["attr"], f["value"], f["stale"], f["confirmed"][:10]) for f in HF[0]["facts"]], [("species", "dog", False, "2026-06-03")])
check("no facts unless asked", "facts" in W.entity_list(BF, "ava", "kid", bubble_only="household")["entities"][0], False)
page = W.entity_page(BF, "household:biscuit", "zach", "admin")
v0 = page["facts"][0]["values"][0]
check("a value says when it was first seen and last confirmed", (v0["since"][:10], v0["confirmed"][:10]), ("2026-06-03", "2026-06-03"))
done = W.set_fact(BF, "Biscuit", "species", "dog", "zach", "admin", people=[{"id": "zach", "name": "Zach", "role": "admin"}])[0]
v1 = W.entity_page(BF, "household:biscuit", "zach", "admin")["facts"][0]["values"][0]
check("Still right? = restating the same value: confirmed moves to today, since stays",
      (done, v1["value"], v1["since"][:10], v1["confirmed"][:10]), (True, "dog", "2026-06-03", BF.now().date().isoformat()))
check("…the same value, not a second one", len(W.entity_page(BF, "household:biscuit", "zach", "admin")["facts"][0]["values"]), 1)

P = W.entity_page(BA, "household:maple-animal-clinic", "zach", "admin", labels)
ph = next(f for f in P["facts"] if f["attr"] == "phone")
check("Zach's page: the phone is disputed, with both values", (ph["state"], sorted(v["value"] for v in ph["values"])),
      ("disputed", ["(555) 0199", "555-0142"]))
ok("…each value has certainty, since and sources", all(v["certainty"] and v["since"].endswith("-07:00") and v["sources"] for v in ph["values"]))
ok("evidence: disputed claims contradict each other's sources",
   all(any(r["kind"] == "contradicts" for r in c["refs"]) for c in P["evidence"]["claims"] if c["status"] == "disputed"))
ok("a household-file source opens its KB note", any(src.get("open") == {"kb": "hh-contacts-vet-maple"} for src in P["evidence"]["sources"]))
ok("a bill source has nothing to open", all("open" not in src for src in P["evidence"]["sources"] if src["kind"] == "bill"))
check("links resolve to visible entities", P["links"], [{"attr": "serves", "id": "household:biscuit", "name": "Biscuit"}])
check("ask is reserved", {f["ask"] for f in P["facts"]}, {None})
ok("history is there for Zach", len(P["history"]) >= 2 and P["history"][0]["at"].endswith("-07:00"))
ok("#746: history's by is display-ready, never a raw id, with its kind",
   all(h["by_kind"] in ("person", "source", "titan") and not str(h["by"]).startswith(("source:", "person:"))
       and h["by"] != "zach" for h in P["history"]))
ok("#746: history moves between claim statuses", {h["to"] for h in P["history"]} <= {"current", "disputed", "proposed",
                                                                                      "superseded", "expired", "retracted", "rejected"})
check("#746: by in words", [W.by_words(b, {"zach": "Zach Hale", "p_ab12cd34": "Robin"}) for b in
                            ("source:bills", "source:notes", "source:calendar", "source:whatever", "zach", "p_ab12cd34",
                             "person:p_ab12cd34", "p_ffff0000", "person:gone", "memory-backfill", "", None)],
      [("a bill", "source"), ("a note", "source"), ("the calendar", "source"), ("titan", "source"),
       ("Zach", "person"), ("Robin", "person"), ("Robin", "person"), ("someone", "person"), ("someone", "person"),
       ("titan", "titan"), ("titan", "titan"), ("titan", "titan")])
K = W.entity_page(BA, "household:maple-animal-clinic", "ava", "kid", labels)
kph = next(f for f in K["facts"] if f["attr"] == "phone")
check("Ava's page: only the household value, no dispute", (kph["state"], [v["value"] for v in kph["values"]]), ("known", ["(555) 0199"]))
ok("…and no trace of the bill anywhere", "0142" not in json.dumps(K) and "August bill" not in json.dumps(K))
check("Zach's private entity doesn't exist for Ava", W.entity_page(BA, "zach:city-water", "ava", "kid"), None)
check("…nor for a shared household device", W.entity_page(BA, "zach:city-water", None, "household"), None)
check("an unknown id doesn't exist", W.entity_page(BA, "household:nope", "zach", "admin"), None)
# #1055: another adult and a shared household device, beside the kid's case above
for who, pid, role in (("Robin (another adult)", "p_robin", "adult"), ("a shared household device", None, "household")):
    ids = [r["id"] for r in W.entity_list(BA, pid, role)["entities"]]
    check(f"{who}: the list is the household's only, never Zach's private entity", ids,
          ["household:biscuit", "household:maple-animal-clinic"])
    check(f"{who}: Zach's private entity's page doesn't exist", W.entity_page(BA, "zach:city-water", pid, role), None)
    pg = W.entity_page(BA, "household:maple-animal-clinic", pid, role, labels)
    check(f"{who}: the household page has no trace of Zach's bill (its phone, its label)",
          ("0142" in json.dumps(pg), "August bill" in json.dumps(pg)), (False, False))
    check(f"{who}: a search can't find the private one", [r["id"] for r in W.entity_list(BA, pid, role, q="water")["entities"]], [])
import household_identity as HH  # noqa: E402
try:
    from fastapi.testclient import TestClient
    import agent_api_server as S2
    W._WORLD = BA
    tc = TestClient(S2.app)
    hdr = {"X-Agent-Token": S2._load_agent_token()} if S2._load_agent_token() else {}
    r = tc.get("/v1/world/entities", headers=hdr)
    check("GET /v1/world/entities answers", (r.status_code, "entities" in r.json(), "counts" in r.json()), (200, True, True))
    r = tc.get("/v1/world/entities/household:maple-animal-clinic", headers=hdr)
    check("GET a page", (r.status_code, r.json()["name"]), (200, "Maple Animal Clinic"))
    r = tc.get("/v1/world/entities/household:nope", headers=hdr)
    check("a missing page is a 404 with {error}", (r.status_code, r.json()), (404, {"error": "not found"}))
    r = tc.get("/v1/world/entities?group=bogus", headers=hdr)
    check("a bad group is a 400 with {error}", (r.status_code, "error" in r.json()), (400, True))
    r = tc.post("/v1/world/entities/household:maple-animal-clinic/facts", headers=hdr,
                json={"attribute": "phone", "value": "555-0300"})
    check("POST a fact: the updated page comes back", (r.status_code, "555-0300" in json.dumps(r.json().get("page"))), (200, True))
    r = tc.post("/v1/world/entities/household:maple-animal-clinic/facts", headers=hdr, json={"attribute": "phone"})
    check("…a missing value is a 400", r.status_code, 400)
    r = tc.post("/v1/world/entities/household:nope/facts", headers=hdr, json={"attribute": "phone", "value": "1"})
    check("…an unknown entity is a 404", r.status_code, 404)
except ImportError as exc:
    print(f"skip API route checks: {exc}")
ok("every role may call the routes", all(HH.route_allowed(r, "GET", "/v1/world/entities") and
                                          HH.route_allowed(r, "GET", "/v1/world/entities/household:biscuit")
                                          for r in ("adult", "kid", "household")))

# ── set_fact: statements and corrections (#358); parents manage kids' schedules ──────────────────

clock = Clock("2026-10-01T12:00")
BS = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=clock)
FOLKS = [{"id": "zach", "name": "Zach", "role": "admin", "parents": []},
         {"id": "robin", "name": "Robin", "role": "adult", "parents": []},
         {"id": "jordan", "name": "Jordan", "role": "adult", "parents": []},
         {"id": "ava", "name": "Ava", "role": "kid", "parents": ["zach", "robin"]}]
BS.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                          value="(555) 0199", observed_at="2026-06-03", kind="household", ref="hh#phone",
                          origin="household.yaml", label="the household file", owner=None, audience="household",
                          aliases=(("vet", ""),), bubble="household", facet="home")])
BS.sync("calendar", W.calendar_observations(ballet, prov, local_time.zone(), assigned={"ava": "ava"},
                                            parents={"ava": ["zach", "robin"]}))
clock.set("2026-10-01T13:00")
done, said, _ = W.set_fact(BS, "the vet", "phone number", "555-0142", "robin", "adult", people=FOLKS)
ok("an adult corrects a household fact", done and "555-0142" in said)
f = {x.attr: x for x in BS.facts("household:maple-animal-clinic", "ava", "kid")}["phone"]
check("…everyone now gets the new number, stated by Robin", ([v.value for v in f.values], f.values[0].word),
      (["555-0142"], "stated"))
check("…and the old one is superseded", [st for st, in BS.world("household").con.execute(
    "SELECT status FROM claims WHERE value_norm='5550199'")], ["superseded"])
# a kid's statement is proposed until a parent confirms (#358): asked by card, held through quiet hours
CARDS = []
W.SEND_ASK = lambda title, detail, **kw: CARDS.append({"title": title, **kw})
clock.set("2026-10-01T23:30")
done, said, why = W.set_fact(BS, "the vet", "phone", "555-9999", "ava", "kid", people=FOLKS)
check("a kid's household statement is proposed, and she hears who'll check", (done, why, "Zach or Robin" in said),
      (True, "proposed", True))
f = {x.attr: x for x in BS.facts("household:maple-animal-clinic", "zach", "admin")}["phone"]
check("…titan keeps answering with the record", [v.value for v in f.values], ["555-0142"])
check("…and at 11:30 PM nobody is asked yet", CARDS, [])
clock.set("2026-10-02T07:05")
check("in the morning the card goes out", W.send_asks(BS), 1)
check("…to her parents, with yes and no", (CARDS[0]["to"], CARDS[0]["source"], [a["label"] for a in CARDS[0]["actions"]]),
      (["robin", "zach"], "world-proposal", ["Yes, that's right", "No"]))
check("…once", W.send_asks(BS), 0)
yes = CARDS[0]["actions"][0]["value"]
check("Ava can't answer her own", W.answer_ask(BS, yes, "ava", "kid", FOLKS)[0], False)
check("…nor Jordan, who it isn't for", W.answer_ask(BS, yes, "jordan", "adult", FOLKS)[0], False)
done, said = W.answer_ask(BS, yes, "robin", "adult", FOLKS)
ok("Robin confirms", done and "555-9999" in said)
f = {x.attr: x for x in BS.facts("household:maple-animal-clinic", "ava", "kid")}["phone"]
check("…now it's the record, stated", ([v.value for v in f.values], f.values[0].word), (["555-9999"], "stated"))
check("…and Zach's tap after hers is too late", W.answer_ask(BS, yes, "zach", "admin", FOLKS), (False, "Someone already answered that."))
clock.set("2026-10-02T07:10")
t_rej = clock.t
CARDS.clear()
done, said, why = W.set_fact(BS, "the vet", "phone", "555-7777", "ava", "kid", people=FOLKS)
check("in the day the card goes out at once", len(CARDS), 1)
done, said = W.answer_ask(BS, CARDS[0]["actions"][1]["value"], "zach", "admin", FOLKS)
ok("Zach says no", done and "won't use it" in said)
check("…the record stands", [v.value for v in {x.attr: x for x in BS.facts("household:maple-animal-clinic")}["phone"].values],
      ["555-9999"])
CARDS.clear()
done, said, why = W.set_fact(BS, "the vet", "phone", "555-7777", "ava", "kid", people=FOLKS)
check("saying a turned-down value again doesn't ask again", (why, W.send_asks(BS), CARDS), ("rejected", 0, []))
done, said, why = W.set_fact(BS, "the vet", "phone", "555-9999", "ava", "kid", people=FOLKS)
check("saying what's on record: nothing to ask", why, "ok")
check("a proposal (and its rejection) never shows as a change",
      [c["diff"] for c in BS.changes(["household:maple-animal-clinic"], t_rej, "zach", "admin")], [])
done, said, _ = W.set_fact(BS, "ballet", "when", "every Thursday, 4:30 PM–6:00 PM", "robin", "adult", people=FOLKS)
ok("a parent changes her child's schedule", done and "Thursday" in said)
kf = {x.attr: x for x in BS.facts("ava:ballet-modern", "ava", "kid")}["schedule"]
check("…Ava sees the new day", [v.value for v in kf.values], ["every Thursday, 4:30 PM–6:00 PM"])
done, said, _ = W.set_fact(BS, "ballet", "when", "every Monday", "jordan", "adult", people=FOLKS)
check("an adult who isn't a parent: the schedule doesn't exist for her", (done, "on record" in said), (False, True))
done, said, _ = W.set_fact(BS, "ballet", "note", "bring tights", "robin", "adult", people=FOLKS)
check("a parent manages schedules, not a child's other facts", (done, "to change" in said), (False, True))
done, said, _ = W.set_fact(BS, "Aunt Jo", "phone", "555 0200", "robin", "adult", "person", people=FOLKS)
ok("something new: created in the speaker's own bubble", done and "new: Aunt Jo" in said and "robin" in BS.bubbles())
check("…which nobody else sees", BS.find("aunt jo's number", "zach", "admin"), [])
done, said, _ = W.set_fact(BS, "Cousin Al", "phone", "555", "robin", "adult", people=FOLKS)
check("unknown and untyped: asks what it is", (done, "person, a pet" in said), (False, True))
done, said, _ = W.set_fact(BS, "the vet", "favourite colour", "blue", "robin", "adult", people=FOLKS)
check("an attribute titan doesn't keep: says which it can", (done, "phone" in said), (False, True))
# review of #440: household members' facts, and health and money at write time
FOLKS.append({"id": "leo", "name": "Leo", "role": "kid", "parents": ["zach", "robin"]})
for pid, nm in (("robin", "Robin"), ("ava", "Ava"), ("leo", "Leo")):
    BS.ensure(W.entity_id(nm), "person", nm, "household", person_id=pid)
done, said, why = W.set_fact(BS, "Leo", "allergies", "peanuts", "zach", "admin", people=FOLKS)
ok("a parent records his kid's allergy", done and why == "ok")
check("…Leo's allergy reaches him and both parents only",
      {p: bool([f for f in BS.facts("household:leo", p, r) if f.attr == "allergies"])
       for p, r in (("leo", "kid"), ("zach", "admin"), ("robin", "adult"), ("ava", "kid"), ("jordan", "adult"))},
      {"leo": True, "zach": True, "robin": True, "ava": False, "jordan": False})
check("…and not the shared household device", [f.attr for f in BS.facts("household:leo", None, "household")], [])
done, said, why = W.set_fact(BS, "Robin", "phone", "555 0101", "zach", "admin", people=FOLKS)
check("Zach can't change Robin's facts", (done, why, "Robin's to change" in said), (False, "denied", True))
done, said, why = W.set_fact(BS, "Robin", "allergies", "penicillin", "robin", "adult", people=FOLKS)
ok("Robin records her own allergy", done)
check("…an adult's health stays with her alone", {p: bool([f for f in BS.facts("household:robin", p, r) if f.attr == "allergies"])
                                                  for p, r in (("robin", "adult"), ("zach", "admin"))},
      {"robin": True, "zach": False})
done, said, why = W.set_fact(BS, "Ava", "school", "Lincoln Middle", "robin", "adult", people=FOLKS)
ok("Robin, a parent, sets Ava's school", done)
done, said, why = W.set_fact(BS, "ballet", "pickup", "Robin", "robin", "adult", people=FOLKS)
ok("…and Ava's ballet pickup", done)
done, said, why = W.set_fact(BS, "Ava", "school", "Roosevelt", "jordan", "adult", people=FOLKS)
check("Jordan, not a parent, can't change Ava's facts", (done, why), (False, "denied"))
BS.sync("household", [obs(entity="city-water", etype="org", name="City Water", attr="phone", value="555 0300",
                          observed_at="2026-09-01", kind="household", ref="cw#phone", origin="household.yaml",
                          owner=None, audience="household", bubble="household", facet="home")])
done, said, why = W.set_fact(BS, "City Water", "billing", "autopay on the 12th", "robin", "adult", people=FOLKS)
check("money on a household entity reaches the adults, not the kids",
      ({f.attr for f in BS.facts("household:city-water", "jordan", "adult")} >= {"billing"},
       "billing" in {f.attr for f in BS.facts("household:city-water", "ava", "kid")}), (True, False))
CARDS.clear()
done, said, why = W.set_fact(BS, "Ava", "allergies", "cats", "ava", "kid", people=FOLKS)
check("a kid's own health detail: proposed, asked of her parents", (why, len(CARDS), CARDS[0]["to"]),
      ("proposed", 1, ["robin", "zach"]))
check("…and not the record until then", [f.attr for f in BS.facts("household:ava", "ava", "kid") if f.attr == "allergies"], [])
check("…Jordan can't confirm what she can't see",
      W.answer_ask(BS, CARDS[0]["actions"][0]["value"], "jordan", "adult", FOLKS)[0], False)
W.answer_ask(BS, CARDS[0]["actions"][0]["value"], "zach", "admin", FOLKS)
check("…confirmed by Zach: Ava and her parents see it, Jordan doesn't",
      {p: bool([f for f in BS.facts("household:ava", p, r) if f.attr == "allergies"])
       for p, r in (("ava", "kid"), ("robin", "adult"), ("jordan", "adult"))},
      {"ava": True, "robin": True, "jordan": False})
# review of #462: a budget per kid per day, one pending ask per fact, admins when a kid has no parents set
CARDS.clear()
clock.set("2026-10-03T09:00")
BS.sync("household", [obs(entity=f"shop-{i}", etype="org", name=f"Shop {i}", attr="phone", value=f"555 01{i:02d}",
                          observed_at="2026-09-01", kind="household", ref=f"s{i}", origin="household.yaml",
                          owner=None, audience="household", bubble="household", facet="home") for i in range(20)])
for i in range(20):
    W.set_fact(BS, f"Shop {i}", "phone", f"555 99{i:02d}", "ava", "kid", people=FOLKS)
check("a burst of 20 statements sends 5 cards", len(CARDS), 5)
check("…the rest wait, still proposed", BS.world("household").con.execute(
    "SELECT COUNT(*) FROM asks WHERE said_by='ava' AND state='held'").fetchone()[0], 15)
clock.set("2026-10-04T08:00")
check("…tomorrow's budget sends 5 more", W.send_asks(BS), 5)
CARDS.clear()
clock.set("2026-10-05T09:00")
for v in ("555 7001", "555 7002", "555 7003"):     # Leo: Ava's held asks have today's budget
    W.set_fact(BS, "Shop 0", "address", v, "leo", "kid", people=FOLKS)
CARDS[:] = [c for c in CARDS if "Leo" in c["title"]]
pending = BS.world("household").con.execute(
    "SELECT COUNT(*) FROM asks a JOIN claims c ON c.id=a.claim_id WHERE c.entity_id='shop-0' AND c.attr='address' "
    "AND a.state IN ('held','sent')").fetchone()[0]
check("one pending ask per fact: the newest statement replaces it", pending, 1)
check("…on one card, updated in place", len({c["key"] for c in CARDS}), 1)
check("…which asks about the newest", "555 7003" in CARDS[-1]["title"], True)
check("…an old button is stale", W.answer_ask(BS, CARDS[0]["actions"][0]["value"], "zach", "admin", FOLKS),
      (False, "There's a newer question about that."))
FOLKS.append({"id": "nia", "name": "Nia", "role": "kid", "parents": []})
BS.ensure("nia", "person", "Nia", "household", person_id="nia")
CARDS.clear()
done, said, why = W.set_fact(BS, "Nia", "allergies", "dust", "nia", "kid", people=FOLKS)
check("a kid with no parents set: her own health goes to the admins, not every adult", (why, CARDS[0]["to"]),
      ("proposed", "admins"))
check("…Jordan can't confirm it", W.answer_ask(BS, CARDS[0]["actions"][0]["value"], "jordan", "adult", FOLKS)[0], False)
ok("…Zach can", W.answer_ask(BS, CARDS[0]["actions"][0]["value"], "zach", "admin", FOLKS)[0])
check("…and then Nia and Zach see it, Jordan doesn't",
      {p: bool([f for f in BS.facts("household:nia", p, r) if f.attr == "allergies"])
       for p, r in (("nia", "kid"), ("zach", "admin"), ("jordan", "adult"))},
      {"nia": True, "zach": True, "jordan": False})
W.SEND_ASK = None
agree = "[MEMORY]\n• The vet's number is 555-9999 (noted today)\n• Robin likes tea"
kept, newer = W.split_recalled(agree, BS, ["household:maple-animal-clinic"])
check("a memory fact that agrees with the record isn't flagged as newer", (newer, "555-9999" in kept), ({}, False))

# ── memory facts → claims (#358): corrections become set_fact, new facts with a value proposals ─────

mclock = Clock("2026-10-02T12:00")
BM = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=mclock)
BM.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                          value="(555) 0142", observed_at="2026-09-01", kind="household", ref="hh#vet",
                          origin="household.yaml", owner=None, audience="household", aliases=(("vet", ""),),
                          bubble="household", facet="home"),
                      obs(entity="swim-lessons", etype="activity", name="Swim lessons", attr="schedule",
                          value="every Thursday, 4:30 PM", observed_at="2026-09-01", kind="household", ref="hh#swim",
                          origin="household.yaml", owner=None, audience="household", aliases=(("swim", ""),),
                          bubble="household", facet="home")])
EV = "model:qwen3-8b"
got = W.from_memory(BM, "zach", "admin", FOLKS, [], [("The vet's number is (555) 0142.", "The vet's number is now 555-0199.")],
                    "no, the vet's number is 555-0199 now", EV)
check("a memory correction of a fact that backs a claim becomes Zach's set_fact",
      [(c, a, code) for c, a, _, code in got], [("household:maple-animal-clinic", "phone", "ok")])
f = {x.attr: x for x in BM.facts("household:maple-animal-clinic", "ava", "kid")}["phone"]
check("…everyone now gets it, stated", ([v.value for v in f.values], f.values[0].word), (["555-0199"], "stated"))
check("a value the person never said isn't used (the extractor can't invent one)",
      W.from_memory(BM, "zach", "admin", FOLKS, [], [("The vet's number is 555-0199.", "The vet's number is now 555-0177.")],
                    "the vet changed numbers again", EV), [])
check("an old fact that didn't back a claim: nothing",
      W.from_memory(BM, "zach", "admin", FOLKS, [], [("The vet is friendly.", "The vet's number is 555-0123.")],
                    "the vet's number is 555-0123", EV), [])
got = W.from_memory(BM, "zach", "admin", FOLKS, ["Swim lessons are every Friday at 5 pm."], [],
                    "they moved to fridays at 5pm", EV)    # his words don't name it: a proposal
check("a new memory fact with a value: a proposal", [(a, v, code) for _, a, v, code in got],
      [("schedule", "every Friday, 5:00 PM", "proposed")])
sf = {x.attr: x for x in BM.facts("household:swim-lessons", "zach", "admin")}["schedule"]
check("…the record still answers Thursday", [v.value for v in sf.values], ["every Thursday, 4:30 PM"])
pr = BM.world("household").con.execute("SELECT c.status, e.kind, e.label FROM claims c JOIN evidence e ON e.claim_id=c.id "
                                        "WHERE c.value='every Friday, 5:00 PM'").fetchone()
check("…extracted, naming its evaluator", (pr["status"], pr["kind"], EV in pr["label"]), ("proposed", "memory", True))
check("what the record already says isn't proposed again",
      W.from_memory(BM, "zach", "admin", FOLKS, ["Swim lessons are every Thursday at 4:30 pm."], [],
                    "swim is thursdays at 4:30", EV), [])
BM.ensure("city-water", "org", "City Water", "household")
check("a sentence naming two things that could hold it: nothing",
      W.from_memory(BM, "zach", "admin", FOLKS, ["The vet and City Water: 555-0100."], [], "vet and city water 555-0100", EV), [])
check("a shared device: nothing", W.from_memory(BM, None, "household", FOLKS, ["Swim is every Monday at 3 pm."], [],
                                               "swim mondays at 3pm", EV), [])
W.set_fact(BM, "swim", "schedule", "every Friday, 5:00 PM", "robin", "adult", people=FOLKS)
sf = {x.attr: x for x in BM.facts("household:swim-lessons", "ava", "kid")}["schedule"]
check("Robin says it: the proposal's value becomes current, as her statement",
      ([v.value for v in sf.values], sf.values[0].word), (["every Friday, 5:00 PM"], "stated"))
CARDS = []
W.SEND_ASK = lambda title, detail, **kw: CARDS.append(title)
got = W.from_memory(BM, "ava", "kid", FOLKS, [], [("The vet's number is 555-0199.", "The vet's number is 555-0111.")],
                    "the vet's number is 555-0111 now", EV)
check("a kid's memory correction waits for a parent, as her statements do", ([c for *_, c in got], len(CARDS)),
      (["proposed"], 1))
W.SEND_ASK = None
# #508: the place named in the person's own words takes the value, as their statement; the person in the sentence
# doesn't get the place's phone
BM.ensure("nova-birch-rd-pharmacy", "org", "NOVA Birch Rd pharmacy", "household")
BM.ensure("nova-health-dental", "org", "NOVA Health dental", "household")
BM.ensure("robin", "person", "Robin", "household", person_id="robin")
line = ("The household has a pickup of Robin's prescriptions from NOVA Birch Rd pharmacy at 1502 N Birch Rd "
        "with the phone number 509-503-1444.")
got = W.from_memory(BM, "zach", "admin", FOLKS, [line], [],
                    "pick up Robin's prescriptions at NOVA Birch Rd pharmacy, 1502 N Birch Rd, 509-503-1444", EV)
check("the pharmacy gets its address and phone as Zach's statements; Robin gets no phone",
      sorted((c, a, code) for c, a, _, code in got),
      [("household:nova-birch-rd-pharmacy", "address", "ok"), ("household:nova-birch-rd-pharmacy", "phone", "ok")])
cf = {x.attr: x for x in BM.facts("household:nova-birch-rd-pharmacy", "zach", "admin")}["address"]
check("…stated, so it answers", ([v.value for v in cf.values], cf.values[0].word), (["1502 N Birch Rd"], "stated"))
check("“NOVA” finds both branches", sorted(BM.find("where do I pick up from NOVA?", "zach", "admin")),
      ["household:nova-birch-rd-pharmacy", "household:nova-health-dental"])
blk, _ = W.context(BM, "Where is NOVA for Robin's prescriptions?", "zach", "admin")
ok("…and the turn is told to pick by purpose or ask, never from search or a text",
   "Several places on record answer to “NOVA”" in blk and "1502 N Birch Rd" in blk)
check("a person's phone only when the sentence says it's theirs", (W._phone_is_theirs("Robin", line),
      W._phone_is_theirs("Robin", "Robin's cell is 509-555-0101")), (False, True))
check("the backfill (no words at hand) only proposes",
      [c[3] for c in W.from_memory(BM, "zach", "admin", FOLKS, ["Swim lessons are every Monday at 3 pm."], [],
                                   "Swim lessons are every Monday at 3 pm.", "backfill", statements=False)], ["proposed"])
# the week's corrections digest: a corrected reply about a fact titan holds → a proposal and an ask to the admin
CARDS = []
W.SEND_ASK = lambda title, detail, **kw: CARDS.append((title, kw["to"]))
got = W.from_correction(BM, "zach", "admin", FOLKS, "Swim lessons are every Friday at 5:00 PM.",
                        "no, swim moved to Tuesdays at 5:00 PM")
check("a correction of a stated value: proposed, and Zach is asked", ([c for *_, c in got], CARDS),
      (["proposed"], [("You corrected titan: Swim lessons is every Tuesday, 5:00 PM?", ["zach"])]))
check("…the record doesn't change until he says yes",
      [v.value for v in {x.attr: x for x in BM.facts("household:swim-lessons", "zach", "admin")}["schedule"].values],
      ["every Friday, 5:00 PM"])
check("a correction that agrees with the record: nothing",
      W.from_correction(BM, "zach", "admin", FOLKS, "Swim is every Monday at 5:00 PM.", "no, every Friday at 5:00 PM"), [])
check("a reply that stated no value of that kind: nothing",
      W.from_correction(BM, "zach", "admin", FOLKS, "I don't know when swim is.", "it's Wednesdays at 5:00 PM"), [])
check("a kid's corrections aren't proposed this way", W.from_correction(
    BM, "ava", "kid", FOLKS, "Swim is every Friday at 5:00 PM.", "no, Mondays at 5:00 PM"), [])
W.SEND_ASK = None

# ── KB notes and scans → proposals, overnight (#358): read in code, owner and audience from the note ──

nclock = Clock("2026-10-02T02:30")
BN = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=nclock)
BN.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                          value="(555) 0142", observed_at="2026-09-01", kind="household", ref="hh#vet",
                          origin="household.yaml", owner=None, audience="household", aliases=(("vet", ""),),
                          bubble="household", facet="home")])
BN.sync("calendar", [obs(entity="ava-therapy", etype="activity", name="Ava Therapy", attr="schedule",
                         value="every Monday, 3:00 PM", observed_at="2026-09-28", kind="calendar", ref="cal#kt",
                         origin="calendar:ava", owner="ava", audience='["ava","robin","zach"]', bubble="ava")])
ROLES = {"zach": "admin", "robin": "adult", "jordan": "adult", "ava": "kid"}
NOTES = [("n1", "Vet info", "Maple Animal Clinic moved. The vet's new number is 555-0177.", {}, "zach", "household",
          "2026-09-20T10:00:00"),
         ("n2", "Errands", "Call the vet and Ava Therapy at 555-0100.", {}, "zach", "private", "2026-09-21"),
         ("n3", "Runbook", "The vet's number is 555-0999.", {"origin": "kb_seed_runbook"}, None, "admins", "2026-09-21"),
         ("hh-contacts-vet", "Vet", "phone: 555-0111", {}, None, "household", "2026-09-21"),
         ("n4", "Jordan's list", "Ava Therapy is every Tuesday at 4 pm.", {}, "jordan", "private", "2026-09-22"),
         ("n5", "Scan: vet receipt", "Maple Animal Clinic, 42 Oak Street. Phone (555) 0142.",
          {"source": "scan"}, "robin", "adults", "2026-09-23")]
ob = W.note_observations(BN, NOTES, ROLES)
check("notes → proposals: only one-entity sentences with a shaped value, never titan's notes or the household file",
      sorted((o.ref.split("#")[0], o.attr, o.value, o.kind) for o in ob),
      [("n1", "phone", "555-0177", "note"), ("n5", "address", "42 Oak Street", "scan"), ("n5", "phone", "(555) 0142", "scan")])
check("…owner, audience and date from the note; proposals only",
      {(o.owner, o.audience, o.observed_at, o.proposes) for o in ob if o.ref.startswith("n1")},
      {("zach", "household", "2026-09-20", True)})
check("…Jordan's note names Ava Therapy, which she can't see: nothing", [o for o in ob if o.ref.startswith("n4")], [])
BN.sync("notes", ob)
pf = {x.attr: x for x in BN.facts("household:maple-animal-clinic", "ava", "kid")}["phone"]
check("the record still answers (555) 0142", [v.value for v in pf.values], ["(555) 0142"])
con = BN.world("household").con
check("…555-0177 waits as a proposal", [r[0] for r in con.execute("SELECT status FROM claims WHERE value='555-0177'")],
      ["proposed"])
check("…the scan's matching number adds nothing to the record's certainty", pf.values[0].word, "stated")
cid177 = con.execute("SELECT id FROM claims WHERE value='555-0177'").fetchone()[0]
BN.world("household").reject(cid177, by="zach")
BN.sync("notes", ob)
check("a value a person turned down stays turned down on the next night's read",
      [r[0] for r in con.execute("SELECT status FROM claims WHERE value='555-0177'")], ["rejected"])
BN.sync("notes", [o for o in ob if not o.ref.startswith("n5")])
check("a note that's gone takes its proposals with it",
      [r[0] for r in con.execute("SELECT status FROM claims WHERE value='42 Oak Street'")], ["retracted"])
# the first live run (10-01): synced mail read as notes, any date near a name taken as a birthday, a billing
# company's number taken as Robin's
for pid, nm in (("zach", "Zach"), ("robin", "Robin"), ("ava", "Ava")):
    BN.ensure(W.entity_id(nm), "person", nm, "household", person_id=pid)
LIVE = [("cx-mail-1", "Meeting with Zach 12pm(MT)", "Meeting with Zach on Sep 24 at 12pm.",
         {"source_type": "connector:mail"}, "zach", "private", "2026-09-24"),
        ("n6", "Errand", "Meeting with Zach on Sep 24.", {"source_type": "note"}, "zach", "private", "2026-09-24"),
        ("n7", "Bill Pay", "Auto pay failed for Robin. Call 509-555-7676 to fix it.", {"source_type": "note"},
         "zach", "private", "2026-09-24"),
        ("n8", "Family", "Zach's birthday is October 2. Robin's cell is 509-555-0101.", {"source_type": "note"},
         "zach", "private", "2026-09-24")]
check("only notes people wrote; a birthday needs the word; a person's phone needs a phone word and their name",
      sorted((o.ref.split("#")[0], o.name, o.attr, o.value) for o in W.note_observations(BN, LIVE, ROLES)),
      [("n8", "Robin", "phone", "509-555-0101"), ("n8", "Zach", "birthday", "October 2")])
check("…the memory path too: a recital date isn't a birthday",
      W.from_memory(BN, "zach", "admin", FOLKS, ["Ava has a recital on March 3."], [], "ava has a recital on march 3",
                    "model:qwen3-8b"), [])
MAIL = [("cx-mail-9", "Your pharmacy order is ready",
         "From: NOVA <noreply@x>\nSubject: Your pharmacy order is ready\nDate: Fri, 2 Oct 2026\n\n"
         "Maple Animal Clinic has moved to 77 Pine Street. Call Maple Animal Clinic at (509) 555-0188. "
         "Robin's birthday is May 2. Robin can call 509-555-7777.", {"source_type": "connector:mail"},
         "zach", "private", "2026-10-02")]
mo = W.note_observations(BN, MAIL, ROLES, mail=True)
check("mail lookups: a business's phone and address, as proposals of kind mail; never a person's details",
      sorted((o.name, o.attr, o.value, o.kind, o.proposes) for o in mo),
      [("Maple Animal Clinic", "address", "77 Pine Street", "mail", True),
       ("Maple Animal Clinic", "phone", "(509) 555-0188", "mail", True)])
check("…and the notes reader still ignores mail", W.note_observations(BN, MAIL, ROLES), [])
# #650: "Google's phone is…" came from a Google Ads email. A list's or a marketer's mail proposes nothing.
PROMO = [("cx-mail-12", "Your ad credit", "From: Google Ads <ads-noreply@google.com>\nSubject: Your ad credit\n\n"
          "Maple Animal Clinic has moved to 77 Pine Street. Call Maple Animal Clinic at (509) 555-0188.",
          {"source_type": "connector:mail"}, "zach", "private", "2026-10-02"),
         ("cx-mail-13", "News", "From: Maple Animal Clinic <care@maple.example>\nSubject: News\n\n"
          "Call Maple Animal Clinic at (509) 555-0188. Unsubscribe from these emails.",
          {"source_type": "connector:mail"}, "zach", "private", "2026-10-02")]
check("a marketer's or a list's mail proposes nothing (#650), though the same words from a person would",
      W.note_observations(BN, PROMO, ROLES, mail=True), [])
BN.ensure("apple", "org", "Apple", "household")
BN.ensure("turbotenant", "org", "TurboTenant", "household")
NOISE = [("cx-mail-10", "Your receipt from Apple", "From: Apple\nSubject: Your receipt\n\nApple billing address: 12006 E Lenora Dr. "
          "Apple order ID 7338312. © 2026 Apple, One Apple Park Way.", {"source_type": "connector:mail"}, "zach", "private",
          "2026-10-02"),
         ("cx-mail-11", "TurboTenant", "From: TT\n\nTurboTenant: rent for the home at 12006 E Lenora Dr is due. "
          "TurboTenant office is at 320 E Vine Dr. Call TurboTenant at 1759739. TurboTenant account number 71171852671.",
          {"source_type": "connector:mail"}, "zach",
          "private", "2026-10-02")]
check("the 10-02 dry run's mistakes: billing addresses, order numbers, footers; only a place's own address stays",
      sorted((o.name, o.attr, o.value) for o in W.note_observations(BN, NOISE, ROLES, mail=True)),
      [("TurboTenant", "address", "320 E Vine Dr")])
W.set_fact(BN, "the vet", "address", "42 Oak Street", "robin", "adult", people=FOLKS)
af = {x.attr: x for x in BN.facts("household:maple-animal-clinic", "ava", "kid")}["address"]
check("a person saying what a note proposed makes it current, as their statement",
      ([v.value for v in af.values], af.values[0].word), (["42 Oak Street"], "stated"))

# 10-01 live: retracting proposals hid the household's people (moved_away saw claims with no evidence)
mclock2 = Clock("2026-10-02T12:00")
BX = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=mclock2)
BX.ensure("ava", "person", "Ava", "household", person_id="ava")
BX.ensure("maple-animal-clinic", "org", "Maple Animal Clinic", "household", aliases=[("vet", "")])
XN = [("x1", "Family", "Ava's birthday is March 14. The vet's number is 555-0123.", {"source_type": "note"},
       "zach", "household", "2026-09-30")]
BX.sync("notes", W.note_observations(BX, XN, ROLES))
BX.sync("notes", [])
check("proposals that came and went leave a household member and an entity findable",
      sorted(BX.find("ava and the vet", "zach", "admin")), ["household:ava", "household:maple-animal-clinic"])

# ── the daily question (#484; Zach, 10-01: 09:00, adults only, pause after 3 ignored) ─────────────

dclock = Clock("2026-10-03T08:00")
BD = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=dclock)
BD.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                          value="(555) 0142", observed_at="2026-09-01", kind="household", ref="hh#vet",
                          origin="household.yaml", owner=None, audience="household", aliases=(("vet", ""),),
                          bubble="household", facet="home")])
DN = [("n%d" % i, "Vet", f"The vet's number is 555-01{i:02d}.", {"source_type": "note"}, "zach", "household",
       "2026-09-2%d" % i) for i in range(1, 6)]
BD.sync("notes", W.note_observations(BD, DN[:1], ROLES))
CARDS = []
send = lambda title, detail, **kw: CARDS.append((title, kw["to"], kw["actions"][0]["value"]))
check("before 09:00: nothing", W.daily_questions(BD, FOLKS, send=send), 0)
dclock.set("2026-10-02T23:00")
check("quiet hours (from 22:00): nothing", W.daily_questions(BD, FOLKS, send=send), 0)
dclock.set("2026-10-03T09:00")
check("at 09:00 Zach, whose note proposed it, is asked; nobody else has a proposal", W.daily_questions(BD, FOLKS, send=send), 1)
check("…a card to him alone", [(t, to) for t, to, _ in CARDS],
      [("Is this right? Maple Animal Clinic's phone is 555-0101", ["zach"])])
check("…once a day", W.daily_questions(BD, FOLKS, send=send), 0)
W.answer_ask(BD, CARDS[0][2], "zach", "admin", FOLKS)
check("Yes: it's the record now", [v.value for v in {x.attr: x for x in BD.facts("household:maple-animal-clinic")}["phone"].values],
      ["555-0101"])
BD.sync("notes", W.note_observations(BD, DN, ROLES))
sent = []
for day in ("04", "05", "06", "07"):
    dclock.set(f"2026-10-{day}T09:00")
    CARDS.clear()
    W.daily_questions(BD, FOLKS, send=send)
    sent.append(len(CARDS))
    if day == "04":
        dclock.set("2026-10-04T15:00")
        check("an open ask of his: no second question that day", W.daily_questions(BD, FOLKS, send=send), 0)
check("ignored days still get the next day's question; after 3 ignored in a row titan stops asking him",
      sent, [1, 1, 1, 0])
old = BD.world("household").con.execute("SELECT a.claim_id FROM asks a WHERE a.state='expired' LIMIT 1").fetchone()[0]
ok("an ignored card still works when he gets to it", W.answer_ask(BD, f"confirm household:{old}", "zach", "admin", FOLKS)[0])
dclock.set("2026-10-08T09:00")
CARDS.clear()
W.daily_questions(BD, FOLKS, send=send)
check("…and answering one starts the questions again", len(CARDS), 1)
# kids too (Zach, 10-01 revision): their own bubble only, never their health or contact details
kid = {"id": "ava", "name": "Ava", "role": "kid", "parents": ["zach", "robin"]}
BD.sync("ava-cal", [obs(entity="piano", etype="activity", name="Piano", attr="schedule", value="every Monday, 4:00 PM",
                         observed_at="2026-09-01", kind="calendar", ref="c#piano", origin="calendar:ava", owner="ava",
                         audience="private", bubble="ava"),
                     obs(entity="ava", etype="person", name="Ava", attr="phone", value="509-555-0100",
                         observed_at="2026-09-01", kind="contact", ref="c#kp", origin="contacts", owner="ava",
                         audience="private", bubble="ava")])
KN = [("k1", "Vet", "The vet's number is 555-0177.", {"source_type": "note"}, "ava", "household", "2026-10-01"),
      ("k2", "Piano", "Piano is every Tuesday at 4 pm now.", {"source_type": "note"}, "ava", "private", "2026-10-01"),
      ("k3", "Me", "My phone number: Ava's cell is 509-555-0199.", {"source_type": "note"}, "ava", "private", "2026-10-01")]
kob = W.note_observations(BD, KN, ROLES)
BD.sync("notes", W.note_observations(BD, DN[:5], ROLES) + kob)
dclock.set("2026-10-09T09:00")
CARDS.clear()
W.daily_questions(BD, [kid], send=send)
check("a kid gets one, about her own bubble only: not the household vet, not her own phone",
      [(t, to) for t, to, _ in CARDS], [("Is this right? Piano is every Tuesday, 4:00 PM", ["ava"])])
done, said = W.answer_ask(BD, CARDS[0][2], "ava", "kid", FOLKS)
check("…she answers it herself", (done, [v.value for v in {x.attr: x for x in BD.facts("ava:piano", "ava", "kid")}["schedule"].values]),
      (True, ["every Tuesday, 4:00 PM"]))
kp = BD.world("ava").con.execute("SELECT id FROM claims WHERE value='509-555-0199'").fetchone()
if kp:
    W.queue_ask(BD.world("ava"), kp[0], "ava", W._aud.normalize(["ava"]), "t", "d")
    check("…but never confirms her own contact details", W.answer_ask(BD, f"confirm ava:{kp[0]}", "ava", "kid", FOLKS),
          (False, "Only a parent can answer that."))

# ── disputes and stale facts in the daily slot (#360): only to someone who can see every side shown ─────

qclock = Clock("2026-10-03T08:00")
BQ = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=qclock)
BQ.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                          value="(555) 0199", observed_at="2026-06-03", kind="household", ref="hh#vet",
                          origin="household.yaml", owner=None, audience="household", aliases=(("vet", ""),),
                          bubble="household"),
                      obs(entity="city-water", etype="org", name="City Water", attr="address", value="18 Alder St",
                          observed_at="2022-03-01", kind="household", ref="hh#cw", origin="household.yaml", owner=None,
                          audience="household", bubble="household")])
BQ.sync("bills", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                      value="(555) 0142", observed_at="2026-08-02", kind="bill", ref="b#aug", origin="bill:maple",
                      owner="zach", audience="private", bubble="household")])
QF = [{"id": "zach", "role": "admin"}, {"id": "jordan", "role": "adult"},
      {"id": "ava", "role": "kid", "parents": ["zach", "robin"]}]
QC = []
qsend = lambda title, detail, **kw: QC.append((kw["to"], title, detail, [(a["label"], a["value"]) for a in kw["actions"]]))
qclock.set("2026-10-03T09:00")
W.daily_questions(BQ, QF, send=qsend)
z = [c for c in QC if c[0] == ["zach"]]
check("Zach, who sees both numbers, gets one card: which is right", (len(z), z[0][1] if z else None),
      (1, "Which is right? Maple Animal Clinic's phone"))
check("…its buttons are the two values and Not sure", sorted(l for l, _ in z[0][3]), ["(555) 0142", "(555) 0199", "Not sure"])
pick = dict(z[0][3])
jor = [c for c in QC if c[0] == ["jordan"]]
check("Jordan can't see the bill: she's never shown it (her card, if any, isn't the dispute)",
      any("0142" in c[2] or "0142" in str(c[3]) for c in jor), False)
check("…a kid, nothing about the household", [c for c in QC if c[0] == ["ava"]], [])
done, said = W.answer_ask(BQ, pick["(555) 0142"], "zach", "admin", QF)
pf = {x.attr: x for x in BQ.facts("household:maple-animal-clinic", "jordan", "adult")}["phone"]
check("Zach picks (555) 0142: everyone's answer, stated by him", (done, [v.value for v in pf.values], pf.values[0].word),
      (True, ["(555) 0142"], "stated"))
check("…and a second tap is too late", W.answer_ask(BQ, pick["(555) 0199"], "zach", "admin", QF)[0], False)
check("Jordan's card was the stale address instead", [c[1] for c in jor], ["Still right? City Water's address is 18 Alder St"])
done, said = W.answer_ask(BQ, jor[0][3][0][1], "jordan", "adult", QF)
af = {x.attr: x for x in BQ.facts("household:city-water", "jordan", "adult")}["address"]
check("…Yes, still right: checked today, no longer stale", (done, W.answer_state(af)), (True, "known"))
QC.clear()
qclock.set("2026-10-04T09:00")
W.daily_questions(BQ, QF, send=qsend)
check("one card a day at most; nothing left to ask them the next day", QC, [])

# ── entity merges (#359): plain duplicates in one owner's bubble, reversible; never across owners ────────

gclock = Clock("2026-10-02T12:00")
BG = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=gclock)
def zbill(eid, name, v, at, ref):
    return obs(entity=eid, etype="org", name=name, attr="bill", value=v, observed_at=at, kind="bill", ref=ref,
               origin=f"bill:{eid}", owner="zach", audience="private", bubble="zach")
BG.sync("bills", [zbill("amazon-com", "Amazon.com", "$16.96 due Oct 10", "2026-09-26", "b1"),
                  zbill("amazon", "Amazon", "$21.79 payment failed Sep 25", "2026-09-25", "b2"),
                  zbill("amazon", "Amazon", "$9.99 due Oct 2", "2026-09-20", "b3"),
                  obs(entity="google-llc", etype="org", name="Google LLC", attr="phone", value="650 253 0000",
                      observed_at="2026-09-01", kind="bill", ref="g1", origin="bill:google-llc", owner="zach",
                      audience="private", bubble="zach"),
                  obs(entity="google", etype="org", name="Google", attr="phone", value="(650) 253-0000",
                      observed_at="2026-09-02", kind="contact", ref="g2", origin="contacts", owner="zach",
                      audience="private", bubble="zach")])
BG.sync("household", [obs(entity="target", etype="org", name="Target", attr="phone", value="555 0300",
                          observed_at="2026-09-01", kind="household", ref="t1", origin="household.yaml", owner="robin",
                          audience="household", bubble="household"),
                      obs(entity="target-corp", etype="org", name="Target Corp", attr="phone", value="555 0301",
                          observed_at="2026-09-01", kind="bill", ref="t2", origin="bill:target", owner="zach",
                          audience="household", bubble="household")])
check("the same name once suffixes are set aside", (W.entity_key("Amazon.com"), W.entity_key("Google LLC"),
      W.entity_key("Ava Therapy")), ("amazon", "google", "avatherapy"))
check("auto-merge: two pairs in Zach's bubble; Robin's and Zach's Target stay apart", W.auto_merge(BG),
      {"merged": 2, "across_owners": 1})
check("…one Amazon now", BG.find("what's the amazon bill?", "zach", "admin"), ["zach:amazon"])
bills = {x.attr: x for x in BG.facts("zach:amazon", "zach", "admin")}["bill"]
check("…holding every bill from both spellings", sorted(v.value for v in bills.values),
      ["$16.96 due Oct 10", "$21.79 payment failed Sep 25", "$9.99 due Oct 2"])
gp = {x.attr: x for x in BG.facts("zach:google", "zach", "admin")}.get("phone") or \
     {x.attr: x for x in BG.facts("zach:google-llc", "zach", "admin")}["phone"]
check("…one number from two sources is one value, corroborated", ([v.word for v in gp.values], len(gp.values)),
      (["corroborated"], 1))
check("…and the merged name still finds it", BG.find("amazon.com", "zach", "admin"), ["zach:amazon"])
check("a merge is reversible", (BG.world("zach").unmerge("amazon-com", by="zach"),
                                sorted(BG.find("amazon and amazon.com", "zach", "admin"))),
      (True, ["zach:amazon", "zach:amazon-com"]))
check("…and logged", [tuple(r) for r in BG.world("zach").con.execute(
    "SELECT src, dst, by, undone_at IS NOT NULL FROM merges ORDER BY at")][:1], [("amazon-com", "amazon", "rule:entity_key", 1)])
W.auto_merge(BG)
gclock.set("2026-10-03T12:00")
BG.sync("bills", [zbill("amazon-com", "Amazon.com", "$16.96 paid Oct 3", "2026-10-03", "b1"),
                  zbill("amazon", "Amazon", "$21.79 payment failed Sep 25", "2026-09-25", "b2"),
                  zbill("amazon", "Amazon", "$9.99 due Oct 2", "2026-09-20", "b3")], remove_missing=False)
ch = BG.changes(["zach:amazon"], gclock.t - 60, "zach", "admin")
check("what changed since covers the merged spelling too", [("$16.96 paid Oct 3" in str(c["diff"])) for c in ch], [True])

# ── qualifiers (#361; Zach, 10-02: "usually Zach, Thursdays Robin") ─────────────────────────────

uclock = Clock("2026-10-02T12:00")
BU = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=uclock)
BU.ensure("soccer", "activity", "Soccer", "household", aliases=[("soccer practice", "")])
BU.ensure("nova-health", "org", "NOVA Health", "household")
W._NAMES.update({"zach": "Zach", "robin": "Robin", "ava": "Ava"})
check("qualifiers as stored", W.norm_qual({"days": "Thursdays", "for": "Ava", "purpose": " Pharmacy ", "x": 1}, FOLKS),
      {"days": ["thursday"], "for": "ava", "purpose": "pharmacy"})
W.set_fact(BU, "soccer", "pickup", "Zach", "zach", "admin", people=FOLKS)
done, said, _ = W.set_fact(BU, "soccer", "pickup", "Robin", "robin", "adult", people=FOLKS, qual={"days": "Thursdays"})
ok("a qualified statement is recorded as its own case", done and "Thursdays" in said)
pf = [f for f in BU.facts("household:soccer") if f.attr == "pickup"]
check("…beside the usual one, not replacing it", sorted((f.qual.get("days", ["-"])[0], f.values[0].value) for f in pf),
      [("-", "Zach"), ("thursday", "Robin")])
blk, _ = W.context(BU, "Who picks up from soccer practice?", "zach", "admin")
ok("asked in general: the usual one first, then the exception",
   "Soccer (usually), pickup: Zach" in blk and "Soccer, pickup (Thursdays): Robin" in blk and "usually one parent, Thursdays the other" in blk)
blk, _ = W.context(BU, "Who picks up from soccer practice on Thursday?", "zach", "admin")
ok("asked about Thursday: just Thursday's", "Soccer, pickup (Thursdays): Robin" in blk and "Answer: Soccer (usually)" not in blk)
W.set_fact(BU, "NOVA Health", "address", "1502 N Birch Rd", "zach", "admin", people=FOLKS, qual={"purpose": "pharmacy"})
W.set_fact(BU, "NOVA Health", "address", "1001 W 2nd Ave", "zach", "admin", people=FOLKS, qual={"purpose": "dental"})
blk, _ = W.context(BU, "What's the address of NOVA Health pharmacy?", "zach", "admin")
ok("a purpose picks the branch's address", [l for l in blk.splitlines() if l.startswith("- Answer:")]
   == ["- Answer: NOVA Health, address (pharmacy): 1502 N Birch Rd (stated: Zach said so, Oct 2)."])
blk, _ = W.context(BU, "What's the address of NOVA Health?", "zach", "admin")
ok("…and with no purpose named, both, each labelled", "(pharmacy)" in blk and "(dental)" in blk)

# ── knowledge gaps (#372): asked twice and not on record → one daily-slot card; never for eval turns ─────

gclock2 = Clock("2026-10-03T12:00")
BK = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=gclock2)
BK.ensure("maple-animal-clinic", "org", "Maple Animal Clinic", "household", aliases=[("vet", "")])
W.context(BK, "When is the vet open?", "zach", "admin", gaps=False)
check("an eval turn records nothing", BK.world("household").con.execute("SELECT COUNT(*) FROM gaps").fetchone()[0], 0)
W.context(BK, "When is the vet open?", "zach", "admin", gaps=True)
GQ = []
gsend = lambda title, detail, **kw: GQ.append((kw["to"], title, kw["actions"][0]["value"]))
gclock2.set("2026-10-04T09:00")
W.daily_questions(BK, [{"id": "zach", "role": "admin"}], send=gsend)
check("asked once: not yet worth a card", GQ, [])
W.context(BK, "what are the vet's hours?", "zach", "admin", gaps=True)
gclock2.set("2026-10-05T09:00")
W.daily_questions(BK, [{"id": "zach", "role": "admin"}], send=gsend)
check("asked twice and still not on record: one card, to him, in the daily slot",
      [(to, t) for to, t, _ in GQ], [(["zach"], "You've asked about Maple Animal Clinic's hours and I didn't have it")])
done, said = W.answer_ask(BK, GQ[0][2], "zach", "admin", FOLKS)
check("Not needed closes it", (done, "won't ask" in said), (True, True))
GQ.clear()
gclock2.set("2026-10-06T09:00")
W.daily_questions(BK, [{"id": "zach", "role": "admin"}], send=gsend)
check("…and it isn't offered again", GQ, [])
W.context(BK, "When is the vet open?", "robin", "adult", gaps=True)
W.context(BK, "vet hours?", "robin", "adult", gaps=True)
W.set_fact(BK, "the vet", "hours", "8-6 weekdays", "zach", "admin", people=FOLKS)
gclock2.set("2026-10-07T09:00")
W.daily_questions(BK, [{"id": "robin", "role": "adult"}], send=gsend)
check("filled since (someone told titan): nothing to ask Robin", GQ, [])

# ── active entities (#366, slice 1): mentions, a recency-decayed baseline, a report-only measure ──────

aclock = Clock("2026-10-03T08:00")
BA2 = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=aclock)
for nm, al in (("Maple Animal Clinic", "vet"), ("Soccer", "soccer"), ("City Water", "water")):
    BA2.ensure(W.entity_id(nm), "org" if nm != "Soccer" else "activity", nm, "household", aliases=[(al, "")])
for hour, q in ((8, "vet hours?"), (9, "soccer pickup?"), (10, "soccer time?"), (11, "soccer place?"), (12, "water bill?")):
    aclock.set(f"2026-10-03T{hour:02d}:00")
    W.context(BA2, q, "zach", "admin", gaps=True)
W.context(BA2, "soccer time?", "zach", "admin", gaps=False)
check("eval turns aren't activity", BA2.world("household").con.execute("SELECT COUNT(*) FROM mentions").fetchone()[0], 5)
check("active now: soccer first (three recent turns), then the newest", [c for c, _ in W.active(BA2, "zach", "admin")],
      ["household:soccer", "household:city-water", "household:maple-animal-clinic"])
check("…only for the person who asked", W.active(BA2, "robin", "adult"), [])
m = W.measure_active(BA2, "zach", "admin", k=1)
check("the measure: how often the top-k before a turn held the entity it named", (m["predicted"], m["hits"]), (4, 2))

# ── qualifier readers (#361): a pickup's days, a business's purpose, read from memory and notes ───────

rclock = Clock("2026-10-03T12:00")
BR = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=rclock)
W._NAMES.update({"zach": "Zach", "robin": "Robin", "ava": "Ava"})
BR.ensure("soccer", "activity", "Soccer", "household", aliases=[("soccer practice", "")])
BR.ensure("nova-health", "org", "NOVA Health", "household")
BR.ensure("nova-birch-rd-pharmacy", "org", "NOVA Birch Rd pharmacy", "household")
check("pickup read: a household member who picks up", (W.value_in("pickup", "Robin picks Ava up from soccer on Thursdays."),
      W.value_in("pickup", "Soccer pickup is Zach."), W.value_in("pickup", "Bob picks up the mail.")), ("Robin", "Zach", None))
check("…with its days", W.qual_in("pickup", "activity", "Soccer", "Robin picks Ava up from soccer on Thursdays."),
      {"days": ["thursday"]})
check("a business's purpose, unless its name says it",
      (W.qual_in("address", "org", "NOVA Health", "NOVA Health's pharmacy is at 1502 N Birch Rd."),
       W.qual_in("address", "org", "NOVA Birch Rd pharmacy", "The pharmacy is at 1502 N Birch Rd.")),
      ({"purpose": "pharmacy"}, {}))
got = W.from_memory(BR, "zach", "admin", FOLKS, ["Robin picks Ava up from soccer practice on Thursdays."], [],
                    "Robin picks Ava up from soccer practice on Thursdays", "model:qwen3-8b")
check("memory: a Thursday pickup, as Zach's statement, in its own case", [(a, v, c) for _, a, v, c in got],
      [("pickup", "Robin", "ok")])
pf = [f for f in BR.facts("household:soccer") if f.attr == "pickup"]
check("…qualified by its day", [(f.qual, f.values[0].value) for f in pf], [({"days": ["thursday"]}, "Robin")])
ob = W.note_observations(BR, [("n1", "NOVA", "NOVA Health's pharmacy is located at 1502 N Birch Rd.",
                               {"source_type": "note"}, "zach", "household", "2026-10-02")], ROLES)
check("notes: the address is proposed for the pharmacy branch", [(o.attr, o.value, o.qual) for o in ob],
      [("address", "1502 N Birch Rd", {"purpose": "pharmacy"})])

# ── anticipatory answers (#372, slice 2): per person, from what they may see; only on a matching day question ──

pclock = Clock("2026-10-08T02:30")          # a Thursday
BP = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=pclock)
W._NAMES.update({"zach": "Zach", "robin": "Robin", "ava": "Ava", "jordan": "Jordan"})
BP.sync("household", [obs(entity="soccer", etype="activity", name="Soccer", attr="schedule", value="every Thursday, 4:30 PM",
                          observed_at="2026-09-01", kind="household", ref="h1", origin="household.yaml", owner=None,
                          audience="household", bubble="household"),
                      obs(entity="soccer", etype="activity", name="Soccer", attr="place", value="Mission Park",
                          observed_at="2026-09-01", kind="household", ref="h2", origin="household.yaml", owner=None,
                          audience="household", bubble="household")])
BP.sync("calendar", [obs(entity="ava-therapy", etype="activity", name="Ava Therapy", attr="schedule",
                         value="every Thursday, 3:00 PM", observed_at="2026-09-28", kind="calendar", ref="c1",
                         origin="calendar:ava", owner="ava", audience='["ava","robin","zach"]', bubble="ava")])
W.set_fact(BP, "soccer", "pickup", "Zach", "zach", "admin", people=FOLKS)
W.set_fact(BP, "soccer", "pickup", "Robin", "robin", "adult", people=FOLKS, qual={"days": "Thursdays"})
PEOPLE = [{"id": "zach", "role": "admin"}, {"id": "jordan", "role": "adult"}]
got = W.prepare_day(BP, PEOPLE, date(2026, 10, 8))
check("prepared per person, from what each may see (Ava Therapy is Zach's to see as a parent, not Jordan's)",
      (got["zach"] > got["jordan"], got["jordan"] > 0), (True, True))
pclock.set("2026-10-08T15:00")
blk = W.prepared_block(BP, "Who's picking up today?", "zach")
check("a pickup question today: Thursday's pickup, not the usual one", ("Robin" in blk, "pickup: Zach" in blk), (True, False))
blk = W.prepared_block(BP, "What's on this afternoon?", "jordan")
check("Jordan's day: soccer, never Ava Therapy", ("Soccer" in blk, "Therapy" in blk), (True, False))
check("no day word: nothing prepared is used", W.prepared_block(BP, "Who picks up?", "zach"), "")
blk, ids = W.context(BP, "Who's picking up today?", "zach", "admin", gaps=True)
check("through the turn block: only when the question names nothing and asks about the day", ("Robin" in blk, ids),
      (True, []))
check("…an eval turn never reads it", W.context(BP, "Who's picking up today?", "zach", "admin", gaps=False), ("", []))
st = W.prepared_stats(BP, 7)
check("counts only: prepared vs used, per person", (st["zach"]["used"] >= 1, st["jordan"]["used"] >= 1, "line" in str(st)),
      (True, True, False))
check("kept in each person's own bubble file", BP.world("jordan").con.execute("SELECT COUNT(*) FROM prepared").fetchone()[0] > 0,
      True)

# ── learned aliases (#359, slice 2): per speaker, from uncorrected turns, two uses, never ambiguous ──────

os.environ["RETRIEVAL_TRACE_DB"] = str(Path(tempfile.mkdtemp()) / "trace.db")
import retrieval_trace as RT  # noqa: E402
RT.DB = Path(os.environ["RETRIEVAL_TRACE_DB"])
lclock = Clock("2026-10-03T12:00")
BL = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=lclock)
BL.ensure("maple-animal-clinic", "org", "Maple Animal Clinic", "household", aliases=[("vet", "")])
BL.ensure("robin", "person", "Robin", "household", person_id="robin")
BL.ensure("city-water", "org", "City Water", "household")
check("naming phrases (with their prefixes)", (W.naming_phrases("What are the animal hospital's hours?"),
      W.naming_phrases("Call Mom about it")), (["animal", "animal hospital's"], ["mom"]))


def turn(tid, speaker, cid, words, corrected=0):
    RT.begin()
    RT.query("world", "x", [], [])
    W.note_resolution(BL, cid, speaker, words, tid, "adult")
    RT.end(tid, speaker, [], corrected=corrected)


turn("t1", "robin", "household:maple-animal-clinic", "What are the animal hospital's hours?")
check("one use: waiting", {r["state"] for r in W.learn_aliases(BL, person="robin")}, {"waiting"})
turn("t2", "robin", "household:maple-animal-clinic", "Call the animal hospital", corrected=1)
check("a corrected turn doesn't count", {r["state"] for r in W.learn_aliases(BL, person="robin")}, {"waiting"})
turn("t3", "robin", "household:maple-animal-clinic", "Is the animal hospital open Saturday?")
check("two uncorrected uses: the longest recurring phrase is ready (dry run)",
      [(W.norm_alias(r["phrase"]), r["state"]) for r in W.learn_aliases(BL, person="robin") if r["state"] != "waiting"],
      [("animal hospital", "would learn")])
W.learn_aliases(BL, apply=True)
check("learned: it finds the clinic for Robin", BL.find("is the animal hospital open?", "robin", "adult"),
      ["household:maple-animal-clinic"])
check("…and for nobody else", BL.find("is the animal hospital open?", "zach", "admin"), [])
turn("k1", "ava", "household:robin", "Ask Mom when she's home")
turn("k2", "ava", "household:robin", "Where's Mom?")
W.learn_aliases(BL, apply=True)
check("a kid's \"Mom\" is hers alone", (BL.find("where is mom", "ava", "kid"), BL.find("where is mom", "zach", "admin")),
      (["household:robin"], []))
turn("a1", "zach", "household:maple-animal-clinic", "the office called")
turn("a2", "zach", "household:city-water", "the office called again")
turn("a3", "zach", "household:city-water", "the office bill")
check("a phrase two things compete for is never learned",
      {r["phrase"]: r["state"] for r in W.learn_aliases(BL, apply=True, person="zach")}.get("office"), "ambiguous")
check("…and nothing of Zach's was learned from it", W.learned_aliases(BL, "zach"), [])
check("the dry view of a person's aliases", [a["alias"] for a in W.learned_aliases(BL, "ava")], ["mom"])

# ── partial names (#359, last part): a chore's object, a joined title's parts; never a stop word or a collision ──

check("chore and joined titles", (W.activity_aliases("Take out garbage"), W.activity_aliases("Ballet + Modern"),
      W.activity_aliases("Piano / Theory"), W.activity_aliases("Dinner and a movie"), W.activity_aliases("Kids & Family")),
      ((("garbage", ""),), (("Ballet", ""), ("Modern", "")), (("Piano", ""), ("Theory", "")), (), ()))
nclock2 = Clock("2026-10-03T12:00")
BC = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=nclock2)
BC.ensure("take-out-recycling", "activity", "Take out recycling", "household", aliases=W.activity_aliases("Take out recycling"))
BC.ensure("take-out-garbage", "activity", "Take out garbage", "household", aliases=W.activity_aliases("Take out garbage"))
check("before: \"recycling\" finds the chore", BC.find("when is recycling?", "zach", "admin"), ["household:take-out-recycling"])
BC.ensure("recycling-center", "org", "Recycling center", "household")
check("a collision: the chore's short name is removed", W.prune_partial_collisions(BC), 1)
check("…so \"recycling\" names neither", BC.find("when is recycling?", "zach", "admin"), [])
check("…the full names still find each", (BC.find("when is take out recycling?", "zach", "admin"),
      BC.find("recycling center hours", "zach", "admin")), (["household:take-out-recycling"], ["household:recycling-center"]))
check("…and a short name nobody shares stays", BC.find("when does the garbage go out?", "zach", "admin"),
      ["household:take-out-garbage"])
BC.ensure("ballet-modern", "activity", "Ballet + Modern", "household", aliases=W.activity_aliases("Ballet + Modern"))
BC.ensure("conditioning-ballet", "activity", "Conditioning + Ballet", "household",
          aliases=W.activity_aliases("Conditioning + Ballet"))
W.prune_partial_collisions(BC)
check("siblings that share a short name keep it: \"ballet\" finds both classes",
      sorted(BC.find("when is ballet?", "zach", "admin")), ["household:ballet-modern", "household:conditioning-ballet"])

# ── context-conditioned lookups (#376): the situation picks what a vague question means; off by default ──

sclock = Clock("2026-10-08T12:00")          # a Thursday
BS2 = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=sclock)
W._NAMES.update({"zach": "Zach", "robin": "Robin"})
BS2.sync("household", [obs(entity="soccer", etype="activity", name="Soccer", attr="schedule", value="every Thursday, 4:30 PM",
                           observed_at="2026-09-01", kind="household", ref="s1", origin="household.yaml", owner=None,
                           audience="household", bubble="household"),
                       obs(entity="soccer", etype="activity", name="Soccer", attr="place", value="Mission Park",
                           observed_at="2026-09-01", kind="household", ref="s2", origin="household.yaml", owner=None,
                           audience="household", bubble="household"),
                       obs(entity="piano", etype="activity", name="Piano", attr="schedule", value="every Monday, 4:00 PM",
                           observed_at="2026-09-01", kind="household", ref="p1", origin="household.yaml", owner=None,
                           audience="household", bubble="household")])
W.set_fact(BS2, "soccer", "pickup", "Zach", "zach", "admin", people=FOLKS)
W.set_fact(BS2, "soccer", "pickup", "Robin", "robin", "adult", people=FOLKS, qual={"days": "Thursdays"})
at = datetime(2026, 10, 8, 15, 45)
check("off by default: a vague question gets nothing", W.context(BS2, "Who's doing pickup?", "zach", "admin", at=at), ("", []))
blk, _ = W.context(BS2, "Who's doing pickup?", "zach", "admin", situation=True, at=at)
ok("on: the next activity on his day, Thursday's pickup, and it says which it picked",
   "Soccer is next" in blk and "Robin" in blk)
check("…nothing soon on the day: still nothing", W.context(BS2, "Who's doing pickup?", "zach", "admin", situation=True,
      at=datetime(2026, 10, 8, 22, 30)), ("", []))
import world_eval as E3  # noqa: E402
cases = E3.situation_cases(BS2)
check("world_eval's situation cases: before and after, in code", E3.score_situations(BS2, cases),
      {"cases": 2, "before": 0, "after": 2})

# ── the per-person brief (#375, an experiment): per person, from what they may see ────────────────────

bclock = Clock("2026-10-03T12:00")
BB = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=bclock)
BB.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                          value="(555) 0142", observed_at="2026-09-01", kind="household", ref="h", origin="household.yaml",
                          owner=None, audience="household", bubble="household")])
BB.sync("bills", [obs(entity="city-water", etype="org", name="City Water", attr="bill", value="$84.20 due Oct 12",
                      observed_at="2026-09-28", kind="bill", ref="b", origin="bill:cw", owner="zach", audience="private",
                      bubble="zach")])
BB.sync("calendar", [obs(entity="ava-therapy", etype="activity", name="Ava Therapy", attr="schedule",
                         value="every Thursday, 3:00 PM", observed_at="2026-09-28", kind="calendar", ref="c",
                         origin="calendar:ava", owner="ava", audience='["ava","robin","zach"]', bubble="ava")])
zb, kb_, sb = W.brief(BB, "zach", "admin"), W.brief(BB, "leo", "kid"), W.brief(BB, "jordan", "adult")
check("Zach's brief: the household's, his own, his child's schedule",
      ("(555) 0142" in zb, "$84.20" in zb, "Ava Therapy" in zb), (True, True, True))
check("a kid's brief: only the household's", ("(555) 0142" in kb_, "$84.20" in kb_, "Ava Therapy" in kb_), (True, False, False))
check("Jordan's: no one else's private facts, no one else's child", ("$84.20" in sb, "Ava Therapy" in sb), (False, False))

# ── the agent side: the flag, the tool, the turn block, the memory filter, the eval pin ──────────

_TMP = tempfile.mkdtemp(prefix="world-")
os.environ["HOUSEHOLD_DB"] = os.path.join(_TMP, "household.db")    # never a real store
os.environ["KB_ACL_DB"] = os.path.join(_TMP, "kb_acl.db")
os.environ.setdefault("AGENT_TURN_SOURCE", "test")
import agent_runtime_lib as A  # noqa: E402

# the card's buttons: a parent's tap is decided in code, and settles it for both parents
import proactive as P  # noqa: E402
pdb = Path(_TMP) / "cards.db"
W.SEND_ASK = lambda title, detail, **kw: P.add_card(title, detail, db=pdb, **kw)
W._WORLD = BS
clock.set("2026-10-02T09:00")
W.set_fact(BS, "the vet", "phone", "555-4444", "ava", "kid", people=FOLKS)
card = next(c for c in P.list_cards(db=pdb, person="robin", role="adult") if c["source"] == "world-proposal")
r = P.card_action(card["id"], "a0", db=pdb, person="robin", role="adult")
check("Robin taps yes: confirmed, no agent turn", (r["ok"], "555-4444" in r["said"], "trigger" in r), (True, True, False))
check("…and the card is gone for Zach too",
      [c["id"] for c in P.list_cards(db=pdb, person="zach", role="admin") if c["id"] == card["id"]], [])
W.SEND_ASK, W._WORLD = None, None

# #354: a person's facet map files their vendors and calendars; unmapped ones are asked, one a day, never guessed
import household_identity as HH  # noqa: E402
fp = HH.get_store().add_person("Facet Tester", "adult")["id"]
BF = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=clock)
fnodes = [{**n, "data": {**n["data"], "provenance": {"owner": fp, "audience": "private"}}} for n in nodes]
BF.sync("bills", W.bill_observations(fnodes, prov))
fpeople = [{"id": fp, "name": "Facet Tester", "role": "adult", "facets": {}}]
check("an unmapped vendor's bills are personal", {r["facet"] for _, r in BF.world(fp).entities()}, {"personal"})
check("one review per vendor", W.facet_reviews(BF, fpeople), 2)
check("…made once", W.facet_reviews(BF, fpeople), 0)
check("a kid isn't asked", W.facet_reviews(BF, [{"id": "ava", "role": "kid"}]), 0)
fcards = []
clock.set("2026-10-03T10:00")
W.daily_questions(BF, people=fpeople, send=lambda title, detail, **kw: fcards.append((title, kw["actions"])))
check("the daily slot asks about one", [t for t, _ in fcards], ["Your City Water bills: work or personal?"])
check("…Work, Personal, Not now", [a["label"] for a in fcards[0][1]], ["Work", "Personal or both", "Not now"])
check("a kid can't answer it", W.answer_ask(BF, fcards[0][1][0]["value"], fp, "kid")[0], False)
done, said = W.answer_ask(BF, fcards[0][1][0]["value"], fp, "adult")
check("Work: saved in their facet map", (done, HH.get_store().facets(fp)), (True, {"vendor:city water": "work"}))
BF.sync("bills", W.bill_observations(fnodes, prov, facets=W.facet_maps()))
ents = dict(BF.world(fp).entities())
check("…and the next sync refiles it", (ents["city-water"]["facet"], ents["pg-e"]["facet"]), ("work", "personal"))
check("…without changing who sees it", ents["city-water"]["audience"], "private")
check("the answered review is closed; the other still waits",
      [r["state"] for r in BF.world(fp).con.execute("SELECT state FROM note_reviews ORDER BY source_id")], ["closed", "pending"])
HH.get_store().set_facets(fp, {"vendor:pg&e": "personal"}, merge=True)
W.facet_reviews(BF, [{**fpeople[0], "facets": HH.get_store().facets(fp)}])
check("a key mapped in settings closes its review",
      [r["state"] for r in BF.world(fp).con.execute("SELECT state FROM note_reviews ORDER BY source_id")], ["closed", "closed"])
BF.sync("bills", W.bill_observations(fnodes, prov))
check("unmapped again: back to personal", dict(BF.world(fp).entities())["city-water"]["facet"], "personal")
try:
    HH.get_store().set_facets(fp, {"vendor:x": "home"})
    ok("home isn't a person's facet", False)
except HH.PairingError:
    ok("home isn't a person's facet", True)
kc = W.calendar_observations(ballet, prov, local_time.zone(), assigned={"ava": "ava"},
                             facets={"ava": {"calendar:ava": "school"}})
check("a kid's calendar files under the kid's map (set by a parent)", {o.facet for o in kc}, {"school"})
check("…unmapped: inbox", {o.facet for o in W.calendar_observations(ballet, prov, local_time.zone(),
                                                                  assigned={"ava": "ava"})}, {"inbox"})

# #357: suggested fences: a sensitive thing the whole household sees, that no fence covers, gets its owner one card
import fences as FN  # noqa: E402
sfolks = [{"id": "zach", "name": "Zach", "role": "admin"}, {"id": "robin", "name": "Robin", "role": "adult"},
          {"id": "ava", "name": "Ava", "role": "kid", "parents": ["zach", "robin"]}]
sfs = FN.Fences(Path(tempfile.mkdtemp()) / "fences.db", people=lambda: sfolks)
BG = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=clock)
hh_o = lambda eid, name, ref: obs(entity=eid, etype="org", name=name, attr="phone", value="(555) 0100",  # noqa: E731
                                  observed_at="2026-09-01", kind="household", ref=ref, origin="household.yaml",
                                  label="the household file", owner=None, audience="household", bubble="household",
                                  facet="home")
BG.sync("household", [hh_o("smith-family-law", "Smith Family Law", "h1"), hh_o("maple-vet", "Maple Vet", "h2"),
                      hh_o("kids-pediatrician", "Kids Pediatrician", "h3")])
check("a legal matter the household sees: one suggestion, to the admin (household.yaml is theirs)",
      W.fence_suggestions(BG, sfolks, sfs), 1)
check("…made once", W.fence_suggestions(BG, sfolks, sfs), 0)
check("…for the law firm only (health has its default fence; the vet is nothing)",
      [(r["entity"], r["attr"]) for r in BG.world("zach").con.execute("SELECT * FROM note_reviews WHERE kind='fence'")],
      [("household:smith-family-law", "legal")])
FN._FENCES, old_fences = sfs, os.environ.get("AGENT_FENCES")
os.environ["AGENT_FENCES"] = "1"
try:
    scards = []
    clock.set("2026-10-04T10:00")
    W.daily_questions(BG, people=sfolks[:1], send=lambda title, detail, **kw: scards.append((title, kw["actions"])))
    check("the daily slot asks", [t for t, _ in scards], ["Keep Smith Family Law to the adults?"])
    check("…Adults only, Just me, Not now", [a["label"] for a in scards[0][1]], ["Adults only", "Just me", "Not now"])
    check("only its owner can answer", W.answer_ask(BG, scards[0][1][0]["value"], "robin", "adult")[0], False)
    ok("a kid sees the law firm before", sfs.admits("ava", "kid", text="Smith Family Law"))
    done, said = W.answer_ask(BG, scards[0][1][0]["value"], "zach", "admin")
    ok("Adults only: fenced", done and "kept to the adults" in said)
    check("…a kid no longer does", sfs.admits("ava", "kid", text="call Smith Family Law"), False)
    ok("…an adult still does", sfs.admits("robin", "adult", text="call Smith Family Law"))
    check("…and it's asked once", W.answer_ask(BG, scards[0][1][0]["value"], "zach", "admin")[0], False)
    BG.sync("household", [hh_o("jones-custody", "Jones Custody Mediation", "h4")])
    W.fence_suggestions(BG, sfolks, sfs)
    sfs.set_entity("household:jones-custody", ["Jones Custody Mediation"], "zach", "adults", by="zach")
    W.fence_suggestions(BG, sfolks, sfs)
    check("fenced in chat meanwhile: its suggestion closes",
          BG.world("zach").con.execute("SELECT state FROM note_reviews WHERE entity='household:jones-custody'").fetchone()[0],
          "closed")
finally:
    FN._FENCES = None
    if old_fences is None:
        os.environ.pop("AGENT_FENCES", None)
    else:
        os.environ["AGENT_FENCES"] = old_fences
check("a kid's own thing: no suggestion (a parent fences for them)", W.fence_suggestions(
    BG, [{"id": "zach", "role": "kid"}], sfs), 0)
check("legal words, not every court or in-law", (FN.classes_in("tennis court on Saturday"),
                                               FN.classes_in("my mother-in-law's birthday"),
                                               FN.classes_in("court date for custody")), (set(), set(), {"legal"}))

w, clock = fresh("2026-10-01T12:00")
w.sync("household", [obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                         value="(555) 0142", observed_at="2026-09-01", kind="household", ref="hh#phone",
                         origin="household.yaml", label="the household file", owner=None, audience="household",
                         aliases=(("vet", ""),))])
w.sync("bills", [obs(entity="dr-patel", etype="org", name="Dr. Patel", attr="phone", value="555 0100",
                     observed_at="2026-09-01", kind="bill", ref="b1", origin="bill:dr-patel", label="a bill",
                     owner="zach", audience="private")])
W._WORLD = w
W._SYNC["at"] = 9e12          # never start a background sync from a test
A._runtime_request = lambda *a, **k: {}
A._http_json = lambda url, payload=None, timeout=30: {}


def as_(pid, role):
    A._TURN_IO.person = {"id": pid, "name": pid} if pid else None
    A._TURN_IO.role = role


old = os.environ.pop("AGENT_WORLD_MODEL", None)
try:
    as_("zach", "admin")
    A._TURN_IO.eval_world = None
    ok("off by default", not A.world_on())
    ok("about is hidden while off", not A._role_allows("about"))
    ok("…from the tool prompt too", "**about**" not in A.build_tool_system_prompt())
    ok("no world block while off", "Household facts" not in A._situational_context_inner("what's the vet's number?"))
    os.environ["AGENT_WORLD_MODEL"] = "1"
    ok("on with the flag", A.world_on() and A._role_allows("about"))
    ok("about is in the prompt when on", "**about**" in A.build_tool_system_prompt())
    ctx = A._situational_context_inner("what's the vet's number?")
    ok("the turn carries the computed answer", "Answer: Maple Animal Clinic, phone: (555) 0142" in ctx)
    real_kb = A._kb_query
    A._kb_query = lambda q, k, timeout=30, passive=False: [{"source_id": "n1", "title": "Vet", "score": 0.9,
                                                            "chunk_text": "The vet number is (555) 0199", "metadata": {}}]
    try:
        ctx2 = A._situational_context_inner("what's the vet's number?")
    finally:
        A._kb_query = real_kb
    ok("the world block comes before the KB passages",
       0 <= ctx2.find("Household facts") < ctx2.find("Relevant memory"))
    A._kb_query = lambda q, k, timeout=30, passive=False: [
        {"source_id": "rb1", "title": "Titan runbook — Deploys", "score": 0.9, "metadata": {"origin": "kb_seed_runbook.py", "domain": "titan"},
         "chunk_text": "To deploy dinner plans run the deploy script on the server"},
        {"source_id": "n2", "title": "Dinner plans", "score": 0.8, "metadata": {},
         "chunk_text": "Dinner plans: tacos on Friday with the Smiths"}]
    try:
        home = A._situational_context_inner("what are our dinner plans friday?")
        work = A._situational_context_inner("how do I deploy titan dinner plans?")
    finally:
        A._kb_query = real_kb
    ok("a household turn carries no runbook note", "Titan runbook" not in home and "tacos" in home)
    ok("a question about titan still does", "Titan runbook" in work)
    ok("the about tool answers", "(555) 0142" in A.execute_tool("about", {"entity": "the vet"}))
    as_("p_kid", "kid")
    ok("a kid may use about", A._role_allows("about"))
    ok("a kid never gets Zach's private entity", "0100" not in A.execute_tool("about", {"entity": "Dr. Patel"}))
    ok("…nor in the turn", "0100" not in A._situational_context_inner("what's dr patel's number?"))
    as_("zach", "admin")
    recall = "[MEMORY — relevant facts about this user]\n• The vet's number is (555) 0199\n• Zach likes jazz"
    check("memory about the paged vet is dropped", A.world_drop_recalled(recall, "what's the vet's number?"),
          "[MEMORY — relevant facts about this user]\n• Zach likes jazz")
    check("memory untouched when nothing is named", A.world_drop_recalled(recall, "play some jazz"), recall)
    newer_recall = recall + "\n• The vet's number changed to (555) 0777 (noted today)"
    A.world_drop_recalled(newer_recall, "what's the vet's number?")
    ok("a newer memory fact is handed to the world block", "maple-animal-clinic" in str(getattr(A._TURN_IO, "world_newer", {})))
    ok("…which flags it in the turn", "Newer than titan's record" in A._situational_context_inner("what's the vet's number?"))
    A.world_drop_recalled("", "anything")
    check("a new turn starts with none", A._TURN_IO.world_newer, {})
    os.environ["AGENT_WORLD_MODEL"] = "0"
    A._TURN_IO.eval_world = True
    ok("an eval turn's pin turns it on", A.world_on())
    A._TURN_IO.eval_world = False
    os.environ["AGENT_WORLD_MODEL"] = "1"
    ok("…and off", not A.world_on())
    check("memory untouched when off", A.world_drop_recalled(recall, "what's the vet's number?"), recall)
finally:
    A._TURN_IO.eval_world = None
    as_(None, None)
    if old is None:
        os.environ.pop("AGENT_WORLD_MODEL", None)
    else:
        os.environ["AGENT_WORLD_MODEL"] = old

try:
    import agent_api_server as S
    check("eval_world pin on an eval turn", S._parse_eval_world({"eval_world": True}, "eval"), True)
    check("ignored on a user turn", S._parse_eval_world({"eval_world": True}, "user"), None)
    check("ignored unless a bool", S._parse_eval_world({"eval_world": "yes"}, "eval"), None)
except ImportError as exc:   # fastapi missing on a bare machine
    print(f"skip agent_api_server checks: {exc}")

# #814: a correction about something titan knew only from a note: the name says what it is, so it's recorded
check("type from a name: titled people and businesses", [W.type_from_name(n) for n in
      ("Dr. Okafor", "Coach Rivera", "Willow Creek Dentistry", "Kestrel Karate", "Bramble Cafe", "Robin", "the blue house")],
      ["person", "person", "org", "org", "org", None, None])
BT = W.Bubbles(Path(tempfile.mkdtemp()) / "world", clock=Clock("2026-10-03T12:00"))
done, said, _ = W.set_fact(BT, "Willow Creek Dentistry", "phone", "(509) 555-0171", "zach", "admin", people=FOLKS)
check("a correction for a business known only from a note is recorded (no type given)", (done, "555-0171" in said), (True, True))
done, said, why = W.set_fact(BT, "Morgan", "phone", "(509) 555-0101", "zach", "admin", people=FOLKS)
check("…a bare name still asks what it is", (done, why), (False, "unknown"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
