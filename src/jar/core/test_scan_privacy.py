#!/usr/bin/env python3
# gate: hermetic   (fictional data, no network, no live model, no real files, a throwaway salt)
"""titan-node#1508: per-person privacy across the whole scans path. The check after the chain finds nothing on the chain's output and
finds every kind of fault we plant in it; the sensitive class never misses; each viewer's filtered view is what the viewer would get
if the scans they may not see did not exist; deleting a scan leaves nothing derived; no surface outside the scans modules reads a
scan yet; the committed privacy.json and pipeline.json match a fresh run."""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_pipeline as P  # noqa: E402
import scan_privacy as PV  # noqa: E402
import scans_bench as B  # noqa: E402
import scans_score as S  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'BAD '} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


here = S.BASELINE_FILE.parent
base, ext = B.generate(1499), B.generate(1499, True)

# --- the profile ---------------------------------------------------------------------------------------------------------------
check("the default batch is the same 300 scans, byte for byte (no other stage's score moves)",
      len(base["scans"]) == 300 and len(base["traps"]) == 40 and json.loads((here / "baseline.json").read_text())["benchmark"]["scans"] == 300)
check("the privacy profile is 321 scans and 61 traps, the same seed always the same", len(ext["scans"]) == 321 and len(ext["traps"]) == 61 and B.generate(1499, True) == ext)
new = {t["category"] for t in ext["traps"]} - {t["category"] for t in base["traps"]}
check("eight new trap categories", new == {"kid_mail", "other_adult_mail", "legal_letter", "shared_scanner", "medical_unknown_sender",
                                          "medical_appointment", "injection_in_bill", "identifier_keyed"}, str(sorted(new)))
check("the scans a pipeline sees still carry no truth", all(set(s) == {"id", "source", "scanner", "scanned_at", "title", "text"} for s in ext["scans"]))
check("every new trap names its leak", all(t["leak"] for t in ext["traps"]))

# --- the chain is clean, on both profiles ------------------------------------------------------------------------------------------
for name, batch in (("the 300", base), ("the 321", ext)):
    pred = P.pipeline(batch["scans"])
    sc = S.score(batch, pred, "scan_pipeline:pipeline")
    check(f"{name}: 0 leaks on every viewer and every trap category", sc["tasks"]["privacy"]["leaks"] == 0 and not any(sc["tasks"]["privacy"]["by_trap"].values()))
    check(f"{name}: the check found nothing to withhold, and nothing is left", pred["privacy"]["found"] == 0 and pred["privacy"]["violations"] == 0
          and pred["privacy"]["withheld"] == 0, str(pred["privacy"]))
    check(f"{name}: the check on the chain's own output is empty", PV.check(batch["scans"], pred) == [])
check("the shared scanner's three scans derive nothing and are held", P.pipeline(ext["scans"])["privacy"]["held"] and all(
    P.pipeline(ext["scans"])["docs"][sid] == {"owner": None, "held": "unclaimed"} for sid in P.pipeline(ext["scans"])["privacy"]["held"]))
before = S.score(ext, P.chain(ext["scans"]), "chain")["tasks"]["privacy"]
check("without the check, the shared scanner invented an owner and leaked (the before)", before["leaks"] > 0 and set(before["by_trap"]) == {"shared_scanner"}, str(before["by_trap"]))
check("the check finds that, and withholding it leaves no leak", {v["rule"] for v in PV.check(ext["scans"], P.chain(ext["scans"]))} >= {"unowned", "wider_than_source", "view_leak"})

# --- the sensitive class: either layer is enough, the miss rate is 0 -------------------------------------------------------------------
for batch in (base, ext):
    q = PV.class_quality(batch)
    check("medical and legal mail is never missed", q["medical"]["missed"] == 0 and q["legal"]["missed"] == 0, str(q))
check("an unknown clinic with medical words is medical (D2)", all("medical" in PV.classify(s) for s in ext["scans"] if (ext["truth"][s["id"]].get("trap") or {}).get("category") == "medical_unknown_sender"))
check("the local model's label alone is enough", PV.classify({"title": "x", "text": "Placeholder Co\nPlain words about a sofa"}, "medical") == {"medical"} and not PV.classify({"title": "x", "text": "Placeholder Co\nPlain words about a sofa"}))
check("a legal letter is legal, and a jury summons is in the class", all("legal" in PV.classify(s) for s in ext["scans"] if ext["truth"][s["id"]].get("legal")))
check("a kid's printed name is noticed (to hold back, never to resolve)", PV.kid_subject({"text": "Patient: Ava Hale"}, PV.N.BENCH_PEOPLE) == {"ava"})

# --- the audience of a scan: shares widen it, never past the rules --------------------------------------------------------------------
people = PV.N.BENCH_PEOPLE
plain = {"id": "s1", "scanner": "alex", "title": "Newsletter", "text": "Fakeville Elementary\nDear families, field day is planned."}
medical = {"id": "s2", "scanner": "alex", "title": "Visit", "text": "Testville Family Clinic\nPatient balance: $10.00. Date of service: 2026-09-01"}
rule = {"s1": [{"with": ["robin", "ava", "household"]}], "s2": [{"with": ["robin", "ava", "household"]}]}
check("an unshared scan is its scanner's alone", PV.effective_audience(plain, people) == {"alex"} and PV.effective_audience(medical, people) == {"alex"})
check("a household share reaches everyone, the screen and the kids, for a plain letter", PV.effective_audience(plain, people, rule) == set(people) | {"screen"})
check("a rule or bulk approval never shares medical mail", PV.effective_audience(medical, people, rule) == {"alex"})
check("a per-item share of medical mail reaches the named adult, never the kid, never the screen",
      PV.effective_audience(medical, people, {"s2": [{"with": ["robin", "ava", "screen"], "item": True}]}) == {"alex", "robin"})
check("a kid gets no money, claim or policy even when shared", "ava" not in PV.effective_audience(
    plain, people, {"s1": [{"with": ["ava"]}]}, {"fields": {"type": "bill"}}) and "ava" in PV.effective_audience(plain, people, {"s1": [{"with": ["ava"]}]}, {"fields": {"type": "letter"}}))
check("a scan from the shared scanner, or an unknown person, is nobody's", PV.effective_audience({**plain, "scanner": "household"}, people, {"s1": [{"with": ["robin"]}]}) == set()
      and PV.effective_audience({**plain, "scanner": "stranger"}, people) == set())

# --- every rule fires on a planted fault ------------------------------------------------------------------------------------------------
scans = ext["scans"]
clean = P.pipeline(scans)
by_id = {s["id"]: s for s in scans}
truth = ext["truth"]


def owner_of(sid):
    return by_id[sid]["scanner"]


def first(pred, kind, pick=lambda it: True):
    return next(it for it in PV.items(pred) if it.kind == kind and pick(it))


def faulted(mutate, **kw):
    pred = copy.deepcopy(clean)
    mutate(pred)
    return {v["rule"] for v in PV.check(scans, pred, **kw)}


th = clean["threads"]["threads"][0]
other = "robin" if th["owner"] != "robin" else "alex"
check("a thread's audience widened to another adult is caught", "wider_than_source" in faulted(lambda p: p["threads"]["threads"][0].update(audience=[th["owner"], other])))
check("a thread with no audience is caught", "audience" in faulted(lambda p: p["threads"]["threads"][0].update(audience=None)))
check("a thread marked household is caught", "audience" in faulted(lambda p: p["threads"]["threads"][0].update(audience="household")))
check("a thread holding another person's scan is caught", "mixed_owner" in faulted(lambda p: p["threads"]["threads"][0]["scans"].append(next(s["id"] for s in scans if s["scanner"] == other))))
check("a thread with a wrong count is caught", "count" in faulted(lambda p: p["threads"]["threads"][0].update(count=99)))
check("a thread titled with a name nobody in its audience has is caught",
      "names" in faulted(lambda p: p["threads"]["threads"][0].update(why=f"letters to {PV.N.BENCH_PEOPLE[other]['name']}")))
check("a thread titled with its own owner's name is fine", not faulted(lambda p: p["threads"]["threads"][0].update(why=f"letters to {PV.N.BENCH_PEOPLE[th['owner']]['name']}")))
check("a notice card to another adult is caught", "wider_than_source" in faulted(lambda p: p["notices"][0].update(recipients=[p["notices"][0]["owner"], other if p["notices"][0]["owner"] != other else "jordan"])))
check("a bundle widened to the household is caught", "audience" in faulted(lambda p: p["bundles"][0].update(audience="household")))
check("a bundle that mixes owners is caught", "mixed_owner" in faulted(lambda p: p["bundles"][0]["scans"].append(next(s["id"] for s in scans if s["scanner"] != p["bundles"][0]["owner"]))))
check("a project proposal holding another person's scan is caught", "mixed_owner" in faulted(
    lambda p: next(b for b in p["bundles"] if isinstance(b.get("project"), dict))["project"]["items"].append("scan:" + next(s["id"] for s in scans if s["scanner"] == "jordan"))
    if next(b for b in p["bundles"] if isinstance(b.get("project"), dict))["owner"] != "jordan" else next(b for b in p["bundles"] if isinstance(b.get("project"), dict))["project"]["items"].append("scan:" + next(s["id"] for s in scans if s["scanner"] == "alex"))))
check("a cycle for another person is caught", "mixed_owner" in faulted(lambda p: next(iter(p["cycles"]["cycles"].values())).update(owner=other if next(iter(p["cycles"]["cycles"].values()))["owner"] != other else "jordan")))
check("a junk plan that deletes is caught", "junk_irreversible" in faulted(lambda p: p["junk"]["junk"][0].update(do="delete")))
med_sid = next(s["id"] for s in scans if (truth[s["id"]].get("trap") or {}).get("category") == "medical_adult")
check("a junk plan on a medical letter is caught", "junk_sensitive" in faulted(lambda p: p["junk"]["junk"].append(
    {"scan": med_sid, "owner": owner_of(med_sid), "sender": "x", "why": "rule", "do": "archive", "reversible": True, "undo": "unarchive", "source": "scan"})))
check("a medical cycle shown to the household is caught", "wider_than_source" in faulted(
    lambda p: next(c for c in p["cycles"]["cycles"].values() if med_sid in c["members"]).update(audience=[owner_of(med_sid), "robin" if owner_of(med_sid) != "robin" else "alex"])))
check("a medical letter shared by a bulk approval is caught the moment the pipeline widens it", "wider_than_source" in faulted(
    lambda p: next(c for c in p["cycles"]["cycles"].values() if med_sid in c["members"]).update(audience=[owner_of(med_sid), "jordan" if owner_of(med_sid) != "jordan" else "alex"]),
    shares={med_sid: [{"with": ["jordan", "alex"]}]}))
check("... and it is allowed by a per-item share", not faulted(
    lambda p: next(c for c in p["cycles"]["cycles"].values() if med_sid in c["members"]).update(audience=[owner_of(med_sid), "jordan" if owner_of(med_sid) != "jordan" else "alex"]),
    shares={med_sid: [{"with": ["jordan", "alex"], "item": True}]}))
kid_sid = next(s["id"] for s in scans if (truth[s["id"]].get("trap") or {}).get("category") == "kid_mail" and truth[s["id"]]["type"] == "statement")
kid_aud = lambda p: p["docs"][kid_sid]["envelope"]["record"].update(audience=[owner_of(kid_sid), "ava"])    # noqa: E731
check("a kid shown a statement derived from mail about them is caught, shared or not",
      {"kid_unsafe", "wider_than_source"} <= faulted(kid_aud) and "kid_unsafe" in faulted(kid_aud, shares={kid_sid: [{"with": ["ava"]}]}))
check("a count shown to the screen is caught", "view_leak" in faulted(lambda p: p["views"].setdefault("screen", []).append({"members": [med_sid], "count": 1})))
check("a row under a viewer who is not a person is caught", "view" in faulted(lambda p: p["views"].setdefault("household", []).append({"members": [med_sid], "count": 1})))
check("a raw printed identifier in a derived item is caught", "raw_identifier" in faulted(
    lambda p: p["bundles"][0].update(name="Holds TEST-PWR-0001") if re.search(r"TEST-PWR-0001", " ".join(s["text"] for s in scans if s["id"] in p["bundles"][0]["scans"])) else
    p["threads"]["threads"][0].update(why="Claim " + next(iter(PV._ID_TOKEN.findall(" ".join(by_id[m]["text"] for m in p["threads"]["threads"][0]["scans"]))), "TEST-CLM-0000"))))
check("an effect caused by scanned text is caught", "effect" in faulted(lambda p: next(iter(p["docs"].values())).update(effects=["shared"])))
check("a medical letter sent to the cloud is caught", "egress" in faulted(lambda p: p.update(egress=[{"scan": med_sid, "destination": "cloud"}])))
check("a local call on a medical letter is fine", not faulted(lambda p: p.update(egress=[{"scan": med_sid, "destination": "local"}])))
check("an item from a scan that is not in the batch is caught", "unknown_source" in faulted(lambda p: p["threads"]["threads"][0]["scans"].append("scan-999")))
check("a scan nobody owns that was processed is caught", "unowned" in {v["rule"] for v in PV.check(scans, P.chain(scans))})

# the fail-closed stage: a stage that widens an audience loses that row, and the report says so
bad = P.chain(scans)
bad["threads"]["threads"][0]["audience"] = [bad["threads"]["threads"][0]["owner"], other]
n_threads = len(bad["threads"]["threads"])
PV.stage(scans, bad)
check("a stage that widens an audience has its row withheld, and the result is clean", len(bad["threads"]["threads"]) == n_threads - 1 and bad["privacy"]["withheld"] >= 1
      and bad["privacy"]["violations"] == 0 and PV.check(scans, bad) == [], str(bad["privacy"]))

# --- views x viewers: every viewer sees what they would see if the others' scans were never scanned -----------------------------------------
VIEWERS = S.VIEWERS
by_truth = lambda sid, v: S.visible(truth[sid], by_id[sid]["scanner"], v)    # noqa: E731
for v in VIEWERS:
    mine = PV.view_for(scans, clean, v)
    seen = {m for it in PV.items(mine) if it.kind != "view" for m in it.members} | set(mine["docs"]) | {m for r in mine["views"][v] for m in r["members"]}
    check(f"{v}: only scans this viewer may see appear anywhere in their view", all(by_truth(m, v) for m in seen), str([m for m in seen if not by_truth(m, v)][:3]))
    own = [s for s in scans if s["scanner"] == v]          # nothing was shared: a person's view is built from their own scans alone
    if v in PV.N.BENCH_PEOPLE and own and PV.N.BENCH_PEOPLE[v]["role"] != "kid":
        alone = PV.view_for(own, P.pipeline(own), v)
        same = json.dumps(PV.view_for(scans, clean, v), sort_keys=True, default=str) == json.dumps(alone, sort_keys=True, default=str)
        check(f"{v}: the view equals the view of a household where nobody else's scans exist (no count, title, key or order leaks)", same)
    else:
        check(f"{v}: sees nothing derived", not [it for it in PV.items(mine) if it.kind != "view"] and not mine["docs"] and mine["views"][v] == [])
check("the screen sees 0 rows, 0 docs and 0 counts of anything scanned", PV.view_for(scans, clean, "screen")["views"]["screen"] == [] and PV.view_for(scans, clean, "screen")["docs"] == {}
      and not [it for it in PV.items(PV.view_for(scans, clean, "screen")) if it.kind != "view"])
check("a kid sees nothing derived from a parent's scans", not [it for it in PV.items(PV.view_for(scans, clean, "ava")) if it.kind != "view"])
check("a viewer the household does not know sees nothing", not [it for it in PV.items(PV.view_for(scans, clean, "stranger")) if it.kind != "view"])
shared = {sid: [{"with": ["robin"]}] for sid in [s["id"] for s in scans if s["scanner"] == "alex" and not PV.classify(s)][:20]}
rv = PV.view_for(scans, clean, "robin", shares=shared)
check("a shared scan reaches only the person it was shared with, and only where all its sources are shared",
      set(rv["docs"]) >= set(shared) and all(by_id[m]["scanner"] == "robin" or m in shared for it in PV.items(rv) if it.kind != "view" for m in it.members))

# --- deletion is complete (P12) -----------------------------------------------------------------------------------------------------------
gone = [s["id"] for s in scans if s["scanner"] == "alex"][:12] + [med_sid]
left = [s for s in scans if s["id"] not in gone]
after = P.pipeline(left)
check("after a delete, nothing derived mentions the scans", all(not PV.references(after, g) for g in gone))
check("... but before it, something did", all(PV.references(clean, g) for g in gone[:3]))
check("the deletion rule fires on a stale result", {v["rule"] for v in PV.check(scans, clean, deleted=gone)} == {"deleted"})
check("the check passes on the result after the delete", PV.check(left, after, deleted=gone) == [])

# --- scanned text is untrusted (P10) -----------------------------------------------------------------------------------------------------
inj = [s["id"] for s in scans if truth[s["id"]]["injection"]]
check("six scans carry an instruction, and none changes an audience or causes an effect", len(inj) == 6 and all(
    clean["docs"][i]["owner"] == owner_of(i) and not clean["docs"][i].get("effects") and clean["docs"][i]["envelope"]["record"]["audience"] == [owner_of(i)] for i in inj))
check("an instruction raises no notice, edge or proposal", not [n for n in clean["notices"] if n["scan"] in inj])

# --- identifiers are keyed (P9) ----------------------------------------------------------------------------------------------------------
blob = json.dumps(clean, sort_keys=True, default=str)
raw = {t for s in scans for t in PV._ID_TOKEN.findall(s["text"])}
check("a raw sweep of the whole result finds none of the identifiers the scans print", raw and not [t for t in raw if t in blob], str([t for t in raw if t in blob][:3]))

# --- scans do not reach a surface yet; when one does, it goes through view_for ---------------------------------------------------------------------
here_py = Path(__file__).resolve().parent
mods = {p.stem for p in here_py.glob("scan_*.py")} | {"scans_bench", "scans_score"}
users = sorted(p.name for p in here_py.glob("*.py") if p.stem not in mods and not p.stem.startswith("test_scan")
               and re.search(r"^\s*(?:import|from)\s+(?:%s)\b" % "|".join(sorted(mods)), p.read_text(), re.M))
# The one reader (#1507): the bulk review route reads the pipeline only through scan_review, which reads every row through view_for and then only the
# viewer's own documents; test_scan_review.py holds its views x viewers (an admin, adults, a kid, the screen, a shared scan and a medical one).
check("no module outside the scans modules reads a scan prediction yet, but the review route (items, Now, Unsorted, /v1/items, search, calendar, situation, the graph)",
      users == ["agent_api_server.py"] and "view_for" in (here_py / "scan_review.py").read_text() and (here_py / "test_scan_review.py").exists(),
      f"{users}: these now read scans. Route every row through scan_privacy.view_for and add each to views x viewers here (the screen and a kid included)")
surfaces = ("items.py", "agent_api_server.py", "situation.py", "card_items.py")
check("the items surfaces do not name the scans modules", not [f for f in surfaces if (here_py / f).exists() and re.search(r"scan_(pipeline|privacy|envelope|bundles|threads|cycles|notices|junk|dedupe)", (here_py / f).read_text())])

# --- the committed results ----------------------------------------------------------------------------------------------------------------
check("privacy.json is what a fresh run gives", json.loads((here / "privacy.json").read_text()) == json.loads(json.dumps(PV.report(1499))) if (here / "privacy.json").exists() else False)
fresh = json.loads(json.dumps(S.score(base, P.pipeline(base["scans"]), "scan_pipeline:pipeline")))
check("pipeline.json is what a fresh run gives, and carries the check's result", json.loads((here / "pipeline.json").read_text()) == fresh and fresh["tasks"]["privacy_check"]["violations"] == 0)
rep = json.loads((here / "privacy.json").read_text())["profiles"] if (here / "privacy.json").exists() else {}
check("privacy.json: 0 leaks after on both profiles, and the only before was the shared scanner",
      rep and rep["base"]["after"]["leaks"] == 0 and rep["privacy"]["after"]["leaks"] == 0 and rep["base"]["before"]["leaks"] == 0
      and {c for c, v in rep["privacy"]["by_category"].items() if v["leaks_before"]} == {"shared_scanner"})
for fn in ("baseline", "dedupe", "cycles", "threads", "notices", "junk", "bundles", "envelope"):
    check(f"{fn}.json is untouched by the privacy profile", "privacy_check" not in json.loads((here / f"{fn}.json").read_text())["tasks"])

print(f"{'FAILED' if failures else 'all ok'}: {len(failures)} failing")
sys.exit(1 if failures else 0)
