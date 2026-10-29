#!/usr/bin/env python3
"""What others see of yours (Settings › Privacy & Sharing; Coordination 10-06): GET /v1/me/sharing.

Only ever the caller's own items, counted by who else may see them; never another person's, never titles or text:
  notes    the caller's KB items (kb_acl owner = them), by audience: private / household / adults / admins / named
  facts    world evidence the caller stated or filed (evidence owner = them), by audience
  places   who sees the caller's own place: "only you", or a kid's parents (Zach 10-02)
  grants   the caller's "Always for <name>" answers for item bodies (#446; the same as GET /v1/sharing/grants)
A shared household screen has no person, so nothing of its own: 403.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

AUDIENCES = ("private", "household", "adults", "admins", "named")


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _bucket(audience: Any) -> str:
    a = str(audience or "private")
    if a.startswith("["):
        return "named"
    return a if a in AUDIENCES else "named"


def notes(person: str, acl_rows: dict[str, tuple[str | None, str]] | None = None) -> dict[str, int]:
    if acl_rows is None:
        # Only notes still in the KB: an access row can outlive its note (670 of Zach's 777 rows had, 10-06)
        import agent_runtime_lib as _lib
        import kb_privacy
        acl_rows = kb_privacy.acl_rows(str(x.get("source_id")) for x in _lib.kb_sources())
    got = Counter(_bucket(aud) for owner, aud in acl_rows.values() if str(owner or "").lower() == person.lower())
    return {a: got.get(a, 0) for a in AUDIENCES}


def facts(person: str, w: Any = None) -> dict[str, int]:
    if w is None:
        import world
        w = world.get()
    worlds = [w.world(b) for b in w.bubbles()] if hasattr(w, "bubbles") else [w]
    got: Counter = Counter()
    for ww in worlds:
        for (aud, n) in ww.con.execute("SELECT audience, COUNT(*) FROM evidence WHERE owner=? GROUP BY audience",
                                       (person,)).fetchall():
            got[_bucket(aud)] += n
    return {a: got.get(a, 0) for a in AUDIENCES}


def places(person: str, role: str | None, people: list[dict[str, Any]]) -> dict[str, Any]:
    me = next((p for p in people if str(p.get("id")) == person), {})
    if role == "kid":
        ps = [str(p.get("name") or "").split(" ")[0] for p in people if str(p.get("id")) in (me.get("parents") or [])]
        return {"seen_by": "parents", "names": ps}
    return {"seen_by": "only you", "names": []}


def summary(person: str | None, role: str | None, people: list[dict[str, Any]], *, acl_rows: Any = None, w: Any = None,
            grants: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not person or role == "household":
        raise Refused(403, "a shared screen has nothing of its own")
    if grants is None:
        import item_open
        folks = {str(p["id"]).lower(): p for p in people}
        grants = [{"asker_first_name": str((folks.get(g["asker"]) or {}).get("name") or "someone").split(" ")[0],
                   "kind": g["kind"]} for g in item_open.grants_of(person)]
    return {"notes": notes(person, acl_rows), "facts": facts(person, w), "places": places(person, role, people),
            "grants": grants}
