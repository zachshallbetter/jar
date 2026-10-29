#!/usr/bin/env python3
"""turn_review.py (goal items 4 and 5): real turns only, the right signals, other people's facts
never shown, the draft private. Hermetic: temp files only."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


T0 = 1_790_800_000.0
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    turns = [
        {"ts": T0 + 10, "source": "user", "ms": 4000, "brain": "gemini", "error": None},
        {"ts": T0 + 100, "source": "user", "ms": 45000, "brain": "gemini", "error": "TimeoutError: timed out"},
        {"ts": T0 + 200, "source": "user", "ms": 3000, "brain": "gemini", "error": None},
        {"ts": T0 + 300, "source": "user", "ms": 3000, "brain": "gemini", "error": None},
        {"ts": T0 + 400, "source": "user", "ms": 3000, "brain": "gemini", "error": None},
        {"ts": T0 + 500, "source": "eval", "ms": 3000, "brain": "gemini", "error": "EvalOnly: boom"},
        {"ts": T0 + 600, "source": "user", "ms": 3000, "brain": "gemini", "error": None},
        {"ts": T0 + 700, "source": "user", "ms": 3000, "brain": "gemini", "error": None},
        {"ts": T0 + 800, "source": "user", "ms": 3000, "brain": "gemini", "error": None, "tool_names": ["schedule_action"]},
    ]
    (d / "turns.jsonl").write_text("\n".join(json.dumps(t) for t in turns) + "\n")
    db = sqlite3.connect(d / "titan_memory.db")
    db.execute("CREATE TABLE messages (id TEXT, conv_id TEXT, role TEXT, content TEXT, ts REAL)")
    db.execute("CREATE TABLE facts (id TEXT, conv_id TEXT, text TEXT, confidence REAL, ts REAL, pinned INT, person_id TEXT)")
    pdf = "What's in this file?  [Attached PDF “{}” (1 page), on this iPhone at attachments/{}.pdf. Use view_image with that path and a page number to see a page.]\nIts text:\nx"
    msgs = [
        ("chat", "user", "Play the White Stripes", T0 + 5), ("chat", "assistant", "Playing it now.", T0 + 10),
        ("chat", "user", "No, it isn't playing", T0 + 95), ("chat", "assistant", "(This reply didn't go through: TimeoutError: timed out)", T0 + 100),
        ("chat", "user", pdf.format("Aug", "a"), T0 + 195), ("chat", "assistant", "A bill.", T0 + 200),
        ("chat", "user", pdf.format("Jul", "b"), T0 + 295), ("chat", "assistant", "I'll let you know if anything comes up.", T0 + 300),
        ("chat", "user", "Anything on Friday?", T0 + 390), ("chat", "assistant", "Nothing.", T0 + 400),
        ("chat", "user", "Anything on Friday?", T0 + 395),
        ("e", "user", "eval question", T0 + 495), ("e", "assistant", "Correction: eval only", T0 + 500),
        ("chat:p_robin", "assistant", "Your sister's party is Saturday.", T0 + 600),
        ("chat:p_robin", "user", "No, my sister's party is Sunday", T0 + 650),
        ("chat:p_robin", "assistant", "Got it, Sunday.", T0 + 700),
        ("chat", "user", "remind me in an hour", T0 + 795), ("chat", "assistant", "Done. I'll remind you later at 10.", T0 + 800),
    ]
    db.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", [(f"m{i}", *m) for i, m in enumerate(msgs)])
    facts = [("f1", "Ava takes ballet on Tuesdays at Adams St.", "zach"), ("f2", "The user asked about the weather.", None),
             ("f3", "Robin's private note about her sister.", "p_robin"), ("f4", "Ava takes ballet on Tuesdays at Adams St.", None)]
    db.executemany("INSERT INTO facts VALUES (?,?,?,1.0,?,0,?)", [(f, "c", t, T0 - i, p) for i, (f, t, p) in enumerate(facts)])
    db.commit()
    env = {**os.environ, "AGENT_TURN_LOG": str(d / "turns.jsonl"), "AGENT_MEMORY_DIR": str(d),
           "REVIEW_DB": str(d / "review.db"), "PROACTIVE_DB": str(d / "proactive.db"),
           "TURN_REVIEW_LOG": str(d / "review.jsonl")}
    p = subprocess.run([sys.executable, str(HERE / "turn_review.py"), "--days", "100000", "--out", str(d / "out")],
                       capture_output=True, text=True, env=env, timeout=120)
    check("runs", p.returncode == 0, p.stderr[-400:])
    md_files = list((d / "out").glob("turn-review-*.md"))
    md = md_files[0].read_text() if md_files else ""
    summary = json.loads((d / "review.jsonl").read_text().splitlines()[-1]) if (d / "review.jsonl").exists() else {}
    check("only real turns are counted", summary.get("turns") == 8 and "EvalOnly" not in md, str(summary))
    check("a turn error is a candidate", "TimeoutError: timed out" in md)
    check("your correction is caught and quoted", "No, it isn't playing" in md)
    check("another person's correction is counted, never quoted",
          summary.get("corrections") == 2 and "sister" not in md and "other people's conversations" in md, str(summary))
    check("the same question twice is a repeat; one question about two files isn't", summary.get("repeats") == 1, str(summary))
    check("a promise to check back is caught; one backed by a scheduled action isn't", summary.get("promises") == 1, str(summary))
    check("a raw error in a reply is caught", summary.get("raw_errors") == 1)
    check("an eval's Correction line isn't counted", summary.get("claim_corrections") == 0)
    check("another person's fact is never shown", "Robin" not in md and "belong to other people" in md)
    check("the memory sample flags a non-fact and a repeat",
          summary.get("facts_filtered") == 1 and summary.get("facts_duplicate") == 1 and summary.get("facts_to_confirm") == 1,
          str(summary))
    check("the draft is private (600)", bool(md_files) and (md_files[0].stat().st_mode & 0o777) == 0o600)

# Slow tools from the turn log's list form, less the person's time (#619): asked_ms when the app says, else
# confirm_ms off the slowest call (an older app).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import turn_review  # noqa: E402
_tt = turn_review._tool_times
check("device_times as the turn log writes it, less asked_ms",
      _tt({"device_times": [["list_reminders", 103], ["create_reminder", 43696, 43000]], "confirm_ms": 43696})
      == [("list_reminders", 103.0), ("create_reminder", 696.0)])
check("an older app: the write's confirm_ms comes off its slowest call",
      _tt({"device_times": [["list_reminders", 103], ["create_reminder", 43696]], "confirm_ms": 43696})
      == [("list_reminders", 103.0), ("create_reminder", 0.0)])

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all turn-review checks passed'}")
sys.exit(1 if failures else 0)
