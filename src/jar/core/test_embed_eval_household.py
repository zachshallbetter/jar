#!/usr/bin/env python3
# gate: hermetic
"""embed_eval_household's scoring and question sets: ranks by cosine, right items by title, counts only."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import embed_eval_household as E  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


docs = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [0.7, 0.7, 0]]
queries = [[1, 0.1, 0], [0, 1, 0.2], [0.1, 0, 1]]
r = E.score(docs, queries, [{0}, {3}, {1}])
check("rank 1, rank 2, rank 4", r, {"n": 3, "hit@1": 1, "hit@3": 2, "mrr": round((1 + 1 / 2 + 1 / 4) / 3, 3)})
check("a right item that isn't ranked counts as a miss", E.score(docs, queries[:1], [{9}]), {"n": 1, "hit@1": 0, "hit@3": 0, "mrr": 0.0})

items = [{"title": "Vet info", "text": "Vet info\nMaple Animal Clinic, (555) 010-2000", "system": False},
         {"title": "Ballet", "text": "Ballet\nThu 5:15 PM", "system": False},
         {"title": "Ballet", "text": "Ballet\nThu 5:15 PM next week", "system": False},
         {"title": "Runbook", "text": "Runbook\nrestart the agent", "system": True}]

asked = []


def ask(text):
    asked.append(text)
    return None if "Vet" in text else "when is ballet?"


lk = E.likely_targets(items, 10, ask)
check("titan's own notes aren't sampled", any("Runbook" in t for t in asked), False)
check("a series counts every item with its title; a failed question is skipped",
      sorted((q, sorted(t)) for q, t in lk), [("when is ballet?", [1, 2]), ("when is ballet?", [1, 2])])
check("same seed, same sample", [sorted(t) for _, t in E.likely_targets(items, 1, lambda t: "q")],
      [sorted(t) for _, t in E.likely_targets(items, 1, lambda t: "q")])

check("doc2query: an item scores its best vector", E.score([[1, 0], [1, 0.2], [0.1, 1]], [[0, 1]], [{0}], owner=[0, 1, 0]),
      {"n": 1, "hit@1": 1, "hit@3": 1, "mrr": 1.0})
check("without owners it's one vector per item", E.score([[1, 0], [0, 1]], [[0, 1]], [{0}])["hit@1"], 0)

seen = []
E.likely_targets([dict(it, head="Zach's note: " + it["title"]) for it in items], 10, lambda t: seen.append(t) or "q",
                 shown=lambda it: it["head"] + "\n" + it["text"])
check("--headers: the writer can be shown the context line", all(t.startswith("Zach's note: ") for t in seen) and len(seen) == 3, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
