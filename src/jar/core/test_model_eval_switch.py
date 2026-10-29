#!/usr/bin/env python3
# gate: hermetic   (temp dirs, fake restarts/warm-ups/harness; no model is loaded, no service is touched)
"""#408 steps 2–3: the nightly candidate eval (model_eval.py) and the switch with automatic rollback
(model_switch.py), with every restart, warm-up, canary and harness run faked. Hermetic.

Run: python3 scripts/test_model_eval_switch.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TMP = Path(tempfile.mkdtemp(prefix="titan-model-eval-"))
for k, v in {"LOCAL_MODEL_DIR": "local-model.d", "LOCAL_MODEL_LEDGER": "models.jsonl", "LOCAL_MODEL_REQUESTS": "requests",
             "MODEL_EVAL_DIR": "model-eval", "TITAN_DEPLOY_HOLD": "deploy.hold", "AGENT_TURN_LOG": "turns.jsonl",
             "PROACTIVE_DB": "proactive.db"}.items():
    os.environ[k] = str(TMP / v)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_model as lm  # noqa: E402
import model_eval as me  # noqa: E402
import model_switch as ms  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}: {detail}")


root, ledger, reqs = TMP / "local-model.d", TMP / "models.jsonl", TMP / "requests"
root.mkdir()
base = {"NAME": "base", "LLAMA_SERVER": "/x/llama-server", "MODEL_FILE": "/x/a.gguf", "ARGS": "--ctx-size 32768", "GPUS": "1"}
cand = dict(base, NAME="cand", ARGS="--ctx-size 32768 --cache-type-k q8_0")
for m in (base, cand):
    (root / f"{m['NAME']}.env").write_text(lm.render(m))
lm.switch("base", by="test", root=root, ledger=ledger, decision="init")


def ledger_lines():
    return [json.loads(x) for x in ledger.read_text().splitlines()]


# --- The card's buttons
check("answer: only an admin can switch", ms.answer("switch:cand", "p_kid", "kid", reqs, root)[0] is False
      and not reqs.exists(), "")
check("answer: Keep changes nothing", ms.answer("keep", "p_z", "admin", reqs, root) == (True, "Keeping the current model.")
      and not reqs.exists(), "")
check("answer: a manifest that's gone is refused", ms.answer("switch:nope", "p_z", "admin", reqs, root)[0] is False, "")
check("answer: the current model is already it", ms.answer("switch:base", "p_z", "admin", reqs, root)[0] is True
      and not reqs.exists(), "")
done, said = ms.answer("switch:cand", "p_z", "admin", reqs, root)
files = list(reqs.glob("*.json"))
check("answer: Switch queues one request (no restart in the API server)", done and len(files) == 1
      and json.loads(files[0].read_text())["to"] == "cand" and json.loads(files[0].read_text())["by"] == "p_z", said)
check("card actions: Switch and Keep as replies", [a["value"] for a in ms.card_actions("cand")] == ["switch:cand", "keep"], "")

# --- Applying: success
calls, notes = [], []
run_ok = lambda cmd, timeout=600: (calls.append(cmd) or (0, "ok"))  # noqa: E731
res = ms.apply_pending(reqs, root=root, ledger=ledger, run=run_ok, healthy=lambda: True,
                       notify=lambda t, d: notes.append(t))
check("apply: the request is handled once and removed", len(res) == 1 and not list(reqs.glob("*.json")), str(res))
check("apply: current is the candidate, the ledger says who asked", lm.current_name(root) == "cand"
      and ledger_lines()[-1]["decision"] == "switch" and ledger_lines()[-1]["by"] == "p_z", str(ledger_lines()[-1]))
check("apply: restarted, warmed, canaried", any("restart" in " ".join(c) for c in calls)
      and any("warm_local.py" in " ".join(c) for c in calls) and any("agent_canary.py" in " ".join(c) for c in calls), str(calls))
check("apply: admins hear it worked", notes and notes[-1] == "titan's local model is now cand", str(notes))

# --- Applying: a failure rolls back by itself
lm.switch("base", by="test", root=root, ledger=ledger)
notes.clear()
def run_warm_fails(cmd, timeout=600):
    return (1, "warm-up: shape mac 400") if "warm_local.py" in " ".join(cmd) and lm.current_name(root) == "cand" else (0, "ok")
res = ms.apply_one({"to": "cand", "by": "p_z"}, root=root, ledger=ledger, run=run_warm_fails, healthy=lambda: True,
                   notify=lambda t, d: notes.append((t, d)))
check("rollback: a failed warm-up flips back to the previous model by itself",
      res["ok"] is False and res["rolled_back"] and lm.current_name(root) == "base"
      and ledger_lines()[-1]["decision"] == "rollback", str(res))
check("rollback: admins hear what failed", notes and "failed; titan is back on base" in notes[-1][0]
      and "400" in notes[-1][1], str(notes))
res = ms.apply_one({"to": "cand", "by": "p_z"}, root=root, ledger=ledger, run=run_ok, healthy=lambda: False,
                   notify=lambda t, d: notes.append((t, d)))
check("rollback: a model that never comes up is rolled back too (and says both failed)",
      res["ok"] is False and lm.current_name(root) == "base" and "so did going back" in notes[-1][0], str(notes[-1]))

# --- The rule
B = me.summarize([{"truth": {"iphone": 33, "mac": 31}, "http_400": 0, "timeouts": 1, "p50_ms": 13000, "canary_ok": True},
                  {"truth": {"iphone": 35, "mac": 32}, "http_400": 0, "timeouts": 0, "p50_ms": 14000, "canary_ok": True}])
check("summarize: means, spreads, totals", B["truth_iphone"] == 34 and B["spread_iphone"] == 2 and B["timeouts"] == 1
      and B["p50_ms"] == 13500, str(B))
good = me.summarize([{"truth": {"iphone": 33, "mac": 31}, "http_400": 0, "timeouts": 0, "p50_ms": 12000, "canary_ok": True}])
check("rule: as good within the margin passes", me.decide(B, good) == (True, []), str(me.decide(B, good)))
for label, change, word in (("an HTTP 400 fails", {"http_400": 1}, "HTTP 400"), ("more timeouts fail", {"timeouts": 2}, "timeouts"),
                            ("lower truth beyond the margin fails", {"truth_mac": 29}, "truth (mac)"),
                            ("p50 over 1.25× fails", {"p50_ms": 17000}, "p50"), ("a failed canary fails", {"canary_ok": False}, "canary")):
    passed, why = me.decide(B, dict(good, **change))
    check(f"rule: {label}", not passed and any(word in w for w in why), str(why))
check("table: both sides, every row", "truth, iPhone" in me.table("base", "cand", B, good) and "canary" in me.table("base", "cand", B, good), "")

# --- One night, faked
order, cards = [], []
def fake_truth(client, out, run):
    side = lm.current_name(root)
    order.append(side)
    return {"base": {"iphone": 34, "mac": 32}, "cand": {"iphone": 35, "mac": 32}}[side][client]
rec = me.night("cand", passes=2, root=root, ledger=ledger, run=run_ok, healthy=lambda: True, truth=fake_truth,
               metrics=lambda a, b: {"http_400": 0, "timeouts": 0, "p50_ms": 13000}, notify=lambda t, d, a: cards.append((t, d, a)))
sides = [s for i, s in enumerate(order) if i % 2 == 0]
check("night: baseline and candidate take turns, B C B C", sides == ["base", "cand", "base", "cand"], str(order))
check("night: the baseline is back at the end", lm.current_name(root) == "base", lm.current_name(root))
check("night: a pass is a ledger line and a card with Switch", rec["decision"] == "eval-pass"
      and ledger_lines()[-1]["decision"] == "eval-pass" and cards[-1][2] == ms.card_actions("cand"), str(rec["why"]))
def crashing_truth(client, out, run):
    if lm.current_name(root) == "cand":
        raise RuntimeError("harness crashed")
    return 34
try:
    me.night("cand", passes=1, root=root, ledger=ledger, run=run_ok, healthy=lambda: True, truth=crashing_truth,
             metrics=lambda a, b: {"http_400": 0, "timeouts": 0, "p50_ms": 1})
except RuntimeError:
    pass
check("night: even if a pass crashes, the baseline comes back", lm.current_name(root) == "base", lm.current_name(root))
cards.clear()
rec = me.night("cand", passes=1, root=root, ledger=ledger, run=run_ok, healthy=lambda: True,
               truth=lambda c, o, r: 20 if lm.current_name(root) == "cand" else 34,
               metrics=lambda a, b: {"http_400": 0, "timeouts": 0, "p50_ms": 1}, notify=lambda t, d, a: cards.append((t, d, a)))
check("night: a fail keeps the model, says why, offers no switch", rec["decision"] == "eval-fail" and cards[-1][2] is None
      and "truth" in cards[-1][1], str(cards[-1][:2]))

# --- The turn log, the window, the hold
log = TMP / "turns.jsonl"
log.write_text("\n".join(json.dumps(r) for r in [
    {"ts": 100, "pin": "local", "ms": 1000}, {"ts": 101, "pin": "local", "ms": 3000},
    {"ts": 102, "pin": "local", "error": "HTTP 400: request exceeds the available context size"},
    {"ts": 103, "pin": "local", "budget_hit": True}, {"ts": 104, "ms": 99999}, {"ts": 900, "pin": "local", "ms": 5}]) + "\n")
mt = me.turn_metrics(100, 104, log)
check("turn metrics: pinned turns in the window only (400, timeout, p50)", mt == {"http_400": 1, "timeouts": 1, "p50_ms": 2000}, str(mt))
la = ZoneInfo("America/Los_Angeles")
check("window: 01:40 PT in, 03:10 out, 01:20 out",
      me.in_window(datetime(2026, 10, 4, 1, 40, tzinfo=la)) and not me.in_window(datetime(2026, 10, 4, 3, 10, tzinfo=la))
      and not me.in_window(datetime(2026, 10, 4, 1, 20, tzinfo=la)), "")
hold = TMP / "deploy.hold"
hold.write_text("9999999999 someone's run\n")
free_future = me.hold_free(hold, now=1000)
hold.write_text("1 an old one\n")
check("hold: someone's future hold skips the night; an expired one doesn't", not free_future and me.hold_free(hold, now=1000), "")
(root / "candidate").symlink_to("cand.env")
check("staged: the candidate symlink names it", me.staged(root) == "cand", str(me.staged(root)))

# --- The card goes through proactive.card_action like the other decided-in-code cards
import proactive  # noqa: E402
card = proactive.add_card("A new local model passed", "table", kind="report", source=ms.SOURCE, to="admins",
                          actions=ms.card_actions("cand"))
cid = card["id"] if isinstance(card, dict) else card
for p in reqs.glob("*.json"):
    p.unlink()
out = proactive.card_action(cid, "a0", person="p_z", role="admin")
check("card: an admin's Switch through card_action queues the request and answers", out.get("ok") and
      len(list(reqs.glob("*.json"))) == 1 and "Switching to cand" in out.get("said", ""), str(out))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
