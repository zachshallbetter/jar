#!/usr/bin/env python3
# gate: hermetic   (Gemini faked at _gemini_call, tools stubbed, temp stores; touches nothing live)
"""#1611: a reply never shows titan's internal text. One case per leak class seen in a real household's chats 10-05..10-13:
  1. the kept conversation's location placeholder ("You're at [your location at the time]");
  2. a held action's check scaffolding ("[checked] Verified: … In your reply, say …", "[calendar] It went on …");
  3. a made-up tool name (the model's reasoning as the call's name) reaching the steps line or the status;
  4. a "Correction:" paragraph under a finished answer, where the answer can be fixed itself (a wrong weekday).
Fictional household (Alex, Robin, Leo); clocks pinned, times built with ZoneInfo.
Run: TZ=UTC python3 scripts/test_reply_no_internal_text.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_TMP = Path(tempfile.mkdtemp(prefix="titan-replyleaks-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "PROACTIVE_DB": "proactive.db", "HELD_ACTIONS_DB": "held.db", "ROUTINES_DB": "routines.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "HOUSEHOLD_PREFS": "household.json", "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db",
             "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ["MEMORY_HYBRID"] = "0"
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_runtime_lib as lib  # noqa: E402
import held_actions  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 8, 14, 7, tzinfo=ZONE)   # a Thursday, in the household's zone
lib._local_now = lambda: NOW.replace(tzinfo=None)
lib._TURN_IO.role, lib._TURN_IO.person = "admin", {"id": "alex", "name": "Alex"}
INTERNAL = (lib.LOCATION_KEPT_AS, "[checked]", "[calendar]", "In your reply", "Tell the user", "Correction:")


def leaks(text: str) -> list[str]:
    return [m for m in INTERNAL if m in (text or "")]


def turn(reply: str, *, where: list[str] | None = None, question: str = "Where am I?") -> tuple[str, dict]:
    """The whole reply pipeline (_run_agent_turn) around a model whose answer is `reply`."""
    real = lib._run_agent_turn_inner

    def inner(messages, allowed_tools=None, max_rounds=5, **kw):
        if where:
            lib._TURN_INFO.info["_where_said"] = list(where)
        return reply
    lib._run_agent_turn_inner = inner
    try:
        out = lib._run_agent_turn([{"role": "user", "content": question}])
    finally:
        lib._run_agent_turn_inner = real
    return out, dict(lib.last_turn_info())


print("── 1. the location placeholder ──")
ADDR = "200 N Elm St, Spokane, WA 99201"
note = lib.location_history_note(["You're at [your location at the time] (within about 15 m).", "Sure."])
check("the model is told, in a fixed line, the placeholder isn't a location and to call where_am_i",
      (note is not None, "where_am_i" in (note or ""), "within about 15 m" in (note or "")), (True, True, False))
check("…no line when the history has no placeholder", lib.location_history_note(["You're at home.", ""]), None)
import agent_api_server as api  # noqa: E402
built, _ = api._build_messages("Where am I now?", [
    {"role": "user", "content": "Where am I?"},
    {"role": "assistant", "content": "You're at [your location at the time] (within about 15 m)."}], False, None)
check("the prompt build adds that line after the history, as a system message; the question stays the asker's own",
      ([m["role"] for m in built][-2:], built[-2]["content"] == note, built[-1]["content"]),
      (["system", "user"], True, "Where am I now?"))
out, info = turn("You're at [your location at the time] (within about 15 m).")
check("copied with no address this turn: a plain note, never the placeholder",
      (leaks(out), lib.LOCATION_NOT_KEPT_SAID in out), ([], True))
check("…counted in the turn log", info.get("internal_text_fixed"), {"location": 1})
out, info = turn("You're at [your location at the time] (within about 15 m).", where=[ADDR, "the kitchen"])
check("with this turn's address: the template is filled", out, f"You're at {ADDR} (within about 15 m).")
check("…and the kept conversation still leaves it out", lib.without_location(out, [ADDR]),
      "You're at [your location at the time] (within about 15 m).")

print("── 2. a held action's check scaffolding ──")
CREATED = ("Created 'Pickup Leo' on Thu Oct 8, 3:00 PM on the Family calendar.\n"
           "[checked] Verified: Thu Oct 8, 3:00 PM — Pickup Leo [calendar: Family]. In your reply, say the day, time "
           "and calendar it's on.\n"
           "[calendar] It went on “Robin”, Robin's calendar: appointments go on a person's own calendar unless it's a "
           "family event. Say which calendar.")
plain = lib.scaffold_for_person(CREATED)
check("the result in plain words", plain,
      "Created 'Pickup Leo' on Thu Oct 8, 3:00 PM on the Family calendar.\nChecked: it's there.\n"
      "It's on the “Robin” calendar.")
WARN = ("Cancelled “Team dinner”.\n[checked] WARNING: “Team dinner” still appears in Thu Oct 8: 7:00 PM — Team "
        "dinner. Tell the user it may not have been removed. In your reply, say exactly what was removed and that "
        "saying “undo” restores it.")
check("a warning keeps its fact, loses its instructions", lib.scaffold_for_person(WARN),
      "Cancelled “Team dinner”.\nHeads up: “Team dinner” still appears in Thu Oct 8: 7:00 PM — Team dinner.")
check("text without the tags is untouched", lib.scaffold_for_person("Created it. [see note] Fine."),
      "Created it. [see note] Fine.")
hid = held_actions.put("alex", "create_calendar_event", {"title": "Pickup Leo", "start": "2026-10-08T15:00"})
real_exec = lib.execute_tool
lib.execute_tool = lambda name, args: CREATED
try:
    out, info = turn("(the model isn't asked)", question=f"Yes, do held action {hid}")
finally:
    lib.execute_tool = real_exec
check("the Do it reply has no scaffolding (10-08: the raw instructions were the reply)", leaks(out), [])
check("…and says what happened", ("Pickup Leo" in out, "Checked: it's there." in out), (True, True))
FLAT = ("Done. [checked] Verified: Thu Oct 8, 3:00 PM — Pickup Leo [calendar: Family]. In your reply, say the day, "
        "time and calendar it's on. [calendar] It went on “Robin”, Robin's calendar: appointments go on a person's "
        "own calendar unless it's a family event. Say which calendar.")
out, info = turn(FLAT, question="Add pickup Leo at 3")
check("a reply that copies it inline: the last guard turns it into plain words", (leaks(out), out),
      ([], "Done.\nChecked: it's there.\nIt's on the “Robin” calendar."))
check("…counted in the turn log", info.get("internal_text_fixed"), {"scaffold": 2})
out, info = turn("Your notes say [checked] in the margin:\n```\n[checked] item\n```", question="what's in my notes?")
check("code the reply shows is left alone", "[checked] item" in out, True)
check("a directive ends at its sentence: the reply's own prose after it stays",
      lib.scaffold_for_person("[checked] Verified: X. In your reply, say the date.\nI added it for tomorrow."),
      "Checked: it's there.\nI added it for tomorrow.")
check("…and on the same line", lib.scaffold_for_person(
    "Done. [checked] Verified: X. In your reply, say the date. I added it for tomorrow."),
    "Done.\nChecked: it's there. I added it for tomorrow.")

print("── 2b. nothing internal is ever streamed (AGENT_STREAM_TOKENS) ──")
import reply_stream  # noqa: E402
STREAMED = ("I put it on your calendar for you, and here is what titan checked after the write went through. "
            "[checked] Verified: Thu Oct 8, 3:00 PM — Pickup Leo [calendar: Family]. In your reply, say the day, "
            "time and calendar it's on.\n[calendar] It went on “Robin”, Robin's calendar: appointments go on a "
            "person's own calendar unless it's a family event. Say which calendar.\nYou were at "
            "[your location at the time] then. Anything else?")
MARKS = ("[checked", "[calendar]", "In your reply", "Say which calendar", "[your", lib.LOCATION_KEPT_AS)
bad: list[str] = []
for size in (1, 2, 3, 5, 7, 11, 16, 40):
    shown: list[str] = []
    rs = reply_stream.ReplyStream(shown.append, filter=lib.stream_filter, stops=lambda t: False, min_first=0)
    rs.begin_round()
    for i in range(0, len(STREAMED), size):
        rs.feed(STREAMED[i:i + size])
    sent = ""
    for text in shown:   # each publish is the whole safe text so far; the delta is what it adds
        delta, sent = reply_stream.next_delta(sent, text)
        bad += [f"{size}:{m}" for m in MARKS if m in delta or m in text]
    if not shown:
        bad.append(f"{size}: nothing streamed")
check("no delta (or text shown so far) ever holds a marker, however the model's text is split", bad, [])
check("…and what was streamed is the plain words", ("Checked: it's there." in shown[-1],
                                                    lib.LOCATION_NOT_KEPT_SAID in shown[-1]), (True, True))

print("── 3. a made-up tool name ──")
FAKE = "add_todos_or_something_similar_wait_let_me_check_tool_defini"
check("checked against the tools before a call", (lib._unknown_tool("add_todo"), lib._unknown_tool("where_am_i"),
                                                   lib._unknown_tool("files")), (None, None, None))
RAN, SAW, STATUS = [], [], []
CALLS = [{"name": FAKE, "args": {"text": "buy milk"}}]


def fake_gemini(body, model=None, timeout=None):
    last = (body.get("contents") or [{}])[-1]
    resp = [p for p in last.get("parts") or [] if "functionResponse" in p]
    if resp:
        SAW.append(str(resp[0]["functionResponse"]["response"]["result"]))
        return {"candidates": [{"content": {"parts": [{"text": "Added buy milk to your list."}]}}]}
    return {"candidates": [{"content": {"parts": [{"functionCall": dict(CALLS[0])}]}}]}


lib._gemini_call = fake_gemini
lib.execute_tool = lambda name, args: RAN.append(name) or "STUB: ran"
real_status = lib._emit_status
lib._emit_status = lambda name, args=None: STATUS.append(name)
lib.begin_turn_info()
try:
    lib._run_gemini_agent([{"role": "user", "content": "add buy milk to my list"}], max_rounds=2)
finally:
    lib.execute_tool, lib._emit_status = real_exec, real_status
info = lib._TURN_INFO.info
check("nothing ran, no status line with that name", (RAN, STATUS), ([], []))
check("the model is told it's no tool, and the closest real ones",
      (SAW[0].startswith("No tool named"), "add_todo" in SAW[0]), (True, True))
check("never in the turn's tool_names (the steps line), counted without its name",
      (FAKE in str(info.get("tool_names")), info.get("unknown_tool_calls")), (False, 1))
check("the steps line has no step for it", lib.turn_trace(info.get("tool_names")), None)
SAW.clear()
CALLS[0] = {"name": "add_todo", "args": {"text": "buy milk"}}
lib.execute_tool = lambda name, args: RAN.append(name) or "Added."
try:
    lib._run_gemini_agent([{"role": "user", "content": "add buy milk to my list"}], max_rounds=2)
finally:
    lib.execute_tool = real_exec
check("a real tool still runs", RAN, ["add_todo"])
MCP_FAKE = "mcp_notes_server_add_todo_maybe"
check("an mcp_-shaped name no connected server offers is no tool", lib._unknown_tool(MCP_FAKE) is not None, True)
RAN.clear(), SAW.clear(), STATUS.clear()
CALLS[0] = {"name": MCP_FAKE, "args": {"text": "buy milk"}}
lib.execute_tool = lambda name, args: RAN.append(name) or "STUB: ran"
lib._emit_status = lambda name, args=None: STATUS.append(name)
lib.begin_turn_info()
try:
    lib._run_gemini_agent([{"role": "user", "content": "add buy milk to my list"}], max_rounds=2)
finally:
    lib.execute_tool, lib._emit_status = real_exec, real_status
info = lib._TURN_INFO.info
check("…refused like any unknown name: nothing ran, no status, not recorded by name, counted",
      (RAN, STATUS, MCP_FAKE in str(info), info.get("unknown_tool_calls"), SAW[0].startswith("No tool named")),
      ([], [], False, 1, True))

print("── 4. a corrected answer replaces the first ──")
out, info = turn("Ballet is Saturday, October 10 at 5:15 PM, and Leo's game is Thursday, October 8.",
                 question="when's ballet?")
check("right weekdays are left as they are",
      out, "Ballet is Saturday, October 10 at 5:15 PM, and Leo's game is Thursday, October 8.")
out, info = turn("Ballet is Friday, October 10 at 5:15 PM.", question="when's ballet?")
check("…Friday, October 10 (a Saturday) reads Saturday", (out, leaks(out)), ("Ballet is Saturday, October 10 at 5:15 PM.", []))
check("…counted as before", info.get("date_corrections"), 1)
check("the day pinned is a Thursday in the household's zone", NOW.strftime("%A"), "Thursday")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
