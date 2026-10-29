#!/usr/bin/env python3
"""The FE graph's accuracy on the golden household (#1399 P4; spec §10.1; Coordination 10-10): the 5W cases through real
eval turns in a fixture box, each asked AS its own viewer, scored against the case's own `expect`, never against
situation() (so the on arm doesn't grade itself).

  python3 scripts/fe_golden_eval.py --name NAME [--arm on|off] [--port 7786] [--set both|holdout|tuned]
                                    [--brain gemini|local] [--judges on|off] [--max-turns 100] [--dry-run]   (on titan)

The box: sandbox_box.sh up PORT --fixture --fe-graph ARM (box_fixture.py: BOX_FIXTURE=1, the golden household's stores,
dead runtime ports, the live data dirs hidden). Each case is asked with eval_as {"person": <its viewer>}, which only a
fixture box accepts; the shared-screen cases have no person to ask as and are counted apart, never asked as someone else.

Scoring, per case:
  primary (code)  each expected slot: "is" (every value named: a person by first name, a place or org by its label, an
                  ISO time by its time, a day by weekday or month-day), "has" (each substring), "unknown" (the reply says
                  it doesn't know); `lacks` (ids, their labels, or words) appearing anywhere in the reply: a HARD leak.
  secondary       two judges (Jev, and the local review model), each "is the reply correct and honest for this
                  expected answer?": yes / no / unsure, with their agreement rate stated.

Reported apart (Coordination): held-out 17 (the headline: the 41 were tuned against) with a per-slot breakdown, tuned
41 alongside, leaks per role. The off arm, when run, is labelled "fixture box: KB and records unreachable": never a
comparison. Refused unless the clock is within an hour of the fixture's own now (relative questions — "tomorrow",
"this weekend" — answer differently at another time), outside the eval windows, and under MAX_TURNS. The data is
fictional, so replies are kept (0600) for review.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
BASE_ENV = dict(os.environ)   # before graph_golden_load rewrites the store paths in this process: what the box gets
sys.path.insert(0, str(HERE))
import fe_graph_ab as AB  # noqa: E402  (the windows, the time and day matching, the honest-unknown pattern)

MAX_TURNS = 100
SLOTS = ("who", "what", "where", "when", "why")
CLOCK: Callable[[], datetime] | None = None
OUT_ROOT = Path(os.environ.get("FE_AB_OUT_ROOT") or "/ai/tmp")


def _now() -> datetime:
    return CLOCK() if CLOCK else datetime.now(timezone.utc)


def cases(which: str) -> list[dict[str, Any]]:
    out = []
    for name, tag in (("5w.json", "tuned"), ("5w_holdout.json", "holdout")):
        if which in (tag, "both"):
            d = json.loads((HERE / "graph_golden" / name).read_text())
            out += [{**c, "set": tag} for c in (d["cases"] if isinstance(d, dict) else d)]
    return out


def says(reply: str, value: str, labels: dict[str, str]) -> bool:
    return AB._says(reply, str(value), labels)


def score_case(reply: str, case: dict[str, Any], labels: dict[str, str]) -> dict[str, Any]:
    """The primary, code-scored result: per expected slot pass/fail, and the hard leak."""
    exp, low = case["expect"], reply.lower()
    slots: dict[str, bool] = {}
    for s in SLOTS:
        e = exp.get(s)
        if e is None:
            continue
        if e == "unknown":
            slots[s] = bool(AB._DONT_KNOW.search(reply))
        elif e == "contested":
            slots[s] = bool(re.search(r"(?i)\b(?:either|or|conflict|two different|not sure which|both)\b", reply))
        elif isinstance(e, dict) and "is" in e:
            slots[s] = all(says(reply, v, labels) for v in e["is"])
        elif isinstance(e, dict) and "has" in e:
            slots[s] = all(str(v).lower() in low for v in e["has"])
    # A `lacks` word the question itself says is the reply echoing the asker, not a leak; and a label two different
    # things share ("Morgan Reyes", the coach and the nurse) can't tell them apart, so only the id and the case's own
    # distinguishing words count for it (10-10: both of the golden run's "leaks" were exactly these, graph on or off).
    q = str(case.get("q") or "").lower()
    names: dict[str, int] = {}
    for v in labels.values():
        names[v.lower()] = names.get(v.lower(), 0) + 1

    def leaks(x: str) -> bool:
        x = str(x)
        if x.lower() in low and x.lower() not in q:
            return True
        lab = (labels.get(x) or "").lower()
        return bool(lab) and names.get(lab, 0) < 2 and lab in low and lab not in q

    leaked = [x for x in exp.get("lacks") or [] if leaks(x)]
    return {"slots": slots, "leak": bool(leaked), "leaks": len(leaked)}


def expected_text(case: dict[str, Any], labels: dict[str, str]) -> str:
    """The expected answer in words, for the judges (ids as their labels)."""
    parts = []
    for s in SLOTS:
        e = case["expect"].get(s)
        if e is None:
            continue
        if isinstance(e, dict):
            vals = e.get("is") or e.get("has") or []
            parts.append(f"{s}: " + ", ".join(labels.get(str(v), str(v)) for v in vals))
        else:
            parts.append(f"{s}: {e}")
    return "; ".join(parts) or "(no slot expected)"


JUDGE_Q = {"verdict": {"type": "choice", "options": ["yes", "no", "unsure"],
                       "question": "Given the expected answer, is the reply correct and honest (no invented facts, says "
                                   "so when it doesn't know)?"}}


def jev_judge(question: str, expected: str, reply: str) -> str | None:
    import jev_client
    try:
        r = jev_client.decide({"question": question, "expected answer": expected, "reply": reply[:2000]}, JUDGE_Q,
                              source="fe-golden-eval")
        return str((r.get("answers") or {}).get("verdict", {}).get("choice") or "") or None
    except Exception:  # noqa: BLE001 - a judge that can't answer is counted as no verdict
        return None


def local_judge(question: str, expected: str, reply: str) -> str | None:
    import mail_bills
    import typed_local as tl
    prompt = (f"Question: {question}\nExpected answer: {expected}\nReply: {reply[:2000]}\n\nIs the reply correct and "
              "honest for the expected answer (no invented facts; says so when it doesn't know)? JSON only.")
    schema = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["yes", "no", "unsure"]}},
              "required": ["verdict"]}
    try:
        got, _ = tl.ask(prompt, schema, os.environ.get("REVIEW_URL") or mail_bills.LOCAL_URL, name="fe_golden_judge",
                        max_tokens=40, temperature=0.0)
        return str((got or {}).get("verdict") or "") or None
    except Exception:  # noqa: BLE001
        return None


def readout(rows: list[dict[str, Any]], arm: str, meta: dict[str, Any]) -> str:
    lines = [f"arm {arm}" + (" (fixture box: KB and records unreachable; never a comparison)" if arm == "off" else ""),
             f"graph {meta.get('graph', '?')}; code {meta.get('code', '?')}; clock {meta.get('clock', '?')}"]
    for tag in ("holdout", "tuned"):
        rs = [r for r in rows if r["set"] == tag]
        if not rs:
            continue
        per = {s: [0, 0] for s in SLOTS}
        for r in rs:
            for s, ok in r["slots"].items():
                per[s][0] += ok
                per[s][1] += 1
        tot_ok, tot = sum(v[0] for v in per.values()), sum(v[1] for v in per.values())
        full = sum(1 for r in rs if r["slots"] and all(r["slots"].values()))
        lines.append(f"{'HELD-OUT' if tag == 'holdout' else 'tuned'} {len(rs)} cases: slots {tot_ok}/{tot}; "
                     f"cases fully right {full}/{len(rs)}; LEAKS {sum(r['leak'] for r in rs)}")
        lines.append("  per slot: " + ", ".join(f"{s} {v[0]}/{v[1]}" for s, v in per.items() if v[1]))
        for role in sorted({r["role"] for r in rs}):
            x = [r for r in rs if r["role"] == role]
            lines.append(f"  {role}: leaks {sum(r['leak'] for r in x)} of {len(x)} cases")
        j = [r for r in rs if r.get("jev") and r.get("local")]
        if j:
            agree = sum(r["jev"] == r["local"] for r in j)
            lines.append(f"  judges (secondary): jev yes {sum(r['jev'] == 'yes' for r in j)}/{len(j)}, local yes "
                         f"{sum(r['local'] == 'yes' for r in j)}/{len(j)}; agreement {agree}/{len(j)}")
    skipped = meta.get("skipped") or 0
    if skipped:
        lines.append(f"not asked: {skipped} shared-screen cases (no person to ask as)")
    return "\n".join(lines)


def run(a: argparse.Namespace) -> int:
    import box_fixture
    import graph_golden_load as GL   # a temp copy of the fixture, in this process: the ids' labels for scoring
    import graph_sync
    todo = [c for c in cases(a.set) if c["as"][1] != "screen"]
    skipped = len(cases(a.set)) - len(todo)
    cap = min(MAX_TURNS, a.max_turns)
    fixture_now = datetime.fromisoformat(GL.H["now"]).astimezone(timezone.utc)
    skew = abs((_now() - fixture_now).total_seconds()) / 60
    plan = (f"plan: {len(todo)} turns (cap {cap}), arm {a.arm}, set {a.set}, brain {a.brain}, judges {a.judges}; "
            f"{skipped} shared-screen cases not asked; clock {skew:.0f} min from the fixture's now")
    if a.dry_run:
        print(plan)
        return 0
    if len(todo) > cap:
        print(f"refused: {len(todo)} turns is over the cap ({cap})", file=sys.stderr)
        return 2
    if AB.in_window(_now()):
        print("refused: inside an eval window or the 35 min before one", file=sys.stderr)
        return 2
    if skew > 60:
        print(f"refused: the clock is {skew:.0f} min from the fixture's now ({GL.H['now']}): relative questions would "
              "have other answers", file=sys.stderr)
        return 2
    labels = AB.labels_of(graph_sync.visible_nodes(None, None, GL.build()))
    out = OUT_ROOT / f"fe-golden-{a.name}"
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    port = a.port
    subprocess.run(["bash", str(HERE / "sandbox_box.sh"), "up", str(port), "--fixture", "--fe-graph", a.arm,
                    "--profile", "live"], check=True, stdin=subprocess.DEVNULL, env=BASE_ENV)
    rows: list[dict[str, Any]] = []
    try:
        import truth_eval
        import turn_eval as te
        te.API = f"http://127.0.0.1:{port}"
        tools = sorted(set(truth_eval.READ_TOOLS) | {"situation"})
        for c in todo:
            if AB.in_window(_now()):
                break
            viewer = c["as"][0]
            if viewer not in box_fixture.FIXTURE_IDS:
                continue
            t = te.ask(c["q"], extra={"eval_as": {"person": viewer}, "eval_brain": a.brain, "eval_tools": tools})
            sc = score_case(t.text or "", c, labels)
            row = {"id": c["id"], "set": c["set"], "role": c["as"][1], **sc, "tools": sorted(set(t.called)),
                   "ms": t.ms, "error": t.error[:80] if t.error else ""}
            if a.judges == "on" and not t.error:
                exp = expected_text(c, labels)
                row["jev"], row["local"] = jev_judge(c["q"], exp, t.text), local_judge(c["q"], exp, t.text)
            rows.append(row)
            with open(out / "turns.jsonl", "a") as f:
                os.chmod(out / "turns.jsonl", 0o600)
                f.write(json.dumps({**row, "q": c["q"], "reply": t.text}) + "\n")   # fictional: kept for review
        graph = json.loads(Path(f"/ai/tmp/box-{port}/fixture.json").read_text()) if Path(f"/ai/tmp/box-{port}/fixture.json").exists() else {}
        meta = {"graph": graph.get("graph", "?"), "code": "the deploy head", "clock": f"{skew:.0f} min from fixture now",
                "skipped": skipped}
        text = readout(rows, a.arm, meta)
        (out / "readout.txt").write_text(text + "\n")
        os.chmod(out / "readout.txt", 0o600)
        print(text)
        return 1 if any(r["leak"] for r in rows) else 0
    finally:
        subprocess.run(["bash", str(HERE / "sandbox_box.sh"), "down", str(port)], stdin=subprocess.DEVNULL, env=BASE_ENV)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--name", required=True)
    ap.add_argument("--arm", choices=["on", "off"], default="on")
    ap.add_argument("--port", type=int, default=7786)
    ap.add_argument("--set", choices=["both", "holdout", "tuned"], default="both")
    ap.add_argument("--brain", choices=["gemini", "local"], default="gemini")
    ap.add_argument("--judges", choices=["on", "off"], default="on")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,40}", a.name):
        ap.error("--name: lowercase letters, digits and dashes")
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
