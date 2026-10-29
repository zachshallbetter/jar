#!/usr/bin/env python3
# gate: hermetic   (temp trace, world and household stores; the local judge and the Decider are fakes)
"""#377 P2: structural proposals from a person's misses (Coordination's design §2–5): candidates found in code over
their own notes (merge, split, retitle), the non-KB questions dropped by the Decider, the local judge's yes, stored
"held" in their own bubble (no card, no apply: P3), and a replay gate per kind (≥ 20 replays, a gain, no regression).
Own notes only, never across owners or audiences, never a fenced pair, a kid gets none, each note's text inside an
untrusted span, and nothing raw in the counts. Fictional people (Alex and Robin, adults; Ava, a kid).
Run: python3 scripts/test_miss_proposals_p2.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = Path(tempfile.mkdtemp(prefix="titan-missprop2-"))
os.environ.update({"RETRIEVAL_TRACE_DB": str(_TMP / "t.db"), "HOUSEHOLD_DB": str(_TMP / "h.db"),
                   "KB_ACL_DB": str(_TMP / "a.db"), "AGENT_FENCES": "0", "PROACTIVE_DB": str(_TMP / "p.db")})
os.environ.pop("MISS_PROPOSALS_P2", None)
import miss_proposals as MP  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


ALEX, ROBIN, AVA = "p_alex", "p_robin", "p_ava"
NOTES = [{"source_id": "n-vet", "title": "Maple Animal Clinic", "first": "Open 8-6 weekdays"},
         {"source_id": "n-vet2", "title": "Pet clinic hours", "first": "Saturday 9-1"},
         {"source_id": "n-shared", "title": "Clinic phone", "first": "555-0101"},
         {"source_id": "n-fenced", "title": "Vet bills", "first": "Paid in August"},
         {"source_id": "n-house", "title": "House things", "first": "Wifi, bins, the boiler"},
         {"source_id": "n-wifi", "title": "Guest notes", "first": "Network Quarry-Guest"}]
ACL = {"n-vet": (ALEX, "private"), "n-vet2": (ALEX, "private"), "n-shared": (ALEX, "household"),
       "n-fenced": (ALEX, "private"), "n-house": (ALEX, "private"), "n-wifi": (ALEX, "private")}
FENCED = {"n-fenced"}


def miss(qid, q, *, reached=(), cited=(), rank=None, corrected=False, at=None):
    return {"query_id": qid, "at": at or qid, "path": "search", "corrected": corrected, "question": q, "shown": list(reached),
            "cited": list(cited), "reached": list(reached), "rank": rank or {s: i + 1 for i, s in enumerate(reached)}}


MISSES = [miss(1, "when is the vet open on saturday", reached=["n-vet", "n-vet2", "n-fenced", "n-shared"]),
          miss(2, "what's the guest wifi", rank={"n-wifi": 9}),
          miss(3, "when do the bins go out", cited=["n-house"], corrected=True),
          miss(4, "what's the wifi password", cited=["n-house"], corrected=True),
          miss(5, "is the boiler serviced", cited=["n-house"], corrected=True)]
fenced = lambda sid, title: sid in FENCED  # noqa: E731

print("── candidates, in code, over their own notes ──")
cands = MP.candidates(ALEX, "admin", MISSES, NOTES, ACL, fenced=fenced)
got = sorted((c["kind"], tuple(n["source_id"] for n in c["notes"])) for c in cands)
check("merge: the two of their notes both shown and neither cited; retitle: one ranked past 5; split: one cited in 3 "
      "corrected misses on different questions",
      got, sorted([("miss_merge", ("n-vet", "n-vet2")), ("miss_retitle", ("n-wifi",)), ("miss_split", ("n-house",))]))
check("never a fenced note in a merge, never a shared note",
      any("n-fenced" in t or "n-shared" in t for k, t in got if k == "miss_merge"), False)
check("a retitle only on a note only they can see", MP.candidates(
    ALEX, "admin", [miss(9, "the clinic's number", rank={"n-shared": 8})], NOTES, ACL, fenced=fenced), [])
check("a kid: no structural candidate at all (aliases and questions only)",
      MP.candidates(AVA, "kid", MISSES, NOTES, ACL, fenced=fenced), [])
check("someone else's notes never: Robin's misses over Alex's notes find nothing of his",
      MP.candidates(ROBIN, "adult", MISSES, [], ACL, fenced=fenced), [])
check("one note cited twice on the same question isn't a split",
      [c for c in MP.candidates(ALEX, "admin", [miss(3, "bins?", cited=["n-house"], corrected=True),
                                               miss(4, "Bins?", cited=["n-house"], corrected=True)], NOTES, ACL,
                                fenced=fenced) if c["kind"] == "miss_split"], [])

SHARED_ACL = {**ACL, "n-vet": (ALEX, "household"), "n-vet2": (ALEX, "household"), "n-house": (ALEX, "adults")}
shared = MP.candidates(ALEX, "admin", MISSES, NOTES, SHARED_ACL, fenced=fenced)
check("a shared pair and a shared note: no merge and no split (structural kinds are private notes only; shared notes "
      "get aliases)", sorted(c["kind"] for c in shared), ["miss_retitle"])
sp = MP.structural(ALEX, "admin", MISSES, NOTES, SHARED_ACL, fenced=fenced, decide=lambda q: True,
                   llm=lambda *a: {"ok": True, "text": "x"})
check("…so no question text lands in any proposal on a shared note",
      [p for p in sp if SHARED_ACL[p["source_id"]][1] != "private"], [])
check("the merge's replay variant carries the other note's title, never its source id",
      MP.variant_for({"kind": "miss_merge", "source_id": "n-vet", "attr": "n-vet2|Vet hours|Pet clinic hours"}),
      {"n-vet": ["Vet hours", "Pet clinic hours"]})

print("── the judge (local), the Decider's pre-filter ──")
seen = []


def fake_llm(system, user, schema):
    seen.append(user)
    if "same thing" in system:
        return {"ok": True, "text": "Vet hours"}
    if "two subjects" in system:
        return {"ok": True, "text": "Wifi | Bins and boiler"}
    return {"ok": True, "text": "Guest wifi"}


props = MP.structural(ALEX, "admin", MISSES, NOTES, ACL, llm=fake_llm, decide=lambda q: "boiler" not in q, fenced=fenced)
check("each candidate judged; a merge carries the other note and the merged title, a split both titles",
      sorted((p["kind"], p["source_id"], p["attr"]) for p in props),
      sorted([("miss_merge", "n-vet", "n-vet2|Vet hours|Pet clinic hours"), ("miss_retitle", "n-wifi", "Guest wifi")]))
check("the Decider drops a question that isn't about saved knowledge (the split's mentions the boiler)",
      any(p["kind"] == "miss_split" for p in props), False)
lines = [ln for u in seen for ln in u.split("\n") if ln[:1].isdigit() and ". " in ln[:4]]
check("the judge's notes each sit in an untrusted span", (len(lines) > 0, all("⟦untrusted" in ln for ln in lines)),
      (True, True))
check("a no from the judge: no proposal", MP.structural(ALEX, "admin", MISSES, NOTES, ACL, fenced=fenced,
                                                        llm=lambda *a: {"ok": False, "text": "x"}, decide=lambda q: True), [])

print("── stored held, in their own bubble: no card ──")
B = W.Bubbles(_TMP / "world")
check("stored", MP.store(B, props), 2)
rows = [(r["kind"], r["state"]) for r in B.world(W.bubble_name(ALEX)).con.execute(
    "SELECT kind, state FROM note_reviews ORDER BY kind")]
check("…as 'held' (the daily slot reads only pending rows)", rows, [("miss_merge", "held"), ("miss_retitle", "held")])
check("…in Alex's bubble only", [b for b in B.bubbles() if B.world(b).con.execute(
    "SELECT count(*) FROM note_reviews").fetchone()[0]], [W.bubble_name(ALEX)])
check("the daily slot never offers a held proposal", W._note_review(B, ALEX), None)
check("counts: numbers only", MP.counts(B, 7),
      {"made": 2, "kept": 0, "not_useful": 0, "open": 0, "held": 2})

print("── the replay gate ──")
def rank(question, extras):
    order = ["n-a", "n-b", "n-c", "n-d", "n-e", "n-wifi"]
    if extras and "n-wifi" in extras:
        order = ["n-wifi"] + [x for x in order if x != "n-wifi"]
    return order


case = {"question": "guest wifi", "target": "n-wifi", "variant": {"n-wifi": ["Guest wifi"]}}
g = MP.replay_gate("miss_retitle", [case] * 25, [], rank)
check("25 replays, the target into the top 5 every time, no regression: ship", (g["gain"], g["regressions"], g["ship"]),
      (25, 0, True))
check("too few replays: never ships", MP.replay_gate("miss_retitle", [case] * 5, [], rank)["ship"], False)
reg = {"question": "the boiler", "cited": "n-e", "variant": {"n-wifi": ["Guest wifi"]}}
check("one regression (a cited note pushed out of the top 5): never ships",
      (MP.replay_gate("miss_retitle", [case] * 25, [reg], rank)["regressions"],
       MP.replay_gate("miss_retitle", [case] * 25, [reg], rank)["ship"]), (1, False))
check("no gain: never ships", MP.replay_gate("miss_retitle", [{**case, "variant": {}}] * 25, [], rank)["ship"], False)
mem = SimpleNamespace(_db=sqlite3.connect(":memory:"))
mem._db.execute("CREATE TABLE messages (conv_id TEXT, role TEXT, content TEXT, ts REAL)")
gates = MP.gate_all(B, [{"id": ALEX, "role": "admin"}], mem, lambda pid, role: rank,
                    bank=[{"person": ALEX, "kind": "miss_retitle", "question": "guest wifi", "target": "n-wifi",
                           "variant": {"n-wifi": ["Guest wifi"]}}] * 24)
check("gate_all: the held proposals plus the bank, per kind, counts only",
      {k: (v["replayed"], v["ship"]) for k, v in gates.items()},
      {"miss_retitle": (25, True), "miss_merge": (1, False), "miss_split": (0, False)})
check("…no question, title or id in its output", any(w in json.dumps(gates) for w in ("wifi", "n-", "vet", ALEX)), False)

print("── the flag ships off ──")
check("run_all's P2 needs MISS_PROPOSALS_P2=1", MP.p2_enabled(), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
