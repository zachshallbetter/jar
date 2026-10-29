#!/usr/bin/env python3
"""The nightly eval of a staged local-model candidate (#408 step 2), ending in a card, never a switch.

Stage a candidate by pointing local-model.d/candidate at its manifest (scripts/local_model.py writes
manifests; `ln -sfn <name>.env /ai/config/local-model.d/candidate`). titan-model-eval.timer runs this
nightly in the 01:30–03:00 PT window (decided 10-02); with no candidate it does nothing.

In place (titan's GPUs are full and :8198 isn't paused, so there's no room to run it beside the live
model): under the deploy hold, the baseline and the candidate take turns as titan-local-agent, B C B C,
each restarted, warmed and canaried (model_switch.bring_up); local truth_eval as the iPhone and the Mac
runs on each, and the turn log gives HTTP 400s, timeouts and p50 for those turns (pin == local). The
baseline always comes back at the end, whatever happened.

The rule (#408, decided as proposed): the candidate passes only with no HTTP 400, timeouts no more
than the baseline's, truth per client no lower than the baseline's minus the margin (max(1, the
spread measured tonight)), p50 at most 1.25× the baseline's, and its canary passing. A pass sends
admins a card with the table and Switch / Keep (model_switch.py does the switch, with rollback); a
fail sends the table and why. Either way a ledger line, and the candidate is unstaged.

  python3 scripts/model_eval.py run [--now] [--passes 2]   # the timer runs `run`
  python3 scripts/model_eval.py status
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_model as lm  # noqa: E402
import local_time  # noqa: E402
import model_switch as ms  # noqa: E402

TURN_LOG = Path(os.environ.get("AGENT_TURN_LOG", "/ai/logs/agent-turns.jsonl"))
HOLD = Path(os.environ.get("TITAN_DEPLOY_HOLD", "/ai/tmp/deploy.hold"))
WORK = Path(os.environ.get("MODEL_EVAL_DIR", "/ai/tmp/model-eval"))
WINDOW = (1.5, 3.0)   # 01:30–03:00 America/Los_Angeles
CLIENTS = ("iphone", "mac")


# ---------------------------------------------------------------------------
# The rule (pure)


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """One side's passes → per-client mean truth, spread, total 400s and timeouts, median p50."""
    out: dict[str, Any] = {"passes": len(runs)}
    for c in CLIENTS:
        vals = [r["truth"][c] for r in runs if r.get("truth", {}).get(c) is not None]
        out[f"truth_{c}"] = round(statistics.mean(vals), 2) if vals else None
        out[f"spread_{c}"] = (max(vals) - min(vals)) if len(vals) > 1 else 0
    out["http_400"] = sum(r.get("http_400", 0) for r in runs)
    out["timeouts"] = sum(r.get("timeouts", 0) for r in runs)
    p50s = [r["p50_ms"] for r in runs if r.get("p50_ms")]
    out["p50_ms"] = int(statistics.median(p50s)) if p50s else None
    out["canary_ok"] = all(r.get("canary_ok", True) for r in runs)
    return out


def decide(base: dict[str, Any], cand: dict[str, Any]) -> tuple[bool, list[str]]:
    """Whether the candidate may be offered, and every reason it may not."""
    why = []
    if cand["http_400"]:
        why.append(f"{cand['http_400']} HTTP 400 (context overflow)")
    if cand["timeouts"] > base["timeouts"]:
        why.append(f"timeouts {cand['timeouts']} > baseline {base['timeouts']}")
    for c in CLIENTS:
        b, k = base.get(f"truth_{c}"), cand.get(f"truth_{c}")
        if b is None or k is None:
            why.append(f"no truth score for the {c}")
            continue
        margin = max(1, base.get(f"spread_{c}", 0), cand.get(f"spread_{c}", 0))
        if k < b - margin:
            why.append(f"truth ({c}) {k} < baseline {b} − {margin}")
    if base.get("p50_ms") and cand.get("p50_ms") and cand["p50_ms"] > 1.25 * base["p50_ms"]:
        why.append(f"p50 {cand['p50_ms'] / 1000:.1f}s > 1.25 × baseline {base['p50_ms'] / 1000:.1f}s")
    if not cand.get("canary_ok", True):
        why.append("its canary failed")
    return not why, why


def table(base_name: str, cand_name: str, base: dict[str, Any], cand: dict[str, Any]) -> str:
    def f(v, unit=""):
        return "–" if v is None else f"{v}{unit}"
    rows = [("truth, iPhone", f(base["truth_iphone"]), f(cand["truth_iphone"])),
            ("truth, Mac", f(base["truth_mac"]), f(cand["truth_mac"])),
            ("HTTP 400", f(base["http_400"]), f(cand["http_400"])),
            ("timeouts", f(base["timeouts"]), f(cand["timeouts"])),
            ("p50", f(base["p50_ms"] and round(base["p50_ms"] / 1000, 1), " s"), f(cand["p50_ms"] and round(cand["p50_ms"] / 1000, 1), " s")),
            ("canary", "ok" if base["canary_ok"] else "failed", "ok" if cand["canary_ok"] else "failed")]
    lines = [f"{'':14} {base_name[:28]:>28}  {cand_name[:28]:>28}"] + [f"{a:14} {b:>28}  {c:>28}" for a, b, c in rows]
    return "\n".join(lines) + f"\n({base['passes']} + {cand['passes']} passes, local truth_eval as the iPhone and the Mac)"


def turn_metrics(since: float, until: float, log: Path = TURN_LOG) -> dict[str, Any]:
    """HTTP 400s, timeouts and p50 of the local-pinned turns in [since, until]."""
    ms_ok, n400, nto = [], 0, 0
    if log.is_file():
        for line in log.open():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("pin") != "local" or not (since <= r.get("ts", 0) <= until + 5):
                continue
            err = str(r.get("error") or "")
            if "400" in err or "exceeds the available context" in err:
                n400 += 1
            elif r.get("budget_hit") or "timed out" in err.lower() or "timeout" in err.lower():
                nto += 1
            elif not err and r.get("ms"):
                ms_ok.append(r["ms"])
    return {"http_400": n400, "timeouts": nto, "p50_ms": int(statistics.median(ms_ok)) if ms_ok else None}


def in_window(now: datetime | None = None) -> bool:
    t = now or datetime.now(local_time.zone())
    h = t.hour + t.minute / 60
    return WINDOW[0] <= h < WINDOW[1]


# ---------------------------------------------------------------------------
# One night


def _truth(client: str, out: Path, run=ms._run) -> int | None:
    code, _ = run([sys.executable, str(HERE / "truth_eval.py"), "--brain", "local", "--client", client,
                   "--jsonl", str(out), "--source", "model-eval"], timeout=1800)
    if not out.is_file():
        return None
    return sum(1 for line in out.read_text().splitlines() if line.strip() and json.loads(line).get("ok"))


def one_pass(side: str, label: str, root: Path, ledger: Path, run=ms._run, healthy=ms.wait_healthy,
             truth: Callable[..., int | None] = _truth, metrics=turn_metrics) -> dict[str, Any]:
    """Make `side` titan's local model (if it isn't), bring it up, run truth as both clients."""
    if lm.current_name(root) != side:
        lm.switch(side, by="model_eval", root=root, ledger=ledger, decision="eval", note=label)
    up, why = ms.bring_up(run, healthy)
    res: dict[str, Any] = {"side": side, "label": label, "canary_ok": up, "truth": {}}
    if not up:
        res["why"] = why
        return res
    t0 = time.time()
    for c in CLIENTS:
        res["truth"][c] = truth(c, WORK / f"{label}-{c}.jsonl", run)
    res.update(metrics(t0, time.time()))
    return res


def night(candidate: str, passes: int = 2, root: Path | None = None, ledger: Path | None = None, run=ms._run,
          healthy=ms.wait_healthy, truth=_truth, metrics=turn_metrics, notify=None) -> dict[str, Any]:
    """B C B C …, then the baseline back; the verdict, the ledger line and the card."""
    root = root or lm.MODEL_DIR
    ledger = ledger or lm.LEDGER
    baseline = lm.current_name(root)
    WORK.mkdir(parents=True, exist_ok=True)
    runs: dict[str, list] = {"base": [], "cand": []}
    try:
        for i in range(passes):
            runs["base"].append(one_pass(baseline, f"base{i + 1}", root, ledger, run, healthy, truth, metrics))
            runs["cand"].append(one_pass(candidate, f"cand{i + 1}", root, ledger, run, healthy, truth, metrics))
            if not runs["cand"][-1]["canary_ok"]:
                break   # the candidate didn't come up: no point in another pass
    finally:
        if lm.current_name(root) != baseline:
            lm.switch(baseline, by="model_eval", root=root, ledger=ledger, decision="eval-end", note="baseline back")
        ms.bring_up(run, healthy)
    base, cand = summarize(runs["base"]), summarize(runs["cand"])
    ok, why = decide(base, cand)
    tab = table(baseline, candidate, base, cand)
    rec = {"ts": round(time.time(), 3), "from": baseline, "to": candidate,
           "decision": "eval-pass" if ok else "eval-fail", "by": "model_eval", "base": base, "cand": cand, "why": why}
    with open(ledger, "a") as f:
        f.write(json.dumps(rec) + "\n")
    if notify:
        if ok:
            notify(f"A new local model passed tonight's eval: {candidate}",
                   "It's at least as good as the current one on every check. Switch to it?\n\n" + tab,
                   ms.card_actions(candidate))
        else:
            notify(f"The candidate model {candidate} didn't pass tonight's eval",
                   "titan keeps the current model. Why: " + "; ".join(why) + "\n\n" + tab, None)
    return rec


def _notify(title: str, detail: str, actions) -> None:
    try:
        import proactive
        proactive.add_card(title, detail, kind="report", urgency=1, source=ms.SOURCE, to="admins",
                           key="model-eval:latest", actions=actions)
    except Exception as exc:  # noqa: BLE001
        print("card not sent:", exc, file=sys.stderr)


def staged(root: Path | None = None) -> str | None:
    link = (root or lm.MODEL_DIR) / "candidate"
    return Path(os.readlink(link)).name.removesuffix(".env") if link.is_symlink() else None


def hold_free(hold: Path = HOLD, now: float | None = None) -> bool:
    """No one else holds deploys (a future epoch in the hold file is someone's run)."""
    if not hold.is_file():
        return True
    try:
        return float(hold.read_text().split()[0]) <= (now or time.time())
    except (ValueError, IndexError):
        return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--now", action="store_true", help="outside the 01:30–03:00 PT window (an owner's run)")
    p.add_argument("--passes", type=int, default=2)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    cand = staged()
    if a.cmd == "status":
        print("current:", lm.current_name(), "| candidate:", cand)
        return 0
    if not cand:
        print("no candidate staged (local-model.d/candidate): nothing to do")
        return 0
    if not a.now and not in_window():
        print("outside the 01:30–03:00 PT window: not tonight")
        return 0
    if not hold_free():
        print("someone holds deploys (/ai/tmp/deploy.hold): skipped tonight")
        return 0
    problems = lm.check(lm.load(cand))
    if problems:
        _notify(f"The candidate model {cand} can't be evaluated", "; ".join(problems), None)
        (lm.MODEL_DIR / "candidate").unlink(missing_ok=True)
        return 1
    HOLD.parent.mkdir(parents=True, exist_ok=True)
    HOLD.write_text(f"{int(time.time()) + 100 * 60} model-eval: {cand} (#408)\n")
    try:
        rec = night(cand, passes=a.passes, notify=_notify)
    finally:
        HOLD.unlink(missing_ok=True)
        (lm.MODEL_DIR / "candidate").unlink(missing_ok=True)   # evaluated: stage it again to re-run
    print(json.dumps({k: rec[k] for k in ("decision", "why")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
