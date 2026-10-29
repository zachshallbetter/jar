#!/usr/bin/env python3
# gate: hermetic   (throwaway stores; nothing is sent: payloads and targets are built, never posted)
"""What Apple sees (#237: pushes are "private": the phone fetches the card itself; 10-07 red team). Every push titan
builds carries no words of the household's: card pushes a fixed alert, the card id and words from fixed lists (its
category, a thread that is one of push.THREADS, never a sender's name, #1208; its level); every card key is ids or
hashes, never a name, a place or a title, and the push no longer carries it; the apns-collapse-id a hash; clear, sync and inbox pushes nothing but a
flag; trips and agent runs counts, times and a state word; a task's Live Activity no step words without the owner's
live_activity_detail (its goal, redacted and ≤60 characters, is the one documented exception: Zach, 10-03). And a push
goes only to devices whose person the card is for: never a kid for an adults' card, never another adult for a private
one, the household screen only for a household card. Fictional people (Alex, Robin; Ava, a kid).
Run: python3 scripts/test_push_privacy.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-pushpriv-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "PUSH_DB": "push.db", "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "FENCES_DB": "fences.db"}.items():
    os.environ[k] = str(_TMP / v)
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_runs  # noqa: E402
import proactive  # noqa: E402
import push  # noqa: E402
import trip_push  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


SECRET = ("Maple Clinic", "Pearl Dental", "Ava", "zq-detail-words", "lease renewal")

print("── card pushes: a fixed alert, the id, a key of ids or hashes ──")
card = proactive.add_card("Ava's appointment at Maple Clinic", "zq-detail-words: bring the lease renewal", kind="alert",
                          urgency=2, source="test", to=["p_robin"], key="test-1")
body = json.dumps(push.payload(card)) + json.dumps(push.payload(card, passive=True))
check("the card push carries no title or detail words", [w for w in SECRET if w in body], [])
check("…its alert is fixed", push.payload(card)["aps"]["alert"], {"title": "titan", "body": "titan has something for you"})
check("…and only the id besides aps (the key stays on titan)", sorted(push.payload(card)), ["aps", "card"])
check("…aps holds only fixed fields", sorted(push.payload(card)["aps"]),
      ["alert", "category", "interruption-level", "mutable-content", "relevance-score", "sound", "thread-id"])

print("── the thread-id is always one of the fixed words, never a name, amount or sender ──")
note = proactive.add_card("From Robin: pick up Ava at 3", "zq-detail-words", urgency=1, source="note", to=["p_alex"],
                          sender="Robin")
bill = proactive.add_card("Pearl Dental $84.12 is due Friday", "", urgency=1, source="bill-due", to=["p_robin"],
                          key="bill-due:zqkey-3f9a0c", sender="Pearl Dental")   # #1699: "zqkey" can't be in a hex id
lock = proactive.add_card("Lock up? The back door is open", "", urgency=2, source="lock-up", to="adults", key="lock-up")
SOURCES = ["note", "person", "voice-note", "agent", "titan", "bill-due", "mail-bills", "calendar", "trigger:calendar",
           "routine", "tasks", "lock-up", "mail", "device-health", "trigger:git", "Robin", "from:robin", "zq-new-source", ""]
FROMS = [None, "", "titan", "Robin", "Alex Quarry", "Pearl Dental", "$84.12"]
KEYS = [None, "leave-ab12", "bill-due:3f9a", "payfail-1", "Robin", "from:robin", "sched-12"]
seen = {push.payload({"id": "x", "urgency": u, "source": s_, "from": f_, "key": k_}, passive=pv, role=r_)["aps"]["thread-id"]
        for s_ in SOURCES for f_ in FROMS for k_ in KEYS for u in (0, 1, 3) for pv in (False, True)
        for r_ in (None, "adult", "household")}
check("over every source, sender, key, urgency, level and role: only fixed words", sorted(seen - set(push.THREADS)), [])
check("…the fixed words are the agreed ones", push.THREADS, ("bills", "calendar", "house", "mail", "chat", "routines", "tasks", "cards"))
whole = json.dumps([push.payload(c_) for c_ in (note, bill, lock)])
check("a note, a bill and a lock-up: no sender, name, amount, key or title anywhere Apple sees",
      [w for w in ("Robin", "Ava", "Pearl", "84.12", "$", "zqkey", "back door", "zq-detail-words") if w in whole], [])
check("…their threads: cards, bills, house", [push.payload(c_)["aps"]["thread-id"] for c_ in (note, bill, lock)],
      ["cards", "bills", "house"])
c2 = dict(card, key="place-todos-Maple Clinic")
check("the apns-collapse-id is a hash of the key, never its words", (len(push.collapse_id(c2)), "Maple" in push.collapse_id(c2)),
      (32, False))
check("clear, sync and inbox pushes carry a flag and an id only",
      (sorted(push.clear_payload("c1")), push.SYNC_PAYLOAD, push.INBOX_PAYLOAD),
      (["aps", "clear"], {"aps": {"content-available": 1}, "sync": 1}, {"aps": {"content-available": 1}, "inbox": 1}))

print("── every card key in the code: ids, dates, weeks or hashes, never a name, place, title or vendor ──")
WORDY = re.compile(r"\{(?:[a-z_.\[\]'\"]*\b(?:place|title|name|who|vendor|what|subject|text|detail|label|goal|entity)\b[a-z_.\[\]'\"]*)\}")
bad = []
for f in sorted(HERE.glob("*.py")):
    if f.name.startswith("test_"):
        continue
    for i, line in enumerate(f.read_text().splitlines(), 1):
        for m in re.finditer(r"\bkey\s*=\s*f[\"']([^\"']+)[\"']", line):
            if WORDY.search(m.group(1)):
                bad.append(f"{f.name}:{i}: {m.group(1)}")
check("no key template interpolates words", bad, [])

import family_meeting  # noqa: E402
sent = []
family_meeting.ask_cards({"week": "2026-10-05", "sort_out": [{"key": "2026-10-08:ava:1530", "day": "Thu", "who": "Ava",
                                                              "text": "Ava has soccer at 3:30 PM"}]},
                         send=lambda *a, **k: sent.append(k["key"]))
check("a family-meeting card's key (the name hid in its section key) carries no name", ("ava" in sent[0].lower(), len(sent)), (False, 1))
import agent_api_server  # noqa: E402
src = (HERE / "agent_api_server.py").read_text()
check("…and the place to-dos card's key is a hash of the place", 'key="place-todos-" + hashlib.sha1(' in src, True)

print("── Live Activities ──")
st = push.task_state(2, 4, title="Call Pearl Dental about Ava", waiting="Robin to confirm", goal="Ava's dentist visit", detail=False)
check("a task's step words stay off the Lock Screen without detail (a blank step, a fixed 'Waiting on you.')",
      (st["stepTitle"], st["waiting"], "Pearl" in json.dumps(st)), ("", "Waiting on you.", False))
long = push.task_state(1, 3, goal="x" * 200)
check("…its goal (the documented exception) is at most 60 characters", len(long["goal"]) <= push.GOAL_CHARS, True)
ts = trip_push.state(900, 25, datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc), "moved")
check("a trip: minutes, times and a state word only", sorted(ts) <= ["leaveBy", "minutes", "start", "state"] and ts["state"] in
      ("moved", "cancelled", "on", "late", "leave", "update"), True)
run = {"state": "running", "started_at": 1e9, "updated_at": 1e9, "files_changed": 3, "lines_added": 10, "lines_removed": 2}
check("an agent run: counts, times and its state only", sorted(agent_runs.content_state(run)),
      ["filesChanged", "linesAdded", "linesRemoved", "startedAt", "state", "updatedAt"])
check("…its push-to-start attributes carry the run id only (the trips rule, #753 option A)",
      agent_runs.start_attributes({**run, "id": "r1", "repo": "zq-secret-repo", "mac": "Robin's MacBook"}),
      {"runID": "r1", "repo": "", "mac": ""})
check("…and its start alert is fixed words: no repo, no Mac name",
      agent_runs.start_alert({**run, "repo": "zq-secret-repo", "mac": "Robin's MacBook"}),
      {"title": "Claude on your Mac", "body": "Claude started on your Mac"})

print("── who a push goes to ──")
DEV = [{"id": "d-alex", "person_id": "p_alex", "role": "adult"}, {"id": "d-robin", "person_id": "p_robin", "role": "adult"},
       {"id": "d-ava", "person_id": "p_ava", "role": "kid"}, {"id": "d-tv", "person_id": "p_tv", "role": "household"}]
push._tokens = lambda db=None: [{"device_id": d["id"], "live": False, "token": "ab" * 16, "bundle": "x", "environment": "development"}
                                for d in DEV]


def to(c):
    return sorted(t["device_id"] for t in push.targets(c, DEV))


priv = proactive.add_card("Robin's own", "", source="test", to=["p_robin"], key="t-priv")
adults = proactive.add_card("For the adults", "", source="test", to="adults", key="t-adults")
house = proactive.add_card("Bins tonight", "", source="test", to="household", key="t-house")
check("a private card: its person's devices only", to(priv), ["d-robin"])
check("an adults' card: never the kid, never the shared screen", to(adults), ["d-alex", "d-robin"])
check("a household card: everyone, the shared screen too", to(house), ["d-alex", "d-ava", "d-robin", "d-tv"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
