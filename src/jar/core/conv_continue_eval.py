#!/usr/bin/env python3
"""The carry-over eval (titan-node#1016 step 5): each case of evalsets/conv_continue.jsonl asked twice as an eval
turn, with CONV_CONTINUE's block (eval_continue true) and without it, the case's previous chat sent as `eval_prior`.

Eval-safe: source=eval, so nothing is remembered and every write is simulated or answered by turn_eval's fake Mac
(#1011/#1012); the previous chat travels in the request (eval_prior) and is never written to anyone's stream, so
there is no scratch stream to clean up. Prints ids, kinds and which checks missed, never a question or a reply.

  python3 scripts/conv_continue_eval.py [--signal recent|edge] [--case cc01] [--json OUT]      (on titan)

The bar to turn CONV_CONTINUE on (Coordination, 10-05): the continuing cases improve clearly with the block, the
controls agree (both arms pass), privacy 2/2 in both arms, and the nightly turn_eval doesn't drop.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
BANK = HERE / "evalsets" / "conv_continue.jsonl"


def load(path: Path = BANK) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def score(arm: dict[str, Any], text: str, tools: set[str]) -> list[str]:
    """Which of an arm's checks missed: key names only, never the text."""
    low = (text or "").lower()
    miss = []
    if any(w.lower() not in low for w in arm.get("expect_all") or []):
        miss.append("expect_all")
    for k in ("expect_any", "expect_any_also"):
        if arm.get(k) and not any(w.lower() in low for w in arm[k]):
            miss.append(k)
    if arm.get("expect_tools_any") and not tools & set(arm["expect_tools_any"]):
        miss.append("expect_tools_any")
    if any(w.lower() in low for w in arm.get("forbid") or []):
        miss.append("forbid")
    if tools & set(arm.get("forbid_tools") or []):
        miss.append("forbid_tools")
    return miss


def run_case(case: dict[str, Any], ask, signal: str = "recent") -> dict[str, Any]:
    import eval_names as names
    prior = names.out(case["prior"])
    out: dict[str, Any] = {"id": case["id"], "kind": case["kind"], "continues": case["truth"]["continues"]}
    for arm, on in (("with", True), ("without", False)):
        t = ask(case["ask"], [], extra={"eval_prior": prior, "eval_continue": on, "eval_signal": signal})
        tools = set(t.called) | t.tools()
        out[arm] = ["error"] if t.error else score(case["truth"][arm], t.text, tools)
        out[f"{arm}_ms"] = t.ms
    return out


def group(kind: str, prior_owner: str, continues: bool) -> str:
    if prior_owner != "self":
        return "privacy"
    return "continue" if continues else "control"


def report(rows: list[dict[str, Any]], bank: list[dict[str, Any]]) -> dict[str, Any]:
    owner = {c["id"]: c["prior"]["owner"] for c in bank}
    by_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "with": 0, "without": 0})
    by_group: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "with": 0, "without": 0, "both": 0, "agree": 0})
    for r in rows:
        for table, key in ((by_kind, r["kind"]), (by_group, group(r["kind"], owner.get(r["id"], "self"), r["continues"]))):
            table[key]["n"] += 1
            table[key]["with"] += not r["with"]
            table[key]["without"] += not r["without"]
            if "both" in table[key]:
                table[key]["both"] += (not r["with"] and not r["without"])
                # the arms agree: both pass, or both miss the same checks (the block changed nothing)
                table[key]["agree"] += (not r["with"]) == (not r["without"]) and sorted(r["with"]) == sorted(r["without"])
    g = {k: dict(v) for k, v in by_group.items()}
    cont, ctrl, priv = g.get("continue", {}), g.get("control", {}), g.get("privacy", {})
    bar = {
        "continue_gain": (cont.get("with", 0) - cont.get("without", 0)) if cont else None,
        "controls_agree": f"{ctrl.get('agree', 0)}/{ctrl.get('n', 0)}",   # the block didn't change a control's answer
        "controls_pass": f"{ctrl.get('both', 0)}/{ctrl.get('n', 0)}",     # ...and it was right in both arms
        "privacy_both_arms": f"{priv.get('both', 0)}/{priv.get('n', 0)}",
    }
    return {"by_kind": {k: dict(v) for k, v in sorted(by_kind.items())}, "by_group": g, "bar": bar}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case")
    ap.add_argument("--json", help="write the per-case results and the report here")
    ap.add_argument("--signal", choices=["recent", "edge"], default="recent",
                    help="which chat the block continues: the 30-min stand-in, or KB's live continues check (#1019)")
    a = ap.parse_args()
    import turn_eval as te
    bank = [c for c in load() if not a.case or c["id"] == a.case]
    rows = []
    for c in bank:
        r = run_case(c, te.ask, a.signal)
        rows.append(r)
        print(f"{r['id']} {r['kind']:<22} with={'ok' if not r['with'] else ','.join(r['with'])} "
              f"without={'ok' if not r['without'] else ','.join(r['without'])}")
    rep = report(rows, bank)
    print(f"\nsignal: {a.signal}")
    print("\nper kind (passed with / without, of n):")
    for k, v in rep["by_kind"].items():
        print(f"  {k:<22} {v['with']}/{v['n']} vs {v['without']}/{v['n']}")
    print("\nper group:")
    for k, v in rep["by_group"].items():
        print(f"  {k:<10} with {v['with']}/{v['n']} · without {v['without']}/{v['n']} · both {v['both']}/{v['n']} · "
              f"agree {v['agree']}/{v['n']}")
    print(f"\nbar: continue gain {rep['bar']['continue_gain']:+d} · controls agree {rep['bar']['controls_agree']} "
          f"(pass {rep['bar']['controls_pass']}) · privacy (both arms) {rep['bar']['privacy_both_arms']}")
    if a.json:
        Path(a.json).write_text(json.dumps({"rows": rows, "report": rep}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
