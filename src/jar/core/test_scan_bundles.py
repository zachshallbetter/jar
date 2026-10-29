#!/usr/bin/env python3
# gate: hermetic   (fictional data, no network, no live service, no real files, a throwaway salt and spaces DB)
"""titan-node#1505: kept bundles (tax year, the car, the house) and the facet and project each scan files under.
The committed score matches a fresh run; a bundle is references, never copies; a bundle has one owner (two people's tax
documents for one year are two bundles); a facet or project is filing only; a project is proposed, and only the owner keeps it."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import projects  # noqa: E402
import scan_bundles as BU  # noqa: E402
import scan_dedupe as D  # noqa: E402
import scans_bench as B  # noqa: E402
import scans_score as S  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'BAD '} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


here = S.BASELINE_FILE.parent
batch = B.generate(1499)
truth, scans = batch["truth"], batch["scans"]
res = json.loads(json.dumps(S.score(batch, BU.pipeline(scans), "scan_bundles:pipeline")))
t = res["tasks"]

# --- drift: what is committed is what runs -----------------------------------------------------------------------------------
check("the committed bundles score matches a fresh run", json.loads((here / "bundles.json").read_text()) == res)
check("the older pipelines' scores carry no bundle tasks (they predict none)",
      all("bundles" not in json.loads((here / f"{n}.json").read_text())["tasks"] for n in ("baseline", "envelope", "dedupe", "cycles", "threads", "notices")))
check("the chained score has them", "bundles" in json.loads((here / "pipeline.json").read_text())["tasks"])

# --- the benchmark's truth ---------------------------------------------------------------------------------------------------
check("PROJECT_MIN_DOCS is the projects module's MIN_ITEMS", B.PROJECT_MIN_DOCS == projects.MIN_ITEMS)
check("truth: the car, the house and a tax year are collections beside the kept bundles", {"house", "vehicle", "tax-2025"} <= {k for x in truth.values() for k in x["collections"]})
check("truth: every assoc bundle is also a collection", all(x["assoc"]["key"] in x["collections"] for x in truth.values() if x["assoc"]["kind"] == "bundle"))
check("truth: a mortgage tax form is in two bundles", any({"house", "tax-2025"} <= set(x["collections"]) for x in truth.values()))
check("truth: junk files under no facet and no bundle", all(x["facet"] is None and not x["collections"] for x in truth.values() if x["junk"]))
check("truth: a project needs three things from one owner", all(len({truth[s["id"]]["row"] for s in scans if s["scanner"] == w and k in truth[s["id"]]["projects"]}) >= 3
                                                                for w in B.ADULTS for k in {p for x in truth.values() for p in x["projects"]}
                                                                if any(k in truth[s["id"]]["projects"] for s in scans if s["scanner"] == w)))
check("the extension drew nothing new from the random stream (the plain facts of a scan are what they were)",
      B.generate(1499) == batch and all(set(s) == {"id", "source", "scanner", "scanned_at", "title", "text"} for s in scans))
full = S.score(batch, S.oracle(scans, truth), "oracle")["tasks"]
check("the oracle scores 1.0 on bundles and classes, with no leak", full["bundles"]["membership"]["f1"] == 1.0 and full["bundles"]["crossed"] == 0
      and full["classes"]["facet"]["accuracy"] == 1.0 and full["classes"]["projects"]["f1"] == 1.0 and full["privacy"]["leaks"] == 0)

# --- the score -----------------------------------------------------------------------------------------------------------------
check("bundles: every membership found, none invented", t["bundles"]["membership"]["precision"] == 1.0 and t["bundles"]["membership"]["recall"] == 1.0, str(t["bundles"]["membership"]))
check("bundles: every kept bundle kind has its members", all(v["recall"] == 1.0 and v["precision"] == 1.0 for v in t["bundles"]["by_kind"].values()))
check("association: the bundle kind and key, 50 of 50", t["association"]["per_kind"]["bundle"]["kind_and_key"]["tp"] == 50)
check("facets: accuracy 1.0 over every scan", t["classes"]["facet"]["accuracy"] == 1.0, str(t["classes"]["facet"]["accuracy"]))
check("projects: proposed exactly where truth has them", t["classes"]["projects"]["f1"] == 1.0, str(t["classes"]["projects"]))
check("no bundle holds two people, and 0 leaks", t["bundles"]["crossed"] == 0 and t["privacy"]["leaks"] == 0)
other = B.generate(7)
o = S.score(other, BU.pipeline(other["scans"]), "seed7")["tasks"]
check("another seed scores the same way", o["bundles"]["membership"]["f1"] == 1.0 and o["classes"]["facet"]["accuracy"] == 1.0 and o["privacy"]["leaks"] == 0)

# --- references, never copies ---------------------------------------------------------------------------------------------------
pred = BU.pipeline(scans)
blob = json.dumps(pred["bundles"], sort_keys=True)
words = {w for s in scans for ln in s["text"].splitlines() if len(ln) > 30 for w in [ln.strip()]}
check("a bundle holds scan ids and keys, no scan text", not any(w in blob for w in words) and all(set(b) <= {"id", "key", "name", "owner", "audience", "documents", "scans", "count", "facet", "project", "state"} for b in pred["bundles"]))
check("a bundle's members are scan ids of the batch", all(m in {s["id"] for s in scans} for b in pred["bundles"] for m in b["scans"]))

# --- a bundle has one owner: two people's tax documents for the same year ----------------------------------------------------------
def scan(i, who, text, day=1):
    return {"id": i, "source": "scan", "scanner": who, "scanned_at": f"2026-09-{day:02d}T08:00:00Z", "title": text.splitlines()[0], "text": text}


def w2(name, wages):
    return (f"Form W-2 Wage and Tax Statement 2025\nEmployer: Example Staffing Co.\nEmployee: {name}\n"
            f"Box 1 Wages, tips, other compensation ${wages}\nTax year 2025\n")


def donate(amt):
    return f"Faketown Food Pantry\nPO Box 0000, Faketown, ZZ 00000\n\nDonation Receipt\nThank you for your donation of ${amt} on March 3, 2025.\nTax year 2025.\n"


two = [scan("s1", "alex", w2("Alex Hale", "41,000.00"), 1), scan("s2", "alex", donate("120.00"), 2), scan("s3", "alex", donate("80.00").replace("March 3", "May 9"), 3),
       scan("s4", "robin", w2("Robin Hale", "52,000.00"), 1), scan("s5", "robin", donate("60.00"), 2),
       scan("s6", "robin", w2("Alex Hale", "9,999.00").replace("Wages", "Other wages"), 3)]       # robin scanned a page that names Alex
r = BU.bundles(two, D.pipeline(two)["docs"])
tax = {b["owner"]: b for b in r["bundles"] if b["key"] == "tax-2025"}
check("two people, one tax year: two bundles with the one key", set(tax) == {"alex", "robin"} and tax["alex"]["id"] != tax["robin"]["id"])
check("each holds its own scanner's documents and nobody else's", tax["alex"]["scans"] == ["s1", "s2", "s3"] and tax["robin"]["scans"] == ["s4", "s5", "s6"])
check("a printed name does not choose the owner (robin's scan of Alex's page is robin's)", "s6" not in tax["alex"]["scans"] and tax["robin"]["owner"] == "robin")
check("each bundle's audience is its owner alone", all(b["audience"] == [b["owner"]] for b in r["bundles"]))
check("a bundle is shown whole to its owner and not at all to anyone else", BU.bundle_for(tax["alex"], "alex") is tax["alex"]
      and all(BU.bundle_for(tax["alex"], v) is None for v in ("robin", "jordan", "ava", "screen", "", None)))
check("one person's count, scans and ids never appear in the other's bundle", set(tax["alex"]["scans"]).isdisjoint(tax["robin"]["scans"]) and tax["alex"]["count"] == 3)
p2 = D.pipeline(two)
BU.stage(two, p2)
who = {x["id"]: x["scanner"] for x in two}
check("the scorer sees no crossed bundle", S._crossed(who, p2) == 0)
bad = copy.deepcopy(p2)
bad["bundles"][0]["scans"] = bad["bundles"][0]["scans"] + [m for b in bad["bundles"] for m in b["scans"] if b["owner"] != bad["bundles"][0]["owner"]][:1]
check("the scorer counts a bundle holding another person's scan as crossed", S._crossed(who, bad) == 1)
wide = copy.deepcopy(p2)
wide["bundles"][0]["audience"] = ["alex", "robin"]
check("the scorer counts a widened audience as crossed", S._crossed(who, wide) == 1)

# --- facet and project: filing only -----------------------------------------------------------------------------------------------
base = D.pipeline(scans)
before = {"views": copy.deepcopy(base["views"]), "docs": {k: {f: copy.deepcopy(v) for f, v in d.items() if f in ("owner", "row", "waiting", "cluster", "type", "sender")}
                                                         for k, d in base["docs"].items()}}
fac = {"robin": {"vendor:examplenet broadband": "work"}}
after = BU.stage(scans, base, facets=fac)
check("the stage changes no owner, row, waiting or view", after["views"] == before["views"] and all(
    {f: v for f, v in d.items() if f in ("owner", "row", "waiting", "cluster", "type", "sender")} == before["docs"][k] for k, d in after["docs"].items()))
net = [(sid, d) for sid, d in after["docs"].items() if d.get("sender") == "Examplenet Broadband"]
check("the owner's own facet map decides before the guess", all(d["facet"] == "work" for sid, d in net if d["owner"] == "robin"), str([d["facet"] for _, d in net]))
check("only the owner's map applies: another person's map never reaches the document",
      BU.facet_of("alex", "Examplenet Broadband", "bill", fac) == "home" and BU.facet_of("robin", "Examplenet Broadband", "bill", fac) == "work"
      and BU.facet_of("robin", "Examplenet Broadband", "bill", {"alex": {"vendor:examplenet broadband": "work"}}) == "home")
check("a map entry that is not a facet is ignored", BU.facet_of("robin", "Examplenet Broadband", "bill", {"robin": {"vendor:examplenet broadband": "everyone"}}) == "home")
check("junk files under no facet", all(d["facet"] is None for d in after["docs"].values() if d["type"] == "junk"))
check("a facet is one of the world model's facets", {d["facet"] for d in after["docs"].values() if d["facet"]} <= set(BU.valid_facets()))
for_alex = [b for b in after["bundles"] if b["owner"] == "alex"]
check("a bundle's audience is the owner whatever the facet", all(b["audience"] == [b["owner"]] for b in after["bundles"]) and for_alex)

# --- projects are proposed, in projects.proposals' shape, and suppressed like projects.proposals does ---------------------------------
props = [b["project"] for b in after["bundles"] if b["project"]]
check("a project is proposed for a tax year, the house or the car with enough documents",
      props and all(len(p["items"]) >= projects.MIN_ITEMS and p["key"].split(":")[1] in ("house", "vehicle") or p["key"].startswith("project:tax-") for p in props))
check("a proposal has projects.proposals' fields", all({"id", "name", "name_source", "count", "items", "reason"} <= set(p) for p in props))
check("a titan-made name is short", all(len(p["name"]) <= projects.NAME_CHARS and len(p["name"].split()) <= projects.NAME_WORDS for p in props))
check("small bundles propose nothing", all(b["project"] is None for b in after["bundles"] if b["count"] < projects.MIN_ITEMS))
one = next(b for b in after["bundles"] if b["project"])
again = BU.bundles(scans, after["docs"], filed={i: "pj_x" for i in one["project"]["items"]}, dismissed=set())
check("a bundle already filed proposes nothing", next(b for b in again["bundles"] if b["id"] == one["id"])["project"] is None)
again = BU.bundles(scans, after["docs"], dismissed={one["project"]["id"]})
check("a dismissed proposal stays dismissed", next(b for b in again["bundles"] if b["id"] == one["id"])["project"] is None)
check("the same pass again is a no-op", BU.bundles(scans, after["docs"], facets=fac)["bundles"] == BU.bundles(scans, after["docs"], facets=fac)["bundles"] == after["bundles"])

# --- the owner keeps it ---------------------------------------------------------------------------------------------------------------
db = Path(tempfile.mkdtemp(prefix="titan-scan-bundles-")) / "spaces.db"
try:
    BU.confirm(one, "robin" if one["owner"] != "robin" else "alex", "adult", db=db)
    check("anyone but the owner is refused, as for a project they cannot see", False)
except projects.Refused as exc:
    check("anyone but the owner is refused, as for a project they cannot see", exc.status == 404)
got = BU.confirm(one, one["owner"], "adult", db=db)
filed = projects.filings(db)
check("keeping files every document of the bundle under a new private project", got["items"] == len(one["project"]["items"])
      and {filed.get(i) for i in one["project"]["items"]} == {got["project"]} and projects.get(got["project"], db)["audience"] == "private")
undone = projects.undo(one["owner"], got["undo"], db)
check("one undo token puts it back", not projects.filings(db) and undone["items"] == len(one["project"]["items"]))
try:
    BU.confirm(next(b for b in after["bundles"] if not b["project"]), one["owner"], "adult", db=db)
    check("a bundle with no proposal cannot be kept as a project", False)
except projects.Refused:
    check("a bundle with no proposal cannot be kept as a project", True)

# --- text is data ----------------------------------------------------------------------------------------------------------------------
inj = [s for s in scans if truth[s["id"]]["injection"]]
check("a scan that tries to instruct changes no bundle: it files under its scanner like any other", inj and all(
    after["docs"][s["id"]]["owner"] == s["scanner"] and not after["docs"][s["id"]].get("effects") for s in inj))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all scan bundle checks passed'}")
sys.exit(1 if failures else 0)
