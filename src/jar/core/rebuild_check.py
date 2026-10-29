#!/usr/bin/env python3
"""What each viewer sees, in counts only: a snapshot before and after titan's household data is rebuilt (10-09, Zach's
wipe-and-rebuild). Read-only. Never a title, a name or an id in its output: viewers are labelled by role.

For four viewers (the admin, an adult, a kid, the shared screen), taken from the people store, it calls the surface
routes in-process (a FastAPI TestClient on agent_api_server, without its startup events, so no discovery, timers or
pushes start) with the viewer's identity put where the auth middleware resolves it (`_resolve_identity`): the routes'
own role checks and per-viewer filters run as for that person's device. No token is minted and no device row is written.
  cards              GET /v1/proactive
  items now/time/unsorted   GET /v1/items?view=…
  threads            GET /v1/threads
  documents          GET /v1/documents
  schedule           GET /v1/schedule
  brief              GET /v1/brief
  kb:<q>             the agent's own kb_retrieve for the viewer (a test-source turn: the privacy filter a turn uses),
                     for 5 fixed generic queries ("bill", "dentist", "school", "trip", "package"); hits counted
  mail:bills 14d, mail:suggestions 14d   cards from mail (sources mail-bills, mail-suggestion) created in the last 14
                     days that this viewer may see (proactive.visible_to), acked or expired ones included
A route a role may not call shows as "403" (the middleware's own refusal), and one that fails as "err".

Outcomes (household-wide, counts only), for the cases Zach named:
  thread             mail matching --thread, a word in the subject or in the sender's domain (KB's rule; the domain is
                     never an argument or printed; --sender-domain only adds an exact one): live in the
                     connectors ledger, in the KB (its source list), owner-only (the admin's, audience private), anyone
                     else's, and held by mail triage by category (never indexed: newsletter, record, …); then the same
                     thread in mail_server.db (the server mail store, the one store after the cut-over): messages (one
                     per message, not per folder), derived, the admin's, KB-flagged, in the KB's list, by triage
                     category and by passes outcome
  calendar           the admin's live calendar items in the ledger against the Mac's tracked count (--mac-calendar N and
                     --mac-calendar-window W, from `rebuild_check.py mac-count` run on the Mac): the full counts, and
                     both inside the Mac's 30-day window less a day's margin (the Mac drops older occurrences from
                     tracking but they stay on titan), whose difference should be 0 after the epoch resend
  compare            before → after per viewer; any increase for the kid or the screen is flagged (exit 1)
  references         (--references [N], after the rebuild) KB's references.live_check (#776's ITEM_REFERENCES check)
                     per viewer: tried/opened/refused/card/errors and a verdict (owner opens every one; another adult
                     opens none and each try makes a share card; a kid or a screen opens none, no card, all refused);
                     a FAIL or err is flagged by compare
The agent's retrieval trace goes to a throwaway file (RETRIEVAL_TRACE_DB); the retrieval runtime's own query log is the
only thing outside it that records a query. Run under the agent unit's environment (KB's /ai/tmp/kb_agentenv.py) so the
stores and flags are live's.

  python3 scripts/rebuild_check.py [--label before|after] [--json OUT] [--thread swaia] [--sender-domain D] [--mac-calendar N]
  python3 scripts/rebuild_check.py compare BEFORE.json AFTER.json
  python3 scripts/rebuild_check.py mac-count          (on the Mac: Jellybean's tracked/outbox/gave-up counts per source)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("AGENT_TURN_SOURCE", "test")   # never a person's turn: no memory, no cards, no stats
import tempfile  # noqa: E402
# retrieval writes a trace row per query (/ai/logs/retrieval-trace.db): into a throwaway file here, so this stays read-only
os.environ["RETRIEVAL_TRACE_DB"] = str(Path(tempfile.mkdtemp(prefix="rebuild-check-")) / "trace.db")

ROUTES = (("cards", "/v1/proactive"), ("items now", "/v1/items?view=now"), ("items time", "/v1/items?view=time"),
          ("items unsorted", "/v1/items?view=unsorted"), ("threads", "/v1/threads"), ("documents", "/v1/documents"),
          ("schedule", "/v1/schedule"), ("brief", "/v1/brief"))
KB_QUERIES = ("bill", "dentist", "school", "trip", "package")
_LIST_KEYS = ("cards", "items", "members", "threads", "documents", "events", "entries", "results")


def count(body: Any) -> int | str:
    """How many things a surface answer shows: the first list under a known key, a schedule's events (per person and
    the household's), else every group's members. An answer with nothing countable but a short status code (the threads
    view's "off") shows as "status:<code>", never its message."""
    if isinstance(body, list):
        return len(body)
    if not isinstance(body, dict):
        return 0
    if isinstance(body.get("people"), list) and any(isinstance(x, dict) and "events" in x for x in body["people"]):
        return (sum(len(x.get("events") or []) for x in body["people"] if isinstance(x, dict))
                + len((body.get("household") or {}).get("events") or []))
    for k in _LIST_KEYS:
        if isinstance(body.get(k), list):
            return len(body[k])
    groups = body.get("groups") or (body.get("surface") or {}).get("groups") if isinstance(body.get("surface"), dict) else body.get("groups")
    if isinstance(groups, list):
        return sum(len(g.get("members") or []) for g in groups if isinstance(g, dict))
    for k in ("days", "sections"):
        if isinstance(body.get(k), list):
            return sum(c for c in (count(x) for x in body[k]) if isinstance(c, int))
    st = body.get("status")
    if isinstance(st, str) and re.fullmatch(r"[a-z_-]{1,20}", st):
        return f"status:{st}"
    return 0


def kb_hits(text: str) -> int:
    """kb_retrieve's answer: one line per hit starting with a bullet or a number; 0 for its "nothing found"."""
    lines = [l for l in str(text or "").splitlines() if l.strip()]
    return sum(1 for l in lines if l.lstrip()[:1] in ("•", "-", "*") or l.lstrip()[:2].rstrip(".").isdigit())


MAIL_CARD_SOURCES = (("mail:bills 14d", "mail-bills"), ("mail:suggestions 14d", "mail-suggestion"))
WINDOW_S = 14 * 86400
WATCH = ("kid", "screen")   # an increase for either is flagged by compare


def mail_cards(person_id: str, role: str, db: Path | None = None, now: float | None = None) -> dict[str, int]:
    """This viewer's mail cards over the last 14 days, by source, read-only from proactive.db (acked and expired too)."""
    import sqlite3
    import proactive
    now = time.time() if now is None else now
    path = Path(db or proactive.DB_PATH)
    out = {k: 0 for k, _ in MAIL_CARD_SOURCES}
    if not path.exists():
        return out
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT source, audience FROM cards WHERE created_at >= ?", (now - WINDOW_S,)).fetchall()
    finally:
        con.close()
    for src, aud in rows:
        for key, want in MAIL_CARD_SOURCES:
            if src == want and proactive.visible_to(aud, person_id, role):
                out[key] += 1
    return out


def _domain(sender: Any) -> str:
    """The sender's domain: the address part of `"Name" <local@domain>` (or a bare address), after its last "@"."""
    m = re.match(r'\s*"?([^<"]*)"?\s*<?([^>]*)>?', str(sender or ""))
    addr = (m.group(2) if m else "") or str(sender or "")
    return addr.rpartition("@")[2].strip().lower().rstrip(".") if "@" in addr else ""


def matches(subject: Any, sender: Any, thread: str | None, domain: str | None = None) -> bool:
    """A mail is the thread's (KB's rule, 10-09): the word in its subject, or the word in its sender's domain (a word
    boundary stops at dots: thread.org and mail.thread.org match, notthread.org doesn't), case-insensitive. The domain
    is never passed in or printed, so it never sits in a process's argv; `domain` (optional, --sender-domain) adds an
    exact domain or its subdomains."""
    d = _domain(sender)
    if thread:
        rx = re.compile(r"\b" + re.escape(thread) + r"\b", re.I)
        if rx.search(str(subject or "")) or (d and rx.search(d)):
            return True
    return bool(domain and d and (d == domain.lower() or d.endswith("." + domain.lower())))


_CODE_RX = re.compile(r"[a-z_]{1,20}")


def server_outcome(thread: str | None, domain: str | None, admin: str, kb_ids: set[str] | None,
                   db: Path | None) -> dict[str, Any]:
    """The thread in mail_server.db (the server mail store, the one mail store after the cut-over), counts only, one per
    message (a mail in two folders counts once): messages, derived, the admin's, marked in the KB, in the KB's source
    list, and by triage category and passes outcome."""
    import sqlite3
    out: dict[str, Any] = {"server messages": 0, "server derived": 0, "server admin's": 0, "server kb flag": 0,
                           "server in kb": 0 if kb_ids is not None else "err"}
    if not db or not db.exists():
        out["server messages"] = "-"
        return out
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        q = ("SELECT owner, account, message_id, item_id, subject, sender, derived_at, kb, "
             + ("category" if "category" in cols else "NULL") + ", " + ("passes" if "passes" in cols else "NULL")
             + " FROM messages")
        seen: set[tuple[str, str, str]] = set()
        for owner, account, mid, item_id, subject, sender, derived_at, kb, cat, passes in con.execute(q):
            if (owner, account, mid) in seen or not matches(subject, sender, thread, domain):
                continue
            seen.add((owner, account, mid))
            out["server messages"] += 1
            out["server derived"] += derived_at is not None
            out["server admin's"] += owner == admin
            out["server kb flag"] += bool(kb)
            if kb_ids is not None and str(item_id) in kb_ids:
                out["server in kb"] += 1
            for name, v in (("category", cat), ("passes", passes)):
                v = v or "none"
                if _CODE_RX.fullmatch(str(v)):
                    out[f"server {name} {v}"] = out.get(f"server {name} {v}", 0) + 1
    finally:
        con.close()
    return out


def thread_outcome(thread: str | None, domain: str | None, admin: str, kb_ids: set[str] | None,
                   ledger: Path, triage: Path | None, server: Path | None = None) -> dict[str, Any]:
    """Counts only: the thread's mail in the ledger (live), in the KB, owner-only (admin + private), anyone else's, and
    what mail triage holds of it by category; then the same thread in mail_server.db (server_outcome). kb_ids None: the
    KB's list couldn't be read ("err")."""
    import sqlite3
    out: dict[str, Any] = {"ledger live": 0, "in kb": 0 if kb_ids is not None else "err", "owner-only": 0, "not owner-only": 0}
    if ledger.exists():
        con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
        try:
            latest: dict[str, tuple[float, Any, Any, Any, Any]] = {}
            for sid, title, fields, owner, aud, at in con.execute(
                    "SELECT source_id, title, fields, owner, audience, at FROM versions WHERE source='mail'"):
                if sid not in latest or at >= latest[sid][0]:
                    latest[sid] = (at, title, fields, owner, aud)
            for sid, item_id, kb, owner in con.execute(
                    "SELECT source_id, item_id, kb, owner FROM items WHERE source='mail' AND deleted_at IS NULL"):
                v = latest.get(sid)
                try:
                    f = json.loads(v[2] or "{}") if v else {}
                except ValueError:
                    f = {}
                if not matches((v[1] if v else "") or f.get("subject"), f.get("from"), thread, domain):
                    continue
                out["ledger live"] += 1
                if kb_ids is not None and str(item_id) in kb_ids:
                    out["in kb"] += 1
                if (owner or (v[3] if v else None)) == admin and (v[4] if v else "private") == "private":
                    out["owner-only"] += 1
                else:
                    out["not owner-only"] += 1
        finally:
            con.close()
    if triage and triage.exists():
        con = sqlite3.connect(f"file:{triage}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(queue)")}
            q = "SELECT title, " + ("sender" if "sender" in cols else "NULL") + ", category FROM queue"
            for title, sender, cat in con.execute(q):
                if matches(title, sender, thread, domain):
                    key = f"triage {cat or 'waiting'}"
                    if re.fullmatch(r"triage [a-z_]{1,20}", key):
                        out[key] = out.get(key, 0) + 1
        finally:
            con.close()
    if server is not None:
        out.update(server_outcome(thread, domain, admin, kb_ids, server))
    return out


# The Mac tracks calendar occurrences inside its sync window (jellybean CalendarMapping.windowBack, 30 days back) and
# drops an older one from tracking without a tombstone: it stays on titan, so the whole ledger runs ahead of the Mac as
# events age out (10-10: 168 vs 166, two occurrences just past the edge). Both sides are compared inside the window less
# a day's margin, so an occurrence at the edge doesn't flap between checks; the full counts are shown alongside.
CAL_WINDOW_S, CAL_MARGIN_S = 30 * 86400, 86400
_OCC_RX = re.compile(r"@(\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:\.\d+)?)(Z|[+-]\d\d:?\d\d)?$")


def occurrence(source_id: Any) -> float | None:
    """The occurrence start in a calendar source_id ("<event>@2026-09-10T00:15:00Z"), as epoch seconds; None if absent."""
    from datetime import datetime, timezone
    m = _OCC_RX.search(str(source_id or ""))
    if not m:
        return None
    try:
        tz = (m.group(2) or "Z").replace("Z", "+00:00")
        tz = tz if ":" in tz else tz[:3] + ":" + tz[3:]                      # titan's Python 3.10: +hh:mm only
        dt = datetime.fromisoformat(m.group(1).split(".")[0] + tz)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def in_window(ids: Any, now: float | None = None) -> int:
    """How many calendar ids fall inside the Mac's window less the margin; an id without a date counts (it can't
    have aged out)."""
    cut = (time.time() if now is None else now) - (CAL_WINDOW_S - CAL_MARGIN_S)
    return sum(1 for i in ids if (occurrence(i) is None or occurrence(i) >= cut))


def calendar_outcome(admin: str, mac_tracked: int | None, ledger: Path, mac_window: int | None = None,
                     now: float | None = None) -> dict[str, Any]:
    """The admin's live calendar items in the ledger against the Mac's tracked ones: the full counts, and (with the
    Mac's in-window count) both inside the window, whose difference should be 0."""
    import sqlite3
    ids: list[str] = []
    if ledger.exists():
        con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
        try:
            ids = [r[0] for r in con.execute("SELECT source_id FROM items WHERE source='calendar' AND deleted_at IS NULL "
                                             "AND owner=?", (admin,))]
        finally:
            con.close()
    out: dict[str, Any] = {"ledger live (admin)": len(ids), "mac tracked": mac_tracked if mac_tracked is not None else "-"}
    if mac_window is not None:
        w = in_window(ids, now)
        out.update({"ledger in window": w, "mac in window": mac_window, "difference": mac_window - w})
    elif mac_tracked is not None:
        out["difference"] = mac_tracked - len(ids)
    return out


def mac_count(base: Path | None = None, now: float | None = None) -> dict[str, dict[str, int]]:
    """On the Mac: Jellybean's per-source sync state, counts only (tracked items, outbox, gave-up)."""
    base = base or Path.home() / "Library/Application Support/jellybean/sync"
    out = {}
    for f in sorted(base.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and "tracked" in d:
            out[f.stem] = {"tracked": len(d.get("tracked") or {}), "outbox": len(d.get("outbox") or []),
                           "gave_up": len(d.get("gaveUpItems") or {})}
            if f.stem == "calendar":
                out[f.stem]["in_window"] = in_window(d.get("tracked") or {}, now)
    return out


def compare(before: dict[str, Any], after: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(lines, flags): each viewer's row before → after; a flag for every number that went up for the kid or the
    screen (a code like "403" turning into a number counts as up)."""
    lines, flags = [], []
    bv, av = before.get("viewers") or {}, after.get("viewers") or {}
    for viewer in list(dict.fromkeys([*bv, *av])):
        lines.append(f"== {viewer} ==")
        b, a = bv.get(viewer) or {}, av.get(viewer) or {}
        for k in list(dict.fromkeys([*b, *a])):
            x, y = b.get(k, "-"), a.get(k, "-")
            up = (isinstance(y, int) and (y > x if isinstance(x, int) else y > 0))
            mark = "  << UP" if up and viewer in WATCH else ""
            if mark:
                flags.append(f"{viewer} {k}: {x} -> {y}")
            lines.append(f"  {k:<22} {x!s:>8} -> {y!s:<8}{mark}")
    for name in ("thread", "calendar"):
        b, a = (before.get("outcomes") or {}).get(name) or {}, (after.get("outcomes") or {}).get(name) or {}
        if a or b:
            lines.append(f"== {name} ==")
            lines.extend(f"  {k:<22} {b.get(k, '-')!s:>8} -> {a.get(k, '-')!s:<8}" for k in dict.fromkeys([*b, *a]))
    for viewer, row in ((after.get("outcomes") or {}).get("references") or {}).items():
        lines.append(f"== references: {viewer} ==")
        lines.extend(f"  {k:<22} {v}" for k, v in row.items())
        if row.get("verdict") in ("FAIL", "err"):
            flags.append(f"references {viewer}: {row['verdict']}")
    return lines, flags


REF_VIEWERS = ("owner", "other_adult", "kid", "screen")
REF_FIELDS = ("tried", "opened", "refused", "card", "errors")


def references_verdict(res: Any) -> dict[str, dict[str, Any]]:
    """KB's references.live_check result (#776's ITEM_REFERENCES check), counts only, with a verdict per viewer:
    owner      every reference it could reach was read: opened == tried − device_offline (references on the owner's Mac
               aren't reached out of a turn) and opened > 0; any other error reason fails; nothing sampled, or only
               Mac-held ones, is never a pass
    other_adult  opened 0 (card is information: server mail is private, so another adult is refused without one; a card
               appears only for a shared device reference)
    kid, screen  opened 0, no card, every try refused (#446 v2 §3: refused before any card)
    Anything not an int is shown as "-"; error reasons only as short codes."""
    out: dict[str, dict[str, Any]] = {}
    for v in REF_VIEWERS:
        r = (res or {}).get(v) if isinstance(res, dict) else None
        if not isinstance(r, dict):
            out[v] = {"verdict": "-"}
            continue
        row: dict[str, Any] = {k: (r.get(k) if isinstance(r.get(k), int) and not isinstance(r.get(k), bool) else "-")
                               for k in REF_FIELDS}
        for code, n in sorted((r.get("error_reasons") or {}).items()) if isinstance(r.get("error_reasons"), dict) else ():
            if re.fullmatch(r"[a-z_]{1,24}", str(code)) and isinstance(n, int):
                row[f"error {code}"] = n
        t, o, rf, c = (row[k] for k in ("tried", "opened", "refused", "card"))
        if "-" in (t, o, rf, c):
            row["verdict"] = "-"
        elif t == 0:
            row["verdict"] = "nothing sampled"
        elif v == "owner":
            off = row.get("error device_offline", 0)
            other = (row["errors"] - off) if isinstance(row["errors"], int) else 1
            row["verdict"] = ("FAIL" if other > 0 or o != t - off else "nothing read (all on the Mac)" if o == 0 else "pass")
        elif v == "other_adult":
            row["verdict"] = "pass" if o == 0 else "FAIL"
        else:
            row["verdict"] = "pass" if o == 0 and c == 0 and rf == t else "FAIL"
        out[v] = row
    return out


def references_check(sample: int = 5) -> dict[str, dict[str, Any]]:
    """Call KB's in-process checker if this titan has it; "-" rows (no verdict) when it doesn't, "err" when it fails."""
    try:
        import references
        fn = getattr(references, "live_check", None)
    except Exception:  # noqa: BLE001
        fn = None
    if fn is None:
        return references_verdict(None)
    try:
        return references_verdict(fn(sample=sample))
    except Exception:  # noqa: BLE001
        return {v: {"verdict": "err"} for v in REF_VIEWERS}


def viewers(store) -> list[tuple[str, dict, dict | None]]:
    """(label, person, a device of theirs or None): the admin, the first adult, the first kid, the first screen."""
    import household_identity as hh
    people = store.people()
    out = []
    for label, pick in (("admin", lambda p: p["id"] == hh.ADMIN_PERSON_ID), ("adult", lambda p: p.get("role") == "adult"),
                        ("kid", lambda p: p.get("role") == "kid"), ("screen", lambda p: p.get("role") == "household")):
        p = next((x for x in people if pick(x)), None)
        if p:
            dev = next((d for d in p.get("devices") or [] if not d.get("revoked")), None)
            out.append((label, {"id": p["id"], "name": p.get("name") or "", "role": p.get("role")}, dev))
        elif label == "screen":   # no shared screen paired: the household identity (KB's live check does the same)
            out.append((label, {"id": "household", "name": "", "role": "household"}, None))
    return out


def snapshot() -> dict[str, dict[str, Any]]:
    from fastapi.testclient import TestClient
    import agent_api_server as api
    import agent_runtime_lib as lib
    import household_identity as hh
    store = hh.get_store()
    client = TestClient(api.app)            # no `with`: startup events (discovery, timers) never run
    real = api._resolve_identity
    out: dict[str, dict[str, Any]] = {}
    try:
        for label, person, dev in viewers(store):
            role = person["role"] if label != "admin" else "admin"
            ident = hh.Identity(person=person, device={"id": dev["id"], "name": dev.get("name") or "", "platform": dev.get("platform") or ""}
                                if dev else None, role=role, via="device" if dev else "master")
            api._resolve_identity = lambda request, _i=ident: _i
            row: dict[str, Any] = {}
            for name, path in ROUTES:
                try:
                    r = client.get(path)
                    row[name] = count(r.json()) if r.status_code == 200 else str(r.status_code)
                except Exception:  # noqa: BLE001
                    row[name] = "err"
            try:
                row.update(mail_cards(person["id"], role))
            except Exception:  # noqa: BLE001
                row.update({k: "err" for k, _ in MAIL_CARD_SOURCES})
            lib._TURN_IO.person, lib._TURN_IO.role = person, role
            _role = lib.turn_role
            lib.turn_role = lambda _r=role: _r
            try:
                for q in KB_QUERIES:
                    try:
                        row[f"kb:{q}"] = kb_hits(lib.execute_tool("kb_retrieve", {"query": q}))
                    except Exception:  # noqa: BLE001
                        row[f"kb:{q}"] = "err"
            finally:
                lib.turn_role = _role
                lib._TURN_IO.person = lib._TURN_IO.role = None
            out[label] = row
    finally:
        api._resolve_identity = real
    return out


def outcomes(thread: str | None, domain: str | None, mac_calendar: int | None,
             references_sample: int | None = None, mac_window: int | None = None) -> dict[str, dict[str, Any]]:
    import connectors_runner as C
    import household_identity as hh
    import mail_server
    import mail_triage
    import agent_runtime_lib as lib
    try:
        kb_ids: set[str] | None = {str(x.get("source_id")) for x in lib.kb_sources()}
    except Exception:  # noqa: BLE001
        kb_ids = None
    ledger = Path(C.LEDGER_PATH)
    return {"thread": thread_outcome(thread, domain, hh.ADMIN_PERSON_ID, kb_ids, ledger, Path(mail_triage.DB),
                                     Path(mail_server.DB)),
            "calendar": calendar_outcome(hh.ADMIN_PERSON_ID, mac_calendar, ledger, mac_window),
            **({"references": references_check(references_sample)} if references_sample else {})}


def render(snap: dict[str, dict[str, Any]], label: str, outs: dict[str, dict[str, Any]] | None = None) -> str:
    keys = [n for n, _ in ROUTES] + [k for k, _ in MAIL_CARD_SOURCES] + [f"kb:{q}" for q in KB_QUERIES]
    lines = [f"rebuild check ({label}) {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())}"]
    for viewer, row in snap.items():
        lines.append(f"== {viewer} ==")
        lines.extend(f"  {k:<22} {row.get(k, '-')}" for k in keys)
    for name, row in (outs or {}).items():
        if name == "references":
            for viewer, r in row.items():
                lines.append(f"== references: {viewer} ==")
                lines.extend(f"  {k:<22} {v}" for k, v in r.items())
            continue
        lines.append(f"== {name} ==")
        lines.extend(f"  {k:<22} {v}" for k, v in row.items())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["mac-count"]:
        for src, c in mac_count().items():
            print(f"{src:<14} tracked {c['tracked']}  outbox {c['outbox']}  gave up {c['gave_up']}"
                  + (f"  in window {c['in_window']}" if "in_window" in c else ""))
        return 0
    if argv[:1] == ["compare"]:
        lines, flags = compare(json.loads(Path(argv[1]).read_text()), json.loads(Path(argv[2]).read_text()))
        print("\n".join(lines))
        print(("FLAGGED for the kid or the screen: " + "; ".join(flags)) if flags else "kid and screen: nothing went up")
        return 1 if flags else 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="before")
    ap.add_argument("--json")
    ap.add_argument("--thread", default="swaia", help="a word in the thread's subjects")
    ap.add_argument("--sender-domain", default=None, help="optional; the thread word in a sender's domain already matches")
    ap.add_argument("--mac-calendar", type=int, default=None, help="the Mac's tracked calendar count (mac-count)")
    ap.add_argument("--mac-calendar-window", type=int, default=None,
                    help="the Mac's tracked calendar count inside its window (mac-count's 'in window')")
    ap.add_argument("--references", type=int, nargs="?", const=5, default=None,
                    help="run KB's ITEM_REFERENCES live check with this sample (default 5); after the rebuild")
    a = ap.parse_args(argv)
    snap = snapshot()
    outs = outcomes(a.thread, a.sender_domain, a.mac_calendar, a.references, a.mac_calendar_window)
    print(render(snap, a.label, outs))
    if a.json:
        Path(a.json).write_text(json.dumps({"label": a.label, "at": time.time(), "viewers": snap, "outcomes": outs}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
