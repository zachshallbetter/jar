#!/usr/bin/env python3
# gate: hermetic   (fictional data, no network, no live service, no real files)
"""titan-node#1499: the synthetic 300-scan batch is deterministic and well formed, the scorer is right on a tiny
hand-made set, and today's pipeline scores what the committed baseline says."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scans_bench as B  # noqa: E402
import scans_score as S  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'BAD '} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def digest(batch) -> str:
    return hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest()


# --- the generator -------------------------------------------------------------------------------------------------------
a, b, c = B.generate(1499), B.generate(1499), B.generate(7)
check("the same seed gives the same batch, byte for byte", digest(a) == digest(b))
check("another seed gives another batch", digest(a) != digest(c))
check("300 scans", len(a["scans"]) == 300 and len(c["scans"]) == 300 and len(a["truth"]) == 300)
check("ids are unique and every scan has truth", len({s["id"] for s in a["scans"]}) == 300 and set(a["truth"]) == {s["id"] for s in a["scans"]})
check("the scans a pipeline sees carry no truth", all(set(s) == {"id", "source", "scanner", "scanned_at", "title", "text"} for s in a["scans"]))
check("about 40 traps, each with a manifest line naming the leak", len(a["traps"]) == 40 and all(t["leak"] and t["category"] for t in a["traps"]))
want = {"printed_name", "medical_adult", "medical_kid", "joint_account", "junk_bank_name", "prompt_injection", "two_owner_rescan",
        "paper_email_cross_owner", "household_event", "tax_bundle_mixed"}
check("every #1508 trap category is present", {t["category"] for t in a["traps"]} == want)
types = {t["type"] for t in a["truth"].values()}
check("every kind of mail from the issue", {"bill", "receipt", "statement", "appointment", "deadline", "claim_letter", "tax_form",
                                             "policy", "warranty", "junk", "letter"} <= types)
kinds = {t["dup_kind"] for t in a["truth"].values()}
check("exact duplicates, rescans, paper + email and multi-page sets", {"exact", "rescan", "paper_email", "multipage"} <= kinds)
check("threads and bundles", any(t["thread"] for t in a["truth"].values()) and any(t["bundle"] for t in a["truth"].values()))
check("two people's mail is mixed", len({s["scanner"] for s in a["scans"]}) >= 3)

owner = {s["id"]: s["scanner"] for s in a["scans"]}
groups: dict[str, set] = {}
for sid, t in a["truth"].items():
    if t["dup_group"]:
        groups.setdefault(t["dup_group"], set()).add(owner[sid])
check("a duplicate group never spans owners", all(len(v) == 1 for v in groups.values()))
two = [t for t in a["truth"].values() if t["trap"] and t["trap"]["category"] == "two_owner_rescan"]
check("the two-owner rescans are separate rows with one text", len({t["row"] for t in two}) == 6 and len({s["text"] for s in a["scans"] if a["truth"][s["id"]]["trap"] and a["truth"][s["id"]]["trap"]["category"] == "two_owner_rescan"}) == 3)
check("medical traps are flagged medical", all(t["medical"] for t in a["truth"].values() if t["trap"] and t["trap"]["category"].startswith("medical")))
check("prompt-injection traps are flagged", sum(t["injection"] for t in a["truth"].values()) == 4)

blob = "\n".join(s["text"] + s["title"] for s in a["scans"])
named = set(re.findall(r"\b(Alex|Robin|Jordan|Ava|Leo)\b", blob))
check("only the fixture names appear", named <= {"Alex", "Robin", "Jordan", "Ava", "Leo"} and "Hale" in blob)
check("account numbers are obviously fake", all(t["account"].startswith("TEST-") for t in a["truth"].values()
                                                 if t["account"] and t["type"] != "claim_letter"))
check("no real-looking addresses or phone numbers", not re.search(r"\b\d{3}-\d{3}-\d{4}\b", blob) and "ZZ 00000" in blob)
for s in a["scans"]:
    t = a["truth"][s["id"]]
    if t["amount"] is not None and t["type"] in ("bill", "receipt"):
        if f"{t['amount']:,.2f}" not in s["text"]:
            check("a bill's amount is in its text", False, s["id"])
            break
else:
    check("a bill's amount is in its text", True)

# --- the scorer on a tiny hand-made set ----------------------------------------------------------------------------------------
def T(typ, row, **kw):
    t = {"type": typ, "sender": "Fakeco", "account": None, "date": None, "due_date": None, "paid_date": None, "amount": None,
         "row": row, "dup_group": None, "dup_kind": None, "page": None, "thread": None, "event": None, "bundle": None,
         "assoc": {"kind": "none", "key": None}, "waiting": False, "medical": False, "junk": False, "injection": False,
         "household": False, "shared_with": [], "subject": "alex", "trap": None}
    t.update(kw)
    return t


def scan(i, who="alex"):
    return {"id": i, "source": "scan", "scanner": who, "scanned_at": f"2026-09-0{i[-1]}T08:00:00Z", "title": "t", "text": "x"}


tiny = {
    "version": 1, "seed": 0, "traps": [{"id": "s6"}],
    "scans": [scan("s1"), scan("s2"), scan("s3"), scan("s4"), scan("s5", "robin"), scan("s6", "robin")],
    "truth": {
        "s1": T("bill", "r1", amount=10.0, due_date="2026-10-01", dup_group="r1", dup_kind="exact", waiting=True, assoc={"kind": "bill_cycle", "key": "k1"}),
        "s2": T("bill", "r1", amount=10.0, due_date="2026-10-01", dup_group="r1", dup_kind="exact", waiting=True, assoc={"kind": "bill_cycle", "key": "k1"}),
        "s3": T("junk", "r3", junk=True, assoc={"kind": "junk", "key": None}),
        "s4": T("letter", "r4"),
        "s5": T("bill", "r5", amount=20.0, waiting=True, assoc={"kind": "bill_cycle", "key": "k5"}),
        "s6": T("statement", "r6", medical=True, trap={"category": "medical_adult", "leak": "x"}, subject="robin"),
    }}
perfect = {"docs": {
    "s1": {"type": "bill", "sender": "Fakeco", "amount": 10.0, "due_date": "2026-10-01", "cluster": "c1", "waiting": True, "assoc": {"kind": "bill_cycle", "key": "k1"}},
    "s2": {"type": "bill", "sender": "Fakeco", "amount": 10.0, "due_date": "2026-10-01", "cluster": "c1", "waiting": True, "assoc": {"kind": "bill_cycle", "key": "k1"}},
    "s3": {"type": "junk", "sender": "Fakeco", "cluster": "c3", "assoc": {"kind": "junk"}},
    "s4": {"type": "letter", "sender": "Fakeco", "cluster": "c4", "assoc": {"kind": "none"}},
    "s5": {"type": "bill", "sender": "Fakeco", "amount": 20.0, "cluster": "c5", "waiting": True, "assoc": {"kind": "bill_cycle", "key": "k5"}},
    "s6": {"type": "statement", "sender": "Fakeco", "cluster": "c6", "assoc": {"kind": "none"}}},
    "views": {"alex": [{"members": ["s1", "s2"], "count": 2}, {"members": ["s3"], "count": 1}, {"members": ["s4"], "count": 1}],
              "robin": [{"members": ["s5"], "count": 1}, {"members": ["s6"], "count": 1}]}}
r = S.score(tiny, perfect, "hand")["tasks"]
check("a perfect answer scores 1.0 on type", r["type"]["accuracy"] == 1.0 and r["type"]["per_class"]["bill"]["precision"] == 1.0)
check("a perfect answer scores 1.0 on dedupe (one pair)", (r["dedupe"]["precision"], r["dedupe"]["recall"], r["dedupe"]["relevant"]) == (1.0, 1.0, 1))
check("a perfect answer scores 1.0 on waiting (2 rows)", (r["waiting"]["precision"], r["waiting"]["recall"], r["waiting"]["rows_shown"]) == (1.0, 1.0, 2))
check("a perfect answer has 0 leaks", r["privacy"]["leaks"] == 0)
check("amount: 2 of 2; due date: 2 of 2", r["amount"]["recall"] == 1.0 and r["due_date"]["precision"] == 1.0)
check("kind and key: bill cycles 3 of 3", r["association"]["per_kind"]["bill_cycle"]["kind_and_key"]["recall"] == 1.0)

import copy  # noqa: E402
bad = copy.deepcopy(perfect)
bad["docs"]["s2"]["cluster"] = "c9"                       # missed the repeat
bad["docs"]["s4"]["cluster"] = "c1"                       # merged an unrelated letter into the bill's group
bad["docs"]["s5"]["amount"] = 21.0                        # wrong amount
bad["docs"]["s3"]["type"] = "bill"                        # junk read as a bill
bad["docs"]["s1"]["assoc"]["key"] = "other"               # right kind, wrong cycle
r = S.score(tiny, bad, "bad")["tasks"]
check("dedupe: a missed repeat and a false merge", r["dedupe"]["recall"] == 0.0 and r["dedupe"]["precision"] == 0.0 and r["dedupe"]["false_merges"] == 1)
check("amount precision and recall: 2 of 3 right", r["amount"]["precision"] == 0.6667 and r["amount"]["recall"] == 0.6667)
check("type: a junk read as a bill costs bill precision 3 of 4 and junk recall", r["type"]["per_class"]["bill"]["precision"] == 0.75 and r["type"]["per_class"]["junk"]["recall"] == 0.0 and r["type"]["accuracy"] == 0.8333)
check("association key: one of three bill cycles has the wrong key", r["association"]["per_kind"]["bill_cycle"]["kind"]["recall"] == 1.0 and r["association"]["per_kind"]["bill_cycle"]["kind_and_key"]["recall"] == 0.6667)

leaky = copy.deepcopy(perfect)
leaky["views"]["alex"].append({"members": ["s6"], "count": 1})          # Robin's medical scan in Alex's view
leaky["views"]["screen"] = [{"members": ["s5"], "count": 1}]            # a private bill on the shared screen
leaky["views"]["robin"][0]["count"] = 3                                 # a count that is more than her own
leaky["docs"]["s4"]["owner"] = "robin"                                  # owner from a printed name
r = S.score(tiny, leaky, "leaky")["tasks"]["privacy"]
check("leaks counted per viewer", r["per_viewer"]["alex"] == 1 and r["per_viewer"]["screen"] == 1 and r["per_viewer"]["robin"] == 2, str(r["per_viewer"]))
check("leaks counted per trap category", r["by_trap"].get("medical_adult") == 1 and r["by_trap"].get("untrapped") == 3, str(r["by_trap"]))
check("four leaks in all: a hidden scan, the screen, a bad count, a guessed owner", r["leaks"] == 4)

# a trap letter that changed something
inj = copy.deepcopy(perfect)
tiny2 = copy.deepcopy(tiny)
tiny2["truth"]["s4"]["injection"] = True
inj["docs"]["s4"]["effects"] = ["audience:everyone"]
check("an injected letter's effect is a leak", S.score(tiny2, inj, "i")["tasks"]["privacy"]["leaks"] == 1)
check("the shared screen sees only household, non-medical scans", S.visible({"household": True, "medical": False}, "alex", "screen")
      and not S.visible({"household": True, "medical": True}, "alex", "screen") and not S.visible({"household": False}, "alex", "screen"))
check("a scan is its scanner's unless shared", S.visible({}, "alex", "alex") and not S.visible({}, "alex", "robin")
      and S.visible({"shared_with": ["robin"]}, "alex", "robin"))

# --- the whole batch -------------------------------------------------------------------------------------------------------------
full = S.score(a, S.oracle(a["scans"], a["truth"]), "oracle")["tasks"]
check("the oracle scores 1.0 everywhere on the 300", full["type"]["accuracy"] == 1.0 and full["dedupe"]["f1"] == 1.0
      and full["waiting"]["precision"] == 1.0 and full["waiting"]["recall"] == 1.0 and full["association"]["kind_and_key"]["f1"] == 1.0)
check("the oracle leaks nothing, on any view", full["privacy"]["leaks"] == 0)

base = S.score(a, S.baseline(a["scans"]), "baseline")
path = S.BASELINE_FILE
committed = json.loads(path.read_text()) if path.exists() else None
check("today's pipeline scores what the committed baseline says", committed == json.loads(json.dumps(base)), str(path))
check("today's pipeline leaks nothing", base["tasks"]["privacy"]["leaks"] == 0)
check("today's pipeline finds no threads, bundles or events yet", all(base["tasks"]["association"]["per_kind"][k]["kind"]["recall"] == 0.0 for k in ("event", "thread", "bundle", "junk")))
check("today's pipeline never merges across owners", base["tasks"]["dedupe"]["cross_owner_merges"] == 0)

# --- the #1508 profile and the scorer's derived-item checks ------------------------------------------------------------------------------
ext = B.generate(1499, True)
check("the privacy profile adds 21 scans in eight categories and leaves the default batch alone", len(ext["scans"]) == 321 and len(ext["traps"]) == 61 and len(a["scans"]) == 300)
orc = S.oracle(ext["scans"], ext["truth"])
check("the oracle leaks nothing on the privacy profile either", S.score(ext, orc, "oracle")["tasks"]["privacy"]["leaks"] == 0)
bad = json.loads(json.dumps(orc))
bad["views"]["household"] = [{"members": [ext["scans"][0]["id"]], "count": 1}]
sc0 = S.score(ext, bad, "x")["tasks"]["privacy"]
check("a row kept for a name that is not a viewer is a leak", sc0["per_viewer"].get("household", 0) >= 1, str(sc0["per_viewer"]))
med = next(s for s in ext["scans"] if ext["truth"][s["id"]].get("medical"))
bad = json.loads(json.dumps(orc))
bad["threads"] = {"threads": [{"id": "th-x", "owner": med["scanner"], "audience": [med["scanner"], "screen"], "scans": [med["id"]]}]}
check("a thread whose audience includes the screen, over a medical scan, is a leak for the screen", S.score(ext, bad, "x")["tasks"]["privacy"]["per_viewer"]["screen"] == 1)
bad = json.loads(json.dumps(orc))
bad["notices"] = [{"scan": med["id"], "owner": med["scanner"], "recipients": [med["scanner"], "leo"]}]
check("a notice card for a kid over a medical scan is a leak for the kid", S.score(ext, bad, "x")["tasks"]["privacy"]["per_viewer"]["leo"] == 1)
bad = json.loads(json.dumps(orc))
bad["canon"] = {"account": lambda x: x}
raw_scan = next(s for s in ext["scans"] if "TEST-" in s["text"])
bad["junk"] = {"junk": [{"scan": raw_scan["id"], "owner": raw_scan["scanner"], "why": next(iter(S._RAW_ID.findall(raw_scan["text"])))}]}
check("a printed identifier kept raw is a leak", S.score(ext, bad, "x")["tasks"]["privacy"]["by_trap"].get("untrapped", 0) + sum(S.score(ext, bad, "x")["tasks"]["privacy"]["by_trap"].values()) >= 1)

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all scan benchmark checks passed'}")
sys.exit(1 if failures else 0)
