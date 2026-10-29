#!/usr/bin/env python3
"""sessions.py (#300): one row per coding session across Claude Code, Codex, Warp and Antigravity, from
their own stores, metadata only: no message or prompt text ever reaches its output. Hermetic: fake
stores in a temp folder, no git or GitHub.

Run: python3 scripts/test_sessions.py
"""
# gate: hermetic
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sessions as ss  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


T = Path(tempfile.mkdtemp(prefix="titan-sessions-test-"))
NOW = time.time()
SECRET = "the alarm code is 4321"   # message text in every store: it must never come out
ss.CLAUDE, ss.CODEX = T / "claude", T / "codex"
ss.WARP, ss.AGY = T / "warp.sqlite", T / "agy.db"

p = ss.CLAUDE / "-Users-z-Projects-app"
p.mkdir(parents=True)
f = p / "sess-1.jsonl"
f.write_text("\n".join(json.dumps(r) for r in [
    {"type": "user", "cwd": "/nope/app", "gitBranch": "feature-x", "message": {"content": SECRET}},
    {"type": "assistant", "message": {"content": SECRET}},
    {"type": "last-prompt", "lastPrompt": SECRET},
    {"type": "custom-title", "customTitle": "Fix the login page"},
    {"type": "agent-name", "agentName": "Coordination"},
    {"type": "pr-link", "prNumber": 42, "prUrl": "https://github.com/o/app/pull/42", "prRepository": "o/app"},
]) + "\n")
old = p / "sess-old.jsonl"
old.write_text(json.dumps({"type": "user", "cwd": "/nope", "message": {"content": SECRET}}) + "\n")
os.utime(old, (NOW - 9 * 86400, NOW - 9 * 86400))
os.utime(f, (NOW - 30, NOW - 30))

c = ss.CODEX / "2026" / "10" / "01"
c.mkdir(parents=True)
(c / "rollout-1.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"id": "cx-1", "cwd": "/nope/site",
                                    "base_instructions": SECRET}}) + "\n" + json.dumps({"type": "message", "text": SECRET}) + "\n")
os.utime(c / "rollout-1.jsonl", (NOW - 7200, NOW - 7200))

con = sqlite3.connect(ss.WARP)
con.execute("CREATE TABLE agent_conversations (id, conversation_id, conversation_data, last_modified_at, summary)")
con.execute("INSERT INTO agent_conversations VALUES (1, 'w-1', ?, ?, ?)", (SECRET, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 120)),
            json.dumps({"title": "Analyze Disk Usage", "initial_query": SECRET, "initial_working_directory": "/nope/Downloads"})))
con.commit()
con.close()
con = sqlite3.connect(ss.AGY)
con.execute("CREATE TABLE conversation_summaries (conversation_id, title, preview, last_modified_time, workspace_uris, status, killed, raw_summary)")
con.execute("INSERT INTO conversation_summaries VALUES ('a-1', 'Refactor the parser', ?, ?, '[\"file:///nope/parser\"]', "
            "'CASCADE_RUN_STATUS_IDLE', 0, ?)", (SECRET, time.strftime("%Y-%m-%d %H:%M:%S.000000+00", time.gmtime(NOW - 3600)), SECRET))
con.commit()
con.close()

rows = ss.collect(NOW - 86400, list(ss.READERS), prs=False, now=NOW)
by = {r["tool"]: r for r in rows}
check("one row per tool in the window; the 9-day-old session left out", sorted(by), ["antigravity", "claude", "codex", "warp"])
check("no message, prompt or summary text anywhere", SECRET in json.dumps(rows), False)
check("Claude: its folder, branch, the name it was given and the PR it linked",
      (by["claude"]["project"], by["claude"]["branch"], by["claude"]["title"], by["claude"]["pr"]["number"],
       by["claude"]["pr"]["repo"], by["claude"]["state"]), ("app", "feature-x", "Coordination", 42, "o/app", "active"))
check("Codex: its id and folder from session_meta", (by["codex"]["id"], by["codex"]["project"]), ("cx-1", "site"))
check("Warp: the title and folder, never the first message", (by["warp"]["title"], by["warp"]["project"]),
      ("Analyze Disk Usage", "Downloads"))
check("Antigravity: its title, folder and own idle state", (by["antigravity"]["title"], by["antigravity"]["project"],
      by["antigravity"]["state"]), ("Refactor the parser", "parser", "idle"))
check("newest first", [r["tool"] for r in rows], ["claude", "warp", "antigravity", "codex"])
ss.AGY = T / "none.db"
check("a missing store is no rows, not an error", ss.antigravity(0), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
