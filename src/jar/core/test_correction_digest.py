#!/usr/bin/env python3
"""
Tests for the weekly corrections digest (scripts/correction_digest.py): finding corrections by rule,
pairing them with the turn log, the model's confirmation dropping non-corrections, draft tasks, the
digest and the card. Hermetic: messages, turns, the model and the card are faked.

Run: python3 scripts/test_correction_digest.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))

import correction_digest as cd  # noqa: E402

T0 = 1_790_800_000.0
MSGS = [   # (conv_id, role, content, ts): the real 09-30 shapes
    ("chat", "user", "What time is my SWAIA meeting tomorrow?", T0),
    ("chat", "assistant", "You don't have a SWAIA meeting tomorrow. You have Hale's appointment at 3:00 PM.", T0 + 5),
    ("chat", "user", "Those are both for Robin. Look back at my emails.", T0 + 40),
    ("chat", "assistant", "Your emails show a recurring Thursday meeting with Jamie.", T0 + 50),
    ("chat", "user", "Tell me more about that.", T0 + 100),
    ("chat", "assistant", "It's weekly at noon Mountain.", T0 + 105),
    ("chat", "user", "Tell me more about that.", T0 + 160),                        # a follow-up, not a do-over
    ("chat", "assistant", "Jamie Schulze organises it.", T0 + 165),
    ("chat", "user", "Add pickup Leo at 2:50 to the household calendar", T0 + 400),
    ("chat", "assistant", "That was declined.", T0 + 405),
    ("chat", "user", "Add pickup Leo at 2:50 to the household calendar\n\n(This needs a device tool — call it.)", T0 + 460),
    ("chat", "assistant", "Added.", T0 + 470),
    ("chat", "user", "Take a look at my recent Claude conversations.", T0 + 900),   # a new request
    ("chat", "assistant", "I can't read those.", T0 + 905),
    ("chat", "user", "Thanks, what's the weather?", T0 + 960),
]
TURNS = [{"ts": T0 + 6, "source": "user", "tool_names": ["list_calendar_events"], "client": "iphone",
          "brain": "gemini", "ms": 8020},
         {"ts": T0 + 406, "source": "user", "tool_names": ["create_calendar_event"], "client": "iphone", "ms": 3100}]


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def run():
    ok = True
    print("\n── only the admin's own conversation is read ──")
    import sqlite3
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "m.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE messages (id TEXT, conv_id TEXT, role TEXT, content TEXT, ts REAL)")
        con.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", [
            ("1", "chat", "user", "Zach's question", T0), ("2", "chat:robin", "user", "Robin's words", T0 + 1),
            ("3", "chat:leo", "user", "Leo's words", T0 + 2)])
        con.commit()
        con.close()
        ok &= check("Zach's conversation only", [m[2] for m in cd.load_messages(T0 - 10, db)], ["Zach's question"])

    print("\n── finding corrections by rule ──")
    FOLKS = ["Robin", "Leo", "Bee"]   # a fictional household: first names and an alias
    found = cd.candidates(MSGS, names=FOLKS)
    ok &= check("an outright correction and a do-over", [(c.why, c.correction[:25]) for c in found],
                [("pattern", "Those are both for Robin."), ("repeat", "Add pickup Leo at 2:50 to")])
    ok &= check("the do-over is the user's words (the device nudge dropped)", found[1].correction,
                "Add pickup Leo at 2:50 to the household calendar")
    def says(text, names=FOLKS):
        conv = [("c", "user", "What's on tomorrow?", T0), ("c", "assistant", "Your dentist at 9.", T0 + 5),
                ("c", "user", text, T0 + 30)]
        return bool(cd.candidates(conv, names=names))
    ok &= check("#733: it was a household member's, by any of their names",
                (says("Those are both Robin's."), says("that was Leo's"), says("It is Bee's")), (True, True, True))
    ok &= check("#733: someone outside the household, or no names known: not by this rule",
                (says("That was Jamie's"), says("That was Robin's", names=[])), (False, False))
    ok &= check("#733: no hard-coded name left", "morgan" in cd._CORRECTION_RX.pattern.lower(), False)
    ok &= check("each keeps what was asked and the reply", (found[0].asked[:20], found[0].reply[:24]),
                ("What time is my SWAI", "You don't have a SWAIA m"))
    for text, want in (("no, the other one", True), ("Why isn't it turned on?", True), ("It should be turned on. Double check.", True),
                       ("Stop asking me for permission", True), ("Take a look at my notes", False), ("Great, thanks", False)):
        ok &= check(f"rule: {text!r}", bool(cd._CORRECTION_RX.search(text)), want)

    print("\n── the turn it corrected ──")
    cd.attach_turns(found, TURNS)
    ok &= check("tools, client and time from the turn log", found[0].turn,
                {"tool_names": ["list_calendar_events"], "client": "iphone", "brain": "gemini", "ms": 8020})

    print("\n── the model confirms, explains and drafts a task ──")
    verdicts = {"Those are both for Robin. Look back at my emails.":
                {"correction": True, "what_went_wrong": "Listed Robin's events as Zach's and skipped his email.",
                 "task": {"question": "What time is my SWAIA meeting tomorrow?",
                          "expect": {"tools_any": ["search_mail"], "reply_none": ["hale"], "bogus": ["x"]}}},
                "Add pickup Leo at 2:50 to the household calendar": {"correction": False}}
    sent = []
    with tempfile.TemporaryDirectory() as d:
        r = cd.run(msgs=MSGS, turns=TURNS, confirm=lambda c: verdicts.get(c.correction),
                   send=lambda title, detail, **kw: sent.append((title, detail, kw)) or {"id": "c"}, out=Path(d))
        ok &= check("the model's 'not a correction' drops one", (r["candidates"], r["corrections"]), (2, 1))
        tasks = [json.loads(l) for l in (Path(d) / f"{r['week']}-tasks.jsonl").read_text().splitlines()]
        ok &= check("a draft task, marked draft, unknown checks dropped",
                    (tasks[0]["draft"], tasks[0]["expect"]), (True, {"tools_any": ["search_mail"], "reply_none": ["hale"]}))
        md = (Path(d) / f"{r['week']}.md").read_text()
        ok &= check("the digest says what went wrong and what ran",
                    ("Listed Robin's events as Zach's" in md, "**Ran:** list_calendar_events (iphone, gemini, 8s)" in md),
                    (True, True))
        ok &= check("one card, keyed by week, quiet", (len(sent), sent[0][2]["key"], sent[0][2]["urgency"]),
                    (1, f"corrections-{r['week']}", 0))
        ok &= check("the card leads with the correction", "Listed Robin's events" in sent[0][1], True)
    sent.clear()
    r = cd.run(msgs=MSGS[:2], turns=[], confirm=lambda c: None, send=lambda *a, **k: sent.append(1), dry_run=True)
    ok &= check("nothing found, dry run: nothing sent", (r["corrections"], sent), (0, []))
    r = cd.run(msgs=MSGS, turns=TURNS, confirm=lambda c: None, dry_run=True)
    ok &= check("no model verdict: the rule stands, repeats listed apart",
                (r["corrections"], "## Asked again" in r["digest"], "## Corrections" in r["digest"]), (2, True, True))

    # corrections of a fact titan holds become proposals with an ask (#358): only with the world model on
    import world
    seen = []
    real = (world.get, world.from_correction)
    world.get = lambda: "W"
    world.from_correction = lambda w, p, r, folks, reply, corr, **kw: seen.append((p, r, reply, corr)) or [("household:x", "phone", "proposed")]
    try:
        kept = cd.candidates(MSGS, names=FOLKS)
        ok &= check("forced: each correction goes to the world model as the admin's",
                    (cd.propose_claims(kept, force=True), {(p, r) for p, r, *_ in seen}), (len(kept), {("zach", "admin")}))
        seen.clear()
        os.environ.pop("AGENT_WORLD_MODEL", None)
        ok &= check("world model off: nothing proposed", (cd.propose_claims(kept), seen), (0, []))
    finally:
        world.get, world.from_correction = real

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
