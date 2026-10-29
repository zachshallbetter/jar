#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A reply never says it made a change no tool made (10-02 review of Zach's conversations: 11 Gemini replies in two
days, e.g. "I've sent the complete architecture spec over to your Claude session" with no tool, "I've added that to
Claude's list" with no add_todo, "I've corrected it" after only recall_events, "I've added that to your Reminders
list" after only list_reminders).
  - the claims are recognised; offers, questions, plans and denials aren't claims;
  - Gemini is asked once more with the tool that would make it (required when the person asked for a change);
  - otherwise: "Correction: I didn't actually …. Want me to?";
  - a card's "Tell me more" never changes anything (10-02 06:37/06:39 wrote todos);
  - "she never got my text" is answered from what was sent (10-02 00:36: "her phone was at 5%").
Fake model and stores. Run: python3 scripts/test_unbacked_claims.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-claims-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "TODOS_DB": "todos.db",
             "SCHEDULED_DB": "scheduled.db", "PROACTIVE_DB": "proactive.db", "PLACES_DB": "places.db",
             "CONNECTORS_LEDGER": "ledger.db", "ROUTINES_DB": "routines.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def ran(*tools, ok=True):
    lib._TURN_IO.outcomes = [{"tool": t, "ok": ok, "write": t in lib._WRITE_TOOLS, "text": "x"} for t in tools]


def whats(text):
    return [w for _, w in lib._unbacked_claims(text)]


print("── the claims (10-02 review) ──")
ran()
check("“I've sent the complete architecture spec over to your Claude session”, no tool",
      whats("I've sent the complete architecture spec over to your Claude session."), ["send that to Claude"])
check("“I've added that to Claude's list”, no tool", whats("I've added that to Claude's list."),
      ["add that to Claude's list"])
ran("recall_events")
check("“I've corrected it” after only recall_events", whats("I've corrected it."), ["make that change"])
ran("list_reminders")
check("“I've added that to your Reminders list” after only list_reminders",
      whats("I've added that to your Reminders list."), ["add that reminder"])
ran()
check("logged on the timeline", whats("I've logged it on the household timeline."), ["log that on the timeline"])
check("added to the calendar", whats("Done, I added the dentist to your Zach calendar."), ["add that to the calendar"])
check("added to the to-do list", whats("I've put it on your to-do list."), ["add that to the to-do list"])
check("sent a text", whats("I've texted Robin that dinner is at 6."), ["send that"])

print("── backed, or not a claim ──")
ran("add_todo")
check("add_todo ran: backed", whats("I've added that to Claude's list."), [])
ran("send_to_claude")
check("send_to_claude ran: backed", whats("I've sent it over to your Claude session."), [])
ran("create_reminder", ok=False)
check("a failed try is _with_honest_outcome's, not this", whats("I've added that to your Reminders list."), [])
ran("log_event")
check("any change backs “I've corrected it”", whats("I've corrected it."), [])
ran()
for text in ("Want me to add it to Claude's list?", "I haven't added it to Reminders yet.",
             "Once you confirm, I'll add it to your calendar.", "I can send that to Claude if you like.",
             "Your meeting was moved to 3 PM.", "Ava's recital was added to the Family calendar by Robin?",
             "I've updated the calendar event to 4 PM."):
    check(f"not corrected: {text!r}", whats(text), [])
lib._TURN_IO.outcomes = None
check("no turn (outcomes None): nothing", whats("I've added that to Claude's list."), [])

print("── the Correction ──")
ran("recall_events")
out = lib._with_backed_claims("I've added that to Claude's list.")
check("one plain line, with an offer", out, "I've added that to Claude's list.\n\nCorrection: I didn't actually add that "
      "to Claude's list. Want me to?")
check("…said once for a repeated claim", lib._with_backed_claims("I've added it to Claude's list. Yes, I added it to "
      "Claude's list.").count("Correction:"), 1)

print("── Gemini, once more with the tool ──")
lib._TURN_IO.role = "admin"
lib._TURN_IO.person = {"id": "zach", "name": "Zach"}
calls = []


def fixed(m, tools, force):
    calls.append((tools, force, m[-1]["content"]))
    lib._TURN_IO.outcomes.append({"tool": "add_todo", "ok": True, "write": True, "text": "Added to Claude's list."})
    return "Added “Look at the Watch build” to Claude's list."


GM = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Add looking at the Watch build to Claude's list"}]
ran()
got = lib._gemini_claim_retry("I've added that to Claude's list.", GM, None, fixed)
check("the retry's backed reply is kept", got, "Added “Look at the Watch build” to Claude's list.")
check("…asked with add_todo only, required (the person asked for it)", (calls[-1][0], calls[-1][1]),
      ({"add_todo"}, ["add_todo"]))
check("…told what it claimed", "Your reply said you did this: add that to Claude's list." in calls[-1][2], True)
ran()
lib._gemini_claim_retry("I've added that to Claude's list.", [GM[0], {"role": "user", "content": "What's Claude up to?"}],
                        None, fixed)
check("the person didn't ask for a change: the tool is offered, not required", calls[-1][1], None)
ran()
got = lib._gemini_claim_retry("I've added that to Claude's list.", GM, None, lambda m, t, f: "I've added it to Claude's list.")
check("a retry that claims again without the tool: the first reply goes on (and gets the Correction)", got,
      "I've added that to Claude's list.")
ran()
n = len(calls)
lib._gemini_claim_retry("I've corrected it.", GM, None, fixed)
check("an unnamed change (no tool to require): no retry, only the Correction", len(calls), n)
ran("home_control")
lib._TURN_IO.outcomes[0]["write"] = True
lib._gemini_claim_retry("I've added that to Claude's list.", GM, None, fixed)
check("something else was already changed this turn: no retry", len(calls), n)
check("…a tool this turn can't use: no retry",
      lib._gemini_claim_retry("I've added that to Claude's list.", GM, {"web_search"}, fixed) == "I've added that to Claude's list."
      and len(calls) == n, True)

print("── speech and pictures (#893) ──")
ran()
check("“I've prepared the speech.”, no tool (the local brain, 3 of 3)",
      whats("I've prepared the speech. Would you like me to play it back now?"), ["read that aloud"])
check("“I've generated the audio”", whats("I've generated the audio for you."), ["read that aloud"])
print("── asked you to confirm (10-16 live) ──")
ran("mail_move_preview", "mail_move_preview")
check("previews only, then “I've asked you to confirm”: not backed",
      whats("I've asked you to confirm bringing back the Railway emails."), ["ask you to confirm that"])
ran("mail_move_preview", "mail_move")
check("mail_move sent the card: backed", whats("I've asked you to confirm bringing back the Railway emails."), [])
ran("send_message")
check("a Mac write asks on the Mac: backed", whats("I've sent you a card to confirm on your Mac."), [])
ran("set_preference")
check("a held setting: backed", whats("I've asked for your OK on your iPhone."), [])
ran()
check("an offer isn't a claim", whats("Want me to ask you to confirm it?"), [])
check("…and the Correction offers it", lib._with_backed_claims("I've asked you to confirm it."),
      "I've asked you to confirm it.\n\nCorrection: I didn't actually ask you to confirm that. Want me to?")
check("“I've created a picture of the garden”", whats("I've created a picture of the garden."), ["make that picture"])
check("an offer isn't a claim", whats("Want me to read that aloud?"), [])
check("a plan isn't a claim", whats("I'll prepare the speech now."), [])
ran("speak")
check("speak ran: backed", whats("I've prepared the speech."), [])
ran("generate_image")
check("generate_image ran: backed", whats("I've created the image."), [])
ran()
check("the Correction says it plainly", lib._with_backed_claims("I've prepared the speech."),
      "I've prepared the speech.\n\nCorrection: I didn't actually read that aloud. Want me to?")

print("── the local brain, once more with the tool in full (#893) ──")
lcalls = []


def spoke(m):
    lcalls.append(m[-1]["content"])
    lib._TURN_IO.outcomes.append({"tool": "speak", "ok": True, "write": False, "text": "Saved speech to /x.wav"})
    return "Here it is: “Dinner is at six.”"


LM = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Say this out loud: Dinner is at six."}]
lib._TURN_INFO.info = {}
ran()
got = lib._local_unbacked_retry("I've prepared the speech. Would you like me to play it back now?", LM, None, spoke)
check("the retry's backed reply is kept", got, "Here it is: “Dinner is at six.”")
check("…told what it claimed and to do it (the person asked)",
      "Your reply said you did this: read that aloud." in lcalls[-1] and "Do it now with speak" in lcalls[-1], True)
check("…with speak's full entry (the local prompt lists it in one line)", "**speak**" in lcalls[-1], True)
check("…and the local tool gate lets the first call run", lib._local_tool_gate("speak"), None)
check("…the turn log notes the retry and the fix", (lib._TURN_INFO.info.get("claim_fix_retry"),
      lib._TURN_INFO.info.get("claim_fix_fixed")), (1, 1))
lib._TURN_INFO.info = {}
ran()
got = lib._local_unbacked_retry("I've prepared the speech.", LM, None, lambda m: "I've prepared the speech again.")
check("a retry that claims again without the tool: the first reply goes on (and gets the Correction)", got,
      "I've prepared the speech.")
ran()
n = len(lcalls)
check("a tool this turn can't use: no retry",
      lib._local_unbacked_retry("I've prepared the speech.", LM, {"web_search"}, spoke) == "I've prepared the speech."
      and len(lcalls) == n, True)
ran("home_control")
lib._TURN_IO.outcomes[0]["write"] = True
lib._local_unbacked_retry("I've prepared the speech.", LM, None, spoke)
check("something else was already changed this turn: no retry", len(lcalls), n)
ran()
lib._local_unbacked_retry("I've corrected it.", LM, None, spoke)
check("an unnamed change (no tool to name): no retry", len(lcalls), n)
ran()
got = lib._local_unbacked_retry("I've prepared the speech.", LM, None, lambda m: "Dinner is at six.")
check("asked to speak, and the retry only prints the words (titan, 10-06: 2 of 3): the first reply goes on, "
      "so it gets the Correction", got, "I've prepared the speech.")
ran()
got = lib._local_unbacked_retry("I've prepared the speech.", [LM[0], {"role": "user", "content": "What did I say about dinner?"}],
                                None, lambda m: "You said dinner is at six.")
check("not asked: a retry that drops the claim is kept", got, "You said dinner is at six.")
lib._TURN_IO.outcomes = None

print("── through a whole turn ──")
seen = {}
real_inner = lib._run_agent_turn_inner


def inner(text):
    def run(messages, *a, **k):
        seen["messages"] = messages
        return text() if callable(text) else text
    return run


lib._run_agent_turn_inner = inner("I've added that to Claude's list.")
out = lib._run_agent_turn([{"role": "user", "content": "add it for Claude"}])
check("a turn whose reply claims an unmade change ends with the Correction",
      out.endswith("Correction: I didn't actually add that to Claude's list. Want me to?"), True)

lib._run_agent_turn_inner = inner(lambda: lib.execute_tool("add_todo", {"title": "Look at the Watch build", "for": "claude"}))
out = lib._run_agent_turn([{"role": "user", "content": "Tell me more about: Watch build failed"}])
check("“Tell me more”: no write runs", out.startswith("Not run: the person tapped “Tell me more”"), True)
check("…nothing on the list", "Watch build" in str(lib.execute_tool("list_todos", {"for": "claude"})), False)
out = lib._run_agent_turn([{"role": "user", "content": "Add looking at the Watch build to Claude's list"}])
check("an ordinary message still writes", out.startswith("Not run"), False)
check("…and read_only is reset after the turn", getattr(lib._TURN_IO, "read_only", False), False)

lib._run_agent_turn_inner = inner("ok")
lib._run_agent_turn([{"role": "user", "content": "Ava says she never got my text"}])
sysmsgs = [m for m in seen["messages"] if m.get("role") == "system" and m["content"].startswith("[DELIVERY CHECK]")]
check("“she never got my text”: what was sent is in front of the model, before the question",
      (len(sysmsgs), seen["messages"][-1]["content"]), (1, "Ava says she never got my text"))
check("…with the rule: no made-up cause", "don't guess a cause (a phone, a battery, a network)" in sysmsgs[0]["content"], True)
for q in ("Did Ava get it?", "my message didn't arrive", "the text never came through"):
    lib._run_agent_turn([{"role": "user", "content": q}])
    check(f"also: {q!r}", any(m.get("role") == "system" and m["content"].startswith("[DELIVERY CHECK]")
                               for m in seen["messages"]), True)
lib._run_agent_turn([{"role": "user", "content": "What's on my calendar today?"}])
check("not for other questions", any(m.get("role") == "system" and m["content"].startswith("[DELIVERY CHECK]")
                                     for m in seen["messages"]), False)
lib._run_agent_turn_inner = real_inner

print("── the Gemini request ──")
sent = {}
lib._gemini_call = lambda body, model=None: (sent.setdefault("bodies", []).append(dict(body)) or
                                             {"candidates": [{"content": {"parts": [{"text": "done"}]}}]})
lib._run_gemini_agent([{"role": "user", "content": "x"}], max_rounds=1, allowed_tools={"add_todo"}, force_tools=["add_todo"])
check("force_tools: the first request requires the tool",
      sent["bodies"][0].get("tool_config"), {"function_calling_config": {"mode": "ANY", "allowed_function_names": ["add_todo"]}})
sent.clear()
lib._run_gemini_agent([{"role": "user", "content": "x"}], max_rounds=1)
check("…without it, no tool_config", "tool_config" in sent["bodies"][0], False)

print("── a source cited but not read: once more (#953) ──")
GS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Were those other bills old?"}]
seen = []


def reread(m, tools):
    seen.append((tools, m[-1]["content"]))
    lib._TURN_IO.outcomes.append({"tool": "kb_retrieve", "ok": True, "write": False, "text": "two September notices"})
    return "Yes: the two September notices in your mail were old."


ran("find_records")
got = lib._gemini_source_retry("The emails confirm those other bills were old notices.", GS, None, reread)
check("the retry that read the mail is kept", got, "Yes: the two September notices in your mail were old.")
check("…offered the mail readers and what it used", {"kb_retrieve", "find_records"} <= seen[-1][0], True)
check("…told what it cited and what it used", ("your email" in seen[-1][1], "find_records" in seen[-1][1]), (True, True))
ran("find_records")
got = lib._gemini_source_retry("The emails confirm they were old.", GS, None, lambda m, t: "The emails show they were old.")
check("a retry that still cites unread mail: the first reply goes on (with its Correction)", got,
      "The emails confirm they were old.")
ran("find_records")
got = lib._gemini_source_retry("The emails confirm they were old.", GS, None,
                               lambda m, t: "Your records show both were billed in September.")
check("a retry that says only what it checked is kept", got, "Your records show both were billed in September.")
ran("kb_retrieve")
n = len(seen)
lib._gemini_source_retry("The emails confirm it.", GS, None, reread)
check("nothing to fix when the mail was read: no retry", len(seen), n)
ran("find_records", "add_todo")
n = len(seen)
lib._gemini_source_retry("The emails confirm it.", GS, None, reread)
check("never after a write this turn (_can_retry)", len(seen), n)

print("── a source the person named but the turn never read (#953 reopened) ──")
Q = "Check my email and my texts for anything from the dealer about the trade-in."
ran("search_messages")
got = lib._gemini_source_retry("Nothing from the dealer in your texts.", GS, None, reread, question=Q)
check("texts read, email skipped and unsaid: one retry, kept once it read the mail", got,
      "Yes: the two September notices in your mail were old.")
check("…told it was asked to check your email", "asked you to check your email" in seen[-1][1], True)
ran("search_messages")
got = lib._gemini_source_retry("Nothing from the dealer in your texts.", GS, None,
                               lambda m, t: "Nothing in your texts. I couldn't check your email just now.", question=Q)
check("a retry that says email wasn't checked is kept", got, "Nothing in your texts. I couldn't check your email just now.")
ran("search_messages")
got = lib._gemini_source_retry("Nothing from the dealer in your texts.", GS, None,
                               lambda m, t: "Still nothing in your texts.", question=Q)
check("a retry that's still silent about email: the first reply goes on (with its Correction)", got,
      "Nothing from the dealer in your texts.")
ran("search_messages")
n = len(seen)
lib._gemini_source_retry("Nothing in your texts, and I couldn't get to your email.", GS, None, reread, question=Q)
check("the reply already says email wasn't checked: no retry", len(seen), n)
ran("search_messages", "search_mail")
lib._gemini_source_retry("Nothing from the dealer in either.", GS, None, reread, question=Q)
check("both read: no retry", len(seen), n)
lib._TURN_IO.outcomes = None

print("── #1610: said done or happening now, with nothing behind it ──")
lib._TURN_IO.last_said = "We have the parent meeting Thursday at 4."
ran()
check("“I'll add that meeting to your calendar now”, no create call",
      whats("I'll add that meeting to your calendar now."), ["add that to the calendar"])
check("“Let me add it to Reminders right away”", whats("Let me add it to your Reminders right away."), ["add that reminder"])
check("“Adding that now.”", whats("Adding that now."), ["add that"])
check("“I'll file a ticket for it now” (#1614's wording, as a commitment)", whats("I'll file a ticket for it now."),
      ["file that ticket"])
check("“I've noted Ava is first”, nothing saved", whats("I've noted Ava is first."), ["save that"])
check("“I'll remember that”", whats("Got it. I'll remember that Leo has swim on Tuesdays."), ["save that"])
check("“Picked up the task and continuing it”", whats("Picked up the task and continuing it."),
      ["start or continue that task"])
check("“Started a task to set up Checkmk on titan”", whats("Started a task to set up Checkmk on titan."),
      ["start or continue that task"])
check("“I'm continuing the task”", whats("I'm continuing the task from yesterday."), ["start or continue that task"])
check("“I've filed a ticket” still the #1614 claim", whats("I've filed a ticket for that."), ["file that ticket"])
for text in ("I can add it to your calendar if you want.", "I'll add it to your calendar once you confirm the time.",
             "I'll add that to your calendar later.", "Want me to add it now?", "I won't add it now.",
             "Should I remember that?", "I haven't noted it.", "Robin started a task for the garden.",
             "You can start a task from the Tasks tab.", "Leo picked up the task of feeding the cat.",
             "Noted as paid, on your word: Lumen Power.", "Your calendar has the meeting at 4 now."):
    check(f"not a claim: {text!r}", whats(text), [])

print("── #1610: backed only by a call that worked ──")
ran("create_calendar_event")
check("create_calendar_event ran: “I'll add it to your calendar now” is backed",
      whats("I'll add that meeting to your calendar now."), [])
ran("create_calendar_event", ok=False)
check("…a failed create doesn't back it", whats("I'll add that meeting to your calendar now."), ["add that to the calendar"])
ran("set_fact")
check("set_fact ran: “I've noted Ava is first” is backed", whats("I've noted Ava is first."), [])
ran("ingest_text")
check("ingest_text ran: backed", whats("I'll remember that."), [])
ran("plan_task", ok=False)
check("plan_task refused (#1614's install rule): “Started a task …” isn't backed",
      whats("Started a task to set up Checkmk on titan."), ["start or continue that task"])
check("…and the Correction doesn't offer it again", lib._with_backed_claims("Started a task to set up Checkmk on titan."),
      "Started a task to set up Checkmk on titan.\n\nCorrection: I didn't actually start or continue that task; it "
      "didn't go through.")
ran("task_answer")
check("task_answer ran: “Picked up the task” is backed", whats("Picked up the task and continuing it."), [])
ran("task_status")
check("only reading the task (task_status) doesn't back it", whats("Picked up the task and continuing it."),
      ["start or continue that task"])
ran()
lib._TURN_IO.role = "admin"
lib._TURN_IO.last_said = "Remember that Ava goes first in the carpool this week."
check("the person said “remember …” (saved after the turn): “I've noted it” is backed", whats("I've noted that."), [])
lib._TURN_IO.last_said = "Ava goes first in the carpool this week."
check("…without “remember”, it isn't", whats("I've noted that."), ["save that"])
check("the Correction: one plain line, with an offer", lib._with_backed_claims("I've noted Ava is first."),
      "I've noted Ava is first.\n\nCorrection: I didn't actually save that. Want me to?")

print("── #1610: re-asked first (#1038/#1550), with the tool that would do it ──")
lib._TURN_INFO.info = {}
calls.clear()


def saved(m, tools, force):
    calls.append((tools, force, m[-1]["content"]))
    lib._TURN_IO.outcomes.append({"tool": "set_fact", "ok": True, "write": True, "text": "Recorded."})
    return "Saved: Ava goes first in the carpool this week."


GN = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Ava goes first in the carpool this week."}]
ran()
got = lib._gemini_claim_retry("I've noted Ava is first.", GN, None, saved)
check("Gemini: the retry's backed reply is kept", got, "Saved: Ava goes first in the carpool this week.")
check("…offered set_fact and ingest_text, not required (they didn't ask to save)", (calls[-1][0], calls[-1][1]),
      ({"set_fact", "ingest_text"}, None))
GC = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Add the porch light to my to-do list"}]
ran()
lib._gemini_claim_retry("I'll add that to your to-do list now.", GC, None, saved)
check("“I'll add … now” after a request to add: add_todo, required", (calls[-1][0], calls[-1][1]),
      ({"add_todo"}, ["add_todo"]))
ran("plan_task", ok=False)
n = len(calls)
lib._gemini_claim_retry("Started a task to set up Checkmk on titan.", GC, None, saved)
check("a refused plan_task isn't offered again; the other task tools are", "plan_task" in calls[-1][0] if len(calls) > n
      else None, False)
lcalls.clear()
ran()
got = lib._local_unbacked_retry("Picked up the task and continuing it.",
                                [{"role": "system", "content": "sys"}, {"role": "user", "content": "continue the task"}],
                                None, lambda m: (lcalls.append(m[-1]["content"]), "I haven't started it yet.")[1])
check("local: asked to continue, the re-ask names the task tools", "task_answer" in (lcalls[-1] if lcalls else ""), True)
check("…a retry that does nothing while asked: the first reply goes on (and gets the Correction)", got,
      "Picked up the task and continuing it.")
lib._TURN_IO.outcomes = None
lib._TURN_IO.last_said = None

print("── #1610: these claims wait for the end of the turn when streamed ──")
for text in ("I'll add that meeting to your calendar now.", "I've noted Ava is first.",
             "You're completely right, it's at 4."):
    check(f"held: {text!r}", lib.stream_stops(text), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
