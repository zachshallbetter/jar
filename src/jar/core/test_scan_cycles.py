#!/usr/bin/env python3
# gate: hermetic   (fictional data, no network, no live model, no real files, a throwaway salt)
"""titan-node#1502: bills, statements and receipts attach to account cycles. The benchmark's bill-cycle key and Waiting scores
rise over the dedupe's; a receipt settles the open cycle it pays and is never an unpaid bill; a cycle belongs to its account's
owner (two people's accounts at one company stay two); attaching never widens an audience; the household's day, not
titan's, decides what is due; the committed score matches a fresh run."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks  # noqa: E402
import local_time  # noqa: E402
import scan_cycles as C  # noqa: E402
import scan_dedupe as D  # noqa: E402
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
truth, scans = batch["truth"], batch["scans"]
scanner = {s["id"]: s["scanner"] for s in scans}
pred = C.pipeline(scans)
res = json.loads(json.dumps(S.score(batch, pred, "scan_cycles:pipeline")))
dedupe_res = json.loads((S.BASELINE_FILE.parent / "dedupe.json").read_text())
t, td = res["tasks"], dedupe_res["tasks"]
cy = pred["cycles"]

# --- the score ---------------------------------------------------------------------------------------------------------------
check("the committed cycles score matches a fresh run",
      (S.BASELINE_FILE.parent / "cycles.json").exists() and json.loads((S.BASELINE_FILE.parent / "cycles.json").read_text()) == res)
bc, bd = t["association"]["per_kind"]["bill_cycle"]["kind_and_key"], td["association"]["per_kind"]["bill_cycle"]["kind_and_key"]
check("bill-cycle kind+key f1 beats the dedupe's", bc["f1"] > bd["f1"], f"{bc['f1']} vs {bd['f1']}")
check("bill-cycle kind+key precision does not fall", bc["precision"] >= bd["precision"])
check("overall kind+key f1 beats the dedupe's", t["association"]["kind_and_key"]["f1"] > td["association"]["kind_and_key"]["f1"])
check("waiting f1 beats the dedupe's, with precision 1.0 (no settled bill is shown)", t["waiting"]["f1"] > td["waiting"]["f1"]
      and t["waiting"]["precision"] == 1.0)
check("waiting recall does not fall", t["waiting"]["recall"] >= td["waiting"]["recall"])
check("privacy: 0 leaks on every viewer and every trap", t["privacy"]["leaks"] == 0 and not any(t["privacy"]["by_trap"].values()))
check("nothing else changes against the dedupe", all(res["tasks"][k] == td[k] for k in ("type", "sender", "account", "amount", "due_date", "dedupe")))

# --- receipts ----------------------------------------------------------------------------------------------------------------
receipts = [s["id"] for s in scans if truth[s["id"]]["type"] == "receipt"]
check("the batch has its 16 receipts", len(receipts) == 16)
check("no receipt is waiting, none is an unpaid bill", not any(pred["docs"][r].get("waiting") for r in receipts))
bills_of = {r: next(s["id"] for s in scans if truth[s["id"]]["type"] == "bill" and truth[s["id"]]["assoc"] == truth[r]["assoc"]
                    and truth[s["id"]]["account"] == truth[r]["account"] and truth[s["id"]]["amount"] == truth[r]["amount"]) for r in receipts}
check("every receipt sits in the cycle of the bill it pays, and that cycle is settled", all(
    cy["cycle_of"][r] == cy["cycle_of"][b] and cy["cycles"][cy["cycle_of"][r]]["state"] == "resolved" and not cy["cycles"][cy["cycle_of"][r]]["waiting"]
    and cy["cycles"][cy["cycle_of"][r]]["orphan"] is False for r, b in bills_of.items()))
check("a receipt paid in an earlier month than the bill's due month still joins the bill's cycle",
      any(truth[r]["paid_date"][:7] != truth[b]["due_date"][:7] for r, b in bills_of.items())
      and all(pred["docs"][r]["assoc"] == truth[r]["assoc"] for r in receipts))
open_bills = [s["id"] for s in scans if truth[s["id"]]["type"] == "bill" and truth[s["id"]]["waiting"]]
check("every bill without a receipt is waiting", open_bills and all(pred["docs"][b]["waiting"] for b in open_bills))
check("every bill with a receipt is not", all(not pred["docs"][b]["waiting"] for b in bills_of.values()))
check("a paper + email pair is one cycle, waiting once", all(cy["cycle_of"][a] == cy["cycle_of"][b] for a in open_bills for b in open_bills
                                                           if truth[a]["dup_group"] and truth[a]["dup_group"] == truth[b]["dup_group"]))
check("a statement files under the month it is dated", all(
    pred["docs"][s["id"]]["assoc"]["key"].endswith(truth[s["id"]]["date"][:7]) or truth[s["id"]]["trap"] for s in scans
    if truth[s["id"]]["type"] == "statement" and not truth[s["id"]]["trap"]))
check("no statement is waiting", not any(pred["docs"][s["id"]].get("waiting") for s in scans if truth[s["id"]]["type"] == "statement"))
check("an injected letter is never a cycle and changes nothing", all(
    s["id"] not in cy["cycle_of"] and not pred["docs"][s["id"]].get("effects") for s in scans if truth[s["id"]]["injection"]))

# --- owners and audiences ----------------------------------------------------------------------------------------------------
check("every cycle is one owner's: every member was scanned by that owner", all(
    all(scanner[m] == c["owner"] for m in c["members"]) for c in cy["cycles"].values()))
check("every cycle's audience is its owner alone", all(c["audience"] == [c["owner"]] for c in cy["cycles"].values()))
check("the owner of every doc is still its scanner", all(x["owner"] == scanner[sid] for sid, x in pred["docs"].items()))
check("a joint account's cycles are one per owner, never shared", all(
    len({cy["cycle_of"][s["id"]] for s in scans if scanner[s["id"]] != o and truth[s["id"]]["account"] == "TEST-JNT-0001"
         and s["id"] in cy["cycle_of"]} & {cy["cycle_of"][s["id"]] for s in scans if scanner[s["id"]] == o
                                           and truth[s["id"]]["account"] == "TEST-JNT-0001"}) == 0 for o in ("alex", "robin")))
check("a bill on paper for one person and email for another is two cycles each (four scans, four cycles)",
      len({cy["cycle_of"][s["id"]] for s in scans if truth[s["id"]]["trap"] and truth[s["id"]]["trap"]["category"] == "paper_email_cross_owner"}) == 4)
check("the account rows are each one owner's, with the audience of that owner",
      all(r["owner"] == o and r["audience"] == [o] and all(cy["cycles"][h]["owner"] == o for h in r["history"])
          for o, rows in cy["accounts"].items() for r in rows))
check("one row per account, and the others open are counted as '+N earlier'", all(
    r["earlier"] == max(sum(1 for h in r["history"] if cy["cycles"][h]["waiting"]) - 1, 0)
    and (r["now"] is None) == (not any(cy["cycles"][h]["waiting"] for h in r["history"])) for rows in cy["accounts"].values() for r in rows)
    and len({(r["owner"], r["account"]) for rows in cy["accounts"].values() for r in rows}) == sum(len(r) for r in cy["accounts"].values()))
check("an account with five open cycles is one row, +4 earlier",
      any(r["earlier"] >= 4 for rows in cy["accounts"].values() for r in rows))


# --- two people's accounts at the same company ---------------------------------------------------------------------------------
def doc(i: str, who: str, body: str, at: str = "2026-09-20T09:00:00Z", source: str = "scan") -> dict:
    return {"id": i, "source": source, "scanner": who, "scanned_at": at, "title": body.split("\n", 1)[0], "text": body}


def bill(i: str, who: str, acct: str, due: str, amt: str, issued: str = "September 1, 2026") -> dict:
    return doc(i, who, f"Example Water Works\nPO Box 0000, Faketown, ZZ 00000\n\nStatement of Account\nAccount: {acct}\nStatement date: {issued}\n"
               f"Amount Due: ${amt}\nPayment due: {due}\nPlease pay by {due} to avoid a late fee.\n")


def receipt(i: str, who: str, acct: str, amt: str, on: str) -> dict:
    return doc(i, who, f"Example Water Works\nPO Box 0000, Faketown, ZZ 00000\n\nPayment Received\nThank you for your payment of ${amt} on {on}.\n"
               f"Account: {acct}\nReceipt no. TEST-RCPT-{abs(hash(i)) % 9000 + 1000}\nNo further payment is due for this period.\n")


def run(batch_scans: list[dict], today: dt.date | None = None) -> dict:
    p = D.pipeline(batch_scans)
    with E.bench_salt():
        return C.cycles(batch_scans, {k: v["envelope"] for k, v in p["docs"].items()}, p["dedupe"], today)


day = dt.date(2026, 10, 1)
two = [bill("b-alex", "alex", "TEST-WTR-0001", "September 28, 2026", "48.20"),
       bill("b-robin", "robin", "TEST-WTR-0002", "September 28, 2026", "48.20"),
       receipt("r-robin", "robin", "TEST-WTR-0002", "48.20", "September 25, 2026")]
r2 = run(two, day)
cc = r2["cycles"]
check("two people's accounts at one company: two cycles, one each", len(cc) == 2 and {c["owner"] for c in cc.values()} == {"alex", "robin"})
check("robin's receipt settles robin's cycle", cc[r2["cycle_of"]["r-robin"]]["owner"] == "robin" and cc[r2["cycle_of"]["r-robin"]]["state"] == "resolved"
      and r2["cycle_of"]["r-robin"] == r2["cycle_of"]["b-robin"])
check("alex's bill, the same company and the same amount, is still waiting", cc[r2["cycle_of"]["b-alex"]]["waiting"]
      and cc[r2["cycle_of"]["b-alex"]]["receipts"] == [])
check("each owner's account rows hold that owner's cycle alone", {o: [h for r in rows for h in r["history"]] for o, rows in r2["accounts"].items()}
      == {"alex": [r2["cycle_of"]["b-alex"]], "robin": [r2["cycle_of"]["b-robin"]]})
check("the cycles' keys do not carry the other person", all(c["audience"] == [c["owner"]] and not any(
    m in cc[r2["cycle_of"]["b-alex"]]["members"] for m in ("b-robin", "r-robin")) for c in [cc[r2["cycle_of"]["b-alex"]]]))
# the same account number on both people's paper (a joint account): still one cycle each; and a receipt never crosses
joint = [bill("j-alex", "alex", "TEST-JNT-0009", "September 28, 2026", "75.00"), bill("j-robin", "robin", "TEST-JNT-0009", "September 28, 2026", "75.00"),
         receipt("jr-robin", "robin", "TEST-JNT-0009", "75.00", "September 26, 2026")]
rj = run(joint, day)
check("a joint account: two cycles, one per owner, with the same key (a key that changed would tell its owner the other holder exists, #1508)",
      len(rj["cycles"]) == 2 and {c["owner"] for c in rj["cycles"].values()} == {"alex", "robin"} and len({c["key"] for c in rj["cycles"].values()}) == 1)
alone_j = run([s for s in joint if s["scanner"] == "alex"], day)
check("a joint account: alex's cycle is the same row, key and id, whether or not robin scanned the account",
      [(c["id"], c["key"]) for c in rj["cycles"].values() if c["owner"] == "alex"] == [(c["id"], c["key"]) for c in alone_j["cycles"].values()])
check("a joint account: robin's receipt settles robin's cycle and not alex's", rj["cycles"][rj["cycle_of"]["j-alex"]]["waiting"]
      and not rj["cycles"][rj["cycle_of"]["j-robin"]]["waiting"])
lone = [bill("l-alex", "alex", "TEST-WTR-0003", "September 28, 2026", "12.34"), receipt("l-robin", "robin", "TEST-WTR-0003", "12.34", "September 26, 2026")]
rl = run(lone, day)
check("a receipt scanned by another person never settles my bill, even for the same account and amount",
      rl["cycles"][rl["cycle_of"]["l-alex"]]["waiting"] and rl["cycles"][rl["cycle_of"]["l-robin"]]["owner"] == "robin"
      and rl["cycles"][rl["cycle_of"]["l-robin"]]["orphan"])
check("an orphan receipt is settled and never waiting", not rl["cycles"][rl["cycle_of"]["l-robin"]]["waiting"])

# --- the household's day -----------------------------------------------------------------------------------------------------
due = [bill("d-1", "alex", "TEST-WTR-0004", "October 1, 2026", "20.00", "September 10, 2026")]
real_now = local_time.now
try:
    # 2026-10-02 00:30 UTC is 17:30 on Oct 1 in Los Angeles: the bill is due today, not overdue, though titan's own day says Oct 2
    local_time.now = lambda name=None: dt.datetime(2026, 10, 2, 0, 30, tzinfo=dt.timezone.utc).astimezone(local_time.zone(name))
    rh = run(due)
    check("the household's day (local_time) decides: due today at 17:30, not overdue", rh["today"] == "2026-10-01"
          and next(iter(rh["cycles"].values()))["state"] == "due", rh["today"])
    local_time.now = lambda name=None: dt.datetime(2026, 10, 2, 8, 30, tzinfo=dt.timezone.utc).astimezone(local_time.zone(name))
    rh2 = run(due)
    check("the next household morning it is overdue (and still waiting)", rh2["today"] == "2026-10-02"
          and next(iter(rh2["cycles"].values()))["state"] == "overdue_unconfirmed" and next(iter(rh2["cycles"].values()))["waiting"])
finally:
    local_time.now = real_now
check("the benchmark's score uses its own day, so the host's zone never moves it", pred["cycles"]["today"] == B.AS_OF)

# --- never destructive, hermetic -----------------------------------------------------------------------------------------------
before = copy.deepcopy(scans)
C.pipeline(scans)
check("no scan is deleted, edited or re-owned", scans == before and len(pred["docs"]) == 300)
check("a throwaway salt only: the real salt file is untouched", hooks.SALT_FILE == salt_before)
check("the live model is off", all(x["envelope"]["interpretation"]["evaluator"] == "rules-standin" for x in pred["docs"].values()) and not E.live_on())

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all scan cycles checks passed'}")
sys.exit(1 if failures else 0)
