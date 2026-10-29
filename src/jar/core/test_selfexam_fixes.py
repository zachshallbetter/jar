#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Hermetic tests for selfexam_fixes (titan-node#595): misses in a row, the diagnosis, a proposal to the owner for
"not found", re-asked first, pair ids only.

    python3 scripts/test_selfexam_fixes.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = Path(tempfile.mkdtemp())
os.environ.update({"HOUSEHOLD_DB": str(_TMP / "h.db"), "KB_ACL_DB": str(_TMP / "a.db"), "AGENT_FENCES": "0",
                   "AGENT_TZ": os.environ.get("AGENT_TZ", "America/Los_Angeles")})
import eval_bank as EB  # noqa: E402
import selfexam_fixes as F  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


B = W.Bubbles(_TMP / "world")
B.sync("household", [W.Obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                           value="555 0142", observed_at="2026-09-01", kind="household", ref="h", origin="household.yaml",
                           owner=None, audience="household", aliases=(("vet", ""),))])
W.set_fact(B, "the vet", "phone", "555 0199", "zach", "admin", people=[])
claim = B.world("household").con.execute("SELECT id FROM claims WHERE value='555 0199'").fetchone()[0]
db = _TMP / "bank.db"
con = EB._db(db)
for ref, q in ((f"world:household:{claim}", "What's Maple Animal Clinic's phone number?"),
               ("world:household:999", "What's gone?")):
    con.execute("INSERT INTO pairs (ref, owner, audience, source, question, answer, created_at) VALUES (?,?,?,?,?,?,?)",
                (ref, "zach", "household", "world-statement", q, "555 0199", 0))
con.commit()
con.close()
for ok in (False, False):
    EB.record(f"world:household:{claim}", ok, db)
EB.record("world:household:999", False, db)
EB.record("world:household:999", False, db)
pairs = EB.for_person("zach", db)
check("misses in a row are counted", sorted(p["misses"] for p in pairs), [2, 2])
EB.record("world:household:999", True, db)
check("…and reset by a pass", sorted(p["misses"] for p in EB.for_person("zach", db)), [0, 2])
rows = F.diagnose(B, EB.for_person("zach", db), {"zach": "admin"})
check("found, and the block has the value: the reply missed it (wording, not knowledge)",
      [(r["why"]) for r in rows], ["reply missed"])
# a question that doesn't name it the way titan knows it
con = EB._db(db)
con.execute("UPDATE pairs SET question='What is the animal doctor phone?' WHERE ref=?", (f"world:household:{claim}",))
con.commit()
con.close()
rows = F.diagnose(B, EB.for_person("zach", db), {"zach": "admin"})
check("not found: a proposal to its owner", ([r["why"] for r in rows], F.propose(B, rows)), (["not found"], 1))
check("…in their daily slot, as a review card", W._note_review(B, "zach")["card"][0],
      "Titan couldn't find Maple Animal Clinic's phone when asked")
F.reask_first(rows, db)
check("…and re-asked first tomorrow", [p["last_asked"] for p in EB.for_person("zach", db) if p["misses"] >= 2], [0])
check("reported by pair id only", all(set(r) >= {"pair", "why"} and r["pair"].startswith("p-") for r in rows), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
