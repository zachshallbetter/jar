#!/usr/bin/env python3
"""Free-text sensitive classes on the local Decider, measured before anything fences by it (#357).

Fences find health and money by word lists and things by the world model's names. Text that names no entity and
matches no list slips past both. This asks the local Decider-2B which kind (health, money, legal, none) each KB
item is, and measures two things, counts only:

  - against the word lists, on items they flag: how often the Decider agrees (its recall where we know);
  - on the items nothing flags (no entity, no list): how often it says sensitive, and how often a second local
    model (Qwen3-8B on :8010) agrees when it does (an estimate of its precision, so its false-positive rate).

Report only: no fence is set and nothing is stored. Never prints a title or a word of any item. Personal data stays
on titan: both models are local; nothing goes to Gemini or Jev.

  python3 scripts/fence_classify_eval.py [--limit 150]       (on titan; uses GPU1's Decider and :8010)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

DECIDER_URL = os.environ.get("DECIDER_URL", "http://127.0.0.1:8031").rstrip("/")
JUDGE_URL = os.environ.get("AGENT_ENDPOINT") or "http://127.0.0.1:8010"
CLASSES = ("health", "money", "legal")
MAX_CHARS = 1500           # the Decider shares GPU1 with the agent: ~4000 chars runs it out of memory
QUESTIONS = {"kind": {
    "type": "choice",
    "instructions": "Is this text mainly about a person's private matters of one of these kinds?",
    "criteria": {"health": "their body or mind: illness, therapy, medication, doctors, appointments for care",
                 "money": "their finances: bills, balances, pay, debts, accounts, purchases and prices",
                 "legal": "a legal matter: lawyers, courts, custody, divorce, contracts in dispute, police",
                 "none": "anything else, including everyday logistics, work, school, hobbies and plans"}}}
JUDGE = """/no_think
Here is a piece of text from a household's notes and mail:

---
{text}
---

Is this text mainly about someone's private {cls} matters ({what})? Return JSON only: {{"yes": true or false}}"""
WHAT = {c: QUESTIONS["kind"]["criteria"][c] for c in CLASSES}


def decider(text: str) -> str:
    body = json.dumps({"state": {"text": text[:MAX_CHARS]}, "questions": QUESTIONS}).encode()
    req = urllib.request.Request(f"{DECIDER_URL}/v1/systemone", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        got = json.loads(r.read())["answers"]["kind"]
    if isinstance(got, dict):   # {"choice"?, "probabilities": {...}} (proactive reads the probabilities too)
        probs = got.get("probabilities") or {}
        got = got.get("choice") or got.get("answer") or (max(probs, key=probs.get) if probs else "none")
    return str(got).strip().lower()


def judge(text: str, cls: str) -> bool:
    prompt = JUDGE.format(text=text[:MAX_CHARS], cls=cls, what=WHAT[cls])
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": 20, "temperature": 0,
                       "response_format": {"type": "json_object"},
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(JUDGE_URL + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())["choices"][0]["message"].get("content") or "{}"
    return bool(json.loads(out).get("yes"))


def score(items: Iterable[dict[str, Any]], classify: Callable[[str], str],
          second: Callable[[str, str], bool] | None) -> dict[str, Any]:
    """Counts only. items: {text, entity: bool, listed: set of word-list classes}."""
    c: Counter[str] = Counter()
    for it in items:
        c["items"] += 1
        try:
            got = classify(it["text"])
        except Exception:  # noqa: BLE001
            c["errors"] += 1
            continue
        got = got if got in CLASSES else "none"
        listed = set(it["listed"]) & set(CLASSES)
        if listed:
            c["listed"] += 1
            c["listed_agree" if got in listed else ("listed_other" if got != "none" else "listed_none")] += 1
            continue
        if it["entity"]:
            c["named_entity"] += 1
            continue
        c["unflagged"] += 1
        if got == "none":
            continue
        c[f"unflagged_{got}"] += 1
        if second is None:
            continue
        try:
            c[f"unflagged_{got}_confirmed" if second(it["text"], got) else f"unflagged_{got}_rejected"] += 1
        except Exception:  # noqa: BLE001
            c["judge_errors"] += 1
    out: dict[str, Any] = dict(sorted(c.items()))
    if c["listed"]:
        out["recall_vs_lists"] = round(c["listed_agree"] / c["listed"], 3)
    if c["unflagged"]:
        out["unflagged_sensitive_rate"] = round(sum(c[f"unflagged_{k}"] for k in CLASSES) / c["unflagged"], 3)
    judged = sum(c[f"unflagged_{k}_confirmed"] + c[f"unflagged_{k}_rejected"] for k in CLASSES)
    if judged:
        out["precision_est"] = round(sum(c[f"unflagged_{k}_confirmed"] for k in CLASSES) / judged, 3)
    return out


def kb_items(limit: int) -> list[dict[str, Any]]:
    """The newest KB items, read on titan: their text, whether the world model names something in them, and the
    word-list classes (fences.classes_in). System notes and eval items are left out."""
    import fences
    import kb_hygiene
    import kb_privacy
    import kb_query
    import world as W
    w = W.get()
    srcs = sorted(kb_hygiene.list_sources(), key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    out = []
    for s in srcs:
        if len(out) >= limit:
            break
        sid = s.get("source_id") or ""
        text, md = kb_query._chunks(sid)
        if not text or kb_privacy.system_meta(md) or md.get("eval_run"):
            continue
        body = f"{s.get('title') or ''}\n{text}"[:MAX_CHARS]
        out.append({"text": body, "entity": bool(w.find(body)), "listed": fences.classes_in(body)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--no-judge", action="store_true", help="skip the second model")
    a = ap.parse_args()
    print(json.dumps(score(kb_items(a.limit), decider, None if a.no_judge else judge)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
