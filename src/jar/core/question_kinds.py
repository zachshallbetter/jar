#!/usr/bin/env python3
"""What the household asks, by kind, and how each kind gets answered (titan-node#378): which kinds come up
weekly and are answered from prose (a model reading notes, messages, the web or nothing) rather than from a
tool that works the facts out in code (mail_bills, schedule_lines, find_records…).

Counts only. The questions are read inside this process, on titan, sorted by keyword rules in code (no model,
nothing sent anywhere), and never printed: the output is kinds, counts, shares and tool names.

  python3 scripts/question_kinds.py [--days 7] [--json]          (on titan)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# First match wins: the more specific kinds come first.
KINDS: list[tuple[str, re.Pattern[str]]] = [(k, re.compile(rx, re.I)) for k, rx in [
    ("remember", r"\b(remember|note that|don'?t forget|keep in mind|save (this|that))\b"),
    ("bills", r"\b(bills?|pay(ment)?|paid|owe|invoice|receipt|charged?|subscription|refund|balance)\b|\$\d"),
    ("travel", r"\b(when (should|do) (i|we) leave|how (long|far)|traffic|directions|drive to|get to)\b"),
    ("reminders", r"\b(remind(er)?s?|to-?dos?|tasks?|overdue)\b"),
    ("calendar", r"\b(calendar|schedule|appointments?|meetings?|events?|what'?s on|am i (free|busy)|plans?)\b"),
    ("weather", r"\b(weather|rain|snow|temperature|forecast|cold|hot) ?(today|tomorrow|outside)?\b"),
    ("home", r"\b(lights?|lamp|thermostat|tv|television|fan|heat(er|ing)?|a/?c|speaker|music|play|volume)\b"),
    ("messages", r"\b(texts?|texted|messages?|emails?|mail|repl(y|ied)|said|told me)\b"),
    ("places", r"\b(where('?s| is| are| was)|address|located|location)\b"),
    ("people", r"\b(who('?s| is| are)|phone number|contact|birthday|how old)\b"),
    ("kids", r"\b(school|teacher|homework|practice|soccer|ballet|piano|pick ?up|drop ?off|recital)\b"),
    ("files", r"\b(files?|folders?|documents?|pdf|downloads?|desktop|notes?)\b"),
    ("titan", r"\b(titan|jellybean|claude|repo|deploy|build|server|pull request|pr|eval|model|gpu)\b"),
    ("time", r"\b(what time|what day|date|today|tomorrow|yesterday|how many days)\b"),
]]
PROSE_READS = {"kb_retrieve", "kb_list", "search_messages", "search_notes", "web_search", "fetch_url", "recall_events",
               "read_file", "file_read", "search_file_contents", "search_claude_chats", "search_mail", "view_image",
               "describe_image"}
REAL_SOURCES = {"user", None, ""}


def kind_of(text: str) -> str:
    for k, rx in KINDS:
        if rx.search(text or ""):
            return k
    return "other"


def _db_path() -> Path:
    try:
        import agent_memory
        return Path(agent_memory._DB_PATH)
    except Exception:  # noqa: BLE001
        return __import__("agent_paths").memory_db()


# "other" split by the LOCAL model (titan-node#378): a fixed label set, one word back, read and dropped in-process.
OTHER_LABELS = ("small_talk", "general_knowledge", "how_to", "writing", "opinion", "math", "shopping", "health",
                "entertainment", "feedback", "follow_up", "unclear")
_LABEL_PROMPT = ("Label the household member's message with exactly one of: " + ", ".join(OTHER_LABELS) + ". "
                 "follow_up = a reply that only makes sense after titan's last answer (\"yes\", \"the second one\"). "
                 "feedback = about titan or its answer. Reply with the label only.")


def local_labeller(endpoint: str = "http://127.0.0.1:8010") -> Any:
    """A labeller on titan's own model (never a hosted one): text → one of OTHER_LABELS, "unclear" when unsure."""
    import urllib.request

    def label(text: str) -> str:
        body = json.dumps({"messages": [{"role": "system", "content": _LABEL_PROMPT + " /no_think"},
                                        {"role": "user", "content": text[:600]}],
                           "temperature": 0, "max_tokens": 8}).encode()
        req = urllib.request.Request(f"{endpoint}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read())["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            return "unclear"
        word = re.sub(r"<think>.*?</think>", "", str(out), flags=re.S).strip().lower().split()
        return word[0].strip(".,") if word and word[0].strip(".,") in OTHER_LABELS else "unclear"
    return label


def questions(db: Path, since: float, split_other: Any = None) -> list[tuple[float, str, str]]:
    """(ts, conv_id, kind) for each user message since; the text is only classified here. With split_other (a
    labeller), "other" becomes "other:<label>"."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT ts, conv_id, content FROM messages WHERE role='user' AND ts >= ? ORDER BY ts",
                           (since,)).fetchall()
    finally:
        con.close()
    out = []
    for ts, conv, content in rows:
        text = re.sub(r"^\[MEMORY[^\n]*\n\n", "", str(content or ""))
        kind = kind_of(text)
        if kind == "other" and split_other is not None:
            kind = f"other:{split_other(text)}"
        out.append((float(ts), str(conv), kind))
    return out


def turns(path: Path, since: float) -> list[dict[str, Any]]:
    out = []
    try:
        with path.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= since and r.get("source") in REAL_SOURCES:
                    out.append(r)
    except OSError:
        pass
    return out


def report(qs: list[tuple[float, str, str]], ts: list[dict[str, Any]], days: float) -> dict[str, Any]:
    """Each question joined to the first real turn that ended within 3 minutes after it."""
    ts = sorted(ts, key=lambda r: r["ts"])
    used: set[int] = set()
    by: dict[str, dict[str, Any]] = {}
    for at, _conv, kind in qs:
        k = by.setdefault(kind, {"asked": 0, "joined": 0, "no_tool": 0, "prose": 0, "structured": 0, "tools": Counter()})
        k["asked"] += 1
        j = next((i for i, r in enumerate(ts) if i not in used and at <= r["ts"] <= at + 180), None)
        if j is None:
            continue
        used.add(j)
        names = set(ts[j].get("tool_names") or [])
        k["joined"] += 1
        k["tools"].update(names)
        if not names:
            k["no_tool"] += 1
        elif names & PROSE_READS and not names - PROSE_READS - {"log_event", "add_todo", "list_todos"}:
            k["prose"] += 1
        else:
            k["structured"] += 1
    out = {}
    for kind, k in sorted(by.items(), key=lambda x: -x[1]["asked"]):
        j = k["joined"] or 1
        out[kind] = {"asked": k["asked"], "per_week": round(k["asked"] * 7 / max(days, 1e-9), 1), "joined": k["joined"],
                     "no_tool_pct": round(100 * k["no_tool"] / j), "prose_pct": round(100 * k["prose"] / j),
                     "structured_pct": round(100 * k["structured"] / j), "top_tools": k["tools"].most_common(5)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--turn-log", type=Path, default=Path(os.environ.get("AGENT_TURN_LOG", "/ai/logs/agent-turns.jsonl")))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--split-other", action="store_true",
                    help="label the 'other' questions with titan's local model (counts only; on titan)")
    a = ap.parse_args()
    since = time.time() - a.days * 86400
    qs = questions(a.db or _db_path(), since, local_labeller() if a.split_other else None)
    tl = turns(a.turn_log, since)
    span = (time.time() - min((q[0] for q in qs), default=time.time())) / 86400
    rep = report(qs, tl, min(a.days, span) or a.days)
    if a.json:
        print(json.dumps({"days": round(min(a.days, span), 1), "questions": len(qs), "kinds": rep}, indent=1))
        return 0
    print(f"{len(qs)} questions over {min(a.days, span):.1f} days (counts only; no text)")
    print(f"{'kind':<11}{'asked':>6}{'/wk':>7}{'joined':>7}{'no tool':>8}{'prose':>7}{'code':>6}  top tools")
    for kind, k in rep.items():
        print(f"{kind:<11}{k['asked']:>6}{k['per_week']:>7}{k['joined']:>7}{k['no_tool_pct']:>7}%{k['prose_pct']:>6}%"
              f"{k['structured_pct']:>5}%  " + ", ".join(f"{t} {n}" for t, n in k["top_tools"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
