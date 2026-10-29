#!/usr/bin/env python3
# gate: hermetic   (a fake workflow runtime and temp stores; nothing live)
"""jellybean#429: a plan an adult makes for the household shows on its screens. plan_task's for_household (adults
only) marks it; GET /v1/home/plans lists only those, with goal and step text redacted and capped, a fixed-phrase
`waiting`, the maker's first name, most recent first. A person's own tasks never appear.
Run: python3 scripts/test_household_plans.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update({"HOUSEHOLD_DB": str(Path(tmp) / "household.db"), "TASK_LOG_DB": str(Path(tmp) / "task_log.db"),
                   "TASKS_DB": str(Path(tmp) / "tasks.db"), "AGENT_TURN_SOURCE": "test",
                   "AGENT_TZ": "America/Los_Angeles"})
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import continuity  # noqa: E402
import household_identity as hh  # noqa: E402
import task_log  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


st = hh.get_store()
alex = st.add_person("Alex Rivera", "adult")["id"]
ava = st.add_person("Ava Rivera", "kid")["id"]

STEPS = {"d-lake": [{"step_key": "s0", "title": "Check Saturday's weather"}, {"step_key": "s1", "title": "Pick a beach"},
                    {"step_key": "s2", "title": "Pack the cooler; the lot access code 48213"}, {"step_key": "s3", "title": "Leave by 9"}],
         "d-own": [{"step_key": "s0", "title": "Private errand"}],
         "d-party": [{"step_key": "s0", "title": "Pick a date"}, {"step_key": "s1", "title": "Invite people"}]}
INSTANCES = [
    {"instance_id": "task-lake", "definition_id": "d-lake", "status": "running", "current_step_key": "s2",
     "input": {"goal": "Saturday at the lake"}, "metadata": {"owner": alex, "audience": "household"},
     "updated_at": "2026-10-03T18:00:00Z"},
    {"instance_id": "task-own", "definition_id": "d-own", "status": "running", "current_step_key": "s0",
     "input": {"goal": "My own errand"}, "metadata": {"owner": alex}, "updated_at": "2026-10-03T19:00:00Z"},
    {"instance_id": "task-party", "definition_id": "d-party", "status": "running", "current_step_key": "s0",
     "input": {"goal": "Ava's birthday party"}, "metadata": {"owner": alex, "audience": "household"},
     "updated_at": "2026-10-03T10:00:00Z"},
]
created: list[dict] = []


def fake_runtime(runtime, path, method, body=None):
    if path == "/v1/instances" and method == "GET":
        return {"instances": [dict(i) for i in INSTANCES]}
    if path.startswith("/v1/definitions/") and method == "GET":
        return {"steps": STEPS.get(path.rsplit("/", 1)[1], [])}
    if path == "/v1/definitions" and method == "POST":
        return {"definition_id": "d-new"}
    if path == "/v1/instances" and method == "POST":
        created.append(body)
        return {"instance_id": body["instance_id"]}
    return {}


lib._runtime_request = fake_runtime
lib._next_step_at = lambda tid, now: None
continuity.emit = lambda *a, **k: None
_real_time = task_log.time.time
task_log.time.time = lambda: 1_790_000_000.0          # the lake's step first…
task_log.record("task-lake", "Saturday at the lake", "Pick a beach", "done", "", alex)
task_log.time.time = lambda: 1_790_000_600.0          # …the party's ten minutes later
task_log.record("task-party", "Ava's birthday party", "Pick a date", "blocked", "Which Saturday works for Grandma?", alex)
task_log.time.time = _real_time

print("── the list ──")
out = lib.household_plans()
ids = [p["id"] for p in out["plans"]]
check("only household plans, never a person's own", ids.count("task-own"), 0)
check("most recent activity first (the party's step was logged last)", ids, ["task-party", "task-lake"])
lake = next(p for p in out["plans"] if p["id"] == "task-lake")
check("goal, maker's first name, progress", (lake["goal"], lake["by"], lake["progress"]),
      ("Saturday at the lake", "Alex", {"done": 2, "total": 4}))
check("step states", [s["state"] for s in lake["steps"]], ["done", "done", "current", "todo"])
check("step text redacted (an access code) and capped", ("48213" in lake["steps"][2]["title"],
      all(len(s["title"]) <= lib.PLAN_STEP_CHARS for s in lake["steps"])), (False, True))
party = next(p for p in out["plans"] if p["id"] == "task-party")
check("waiting: a fixed phrase, never the step's note", (party["steps"][0]["state"], party["steps"][0]["waiting"],
      "Grandma" in str(party)), ("waiting", "Alex to confirm", False))
check("updated is ISO with the household's offset", lake["updated"].endswith(("-07:00", "-08:00")), True)

print("── making one ──")
alex_admin = st.add_person("Robin Rivera", "admin")["id"]
lib._TURN_IO.person, lib._TURN_IO.role = {"id": alex_admin, "name": "Robin"}, "admin"
said = lib.execute_tool("plan_task", {"goal": "Plan Saturday", "steps": "Weather; Pick a park", "for_household": "true"})
check("the admin's plan for the household", (created[-1]["metadata"].get("audience"), "for the household" in said),
      ("household", True))
said = lib.execute_tool("plan_task", {"goal": "My errand", "steps": "Bank; Post office"})
check("without for_household it stays theirs", ("audience" in created[-1]["metadata"], "for the household" in said),
      (False, False))
n = len(created)
lib._TURN_IO.person, lib._TURN_IO.role = {"id": ava, "name": "Ava"}, "kid"
said = lib.execute_tool("plan_task", {"goal": "My fort", "steps": "Blankets; Chairs", "for_household": "true"})
check("anyone else can't plan (plan_task is admin-only)", (len(created), said), (n, lib._ROLE_REFUSAL))
# 10-03 audit (#802's shape): planning the same goal again left the old task open beside the new one.
lib._TURN_IO.person, lib._TURN_IO.role = {"id": alex, "name": "Alex"}, "admin"
n = len(created)
said = lib.execute_tool("plan_task", {"goal": "Saturday at the lake, again", "steps": "Weather; Beach"})
check("a similar goal still open: not planned, naming it", (said.startswith("Not planned: “Saturday at the lake” (task-lake) "
      "is still open"), len(created)), (True, n))
said = lib.execute_tool("plan_task", {"goal": "Saturday at the lake", "steps": "Weather; Beach", "new": "yes"})
check("new='yes' plans a separate one", (said.startswith("Started task"), len(created)), (True, n + 1))
said = lib.execute_tool("plan_task", {"goal": "My own errand", "steps": "Bank; Post office"})
check("their own private task counts too", said.startswith("Not planned: “My own errand”"),
      True)
lib._TURN_IO.person = lib._TURN_IO.role = None

print("── the API ──")
src = (Path(__file__).with_name("agent_api_server.py")).read_text()
check("GET /v1/home/plans serves household_plans", ('@app.get("/v1/home/plans")' in src, "_lib.household_plans" in src),
      (True, True))
lib._runtime_request = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
check("the runtime down: an empty list, never an error", lib.household_plans(), {"plans": []})

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
