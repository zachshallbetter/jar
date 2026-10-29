#!/usr/bin/env python3
# gate: hermetic
"""Every surface titan emits still decodes in the apps already shipped (#1060; the merged-payload rule in reverse).
`shipped_parse` is a line-for-line port of jellybean v0.13.60's `ItemsSurface.parse` (mac/Sources/Jellybean/
ItemsSurface.swift, shared by the Mac, iPhone and TV): what those clients read, and what they silently drop. Each
resolver's real output (items now/time/unsorted/project from fictional readers; Settings, cards and threads from their
golden vectors) must keep every group, member and operation through it, keep `label.key` (the shipped apps draw headings
from it), and keep each resolver's keys exactly as frozen in scripts/evalsets/surfaces/shipped_keys.json. Removing or
renaming a key needs a release that no longer reads it, and its own ticket. `--write` refreshes the frozen keys (review).
Run: python3 scripts/test_surfaces_shipped_shape.py [--write]"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TMP = Path(tempfile.mkdtemp(prefix="titan-shipped-shape-"))
for k in ("SURFACES_DB", "HOUSEHOLD_DB", "PROACTIVE_DB", "SITTER_DB", "TODOS_DB", "SCHEDULED_DB", "WORLD_DIR"):
    os.environ[k] = str(_TMP / k.lower())
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
VECTORS = HERE / "evalsets" / "surfaces"
FROZEN = VECTORS / "shipped_keys.json"
WRITE = "--write" in sys.argv
import items as I  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


def _str(v: Any) -> str | None:
    """ItemsSurface.str: a string (or a number's text), trimmed; nil when empty."""
    s = v if isinstance(v, str) else str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return (s.strip() or None) if s is not None else None


def _dict(v: Any) -> dict[str, Any] | None:
    return v if isinstance(v, dict) else None


def shipped_parse(o: Any) -> dict[str, Any] | None:
    """v0.13.60 ItemsSurface.parse: nil without a `groups` list; a group without an id, a member without subject.id
    and an operation without an id are dropped; a group's heading comes from label.key."""
    if not isinstance(o, dict) or not isinstance(o.get("groups"), list):
        return None
    groups = []
    for g in o["groups"]:
        if not isinstance(g, dict) or _str(g.get("id")) is None:
            continue
        members = []
        for m in g.get("members") if isinstance(g.get("members"), list) else []:
            subj = _dict(m.get("subject")) if isinstance(m, dict) else None
            if subj is None or _str(subj.get("id")) is None:
                continue
            slots = _dict(m.get("slots")) or {}
            members.append({"id": _str(subj["id"]), "kind": _str(subj.get("kind")) or "",
                            "title": _str(slots.get("title")) or "",
                            "actions": [a for a in (_str(x) for x in slots.get("actions") or []) if a]})
        groups.append({"id": _str(g["id"]), "role": _str(g.get("role")) or "supporting",
                       "labelKey": _str((_dict(g.get("label")) or {}).get("key")) or "", "members": members})
    ops = {}
    for op in o.get("operations") if isinstance(o.get("operations"), list) else []:
        if isinstance(op, dict) and _str(op.get("id")):
            route = _dict(op.get("route")) or {}
            ops[_str(op["id"])] = {"verb": _str(op.get("verb")) or "", "method": _str(route.get("method")),
                                   "path": _str(route.get("path"))}
    return {"groups": groups, "operations": ops}


# ── every resolver's real output ──────────────────────────────────────────────────────────────────────────────────
NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc).timestamp()
late = I.from_scheduled({"id": "s1", "owner": "alex", "audience": "private", "what": "Return the library books",
                         "run_at": NOW - 3600, "state": "pending", "updated_at": 1.0})
waiting = I.from_task({"id": "t2", "goal": "Wait on the quote", "status": "running", "owner": "alex", "updated_at": "1",
                       "steps": [{"title": "a", "state": "blocked"}]})
moving = I.from_task({"id": "t1", "goal": "Plan the trip", "status": "running", "owner": "alex", "updated_at": "1",
                      "steps": [{"title": "a", "state": "done"}, {"title": "b", "state": "current"}]})
base = {"todos": lambda: [I.from_todo({"id": "1", "title": "Buy stamps", "for": "alex", "created_at": 1.0, "done_at": None})],
        "scheduled": lambda: [late], "tasks": lambda: [waiting, moving],
        "chats": lambda: [I.from_chat({"id": "c1", "title": "Garden ideas", "last_at": "2026-10-06T14:00:00Z"}, "alex", None),
                          I.from_chat({"id": "c2", "title": "Kitchen paint", "last_at": "2026-10-05T14:00:00Z"}, "alex",
                                      {"project": "pr-1"})]}
SURFACES: dict[str, Any] = {
    "items.now": I.resolve("now", "alex", "adult", base, now=NOW),
    "items.time": I.resolve("time", "alex", "adult", base, now=NOW, start="2026-10-06", end="2026-10-12"),
    "items.unsorted": I.resolve("unsorted", "alex", "adult", base, now=NOW),
    "items.project": I.resolve("project:pr-1", "alex", "adult", base, now=NOW, project_ok=lambda x: x == "pr-1"),
}
for p in sorted(VECTORS.glob("*.json")):
    if p.name != FROZEN.name:
        SURFACES[p.stem] = json.loads(p.read_text())

print("── each surface through v0.13.60's parser ──")
keys: dict[str, list[str]] = {}
for name, s in SURFACES.items():
    got = shipped_parse(s)
    check(f"{name}: decodes", got is not None, True)
    if got is None:
        continue
    check(f"{name}: no group dropped", [g["id"] for g in got["groups"]], [g.get("id") for g in s["groups"]])
    check(f"{name}: no member dropped", sum(len(g["members"]) for g in got["groups"]),
          sum(len(g.get("members") or []) for g in s["groups"]))
    check(f"{name}: no operation dropped", sorted(got["operations"]), sorted(o.get("id") for o in s.get("operations") or []))
    check(f"{name}: every group keeps label.key (the shipped heading)", [g["id"] for g in got["groups"] if not g["labelKey"]], [])
    check(f"{name}: every group also has English text beside it (§10)",
          [g["id"] for g in s["groups"] if not _str((_dict(g.get("label")) or {}).get("text"))], [])
    check(f"{name}: member actions resolve to operations",
          sorted({a for g in got["groups"] for m in g["members"] for a in m["actions"] if a not in got["operations"]}), [])
    check(f"{name}: every routed operation is titan's own /v1/ path",
          [i for i, o in got["operations"].items() if o["path"] and not o["path"].startswith("/v1/")], [])
    keys[name] = sorted({g["labelKey"] for g in got["groups"]})

print("── label keys stay as shipped ──")
if WRITE:
    FROZEN.write_text(json.dumps(keys, indent=1, sort_keys=True) + "\n")
frozen = json.loads(FROZEN.read_text()) if FROZEN.exists() else {}
for name, ks in frozen.items():
    check(f"{name}: no frozen key removed or renamed", sorted(set(ks) - set(keys.get(name, []))), [])
print("── the fields the iPhone deck and threads read (#1060 gaps, agreed with iOS 10-09) ──")
for name in (n for n in SURFACES if n.startswith("cards.")):
    s = SURFACES[name]
    ms = [m for g in s["groups"] for m in g["members"]]
    check(f"{name}: top-level now (the /v1/cards/wait cursor) is a number", isinstance(s.get("now"), (int, float)), True)
    check(f"{name}: every member carries its plain card, its own id inside",
          [m["subject"]["id"] for m in ms if (m.get("card") or {}).get("id") != m["subject"]["id"]], [])
    check(f"{name}: a card's data keeps its own kind (CapCard/VoiceNote read data.kind)",
          [m["subject"]["id"] for m in ms if isinstance(m["card"].get("data"), dict) and not m["card"]["data"].get("kind")], [])
    acts = [o for o in s["operations"] if o["contract"] == "jellybean.card.action@1"]
    check(f"{name}: every card button says action {{id, kind, value}}",
          [o["id"] for o in acts if set(o.get("action") or {}) != {"id", "kind", "value"}], [])
    check(f"{name}: every ask has words beside its key",
          [o["id"] for o in s["operations"] if o["verb"] == "ask" and "prompt" not in o.get("params", {})
           and not (o.get("label") or {}).get("text")], [])
for name in (n for n in SURFACES if n.startswith("threads.")):
    s = SURFACES[name]
    check(f"{name}: every chat member carries item_id", [m["subject"]["id"] for g in s["groups"] for m in g["members"]
                                                          if not str(m.get("item_id", "")).startswith("chat-")], [])
    check(f"{name}: every thread group carries edges, fringe and fringe_ops",
          [g["id"] for g in s["groups"] if g["id"].startswith("g-thread-") and not {"edges", "fringe", "fringe_ops"} <= set(g)], [])
    check(f"{name}: relate ops hold pairs of two and a related flag",
          [o["id"] for o in s["operations"] if o["verb"] in ("relate", "unrelate")
           and not (all(len(p) == 2 for p in o["params"]["pairs"]) and isinstance(o["params"]["related"], bool))], [])
check("the port reads what the Swift code reads: a group with no label decodes with an empty key, not dropped",
      shipped_parse({"groups": [{"id": "g-x", "members": []}]})["groups"][0]["labelKey"], "")
check("…and a group without an id is dropped (why every group must keep its id)",
      shipped_parse({"groups": [{"label": {"key": "k"}}]})["groups"], [])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
