#!/usr/bin/env python3
# gate: hermetic   (fictional data, no network, no live model, no real files, a throwaway salt)
"""titan-node#1501: dedupe and join for scans. The four layers (exact, rescan, multi-page, paper + email) raise dedupe recall
over the envelope without lowering precision; a join never crosses owners; hidden copies are links with an undo; the
committed score matches a fresh run."""
from __future__ import annotations

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hooks  # noqa: E402
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
truth = batch["truth"]
scans = batch["scans"]
scanner = {s["id"]: s["scanner"] for s in scans}
pred = D.pipeline(scans)
res = json.loads(json.dumps(S.score(batch, pred, "scan_dedupe:pipeline")))
env = json.loads((S.BASELINE_FILE.parent / "envelope.json").read_text())["tasks"]["dedupe"]
t = res["tasks"]
d = t["dedupe"]

# --- the score -----------------------------------------------------------------------------------------------------------
committed = S.BASELINE_FILE.parent / "dedupe.json"
check("the committed dedupe score matches a fresh run", committed.exists() and json.loads(committed.read_text()) == res)
check("dedupe precision is not lower than the envelope's", d["precision"] >= env["precision"], f"{d['precision']} vs {env['precision']}")
check("dedupe f1 beats the envelope's", d["f1"] > env["f1"])
check("multi-page recall beats the envelope's", d["by_kind"]["multipage"]["recall"] > env["by_kind"]["multipage"]["recall"])
check("rescan recall reaches 1.0", d["by_kind"]["rescan"]["recall"] == 1.0)
check("exact and paper + email stay at 1.0", d["by_kind"]["exact"]["recall"] == 1.0 and d["by_kind"]["paper_email"]["recall"] == 1.0)
check("no false merges, no cross-owner merges", d["false_merges"] == 0 and d["cross_owner_merges"] == 0)
check("privacy: 0 leaks on every viewer and every trap", t["privacy"]["leaks"] == 0 and not any(t["privacy"]["by_trap"].values()))
check("nothing else regresses against the envelope", all(res["tasks"][k] == json.loads((S.BASELINE_FILE.parent / "envelope.json").read_text())["tasks"][k]
                                                         for k in ("type", "sender", "account", "amount", "due_date")))

# --- owners ---------------------------------------------------------------------------------------------------------------
dd = pred["dedupe"]
check("no cluster holds two owners", all(len({scanner[m] for m, c in dd["cluster"].items() if c == root}) == 1 for root in dd["clusters"]))
check("every view row is one owner's own scans", all(all(scanner[m] == v for m in row["members"]) for v, rows in pred["views"].items() for row in rows))
check("the owner of every doc is still its scanner", all(x["owner"] == scanner[sid] for sid, x in pred["docs"].items()))
two = [s for s in scans if truth[s["id"]]["trap"] and truth[s["id"]]["trap"]["category"] == "two_owner_rescan"]
check("two people's identical-looking letters stay separate (3 letters, 6 scans, 6 clusters)",
      len(two) == 6 and len({dd["cluster"][s["id"]] for s in two}) == 6 and len({s["text"] for s in two}) == 3)
cross = [s for s in scans if truth[s["id"]]["trap"] and truth[s["id"]]["trap"]["category"] == "paper_email_cross_owner"]
check("a bill on paper for one person and by email for another is never joined",
      len(cross) == 4 and len({dd["cluster"][s["id"]] for s in cross}) == 4)

# the same text, scanned by two people, a hand-made case: identical bodies, mail and paper, two scanners
base = {"id": "x-1", "source": "scan", "scanner": "alex", "scanned_at": "2026-09-01T08:00:00Z", "title": "Notice",
        "text": "Example Water Works\nPO Box 0000, Faketown, ZZ 00000\n\nNotice of rate change dated September 1, 2026. Ref 12\n"}
twin = {**base, "id": "x-2", "scanner": "robin", "scanned_at": "2026-09-01T09:00:00Z"}
again = {**base, "id": "x-3", "scanned_at": "2026-09-02T09:00:00Z"}
p3 = D.pipeline([base, twin, again])
c = p3["dedupe"]["cluster"]
check("hand-made: the same letter by alex and robin is two rows; alex's own repeat joins alex's", c["x-1"] == c["x-3"] != c["x-2"])
check("hand-made: neither owner's view counts the other's scan", {tuple(r["members"]) for r in p3["views"]["alex"]} == {("x-1", "x-3")}
      and {tuple(r["members"]) for r in p3["views"]["robin"]} == {("x-2",)})
bill = next(s for s in scans if truth[s["id"]]["type"] == "bill" and s["source"] == "scan" and not truth[s["id"]]["dup_group"])
mail = {**bill, "id": "x-4", "source": "email", "scanner": "robin" if bill["scanner"] != "robin" else "alex"}
mail["text"] = f"From: {truth[bill['id']]['sender']} <noreply@x.example>\nSubject: Your bill is ready\n\n" + bill["text"].split("\n", 2)[2]
p4 = D.pipeline([bill, mail])
check("hand-made: the same bill on paper (one owner) and email (another) is never joined", p4["dedupe"]["cluster"][bill["id"]] != p4["dedupe"]["cluster"]["x-4"])
mail["scanner"] = bill["scanner"]
p5 = D.pipeline([x for x in scans if x["id"] != bill["id"]] + [bill, mail])   # the linker measures against the house's whole corpus
check("hand-made: the same bill on paper and email, one owner, is joined by the linker", p5["dedupe"]["cluster"][bill["id"]] == p5["dedupe"]["cluster"]["x-4"]
      and [h["layer"] for h in p5["dedupe"]["clusters"][p5["dedupe"]["cluster"]["x-4"]]["hidden"]] == ["paper_email"])

# --- the layers ------------------------------------------------------------------------------------------------------------
news = [s for s in scans if truth[s["id"]]["type"] == "letter" and "newsletter" in s["text"]]
check("newsletters that differ only in a reference number stay separate (clusters are the truth's groups)", len(news) >= 4
      and all((dd["cluster"][a["id"]] == dd["cluster"][b["id"]]) == bool(truth[a["id"]]["dup_group"] and truth[a["id"]]["dup_group"] == truth[b["id"]]["dup_group"])
              for a in news for b in news if a["id"] < b["id"]))
claims = [s["id"] for s in scans if truth[s["id"]]["type"] == "claim_letter"]
check("a claim thread's letters are four documents, not one", len({dd["cluster"][i] for i in claims}) == len(claims))
check("two bills of one account, different months, stay two", all(
    dd["cluster"][a] != dd["cluster"][b] for a in [s["id"] for s in scans if truth[s["id"]]["type"] == "bill"][:12]
    for b in [s["id"] for s in scans if truth[s["id"]]["type"] == "bill" and truth[s["id"]]["account"] == truth[a]["account"]
              and truth[s["id"]]["due_date"] != truth[a]["due_date"] and truth[s["id"]]["dup_group"] != truth[a]["dup_group"]]))
check("pages of a document are listed in page order", all(
    [S_ for S_ in c_["pages"]] == sorted(c_["pages"], key=lambda m: D._page(next(s for s in scans if s["id"] == m)["text"])[0])
    for c_ in dd["clusters"].values() if c_.get("pages")))
check("pages that can't be told apart (one page number twice) are not joined; they go to review",
      dd["review"] and all(r["kind"] == "ambiguous_pages" and len({dd["cluster"][m] for m in r["members"]}) == len(r["members"]) for r in dd["review"]))
check("an ambiguous page is never hidden", not any(h["scan"] in r["members"] for r in dd["review"] for c_ in dd["clusters"].values() for h in c_["hidden"]))

# --- never destructive, with undo ----------------------------------------------------------------------------------------------
before = copy.deepcopy(scans)
D.pipeline(scans)
check("no scan is deleted, edited or re-owned", scans == before and len(pred["docs"]) == 300)
hidden = [(root, h["scan"]) for root, c_ in dd["clusters"].items() for h in c_["hidden"]]
check("every hidden copy is named with the layer that linked it", hidden and all(h["layer"] in ("exact", "rescan", "paper_email", "multipage")
                                                                               for c_ in dd["clusters"].values() for h in c_["hidden"]))
root, sid = hidden[0]
un = D.undo(dd, sid)
check("undo splits one copy back out as its own row", un["cluster"][sid] == sid and sid not in {h["scan"] for c_ in un["clusters"].values() for h in c_["hidden"]})
check("undo leaves everything else as it was", all(un["cluster"][k] == v for k, v in dd["cluster"].items() if k != sid) and dd["cluster"][sid] == root)
check("the kept copy of a paper + email pair is the email", all(
    next(s for s in scans if s["id"] == c_["kept"])["source"] == "email" for c_ in dd["clusters"].values() if len(c_["sources"]) == 2))

# --- hermetic -----------------------------------------------------------------------------------------------------------------
check("a throwaway salt only: the real salt file is untouched", hooks.SALT_FILE == salt_before)
check("the live model is off: the envelope's evaluator is the rules'", all(x["envelope"]["interpretation"]["evaluator"] == "rules-standin" for x in pred["docs"].values())
      and not E.live_on())

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all scan dedupe checks passed'}")
sys.exit(1 if failures else 0)
