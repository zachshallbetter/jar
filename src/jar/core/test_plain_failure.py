#!/usr/bin/env python3
"""Evening goal #4: a failed turn tells the person what happened in plain words, with a way out,
never "HTTPError: HTTP Error 400: Bad Request" (09-30: twelve times in a row on the phone). Hermetic."""
from __future__ import annotations

import io
import re
import sys
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_runtime_lib as lib  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def http(code: int, msg: str = "x") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://model", code, msg, {}, io.BytesIO(b""))


CASES = [
    ("timeout", TimeoutError("timed out"), "too long"),
    ("turn budget", lib.TurnBudgetExceeded("budget"), "too long"),
    ("a wrapped timeout", RuntimeError("TimeoutError: timed out"), "too long"),
    ("a context overflow", RuntimeError("HTTP Error 400: request (16612 tokens) exceeds the available context size"), "grown too long"),
    ("a bad request", http(400, "Bad Request"), "couldn't take the request"),
    ("rate limited", http(429, "Too Many Requests"), "Try again in a minute"),
    ("the model's service down", http(503, "Service Unavailable"), "having trouble"),
    ("a key problem", http(401, "Unauthorized"), "asking again won't help"),
    ("a broken pipe", BrokenPipeError(32, "Broken pipe"), "lost the connection"),
    ("a refused connection", urllib.error.URLError("Connection refused"), "lost the connection"),
    ("anything else", KeyError("brain"), "Something broke on my side"),
]
RAW = re.compile(r"HTTP|Error\b|Exception|Traceback|errno|\b\d{3}\b|[A-Z][a-z]+[A-Z]\w+")   # codes, class names
WAY_OUT = re.compile(r"Ask again|Try again|Start a new chat|asking again won't help", re.I)
for name, exc, want in CASES:
    msg = lib.plain_failure(exc)
    check(f"{name}: says what happened", want in msg, msg)
    check(f"{name}: no raw error", not RAW.search(msg), msg)
    check(f"{name}: offers a way out", bool(WAY_OUT.search(msg)), msg)

# jellybean#162: the way out as buttons, matching the words
WANT = {"timeout": ["retry"], "turn budget": ["retry"], "a wrapped timeout": ["retry"],
        "a context overflow": ["new_chat", "retry"], "a bad request": ["retry", "new_chat"], "rate limited": ["retry"],
        "the model's service down": ["retry"], "a key problem": [], "a broken pipe": ["retry"],
        "a refused connection": ["retry"], "anything else": ["retry"]}
for name, exc, _ in CASES:
    kinds = [a["kind"] for a in lib.failure_actions(exc)]
    check(f"{name}: buttons {WANT[name]}", kinds == WANT[name], str(kinds))
    words = lib.plain_failure(exc)
    if "new_chat" in kinds:
        check(f"{name}: the words offer a new chat too", "new chat" in words.lower(), words)
check("labels the apps show", [a["label"] for a in lib.failure_actions(http(400, "Bad Request"))] == ["Ask again", "New chat"])

src = (HERE / "agent_api_server.py").read_text()
check("the SSE error carries the actions", '"actions": error_actions' in src and 'actions=_lib.failure_actions(exc)' in src)
check("the phone and Mac see the plain message (SSE error)", 'error = turn.get("message") or turn.get("error")' in src)
check("the raw error stays in the turn record", 'error=str(exc), message=_lib.plain_failure(exc)' in src)
check("the history line is plain too", "(This reply didn't go through: {_lib.plain_failure(exc)})" in src)
check("the non-streaming answer is plain", "detail=_lib.plain_failure(exc)) from exc" in src)

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all plain-failure checks passed'}")
sys.exit(1 if failures else 0)
