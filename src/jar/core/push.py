#!/usr/bin/env python3
"""Push notifications to the iPhone (APNs), contract v1 in notes/location-coordination-2026-09-30.md.

A card a person would see (proactive.visible_to) reaches their phone at once, with the app closed,
when it matters: urgency 2-3, or urgency 1 addressed to them by name. The push carries no content: a
generic alert and the card's id. The phone's Notification Service Extension fetches the card
from titan with its own device token and fills in the title and detail, so Apple never sees it.
A device whose app is open (on the #180 long-poll in the last minute) gets nothing; the card is
already on its screen.

The push's own fields follow the phone's rules (jellybean#1056, CardNotice in ios/Shared/PushShared.swift), so a
notification whose fetch times out still lands right: its category (`jb.card`, or `jb.card.safety` for a safety card
or one that can't be muted), a thread that is one fixed word (bills, calendar, house, mail, chat, routines, tasks,
cards; never a person's name, #1208), its interruption level (time-sensitive for a safety card or time to leave,
passive for urgency 0 or while the person is busy elsewhere, otherwise active) and a relevance score. A card that
expires leaves the phones it was for (clear_expired), once.

Across a person's devices (jellybean#204): a card Done or acted on one device sends that person's other
phones a silent push carrying only the card's id ("clear"), so its notification leaves their Notification
Center too. And while the person is using another of their devices (a chat turn, a card, a read in the last
ACTIVE_S), a card's push to the phone is `passive`: it lands quietly, no banner or sound. A "leave now" card
stays time-sensitive. Focus stays iOS's: passive and time-sensitive are the levels it understands.

Continuity (#419): a change in a person's feed (continuity.emit) sends their *other* phones a silent
background push with no content at all ("sync"), so the app fetches /v1/sync itself. Not to the
device the change came from, not to one already waiting on /v1/sync, and at most one per device
every SYNC_COALESCE_S.

Tokens are kept per device (dropped when the device is revoked, when the phone turns card
notifications off, or when APNs says 410 / BadDeviceToken). Signing: an APNs auth key (.p8) in
APNS_KEY_FILE with APNS_KEY_ID and APNS_TEAM_ID; without them push is off and nothing is sent.
HTTP/2 goes through curl (titan's python has no h2); the JWT is ES256 (PyJWT + cryptography).

Live Activities (titan-node#725, jellybean#350): a task the phone shows on the Lock Screen and in the Dynamic
Island updates while the app is closed. The app registers each activity's own push token (POST
/v1/devices/self/activities, {kind, ref, token, environment}, token null when it ends); titan sends `liveactivity`
pushes to that token on a step done, on waiting for the person, and at the end. Only the task's owner's own
devices, never a shared household device. What's on the Lock Screen is the task's goal (Zach, 10-03: like a
calendar title; redacted, at most 60 characters), counts and fixed phrases ("Step 3 of 4", "Waiting on you.",
"Done"); the step's own title and what it waits for only when the owner has turned on live_activity_detail. A
step whose next one is more than 30 minutes away ends the activity rather than leaving it up.

Driving now (jellybean#310): a phone says it's in a car (PATCH /v1/devices/self {motion: "driving"}, null when it
stops); its important-but-not-urgent card pushes are held (an urgent one, urgency 3 or a "leave now", still goes) and
sent when it stops, skipping cards dealt with meanwhile. Only "driving" or not is kept, and only for DRIVING_TTL_S: a
phone that never says it stopped gets its pushes again after that.

Push-to-start (jellybean#350): with the app closed nothing could start an activity. The app registers its
pushToStartTokenUpdates token once ({kind: "task-start", token, environment, bundle}; null turns it off), and on an
update (a step running, waiting on the person, the next step within 30 minutes) titan sends a `liveactivity` "start"
(attributes-type TaskActivityAttributes, attributes {taskID, goal}, today's content-state, a fixed alert,
input-push-token) to the owner's devices that have no activity of that task yet: once per device per task, until it
ends. iOS then hands the app the new activity's token, which it registers as kind "task".

Store: SQLite at $PUSH_DB (default /ai/datasets/connectors/push.db).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

DB_PATH = Path(os.environ.get("PUSH_DB", "/ai/datasets/connectors/push.db"))
HOSTS = {"development": "https://api.sandbox.push.apple.com", "production": "https://api.push.apple.com"}
LIVE_WINDOW_S = 60
SYNC_COALESCE_S = 30
ACTIVE_S = 120
_TOKEN_RX = re.compile(r"^[0-9a-fA-F]{32,200}$")
_BUNDLE_RX = re.compile(r"^[A-Za-z0-9.\-]{3,155}$")
_lock = threading.Lock()
_jwt_cache: dict[str, Any] = {}


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("PUSH_DB") or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("""CREATE TABLE IF NOT EXISTS push_tokens (
        device_id TEXT PRIMARY KEY, token TEXT NOT NULL, environment TEXT NOT NULL, bundle TEXT NOT NULL,
        updated_at REAL NOT NULL)""")
    # Banners for titan's cards (jellybean#786 row 1): the phone's "Important cards" switch. Off keeps the token, so the
    # silent pushes (sync, clear, the #776 inbox wake) still reach it; only card alerts skip it. Rows from before: on.
    if "card_alerts" not in {r[1] for r in con.execute("PRAGMA table_info(push_tokens)")}:
        con.execute("ALTER TABLE push_tokens ADD COLUMN card_alerts INTEGER NOT NULL DEFAULT 1")
    con.execute("CREATE TABLE IF NOT EXISTS device_live (device_id TEXT PRIMARY KEY, at REAL NOT NULL)")
    # The continuity feed's own: a device waiting on /v1/sync, and when each was last nudged (#419).
    con.execute("CREATE TABLE IF NOT EXISTS sync_live (device_id TEXT PRIMARY KEY, at REAL NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS sync_nudged (device_id TEXT PRIMARY KEY, at REAL NOT NULL)")
    # The last time each person did something on each device (jellybean#204).
    con.execute("CREATE TABLE IF NOT EXISTS person_active (person TEXT NOT NULL, device_id TEXT NOT NULL, at REAL NOT NULL, "
                "PRIMARY KEY (person, device_id))")
    # One Live Activity's own push token per device, kind and ref (#725).
    con.execute("CREATE TABLE IF NOT EXISTS activity_tokens (device_id TEXT NOT NULL, person TEXT NOT NULL, "
                "kind TEXT NOT NULL, ref TEXT NOT NULL, token TEXT NOT NULL, environment TEXT NOT NULL, "
                "bundle TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY (device_id, kind, ref))")
    # Push-to-start (jellybean#350): one "start" per device per task, until that task's activity ends.
    con.execute("CREATE TABLE IF NOT EXISTS activity_starts (device_id TEXT NOT NULL, ref TEXT NOT NULL, "
                "at REAL NOT NULL, PRIMARY KEY (device_id, ref))")
    # Driving now (jellybean#310): a phone that says it's in a car, until when; and the pushes held for it.
    # A trip activity's lead (seconds from leave-by to the event's start) and travel minutes (jellybean#753): what titan
    # needs to say a moved event's new leave-by. Two numbers, gone with the token.
    con.execute("CREATE TABLE IF NOT EXISTS trip_details (device_id TEXT NOT NULL, ref TEXT NOT NULL, lead REAL NOT NULL, "
                "minutes INTEGER NOT NULL, updated REAL NOT NULL, PRIMARY KEY (device_id, ref))")
    con.execute("CREATE TABLE IF NOT EXISTS device_driving (device_id TEXT PRIMARY KEY, until REAL NOT NULL)")
    # An upcoming trip as the phone worked it out (jellybean#753 push-to-start): when to leave, the lead to the event's
    # start, travel minutes and the mode. Times only, never the event's title or place; gone once the event starts.
    con.execute("CREATE TABLE IF NOT EXISTS trip_plans (device_id TEXT NOT NULL, person TEXT NOT NULL, ref TEXT NOT NULL, "
                "leave_by REAL NOT NULL, lead REAL NOT NULL, minutes INTEGER NOT NULL, mode TEXT NOT NULL, "
                "updated REAL NOT NULL, PRIMARY KEY (device_id, ref))")
    con.execute("CREATE TABLE IF NOT EXISTS held_pushes (device_id TEXT NOT NULL, card_id TEXT NOT NULL, card TEXT NOT NULL, "
                "at REAL NOT NULL, PRIMARY KEY (device_id, card_id))")
    return con


# ── registration ────────────────────────────────────────────────────────────────────────────────

def register(device_id: str, token: Any, environment: Any, bundle: Any, db: Path | None = None, *,
             card_alerts: Any = True) -> None:
    """`card_alerts`: false mutes titan's card banners on this device; the token stays for the silent pushes. Missing
    (an older phone): true."""
    tok, env, bun = str(token or "").strip(), str(environment or "").strip().lower(), str(bundle or "").strip()
    if card_alerts is None:
        card_alerts = True
    if not isinstance(card_alerts, bool):
        raise ValueError("card_alerts must be true or false")
    if not _TOKEN_RX.fullmatch(tok):
        raise ValueError("token must be the APNs device token in hex")
    if env not in HOSTS:
        raise ValueError("environment must be development or production")
    if not _BUNDLE_RX.fullmatch(bun):
        raise ValueError("bundle must be the app's bundle id")
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT INTO push_tokens (device_id, token, environment, bundle, updated_at, card_alerts) "
                        "VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(device_id) DO UPDATE SET token=excluded.token, environment=excluded.environment, "
                        "bundle=excluded.bundle, updated_at=excluded.updated_at, card_alerts=excluded.card_alerts",
                        (device_id, tok.lower(), env, bun, time.time(), int(card_alerts)))
            con.commit()
        finally:
            con.close()


def unregister(device_id: str, db: Path | None = None) -> bool:
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM push_tokens WHERE device_id=?", (device_id,)).rowcount
            for t in ("device_live", "sync_live", "sync_nudged", "person_active"):
                con.execute(f"DELETE FROM {t} WHERE device_id=?", (device_id,))
            con.commit()
            return n > 0
        finally:
            con.close()


def mark_live(device_id: str, db: Path | None = None) -> None:
    """The device's app is waiting on the long-poll: its cards show on screen, no push needed."""
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT INTO device_live (device_id, at) VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET at=excluded.at",
                        (device_id, time.time()))
            con.commit()
        finally:
            con.close()


def mark_sync_live(device_id: str, db: Path | None = None) -> None:
    """The device is waiting on /v1/sync: its changes reach it there, no nudge needed. (Separate from
    mark_live: an app syncing in the background still wants its cards pushed.)"""
    _stamp("sync_live", device_id, db)


def _stamp(table: str, device_id: str, db: Path | None = None) -> None:
    with _lock:
        con = _db(db)
        try:
            con.execute(f"INSERT INTO {table} (device_id, at) VALUES (?,?) ON CONFLICT(device_id) DO UPDATE SET at=excluded.at",
                        (device_id, time.time()))
            con.commit()
        finally:
            con.close()


def note_active(person: str | None, device_id: str | None, db: Path | None = None) -> None:
    """The person just did something on this device (a turn, a card, a read). Never raises."""
    if not person or not device_id:
        return
    try:
        with _lock:
            con = _db(db)
            try:
                con.execute("INSERT INTO person_active (person, device_id, at) VALUES (?,?,?) "
                            "ON CONFLICT(person, device_id) DO UPDATE SET at=excluded.at", (person, device_id, time.time()))
                con.commit()
            finally:
                con.close()
    except Exception:  # noqa: BLE001 - a stamp never breaks what the person did
        pass


def active_elsewhere(person: str, device_id: str, now: float | None = None, db: Path | None = None) -> bool:
    """Whether the person used another of their devices in the last ACTIVE_S."""
    now = now or time.time()
    con = _db(db)
    try:
        row = con.execute("SELECT 1 FROM person_active WHERE person=? AND device_id<>? AND at>=? LIMIT 1",
                          (person, device_id, now - ACTIVE_S)).fetchone()
        return row is not None
    finally:
        con.close()


def _tokens(db: Path | None = None) -> list[dict[str, Any]]:
    con = _db(db)
    try:
        live = {r[0]: r[1] for r in con.execute("SELECT device_id, at FROM device_live")}
        return [{"device_id": r[0], "token": r[1], "environment": r[2], "bundle": r[3],
                 "live": time.time() - live.get(r[0], 0) < LIVE_WINDOW_S, "card_alerts": bool(r[4])}
                for r in con.execute("SELECT device_id, token, environment, bundle, card_alerts FROM push_tokens")]
    finally:
        con.close()


# ── which devices a card goes to ────────────────────────────────────────────────────────────────

def _proactive_db():
    return None   # proactive's own default (PROACTIVE_DB); a seam for tests


def should_push(card: dict[str, Any]) -> bool:
    """Urgency 2-3, or urgency 1 addressed to people by name (a person's own coordination, place routines,
    notes sent to them). Calm cards for a group, and urgency 0 (after-the-fact news: "Resolved", "battery fine
    again", a digest) for anyone, wait for the app."""
    u = int(card.get("urgency") or 0)
    if card.get("group") == "engineering":
        # jellybean#289: engineering cards wait in their folded card; only an urgent one (3) interrupts.
        return u >= 3
    return u >= 2 or (u >= 1 and str(card.get("to") or "").startswith("["))


def targets(card: dict[str, Any], devices: list[dict[str, Any]], db: Path | None = None) -> list[dict[str, Any]]:
    """The registered, not-live devices whose person would see this card. `devices` are the
    household's devices: {id, person_id, role, revoked}."""
    import proactive
    by_id = {d["id"]: d for d in devices if not d.get("revoked")}
    out = []
    for t in _tokens(db):
        d = by_id.get(t["device_id"])
        if not d or t["live"] or not t.get("card_alerts", True):   # muted: no card banners (jellybean#786 row 1)
            continue
        viewer = "household" if d["role"] == "household" else d["person_id"]
        if proactive.visible_to(card.get("to") or "admins", viewer, d["role"]):
            if card.get("id") and proactive.hidden_for(viewer, card, db=_proactive_db()):
                continue   # #1039: this person muted cards like it, or snoozed this one: no banner for them
            if not proactive.alert_for(card, viewer, d["role"], d["id"], db=_proactive_db()):
                continue   # #1039 (10-06): their source is quiet, or this device's level leaves the card out
            import fences   # #357: no push for a card the fence keeps from this person (list_cards hides it too)
            if not fences.get().admits(viewer, d["role"], card.get("from"),
                                       text=f"{card.get('title') or ''}\n{card.get('detail') or ''}", item=card.get("id")):
                continue
            out.append({**t, "person_id": d["person_id"], "role": d["role"]})
    return out


def sync_targets(person: str, except_device: str | None, devices: list[dict[str, Any]],
                 db: Path | None = None) -> list[dict[str, Any]]:
    """`person`'s registered devices to nudge for a change: not the one it came from, not revoked or
    a shared household device, not waiting on /v1/sync now, not nudged in the last SYNC_COALESCE_S."""
    mine = {d["id"] for d in devices if d["person_id"] == person and not d.get("revoked") and d["role"] != "household"}
    now = time.time()
    con = _db(db)
    try:
        live = {r[0]: r[1] for r in con.execute("SELECT device_id, at FROM sync_live")}
        nudged = {r[0]: r[1] for r in con.execute("SELECT device_id, at FROM sync_nudged")}
    finally:
        con.close()
    return [t for t in _tokens(db) if t["device_id"] in mine and t["device_id"] != except_device
            and now - live.get(t["device_id"], 0) >= LIVE_WINDOW_S and now - nudged.get(t["device_id"], 0) >= SYNC_COALESCE_S]


def _household_devices() -> list[dict[str, Any]]:
    import household_identity as hh
    out = []
    for p in hh.get_store().people():
        for d in p.get("devices", []):
            out.append({"id": d["id"], "person_id": p["id"], "role": p["role"], "revoked": d.get("revoked")})
    return out


# ── sending ─────────────────────────────────────────────────────────────────────────────────────

def configured() -> bool:
    kf = os.environ.get("APNS_KEY_FILE", "")
    return bool(kf and os.environ.get("APNS_KEY_ID") and os.environ.get("APNS_TEAM_ID") and Path(kf).is_file())


def _jwt() -> str:
    """The provider token, reused for 40 minutes (APNs wants one between 20 and 60)."""
    now = time.time()
    if _jwt_cache.get("token") and now - _jwt_cache.get("iat", 0) < 40 * 60:
        return _jwt_cache["token"]
    import jwt   # PyJWT; ES256 via cryptography
    key = Path(os.environ["APNS_KEY_FILE"]).read_text()
    tok = jwt.encode({"iss": os.environ["APNS_TEAM_ID"], "iat": int(now)}, key, algorithm="ES256",
                     headers={"kid": os.environ["APNS_KEY_ID"]})
    tok = tok.decode() if isinstance(tok, bytes) else tok
    _jwt_cache.update(token=tok, iat=now)
    return tok


# The phone's NoticeThread kinds (jellybean ios/Shared/PushShared.swift), by a card's plain source.
THREAD_HOUSE = frozenset({"lock-up", "home-security", "alarm", "security", "home", "sitter", "tv-unlock"})
THREAD_BILLS = frozenset({"mail-bills", "bill-due", "bills", "approve-cap"})
THREAD_CALENDAR = frozenset({"calendar", "next-stop", "family-meeting", "schedule"})
THREAD_MAIL = frozenset({"mail", "mail-move", "mail-unsub", "mail-suggestion", "kb-digest"})
THREAD_ROUTINES = frozenset({"routine", "routines"})
THREAD_TASKS = frozenset({"tasks", "scheduled", "todos"})
THREAD_CHAT = frozenset({"agent", "titan"})
# Every thread-id titan sends: fixed words, so Apple learns a kind at most, never who or what (#1208).
THREADS = ("bills", "calendar", "house", "mail", "chat", "routines", "tasks", "cards")
RELEVANCE = {"time-sensitive": 1.0, "active": 0.6, "passive": 0.2}


def plain_source(source: Any) -> str:
    """A trigger's source says where it came from ("trigger:calendar", "loop:checks"): the last part (CardNotice.plain)."""
    s = str(source or "").strip().lower().rsplit(":", 1)[-1]
    return s or "titan"


def _sender(card: dict[str, Any]) -> bool:
    f = str(card.get("from") or "").strip().lower()
    return bool(f) and f != "titan"


def thread_id(card: dict[str, Any]) -> str:
    """The Notification Center thread, as the phone's NoticeThread works it out, but one of THREADS: a note a person
    sent is "cards" here (the phone's own thread for it, from:<person>, is a name Apple must never see)."""
    s, k = plain_source(card.get("source")), str(card.get("key") or "")
    if s in THREAD_HOUSE:
        return "house"
    if s in THREAD_BILLS or k.startswith("bill-due") or k.startswith("payfail-"):
        return "bills"
    if s in THREAD_CALENDAR or k.startswith("leave-"):
        return "calendar"
    if s in THREAD_MAIL:
        return "mail"
    if _sender(card):
        return "cards"
    if s in THREAD_ROUTINES:
        return "routines"
    if s in THREAD_TASKS:
        return "tasks"
    if s in THREAD_CHAT:
        return "chat"
    return "cards"


def is_safety(card: dict[str, Any]) -> bool:
    """A safety card (Lock Up, alarms): its source is exactly one of proactive.SAFETY_SOURCES, as the server's own
    can_mute, quiet hours and can_stop read it. No prefix match ("kid:alarm", "trigger:alarm" are ordinary): a source
    a caller wrote must never become a safety card by its last word (titan-node#1289 review). Only titan's own jobs and
    device-less callers can set one; a paired device's is renamed at POST /v1/cards (agent_api_server)."""
    import proactive
    return proactive.is_safety_source(card.get("source"))   # as the phone reads it (#1056)


def category(card: dict[str, Any], role: str | None = None) -> str:
    """`jb.card.safety` (Ask, Later, Done behind Face ID; no Stop these) for a safety card or one this device's person
    can't mute; `jb.card` otherwise."""
    import proactive
    return "jb.card.safety" if is_safety(card) or not proactive.can_mute(card, role) else "jb.card"


def interruption_level(card: dict[str, Any], passive: bool = False) -> str:
    """time-sensitive for a safety card or time to leave (never quiet); passive for urgency 0 and while the person is
    busy on another device; active otherwise."""
    if is_safety(card) or str(card.get("key") or "").startswith("leave-"):
        return "time-sensitive"
    u = card.get("urgency")
    if passive or (int(u) if u is not None else 1) <= 0:
        return "passive"
    return "active"


def apns_priority(card: dict[str, Any]) -> str:
    """"10" (at once) for urgency 2-3, an urgent card or a time-sensitive one; "5" (when the phone is next awake) else."""
    if int(card.get("urgency") or 0) >= 2 or _urgent(card) or interruption_level(card) == "time-sensitive":
        return "10"
    return "5"


def payload(card: dict[str, Any], passive: bool = False, role: str | None = None) -> dict[str, Any]:
    """`passive`: the person is busy on another device: it lands quietly (no banner, no sound). A "leave
    now" or safety card is never quiet. `role`: the receiving device's (a household device can't mute a card).
    What Apple sees: a fixed alert, the card's id, and words from fixed lists (category, thread, level)."""
    level = interruption_level(card, passive)
    aps: dict[str, Any] = {"alert": {"title": "titan", "body": "titan has something for you"},
                           "mutable-content": 1, "sound": "default", "category": category(card, role),
                           "thread-id": thread_id(card), "interruption-level": level,
                           "relevance-score": RELEVANCE[level]}
    if level == "passive":
        aps.pop("sound")
    return {"aps": aps, "card": card["id"]}


def clear_payload(card_id: str) -> dict[str, Any]:
    """A card dealt with elsewhere: its id, nothing else."""
    return {"aps": {"content-available": 1}, "clear": str(card_id)}


SYNC_PAYLOAD: dict[str, Any] = {"aps": {"content-available": 1}, "sync": 1}   # nothing but "sync"
INBOX_PAYLOAD: dict[str, Any] = {"aps": {"content-available": 1}, "inbox": 1}   # #776: drain /v1/device/inbox


def _curl_send(target: dict[str, Any], card: dict[str, Any]) -> tuple[int, str]:
    """POST one card's notification over HTTP/2 with curl; (status, reason)."""
    return _curl_post(target, payload(card, passive=bool(target.get("passive")), role=target.get("role")), "alert",
                      apns_priority(card), collapse_id(card))


def collapse_id(card: dict[str, Any]) -> str:
    """apns-collapse-id: stable per card key, but a hash, so no key's words ever reach Apple (10-07)."""
    import hashlib
    return hashlib.sha1(str(card.get("key") or card["id"]).encode()).hexdigest()[:32]


def _curl_sync(target: dict[str, Any]) -> tuple[int, str]:
    """The continuity nudge: a background push (Apple wants priority 5 for those)."""
    return _curl_post(target, SYNC_PAYLOAD, "background", "5", "sync")


def _curl_post(target: dict[str, Any], body: dict[str, Any], push_type: str, prio: str, collapse: str) -> tuple[int, str]:
    cmd = ["curl", "--http2", "-s", "-m", "10", "-o", "-", "-w", "\n%{http_code}",
           "-H", f"authorization: bearer {_jwt()}", "-H", f"apns-push-type: {push_type}",
           "-H", f"apns-topic: {target['bundle']}", "-H", f"apns-collapse-id: {collapse}",
           "-H", f"apns-priority: {prio}", "--data-binary", "@-",
           f"{HOSTS[target['environment']]}/3/device/{target['token']}"]
    r = subprocess.run(cmd, input=json.dumps(body).encode(), capture_output=True, timeout=15)
    body, _, code = r.stdout.decode(errors="replace").rpartition("\n")
    try:
        status = int(code.strip())
    except ValueError:
        return 0, (r.stderr.decode(errors="replace") or "curl failed")[:200]
    reason = ""
    try:
        reason = json.loads(body).get("reason", "") if body.strip() else ""
    except ValueError:
        pass
    return status, reason


# ── driving now (jellybean#310) ─────────────────────────────────────────────────────────────────

DRIVING_TTL_S = 45 * 60   # a phone that never says it stopped: its pushes come through again after this


def _urgent(card: dict[str, Any]) -> bool:
    """Comes through even while driving: urgency 3, or a "leave now" card."""
    return int(card.get("urgency") or 0) >= 3 or str(card.get("key") or "").startswith("leave-")


def set_driving(device_id: str, driving: bool, *, now: float | None = None,
                send: Callable[[dict[str, Any], dict[str, Any]], tuple[int, str]] | None = None,
                db: Path | None = None) -> list[str]:
    """The phone says it's driving (or not, PATCH /v1/devices/self {motion}). Driving: its important-but-not-urgent card
    pushes are held. Stopped: the held ones still open go now. Returns what was sent ("<card>: <status>")."""
    now = now or time.time()
    with _lock:
        con = _db(db)
        try:
            if driving:
                con.execute("INSERT INTO device_driving (device_id, until) VALUES (?,?) "
                            "ON CONFLICT(device_id) DO UPDATE SET until=excluded.until", (device_id, now + DRIVING_TTL_S))
            else:
                con.execute("DELETE FROM device_driving WHERE device_id=?", (device_id,))
            con.commit()
        finally:
            con.close()
    return [] if driving else flush_held(device_id, send=send, db=db)


def is_driving(device_id: str, now: float | None = None, db: Path | None = None) -> bool:
    con = _db(db)
    try:
        row = con.execute("SELECT until FROM device_driving WHERE device_id=?", (device_id,)).fetchone()
    finally:
        con.close()
    return bool(row) and row[0] > (now or time.time())


def _hold(device_id: str, card: dict[str, Any], db: Path | None = None) -> None:
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT OR REPLACE INTO held_pushes (device_id, card_id, card, at) VALUES (?,?,?,?)",
                        (device_id, str(card["id"]), json.dumps({k: card.get(k) for k in ("id", "key", "urgency", "group", "to")}),
                         time.time()))
            con.commit()
        finally:
            con.close()


def flush_held(device_id: str, *, send: Callable[[dict[str, Any], dict[str, Any]], tuple[int, str]] | None = None,
               db: Path | None = None) -> list[str]:
    """The held pushes of a device that stopped (or whose driving expired): each card still open goes now; one acked
    or gone meanwhile is dropped."""
    import proactive
    with _lock:
        con = _db(db)
        try:
            rows = con.execute("SELECT card_id, card FROM held_pushes WHERE device_id=? ORDER BY at", (device_id,)).fetchall()
            con.execute("DELETE FROM held_pushes WHERE device_id=?", (device_id,))
            con.commit()
            # Muted since the alerts were held: they're dropped, never sent later (jellybean#786 row 1).
            tok = con.execute("SELECT device_id, token, environment, bundle FROM push_tokens WHERE device_id=? AND card_alerts=1",
                              (device_id,)).fetchone()
        finally:
            con.close()
    if not rows or not tok or (send is None and not configured()):
        return []
    open_ids = {str(c.get("id")) for c in proactive.list_cards()}
    target = {"device_id": tok[0], "token": tok[1], "environment": tok[2], "bundle": tok[3]}
    out = []
    for card_id, blob in rows:
        if card_id not in open_ids:
            continue
        try:
            status, reason = (send or _curl_send)(target, json.loads(blob))
        except Exception as exc:  # noqa: BLE001
            status, reason = 0, type(exc).__name__
        out.append(f"{card_id}: {status}{(' ' + reason) if reason else ''}")
    return out


def push_card(card: dict[str, Any], *, devices: list[dict[str, Any]] | None = None,
              send: Callable[[dict[str, Any], dict[str, Any]], tuple[int, str]] | None = None,
              db: Path | None = None) -> list[str]:
    """Send a card's content-free notification to every device that should get it. Returns
    "<device>: <status>" lines. Dead tokens (410, BadDeviceToken, Unregistered) are dropped. A phone that's driving
    (jellybean#310) gets only urgent ones; the rest are held until it stops ("<device>: held")."""
    if not should_push(card):
        return []
    if send is None and not configured():
        return []
    out = []
    for t in targets(card, devices if devices is not None else _household_devices(), db):
        if not _urgent(card) and is_driving(t["device_id"], db=db):
            _hold(t["device_id"], card, db)
            out.append(f"{t['device_id']}: held")
            continue
        t = {**t, "passive": active_elsewhere(t["person_id"], t["device_id"], db=db)}
        try:
            status, reason = (send or _curl_send)(t, card)
        except Exception as exc:  # noqa: BLE001 - one phone failing never stops the rest
            status, reason = 0, type(exc).__name__
        if status == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
            unregister(t["device_id"], db)
        out.append(f"{t['device_id']}: {status}{(' ' + reason) if reason else ''}")
    return out


def push_card_async(card: dict[str, Any]) -> None:
    """From proactive.add_card: never blocks card creation; a non-daemon thread so a timer script
    that adds a card and exits still sends it."""
    if not should_push(card) or not configured():
        return
    import egress   # sent on another thread: noted here, on the turn's (#409)
    egress.note("push")

    def run() -> None:
        try:
            for line in push_card(card):
                print(f"[push] {card['id']} → {line}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[push] {card.get('id')}: {type(exc).__name__}: {exc}"[:200], file=sys.stderr)

    threading.Thread(target=run, name="apns-push", daemon=False).start()


def nudge_sync(person: str, except_device: str | None = None, *, devices: list[dict[str, Any]] | None = None,
               send: Callable[[dict[str, Any]], tuple[int, str]] | None = None, db: Path | None = None) -> list[str]:
    """Nudge `person`'s other phones to fetch /v1/sync (#419). Returns "<device>: <status>" lines; dead
    tokens are dropped. A nudge counts against the coalescing window even when it fails."""
    if send is None and not configured():
        return []
    out = []
    for t in sync_targets(person, except_device, devices if devices is not None else _household_devices(), db):
        _stamp("sync_nudged", t["device_id"], db)
        try:
            status, reason = (send or _curl_sync)(t)
        except Exception as exc:  # noqa: BLE001
            status, reason = 0, type(exc).__name__
        if status == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
            unregister(t["device_id"], db)
        out.append(f"{t['device_id']}: {status}{(' ' + reason) if reason else ''}")
    return out


def nudge_sync_async(person: str, except_device: str | None = None) -> None:
    """From continuity.emit: never blocks the change."""
    if not configured():
        return

    def run() -> None:
        try:
            for line in nudge_sync(person, except_device):
                print(f"[push] sync {person} → {line}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[push] sync {person}: {type(exc).__name__}: {exc}"[:200], file=sys.stderr)

    threading.Thread(target=run, name="apns-sync", daemon=False).start()


def clear_targets(person: str, except_device: str | None, devices: list[dict[str, Any]],
                  db: Path | None = None, *, household: bool = False) -> list[dict[str, Any]]:
    """`person`'s registered phones to clear a card on: not the one that dealt with it, not a shared
    household device (unless `household`: an expired card leaves the shared screen too), not one whose app is open
    (its live cards already drop it)."""
    mine = {d["id"] for d in devices if d["person_id"] == person and not d.get("revoked")
            and (household or d["role"] != "household")}
    return [t for t in _tokens(db) if t["device_id"] in mine and t["device_id"] != except_device and not t["live"]]


def clear_card(person: str, card_id: str, except_device: str | None = None, *, devices: list[dict[str, Any]] | None = None,
               send: Callable[[dict[str, Any], dict[str, Any]], tuple[int, str]] | None = None,
               db: Path | None = None, household: bool = False) -> list[str]:
    """Tell `person`'s other phones a card was dealt with (jellybean#204): its notification goes."""
    if send is None and not configured():
        return []
    body = clear_payload(card_id)
    out = []
    for t in clear_targets(person, except_device, devices if devices is not None else _household_devices(), db,
                           household=household):
        try:
            status, reason = (send or (lambda tt, b: _curl_post(tt, b, "background", "5", ("clear-" + card_id)[:64])))(t, body)
        except Exception as exc:  # noqa: BLE001
            status, reason = 0, type(exc).__name__
        if status == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
            unregister(t["device_id"], db)
        out.append(f"{t['device_id']}: {status}{(' ' + reason) if reason else ''}")
    return out


def clear_card_async(person: str, card_id: str, except_device: str | None = None) -> None:
    """From the card endpoints: never blocks the answer."""
    if not configured():
        return

    def run() -> None:
        try:
            for line in clear_card(person, card_id, except_device):
                print(f"[push] clear {card_id} → {line}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[push] clear {card_id}: {type(exc).__name__}: {exc}"[:200], file=sys.stderr)

    threading.Thread(target=run, name="apns-clear", daemon=False).start()


EXPIRY_WINDOW_S = 600   # cards that expired this recently are swept; older ones were swept already (or titan was down)
_expiry_cleared: dict[str, float] = {}   # card id → its expiry: cleared already, kept while it's inside the window


def clear_expired(now: float | None = None, *, devices: list[dict[str, Any]] | None = None,
                  send: Callable[[dict[str, Any], dict[str, Any]], tuple[int, str]] | None = None,
                  db: Path | None = None, proactive_db: Path | None = None,
                  window: float = EXPIRY_WINDOW_S) -> list[str]:
    """A card that expired leaves the phones it was for: each person (and shared screen) whose audience includes it
    gets a silent "clear" for it, once (the API's 20 s loop). Only cards that could have pushed (should_push): the rest
    never put a notification there. Returns "<card> <device>: <status>" lines."""
    if send is None and not configured():
        return []
    import proactive
    now = time.time() if now is None else now
    for cid, at in list(_expiry_cleared.items()):
        if at <= now - window:
            del _expiry_cleared[cid]   # out of the window: the query below can't return it again
    out: list[str] = []
    for card in proactive.expired_between(now - window, now, db=proactive_db):
        if card["id"] in _expiry_cleared or not should_push(card):
            continue
        _expiry_cleared[card["id"]] = float(card["expires_at"])
        if devices is None:
            devices = _household_devices()
        people = sorted({d["person_id"] for d in devices if not d.get("revoked") and proactive.visible_to(
            card.get("to") or "admins", "household" if d["role"] == "household" else d["person_id"], d["role"])})
        for person in people:
            for line in clear_card(person, card["id"], None, devices=devices, send=send, db=db, household=True):
                out.append(f"{card['id']} {line}")
    return out


# ── Live Activities (#725) ──────────────────────────────────────────────────────────────────────

ACTIVITY_KINDS = ("task", "trip", "agent")   # agent: a coding agent on the Mac (jellybean#699)
_REF_RX = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
REFERENCE_EPOCH = 978307200   # 2001-01-01 UTC: a Swift Codable Date in content-state is seconds since then


def clean_trip(trip: Any) -> tuple[float, int] | None:
    """A trip token's {lead, minutes} (jellybean#753): lead in seconds (≤ 1 day), travel minutes (≤ 1 day)."""
    if trip is None:
        return None
    if not isinstance(trip, dict):
        raise ValueError("trip must be {lead, minutes}")
    lead, minutes = trip.get("lead"), trip.get("minutes")
    if isinstance(lead, bool) or not isinstance(lead, (int, float)) or not 0 <= float(lead) <= 86400:
        raise ValueError("trip.lead must be seconds")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 1440:
        raise ValueError("trip.minutes must be whole minutes")
    return float(lead), int(minutes)


def register_activity(device_id: str, person: str, role: str, kind: Any, ref: Any, token: Any, environment: Any,
                      bundle: Any = None, db: Path | None = None, trip: Any = None) -> bool:
    """One activity's push token, replaced when the phone rotates it (pushTokenUpdates). token None/"" removes it
    (the activity ended); returns whether a row was removed then. Never for a shared household device. The bundle
    defaults to this device's card-push bundle."""
    if role == "household":
        raise ValueError("a shared household device has no Live Activities")
    k, r = str(kind or "").strip().lower(), str(ref or "").strip()
    if k in START_KINDS.values():
        r = "*"   # the device's push-to-start token (pushToStartTokenUpdates): one per device; the app's ref is ignored
    elif k not in ACTIVITY_KINDS:
        raise ValueError("kind must be task, trip, agent, task-start, agent-start or trip-start")
    if r != "*" and not _REF_RX.fullmatch(r):
        raise ValueError("ref must be the task or trip id")
    details = clean_trip(trip) if k == "trip" else None
    tok = str(token or "").strip()
    if not tok:
        return unregister_activity(device_id, k, r, db)
    env = str(environment or "").strip().lower()
    if not _TOKEN_RX.fullmatch(tok):
        raise ValueError("token must be the activity's push token in hex")
    if env not in HOSTS:
        raise ValueError("environment must be development or production")
    bun = str(bundle or "").strip()
    with _lock:
        con = _db(db)
        try:
            if not bun:
                row = con.execute("SELECT bundle FROM push_tokens WHERE device_id=?", (device_id,)).fetchone()
                bun = row[0] if row else ""
            if not _BUNDLE_RX.fullmatch(bun):
                raise ValueError("bundle must be the app's bundle id (or register for card push first)")
            con.execute("INSERT INTO activity_tokens VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(device_id, kind, ref) DO "
                        "UPDATE SET person=excluded.person, token=excluded.token, environment=excluded.environment, "
                        "bundle=excluded.bundle, updated=excluded.updated",
                        (device_id, str(person).lower(), k, r, tok.lower(), env, bun, time.time()))
            if details is not None:
                con.execute("INSERT OR REPLACE INTO trip_details VALUES (?,?,?,?,?)",
                            (device_id, r, details[0], details[1], time.time()))
            con.commit()
        finally:
            con.close()
    return True


def unregister_activity(device_id: str, kind: str, ref: str, db: Path | None = None) -> bool:
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM activity_tokens WHERE device_id=? AND kind=? AND ref=?",
                            (device_id, kind, ref)).rowcount
            if kind == "trip":
                con.execute("DELETE FROM trip_details WHERE device_id=? AND ref=?", (device_id, ref))
            con.commit()
            return n > 0
        finally:
            con.close()


def forget_activities(device_id: str | None = None, *, kind: str | None = None, ref: str | None = None,
                      db: Path | None = None) -> int:
    """A revoked device's activities, or every device's for one ended task (kind + ref)."""
    with _lock:
        con = _db(db)
        try:
            if device_id:
                n = con.execute("DELETE FROM activity_tokens WHERE device_id=?", (device_id,)).rowcount
                con.execute("DELETE FROM trip_details WHERE device_id=?", (device_id,))
                con.execute("DELETE FROM trip_plans WHERE device_id=?", (device_id,))
            else:
                n = con.execute("DELETE FROM activity_tokens WHERE kind=? AND ref=?", (kind, ref)).rowcount
                if kind == "trip":
                    con.execute("DELETE FROM trip_details WHERE ref=?", (ref,))
            con.commit()
            return n
        finally:
            con.close()


def activity_targets(owner: str, kind: str, ref: str, devices: list[dict[str, Any]],
                     db: Path | None = None) -> list[dict[str, Any]]:
    """The owner's own live devices showing this activity: registered by the owner, on a device that's still
    theirs, not revoked and not a shared household device."""
    me = str(owner or "").lower()
    mine = {d["id"] for d in devices
            if str(d["person_id"]).lower() == me and not d.get("revoked") and d["role"] != "household"}
    con = _db(db)
    try:
        rows = con.execute("SELECT device_id, person, token, environment, bundle FROM activity_tokens "
                           "WHERE kind=? AND ref=?", (kind, ref)).fetchall()
    finally:
        con.close()
    return [{"device_id": d, "ref": ref, "token": t, "environment": e, "bundle": b}
            for d, p, t, e, b in rows if p == me and d in mine]


def trip_targets(owner: str, ref: str, devices: list[dict[str, Any]], db: Path | None = None) -> list[dict[str, Any]]:
    """The owner's own devices showing this trip (activity_targets), each with its lead and minutes (jellybean#753).
    A device that registered no details is left out: titan can't say its leave-by."""
    targets = activity_targets(owner, "trip", ref, devices, db)
    con = _db(db)
    try:
        det = {d: (l, m) for d, l, m in con.execute("SELECT device_id, lead, minutes FROM trip_details WHERE ref=?", (ref,))}
    finally:
        con.close()
    return [{**t, "lead": det[t["device_id"]][0], "minutes": det[t["device_id"]][1]} for t in targets if t["device_id"] in det]


def rekey_trip(owner: str, old: str, new: str, db: Path | None = None) -> int:
    """A moved event's trip tokens follow it to its new ref, so a second move is noticed too. The owner's only."""
    with _lock:
        con = _db(db)
        try:
            devs = [d for (d,) in con.execute("SELECT device_id FROM activity_tokens WHERE kind='trip' AND ref=? AND person=?",
                                              (old, str(owner).lower()))]
            for d in devs:
                con.execute("DELETE FROM activity_tokens WHERE device_id=? AND kind='trip' AND ref=?", (d, new))
                con.execute("UPDATE activity_tokens SET ref=? WHERE device_id=? AND kind='trip' AND ref=?", (new, d, old))
                con.execute("DELETE FROM trip_details WHERE device_id=? AND ref=?", (d, new))
                con.execute("UPDATE trip_details SET ref=? WHERE device_id=? AND ref=?", (new, d, old))
            con.commit()
            return len(devs)
        finally:
            con.close()


GOAL_CHARS = 60


def task_state(step: int, total: int, *, title: str = "", waiting: str | None = None, done: bool = False,
               next_at: float | None = None, detail: bool = False, goal: str = "") -> dict[str, Any]:
    """TaskActivityAttributes.ContentState. `goal` always (the task's, redacted, ≤ GOAL_CHARS; Zach 10-03: shown by
    default, like a calendar title). Without `detail` (the owner's live_activity_detail preference) nothing of the
    step's own words: a blank stepTitle and the fixed "Waiting on you."."""
    import redact
    g = " ".join(redact.redact_personal(str(goal or "")).split())
    g = g if len(g) <= GOAL_CHARS else g[:GOAL_CHARS - 1].rstrip() + "…"
    st: dict[str, Any] = {"goal": g, "step": max(0, int(step)), "total": max(0, int(total)),
                          "stepTitle": " ".join(str(title or "").split())[:80] if detail else "",
                          "done": bool(done)}
    if waiting is not None:
        st["waiting"] = " ".join(str(waiting).split())[:80] if detail and waiting else "Waiting on you."
    if next_at:
        st["nextAt"] = float(next_at) - REFERENCE_EPOCH
    return st


def activity_payload(state: dict[str, Any], event: str = "update", now: float | None = None,
                     dismiss_after: float | None = 15 * 60) -> dict[str, Any]:
    """An ActivityKit push: update, or end (dismissed from the Lock Screen after `dismiss_after` seconds; None: iOS's
    default policy, so the person sees it ended and why). `timestamp` and `dismissal-date` are Unix seconds."""
    now = now or time.time()
    aps: dict[str, Any] = {"timestamp": int(now), "event": event, "content-state": state}
    if event == "end" and dismiss_after is not None:
        aps["dismissal-date"] = int(now + dismiss_after)
    return {"aps": aps}


START_KIND = "task-start"
START_ATTRIBUTES_TYPE = "TaskActivityAttributes"
# Each activity kind that titan can start while the app is closed, and its push-to-start token's kind.
START_KINDS = {"task": START_KIND, "agent": "agent-start", "trip": "trip-start"}


def start_payload(state: dict[str, Any], attributes: dict[str, Any], now: float | None = None, *,
                  attributes_type: str = START_ATTRIBUTES_TYPE, alert: dict[str, str] | None = None) -> dict[str, Any]:
    """An ActivityKit push-to-start (jellybean#350): starts an activity while the app is closed. The alert is
    required for a start; a task's says its goal (already redacted in `state`) and a fixed phrase."""
    now = now or time.time()
    if alert is None:
        body = "Waiting on you." if state.get("waiting") else (
            f"Step {state['step']} of {state['total']}" if state.get("total") else "Working on it")
        alert = {"title": state.get("goal") or "Task", "body": body}
    return {"aps": {"timestamp": int(now), "event": "start", "content-state": state,
                    "attributes-type": attributes_type, "attributes": attributes, "alert": alert,
                    "input-push-token": 1}}   # iOS gives the new activity's update token to the app, which registers it


def start_targets(owner: str, ref: str, devices: list[dict[str, Any]], db: Path | None = None,
                  kind: str = "task") -> list[dict[str, Any]]:
    """The owner's own live devices that can start this activity: a push-to-start token, no activity of it yet (no
    per-activity token), and no start already sent for it."""
    me = str(owner or "").lower()
    mine = {d["id"] for d in devices
            if str(d["person_id"]).lower() == me and not d.get("revoked") and d["role"] != "household"}
    con = _db(db)
    try:
        rows = con.execute("SELECT device_id, person, token, environment, bundle FROM activity_tokens "
                           "WHERE kind=? AND ref='*'", (START_KINDS[kind],)).fetchall()
        live = {d for (d,) in con.execute("SELECT device_id FROM activity_tokens WHERE kind=? AND ref=?", (kind, ref))}
        sent = {d for (d,) in con.execute("SELECT device_id FROM activity_starts WHERE ref=?", (ref,))}
    finally:
        con.close()
    return [{"device_id": d, "ref": ref, "token": t, "environment": e, "bundle": b}
            for d, p, t, e, b in rows if p == me and d in mine and d not in live and d not in sent]


def _mark_started(device_id: str, ref: str, db: Path | None = None) -> None:
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT OR REPLACE INTO activity_starts VALUES (?,?,?)", (device_id, ref, time.time()))
            con.commit()
        finally:
            con.close()


def _forget_starts(ref: str, db: Path | None = None) -> None:
    with _lock:
        con = _db(db)
        try:
            con.execute("DELETE FROM activity_starts WHERE ref=?", (ref,))
            con.commit()
        finally:
            con.close()


def _curl_activity(target: dict[str, Any], body: dict[str, Any]) -> tuple[int, str]:
    end = body["aps"].get("event") == "end"
    return _curl_post({**target, "bundle": f"{target['bundle']}.push-type.liveactivity"}, body, "liveactivity",
                      "10" if end else "5", f"la-{target.get('ref') or target['device_id']}"[:64])


def send_activity(owner: str, kind: str, ref: str, state: dict[str, Any], event: str = "update", *,
                  dismiss_after: float | None = None, devices: list[dict[str, Any]] | None = None,
                  send: Callable[[dict[str, Any], dict[str, Any]], tuple[int, str]] | None = None,
                  db: Path | None = None, start: dict[str, Any] | None = None,
                  start_type: str = START_ATTRIBUTES_TYPE, start_alert: dict[str, str] | None = None) -> list[str]:
    """Update (or end) one activity on its owner's devices. Dead tokens go; at the end every token for it goes.
    `start` (the activity's attributes: a task's {taskID, goal}, an agent run's {runID, repo, mac}): on an update, the
    owner's devices with a push-to-start token and no activity of it yet get a "start" push instead (jellybean#350);
    the app then registers that activity's own token and the updates go there."""
    if send is None and not configured():
        return []
    body = activity_payload(state, event, **({"dismiss_after": dismiss_after} if dismiss_after is not None else {}))
    devs = devices if devices is not None else _household_devices()
    out = []
    if start is not None and kind in START_KINDS and event == "update":
        sbody = start_payload(state, start, attributes_type=start_type, alert=start_alert)
        for t in start_targets(owner, ref, devs, db, kind):
            try:
                status, reason = (send or _curl_activity)(t, sbody)
            except Exception as exc:  # noqa: BLE001
                status, reason = 0, type(exc).__name__
            if status == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken"):
                unregister_activity(t["device_id"], START_KINDS[kind], "*", db)
            elif 200 <= status < 300:
                _mark_started(t["device_id"], ref, db)
            out.append(f"{t['device_id']}: start {status}{(' ' + reason) if reason else ''}")
    for t in activity_targets(owner, kind, ref, devs, db):
        try:
            status, reason = (send or _curl_activity)(t, body)
        except Exception as exc:  # noqa: BLE001 - one phone failing never stops the rest
            status, reason = 0, type(exc).__name__
        if status == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken"):
            unregister_activity(t["device_id"], kind, ref, db)
        out.append(f"{t['device_id']}: {status}{(' ' + reason) if reason else ''}")
    if event == "end":
        forget_activities(kind=kind, ref=ref, db=db)
        _forget_starts(ref, db)
    return out


def send_activity_async(owner: str, kind: str, ref: str, state: dict[str, Any], event: str = "update",
                        dismiss_after: float | None = None, start: dict[str, Any] | None = None,
                        start_type: str = START_ATTRIBUTES_TYPE, start_alert: dict[str, str] | None = None) -> None:
    """From the task tools: never blocks the step."""
    if not configured():
        return
    import egress   # noted on the turn's thread (#409)
    egress.note("push")

    def run() -> None:
        try:
            for line in send_activity(owner, kind, ref, state, event, dismiss_after=dismiss_after, start=start,
                                      start_type=start_type, start_alert=start_alert):
                print(f"[push] activity {kind}:{ref} {event} → {line}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[push] activity {kind}:{ref}: {type(exc).__name__}: {exc}"[:200], file=sys.stderr)

    threading.Thread(target=run, name="apns-activity", daemon=False).start()


def nudge_inbox(device_id: str, *, send: Callable[[dict[str, Any], dict[str, Any]], tuple[int, str]] | None = None,
                db: Path | None = None) -> list[str]:
    """Wake one iPhone to drain its /v1/device/inbox (#776: titan asked it for an item it holds): a background push
    with nothing but "inbox", priority 5, collapse id inbox. Only that device's own token; a dead one goes."""
    if send is None and not configured():
        return []
    t = next((x for x in _tokens(db) if x["device_id"] == device_id), None)
    if t is None:
        return []
    try:
        status, reason = (send or (lambda tg, body: _curl_post(tg, body, "background", "5", "inbox")))(t, INBOX_PAYLOAD)
    except Exception as exc:  # noqa: BLE001 - a push that fails is a phone that doesn't wake
        status, reason = 0, type(exc).__name__
    if status == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
        unregister(device_id, db)
    return [f"{device_id}: {status}{(' ' + reason) if reason else ''}"]


# ── Trips started while the app is closed (jellybean#753 push-to-start, contract on that issue 10-07) ──────────────
#
# iOS lets only an open app start a Live Activity, so the phone tells titan its upcoming trips ahead (POST
# v1/devices/self/trips) and gives titan its TripActivityAttributes push-to-start token (kind "trip-start"). An hour
# before leave-by, if that device shows no activity for the trip yet, titan starts it. Decided 10-07 (option A, the
# 10-04 rule): the start carries no title and no place; the Lock Screen says "Your next event", leave-by and minutes.

TRIP_START_BEFORE_S = 3600
TRIP_MODES = {"car": "car.fill", "transit": "tram.fill", "walk": "figure.walk"}
TRIP_PLANS_PER_DEVICE = 20
TRIP_START_ALERT = {"title": "Time to leave soon", "body": "Your trip is on the Lock Screen."}


def register_trip_plan(device_id: str, person: str, role: str, body: Any, db: Path | None = None,
                       now: float | None = None) -> None:
    """{ref, leave_by (Unix seconds), lead (s), minutes, mode}: kept until the event starts. Never a shared screen."""
    if role == "household":
        raise ValueError("a shared household device has no Live Activities")
    if not isinstance(body, dict):
        raise ValueError("expected {ref, leave_by, lead, minutes, mode}")
    now = time.time() if now is None else now
    ref = str(body.get("ref") or "").strip()
    if not _REF_RX.fullmatch(ref):
        raise ValueError("ref must be the trip's ref")
    lead, minutes = clean_trip({"lead": body.get("lead"), "minutes": body.get("minutes")}) or (0.0, 0)
    leave_by = body.get("leave_by")
    if isinstance(leave_by, bool) or not isinstance(leave_by, (int, float)) or not now - 86400 <= float(leave_by) <= now + 8 * 86400:
        raise ValueError("leave_by must be Unix seconds within the next week")
    mode = str(body.get("mode") or "car").strip().lower()
    if mode not in TRIP_MODES:
        raise ValueError("mode must be car, transit or walk")
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT OR REPLACE INTO trip_plans VALUES (?,?,?,?,?,?,?,?)",
                        (device_id, str(person).lower(), ref, float(leave_by), lead, minutes, mode, now))
            # At most a handful per device: the oldest go.
            con.execute("DELETE FROM trip_plans WHERE device_id=? AND ref NOT IN (SELECT ref FROM trip_plans WHERE device_id=? "
                        "ORDER BY updated DESC LIMIT ?)", (device_id, device_id, TRIP_PLANS_PER_DEVICE))
            con.commit()
        finally:
            con.close()


def remove_trip_plan(device_id: str, ref: str, db: Path | None = None) -> bool:
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM trip_plans WHERE device_id=? AND ref=?", (device_id, str(ref or ""))).rowcount
            con.commit()
            return n > 0
        finally:
            con.close()


def trip_planned(owner: str, ref: str, db: Path | None = None) -> bool:
    con = _db(db)
    try:
        return con.execute("SELECT 1 FROM trip_plans WHERE ref=? AND person=? LIMIT 1", (ref, str(owner).lower())).fetchone() is not None
    finally:
        con.close()


def trip_plan_moved(owner: str, old: str, new: str, new_start: float, db: Path | None = None) -> int:
    """The event moved (trip_push.judge): the owner's plans follow it, with leave-by from the new start."""
    with _lock:
        con = _db(db)
        try:
            rows = con.execute("SELECT device_id, lead FROM trip_plans WHERE ref=? AND person=?", (old, str(owner).lower())).fetchall()
            for d, lead in rows:
                con.execute("DELETE FROM trip_plans WHERE device_id=? AND ref=?", (d, new))
                con.execute("UPDATE trip_plans SET ref=?, leave_by=? WHERE device_id=? AND ref=?", (new, new_start - lead, d, old))
                con.execute("DELETE FROM activity_starts WHERE device_id=? AND ref=?", (d, old))
            con.commit()
            return len(rows)
        finally:
            con.close()


def trip_plan_gone(owner: str, ref: str, db: Path | None = None) -> int:
    """The event was cancelled: nothing starts for it."""
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM trip_plans WHERE ref=? AND person=?", (ref, str(owner).lower())).rowcount
            con.commit()
            return n
        finally:
            con.close()


def trip_start_payload(ref: str, leave_by: float, lead: float, minutes: int, mode: str, now: float | None = None) -> dict[str, Any]:
    """The push that starts a trip: TripActivityAttributes with no title or place (option A), and its first state."""
    attributes = {"eventID": ref, "eventTitle": "", "place": "", "eventStart": leave_by + lead - REFERENCE_EPOCH, "pushRef": ref}
    state = {"leaveBy": leave_by - REFERENCE_EPOCH, "minutes": int(minutes), "symbol": TRIP_MODES.get(mode, "car.fill")}
    return start_payload(state, attributes, now, attributes_type="TripActivityAttributes", alert=dict(TRIP_START_ALERT))


def send_trip_starts(now: float | None = None, *, devices: list[dict[str, Any]] | None = None,
                     send: Callable[[dict[str, Any], dict[str, Any]], tuple[int, str]] | None = None,
                     db: Path | None = None) -> list[str]:
    """Every 30 s (the routine nudger's loop): each plan whose leave-by is within the hour gets one start on its own
    device, if that device has a trip-start token and shows no activity for it yet. Plans whose event has started go."""
    if send is None and not configured():
        return []
    now = time.time() if now is None else now
    with _lock:
        con = _db(db)
        try:
            con.execute("DELETE FROM trip_plans WHERE leave_by + lead <= ?", (now,))
            con.commit()
            plans = con.execute("SELECT device_id, person, ref, leave_by, lead, minutes, mode FROM trip_plans "
                                "WHERE leave_by - ? <= ?", (TRIP_START_BEFORE_S, now)).fetchall()
            starts = {(d, t, e, b) for d, t, e, b in con.execute(
                "SELECT device_id, token, environment, bundle FROM activity_tokens WHERE kind='trip-start' AND ref='*'")}
            live = {(d, r) for d, r in con.execute("SELECT device_id, ref FROM activity_tokens WHERE kind='trip'")}
            sent = {(d, r) for d, r in con.execute("SELECT device_id, ref FROM activity_starts")}
        finally:
            con.close()
    if not plans:
        return []
    devs = devices if devices is not None else _household_devices()
    owner_of = {d["id"]: str(d["person_id"]).lower() for d in devs if not d.get("revoked") and d["role"] != "household"}
    token_of = {d: (t, e, b) for d, t, e, b in starts}
    out = []
    for d, person, ref, leave_by, lead, minutes, mode in plans:
        if owner_of.get(d) != person or d not in token_of or (d, ref) in live or (d, ref) in sent:
            continue
        tok, env, bun = token_of[d]
        target = {"device_id": d, "ref": ref, "token": tok, "environment": env, "bundle": bun}
        try:
            status, reason = (send or _curl_activity)(target, trip_start_payload(ref, leave_by, lead, minutes, mode, now))
        except Exception as exc:  # noqa: BLE001 - one phone failing never stops the rest
            status, reason = 0, type(exc).__name__
        if status == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken"):
            unregister_activity(d, "trip-start", "*", db)
        elif 200 <= status < 300:
            _mark_started(d, ref, db)
        out.append(f"{d}: trip start {status}{(' ' + reason) if reason else ''}")
    return out
