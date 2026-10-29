#!/usr/bin/env python3
# gate: hermetic
"""Memory's "Needs a look" queue (#1074): the nightly pass finds one disagreement and one duplicate in seeded facts,
per person and within groups; resolving each changes only those rows (and restores with its undo token); a kid's queue
never holds an adult's facts; at most 5 open; ignored for 30 days drops off; a pair is never asked twice; fading once
usage has been tracked long enough. Fictional people (Robin, an adult; Ava, a kid; Jordan, another adult); a temporary
memory; the local model is a fake.
Run: python3 scripts/test_memory_review.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-memreview-"))
os.environ["AGENT_MEMORY_DIR"] = str(_TMP / "memory")
os.environ["MEMORY_HYBRID"] = "0"
for k, v in {"HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import agent_memory  # noqa: E402
import memory_review as R  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


ROBIN, AVA, JORDAN = "p_robin", "p_ava", "p_jordan"
PEOPLE = [{"id": ROBIN, "role": "adult"}, {"id": AVA, "role": "kid"}, {"id": JORDAN, "role": "adult"},
          {"id": "screen", "role": "household"}]
NOW = time.time()
mem = agent_memory.AgentMemory()


def fact(pid, text, group, days_ago):
    """Stored directly, as rows from before the near-duplicate check were (add_fact would refuse the second)."""
    import uuid
    fid = str(uuid.uuid4())
    mem._db.execute("INSERT INTO facts (id, conv_id, text, confidence, ts, person_id, grp) VALUES (?,?,?,?,?,?,?)",
                    (fid, f"chat:{pid}", text, 1.0, NOW - days_ago * 86400, pid, group))
    mem._db.commit()
    mem._bm25 = None
    return fid


veg = fact(ROBIN, "Robin is vegetarian.", "preference", 16)
steak = fact(ROBIN, "Robin ordered the steak at Harbor Grill.", "preference", 2)
fact(ROBIN, "Robin likes window seats on long flights.", "preference", 10)
bus1 = fact(ROBIN, "Robin takes the 7:40 bus to the office on weekdays.", "routine", 30)
bus2 = fact(ROBIN, "Robin takes the 7:40 bus to the office on weekdays now.", "routine", 3)
fact(ROBIN, "Robin's dentist is Lakeside Clinic.", "health", 40)
fact(AVA, "Ava plays soccer on Saturdays.", "routine", 5)
fact(AVA, "Ava's favorite color is green.", "preference", 5)
fact(JORDAN, "Jordan is vegetarian.", "preference", 5)
ASKED = []


def fake_ask(pairs):
    ASKED.extend(pairs)
    return [("vegetarian" in a and "steak" in b) or ("vegetarian" in b and "steak" in a) for a, b in pairs]


def rows():
    return sorted(tuple(r) for r in mem._db.execute("SELECT id, person_id, text FROM facts"))


print("── the nightly pass ──")
got = R.nightly(mem, PEOPLE, ask=fake_ask, now=NOW)
check("one disagreement and one duplicate, for Robin; the screen isn't a person", (got["disagree"], got["duplicate"],
                                                                                  got["people"]), (1, 1, 3))
check("pairs are only compared within a group: Robin's health fact never meets the steak",
      any("dentist" in a or "dentist" in b for a, b in ASKED), False)
check("Jordan's 'vegetarian' never meets Robin's 'steak' (per person)",
      any(("Jordan" in a) != ("Jordan" in b) for a, b in ASKED), False)
q = R.queue(mem, ROBIN, "adult", now=NOW)
check("Robin's queue: the disagreement (older first) and the duplicate, with their choices",
      sorted((i["kind"], i.get("a", {}).get("id"), i.get("b", {}).get("id"), tuple(i["choices"])) for i in q),
      sorted([("disagree", veg, steak, ("still_true", "not_any_more", "forget_both")),
              ("duplicate", bus1, bus2, ("merge", "keep_both"))]))
check("Ava's queue never holds an adult's facts (and here is empty)", R.queue(mem, AVA, "kid", now=NOW), [])
n_asked = len(ASKED)
dry = R.nightly(mem, PEOPLE, ask=fake_ask, now=NOW, dry_run=True)
check("a dry run writes nothing and never calls the model (it says how many it would ask)",
      (dry["disagree"], len(R.queue(mem, ROBIN, "adult", now=NOW)), len(ASKED) - n_asked, dry["would_ask"] > 0),
      (0, 2, 0, True))
check("run again: a pair already queued is never asked twice", R.nightly(mem, PEOPLE, ask=fake_ask, now=NOW)["disagree"], 0)

print("── resolving changes only those rows ──")
before = rows()
dis = next(i for i in q if i["kind"] == "disagree")
r1 = R.resolve(mem, ROBIN, "adult", dis["id"], "not_any_more", now=NOW)
check("Not any more: the older fact goes, nothing else", sorted(set(before) - set(rows())),
      [(veg, ROBIN, "Robin is vegetarian.")])
check("…and its undo token brings it back exactly", (mem.restore_batch(r1["undo"], person_id=ROBIN), rows() == before),
      (1, True))
dup = next(i for i in q if i["kind"] == "duplicate")
r2 = R.resolve(mem, ROBIN, "adult", dup["id"], "merge", now=NOW)
check("Merge keeps the newer wording, forgets the older", [x[0] for x in sorted(set(before) - set(rows()))], [bus1])
check("resolved items leave the queue", R.queue(mem, ROBIN, "adult", now=NOW), [])
check("resolved pairs are never queued again", R.nightly(mem, PEOPLE, ask=fake_ask, now=NOW)["duplicate"], 0)
for label, args, status in (("another person's item: 404", (JORDAN, "adult", dis["id"], "forget_both"), 404),
                            ("a shared screen: 403", (None, "household", dis["id"], "forget_both"), 403),
                            ("a choice that doesn't fit: 400", None, 400)):
    try:
        if args is None:
            mem._db.execute("INSERT INTO memory_review (id, person, kind, a, b, created_at) VALUES ('mr-x', ?, 'disagree', "
                            "?, ?, ?)", (ROBIN, veg, steak, NOW))
            R.resolve(mem, ROBIN, "adult", "mr-x", "merge", now=NOW)
        else:
            R.resolve(mem, *args, now=NOW)
        check(label, "resolved", status)
    except R.Refused as e:
        check(label, e.status, status)
mem._db.execute("DELETE FROM memory_review WHERE id='mr-x'")

print("── the cap, expiry and fading ──")
for i in range(8):
    fact(JORDAN, f"Jordan parks car number {i} in bay twelve.", "place", 20)
    fact(JORDAN, f"Jordan parks car number {i} in bay twelve now.", "place", 1)
got = R.nightly(mem, PEOPLE, ask=lambda pairs: [False] * len(pairs), now=NOW)
check("at most 5 open per person", len(R.queue(mem, JORDAN, "adult", now=NOW)), 5)
check("ignored for 30 days: it drops off, nothing changed", (len(R.queue(mem, JORDAN, "adult", now=NOW + 31 * 86400)),
                                                             R.nightly(mem, [], now=NOW + 31 * 86400)["expired"]), (0, 5))
mem._db.execute("INSERT OR REPLACE INTO memory_meta VALUES ('usage_since', ?)", (str(NOW - 90 * 86400),))
mem._db.commit()
got = R.nightly(mem, [p for p in PEOPLE if p["id"] == AVA], ask=lambda p: [False] * len(p), now=NOW)
fq = [i for i in R.queue(mem, AVA, "kid", now=NOW) if i["kind"] == "fading"]
check("fading: once usage has been tracked 60 days, unused facts are one item, counts only",
      (got["fading"], fq[0]["count"] if fq else None, "ids" in (fq[0] if fq else {})), (1, 2, False))
before = rows()
r3 = R.resolve(mem, AVA, "kid", fq[0]["id"], "forget_all", now=NOW)
check("Forget all: just those, only Ava's", sorted({x[1] for x in set(before) - set(rows())}), [AVA])
mem.restore_batch(r3["undo"], person_id=AVA)

print("── routes and the local-only rule ──")
import household_identity as hh  # noqa: E402
check("adults and kids read their own queue; a shared screen doesn't",
      [hh.route_allowed(r, "GET", "/v1/memory/review") for r in ("admin", "adult", "kid", "household")],
      [True, True, True, False])
import memory_groups  # noqa: E402
memory_groups.LOCAL_URL = "https://api.example.com/v1"
try:
    R.ask_local([("a", "b")])
    check("never a hosted model", "asked", "refused")
except ValueError:
    check("never a hosted model", "refused", "refused")
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
