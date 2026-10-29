#!/usr/bin/env python3
"""A person's contacts as owner-only people with hard keys (#1380; design: the #1380 comment of 10-10).

The owner's Mac posts their contacts (the reader isn't built yet). Each becomes, in the owner's own world bubble and
private to them:
  - a person entity (`ct-<hash>`): the name, and the nickname as an alias;
  - `phone_key` / `email_key` claims: hooks.keyed() of the phone's last ten digits and of the lowercased address, the
    same keys hooks give a text's sender or a mail's From, so a text or mail is attributed by key, never by name;
  - `relation` claims from the owner's own card's Related Names ("mother", "coach"), on the contact the name is;
  - a reference (references.py, kind `contact`), keyed on the Mac's contact id: what a delete removes.
The raw phone or address is keyed on arrival and kept nowhere: not a claim, an alias, a ref, a log or an error.
A field the design keeps off titan (an address, a birthday, notes, a photo…) is refused, never dropped silently.
A contact is a household member only on a shared hard key or the owner's word (graph_sync._person_same_as); a name
never fuses two people. On for each person until they turn it off (#1627: the governance capability "contacts", for
the contacts' owner); off for the house with CONTACTS_CONNECTOR=0 or the switches file. Nothing arrives until the
owner's Mac sends (its own switch, jellybean#1216).
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

FIELDS = frozenset({"id", "given", "family", "nickname", "organization", "phones", "emails"})
RELATION_FIELDS = frozenset({"label", "name"})
KIND = "contact"
MAX_CONTACTS = 5000
MAX_VALUES = 8          # phones or emails per contact


class ContactError(ValueError):
    """A refused payload. Its message names fields, never a value."""


def enabled(person: str | None = None) -> bool:
    """Whether titan takes contacts from `person` (None: whether the house has it on at all)."""
    import governance
    return governance.allowed(person, "contacts").run


def entity_for(owner: str, native_id: str) -> str:
    return "ct-" + hashlib.sha256(f"{owner}\0{native_id}".encode()).hexdigest()[:16]


def source_for(owner: str, native_id: str) -> str:
    """One world source per contact, so a delete is that source's sync with nothing."""
    return f"contacts:{entity_for(owner, native_id)}"


def display_name(c: dict[str, Any]) -> str:
    full = " ".join(p for p in (str(c.get("given") or "").strip(), str(c.get("family") or "").strip()) if p)
    return full or str(c.get("nickname") or "").strip() or str(c.get("organization") or "").strip()


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def phone_key(raw: Any) -> str | None:
    import hooks
    digits = re.sub(r"\D", "", str(raw or ""))
    return hooks.keyed("phone", digits[-10:]) if len(digits) >= 7 else None


def email_key(raw: Any) -> str | None:
    import hooks
    v = str(raw or "").strip().lower()
    return hooks.keyed("addr", v) if "@" in v and " " not in v else None


def check(contacts: list[dict[str, Any]], relations: list[dict[str, Any]]) -> None:
    """Refuse what the design keeps off titan, naming the fields only."""
    if len(contacts) > MAX_CONTACTS:
        raise ContactError(f"too many contacts in one sync (cap {MAX_CONTACTS})")
    extra = sorted({k for c in contacts for k in c} - FIELDS)
    if extra:
        raise ContactError("fields titan doesn't keep: " + ", ".join(extra))
    extra = sorted({k for r in relations for k in r} - RELATION_FIELDS)
    if extra:
        raise ContactError("relation fields titan doesn't keep: " + ", ".join(extra))
    for c in contacts:
        if not str(c.get("id") or "").strip():
            raise ContactError("a contact without its id")
        for k in ("phones", "emails"):
            if not isinstance(c.get(k) or [], list) or len(c.get(k) or []) > MAX_VALUES:
                raise ContactError(f"{k}: a list of at most {MAX_VALUES}")


def observations(owner: str, contacts: list[dict[str, Any]], relations: list[dict[str, Any]],
                 observed_at: str) -> dict[str, list[Any]]:
    """source → the world observations for each contact. Raw phones and addresses are keyed here and go no further."""
    import world
    bubble = world.bubble_name(owner)
    by_name: dict[str, list[str]] = {}
    for c in contacts:
        by_name.setdefault(_norm_name(display_name(c)), []).append(str(c["id"]))
    rel_of: dict[str, list[str]] = {}
    for r in relations:
        hit = by_name.get(_norm_name(str(r.get("name") or "")), [])
        label = str(r.get("label") or "").strip().lower()
        if len(hit) == 1 and label:                # a name two contacts share names neither: never guessed
            rel_of.setdefault(hit[0], []).append(label)
    out: dict[str, list[Any]] = {}
    for c in contacts:
        nid = str(c["id"])
        name = display_name(c)
        src = source_for(owner, nid)
        if not name:
            out[src] = []
            continue
        nick = str(c.get("nickname") or "").strip()
        base = dict(entity=entity_for(owner, nid), etype="person", name=name, observed_at=observed_at, kind="contact",
                    origin=f"contact:{entity_for(owner, nid)}", label="your contacts", owner=owner, audience="private",
                    entity_owner=owner, entity_audience="private", bubble=bubble, facet="social",
                    aliases=((nick, ""),) if nick and _norm_name(nick) != _norm_name(name) else ())
        obs = []
        for i, k in enumerate(sorted({x for x in (phone_key(p) for p in c.get("phones") or []) if x})):
            obs.append(world.Obs(attr="phone_key", value=k, ref=f"{base['entity']}#phone:{i}", **base))
        for i, k in enumerate(sorted({x for x in (email_key(e) for e in c.get("emails") or []) if x})):
            obs.append(world.Obs(attr="email_key", value=k, ref=f"{base['entity']}#email:{i}", **base))
        for label in sorted(set(rel_of.get(nid, []))):
            obs.append(world.Obs(attr="relation", value=label, qual={"of": owner}, ref=f"{base['entity']}#rel:{label}",
                                 **base))
        out[src] = obs
    return out


def _ensure_named(w, owner: str, c: dict[str, Any]) -> None:
    """A contact with a name but no phone, email or relation is still a person to its owner."""
    import world
    name = display_name(c)
    if name:
        w.ensure(entity_for(owner, str(c["id"])), "person", name, audience="private", owner=owner,
                 bubble=world.bubble_name(owner), source="contacts")


def ingest(owner: str, role: str, device_id: str, contacts: list[dict[str, Any]],
           relations: list[dict[str, Any]] | None = None, *, full: bool = False, observed_at: str = "",
           w=None, refs_path: Path | None = None) -> dict[str, int]:
    """A sync from the owner's Mac. full=True: the listing is every contact, so one it doesn't list was deleted."""
    import references
    import world
    relations = relations or []
    check(contacts, relations)
    if role not in ("admin", "adult"):
        raise ContactError("contacts come from an adult's own Mac")
    w = w or world.Bubbles(Path(os.environ.get("WORLD_DIR") or world.WORLD_DIR))
    observed_at = observed_at or world.date.today().isoformat()
    obs = observations(owner, contacts, relations, observed_at)
    n = {"contacts": len(contacts), "claims": 0, "deleted": 0}
    for c in contacts:
        src = source_for(owner, str(c["id"]))
        w.sync(src, obs[src])
        if not obs[src]:
            _ensure_named(w, owner, c)
        n["claims"] += len(obs[src])
        digest = hashlib.sha256(repr(sorted((o.attr, str(o.value)) for o in obs[src])).encode()).hexdigest()
        references.put(owner, role, device_id, {"kind": KIND, "native_id": str(c["id"]), "content_hash": digest,
                                                "title": display_name(c) or None}, path=refs_path)
    if full:
        listed = {str(c["id"]) for c in contacts}
        for nid in references.native_ids(owner, device_id, KIND, path=refs_path):
            if nid not in listed:
                delete(owner, device_id, nid, w=w, refs_path=refs_path)
                n["deleted"] += 1
    return n


def delete(owner: str, device_id: str, native_id: str, *, w=None, refs_path: Path | None = None) -> bool:
    """A contact deleted on the Mac: its claims, its entity (name and aliases), any merge it was in, and its reference."""
    import references
    import world
    w = w or world.Bubbles(Path(os.environ.get("WORLD_DIR") or world.WORLD_DIR))
    eid = entity_for(owner, native_id)
    ww = w.world(world.bubble_name(owner)) if hasattr(w, "world") else w
    ww.sync(source_for(owner, native_id), [])
    purge(ww.con, eid)
    return references.remove(references.ref_id(owner, device_id, KIND, native_id), owner, path=refs_path)


# Every row in a bubble file that names an entity or one of its claims (KB's review of #1380: a deletion leaves
# nothing). (table, column, what the column holds)
_BY_CLAIM = (("claim_log", "claim_id"), ("asks", "claim_id"), ("evidence", "claim_id"))
_BY_ENTITY = (("asks", "entity_id"), ("gaps", "entity_id"), ("mentions", "entity_id"), ("alias_learn", "entity_id"), ("prepared", "entity"),
              ("note_reviews", "entity"), ("claims", "entity_id"), ("aliases", "entity_id"), ("entities", "id"))


def purge(con, eid: str) -> None:
    """Remove every row about one entity from a bubble file: its claims' logs, asks and evidence, then the rows naming
    the entity, its merges, and the entity itself; and its id from the #366 measurement log's lists."""
    import json
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with con:
        claims = [r[0] for r in con.execute("SELECT id FROM claims WHERE entity_id=?", (eid,))] if "claims" in have else []
        for table, col in _BY_CLAIM:
            if table in have and claims:
                con.execute(f"DELETE FROM {table} WHERE {col} IN ({','.join('?' * len(claims))})", claims)
        if "merges" in have:
            con.execute("DELETE FROM merges WHERE src=? OR dst=?", (eid, eid))
        for table, col in _BY_ENTITY:
            if table in have:
                con.execute(f"DELETE FROM {table} WHERE {col}=?", (eid,))
        if "active_log" in have:
            for rid, *lists in con.execute("SELECT rowid, named, a_top, b_top FROM active_log").fetchall():
                vals = [json.loads(x or "[]") for x in lists]
                if any(any(str(v).endswith(eid) for v in xs) for xs in vals):
                    kept = [json.dumps([v for v in xs if not str(v).endswith(eid)]) for xs in vals]
                    con.execute("UPDATE active_log SET named=?, a_top=?, b_top=? WHERE rowid=?", (*kept, rid))


def same_as_member(owner: str, native_id: str, member: str, *, observed_at: str = "", w=None) -> None:
    """The owner's word: this contact is that household member. Kept in the owner's own bubble."""
    import world
    w = w or world.Bubbles(Path(os.environ.get("WORLD_DIR") or world.WORLD_DIR))
    eid = entity_for(owner, native_id)
    ww = w.world(world.bubble_name(owner)) if hasattr(w, "world") else w
    row = ww.con.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()
    if row is None:
        raise ContactError("no such contact")
    ww.sync(f"contacts-word:{eid}", [world.Obs(
        entity=eid, etype="person", name=row["name"], attr="same_as_member", value=member,
        observed_at=observed_at or world.date.today().isoformat(), kind="user", ref=f"{eid}#member",
        origin=f"user:{owner}", label="you said", owner=owner, audience="private", by=owner,
        bubble=world.bubble_name(owner))])
