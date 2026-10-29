#!/usr/bin/env python3
# gate: hermetic
"""Where a /v1/items request's time goes (titan-node#1552): every items-latency.jsonl line carries `parts` (reader
name -> ms, one numeric entry per reader that ran, plus kb_visible, snoozed and resolve) and `cold` (whether any of the
read-path caches #1548 added, records / timeline / KB titles, had to go to the runtime for this request). Checked here:
the fields per view and viewer, cold on a first read and warm on the next within the TTL, cold again after a write,
one viewer's request never counted into another's, the response body byte-identical with the timing on and off, and
no id, title or name from the fixture anywhere in the log. Fictional people (Alex and Robin, adults; Ava, a kid);
throwaway stores; a stub runtime; no model.
Run: python3 scripts/test_items_latency_parts.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-items-latency-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db",
             "SCHEDULED_DB": "scheduled.db", "REFERENCES_DB": "references.db", "WORLD_DIR": "world",
             "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt", "FENCES_DB": "fences.db",
             "SITTER_DB": "sitter.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db", "PUSH_DB": "push.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "MAIL_BILLS_DB": "mail_bills.db", "ITEMS_LATENCY_LOG": "items-latency.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import push  # noqa: E402
import scheduled  # noqa: E402
import todos  # noqa: E402
import world as _world  # noqa: E402

push.push_card_async = lambda card: None   # never a real push from a test
LOG = _TMP / "items-latency.jsonl"
api.ITEMS_LATENCY_LOG = LOG
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}

TZ = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=TZ)
lib._local_now = lambda: NOW
T0 = NOW.timestamp()

# a stub runtime standing in as the real one (so #1548's caches are on): it counts what it's asked, answers empty
CALLS = {"n": 0}


def _stub(service, path, method="GET", *a, **k):
    CALLS["n"] += 1
    return {"nodes": [], "events": [], "chunks": [], "sources": []}


lib._runtime_request = _stub
lib._LIVE_RUNTIME_REQUEST = _stub
lib.kb_sources = lambda *a, **k: []
lib._LIVE_KB_SOURCES = lib.kb_sources
_world.household_calendars = lambda *a, **k: {"family"}
lib.tasks_for = lambda viewer, role: {"tasks": []}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Hale", "adult", "Alex's iPhone")
ROBIN, robin = pair("Robin Hale", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
TITLES = ("Return the zebra kite", "Jordan's quartz recital", "Robin's walrus errand")
ids = ["todo:" + todos.add(TITLES[0], alex, created_by="alex")["id"],
       "scheduled:" + scheduled.add(alex, TITLES[1], "notify", {"text": "x"}, T0 + 3600, audience="household")["id"],
       "todo:" + todos.add(TITLES[2], robin, created_by="robin")["id"]]


def clear_caches():
    with lib._READS_LOCK:
        lib._RECORDS.clear()
        lib._TIMELINE.clear()
        lib._KB_TITLES.update(at=0.0, gen=-1, val=None)


def rows():
    return [json.loads(x) for x in LOG.read_text().splitlines()] if LOG.exists() else []


def get(view, h):
    n = len(rows())
    r = c.get(f"/v1/items?view={view}", headers=h)
    got = rows()
    return r, (got[-1] if len(got) == n + 1 else None)


print("── the fields ──")
clear_caches()
r, row = get("now", ALEX)
check("Alex's Now answers, one log line", (r.status_code, row is not None), (200, True))
check("the line: the old fields plus parts and cold", sorted(row), ["cold", "ms", "parts", "role", "status", "ts", "view"])
READERS = {"todos", "scheduled", "routines", "tasks", "references", "chats", "notes", "linked", "bills", "mail",
           "events"}
check("a numeric (int, >= 0) entry for every reader that ran, plus kb_visible, snoozed and resolve",
      (READERS | {"kb_visible", "snoozed", "resolve"} <= set(row["parts"]),
       all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in row["parts"].values())), (True, True))
check("…and nothing that isn't a reader or a step", set(row["parts"]) - READERS - {"kb_visible", "snoozed", "resolve"},
      set())
check("resolve covers its readers (they run inside it)", row["parts"]["resolve"] >= max(row["parts"][k] for k in READERS),
      True)
check("cold is a boolean, and true on a first read (empty caches)", (type(row["cold"]), row["cold"]), (bool, True))
r2, row2 = get("now", ALEX)
check("the same request again within the TTL: warm", row2["cold"], False)
lib._reads_changed("record")
_r, row3 = get("now", ALEX)
check("after a record write: cold again", row3["cold"], True)

_r, urow = get("unsorted", ALEX)
check("Unsorted adds its suggested reader to parts", "suggested" in urow["parts"], True)
_r, krow = get("now", AVA)
check("a kid's Now: the same readers timed, role kid, ms only",
      (krow["role"], READERS <= set(krow["parts"]), all(type(v) is int for v in krow["parts"].values())),
      ("kid", True, True))

print("── one request's counts are its own ──")
clear_caches()
# a read outside any request counts nowhere (and never fails); this one fills the records cache
lib.record_nodes("bill")
check("a read outside a request leaves no counter behind", lib._READ_STATS.get(), None)
with lib.read_stats(lib.read_stats_new()) as other:
    lib.record_nodes("bill")
check("a counter set in one context isn't the request's: another context counted its own hit",
      (other["hit"], other["miss"]), (1, 0))
check("…and nothing is left set after it", lib._READ_STATS.get(), None)

print("── the body is byte-identical with the timing on and off ──")
clear_caches()
on = [c.get(f"/v1/items?view={v}", headers=h).content for v, h in (("now", ALEX), ("unsorted", ROBIN), ("now", AVA),
                                                                     ("time&from=2026-10-08&to=2026-10-09", ALEX))]
_real = api._timed_readers
api._timed_readers = lambda readers, parts, stats: readers
try:
    clear_caches()
    off = [c.get(f"/v1/items?view={v}", headers=h).content for v, h in (("now", ALEX), ("unsorted", ROBIN),
                                                                          ("now", AVA),
                                                                          ("time&from=2026-10-08&to=2026-10-09", ALEX))]
finally:
    api._timed_readers = _real
check("every view x viewer: the same bytes", [a == b for a, b in zip(on, off)], [True, True, True, True])
check("…and the bodies really carry the fixture (so the next check means something)",
      TITLES[0].encode() in on[0] and ids[0].encode() in on[0], True)

print("── never an id, a title or a name ──")
text = LOG.read_text()
leaks = [x for x in (*TITLES, *ids, alex, robin, ava, "Alex", "Robin", "Ava", "zebra", "quartz", "walrus")
         if x and x in text]
check("no fixture id, title, word, person id or name anywhere in the log", leaks, [])
check("every line: only names, ms and booleans in parts and cold",
      all(isinstance(r_["cold"], bool) and all(isinstance(k, str) and type(v) is int for k, v in r_["parts"].items())
          for r_ in rows()), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
