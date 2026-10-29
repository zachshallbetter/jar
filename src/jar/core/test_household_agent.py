#!/usr/bin/env python3
"""Functional eval for the Titan household agent.

Drives the REAL agent via `titan agent prompt --tools`: stores facts, then asks
questions in a FRESH session (conversation history cleared) so answers must come
from the knowledge base via retrieval — not from in-context memory. Asserts the
answers contain the expected values. Catches the failure modes seen in practice:
false "Saved" (no actual ingest), blank recalls, and wrong/hallucinated answers.

Scenarios: scripts/household-agent-eval.jsonl. Each line:
  {"name": str,
   "ingest": [prompt, ...],                       # facts to store first
   "checks": [{"q": str,
               "expect_all": [substr, ...],        # all must appear (case-insensitive)
               "expect_any": [substr, ...]}, ...]} # at least one must appear

Usage:
  python3 scripts/test_household_agent.py [--runs N] [--manifest PATH]
                                          [--index-wait SECONDS] [--reset-cmd CMD]

--reset-cmd clears the host-side conversation history between turns (default targets
the standard titan-local-agent workspace over the `titan` SSH alias). Override if your
host alias or agent root differ.

NOTE: the eval saves made-up facts through the real agent. By default the knowledge base and
the record store (contacts, bills) are snapshotted first and rolled back after, so nothing it
saves persists; --keep-kb leaves them. Eval turns never write to the household timeline.
Runs only with --live, one at a time. A kill that skips the rollback leaves its snapshot at
/ai/tmp/household-eval-pending.json, and the next run refuses until a person checks it.
Every turn of a run carries its id (AGENT_EVAL_RUN, #346): what it writes is stamped eval_run=<id>,
and the rollback and verify act on stamped items only, so a note or record someone really writes
during the run (~7 min, 03:30) is never reverted. Items the Mac's sync adds or changes are never touched.
Left as before: an item the eval deletes is restored from the snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TITAN = str(SCRIPT_DIR / "titan.sh")
DEFAULT_MANIFEST = SCRIPT_DIR / "household-agent-eval.jsonl"
DEFAULT_RESET_CMD = (
    "ssh titan 'rm -f /ai/projects/titan-local-agent/runtime/"
    "titan-local-agent-history.jsonl'"
)
CALL_TIMEOUT = 120


def kb_hygiene(*args: str) -> str:
    """Run scripts/kb_hygiene.py on the Titan host (directly when we are on it, else over ssh)."""
    import shlex
    import socket
    inner = ("cd /ai/projects/titan-node && set -a && . /ai/projects/titan-local-agent/agent.env && set +a && "
             "python3 scripts/kb_hygiene.py " + " ".join(shlex.quote(a) for a in args))
    on_host = socket.gethostname().startswith("titan") or os.environ.get("TITAN_REMOTE_EXEC") == "1"
    cmd = ["bash", "-c", inner] if on_host else ["ssh", os.environ.get("TITAN_SSH_ALIAS", "titan"), inner]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(f"kb_hygiene {args[0]} failed: {(res.stderr or res.stdout).strip()[:300]}")
    return res.stdout.strip()


def reset_history(reset_cmd: str) -> None:
    subprocess.run(reset_cmd, shell=True, capture_output=True, timeout=30)


# This run's id: every turn carries it, and what those turns write is stamped with it (#346).
EVAL_RUN = "he-" + time.strftime("%Y%m%d%H%M%S")


def agent_prompt(text: str) -> str:
    """Run one --tools prompt through the agent and return its final answer text."""
    res = subprocess.run(
        ["bash", TITAN, "agent", "prompt", "--tools", "--context", "none", text],
        capture_output=True, text=True, timeout=CALL_TIMEOUT,
        env={**os.environ, "AGENT_TURN_SOURCE": "eval", "AGENT_EVAL_RUN": EVAL_RUN},
    )
    return (res.stdout or "").strip()


def check_passes(answer: str, check: dict) -> bool:
    low = answer.lower()
    if not answer:
        return False
    expect_all = check.get("expect_all", [])
    expect_any = check.get("expect_any", [])
    expect_absent = check.get("expect_absent", [])
    if expect_all and not all(s.lower() in low for s in expect_all):
        return False
    if expect_any and not any(s.lower() in low for s in expect_any):
        return False
    if expect_absent and any(s.lower() in low for s in expect_absent):
        return False
    return bool(expect_all or expect_any or expect_absent)


def load_scenarios(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1, help="repeat the full suite N times (reliability)")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--index-wait", type=int, default=6, help="seconds to wait for ingest->retrieval indexing")
    ap.add_argument("--reset-cmd", default=DEFAULT_RESET_CMD)
    ap.add_argument("--keep-kb", action="store_true",
                    help="leave the eval's facts in the knowledge base (default: snapshot before, roll back after)")
    ap.add_argument("--live", action="store_true",
                    help="required: this eval writes made-up household facts through the live agent")
    args = ap.parse_args()
    if not args.live:
        # 10-01: a sweep of every test_*.py ran this twice under a 120 s alarm; both were killed before
        # their rollback and 9 fixture notes (a fake wifi password, door codes) stayed in Zach's KB.
        print("Not run: this eval writes made-up household facts through the live agent. Pass --live "
              "(household_eval_nightly.py does).")
        return 0
    import fcntl
    import signal
    lock = open("/tmp/household-eval.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Not run: another household eval is running (/tmp/household-eval.lock).")
        return 1

    def _stop(signum, _frame):
        raise SystemExit(f"stopped by signal {signum}")   # unwinds through the finally below: the rollback runs
    for sig in (signal.SIGTERM, signal.SIGALRM, signal.SIGHUP):
        signal.signal(sig, _stop)
    snap = None
    if not args.keep_kb:
        # The eval saves made-up household facts through the real agent. Snapshot the KB and the
        # record store first and roll back after, so none linger and a real note or contact that
        # shares a name ("Vet") is restored.
        # A fixed path, so a run that was killed outright (no finally) blocks the next one until checked.
        snap = "/ai/tmp/household-eval-pending.json"
        print("kb: " + kb_hygiene("snapshot", "--out", snap))
    try:
        return _run(args)
    finally:
        if snap:
            for sig in (signal.SIGTERM, signal.SIGALRM, signal.SIGHUP, signal.SIGINT):
                signal.signal(sig, signal.SIG_IGN)   # the rollback itself is never cut short
            print("kb: " + kb_hygiene("rollback", "--snapshot", snap, "--eval-run", EVAL_RUN))
            # Proof, not hope: nothing the eval wrote may remain. A leftover makes kb_hygiene exit
            # non-zero, which raises here and fails the run loudly (the nightly alerts on it).
            print("kb: " + kb_hygiene("verify", "--snapshot", snap, "--remove", "--eval-run", EVAL_RUN))


def _run(args) -> int:

    scenarios = load_scenarios(Path(args.manifest))
    # tally[(scenario, q)] = [passes, total]
    tally: dict[tuple[str, str], list[int]] = {}

    for run in range(1, args.runs + 1):
        if args.runs > 1:
            print(f"\n===== run {run}/{args.runs} =====")
        for sc in scenarios:
            name = sc["name"]
            reset_history(args.reset_cmd)
            for ing in sc.get("ingest", []):
                agent_prompt(ing)
            if sc.get("ingest"):
                time.sleep(args.index_wait)
            for check in sc["checks"]:
                reset_history(args.reset_cmd)  # fresh session -> force KB retrieval
                ans = agent_prompt(check["q"])
                ok = check_passes(ans, check)
                key = (name, check["q"])
                t = tally.setdefault(key, [0, 0])
                t[0] += int(ok); t[1] += 1
                status = "PASS" if ok else "FAIL"
                preview = ans.replace("\n", " ")[:90] or "<empty>"
                print(f"  [{status}] {name}: {check['q']}  -> {preview}")

    print("\n===== summary =====")
    total_pass = total = 0
    failed = []
    for (name, q), (p, n) in tally.items():
        total_pass += p; total += n
        if p < n:
            failed.append((name, q, p, n))
        if args.runs > 1:
            print(f"  {p}/{n}  {name}: {q}")
    print(f"checks passed: {total_pass}/{total}")
    if failed:
        print("FAILURES:")
        for name, q, p, n in failed:
            print(f"  {name}: {q} ({p}/{n})")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
