#!/usr/bin/env python3
# gate: hermetic   (no model, no stores: the rows, the retry with a fake run, and per-role declared tools)
"""Questions that need a read (#1203 fix 2, #1244, #1249): the routines, what's set up, what's private, the threads, a group's
or person's messages. One retry, read then answer, only with a reader this asker's role has, kept only when it ran.
Views x viewers: an adult, a kid, a shared screen and the admin, each asking about a group they're not in, are never
asked to read anything their role can't (a kid and a screen have no search_messages), and the read itself is the
tool's own, per asker. Plus #1244's other half (the injection disclosure runs on every brain's reply) and #1203 fix 5
(turn_eval's where-run expectation on a box). Fictional people.
Run: python3 scripts/test_required_reads.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-reqreads-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import household_identity as hi  # noqa: E402
import required_reads as R  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


READERS = ({r for _, _, rs in R.ROWS for r in rs} | {r for _, _, rs, needs in R.ACTS for r in (*rs, *needs)}
           | {"view_image"})   # #1249's reader too (the role table decides it)
ALL = set(READERS)

print("── which questions need which read ──")
WANT = {
    "What routines do I have?": "list_routines",
    "Who's in the household?": "capabilities",
    "What could I give you to improve your ability to do anything I ask?": "capabilities",
    "What have you kept private for me? What's fenced?": "list_private",
    "What's going on with my Cobalt Wireless trade-in?": "threads",
    "Anything new from the soccer group?": "search_messages",
    "What did Robin text me about pickup?": "search_messages",
    "What did the book club chat say about Thursday?": "search_messages",
}
check("each target question gets its reader", {q: [r for _, r in R.needed(q, ALL, set())] for q in WANT},
      {q: [r] for q, r in WANT.items()})
NOT = ["What's the weather tomorrow?", "Set the thermostat to 70.", "Draw the picture of a bean.", "Text Robin I'm late.",
       "What time is it?", "Add milk to the list."]
check("nothing else is asked to read", {q: R.needed(q, ALL, set()) for q in NOT}, {q: [] for q in NOT})
check("a reader that already ran: nothing missing", R.needed("Anything new from the soccer group?", ALL, {"search_messages"}), [])

print("── views x viewers: a group they're not in ──")


def declared(role):
    return {t for t in READERS if role == "admin" or hi.role_allows_tool(role, t)}


Q = "Anything new from the soccer group?"
got = {role: [r for _, r in R.needed(Q, declared(role), set())] for role in ("admin", "adult", "kid", "household")}
check("an adult or the admin is asked to read their OWN messages (search_messages runs on their own Mac); a kid and a "
      "shared screen have no search_messages, so they're never asked", got,
      {"admin": ["search_messages"], "adult": ["search_messages"], "kid": [], "household": []})
for role in ("admin", "adult", "kid", "household"):
    for q in WANT:
        asked = {r for _, r in R.needed(q, declared(role), set())}
        if not asked <= declared(role):
            check(f"{role}: '{q}' asks only for a reader the role has", sorted(asked - declared(role)), [])
check("no row ever asks a role for a reader it doesn't have (all roles, all target questions)",
      all({r for _, r in R.needed(q, declared(role), set())} <= declared(role)
          for role in ("admin", "adult", "kid", "household") for q in WANT), True)
check("a shared screen: never list_private or threads (another person's), whatever is asked",
      sorted({r for q in WANT for _, r in R.needed(q, declared("household"), set())} & {"list_private", "threads",
                                                                                      "search_messages"}), [])
check("search_messages is the asker's own device's read (the role table says so), never a household one",
      ("search_messages" in hi.TOOL_ROLES and hi.TOOL_ROLES["search_messages"] == frozenset({"admin", "adult"})), True)

print("── the retry ──")
import agent_runtime_lib as lib  # noqa: E402
io = lib._TURN_IO
real_specs = lib._turn_tool_specs
calls = []


def turn(role="adult", outcomes=None):
    lib._turn_tool_specs = lambda: {n: {"description": n, "parameters": {}} for n in declared(role)}
    io.outcomes = list(outcomes or [])
    io.tools_used = set()
    io.deadline = None
    lib._TURN_INFO.info = {}


def reads(tool, answer):
    def run(m):
        calls.append(m)
        io.outcomes.append({"tool": tool, "ok": True, "write": False, "text": "…"})
        return answer
    return run


M = [{"role": "user", "content": Q}]
turn()
got = lib._required_read_retry("No new updates from the soccer group.", M, reads("search_messages", "Practice moved to 5."))
check("answered without reading: asked once more, read, and the answer replaces it", got, "Practice moved to 5.")
check("…the instruction names the reader and forbids answering from memory",
      ("call search_messages" in calls[-1][-1]["content"], "don't answer from memory" in calls[-1][-1]["content"]),
      (True, True))
check("…counted, and counted as fixed", (lib._TURN_INFO.info.get("required_read_retry"),
                                          lib._TURN_INFO.info.get("required_read_retry_fixed")), (1, 1))
calls.clear()
turn()
got = lib._required_read_retry("No new updates.", M, lambda m: "Still nothing new.")
check("the second try didn't read: the first reply stands", got, "No new updates.")
turn(role="kid")
got = lib._required_read_retry("No new updates.", M, reads("search_messages", "x"))
check("a kid: no search_messages, so never asked (nothing widens)", (got, len(calls)), ("No new updates.", 0))
turn(role="household")
got = lib._required_read_retry("No new updates.", M, reads("search_messages", "x"))
check("a shared screen: never asked", (got, len(calls)), ("No new updates.", 0))
turn(outcomes=[{"tool": "add_todo", "ok": True, "write": True, "text": "added"}])
got = lib._required_read_retry("No new updates.", M, reads("search_messages", "x"))
check("something was changed this turn: never asked again", len(calls), 0)
turn(outcomes=[{"tool": "search_messages", "ok": True, "write": False, "text": "…"}])
got = lib._required_read_retry("No new updates.", M, reads("search_messages", "x"))
check("it already read: left as it is", len(calls), 0)
turn()
io.deadline = __import__("time").monotonic() + 3
lib._required_read_retry("No new updates.", M, reads("search_messages", "x"))
check("too little time left: left as it is", len(calls), 0)
io.deadline = None

print("── #1249: a follow-up about this conversation's own attachment ──")
ATT = [{"role": "user", "content": "What does this image show?  [Attached photo “Photo”, on this iPhone at "
                                   "attachments/photo-ab12cd34.jpg. Use view_image with that path to see it.]"},
       {"role": "assistant", "content": "A map of the lake trail."}]
FOLLOW = ATT + [{"role": "user", "content": "What's in the bottom corner of it?"}]
check("a visual follow-up, not just 'it'",
      [R.refers_to_attachment(q) for q in ("What's in the bottom corner of it?", "What does that sign say?",
                                          "Is it going to rain?", "Who made it?", "Draw the picture of a bean.")],
      [True, True, False, False, False])
check("the reader comes from titan's own attachment hint", (R.attachment_reader(lib._earlier_attachment(FOLLOW)),
      R.attachment_reader("")), ("view_image", None))
turn()
got = lib._required_read_retry("It's a trail map.", FOLLOW, reads("view_image", "The bottom corner shows a compass."))
check("an adult: the follow-up is asked to look again, at the exact path from THIS conversation",
      (got, "attachments/photo-ab12cd34.jpg" in calls[-1][-1]["content"], "call view_image" in calls[-1][-1]["content"]),
      ("The bottom corner shows a compass.", True, True))
calls.clear()
turn(role="kid")
lib._required_read_retry("It's a trail map.", FOLLOW, reads("view_image", "x"))
check("a kid's own conversation: view_image is theirs, so they're asked too", len(calls), 1)
calls.clear()
turn(role="household")
got = lib._required_read_retry("It's a trail map.", FOLLOW, reads("view_image", "x"))
check("a shared screen: no view_image, never asked", (got, len(calls)), ("It's a trail map.", 0))
turn()
got = lib._required_read_retry("Sunny.", [{"role": "user", "content": "What's in the bottom corner of it?"}],
                               reads("view_image", "x"))
check("no attachment in this conversation: nothing to look at again", (got, len(calls)), ("Sunny.", 0))
turn()
lib._required_read_retry("x", ATT + [{"role": "user", "content": "Is it going to rain?"}], reads("view_image", "x"))
check("an attachment earlier, but a question that isn't about it: not asked", len(calls), 0)
turn()
NEW = [{"role": "user", "content": "What's in the bottom corner of this?  [Attached photo “Photo”, on this iPhone at "
                                    "attachments/photo-ef56.jpg. Use view_image with that path to see it.]"}]
lib._required_read_retry("x", NEW, reads("view_image", "x"))
check("the photo is in the newest message: the turn's own job, not a follow-up retry", len(calls), 0)
turn(outcomes=[{"tool": "view_image", "ok": True, "write": False, "text": "…"}])
lib._required_read_retry("x", FOLLOW, reads("view_image", "x"))
check("it already looked again: left as it is", len(calls), 0)

print("── #1251: the Mac right now, and requests that need one act ──")
MAC_Q = "What app am I using on my Mac right now?"
check("the Mac right now: the Mac's own read", [r for _, r in R.needed(MAC_Q, ALL, set())], ["get_frontmost_app"])
check("…not 'run the self-tests on my Mac', 'is Jellybean running on my MacBook?' or 'what's on my calendar?'",
      [R.needed(q, ALL, set()) for q in ("Run the self-tests on my Mac.", "Is Jellybean running on my MacBook?",
                                         "What's on my calendar right now?")], [[], [], []])
ACT_Q = {"Do the next step of Ava's party task on Monday at 9 AM.": "schedule_action",
         "Keep going on Ava's party task on your own, once a day.": "task_autonomy",
         "Continue the task task-5f2a: Sunday is fine for the party.": "task_answer"}
check("each task request gets its one act", {q: (R.needed_act(q, ALL, set()) or ("", ""))[1] for q in ACT_Q}, ACT_Q)
check("…and no read", {q: R.needed(q, ALL, set()) for q in ACT_Q}, {q: [] for q in ACT_Q})
NOT_ACT = ["Remind me every day to water the plants.", "What tasks do I have?", "Plan Ava's birthday party.",
           "What's the next step?", "Text Robin every day at 5.", "What app am I using?"]
check("nothing else is asked to act", {q: R.needed_act(q, ALL, set()) for q in NOT_ACT}, {q: None for q in NOT_ACT})
check("the act already ran: nothing", R.needed_act("Continue the task task-5f2a: Sunday.", ALL, {"task_answer"}), None)
check("views x viewers: the task tools are the admin's (a step scheduled runs run_task, so no one else is asked to "
      "schedule one); the Mac's reads never a kid's or a screen's", {role: ([(R.needed_act(q, declared(role), set()) or ("", None))[1] for q in ACT_Q],
                                      [r for _, r in R.needed(MAC_Q, declared(role), set())])
                                for role in ("admin", "adult", "kid", "household")},
      {"admin": (["schedule_action", "task_autonomy", "task_answer"], ["get_frontmost_app"]),
       "adult": ([None, None, None], ["get_frontmost_app"]),
       "kid": ([None, None, None], []),
       "household": ([None, None, None], [])})

TASK = [{"role": "user", "content": "Continue the task task-5f2a: Sunday is fine for the party."}]


def acts(tool, answer):
    def run(m):
        calls.append(m)
        io.outcomes.append({"tool": tool, "ok": True, "write": True, "text": "…"})
        return answer
    return run


calls.clear()
turn(role="admin", outcomes=[{"tool": "list_reminders", "ok": True, "write": False, "text": "…"}])
got = lib._required_read_retry("You have no reminders about the party.", TASK, acts("task_answer", "Passed it on: Sunday."))
check("the admin's task answer read reminders instead: asked once to call task_answer, kept when it did",
      (got, "call task_answer" in calls[-1][-1]["content"], lib._TURN_INFO.info.get("required_act_retry"),
       lib._TURN_INFO.info.get("required_act_retry_fixed")), ("Passed it on: Sunday.", True, 1, 1))
calls.clear()
turn(role="admin")
got = lib._required_read_retry("Noted.", TASK, lambda m: "Okay, noted again.")
check("…the second try didn't act: the first reply stands", got, "Noted.")
turn(role="adult")
got = lib._required_read_retry("Noted.", TASK, acts("task_answer", "x"))
check("an adult has no task tools: never asked", (got, len(calls)), ("Noted.", 0))
turn(role="admin", outcomes=[{"tool": "create_reminder", "ok": True, "write": True, "text": "…"}])
lib._required_read_retry("Noted.", TASK, acts("task_answer", "x"))
check("something already changed this turn: never asked to act on top of it", len(calls), 0)

print("── #1251: the Mac right now, from a phone whose Mac can't be reached ──")
MAC = [{"role": "user", "content": MAC_Q}]
real_client = lib._turn_client
STATUS = "Your Mac (Test Mac): not connected; last checked in 3 hours ago."


def phone_turn(role="adult", mac_reachable=False, status=STATUS):
    turn(role)
    specs = {n: {"description": n, "parameters": {}} for n in declared(role)
             if mac_reachable or n not in {"get_frontmost_app", "get_screen", "get_field_state"}}
    lib._turn_tool_specs = lambda: specs
    lib._turn_client = lambda: "iphone"
    io.mac_status = status


calls.clear()
phone_turn()
got = lib._required_read_retry("You have a meeting at 3.", MAC, lambda m: (calls.append(m), "I can't reach your Mac right now.")[1])
check("unreachable: asked once to say so, from titan's own Mac status line, and kept when it did",
      (got, STATUS in calls[-1][-1]["content"], lib._TURN_INFO.info.get("mac_out_retry"),
       lib._TURN_INFO.info.get("mac_out_retry_fixed")), ("I can't reach your Mac right now.", True, 1, 1))
calls.clear()
phone_turn()
got = lib._required_read_retry("You have a meeting at 3.", MAC, lambda m: (calls.append(m), "You're in Safari.")[1])
check("…a second try that still doesn't say so: the first reply stands", got, "You have a meeting at 3.")
calls.clear()
phone_turn()
lib._required_read_retry("Your Mac isn't reachable from here right now.", MAC, lambda m: (calls.append(m), "x")[1])
check("…already said: left as it is", len(calls), 0)
phone_turn(mac_reachable=True)
lib._required_read_retry("You have a meeting at 3.", MAC, reads("get_frontmost_app", "Safari is in front."))
check("reachable: the read retry, not the unreachable one", (lib._TURN_INFO.info.get("required_read_retry"),
      lib._TURN_INFO.info.get("mac_out_retry")), (1, None))
calls.clear()
phone_turn(status=None)
lib._required_read_retry("You have a meeting at 3.", MAC, lambda m: (calls.append(m), "x")[1])
check("no Mac status line (not a phone turn titan described): never asked", len(calls), 0)
lib._turn_client = real_client
io.mac_status = None

print("── #1251: the brain-size rows, and the local retry's forced call ──")
FORCE_Q = {"Rename the Gemini captures in my Downloads by what's in them.": "name_images",
           "That worked. I'll want that again, keep it.": "create_skill"}
check("each gets its one act", {q: (R.needed_act(q, ALL, set()) or ("", ""))[1] for q in FORCE_Q}, FORCE_Q)
NOT_FORCE = ["Rename my screenshots folder.", "What's in this picture?", "Ask me before deleting anything.", "Keep it short.",
             "Don't ask me again about the weather.", "Keep it for me?",
             "Stop asking me for permission, just do things.", "Don't ask me first, just do it."]
check("not these (and never 'stop asking me for permission': how titan asks first is the person's own setting)",
      {q: R.needed_act(q, ALL | {"set_preference"}, set()) for q in NOT_FORCE}, {q: None for q in NOT_FORCE})
check("no act row targets a tool in FORCE_NEVER (set_preference, trust and grants)",
      sorted({t for _, _, ts, _ in R.ACTS for t in ts} & R.FORCE_NEVER), [])
check("views x viewers: only a tool the role has (name_images and create_skill are an adult's or the admin's; a kid "
      "and a shared screen are never asked)", {role: [(R.needed_act(q, declared(role), set()) or ("", None))[1] for q in FORCE_Q]
                                                for role in ("admin", "adult", "kid", "household")},
      {"admin": ["name_images", "create_skill"], "adult": ["name_images", "create_skill"],
       "kid": [None, None], "household": [None, None]})

forced_calls = []


def forced(tool_ran):
    def go(m, tool):
        forced_calls.append(tool)
        io.outcomes.append({"tool": tool_ran, "ok": True, "write": True, "text": "…"})
        return "Saved the skill “disk-free”."
    return go


Q_KEEP = [{"role": "user", "content": "That worked. I'll want that again, keep it."}]
calls.clear(); forced_calls.clear()
turn(role="adult")
got = lib._required_read_retry("Glad it worked!", Q_KEEP, reads("x", "unused"), forced=forced("create_skill"))
check("local: the act retry goes through the forced runner with the named tool, and is kept when it ran",
      (got, forced_calls, len(calls), lib._TURN_INFO.info.get("required_act_forced"),
       lib._TURN_INFO.info.get("required_act_retry_fixed")), ("Saved the skill “disk-free”.", ["create_skill"], 0, 1, 1))
check("…and the forced marker is cleared after it", getattr(io, "forced_tool", None), None)
forced_calls.clear()
turn(role="adult")
got = lib._required_read_retry("Okay.", Q_KEEP, reads("x", "unused"), forced=forced("kb_ingest"))
check("…a forced round whose call didn't run the named tool: the first reply stands", got, "Okay.")
calls.clear(); forced_calls.clear()
turn(role="adult")
lib._required_read_retry("Okay.", Q_KEEP, acts("create_skill", "Done."))
check("Gemini (no forced runner): the plain act retry, as before", (len(calls), forced_calls), (1, []))
calls.clear(); forced_calls.clear()
turn(role="adult")
lib._required_read_retry("No new updates.", M, reads("search_messages", "Practice moved."), forced=forced("x"))
check("a read row never uses the forced runner", (len(calls), forced_calls), (1, []))
forced_calls.clear()
turn(role="adult", outcomes=[{"tool": "ingest_text", "ok": True, "write": True, "text": "saved"}])
lib._required_read_retry("Saved a note.", Q_KEEP, reads("x", "x"), forced=forced("create_skill"))
check("something already changed this turn (a note saved): never forced on top of it", forced_calls, [])
forced_calls.clear()
turn(role="kid")
lib._required_read_retry("Okay.", [{"role": "user", "content": "That worked. I'll want that again, keep it."}],
                         reads("x", "x"), forced=forced("create_skill"))
check("a kid has no create_skill: never forced", forced_calls, [])

print("── a forced call never changes a preference, least of all asking first (Coordination, 10-08) ──")
real_src = lib.turn_source
lib.turn_source = lambda: "user"   # past the eval short-circuit, to the real check
saved_store = None
for key, val in (("ask_first", "off"), ("ask_first", "on"), ("offers", "off"), ("calendar", "Family")):
    io.forced_tool = "create_skill"
    io.person = {"id": "p_alex", "name": "Alex"}
    out = str(lib._execute_tool("set_preference", {"name": key, "value": val}))
    io.forced_tool = io.person = None
    check(f"forced: set_preference {key}={val} is refused, nothing changed", out.startswith("Not changed: preferences"), True)
lib.turn_source = real_src
FORCED_REQ = {act[1] for q in ("Stop asking me for permission, just do things.", "Don't ask me first.",
                                "Rename the Gemini captures by what's in them.")
              for act in [R.needed_act(q, ALL | R.FORCE_NEVER, set())] if act}
check("no request, however worded, forces set_preference", "set_preference" in FORCED_REQ, False)

print("── the no-access retry is unchanged (a bad edit once reached it) ──")
calls.clear()
turn(role="adult")
got = lib._no_access_retry("I don't have access to Robin's text messages.", M, reads("search_messages", "Robin said 5."))
check("it still asks once and keeps the read", (got, len(calls)), ("Robin said 5.", 1))

print("── the grammar, and the local runner's first round ──")
g = lib.tool_call_grammar("set_preference")
check("one <tool_call> to that tool, any JSON arguments",
      (g.startswith('root ::= "<tool_call>\\n{\\"tool\\": \\"set_preference\\""'), "object ::=" in g, "string ::=" in g),
      (True, True, True))
check("a tool name can't break out of the grammar", 'evil' in lib.tool_call_grammar('evil"; root ::= x') and
      '"; root' not in lib.tool_call_grammar('evil"; root ::= x'), True)
sample = '<tool_call>\n{"tool": "set_preference", "name": "ask_first", "value": "off"}\n</tool_call>'
check("what the grammar allows is what parse_tool_calls reads", lib.parse_tool_calls(sample),
      [{"tool": "set_preference", "name": "ask_first", "value": "off"}])
sent = []
real_http, real_exec = lib._http_json, lib.execute_tool


def fake_http(url, payload=None, timeout=30):
    if payload is None:
        return {"data": [{"id": "qwen"}]}
    sent.append(dict(payload))
    if len(sent) == 1:
        return {"choices": [{"message": {"content": sample}, "finish_reason": "stop"}]}
    return {"choices": [{"message": {"content": "Done: I won't ask first."}, "finish_reason": "stop"}]}


lib._http_json = fake_http
lib.execute_tool = lambda name, args: "Set ask_first off."
turn(role="adult")
out = lib._run_local_agent([{"role": "user", "content": "Stop asking."}], force_tool="set_preference", max_rounds=2)
check("force_tool: the first round carries the grammar, the next round doesn't",
      (out, "grammar" in sent[0], "grammar" in sent[1]), ("Done: I won't ask first.", True, False))
sent.clear()


def refuses(url, payload=None, timeout=30):
    if payload is None:
        return {"data": []}
    if "grammar" in payload:
        raise __import__("urllib.error").error.HTTPError(url, 400, "no grammar", {}, None)
    sent.append(dict(payload))
    return {"choices": [{"message": {"content": "Okay."}, "finish_reason": "stop"}]}


lib._http_json = refuses
check("a server that refuses the grammar: the round runs unconstrained",
      (lib._run_local_agent([{"role": "user", "content": "x"}], force_tool="set_preference"), len(sent)), ("Okay.", 1))
lib._http_json, lib.execute_tool = real_http, real_exec

print("── #1251 (10-08): a failed call isn't done ──")
Q_REN = [{"role": "user", "content": "Rename the Gemini captures in my Downloads by what's in them."}]
forced_calls.clear()
turn(role="adult", outcomes=[{"tool": "name_images", "ok": False, "write": False, "text": "Error: paths is required"}])
lib._required_read_retry("I couldn't name them.", Q_REN, reads("x", "x"), forced=forced("name_images"))
check("name_images failed (no paths): the act is still needed, so the forced retry comes", forced_calls, ["name_images"])
forced_calls.clear()
turn(role="adult", outcomes=[{"tool": "name_images", "ok": True, "write": False, "text": "Suggested names"}])
lib._required_read_retry("Here are the names.", Q_REN, reads("x", "x"), forced=forced("name_images"))
check("…it ran OK: done, nothing forced", forced_calls, [])

print("── wired into both brains, and the disclosure on every reply ──")
src = (HERE / "agent_runtime_lib.py").read_text()
check("local: lookup, then no-access, then required-read, then the claim retry", (
      src.index("text = _local_lookup_retry(text, messages") < src.index("text = _no_access_retry(text, messages")
      < src.index("text = _required_read_retry(text, messages") < src.index("text = _local_claim_retry(text, messages")),
      True)
check("Gemini: after the no-access retry", src.index("text = _no_access_retry(text, gm") < src.index(
      "text = _required_read_retry(text, gm"), True)
check("#1244: the injection disclosure wraps the turn's reply for every brain (the outer chain, not one brain's path)",
      "return _never_empty(_without_internal_text(_with_injection_disclosed(" in src, True)
check("the turn log keeps required_read_retry and …_fixed",
      '"required_read_retry", "required_read_retry_fixed"' in src, True)
check("#1251: and required_act_retry, mac_out_retry (each …_fixed)",
      '"required_act_retry", "required_act_retry_fixed", "required_act_forced", "mac_out_retry", "mac_out_retry_fixed"' in src, True)
check("#1251: the local site passes the forced runner; Gemini's doesn't", ("force_tool=tool, **opts" in src,
      "text = _required_read_retry(text, gm, lambda m: _run_gemini_agent(m, max_rounds=2, allowed_tools=allowed_tools))" in src),
      (True, True))

print("── #1203 fix 5: where-run on a box ──")
import turn_eval as te  # noqa: E402
box_good = te.Turn(text="I run here, on your own hardware, and you set the rules.")
box_bad = te.Turn(text="Chat runs on Google's Gemini.")
os.environ["TURN_EVAL_PROFILE"] = "box"
check("box: 'runs here' passes; 'chat runs on Gemini' is the miss", (te.c_where_run(box_good), te.c_where_run(box_bad)),
      ([], ["said chat runs on Gemini on a box with no cloud brain", "didn't say it runs here"]))
os.environ.pop("TURN_EVAL_PROFILE")
check("live (no profile): the Gemini expectation as before", te.c_where_run(box_good)[:1],
      ["didn't say chat runs on Google's Gemini"])

lib._turn_tool_specs = real_specs
io.outcomes = io.tools_used = None

print("── #411 day one: the house right now needs home_status (questions only, never a command) ──")
import required_reads as _rr  # noqa: E402
_D = {"home_status", "home_control"}
for q, exp in (("What's on in the house right now?", True), ("What lights are on?", True), ("What's the house doing?", True),
               ("Is the garage open?", True), ("Is the front door locked?", True), ("Is anything left on?", True),
               ("Turn on the kitchen light.", False), ("Turn everything off.", False), ("Unlock the front door.", False),
               ("What's on my calendar tomorrow?", False), ("What's on TV tonight?", False), ("Is Robin home?", False)):
    check(f"{q!r} → home_status {'needed' if exp else 'not asked'}",
          bool([x for x in _rr.needed(q, _D, set()) if x[1] == "home_status"]), exp)
check("…and not when home_status already ran", _rr.needed("What lights are on?", _D, {"home_status"}), [])
check("…nor for an asker without it (a role that doesn't have the home tools)", _rr.needed("What lights are on?", set(), set()), [])


print("── the house row: never a person's whereabouts; each role sees only its devices (Coordination's review) ──")
for q in ("Is Robin home?", "Where is Alex?", "Is Alex at home?", "Who's home?", "Is anyone home?", "Where's Ava right now?",
          "Is Robin at the house?"):
    check(f"{q!r}: a person's location, never the house row", [x for x in _rr.needed(q, _D, set()) if x[1] == "home_status"], [])
import home_assistant as _ha  # noqa: E402
_STATES = [{"entity_id": e, "state": st, "attributes": {"friendly_name": n, **({"device_class": "garage"} if "garage" in e else {})}}
           for e, st, n in (("light.kitchen", "on", "Kitchen Light"), ("lock.front_door", "locked", "Front Door"),
                            ("alarm_control_panel.home", "armed_home", "Home Alarm"),
                            ("cover.garage_door", "closed", "Garage Door"), ("camera.porch", "idle", "Porch Camera"))]
_real_http, _real_role = _ha._http, lib.turn_role
_ha._http = lambda method, path, body=None: _STATES if path.startswith("/api/states") else ""
for role, sees in (("admin", True), ("adult", True), ("kid", False), ("household", False)):
    lib.turn_role = lambda r=role: r
    out = lib._execute_tool("home_status", {})
    check(f"{role}: the read the row asks for shows the kitchen light; locks, alarm, garage, camera {'shown' if sees else 'hidden'}",
          ("Kitchen" in out, any(n in out for n in ("Front Door", "Home Alarm", "Garage Door", "Porch Camera"))), (True, sees))
_ha._http, lib.turn_role = _real_http, _real_role
check("the row only names a reader declared for the asker: none declared, nothing asked",
      _rr.needed("What's on in the house?", {"capabilities"}, set()), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
