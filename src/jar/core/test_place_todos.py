#!/usr/bin/env python3
"""titan-node#284: a todo can be for a place ("when I'm at home"); arriving there, the person gets one card with
what's waiting: their own and the household's, never Claude's or anyone else's. Hermetic: temp stores.
# gate: hermetic
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
T = Path(tempfile.mkdtemp())
os.environ["TODOS_DB"] = str(T / "todos.db")
os.environ["PROACTIVE_DB"] = str(T / "proactive.db")
import continuity  # noqa: E402
continuity.emit = lambda *a, **k: None
import todos  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


a = todos.add("Water the basil", "robin", place="Home")
todos.add("Return the drill", "household", place="home")
todos.add("Buy stamps", "robin")                          # no place
todos.add("Pick up the order", "robin", place="Costco")
todos.add("Zach's errand at home", "zach", place="home")       # someone else's
todos.add("Fix the build", todos.CLAUDE, place="home")

check("the place is stored, cleaned", a.get("place") == "home", str(a))
w = todos.waiting_at("robin", "adult", "HOME")
check("arriving home: her own and the household's, oldest first",
      [t["title"] for t in w] == ["Water the basil", "Return the drill"], str([t["title"] for t in w]))
check("never someone else's or Claude's", not any(t["title"] in ("Zach's errand at home", "Fix the build") for t in w))
check("another place: only its own", [t["title"] for t in todos.waiting_at("robin", "adult", "costco")] == ["Pick up the order"])
check("a done todo stops waiting", todos.complete(a["id"]) and
      [t["title"] for t in todos.waiting_at("robin", "adult", "home")] == ["Return the drill"])
check("nowhere named: nothing", todos.waiting_at("robin", "adult", "") == [])

import proactive  # noqa: E402
sent = []
proactive.add_card = lambda *a, **k: sent.append((a, k)) or {}
import agent_api_server as srv  # noqa: E402
srv._place_todos_card("robin", "adult", "home")
check("one card, to her only, keyed by the place", len(sent) == 1 and sent[0][1]["to"] == ["robin"]
      and sent[0][1]["key"] == "place-todos-" + __import__("hashlib").sha1(b"home").hexdigest()[:12] and sent[0][0][0] == "At home: 1 thing waiting"
      and "Return the drill (household)" in sent[0][0][1], str(sent))
sent.clear()
srv._place_todos_card("robin", "adult", "the gym")
check("nothing waiting: no card", sent == [])

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed")
sys.exit(1 if failures else 0)
