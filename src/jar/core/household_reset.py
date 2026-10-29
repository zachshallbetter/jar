#!/usr/bin/env python3
"""Wipe household data that titan derived from its sources, and rebuild it from those sources (Zach, 10-09: "all
of it exists at the sources anyway"). Device configs, people, grants, fences, the person's own words and everything
person-made are kept. Anything this script isn't sure of is KEPT and listed.

    python3 scripts/household_reset.py                    # --dry-run (default): per store and table, the action and
                                                          #   the row count. Counts only; nothing is written.
    python3 scripts/household_reset.py --coverage [DAY …] # what titan holds per source for those days (counts only)
    python3 scripts/household_reset.py --apply [--keep-device-items]
        Refuses unless the agent API and every pass are stopped (systemd). Then a named backup
        (/ai/datasets/backups/stores/pre-reset-<ts>: every SQLite store touched, and pg_dump of retrieval_runtime and
        record_runtime), then the wipe. The KB and records are wiped through their runtimes' own DELETE routes, one
        source or node at a time, with its sharing row (kb_privacy.drop_acl).
        Items a person's Mac pushed (the calendar and Mac mail connectors: ledger rows, their KB copies and record
        nodes, text hooks) are wiped too, and the ledger epoch is bumped last: each Mac sees the new epoch on its next
        sync (GET /v1/connectors/tombstones, every ingest answer) and sends everything again (jellybean#1157).
        --keep-device-items keeps them, for a Mac that can't resend yet.
    python3 scripts/household_reset.py --rebuild [--days 90]
        Runs the titan-side passes in order (server mail sync, re-derive over --days, triage, bills, envelopes, hooks,
        linker, chat links, world, system world, KB questions), then reports per source: at the source, ingested,
        derived. Counts only.

Run on titan through kb_agentenv.py (the agent's own environment): python3 /ai/tmp/kb_agentenv.py <this> …
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_time  # noqa: E402
STORES = Path(os.environ.get("RESET_STORES_DIR", "/ai/datasets/connectors"))
BACKUPS = Path(os.environ.get("RESET_BACKUP_DIR", "/ai/datasets/backups/stores"))

# Sources a person's Mac pushes (connectors_runner); titan can't re-read them itself.
DEVICE_SOURCES = ("calendar", "mail")
# KB source types and record node id prefixes those device items were written under.
DEVICE_KB_TYPES = ("connector:calendar", "connector:mail")
DEVICE_NODE_PREFIXES = ("cx-calendar-", "cx-mail-")
# Records titan derived itself from mail (mail_bills, the envelope and entity passes): re-derived by the rebuild.
DERIVED_NODE_RULES = (("bill", "mail"), ("receipt", "mail"), ("entity", "mail"))
# Records wiped by Coordination's call (10-09): test junk, the sources that are off, and GitHub stars (the connector
# writes them again if it's on).
OTHER_NODE_RULES = (("smoke", "-"), ("testhhbill", "-"), ("browsing_digest", "browsing"), ("download", "downloads"),
                    ("github_star", "github_stars"))

# ── the plan: per store, tables to wipe and tables to keep. A table in neither is KEPT ("unlisted"). ──────────────
# wipe: every row. wipe_where: only rows matching; a "device" condition is kept with --keep-device-items, and a
# "titan" condition (titan's own rows) widens to every row when the devices resend theirs.
# A "derived" condition is always applied as written: the rest of the table is people's own (a confirmed place).
PLAN: dict[str, dict[str, Any]] = {
    "proactive.db": {
        "wipe": ["cards", "card_acks", "card_snoozes", "device_jobs", "device_jobs_seen", "device_delivery", "item_snoozes"],
        "keep": ["triggers", "check_catalog", "card_mutes", "card_feedback", "card_source_modes", "person_quiet",
                 "device_quiet"],
        "why": "cards are derived and come back from the passes; mutes, feedback, quiet hours and triggers are people's"},
    "mail_bills.db": {
        "wipe": ["bill_due_cards", "cards", "seen"],
        "keep": ["bill_said", "obligation_claims"],
        "why": "bill cards are derived; a person's word on a bill (claims) is theirs, not at any source (#951 step 3)"},
    "mail_suggestions.db": {"wipe": ["cards", "seen"], "why": "derived from mail"},
    "mail_triage.db": {"wipe": ["queue"], "why": "derived from mail"},
    "mail_server.db": {
        "wipe": ["messages", "folders"],
        "keep": ["mail_plans"],
        "why": "server mail and its per-folder sync state: wiped so every mail is read again; the accounts are config "
               "(agent env), not rows; mail_plans are a person's move plans"},
    "links.db": {
        "wipe": ["links", "link_log", "chat_links", "chat_turns", "event_signals", "obligation_signals"],
        # a device's own items (texts: "dev-…") keep their hooks and topic vectors unless the Macs resend
        "wipe_where": {t: ("item_id NOT LIKE 'dev-%'", "titan") for t in ("hooks", "topics")},
        "keep": ["link_statements"],
        "why": "the link index is derived; link_statements are people's own related/not-related corrections"},
    "item_refs.db": {"wipe": ["derived", "pending"], "keep": ["body_grants"],
                     "why": "body_grants are people's grants (#446)"},
    "references.db": {"keep": ["refs", "file_shares", "file_reads", "file_changes"],
                      "why": "file shares and their logs are people's grants (#395); refs are device pointers: unsure, kept"},
    "ledger.db": {
        "wipe_where": {t: ("source IN (" + ",".join(f"'{s}'" for s in DEVICE_SOURCES) + ")", "device")
                       for t in ("items", "versions", "series")},
        "keep": ["tombstones", "sources_off"],
        "why": "connector ingest state for the device sources, so they ingest again; tombstones (what a person "
               "forgot) and sources turned off are people's choices; other sources (browsing and downloads are off, "
               "github_stars, fixture, daily) are kept"},
    "reflection.db": {"wipe": ["dreams", "letters"], "why": "written nightly from the day's activity: derived"},
    "our_year.db": {"keep": ["moments", "photo_days"], "why": "unsure whether moments are picked by a person: kept"},
    "places.db": {
        "wipe_where": {"places": ("confirmed_by IS NULL", "derived")},
        "keep": ["place_events", "location_modes", "held_departures", "next_stop_cards"],
        "why": "a candidate place is derived from calendars and place events and comes back from places_seed; a "
               "confirmed place (confirmed_by) is a person's word, and their place events, modes and held departures "
               "are theirs (#1427)"},
    "graph.db": {"wipe": ["*"], "why": "the relational FE graph: a projection rebuilt from the other stores (#1399)"},
    "world.db": {"wipe": ["*"], "why": "the world model (Zach chose to include it)"},
    "world/*.db": {
        "wipe": ["claims", "claim_log", "evidence", "entities", "aliases", "alias_learn", "mentions", "gaps",
                 "daily_asks", "prepared"],
        "keep": ["note_reviews", "merges", "asks", "active_log"],
        "why": "the derived world model is wiped (Zach's call); his own decisions (note reviews, merges, answered asks, "
               "the active log) are at no source and kept (Coordination, 10-09)"},
}
# Whole stores kept, as Coordination listed them (named so the dry run shows they were considered).
KEEP_STORES = ("household.db", "grants.db", "fences.db", "routines.db", "todos.db", "tasks.db",
               "scheduled.db", "held_actions.db", "spaces.db", "uploads.db", "claude_chats.db", "chat_titles.db",
               "custody.db", "kids_chores.db", "sitter.db", "lock_up.db", "tv_unlock.db", "push.db", "continuity.db",
               "handoff.db", "about.db", "family_meeting.db", "generated_images.db", "github_stars.db", "task_log.db",
               "kb_acl.db", "surfaces.db", "capture.db")

# Units that must be stopped for --apply (the agent API and every pass that writes these stores).
MUST_BE_STOPPED = ("titan-agent-api.service", "titan-mail-idle.service", "titan-agent-canary.timer",
                   "titan-mail-server.timer", "titan-mail-bills.timer", "titan-chat-links.timer",
                   "titan-kb-lifecycle.timer", "titan-kb-questions.timer", "titan-world-notes.timer",
                   "titan-system-world.timer", "titan-reflection-dream.timer", "titan-reflection-letter.timer",
                   "titan-household-coordination.timer", "titan-scheduled.timer", "titan-household-eval.timer",
                   "titan-mail-compare.timer", "titan-me-purge.timer", "titan-memory-review.timer",
                   "titan-device-health.timer", "titan-family-meeting.timer", "titan-youtube-sync.timer",
                   "titan-github-stars.timer")

# The rebuild, in order (titan-side; the Macs' connectors resend on their own once asked).
def rebuild_steps(days: float) -> list[list[str]]:
    py, s = sys.executable, str(HERE)
    return [[py, f"{s}/mail_server.py", "sync"],
            [py, f"{s}/mail_server.py", "rederive", "--days", str(days)],
            [py, f"{s}/mail_triage.py", "--apply"],
            [py, f"{s}/mail_bills.py", "--apply"],
            [py, f"{s}/envelope_pass.py", "--apply", "--limit", "400"],
            [py, f"{s}/hooks.py", "--backfill", "--apply"],
            [py, f"{s}/linker.py", "--apply"],
            [py, f"{s}/chat_links_cycle.py", "--apply"],
            [py, f"{s}/world.py", "sync", "--source", "notes"],
            [py, f"{s}/world.py", "prepare", "--day", "today"],
            [py, f"{s}/world.py", "aliases", "--learn"],
            [py, f"{s}/system_world.py", "--apply"],
            [py, f"{s}/kb_questions.py", "refresh", "--apply"]]


# ── stores ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                      "AND name NOT LIKE '%_content' AND name NOT LIKE '%_data' AND name NOT LIKE '%_idx' "
                                      "AND name NOT LIKE '%_docsize' AND name NOT LIKE '%_config' ORDER BY name")]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _count(con: sqlite3.Connection, table: str, where: str | None = None) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {_q(table)}" + (f" WHERE {where}" if where else "")).fetchone()[0]


def _plan_for(path: Path) -> dict[str, Any] | None:
    rel = str(path.relative_to(STORES))
    if rel in PLAN:
        return PLAN[rel]
    if rel.startswith("world/") and rel.endswith(".db"):
        return PLAN["world/*.db"]
    return None


def store_actions(device_resend: bool) -> list[dict[str, Any]]:
    """Every row group the reset would touch or keep: {store, table, action (wipe | keep), rows, why, note}."""
    out: list[dict[str, Any]] = []
    files = sorted(p for p in STORES.rglob("*.db") if p.is_file() and "before-" not in p.name)
    for p in files:
        rel = str(p.relative_to(STORES))
        plan = _plan_for(p)
        try:
            con = _ro(p)
            tables = _tables(con)
        except sqlite3.Error:
            out.append({"store": rel, "table": "*", "action": "keep", "rows": None, "note": "unreadable"})
            continue
        if plan is None:
            note = "listed keep" if rel in KEEP_STORES else "not in the plan: kept"
            out.append({"store": rel, "table": "*", "action": "keep", "rows": sum(_count(con, t) for t in tables),
                        "note": note})
            continue
        wipe, keep, where = set(plan.get("wipe") or []), set(plan.get("keep") or []), plan.get("wipe_where") or {}
        for t in tables:
            n = _count(con, t)
            if t in where:
                cond, gate = where[t]
                if gate == "titan" and device_resend:
                    cond = "1=1"   # the devices resend theirs too
                hit = _count(con, t, cond)
                if gate == "device" and not device_resend:
                    out.append({"store": rel, "table": t, "action": "keep", "rows": n,
                                "note": f"{hit} device-sourced rows kept with --keep-device-items"})
                else:
                    kept_note = {"device": f"{n - hit} other sources' rows kept",
                                 "derived": f"{n - hit} people's rows kept"}.get(gate,
                                                                                 f"{n - hit} device rows kept with --keep-device-items")
                    out.append({"store": rel, "table": t, "action": "wipe", "rows": hit, "where": cond,
                                "note": "" if not n - hit else kept_note})
            elif "*" in wipe or t in wipe:
                out.append({"store": rel, "table": t, "action": "wipe", "rows": n})
            elif t in keep:
                out.append({"store": rel, "table": t, "action": "keep", "rows": n})
            else:
                out.append({"store": rel, "table": t, "action": "keep", "rows": n, "note": "unlisted: kept"})
        missing = sorted((wipe - {"*"}) - set(tables))
        if missing:
            out.append({"store": rel, "table": ",".join(missing), "action": "-", "rows": 0, "note": "planned, absent"})
    return out


# ── postgres (the KB and records), through psql with the unit's own URL; nothing of it printed ───────────────────

def _unit_env(unit: str) -> dict[str, str]:
    show = lambda p: subprocess.run(["systemctl", "show", "-p", p, "--value", unit],  # noqa: E731
                                    capture_output=True, text=True).stdout
    env = dict(t.partition("=")[::2] for t in shlex.split(show("Environment")))
    for f in re.findall(r"(/[^\s(]+)", show("EnvironmentFiles")):
        if Path(f).is_file():
            for line in Path(f).read_text().splitlines():
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)=(.*)$", line)
                if m:
                    env[m.group(1)] = m.group(2).strip().strip("'\"")
    return env


def _pg_env(unit: str) -> dict[str, str]:
    u = urlsplit(_unit_env(unit).get("DATABASE_URL", ""))
    return dict(os.environ, PGHOST=u.hostname or "", PGPORT=str(u.port or 5432), PGUSER=unquote(u.username or ""),
                PGPASSWORD=unquote(u.password or ""), PGDATABASE=u.path.lstrip("/"))


def psql(unit: str, sql: str) -> list[list[str]]:
    r = subprocess.run(["psql", "-X", "-A", "-t", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql], env=_pg_env(unit),
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"psql on {unit} failed")   # never the error text: it can carry the connection string
    return [line.split("\t") for line in r.stdout.strip().splitlines() if line]


def _in(values: tuple[str, ...]) -> str:
    return ",".join("'" + v.replace("'", "''") + "'" for v in values)


def kb_record_actions(device_resend: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t, n in psql("retrieval-runtime.service", "SELECT source_type, count(*) FROM retrieval_sources GROUP BY 1 ORDER BY 1"):
        dev = t in DEVICE_KB_TYPES
        act = "wipe" if dev and device_resend else "keep"
        note = ("device-sourced: kept with --keep-device-items" if dev and not device_resend else
                "" if dev else {"knowledge_record": "runbook and decision notes (titan's own seeds): kept",
                                "note": "notes a person wrote: kept", "web": "pages a person saved: kept"}.get(t, "kept"))
        out.append({"store": "postgres retrieval_runtime", "table": f"retrieval_sources[{t}]", "action": act,
                    "rows": int(n), "note": note})
    for t, n in psql("retrieval-runtime.service", "SELECT 'retrieval_queries', count(*) FROM retrieval_queries"):
        out.append({"store": "postgres retrieval_runtime", "table": t, "action": "keep", "rows": int(n),
                    "note": "the query log: unsure, kept"})
    rows = psql("record-runtime.service",
                "SELECT node_type, coalesce(data->'provenance'->>'source', data->>'source', '-'), "
                "CASE " + " ".join(f"WHEN node_id LIKE '{p}%' THEN '{p}'" for p in DEVICE_NODE_PREFIXES) +
                " ELSE '' END, count(*) FROM nodes GROUP BY 1,2,3 ORDER BY 1,2,3")
    for typ, src, dev_pfx, n in rows:
        derived = (typ, src) in DERIVED_NODE_RULES and not dev_pfx
        other = (typ, src) in OTHER_NODE_RULES and not dev_pfx
        if dev_pfx:
            act, note = ("wipe", "device-sourced") if device_resend else ("keep", "device-sourced: kept with --keep-device-items")
        elif derived:
            act, note = "wipe", "derived from mail by titan's passes"
        elif other:
            act, note = "wipe", "test junk, a source that's off, or stars (Coordination, 10-09)"
        else:
            act, note = "keep", ("unsure, kept" if src == "-" else "kept")
        out.append({"store": "postgres record_runtime", "table": f"nodes[{typ}/{src}{'/' + dev_pfx if dev_pfx else ''}]",
                    "action": act, "rows": int(n), "note": note})
    return out


# ── the dry run ────────────────────────────────────────────────────────────────────────────────────────────────────

def dry_run(device_resend: bool) -> list[dict[str, Any]]:
    rows = store_actions(device_resend)
    try:
        rows += kb_record_actions(device_resend)
    except Exception as exc:  # noqa: BLE001
        rows.append({"store": "postgres", "table": "*", "action": "keep", "rows": None,
                     "note": f"not readable here ({type(exc).__name__})"})
    return rows


def print_rows(rows: list[dict[str, Any]]) -> None:
    tot = Counter()
    for r in rows:
        tot[r["action"]] += r["rows"] or 0
        print(f"{r['action']:<5} {r['store']:<34} {r['table']:<44} {'' if r['rows'] is None else r['rows']:>7}"
              + (f"  ({r['note']})" if r.get("note") else ""))
    print(f"\nrows to wipe: {tot['wipe']}; rows kept: {tot['keep']}")
    print("\nOpen questions (Coordination's answers, 10-09): (a) calendars: waits on Desktop's gaveUp=66 fix; "
          "(b) personal/action mail threads to the KB, owner-only (JEV); (c) read Sent and Archive too (JEV, mail_server "
          "folders); (d) notes/contacts: later tickets; reminders stay on the Mac; transcripts via Desktop's watcher.")


# ── apply ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def _state(unit: str) -> str:
    return subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", unit], capture_output=True,
                          text=True).stdout.strip()


def running_units(units: tuple[str, ...] = MUST_BE_STOPPED, state: Callable[[str], str] = _state) -> list[str]:
    """Units still active (or starting). A timer counts until stopped; so does its service mid-run."""
    out = []
    for u in units:
        if state(u) in ("active", "activating", "reloading", "deactivating"):
            out.append(u)
        if u.endswith(".timer"):
            svc = u[:-6] + ".service"
            if state(svc) in ("active", "activating", "reloading"):
                out.append(svc)
    return out


def backup(ts: str, rows: list[dict[str, Any]]) -> Path:
    dest = BACKUPS / f"pre-reset-{ts}"
    dest.mkdir(parents=True, exist_ok=False)
    os.chmod(dest, 0o700)
    for rel in sorted({r["store"] for r in rows if not r["store"].startswith("postgres")}):
        src, out = STORES / rel, dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(src) as s, sqlite3.connect(out) as d:
            s.backup(d)
        os.chmod(out, 0o600)
    pg_dump(dest)
    return dest


# Tables a runtime's own DB user can't read (another role owns them) and the reset never touches: left out of the dump.
# retrieval_runtime's okb_* (okb_namespaces, okb_works, okb_chunks) are empty and unused (10-10: pg_dump's "permission
# denied for table okb_namespaces" stopped the first --apply before any wipe).
PG_DUMP_EXCLUDE = {"retrieval_runtime": ("okb_*",)}


def pg_dump_cmd(name: str, out: Path) -> list[str]:
    return ["pg_dump", "-Fc", "-f", str(out), *[f"--exclude-table={t}" for t in PG_DUMP_EXCLUDE.get(name, ())]]


def excluded_counts(name: str, unit: str, pg: Callable[[str, str], list[list[str]]] | None = None) -> dict[str, int | None]:
    """Each table left out of a dump, with its live row count (pg_stat, which needs no read on the table), so one that
    ever fills shows. None when it can't be told."""
    pats = PG_DUMP_EXCLUDE.get(name, ())
    if not pats:
        return {}
    like = " OR ".join("relname LIKE '" + p.replace("*", "%").replace("'", "''") + "'" for p in pats)
    try:
        rows = (pg or psql)(unit, f"SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE {like} ORDER BY relname")
        return {r[0]: int(r[1]) for r in rows}
    except Exception:  # noqa: BLE001
        return {p: None for p in pats}


def pg_dump(dest: Path) -> None:
    """pg_dump -Fc of the KB and records into the backup; any failure stops the reset before anything is wiped."""
    for unit, name in (("retrieval-runtime.service", "retrieval_runtime"), ("record-runtime.service", "record_runtime")):
        out = dest / f"{name}.pgdump"
        left_out = excluded_counts(name, unit)
        if left_out:
            print(f"[reset] {name}: left out of the dump (rows): " + ", ".join(f"{t}={n}" for t, n in left_out.items()),
                  flush=True)
        r = subprocess.run(pg_dump_cmd(name, out), env=_pg_env(unit), capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"pg_dump of {name} failed: nothing was wiped")
        os.chmod(out, 0o600)


def _runtime_delete(service: str, path: str) -> bool:
    import agent_runtime_lib as lib
    try:
        lib._runtime_request(service, path, "DELETE")
        return True
    except Exception as exc:  # noqa: BLE001
        return "404" in str(exc)


def apply(device_resend: bool) -> dict[str, int]:
    rows = dry_run(device_resend)
    busy = running_units()
    if busy:
        raise SystemExit("refused: stop these first: " + ", ".join(busy))
    ts = time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())
    dest = backup(ts, rows)
    print(f"backup: {dest}")
    done: Counter = Counter()
    # KB and records first, through their runtimes (each with its sharing row), while the ledger still names them
    import kb_privacy
    kb_types = DEVICE_KB_TYPES if device_resend else ()
    if kb_types:
        for (sid,) in psql("retrieval-runtime.service",
                           f"SELECT source_id FROM retrieval_sources WHERE source_type IN ({_in(kb_types)})"):
            if _runtime_delete("retrieval", "/v1/retrieval/sources/" + quote(sid, safe="")):
                kb_privacy.drop_acl(sid)
                done["kb_sources"] += 1
    rules = " OR ".join(f"(node_type='{t}' AND coalesce(data->'provenance'->>'source', data->>'source','-')='{s}'"
                        + " AND " + " AND ".join(f"node_id NOT LIKE '{p}%'" for p in DEVICE_NODE_PREFIXES) + ")"
                        for t, s in DERIVED_NODE_RULES + OTHER_NODE_RULES)
    if device_resend:
        rules += " OR " + " OR ".join(f"node_id LIKE '{p}%'" for p in DEVICE_NODE_PREFIXES)
    for (nid,) in psql("record-runtime.service", f"SELECT node_id FROM nodes WHERE {rules}"):
        if _runtime_delete("record", "/v1/nodes/" + quote(nid, safe="")):
            done["record_nodes"] += 1
    # then the SQLite stores, one transaction per store
    by_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["action"] == "wipe" and not r["store"].startswith("postgres"):
            by_store[r["store"]].append(r)
    for rel, rs in by_store.items():
        with sqlite3.connect(STORES / rel) as con:
            for r in rs:
                cur = con.execute(f"DELETE FROM {_q(r['table'])}" + (f" WHERE {r['where']}" if r.get("where") else ""))
                done[f"{rel}:{r['table']}"] += cur.rowcount
    # last: a new ledger epoch, so every Mac sends everything again on its next sync (jellybean#1157)
    import connectors_runner
    done["ledger_epoch_bumped"] = 1 if device_resend and connectors_runner.bump_epoch() else 0
    return dict(done)


# ── coverage and the rebuild's report ──────────────────────────────────────────────────────────────────────────────

def coverage(days: list[str]) -> dict[str, Any]:
    """Per source, for the given days (as stored): ledger items / in KB / as record node; server mail / derived;
    text items (device hooks); cards by source. Counts only."""
    out: dict[str, Any] = {}
    led = defaultdict(lambda: [0, 0, 0])
    with _ro(STORES / "ledger.db") as con:
        for src, obs, kb, node in con.execute("SELECT source, observed_at, kb, node FROM items WHERE deleted_at IS NULL"):
            d = str(obs or "")[:10]
            if d in days:
                a = led[f"{src} {d}"]
                a[0] += 1
                a[1] += bool(kb)
                a[2] += bool(node)
    out["ledger (items, kb, node)"] = dict(led)
    ms = defaultdict(lambda: [0, 0])
    with _ro(STORES / "mail_server.db") as con:
        for sent, derived in con.execute("SELECT sent_at, derived_at FROM messages"):
            d = str(sent or "")[:10]
            if d in days:
                ms[d][0] += 1
                ms[d][1] += derived is not None
    out["server mail (messages, derived)"] = dict(ms)
    with _ro(STORES / "links.db") as con:
        out["text items (device hooks)"] = {d: con.execute(
            "SELECT COUNT(DISTINCT item_id) FROM hooks WHERE source='text' AND kind='date:item' AND key=?", (d,)
        ).fetchone()[0] for d in days}
    with _ro(STORES / "proactive.db") as con:
        cards = Counter()
        for src, at in con.execute("SELECT source, created_at FROM cards"):
            d = local_time.day(at or 0)
            if d in days:
                cards[f"{src} {d}"] += 1
    out["cards"] = dict(cards)
    return out


def rebuild_report() -> dict[str, Any]:
    """Per source: at the source (what titan was given or read), ingested (KB, records), derived (passes)."""
    rep: dict[str, Any] = {}
    with _ro(STORES / "ledger.db") as con:
        for src, n, kb, node in con.execute("SELECT source, COUNT(*), SUM(kb>0), SUM(node>0) FROM items "
                                            "WHERE deleted_at IS NULL GROUP BY source"):
            rep[f"connector:{src}"] = {"at_source": n, "ingested_kb": kb or 0, "ingested_records": node or 0}
    with _ro(STORES / "mail_server.db") as con:
        n, derived, kb = con.execute("SELECT COUNT(*), SUM(derived_at IS NOT NULL), SUM(kb>0) FROM messages").fetchone()
        folders = con.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
        rep["server mail"] = {"at_source": n, "folders": folders, "derived": derived or 0, "ingested_kb": kb or 0}
    with _ro(STORES / "links.db") as con:
        rep["links"] = {"items": con.execute("SELECT COUNT(DISTINCT item_id) FROM hooks").fetchone()[0],
                        "edges": con.execute("SELECT COUNT(*) FROM links").fetchone()[0]}
    with _ro(STORES / "mail_bills.db") as con:
        rep["bills"] = {"seen": con.execute("SELECT COUNT(*) FROM seen").fetchone()[0],
                        "cards": con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]}
    with _ro(STORES / "proactive.db") as con:
        rep["cards"] = dict(con.execute("SELECT source, COUNT(*) FROM cards GROUP BY source").fetchall())
    world = Counter()
    for p in sorted((STORES / "world").glob("*.db")):
        with _ro(p) as con:
            world["entities"] += con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            world["claims"] += con.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    rep["world"] = dict(world)
    try:
        rep["kb"] = dict((t, int(n)) for t, n in psql("retrieval-runtime.service",
                                                       "SELECT source_type, count(*) FROM retrieval_sources GROUP BY 1"))
        rep["records"] = dict((t, int(n)) for t, n in psql("record-runtime.service",
                                                            "SELECT node_type, count(*) FROM nodes GROUP BY 1"))
    except Exception as exc:  # noqa: BLE001
        rep["kb"] = rep["records"] = f"not readable ({type(exc).__name__})"
    return rep


def rebuild(days: float) -> dict[str, Any]:
    busy = [u for u in running_units(("titan-agent-api.service",)) if u]
    steps = []
    for cmd in rebuild_steps(days):
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True)
        steps.append({"step": " ".join(Path(c).name if c.startswith("/") else c for c in cmd[1:]),
                      "exit": r.returncode, "seconds": round(time.time() - t0, 1)})
        print(f"[rebuild] {steps[-1]}", flush=True)   # the step and its exit only: never its output
    return {"agent_api_was_running": bool(busy), "steps": steps, "report": rebuild_report()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="the default")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rebuild", action="store_true")
    g.add_argument("--coverage", nargs="*", metavar="DAY")
    ap.add_argument("--keep-device-items", action="store_true",
                    help="keep what the Macs pushed (the default wipes it and bumps the ledger epoch: they resend)")
    ap.add_argument("--days", type=float, default=90, help="--rebuild: re-derive server mail over this many days")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    a.device_resend = not a.keep_device_items
    if a.coverage is not None:
        print(json.dumps(coverage(a.coverage or [time.strftime("%Y-%m-%d")]), indent=1))
    elif a.apply:
        print(json.dumps(apply(a.device_resend), indent=1))
    elif a.rebuild:
        print(json.dumps(rebuild(a.days), indent=1))
    else:
        rows = dry_run(a.device_resend)
        print(json.dumps(rows, indent=1)) if a.json else print_rows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
