#!/usr/bin/env python3
# gate: hermetic   (fictional data, no network, no live model, no real files, a throwaway salt)
"""titan-node#1498: the scans stages chained (envelope, dedupe, junk, cycles, threads, notices, bundles) in scan_pipeline.py, and the
benchmark's claim-thread truth, which the letters themselves show. The committed chained score matches a fresh run; the chain
beats every single stage on what it adds and loses nothing; each stage's own pipeline is still what it was; privacy stays 0."""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_pipeline as P  # noqa: E402
import scans_bench as B  # noqa: E402
import scans_score as S  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'BAD '} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


here = S.BASELINE_FILE.parent
batch = B.generate(1499)
truth, scans = batch["truth"], batch["scans"]
res = json.loads(json.dumps(S.score(batch, P.pipeline(scans), "scan_pipeline:pipeline")))
t = res["tasks"]
check("the committed chained score matches a fresh run", (here / "pipeline.json").exists() and json.loads((here / "pipeline.json").read_text()) == res)

# --- every stage's own pipeline still scores what is committed ---------------------------------------------------------------
for name, mod in (("dedupe", "scan_dedupe"), ("cycles", "scan_cycles"), ("threads", "scan_threads"), ("notices", "scan_notices"), ("junk", "scan_junk"), ("bundles", "scan_bundles")):
    m = __import__(mod)
    check(f"{name} on its own still scores its committed result",
          json.loads(json.dumps(S.score(batch, m.pipeline(scans), f"{mod}:pipeline"))) == json.loads((here / f"{name}.json").read_text()))

# --- the chain keeps what each stage added -----------------------------------------------------------------------------------
single = {n: json.loads((here / f"{n}.json").read_text())["tasks"] for n in ("dedupe", "junk", "cycles", "threads", "notices", "bundles")}
check("stages are chained in order", [n for n, _ in P.STAGES] == ["envelope", "dedupe", "junk", "cycles", "threads", "notices", "bundles", "privacy"])
check("dedupe is the dedupe stage's", t["dedupe"] == single["dedupe"]["dedupe"])
for k in ("bill_cycle", "thread", "event", "junk", "bundle"):
    mine = t["association"]["per_kind"][k]["kind_and_key"]["f1"]
    best = max((s["association"]["per_kind"][k]["kind_and_key"]["f1"] or 0) for s in single.values())
    check(f"association for {k} is as good as any single stage's", mine >= best, f"{mine} < {best}")
check("association F1 beats every single stage's", all(t["association"]["kind_and_key"]["f1"] > s["association"]["kind_and_key"]["f1"] for s in single.values()))
check("waiting on you: precision and recall beat every single stage's",
      all(t["waiting"]["precision"] >= s["waiting"]["precision"] and t["waiting"]["recall"] >= s["waiting"]["recall"] for s in single.values())
      and t["waiting"]["precision"] > 0.99 and t["waiting"]["recall"] > 0.99, str(t["waiting"]))
check("bundles: every tax form, policy and warranty is on its bundle, and every bundle is one person's",
      t["association"]["per_kind"]["bundle"]["kind_and_key"]["f1"] == 1.0 and t["bundles"]["crossed"] == 0
      and t["bundles"]["membership"]["f1"] == 1.0 and t["classes"]["facet"]["accuracy"] == 1.0, str(t["bundles"]))
check("privacy: 0 leaks on every viewer and every trap", t["privacy"]["leaks"] == 0 and not any(t["privacy"]["by_trap"].values()))

# --- claim-thread truth follows from the letters ----------------------------------------------------------------------------
ASK = re.compile(r"(?i)please (send|provide|submit|return|reply|respond|call|sign)|you may appeal")
CLOSE = re.compile(r"(?i)claim has been resolved|no further action is required")
threads: dict[str, list[str]] = defaultdict(list)
for s in scans:
    if truth[s["id"]]["type"] == "claim_letter":
        threads[truth[s["id"]]["thread"]].append(s["id"])
by_id = {s["id"]: s for s in scans}


def when(sid):
    return truth[sid]["date"]


derived = {}
for th, ids in threads.items():
    ids.sort(key=lambda i: (when(i), i))
    texts = [by_id[i]["text"] for i in ids]
    derived[th] = any(ASK.search(x) for x in texts) and not CLOSE.search(texts[-1])
check("seven claim threads, of four letters each", len(threads) == 7 and all(len(v) == 4 for v in threads.values()))
check("a thread waits exactly when a letter asked and the latest does not close", all(
    all(truth[i]["waiting"] == derived[th] for i in ids) for th, ids in threads.items()))
check("some threads end closed and some end open", 0 < sum(derived.values()) < len(derived), str(derived))
check("the same seed gives the same batch", B.generate(1499) == batch and B.generate(7) != batch)
other = B.generate(7)
check("the truth follows the letters on another seed too", all(
    truth_w == (any(ASK.search(by[i]["text"]) for i in ids) and not CLOSE.search(by[ids[-1]]["text"]))
    for by, ids, truth_w in [(
        {s["id"]: s for s in other["scans"]},
        sorted((s["id"] for s in other["scans"] if other["truth"][s["id"]]["thread"] == th),
               key=lambda i: (other["truth"][i]["date"], i)), other["truth"][next(s["id"] for s in other["scans"] if other["truth"][s["id"]]["thread"] == th)]["waiting"])
        for th in {x["thread"] for x in other["truth"].values() if x["thread"]}]))

print(f"{'FAILED' if failures else 'all ok'}: {len(failures)} failing")
sys.exit(1 if failures else 0)
