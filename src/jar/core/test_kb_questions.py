#!/usr/bin/env python3
# gate: hermetic
"""Likely questions for system notes, as an extra chunk (#371): only system-zone notes get them; the note's own text
is re-sent unchanged (so the runtime replaces only its extras); questions are made only when a note's text changed;
a dry run calls nothing; a failed answer is tried again next run; never a hosted model; the cache is private and
drops notes that are gone; the timer does nothing until KB_QUESTIONS=apply. The runtime and the model are fakes.
Run: python3 scripts/test_kb_questions.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kbq-"))
os.environ["KB_QUESTIONS_CACHE"] = str(_TMP / "kbq.json")
os.environ.pop("KB_QUESTIONS", None)
sys.path.insert(0, str(Path(__file__).parent))
import kb_questions as Q  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


# a fake runtime: sources with body chunks; an index POST with extra_chunks replaces only the extras when the text is
# the same (state#19's rule), and is recorded
KB = {"note-runbook-backups": {"title": "Backups", "md": {"origin": "kb_seed:runbook", "domain": "titan"},
                               "text": "Backups run nightly at 02:15.\n\nRestore with restore.sh."},
      "note-robin-1": {"title": "Packing list", "md": {"owner": "p_robin"}, "text": "Sunscreen, two towels."},
      "cx-mail-1": {"title": "Your order", "md": {"source": "mail"}, "text": "Order shipped."}}
POSTS = []


def call(service, path, method="GET", payload=None, timeout=30):
    if method == "GET":
        sid = path.split("/sources/")[1].split("/")[0]
        n = KB.get(sid)
        paras = n["text"].split("\n\n") if n else []
        return {"chunks": [{"chunk_index": i, "chunk_text": p, "metadata": n["md"]} for i, p in enumerate(paras)]}
    POSTS.append(payload)
    return {"extra_chunk_count": len(payload.get("extra_chunks") or [])}


ASKED = []


def ask(text):
    ASKED.append(text)
    return ["When do backups run?", "How do I restore a backup?", "Where are the backups kept?"]


SOURCES = [{"source_id": k, "title": v["title"], "source_type": "knowledge_record" if k.startswith("note-runbook") else "note",
            "updated_at": "2026-10-01T00:00:00Z"} for k, v in KB.items()]

print("── dry run, then apply ──")
check("a dry run: counts only, no model, no writes", (Q.refresh(False, sources=SOURCES, call=call, ask=ask), ASKED, POSTS),
      ({"system": 1, "unchanged": 0, "made": 0, "sent": 0, "failed": 0, "would_make": 1}, [], []))
got = Q.refresh(True, sources=SOURCES, call=call, ask=ask)
check("apply: only the system note gets questions", (got["made"], got["sent"], [p["source_id"] for p in POSTS]),
      (1, 1, ["note-runbook-backups"]))
p = POSTS[0]
check("…its own text re-sent unchanged (so the runtime replaces only the extras), with its metadata and stamp",
      (p["text"], p["metadata"], p["source_updated_at"]), (KB["note-runbook-backups"]["text"], KB["note-runbook-backups"]["md"],
                                                            "2026-10-01T00:00:00Z"))
check("…and one extra chunk: its title over its questions, as the A/B measured",
      p["extra_chunks"], ["Backups\nWhen do backups run?\nHow do I restore a backup?\nWhere are the backups kept?"])
check("the cache is private (0600)", oct(Path(os.environ["KB_QUESTIONS_CACHE"]).stat().st_mode & 0o777), "0o600")

print("── only what changed ──")
POSTS.clear()
ASKED.clear()
check("run again: nothing changed, nothing asked or sent", (Q.refresh(True, sources=SOURCES, call=call, ask=ask)["unchanged"],
                                                           ASKED, POSTS), (1, [], []))
KB["note-runbook-backups"]["text"] += "\n\nThey're kept 14 days."
got = Q.refresh(True, sources=SOURCES, call=call, ask=ask)
check("the note changed: new questions, sent once", (got["made"], got["sent"], len(ASKED)), (1, 1, 1))
POSTS.clear()
got = Q.refresh(True, sources=SOURCES, call=call, ask=lambda t: [])
KB["note-runbook-backups"]["text"] += "\n\nAnd on Sundays too."
got = Q.refresh(True, sources=SOURCES, call=call, ask=lambda t: [])
check("no questions from the model: nothing sent, tried again next run", (got["failed"], POSTS), (1, []))
got = Q.refresh(True, sources=SOURCES, call=call, ask=ask)
check("…and the next run makes them", (got["made"], got["sent"]), (1, 1))
Q.refresh(True, sources=[s for s in SOURCES if s["source_id"] != "note-runbook-backups"], call=call, ask=ask)
check("a note that's gone takes its questions with it", "note-runbook-backups" in json.loads(
    Path(os.environ["KB_QUESTIONS_CACHE"]).read_text()), False)

print("── local model only, and the switch ──")
try:
    Q.local_questions("x", url="https://api.example.com")
    check("never a hosted model", "asked", "refused")
except ValueError:
    check("never a hosted model", "refused", "refused")
import io  # noqa: E402
import contextlib  # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    Q.main(["refresh", "--apply"])
check("the timer does nothing until KB_QUESTIONS=apply", json.loads(buf.getvalue()), {"skipped": "KB_QUESTIONS is not 'apply'"})
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
