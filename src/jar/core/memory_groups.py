#!/usr/bin/env python3
"""A group for every remembered fact (titan-node#1073; Settings › Assistant › All N things).

  person:<id>   the fact is about one household member other than its owner (the roster, matched by first name or
                alias the way chat_links matches people: chat_links.members_finder, not a second matcher)
  routine | preference | place | work | health | other
                otherwise, picked by titan's local model (AGENT_ENDPOINT, :8010; never a hosted model: these are the
                most personal facts titan keeps), a batch at a time, the answer's shape fixed by typed_local's schema

`health` is private to its person: recall leaves it out on a shared screen or the TV (agent_memory.recall,
include_health), and the groups list labels it so.

  assign(mem, person_id, people, classify=…)   group every fact of theirs that has none → counts by group
  python3 scripts/memory_groups.py backfill [--apply]          every person, once (counts only; dry run by default)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

KINDS = ("routine", "preference", "relationship", "place", "decision", "work", "health", "other")
LABELS = {"routine": "Routines", "preference": "Preferences", "relationship": "People in your life", "place": "Places",
          "decision": "Decisions", "work": "Work", "health": "Health (only in your own chats)", "other": "Other"}
LOCAL_URL = os.environ.get("AGENT_ENDPOINT") or "http://127.0.0.1:8010"
BATCH = 12


def label(group: str, people: list[dict[str, Any]]) -> str:
    if group.startswith("person:"):
        pid = group.split(":", 1)[1]
        name = next((str(p.get("name") or "") for p in people if str(p.get("id")) == pid), "")
        return name.split(" ")[0] or "Someone"
    return LABELS.get(group, "Other")


def person_group(text: str, owner: str, people: list[dict[str, Any]]) -> str | None:
    """person:<id> when the fact names exactly one household member other than its owner."""
    import chat_links
    found = chat_links.members_finder([p for p in people if str(p.get("id")) != str(owner)])(text)
    return f"person:{next(iter(found))}" if len(found) == 1 else None


def classify_local(texts: list[str], url: str = LOCAL_URL, post: Any = None) -> list[str]:
    """One kind per fact, from the local model; [] when it can't answer (they wait for the next run). Local only."""
    if (urllib.parse.urlparse(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("facts are only ever grouped by the local model")
    import typed_local
    lines = "\n".join(f"{i + 1}. {t[:200]}" for i, t in enumerate(texts))
    prompt = ("Sort each remembered fact about a person into one group: routine (a regular schedule or habit), "
              "preference (likes, dislikes, how they want things), relationship (who someone outside the household is to "
              "them: a friend, a therapist, a coworker), place (somewhere that matters to them), decision (a standing "
              "choice they made), work, "
              "health (medical, allergies, medications, care providers), or other.\n\n" + lines +
              f'\n\nAnswer {{"groups": [...]}} with exactly {len(texts)} groups, in order.')
    schema = typed_local.obj({"groups": {"type": "array", "items": typed_local.enum(KINDS),
                                        "minItems": len(texts), "maxItems": len(texts)}}, ["groups"])
    try:
        got, _ = typed_local.ask(prompt, schema, url, max_tokens=20 + 8 * len(texts), timeout=120,
                                 ensure=lambda o: None if len(o.get("groups") or []) == len(texts) else "one per fact",
                                 post=post)
    except Exception:  # noqa: BLE001
        return []
    return [g if g in KINDS else "other" for g in (got or {}).get("groups", [])] if got else []


def assign(mem: Any, person_id: str, people: list[dict[str, Any]],
           classify: Callable[[list[str]], list[str]] | None = None, apply: bool = True) -> dict[str, int]:
    """Group every fact of the person's that has none. → counts by group (apply False: only counted)."""
    classify = classify or classify_local
    rows = mem._db.execute("SELECT id, text FROM facts WHERE person_id=? AND grp IS NULL", (person_id,)).fetchall()
    out: dict[str, int] = {}
    pending: list[tuple[str, str]] = []
    for r in rows:
        g = person_group(r["text"], person_id, people)
        if g:
            out["person"] = out.get("person", 0) + 1
            if apply:
                mem.set_group(r["id"], g, person_id=person_id)
        else:
            pending.append((r["id"], r["text"]))
    for i in range(0, len(pending), BATCH):
        part = pending[i:i + BATCH]
        kinds = classify([t for _, t in part])
        if len(kinds) != len(part):
            out["left for later"] = out.get("left for later", 0) + len(part)
            continue
        for (fid, _), g in zip(part, kinds):
            out[g] = out.get(g, 0) + 1
            if apply:
                mem.set_group(fid, g, person_id=person_id)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Group remembered facts (#1073); counts only")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    import agent_memory
    import household_identity as hh
    mem = agent_memory.AgentMemory()
    people = hh.get_store().people()
    owners = [r[0] for r in mem._db.execute("SELECT DISTINCT person_id FROM facts WHERE grp IS NULL")]
    print(json.dumps({"people": len(owners), "groups": {f"person {i + 1}": assign(mem, o, people, apply=a.apply)
                                                        for i, o in enumerate(owners)}, "applied": a.apply}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
