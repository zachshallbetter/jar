#!/usr/bin/env python3
"""#349: the weekly review's decisions are acted on. Facts kept (pinned) or forgotten (restorably), findings
confirmed into a ticket or struck (and not raised again), one card a week; recent_turns is content-free.
Hermetic: temp stores, fake memory and todos."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
tmp = Path(tempfile.mkdtemp())
os.environ["REVIEW_DB"] = str(tmp / "review.db")
import review_queue as rq  # noqa: E402
import turn_review  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


added = rq.add_week("2026-10-04", [{"text": "Titan promised to check back: 2 time(s)."}],
                    [{"id": "f-old", "text": "The user asked about the weather.", "suggest": "forget"},
                     {"id": "f-ava", "text": "Ava takes ballet on Tuesdays.", "suggest": "keep"}])
items = rq.pending()
check("a week's findings and facts are queued with ids", added == 3 and [i["id"] for i in items] == ["R1", "R2", "R3"]
      or added == 3, str(items))
listing = rq.describe(items)
check("the listing says what each decision does", "keep or forget" in listing and "confirm" in listing and "R1" in listing, listing)

forgotten, pinned, issues = [], [], []
kw = dict(forget=lambda f: forgotten.append(f) or True, pin=lambda f: pinned.append(f) or True,
          file_issue=lambda t: issues.append(t) or "ticket td-1")
ids = {i["text"][:20]: i["id"] for i in items}
fact_old = next(i["id"] for i in items if i["ref"] == "f-old")
fact_ava = next(i["id"] for i in items if i["ref"] == "f-ava")
finding = next(i["id"] for i in items if i["kind"] == "finding")
out = rq.decide([fact_old], "forget", "", **kw)
check("forget: the fact is forgotten (restorably)", forgotten == ["f-old"] and "forgotten (restorable)" in out[0], str(out))
out = rq.decide([fact_ava], "keep", "", **kw)
check("keep: the fact is pinned", pinned == ["f-ava"] and "kept (pinned)" in out[0], str(out))
out = rq.decide([fact_ava], "forget", "", **kw)
check("a decided item isn't decided twice", "already decided" in out[0] and forgotten == ["f-old"], str(out))
out = rq.decide([finding], "keep", "", **kw)
check("a finding can't be 'kept'", "takes confirm or strike" in out[0], str(out))
out = rq.decide([finding], "strike", "the scheduled action covered it", **kw)
check("strike: recorded with the reason, no ticket", "struck" in out[0] and not issues, str(out))
again = rq.add_week("2026-10-11", [{"text": "Titan promised to check back: 2 time(s)."},
                                    {"text": "A raw error reached a reply: 1 time(s)."}], [])
check("a struck finding isn't raised again; a new one is", again == 1 and any("raw error" in i["text"] for i in rq.pending()))
new = next(i["id"] for i in rq.pending() if "raw error" in i["text"])
out = rq.decide([new], "confirm", "", **kw)
check("confirm: it becomes a ticket on Claude's list", issues and issues[0].startswith("Weekly review: A raw error") and "ticket td-1" in out[0], str(out))
check("the kept share is the week's memory number", rq.kept_share("2026-10-04") == (1, 2))
check("a fact gone from memory is said so", "no longer in memory" in rq.decide(
    [rq.add_week("2026-10-11", [], [{"id": "f-gone", "text": "x y z", "suggest": "keep"}]) and next(
        i["id"] for i in rq.pending() if i["ref"] == "f-gone")], "keep", "", forget=lambda f: False, pin=lambda f: False,
    file_issue=lambda t: "")[0])
check("the store is private (600)", (os.stat(os.environ["REVIEW_DB"]).st_mode & 0o777) == 0o600)

# The weekly run queues and sends one card; nothing added, no card.
os.environ["REVIEW_DB"] = str(tmp / "review2.db")
cards = []
mem = {"filtered": [("f-a", "Asked about weather.")], "duplicate": [], "confirm": [("f-b", "Leo plays soccer.")]}
n = turn_review.queue_week(["You corrected titan right after a reply: 1 time(s)."], mem,
                           send=lambda *a, **k: cards.append((a, k)), db=tmp / "review2.db")
check("the weekly run queues and sends one card to the admins with a Review button",
      n == 3 and len(cards) == 1 and cards[0][1]["to"] == "admins" and cards[0][1]["actions"][0]["value"] == "Show this week's review"
      and "1 finding and 2 facts" in cards[0][0][0], str(cards))
cards.clear()
turn_review.queue_week(["You corrected titan right after a reply: 1 time(s)."], mem, send=lambda *a, **k: cards.append(1),
                       db=tmp / "review2.db")
check("a rerun adds nothing and sends no card", not cards)

# recent_turns: content-free.
log = tmp / "turns.jsonl"
now = 1_790_900_000.0
log.write_text("\n".join(json.dumps(t) for t in [
    {"ts": now - 60, "source": "user", "ms": 3000, "brain": "gemini", "error": None},
    {"ts": now - 120, "source": "user", "ms": 52000, "brain": "local", "error": "TimeoutError: timed out",
     "device_times": {"list_calendar_events": 14000}},
    {"ts": now - 90, "source": "eval", "ms": 1000, "brain": "gemini", "error": "EvalOnly"}]) + "\n")
r = turn_review.recent_summary(24, path=log, now=now)
check("recent_turns counts real turns only, with errors and slow tools", "2 turns" in r and "TimeoutError" in r
      and "EvalOnly" not in r and "list_calendar_events" in r, r)
check("recent_turns on a quiet day says so", "No real turns" in turn_review.recent_summary(1, path=log, now=now + 86400 * 3))

# The tools are admin-only (household_identity's default).
import household_identity as hh  # noqa: E402
check("the review tools and recent_turns are admin-only",
      all(hh.role_allows_tool("admin", t) and not hh.role_allows_tool("adult", t) and not hh.role_allows_tool("kid", t)
          for t in ("weekly_review", "review_decide", "recent_turns")))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all review-loop checks passed'}")
sys.exit(1 if failures else 0)
