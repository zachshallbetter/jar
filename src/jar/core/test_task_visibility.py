#!/usr/bin/env python3
# gate: hermetic   (temp household store; the workflow runtime is a fake)
"""#1239 (Zach 10-08): own + shared + kids. One reader policy, task_visible, for every task surface: tasks_for (Now,
task cards, item links) and the task tools by id. Views x viewers: the admin, two adults, a kid and a shared screen,
over tasks that are someone's own, a kid's, shared with one person, the adults', and the household's. The admin sees
exactly what any adult would. Fictional people.
Run: python3 scripts/test_task_visibility.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-taskvis-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "TASK_LOG_DB": "task_log.db", "TASKS_DB": "tasks.db",
             "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import household_identity as hh  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


store = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(store)
ADMIN = hh.ADMIN_PERSON_ID
robin = store.add_person("Robin", "adult")["id"]
jordan = store.add_person("Jordan", "adult")["id"]
ava = store.add_person("Ava", "kid")["id"]   # Robin's
leo = store.add_person("Leo", "kid")["id"]   # the admin's
store.update_person(ava, parents=[robin])
store.update_person(leo, parents=[ADMIN])


def inst(tid, owner, audience=None):
    md = {"owner": owner, **({"audience": audience} if audience is not None else {})}
    return {"instance_id": tid, "definition_id": "d", "status": "running", "current_step_key": "s0",
            "input": {"goal": tid}, "metadata": md, "updated_at": "2026-10-08T07:00:00Z"}


INSTANCES = [inst("t-admin", ADMIN), inst("t-robin", robin), inst("t-jordan", jordan), inst("t-ava", ava),
             inst("t-leo", leo), inst("t-to-robin", jordan, [robin]), inst("t-adults", jordan, "adults"),
             inst("t-house", jordan, "household"), {**inst("t-legacy", ADMIN), "metadata": {}}]
BY_ID = {i["instance_id"]: i for i in INSTANCES}


def fake_runtime(runtime, path, method, body=None):
    if path == "/v1/instances":
        return {"instances": [dict(i) for i in INSTANCES]}
    if path.startswith("/v1/instances/"):
        return dict(BY_ID.get(path.rsplit("/", 1)[1]) or {})
    if path.startswith("/v1/definitions/"):
        return {"steps": [{"step_key": "s0", "title": "Start"}]}
    raise AssertionError(path)


lib._runtime_request = fake_runtime
VIEWERS = {"admin": (ADMIN, "admin"), "robin": (robin, "adult"), "jordan": (jordan, "adult"), "ava": (ava, "kid"),
           "screen": (None, "household")}
EXPECT = {
    "admin": {"t-admin", "t-leo", "t-adults", "t-house", "t-legacy"},
    "robin": {"t-robin", "t-ava", "t-to-robin", "t-adults", "t-house"},
    "jordan": {"t-jordan", "t-to-robin", "t-adults", "t-house"},
    "ava": {"t-ava", "t-house"},
    "screen": set(),
}

print("── views x viewers: tasks_for (Now, task cards, item links) ──")
got = {v: {t["id"] for t in lib.tasks_for(pid, role)["tasks"]} for v, (pid, role) in VIEWERS.items()}
check("each viewer sees own + shared with them or the household + (an adult's or the admin's) own kids'", got, EXPECT)
check("the admin sees exactly what an adult would: never another adult's private task (Robin's, Jordan's) or another "
      "parent's kid's (Ava's)", sorted(got["admin"] & {"t-robin", "t-jordan", "t-ava", "t-to-robin"}), [])
check("a kid: never a parent's or a sibling's private task, nor the adults'", sorted(got["ava"] - {"t-ava", "t-house"}), [])
check("a shared screen: none here (household plans are household_plans')", got["screen"], set())
check("a task from before owners were kept is the admin's, and only the admin's",
      {v: "t-legacy" in s for v, s in got.items()}, {"admin": True, "robin": False, "jordan": False, "ava": False,
                                                     "screen": False})

print("── views x viewers: the task tools by id ──")
io = lib._TURN_IO


def tool(viewer, tid, name="task_status", background=False):
    pid, role = VIEWERS[viewer]
    io.person = {"id": pid, "name": viewer} if pid else None
    io.role = role
    io.jev_source = "proactive" if background else None
    try:
        return lib._task_tool_hidden(name, {"task_id": tid})
    finally:
        io.person = io.role = io.jev_source = None


check("the admin asking about Robin's private task by id: no such task (its existence isn't told either)",
      tool("admin", "t-robin"), "Error: no task t-robin.")
check("…every task tool the same", {n: tool("admin", "t-robin", n) for n in sorted(lib.TASK_ID_TOOLS)},
      {n: "Error: no task t-robin." for n in sorted(lib.TASK_ID_TOOLS)})
check("the admin's own, their kid's, the household's: allowed",
      [tool("admin", t) for t in ("t-admin", "t-leo", "t-house")], [None, None, None])
check("for every viewer, a tool allows exactly the tasks tasks_for shows",
      {v: {t for t in BY_ID if tool(v, t) is None} for v in VIEWERS}, EXPECT)
check("a background turn (the task runner, autonomy) acts for the task's owner: not held",
      tool("admin", "t-robin", background=True), None)
check("an unknown id or no id: left to the tool's own error",
      (tool("admin", "t-nope"), lib._task_tool_hidden("task_status", {})), (None, None))
check("other tools: never held", lib._task_tool_hidden("list_todos", {"task_id": "t-robin"}), None)
src = (Path(__file__).resolve().parent / "agent_runtime_lib.py").read_text()
check("the chokepoint runs in _execute_tool_checked, and tasks_for reads through task_visible",
      ("hidden_task = _task_tool_hidden(name, args)" in src,
       "instances = [i for i in instances if task_visible(i, viewer, role)]" in src), (True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
