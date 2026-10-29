#!/usr/bin/env python3
"""Household retrieval, scored nightly (goal item 3, 10-01): does titan's knowledge base answer the
household's questions from the right item, and say nothing when nothing is stored?

  found       17 questions (by zone: facts, sync, web, system), each with words its right item contains. An explicit search (kb_retrieve's
              path) passes when one of its top 3 hits holds them.
  not stored  3 questions nothing in the KB answers. Passes when titan's per-turn context (exactly
              what the model reads: the 0.5 floor and the topic-word filter) holds no "Relevant memory"
              for it (09-30: "when does the trash go out?" returned a loop decision and two newsletters).
              If what comes back does answer it (its topic words), the question is skipped and logged:
              the household told titan, so it isn't "not stored" any more.

Runs as the admin, read-only. Prints one line per miss and "retrieval checks passed: X/Y" (the nightly
eval adds it to its score), and appends one JSON line to /ai/logs/retrieval-eval.jsonl for the scorecard.

  python3 scripts/retrieval_eval.py [--no-log]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_names as _names   # fictional names in the questions; real ones only at titan's KB (#878)

LOG = Path(os.environ.get("RETRIEVAL_EVAL_LOG", "/ai/logs/retrieval-eval.jsonl"))
# (id, question, words the right item contains: any one of these, lowercase)
FOUND = [
    ("ava-ballet", "When is Ava's ballet?", ["calendar: ava"]),
    ("tmobile-bill", "How much is the T-Mobile bill?", ["t-mobile"]),
    ("dentist", "When is the dentist appointment?", ["dental", "dentist"]),
    ("leo-pickup", "When do I pick up Leo from school?", ["pickup leo", "pick up leo"]),
    ("ballet-studio", "Where is the ballet studio?", ["adams st"]),
    ("deploy-titan", "How do I deploy titan-node?", ["deploy_titan_node"]),
    ("vercel", "Can I deploy to Vercel?", ["vercel"]),
    ("coordinator-auth", "How do the runtimes authenticate callers?", ["ed25519"]),
    ("pairing", "How does a new phone pair with titan?", ["pair"]),
    ("push", "How do cards reach the iPhone when the app is closed?", ["apns"]),
    # per zone (#833, 10-04): mail, saved pages and household notes had no case
    ("website-hosting", "What did AAF Spokane say about transferring the website hosting?", ["website hosting", "three websites"]),
    ("public-works", "Is there a Spokane Public Works invoice?", ["public works"]),
    ("vercel-failed", "Did a payment to Vercel fail?", ["payment to vercel", "vercel inc"]),
    ("spotlight-memory", "What was that article about Spotlight Memory?", ["spotlight memory"]),
    ("llms-grow", "What did the Percepta post say about LLMs growing their own capabilities?", ["grow their own"]),
    ("late-start", "When is the kids' Thursday late start?", ["late start"]),
    ("ava-school", "What's Ava's school schedule?", ["school schedule"]),
]
# The zone each case's right item lives in (zones.py): the score is reported per zone too, so a partition change
# that helps one zone and hurts another shows (#833's bar: no zone below its baseline).
ZONE = {"ava-ballet": "sync", "dentist": "sync", "leo-pickup": "sync", "ballet-studio": "sync",
        "website-hosting": "sync", "public-works": "sync", "vercel-failed": "sync",
        "tmobile-bill": "facts", "late-start": "facts", "ava-school": "facts",
        "spotlight-memory": "web", "llms-grow": "web",
        "deploy-titan": "system", "vercel": "system", "coordinator-auth": "system", "pairing": "system", "push": "system"}
# (id, question, words an answer would contain). Nothing answers these today, but any of them can
# become true (Zach may well tell titan trash day). Memory that answers one skips it, logged, so the
# nightly never fails on a real answer; memory that doesn't answer it is the noise this checks for.
NOT_STORED = [
    ("trash-day", "When does the trash go out?", ["trash", "garbage", "recycling"]),
    ("wifi-password", "What's the wifi password?", ["wifi", "wi-fi"]),
    ("robin-restaurant", "What is Robin's favorite restaurant?", ["restaurant"]),
]


def _text(h: dict) -> str:
    return _names.back(f"{h.get('title') or ''}\n{h.get('chunk_text') or h.get('text') or ''}").lower()


def run(query=None, context=None) -> dict:
    """Score every question. `query(q, k, passive)` returns hits and `context(q)` the per-turn context
    (defaults: titan's own, as the admin)."""
    if query is None or context is None:
        import agent_runtime_lib as lib
        lib._TURN_IO.role, lib._TURN_IO.person = None, None   # the admin: every household item is visible
        lib._TURN_IO.source = "eval"   # an eval: it teaches titan nothing about the house (#366: 221 fake mentions)
        query = query or (lambda q, k, passive: lib._kb_query(_names.out(q), k, passive=passive))   # noqa: E731
        context = context or lib._situational_context
    missed: list[str] = []
    found = 0
    by_zone: dict[str, list[int]] = {}
    for qid, q, words in FOUND:
        by_zone.setdefault(ZONE.get(qid, "facts"), [0, 0])[1] += 1
        try:
            hits = query(q, 3, False)
        except Exception as exc:  # noqa: BLE001
            hits, words = [], words
            print(f"  {qid}: search failed ({type(exc).__name__}) (0/1)")
            missed.append(qid)
            continue
        if any(w in _text(h) for h in hits for w in words):
            found += 1
            by_zone[ZONE.get(qid, "facts")][0] += 1
        else:
            missed.append(qid)
            print(f"  {qid}: the right item wasn't in the top 3 for {q!r} (0/1)")
    quiet = 0
    skipped: list[str] = []
    for qid, q, topic in NOT_STORED:
        try:
            ctx = _names.back(context(_names.out(q)) or "")
        except Exception as exc:  # noqa: BLE001
            print(f"  {qid}: context failed ({type(exc).__name__}) (0/1)")
            missed.append(qid)
            continue
        memory = ctx.split("Relevant memory", 1)[1].lower() if "Relevant memory" in ctx else ""
        if not memory:
            quiet += 1
        elif any(w in memory for w in topic):
            skipped.append(qid)
            print(f"  {qid}: something stored now answers {q!r}; skipped. Replace the question if that's real.")
        else:
            missed.append(qid)
            first = ctx.split("Relevant memory", 1)[1].splitlines()[1:2]
            print(f"  {qid}: nothing should be stored, yet the context carries {first[0][:80] if first else 'memory'!r} (0/1)")
    return {"found": found, "found_total": len(FOUND), "by_zone": by_zone, "not_stored": quiet,
            "not_stored_total": len(NOT_STORED) - len(skipped), "failing": missed, "skipped": skipped}


def run_world(limit: int = 15) -> dict:
    """The world model's half (#325), reported apart from the pass/fail total above: for each lookup
    world_eval builds from world.db, does the per-turn context (world model on, as the admin) carry
    the right value? None when world.db holds nothing."""
    import agent_runtime_lib as lib
    import world
    import world_eval
    cases = [c for c in world_eval.lookups(world.get(), limit, unknown=0) if c.values]
    if not cases:
        return {}
    lib._TURN_IO.role, lib._TURN_IO.person, lib._TURN_IO.eval_world = None, None, True
    lib._TURN_IO.source = "eval"   # in-process, so no API sets it: no mentions, gaps or told checks from here (#366)
    try:
        hit, missed = 0, []
        for c in cases:
            ctx = _names.back(lib._situational_context(_names.out(c.question)) or "")
            if all(world_eval.mentions(ctx, v, c.name.split(".")[1]) for v in c.values):
                hit += 1
            else:
                missed.append(c.name)
                print(f"  world {c.name}: the context doesn't carry {c.values} for {c.question!r}")
    finally:
        lib._TURN_IO.eval_world = None
    return {"world": hit, "world_total": len(cases), "world_failing": missed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-log", action="store_true")
    a = ap.parse_args()
    r = run()
    passed, total = r["found"] + r["not_stored"], r["found_total"] + r["not_stored_total"]
    print(f"retrieval checks passed: {passed}/{total}")
    print("found by zone: " + " · ".join(f"{z} {v[0]}/{v[1]}" for z, v in sorted(r["by_zone"].items())))
    try:
        wr = run_world()
    except Exception as exc:  # noqa: BLE001 - no world.db here
        wr = {}
        print(f"(world checks skipped: {exc})", file=sys.stderr)
    if wr:
        print(f"world context checks: {wr['world']}/{wr['world_total']} (not in the total above)")
        r.update(wr)
    try:   # the bank from real questions (#862): per kind recall@3 and wrong zones, against its baseline
        import kb_bank
        br = kb_bank.nightly()
    except Exception as exc:  # noqa: BLE001 - no KB, no bank: the fixed cases above still count
        br = None
        print(f"(KB bank skipped: {exc})", file=sys.stderr)
    if br:
        print("KB bank: " + " · ".join(f"{k} {v['found']}/{v['n']}" for k, v in sorted(br["by_kind"].items()))
              + f"; wrong zone {br['wrong_zone']}/{br['cases']}"
              + (f"; below baseline: {', '.join(br['below_baseline'])}" if br["below_baseline"] else ""))
        r["bank"] = {k: br[k] for k in ("by_kind", "cases", "found", "wrong_zone", "misses", "below_baseline")}
    if not a.no_log:
        rev = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent.parent), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        try:
            with LOG.open("a") as f:
                f.write(json.dumps({"ts": time.time(), "rev": rev, **r}) + "\n")
        except OSError as exc:
            print(f"(couldn't append to {LOG}: {exc})", file=sys.stderr)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
