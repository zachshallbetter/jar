#!/usr/bin/env python3
# gate: hermetic
"""fence_classify_eval's scoring: counts only, recall against the word lists, precision from a second model."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fence_classify_eval as F  # noqa: E402

items = [
    {"text": "a1", "entity": False, "listed": {"health"}},        # listed, Decider agrees
    {"text": "a2", "entity": False, "listed": {"money"}},         # listed, Decider says none
    {"text": "a3", "entity": True, "listed": set()},              # names an entity: covered elsewhere
    {"text": "u1", "entity": False, "listed": set()},             # unflagged, Decider: legal, judge agrees
    {"text": "u2", "entity": False, "listed": set()},             # unflagged, Decider: health, judge disagrees
    {"text": "u3", "entity": False, "listed": set()},             # unflagged, none
    {"text": "boom", "entity": False, "listed": set()},           # the Decider fails
]
said = {"a1": "health", "a2": "none", "a3": "money", "u1": "legal", "u2": "Health", "u3": "none"}


def classify(t):
    if t == "boom":
        raise OSError("down")
    return said[t].lower()


out = F.score(items, classify, lambda t, c: t == "u1")
expect = {"items": 7, "errors": 1, "listed": 2, "listed_agree": 1, "listed_none": 1, "named_entity": 1, "unflagged": 3,
          "unflagged_legal": 1, "unflagged_legal_confirmed": 1, "unflagged_health": 1, "unflagged_health_rejected": 1,
          "recall_vs_lists": 0.5, "unflagged_sensitive_rate": 0.667, "precision_est": 0.5}
bad = {k: (out.get(k), v) for k, v in expect.items() if out.get(k) != v}
no_text = all(isinstance(v, (int, float)) for v in out.values())
print("ok" if not bad and no_text else f"FAIL {bad} counts-only={no_text}")
sys.exit(0 if not bad and no_text else 1)
