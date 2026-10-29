#!/usr/bin/env python3
"""Seed the world model from what the household already said (10-02 audit: the household bubble held 2 claims;
the other members none). Each claim is a fact Zach stated in chat or titan's own records hold, written
through world.set_fact as the person who said it, with where they said it (`source`), so every rule holds: whose
facts these are, a kid's health to the kid and their parents, money to the adults. Nothing guessed: a fact that
isn't stated as such (one visit somewhere, another adult's own details) is a question for Zach instead (#389).

  python3 scripts/world_seed.py            dry run on a copy of the world (counts and outcomes, never values)
  The facts themselves are in WORLD_SEED_FILE on titan (#869), never in this repo.
  python3 scripts/world_seed.py --apply    (a fleet write: only with the owner session's go)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


# The household's own facts are household data, so they live on titan, not in this repo (#869): a JSON file at
# WORLD_SEED_FILE (default /ai/datasets/connectors/world_seed.json, 0600). The repo carries only a fictional fixture
# (fixtures/world_seed.example.json) that the tests load explicitly.
#   {"seeds": [[entity, type, attr, value, qual|null, {"ref", "label", "observed_at"}], ...],
#    "chore_note": {"ref", "label", "observed_at"}, "chores": [[name, when, qual|null], ...], "questions": [str, ...]}
LIVE_FILE = Path("/ai/datasets/connectors/world_seed.json")
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "world_seed.example.json"


def _path() -> Path:
    env = os.environ.get("WORLD_SEED_FILE")
    if env:
        return Path(env)
    return LIVE_FILE if LIVE_FILE.is_file() else FIXTURE


def load(path: Path | None = None) -> dict[str, Any]:
    d = json.loads(Path(path or _path()).read_text())
    return {"seeds": [tuple(x) for x in d.get("seeds", [])], "chore_note": dict(d.get("chore_note") or {}),
            "chores": [tuple(x) for x in d.get("chores", [])], "questions": list(d.get("questions", []))}


_D = load()
SEEDS: list[tuple[str, str, str, str, dict[str, Any] | None, dict[str, str]]] = _D["seeds"]
CHORE_NOTE: dict[str, str] = _D["chore_note"]
CHORES: list[tuple[str, str, dict[str, Any] | None]] = _D["chores"]
QUESTIONS: list[str] = _D["questions"]


def seed(w, people: list[dict[str, Any]]) -> list[dict[str, str]]:
    import world as W
    names = {str(p["id"]): str(p.get("name") or "") for p in people}
    out = []
    for entity, etype, attr, value, qual, source in SEEDS:
        done, said, code = W.set_fact(w, entity, attr, value, "zach", "admin", etype=etype, people=people, names=names,
                                      qual=qual, source=source)
        out.append({"entity": entity, "attr": attr, "outcome": code})
    for name, when, qual in CHORES:
        done, said, code = W.set_fact(w, name, "schedule", when, "zach", "admin", etype="activity", people=people,
                                      names=names, qual=qual, source=CHORE_NOTE, household=True)
        out.append({"entity": name, "attr": "schedule", "outcome": code})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    import household_identity as hh
    import world as W
    people = hh.get_store().people()
    if a.apply:
        print(json.dumps({"applied": True, "claims": seed(W.get(), people)}))
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="world-seed-", dir="/ai/tmp" if Path("/ai/tmp").is_dir() else None))
    try:
        shutil.copytree(W.WORLD_DIR, tmp / "world")
        os.environ["WORLD_DIR"] = str(tmp / "world")
        print(json.dumps({"applied": False, "claims": seed(W.Bubbles(tmp / "world"), people), "questions": len(QUESTIONS)}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
