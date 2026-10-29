#!/usr/bin/env python3
# gate: hermetic
"""A trip's Live Activity stays right with the app closed (jellybean#753, trip_push.py): the event's ref, where a
removed event went (moved within 7 days by its UID, or cancelled), the pushes (leave-by, minutes, start and state;
never a title or place), the token following a moved event, per-person matching, and a hook that can't hurt ingest.
APNs and the calendar ledger are fakes; no network.
Run: python3 scripts/test_trip_push.py"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-trip-push-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "PUSH_DB": "push.db", "CONNECTORS_LEDGER": "ledger.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import push  # noqa: E402
import trip_push as tp  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.160} expected={expected!r:.160}")


UID = "0F3C9A2E-7D41-4C8B-9E55-2B0D4A0E0001"
OLD = f"{UID}@2026-10-05T16:00:00Z"
NEW = f"{UID}@2026-10-05T18:30:00Z"
BUNDLE = "com.zachshallbetter.jellybean.ios"

print("── the event's ref, and where it went ──")
check("the ref: 'c' + 32 hex, a valid activity ref", (len(tp.ref_for(OLD)), bool(push._REF_RX.fullmatch(tp.ref_for(OLD)))), (33, True))
check("…the same for the same occurrence, different for another", (tp.ref_for(OLD) == tp.ref_for(OLD), tp.ref_for(OLD) == tp.ref_for(NEW)), (True, False))
check("…neither the UID nor the time is in it", (UID.lower()[:8] in tp.ref_for(OLD), "2026" in tp.ref_for(OLD)), (False, False))
check("a UID with an @ in it", tp.split("abc@google.com@2026-10-05T16:00:00Z")[0], "abc@google.com")
check("not a calendar key: nothing", (tp.split("no-time-here"), tp.split("x@not-a-date")), (None, None))
check("moved: the same UID, nearest start within 7 days", tp.replacement(OLD, [OLD, NEW, f"{UID}@2026-10-09T16:00:00Z"])[0], NEW)
check("a UID that comes back months later is a new event, not a move", tp.replacement(OLD, [f"{UID}@2027-02-05T16:00:00Z"]), None)
check("another event's UID is never a move", tp.replacement(OLD, ["OTHER-UID@2026-10-05T18:30:00Z"]), None)

print("── what's pushed: leave-by, minutes, start, state ──")
new_start = datetime(2026, 10, 5, 18, 30, tzinfo=timezone.utc)
st = tp.state(1740, 24, new_start, "moved")
check("keys: never a title or place", sorted(st), ["leaveBy", "minutes", "start", "state"])
check("leave-by = new start − the phone's lead (seconds since 2001)", st["leaveBy"], new_start.timestamp() - 1740 - 978307200)

print("── registering a trip's token, with its lead and minutes ──")
devs = [{"id": "alex-phone", "person_id": "alex", "role": "adult", "revoked": False},
        {"id": "robin-phone", "person_id": "robin", "role": "adult", "revoked": False}]
REF = tp.ref_for(OLD)
check("kept", push.register_activity("alex-phone", "alex", "adult", "trip", REF, "ab" * 32, "development", BUNDLE,
                                     trip={"lead": 1740, "minutes": 24}), True)
check("its lead and minutes go with it", [(t["device_id"], t["lead"], t["minutes"]) for t in push.trip_targets("alex", REF, devs)],
      [("alex-phone", 1740.0, 24)])
for bad in ({"lead": -1, "minutes": 3}, {"lead": 60, "minutes": 2.5}, {"lead": "x", "minutes": 3}, "fast"):
    try:
        push.register_activity("alex-phone", "alex", "adult", "trip", REF, "ab" * 32, "development", BUNDLE, trip=bad)
        check(f"refused: {bad!r}", "kept", "refused")
    except ValueError:
        check(f"refused: {bad!r}", "refused", "refused")
check("a token without details isn't a target (titan can't say its leave-by)",
      (push.register_activity("robin-phone", "robin", "adult", "trip", REF, "cd" * 32, "development", BUNDLE),
       [t["device_id"] for t in push.trip_targets("robin", REF, devs)]), (True, []))

print("── moved: an update, and the token follows the event ──")
sent: list = []
rec = lambda t, b: sent.append((t["device_id"], b)) or (200, "")  # noqa: E731
out = tp.judge("alex", OLD, live=lambda prefix: [NEW] if NEW.startswith(prefix) else [], devices=devs, send=rec)
check("moved", out, "moved")
check("one update to Alex's phone", [(d, b["aps"]["event"], b["aps"]["content-state"]["state"]) for d, b in sent],
      [("alex-phone", "update", "moved")])
wire = json.dumps([b for _, b in sent])
check("the payload: no title, no place, no UID", [w in wire for w in ("Dentist", "Pearl", UID, "location", "title")], [False] * 5)
check("the token now answers to the moved event's ref", ([t["device_id"] for t in push.trip_targets("alex", tp.ref_for(NEW), devs)],
      push.trip_targets("alex", REF, devs)), (["alex-phone"], []))
LATER = f"{UID}@2026-10-06T09:00:00Z"
sent.clear()
check("a second move is noticed too", tp.judge("alex", NEW, live=lambda p: [LATER], devices=devs, send=rec), "moved")

print("── cancelled: the activity ends, and says why ──")
sent.clear()
check("cancelled", tp.judge("alex", LATER, live=lambda p: [], devices=devs, send=rec), "cancelled")
body = sent[0][1]["aps"]
check("an end, state cancelled", (body["event"], body["content-state"]["state"]), ("end", "cancelled"))
check("iOS's default dismissal (no dismissal-date): the person sees it ended", "dismissal-date" in body, False)
check("its tokens are gone", push.trip_targets("alex", tp.ref_for(LATER), devs), [])

print("── per person: another's calendar never drives someone's trip ──")
push.register_activity("alex-phone", "alex", "adult", "trip", REF, "ef" * 32, "development", BUNDLE, trip={"lead": 900, "minutes": 10})
sent.clear()
check("Robin's batch removing the same occurrence: nothing for Alex", tp.judge("robin", OLD, live=lambda p: [], devices=devs, send=rec), "none")
check("…nothing sent, and Alex's activity is untouched", (sent, [t["device_id"] for t in push.trip_targets("alex", REF, devs)]), ([], ["alex-phone"]))
import connectors_runner as cx  # noqa: E402
con = cx._db(Path(os.environ["CONNECTORS_LEDGER"]))
con.execute("INSERT INTO items (source, source_id, content_hash, item_id, synced_at, owner, upstream_id) VALUES "
            "('calendar', ?, 'h', 'i1', ?, 'robin', ?)", ("robin:" + NEW, time.time(), NEW))
con.commit(); con.close()
check("Alex's lookup sees only Alex's own items (Robin's copy of the moved event isn't Alex's move)",
      (cx.live_upstream_ids("alex", "calendar", UID + "@"), cx.live_upstream_ids("robin", "calendar", UID + "@")), ([], [NEW]))

print("── the ingest hook ──")
calls: list = []
n = tp.after_calendar_batch("alex", [{"source_id": OLD, "deleted": True}, {"source_id": NEW, "title": "Dentist"},
                                     {"source_id": "no-time", "deleted": True}, "junk"],
                            grace=0.05, run=lambda o, s: calls.append((o, s)) or "moved")
time.sleep(0.3)
check("each removed calendar item is judged after the grace, the person's own", (n, calls), (1, [("alex", OLD)]))
n = tp.after_calendar_batch("alex", [{"source_id": OLD, "deleted": True}], grace=0.05,
                            run=lambda o, s: (_ for _ in ()).throw(RuntimeError("boom")))
time.sleep(0.3)
check("a judgement that raises is caught (the batch was already stored)", n, 1)
check("a batch it can't read schedules nothing and never raises", tp.after_calendar_batch("alex", None), 0)   # type: ignore[arg-type]

print("── the routes ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}
s = c.post("/v1/pair/start", json={"new_person": {"name": "Jordan", "role": "adult"}}, headers=M).json()
J = {"X-Agent-Token": c.post("/v1/pair/claim", json={"code": s["code"], "device_name": "Jordan's iPhone", "platform": "ios"}).json()["token"]}
r = c.post("/v1/devices/self/activities", json={"kind": "trip", "ref": REF, "token": "12" * 32, "environment": "development",
                                                "bundle": BUNDLE, "trip": {"lead": 1200, "minutes": 15}}, headers=J)
check("a trip's token with its lead and minutes: 204", r.status_code, 204)
check("a malformed trip: 400", c.post("/v1/devices/self/activities", json={"kind": "trip", "ref": REF, "token": "12" * 32,
      "environment": "development", "bundle": BUNDLE, "trip": {"lead": "soon"}}, headers=J).status_code, 400)
hooked: list = []
real_ingest, real_hook = cx.ingest, tp.after_calendar_batch
cx.ingest = lambda source, items, **kw: {"written": 0, "unchanged": 0, "removed": len(items), "forgotten": 0, "errors": []}
tp.after_calendar_batch = lambda owner, items: hooked.append((owner, len(items))) or 0
r = c.post("/v1/connectors/calendar/items", json={"items": [{"source_id": OLD, "deleted": True, "deleted_reason": "deleted"}]}, headers=M)
check("the calendar ingest calls the hook with the caller's own person, after the batch",
      (r.status_code, hooked), (200, [(hh.owner_identity().person_id, 1)]))
c.post("/v1/connectors/mail/items", json={"items": [{"source_id": "m1", "deleted": True}]}, headers=M)
check("…only for the calendar", len(hooked), 1)
cx.ingest, tp.after_calendar_batch = real_ingest, real_hook
check("the route doc is current", bool(re.search(r"trip", (Path(__file__).parent.parent / "docs" / "api-routes.md").read_text())), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
