#!/usr/bin/env python3
"""Nightly reflection: the Activity Letter and the overnight "dream" for Jellybean.

  letter  (23:00, titan-reflection-letter.timer): the day rewritten as a short first-person note.
          Each sentence that describes an action carries a `ref` to the log entry it came from.
  dream   (05:30, titan-reflection-dream.timer): 2-4 findings about the previous day (patterns,
          things prepared, compared with the same weekday a week earlier), plus one question the
          user could approve ("Set it up" sends `action_prompt` to chat).

Material (gather): that local day's household-timeline and dev-activity events, proactive cards,
card replies and triggers, named-check runs, connector ingests (counts per source only) and memory
facts saved that day. Titles only, capped (~6000 chars) for the local model's 16k context.
The letter is Zach's: only what he may see (audience.visible(..., "zach", "admin")): his own timeline
entries, the household's and titan's own; cards for him; his own card replies.

LOCAL ONLY: household data never goes to Gemini, Jev or any hosted service. The model call goes to
the agent's llama-server (LOCAL_LLM_URL, else AGENT_ENDPOINT, default http://127.0.0.1:8010) and is
refused unless that URL is a loopback address.

Store: SQLite at $REFLECTION_DB (default /ai/datasets/connectors/reflection.db); one row per day,
re-running a day replaces it.

  python3 scripts/reflection.py letter [--day YYYY-MM-DD] [--dry-run]
  python3 scripts/reflection.py dream  [--day YYYY-MM-DD] [--dry-run]   (default: yesterday)
  python3 scripts/reflection.py show   [--day YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_runtime_lib as lib  # noqa: E402

DB_PATH = Path(os.environ.get("REFLECTION_DB", "/ai/datasets/connectors/reflection.db"))
MATERIAL_CHARS = int(os.environ.get("REFLECTION_MATERIAL_CHARS", "6000"))
MAX_ITEMS = 80
TITLE_CHARS = 160
ICONS = ("clock", "repeat", "check", "alert", "calendar", "code", "home", "moon")
EMPTY_LETTER = ("Nothing to write yet. Once I've done something today, I'll put it here in plain words, "
                "and every sentence will link to the action it describes.")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

_lock = threading.Lock()


# ---------------------------------------------------------------------------- time helpers

def _tz():
    return lib._local_now().tzinfo


def _today() -> str:
    return lib._local_now().strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (lib._local_now() - timedelta(days=1)).strftime("%Y-%m-%d")


def letter_default_day() -> str:
    """Today, except before 05:00, when it's yesterday: a 23:00 run missed while titan was off
    catches up at boot (Persistent=true) and should still write the day that just ended."""
    now = lib._local_now()
    return (now - timedelta(days=1) if now.hour < 5 else now).strftime("%Y-%m-%d")


def _bounds(day: str) -> tuple[float, float]:
    """[start, end) of a local calendar day as epoch seconds (DST-safe: both ends are local midnights)."""
    d = date.fromisoformat(day)
    tz = _tz()
    nxt = d + timedelta(days=1)
    return (datetime(d.year, d.month, d.day, tzinfo=tz).timestamp(),
            datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).timestamp())


_TS_RX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?\s*(Z|[+-]\d{2}:?\d{2})?$")


def _parse_ts(value: Any) -> float | None:
    """Epoch seconds from an event timestamp. The event runtime emits chrono RFC 3339 with
    nanoseconds ("2026-09-29T17:02:03.123456789+00:00"), which Python 3.10's fromisoformat
    rejects, so parse it by hand. Numbers are taken as epoch seconds; a naive string as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _TS_RX.match(str(value).strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, off = m.groups()
    tz = timezone.utc
    if off and off != "Z":
        sign = 1 if off[0] == "+" else -1
        digits = off[1:].replace(":", "")
        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4])))
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=tz)
    return dt.timestamp() + (float("0." + frac) if frac else 0.0)


def _hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, _tz()).strftime("%H:%M")


def _redact(text: str) -> str:
    """Strip household secrets (codes, PINs, account numbers) from material: the letter is prose on
    a screen and has no reason to repeat a door code."""
    return lib._SECRET_RX.sub(lambda m: m.group(0).replace(m.group(1), "(hidden)"), text or "")


def _clip(text: Any, n: int = TITLE_CHARS) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------- gather

def _item(iid: str, epoch: float, kind: str, title: str, priority: int = 2, **extra: Any) -> dict[str, Any]:
    return {"id": iid, "time": _hhmm(epoch), "ts": epoch, "kind": kind, "title": _clip(_redact(title)),
            "priority": priority, **extra}


def calendar_entry(e: dict[str, Any]) -> bool:
    """A calendar item the connector put on the timeline (#636): something scheduled, not something that happened.
    Its stamps are when it synced (10-02: a 09-30 "Pickup Leo from school", re-synced at 07:58, read as
    "7:58 AM: you picked up Leo")."""
    pl = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    return str(pl.get("source") or "").lower() == "calendar"


def calendar_start(e: dict[str, Any]) -> float | None:
    """When a calendar entry is scheduled for: its payload's starts_at, or the occurrence in its upstream id
    ("<uid>@2026-09-30T21:50:00Z"). None when it doesn't say: never its sync time."""
    pl = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    ts = _parse_ts(pl.get("starts_at"))
    if ts is not None:
        return ts
    for v in (pl.get("upstream_id"), pl.get("source_id")):
        if v and "@" in str(v):
            ts = _parse_ts(str(v).rsplit("@", 1)[1])
            if ts is not None:
                return ts
    return None


def _event_ts(e: dict[str, Any]) -> float | None:
    """When an event happened. The event runtime's EventRecord carries `timestamp` (its occurred_at,
    which defaults to publish time) and `published_at`; connector notes also carry payload.observed_at.
    An event with none of these can't be placed on a day and is skipped."""
    pl = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    if calendar_entry(e):
        return calendar_start(e)
    if pl.get("synced_at") and _parse_ts(pl.get("observed_at")) is not None:
        # A connector's entry (a download, a mail): when the thing happened is its observed_at; the event's own stamp
        # is when it synced (10-02 audit: 203 downloads all at 09-30 18:24).
        return _parse_ts(pl.get("observed_at"))
    for v in (e.get("timestamp"), e.get("occurred_at"), e.get("published_at"), e.get("created_at"),
              pl.get("observed_at")):
        ts = _parse_ts(v)
        if ts is not None:
            return ts
    return None


READER, READER_ROLE = "zach", "admin"   # whose letter it is


def _for_reader(aud: str | None, text: str = "", item: str | None = None) -> bool:
    import audience
    import fences   # #357: the letter holds back what a fence keeps from its reader, like their chat would
    return audience.visible(aud or "admins", READER, READER_ROLE) and fences.get().admits(
        READER, READER_ROLE, None, text=text, item=item)


def _timeline_items(start: float, end: float, timeline: Callable[[str], list], gaps: list[str]) -> list[dict]:
    out = []
    for stream, kind in (("household-timeline", "timeline"), (lib.DEV_STREAM, "dev")):
        try:
            evs = timeline(stream) or []
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"{stream} unavailable ({_clip(exc, 80)})")
            continue
        for e in evs:
            if calendar_entry(e):    # scheduled, not what happened that day (#636)
                continue
            ts = _event_ts(e)
            if ts is None or not (start <= ts < end) or not lib._event_visible(e, READER, READER_ROLE):
                continue
            pl = e.get("payload") if isinstance(e.get("payload"), dict) else {}
            text = str(pl.get("text") or e.get("event_type") or "").strip()
            if not text:
                continue
            eid = str(e.get("event_id") or e.get("id") or f"{int(ts)}")
            title = text + (" (this entry was corrected later)" if e.get("corrected") else "")
            out.append(_item(f"ev-{eid}"[:80], ts, kind, title, priority=1 if kind == "timeline" else 2))
    return out


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path or not Path(path).exists():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def _proactive_items(day: str, start: float, end: float, db: Path, gaps: list[str]) -> list[dict]:
    out: list[dict] = []
    try:
        con = _ro(db)
    except sqlite3.Error as exc:
        gaps.append(f"proactive store unavailable ({exc})")
        return out
    if con is None:
        return out
    try:
        try:
            for cid, title, created, acked, aud in _cards(
                    con, "SELECT id, title, created_at, acked_at{aud} FROM cards WHERE (created_at>=? AND created_at<?) "
                    "OR (acked_at>=? AND acked_at<?)", (start, end, start, end)):
                if not _for_reader(aud, str(title or ""), cid):
                    continue
                if start <= created < end:
                    state = ("not cleared yet" if acked is None else
                             f"you cleared it at {_hhmm(acked)}" if acked < end else "cleared later")
                    out.append(_item(f"card-{cid}", created, "card", f"I raised a card: {title} ({state})", 1))
                elif acked is not None:
                    out.append(_item(f"ack-{cid}", acked, "card", f"You cleared the card: {title}", 2))
        except sqlite3.Error:
            pass
        try:
            try:
                rows = con.execute("SELECT id, source, summary, received_at, outcome, reason, card_id, person FROM triggers "
                                   "WHERE received_at>=? AND received_at<?", (start, end)).fetchall()
            except sqlite3.OperationalError:   # a store from before card replies had a person
                rows = [r + (None,) for r in con.execute(
                    "SELECT id, source, summary, received_at, outcome, reason, card_id FROM triggers "
                    "WHERE received_at>=? AND received_at<?", (start, end))]
        except sqlite3.Error:
            rows = []
        counts: dict[str, int] = {}
        for tid, src, summary, recv, outcome, reason, card_id, person in rows:
            counts[outcome or "queued"] = counts.get(outcome or "queued", 0) + 1
            if src == "card-reply" and person and person != READER:
                continue   # someone else's reply is theirs
            if src == "card-reply":
                out.append(_item(f"trig-{tid}", recv, "reply", summary, 1))
            elif outcome == "error":
                out.append(_item(f"trig-{tid}", recv, "failure",
                                 f"I couldn't process an event from {src}: {summary} ({_clip(reason, 60)})", 0))
            elif outcome == "rate_limited":
                out.append(_item(f"trig-{tid}", recv, "failure", f"Skipped an event from {src} (hourly cap): {summary}", 0))
        if rows:
            names = {"reported": "reported to you", "logged": "logged", "no_action": "needed nothing",
                     "dropped": "dropped as noise", "error": "failed", "rate_limited": "skipped (cap)"}
            parts = [f"{n} {names.get(k, k)}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])]
            last = max(r[3] for r in rows)
            out.append(_item(f"triggers-{day}", last, "triggers", f"Looked at {len(rows)} incoming events: " + ", ".join(parts), 3))
        try:
            for jid, name, req, fin, ok, code, dur in con.execute(
                    "SELECT id, check_name, requested_at, finished_at, ok, exit_code, duration_ms FROM device_jobs "
                    "WHERE (requested_at>=? AND requested_at<?) OR (finished_at>=? AND finished_at<?)",
                    (start, end, start, end)):
                if fin is None:
                    out.append(_item(f"job-{jid}", req, "check", f"Asked the Mac to run {name}; it hasn't reported back", 1))
                elif ok:
                    secs = f" in {round((dur or 0) / 1000)}s" if dur else ""
                    out.append(_item(f"job-{jid}", fin, "check", f"Check {name} passed{secs}", 2))
                else:
                    out.append(_item(f"job-{jid}", fin, "failure", f"Check {name} FAILED (exit {code})", 0))
        except sqlite3.Error:
            pass
    finally:
        con.close()
    return out


def _cards(con: sqlite3.Connection, query: str, args: tuple) -> list[tuple]:
    """Card rows with their audience last ({aud} in the query); a store from before audiences has
    none, and its cards were all the admin's."""
    try:
        return con.execute(query.format(aud=", audience"), args).fetchall()
    except sqlite3.OperationalError:
        return [r + (None,) for r in con.execute(query.format(aud=""), args)]


def _open_cards(db: Path, before: float, limit: int = 5) -> list[dict]:
    """Cards still unacknowledged, created before `before` (what's waiting on the user)."""
    con = _ro(db)
    if con is None:
        return []
    try:
        rows = _cards(con, "SELECT id, title, created_at{aud} FROM cards WHERE acked_at IS NULL AND created_at<? "
                      "ORDER BY created_at DESC LIMIT ?", (before, limit * 4))
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return [_item(f"card-{cid}", created, "waiting",
                  f"Still waiting on you (from {datetime.fromtimestamp(created, _tz()).strftime('%a %b %d')}): {title}", 1)
            for cid, title, created, aud in rows if _for_reader(aud, str(title or ""), cid)][:limit]


def _failed_checks(db: Path, since: float, until: float, limit: int = 5) -> list[dict]:
    con = _ro(db)
    if con is None:
        return []
    try:
        rows = con.execute("SELECT id, check_name, finished_at, exit_code FROM device_jobs WHERE ok=0 AND finished_at>=? "
                           "AND finished_at<? ORDER BY finished_at DESC LIMIT ?", (since, until, limit)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return [_item(f"job-{jid}", fin, "failure",
                  f"Check {name} FAILED (exit {code}) on {datetime.fromtimestamp(fin, _tz()).strftime('%a')}", 0)
            for jid, name, fin, code in rows]


def _ingest_items(day: str, start: float, end: float, ledger: Path) -> list[dict]:
    """Counts per source only: what synced, never what it said. The daily-log source is skipped
    (it mirrors the timeline)."""
    con = _ro(ledger)
    if con is None:
        return []
    try:
        rows = con.execute("SELECT source, COUNT(*), MAX(synced_at) FROM items WHERE synced_at>=? AND synced_at<? "
                           "AND source<>'daily' AND deleted_at IS NULL GROUP BY source", (start, end)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return [_item(f"ingest-{src}-{day}", last, "ingest", f"Synced {n} item{'s' if n != 1 else ''} from {src}", 3)
            for src, n, last in rows]


def _memory_db_path() -> Path:
    return __import__("agent_paths").memory_db()


def _fact_items(start: float, end: float, memory_db: Path) -> list[dict]:
    """Facts saved (or updated: agent_memory rewrites `ts` when a fact's value changes) that day.
    Zach's only: the letter is his, and other people's memory is theirs (household_identity)."""
    con = _ro(memory_db)
    if con is None:
        return []
    try:
        rows = con.execute("SELECT id, text, ts FROM facts WHERE ts>=? AND ts<? AND (person_id IS NULL OR person_id=?) "
                           "ORDER BY ts", (start, end, "zach")).fetchall()
    except sqlite3.OperationalError:   # a memory db from before per-person facts (no person_id column)
        try:
            rows = con.execute("SELECT id, text, ts FROM facts WHERE ts>=? AND ts<? ORDER BY ts", (start, end)).fetchall()
        except sqlite3.Error:
            rows = []
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return [_item(f"fact-{fid}"[:80], ts, "memory", f"Remembered: {text}", 3) for fid, text, ts in rows]


def _chores_items(day: str, end: float, chores: Callable[[date], str | None] | None = None) -> list[dict]:
    """Tomorrow's household chores for the letter, computed (world.chores_due with custody), never read from a note."""
    nxt = date.fromisoformat(day) + timedelta(days=1)
    try:
        if chores is None:
            import custody
            import household_identity as hh
            import local_time
            import world
            people = hh.get_store().people()
            found = world.chores_due(world.get(), nxt, people, custody.state_at, zone=local_time.zone(),
                                     person=READER, role=READER_ROLE)
            line = world.chores_line(found, f"tomorrow ({nxt:%a %b} {nxt.day})")
        else:
            line = chores(nxt)
    except Exception:  # noqa: BLE001 - the letter goes on without it
        return []
    return [_item(f"chores-{nxt.isoformat()}", end - 1, "tomorrow", line, 1)] if line else []


def gather(day: str | None = None, *, timeline: Callable[[str], list] | None = None,
           proactive_db: Path | None = None, ledger: Path | None = None, memory_db: Path | None = None,
           budget: int | None = None, include_waiting: bool = True) -> dict[str, Any]:
    """That local day's material: {day, items, count, gaps}. Items are {id, time, kind, title, ...}
    in time order, capped at `budget` characters (failures and cards first, counts last)."""
    import connectors_runner
    import proactive
    day = day or _today()
    start, end = _bounds(day)
    gaps: list[str] = []
    pdb = Path(proactive_db or proactive.DB_PATH)
    items = (_timeline_items(start, end, timeline or lib._timeline_events, gaps)
             + _proactive_items(day, start, end, pdb, gaps)
             + _ingest_items(day, start, end, Path(ledger or connectors_runner.LEDGER_PATH))
             + _fact_items(start, end, Path(memory_db or _memory_db_path())))
    count = len(items)
    items += _chores_items(day, end)   # tomorrow's, so the letter can say "recycling goes out tomorrow night"   # what happened that day; open cards from earlier days don't count
    if include_waiting:
        seen = {i["id"] for i in items}
        items += [c for c in _open_cards(pdb, start) if c["id"] not in seen]
    items = _cap(items, MATERIAL_CHARS if budget is None else budget)
    return {"day": day, "items": items, "count": min(count, len([i for i in items if i["kind"] != "waiting"])),
            "gaps": gaps}


def _part(hhmm: str) -> str:
    h = int(hhmm[:2]) if hhmm[:2].isdigit() else 12
    return "night" if h < 5 else "morning" if h < 12 else "afternoon" if h < 17 else "evening" if h < 22 else "night"


def _line(i: dict[str, Any]) -> str:
    return f"[{i['id']}] {i['time']} {_part(i['time'])} {i['kind']}: {i['title']}"


def _cap(items: list[dict], budget: int) -> list[dict]:
    """Keep the most important items that fit the budget (and MAX_ITEMS), in time order (open cards from earlier days last)."""
    kept, used = [], 0
    for i in sorted(items, key=lambda x: (x["priority"], x["ts"])):
        n = len(_line(i)) + 1
        if used + n > budget or len(kept) >= MAX_ITEMS:
            continue
        kept.append(i)
        used += n
    return sorted(kept, key=lambda x: (x["kind"] == "waiting", x["ts"]))   # older open cards last


def _log(items: list[dict]) -> list[dict[str, str]]:
    return [{"id": i["id"], "time": i["time"], "kind": i["kind"], "title": i["title"]} for i in items]


# ---------------------------------------------------------------------------- the local model

def _llm_url() -> str:
    return (os.environ.get("LOCAL_LLM_URL") or os.environ.get("AGENT_ENDPOINT") or "http://127.0.0.1:8010").rstrip("/")


def _assert_local(url: str) -> None:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in _LOCAL_HOSTS and not host.startswith("127."):
        raise RuntimeError(f"reflection runs on the local model only; refusing non-loopback LLM URL {url!r}")


def _local_llm(system: str, user: str, schema: dict[str, Any], *, max_tokens: int,
               temperature: float = 0.4) -> tuple[dict[str, Any], str]:
    """One grammar-constrained call to the agent's llama-server. Returns (parsed JSON, model id)."""
    url = _llm_url()
    _assert_local(url)
    model = "local"
    try:
        data = (lib._http_json(f"{url}/v1/models", timeout=10) or {}).get("data") or []
        model = (data[0].get("id") if data else None) or model
    except Exception:  # noqa: BLE001
        pass
    payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user + " /no_think"}],
               "response_format": {"type": "json_schema",
                                   "json_schema": {"name": "reflection", "strict": True, "schema": schema}},
               "chat_template_kwargs": {"enable_thinking": False}}
    resp = lib._http_json(f"{url}/v1/chat/completions", payload=payload, timeout=300)
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    text = re.sub(r"<think>.*?</think>", "", msg.get("content") or msg.get("reasoning_content") or "", flags=re.S).strip()
    try:
        return json.loads(text), model
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise RuntimeError(f"local model returned no JSON: {text[:200]!r}")
        return json.loads(m.group(0)), model


def _ref_schema(ids: list[str]) -> dict[str, Any]:
    return {"anyOf": [{"type": "string", "enum": ids}, {"type": "null"}]} if ids else {"type": "null"}


def _material_text(items: list[dict]) -> str:
    return "\n".join(_line(i) for i in items) or "(nothing)"


# ---------------------------------------------------------------------------- storage

def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("""CREATE TABLE IF NOT EXISTS letters (day TEXT PRIMARY KEY, written_at REAL, body TEXT,
                   model TEXT, material_count INTEGER)""")
    con.execute("CREATE TABLE IF NOT EXISTS dreams (day TEXT PRIMARY KEY, written_at REAL, body TEXT)")
    return con


def _store(table: str, day: str, body: dict[str, Any], db: Path | None, model: str | None = None,
           material_count: int = 0) -> float:
    now = time.time()
    with _lock:
        con = _db(db)
        try:
            if table == "letters":
                con.execute("INSERT OR REPLACE INTO letters VALUES (?,?,?,?,?)",
                            (day, now, json.dumps(body), model, material_count))
            else:
                con.execute("INSERT OR REPLACE INTO dreams VALUES (?,?,?)", (day, now, json.dumps(body)))
            con.commit()
        finally:
            con.close()
    return now


def _shape(day: str, written_at: float, body: dict[str, Any], **extra: Any) -> dict[str, Any]:
    d = date.fromisoformat(day)
    return {"day": day, "weekday": d.strftime("%A"), "written_at": written_at,
            "written_time": _hhmm(written_at), **extra, **body}


def get_letter(day: str | None = None, db: Path | None = None) -> dict[str, Any] | None:
    """The stored letter for `day` (default: the latest), shaped for GET /v1/letter."""
    con = _db(db)
    try:
        q = "SELECT day, written_at, body, model, material_count FROM letters "
        row = (con.execute(q + "WHERE day=?", (day,)) if day else con.execute(q + "ORDER BY day DESC LIMIT 1")).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return _shape(row[0], row[1], json.loads(row[2]), model=row[3], material_count=row[4])


def get_dream(day: str | None = None, db: Path | None = None) -> dict[str, Any] | None:
    con = _db(db)
    try:
        q = "SELECT day, written_at, body FROM dreams "
        row = (con.execute(q + "WHERE day=?", (day,)) if day else con.execute(q + "ORDER BY day DESC LIMIT 1")).fetchone()
    finally:
        con.close()
    if not row:
        return None
    body = json.loads(row[2])
    if body.get("question") and _done_since(body["question"] + " " + str(body.get("action_prompt") or ""), row[1]):
        body = {**body, "question": "", "action_prompt": "", "question_done": True}
    return _shape(row[0], row[1], body)


_DONE_RX = re.compile(r"^- (\d{2}):(\d{2}) .*?“([^”]{4,80})”", re.M)


def _done_since(asked: str, written_at: float, log_dir: Path | None = None, now: float | None = None) -> bool:
    """Whether the daily log shows the dream's one question done since the dream was written: a line after
    it that names, in quotes, something the question names (10-02: the 05:30 dream still offered "add a
    reminder for the Costco grocery list" at 17:00, after "- 07:12 Added the reminder “Costco grocery list”")."""
    folder = log_dir or Path(os.environ.get("DAILY_LOG_DIR", "/ai/datasets/daily"))
    start = datetime.fromtimestamp(written_at, _tz())
    end = datetime.fromtimestamp(now or time.time(), _tz()).date()
    low, d = asked.lower(), start.date()
    while d <= end:
        try:
            text = (folder / f"{d.isoformat()}.md").read_text(errors="replace")
        except OSError:
            text = ""
        for m in _DONE_RX.finditer(text):
            if d == start.date() and (int(m.group(1)), int(m.group(2))) < (start.hour, start.minute):
                continue
            if m.group(3).lower() in low:
                return True
        d += timedelta(days=1)
    return False


# ---------------------------------------------------------------------------- the letter

_LETTER_SYSTEM = """You are Jellybean, the household's assistant, writing tonight's short letter about \
your day to the people you help. You write in the first person ("I"), plainly and warmly, like a \
thoughtful house-sitter's note. No headings, no lists, no emojis, no marketing tone.

Hard rules:
- Use ONLY the log below. Never invent an action, time, person or result that isn't in it.
- Every sentence that describes something that happened carries the id of the log entry it comes \
from as "ref" (copy the id inside the square brackets exactly). Sentences that only connect or \
summarize have ref null.
- Mention times naturally ("at 7:02") when useful. Each entry is labelled morning, afternoon, \
evening or night; use those labels, and call a part of the day quiet only if it has no entries.
- Say plainly when a check or task failed. Write "One thing I got wrong: ..." ONLY about an entry \
marked "(this entry was corrected later)"; if there is none, don't use that phrase.
- Don't say what is waiting on the user or that nothing is: that closing line is added separately.
- At most 4 short paragraphs and about 180 words in total. Counts (synced items, events looked at) \
deserve at most one short sentence."""

_LETTER_PROMPT = """Today is {weekday} {day}. Here is today's log, one entry per line as \
[id] time part-of-day kind: what happened. {span}
{gaps}
{material}

Write the letter as JSON: {{"paragraphs": [[{{"text": "one sentence", "ref": "id or null"}}, ...], ...]}}."""


def _letter_schema(ids: list[str]) -> dict[str, Any]:
    sentence = {"type": "object", "properties": {"text": {"type": "string"}, "ref": _ref_schema(ids)},
                "required": ["text", "ref"], "additionalProperties": False}
    return {"type": "object", "additionalProperties": False, "required": ["paragraphs"],
            "properties": {"paragraphs": {"type": "array", "minItems": 1, "maxItems": 4,
                                          "items": {"type": "array", "minItems": 1, "maxItems": 8, "items": sentence}}}}


def validate_letter(raw: Any, ids: set[str], max_words: int = 230) -> tuple[list[list[dict]], int]:
    """(paragraphs, dropped_refs): refs not in `ids` become null (the text stays); at most 4
    paragraphs of 8 sentences; over `max_words` words, sentences are dropped from the end of the
    middle of the letter so the closing sentence (what's waiting on the user) survives."""
    dropped = 0
    paras: list[list[dict]] = []
    for p in (raw.get("paragraphs") if isinstance(raw, dict) else None) or []:
        if not isinstance(p, list):
            continue
        out = []
        for s in p[:8]:
            if not isinstance(s, dict):
                continue
            text = _clip(s.get("text"), 400)
            if not text:
                continue
            ref = s.get("ref")
            ref = str(ref) if isinstance(ref, str) and ref else None
            if ref is not None and ref not in ids:
                ref, dropped = None, dropped + 1
            out.append({"text": text, "ref": ref})
        if out:
            paras.append(out)
    paras = paras[:4]
    words = lambda: sum(len(s["text"].split()) for p in paras for s in p)  # noqa: E731
    while paras and words() > max_words and sum(len(p) for p in paras) > 1:
        last_p = len(paras) - 1
        # drop the sentence just before the closing one
        if len(paras[last_p]) > 1:
            paras[last_p].pop(-2)
        else:
            target = paras[last_p - 1]
            target.pop()
            if not target:
                paras.pop(last_p - 1)
    return paras, dropped


_WAITING_RX = re.compile(r"waiting on you|nothing is waiting|waiting for you", re.I)


def _collapse_dev(items: list[dict], day: str) -> tuple[list[dict], dict[str, str]]:
    """Code changes would drown out the household: three or more become one summary entry for
    the model. Returns (material, alias) where alias maps the summary id to the latest change."""
    dev = [i for i in items if i["kind"] == "dev"]
    if len(dev) < 3:
        return items, {}
    repos: dict[str, int] = {}
    for i in dev:
        repo = i["title"].split(" ", 1)[0]
        repos[repo] = repos.get(repo, 0) + 1
    last = max(dev, key=lambda i: i["ts"])
    per = ", ".join(f"{r} {n}" for r, n in sorted(repos.items(), key=lambda kv: -kv[1]))
    summary = {**last, "id": f"dev-{day}", "priority": 2,
               "title": _clip(f"{len(dev)} code changes landed ({per}); the latest: {last['title']}", 220)}
    rest = [i for i in items if i["kind"] != "dev"]
    return sorted(rest + [summary], key=lambda x: (x["kind"] == "waiting", x["ts"])), {summary["id"]: last["id"]}


def _fix_refs(paras: list[list[dict]], by_id: dict[str, dict]) -> int:
    """The grammar keeps refs to real ids but not to the RIGHT ones. A sentence naming a time
    points at an entry with that time (the best word match among them); otherwise the ref must
    share a content word with its entry or it's dropped. Returns how many refs changed."""
    changed = 0
    for p in paras:
        for s in p:
            words = lib._cite_words(s["text"])
            times = set(re.findall(r"\b(\d{1,2}:\d{2})\b", s["text"]))
            times = {t.zfill(5) for t in times}
            ref = s["ref"]
            if times:
                cands = [i for i in by_id.values() if i["time"] in times]
                if cands and (ref is None or by_id.get(ref, {}).get("time") not in times):
                    best = max(cands, key=lambda i: len(words & lib._cite_words(i["title"])))
                    s["ref"], changed = best["id"], changed + 1
                    continue
            if ref and not (words & lib._cite_words(by_id[ref]["title"])):
                s["ref"], changed = None, changed + 1
    return changed


def _closing(waiting: list[dict], ids: set[str]) -> dict[str, Any]:
    """The letter's last line, from the cards still open when it's written (never the model's guess)."""
    if not waiting:
        return {"text": "Nothing is waiting on you.", "ref": None}
    first = waiting[0]
    what = re.sub(r"^Still waiting on you \(from [^)]*\): ", "", first["title"])
    ref = first["id"] if first["id"] in ids else None
    if len(waiting) == 1:
        return {"text": f"One thing is waiting on you: {what}.", "ref": ref}
    return {"text": f"{len(waiting)} things are waiting on you, starting with {what}.", "ref": ref}


def write_letter(day: str | None = None, *, material: dict[str, Any] | None = None,
                 llm: Callable[..., tuple[dict, str]] | None = None, db: Path | None = None,
                 dry_run: bool = False, **gather_kw: Any) -> dict[str, Any]:
    """Write (and store, unless dry_run) the letter for `day` (default: letter_default_day()). Returns the stored shape."""
    day = day or letter_default_day()
    mat = material or gather(day, **gather_kw)
    items = mat["items"]
    log = _log(items)
    if not mat["count"] and mat.get("gaps"):
        raise RuntimeError("nothing to write and sources unreadable (" + "; ".join(mat["gaps"]) +
                           "); not storing an empty letter")
    if not mat["count"]:
        body = {"empty": True, "paragraphs": [], "log": log, "empty_text": EMPTY_LETTER}
        model = None
    else:
        shown, alias = _collapse_dev([i for i in items if i["kind"] != "waiting"], day)
        ids = [i["id"] for i in shown]
        gaps = ("(Could not read: " + "; ".join(mat["gaps"]) + ". Don't call the day quiet because of that.)\n"
                if mat.get("gaps") else "")
        day_items = [i for i in shown if i["kind"] != "waiting"]
        span = (f"Entries run from {min(i['time'] for i in day_items)} to {max(i['time'] for i in day_items)}."
                if day_items else "")
        prompt = _LETTER_PROMPT.format(weekday=date.fromisoformat(day).strftime("%A"), day=day, gaps=gaps,
                                       material=_material_text(shown), span=span)
        raw, model = (llm or _local_llm)(_LETTER_SYSTEM, prompt, _letter_schema(ids), max_tokens=900)
        paras, dropped = validate_letter(raw, set(ids))
        paras = [[x for x in p if not _WAITING_RX.search(x["text"])] for p in paras]
        paras = [p for p in paras if p]
        by_id = {i["id"]: i for i in shown}
        dropped += _fix_refs(paras, by_id)
        for p in paras:
            for x in p:
                if x["ref"] in alias:
                    x["ref"] = alias[x["ref"]]
        if paras:
            import proactive
            waiting = _open_cards(Path(gather_kw.get("proactive_db") or proactive.DB_PATH), time.time())
            paras[-1].append(_closing(waiting, {i["id"] for i in items}))
        body = {"empty": not paras, "paragraphs": paras, "log": log, "dropped_refs": dropped,
                "gaps": mat.get("gaps") or []}
        if not paras:
            body["empty_text"] = EMPTY_LETTER
    if dry_run:
        return _shape(day, time.time(), body, model=model, material_count=mat["count"], dry_run=True)
    written = _store("letters", day, body, db, model=model, material_count=mat["count"])
    return _shape(day, written, body, model=model, material_count=mat["count"])


# ---------------------------------------------------------------------------- the dream

_DREAM_SYSTEM = """You are Jellybean, the household's assistant. Overnight you look back at \
yesterday and compare it with the same weekday a week earlier, to notice patterns in the \
household's day and prepare things. You report up to 4 short findings in plain words (one sentence \
each, under 20 words) about the people and their day: something that moved, something forgotten or \
still open, a pattern worth acting on, such as "The 12-year-old's pickup slid 30 min later, third week running." \
or "The water bill card is still open from Tuesday." Never about the assistant itself, its builds, \
checks, logs or syncing. A quiet day has no findings, and that's fine.

Hard rules:
- Use ONLY the material below. Never invent events, numbers or patterns it doesn't show. A pattern \
needs evidence on both days or repeated entries; otherwise describe it as a single observation.
- "ref" is the id (in square brackets) of the entry a finding is mainly about, or null.
- "icon" is one of: clock (timing), repeat (a pattern), check (something done or ready), alert \
(a failure or risk), calendar, code, home, moon (quiet or overnight).
- "question": ONE concrete thing you could set up that the user could approve with one tap \
(e.g. "Want me to move the standup reminder to 9:30?"). It must be something you can actually do \
with the actions listed below — never a new feature, a setting you don't have, or a change to \
how you work. "action_prompt": the exact chat message that approving sends, written as the user \
asking you to do it. If nothing is worth asking, both are "".
- Never report the absence of data ("no prior data to compare", "nothing happened last week"):
  that is not a finding. Leave it out.
- If nothing is noteworthy, return an empty findings list and empty strings."""

_DREAM_PROMPT = """Yesterday was {weekday} {day}.

Yesterday's log:
{today}

The same weekday one week earlier ({prev_day}):
{prev}

Still open (cards not cleared, recent failed checks):
{open}
{gaps}
Things you can set up (the only kinds of question allowed): {actions}
Answer as JSON: {{"findings": [{{"icon": "...", "text": "...", "ref": "id or null"}}], \
"question": "...", "action_prompt": "..."}}."""


def _dream_schema(ids: list[str]) -> dict[str, Any]:
    finding = {"type": "object", "additionalProperties": False, "required": ["icon", "text", "ref"],
               "properties": {"icon": {"type": "string", "enum": list(ICONS)}, "text": {"type": "string"},
                              "ref": _ref_schema(ids)}}
    return {"type": "object", "additionalProperties": False, "required": ["findings", "question", "action_prompt"],
            "properties": {"findings": {"type": "array", "minItems": 0, "maxItems": 4, "items": finding},
                           "question": {"type": "string"}, "action_prompt": {"type": "string"}}}


# The dream is about the household's day, never titan's own plumbing (#387: 10-01's only finding was
# "Three git skipped events in the afternoon"). Facts in code: the material it reads is the household's
# (timeline posts, cards that aren't titan's own, replies, remembered facts), and a finding about builds,
# checks, git, deploys or titan itself is dropped whatever the model says.
DREAM_KINDS = frozenset({"timeline", "card", "reply", "memory", "waiting"})
_PLUMBING_RX = re.compile(r"\b(?:git|commits?|repos?(?:itory)?|pull requests?|builds?|deploy\w*|canary|evals?|"
                          r"runtimes?|server|titan|jellybean-\w+|checks? (?:failed|passed)|(?:failed|passed) checks?|"
                          r"skipped (?:an )?events?|hourly cap|incoming events|synced \d+|connectors?|workflow|"
                          r"self-?tests?|logs?)\b", re.I)


def _household(item: dict[str, Any]) -> bool:
    """Material the dream may use: the household's, not titan's own plumbing."""
    return item.get("kind") in DREAM_KINDS and not _PLUMBING_RX.search(str(item.get("title") or ""))


_NO_DATA_RX = re.compile(r"\b(no (prior|previous|earlier|comparable|historical) (data|entries|activity|log)|"
                         r"nothing to compare|no data (to|for) compar)", re.I)


def validate_dream(raw: Any, ids: set[str]) -> tuple[dict[str, Any], int]:
    dropped = 0
    findings = []
    for f in (raw.get("findings") if isinstance(raw, dict) else None) or []:
        if not isinstance(f, dict) or not _clip(f.get("text"), 200):
            continue
        if _NO_DATA_RX.search(str(f.get("text"))):   # "no prior data to compare" isn't a finding
            continue
        if _PLUMBING_RX.search(str(f.get("text"))):   # about titan's plumbing, not the household's day (#387)
            dropped += 1
            continue
        ref = f.get("ref") if isinstance(f.get("ref"), str) and f.get("ref") else None
        if ref is not None and ref not in ids:
            ref, dropped = None, dropped + 1
        icon = f.get("icon") if f.get("icon") in ICONS else "moon"
        findings.append({"icon": icon, "text": _clip(f.get("text"), 200), "ref": ref})
    findings = findings[:4]
    question = _clip((raw or {}).get("question"), 200) if isinstance(raw, dict) else ""
    action = _clip((raw or {}).get("action_prompt"), 300) if isinstance(raw, dict) else ""
    if not question or not action:
        question = action = ""
    return {"findings": findings, "question": question, "action_prompt": action}, dropped


def _dream_actions() -> str:
    """What a "Set it up" button can really lead to: the agent's own actions plus the named checks
    the Mac has published. A dream once offered to "hold test cards until you're done coding",
    which nothing can do."""
    acts = ["add a reminder", "add a calendar event", "save a note to the knowledge base",
            "remember a fact", "leave the user a card"]
    try:
        import proactive
        names = [c["name"] for c in proactive.catalog()][:8]
        if names:
            acts.append("run one of these checks on the Mac: " + ", ".join(names))
    except Exception:  # noqa: BLE001 - the catalog is optional
        pass
    return "; ".join(acts) + "."


def write_dream(day: str | None = None, *, llm: Callable[..., tuple[dict, str]] | None = None,
                db: Path | None = None, dry_run: bool = False, **gather_kw: Any) -> dict[str, Any]:
    """Write (and store, unless dry_run) the dream about `day` (default: yesterday)."""
    import proactive
    day = day or _yesterday()
    prev_day = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    cur = gather(day, budget=3500, include_waiting=False, **gather_kw)
    prev = gather(prev_day, budget=1500, include_waiting=False, **gather_kw)
    cur = {**cur, "items": [i for i in cur["items"] if _household(i)]}     # the household's day only (#387)
    prev = {**prev, "items": [i for i in prev["items"] if _household(i)]}
    start, end = _bounds(day)
    pdb = Path(gather_kw.get("proactive_db") or proactive.DB_PATH)
    seen = {x["id"] for x in cur["items"]}
    open_items = [i for i in _cap(_open_cards(pdb, end), 1000) if i["id"] not in seen and _household(i)]
    items = cur["items"] + prev["items"] + open_items
    log = _log(items)
    empty = {"empty": True, "findings": [], "question": "", "action_prompt": "", "log": log}
    if not cur["items"] and cur["gaps"] and not cur.get("count"):
        raise RuntimeError("sources unreadable (" + "; ".join(cur["gaps"]) + "); not storing an empty dream")
    if not items:
        body, model = empty, None
    else:
        ids = [i["id"] for i in items]
        gaps = sorted(set(cur["gaps"] + prev["gaps"]))
        prompt = _DREAM_PROMPT.format(weekday=date.fromisoformat(day).strftime("%A"), day=day, prev_day=prev_day,
                                      today=_material_text(cur["items"]), prev=_material_text(prev["items"]),
                                      open=_material_text(open_items), actions=_dream_actions(),
                                      gaps=("(Could not read: " + "; ".join(gaps) + ")\n") if gaps else "")
        raw, model = (llm or _local_llm)(_DREAM_SYSTEM, prompt, _dream_schema(ids), max_tokens=700)
        parsed, dropped = validate_dream(raw, set(ids))
        body = {**empty, **parsed, "empty": not parsed["findings"], "dropped_refs": dropped}
    body["model"] = model
    if dry_run:
        return _shape(day, time.time(), body, dry_run=True)
    written = _store("dreams", day, body, db)
    return _shape(day, written, body)


# ---------------------------------------------------------------------------- CLI

def _print_letter(r: dict[str, Any] | None) -> None:
    if not r:
        print("(no letter)")
        return
    print(f"Today, from Jellybean — {r['weekday']} · written {r['written_time']}  [{r['day']}]")
    if r.get("empty"):
        print("  " + r.get("empty_text", EMPTY_LETTER))
    for p in r.get("paragraphs") or []:
        print("  " + " ".join(s["text"] + (f" [{s['ref']}]" if s.get("ref") else "") for s in p))
    print(f"  Log · {len(r.get('log') or [])}")


def _print_dream(r: dict[str, Any] | None) -> None:
    if not r:
        print("(no dream)")
        return
    print(f"While you were away — last night · {r['written_time']}  [about {r['day']}]")
    for f in r.get("findings") or []:
        print(f"  ({f['icon']}) {f['text']}" + (f" [{f['ref']}]" if f.get("ref") else ""))
    if r.get("question"):
        print(f"  Still mulling: {r['question']}\n    Set it up → {r['action_prompt']}")
    if r.get("empty"):
        print("  (nothing noteworthy)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["letter", "dream", "show", "material"])
    ap.add_argument("--day", help="YYYY-MM-DD (local). letter: today (yesterday before 05:00); material: today; dream: yesterday; show: latest")
    ap.add_argument("--dry-run", action="store_true", help="write but don't store; prints the material too")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.day:
        try:
            date.fromisoformat(a.day)
        except ValueError:
            ap.error("--day must be YYYY-MM-DD")
    mat = None
    if a.cmd == "material" or (a.dry_run and a.cmd == "letter"):
        mat = gather(a.day or (letter_default_day() if a.cmd == "letter" else None))
        print(f"# material for {mat['day']}: {mat['count']} items" + (f"; gaps: {mat['gaps']}" if mat["gaps"] else ""))
        print(_material_text(mat["items"]))
        if a.cmd == "material":
            return 0
    if a.cmd in ("letter", "dream"):
        try:
            if a.cmd == "letter":
                r, show = write_letter(mat["day"] if mat else a.day, material=mat, dry_run=a.dry_run), _print_letter
            else:
                r, show = write_dream(a.day, dry_run=a.dry_run), _print_dream
        except Exception as exc:  # noqa: BLE001 - one line for the journal, non-zero for systemd
            print(f"reflection {a.cmd} failed: {exc}", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps(r, indent=1))
        else:
            show(r)
    else:
        lt, dr = get_letter(a.day), get_dream(a.day)
        if a.json:
            print(json.dumps({"letter": lt, "dream": dr}, indent=1))
        else:
            _print_letter(lt)
            print()
            _print_dream(dr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
