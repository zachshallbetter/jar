#!/usr/bin/env python3
# gate: hermetic
"""A group for every remembered fact (#1073): a household member's name groups it under them (the roster, as
chat_links matches people), the local model sorts the rest (faked here); groups add up to the person's count; a kid sees
only their own; health stays out of a TV turn's recall; bulk forget by group or chat, then one undo, restores exactly
the same rows. Fictional household (Alex, Robin, Ava); a temporary memory; no model.
Run: python3 scripts/test_memory_groups.py"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ["AGENT_MEMORY_DIR"] = tempfile.mkdtemp(prefix="titan-mem-groups-")
os.environ["MEMORY_HYBRID"] = "0"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_memory  # noqa: E402
import memory_groups as G  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r} expected={expected!r}"))


PEOPLE = [{"id": "alex", "name": "Alex Hale", "role": "admin"}, {"id": "p_robin", "name": "Robin Hale", "role": "adult"},
          {"id": "p_ava", "name": "Ava Hale", "role": "kid", "parents": ["alex", "p_robin"]}]
m = agent_memory.AgentMemory()
FACTS = {"Ava's ballet is on Mondays.": "person", "Robin takes the early train on Fridays.": "person",
         "Alex likes his coffee black.": "preference", "Alex is allergic to penicillin.": "health",
         "Alex works from home on Wednesdays.": "work", "Alex goes running every morning at 6.": "routine",
         "The lake cabin is Alex's favourite spot.": "place"}
for t in FACTS:
    m.add_fact("chat:alex" if "allergic" not in t else "chat:alex-2", t, 1.0 if "coffee" in t else agent_memory.EXTRACTED_CONFIDENCE, person_id="alex")
m.add_fact("chat:p_ava", "Ava likes the swings at the park.", 1.0, person_id="p_ava")
SORT = {t: k for t, k in FACTS.items() if k != "person"}
asked = []


def fake(texts):
    asked.append(list(texts))
    return [SORT.get(t, "other") for t in texts]


print("── grouping ──")
got = G.assign(m, "alex", PEOPLE, classify=fake)
check("two facts name one household member each: their person's group", got.get("person"), 2)
check("the model only sees what the roster didn't group", sorted(t for b in asked for t in b), sorted(SORT))
rows = {f["text"]: f["group"] for f in m.list_facts(person_id="alex")}
check("a named member's fact sits under them", (rows["Ava's ballet is on Mondays."], rows["Robin takes the early train on Fridays."]),
      ("person:p_ava", "person:p_robin"))
check("the model's groups", rows["Alex is allergic to penicillin."], "health")
check("a second run has nothing left to group", G.assign(m, "alex", PEOPLE, classify=fake), {})
gs = m.groups(person_id="alex")
check("groups add up to the person's fact count", sum(g["count"] for g in gs), m.total_fact_count("alex"))
check("each group shows up to two examples", all(1 <= len(g["examples"]) <= 2 for g in gs), True)
check("labels: a person's first name; health says it stays in your own chats",
      (G.label("person:p_ava", PEOPLE), G.label("health", PEOPLE)), ("Ava", "Health (only in your own chats)"))
check("a model that can't answer leaves them for later", G.assign(m, "p_ava", PEOPLE, classify=lambda t: []),
      {"left for later": 1})
check("a kid sees only their own groups", [g["group"] for g in m.groups(person_id="p_ava")], ["other"])
try:
    G.classify_local(["x"], url="https://api.example.com")
    check("grouping is local only", "asked", "refused")
except ValueError:
    check("grouping is local only", "refused", "refused")

print("── source and health ──")
check("source=said: what was told to titan", [f["text"] for f in m.list_facts(person_id="alex", source="said")],
      ["Alex likes his coffee black."])
check("source=chat: what was read out of chats", len(m.list_facts(person_id="alex", source="chat")), len(FACTS) - 1)
r = m.recall("penicillin allergy", person_id="alex")
check("health in the person's own chat", any("penicillin" in f for f in r.used), True)
r = m.recall("penicillin allergy", person_id="alex", include_health=False)
check("never on the TV", any("penicillin" in f for f in r.used), False)

print("── bulk forget, one undo ──")
before = sorted((f["id"], f["text"], f["group"], f["confidence"], f["ts"]) for f in m.list_facts(person_id="alex"))
b = m.forget_batch(person_id="alex", group="person:p_ava")
check("a group forgotten as one batch", (b["count"], bool(b["token"])), (1, True))
c = m.forget_batch(person_id="alex", conv_id="chat:alex-2")
check("everything learned from one chat", c["count"], 1)
check("they're out of recall", any("ballet" in f or "penicillin" in f for f in m.recall("ballet penicillin", person_id="alex").used),
      False)
check("another person's undo token restores nothing", m.restore_batch(b["token"], person_id="p_robin"), 0)
check("undo brings the batch back", (m.restore_batch(b["token"], person_id="alex"), m.restore_batch(c["token"], person_id="alex")), (1, 1))
after = sorted((f["id"], f["text"], f["group"], f["confidence"], f["ts"]) for f in m.list_facts(person_id="alex"))
check("…exactly the same rows (id, text, group, confidence, time)", after, before)
check("a batch never touches another person", m.total_fact_count("p_ava"), 1)
try:
    m.forget_batch(person_id="alex")
    check("a group or a chat is required", "forgot", "refused")
except ValueError:
    check("a group or a chat is required", "refused", "refused")
import household_identity as hh  # noqa: E402
check("the routes are for people, never a shared screen",
      [hh.route_allowed(r, "POST", "/v1/memory/forget") for r in ("adult", "kid", "household")], [True, True, False])

print("── health only on the person's own channel, through the real prompt path ──")
for k, v in {"HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "AGENT_TURN_LOG": "turns.jsonl", "WORLD_DIR": "world", "WORLD_DB": "world.db"}.items():
    os.environ.setdefault(k, os.path.join(os.environ["AGENT_MEMORY_DIR"], v))
import time  # noqa: E402
import agent_api_server as api  # noqa: E402
api._get_memory = lambda: m
api._MEMORY_AVAILABLE = True
api._lib.world_drop_recalled = lambda text, q: text


def prompt_as(ident):
    tid = f"t-{time.time_ns()}"
    api._turns[tid] = {"identity": ident}
    msgs, _ = api._build_messages("What is Alex allergic to? penicillin", [], False, None, turn_id=tid)
    return "\n".join(str(x.get("content") or "") for x in msgs)


OWN = hh.Identity(person={"id": "alex", "name": "Alex Hale", "role": "admin"}, device={"id": "d1", "name": "Alex's iPhone",
                                                                                     "platform": "ios"}, role="admin", via="device")
SCREEN = hh.Identity(person={"id": "house", "name": "Living room TV", "role": "household"}, device={"id": "tv1", "name": "TV",
                                                                                                    "platform": "tvos"},
                     role="household", via="device")
SERVICE = hh.Identity(person={"id": "alex", "name": "Alex Hale", "role": "admin"}, device=None, role="admin", via="engineering")
check("the person's own device: their health fact is in the prompt", "penicillin" in prompt_as(OWN).lower().split("question")[0]
      or "allergic to penicillin" in prompt_as(OWN), True)
check("a shared screen: no recall at all, so no health", "allergic to penicillin" in prompt_as(SCREEN), False)
check("a service token: no health", "allergic to penicillin" in prompt_as(SERVICE), False)

print("── a restore keeps the pin and the use ──")
fid = next(f["id"] for f in m.list_facts(person_id="alex") if "coffee" in f["text"])
m.set_pinned(fid, True, person_id="alex")
m.stamp_used(["Alex likes his coffee black."], person_id="alex")
keep = next((f["pinned"], f["last_used"], f["use_count"]) for f in m.list_facts(person_id="alex") if f["id"] == fid)
m.forget_fact(fid, person_id="alex")
m.restore_fact(fid, person_id="alex")
check("one fact: pinned, last_used and use_count come back",
      next((f["pinned"], f["last_used"], f["use_count"]) for f in m.list_facts(person_id="alex") if f["id"] == fid), keep)
t = m.forget_batch(person_id="alex", group="preference")["token"]
m.restore_batch(t, person_id="alex")
check("a batch: the same", next((f["pinned"], f["last_used"], f["use_count"]) for f in m.list_facts(person_id="alex")
                                if f["id"] == fid), keep)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
