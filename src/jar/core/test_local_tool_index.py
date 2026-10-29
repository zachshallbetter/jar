#!/usr/bin/env python3
# gate: hermetic   (temp stores; builds prompts only; the llama-server calls are stubbed)
"""The local model's short tool list (#403). Measured 10-01: the admin's Mac and iPhone prompts were 16.4k
and 16.8k tokens before the turn, over the 16,384 window, so every local device turn failed with HTTP 400.
The local model now sees full entries only for LOCAL_FULL_TOOLS, one line for the rest, and tool_help for
details. Gemini's prompt must not change at all. Hermetic.

Run: python3 scripts/test_local_tool_index.py
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-toolindex-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


try:
    import agent_api_server as api
    import household_identity as hh
except Exception as exc:  # noqa: BLE001
    print(f"  [SKIP] agent_api_server not importable here: {exc}\n0 passed, 0 failed")
    sys.exit(0)

admin = hh.owner_identity()
kid = hh.Identity({"id": "k", "name": "Kid", "role": "kid"}, None, "kid", "device")


def first_of(ident, client, device):
    return [m for m in api.turn_prefix_messages(ident, client=client, device=device) if m["role"] == "system"][:1]


def gemini_system(msgs):
    """What _run_gemini_agent sends as system_instruction (before the secret shield)."""
    return lib._prompt_for_native_tools("\n\n".join(m["content"] for m in msgs if m["role"] == "system").strip())


print("\n── Gemini's prompt is byte-identical with the short local list on and off ──")
real_compact = lib._compact_tool_entries
for label, client, device in (("chat", None, False), ("mac", None, True), ("iphone", "iphone", True)):
    msgs = api.turn_prefix_messages(admin, client=client, device=device)
    with_index = gemini_system(msgs)
    lib._compact_tool_entries = lambda text: text
    try:
        without = gemini_system(api.turn_prefix_messages(admin, client=client, device=device))
    finally:
        lib._compact_tool_entries = real_compact
    check(f"{label}: Gemini system text identical", with_index == without, True)
    check(f"{label}: … and it carries no one-line index note", lib._TOOL_INDEX_NOTE in with_index, False)

print("\n── the local prefix: full entries for the core tools, one line for the rest ──")
first = first_of(admin, "iphone", True)
full = lib._prompt_for_text_tools(first[0]["content"])
local = lib._local_messages(first + [{"role": "user", "content": "x"}])[0]["content"]
entries_full = dict(lib._TOOL_ENTRY_RE.findall(full))
check("the full Mac prompt has 100+ tool entries (this store has no Mac, so the iPhone's has fewer)",
      len(lib._TOOL_ENTRY_RE.findall(lib._prompt_for_text_tools(first_of(admin, None, True)[0]["content"]))) > 100, True)
core_in = [n for n in entries_full if n in lib.LOCAL_FULL_TOOLS]
check("every core tool in this shape keeps its full entry with params",
      [n for n in core_in if f"- **{n}**: {entries_full[n]}\n  params:" not in local], [])
others = [n for n in entries_full if n not in lib.LOCAL_FULL_TOOLS]
check("every other tool is one line, no params",
      [n for n in others if f"- **{n}**: " not in local or f"- **{n}**: {entries_full[n]}\n  params:" in local], [])
check("tool_help and skill_guide are full", all(f"- **{n}**: {entries_full[n]}\n  params:" in local
                                                for n in ("tool_help", "skill_guide")), True)
check("the note says to call tool_help first", lib._TOOL_INDEX_NOTE in local, True)
shrink = 1 - len(local) / (len(full) + len(lib._LOCAL_FORMAT_HINT) + 2)
check(f"the admin iPhone local prefix is at least 25% smaller (it is {shrink:.0%})", shrink >= 0.25, True)
check("deterministic: the same shape builds the same prefix",
      local == lib._local_messages(first_of(admin, "iphone", True) + [{"role": "user", "content": "x"}])[0]["content"], True)
check("idempotent", lib._compact_tool_entries(lib._compact_tool_entries(full)) == lib._compact_tool_entries(full), True)
check("a prompt without tool_help is left alone", lib._compact_tool_entries("- **a_tool**: Does a thing.\n  params: x: y"),
      "- **a_tool**: Does a thing.\n  params: x: y")
check("every core-list name is a real tool (a rename fails here)",
      sorted(n for n in lib.LOCAL_FULL_TOOLS if n not in lib._TOOL_REGISTRY and n not in lib._DEVICE_TOOLS), [])
check("one line: first sentence, at most 14 words",
      lib._one_line("Send a text message to a contact through Messages on the Mac. Always confirm first."),
      "Send a text message to a contact through Messages on the Mac.")

print("\n── tool_help: the full entry, only for tools this person may use ──")
io = lib._TURN_IO
lib._TURN_INFO.info = {}
try:
    io.role, io.client = "admin", None
    io.device = lambda req: {"error": "test"}
    io.device_tools = lib.device_tools_for_client(None, None)
    mac_full = lib._prompt_for_text_tools(first_of(admin, None, True)[0]["content"])
    io.role, io.client, io.device = "admin", None, (lambda req: {"error": "test"})
    io.device_tools = lib.device_tools_for_client(None, None)
    got = lib.execute_tool("tool_help", {"name": "send_message"})
    check("a device tool's entry, exactly as the full prompt shows it", got in mac_full and got.startswith("- **send_message**:"), True)
    got = lib.execute_tool("tool_help", {"name": "Jev_Decide"})
    check("a registry tool, name case-insensitive", got.startswith("- **jev_decide**:") and "\n  params: " in got, True)
    check("unknown name", lib.execute_tool("tool_help", {"name": "teleport"}), "No tool named 'teleport' is available here.")
    check("names looked up are recorded once each", lib._TURN_INFO.info.get("tools_looked_up"), ["send_message", "jev_decide"])
    io.role = "kid"
    check("a kid never gets an admin tool's entry", lib.execute_tool("tool_help", {"name": "run_shell"}),
          "No tool named 'run_shell' is available here.")
    check("… nor an adult-only one", lib.execute_tool("tool_help", {"name": "send_message"}),
          "No tool named 'send_message' is available here.")
    check("… but gets their own", lib.execute_tool("tool_help", {"name": "get_weather"}).startswith("- **get_weather**:"), True)
    check("every role may call tool_help", all(hh.role_allows_tool(r, "tool_help") for r in ("adult", "kid", "household")), True)
finally:
    io.role = io.client = io.device = io.device_tools = None
    lib._TURN_INFO.info = {}

print("\n── a kid's local prefix lists only a kid's tools ──")
kid_local = lib._local_messages(first_of(kid, "iphone", True) + [{"role": "user", "content": "x"}])[0]["content"]
check("no admin or adult tools, not even as one line", [n for n in ("run_shell", "send_message", "jev_decide", "read_file")
                                                        if f"**{n}**" in kid_local], [])

print("\n── warm_local skips a shape over the context ──")
import warm_local as wl  # noqa: E402


class Fake:
    def __init__(self, n_tokens, n_ctx=16384, fail=False):
        self.n, self.ctx, self.fail = n_tokens, n_ctx, fail

    def _http_json(self, url, payload=None, timeout=30):
        if self.fail:
            raise OSError("down")
        if url.endswith("/props"):
            return {"default_generation_settings": {"n_ctx": self.ctx}}
        return {"tokens": list(range(self.n))}


msgs = [{"role": "system", "content": "x"}]
check("16,822 tokens against 16,384: skipped, with the numbers", wl.too_big(Fake(16822), "u", msgs), "16,822 tokens > 16,384 ctx")
check("11,800 tokens: warmed", wl.too_big(Fake(11800), "u", msgs), None)
check("within 512 of the window: skipped (no room for a turn)", wl.too_big(Fake(16000), "u", msgs) is not None, True)
check("the server can't say: try anyway", wl.too_big(Fake(0, fail=True), "u", msgs), None)

print("\n── the turn log's local_prompt breakdown ──")
lib._TURN_INFO.info = {}
lib._note_local_prompt([{"role": "system", "content": "p" * 300}, {"role": "system", "content": "[SITUATION]" + "s" * 89},
                        {"role": "system", "content": "w" * 60}, {"role": "user", "content": "h" * 30},
                        {"role": "assistant", "content": "a" * 30}, {"role": "user", "content": "q" * 9}])
check("prefix, situation, per-turn system, history, question (≈ chars/3.6 before the server answers)",
      lib._TURN_INFO.info.get("local_prompt"), {"prefix": 83, "situation": 27, "system": 16, "history": 16, "question": 2})
lib._note_local_prompt([{"role": "system", "content": "different"}])
check("the first local call of the turn is the one recorded", lib._TURN_INFO.info["local_prompt"]["prefix"], 83)
chars = dict(lib._TURN_INFO.info["_local_prompt_chars"])
lib._calibrate_local_prompt({"timings": {"prompt_n": 120, "cache_n": 120}})
check("calibrated to the server's real count (prompt_n + cache_n = 240), split by the parts' chars",
      lib._TURN_INFO.info["local_prompt"], {k: round(v * 240 / sum(chars.values())) for k, v in chars.items()})
check("… and calibrated once", "_local_prompt_chars" in lib._TURN_INFO.info, False)
lib._TURN_INFO.info = {}
lib._note_local_prompt([{"role": "system", "content": "x" * 360}, {"role": "user", "content": "q" * 36}])
lib._calibrate_local_prompt({"usage": {"prompt_tokens": 1100}})
check("usage.prompt_tokens when there are no timings", lib._TURN_INFO.info["local_prompt"], {"prefix": 1000, "question": 100})
lib._TURN_INFO.info = {}

print("\n── the local loop's tool gate (#403 follow-up) ──")
io.role, io.client, io.device = "admin", None, (lambda req: {"error": "test"})
io.device_tools = lib.device_tools_for_client(None, None)
lib._TURN_INFO.info = {}
try:
    g = lib._local_tool_gate("file_search")
    check("a made-up name: nothing runs, the tool sharing its words is named first",
          (g.startswith("No tool named 'file_search', so nothing ran."), g.split("Closest: ")[1].startswith("files")),   # #1251 C
          (True, True))
    check("a near miss gets the real tool", lib._local_tool_gate("weather").split("Closest: ")[1].startswith("get_weather"), True)
    check("a core tool runs", lib._local_tool_gate("list_calendar_events"), None)
    g = lib._local_tool_gate("send_message")
    check("a one-line tool not looked up: its full entry and 'call it again', not a run",
          (g.startswith("Not run yet: send_message"), "\n  params: " in g, g.endswith("Call it again with these params.")), (True, True, True))
    check("… the second call runs", lib._local_tool_gate("send_message"), None)
    lib._tool_help({"name": "set_alarm"})
    check("a one-line tool already read with tool_help runs", lib._local_tool_gate("set_alarm"), None)
    check("the gate is in the turn log", lib._TURN_INFO.info.get("tools_gated"), ["file_search:unknown", "weather:unknown", "send_message:params"])
    io.role = "kid"
    check("a kid naming an admin tool gets 'no tool', never its entry",
          lib._local_tool_gate("run_shell").startswith("No tool named 'run_shell'"), True)

    print("\n── the 12 guessed calls from #491's after pass, replayed ──")
    io.role = "admin"
    guessed = ["send_message", "send_message", "send_message", "name_images", "name_images", "write_file", "run_check",
               "file_search", "plan_task", "schedule_action", "set_alarm", "set_fact"]
    lib._TURN_INFO.info = {}
    before_ran = len(guessed)   # before: every call ran with whatever params the model guessed
    outcomes = [lib._local_tool_gate(t) for t in guessed]
    first = {}
    for t, o in zip(guessed, outcomes):
        first.setdefault(t, o)
    check("before: 12 of 12 ran on guessed params; after: no first call of a one-line tool runs blind",
          (before_ran, sum(1 for o in first.values() if o is None)), (12, 0))
    check("… the made-up name is refused with real names", first["file_search"].startswith("No tool named"), True)
    check("… repeats of a tool already shown run (send_message 2nd/3rd, name_images 2nd)",
          [o is None for t, o in zip(guessed, outcomes) if guessed.count(t) > 1], [False, True, True, False, True])

    print("\n── end to end through _run_local_agent (model replies scripted) ──")
    lib._TURN_INFO.info = {}
    replies = iter(['<tool_call>{"tool": "send_message", "args": {"to": "Robin", "text": "hi"}}</tool_call>',
                    '<tool_call>{"tool": "send_message", "args": {"recipient": "Robin", "body": "hi"}}</tool_call>',
                    "Sent."])
    sent_msgs, ran = [], []
    real_http, real_exec = lib._http_json, lib.execute_tool
    lib._http_json = lambda url, payload=None, timeout=30: (sent_msgs.append([m["content"] for m in payload["messages"]]) or
                     {"choices": [{"message": {"content": next(replies)}}], "timings": {"prompt_n": 50, "cache_n": 950}})
    lib.execute_tool = lambda name, args: ran.append((name, dict(args.get("args") or args))) or "Done."
    try:
        out = lib._run_local_agent([{"role": "system", "content": "sys"}, {"role": "user", "content": "text Robin hi"}],
                                   endpoint="http://x", model="m")
    finally:
        lib._http_json, lib.execute_tool = real_http, real_exec
    check("the first send_message didn't run; the second did, with the params it was shown", ran,
          [("send_message", {"recipient": "Robin", "body": "hi"})])
    check("the model saw the full entry before calling again", "Not run yet: send_message" in sent_msgs[1][-1], True)
    check("the reply comes through", out, "Sent.")
    check("local_prompt calibrated to the first call's real count", sum(lib._TURN_INFO.info["local_prompt"].values()), 1000)
finally:
    io.role = io.client = io.device = io.device_tools = None
    lib._TURN_INFO.info = {}

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
