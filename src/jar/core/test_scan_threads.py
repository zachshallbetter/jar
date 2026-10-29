#!/usr/bin/env python3
# gate: hermetic   (fictional data, no network, no live model, no real files, a throwaway salt)
"""titan-node#1504: letters, claims and cases form threads. A claim's letters become one row per thread through the linker's
own machinery; a thread never joins two owners or widens an audience (two people's claims against one insurer stay two
threads); the code is never kept; a correction splits a thread; the committed score matches a fresh run."""
from __future__ import annotations

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks  # noqa: E402
import linker  # noqa: E402
import scan_dedupe as D  # noqa: E402
import scan_envelope as E  # noqa: E402
import scan_threads as T  # noqa: E402
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
before = copy.deepcopy(scans)
pred = T.pipeline(scans)
res = json.loads(json.dumps(S.score(batch, pred, "scan_threads:pipeline")))
t = res["tasks"]
dd_score = json.loads((S.BASELINE_FILE.parent / "dedupe.json").read_text())["tasks"]
th = pred["threads"]["threads"]

# --- the score --------------------------------------------------------------------------------------------------------------
committed = S.BASELINE_FILE.parent / "threads.json"
check("the committed threads score matches a fresh run", committed.exists() and json.loads(committed.read_text()) == res)
tk = t["association"]["per_kind"]["thread"]
check("every claim letter is in a thread, filed under the right one (28 of 28, no extras)",
      tk["kind_and_key"]["tp"] == 28 and tk["kind"]["precision"] == 1.0 and tk["kind"]["recall"] == 1.0 and tk["kind_and_key"]["predicted"] == 28)
check("the association totals beat dedupe's", t["association"]["kind"]["recall"] > dd_score["association"]["kind"]["recall"]
      and t["association"]["kind"]["precision"] == 1.0)
check("waiting recall beats dedupe's, with the five waiting claim rows found", t["waiting"]["recall"] > dd_score["waiting"]["recall"]
      and t["waiting"]["tp"] - dd_score["waiting"]["tp"] == 5, str(t["waiting"]))
check("everything else is what dedupe scored", all(t[k] == dd_score[k] for k in ("type", "sender", "account", "amount", "due_date", "dedupe")))
check("privacy: 0 leaks on every viewer and every trap", t["privacy"]["leaks"] == 0 and not any(t["privacy"]["by_trap"].values()))

# --- the threads ------------------------------------------------------------------------------------------------------------
check("seven claims, seven threads of four letters", len(th) == 7 and all(x["count"] == 4 for x in th))
check("a thread's scans are the truth's thread, exactly", all(
    {s for s in x["scans"]} == {sid for sid, y in truth.items() if y["thread"] and sid in x["scans"] and y["thread"] == truth[x["scans"][0]]["thread"]}
    and len({truth[s]["thread"] for s in x["scans"]}) == 1 for x in th))
check("every thread is one owner's, audience that owner alone", all(
    {scanner[s] for s in x["scans"]} == {x["owner"]} and x["audience"] == [x["owner"]] for x in th))
check("a thread lists its letters oldest first, with first and latest dates", all(
    [truth[s]["date"] for s in x["scans"]] == sorted(truth[s]["date"] for s in x["scans"])
    and x["first"] == truth[x["scans"][0]]["date"] and x["latest"] == truth[x["scans"][-1]]["date"] for x in th))
check("a thread is one view row with its count, in its owner's view only", all(
    sum(1 for r in rows if r.get("thread") == x["id"]) == (x["owner"] == v) and all(
        r["count"] == 4 for r in rows if r.get("thread") == x["id"]) for x in th for v, rows in pred["views"].items()))
check("every view row holds only that viewer's own scans", all(all(scanner[m] == v for m in r["members"]) for v, rows in pred["views"].items() for r in rows))
check("no code is kept: no claim number in any thread or doc output",
      "CLM-1" not in json.dumps(pred["threads"]) and "CLM-1" not in json.dumps({k: {f: v for f, v in d.items() if f != "envelope"} for k, d in pred["docs"].items()}))
check("only claim letters and letters are threaded", all(truth[s]["type"] in ("claim_letter", "letter") for x in th for s in x["scans"]))
check("a letter and its repeat are one document, not a thread", all(
    len(x["documents"]) == x["count"] for x in th))

# --- two people, one insurer --------------------------------------------------------------------------------------------------
def letter(sid: str, who: str, when: str, claim: str, subject: str, body: str, sender: str = "Example Mutual Insurance", at: str = "2026-09-02T09:00:00Z") -> dict:
    return {"id": sid, "source": "scan", "scanner": who, "scanned_at": at, "title": subject,
            "text": f"{sender}\nPO Box 0000, Faketown, ZZ 00000\n\n{who.upper()} HALE\n100 Example Street, Faketown, ZZ 00000\n\nRe: Claim {claim}\n{subject}\nDate: {when}\n{body}\n"}


ASK = "Please send the itemized receipt for the disputed charge."
RCV = "We are reviewing your claim and will write again within 30 days."
mix = [letter("h-1", "alex", "June 1, 2026", "TEST-CLM-7001", "We received your claim", RCV),
       letter("h-2", "alex", "June 15, 2026", "TEST-CLM-7001", "Request for more information", ASK),
       letter("h-3", "robin", "June 3, 2026", "TEST-CLM-7002", "We received your claim", RCV),
       letter("h-4", "robin", "June 17, 2026", "TEST-CLM-7002", "Request for more information", ASK),
       # the same printed claim number on both people's letters (a joint claim): still two people's mail
       letter("h-5", "alex", "July 1, 2026", "TEST-CLM-7003", "We received your claim", RCV),
       letter("h-6", "robin", "July 2, 2026", "TEST-CLM-7003", "We received your claim", RCV),
       letter("h-7", "alex", "July 20, 2026", "TEST-CLM-7003", "Request for more information", ASK)]
p = T.pipeline(mix)
tt = p["threads"]["threads"]
owners = sorted((x["owner"], tuple(x["scans"])) for x in tt)
check("two people's claims against one insurer are two threads, one each",
      owners == [("alex", ("h-1", "h-2")), ("alex", ("h-5", "h-7")), ("robin", ("h-3", "h-4"))], str(owners))
check("a claim number printed on both people's letters never joins them; robin's lone letter is no thread",
      all(len({m for m in x["scans"]} & {"h-6"}) == 0 for x in tt) and "h-6" not in p["threads"]["of"])
with E.bench_salt():
    envs = {s["id"]: E.envelope(s) for s in mix}
    dd = D.dedupe(mix, envs)
    raw_items = {}
    for s in mix:
        it = linker.Item(s["id"], "scans", None, s["scanner"], s["scanner"])
        for h in hooks.extract({"text": s["text"], "title": s["title"], "at": "2026-07-01"}):
            it.keys[h.kind].add(h.key)
        raw_items[s["id"]] = it
    raw = linker.score(raw_items)
    check("the raw linker does accept a cross-owner edge on the shared number", any(
        {e.a, e.b} == {"h-5", "h-6"} and e.state == "accepted" for e in raw))
    seen_by_robin = linker.threads_for(raw_items, [e for e in raw if raw_items[e.a].owner == raw_items[e.b].owner],
                                       lambda it: it.owner == "robin")
    check("threads_for with robin as the viewer holds none of alex's letters", all(
        not ({"h-1", "h-2", "h-5", "h-7"} & set(x.items)) for x in seen_by_robin))
    base = T.threads(mix, envs, dd)
    # a person says two letters are not related: the thread splits; and a correction stays inside one owner
    split = T.threads(mix, envs, dd, said={("h-1", "h-2"): False})
    check("a correction (not related) splits the thread", "h-1" not in split["of"] and "h-2" not in split["of"] and "h-3" in split["of"])
    joined = T.threads(mix, envs, dd, said={("h-1", "h-6"): True})
    who = {m["id"]: m["scanner"] for m in mix}
    check("a correction (related) across two owners joins nothing", "h-6" not in joined["of"]
          and all(len({who[s] for s in x["scans"]}) == 1 for x in joined["threads"]))
check("a thread that asked for papers and is not closed is waiting",
      {x["scans"][0]: x["waiting"] for x in tt} == {"h-1": True, "h-3": True, "h-5": True})
closed = mix[:2] + [letter("h-8", "alex", "July 1, 2026", "TEST-CLM-7001", "Claim closed", "Your claim has been paid. No further action is required.")]
pc = T.pipeline(closed)
check("a thread whose latest letter closes the matter leaves Waiting on you", pc["threads"]["threads"][0]["count"] == 3 and not pc["threads"]["threads"][0]["waiting"])
accts = [letter("h-9", "alex", "June 1, 2026", "TEST-CLM-7101", "Statement", RCV), letter("h-10", "alex", "June 20, 2026", "TEST-CLM-7102", "Statement", RCV)]
check("two letters from one sender with different claim numbers are not a thread", not T.pipeline(accts)["threads"]["threads"])
rep = [letter("h-11", "alex", "June 1, 2026", "TEST-CLM-7201", "We received your claim", RCV),
       letter("h-12", "alex", "June 1, 2026", "TEST-CLM-7201", "We received your claim", RCV, at="2026-09-03T09:00:00Z")]
check("a letter scanned twice is one document, not a thread", not T.pipeline(rep)["threads"]["threads"])

# --- never destructive, hermetic ---------------------------------------------------------------------------------------------
check("no scan is edited or re-owned", scans == before and all(d["owner"] == scanner[sid] for sid, d in pred["docs"].items()))
check("a throwaway salt only: the real salt file is untouched", hooks.SALT_FILE == salt_before)
check("the live model is off", not E.live_on() and all(d["envelope"]["interpretation"]["evaluator"] == "rules-standin" for d in pred["docs"].values()))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all scan thread checks passed'}")
sys.exit(1 if failures else 0)
