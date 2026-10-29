#!/usr/bin/env python3
# gate: hermetic
"""Coding agents on the Mac, seen and stopped from the iPhone (jellybean#699, agent_runs.py): the Mac's reports, the
phone's list, the Live Activity pushes (counts and the repo's name only), and Stop routed to the Mac. In memory only.
The Mac and APNs are fakes here; no network.
Run: python3 scripts/test_agent_runs.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-agent-runs-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "PUSH_DB": "push.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runs as ar  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.160} expected={expected!r:.160}")


RID = "6f1d2c39-6c8b-4b7b-9c55-2b0d4a0e0001"
T0 = 1_791_100_000.0


def report(**kw):
    r = {"id": RID, "repo": "titan-node", "state": "running", "started_at": T0, "updated_at": T0,
         "files_changed": 0, "lines_added": 0, "lines_removed": 0, "ends_by": T0 + 1800}
    r.update(kw)
    return r


def refused(body) -> str | None:
    try:
        ar.clean_report(body)
    except ar.ReportError as exc:
        return str(exc)
    return None


print("── the Mac's report: exactly the contract's fields ──")
check("a running run, kept as sent", ar.clean_report(report())["repo"], "titan-node")
check("the id is lowercased", ar.clean_report(report(id=RID.upper()))["id"], RID)
check("a prompt never travels: refused, not dropped", refused(report(prompt="Fix the login bug")), "unknown fields: prompt")
check("…nor a path or branch", refused(report(worktree="/Users/x/wt", branch="agent/x")), "unknown fields: branch, worktree")
check("never the ase repo, whatever the case", [refused(report(repo=r)) for r in ("ase", "ASE", "Ase")],
      ["not this repository"] * 3)
check("a repo is a folder name, not a path", refused(report(repo="../titan-node")) is not None, True)
check("the id is the run's UUID", refused(report(id="abc")), "id must be the run's UUID")
check("the state is one of five", refused(report(state="paused")) is not None, True)
check("counts are whole numbers", [refused(report(files_changed=v)) is not None for v in (-1, 1.5, True, "3")],
      [True, True, True, True])
check("times are Unix seconds", refused(report(started_at="noon")), "started_at must be Unix seconds")
check("ends_by may be left out", ar.clean_report({k: v for k, v in report().items() if k != "ends_by"})["ends_by"], None)

print("── the runs, in memory ──")
now = [T0]
runs = ar.Runs(now=lambda: now[0])
r0 = ar.clean_report(report())
ev, run = runs.report("Zach", "mac-1", "Alex's MacBook Pro", r0)
check("the first running report starts the activity", (ev, run["person"], run["mac"]), ("start", "zach", "Alex's MacBook Pro"))
check("a heartbeat with the same counts: nothing to push", runs.report("zach", "mac-1", None,
      ar.clean_report(report(updated_at=T0 + 120))), None)
ev, run = runs.report("zach", "mac-1", None, ar.clean_report(report(updated_at=T0 + 240, files_changed=3, lines_added=41)))
check("a count changed: an update", (ev, run["files_changed"], run["mac"]), ("update", 3, "Alex's MacBook Pro"))
try:
    runs.report("robin", "mac-9", None, r0)
    check("another person can't report my run", "kept", "refused")
except ar.ReportError:
    check("another person can't report my run", "refused", "refused")
check("my list: the run, with the Mac's name", [(x["id"], x["mac"], x["state"]) for x in runs.for_person("zach")],
      [(RID, "Alex's MacBook Pro", "running")])
check("someone else's list: nothing of mine", runs.for_person("robin"), [])
now[0] = T0 + 240 + 11 * 60
check("not heard from in 10 minutes: unknown", runs.for_person("zach")[0]["state"], "unknown")
ev, run = runs.report("zach", "mac-1", None, ar.clean_report(report(state="done", updated_at=now[0], files_changed=4)))
check("done: the activity ends", (ev, run["state"]), ("end", "done"))
check("a late heartbeat never brings it back", runs.report("zach", "mac-1", None, r0), None)
check("…still done", runs.get(RID)["state"], "done")
now[0] += 61 * 60
check("an hour after it ended, it's gone (nothing kept)", (runs.get(RID), runs.for_person("zach")), (None, []))
other = "6f1d2c39-6c8b-4b7b-9c55-2b0d4a0e0002"
runs.report("zach", "mac-1", None, ar.clean_report(report(id=other, started_at=now[0], updated_at=now[0])))
check("mark_stopped: the route's answer ends it", runs.mark_stopped(other)["state"], "stopped")
check("…only once", runs.mark_stopped(other), None)

print("── the Live Activity's payload: counts, times and the repo's name only ──")
import push  # noqa: E402
secret = "Refactor PrivateThing in secrets/notes.txt"
rep = ar.clean_report(report(files_changed=3, lines_added=41, lines_removed=7))
run = {**rep, "person": "zach", "mac": "Alex's MacBook Pro", "received_at": T0, "ended_at": None}
state = ar.content_state(run)
check("content-state keys", sorted(state), ["endsBy", "filesChanged", "linesAdded", "linesRemoved", "startedAt", "state",
                                            "updatedAt"])
check("dates as Swift reads them (seconds since 2001)", state["startedAt"], T0 - 978307200)
start = push.start_payload(state, ar.start_attributes(run), attributes_type=ar.ATTRIBUTES_TYPE, alert=ar.start_alert(run))
check("the start: its type, attributes and alert", (start["aps"]["attributes-type"], start["aps"]["attributes"],
      start["aps"]["alert"]), ("AgentRunActivityAttributes", {"runID": RID, "repo": "", "mac": ""},
                               {"title": "Claude on your Mac", "body": "Claude started on your Mac"}))
wire = json.dumps([start, push.activity_payload(state), push.activity_payload(state, "end")])
check("no prompt or file text can reach the payload (the report refuses it; nothing else is read)",
      [w in wire for w in ("Refactor", "PrivateThing", "secrets", "notes.txt", "prompt", "worktree", "branch")],
      [False] * 7)
check("the report with a prompt never becomes a run", refused({**report(), "prompt": secret}) is not None, True)

print("── the push kinds ──")
check("agent and agent-start are kinds", (push.register_activity("dev-1", "zach", "admin", "agent", RID, "ab" * 32,
      "development", "com.zachshallbetter.jellybean.ios"), push.register_activity(
      "dev-2", "zach", "admin", "agent-start", "AgentRunActivityAttributes", "cd" * 32, "development",
      "com.zachshallbetter.jellybean.ios")), (True, True))
check("task kinds still are", push.register_activity("dev-1", "zach", "admin", "task-start", "x", "ef" * 32, "development",
                                                     "com.zachshallbetter.jellybean.ios"), True)
sent: list = []
devs = [{"id": d, "person_id": "zach", "role": "admin", "revoked": False} for d in ("dev-1", "dev-2")]
push.send_activity("zach", "agent", RID, state, "update", devices=devs, send=lambda t, b: sent.append((t["device_id"],
                   b["aps"]["event"], b["aps"].get("attributes-type"))) or (200, ""),
                   start=ar.start_attributes(run), start_type=ar.ATTRIBUTES_TYPE, start_alert=ar.start_alert(run))
check("the phone with no activity of it is started; the one showing it is updated",
      sorted(sent), [("dev-1", "update", None), ("dev-2", "start", "AgentRunActivityAttributes")])
sent.clear()
push.send_activity("zach", "agent", RID, state, "update", devices=devs, send=lambda t, b: sent.append(t["device_id"]) or (200, ""),
                   start=ar.start_attributes(run), start_type=ar.ATTRIBUTES_TYPE, start_alert=ar.start_alert(run))
check("…started once", sorted(sent), ["dev-1"])
push.send_activity("zach", "agent", RID, state, "end", devices=devs, send=lambda t, b: (200, ""))
check("the end forgets its tokens", push.activity_targets("zach", "agent", RID, devs), [])
check("a task's start still uses the task's token", [t["device_id"] for t in push.start_targets("zach", "t-1", devs)], ["dev-1"])

print("── agent_stop is never the model's ──")
import agent_runtime_lib as lib  # noqa: E402
declared = {d["name"] for d in lib._gemini_function_declarations(None)}
check("not in the tool registry, the device tools, or Gemini's declarations",
      ("agent_stop" in lib._TOOL_REGISTRY, "agent_stop" in lib._DEVICE_TOOLS, "agent_stop" in declared), (False, False, False))
check("…while run_agent is (the tables are the right ones)", "run_agent" in lib._DEVICE_TOOLS, True)

print("── the routes ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import cross_device as xd  # noqa: E402
import household_identity as hh  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
store = hh.HouseholdStore(_TMP / "api.db")
hh.reset_store(store)
ar.RUNS.clear()
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER, "X-Device-Key": "mac-key-1"}


def paired(name, role, device):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    return {"X-Agent-Token": c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": "ios"}).json()["token"]}


KID, ADULT = paired("Ava", "kid", "Ava's iPad"), paired("Robin", "adult", "Robin's iPhone")
r = c.post("/v1/agent-runs", json=report(), headers=M)
check("the Mac reports a run", (r.status_code, r.json()), (200, {"id": RID, "kept": True}))
check("a prompt is refused with 400", c.post("/v1/agent-runs", json=report(prompt="x"), headers=M).status_code, 400)
check("the ase repo: 400", c.post("/v1/agent-runs", json=report(repo="ase"), headers=M).status_code, 400)
runs_list = c.get("/v1/agent-runs", headers=M).json()["runs"]
check("the owner's list", [(x["id"], x["repo"], x["state"]) for x in runs_list], [(RID, "titan-node", "running")])
check("a kid may never: 403 (the role)", (c.get("/v1/agent-runs", headers=KID).status_code,
      c.post(f"/v1/agent-runs/{RID}/stop", headers=KID).status_code), (403, 403))
check("another adult sees none of mine", c.get("/v1/agent-runs", headers=ADULT).json(), {"runs": []})
check("another adult's stop of my run: 404 (nobody learns it exists)", c.post(f"/v1/agent-runs/{RID}/stop", headers=ADULT).status_code, 404)
check("an unknown run: 404", c.post("/v1/agent-runs/6f1d2c39-6c8b-4b7b-9c55-2b0d4a0e0fff/stop", headers=M).status_code, 404)
check("the routes' roles: admin and adult only", [hh.route_allowed(role, "POST", f"/v1/agent-runs/{RID}/stop")
      for role in ("admin", "adult", "kid", "household")], [True, True, False, False])

routed: list = []
answer = {"result": '{"state":"stopped"}'}
store.macs_for = lambda pid: [{"device_key": "mac-key-1", "name": "Alex's MacBook Pro"}]


def fake_route(req, **kw):
    routed.append((req["tool"], req["args"], kw["mac"]["device_key"], kw["approved"]))
    return answer


xd_route, xd.route = xd.route, fake_route
r = c.post(f"/v1/agent-runs/{RID}/stop", headers=M)
check("stop: routed to the Mac that reported it, as agent_stop, pre-approved (the phone asked)",
      (r.status_code, r.json(), routed), (200, {"state": "stopped"}, [("agent_stop", {"id": RID}, "mac-key-1", True)]))
check("…and the run is stopped", c.get("/v1/agent-runs", headers=M).json()["runs"][0]["state"], "stopped")
check("stopping it again: 409 with its state", (lambda r: (r.status_code, r.json()))(c.post(f"/v1/agent-runs/{RID}/stop", headers=M)),
      (409, {"state": "stopped"}))
cases = [({"result": '{"state":"done"}'}, 409), ({"result": '{"error":"unknown"}'}, 404),
         ({"result": xd.NO_WAKE, "completed": False}, 503), ({"error": xd.NO_ANSWER}, 504),
         ({"error": "It failed on your Mac."}, 502), ({"result": "Sure, stopped it!"}, 502)]
codes = []
for i, (ans, _) in enumerate(cases):
    rid = f"6f1d2c39-6c8b-4b7b-9c55-2b0d4a0e01{i:02d}"
    c.post("/v1/agent-runs", json=report(id=rid), headers=M)
    answer = ans
    codes.append(c.post(f"/v1/agent-runs/{rid}/stop", headers=M).status_code)
check("the Mac's answers: done 409, unknown 404, no wake 503, no answer 504, failed 502, prose 502",
      codes, [code for _, code in cases])
check("the stop waits at least 15 s for the Mac", ar.STOP_TIMEOUT_S >= 15, True)
store.macs_for = lambda pid: []
c.post("/v1/agent-runs", json=report(id="6f1d2c39-6c8b-4b7b-9c55-2b0d4a0e0300"), headers=M)
check("no Mac titan can reach: 503", c.post("/v1/agent-runs/6f1d2c39-6c8b-4b7b-9c55-2b0d4a0e0300/stop", headers=M).status_code, 503)
xd.route = xd_route
check("nothing about runs is written under the test's state", sorted(p.name for p in _TMP.rglob("*agent*run*")), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
