#!/usr/bin/env python3
# gate: hermetic   (temp trace, memory, world and proactive stores; the local model is a fake)
"""#377 half 2: each person's knowledge misses → at most one proposal per miss, to that person only, through the daily
slot (#586's path) with Keep / Not useful; the judge reads only the asker's own notes; a proposal names the miss it
answers; Kept vs Not useful are counted for the weekly report. Fictional people (Alex and Robin, adults; Ava, a kid).
Run: python3 scripts/test_miss_proposals.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = Path(tempfile.mkdtemp(prefix="titan-missprop-"))
os.environ.update({"RETRIEVAL_TRACE_DB": str(_TMP / "t.db"), "HOUSEHOLD_DB": str(_TMP / "h.db"),
                   "KB_ACL_DB": str(_TMP / "a.db"), "AGENT_FENCES": "0", "PROACTIVE_DB": str(_TMP / "p.db"),
                   "MEMORY_DB": str(_TMP / "no-memory.db")})
import household_identity as hh  # noqa: E402
import miss_proposals as MP  # noqa: E402
import proactive  # noqa: E402
import retrieval_trace as T  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


class Clock:
    def __init__(self, iso):
        self.set(iso)

    def __call__(self):
        return self.t

    def set(self, iso):
        from zoneinfo import ZoneInfo
        self.t = datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(os.environ.get("AGENT_TZ", "America/Los_Angeles"))).timestamp()


clock = Clock("2026-10-07T12:00")
B = W.Bubbles(_TMP / "world", clock=clock)
W.get = lambda: B
ALEX, ROBIN, AVA = "p_alex", "p_robin", "p_ava"

# their own words, where they live: each person's conversation (the trace itself holds none)
mem_db = sqlite3.connect(":memory:", check_same_thread=False)
mem_db.execute("CREATE TABLE messages (conv_id TEXT, role TEXT, content TEXT, ts REAL)")
MEM = SimpleNamespace(_db=mem_db)


def miss(person, question, *, corrected=True):
    mem_db.execute("INSERT INTO messages VALUES (?,?,?,?)", (hh.conv_id_for(person), "user", question, time.time() - 0.01))
    time.sleep(0.03)            # each lookup reads the question just before it, as in a real conversation
    T.begin()
    T.query("search", "x", [] if not corrected else [{"source_id": "n-x"}], [])
    T.end(f"turn-{person}-{question[:6]}", person, [], corrected=2 if corrected else 0)
    time.sleep(0.03)


miss(ALEX, "when does the vet open on saturdays")
miss(ALEX, "what's the guest wifi again")
miss(ROBIN, "zzrobin when is my dentist")
miss(AVA, "what time is practice")
SOURCES = [{"source_id": "n-vet", "title": "Maple Animal Clinic", "source_type": "note"},
           {"source_id": "n-alex-car", "title": "Car service log", "source_type": "note"},
           {"source_id": "n-robin", "title": "zzrobin Pearl Dental", "source_type": "note"},
           {"source_id": "n-house", "title": "Wifi for guests", "source_type": "note"},
           {"source_id": "cx-mail-1", "title": "Clinic newsletter", "source_type": "connector:mail"}]
ACL = {"n-vet": (ALEX, "private"), "n-alex-car": (ALEX, "private"), "n-robin": (ROBIN, "private"),
       "n-house": (ROBIN, "household"), "cx-mail-1": (ALEX, "private")}
SEEN: dict[str, list[str]] = {}


def fake_llm(system, user, schema):
    """A stand-in for the local model: records what it was shown, answers by keyword."""
    q = user.split("\n", 1)[0]
    SEEN.setdefault(q, []).append(user)
    if "vet" in q:
        return {"kind": "alias", "note": "1", "text": "the vet"}
    if "wifi" in q:
        return {"kind": "note", "note": "", "text": "Guest wifi"}
    if "dentist" in q:
        return {"kind": "question", "note": "1", "text": "When is my dentist appointment?"}
    return {"kind": "none", "note": "", "text": ""}


PEOPLE = [{"id": ALEX, "role": "adult"}, {"id": ROBIN, "role": "adult"}, {"id": AVA, "role": "kid"}]
r = MP.run_all(apply=True, w=B, people=PEOPLE, sources=SOURCES, acl=ACL, mem=MEM, llm=fake_llm, text_of=lambda sid: "")
check("one proposal per miss the judge could answer (Ava's: none)", (r["proposals"], r["stored"]), (3, 3))


def rows(person):
    ww = B.world(W.bubble_name(person))
    return [dict(x) for x in ww.con.execute("SELECT * FROM note_reviews WHERE person=? AND kind LIKE 'miss_%' ORDER BY id", (person,))]


a = rows(ALEX)
check("Alex: an alias for his own note, and a note to write", sorted((x["kind"], x["source_id"], x["attr"]) for x in a),
      [("miss_alias", "n-vet", "the vet"), ("miss_note", "", "Guest wifi")])
check("…each names the miss it answers: its query id and his own words",
      all(x["entity"].startswith("q") and x["detail"] for x in a) and {x["detail"] for x in a} ==
      {"when does the vet open on saturdays", "what's the guest wifi again"}, True)
check("Robin: a likely question for her own note", [(x["kind"], x["source_id"]) for x in rows(ROBIN)], [("miss_question", "n-robin")])
check("per person: Robin's question is in no one else's file", any("zzrobin" in x["detail"] for x in rows(ALEX) + rows(AVA)), False)
check("…and the judge never saw another person's notes with Alex's question (only his own: the vet, the car log)",
      [("zzrobin" in u, "Wifi for guests" in u, "Clinic newsletter" in u) for u in SEEN["Question: when does the vet open on saturdays"]],
      [(False, False, False)])
check("a household note that isn't his (Robin's wifi note) isn't offered for him to edit",
      any("Wifi for guests" in u for q, us in SEEN.items() if "zzrobin" not in q for u in us), False)
check("a re-run proposes nothing new for misses already answered",
      MP.run_all(apply=True, w=B, people=PEOPLE, sources=SOURCES, acl=ACL, mem=MEM, llm=fake_llm, text_of=lambda sid: "")["stored"], 0)

print("── the daily slot (#586's path): to the person alone, Keep / Not useful ──")
cards = []
send = lambda title, detail, **kw: cards.append({"to": kw["to"], "title": title, "detail": detail, "actions": kw["actions"],
                                                 "key": kw.get("key")})
clock.set("2026-10-08T09:00")
W.daily_questions(B, [{"id": ALEX, "role": "adult"}], send=send)
check("one card, to Alex alone (his newest miss first)", [(c["to"], c["title"]) for c in cards],
      [([ALEX], "Write a note: “Guest wifi”?")])
check("…naming the miss in his own words", cards[0]["detail"], "You asked “what's the guest wifi again” and titan didn't find it.")
check("…Keep starts a turn he sees (marked so memory never learns from the tap); Not useful is a reply",
      [(x["id"], x["kind"], x["value"].startswith("(from a suggestion card)") if x["kind"] == "prompt" else x["value"][:6])
       for x in cards[0]["actions"]], [("keep", "prompt", True), ("notuseful", "reply", "missno")])
print("── Keep and Not useful are counted ──")
c = proactive.add_card(cards[0]["title"], cards[0]["detail"], source=W.ASK_SOURCE, to=[ALEX], key=cards[0]["key"],
                       actions=cards[0]["actions"])
proactive.card_action(c["id"], "keep", person=ALEX, role="adult")
check("Keep on the card marks it kept", [x["state"] for x in rows(ALEX) if x["kind"] == "miss_note"], ["kept"])
cards.clear()
clock.set("2026-10-09T09:00")
W.daily_questions(B, [{"id": ALEX, "role": "adult"}], send=send)
check("next day, the alias card", cards[0]["title"], "Find “Maple Animal Clinic” when you say “the vet”?")
nu = next(x for x in cards[0]["actions"] if x["id"] == "notuseful")["value"]
check("Robin can't answer Alex's", W.answer_ask(B, nu, ROBIN, "adult", [])[0], False)
check("Not useful: noted", W.answer_ask(B, nu, ALEX, "adult", []), (True, "OK, noted."))
check("the weekly counts, no person and no words", MP.counts(B, 7, now=clock()),
      {"made": 3, "kept": 1, "not_useful": 1, "open": 1, "held": 0})
check("the turn report prints the line", "knowledge-miss proposals" in (Path(MP.__file__).parent / "agent_turn_report.py").read_text(),
      True)
check("the dream job runs it, non-fatal", "-/usr/bin/python3 /ai/projects/titan-node/scripts/miss_proposals.py --apply" in
      (Path(MP.__file__).parent.parent / "server-setup" / "titan-reflection-dream.service").read_text(), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
