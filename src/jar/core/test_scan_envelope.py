#!/usr/bin/env python3
# gate: hermetic   (fictional data, no network, no live model, no real files, a throwaway salt)
"""titan-node#1500: the scan envelope reads type, sender, account (a keyed hash only), dates, amounts and identifiers from
a scan's text; every derived field keeps its owner and audience; the committed score matches a fresh run and beats the
baseline; the live model is off by default and checked when on."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks  # noqa: E402
import scan_envelope as E  # noqa: E402
import scans_bench as B  # noqa: E402
import scans_score as S  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'BAD '} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


salt_before = hooks.SALT_FILE
batch = B.generate(1499)
truth = batch["truth"]
pred = E.pipeline(batch["scans"])
res = json.loads(json.dumps(S.score(batch, pred, "scan_envelope:pipeline")))
base = json.loads(S.BASELINE_FILE.read_text())
t, b = res["tasks"], base["tasks"]

# --- the score ---------------------------------------------------------------------------------------------------------
committed = S.BASELINE_FILE.parent / "envelope.json"
check("the committed envelope score matches a fresh run", committed.exists() and json.loads(committed.read_text()) == res, str(committed))
check("type accuracy beats the baseline", t["type"]["accuracy"] > b["type"]["accuracy"] and t["type"]["macro_f1"] > b["type"]["macro_f1"])
check("type accuracy is at least 0.95", t["type"]["accuracy"] >= 0.95)
check("sender f1 beats the baseline and reaches 0.95", t["sender"]["f1"] > b["sender"]["f1"] and t["sender"]["f1"] >= 0.95)
check("account f1 beats the baseline (which read none)", t["account"]["f1"] > (b["account"]["f1"] or 0) and t["account"]["recall"] >= 0.95)
check("amount and due date do not fall below the baseline", t["amount"]["f1"] >= b["amount"]["f1"] and t["due_date"]["f1"] >= b["due_date"]["f1"])
check("dedupe and waiting are what they were (not this slice)", t["dedupe"]["f1"] == b["dedupe"]["f1"] and t["waiting"]["f1"] == b["waiting"]["f1"])
check("privacy: 0 leaks, on every viewer and every trap", t["privacy"]["leaks"] == 0 and not any(t["privacy"]["by_trap"].values()))
check("no unattributed rows: the benchmark's own accounts are scored as keys, not numbers", t["account"]["predicted"] > 0)

# --- privacy of the envelope itself -----------------------------------------------------------------------------------------
envs = {sid: d["envelope"] for sid, d in pred["docs"].items()}
scanner = {s["id"]: s["scanner"] for s in batch["scans"]}
check("every envelope is its scanner's: owner and audience", all(e["record"]["owner"] == scanner[sid] and e["record"]["audience"] == [scanner[sid]] for sid, e in envs.items()))
check("the owner of a doc in the prediction is the scanner", all(d["owner"] == scanner[sid] for sid, d in pred["docs"].items()))
blob = json.dumps(envs)
check("no account, claim, policy or offer code is kept: only keyed hashes", "TEST-" not in blob)
check("an account is a 24-hex keyed hash", all(len(e["fields"]["account"]) == 24 for e in envs.values() if e["fields"]["account"]))
two = [s for s in batch["scans"] if truth[s["id"]]["trap"] and truth[s["id"]]["trap"]["category"] == "two_owner_rescan"]
check("one text scanned by two people stays two owners", len({scanner[s["id"]] for s in two}) >= 2 and all(envs[s["id"]]["record"]["owner"] == s["scanner"] for s in two))
named = [s for s in batch["scans"] if truth[s["id"]]["trap"] and truth[s["id"]]["trap"]["category"] == "printed_name"]
check("a printed name never sets the owner", named and all(envs[s["id"]]["record"]["owner"] == s["scanner"] for s in named))
inj = [s["id"] for s in batch["scans"] if truth[s["id"]]["injection"]]
check("injection text is flagged and has no effect", inj and all(envs[i]["fields"]["instruction_like"] and not pred["docs"][i].get("effects") for i in inj))
check("an instruction in a scan is read as junk, never acted on", all(envs[i]["fields"]["type"] == "junk" for i in inj))
check("only injected scans are flagged instruction-like", {i for i, e in envs.items() if e["fields"]["instruction_like"]} == set(inj))
check("the envelope says it is lossy and that rules read it", all(e["interpretation"] == {"label": "lossy-disclosed", "evaluator": "rules-standin"} for e in envs.values()))
check("a throwaway salt only: the real salt file is untouched", hooks.SALT_FILE == salt_before)

# --- the fields ------------------------------------------------------------------------------------------------------------
bill = next(s for s in batch["scans"] if truth[s["id"]]["type"] == "bill" and s["source"] == "scan")
f = envs[bill["id"]]["fields"]
tb = truth[bill["id"]]
check("a bill: sender, amount, issued and due dates", (f["sender"], f["amount"], f["due_date"], f["issued"]) == (tb["sender"], tb["amount"], tb["due_date"], tb["date"]), str(f))
rc = next(s for s in batch["scans"] if truth[s["id"]]["type"] == "receipt")
check("a receipt: paid date and amount", (envs[rc["id"]]["fields"]["paid_date"], envs[rc["id"]]["fields"]["amount"]) == (truth[rc["id"]]["paid_date"], truth[rc["id"]]["amount"]))
ap = next(s for s in batch["scans"] if truth[s["id"]]["type"] == "appointment")
check("an appointment has an event date and no due date", envs[ap["id"]]["fields"]["event_date"] and not envs[ap["id"]]["fields"]["due_date"])
em = next(s for s in batch["scans"] if s["source"] == "email")
check("an email's sender is its From name", envs[em["id"]]["fields"]["sender"] == truth[em["id"]]["sender"])
cl = next(s for s in batch["scans"] if truth[s["id"]]["type"] == "claim_letter")
check("a claim letter files under its claim number's hash, and lists it as an identifier", envs[cl["id"]]["fields"]["account"] in [i["key"] for i in envs[cl["id"]]["fields"]["identifiers"]])
check("a bill's identifiers are hooks' own keyed hashes", all(i["kind"].startswith("id:") and len(i["key"]) == 24 for e in envs.values() for i in e["fields"]["identifiers"]))
with E.bench_salt():
    same = E.account_key({"text": "Account: TEST-AAA-0001"}, "bill") == hooks.keyed("id", "TESTAAA0001")
check("the account key is hooks.keyed over the digits-and-letters (the linker's own key)", same)
check("a junk mailer's amount is not an amount", all(envs[s["id"]]["fields"]["amount"] is None for s in batch["scans"] if truth[s["id"]]["type"] in ("junk", "warranty")))

# --- the scorer's canon -------------------------------------------------------------------------------------------------------
tr = {"a": {"account": "TEST-X-1"}}
check("score_field compares keys when given a canon", S.score_field(tr, {"a": {"account": "K1"}}, "account", lambda v: "K1")["tp"] == 1
      and S.score_field(tr, {"a": {"account": "TEST-X-1"}}, "account")["tp"] == 1)

# --- the live model: off by default, checked when on --------------------------------------------------------------------------
old = os.environ.pop("SCAN_ENVELOPE_LIVE", None)
check("the live model is off by default", not E.live_on())
for v, want in (("1", True), ("0", False), ("", False), ("1 # on", False), ("true", False)):
    os.environ["SCAN_ENVELOPE_LIVE"] = v
    check(f"SCAN_ENVELOPE_LIVE={v!r} is {'on' if want else 'off'}", E.live_on() == want)
os.environ.pop("SCAN_ENVELOPE_LIVE")
seen: list[str] = []


def fake(answer):
    def ask(prompt, schema, url, **kw):
        seen.append(prompt)
        if isinstance(answer, Exception):
            raise answer
        return answer, "fake"
    return ask


scan = bill
check("a good model answer is used", E.live_read(scan, fake({"type": "statement", "sender": tb["sender"]})) == {"type": "statement", "sender": tb["sender"]})
check("the scan's text went in as marked untrusted", "⟦untrusted source=file" in seen[-1] and bill["text"].split("\n")[0] in seen[-1])
check("a type outside the kinds is refused", "type" not in E.live_read(scan, fake({"type": "wire_transfer", "sender": "x"})))
check("a sender that is not in the scan's text is refused", "sender" not in E.live_read(scan, fake({"type": "bill", "sender": "Invented Corp"})))
check("a model that is down leaves the rules' answer", E.live_read(scan, fake(RuntimeError("down"))) == {})
with E.bench_salt():
    env = E.envelope(scan, live=lambda s: E.live_read(s, fake({"type": "statement", "sender": tb["sender"]})))
check("with a model, the envelope says so and keeps owner and audience", env["interpretation"]["evaluator"] == "model+rules"
      and env["fields"]["type"] == "statement" and env["record"]["audience"] == [scan["scanner"]])
if old is not None:
    os.environ["SCAN_ENVELOPE_LIVE"] = old

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all scan envelope checks passed'}")
sys.exit(1 if failures else 0)
