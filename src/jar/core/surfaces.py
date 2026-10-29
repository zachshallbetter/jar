#!/usr/bin/env python3
"""Settings lists as ResolvedSurfaces (#1060; docs/surfaces.md, profile jb-1): Household facts and Privacy › Always
shared, the next adopters after memory. One envelope, one size rule, one undo store; items.py keeps its own for now.

**Access projection first** (docs/surfaces.md §6): each surface is built only from its store's own per-viewer reader
(world.entity_list for the household bubble, item_open.grants_of for the caller's own grants), so every member, count
and group is something the viewer may see. A shared screen gets neither (the route table).

**Size** (§2.7): sizeClass after projection. At every size the surface resolves a needs-a-look group (role
`attention`, at most ATTENTION_CAP) and then one group per who or what, ordered by use; the recipe shows the queue first
at `hundreds`. A group's batch operation returns one undo token (POST /v1/surfaces/undo), the caller's own, for
UNDO_TTL_S.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

DB_PATH = Path(os.environ.get("SURFACES_DB", "/ai/datasets/connectors/surfaces.db"))
ATTENTION_CAP = 5
UNUSED_S = 90 * 86400            # an always-grant nobody has used for this long needs a look
UNDO_TTL_S = 7 * 86400
_lock = threading.Lock()


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def size_class(n: int) -> str:
    return "few" if n <= 5 else "dozens" if n < 100 else "hundreds"


# ── group labels (#1060 §10, decided 10-09): a stable key the apps key on, plus English text beside it ───────────────
# Shipped apps (0.13.60) read only `label.key`; never rename or drop one. For keys the apps already render, the text is
# the app's own words (jellybean ItemsSurface.label), so old and new clients say the same thing.
LABELS = {
    "items.now.waiting_on_you": "Waiting on you", "items.now.today": "Today", "items.now.open": "Open",
    "items.now.moving": "Moving on its own", "items.now.waiting_on_others": "Waiting on others",
    "items.now.needs_a_home": "Needs a home", "items.now.due_soon": "Due soon", "items.now.new_mail": "New mail",
    "items.now.filed_for_you": "Filed for you", "items.project": "Project", "items.project.linked": "Linked",
    "items.person": "Person", "items.unsorted": "Unsorted", "items.unknown": "Couldn't read",
    "household.needs_a_look": "Needs a look", "household.places": "Places", "sharing.needs_a_look": "Needs a look",
    "cards.needs_a_decision": "Needs a decision", "cards.all": "Cards", "cards.engineering": "Engineering",
    "cards.done_today": "Done today", "threads.other": "Other chats", "threads.thread": "A thread",
    "documents.all": "Documents", "documents.on_devices": "On people's devices",
    "schedule.overlaps": "Overlaps", "schedule.day": "Day",
    "brief.needs_you": "Needs you", "brief.today": "Today", "brief.coming_up": "Coming up",
    "account.now": "Now", "account.cycle": "Billed", "account.earlier": "Earlier",
    "rooms.all": "Rooms",
    "review.to_vendor": "Go to a sender", "review.junk": "Look like junk", "review.appointments": "Look like appointments",
    "review.deadlines": "Look like deadlines", "review.bundle": "Go to a collection", "review.thread": "One conversation",
    "review.filed_for_you": "Filed for you", "review.left_out": "Left for you",
}


def label(key: str, text: str | None = None, **args: Any) -> dict[str, Any]:
    """A group label: {key, text[, args]}. `text` overrides for titan-owned names (a person, a thread); a key with no
    English text is a bug (test_surfaces_shipped_shape.py checks every resolver's)."""
    out: dict[str, Any] = {"key": key, "text": text or LABELS[key]}
    if args:
        out["args"] = args
    return out


# ── the envelope ─────────────────────────────────────────────────────────────────────────────────────────────────────

def member(kind: str, mid: str, rule: str, inputs: list[str], basis: str, priority: int,
           slots: dict[str, Any]) -> dict[str, Any]:
    return {"subject": {"kind": kind, "id": mid}, "basis": [{"rule": rule, "inputs": inputs, "kind": basis}],
            "priority": priority, "slots": slots, "state": "eligible"}


def operation(oid: str, verb: str, kind: str, sid: str, contract: str, method: str, path: str, *,
              params: dict[str, Any] | None = None, exposure: str = "available", reason: str | None = None,
              reversibility: str = "reversible", recovery: list[str] | None = None,
              variant: str | None = None, fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    op = {"id": oid, "verb": verb, "subject": {"kind": kind, "id": sid}, "exposure": exposure,
          "contract": contract, "route": {"method": method, "path": path}, "verification": "none",
          "reversibility": reversibility}
    if params:
        op["params"] = params
    if reason:
        op["reason"] = {"key": reason}
    if recovery:
        op["recovery"] = recovery
    if variant:
        op["variant"] = variant
    if fields:     # an op that takes input says what (Coordination, 10-10, #1511): the apps render one generic sheet
        op["fields"] = fields
    return op


def text_field(key: str, label_text: str, *, required: bool = True, value: str | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {"key": key, "label": label_text, "kind": "text", "required": required}
    if value is not None:
        f["value"] = value
    return f


def envelope(*, resolver: str, profile: str, purpose: str, viewer: str | None, role: str, rule: str,
             groups: list[dict[str, Any]], operations: list[dict[str, Any]], deps: list[tuple[str, str]],
             inputs: list[str], now: float, lens: dict[str, Any] | None = None,
             subject: dict[str, str] | None = None, excluded: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The ResolvedSurface around resolved groups and operations. sizeClass counts distinct members (a needs-a-look
    member is also in its own group); the fingerprint covers only what the viewer can see. `excluded`: the viewer's
    own ineligible items as [{rule, count}] (§4: muted, snoozed, done before today, capped), counts and rules only,
    never an id, and never anything another person's projection holds (a resolver counts only what it was served)."""
    n = len({m["subject"]["id"] for g in groups for m in g["members"]})
    deps = sorted(set(deps))
    fp_in = {"deps": deps, "lens": lens or {}, "viewer": viewer, "role": role, "resolver": resolver,
             "groups": [(g["id"], [m["subject"]["id"] for m in g["members"]]) for g in groups],
             "ops": sorted((o["id"], o["exposure"]) for o in operations), "now": int(now // 3600) * 3600}
    excluded = [x for x in excluded or [] if x.get("count")]
    if excluded:      # the viewer's own ineligible counts (§4); absent when none, so earlier fingerprints hold
        fp_in["excluded"] = sorted((x["rule"], x["count"]) for x in excluded)
    digest = hashlib.sha256(json.dumps(fp_in, sort_keys=True, default=str).encode()).hexdigest()
    return {"protocol": "sss", "protocolVersion": "0.1.0", "profileVersion": "jb-1",
            "resolutionId": "rs-" + digest[:16], "profile": profile, "purpose": purpose,
            **({"subject": subject} if subject else {}),
            "accessProjection": {"viewer": viewer, "role": role, "rule": rule},
            "groups": groups, "operations": operations, "states": {"withheld": 0, "unknown": 0},
            "sizeClass": size_class(n),
            "provenance": {"resolver": resolver, "inputs": inputs, "generated_at": _iso(now)},
            "explanation": {"included": [{"group": g["id"], "rule": g["members"][0]["basis"][0]["rule"] if g["members"]
                                          else None, "count": len(g["members"])} for g in groups],
                            "excluded": excluded, "ordering": [g["ordering"] for g in groups]},
            "dependencies": [{"subject": {"id": i}, "revision": r} for i, r in deps],
            "fingerprint": "sha256:" + digest}


# ── one undo store for batch operations ─────────────────────────────────────────────────────────────────────────────

def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("SURFACES_DB") or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE IF NOT EXISTS surface_undo (token TEXT PRIMARY KEY, person TEXT NOT NULL, kind TEXT NOT NULL, "
                "payload TEXT NOT NULL, at REAL NOT NULL)")
    return con


def save_undo(person: str, kind: str, payload: dict[str, Any], path: Path | None = None) -> str:
    token = "su-" + secrets.token_hex(12)
    with _lock:
        con = _db(path)
        try:
            con.execute("DELETE FROM surface_undo WHERE at < ?", (time.time() - UNDO_TTL_S,))
            con.execute("INSERT INTO surface_undo VALUES (?,?,?,?,?)", (token, person, kind, json.dumps(payload), time.time()))
            con.commit()
        finally:
            con.close()
    return token


def take_undo(person: str, token: str, path: Path | None = None) -> tuple[str, dict[str, Any]] | None:
    """The caller's own unexpired token, used up: (kind, payload), or None (unknown, someone else's, expired)."""
    with _lock:
        con = _db(path)
        try:
            r = con.execute("SELECT kind, payload, at FROM surface_undo WHERE token=? AND person=?",
                            (str(token), str(person))).fetchone()
            if r is None or r[2] < time.time() - UNDO_TTL_S:
                return None
            con.execute("DELETE FROM surface_undo WHERE token=?", (token,))
            con.commit()
            return r[0], json.loads(r[1])
        finally:
            con.close()


# ── Household facts (Settings › Household) ───────────────────────────────────────────────────────────────────────────

HOUSEHOLD_RESOLVER = "household-facts@1"
CAN_CONFIRM = ("admin", "adult")             # kids don't set household facts (world.set_fact)


def _facts_of(w, cid: str, person: str | None, role: str) -> list[Any]:
    return list(w.facts(cid, person, role))


def household_facts(w, person: str | None, role: str, now: float, *, q: str | None = None,
                    places: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The household bubble's facts as this viewer sees them, as a ResolvedSurface. One member per fact (entity ×
    attribute × qualifier); needs a look: stale or disputed facts, oldest first; then one group per entity, the most
    recently confirmed first. `q` narrows by entity name. `places` (#1510): the places this viewer may see
    (places_seed.listing, already per viewer), as a Places group with add on the group and edit / remove on each place
    the viewer may change; None leaves the group out."""
    import world
    if role not in ("admin", "adult", "kid") or not person:
        raise Refused(403, "household facts are a person's settings page")
    tz = w.now().tzinfo
    ents = world.entity_list(w, person, role, bubble_only="household", q=q or None, limit=500)["entities"]
    may = role in CAN_CONFIRM
    groups, ops, deps, queue = [], [], [], []
    for e in ents:
        if e.get("type") == "assistant":    # titan's own capabilities are system status, not a household fact (10-10)
            continue
        cid, rows = e["id"], []
        newest = ""
        for f in _facts_of(w, cid, person, role):
            st = world.answer_state(f)
            qk = world._qual_key(f.qual) if f.qual else ""
            fid = f"{cid}#{f.attr}" + (f"#{qk}" if qk else "")
            seen = world._iso_day(max((ev.observed_at for v in f.values for ev in v.evidence), default=None), tz)
            newest = max(newest, seen or "")
            value = ", ".join(world._show(v) for v in f.values)
            slots: dict[str, Any] = {"title": world._label(f), "trailing": {"value": value}, "actions": []}
            if seen:
                slots["meta"] = seen
            if st in ("stale", "disputed"):
                slots["status"] = {"text": f"fact.{st}", "tone": "attention"}
            single = len(f.values) == 1
            if single:
                oid = f"op-confirm-{fid}"
                ops.append(operation(oid, "confirm", "fact", fid, "jellybean.fact.confirm@1", "POST",
                                     f"/v1/world/entities/{cid}/facts",
                                     params={"attribute": f.attr, "value": world._show(f.values[0]),
                                             **({"qual": f.qual} if f.qual else {})},
                                     exposure="available" if may else "unavailable",
                                     reason=None if may else "kids.propose_only"))
                slots["actions"].append(oid)
            m = member("fact", fid, "household-fact", ["household bubble", "evidence"], "observed", 2, slots)
            rows.append(m)
            deps.append((fid, f"{st}:{value}:{seen}"))
            if st in ("stale", "disputed"):
                queue.append((seen or "", fid, m))
        if not rows:
            continue
        gid = f"g-entity-{cid}"
        stale_here = [x for x in rows if (x["slots"].get("status") or {}).get("text") == "fact.stale"]
        gops = []
        if stale_here:
            oid = f"op-confirm-all-{cid}"
            ops.append(operation(oid, "confirm", "entity", cid, "jellybean.fact.confirm_all@1", "POST",
                                 "/v1/surfaces/household-facts/confirm", params={"entity": cid},
                                 exposure="available" if may else "unavailable",
                                 reason=None if may else "kids.propose_only", recovery=["undo"]))
            gops.append(oid)
        groups.append({"id": gid, "role": "primary", "label": label("household.entity", e["name"]),
                       "members": rows, "ordering": "attribute", "operations": gops, "_newest": newest})
    groups.sort(key=lambda g: g["id"])
    groups.sort(key=lambda g: g["_newest"], reverse=True)      # most recently confirmed first (stable: then by id)
    for g in groups:
        g.pop("_newest", None)
    queue.sort(key=lambda t: (t[0] == "", t[0], t[1]))
    attention = {"id": "g-needs-a-look", "role": "attention", "label": label("household.needs_a_look"),
                 "members": [m for _, _, m in queue[:ATTENTION_CAP]], "ordering": "oldest-first"}
    tail: list[dict[str, Any]] = []
    if places is not None and not q:
        pg, pops, pdeps = _places_group(places, may)
        tail, ops, deps = [pg], ops + pops, deps + pdeps
    return envelope(resolver=HOUSEHOLD_RESOLVER, profile="collection", purpose="edit", viewer=person, role=role,
                    rule="world.entity_list (household bubble, per viewer)", groups=[attention] + groups + tail,
                    operations=ops, deps=deps, inputs=["world:household", "now", "viewer"] + (["places"] if tail else []),
                    now=now,
                    lens={"q": q or None})


def confirm_all(w, person: str, role: str, cid: str, path: Path | None = None) -> dict[str, Any]:
    """Restate every stale single-valued fact of one household entity as this person's own statement, with one undo
    token: their earlier statements on those attributes are snapshotted first and put back on undo."""
    import world
    if role not in CAN_CONFIRM or not person:
        raise Refused(403, "only an adult confirms household facts")
    b, _ = w.split(cid)
    if b != "household" or not w.visible_entity(cid, person, role):
        raise Refused(404, "no such household entity")
    ent = dict(w.entity(cid) or {})
    stale = [f for f in _facts_of(w, cid, person, role) if world.answer_state(f) == "stale" and len(f.values) == 1]
    snap: dict[str, list[dict[str, Any]]] = {}
    done = 0
    for f in stale:
        if f.attr not in snap:
            snap[f.attr] = w.user_statements(cid, f.attr, person)
        ok, _said, _why = world.set_fact(w, str(ent.get("name") or cid), f.attr, world._show(f.values[0]), person, role,
                                         cid=cid, qual=dict(f.qual) if f.qual else None)
        done += bool(ok)
    if not done:
        return {"confirmed": 0, "token": None}
    token = save_undo(person, "household-facts.confirm", {"entity": cid, "snapshots": snap}, path)
    return {"confirmed": done, "token": token}


# ── Settings › Household › Places (#1510): add / edit / remove on household-facts@1, over the one registry ───────────

def _choice(key: str, label_text: str, choices: list[tuple[str, str]], value: str) -> dict[str, Any]:
    return {"key": key, "label": label_text, "kind": "choice", "required": True, "value": value,
            "choices": [{"value": v, "label": l} for v, l in choices]}


def _kind_choices() -> list[tuple[str, str]]:
    import places_seed
    return [(k, k.capitalize()) for k in places_seed.KINDS]


VISIBILITY_CHOICES = [("private", "Just me"), ("household", "The household")]


def _places_group(rows: list[dict[str, Any]], may: bool) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[str, str]]]:
    """The Places group: add on the group (fields: name, kind, who sees it), edit and remove on each place the viewer
    may change. A kid sees the places with every operation unavailable (kids.propose_only); a place that isn't the
    viewer's to change (an adult's shared place seen by another adult) carries no operations."""
    exposure, reason = ("available", None) if may else ("unavailable", "kids.propose_only")
    ops = [operation("op-place-add", "add", "group", "g-places", "jellybean.place.add@1", "POST", "/v1/places",
                     exposure=exposure, reason=reason, recovery=["undo"],
                     fields=[text_field("name", "Name"), _choice("kind", "Kind", _kind_choices(), "named"),
                             _choice("visibility", "Who sees it", VISIBILITY_CHOICES, "private")])]
    members, deps = [], []
    for p in rows:
        slots: dict[str, Any] = {"title": p["label"], "media": {"symbol": "mappin"},
                                 "meta": "Shared with the household" if p["shared"] else "Private", "actions": []}
        if p["kind"] != "candidate":
            slots["subtitle"] = p["kind"].capitalize()
        if not p["confirmed"]:
            slots["meta"] += ", learned"
        if p.get("may_edit"):
            fields = [text_field("name", "Name", value=p["label"]),
                      _choice("kind", "Kind", _kind_choices(), p["kind"] if p["kind"] != "candidate" else "named")]
            if p.get("mine"):
                fields.append(_choice("visibility", "Who sees it", VISIBILITY_CHOICES,
                                      "household" if p["shared"] else "private"))
            ops.append(operation(f"op-place-edit-{p['id']}", "edit", "place", p["id"], "jellybean.place.edit@1",
                                 "PATCH", f"/v1/places/{p['id']}", exposure=exposure, reason=reason, recovery=["undo"],
                                 fields=fields))
            ops.append(operation(f"op-place-remove-{p['id']}", "remove", "place", p["id"], "jellybean.place.remove@1",
                                 "DELETE", f"/v1/places/{p['id']}", exposure=exposure, reason=reason,
                                 recovery=["undo"], variant="destructive"))
            slots["actions"] = [f"op-place-edit-{p['id']}", f"op-place-remove-{p['id']}"]
        members.append(member("place", p["id"], "place", ["places registry"],
                              "declared" if p["confirmed"] else "observed", 2, slots))
        deps.append((p["id"], f"{p['label']}:{p['kind']}:{p['shared']}:{p['confirmed']}"))
    group = {"id": "g-places", "role": "primary", "label": label("household.places"), "members": members,
             "ordering": "name", "operations": ["op-place-add"]}
    return group, ops, deps


def place_change(person: str, role: str, verb: str, *, place_id: str | None = None, body: Any = None,
                 path: Path | None = None, places_db: Any = None, household: Any = None) -> dict[str, Any]:
    """add {name, kind?, visibility?} → {place, token}; edit {name?, kind?, visibility?} → {place, token};
    remove → {place, token}. An adult's (a parent for their kid's place); the token is theirs and undoes exactly that
    (POST /v1/surfaces/undo). A kid proposes, nothing here."""
    import places_seed as PS
    if role not in CAN_CONFIRM or not person:
        raise Refused(403, "places are changed by an adult")
    try:
        if verb == "add":
            place, before = PS.add(person, role, body, places_db=places_db, household=household)
            kind = "places.add"
        elif verb == "edit":
            place, before = PS.edit(person, role, str(place_id or ""), body, places_db=places_db, household=household)
            kind = "places.edit"
        elif verb == "remove":
            place, before = PS.remove(person, role, str(place_id or ""), places_db=places_db, household=household)
            kind = "places.remove"
        else:
            raise Refused(400, "unknown place operation")
    except PS.ConfirmError as exc:
        raise Refused(exc.status, exc.detail) from None
    token = save_undo(person, kind, {"id": place["id"], "before": before}, path)
    return {"place": place, "token": token}


# ── Privacy › Always shared ──────────────────────────────────────────────────────────────────────────────────────────

SHARED_RESOLVER = "always-shared@1"


def always_shared(owner: str | None, role: str, people: dict[str, dict[str, Any]], now: float,
                  grants_of: Callable[[str], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """The caller's own "Always for <name>" answers as a ResolvedSurface. Needs a look: unused for 90 days (or never
    used and given more than 90 days ago), least recently used first; then one group per asker, the most recently
    used first. Askers by first name only; never a grant someone else gave."""
    import item_open
    if role not in ("admin", "adult", "kid") or not owner:
        raise Refused(403, "always-shared is a person's settings page")
    rows = (grants_of or item_open.grants_of)(owner)
    by_asker: dict[str, list[dict[str, Any]]] = {}
    for g in rows:
        by_asker.setdefault(g["asker"], []).append(g)
    groups, ops, deps, queue = [], [], [], []
    for asker, gs in by_asker.items():
        name = str((people.get(str(asker).lower()) or {}).get("name") or "someone").split(" ")[0]
        mem = []
        last = 0.0
        for g in sorted(gs, key=lambda g: g["kind"]):
            gid = item_open.grant_id(owner, asker, g["kind"])
            used = g.get("used_at")
            last = max(last, float(used or 0))
            unused = (now - float(used)) >= UNUSED_S if used else (now - float(g["at"])) >= UNUSED_S
            oid = f"op-remove-{gid}"
            ops.append(operation(oid, "remove", "grant", gid, "jellybean.grant.remove@1", "DELETE",
                                 f"/v1/sharing/grants/{gid}", reversibility="compensatable", variant="destructive"))
            slots: dict[str, Any] = {"title": g["kind"], "subtitle": name, "actions": [oid]}
            if used:
                slots["meta"] = _iso(used)
            if unused:
                slots["status"] = {"text": "grant.unused", "tone": "attention"}
            m = member("grant", gid, "always-grant", ["body_grants"], "declared", 2, slots)
            mem.append(m)
            deps.append((gid, f"{g['at']}:{used}"))
            if unused:
                queue.append((float(used or g["at"]), gid, m))
        oid = f"op-stop-{asker}"
        ops.append(operation(oid, "remove", "person", str(asker), "jellybean.grant.stop_all@1", "POST",
                             "/v1/sharing/grants/stop", params={"asker": asker}, recovery=["undo"],
                             variant="destructive"))
        groups.append({"id": f"g-asker-{asker}", "role": "primary", "label": label("sharing.asker", name),
                       "members": mem, "ordering": "kind", "operations": [oid], "_last": last})
    groups.sort(key=lambda g: (-g["_last"], g["id"]))
    for g in groups:
        g.pop("_last", None)
    queue.sort(key=lambda t: (t[0], t[1]))
    attention = {"id": "g-needs-a-look", "role": "attention", "label": label("sharing.needs_a_look"),
                 "members": [m for _, _, m in queue[:ATTENTION_CAP]], "ordering": "least-recently-used"}
    return envelope(resolver=SHARED_RESOLVER, profile="collection", purpose="edit", viewer=owner, role=role,
                    rule="item_open.grants_of (the owner's own)", groups=[attention] + groups, operations=ops,
                    deps=deps, inputs=["body_grants", "now", "viewer"], now=now)


def stop_all(owner: str, asker: str, path: Path | None = None, grants_path: Path | None = None) -> dict[str, Any]:
    """Take back every grant the owner gave this asker, with one undo token."""
    import item_open
    rows = item_open.take(owner, asker=str(asker), path=grants_path)
    if not rows:
        return {"stopped": 0, "token": None}
    return {"stopped": len(rows), "token": save_undo(owner, "always-shared.stop", {"rows": rows}, path)}


# ── Settings › Rooms (#1511) ────────────────────────────────────────────────────────────────────────────────────────

ROOMS_RESOLVER = "rooms@1"
CAN_EDIT_ROOMS = ("admin", "adult")


def _screens(store) -> dict[str, int]:
    """key(room) → how many household screens name it. Only screens: a person's own device in a room is where that
    person is, which a household list never shows (10-01 rules)."""
    import rooms as R
    out: dict[str, int] = {}
    for p in store.people():
        if p.get("role") != "household":
            continue
        for d in p.get("devices") or []:
            if d.get("room") and not d.get("revoked"):
                out[R.key(d["room"])] = out.get(R.key(d["room"]), 0) + 1
    return out


def rooms_surface(person: str | None, role: str, room_list: list[dict[str, Any]], screens: dict[str, int],
                  now: float) -> dict[str, Any]:
    """Settings › Rooms as a ResolvedSurface: one group, a member per room (name, whether it's a Home Assistant area,
    how many household screens are in it); add on the group, rename and remove on each room (remove with undo). Kids
    see the rooms, with the operations unavailable; a shared screen gets none (403)."""
    import rooms as R
    if role not in ("admin", "adult", "kid") or not person:
        raise Refused(403, "rooms are a person's settings page")
    may = role in CAN_EDIT_ROOMS
    exposure, reason = ("available", None) if may else ("unavailable", "kids.propose_only")
    ops = [operation("op-room-add", "add", "group", "g-rooms", "jellybean.room.add@1", "POST", "/v1/rooms",
                     exposure=exposure, reason=reason, fields=[text_field("name", "Name")])]
    members, deps = [], []
    for r in room_list:
        n = screens.get(R.key(r["name"]), 0)
        slots: dict[str, Any] = {"title": r["name"], "media": {"symbol": "house"}, "trailing": {"value": str(n)},
                                 "meta": "Home Assistant area" if r.get("ha_area") else "Added here",
                                 "actions": [f"op-room-rename-{r['id']}", f"op-room-remove-{r['id']}"]}
        ops.append(operation(f"op-room-rename-{r['id']}", "rename", "room", r["id"], "jellybean.room.rename@1",
                             "PATCH", f"/v1/rooms/{r['id']}", exposure=exposure, reason=reason, recovery=["undo"],
                             fields=[text_field("name", "Name", value=r["name"])]))
        ops.append(operation(f"op-room-remove-{r['id']}", "remove", "room", r["id"], "jellybean.room.remove@1",
                             "DELETE", f"/v1/rooms/{r['id']}", exposure=exposure, reason=reason, recovery=["undo"],
                             variant="destructive"))
        members.append(member("room", r["id"], "room", ["rooms store", "home assistant areas"], "declared", 2, slots))
        deps.append((r["id"], f"{r['name']}:{bool(r.get('ha_area'))}:{n}"))
    group = {"id": "g-rooms", "role": "primary", "label": label("rooms.all"), "members": members,
             "ordering": "name", "operations": ["op-room-add"]}
    return envelope(resolver=ROOMS_RESOLVER, profile="collection", purpose="edit", viewer=person, role=role,
                    rule="the household's rooms (no person's whereabouts)", groups=[group], operations=ops, deps=deps,
                    inputs=["rooms", "home assistant areas", "household screens"], now=now)


def room_change(person: str, role: str, store, verb: str, *, room_id: str | None = None, name: Any = None,
                path: Path | None = None, rooms_path: Path | None = None) -> dict[str, Any]:
    """add {name} → {room}; rename {room_id, name} → {room, screens, token}; remove {room_id} → {room, screens, token}.
    The screens follow the room (household store); `screens` names household screens only. An adult's or the
    admin's; the undo token is theirs (POST /v1/surfaces/undo)."""
    import rooms as R
    if role not in CAN_EDIT_ROOMS or not person:
        raise Refused(403, "rooms are changed by an adult")
    try:
        if verb == "add":
            return {"room": R.add(name, path=rooms_path)}
        if verb == "rename":
            room, old = R.rename(str(room_id or ""), name, path=rooms_path)
            moved = store.move_room(old, room["name"]) if R.key(old) != R.key(room["name"]) else []
            token = save_undo(person, "rooms.rename", {"id": room["id"], "old": old, "new": room["name"],
                                                       "devices": moved}, path)
            return {"room": room, "screens": [d["name"] for d in moved if d["screen"]], "token": token}
        if verb == "remove":
            room = R.remove(str(room_id or ""), path=rooms_path)
            moved = store.move_room(room["name"], None)
            token = save_undo(person, "rooms.remove", {"id": room["id"], "name": room["name"], "devices": moved}, path)
            return {"room": room, "screens": [d["name"] for d in moved if d["screen"]], "token": token}
    except R.RoomError as exc:
        raise Refused(exc.status, exc.detail) from None
    raise Refused(400, "unknown room operation")


# ── undo ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def undo(person: str, token: str, w=None, path: Path | None = None, grants_path: Path | None = None,
         places_db: Any = None) -> dict[str, Any]:
    got = take_undo(person, token, path)
    if got is None:
        raise Refused(404, "no such undo")
    kind, payload = got
    if kind == "always-shared.stop":
        import item_open
        return {"undone": kind, "restored": item_open.restore(person, payload["rows"], grants_path)}
    if kind in ("rooms.rename", "rooms.remove"):
        import household_identity as hh
        import rooms as R
        try:
            room = R.restore(payload["id"], payload["old"] if kind == "rooms.rename" else payload["name"])
        except R.RoomError as exc:
            raise Refused(exc.status, exc.detail) from None
        back = hh.get_store().put_device_rooms(payload.get("devices") or [])
        return {"undone": kind, "room": room, "screens_back": back}
    if kind in ("places.add", "places.edit", "places.remove"):
        import places_seed
        back = places_seed.restore(payload.get("before"), payload["id"], places_db=places_db)
        return {"undone": kind, "restored": int(back)}
    if kind == "scans-review.change":      # review@1 (#1507): approve / exclude / an automatic filing, put back exactly
        import review_store
        return {"undone": kind, "restored": review_store.restore(person, payload)}
    if kind == "household-facts.confirm":
        if w is None:
            import world
            w = world.get()
        for attr, rows in payload["snapshots"].items():
            w.restore_user_statements(payload["entity"], attr, person, rows)
        return {"undone": kind, "restored": len(payload["snapshots"])}
    raise Refused(400, "unknown undo")


# ── the cards deck (#1060 migration step 2: `attention`, beside GET /v1/proactive) ────────────────────────────────────

CARDS_RESOLVER = "cards@1"


def _card_priority(urgency: Any) -> int:
    """SSS §13 from a card's urgency: 3 → 0 required, 2 → 1 primary, 1 → 2 supporting, 0 → 3 peripheral."""
    try:
        u = int(urgency or 0)
    except (TypeError, ValueError):
        u = 0
    return {3: 0, 2: 1, 1: 2}.get(max(0, min(u, 3)), 3)


def _cap_input(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """An approve_cap card's slider (jellybean#707) as the Approve operation's declared input: the app posts
    {action_id, value: "cap:<cap_id>:<n>"}, which proactive.card_action checks against the card's own cap."""
    if not data or data.get("kind") != "approve_cap" or not data.get("cap_id"):
        return None
    return {"kind": "range", "sends": "value", "value_format": "cap:{cap_id}:{value}",
            **{k: data.get(k) for k in ("cap_id", "unit", "min", "max", "step", "default", "format")}}


def cards(served: list[dict[str, Any]], person: str | None, role: str, now: float, day_start: float,
          *, safety_sources: frozenset[str] | set[str] = frozenset(),
          ineligible: dict[str, int] | None = None) -> dict[str, Any]:
    """The cards deck as a ResolvedSurface (`attention`). `served` is exactly what this viewer's GET /v1/proactive
    returns with include_acked (proactive.list_cards for them, then card_items): access projection is already done,
    so nothing here can add a card they couldn't see. Groups: needs a decision (approval cards), cards (the rest,
    engineering cards in their own group), and done today (acknowledged since the household day began). Each card's
    verbs name the existing routes; exposure follows each route's own rules (a shared screen never gets Later or Stop
    these; a safety card can't be muted or put off). `now` and `day_start` are inputs, never the wall clock.
    Additive for the apps' card features (#1060, from iOS): top-level `now` (the long-poll cursor), each member's
    `card` (the plain payload's card for this viewer, verbatim), and each button's `action {id, kind, value}`
    (plus `input` for an approve_cap slider)."""
    screen = role == "household"
    attention, regular, eng, history, ops, deps = [], [], [], [], [], []
    done_before = 0
    for c in served:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        acked = c.get("acked_at")
        if acked and float(acked) < day_start:
            done_before += 1
            continue                                   # done before today: not on the deck
        safety = str(c.get("source") or "").strip().lower() in safety_sources   # proactive.is_safety_source
        upd = float(c.get("updated_at") or c.get("created_at") or 0)
        slots: dict[str, Any] = {"title": str(c.get("title") or ""), "actions": []}
        if c.get("detail"):
            slots["subtitle"] = str(c["detail"])
        src = str(c.get("source") or "").rsplit(":", 1)[-1]
        if src:
            slots["meta"] = src
        if c.get("snoozed_from"):
            slots["status"] = {"text": "card.back_from_later", "tone": "info"}
        elif not acked and _card_priority(c.get("urgency")) <= 1:
            slots["status"] = {"text": "card.urgent", "tone": "attention"}
        data = c.get("data") if isinstance(c.get("data"), dict) else None
        cap = _cap_input(data)
        if not acked:
            item = c.get("item") if isinstance(c.get("item"), dict) else None
            if item and item.get("id"):
                oid = f"op-open-{cid}"
                ops.append(operation(oid, "open", "card", cid, "jellybean.card.open@1", "GET", "/v1/items",
                                     params={"view": "now", "item": item["id"],
                                             **({"project": item["project"]} if item.get("project") else {})}))
                slots["actions"].append(oid)
            oid = f"op-ask-{cid}"
            ask = operation(oid, "ask", "card", cid, "jellybean.card.ask@1", "POST", "/v1/prompt",
                            params=({"prompt": str(c["action_prompt"])} if c.get("action_prompt")
                                    else {"prompt_key": "card.tell_me_more"}))
            ask["label"] = {"key": "card.tell_me_more", "text": "Tell me more"}
            ops.append(ask)
            slots["actions"].append(oid)
            for i, a in enumerate(c.get("actions") or []):
                if not isinstance(a, dict) or not a.get("id"):
                    continue
                oid = f"op-act-{cid}-{a['id']}"
                op = operation(oid, str(a.get("kind") or "prompt"), "card", cid, "jellybean.card.action@1", "POST",
                               f"/v1/proactive/{cid}/action", params={"action_id": str(a["id"])},
                               variant="primary" if (c.get("approval") and i == 0) else None,
                               reversibility="unknown")
                if a.get("label"):
                    op["label"] = {"text": str(a["label"])}    # the card's own button words: content titan owns
                op["action"] = {"id": str(a["id"]), "kind": str(a.get("kind") or "prompt"), "value": a.get("value")}
                if cap and a["id"] == "approve":
                    op["input"] = cap
                ops.append(op)
                slots["actions"].append(oid)
            oid = f"op-later-{cid}"
            ops.append(operation(oid, "later", "card", cid, "jellybean.card.later@1", "POST",
                                 f"/v1/proactive/{cid}/snooze", params={"until": None},
                                 exposure="withheld" if screen else ("unavailable" if safety else "available"),
                                 reason="card.safety" if (safety and not screen) else None))
            slots["actions"].append(oid)
            oid = f"op-done-{cid}"
            ops.append(operation(oid, "done", "card", cid, "jellybean.card.done@1", "POST", f"/v1/proactive/{cid}/ack"))
            slots["actions"].append(oid)
            oid = f"op-mute-{cid}"
            ops.append(operation(oid, "mute", "card", cid, "jellybean.card.mute@1", "POST", f"/v1/proactive/{cid}/mute",
                                 exposure="withheld" if screen else ("available" if c.get("can_mute") else "unavailable"),
                                 reason="card.safety" if (not screen and not c.get("can_mute")) else None,
                                 recovery=["unmute"]))
            slots["actions"].append(oid)
        m = member("card", cid, "card", ["source", "urgency", "updated_at"] + (["acked_at"] if acked else []),
                   "declared", _card_priority(c.get("urgency")), slots)
        m["card"] = dict(c)   # the plain payload's card for this viewer, verbatim: the apps' per-card parsers read it
        deps.append((cid, f"{upd}:{acked or ''}:{c.get('expires_at') or ''}"))
        key = (_card_priority(c.get("urgency")), -upd, cid)
        if acked:
            history.append((-float(acked), cid, m))
        elif c.get("approval"):
            attention.append((key, m))
        elif c.get("group") == "engineering":
            eng.append((key, m))
        else:
            regular.append((key, m))
    groups = [{"id": "g-needs-a-decision", "role": "attention", "label": label("cards.needs_a_decision"),
               "members": [m for _, m in sorted(attention, key=lambda t: t[0])], "ordering": "priority,then-newest"},
              {"id": "g-cards", "role": "supporting", "label": label("cards.all"),
               "members": [m for _, m in sorted(regular, key=lambda t: t[0])], "ordering": "priority,then-newest"}]
    if eng:
        groups.append({"id": "g-engineering", "role": "supporting", "label": label("cards.engineering"),
                       "members": [m for _, m in sorted(eng, key=lambda t: t[0])], "ordering": "priority,then-newest"})
    groups.append({"id": "g-done-today", "role": "history", "label": label("cards.done_today"),
                   "members": [m for _, _, m in sorted(history)], "ordering": "newest-first"})
    out = envelope(resolver=CARDS_RESOLVER, profile="attention", purpose="act", viewer=person, role=role,
                   rule="proactive.list_cards (this viewer's own cards) + card_items", groups=groups, operations=ops,
                   deps=deps, inputs=["proactive:cards", "now", "day_start", "viewer"], now=now,
                   lens={"day_start": int(day_start)},
                   excluded=[{"rule": "done-before-today", "count": done_before}]
                   + [{"rule": k, "count": v} for k, v in sorted((ineligible or {}).items())])
    out["now"] = now      # the plain payload's `now`: the cursor, passed as `since` to GET /v1/cards/wait
    return out


# ── threads (#1060 migration step 3: GET /v1/conversations?view=threads&as=surface) ─────────────────────────────────

THREADS_RESOLVER = "threads@1"


def _chat_member(r: dict[str, Any], rule: str, inputs: list[str], basis: str) -> dict[str, Any]:
    slots: dict[str, Any] = {"title": (r.get("title") or r.get("preview") or "").strip(),
                             "actions": [f"op-open-chat:{r['id']}", f"op-file-chat:{r['id']}"]}
    if r.get("preview") and r.get("title"):
        slots["subtitle"] = r["preview"]
    if r.get("last_at"):
        slots["meta"] = r["last_at"]
    if r.get("count"):
        slots["trailing"] = {"value": str(r["count"])}
    if (r.get("project") or {}).get("id"):
        slots["projects"] = [r["project"]["id"]]
    m = member("chat", f"chat:{r['id']}", rule, inputs, basis, 2, slots)
    m["item_id"] = r.get("item_id") or f"chat-{r['id']}"     # what POST /v1/threads/relate takes
    return m


def _relate_op(oid: str, verb: str, tid: str, pairs: list[tuple[str, str]], related: bool) -> dict[str, Any]:
    """The person's word on a thread (docs/chat-threads-api.md): the app posts {a, b, related} once per pair."""
    return operation(oid, verb, "thread", tid, f"jellybean.thread.{verb}@1", "POST", "/v1/threads/relate",
                     params={"pairs": [list(p) for p in pairs], "related": related})


def threads(body: dict[str, Any], person: str | None, role: str, now: float) -> dict[str, Any]:
    """chat_threads.build's view (already this person's own chats) as a `collection` surface. One group per thread,
    named by titan (its label text is the thread's name), then `g-other`: every row of `recent` no shown thread holds.
    Bundle, never merge: each conversation is its own member, in exactly one group. Ops are the items surface's chat
    ops (open in the app; file through PATCH /v1/conversations/{id}). facets and filters ride along for the filter
    chips; precision, labels and the spot-check stay on the plain view."""
    groups: list[dict[str, Any]] = []
    ops: list[dict[str, Any]] = []
    seen: set[str] = set()
    rows: dict[str, dict[str, Any]] = {}

    def newest(rs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rs, key=lambda r: (str(r.get("last_at") or ""), str(r["id"])), reverse=True)

    for t in body.get("threads") or []:
        mine = [r for r in newest(t.get("conversations") or []) if str(r["id"]) not in seen]
        if not mine:
            continue
        for r in mine:
            seen.add(str(r["id"]))
            rows[str(r["id"])] = r
        tid = str(t["id"])
        edges = [e for e in t.get("edges") or [] if isinstance(e, dict) and e.get("a") and e.get("b")]
        members = [_chat_member(r, "thread-linker", ["edges"], "derived") for r in mine]
        unsaid = [(e["a"], e["b"]) for e in edges if e.get("state") != "verified"]
        if unsaid:     # titan joined these on its own: the person may confirm them (once they all said so, nothing to ask)
            ops.append(_relate_op(f"op-same-{tid}", "relate", tid, unsaid, True))
        for m in members:
            pairs = [(e["a"], e["b"]) for e in edges if m["item_id"] in (e["a"], e["b"])]
            if pairs:
                oid = f"op-notin-{tid}-{m['subject']['id']}"
                ops.append(_relate_op(oid, "unrelate", tid, pairs, False))
                m["slots"]["actions"].append(oid)
        fringe = [f for f in t.get("fringe") or [] if isinstance(f, dict) and f.get("item_id")]
        fringe_ops = []
        for f in fringe:
            e = f.get("edge") or {}
            if e.get("a") and e.get("b"):
                oid = f"op-fringe-{tid}-{f['item_id']}"
                ops.append(_relate_op(oid, "relate", tid, [(e["a"], e["b"])], True))
                fringe_ops.append(oid)
        groups.append({"id": f"g-thread-{tid}", "role": "primary",
                       "label": label("threads.thread", (t.get("name") or "").strip() or (t.get("description") or "").strip() or None),
                       "summary": t.get("description") or "", "members": members, "ordering": "newest-first",
                       "edges": edges, "fringe": fringe, "fringe_ops": fringe_ops,
                       **({"same_op": f"op-same-{tid}"} if unsaid else {})})
    other = [r for r in newest(body.get("recent") or []) if str(r["id"]) not in seen]
    for r in other:
        rows[str(r["id"])] = r
    groups.append({"id": "g-other", "role": "supporting", "label": label("threads.other"),
                   "members": [_chat_member(r, "recent", ["last_at"], "declared") for r in other],
                   "ordering": "newest-first"})
    for cid in sorted(rows):
        ops.append({"id": f"op-open-chat:{cid}", "verb": "open", "subject": {"kind": "chat", "id": f"chat:{cid}"},
                    "exposure": "available", "contract": "jellybean.chat.open@1", "verification": "none",
                    "reversibility": "reversible"})
        ops.append(operation(f"op-file-chat:{cid}", "file", "chat", f"chat:{cid}", "jellybean.chat.file@1",
                             "PATCH", f"/v1/conversations/{cid}"))
    filters = body.get("filters") or []
    out = envelope(resolver=THREADS_RESOLVER, profile="collection", purpose="browse", viewer=person, role=role,
                   rule="chat_threads.build (this person's own conversations)", groups=groups, operations=ops,
                   deps=[(f"chat:{cid}", str(r.get("last_at") or "")) for cid, r in rows.items()],
                   inputs=["conversations", "linker", "now", "viewer"], now=now,
                   lens={"view": "threads", "filters": filters, "since": body.get("since")})
    out["facets"], out["filters"] = body.get("facets") or {}, filters
    return out


# ── documents (#1060 migration step 4: GET /v1/documents?as=surface) ──────────────────────────────────────────────

DOCUMENTS_RESOLVER = "documents@1"


def _ask_about(kind: str, sid: str) -> dict[str, Any]:
    """Ask about a thing (#1322): the person's own fixed words plus a reference titan resolves for them, never a title
    pasted into the prompt (the title reaches the model inside the turn's untrusted markers, about_ref.py)."""
    op = operation(f"op-ask-{sid}", "ask", kind, sid, f"jellybean.{kind}.ask@1", "POST", "/v1/prompt",
                   params={"prompt": "Tell me about this.", "about": {"kind": kind, "id": sid}})
    op["label"] = {"key": f"{kind}.ask", "text": "Ask about this"}
    return op


def documents(listing: dict[str, Any], person: str | None, role: str, now: float) -> dict[str, Any]:
    """documents.listing (household-audience only, the same for every caller) as a `collection` surface. Groups:
    `g-documents` (what opens: household KB text, and originals their owner shared as a file), then `g-on-devices`
    (originals still on their owner's device: Open is `unavailable`, reason `document.on_device`). Each member keeps
    the plain row verbatim (`document`) for the File Provider's parser; open is GET /v1/documents/{id}/content.
    Ask (#1322) sends the person's own words plus {about: {kind: document, id}}; titan resolves it for them and adds
    its title and text to the turn inside the untrusted markers (#473), read-only."""
    rows = [d for d in listing.get("documents") or [] if isinstance(d, dict) and d.get("id")]
    rows.sort(key=lambda d: (str(d.get("modified") or ""), str(d["id"])), reverse=True)
    opens, held, ops, deps = [], [], [], []
    for d in rows:
        did, title = str(d["id"]), str(d.get("title") or "").strip()
        slots: dict[str, Any] = {"title": title or str(d.get("kind") or "document"), "actions": []}
        if d.get("modified"):
            slots["meta"] = str(d["modified"])
        if d.get("kind"):
            slots["subtitle"] = str(d["kind"])
        ok = bool(d.get("available"))
        oid = f"op-open-{did}"
        ops.append(operation(oid, "open", "document", did, "jellybean.document.open@1", "GET",
                             f"/v1/documents/{did}/content", exposure="available" if ok else "unavailable",
                             reason=None if ok else "document.on_device"))
        ops.append(_ask_about("document", did))
        slots["actions"] += [oid, f"op-ask-{did}"]
        m = member("document", did, "household-document", ["audience", "file_shared"], "declared", 2, slots)
        m["document"] = dict(d)
        (opens if ok else held).append(m)
        deps.append((did, f"{d.get('modified') or ''}:{int(ok)}"))
    groups = [{"id": "g-documents", "role": "primary", "label": label("documents.all"), "members": opens,
               "ordering": "newest-first"},
              {"id": "g-on-devices", "role": "supporting", "label": label("documents.on_devices"), "members": held,
               "ordering": "newest-first"}]
    return envelope(resolver=DOCUMENTS_RESOLVER, profile="collection", purpose="browse", viewer=person, role=role,
                    rule="documents.listing (household audience only, the same for every caller)", groups=groups,
                    operations=ops, deps=deps, inputs=["kb:household", "references:household", "file_share", "viewer"],
                    now=now, lens={"audience": "household"})


# ── schedule (#1060 migration step 4: GET /v1/schedule?as=surface) ────────────────────────────────────────────────

SCHEDULE_RESOLVER = "schedule@1"


def _day_label(d: date, today: date) -> dict[str, Any]:
    return label("schedule.day", "Today" if d == today else f"{d.strftime('%a %b')} {d.day}", date=d.isoformat())


def _event_span(e: dict[str, Any], zone: Any) -> tuple[date, datetime | None, datetime | None]:
    """-> (household day, start, end) in household time; all-day events have no clock times."""
    raw = str(e.get("start") or "")
    if e.get("all_day"):
        return date.fromisoformat(raw[:10]), None, None
    st = datetime.fromisoformat(raw)
    st = st.astimezone(zone).replace(tzinfo=None) if (st.tzinfo and zone) else st.replace(tzinfo=None)
    en = None
    if e.get("end"):
        x = datetime.fromisoformat(str(e["end"]))
        en = x.astimezone(zone).replace(tzinfo=None) if (x.tzinfo and zone) else x.replace(tzinfo=None)
    return st.date(), st, en


def schedule(view: dict[str, Any], person: str | None, role: str, now: float, today: date, zone: Any = None
             ) -> dict[str, Any]:
    """schedule_view.for_viewer's view (already this viewer's: audience, calendars, kids' calendars, fences) as a
    `temporal` surface. One group per household day with events (today always, `primary`; later days `supporting`),
    each day's all-day events first, then by start. `g-overlaps` (`attention`) first: the viewer's own timed events
    that overlap (theirs, or theirs to run); a shared screen has none. Each member keeps the plain event verbatim
    (`event`) plus `whose {id, name, you}`. Open (in the app) and Ask (#1322): the person's own words plus {about:
    {kind: event, id}}, resolved on titan through this same view, its title inside the untrusted markers."""
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for p in view.get("people") or []:
        for e in p.get("events") or []:
            rows.append((e, {"id": p.get("id"), "name": p.get("name"), "you": bool(p.get("you"))}))
    for e in (view.get("household") or {}).get("events") or []:
        rows.append((e, {"id": "household", "name": None, "you": False}))
    days: dict[date, list[tuple[tuple, dict[str, Any]]]] = {today: []}
    mine: list[tuple[datetime, datetime, str]] = []
    ops: list[dict[str, Any]] = []
    deps: list[tuple[str, str]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for e, whose in rows:
        eid = str(e.get("id") or "")
        if not eid or eid in by_id:
            continue
        try:
            d, st, en = _event_span(e, zone)
        except ValueError:
            continue
        slots: dict[str, Any] = {"title": str(e.get("title") or ""), "meta": str(e.get("start") or ""),
                                 "actions": [f"op-open-{eid}", f"op-ask-{eid}"]}
        if e.get("all_day"):
            slots["all_day"] = True
        if e.get("location"):
            slots["subtitle"] = str(e["location"])
        if whose["id"] != "household" and not whose["you"] and whose.get("name"):
            slots["audience"] = str(whose["name"])
        m = member("event", eid, "visible-event", ["calendar", "audience", "fences", "time"], "declared",
                   1 if d == today else 2, slots)
        m["event"], m["whose"] = dict(e), whose
        by_id[eid] = m
        days.setdefault(d, []).append(((0 if st is None else 1, st or datetime.min, eid), m))
        ops.append({"id": f"op-open-{eid}", "verb": "open", "subject": {"kind": "event", "id": eid},
                    "exposure": "available", "contract": "jellybean.event.open@1", "verification": "none",
                    "reversibility": "reversible"})
        ops.append(_ask_about("event", eid))
        deps.append((eid, str(e.get("start") or "") + "|" + str(e.get("end") or "")))
        if st is not None and person and (whose["you"] or e.get("managed_by_you")):
            mine.append((st, en or st, eid))
    clash: set[str] = set()
    mine.sort()
    for i, (a0, a1, ai) in enumerate(mine):
        for b0, _b1, bi in mine[i + 1:]:
            if b0 >= max(a1, a0 + timedelta(seconds=1)):
                break
            clash |= {ai, bi}
    groups: list[dict[str, Any]] = []
    if clash:
        over = []
        for eid in sorted(clash, key=lambda x: (by_id[x]["slots"]["meta"], x)):
            m = json.loads(json.dumps(by_id[eid]))
            m["slots"]["status"] = {"text": "schedule.overlaps", "tone": "attention"}
            m["basis"] = [{"rule": "overlaps-your-own", "inputs": ["time", "owner"], "kind": "derived"}]
            m["priority"] = 0
            over.append(m)
        groups.append({"id": "g-overlaps", "role": "attention", "label": label("schedule.overlaps"),
                       "members": over, "ordering": "time"})
    for d in sorted(days):
        groups.append({"id": f"g-day-{d.isoformat()}", "role": "primary" if d == today else "supporting",
                       "label": _day_label(d, today), "members": [m for _, m in sorted(days[d], key=lambda t: t[0])],
                       "ordering": "time"})
    out = envelope(resolver=SCHEDULE_RESOLVER, profile="temporal", purpose="monitor", viewer=person, role=role,
                   rule="schedule_view.for_viewer (audience, calendars, kids' calendars, fences)", groups=groups,
                   operations=ops, deps=deps, inputs=["appointments", "people", "household_calendars", "fences",
                                                      "today", "viewer"], now=now,
                   lens={"start": view.get("start"), "end": view.get("end"), "today": today.isoformat()})
    out["partial"] = bool(view.get("partial"))
    return out


# ── the brief (#1060 migration step 4: GET /v1/brief) ──────────────────────────────────────────────────────────────

BRIEF_RESOLVER = "brief@1"
BRIEF_SOURCES = ("schedule@1", "items@1", "cards@1", "weather")


def brief(sched: dict[str, Any], items_now: dict[str, Any], deck: dict[str, Any], person: str | None, role: str,
          now: float, today: date, *, weather: dict[str, Any] | None = None) -> dict[str, Any]:
    """The daily brief, resolved once on titan (§10, decided 10-09): a composition of three surfaces already built for
    this viewer (schedule, items view=now, the cards deck), so it can show nothing they don't. Members and operations
    are taken verbatim from those surfaces. Groups:
      - g-needs-you (attention, at most ATTENTION_CAP): cards needing a decision, then what waits on you, by priority;
      - g-today (primary, focus): today's events, then what Now holds for today;
      - g-coming-up (supporting): tomorrow's events, then what's due soon.
    The header carries the date and titan's weather line (household, no person's data).

    **Boundary: titan's sources only.** The Mac's EventKit Reminders never leave the owner's Mac (the same principle as
    #446's bodies): the brief has no reminder source, no `reminder` member and no EventKit input. The Mac may show its
    local reminders beside this brief; they are never sent up. test_surfaces_brief.py checks this."""
    def grp(s: dict[str, Any], gid: str) -> list[dict[str, Any]]:
        return [m for g in s.get("groups") or [] if g.get("id") == gid for m in g.get("members") or []]

    tomorrow = (today + timedelta(days=1)).isoformat()
    needs_all = sorted(grp(deck, "g-needs-a-decision") + grp(items_now, "g-waiting-on-you"),
                       key=lambda m: (m.get("priority", 3), m["subject"]["id"]))
    needs = needs_all[:ATTENTION_CAP]
    todays = grp(sched, f"g-day-{today.isoformat()}") + grp(items_now, "g-today")
    coming = grp(sched, f"g-day-{tomorrow}") + grp(items_now, "g-due-soon")
    groups = [{"id": "g-needs-you", "role": "attention", "label": label("brief.needs_you"), "members": needs,
               "ordering": "priority"},
              {"id": "g-today", "role": "primary", "label": label("brief.today"), "members": todays,
               "ordering": "time", "focus": True},
              {"id": "g-coming-up", "role": "supporting", "label": label("brief.coming_up"), "members": coming,
               "ordering": "time"}]
    wanted = {a for g in groups for m in g["members"] for a in (m.get("slots") or {}).get("actions") or []}
    ops, seen = [], set()
    for s in (deck, items_now, sched):
        for o in s.get("operations") or []:
            if isinstance(o, dict) and o.get("id") in wanted and o["id"] not in seen:
                seen.add(o["id"])
                ops.append(o)
    deps = [(d["subject"]["id"], str(d.get("revision") or "")) for s in (deck, items_now, sched)
            for d in s.get("dependencies") or [] if isinstance(d, dict) and (d.get("subject") or {}).get("id")]
    shown = {m["subject"]["id"] for g in groups for m in g["members"]}
    out = envelope(resolver=BRIEF_RESOLVER, profile="temporal", purpose="orient", viewer=person, role=role,
                   rule="composition of this viewer's schedule, items (now) and cards surfaces; titan's sources only",
                   groups=groups, operations=ops, deps=[x for x in deps if x[0] in shown],
                   inputs=list(BRIEF_SOURCES) + ["today", "viewer"], now=now, lens={"today": today.isoformat()},
                   excluded=[{"rule": "attention-cap", "count": len(needs_all) - len(needs)}]
                   + [{**x, "from": src} for src, s_ in (("cards", deck), ("items", items_now))
                      for x in (s_.get("explanation") or {}).get("excluded") or []
                      if src == "items" or x.get("rule") in ("muted", "snoozed")])
    out["header"] = {"date": f"{today.strftime('%a %b')} {today.day}", "weather": weather}
    out["sources"] = {k: (v.get("provenance") or {}).get("resolver") for k, v in
                      (("schedule", sched), ("items", items_now), ("cards", deck))}
    return out


# ── an account's page (#951 step 5: GET /v1/surfaces/account?ref=) ────────────────────────────────────────────────────

ACCOUNT_RESOLVER = "account@1"
_PIECE_SIDE = {"paid": "+", "said_paid": "+", "said_handled": "+", "failed": "-", "said_unpaid": "-",
               "said_disputed": "-", "said_not_mine": "0", "due": "0"}
_STATE_SIDE = {"resolved": "+", "partly_paid": "+", "failed": "-", "overdue_unconfirmed": "-", "contested": "-",
               "due": "0", "stale": "0", "not_mine": "0"}
_STATE_TONE = {"failed": "critical", "overdue_unconfirmed": "attention", "contested": "attention",
               "partly_paid": "attention", "due": "neutral", "resolved": "positive", "stale": "info", "not_mine": "info"}
SAY_KINDS = ("paid", "disputed", "not_mine")


def _relation(piece_kind: str, state: str) -> str:
    """supports | contradicts | context: a piece against the state its thread settled on (Fundamental's evidence
    field, #951): a payment supports "paid", a failure contradicts it; a notice or someone's "not mine" is context."""
    p, s = _PIECE_SIDE.get(piece_kind, "0"), _STATE_SIDE.get(state, "0")
    if p == "0" or s == "0":
        return "context"
    return "supports" if p == s else "contradicts"


def account(threads: list[Any], ref: str, person: str | None, role: str, now: float, *,
            my_word: dict[str, Any] | None = None) -> dict[str, Any]:
    """One account's obligation timeline for this viewer, as an `explain` surface (purpose trace). `threads`: this
    viewer's own settled threads of the account (obligations, per viewer: their words, the words shared with them, their
    own device's texts), newest first. Groups: `g-now` (the current cycle's state, as attention when it needs someone),
    then `g-cycle-<day>` per cycle, newest first: every dated piece of evidence, with its source (`observed` from mail
    and records, `declared` a person's word, `derived` a text read on its owner's device) and how it bears on the state
    (`piece.relation`: supports, contradicts, context). Operations: the viewer's own word on the current cycle (paid,
    disputed, not mine), and, for a household or adults' bill, sharing their word with the adults or taking it back."""
    import obligations as O
    threads = sorted(threads, key=lambda t: t.cycle, reverse=True)
    cur = threads[0]
    vendor = cur.vendor
    bid = f"bill:{cur.account}:{cur.cycle.isoformat()}"
    ops: list[dict[str, Any]] = []
    for k in SAY_KINDS:
        ops.append(operation(f"op-say-{k}-{ref}", "set", "bill", bid, f"jellybean.bill.say@1", "POST",
                             f"/v1/obligations/{ref}/say", params={"kind": k},
                             variant="primary" if k == "paid" else "secondary"))
    word_ops = [o["id"] for o in ops]
    shareable = cur.audience in ("household", "adults")
    if shareable and my_word:
        if my_word.get("audience") == "private":
            ops.append(operation(f"op-share-{ref}", "share", "bill", bid, "jellybean.bill.share@1", "POST",
                                 f"/v1/obligations/{ref}/share", params={"yes": True}))
            word_ops.append(f"op-share-{ref}")
        else:
            ops.append(operation(f"op-take-back-{ref}", "undo", "bill", bid, "jellybean.bill.take_back@1", "POST",
                                 f"/v1/obligations/{ref}/take-back"))
            word_ops.append(f"op-take-back-{ref}")
    said = O.SAID.get(cur.state, cur.state)
    by = f", {O._handles(cur.handled_by)} handling it" if cur.handled_by and cur.state in O.OPEN else ""
    slots: dict[str, Any] = {"title": vendor, "status": {"text": said + by, "tone": _STATE_TONE.get(cur.state, "neutral")},
                             "actions": word_ops}
    if cur.amount is not None:
        slots["trailing"] = {"value": f"${cur.amount:,.2f}"}
    if cur.due:
        slots["meta"] = cur.due.isoformat()
    groups = [{"id": "g-now", "role": "attention" if cur.state in ("failed", "overdue_unconfirmed", "contested",
                                                                     "partly_paid") else "primary",
               "label": label("account.now"), "ordering": "current",
               "members": [member("bill", bid, "settled-in-code", ["pieces"], "derived", 0, slots)]}]
    deps = [(bid, f"{cur.state}:{cur.amount}")]
    for i, t in enumerate(threads):
        mem = []
        for n, p in enumerate(sorted(t.pieces, key=lambda q: (q.when, q.kind == "paid"))):
            kind = "message" if p.via == "text" else ("fact" if p.kind.startswith("said_") else "bill")
            basis = "derived" if p.via == "text" else ("declared" if p.kind.startswith("said_") else "observed")
            words = t.chain().split(" → ")[n] if n < len(t.chain().split(" → ")) else p.kind
            rel = _relation(p.kind, t.state)
            ps: dict[str, Any] = {"title": words, "meta": p.when.isoformat(),
                                  "status": {"text": rel, "tone": "attention" if rel == "contradicts" else
                                             "positive" if rel == "supports" else "neutral"}}
            if p.amount is not None:
                ps["trailing"] = {"value": f"${p.amount:,.2f}"}
            pid = f"piece:{ref}:{t.cycle.isoformat()}:{n}"
            m = member(kind, pid, {"observed": "from-mail-or-records", "declared": "a-persons-word",
                                   "derived": "a-text-on-its-owners-device"}[basis], ["pieces"], basis, 2, ps)
            m["piece"] = {"kind": p.kind, "relation": rel, "on": p.when.isoformat(),
                          **({"amount": p.amount} if p.amount is not None else {}),
                          **({"via": p.via} if p.via else {}), **({"adopted": True} if p.adopted else {})}
            mem.append(m)
        groups.append({"id": f"g-cycle-{t.cycle.isoformat()}", "role": "evidence" if i == 0 else "history",
                       "label": label("account.cycle" if i == 0 else "account.earlier",
                                      f"Billed {t.cycle:%b %-d}" if t.cycle else None),
                       "ordering": "oldest-first", "members": mem})
        deps.append((f"bill:{t.account}:{t.cycle.isoformat()}", f"{t.state}:{len(t.pieces)}"))
    return envelope(resolver=ACCOUNT_RESOLVER, profile="explain", purpose="trace", viewer=person, role=role,
                    rule="obligations per viewer (their words, words shared with them, their own device's texts)",
                    groups=groups, operations=ops, deps=deps, inputs=["records:bill", "records:receipt",
                                                                     "obligation_claims", "obligation_signals", "viewer"],
                    now=now, subject={"kind": "bill", "id": ref})
