#!/usr/bin/env python3
# gate: hermetic   (the golden household in memory; no store, no model, no network)
"""The 5W eval (#1399 P3; spec §10.1): situation() over the golden household (scripts/graph_golden/household.json),
asked the 41 questions in scripts/graph_golden/5w.json as each case's viewer, through graph_sync's real readers
(P2, #1425) over the fixture loaded into temp stores (graph_golden_load).

Per case, each expectation is one check:
  slot "is"        exactly these values (ids, or times to the minute)   slot "has"  at least these (substring)
  slot "unknown"   no supported answer                                  "contested" both chains listed
  via              the why-chain cites these items                      rung        the weakest rung stated
  lacks_link       no chain joins these two                             proposals   no second card for one plan
  lacks            these ids or words appear NOWHERE in the bundle: a privacy check, and a HARD failure

Exit 1 on any `lacks` failure (a leak), or when the checks passed fall below FLOOR (a regression). The table prints
per story. Run: python3 scripts/test_situation.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import graph_golden_load as GL  # noqa: E402  (first: points every store at a temp dir and loads the fixture)
import graph_sync  # noqa: E402
from situation import situation  # noqa: E402


class GoldenGraph:
    """graph_sync's real readers (P2, #1425) over the golden household's graph.db: what situation() reads live."""

    def __init__(self) -> None:
        self.db = GL.build()
        self.zone, self.now = GL.H["zone"], GL.H["now"]
        self._today = date.fromisoformat(self.now[:10])

    def visible_nodes(self, viewer, role, path=None):
        return graph_sync.visible_nodes(viewer, role, self.db)

    def visible_edges(self, viewer, role, path=None, **kw):
        return graph_sync.visible_edges(viewer, role, self.db, **kw)

    def same_as(self, viewer, role, node_id=None, path=None):
        return graph_sync.same_as(viewer, role, node_id, self.db)

    def obligation_state(self, viewer, role, node_id, path=None):
        return graph_sync.obligation_state(viewer, role, node_id, records=GL.bill_records, today=self._today,
                                           path=self.db)

FLOOR = 86    # 10-14: 86/104 checks (inf-1 whole, #1650 thread:device + reply:device); before 85/104 (#1396), 10-10 84/102
# Cases the graph can't answer yet, by KB's decisions (10-10): reported apart, never "fixed" by faking the data.
DATA_BLOCKED = {
    "plan-1": "the plan's texts aren't tied to the proposal (the device thread isn't on text hooks yet)",
    "plan-2": "the plan's texts aren't tied to the proposal (the device thread isn't on text hooks yet)",
    "plan-3": "a text's body stays on its owner's device (#446): answer-time read, not a graph attr",
    "plan-4": "the evidence is a text's body (#446) and the texts aren't tied to the proposal",
    "meet-2": "a meeting's dated plans (the cleanup's day and place) aren't structured rows yet",
    "meet-4": "a meeting's dated plans (the next meeting's day) aren't structured rows yet",
    "priv-2": "a text's body stays on its owner's device (#446): answer-time read, not a graph attr",
    "rel-2": "a note's words are an answer-time kb_retrieve read (the KB's own fences), not a graph attr",
    "priv-5": "a note's words are an answer-time kb_retrieve read (the KB's own fences), not a graph attr",
}
ROLE = {"admin": "admin", "adult": "adult", "kid": "kid", "screen": "household"}


def _norm(v: str) -> str:
    return str(v).strip().lower()


def check_slot(got, exp) -> bool:
    if exp == "unknown":
        return got == "unknown"
    if exp == "contested":
        return isinstance(got, dict) and bool(got.get("contested"))
    if not isinstance(got, dict) or "is" not in got:
        return False
    vals = [_norm(v) for v in got["is"]]
    if "is" in exp:
        want = [_norm(v) for v in exp["is"]]
        return sorted(set(vals)) == sorted(set(want))
    if "has" in exp:
        return all(any(_norm(w) in v for v in vals) for w in exp["has"])
    return False


def run_case(g, case) -> tuple[list[tuple[str, bool]], dict]:
    viewer, role = case["as"]
    bundle = situation(case["q"], viewer, ROLE.get(role, role), g, zone=g.zone, now=g.now)
    exp, checks = case["expect"], []
    for slot in ("who", "what", "where", "when"):
        if slot in exp:
            checks.append((slot, check_slot(bundle.get(slot), exp[slot])))
    if "via" in exp:
        checks.append(("via", all(v in (bundle.get("via") or []) for v in exp["via"])))
    if "rung" in exp:
        checks.append(("rung", bundle.get("rung") == exp["rung"]))
    if "lacks_link" in exp:
        lines = bundle.get("why") or []
        checks.append(("lacks_link", not any({ln["a"], ln["b"]} == set(pair) for pair in exp["lacks_link"]
                                             for ln in lines if ln.get("type") == "related")))
    if "proposals" in exp:
        got = bundle.get("proposals") or []
        p = exp["proposals"]
        checks.append(("proposals", len(got) == p["count"] and sorted(got) == sorted(p["is"]) and not bundle.get("raised")))
    if "lacks" in exp:
        blob = json.dumps(bundle).lower()
        checks.append(("lacks", not any(_norm(x) in blob for x in exp["lacks"])))
    return checks, bundle


def visible_ids(g, viewer: str, role: str) -> set[str]:
    """Every native id this viewer may see: their nodes, and the source refs of their visible edges."""
    out = set()
    for nid in g.visible_nodes(viewer, role):
        kind, _, rest = nid.partition(":")
        out |= {nid, rest}
        if kind == "event":
            out.add(rest)   # a calendar event is named by its item id
    for e in g.visible_edges(viewer, role):
        out |= {p["ref"] for p in e["prov"]}
    return out


def named_ids(bundle: dict) -> set[str]:
    """The ids a bundle names: slot values that look like ids, its via, and both ends of every chain line."""
    out = set(bundle.get("via") or [])
    for slot in ("who", "where"):
        v = bundle.get(slot)
        if isinstance(v, dict):
            out |= {x for x in v.get("is", []) if ":" in x or x.startswith(("p_", "pl-"))}
    for ln in bundle.get("why") or []:
        out |= {ln["a"], ln["b"]}
    return out


# Probes beyond the 41 (no expected answer, only what must never show): a name the viewer can't see resolves to nothing.
PROBES = [("p_ava", "kid", "What did Jordan text Robin about the surprise party?", ["surprise", "dev-text-surprise", "clubhouse"]),
          ("screen", "household", "Who is Morgan Reyes?", ["coach", "nurse", "morgan-reyes"]),
          ("p_jordan", "adult", "What's the Harbor bill?", ["62.5", "cx-mail-bill-3", "harbor mobile:2026"]),
          ("p_leo", "kid", "Where's Ava right now?", ["pe-ava-1", "pl-cedar"]),
          ("p_robin", "adult", "What did the club board decide?", ["$40", "cleanup", "cx-meeting-club-1"]),
          ("p_alex", "admin", "When is Robin's yoga?", ["yoga", "willow", "cx-calendar-yoga"])]


def holdout() -> int:
    """The held-out set (graph_golden/5w_holdout.json, written by someone who hasn't seen situation.py's rules): its
    score is REPORTED, never gated, and never printed case by case, so nobody tunes on it. Counts only. A privacy leak
    in it is a hard failure all the same: privacy holds on any phrasing. → its leak count."""
    path = HERE / "graph_golden" / "5w_holdout.json"
    if not path.exists():
        print("held-out: none yet")
        return 0
    g = GRAPH
    cases = json.loads(path.read_text())["cases"]
    ok = checks_ok = checks_n = leaks = 0
    by_check: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for c in cases:
        try:
            checks, _ = run_case(g, c)
        except Exception:  # noqa: BLE001 - a crash on an unseen question is a miss, counted
            checks = [("error", False)]
        ok += all(r for _, r in checks)
        checks_ok += sum(r for _, r in checks)
        checks_n += len(checks)
        for name, r in checks:
            by_check[name][0] += r
            by_check[name][1] += 1
            leaks += name == "lacks" and not r
    print(f"held-out (reported, not gated): {ok}/{len(cases)} cases, {checks_ok}/{checks_n} checks, privacy leaks {leaks}; "
          + "  ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(by_check.items())))
    return leaks


GRAPH = GoldenGraph()


def main() -> int:
    g = GRAPH
    cases = json.loads((HERE / "graph_golden" / "5w.json").read_text())["cases"]
    by_story: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])   # cases ok, cases, checks ok, checks
    by_slot: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    leaks, passed_checks, total_checks = [], 0, 0
    passed_ids: set[str] = set()
    for c in cases:
        checks, bundle = run_case(g, c)
        ok = all(r for _, r in checks)
        if ok:
            passed_ids.add(c["id"])
        st = by_story[c["story"]]
        st[0] += ok
        st[1] += 1
        st[2] += sum(r for _, r in checks)
        st[3] += len(checks)
        passed_checks += sum(r for _, r in checks)
        total_checks += len(checks)
        for name, r in checks:
            by_slot[name][0] += r
            by_slot[name][1] += 1
            if name == "lacks" and not r:
                leaks.append(c["id"])
        bad = [n for n, r in checks if not r]
        tag = "ok" if ok else ("data" if c["id"] in DATA_BLOCKED else "miss")
        print(f"  [{tag}] {c['id']:<15} {c['q'][:52]:<52}" + (f"  missed: {', '.join(bad)}" if bad else "")
              + (f"  ({DATA_BLOCKED[c['id']]})" if not ok and c["id"] in DATA_BLOCKED else ""))
    print("\nper story                cases      checks")
    for story, (co, cn, ko, kn) in by_story.items():
        print(f"  {story:<22} {co:>3}/{cn:<3}    {ko:>3}/{kn:<3}")
    print("per check   " + "  ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(by_slot.items())))
    # property: no bundle, for any case, names an id its viewer can't see
    outside = []
    for c in cases:
        viewer, role = c["as"]
        r = ROLE.get(role, role)
        bundle = situation(c["q"], viewer, r, g, zone=g.zone, now=g.now)
        extra = named_ids(bundle) - visible_ids(g, viewer, r)
        if extra:
            outside.append(f"{c['id']}: {sorted(extra)}")
    print(f"\nproperty: every id a bundle names is visible to its viewer: {'ok' if not outside else 'FAIL ' + '; '.join(outside)}")
    probe_leaks = []
    for viewer, role, q, never in PROBES:
        blob = json.dumps(situation(q, viewer, role, g, zone=g.zone, now=g.now)).lower()
        hit = [x for x in never if x.lower() in blob]
        if hit:
            probe_leaks.append(f"{viewer} {q!r}: {hit}")
    print(f"probes (a name the viewer can't see resolves to nothing): {len(PROBES) - len(probe_leaks)}/{len(PROBES)}"
          + (" LEAK " + "; ".join(probe_leaks) if probe_leaks else ""))
    leaks += outside + probe_leaks
    cases_ok = sum(v[0] for v in by_story.values())
    blocked = [c["id"] for c in cases if c["id"] in DATA_BLOCKED]
    print(f"blocked by data the graph doesn't hold yet: {len(blocked)} ({', '.join(blocked)}); "
          f"of the rest: {sum(1 for c in cases if c['id'] not in DATA_BLOCKED and c['id'] in passed_ids)}"
          f"/{len(cases) - len(blocked)}")
    print(f"\n{cases_ok}/{len(cases)} cases, {passed_checks}/{total_checks} checks; privacy leaks: {len(leaks)}"
          + (f" ({', '.join(leaks)})" if leaks else ""))
    held_leaks = holdout()
    failed = bool(leaks) or bool(held_leaks) or passed_checks < FLOOR
    print(f"{passed_checks} passed, {int(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
