#!/usr/bin/env python3
# gate: hermetic
"""Eval turns never write to a real device (10-05 audit): `jb ask` sends eval turns through the Mac's real chat, and
the Mac's trust tiers ran device writes without asking. On an eval turn a device tool whose risk isn't "read" is
simulated ("Done (eval: not sent)") unless the eval client answers device requests itself and says so
("eval_device": "fake": turn_eval, the SkillOpt harness), a pin honoured on eval turns only. Reads still go to the
device. Fail closed: an unknown or missing risk counts as a write. A person's turn is unchanged. Hermetic: a recording
fake device; nothing leaves the process.

Run: python3 scripts/test_eval_device_writes.py
"""
import os
import sys
import tempfile

os.environ.setdefault("AGENT_TURN_LOG", os.path.join(tempfile.mkdtemp(), "turns.jsonl"))
os.environ.setdefault("HOUSEHOLD_DB", os.path.join(tempfile.mkdtemp(), "household.db"))
os.environ.setdefault("AGENT_MEMORY_DIR", os.path.join(tempfile.mkdtemp(), "memory"))
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


sent = []


def device(req):
    sent.append(req["tool"])
    return {"result": "Done."}


def turn(source, pin=None):
    io = lib._TURN_IO
    io.device, io.client, io.role, io.device_tools, io.remote = device, "mac", "admin", None, None
    io.person = {"id": "p_alex", "name": "Alex"}
    io.source, io.eval_device = source, pin
    sent.clear()


def end():
    io = lib._TURN_IO
    io.device = io.client = io.role = io.device_tools = io.remote = io.person = io.source = io.eval_device = None


T = lib._DEVICE_TOOLS
non_read = sorted(n for n, s in T.items() if s.get("risk") != "read" and not s.get("internal"))
reads = sorted(n for n, s in T.items() if s.get("risk") == "read" and not s.get("internal"))
print("── the device tools ──")
check("there are device writes to guard", len(non_read) > 10, True)
check("send_message, run_shell, create_calendar_event are among them",
      all(t in non_read for t in ("send_message", "run_shell", "create_calendar_event")), True)

print("── a jb-ask-shaped eval turn: the real Mac paired, no pin ──")
turn("eval")
leaked, unsimulated = [], []
for name in non_read:
    out = lib._execute_tool(name, {"path": "~/Documents/x.txt", "title": "x", "text": "x", "command": "true"})
    if sent:
        leaked.append(name)
        sent.clear()
    if not out.startswith("Done (eval: not sent)"):
        unsimulated.append(name)
check("no device write reaches the device", leaked, [])
check("each is answered as simulated", unsimulated, [])
check("…and the direct device path is guarded too (every caller, not only _execute_tool)",
      [lib._device_request(n, {}) .startswith("Done (eval: not sent)") for n in ("send_message", "run_shell")]
      + [bool(sent)], [True, True, False])
lib._TURN_IO.device_wait = None
out = lib._device_request(reads[0], {})
check(f"a read still goes to the device ({reads[0]})", sent, [reads[0]])
print("── fail closed: an unknown risk counts as a write ──")
T["eval_probe_tool"] = {"risk": "sideways", "parameters": {}}
T["eval_probe_norisk"] = {"parameters": {}}
try:
    sent.clear()
    check("a risk that isn't 'read' is blocked", lib._eval_device_write_blocked("eval_probe_tool"), True)
    check("a missing risk is blocked", lib._eval_device_write_blocked("eval_probe_norisk"), True)
    check("…and never dispatched", [lib._device_request(n, {}).startswith("Done (eval: not sent)")
                                    for n in ("eval_probe_tool", "eval_probe_norisk")] + sent, [True, True])
    check("a tool missing from the table entirely is never dispatched",
          lib._device_request("eval_probe_unknown", {}).startswith("Done (eval: not sent)") and not sent, True)
finally:
    T.pop("eval_probe_tool", None)
    T.pop("eval_probe_norisk", None)
end()

print("── a turn_eval-shaped turn: the pin, answered by the eval's fake ──")
turn("eval", pin="fake")
out = lib._execute_tool("send_message", {"to": "Robin", "text": "hi"})
check("a write reaches the eval's own fake device", sent, ["send_message"])
check("not answered as simulated", out.startswith("Done (eval: not sent)"), False)
end()

print("── the pin is honoured on eval turns only ──")
turn("user", pin="fake")
check("a person's turn ignores a stray pin (and is never simulated)", lib._eval_device_write_blocked("send_message"), False)
end()
turn("canary")
check("a canary turn isn't an eval turn: unchanged", lib._eval_device_write_blocked("send_message"), False)
end()

print("── a person's turn is unchanged ──")
turn("user")
out = lib._execute_tool("send_message", {"to": "Robin", "text": "hi"})
check("their write goes to their device", sent, ["send_message"])
check("not simulated", out.startswith("Done (eval: not sent)"), False)
end()

print("── the API's pin and the eval clients ──")
here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "agent_api_server.py")).read()
i = src.find("def _parse_eval_device(")
ns: dict = {}
exec(compile(src[i:src.find("\n\n\n", i)], "_parse_eval_device", "exec"), ns)
p = ns["_parse_eval_device"]
check("eval + fake → fake", p({"eval_device": "fake"}, "eval"), "fake")
check("a person's turn never gets the pin", p({"eval_device": "fake"}, "user"), None)
check("no pin, or any other value → none (simulated)", [p({}, "eval"), p({"eval_device": "real"}, "eval")], [None, None])
check("both turn paths set it", src.count('_turns[turn_id]["eval_device"] = _parse_eval_device(body,'), 2)
check("the turn sets and resets io.eval_device", ("io.eval_device = (_turns" in src, "io.eval_device = None" in src),
      (True, True))
for f in ("turn_eval.py", os.path.join("skillopt", "harness.py")):
    check(f"{f} declares its fake device", '"eval_device": "fake"' in open(os.path.join(here, f)).read(), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
