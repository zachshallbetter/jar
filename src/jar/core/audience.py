#!/usr/bin/env python3
"""Who an item is for: one rule for every household store (cards, the knowledge base, the
timeline, records, todos, routines).

Every stored item has an `owner` (a person id, or None for the household or titan itself) and an
`audience`:
  "private"    the owner only (the default for anything personal)
  "household"  everyone, including kids and shared devices
  "adults"     the admin and adults
  "admins"     the admin only (titan's system and dev information)
  [ids]        those people (and the owner)

Zach, 09-30: Robin's texts, files and knowledge must not mix with his, and his system
information must not show up in her chats, yet sharing must take no effort ("share that with
Robin"). So: private by default for personal data, explicit audiences for the rest, and one
call (`share`) to widen an item.
"""
from __future__ import annotations

import json
import re
from typing import Any

GROUPS = ("private", "household", "adults", "admins")


def normalize(to: Any, default: str = "admins") -> str:
    """Store form: one of GROUPS, or a JSON list of person ids (sorted, lowercased)."""
    if to is None or to == "" or to == []:
        return default
    if isinstance(to, str):
        t = to.strip()
        if t.lower() in GROUPS:
            return t.lower()
        if t.startswith("["):
            try:
                return normalize(json.loads(t), default)
            except ValueError:
                pass
        to = [x for x in re.split(r"[,\s]+", t) if x]
    ids = sorted({str(x).strip().lower() for x in to if str(x).strip()})
    return json.dumps(ids) if ids else default


def visible(audience: str | None, person_id: str | None, role: str | None, *, owner: str | None = None) -> bool:
    """Whether the person (with role) may see an item with this audience and owner.
    person_id and role both None: an internal caller (titan itself) sees everything."""
    if person_id is None and role is None:
        return True
    if audience == "system":
        # the system bubble (titan's own services, from health checks, #355): the admin and engineering agents only
        return role in ("admin", "engineering")
    if role == "engineering":
        return False      # an engineering agent reads titan's system notes only (kb_privacy.kb_visible, #355)
    aud = audience or "admins"
    pid = str(person_id or "").lower()
    if owner and pid and str(owner).lower() == pid:
        return True
    if aud == "household":
        return True
    if aud == "adults":
        return role in ("admin", "adult")
    if aud == "admins":
        return role == "admin"
    if aud == "private":
        return bool(owner) and str(owner).lower() == pid
    try:
        return pid in json.loads(aud)
    except ValueError:
        return False


def widen(audience: str | None, add: Any) -> str:
    """The audience after sharing with more people or a group ("share that with Robin")."""
    new = normalize(add, default="private")
    if new in ("household", "adults", "admins"):
        order = {"private": 0, "admins": 1, "adults": 2, "household": 3}
        cur = audience if audience in order else "private"
        return new if order[new] >= order.get(cur, 0) else cur
    if audience in ("household", "adults"):
        return audience
    ids = set(json.loads(new))
    if audience and audience.startswith("["):
        ids |= set(json.loads(audience))
    return json.dumps(sorted(ids))
