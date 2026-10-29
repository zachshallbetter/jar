#!/usr/bin/env python3
"""Leave-one-out precision for a project page's "linked to this project" (#1180), on the house: for each adult, each
item filed under a project they can see is taken out in turn and the File-it suggester (#1175, the same one the page
uses) is asked where it belongs. Precision: of the suggestions made, how many named the item's real project;
coverage: how many filed items got a suggestion at all. By link state. Counts only (no ids, no titles).

  python3 scripts/project_linked_eval.py [--person ID]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any


def evaluate(pid: str, role: str, items_: dict, edges: list, filed: dict[str, str], project_names: dict[str, str],
             visible) -> dict[str, Any]:
    import file_suggest
    vis = {i: x for i, x in items_.items() if visible(x)}
    es = [e for e in edges if e.a in vis and e.b in vis]
    held = [(i, p) for i, p in filed.items() if i in vis and p in project_names]
    c: Counter = Counter()
    for i, real in held:
        projs = [{"id": p_, "name": n, "members": [j for j, pr in filed.items() if pr == p_ and j in vis and j != i]}
                 for p_, n in project_names.items()]
        got = file_suggest.suggest(vis, es, lambda _x: True, projs, role=role).get(i)
        if not got:
            c["none"] += 1
            continue
        c[f"{got['state']}:{'right' if got['project'] == real else 'wrong'}"] += 1
    made = sum(v for k, v in c.items() if k != "none")
    right = sum(v for k, v in c.items() if k.endswith(":right"))
    firm = {k: v for k, v in c.items() if k.split(":")[0] in ("verified", "accepted")}
    firm_made, firm_right = sum(firm.values()), sum(v for k, v in firm.items() if k.endswith(":right"))
    return {"filed": len(held), "suggested": made, "right": right,
            "precision": round(right / made, 2) if made else None,
            "precision_firm": round(firm_right / firm_made, 2) if firm_made else None,
            "coverage": round(made / len(held), 2) if held else None, "by_state": dict(sorted(c.items()))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--person")
    a = ap.parse_args(argv)
    import file_suggest
    import hooks
    import household_identity as hh
    import linker
    import projects
    import threadview
    con = hooks.connect()
    try:
        items_, edges = linker.scored(con)
    finally:
        con.close()
    filed = {i: p for i, p in projects.filings().items() if i in items_}
    out = {}
    for p in hh.get_store().people():
        if p.get("role") not in ("admin", "adult") or (a.person and p["id"] != a.person):
            continue
        out[f"{p['role']}#{len(out)}"] = evaluate(p["id"], p["role"], items_, edges, filed,
                                                  file_suggest.visible_projects(p["id"], p["role"]),
                                                  threadview._visible_fn(p["id"], p["role"]))
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
