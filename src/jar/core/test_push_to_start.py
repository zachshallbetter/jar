#!/usr/bin/env python3
# gate: hermetic   (temp push.db; a fake APNs sender, nothing leaves the process)
"""jellybean#350: a task's Live Activity starts while the app is closed. The app registers its push-to-start token once
({kind: "task-start", token, environment, bundle}); on an update (a step running, waiting on the person, the next step
within 30 minutes) titan sends a `liveactivity` "start" to the owner's devices that have no activity of the task yet,
once per device per task; the app registers that activity's own token and the updates go there. Owner-only, never a
shared household device; the end clears it.
Run: python3 scripts/test_push_to_start.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-pts-test-"))
os.environ["PUSH_DB"] = str(_TMP / "push.db")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import push  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


DEVICES = [{"id": "dev-a", "person_id": "p_alex", "role": "adult"},
           {"id": "dev-a2", "person_id": "p_alex", "role": "adult"},
           {"id": "dev-r", "person_id": "p_robin", "role": "adult"},
           {"id": "dev-h", "person_id": "household", "role": "household"}]
BUNDLE = "com.example.jellybean"
sent: list[tuple[str, dict]] = []


def fake(target, body):
    sent.append((target["device_id"], body))
    return 200, ""


print("── registration ──")
check("task-start registers with any ref (the app sends its attributes type)",
      push.register_activity("dev-a", "p_alex", "adult", "task-start", "TaskActivityAttributes", "a" * 64, "development",
                             BUNDLE), True)
push.register_activity("dev-a2", "p_alex", "adult", "task-start", None, "b" * 64, "development", BUNDLE)
push.register_activity("dev-r", "p_robin", "adult", "task-start", None, "c" * 64, "development", BUNDLE)
try:
    push.register_activity("dev-h", "household", "household", "task-start", None, "d" * 64, "development", BUNDLE)
    check("a shared household device is refused", False, True)
except ValueError:
    check("a shared household device is refused", True, True)
try:
    push.register_activity("dev-a", "p_alex", "adult", "bogus", None, "a" * 64, "development", BUNDLE)
    check("an unknown kind is refused", False, True)
except ValueError as exc:
    check("an unknown kind is refused", "task-start" in str(exc), True)

print("── the start ──")
state = push.task_state(2, 4, goal="Renew the passport", title="Fill it in", detail=False)
attrs = {"taskID": "wf-9", "goal": state["goal"]}
out = push.send_activity("p_alex", "task", "wf-9", state, "update", devices=DEVICES, send=fake, start=attrs)
check("both of Alex's phones get a start, nobody else's", sorted(d for d, _ in sent), ["dev-a", "dev-a2"])
aps = sent[0][1]["aps"]
check("the payload", (aps["event"], aps["attributes-type"], aps["attributes"], aps["input-push-token"],
                      aps["alert"], isinstance(aps["timestamp"], int)),
      ("start", "TaskActivityAttributes", {"taskID": "wf-9", "goal": "Renew the passport"}, 1,
       {"title": "Renew the passport", "body": "Step 2 of 4"}, True))
check("content-state is today's task state (goal only by default: no step words)",
      (aps["content-state"]["goal"], aps["content-state"]["stepTitle"]), ("Renew the passport", ""))
check("reported per device", sorted(out), sorted(["dev-a: start 200", "dev-a2: start 200"]))

sent.clear()
push.send_activity("p_alex", "task", "wf-9", state, "update", devices=DEVICES, send=fake, start=attrs)
check("once per device per task: the next update doesn't start it again", sent, [])

push.register_activity("dev-a", "p_alex", "adult", "task", "wf-9", "e" * 64, "development", BUNDLE)
sent.clear()
push.send_activity("p_alex", "task", "wf-9", state, "update", devices=DEVICES, send=fake, start=attrs)
check("once the activity registered its own token, updates go to it", [d for d, b in sent if b["aps"]["event"] == "update"],
      ["dev-a"])

print("── waiting, and the end ──")
w = push.start_payload(push.task_state(3, 4, goal="Renew the passport", waiting="the photo"), attrs)
check("waiting on the person: a fixed phrase", w["aps"]["alert"]["body"], "Waiting on you.")
push.send_activity("p_alex", "task", "wf-9", push.task_state(4, 4, done=True, goal="x"), "end", devices=DEVICES, send=fake)
sent.clear()
push.send_activity("p_alex", "task", "wf-9", state, "update", devices=DEVICES, send=fake, start=attrs)
check("after the end, a new run starts again", sorted(d for d, b in sent if b["aps"]["event"] == "start"), ["dev-a", "dev-a2"])
sent.clear()
push.send_activity("p_alex", "task", "wf-10", push.task_state(4, 4, done=True, goal="x"), "end", devices=DEVICES,
                   send=fake, start=attrs)
check("an end never starts anything", [b for _, b in sent if b["aps"]["event"] == "start"], [])

print("── dead tokens ──")
sent.clear()
push.send_activity("p_robin", "task", "wf-11", state, "update", devices=DEVICES,
                   send=lambda t, b: (410, "Unregistered"), start={"taskID": "wf-11", "goal": "g"})
con = push._db()
left = con.execute("SELECT COUNT(*) FROM activity_tokens WHERE device_id='dev-r' AND kind='task-start'").fetchone()[0]
con.close()
check("a dead push-to-start token goes", left, 0)

print("── the null token turns it off ──")
check("token null removes the device's start token",
      push.register_activity("dev-a2", "p_alex", "adult", "task-start", "TaskActivityAttributes", None, None), True)
print("\nexample start payload:", json.dumps(push.start_payload(state, attrs, now=1_800_000_000), ensure_ascii=False))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
