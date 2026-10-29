#!/usr/bin/env python3
"""
Unit tests for _classify_turn() — the heuristic pre-router used by AGENT_TOOL_BRAIN=auto.

Run: python3 scripts/test_brain_router.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from agent_runtime_lib import _classify_turn
import agent_runtime_lib as _lib
# Chat is all Gemini (#723): every turn answers "gemini". The rules still name the turns the local model could
# answer, in the route reason ("trivial-gemini", "jev-local-gemini"); "local" below means that rule matched.

def msg(role, content):
    return {"role": role, "content": content}


def check(label, messages, expected):
    _lib._TURN_INFO.route = {}
    brain = _classify_turn(messages)
    reason = str((getattr(_lib._TURN_INFO, "route", None) or {}).get("reason") or "")
    got = "local" if reason in ("trivial-gemini", "jev-local-gemini") else brain
    ok = got == expected and brain == "gemini"
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: rule={got!r} brain={brain!r} expected rule={expected!r}")
    return ok


def run():
    ok = True
    # The baseline rules must not depend on the host's live settings: with AGENT_JEV_ROUTER=1 (as in
    # titan's agent.env) ambiguous turns would go to the real Jev. run_jev() turns it on explicitly.
    os.environ["AGENT_JEV_ROUTER"] = "0"

    print("\n── Trivial turns → local ──")
    ok &= check("greeting hi",        [msg("user", "hi")],                        "local")
    ok &= check("greeting hello",     [msg("user", "Hello!")],                    "local")
    ok &= check("acknowledgment ok",  [msg("user", "ok")],                        "local")
    ok &= check("thanks",             [msg("user", "Thanks!")],                   "local")
    ok &= check("what time is it",    [msg("user", "What time is it?")],           "local")
    ok &= check("what day is it",     [msg("user", "what day is it")],             "local")
    ok &= check("pure math",          [msg("user", "2 + 2 * 8")],                 "local")
    ok &= check("how are you",        [msg("user", "how are you")],               "local")
    ok &= check("good morning",       [msg("user", "good morning")],              "local")
    ok &= check("tell me a joke",     [msg("user", "tell me a joke")],            "local")
    ok &= check("sounds good",        [msg("user", "sounds good")],               "local")

    print("\n── Short replies mid-thread → gemini (they need the thread) ──")
    offer = [msg("user", "What's on tomorrow?"), msg("assistant", "Piano at 3. Want me to add a reminder?")]
    ok &= check("'Yeah' answering an offer",   offer + [msg("user", "Yeah")],                        "gemini")
    ok &= check("'Yep'",                       offer + [msg("user", "Yep")],                         "gemini")
    ok &= check("'No'",                        offer + [msg("user", "no")],                          "gemini")
    ok &= check("a correction",                offer + [msg("user", "Don't do that again")],         "gemini")
    ok &= check("'the other one'",             offer + [msg("user", "No, the other one")],           "gemini")
    ok &= check("'thanks' closing is local",   offer + [msg("user", "Thanks!")],                     "local")
    ok &= check("'great' closing is local",    offer + [msg("user", "great")],                       "local")
    ok &= check("'ok' at the start is local",  [msg("user", "ok")],                                  "local")
    ok &= check("the MEMORY block isn't the user's words",
                offer + [msg("user", "[MEMORY — facts]\nx\n\nyep")],                                "gemini")

    print("\n── Hard turns → gemini ──")
    ok &= check("schedule meeting",   [msg("user", "Schedule a meeting tomorrow at 3pm")],     "gemini")
    ok &= check("create reminder",    [msg("user", "remind me to call the plumber at 9am")],   "gemini")
    ok &= check("find records",       [msg("user", "find all bills from last month")],          "gemini")
    ok &= check("search contacts",    [msg("user", "search my contacts for Joe")],              "gemini")
    ok &= check("plex playing",       [msg("user", "what's playing on plex")],                 "gemini")
    ok &= check("plex pause",         [msg("user", "pause plex")],                             "gemini")
    ok &= check("lights",             [msg("user", "turn off the lights")],                    "gemini")
    ok &= check("add record",         [msg("user", "add a new bill for $150")],                "gemini")
    ok &= check("delete contact",     [msg("user", "delete contact John Smith")],              "gemini")
    ok &= check("fetch url",          [msg("user", "fetch the content of this url")],          "gemini")
    ok &= check("calendar query",     [msg("user", "what's on my calendar today?")],           "gemini")
    ok &= check("any events",         [msg("user", "any events this week?")],                  "gemini")
    ok &= check("run shell",          [msg("user", "run a shell command to list files")],      "gemini")
    ok &= check("run script",         [msg("user", "run script deploy.sh")],                   "gemini")
    ok &= check("analyze document",   [msg("user", "analyze this invoice")],                   "gemini")
    ok &= check("edit file",          [msg("user", "edit the config file")],                   "gemini")

    print("\n── Ambiguous → gemini (conservative) ──")
    ok &= check("general question",   [msg("user", "what should I do this weekend?")],        "gemini")
    ok &= check("who is",             [msg("user", "who is my dentist?")],                    "gemini")
    ok &= check("empty",              [],                                                      "gemini")
    ok &= check("complex question",   [msg("user", "How does the agent brain work?")],        "gemini")

    print("\n── Memory recall prefix is ignored ──")
    mem = "[MEMORY — relevant facts about this user]\n• Schedules meetings on Mondays\n• Has a Plex server\n\n"
    ok &= check("recall + thanks",    [msg("user", mem + "thanks")],              "local")
    ok &= check("recall + lights",    [msg("user", mem + "turn off the lights")], "gemini")

    print("\n── Recent tool use → gemini (rule 1) ──")
    ok &= check("prior tool call",
        [
            msg("user", "find my bills"),
            msg("assistant", '<tool_call>{"tool": "find_records", "args": {}}</tool_call>'),
            msg("user", "thanks"),       # would be trivial on its own
        ],
        "gemini",
    )
    ok &= check("prior tool result",
        [
            msg("user", "what time is it"),   # trivial question
            msg("tool", "2026-07-08T14:32"),  # but tool result in recent history
        ],
        "gemini",
    )

    ok &= run_jev()

    print()
    if ok:
        print("All tests passed.")
        return 0
    else:
        print("Some tests FAILED.")
        return 1


class _FakeJev:
    """Stands in for jev_client: answers the brain questions from a canned table."""

    def __init__(self, choice, conf, tools, fail=False):
        self.choice, self.conf, self.tools, self.fail, self.calls = choice, conf, tools, fail, 0

    def decide(self, state, questions, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("network down")
        return {"answers": {"brain": {"type": "choice", "choice": self.choice, "confidence": self.conf},
                            "needs_tools": {"type": "noul", "noul": self.tools}},
                "_meta": {"latency_ms": 1, "cost_usd": 0.0}}


def run_jev():
    import agent_runtime_lib as lib
    ok = True
    ambiguous = [msg("user", "what should I do this weekend?")]
    orig = lib._jev_client
    os.environ["AGENT_JEV_ROUTER"] = "1"
    try:
        print("\n── Jev router (AGENT_JEV_ROUTER=1, rule 4) ──")
        cases = [
            ("confident local → still gemini (asked in shadow since #723)", _FakeJev("local", 0.9, 0.1), ambiguous, "gemini"),
            ("low confidence → gemini",       _FakeJev("local", 0.6, 0.1),    ambiguous, "gemini"),
            ("needs tools → gemini",          _FakeJev("local", 0.9, 0.6),    ambiguous, "gemini"),
            ("frontier → gemini",             _FakeJev("frontier", 0.95, 0.9), ambiguous, "gemini"),
            ("error → gemini",                _FakeJev("local", 0.9, 0.1, fail=True), ambiguous, "gemini"),
        ]
        for label, fake, msgs, expected in cases:
            lib._jev_client = lambda fake=fake: fake
            ok &= check(label, msgs, expected)
        fake = _FakeJev("local", 0.99, 0.0)
        lib._jev_client = lambda: fake
        ok &= check("hard pattern never asks Jev", [msg("user", "turn off the lights")], "gemini")
        ok &= check("trivial never asks Jev", [msg("user", "hi")], "local")
        # 10-01: Jev sent these to the local model as small talk; they need capabilities (live state)
        for q in ("What's new with you this week?", "What can you do?", "Who's in the household?",
                  "Is Home Assistant connected?", "Is my Mac running?", "Verify that for me"):
            ok &= check(f"titan's own state never asks Jev: {q!r}", [msg("user", q)], "gemini")
        if fake.calls:
            print(f"  [FAIL] Jev was called {fake.calls}x for hard/trivial turns")
            ok = False
        lib._jev_client = lambda: None
        ok &= check("no key → gemini", ambiguous, "gemini")
        os.environ["AGENT_JEV_ROUTER"] = "0"
        lib._jev_client = lambda: _FakeJev("local", 0.99, 0.0)
        ok &= check("router off → gemini", ambiguous, "gemini")
    finally:
        lib._jev_client = orig
        os.environ.pop("AGENT_JEV_ROUTER", None)
    return ok


if __name__ == "__main__":
    sys.exit(run())
