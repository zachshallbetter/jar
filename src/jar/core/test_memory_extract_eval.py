#!/usr/bin/env python3
# gate: hermetic   (no model: the oracle feeds each window's ideal output through the real extractor)
"""The memory extractor's code path keeps what a perfect model would say (#1090, #1092): on the fictional bank
(scripts/evalsets/memory_extract.jsonl), every new fact and correction is stored, nothing-new windows store nothing,
and no fact is false or wrongly forgotten. A filter or check that starts dropping real facts fails here.
Run: python3 scripts/test_memory_extract_eval.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory_extract_eval as me  # noqa: E402

t = me.run("oracle", ["v0"], 1, False)["v0"]
got = (t["new_kept"], t["new_expected"], t["fix_made"], t["fix_expected"], t["none_clean"], t["none_windows"],
       t["false_facts"], t["false_replaces"])
want = (t["new_expected"], t["new_expected"], t["fix_expected"], t["fix_expected"], t["none_windows"], t["none_windows"], 0, 0)
ok = got == want and t["new_expected"] >= 13 and t["fix_expected"] >= 4
print(f"{'PASS' if ok else 'FAIL'}  oracle: new {got[0]}/{got[1]}, fixes {got[2]}/{got[3]}, nothing-new clean "
      f"{got[4]}/{got[5]}, false facts {got[6]}, false REPLACES {got[7]}")
sys.exit(0 if ok else 1)
