#!/usr/bin/env python3
# gate: hermetic   (Gemini faked; every check's trigger stubbed; temp stores and turn log)
"""Post-answer re-asks capped per turn (#1550). After Gemini answers, each check that finds something re-asks it on
its own; 10-05..10-10 a one-tool canary turn stacked 6-8 Gemini calls. A turn now gets AGENT_POSTANSWER_MAX extra calls
(default 2), taken in priority order (claim, then source / no-access / required read, then notes, then the self-check
last). A skipped check's Correction line still goes on the reply. The canary's tool turn warns, still passing, when it
took more than 3 model calls or any re-ask or Correction fired. Fictional people.
Run: TZ=UTC python3 scripts/test_postanswer_budget.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-postanswer-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json",
             "KB_ACL_DB": "kb_acl.db", "CONNECTORS_LEDGER": "ledger.db", "AGENT_CANARY_LAST": "canary.json"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["AGENT_TOOL_BRAIN"] = "gemini"
os.environ.pop("AGENT_POSTANSWER_MAX", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402
import claim_checks  # noqa: E402
import effort  # noqa: E402
import required_reads  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


DRAFT = "Leo's recital is on Thursday, and I added a reminder for it."
QUESTION = "When is Leo's recital?"
READERS = {"search_messages", "list_calendar_events", "create_reminder", "kb_retrieve", "search_notes"}
FIRE: set[str] = set()   # which checks find something this turn
calls: list[str] = []

# What each re-ask's instruction starts with: how the fake model knows which pass it is.
KINDS = (("Your reply said you did this", "claim"), ("no tool read", "source"), ("you can't access", "no_access"),
         ("This question needs", "required_read"), ("The system checked your answer", "notes"),
         ("Before this reply goes", "self_check"))


def fake_gemini(messages, max_rounds=6, model=None, allowed_tools=None, force_tools=None):
    last = str(messages[-1].get("content") or "")
    kind = next((k for marker, k in KINDS if marker in last), "answer")
    calls.append(kind)
    lib._note_gemini_call(1.0)
    return DRAFT if kind == "answer" else f"(again, {kind})"


real = {"run": lib._run_gemini_agent, "unbacked": lib._unbacked_claims, "notes": lib._claim_notes,
        "decls": lib._gemini_function_declarations, "specs": lib._turn_tool_specs, "situ": lib._situational_context,
        "unread": claim_checks.unread_sources, "asked": claim_checks.asked_unread,
        "noacc": claim_checks.false_no_access, "needed": required_reads.needed, "decide": effort.decide}
lib._run_gemini_agent = fake_gemini
lib._situational_context = lambda query=None: "Now: Thursday"
lib._unbacked_claims = lambda text: [(("create_reminder",), "add a reminder for Leo's recital")] if "claim" in FIRE else []
lib._claim_notes = lambda text, question="": ["the calendar shows Leo's recital on Friday"] if "notes" in FIRE else []
lib._gemini_function_declarations = lambda allowed=None: [{"name": n} for n in sorted(READERS)]
lib._turn_tool_specs = lambda: {n: {"description": n, "parameters": {}} for n in READERS}
claim_checks.unread_sources = lambda text, reads: ([("notes", "your notes", ["search_notes"])]
                                                   if "source" in FIRE else [])
claim_checks.asked_unread = lambda question, text, reads: []
claim_checks.false_no_access = lambda text, declared, used: ([("messages", "search_messages")]
                                                             if "no_access" in FIRE else [])
required_reads.needed = lambda q, declared, used: ([("the calendar", "list_calendar_events")]
                                                   if "required_read" in FIRE else [])
effort.decide = lambda q, asked=None, hint=None: (("high", "test") if "self_check" in FIRE else ("normal", "test"))
lib._TURN_IO.brain = None


def turn(fire, budget=None):
    """One Gemini turn through _run_agent_turn_inner with these checks firing; the reply and its turn info."""
    FIRE.clear(); FIRE.update(fire)
    calls.clear()
    if budget is None:
        os.environ.pop("AGENT_POSTANSWER_MAX", None)
    else:
        os.environ["AGENT_POSTANSWER_MAX"] = budget
    lib._TURN_IO.outcomes, lib._TURN_IO.tools_used, lib._TURN_IO.deadline = [], set(), None
    lib._TURN_INFO.info = {"tool_names": ["kb_retrieve"]}
    out = lib._run_agent_turn_inner([{"role": "system", "content": "rules"}, {"role": "user", "content": QUESTION}],
                                    allowed_tools=set(READERS))
    return out, lib._TURN_INFO.info


ALL = ("claim", "source", "no_access", "required_read", "notes", "self_check")
try:
    print("── the budget (default 2) ──")
    out, info = turn({"claim", "source", "no_access", "notes"})
    check("four checks fire: the answer plus at most 2 re-asks, in priority order", calls, ["answer", "claim", "source"])
    check("…counted by kind in the turn info", (info.get("postanswer"), info.get("postanswer_skipped")),
          ({"claim": 1, "source": 1}, {"no_access": 1, "notes": 1}))
    check("…a skipped check doesn't log as a re-ask", (info.get("no_access_retry"), info.get("claim_retry")), (None, None))
    check("…the first reply stands (neither re-ask fixed it)", out, DRAFT)
    check("…gemini_calls says 3", info.get("gemini_calls"), 3)

    out, info = turn(set(ALL))
    check("all six fire: claim and source run; required read, notes and the self-check wait", calls,
          ["answer", "claim", "source"])
    check("…the self-check, last in line, logs 'budget'", info.get("self_check"), "budget")
    check("…every skipped kind is named",
          info.get("postanswer_skipped"), {"no_access": 1, "required_read": 1, "notes": 1, "self_check": 1})

    out, info = turn({"notes", "self_check"})
    check("lower-priority checks run when the higher ones don't fire", calls, ["answer", "notes", "self_check"])
    check("…nothing skipped", info.get("postanswer_skipped"), None)

    print("── notes still attached when a re-ask is skipped ──")
    out, info = turn({"claim", "source", "notes"})
    check("notes skipped (claim and source took the budget)", info.get("postanswer_skipped"), {"notes": 1})
    noted = lib._with_checked_answers(lib._with_backed_claims(out), QUESTION)
    check("the claim's Correction goes on", "Correction: I didn't actually add a reminder for Leo's recital" in noted, True)
    check("the notes' Correction goes on", "Correction: The calendar shows Leo's recital on Friday." in noted, True)
    src = Path(lib.__file__).read_text()
    check("…and the turn wraps the Gemini reply in both",
          src.index("_with_checked_answers(_with_true_weekdays") < src.index("_with_backed_claims(_with_honest_outcome")
          < src.index("_without_trailing_offer(_run_agent_turn_inner("), True)

    print("── the env ──")
    out, info = turn(set(ALL), budget="10")
    check("a budget above what fires: behaviour unchanged, every check re-asks once", calls, ["answer", *ALL])
    check("…nothing skipped, all counted", (info.get("postanswer_skipped"), sum(info["postanswer"].values())), (None, 6))
    out, info = turn(set(ALL), budget="0")
    check("AGENT_POSTANSWER_MAX=0: no re-asks at all", calls, ["answer"])
    out, info = turn(set(ALL), budget="lots")
    check("an unreadable value falls back to 2", calls, ["answer", "claim", "source"])
    out, info = turn(set(ALL), budget="")
    check("an empty value is the default 2", calls, ["answer", "claim", "source"])
    out, info = turn(set())
    check("nothing fires: one call, nothing recorded", (calls, info.get("postanswer"), info.get("postanswer_skipped")),
          (["answer"], None, None))

    print("── outside a budgeted turn: unchanged ──")
    lib._TURN_IO.postanswer = None
    check("a check run on its own always takes", [lib._postanswer_take("notes") for _ in range(5)], [True] * 5)
    check("the budget is cleared after the Gemini turn", getattr(lib._TURN_IO, "postanswer", None), None)
    FIRE.clear(); FIRE.add("no_access")
    lib._TURN_IO.outcomes, lib._TURN_IO.deadline = [], None
    lib._TURN_INFO.info = {}
    asked: list[int] = []
    for _ in range(4):
        lib._no_access_retry(DRAFT, [{"role": "user", "content": QUESTION}], lambda m: asked.append(1) or "x")
    check("…so the local brain's retries (and their own tests) are never capped", len(asked), 4)
    check("…and nothing is counted there", lib._TURN_INFO.info.get("postanswer"), None)

    print("── the turn log ──")
    lib._TURN_INFO.info = {"brain": "gemini", "postanswer": {"claim": 1, "source": 1}, "postanswer_skipped": {"notes": 1},
                           "claim_fix_retry": 1, "gemini_calls": 3}
    lib._log_turn(0.0, 5, None)
    rec = json.loads((_TMP / "turns.jsonl").read_text().splitlines()[-1])
    check("postanswer, postanswer_skipped and claim_fix_retry are logged",
          (rec.get("postanswer"), rec.get("postanswer_skipped"), rec.get("claim_fix_retry")),
          ({"claim": 1, "source": 1}, {"notes": 1}, 1))
finally:
    lib._run_gemini_agent, lib._unbacked_claims, lib._claim_notes = real["run"], real["unbacked"], real["notes"]
    lib._gemini_function_declarations, lib._turn_tool_specs = real["decls"], real["specs"]
    lib._situational_context = real["situ"]
    claim_checks.unread_sources, claim_checks.asked_unread = real["unread"], real["asked"]
    claim_checks.false_no_access, required_reads.needed, effort.decide = real["noacc"], real["needed"], real["decide"]
    lib._TURN_IO.outcomes = lib._TURN_IO.tools_used = lib._TURN_IO.postanswer = None
    lib._TURN_INFO.info = None
    os.environ.pop("AGENT_POSTANSWER_MAX", None)

print("── the canary ──")
import agent_canary  # noqa: E402
W = agent_canary.tool_turn_warning
check("a clean one-tool turn (2 calls): no warning", W({"brain": "gemini", "gemini_calls": 2}), None)
check("3 calls is still fine", W({"gemini_calls": 3}), None)
check("4 calls warns", W({"gemini_calls": 4}), "4 model calls")
check("a re-ask warns even under 3 calls, naming it", W({"gemini_calls": 2, "postanswer": {"source": 1}}),
      "2 model calls; re-asked: source")
check("the 10-05 shape: kinds re-asked and skipped, plus corrections",
      W({"gemini_calls": 7, "postanswer": {"claim": 1, "source": 1}, "postanswer_skipped": {"notes": 1},
         "answer_corrections": 1}),
      "7 model calls; re-asked: claim, source; over budget: notes; corrections: answer_corrections")
check("the older retry fields count too", W({"gemini_calls": 2, "claim_retry": 1, "degenerate_retries": 1}),
      "2 model calls; re-asked: degenerate, notes")
check("a self-check that ran counts; one skipped for time doesn't",
      (W({"gemini_calls": 2, "self_check": "ok"}), W({"gemini_calls": 1, "self_check": "skipped"})),
      ("2 model calls; re-asked: self_check", None))

real_turn, real_info = agent_canary.lib._run_agent_turn, agent_canary.lib.last_turn_info
try:
    agent_canary.lib._run_agent_turn = lambda msgs, **k: "10"
    agent_canary.lib.last_turn_info = lambda: {"brain": "gemini", "gemini_calls": 5, "postanswer": {"source": 1}}
    got = agent_canary.check_tool_turn()
    check("check_tool_turn: right answer, too many calls -> passing with a warning",
          got, ("brain=gemini", "5 model calls; re-asked: source"))
    agent_canary.lib.last_turn_info = lambda: {"brain": "gemini", "gemini_calls": 2}
    check("…a clean turn returns just its detail", agent_canary.check_tool_turn(), "brain=gemini")

    agent_canary.CHECKS = [("tool_turn", agent_canary.check_tool_turn)]
    agent_canary.LAST, agent_canary.HISTORY = str(_TMP / "canary.json"), str(_TMP / "canary.jsonl")
    agent_canary.lib.last_turn_info = lambda: {"brain": "gemini", "gemini_calls": 6, "postanswer": {"claim": 1, "notes": 1}}
    sys.argv = ["agent_canary.py", "--no-alert", "--only", "tool_turn"]
    rc = agent_canary.main()
    rec = json.loads((_TMP / "canary.json").read_text())
    check("main: a warned check stays ok, the run exits 0 and the status stays OK (the watchdog's problem list)",
          (rc, rec["status"], rec["checks"]["tool_turn"]["ok"]), (0, "OK", True))
    check("…the warning is recorded on the check and at the top",
          (rec["checks"]["tool_turn"].get("warn"), rec.get("warnings")),
          ("6 model calls; re-asked: claim, notes", {"tool_turn": "6 model calls; re-asked: claim, notes"}))
    agent_canary.lib._run_agent_turn = lambda msgs, **k: "nine"
    rc = agent_canary.main()
    rec = json.loads((_TMP / "canary.json").read_text())
    check("a wrong answer still fails, with no warning", (rc, rec["status"], "warn" in rec["checks"]["tool_turn"]),
          (1, "FAIL", False))
finally:
    agent_canary.lib._run_agent_turn, agent_canary.lib.last_turn_info = real_turn, real_info

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
