#!/usr/bin/env python3
"""titan-node#379: confidence bins vs how often a person later corrected the claim, aggregate only. Hermetic: a
temp bubble file.
# gate: hermetic
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import world as W  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


root = Path(tempfile.mkdtemp())
con = W.connect(root / "household.db")
now = time.time()
con.execute("INSERT INTO entities (id, type, name, audience, created_at) VALUES ('household:vet','org','Vet','household',?)", (now,))


def claim(cid: int, value: str, prior: float, corrected_by: str | None) -> None:
    con.execute("INSERT INTO claims (id, entity_id, attr, value_norm, value, created_at, updated_at) "
                "VALUES (?, 'household:vet', 'phone', ?, ?, ?, ?)", (cid, value, value, now - 100, now - 100))
    con.execute("INSERT INTO evidence (claim_id, source, kind, ref, origin, observed_at, prior, audience, added_at) "
                "VALUES (?, 'notes', 'note', ?, ?, ?, ?, 'household', ?)",
                (cid, f"r{cid}", f"o{cid}", time.strftime("%Y-%m-%d"), prior, now - 100))
    con.execute("INSERT INTO claim_log (claim_id, from_state, to_state, at, by) VALUES (?, NULL, 'current', ?, 'source:notes')",
                (cid, now - 90))
    if corrected_by:
        con.execute("INSERT INTO claim_log (claim_id, from_state, to_state, at, by) VALUES (?, 'current', 'superseded', ?, ?)",
                    (cid, now - 10, corrected_by))


claim(1, "555-0100", 0.95, None)          # high confidence, never corrected
claim(2, "555-0101", 0.6, "zach")         # middling, a person corrected it
claim(3, "555-0102", 0.6, "source:notes")  # moved by a re-sync: not a correction
con.commit()
con.close()

rep = W.calibration(root)
check("every claim that became current is counted", rep["claims"] == 3, json.dumps(rep))
by = {(b["lo"], b["hi"]): b for b in rep["bins"]}
mid = next(b for b in rep["bins"] if b["lo"] <= 0.6 < b["hi"])
top = next(b for b in rep["bins"] if b["n"] and b["lo"] >= 0.85)
check("a person's correction counts; a source re-sync doesn't", (mid["n"], mid["corrected"]) == (2, 1), str(mid))
check("the high bin: counted, never corrected", (top["n"], top["corrected"], top["rate"]) == (1, 0, 0.0), str(top))
check("aggregate only: no values or names", not any(x in json.dumps(rep) for x in ("555-", "Vet", "household:vet")))
check("an empty bin has no rate", any(b["n"] == 0 and b["rate"] is None for b in rep["bins"]))

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed")
sys.exit(1 if failures else 0)
