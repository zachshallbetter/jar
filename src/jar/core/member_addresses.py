#!/usr/bin/env python3
"""Each household member's own email addresses, keyed, from what titan already holds (#1424, route (a)), so a calendar
attendee or a mail party given as an address resolves to that member in the relational graph (#1399).

Sources, per owner (never a mail account's credentials file: that is never opened, read or parsed):
  sent       the From address of the mail in that owner's Sent folder(s) (mail_server.db): the most frequent one, and
             every other one seen at least MIN_SEEN times (an alias they really send from). A one-off From in a Sent
             folder (a shared mailbox, a forward) isn't theirs.
  calendar   a synced calendar's owner identity (the ledger's calendar_owner) when it's an address, by the same rule.

Each address is keyed in the same expression it's read in, with hooks.keyed("addr", address.lower()), the same HMAC the
link hooks use (the salt never leaves titan). Only that key is stored, in the people row's addr_keys (kept across a
reset: it's a fact about a person, rederived the same way). No raw address is stored, logged, printed, returned or put
in an exception. Only a Sent folder's From counts (never Reply-To, never an Inbox From), and a key found for two people
(a shared mailbox) goes to neither, never guessed.

  python3 scripts/member_addresses.py            dry run: per person, how many keys from each source (counts only)
  python3 scripts/member_addresses.py --apply    store them
"""
from __future__ import annotations

import argparse
import email.utils
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

MIN_SEEN = 3
SENT_RX = re.compile(r"(^|[/.])(sent|sent messages|sent items|sent mail)$", re.I)
# Mail sent AS a role (the club's board@, a shop's info@) is the person acting for someone else, not their own address
# (KB's review, #1429): such a From is never theirs, whatever its count.
ROLE_LOCAL = frozenset("""info admin administrator board team office support hello contact contactus noreply no-reply
donotreply do-not-reply family billing accounts sales help service team members secretary treasurer president chair
staff hr jobs careers press media news newsletter notifications alerts mail postmaster webmaster hostmaster abuse
security privacy legal orders shop store bookings reservations events hello reception frontdesk front""".split())


def _addr(header: Any) -> str | None:
    """The address in one From value ("Name <a@b>", or a bare address), lowercased; None when there isn't one."""
    _, a = email.utils.parseaddr(str(header or ""))
    a = a.strip().lower()
    return a if re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", a) else None


def _read(header: Any, org_domains: set[str]) -> tuple[str, str, bool] | None:
    """One From value → (its key, its registrable domain, whether it may be someone's own), or None. The address is
    keyed here, in the one expression it's read in, and goes; only the key and the domain leave this function."""
    import hooks
    a = _addr(header)
    if not a:
        return None
    local, _, dom = a.partition("@")
    reg = hooks.registrable(dom)
    own = local.split("+", 1)[0] not in ROLE_LOCAL and reg not in org_domains
    return hooks.keyed("addr", a), reg, own


def _pick(counts: Counter, domain: dict[str, str], own: dict[str, bool]) -> tuple[dict[str, int], int]:
    """The most frequent own key, and an alias only on that one's registrable domain, seen at least MIN_SEEN times,
    never a role address or an org's domain (#1429 review: mail sent AS the club's board@ isn't Alex's address).
    → (kept {key: n}, how many aliases were skipped)."""
    mine = Counter({k: n for k, n in counts.items() if own.get(k)})
    if not mine:
        return {}, len([k for k in counts if not own.get(k)])
    import hooks
    top = mine.most_common(1)[0][0]
    if domain.get(top) in hooks.PUBLIC_MAIL:
        # a shared mail provider: "the same domain" means nothing there (Alex sending as Robin's Gmail), so no aliases
        return {top: mine[top]}, len(counts) - 1
    kept = {k: n for k, n in mine.items() if k == top or (n >= MIN_SEEN and domain.get(k) == domain.get(top))}
    return kept, len(counts) - len(kept)


def org_domains(root: Path | None = None) -> set[str]:
    """Registrable domains the world model knows as an org's (its domain/website/email claims, and domain-shaped
    aliases): mail from one of those is the org's, never a member's own."""
    import hooks
    import world
    out: set[str] = set()
    for f in sorted(Path(root or world.WORLD_DIR).glob("*.db")):
        try:
            con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            vals = [r[0] for r in con.execute("SELECT c.value FROM claims c JOIN entities e ON e.id=c.entity_id "
                                              "WHERE e.type='org' AND c.attr IN ('domain','website','email')")]
            vals += [r[0] for r in con.execute("SELECT a.alias FROM aliases a JOIN entities e ON e.id=a.entity_id "
                                               "WHERE e.type='org'")]
        except sqlite3.Error:
            vals = []
        finally:
            con.close()
        for v in vals:
            m = re.search(r"([a-z0-9-]+(?:\.[a-z0-9-]+)+)\s*$", str(v or "").lower().split("@")[-1].split("/")[0]
                          if "." in str(v or "") else "")
            if m:
                out.add(hooks.registrable(m.group(1)))
    return out


def from_sent(db: Path | None = None, orgs: set[str] | None = None) -> dict[str, tuple[dict[str, int], int]]:
    """{owner: ({key: n}, skipped)} from the From of each owner's Sent-folder mail. Keyed as it's read."""
    import mail_server
    p = Path(db or mail_server.DB)
    if not p.exists():
        return {}
    orgs = org_domains() if orgs is None else orgs
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    per: dict[str, tuple[Counter, dict[str, str], dict[str, bool]]] = {}
    try:
        for owner, folder, sender in con.execute("SELECT owner, folder, sender FROM messages"):
            if not SENT_RX.search(str(folder or "")):
                continue
            got = _read(sender, orgs)
            if got:
                c, dom, own = per.setdefault(owner, (Counter(), {}, {}))
                c[got[0]] += 1
                dom[got[0]], own[got[0]] = got[1], got[2]
    finally:
        con.close()
    return {o: _pick(*v) for o, v in per.items()}


def from_calendar(ledger: Path | None = None, orgs: set[str] | None = None) -> dict[str, tuple[dict[str, int], int]]:
    """{owner: ({key: n}, skipped)} from synced calendars' owner identity when it's an address, counted once per event
    (not per version), by the same rule."""
    import connectors_runner as C
    p = Path(ledger or C.LEDGER_PATH)
    if not p.exists():
        return {}
    orgs = org_domains() if orgs is None else orgs
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    per: dict[str, tuple[Counter, dict[str, str], dict[str, bool]]] = {}
    seen: set[tuple[str, str]] = set()
    try:
        owner_of = {sid: o for sid, o in con.execute("SELECT source_id, owner FROM items WHERE source='calendar'")}
        for sid, fields in con.execute("SELECT source_id, fields FROM versions WHERE source='calendar'"):
            o = owner_of.get(sid)
            if not o:
                continue
            try:
                got = _read(json.loads(fields or "{}").get("calendar_owner"), orgs)
            except ValueError:
                continue
            if got and (sid, got[0]) not in seen:
                seen.add((sid, got[0]))
                c, dom, own = per.setdefault(o, (Counter(), {}, {}))
                c[got[0]] += 1
                dom[got[0]], own[got[0]] = got[1], got[2]
    finally:
        con.close()
    return {o: _pick(*v) for o, v in per.items()}


def plan(store=None, mail_db: Path | None = None, ledger: Path | None = None) -> dict[str, Any]:
    """{"keys": {person: [key]}, "by_source": {source: {person: n}}, "shared": n}: every household member's keys (an
    owner not in the people store is skipped); a key that two people would get (a shared mailbox) goes to neither."""
    import household_identity as hh
    store = store or hh.get_store()
    people = {p["id"] for p in store.people()}
    orgs = org_domains()
    raw_sources = {"sent": from_sent(mail_db, orgs), "calendar": from_calendar(ledger, orgs)}
    sources = {src: {o: ks for o, (ks, _) in got.items()} for src, got in raw_sources.items()}
    skipped = sum(n for got in raw_sources.values() for o, (_, n) in got.items() if o in people)
    keys: dict[str, set[str]] = {pid: set() for pid in people}
    for got in sources.values():
        for o, ks in got.items():
            if o in keys:
                keys[o] |= set(ks)
    seen = Counter(k for ks in keys.values() for k in ks)
    shared = {k for k, n in seen.items() if n > 1}
    return {"keys": {pid: sorted(ks - shared) for pid, ks in keys.items()},
            "by_source": {src: {o: len(ks) for o, ks in got.items() if o in people} for src, got in sources.items()},
            "shared": len(shared), "skipped_alias": skipped}


def apply(p: dict[str, Any], store=None) -> int:
    """Store every member's keys (a member with none is cleared, so a dropped address goes too)."""
    import household_identity as hh
    store = store or hh.get_store()
    return sum(store.set_addr_keys(pid, ks) for pid, ks in p["keys"].items())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    p = plan()
    for src, per in p["by_source"].items():
        print(f"{src}: " + (", ".join(f"{person} {n}" for person, n in sorted(per.items())) or "none"))
    print(f"shared between people (given to neither): {p['shared']}; skipped (a role address, another domain, an "
          f"org's, or seen too rarely): {p['skipped_alias']}")
    if a.apply:
        print(f"stored {apply(p)} keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
