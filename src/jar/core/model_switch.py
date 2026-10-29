#!/usr/bin/env python3
"""Switching the local model, with automatic rollback (#408 steps 2–3).

titan never switches on its own (decided 10-02, #389): the nightly eval (model_eval.py) sends admins a
card with the table and two buttons, Switch and Keep. Pressing Switch only drops a request file here
(`answer`, called from proactive.card_action, inside the API server). `apply` (a oneshot run by
titan-model-switch.path when a request appears, or by hand) performs it:

  1. local_model.switch to the candidate (the ledger says who asked);
  2. restart titan-local-agent and wait for :8010 to answer;
  3. warm the prompt cache (warm_local.py) and run the canary's local checks.

Any step failing rolls back by itself: current flips back, the service restarts and warms, and admins
get a card saying what failed. Every switch and rollback is a ledger line (models.jsonl).

  python3 scripts/model_switch.py apply            # process pending requests (the path unit runs this)
  python3 scripts/model_switch.py status           # current, pending requests, the last ledger lines
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_model as lm  # noqa: E402

REQUESTS = Path(os.environ.get("LOCAL_MODEL_REQUESTS", str(lm.MODEL_DIR / "requests")))
LOCAL_HEALTH = os.environ.get("LOCAL_MODEL_HEALTH", "http://127.0.0.1:8010/health")
SOURCE = "model-switch"   # the card source proactive.card_action hands to `answer`


# ---------------------------------------------------------------------------
# The card's buttons (inside the API server: no restart here, only a request file)


def card_actions(candidate: str) -> list[dict[str, str]]:
    """The eval card's buttons: Switch (to that manifest) and Keep."""
    return [{"label": "Switch", "kind": "reply", "value": f"switch:{candidate}"},
            {"label": "Keep the current model", "kind": "reply", "value": "keep"}]


def answer(value: str, person: str | None, role: str | None, requests: Path | None = None,
           root: Path | None = None) -> tuple[bool, str]:
    """A press on the card: admins only. Switch queues the request (model_switch apply does it);
    Keep changes nothing. Returns (done, what to say)."""
    requests = requests or REQUESTS
    root = root or lm.MODEL_DIR
    if role != "admin":
        return False, "Only an admin can switch titan's model."
    if value == "keep":
        return True, "Keeping the current model."
    if not value.startswith("switch:"):
        return False, "That isn't a choice on this card."
    name = value.split(":", 1)[1]
    if not (root / f"{name}.env").is_file():
        return False, f"There's no manifest {name} on titan any more."
    if lm.current_name(root) == name:
        return True, f"{name} is already the model."
    requests.mkdir(parents=True, exist_ok=True)
    req = {"ts": round(time.time(), 3), "to": name, "by": person or "admin"}
    path = requests / f"{int(req['ts'] * 1000)}-{name}.json"
    path.write_text(json.dumps(req))
    return True, f"Switching to {name}: titan restarts its local model and checks it; it rolls back on its own if anything fails."


# ---------------------------------------------------------------------------
# Applying a request (a oneshot outside the API server)


def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)[-2000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def restart_local(run: Callable[..., tuple[int, str]] = _run) -> tuple[bool, str]:
    code, out = run(["sudo", "-n", "systemctl", "restart", "titan-local-agent"], timeout=120)
    return code == 0, out


def wait_healthy(url: str = LOCAL_HEALTH, timeout: float = 180, sleep: Callable[[float], None] = time.sleep,
                 get: Callable[[str], int] | None = None) -> bool:
    """Until llama-server answers 200 on /health (it's 503 while the model loads)."""
    def _get(u: str) -> int:
        try:
            with urllib.request.urlopen(u, timeout=5) as r:
                return r.status
        except Exception:  # noqa: BLE001
            return 0
    get = get or _get
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if get(url) == 200:
            return True
        sleep(3)
    return False


def warm_and_check(run: Callable[..., tuple[int, str]] = _run) -> tuple[bool, str]:
    """warm_local.py (every shape warms or says why), then the canary's local checks."""
    code, out = run([sys.executable, str(HERE / "warm_local.py")], timeout=600)
    if code != 0:
        return False, "warm-up failed: " + out[-300:]
    code, out = run([sys.executable, str(HERE / "agent_canary.py"), "--no-alert", "--only", "local_model", "tool_turn"],
                    timeout=600)
    return code == 0, ("canary failed: " + out[-300:]) if code else "warm and canary ok"


def bring_up(run=_run, healthy=wait_healthy) -> tuple[bool, str]:
    ok, out = restart_local(run)
    if not ok:
        return False, "restart failed: " + out[-300:]
    if not healthy():
        return False, "the local model didn't come up within 3 minutes"
    return warm_and_check(run)


def apply_one(req: dict[str, Any], root: Path | None = None, ledger: Path | None = None, run=_run, healthy=wait_healthy,
              notify: Callable[[str, str], Any] | None = None) -> dict[str, Any]:
    """Switch to req["to"], bring it up and check it; roll back on any failure. Returns what happened."""
    root = root or lm.MODEL_DIR
    ledger = ledger or lm.LEDGER
    before = lm.current_name(root)
    name = req["to"]
    lm.switch(name, by=req.get("by", "admin"), root=root, ledger=ledger, decision="switch", note="from the eval card")
    ok, why = bring_up(run, healthy)
    if ok:
        if notify:
            notify(f"titan's local model is now {name}", f"Switched from {before}; warm-up and canary passed. "
                   "The previous model is kept 14 days.")
        return {"ok": True, "to": name, "from": before}
    # Roll back by itself (decided 10-02): the previous manifest, restarted, warmed.
    lm.switch(before, by="model_switch rollback", root=root, ledger=ledger, decision="rollback", note=why[:300])
    back, why_back = bring_up(run, healthy)
    if notify:
        notify(f"Switching to {name} failed; titan is back on {before}" if back
               else f"Switching to {name} failed, and so did going back to {before}",
               why + ("" if back else "\nRollback: " + why_back))
    return {"ok": False, "to": name, "from": before, "why": why, "rolled_back": back}


def _notify_admins(title: str, detail: str) -> None:
    try:
        import proactive
        proactive.add_card(title, detail, kind="report", urgency=2, source=SOURCE, to="admins",
                           key="model-switch:result")
    except Exception as exc:  # noqa: BLE001
        print("card not sent:", exc, file=sys.stderr)


def apply_pending(requests: Path | None = None, **kw) -> list[dict[str, Any]]:
    """Every pending request, oldest first; each file goes once it's been handled."""
    requests = requests or REQUESTS
    out = []
    for path in sorted(requests.glob("*.json")) if requests.is_dir() else []:
        try:
            req = json.loads(path.read_text())
        except ValueError:
            path.unlink(missing_ok=True)
            continue
        path.unlink(missing_ok=True)   # first: a crash mid-switch never repeats it on the next run
        out.append(apply_one(req, **kw))
    return out


def main(argv: list[str] | None = None) -> int:
    cmd = (argv or sys.argv[1:] or ["status"])[0]
    if cmd == "apply":
        results = apply_pending(notify=_notify_admins)
        for r in results:
            print(json.dumps(r))
        return 0 if all(r["ok"] for r in results) else 1
    if cmd == "status":
        print("current:", lm.current_name())
        pending = sorted(REQUESTS.glob("*.json")) if REQUESTS.is_dir() else []
        print("pending:", [p.name for p in pending])
        if lm.LEDGER.is_file():
            for line in lm.LEDGER.read_text().splitlines()[-5:]:
                print(line)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
