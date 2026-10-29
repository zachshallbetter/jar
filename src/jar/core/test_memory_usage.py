#!/usr/bin/env python3
# gate: hermetic
"""When titan last used a remembered fact (#1072): a real turn's recall stamps last_used and use_count on exactly the
facts it put in front of the model, for that person only; an eval, temporary, brain-pinned or canary-process turn never
does. GET /v1/memory/facts sorts by use and lists the unused. Fictional people (Robin, an adult; Ava, a kid); a
temporary memory; no model.
Run: python3 scripts/test_memory_usage.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ["AGENT_MEMORY_DIR"] = tempfile.mkdtemp(prefix="titan-mem-usage-")
os.environ["MEMORY_HYBRID"] = "0"
for k, v in {"HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "AGENT_TURN_LOG": "turns.jsonl", "WORLD_DIR": "world", "WORLD_DB": "world.db"}.items():
    os.environ[k] = os.path.join(os.environ["AGENT_MEMORY_DIR"], v)
os.environ.pop("AGENT_TURN_SOURCE", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_memory  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r} expected={expected!r}"))


mem = agent_memory.AgentMemory()
api._get_memory = lambda: mem
api._MEMORY_AVAILABLE = True
api._lib.world_drop_recalled = lambda text, q: text
ROBIN = hh.Identity(person={"id": "p_robin", "name": "Robin Hale", "role": "adult"}, device=None, role="adult", via="device")
AVA = hh.Identity(person={"id": "p_ava", "name": "Ava Hale", "role": "kid"}, device=None, role="kid", via="device")
# recall also brings each hit's neighbours by insertion order, so the facts the tests call unused sit far away
# (and BM25 keeps common words: the unused ones share no word with the questions asked)
FILL = [f"Gym locker {n} belongs to Robin." for n in range(11, 15)]
for text in ("Robin prefers oat milk.", "Robin drives a blue hatchback.", *FILL, "Robin's piano lesson is on Thursday at 4 PM.",
             *[f"Library card ending {n} belongs to Robin." for n in range(21, 25)]):
    mem.add_fact("chat:p_robin", text, 1.0, person_id="p_robin")
mem.add_fact("chat:p_ava", "Ava's piano lesson is on Monday at 5 PM.", 1.0, person_id="p_ava")


def turn(ident, question, **t):
    tid = f"t-{time.time_ns()}"
    api._turns[tid] = {"identity": ident, **t}
    api._build_messages(question, [], False, None, turn_id=tid)


def row(person, needle):
    return next(f for f in mem.list_facts(person_id=person) if needle in f["text"])


print("── a real turn stamps what it used ──")
check("before: never used", (row("p_robin", "piano")["last_used"], row("p_robin", "piano")["use_count"]), (None, 0))
turn(ROBIN, "When is my piano lesson?")
r = row("p_robin", "piano")
check("the fact the recall put in front of the model: stamped", (r["use_count"], r["last_used"] is not None), (1, True))
check("one it didn't use: untouched", (row("p_robin", "hatchback")["use_count"], row("p_robin", "oat milk")["use_count"]), (0, 0))
check("another person's fact with the same words: untouched", row("p_ava", "piano")["use_count"], 0)
turn(ROBIN, "Remind me about piano")
check("again: counted again", row("p_robin", "piano")["use_count"], 2)
turn(AVA, "When is my piano lesson?")
check("the kid's turn stamps the kid's own fact only", (row("p_ava", "piano")["use_count"], row("p_robin", "piano")["use_count"]),
      (1, 2))

print("── never from an eval, a temporary chat, a pinned brain or a canary ──")
before = row("p_robin", "piano")["use_count"]
turn(ROBIN, "When is my piano lesson?", source="eval")
turn(ROBIN, "When is my piano lesson?", source="temporary")
turn(ROBIN, "When is my piano lesson?", eval_brain="local")
os.environ["AGENT_TURN_SOURCE"] = "canary"
turn(ROBIN, "When is my piano lesson?")
os.environ.pop("AGENT_TURN_SOURCE")
check("eval, temporary, pinned and canary turns leave it as it was", row("p_robin", "piano")["use_count"], before)

print("── the list: sorted by use, and the unused ──")
by_use = mem.list_facts(person_id="p_robin", sort="use")
counts = [f["use_count"] for f in by_use]
check("sort=use: most used first, the piano fact in the top tier",
      (counts == sorted(counts, reverse=True), any("piano" in f["text"] for f in by_use if f["use_count"] == counts[0])),
      (True, True))
cut = time.time() + 1
unused = sorted(f["text"] for f in mem.list_facts(person_id="p_robin", unused_since=cut - 3600))
check("unused_since: never-used facts, not the one used just now",
      ("Robin prefers oat milk." in unused, any("piano" in t for t in unused)), (True, False))
check("tracking says when it began", isinstance(mem.usage_since(), float), True)
check("stamp_used is per person", mem.stamp_used(["Robin prefers oat milk."], person_id="p_ava"), 0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
