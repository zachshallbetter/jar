#!/usr/bin/env python3
"""The one decision for titan's own tools (#1628 slice 3; design in docs/design/governance-chokepoint.md).

`decide(tool, person)` answers whether a tool may be offered and run this turn, in the design's order, for the parts
titan enforces now:

  1. Not one of titan's tools and not a device tool: refused (no descriptor, so nothing later can be checked).
  2. Every capability turned off (the kill switch): refused, except the assistant's own domain (dates, counting,
     choices, its help), so titan can still say what's off.
  3. The tool's domain turned off: refused.
  4. The person turned the domain off for themselves (their tier is Never): refused.
  5. Otherwise run. Who may use a tool (the role table) and the Watch's short list are checked before this, in
     agent_runtime_lib._role_allows, as before.

Device tools pass through: the Mac and phone decide them with their own trust tiers. "Ask" stays where it is today
(the tools that confirm on the person's own device); per-domain ask tiers on titan come later.

Kill switches live in a small JSON file an admin edits (CAPABILITY_SWITCHES, default
/ai/config/capability_switches.json): {"all": false, "off": ["web", "mail"]}. A missing or unreadable file means
nothing is switched off. A person's own tiers live in spaces.db's capability_tiers, so delete-my-data covers them.

Capabilities (#1627, Zach 2026-10-15: "This is a per user setting"): finer than a domain, each one on for every person
until that person turns it off, in the same capability_tiers rows (keyed by the capability's id). `allowed(person, id)`
is the check a capability's own code path makes (a background job asks for the data's owner); decide() makes it for a
tool that belongs to one. A capability can still be killed for the whole house: its id (or its domain) in the switches
file's "off", or its old env flag set to exactly "0". The old flags are no longer the on-switch once a capability's
code path moves to allowed() (one PR each); until then they still gate as before.

Memory (#1690): "remembering things about you" (facts read out of chat, corrections, dev notes, a person's own edits) is
a capability too, on for everyone until they turn it off. Off, nothing new is stored for them; what's already there
can still be read, forgotten, exported and deleted. It has no old env flag: the switches file ("memory" in "off", or
everything off) is its house kill switch.
Who has a setting for it follows the memory routes' role rule (household_identity): people, never a shared screen.

CLI (an admin on titan): python3 governance.py status | tiers <person> | set <person> <domain|capability> never|always
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tool_descriptors as T

TIERS = ("always", "never")   # what titan acts on today; the Mac's ask tiers join with per-domain asks
_SWITCHES_DEFAULT = "/ai/config/capability_switches.json"
_lock = threading.Lock()
_switch_cache: dict[str, Any] = {"path": None, "mtime": None, "value": {"all": False, "off": frozenset()}}


@dataclass(frozen=True)
class Decision:
    run: bool
    why: str = ""          # a short machine reason: no_descriptor, all_off, domain_off, person_never
    message: str = ""      # what the model is told when it isn't run


RUN = Decision(True)


@dataclass(frozen=True)
class Capability:
    words: str                                 # how Settings names it
    domain: str                                # its domain's Never (and kill switch) covers it too
    tools: frozenset[str] = frozenset()        # the tools that are this capability (decide checks them)
    flag: str | None = None                    # the old env switch (see `moved`)
    background: bool = False                   # runs without a turn, for the data's owner
    moved: bool = False                        # its code path follows allowed() now (#1627, one PR each): its flag is
    #                                            only the house's kill switch (exactly "0"). Not moved yet: the flag is
    #                                            still its on-switch, so it reads as off for the house unless it's on
    route: tuple[str, str] | None = None       # a person-scoped route (method, path): a role has a setting for this
    #                                            capability when household_identity lets it call that route


CAPABILITIES: dict[str, Capability] = {
    "mail_moves": Capability("moving, archiving and marking your mail on the mail servers, and unsubscribing", "mail",
                             frozenset({"mail_move_preview", "mail_move", "mail_move_undo", "mail_unsubscribe_preview",
                                        "mail_unsubscribe"}), "MAIL_SERVER_WRITES",
                             moved=True),
    "mail_push": Capability("new mail noticed the moment it arrives", "mail", flag="MAIL_IDLE", background=True),
    "contacts": Capability("your contacts in titan's knowledge base", "records", flag="CONTACTS_CONNECTOR", background=True,
                           moved=True),
    "code_sandbox": Capability("running code in titan's sandbox", "compute", frozenset({"run_python"}), "ALLOW_CODE_EXECUTION",
                               moved=True),
    "inbox_proposals": Capability("suggestions for titan's picture of the house from your inbox", "records",
                                  flag="WORLD_INBOX_PROPOSALS", background=True, moved=True),
    # Not here: SCAN_JUNK_LIVE / SCAN_ENVELOPE_LIVE. They only let the scans benchmark call the model; no live scan
    # source exists yet (scan_review.py), so a Settings switch for them would do nothing. They join when one does.
    "task_chain": Capability("quick steps of your durable tasks run back to back", "tasks", flag="TASK_CHAIN",
                             background=True),
    "lock_up": Capability("locking up: the locks and the alarm", "homeSecurity", frozenset({"lock_up"}), "HA_LOCK_UP"),
    # #1690: no env flag; the switches file's "off" (or "all") is the house's kill switch. The assistant's domain, so a
    # person's own memory is never caught by another domain's Never.
    "memory": Capability("remembering things about you: facts, corrections and your notes about titan", "assistant",
                         moved=True, route=("GET", "/v1/memory/facts")),
}
_TOOL_CAPABILITY = {t: cid for cid, c in CAPABILITIES.items() for t in c.tools}


def _switches_path() -> Path:
    return Path(os.environ.get("CAPABILITY_SWITCHES") or _SWITCHES_DEFAULT)


def switches() -> dict[str, Any]:
    """{"all": bool, "off": frozenset of domains}; re-read only when the file changes."""
    p = _switches_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"all": False, "off": frozenset()}
    with _lock:
        c = _switch_cache
        if c["path"] == str(p) and c["mtime"] == mtime:
            return c["value"]
        try:
            raw = json.loads(p.read_text())
        except (OSError, ValueError):
            raw = {}
        value = {"all": raw.get("all") is True,
                 "off": frozenset(d for d in (raw.get("off") or [])
                                  if isinstance(d, str) and (d in T.DOMAINS or d in CAPABILITIES))}
        c.update(path=str(p), mtime=mtime, value=value)
        return value


def _db(path: Path | None = None) -> sqlite3.Connection:
    import spaces
    con = spaces._db(path)
    con.execute("CREATE TABLE IF NOT EXISTS capability_tiers (person TEXT NOT NULL, domain TEXT NOT NULL, "
                "tier TEXT NOT NULL, updated_at REAL, PRIMARY KEY (person, domain))")
    return con


def tiers(person: str | None, path: Path | None = None) -> dict[str, str]:
    """The person's own tiers, {domain: tier}; none set means every domain is on."""
    if not person:
        return {}
    con = _db(path)
    try:
        return {d: t for d, t in con.execute("SELECT domain, tier FROM capability_tiers WHERE person=?", (person,))}
    finally:
        con.close()


def set_tier(person: str, domain: str, tier: str, path: Path | None = None) -> dict[str, str]:
    """Set one of the person's own tiers. Always removes the row (on is the default)."""
    if domain not in T.DOMAINS and domain not in CAPABILITIES:
        raise ValueError(f"unknown domain or capability {domain!r}")
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    con = _db(path)
    try:
        with con:
            if tier == "always":
                con.execute("DELETE FROM capability_tiers WHERE person=? AND domain=?", (person, domain))
            else:
                con.execute("INSERT INTO capability_tiers (person, domain, tier, updated_at) VALUES (?,?,?,?) "
                            "ON CONFLICT(person, domain) DO UPDATE SET tier=excluded.tier, updated_at=excluded.updated_at",
                            (person, domain, tier, time.time()))
    finally:
        con.close()
    return tiers(person, path)


def killed(capability: str) -> bool:
    """Off for the whole house: the switches file (everything, its domain or its id); for a moved capability its old flag
    exactly "0"; for one not moved yet, its old flag anything but on (its code still needs it, so that's the truth)."""
    c = CAPABILITIES[capability]
    sw = switches()
    # "all" off kills every capability, the assistant's domain's too (memory, #1690): only talking is left
    if sw["all"] or c.domain in sw["off"] or capability in sw["off"]:
        return True
    if not c.flag:
        return False
    v = os.environ.get(c.flag, "").strip().lower()
    return v == "0" if c.moved else v not in ("1", "true", "yes", "on")


def allowed(person: str | None, capability: str, *, person_tiers: dict[str, str] | None = None,
            path: Path | None = None) -> Decision:
    """Whether a capability may act for `person` (the turn's person, or the data's owner for a background job): not
    killed for the house, and neither it nor its domain turned off by that person. No person (a system job): only the
    house's switches apply."""
    c = CAPABILITIES[capability]
    if killed(capability):
        return Decision(False, "capability_off", f"Not run: {c.words} is switched off on titan for now.")
    if person_tiers is not None:
        mine = person_tiers
    elif person:
        try:
            mine = tiers(person, path)
        except Exception:  # noqa: BLE001  (their settings can't be read: closed, never their Off ignored)
            return Decision(False, "unreadable", f"Not run: titan couldn't read this person's settings for {c.words}.")
    else:
        mine = {}
    if mine.get(capability) == "never" or mine.get(c.domain) == "never":
        return Decision(False, "person_never", f"Not run: this person has turned off {c.words}. "
                                               "They can turn it back on in Settings.")
    return RUN


def for_role(role: str | None) -> list[str]:
    """The capabilities a person with this role has a setting for: background ones (their own data) always; a tool's
    only when the role may use one of its tools (a kid has no lock-up switch to show); a route's only when the role may
    call that route (memory: people, never a shared screen)."""
    import household_identity as hi
    return [cid for cid, c in CAPABILITIES.items()
            if c.background or any(hi.role_allows_tool(role, t) for t in c.tools)
            or (c.route is not None and hi.route_allowed(role, *c.route))]


def decide(tool: str, *, person_tiers: dict[str, str] | None = None, device_tools: Any = ()) -> Decision:
    """Whether titan may offer and run `tool` for a person whose tiers are `person_tiers`."""
    d = T.lookup(tool)
    if d is None:
        if tool in device_tools:
            return RUN   # the Mac or phone decides
        return Decision(False, "no_descriptor", f"Not run: {tool} isn't a tool titan knows.")
    sw = switches()
    words = T.DOMAINS.get(d.domain, d.domain)
    if sw["all"] and d.domain != "assistant":
        return Decision(False, "all_off", "Not run: titan's tools are switched off for now (only talking works).")
    if d.domain in sw["off"]:
        return Decision(False, "domain_off", f"Not run: {words} is switched off on titan for now.")
    if (person_tiers or {}).get(d.domain) == "never":
        return Decision(False, "person_never", f"Not run: this person has turned off {words}. They can turn it back on in Settings.")
    cap = _TOOL_CAPABILITY.get(tool)
    if cap:
        return allowed(None, cap, person_tiers=person_tiers or {})
    return RUN


def needs_hold(tool: str) -> bool:
    """An always_ask tool the runtime must hold for the person's OK before it runs: every always_ask tool except those
    that confirm on the owner's own device by themselves (tool_descriptors.CONFIRMS_ON_DEVICE)."""
    d = T.lookup(tool)
    return bool(d and d.always_ask) and tool not in T.CONFIRMS_ON_DEVICE


def _main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("status", "tiers", "set"):
        print(__doc__.split("CLI", 1)[1].strip())
        return 2
    if argv[0] == "status":
        sw = switches()
        print(json.dumps({"switches_file": str(_switches_path()), "all_off": sw["all"], "off": sorted(sw["off"]),
                          "capabilities_killed": sorted(c for c in CAPABILITIES if killed(c))}))
        return 0
    if argv[0] == "tiers" and len(argv) == 2:
        print(json.dumps(tiers(argv[1]), sort_keys=True))
        return 0
    if argv[0] == "set" and len(argv) == 4:
        try:
            print(json.dumps(set_tier(argv[1], argv[2], argv[3]), sort_keys=True))
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        return 0
    print("usage: governance.py status | tiers <person> | set <person> <domain|capability> never|always", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
