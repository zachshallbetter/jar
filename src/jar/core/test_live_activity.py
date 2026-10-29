#!/usr/bin/env python3
# gate: hermetic
"""A task's Live Activity on its owner's phone (titan-node#725, jellybean#350): the token route, owner-only
targets, the ActivityKit payload, the privacy default, and the task hooks, against a fake APNs.
Run: python3 scripts/test_live_activity.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-liveact-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "TASK_LOG_DB": "task_log.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "GEMINI_API_KEY"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import push  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


BUNDLE = "com.zachshallbetter.jellybean.ios"
DEVICES = [{"id": "dev-z", "person_id": "zach", "role": "admin"},
           {"id": "dev-z2", "person_id": "zach", "role": "admin", "revoked": 1},
           {"id": "dev-m", "person_id": "p_mir", "role": "adult"},
           {"id": "dev-h", "person_id": "p_kitchen", "role": "household"}]


def rows():
    con = push._db()
    try:
        return sorted(con.execute("SELECT device_id, person, kind, ref, token FROM activity_tokens").fetchall())
    finally:
        con.close()


print("── registration ──")
push.register("dev-z", "a" * 64, "development", BUNDLE)
check("registers, the bundle from the card-push row",
      (push.register_activity("dev-z", "zach", "admin", "task", "wf-1", "1" * 64, "development"), rows()),
      (True, [("dev-z", "zach", "task", "wf-1", "1" * 64)]))
push.register_activity("dev-z", "zach", "admin", "task", "wf-1", "2" * 64, "development")
check("a rotated token replaces the old one", rows(), [("dev-z", "zach", "task", "wf-1", "2" * 64)])
check("token null removes it", (push.register_activity("dev-z", "zach", "admin", "task", "wf-1", None, None), rows()),
      (True, []))
for label, args in (("a household device", ("dev-h", "p_kitchen", "household", "task", "wf-1", "3" * 64, "development", BUNDLE)),
                    ("a bad kind", ("dev-z", "zach", "admin", "chore", "wf-1", "3" * 64, "development")),
                    ("a bad ref", ("dev-z", "zach", "admin", "task", "wf 1/../x", "3" * 64, "development")),
                    ("a bad token", ("dev-z", "zach", "admin", "task", "wf-1", "nothex", "development")),
                    ("a bad environment", ("dev-z", "zach", "admin", "task", "wf-1", "3" * 64, "staging")),
                    ("no bundle and no card-push row", ("dev-m", "p_mir", "adult", "task", "wf-1", "3" * 64, "development"))):
    try:
        push.register_activity(*args)
        check(f"{label} refused", "accepted", "ValueError")
    except ValueError:
        check(f"{label} refused", "ValueError", "ValueError")

print("── owner-only targets ──")
push.register_activity("dev-z", "zach", "admin", "task", "wf-1", "1" * 64, "development")
push.register_activity("dev-z2", "zach", "admin", "task", "wf-1", "4" * 64, "development", BUNDLE)  # revoked
push.register_activity("dev-m", "p_mir", "adult", "task", "wf-1", "5" * 64, "production", BUNDLE)  # not the owner
push.register_activity("dev-z", "zach", "admin", "task", "wf-2", "6" * 64, "development")           # another task
check("only the owner's live device, this task", [t["device_id"] for t in push.activity_targets("zach", "task", "wf-1", DEVICES)],
      ["dev-z"])
check("a device that moved to someone else: not the owner's",
      push.activity_targets("zach", "task", "wf-1", [{"id": "dev-z", "person_id": "p_mir", "role": "adult"}]), [])
check("Robin's own, for her", [t["device_id"] for t in push.activity_targets("p_mir", "task", "wf-1", DEVICES)], ["dev-m"])

print("── content-state and payload ──")
check("default: the goal (Zach, 10-03), counts, no step text",
      push.task_state(3, 4, title="Call the DMV about the plates", goal="Renew the car registration"),
      {"goal": "Renew the car registration", "step": 3, "total": 4, "stepTitle": "", "done": False})
long = push.task_state(1, 2, goal="Plan Ava's birthday party at the trampoline park with twelve friends and pizza after")
check("the goal is capped at 60 characters", (len(long["goal"]), long["goal"].endswith("…")), (60, True))
check("…and redacted (no card or account numbers)", "4111" in push.task_state(1, 2, goal="Pay card 4111 1111 1111 1111")["goal"],
      False)
check("waiting, default: the fixed phrase", push.task_state(2, 4, title="x", waiting="Your license number")["waiting"],
      "Waiting on you.")
check("opted in: the step's title and what it waits on",
      {k: v for k, v in push.task_state(2, 4, title="Call the DMV", waiting="Your license number", detail=True).items()
       if k in ("stepTitle", "waiting")}, {"stepTitle": "Call the DMV", "waiting": "Your license number"})
check("nextAt: seconds since 2001-01-01 (Swift's Codable Date)",
      push.task_state(1, 2, next_at=978307200 + 3600)["nextAt"], 3600.0)
up = push.activity_payload(push.task_state(3, 4), now=1_800_000_000)
check("update: timestamp, event, content-state; no dismissal", (sorted(up["aps"]), up["aps"]["event"], up["aps"]["timestamp"]),
      (["content-state", "event", "timestamp"], "update", 1_800_000_000))
end = push.activity_payload(push.task_state(4, 4, done=True), "end", now=1_800_000_000)
check("end: a dismissal date after it", (end["aps"]["event"], end["aps"]["dismissal-date"] - 1_800_000_000,
                                         end["aps"]["content-state"]["done"]), ("end", 900, True))

print("── sending ──")
calls = []
real_post = push._curl_post
push._curl_post = lambda t, body, kind, prio, collapse: calls.append((t["bundle"], kind, prio, t["token"])) or (200, "")
push._jwt = lambda: "jwt"
push._curl_activity({"device_id": "dev-z", "ref": "wf-1", "token": "1" * 64, "environment": "development",
                     "bundle": BUNDLE}, up)
push._curl_activity({"device_id": "dev-z", "ref": "wf-1", "token": "1" * 64, "environment": "development",
                     "bundle": BUNDLE}, end)
check("topic .push-type.liveactivity, type liveactivity, priority 5 update / 10 end",
      [(b, k, p) for b, k, p, _ in calls],
      [(BUNDLE + ".push-type.liveactivity", "liveactivity", "5"), (BUNDLE + ".push-type.liveactivity", "liveactivity", "10")])
push._curl_post = real_post
sent = []
check("unconfigured and no fake: nothing sent", push.send_activity("zach", "task", "wf-1", push.task_state(1, 4), devices=DEVICES), [])
out = push.send_activity("zach", "task", "wf-1", push.task_state(2, 4), devices=DEVICES,
                         send=lambda t, b: sent.append((t["device_id"], b["aps"]["event"])) or (200, ""))
check("an update reaches the owner's phone only", (out, sent), (["dev-z: 200"], [("dev-z", "update")]))
push.send_activity("zach", "task", "wf-2", push.task_state(1, 2), devices=DEVICES, send=lambda t, b: (410, "Unregistered"))
check("a dead token goes", [r for r in rows() if r[3] == "wf-2"], [])
push.send_activity("zach", "task", "wf-1", push.task_state(4, 4, done=True), "end", devices=DEVICES,
                   send=lambda t, b: (200, ""))
check("the end forgets every token for that task", [r for r in rows() if r[3] == "wf-1"], [])

print("\n── the API ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": "ios"}).json()
    return {"X-Agent-Token": cl["token"]}, cl["device"]["id"], s["person"]["id"]


MIR, dev, rob = pair("Robin", "adult", "Robin's iPhone")
KIT, kdev, _ = pair("Kitchen", "household", "Kitchen iPad")
A = {"kind": "task", "ref": "wf-9", "token": "7" * 64, "environment": "development", "bundle": BUNDLE}
check("her phone registers an activity", c.post("/v1/devices/self/activities", json=A, headers=MIR).status_code, 204)
check("stored for her device and her", [r for r in rows() if r[3] == "wf-9"], [(dev, rob, "task", "wf-9", "7" * 64)])
check("a household device: refused by the route table", c.post("/v1/devices/self/activities", json=A, headers=KIT).status_code, 403)
check("the master token (no device): 400", c.post("/v1/devices/self/activities", json=A, headers=M).status_code, 400)
check("a bad kind: 400", c.post("/v1/devices/self/activities", json={**A, "kind": "x"}, headers=MIR).status_code, 400)
check("token null: 204, gone", (c.post("/v1/devices/self/activities", json={**A, "token": None}, headers=MIR).status_code,
                                [r for r in rows() if r[3] == "wf-9"]), (204, []))
check("detail is off by default", c.get("/v1/me", headers=MIR).json()["settings"]["live_activity_detail"], False)
check("PATCH /v1/me turns it on", c.patch("/v1/me", json={"live_activity_detail": True}, headers=MIR).json()["settings"],
      {"live_activity_detail": True})
check("read back", c.get("/v1/me", headers=MIR).json()["settings"]["live_activity_detail"], True)
check("not a bool: 400", c.patch("/v1/me", json={"live_activity_detail": "on"}, headers=MIR).status_code, 400)
check("never in the turn's [PREFERENCES] line", "live_activity_detail" in hh.DISPLAY_PREFERENCES, True)
c.post("/v1/devices/self/activities", json=A, headers=MIR)
c.delete(f"/v1/devices/{dev}", headers=M)
check("revoking the phone drops its activities", [r for r in rows() if r[0] == dev], [])

print("\n── the task hooks ──")
import agent_runtime_lib as lib  # noqa: E402
lib._TURN_IO.source = "user"   # the deploy gate runs with AGENT_TURN_SOURCE=test
got = []
push.send_activity_async = lambda owner, kind, ref, state, event="update", dismiss_after=None, start=None: got.append(
    (owner, kind, ref, state, event, dismiss_after, start))
STEPS = [{"step_key": "a", "title": "Find the form"}, {"step_key": "b", "title": "Fill it in"},
         {"step_key": "c", "title": "Mail it"}]
lib._task_live({"instance_id": "wf-5", "current_step_key": "b", "metadata": {"owner": rob}}, STEPS)
check("step done: the owner, step 2 of 3, an update",
      got[-1][:3] + (got[-1][3]["step"], got[-1][3]["total"], got[-1][4]), (rob, "task", "wf-5", 2, 3, "update"))
check("her detail on: the title shows", got[-1][3]["stepTitle"], "Fill it in")
check("an update can start the activity on a phone without one (jellybean#350): {taskID, goal}",
      got[-1][6], {"taskID": "wf-5", "goal": got[-1][3]["goal"]})
st.set_pref(rob, "live_activity_detail", "off")
lib._task_live({"instance_id": "wf-5", "current_step_key": "c", "metadata": {"owner": rob}}, STEPS, waiting="the stamp")
check("waiting, detail off: counts and the fixed phrase",
      {k: got[-1][3][k] for k in ("step", "stepTitle", "waiting")}, {"step": 3, "stepTitle": "", "waiting": "Waiting on you."})
lib._task_live({"instance_id": "wf-5", "metadata": {"owner": rob}}, STEPS, done=True)
check("finished: end, 3 of 3, done", (got[-1][4], got[-1][3]["step"], got[-1][3]["done"]), ("end", 3, True))
lib._task_live({"instance_id": "wf-6", "current_step_key": "a"}, STEPS)
check("an instance from before owners were recorded is Zach's", got[-1][0], hh.ADMIN_PERSON_ID)
n = len(got)
lib._TURN_IO.source = "eval"
lib._task_live({"instance_id": "wf-5", "current_step_key": "a", "metadata": {"owner": rob}}, STEPS)
check("an eval turn sends nothing", len(got), n)

print("\n── a task that waits a day: the activity ends, it doesn't hang (10-03, real device) ──")
NOW = 1_790_000_000.0
check("finished: end", lib.live_plan(done=True, waiting=None, running=False, next_at=None, now=NOW), ("end", None))
check("step done, the next one tomorrow at 9: end", lib.live_plan(done=False, waiting=None, running=False,
      next_at=NOW + 20 * 3600, now=NOW), ("end", None))
check("…in 30 minutes or less: stays, with nextAt", lib.live_plan(done=False, waiting=None, running=False,
      next_at=NOW + 25 * 60, now=NOW), ("update", NOW + 25 * 60))
check("running a step: update, no nextAt", lib.live_plan(done=False, waiting=None, running=True, next_at=NOW + 99999,
      now=NOW), ("update", None))
check("waiting on the person: update, no nextAt", lib.live_plan(done=False, waiting="x", running=False,
      next_at=NOW + 99999, now=NOW), ("update", None))
check("a task run by hand (no schedule): update", lib.live_plan(done=False, waiting=None, running=False, next_at=None,
      now=NOW), ("update", None))
lib._TURN_IO.source = "user"
import time as _t  # noqa: E402
saved_next = lib._next_step_at
lib._next_step_at = lambda tid, now: now + 20 * 3600          # a daily task: tomorrow at 9
try:
    lib._task_live({"instance_id": "wf-7", "current_step_key": "b", "metadata": {"owner": rob},
                    "input": {"goal": "Ava's party"}}, STEPS)
    check("the hook carries the task's goal", got[-1][3]["goal"], "Ava's party")
    check("the hook: a step done, next tomorrow → end, dismissed soon, no nextAt",
          (got[-1][4], got[-1][5], "nextAt" in got[-1][3]), ("end", lib.LIVE_DISMISS_S, False))
    lib._task_live({"instance_id": "wf-7", "current_step_key": "c", "metadata": {"owner": rob}}, STEPS, running=True)
    check("…the next step starts running: update (a fresh activity's token gets it)", (got[-1][4], got[-1][3]["step"]),
          ("update", 3))
    lib._next_step_at = lambda tid, now: now + 10 * 60
    lib._task_live({"instance_id": "wf-7", "current_step_key": "b", "metadata": {"owner": rob}}, STEPS)
    check("…next in 10 minutes: update, nextAt in seconds since 2001",
          (got[-1][4], round(got[-1][3]["nextAt"] + 978307200 - _t.time()) // 60), ("update", 10))
    lib._task_live({"instance_id": "wf-7", "metadata": {"owner": rob}}, STEPS, done=True)
    check("…finished: end with the default dismissal", (got[-1][4], got[-1][5]), ("end", None))
finally:
    lib._next_step_at = saved_next
sent = []
push.register_activity("dev-z", "zach", "admin", "task", "wf-x", "8" * 64, "development", BUNDLE)
push.send_activity("zach", "task", "wf-x", push.task_state(1, 2), "end", dismiss_after=120, devices=DEVICES,
                   send=lambda t, b: sent.append(b["aps"]) or (200, ""))
check("send_activity: the dismissal is the one asked for", [a["dismissal-date"] - a["timestamp"] for a in sent], [120])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
