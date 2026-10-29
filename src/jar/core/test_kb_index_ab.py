#!/usr/bin/env python3
# gate: hermetic
"""kb_index_ab's pieces (#371): the runtime's chunker reproduced, splitting by heading, per-type chunking, and the
scoring (best chunk per item, hit@3 per kind and origin) on a fake embedder. No embedding server, no KB.
Run: python3 scripts/test_kb_index_ab.py"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import kb_index_ab as X  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


check("paragraphs merge up to the limit", X.chunk_text("a" * 10 + "\n\n" + "b" * 10 + "\n\n" + "c" * 20, 25),
      ["a" * 10 + "\n\n" + "b" * 10, "c" * 20])
check("a long paragraph is cut", [len(c) for c in X.chunk_text("x" * 50, 20)], [20, 20, 10])
doc = "Intro line.\n\n## Backups\nNightly at 3.\n\n## Restores\nFrom the latest dump.\n\nGarage:\nCode in the binder."
check("sections keep their headings", [s.splitlines()[0] for s in X.sections(doc)], ["Intro line.", "## Backups", "## Restores",
                                                                                      "Garage:"])
check("mail stays whole", len(X.by_type("mail", "p1\n\n" * 400)), 1)
check("a runbook splits by heading", len(X.by_type("system", doc)), 4)

# Fake embedder: a text's vector counts four topic words, so similarity is topic overlap.
TOPICS = ("backup", "garage", "ballet", "invoice")
emb = lambda texts: [[t.lower().count(w) + 0.01 for w in TOPICS] for t in texts]  # noqa: E731
items = [{"source_id": "note-runbook-a", "kind": "system", "text": doc.replace("Nightly", "backup nightly"), "title": "Backups",
          "header": "Titan runbook: Backups"},
         {"source_id": "cx-calendar-b", "kind": "calendar", "text": "Ballet recital Friday", "title": "Ballet", "header": ""},
         {"source_id": "cx-mail-c", "kind": "mail", "text": "Your invoice is attached", "title": "Invoice", "header": ""}]
cases = [{"q": "when is the backup", "expect": ["note-runbook-a"], "kind": "system", "origin": "generated"},
         {"q": "ballet", "expect": ["cx-calendar-b"], "kind": "calendar", "origin": "cited"},
         {"q": "garage code", "expect": ["cx-mail-c"], "kind": "mail", "origin": "miss"}]
r = X.run(items, cases, emb, {"cx-mail-c": ["Is there an invoice?"]})
check("every variant scored", sorted(r), ["V0", "V0Q", "V0Qsys", "V0Qsys-app", "V1", "V1Q", "V2", "V3"])
check("hits by kind and by origin", (r["V0"]["by_kind"]["system"], r["V0"]["by_kind"]["origin:cited"]), ([1, 1], [1, 1]))
check("the totals count each case once", (r["V0"]["cases"], r["V3"]["cases"]), (3, 3))
check("V3 adds a questions chunk per item that has them", r["V3"]["chunks"] - r["V2"]["chunks"], 1)
check("V0Q adds it to today's chunks, with no header", r["V0Q"]["chunks"] - r["V0"]["chunks"], 1)
r2 = X.run(items, cases, emb, {"cx-mail-c": ["Is there an invoice?"], "note-runbook-a": ["When do backups run?"]})
check("V0Qsys adds it only for system items (the mail item's questions are left out)",
      (r2["V0Q"]["chunks"] - r2["V0"]["chunks"], r2["V0Qsys"]["chunks"] - r2["V0"]["chunks"]), (2, 1))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
