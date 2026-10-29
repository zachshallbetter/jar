#!/usr/bin/env python3
"""An A/B readout with its own noise band (titan-node#1144): counts only, never a question, answer or reply.

The truth_eval A/B runs (ab_run.sh) leave one --jsonl file per arm, pass and client: <ARM>-<n>-<client>.jsonl, ARM
A (before), B (after) and C (the A/A control: A's code again, on its own box, interleaved the same way). Per brain and
client, per category:
  A, B, C     the mean per pass (ok answers)
  B-A         the change
  band        max(|C-A|, one flip = 1/passes): how far the same code moved between two boxes in the same window
  DROP        B-A < 0 and beyond the band (with no control run, beyond one flip)
and per case, the ok counts per arm, flagging a case only when |A-B| is beyond max(|A-C|, 1). #1064 judged single 3-pass flips by
eye (A alone went 1/3 -> 3/3 on one case); this makes "nothing drops" mean "nothing drops beyond the noise".

--set wants_schedule judges only the cases the change can reach (truth_eval.in_set); the question text is read for
that test and never printed.

  python3 scripts/ab_readout.py DIR [--set all|wants_schedule] [--json]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

_FILE = re.compile(r"^([ABC])-(\d+)-(\w+)\.jsonl$")


def _in_set(question: str, which: str) -> bool:
    if which == "wants_schedule":
        import schedule_digest
        return schedule_digest.wants_schedule(question)
    return True


def load(d: str, which: str = "all") -> dict[str, Any]:
    """→ {"passes": {(arm, brain, client): [{category: [ok, n]}, …]}, "cases": {(brain, client, name): {arm: [ok…]}},
    "ms": {(arm, brain, client): [ms…]}}"""
    passes: dict[tuple, list] = collections.defaultdict(list)
    cases: dict[tuple, dict] = collections.defaultdict(lambda: collections.defaultdict(list))
    ms: dict[tuple, list] = collections.defaultdict(list)
    for f in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
        m = _FILE.match(os.path.basename(f))
        if not m:
            continue
        arm, _n, client = m.group(1), int(m.group(2)), m.group(3)
        per: dict[str, dict[str, list[int]]] = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
        for line in open(f, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not _in_set(str(r.get("question") or ""), which):
                continue
            b = str(r.get("brain") or "?")
            c = per[b][str(r.get("category") or "?")]
            c[0] += bool(r.get("ok"))
            c[1] += 1
            cases[(b, client, str(r.get("name") or "?"))][arm].append(bool(r.get("ok")))
            ms[(arm, b, client)].append(int(r.get("ms") or 0))
        for b, cats in per.items():
            passes[(arm, b, client)].append({k: list(v) for k, v in cats.items()})
    # #402: a box-profile arm has no Gemini (sandbox_box.sh --profile box), so its "gemini" rows are 0 by design, not a
    # regression: left out of the comparison, and said so
    skipped: list[str] = []
    try:
        profiles = json.loads((Path(d) / "profiles.json").read_text())
    except (OSError, ValueError):
        profiles = {}
    for arm, prof in sorted(profiles.items()):
        if prof == "box":
            for key in [k for k in passes if k[0] == arm and k[1] == "gemini"]:
                passes.pop(key)
                ms.pop(key, None)
            for ck in [k for k in cases if k[0] == "gemini"]:
                cases[ck].pop(arm, None)
            skipped.append(f"{arm} (box profile: no Gemini; its gemini rows aren't compared)")
    return {"passes": passes, "cases": cases, "ms": ms, "skipped": skipped}


def _mean(ps: list[dict], cat: str | None = None) -> float | None:
    if not ps:
        return None
    vals = [(p.get(cat, [0, 0])[0] if cat else sum(v[0] for v in p.values())) for p in ps]
    return statistics.mean(vals)


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def readout(data: dict[str, Any]) -> dict[str, Any]:
    passes, cases = data["passes"], data["cases"]
    out: dict[str, Any] = {"groups": [], "drops": [], "case_flags": [], "control": False, "skipped": data.get("skipped") or []}
    for b, client in sorted({(b, c) for _a, b, c in passes}):
        A, B, C = (passes.get((arm, b, client), []) for arm in "ABC")
        control = bool(C)
        out["control"] = out["control"] or control
        cats = sorted({k for p in A + B + C for k in p})
        rows = []
        for k in cats:
            a, bb, c = _mean(A, k), _mean(B, k), _mean(C, k)
            n = next((p[k][1] for p in A + B if k in p), 0)
            delta = (bb - a) if a is not None and bb is not None else None
            # the band is at least one flip in one pass (1/passes): the first A/A/A run (10-07) had B = A's own code
            # drop 0.33 in a category where C happened not to move, so one control pair alone under-reads the noise
            floor = 1 / max(1, len(A))
            band = max(abs(c - a), floor) if control and a is not None and c is not None else floor
            drop = delta is not None and delta < -1e-9 and -delta > band + 1e-9
            rows.append({"category": k, "n": n, "A": _r(a), "B": _r(bb), "C": _r(c), "delta": _r(delta), "band": _r(band),
                         "drop": drop})
            if drop:
                out["drops"].append({"brain": b, "client": client, "category": k, "delta": _r(delta), "band": _r(band)})
        out["groups"].append({"brain": b, "client": client, "A": [sum(v[0] for v in p.values()) for p in A],
                              "B": [sum(v[0] for v in p.values()) for p in B],
                              "C": [sum(v[0] for v in p.values()) for p in C],
                              "of": [sum(v[1] for v in p.values()) for p in A],
                              "ms": {arm: int(statistics.median(data["ms"][(arm, b, client)]))
                                     for arm in "ABC" if data["ms"].get((arm, b, client))},
                              "categories": rows})
    for (b, client, name), arms in sorted(cases.items()):
        a, bb, c = arms.get("A", []), arms.get("B", []), arms.get("C", [])
        if not a or not bb:
            continue
        swing = max(abs(sum(c) - sum(a)) if c else 0, 1)    # at least one ok answer either way (one flip)
        if abs(sum(a) - sum(bb)) > swing:
            out["case_flags"].append({"brain": b, "client": client, "case": name, "A": f"{sum(a)}/{len(a)}",
                                      "B": f"{sum(bb)}/{len(bb)}", "C": f"{sum(c)}/{len(c)}" if c else None,
                                      "drop": sum(bb) < sum(a)})
    return out


def render(r: dict[str, Any]) -> str:
    lines = [f"(not compared: {s})" for s in r.get("skipped") or []]
    for g in r["groups"]:
        lines.append(f"== {g['brain']} · {g['client']} ==")
        for arm in "ABC":
            if g[arm]:
                lines.append(f"  {arm}: passes {g[arm]} of {g['of']}  median ms {g['ms'].get(arm, '-')}")
        for c in g["categories"]:
            band = f"band {c['band']:.2f}" + ("" if r["control"] else ", no control")
            flag = "  DROP" if c["drop"] else ""
            if c["delta"] is None:   # no B for this brain (a box arm has no Gemini): A and the control only
                lines.append(f"    {c['category']:<12} A {c['A']}/{c['n']}  B –")
                continue
            lines.append(f"    {c['category']:<12} A {c['A']}/{c['n']}  B {c['B']}/{c['n']}  B-A {c['delta']:+.2f}  ({band}){flag}")
    lines.append("cases beyond the A/A swing:" if r["control"] else "cases that moved (no control run):")
    for c in r["case_flags"]:
        lines.append(f"  {c['brain']} {c['client']} {c['case']}: A {c['A']} -> B {c['B']}" + (f" (A' {c['C']})" if c["C"] else "")
                     + ("  drop" if c["drop"] else ""))
    lines.append("DROPS beyond the band: " + (", ".join(f"{d['brain']} {d['client']} {d['category']} {d['delta']:+.2f}"
                                                         for d in r["drops"]) or "none"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A/B readout with an A/A noise band (#1144); counts only")
    ap.add_argument("dir")
    ap.add_argument("--set", choices=["all", "wants_schedule"], default="all")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = readout(load(a.dir, a.set))
    print(json.dumps(r, indent=1) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
