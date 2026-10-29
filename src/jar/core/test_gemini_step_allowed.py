#!/usr/bin/env python3
# gate: hermetic   (Gemini faked at _gemini_call; tools recorded, nothing runs)
"""titan-node#1288 review: the Gemini native function-calling loop runs only the narrowed turn's own set, as the text
loops do. A task step on Gemini whose set holds `file_list` but no Mac file reads can't run `files` mode `contents`
(search_file_contents on the Mac) or any tool outside its set; titan's own `files` mode `titan` (file_list) still
runs. An unnarrowed turn is unchanged.
Run: python3 scripts/test_gemini_step_allowed.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-geministep-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "PROACTIVE_DB": "proactive.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


RAN: list[tuple[str, dict]] = []
SAW: list[str] = []
CALL = {"name": "files", "args": {"mode": "contents", "query": "lease renewal"}}


def fake_gemini(body, model=None, timeout=None):
    last = (body.get("contents") or [{}])[-1]
    resp = [p for p in last.get("parts") or [] if "functionResponse" in p]
    if resp:
        SAW.append(str(resp[0]["functionResponse"]["response"]["result"]))
        return {"candidates": [{"content": {"parts": [{"text": "Done."}]}}]}
    return {"candidates": [{"content": {"parts": [{"functionCall": dict(CALL)}]}}]}


lib._gemini_call = fake_gemini
real_exec = lib.execute_tool
lib.execute_tool = lambda name, args: RAN.append((name, dict(args))) or "STUB: ran"
lib._TURN_IO.role, lib._TURN_IO.person = "admin", {"id": "alex", "name": "Alex"}
MSG = [{"role": "user", "content": "Step 2: compare the quotes."}]


def run(call, allowed):
    RAN.clear(), SAW.clear()
    CALL.clear(), CALL.update(call)
    out = lib._run_gemini_agent(MSG, max_rounds=2, allowed_tools=allowed)
    return out, [n for n, _ in RAN], SAW[:1]


STEP = set(lib._SAFE_STEP_TOOLS)
check("(setup) the safe step set holds file_list and no Mac file reads",
      ("file_list" in STEP, bool(STEP & {"search_file_contents", "find_files"})), (True, False))
out, ran, saw = run({"name": "files", "args": {"mode": "contents", "query": "lease renewal"}}, STEP)
check("a task step names `files` mode `contents`: refused, nothing runs, Gemini is told it isn't available",
      (ran, saw), ([], ["Tool not available in this context."]))
out, ran, saw = run({"name": "files", "args": {"mode": "name", "query": "lease"}}, STEP)
check("…mode `name` (find_files) the same", (ran, saw), ([], ["Tool not available in this context."]))
out, ran, saw = run({"name": "search_file_contents", "args": {"query": "lease"}}, STEP)
check("…the old tool's own name, outside the set, the same", (ran, saw), ([], ["Tool not available in this context."]))
out, ran, saw = run({"name": "send_message", "args": {"to": "Robin", "text": "hi"}}, STEP)
check("…any tool outside the set (send_message) the same", (ran, saw), ([], ["Tool not available in this context."]))
out, ran, saw = run({"name": "files", "args": {"mode": "titan", "path": "/ai/tmp"}}, STEP)
check("`files` mode `titan` (file_list, in the set) runs", ran, ["files"])
out, ran, saw = run({"name": "kb_retrieve", "args": {"query": "quotes"}}, STEP)
check("a tool in the set runs", ran, ["kb_retrieve"])
out, ran, saw = run({"name": "files", "args": {"mode": "contents", "query": "lease"}}, None)
check("an unnarrowed turn (no allowed set): unchanged, it runs (role and device checks are execute_tool's)",
      ran, ["files"])

lib.execute_tool = real_exec
lib._TURN_IO.role = lib._TURN_IO.person = None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
