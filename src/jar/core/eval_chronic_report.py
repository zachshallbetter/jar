#!/usr/bin/env python3
"""Chronic eval misses as issues (#1517): a case failing 3+ nights running gets one `eval-chronic` issue.

Reads the nightly history (HOUSEHOLD_EVAL_HISTORY, default /ai/logs/household-eval.jsonl). Per chronic case (keyed on
case id) it builds {title 'Eval chronic: <case>', body, labels}: the nights failing, the first and last night, the
owner (scripts/eval_owners.json: case-id prefix -> owner, default coordination) and the last 3 failure reasons. Case
ids and check text only, never message content. Prints JSON by default. `--gh` creates or updates one issue per case
(found by exact title) and closes it after 3 passing nights.

  python3 scripts/eval_chronic_report.py [--gh] [--nights 3]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
NIGHTS = 3
LABEL = "eval-chronic"
TITLE = "Eval chronic: "
REASONS = 3


def history_path() -> Path:
    return Path(os.environ.get("HOUSEHOLD_EVAL_HISTORY") or "/ai/logs/household-eval.jsonl")


def load_history(path: Path | None = None) -> list[dict]:
    try:
        lines = (path or history_path()).read_text().splitlines()
    except OSError:
        return []
    rows = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue   # a torn line costs that night, not the report
        if isinstance(r, dict):
            rows.append(r)
    return rows


def misses(rec: dict) -> set[str]:
    return set(rec.get("real", rec.get("failing")) or [])


def owner_of(case: str, owners: dict | None = None) -> str:
    if owners is None:
        try:
            owners = json.loads(Path(os.environ.get("EVAL_OWNERS") or HERE / "eval_owners.json").read_text())
        except (OSError, ValueError):
            owners = {}
    best = ""
    for pre in (owners.get("prefixes") or {}):
        if case.startswith(pre) and len(pre) > len(best):
            best = pre
    return (owners.get("prefixes") or {}).get(best) or owners.get("default") or "coordination"


def streak(history: list[dict], case: str) -> int:
    n = 0
    for rec in reversed(history):
        if case not in misses(rec):
            break
        n += 1
    return n


def chronic_cases(history: list[dict], nights: int = NIGHTS) -> list[str]:
    seen = set().union(*(misses(r) for r in history[-1:])) if history else set()
    return sorted(c for c in seen if streak(history, c) >= nights)


def build_issue(history: list[dict], case: str, owners: dict | None = None) -> dict:
    n = streak(history, case)
    run = history[-n:]
    reasons = []
    for rec in reversed(run):
        txt = (rec.get("reasons") or {}).get(case)
        if txt:
            reasons.append(f"{rec.get('ts', '?')}: {str(txt)[:200]}")
        if len(reasons) == REASONS:
            break
    body = "\n".join([
        f"Case `{case}` has missed the nightly household eval {n} nights running.",
        "",
        f"- owner: {owner_of(case, owners)}",
        f"- nights failing: {n}",
        f"- first night: {run[0].get('ts', '?')}",
        f"- last night: {run[-1].get('ts', '?')}",
        "- last failure reasons (check text only):",
        *([f"  - {r}" for r in reasons] or ["  - none recorded"]),
        "",
        "Opened by scripts/eval_chronic_report.py; it closes itself after 3 passing nights. "
        "To park a case on purpose, add it to scripts/eval_quarantine.json with an owner, an expiry and a reason.",
    ])
    return {"title": TITLE + case, "body": body, "labels": [LABEL]}


def report(history: list[dict], nights: int = NIGHTS, owners: dict | None = None) -> list[dict]:
    return [build_issue(history, c, owners) for c in chronic_cases(history, nights)]


def recovered(history: list[dict], case: str, nights: int = NIGHTS) -> bool:
    """Passing `nights` nights running (needs that many records)."""
    return len(history) >= nights and all(case not in misses(r) for r in history[-nights:])


def _gh(args: list[str]) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"gh {args[0]} {args[1]} failed (exit {r.returncode})")
    return r.stdout


def sync(history: list[dict], gh: Callable[[list[str]], str] = _gh, nights: int = NIGHTS,
         owners: dict | None = None) -> list[str]:
    """Create/update one issue per chronic case; close open ones that passed `nights` nights. Returns actions."""
    acts: list[str] = []
    open_ = json.loads(gh(["issue", "list", "--label", LABEL, "--state", "open", "--limit", "200",
                           "--json", "number,title"]) or "[]")
    by_title = {i["title"]: i["number"] for i in open_}
    for issue in report(history, nights, owners):
        num = by_title.get(issue["title"])
        if num:
            gh(["issue", "edit", str(num), "--body", issue["body"]])
            acts.append(f"updated #{num} {issue['title']}")
        else:
            gh(["issue", "create", "--title", issue["title"], "--body", issue["body"], "--label", LABEL])
            acts.append(f"created {issue['title']}")
    for title, num in by_title.items():
        if title.startswith(TITLE) and recovered(history, title[len(TITLE):], nights):
            gh(["issue", "close", str(num), "--comment", f"Passing {nights} nights running; closing."])
            acts.append(f"closed #{num} {title}")
    return acts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gh", action="store_true")
    ap.add_argument("--nights", type=int, default=NIGHTS)
    a = ap.parse_args(argv)
    hist = load_history()
    if a.gh:
        for line in sync(hist, nights=a.nights):
            print(line)
    else:
        print(json.dumps(report(hist, a.nights), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
