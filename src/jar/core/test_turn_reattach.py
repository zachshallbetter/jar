#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A dropped stream isn't a Stop (jellybean#369): Zach's 14:12 and 14:52 duplicates were a phone's resend of a
turn titan had marked cancelled but ran anyway.
  - POST /v1/turns/{id}/cancel is the one real cancel: owner only, nothing more runs (a waiting device request is
    released), nothing is remembered;
  - a dropped iPhone stream keeps the turn running, and GET /v1/turns/{id} answers done with the reply;
  - a Mac that says so (client "mac", jellybean#392) runs on too; an older client's disconnect is still its Stop,
    now really stopping; a "mac" turn routes its device tools exactly like a client-less one;
  - client_msg_id re-attaches the same person's turn within 10 minutes (running → streamed, done → done again);
  - the turn store is bounded (10-minute TTL, a cap).
Fake turns, temp stores; no model, no device. Run: python3 scripts/test_turn_reattach.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-reattach-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "TODOS_DB": "todos.db",
             "TASK_LOG_DB": "tasklog.db", "CONTINUITY_DB": "continuity.db", "PUSH_DB": "push.db",
             "PROACTIVE_DB": "proactive.db", "ROUTINES_DB": "routines.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json",
             "AGENT_TURN_LOG": "turns.jsonl", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db",
             "SCHEDULED_DB": "scheduled.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("TITAN_DISCOVERY", None)
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(body, name):
    s = c.post("/v1/pair/start", json=body, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": name, "platform": "ios"}).json()["token"]
    return {"X-Agent-Token": tok}, s["person"]["id"]


ZP, zach = pair({"person_id": hh.owner_identity().person_id}, "Zach's iPhone")
MIR, mid = pair({"new_person": {"name": "Robin", "role": "adult"}}, "Robin's iPhone")

# ── a controllable fake turn ──────────────────────────────────────────────────────────────────────
gate = threading.Event()
calls: list[str] = []
after: list[str] = []      # what a tool call returned after the gate opened
remembered: list[str] = []
mode = {"device": False}


def fake_turn(messages, *a, **k):
    calls.append("turn")
    io = api._lib._TURN_IO
    if io.status:
        io.status("Working…")
    if mode["device"] and io.device:
        after.append(str(api._lib._device_request("list_reminders", {})))
    gate.wait(5)
    after.append(api._lib.execute_tool("get_datetime", {}))
    return "The answer."


api._lib._run_agent_turn = fake_turn
api._remember_turn = lambda tid, ident, q, reply, **k: remembered.append(reply)


def new_turn(ident, client=None, cmid=None):
    tid = f"t-{len(api._turns)}-{time.time_ns()}"
    api._init_turn(tid)
    api._turns[tid].update({"identity": ident, "client": client, "client_msg_id": cmid, "source": "user"})
    return tid


def wait_state(tid, states, s=5.0):
    end = time.time() + s
    while time.time() < end:
        if (api._turns.get(tid) or {}).get("state") in states:
            break
        time.sleep(0.02)
    return (api._turns.get(tid) or {}).get("state")


async def drop_after_first(tid, device=False):
    """The stream sends its first event, then the client goes away (aclose: GeneratorExit)."""
    gen = api._sse_gen(tid, "q", [], True, None, device)
    first = await gen.__anext__()
    await gen.aclose()
    return first


def reset():
    gate.clear()
    calls.clear()
    after.clear()
    remembered.clear()
    mode["device"] = False


owner = hh.owner_identity()

print("── a dropped iPhone stream keeps running ──")
reset()
tid = new_turn(owner, client="iphone")
loop = asyncio.new_event_loop()
first = loop.run_until_complete(drop_after_first(tid))
check("the stream had started (a status went out)", first.startswith("event: status"), True)
check("the drop doesn't cancel a phone's turn", api._turns[tid]["state"], "running")
gate.set()
check("…it finishes", wait_state(tid, ("done",)), "done")
got = c.get(f"/v1/turns/{tid}", headers=M).json()
check("GET /v1/turns answers done with the reply, sources and blocks", (got["state"], got["text"], "sources" in got,
      "blocks" in got, "egress" in got), ("done", "The answer.", True, True, True))
check("…remembered once, and its tools ran", (remembered, after[-1].startswith("Not run")), (["The answer."], False))
check("another person can't read it", c.get(f"/v1/turns/{tid}", headers=MIR).status_code, 404)

print("── a client-less (older Mac) disconnect is still its Stop, and now it really stops ──")
reset()
tid = new_turn(owner, client=None)
loop.run_until_complete(drop_after_first(tid))
check("cancelled on the drop", api._turns[tid]["state"], "cancelled")
gate.set()
time.sleep(0.3)
check("…a late result doesn't make it done", wait_state(tid, ("done",), 0.5), "cancelled")
check("…no tool ran after the Stop, nothing remembered", (after[-1].startswith("Not run: the user stopped"), remembered),
      (True, []))

print("── a Mac that says client \"mac\" (jellybean#392) ──")
check("_parse_client keeps \"mac\" and \"iphone\"; anything else is None",
      [api._parse_client({"client": c}) for c in ("mac", "MAC ", "iphone", "watch", None)], ["mac", "mac", "iphone", None, None])
reset()
tid = new_turn(owner, client="mac")
loop.run_until_complete(drop_after_first(tid))
check("a dropped \"mac\" stream runs on", api._turns[tid]["state"], "running")
gate.set()
check("…to done, remembered once", (wait_state(tid, ("done",)), remembered), ("done", ["The answer."]))
lib = api._lib
for tools in (None, ["read_file", "get_screen"]):
    check(f"\"mac\" gets the same device tools as no client (reported {tools})",
          lib.device_tools_for_client("mac", tools), lib.device_tools_for_client(None, tools))
check("…no phone-to-Mac remote, either way", (api._remote_for_turn(owner, "mac"), api._eval_remote(owner, "mac", "live")),
      (api._remote_for_turn(owner, None), api._eval_remote(owner, None, "live")))
for c_ in ("mac", None):
    lib._TURN_IO.client = c_
    check(f"_turn_client for {c_!r} is the Mac", lib._turn_client(), "mac")
lib._TURN_IO.client = None
check("the Mac's status line is the same", api._mac_status_line(owner, "mac", None, None) == api._mac_status_line(owner, None, None, None), True)

print("── POST /v1/turns/{id}/cancel ──")
reset()
tid = new_turn(owner, client="iphone")
th = threading.Thread(target=lambda: loop.run_until_complete(drop_after_first(tid)))
th.start()
th.join(3)
check("someone else's cancel: 404, and it runs on", (c.post(f"/v1/turns/{tid}/cancel", headers=MIR).status_code,
      api._turns[tid]["state"]), (404, "running"))
check("an unknown turn: 404", c.post("/v1/turns/nope/cancel", headers=ZP).status_code, 404)
check("the owner's cancel: 204, cancelled", (c.post(f"/v1/turns/{tid}/cancel", headers=M).status_code,
      api._turns[tid]["state"]), (204, "cancelled"))
gate.set()
time.sleep(0.3)
check("…nothing more runs, nothing is remembered, it stays cancelled", (after[-1].startswith("Not run: the user stopped"),
      remembered, api._turns[tid]["state"]), (True, [], "cancelled"))
check("a finished turn's cancel: 204, left as it is", c.post(f"/v1/turns/{tid}/cancel", headers=M).status_code, 204)

reset()
mode["device"] = True
tid = new_turn(owner, client="iphone")
gen_box = {}


def run_with_device():
    async def go():
        gen = api._sse_gen(tid, "q", [], True, None, True)
        async for chunk in gen:
            if chunk.startswith("event: device"):
                gen_box["device"] = chunk
                await gen.aclose()
                return
    loop2 = asyncio.new_event_loop()
    loop2.run_until_complete(go())


th = threading.Thread(target=run_with_device)
th.start()
th.join(5)
check("a device request went out", "device" in gen_box, True)
t0 = time.time()
c.post(f"/v1/turns/{tid}/cancel", headers=M)
gate.set()
check("Stop releases the waiting device request at once", wait_state(tid, ("cancelled",)) == "cancelled" and
      bool(after) and after[0].startswith("Device action was not completed: Stopped by the user"), True)
check("…well inside the 120 s device wait", time.time() - t0 < 5, True)

print("── client_msg_id re-attaches ──")
reset()
gate.set()


def stream(headers, cmid, client="iphone"):
    with c.stream("POST", "/v1/prompt/stream", headers=headers,
                  json={"prompt": "What's on today?", "client": client, "client_msg_id": cmid}) as r:
        body = "".join(r.iter_text())
        return r.headers.get("x-turn-id"), body


t1, b1 = stream(ZP, "cm-1")
check("the first send runs a turn to done", ("event: done" in b1, calls.count("turn")), (True, 1))
t2, b2 = stream(ZP, "cm-1")
check("the same id again: the same turn's done, nothing new runs", (t2 == t1, "event: done" in b2, calls.count("turn")),
      (True, True, 1))
check("…its answer again", json.loads(b2.split("event: done\ndata: ")[1].split("\n")[0])["text"], "The answer.")
t3, _ = stream(MIR, "cm-1")
check("another person's same id: their own turn", (t3 != t1, calls.count("turn")), (True, 2))
t4, _ = stream(ZP, "cm-2")
check("a new id: a new turn", (t4 not in (t1, t2), calls.count("turn")), (True, 3))
api._turns[t1]["state"] = "cancelled"
t5, _ = stream(ZP, "cm-1")
check("after a cancel the same id runs as new", (t5 != t1, calls.count("turn")), (True, 4))
api._turns[t5]["created_at"] -= api.TURN_KEEP_S + 1
t6, _ = stream(ZP, "cm-1")
check("after 10 minutes it runs as new", (t6 != t5, calls.count("turn")), (True, 5))

# Running: the resend streams the running turn to its end.
reset()
tid = new_turn(owner, client="iphone", cmid="cm-run")
api._turns[tid]["status"] = ["Working…"]
threading.Timer(0.4, lambda: api._finish(tid, "done", text="Finished meanwhile.")).start()
t7, b7 = stream(ZP, "cm-run")
check("a running turn: re-attached, streamed to its done", (t7, "Working" in b7, "Finished meanwhile." in b7, calls),
      (tid, True, True, []))

print("── the store is bounded ──")
now = time.time()
for i in range(30):
    api._turns[f"old-{i}"] = {"state": "done", "created_at": now - 2000, "finished_at": now - 1000}
api._turns["run-x"] = {"state": "running", "created_at": now - 30}
with api._turns_lock:
    api._prune_turns(now)
check("finished turns past 10 minutes go; a running one stays",
      (any(k.startswith("old-") for k in api._turns), "run-x" in api._turns), (False, True))
cap = api.TURN_CAP
api.TURN_CAP = 5
for i in range(12):
    api._turns[f"fin-{i}"] = {"state": "done", "created_at": now, "finished_at": now + i}   # the newest
with api._turns_lock:
    api._prune_turns(now)
finished = [k for k, v in api._turns.items() if v.get("state") in api._FINISHED]
check("past the cap the oldest finished go first, never the running",
      (len(api._turns) <= 5 or not finished, "fin-11" in api._turns, "fin-0" in api._turns, "run-x" in api._turns),
      (True, True, False, True))
api.TURN_CAP = cap
KID, _ = pair({"new_person": {"name": "Ava", "role": "kid"}}, "Ava's iPhone")
check("the cancel route is open to every person's token (a kid's unknown turn: 404, not 403)",
      c.post("/v1/turns/nope/cancel", headers=KID).status_code, 404)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
