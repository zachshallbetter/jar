#!/usr/bin/env python3
# gate: hermetic   (no stores written, nothing sent: the executor's own call is a stub; the reply fix on plain strings)
"""#1251, two box-brain misses fixed in code. "Remind everyone dinner is at 6" made a reminder only the asker sees:
the reminder is held and the model told to leave the household a card (notify_user), only when notify_user is the
asker's. And a routine said to text someone (F-14; a routine only shows a card) is put right in the reply when this
turn saved a routine and nothing that sends ran. Views x viewers: every role, the card only to the people they named.
Fictional people.
Run: python3 scripts/test_everyone_guard.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-everyone-"))
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "SCHEDULED_DB": "scheduled.db", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import everyone_guard as G  # noqa: E402
import household_identity as hi  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── what asks for everyone ──")
YES = {"Remind everyone dinner is at 6 tonight.": "Remind everyone", "remind the whole family about the dentist":
       "remind the whole family", "Remind all of us to take out the bins.": "Remind all of us",
       "Remind the kids to pack their bags.": "Remind the kids"}
check("each one", {q: G.everyone_words(q) for q in YES}, YES)
NO = ["Remind me dinner is at 6.", "Remind Ava to pack her bag.", "Tell everyone dinner is at 6.",
      "Everyone should be reminded? No, just me: remind me at 6.", "What did everyone say?"]
check("not these (a reminder for themselves or one person; 'tell everyone' is already a card)",
      {q: G.everyone_words(q) for q in NO}, {q: None for q in NO})

print("── in the executor ──")
import agent_runtime_lib as lib  # noqa: E402
io = lib._TURN_IO
real_exec, real_specs = lib._execute_tool, lib._turn_tool_specs
lib._execute_tool = lambda name, args: "STUB: ran"   # nothing real runs, whatever the guard decides


def run(tool, args, words, role="adult", jev_source=None):
    lib._turn_tool_specs = lambda: {n: {} for n in ("create_reminder", "add_todo", "notify_user")
                                    if role == "admin" or hi.role_allows_tool(role, n)}
    saved = {k: getattr(io, k, None) for k in ("everyone", "later", "jev_source", "role")}
    try:
        io.everyone, io.later, io.jev_source, io.role = words, None, jev_source, role
        return str(lib.execute_tool(tool, args))
    finally:
        for k, v in saved.items():
            setattr(io, k, v)


lib._TURN_INFO.info = {}
out = run("create_reminder", {"title": "Dinner at 6"}, "Remind everyone")
check("'remind everyone': the reminder doesn't run, and the model is told to card the household",
      (out.startswith("Not run: create_reminder reaches only the person asking"), "notify_user with to set to household"
       in out, "expires_in_min" in out), (True, True, True))
check("…counted in the turn log", lib._TURN_INFO.info.get("everyone_held"), 1)
check("…add_todo too", run("add_todo", {"title": "Dinner at 6"}, "Remind everyone").startswith("Not run:"), True)
print("── 'remind the kids': the asker's OWN kids, by name ──")
store = hi.HouseholdStore(_TMP / "people.db")
hi.reset_store(store)
robin = store.add_person("Robin", "adult")
jordan = store.add_person("Jordan", "adult")
ava, leo, hale = (store.add_person(n, "kid") for n in ("Ava", "Leo", "Hale"))
store.update_person(ava["id"], parents=[robin["id"]])
store.update_person(leo["id"], parents=[robin["id"]])
store.update_person(hale["id"], parents=[hi.ADMIN_PERSON_ID])


def kids_ask(person, role):
    saved = getattr(io, "person", None)
    io.person = {"id": person["id"], "name": person["name"], "role": role} if person else None
    try:
        return run("create_reminder", {"title": "Bags"}, "Remind the kids", role=role)
    finally:
        io.person = saved


out = kids_ask(robin, "adult")
check("Robin (Ava's and Leo's parent): the card goes to Ava and Leo by name, never Hale or the household",
      ("to set to Ava, Leo," in out, "Hale" in out, "household" in out), (True, False, False))
out = kids_ask(jordan, "adult")
check("Jordan, an adult who isn't any kid's parent: nobody named from the roster; told to ask whom they mean",
      (out.startswith("Not run:"), "notify_user" in out, any(n in out for n in ("Ava", "Leo", "Hale")),
       "Ask them in one line whom to remind" in out), (True, False, False, True))
out = kids_ask({"id": hi.ADMIN_PERSON_ID, "name": "Admin"}, "admin")
check("the admin: only their own kid (Hale), not every kid in the house",
      ("to set to Hale," in out, "Ava" in out or "Leo" in out), (True, False))
out = kids_ask(None, "household")
check("a shared screen has no kids of its own: nobody named", any(n in out for n in ("Ava", "Leo", "Hale")), False)
check("notify_user itself always runs", run("notify_user", {"title": "Dinner", "to": "household"}, "Remind everyone"),
      "STUB: ran")
check("no everyone words: the reminder runs as before", run("create_reminder", {"title": "x"}, None), "STUB: ran")
check("a background turn: never held (nobody asked)",
      run("create_reminder", {"title": "x"}, "Remind everyone", jev_source="proactive"), "STUB: ran")
check("views x viewers: every role that has notify_user is pointed to the card; nobody gains a tool",
      {role: run("create_reminder", {"title": "x"}, "Remind everyone", role=role).startswith("Not run:")
       for role in ("admin", "adult", "kid", "household")},
      {role: hi.role_allows_tool(role, "notify_user") or role == "admin" for role in ("admin", "adult", "kid", "household")})
lib._turn_tool_specs = lambda: {"create_reminder": {}}
saved = io.everyone
io.everyone = "Remind everyone"
check("no notify_user for this asker: the reminder runs as before (nothing to point to)",
      str(lib.execute_tool("create_reminder", {"title": "x"})), "STUB: ran")
io.everyone = saved
src = (HERE / "agent_runtime_lib.py").read_text()
check("set from the last user message, cleared at the turn's end, in the turn log",
      ("_TURN_IO.everyone = _eg.everyone_words(_users_words(" in src, "_TURN_IO.later = _TURN_IO.later_asked = _TURN_IO.everyone = _TURN_IO.repeat_send = None" in src,
       '"everyone_held"' in src), (True, True, True))

print("── a routine said to send a text ──")


def reply(text, outcomes):
    io.outcomes = outcomes
    lib._TURN_INFO.info = {}
    try:
        return lib._with_kept_promises(text)
    finally:
        io.outcomes = None


SAVED = [{"tool": "save_routine", "ok": True, "write": True, "text": "Saved routine “Ava ballet”."}]
got = reply("Done! I've set up a routine. Every weekday at 4:30 I'll text Ava to get ready for ballet.", SAVED)
check("saved a routine, said it will text: that sentence becomes the true one, with the offer to schedule it",
      got, "Done! I've set up a routine. " + lib._ROUTINE_TRUE + " Want me to schedule the text to send instead?")
check("…counted", lib._TURN_INFO.info.get("routine_text_fixed"), 1)
check("'the routine will message Ava' too",
      lib._ROUTINE_TRUE in reply("Saved. The routine will message Ava at 4:30 each weekday.", SAVED), True)
check("a text really scheduled (schedule_action ran): left as it is",
      reply("I'll text Ava at 4:30 every weekday.", SAVED + [{"tool": "schedule_action", "ok": True, "write": True,
                                                            "text": "Scheduled."}]), "I'll text Ava at 4:30 every weekday.")
check("no routine saved: left as it is (other checks own that)", reply("I'll text Ava now.", []), "I'll text Ava now.")
check("the routine saved, and the reply says what really happens: left as it is",
      reply("Saved: each weekday at 4:30 you'll get a card to text Ava.", SAVED),
      "Saved: each weekday at 4:30 you'll get a card to text Ava.")
check("a failed save: left as it is", reply("I'll text Ava at 4:30.", [{**SAVED[0], "ok": False}]), "I'll text Ava at 4:30.")
import turn_eval as te  # noqa: E402
fixed = reply("Done! Every weekday at 4:30 I'll text Ava to get ready for ballet.", SAVED)
check("turn_eval's routine-cant-text passes the fixed reply", te.c_routine_cant_text(te.Turn(text=fixed)), [])

lib._execute_tool, lib._turn_tool_specs = real_exec, real_specs
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
