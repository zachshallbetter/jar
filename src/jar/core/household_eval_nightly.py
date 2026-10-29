#!/usr/bin/env python3
"""Nightly household-agent eval on the Titan host (titan-household-eval.timer).

Runs scripts/test_household_agent.py, scripts/turn_eval.py, scripts/retrieval_eval.py, scripts/world_eval.py (the world model:
lookups, privacy leaks, kids' household facts; its own line in /ai/logs/world-eval.jsonl for the scorecard, not in this
total), scripts/items_redteam.py (the items surface: every view × viewer on a fictional household, its own line in
/ai/logs/items-redteam.jsonl; a leak pages) and scripts/privacy_eval.py (plan item 9:
one person's data never reaches another) against the live agent, keeps a score history in
/ai/logs/household-eval.jsonl, and alerts (titan_alert, edge-triggered) when the pass
rate falls below HOUSEHOLD_EVAL_MIN (default 0.85) or drops by 2+ checks from the last
run. The canary answers "is the agent up?"; this answers "is it still any good?".

  python3 scripts/household_eval_nightly.py [--runs 1]
"""
from __future__ import annotations

from typing import Any, Callable
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from titan_alert import EdgeAlert  # noqa: E402

HISTORY = "/ai/logs/household-eval.jsonl"
# On the host, clear the CLI history file directly (the eval's default does it over `ssh titan`).
RESET = "rm -f /ai/projects/titan-local-agent/runtime/titan-local-agent-history.jsonl"


# ── what the night's result says, and who hears it (10-06: a world-privacy leak reported 3 nights running went
# unnoticed inside "still failing, changed" alerts that bundled it with the score) ──

PRIVACY_KEYS = ("privacy", "world-privacy", "privacy-eval", "items-privacy", "items-redteam")   # a leak, or no proof
# A failing check whose reason is the harness, not the answer: the agent restarted, timed out or errored.
_INFRA_RX = re.compile(r"error: |timed out|TimeoutError|HTTP 5\d\d|Connection(?:Refused|Reset)Error|"
                       r"RemoteDisconnected|URLError|Gateway", re.I)
_FAIL_RX = re.compile(r"^\s+([\w-]+): (.*)\((\d+)/(\d+)\)\s*$", re.M)


def split_failures(out: str) -> tuple[list[str], list[str]]:
    """The night's failing checks, as (infrastructure errors, real misses) by name."""
    infra, real = [], []
    for name, reason, _a, _b in _FAIL_RX.findall(out):
        (infra if _INFRA_RX.search(reason) else real).append(name)
    return infra, real


def summary_line(passed: int, total: int, infra: list[str], real: list[str]) -> str:
    """"213/293: 78 errors (infra), 2 real misses (a, b)" — infra apart, so a deploy mid-eval isn't read as a
    regression, and a real miss isn't lost among timeouts."""
    head = f"{passed}/{total}"
    bits = []
    if infra:
        bits.append(f"{len(infra)} error{'s' if len(infra) != 1 else ''} (infra)")
    bits.append(f"{len(real)} real miss{'es' if len(real) != 1 else ''}" + (f" ({', '.join(real[:8])}"
                                                                           + (", …" if len(real) > 8 else "") + ")"
                                                                           if real else ""))
    return head + ": " + ", ".join(bits)


CHRONIC_NIGHTS = 3   # #1517: a real miss this many nights running is chronic, whatever the overall rate


def new_and_chronic(history: list[dict], real: list[str], nights: int = CHRONIC_NIGHTS) -> tuple[list[str], list[str]]:
    """Tonight's real misses as (new: not failing last night; chronic: failing `nights` nights running, tonight
    included). Compared by case name, never by totals: cases are added most nights, so a totals-equal check never
    fired (10-10: 10 chronic misses at 97% raised nothing). A night's misses are its `real` list, or `failing` on
    lines written before #1517; a harness that didn't run (no record) breaks a streak rather than extending it."""
    past = [set(r.get("real", r.get("failing")) or []) for r in history[-(nights - 1):]] if nights > 1 else []
    last = past[-1] if past else None
    new = sorted(n for n in real if last is not None and n not in last)
    chronic = sorted(n for n in real if len(past) == nights - 1 and all(n in night for night in past))
    return new, chronic


QUARANTINE = str(SCRIPTS / "eval_quarantine.json")   # {case: {owner, until: YYYY-MM-DD, why}} (#1517)


def quarantine_state(real: list[str], now: float | None = None, path: str | None = None) -> tuple[set[str], list[str]]:
    """(cases under an unexpired quarantine, expired quarantines as "case (owner, until …)"). A quarantined case is
    known, not new, but is still listed as chronic; an expired one is reported until someone removes or renews it."""
    try:
        q = json.loads(Path(path or os.environ.get("EVAL_QUARANTINE") or QUARANTINE).read_text())
    except (OSError, ValueError):
        return set(), []
    day = time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))
    active, expired = set(), []
    for case, info in sorted((q if isinstance(q, dict) else {}).items()):
        info = info if isinstance(info, dict) else {}
        until = str(info.get("until") or "")
        if until and until >= day:
            active.add(case)
        else:
            expired.append(f"{case} ({info.get('owner', '?')}, until {until or 'unset'})")
    return active, expired


def history_records(limit: int = 30) -> list[dict]:
    try:
        return [json.loads(x) for x in Path(HISTORY).read_text().splitlines()[-limit:] if x.strip()]
    except Exception:
        return []


def notify(problems: dict[str, str], *, rate: float, floor: float, summary: str,
           alert: Callable[..., Any], edge: Any) -> list[str]:
    """Who hears what (→ the kinds sent):
      - any privacy problem pages right away, on its own and every night it persists (level, not edge): a leak must
        never hide inside another alert or go quiet because it was already announced;
      - a night under the bar sends one summary, infra errors apart from real misses;
      - harness breakage (a harness that didn't run, failing unit checks, a regression) stays edge-triggered."""
    sent = []
    priv = {k: v for k, v in problems.items() if k in PRIVACY_KEYS}
    if priv:
        alert("Titan PRIVACY: " + ", ".join(sorted(priv)), "; ".join(f"{k}: {v[:300]}" for k, v in sorted(priv.items())),
              priority="urgent", tags="rotating_light")
        sent.append("privacy")
    if rate < floor:
        alert("Titan household eval under its bar", f"{summary} (bar {floor:.0%})", priority="default", tags="chart")
        sent.append("summary")
    rest = {k: v for k, v in problems.items() if k not in PRIVACY_KEYS and k != "score"}
    if edge.update(rest):
        sent.append("edge")
    return sent


# The card builders and tags shipped 10-05, at unit level, every night (hermetic: each runs exactly as the deploy gate
# runs it, with throwaway stores and the allowlisted environment, so nothing live is touched).
UNIT_TESTS = ("test_bill_due.py", "test_next_stop.py", "test_bills_said.py", "test_chat_tags.py", "test_step_caps.py",
              "test_push_privacy.py")   # 10-07: what Apple sees stays word-free


def run_units() -> tuple[int, int, list[str], str]:
    """(passed, total, failing names, output) over UNIT_TESTS, each through gate_env.sh's allowlist."""
    passed = total = 0
    failing: list[str] = []
    out = ""
    for t in UNIT_TESTS:
        script = ('TEST_STATE="$(mktemp -d)"; trap \'rm -rf "$TEST_STATE"\' EXIT; '
                  'eval "$(sed -n \'/^export DAILY_LOG_DIR=/,/[^\\\\]$/p\' scripts/deploy_titan_node.sh)"; '
                  f'. scripts/gate_env.sh; gate_env_build; gate_python scripts/{t}')
        r = subprocess.run(["bash", "-c", script], cwd=str(SCRIPTS.parent), capture_output=True, text=True, timeout=600)
        text = r.stdout + r.stderr
        out += f"\n-- {t} --\n" + text[-600:]
        m = re.search(r"(\d+) passed, (\d+) failed", text)
        if m:
            p, f = int(m.group(1)), int(m.group(2))
            passed, total = passed + p, total + p + f
            if f or r.returncode:
                failing.append(t)
        else:
            total += 1
            failing.append(t)
    return passed, total, failing, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    a = ap.parse_args()
    t0 = time.time()
    proc = subprocess.run([sys.executable, str(SCRIPTS / "test_household_agent.py"), "--live", "--runs", str(a.runs),
                           "--reset-cmd", RESET], capture_output=True, text=True, timeout=3600)
    out = hh_out = proc.stdout + proc.stderr
    # The turn eval (scripts/turn_eval.py): the Jellybean path, device tools against a fake Mac.
    turn = subprocess.run([sys.executable, str(SCRIPTS / "turn_eval.py")], capture_output=True, text=True, timeout=1800)
    turn_out = turn.stdout + turn.stderr
    out += "\n\n== turn eval ==\n" + turn_out
    # The eval bank (#373): what people stated or corrected becomes (question, answer) pairs, each person's own.
    bank = subprocess.run([sys.executable, str(SCRIPTS / "eval_bank.py"), "harvest"], capture_output=True, text=True, timeout=300)
    out += "\n\n== eval bank ==\n" + bank.stdout + bank.stderr
    # The self-exam (#373): titan is asked the admin's own stated facts and graded in code; ids and pass/fail only.
    exam = subprocess.run([sys.executable, str(SCRIPTS / "self_exam.py"), "--source", "nightly"], capture_output=True,
                          text=True, timeout=1200)
    out += "\n\n== self exam ==\n" + exam.stdout + exam.stderr
    # #595: pairs missed two nights running, diagnosed; "not found" proposed to the owner; re-asked first tomorrow
    fixes = subprocess.run([sys.executable, str(SCRIPTS / "selfexam_fixes.py"), "--apply"], capture_output=True,
                           text=True, timeout=600)
    out += "\n== self-exam misses ==\n" + fixes.stdout + fixes.stderr
    # The red team grows (#373): the local model adds a few new attacks to the bank before the privacy eval runs.
    grow = subprocess.run([sys.executable, str(SCRIPTS / "redteam_grow.py")], capture_output=True, text=True, timeout=600)
    out += "\n\n== red team grow ==\n" + grow.stdout + grow.stderr
    # The privacy eval: turns AS a synthetic adult and kid must never surface Zach's data.
    try:
        priv = subprocess.run([sys.executable, str(SCRIPTS / "privacy_eval.py")], capture_output=True, text=True,
                              timeout=1800)
        priv_out = priv.stdout + priv.stderr
        priv_rc = priv.returncode
    except subprocess.TimeoutExpired as exc:   # #1694: keep what it printed, and the rest of the night's evals
        def _txt(b: object) -> str:
            return b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b or "")
        priv_out = _txt(exc.stdout) + _txt(exc.stderr) + "\nprivacy eval timed out after 1800 s"
        priv_rc = -1
    out += "\n\n== privacy eval ==\n" + priv_out
    # Household retrieval (goal item 3): right items found, nothing in the context when nothing is stored.
    retr = subprocess.run([sys.executable, str(SCRIPTS / "retrieval_eval.py")], capture_output=True, text=True, timeout=600)
    retr_out = retr.stdout + retr.stderr
    out += "\n\n== retrieval eval ==\n" + retr_out
    # The world model (#364): logs its own line (source nightly) that the scorecard reads; a leak alerts here.
    world = subprocess.run([sys.executable, str(SCRIPTS / "world_eval.py"), "--source", "nightly"],
                           capture_output=True, text=True, timeout=2400)
    world_out = world.stdout + world.stderr
    out += "\n\n== world eval ==\n" + world_out
    # The items surface's red team (10-07): every view × viewer on a fictional household in throwaway stores; its own
    # line in /ai/logs/items-redteam.jsonl for the scorecard; a leak pages like world privacy.
    itr = subprocess.run([sys.executable, str(SCRIPTS / "items_redteam.py"), "--source", "nightly",
                          "--log", "/ai/logs/items-redteam.jsonl"], capture_output=True, text=True, timeout=1200)
    itr_out = itr.stdout + itr.stderr
    out += "\n\n== items red team ==\n" + itr_out
    # The truth eval (#385; Zach, 10-01: nightly, both brains): its own line per brain in truth-eval.jsonl,
    # which the scorecard's response area reads. Eval turns: nothing remembered, read tools only.
    truth = subprocess.run([sys.executable, str(SCRIPTS / "truth_eval.py"), "--brain", "both", "--source", "nightly",
                            "--summary-jsonl", "/ai/logs/truth-eval.jsonl"], capture_output=True, text=True, timeout=2700)
    out += "\n\n== truth eval ==\n" + truth.stdout + truth.stderr
    units = run_units()
    out += "\n\n== unit checks (10-05 features) ==\n" + units[3]
    # KB tuning (#370): proposed floors and top-k from the week's trace, as a report; nothing is applied
    try:
        import kb_tuning
        tdir = Path("/ai/logs/kb-tuning")
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"sweep-{time.strftime('%Y%m%d')}.json").write_text(json.dumps(kb_tuning.sweep(14), indent=2))
        out += "\n\n== kb tuning ==\nsweep written to /ai/logs/kb-tuning/ (proposals only)"
    except Exception as exc:  # noqa: BLE001 - never fails the nightly
        out += f"\n\n== kb tuning ==\nsweep failed: {exc}"
    log = "/ai/logs/household-eval-%s.log" % time.strftime("%Y%m%d-%H%MZ", time.gmtime())
    try:
        Path(log).write_text(out)
    except Exception:
        log = ""
    m = re.search(r"checks passed:\s*(\d+)/(\d+)", hh_out)
    tm = re.search(r"checks passed:\s*(\d+)/(\d+)", turn_out)
    failures = re.findall(r"^\s+([\w-]+): .*\((\d+)/(\d+)\)\s*$", out, re.M)
    failures_text = _FAIL_RX.findall(out)   # (case id, check text, …): the reasons kept for eval_chronic_report.py
    problems: dict[str, str] = {}
    if not m:
        passed = total = 0
        problems["eval"] = "eval did not produce a score (exit %d); see %s" % (proc.returncode, log)
    else:
        passed, total = int(m.group(1)), int(m.group(2))
        if tm:
            passed, total = passed + int(tm.group(1)), total + int(tm.group(2))
        else:
            problems["turn-eval"] = "turn eval did not produce a score (exit %d); see %s" % (turn.returncode, log)
        pm = re.search(r"privacy checks passed:\s*(\d+)/(\d+)", priv_out)
        if pm:
            passed, total = passed + int(pm.group(1)), total + int(pm.group(2))
            if int(pm.group(1)) < int(pm.group(2)):
                # Any leak is its own alert, whatever the overall rate.
                leaked = re.findall(r"^\s+([\w-]+): surfaced Zach's ([\w, ]+) to (\w+) \(\d+/\d+\)", priv_out, re.M)
                problems["privacy"] = "privacy eval %s/%s: %s; see %s" % (
                    pm.group(1), pm.group(2), "; ".join(f"{n} ({k} to {w})" for n, k, w in leaked) or "failing", log)
        else:
            problems["privacy-eval"] = "privacy eval did not produce a score (exit %d); see %s" % (priv_rc, log)
        nr = re.search(r"privacy not reached \(time budget \d+ s\): (\d+)", priv_out)
        if nr:   # cases never run: no proof they're safe tonight, said as its own problem (a privacy key: it alerts)
            problems["privacy-eval"] = "privacy eval ran out of time: %s cases not reached; see %s" % (nr.group(1), log)
        rm = re.search(r"retrieval checks passed:\s*(\d+)/(\d+)", retr_out)
        if rm:
            passed, total = passed + int(rm.group(1)), total + int(rm.group(2))
            if int(rm.group(1)) < int(rm.group(2)):
                missed = re.findall(r"^\s+([\w-]+): ", retr_out, re.M)
                problems["retrieval"] = "retrieval eval %s/%s: %s; see %s" % (rm.group(1), rm.group(2), ", ".join(missed), log)
        else:
            problems["retrieval-eval"] = "retrieval eval did not produce a score (exit %d); see %s" % (retr.returncode, log)
        wm = re.search(r"world privacy leaks:\s*(\d+)/(\d+)", world_out)
        if wm and int(wm.group(1)) > 0:
            problems["world-privacy"] = "world model leaked to a viewer in %s/%s privacy checks; see %s" % (
                wm.group(1), wm.group(2), log)
        elif not wm and "holds nothing to ask about" not in world_out:
            problems["world-eval"] = "world eval did not finish (exit %d); see %s" % (world.returncode, log)
        im = re.search(r"items privacy leaks:\s*(\d+)/(\d+)", itr_out)
        if im and int(im.group(1)) > 0:
            problems["items-privacy"] = "the items surface leaked to a viewer in %s/%s view checks; see %s" % (
                im.group(1), im.group(2), log)
        elif not im:
            problems["items-redteam"] = "items red team did not finish (exit %d); see %s" % (itr.returncode, log)
        passed, total = passed + units[0], total + units[1]
        if units[2]:
            problems["units"] = "unit checks failing: %s; see %s" % (", ".join(units[2]), log)
        rate = passed / total if total else 0.0
        floor = float(os.environ.get("HOUSEHOLD_EVAL_MIN", "0.85"))
        names = ", ".join(n for n, _, _ in failures) or "none"
        if rate < floor:
            problems["score"] = "%d/%d (< %.0f%%); failing: %s" % (passed, total, floor * 100, names)
    infra, real = split_failures(out)
    new, chronic = new_and_chronic(history_records(), real)
    quarantined, q_expired = quarantine_state(real)
    new = [n for n in new if n not in quarantined]
    if m and q_expired:
        problems["quarantine-expired"] = "expired quarantines: " + ", ".join(q_expired)
    if m and new:   # #1517: a case that passed last night and misses tonight, named (edge: announced once)
        problems["regression"] = "new misses: %s; see %s" % (", ".join(new), log)
    if m and chronic:   # #1517: misses CHRONIC_NIGHTS nights running are named, not silent (edge: when the set changes)
        problems["chronic"] = "failing %d+ nights: %s" % (CHRONIC_NIGHTS, ", ".join(chronic))
    summary = summary_line(passed, total, infra, real)
    if "score" in problems:
        problems["score"] = summary
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "passed": passed, "total": total,
           "failing": [n for n, _, _ in failures], "infra_errors": len(infra), "real_misses": len(real),
           "real": real, "reasons": {n: r.strip()[:200] for n, r, _a, _b in failures_text if n in real}, "new": new, "chronic": chronic,
           "minutes": round((time.time() - t0) / 60, 1), "log": log}
    try:
        with open(HISTORY, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    import titan_alert
    floor = float(os.environ.get("HOUSEHOLD_EVAL_MIN", "0.85"))
    notify(problems, rate=(passed / total if total else 0.0), floor=floor, summary=summary,
           alert=titan_alert.alert, edge=EdgeAlert("household-eval-harness", "Titan household eval harness"))
    print("household eval %s in %.1f min%s" % (summary, rec["minutes"],
                                              " | " + "; ".join(problems.values()) if problems else ""))
    # A missed check is a result, not a unit failure: it goes out as the alert
    # above and in the history. Exit non-zero only when a harness did not run,
    # so systemd (and Jellybean's unit list) shows "failed" for real breakage.
    broken = [k for k in problems if k.endswith("-eval") or k == "eval"]
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
