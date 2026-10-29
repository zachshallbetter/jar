#!/usr/bin/env python3
"""jellybean#228, titan half: GET /v1/tasks is per viewer (their own; the admin the same, #1239; none for a household
device), and each task says who owns it, when it last moved, its last logged step (never the step's note) and how
it advances on its own. Hermetic: the workflow runtime is faked; task_log and scenarios use temp stores.
# gate: hermetic
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
tmp = tempfile.mkdtemp()
os.environ["HOUSEHOLD_DB"] = str(Path(tmp) / "household.db")   # task_visible's parents lookup: a temp household
os.environ["TASK_LOG_DB"] = str(Path(tmp) / "task_log.db")
os.environ["TASKS_DB"] = str(Path(tmp) / "tasks.db")
import agent_runtime_lib as lib  # noqa: E402
import continuity  # noqa: E402
import scenarios as sc  # noqa: E402
import task_log  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


STEPS = [{"step_key": f"s{i}", "title": t} for i, t in enumerate(["Find quotes", "Compare", "Book"])]
INSTANCES = [
    {"instance_id": "task-z1", "definition_id": "d", "status": "running", "current_step_key": "s1",
     "input": {"goal": "Fix the fence"}, "metadata": {"owner": "zach"}, "updated_at": "2026-10-01T10:00:00Z"},
    {"instance_id": "task-m1", "definition_id": "d", "status": "running", "current_step_key": "s0",
     "input": {"goal": "Plan the party"}, "metadata": {"owner": "robin"}, "updated_at": "2026-10-01T11:00:00Z"},
    {"instance_id": "task-old", "definition_id": "d", "status": "completed", "current_step_key": "",
     "input": {"goal": "Before owners were recorded"}, "updated_at": "2026-09-28T09:00:00Z"},
]


def fake_runtime(runtime: str, path: str, method: str, body=None):
    if path == "/v1/instances":
        return {"instances": [dict(i) for i in INSTANCES]}
    if path.startswith("/v1/definitions/"):
        return {"steps": STEPS}
    raise AssertionError(path)


lib._runtime_request = fake_runtime
continuity.emit = lambda *a, **k: None   # task_log.record's event: not under test here

now = time.time()
task_log.record("task-z1", "Fix the fence", "Find quotes", "done", "Called Bob at 555-0100", "zach")
task_log.record("task-m1", "Plan the party", "Find quotes", "blocked", "Which date?", "robin")

print("who sees what")
ids = lambda out: [t["id"] for t in out["tasks"]]  # noqa: E731
mine = lib.tasks_for("robin", "adult")
check("an adult sees only their own", ids(mine) == ["task-m1"], str(ids(mine)))
check("a kid with none sees none", lib.tasks_for("leo", "kid")["tasks"] == [])
check("a household device sees none", lib.tasks_for(None, "household")["tasks"] == [])
everyone = lib.tasks_for("zach", "admin")
check("the admin sees their own (#1239: never another adult's private task), running first",
      ids(everyone) == ["task-z1", "task-old"], str(ids(everyone)))
check("an instance with no recorded owner is Zach's", [t["owner"] for t in everyone["tasks"] if t["id"] == "task-old"]
      == ["zach"])
check("…so an adult never sees it", "task-old" not in ids(lib.tasks_for("jordan", "adult")))
robins = lib.tasks_for("robin", "adult")["tasks"]   # fields of an adult's task, read by its owner

print("fields")
z = next(t for t in everyone["tasks"] if t["id"] == "task-z1")
m = next(t for t in robins if t["id"] == "task-m1")
check("owner", z["owner"] == "zach" and m["owner"] == "robin")
check("last_step: title, state, at", z.get("last_step", {}).get("title") == "Find quotes"
      and z["last_step"]["state"] == "done" and z["last_step"]["at"].endswith("Z"), str(z.get("last_step")))
check("moved_at is the newest log line", z["moved_at"] == z["last_step"]["at"])
check("the step's note never goes out", "555-0100" not in json.dumps(everyone) and "Which date" not in json.dumps(robins))
check("steps: done, current, pending", [s["state"] for s in z["steps"]] == ["done", "current", "pending"],
      str(z["steps"]))
check("a blocked current step is marked blocked", [s["state"] for s in m["steps"]] == ["blocked", "pending", "pending"],
      str(m["steps"]))
old = next(t for t in everyone["tasks"] if t["id"] == "task-old")
check("never logged: moved_at falls back to updated_at, no last_step", old["moved_at"] == old["updated_at"]
      and "last_step" not in old)
check("never opted in: no autonomy", "autonomy" not in z and "autonomy" not in old)

print("advancing on its own")
sc.create("task-z1", "zach", "Fix the fence", cadence="daily", now=now)
sc.update("task-z1", next_due=now + 3600)
a = next(t for t in lib.tasks_for("zach", "admin")["tasks"] if t["id"] == "task-z1").get("autonomy") or {}
check("on, daily, next_at, not halted", a.get("on") is True and a.get("cadence") == "daily" and a.get("next_at")
      and a.get("halted") is False and a.get("reason") is None, str(a))
env = sc.envelope(sc.get("task-z1"), "next", 1, grant_digest=None, changes=[], now=now)
sc.record(env, sc.halt("dependency_changed", [], [], "zach"))
sc.update("task-z1", state="waiting")
a = next(t for t in lib.tasks_for("zach", "admin")["tasks"] if t["id"] == "task-z1")["autonomy"]
check("waiting: halted, the reason in words, no next_at", a["halted"] is True and a["next_at"] is None
      and a["reason"] == "Something it depends on changed.", str(a))
sc.update("task-z1", state="stopped", next_due=None)
a = next(t for t in lib.tasks_for("zach", "admin")["tasks"] if t["id"] == "task-z1")["autonomy"]
check("stopped: off, halted, a reason", a["on"] is False and a["halted"] is True and a["reason"], str(a))
sc.update("task-z1", state="active", cadence="off")
a = next(t for t in lib.tasks_for("zach", "admin")["tasks"] if t["id"] == "task-z1")["autonomy"]
check("its schedule stopped (cadence off): not on, no cadence", a["on"] is False and a["cadence"] is None, str(a))

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed" + (f" ({', '.join(failures)})" if failures else ""))
sys.exit(1 if failures else 0)
