#!/usr/bin/env python3
"""S3 (docs/surfaces.md §8): the apps render one ResolvedSurface with identical membership, groups and operations.

Each app's self-test reads titan's golden vectors (scripts/evalsets/surfaces/*.json, the merged files, verbatim) and
writes what its recipe would render, in this shape (docs/surfaces.md §8, "S3: what the apps emit"):

    {"s3": 1, "app": "mac" | "ios" | "tv", "build": "<app version>",
     "results": [{"vector": "cards.admin",
                  "groups": [{"id": "g-needs-a-decision", "role": "attention", "members": ["c-cap", "c-lock"]}, …],
                  "operations": [{"id": "op-done-c-cap", "exposure": "available"}, …]}, …]}

The items route's per-viewer answers (evalsets/surfaces/items/items.<view>.<viewer>.json, {status, body}) are vectors
too, named items.<view>.<viewer> and compared on their body; a refusal or the plain project list is listed as not
comparable (`expected`'s not_comparable) and never a vector.

A read-only view declares `"draws_operations": false` on that vector (and emits no operations): its groups, members and
order are still compared exactly, and titan's operations are reported as not drawn, counted, never failed. A group an app
deliberately doesn't draw is declared in `"omits_groups": ["g-…"]`: reported as not drawn, counted, never failed; an
omitted group that's emitted anyway fails, and so does any undeclared missing group. The operations of members that
live only inside omitted groups are not drawn either (counted, never failed; emitting one fails); a member that is also
in a drawn group keeps its operations strict. A verb an app doesn't offer on that view (the Files extension has no Ask)
is declared in `"omits_operations": ["ask"]`: titan's operations with that verb are reported as not drawn, counted, never
failed; emitting one fails; every other verb stays strict.

This compares each app's file with what titan expects from the same saved vectors (never a live route), and, given two
files, checks they cover the same vectors. The diff names ids and roles only, never a title or any text, so it can be
pasted into a PR body. Exit 0 when every emitted vector matches.

    python3 scripts/surfaces_s3.py expected [vector …]        # titan's lists, the same shape (for the apps' own checks)
    python3 scripts/surfaces_s3.py compare mac.json [ios.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

VECTORS = Path(__file__).parent / "evalsets" / "surfaces"
NOT_SURFACES = {"shipped_keys"}
VERSION = 1


ITEMS_DIR = "items"   # the items route's per-viewer answers ({status, body}), test_surfaces_items_viewers.py


def _items_files(root: Path) -> dict[str, Path]:
    d = root / ITEMS_DIR
    return {p.stem: p for p in sorted(d.glob("items.*.json"))} if d.is_dir() else {}


def _comparable(wrapped: dict[str, Any]) -> bool:
    """An items vector is a surface to compare when the route answered 200 with one: never the plain project list
    (not_sss) or a refusal (a 404, a screen's person page)."""
    return wrapped.get("status") == 200 and "not_sss" not in wrapped and isinstance(wrapped.get("body"), dict) \
        and "groups" in wrapped["body"]


def not_comparable(root: Path = VECTORS) -> list[str]:
    """The items vectors S3 leaves out (projects.*, the 404s, people.screen), by name."""
    return sorted(n for n, p in _items_files(root).items() if not _comparable(json.loads(p.read_text())))


def load(name: str, root: Path = VECTORS) -> dict[str, Any]:
    """A vector's surface by name: a top-level file, or an items vector unwrapped from its {status, body}."""
    p = root / f"{name}.json"
    if p.exists():
        return json.loads(p.read_text())
    items = _items_files(root)
    if name in items:
        w = json.loads(items[name].read_text())
        if _comparable(w):
            return w["body"]
    raise FileNotFoundError(name)


def vector_names(root: Path = VECTORS) -> list[str]:
    top = [p.stem for p in root.glob("*.json") if p.stem not in NOT_SURFACES]
    return sorted(top + [n for n, p in _items_files(root).items() if _comparable(json.loads(p.read_text()))])


def lists(surface: dict[str, Any]) -> dict[str, Any]:
    """What a recipe renders, as S3 compares it: groups in order (id, role, member ids in order), and operations as a
    set of (id, exposure). Nothing else: no titles, slots or text."""
    return {"groups": [{"id": g["id"], "role": g.get("role"), "members": [m["subject"]["id"] for m in g.get("members") or []]}
                       for g in surface.get("groups") or []],
            "operations": sorted(({"id": o["id"], "exposure": o.get("exposure")} for o in surface.get("operations") or []),
                                 key=lambda o: o["id"])}


def expected(names: list[str] | None = None, root: Path = VECTORS) -> dict[str, Any]:
    names = names or vector_names(root)
    return {"s3": VERSION, "app": "titan", "build": None,
            "results": [{"vector": n, **lists(load(n, root))} for n in names],
            "not_comparable": not_comparable(root)}


def hidden_ops(want: dict[str, Any], omitted: set[str], op_subjects: dict[str, str]) -> set[str]:
    """Operations that live only inside omitted groups: their subject is a member of an omitted group and of no drawn
    one. A group not drawn means nothing in it is drawn; a member that is also in a drawn group stays strict."""
    inside = {m for g in want["groups"] if g["id"] in omitted for m in g["members"]}
    drawn = {m for g in want["groups"] if g["id"] not in omitted for m in g["members"]}
    only = inside - drawn
    return {oid for oid, subj in op_subjects.items() if subj in only}


def verb_ops(omitted_verbs: set[str], op_verbs: dict[str, str]) -> set[str]:
    """Operations whose verb the app declared it doesn't offer on this view (omits_operations)."""
    return {oid for oid, verb in op_verbs.items() if verb in omitted_verbs}


def diff(want: dict[str, Any], got: dict[str, Any], op_subjects: dict[str, str] | None = None,
         op_verbs: dict[str, str] | None = None) -> list[str]:
    """One vector's differences, ids and roles only."""
    out: list[str] = []
    omitted = {str(x) for x in got.get("omits_groups") or []}
    for gid in sorted(omitted):
        if any(isinstance(g, dict) and g.get("id") == gid for g in got.get("groups") or []):
            out.append(f"group {gid}: declared omitted, but emitted")
    wg = {g["id"]: g for g in want["groups"] if g["id"] not in omitted}
    gg = {g.get("id"): g for g in got.get("groups") or [] if isinstance(g, dict) and g.get("id") not in omitted}
    for gid in wg:
        if gid not in gg:
            out.append(f"group {gid}: missing")
    for gid in gg:
        if gid not in wg:
            out.append(f"group {gid}: not in titan's surface")
    shared = [g for g in wg if g in gg]
    if [g for g in [x.get("id") for x in got.get("groups") or [] if isinstance(x, dict)] if g in wg] != shared:
        out.append("groups: in a different order")
    for gid in shared:
        w, g = wg[gid], gg[gid]
        if g.get("role") != w["role"]:
            out.append(f"group {gid}: role {g.get('role')} (titan: {w['role']})")
        gm = [str(x) for x in g.get("members") or []]
        missing = [m for m in w["members"] if m not in gm]
        extra = [m for m in gm if m not in w["members"]]
        if missing:
            out.append(f"group {gid}: missing members {', '.join(missing)}")
        if extra:
            out.append(f"group {gid}: extra members {', '.join(extra)}")
        if not missing and not extra and gm != w["members"]:
            out.append(f"group {gid}: members in a different order")
    if got.get("draws_operations") is False:
        # a read-only view (declared): groups and members are compared above; titan's operations aren't drawn, and
        # drawing any at all contradicts the declaration
        drawn = [o for o in got.get("operations") or [] if isinstance(o, dict)]
        if drawn:
            out.append(f"declared draws_operations false, but emitted {len(drawn)} operation(s)")
        return out
    hidden = hidden_ops(want, omitted, op_subjects or {}) if omitted else set()
    verbs = {str(x) for x in got.get("omits_operations") or []}
    by_verb = verb_ops(verbs, op_verbs or {}) - hidden if verbs else set()
    wo = {o["id"]: o["exposure"] for o in want["operations"] if o["id"] not in hidden | by_verb}
    go = {o.get("id"): o.get("exposure") for o in got.get("operations") or [] if isinstance(o, dict)}
    for oid in sorted(hidden & set(go), key=str):
        out.append(f"operation {oid}: in a declared omitted group, but emitted")
        go.pop(oid)
    for oid in sorted(by_verb & set(go), key=str):
        out.append(f"operation {oid}: verb {(op_verbs or {}).get(oid)} declared omitted, but emitted")
        go.pop(oid)
    for oid in sorted(set(wo) - set(go)):
        out.append(f"operation {oid}: missing")
    for oid in sorted(set(go) - set(wo), key=str):
        out.append(f"operation {oid}: not in titan's surface")
    for oid in sorted(set(wo) & set(go)):
        if go[oid] != wo[oid]:
            out.append(f"operation {oid}: exposure {go[oid]} (titan: {wo[oid]})")
    return out


def compare(files: list[dict[str, Any]], root: Path = VECTORS) -> tuple[bool, list[str]]:
    """→ (all equal, report lines). Each file against titan's expected lists, so two apps that both match titan render
    the same surface on every vector they share. Apps render different surfaces (the TV draws a few), so a vector one
    emits and another doesn't is reported, not a failure; `shared` says how many both compared."""
    known = set(vector_names(root))
    skip = set(not_comparable(root))
    lines: list[str] = []
    ok = True
    covered: list[set[str]] = []
    for f in files:
        app = str(f.get("app") or "?")
        if f.get("s3") != VERSION or not isinstance(f.get("results"), list):
            lines.append(f"{app}: not an S3 file (s3: {VERSION} and a results list expected)")
            ok = False
            covered.append(set())
            continue
        seen: set[str] = set()
        n_diff = 0
        for r in f["results"]:
            name = str((r or {}).get("vector") or "")
            if name not in known:
                lines.append(f"{app} {name}: not comparable (a refusal or the plain project list, not a surface)"
                             if name in skip else f"{app} {name or '?'}: no such vector in titan's evalsets")
                n_diff += 1
                ok = False
                continue
            seen.add(name)
            want = expected([name], root)["results"][0]
            raw = load(name, root)
            subjects = {o["id"]: (o.get("subject") or {}).get("id") for o in raw.get("operations") or []}
            op_verbs = {o["id"]: str(o.get("verb") or "") for o in raw.get("operations") or []}
            d = diff(want, r, subjects, op_verbs)
            lines += [f"{app} {name}: {x}" for x in d]
            if r.get("draws_operations") is False and not d:
                lines.append(f"{app} {name}: {len(want['operations'])} operation(s) not drawn (declared read-only)")
            for gid in sorted({str(x) for x in r.get("omits_groups") or []} & {g["id"] for g in want["groups"]}):
                if not d:
                    n = len(next(g for g in want["groups"] if g["id"] == gid)["members"])
                    lines.append(f"{app} {name}: group {gid} not drawn (declared), {n} member(s)")
            if not d and r.get("omits_groups"):
                h = hidden_ops(want, {str(x) for x in r["omits_groups"]}, subjects)
                if h:
                    lines.append(f"{app} {name}: {len(h)} operation(s) not drawn (declared, inside omitted groups)")
            verbs = sorted({str(x) for x in r.get("omits_operations") or []})
            if not d and verbs and r.get("draws_operations") is not False:
                inside = hidden_ops(want, {str(x) for x in r.get("omits_groups") or []}, subjects)
                n = len(verb_ops(set(verbs), op_verbs) - inside)
                lines.append(f"{app} {name}: {n} operation(s) not drawn (declared: {', '.join(verbs)})")
            n_diff += len(d)
            ok = ok and not d
        lines.append(f"{app}: {len(seen)} vector(s) compared, {n_diff} difference(s); not emitted: {len(known - seen)}")
        covered.append(seen)
    if len(covered) > 1:
        apps = [str(f.get("app") or "?") for f in files]
        lines.append(f"shared: {len(set.intersection(*covered))} vector(s) compared by every app")
        for a, c in zip(apps, covered):
            others = set().union(*(x for x in covered if x is not c)) - c
            if others:
                lines.append(f"{a}: doesn't emit {', '.join(sorted(others))} (another app does; not a failure)")
    return ok, lines


def main(argv: list[str]) -> int:
    if argv[:1] == ["expected"]:
        print(json.dumps(expected(argv[1:] or None), indent=1))
        return 0
    if argv[:1] == ["compare"] and argv[1:]:
        ok, lines = compare([json.loads(Path(p).read_text()) for p in argv[1:]])
        print("\n".join(lines))
        print("S3: equal" if ok else "S3: NOT equal")
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
