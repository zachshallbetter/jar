#!/usr/bin/env python3
"""Device health: titan notices a device or sync problem before the person does, and tells them.

Every 15 minutes (titan-device-health.timer) this reads what titan already knows, the same signals as
`capabilities`, and sends a card only when something CHANGES: a new problem, or one that has
cleared. Never a repeat. Each person's devices are theirs (plan item 9): a problem with a device
goes to its owner only (to=[person id]); a household-wide problem (Home Assistant, a titan service,
a shared household device) goes to the admins. Cards are keyed health-<person>-<thing>, so a change
updates the card in place.

  device        a paired device that usually checks in hasn't for DEVICE_QUIET_H
  mac           the person's Mac can't be reached or woken from their phone for MAC_QUIET_H
  battery       the person's Mac is at LOW_BATTERY_PCT or less and not plugged in (v2: Jellybean 0.12.6+
                heartbeat; a reading older than FRESH_H is ignored, so a sleeping Mac's last one never cards)
  disk          the person's Mac has under LOW_DISK_GB free (same heartbeat, same freshness)
  task          a multi-step task (plan_task) that isn't finished hasn't moved in STALL_H, or failed (to its
                owner, from the task's metadata; one from before owners were recorded goes to the admin;
                09-29: a task sat at step 1 for ~8 h and nothing noticed)
  sync          a Jellybean sync source that was syncing has been quiet for SYNC_STALE_H (to the admin,
                whose Mac syncs)
  home          Home Assistant has a token but isn't answering (admins)
  backup        a box's encrypted backups haven't been opened with the recovery phrase in VERIFY_DUE_DAYS (or ever):
                the box can't check its own (#1187), so its owner (the admin) is asked to, once; never on titan,
                which has no box backups
  listener      a box (BOX_SETUP=1, never titan) has something listening beyond loopback that its firewall allowlist
                doesn't expect (box_firewall.unexpected, #407): to the admin, by port and process name only (never a
                command line); cleared when it stops
  service       one of titan's models or runtimes is down (admins); each is tried SERVICE_TRIES times
                SERVICE_GAP_S apart with a SERVICE_TIMEOUT_S timeout before it counts as down

No flapping (10-01: the first cycle sent "titan's Decider is down" from one slow answer): a problem
sends a card only once it's been seen on CONFIRM_RUNS consecutive runs; a recovery clears on the
first good run.

Quiet hours, 22:00-07:00 household time: a new problem waits until morning unless it has lasted
QUIET_OVERRIDE_H; a cleared one waits too. State is kept in DEVICE_HEALTH_STATE.

  python3 scripts/device_health.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

STATE_PATH = Path(os.environ.get("DEVICE_HEALTH_STATE", "/ai/datasets/connectors/device_health.json"))
DEVICE_QUIET_H = 24.0
MAC_QUIET_H = 6.0
SYNC_STALE_H = 12.0
QUIET_OVERRIDE_H = 3.0
QUIET = (22, 7)          # household-local hours: from 22:00 to 07:00
LOW_BATTERY_PCT = 15
LOW_DISK_GB = 10.0
FRESH_H = 1.0            # battery and disk readings only from a heartbeat this recent
STALL_H = 6.0
TASK_DONE = ("completed", "complete", "done", "cancelled", "canceled", "closed")
TASK_CLOSED = ("paused",)   # close_task pauses the instance; nothing else pauses one
CONFIRM_RUNS = 2         # a problem must be seen this many runs in a row before it's a card
SERVICE_TRIES, SERVICE_GAP_S, SERVICE_TIMEOUT_S = 3, 2.0, 3.0
SYNC_SOURCES = ("calendar", "mail", "downloads", "browsing")
# jellybean#413: the Mac says how each source is (its presence heartbeat's "sources"); these get a card when one
# can't be read: waiting (the app isn't open) this long, or needing the person's OK.
SOURCE_CARDS = {"mail": "Mail"}
SOURCE_WAITING_H = 2.0
NEW_ITEM_SOURCES = ("mail",)   # sources that bring something new every day; quiet this long → said (#648)
NEW_ITEM_QUIET_H = float(os.environ.get("DEVICE_HEALTH_MAIL_QUIET_H", "18"))


@dataclass
class Finding:
    key: str             # health-<person or household>-<thing>
    to: Any              # [person id] or "admins"
    problem: bool
    title: str
    detail: str


def _age_h(stamp: Any, now: float) -> float | None:
    """Hours since an ISO timestamp. The Rust runtimes write nanoseconds (".487314493+00:00"), which
    datetime.fromisoformat rejects: the fraction is cut to microseconds first."""
    import re
    from datetime import datetime
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        return (now - float(stamp)) / 3600   # an epoch time (the Mac's source states keep these)
    try:
        s = re.sub(r"(\.\d{6})\d+", r"\1", str(stamp).replace("Z", "+00:00"))
        return (now - datetime.fromisoformat(s).timestamp()) / 3600
    except (TypeError, ValueError):
        return None


def _hours(h: float) -> str:
    return f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} days"


_POSSESSIVE = re.compile(r"^\s*[A-Z][\w.-]*(?:\s+[A-Z][\w.-]*)?['’]s?\s+(?=\S)")


def device_label(name: str, person: str = "", shared: bool = False) -> str:
    """A device's name for a card title, without a "Your …" in front. On the owner's own card a leading possessive
    goes ("Alex's MacBook Pro" → "MacBook Pro"): the card is theirs already. A shared device keeps its own name, with
    its place in front when the name doesn't say it ("Apple TV" → "Living Room Apple TV")."""
    n = " ".join(str(name or "").split()) or "device"
    if shared:
        return n if not person or person.lower() in n.lower() else f"{person} {n}"
    bare = _POSSESSIVE.sub("", n, count=1).strip()
    return bare or n


def findings(people: list[dict], macs: Callable[[str], list], live: Callable[[str, str], bool],
             sync: dict, home: str | None, services: dict[str, bool], admin_id: str, now: float,
             tasks: list[dict] | None = None, backup: dict | None = None,
             listeners: list[dict] | None = None) -> list[Finding]:
    """What's wrong (and, for each thing titan watches, what's fine) right now."""
    out: list[Finding] = []
    if backup and backup.get("backups"):
        # #1187: owner-only (the admin holds the phrase); box_backup.status() decides when it's due
        days = backup.get("verified_days")
        out.append(Finding(f"health-{admin_id}-backup-verify", [admin_id], bool(backup.get("verify_due")),
                           "Check a backup with your recovery phrase" if backup.get("verify_due")
                           else "Your backups were checked",
                           (("They've never been opened with it. " if days is None else
                             f"It's been {int(days)} days since one was opened with it. ")
                            + "The box can't open its own backups, so only you can tell they'd restore: run "
                              "box_backup.py verify with the newest one.") if backup.get("verify_due") else ""))
    for p in people:
        pid, shared = p["id"], p.get("role") == "household"
        to = "admins" if shared else [pid]
        for d in p.get("devices") or []:
            if d.get("revoked") or not d.get("last_seen"):
                continue      # never seen: nothing it usually does has stopped
            age = _age_h(d["last_seen"], now)
            name = device_label(d.get("name") or d.get("platform") or "device", str(p.get("name") or ""), shared)
            bad = age is not None and age >= DEVICE_QUIET_H
            out.append(Finding(f"health-{pid}-device-{d.get('id')}", to, bad,
                               f"{name} hasn't reached titan in {_hours(age)}" if bad else f"{name} is back",
                               "Is Jellybean still installed and signed in there? Open it once to reconnect." if bad
                               else "It reached titan again."))
        if shared:
            continue          # a shared household device has no Mac of its own
        for m in macs(pid) or []:
            age = _age_h(m.get("updated_at"), now)
            wakeable = bool(m.get("mac_address") or (m.get("lan_ip") and m.get("wake_port")))
            bad = age is not None and age >= MAC_QUIET_H and not live(pid, m["device_key"]) and not wakeable
            mac = device_label(m.get("name") or "Mac")
            if age is not None and age < FRESH_H:
                pct, ac, disk = m.get("battery_pct"), m.get("on_ac"), m.get("disk_free_gb")
                if isinstance(pct, (int, float)) and ac is not None:
                    low = pct <= LOW_BATTERY_PCT and ac is False
                    out.append(Finding(f"health-{pid}-battery-{m['device_key']}", [pid], low,
                                       f"{mac} is at {pct:.0f}% and not plugged in" if low
                                       else f"{mac} battery is fine again",
                                       "Plug it in soon, or it'll sleep and titan can't reach it from your phone."
                                       if low else ""))
                if isinstance(disk, (int, float)):
                    full = disk < LOW_DISK_GB
                    out.append(Finding(f"health-{pid}-disk-{m['device_key']}", [pid], full,
                                       f"{mac} has {disk:.1f} GB free" if full
                                       else f"{mac} has room again",
                                       "Downloads and the Trash are the usual places to clear." if full else ""))
            if age is not None and age < FRESH_H:
                for src, st in sorted((m.get("sources") or {}).items()):
                    if src not in SOURCE_CARDS or not isinstance(st, dict):
                        continue
                    level = str(st.get("level") or "")
                    held = _age_h(st.get("since"), now)
                    waiting = level == "waiting" and held is not None and held >= SOURCE_WAITING_H
                    blocked = level in ("needsRequest", "denied")
                    app = SOURCE_CARDS[src]
                    out.append(Finding(
                        f"health-{pid}-source-{src}-{m['device_key']}", [pid], waiting or blocked,
                        f"{app} isn't open on your {mac}, so new {src} isn't reaching titan" if waiting else
                        f"Jellybean needs your OK to read {app} on your {mac}" if blocked else
                        f"{app} is reaching titan again from your {mac}",
                        (f"Open {app} on the Mac; Jellybean reads it only while it's running, and never opens it. "
                         "Bills and suggestions from it wait until then." if waiting else
                         f"On the Mac, open Jellybean's settings and allow it to read {app}." if blocked else "")))
            out.append(Finding(f"health-{pid}-mac-{m['device_key']}", [pid], bad,
                               f"{mac} hasn't checked in for {_hours(age)} and can't be woken" if bad
                               else f"{mac} can be reached again",
                               "From your phone, titan can't reach it for Find My, files or checks. Is it shut down, "
                               "or is Jellybean closed?" if bad else "Phone requests can reach it again."))
    for src in SYNC_SOURCES:
        s = sync.get(src) or {}
        if not s.get("items"):
            continue          # never synced: not something that stopped
        age = _age_h(s.get("last_sync"), now)
        bad = age is not None and age >= SYNC_STALE_H
        # Mail: nothing *new* is the signal (#648). The Mac re-sends old mail, so a fresh sync time said "running
        # again" while Mail.app was closed and no mail had arrived for a day.
        new_age = _age_h(s.get("last_new"), now) if src in NEW_ITEM_SOURCES and s.get("last_new") else None
        # A Mac that says why (jellybean#413: Mail isn't open) has its own card; this one is for a Mac that can't say.
        said = any(str(((m.get("sources") or {}).get(src) or {}).get("level") or "granted") != "granted"
                   for m in (macs(admin_id) or []))
        quiet = not bad and not said and new_age is not None and new_age >= NEW_ITEM_QUIET_H
        out.append(Finding(f"health-{admin_id}-sync-{src}", [admin_id], bad or quiet,
                           f"{src.capitalize()} sync from your Mac stopped {_hours(age)} ago" if bad else
                           f"No new {src} has reached titan for {_hours(new_age)}" if quiet
                           else f"{src.capitalize()} sync is running again",
                           "Jellybean on the Mac sends it; is the Mac asleep or Jellybean closed?" if bad else
                           f"Your Mac still checks in, but nothing new comes from {src.capitalize()}: is the "
                           f"{src.capitalize()} app open on the Mac? Bills and suggestions from {src} wait until it is."
                           if quiet else ""))
    for t in tasks or []:
        meta = t.get("metadata") if isinstance(t.get("metadata"), dict) else {}
        owner = str(meta.get("owner") or admin_id)
        goal = str(meta.get("goal") or (t.get("input") or {}).get("goal") or t.get("instance_id") or "a task")[:80]
        status = str(t.get("status") or t.get("state") or "").lower()
        step = str(t.get("current_step_key") or "")
        at = f" at step {int(step[1:]) + 1}" if step[:1] == "s" and step[1:].isdigit() else ""
        age = _age_h(t.get("updated_at") or t.get("created_at"), now)
        closed = status in TASK_CLOSED
        done = status in TASK_DONE or closed
        failed = status == "failed"
        stalled = not done and not failed and age is not None and age >= STALL_H
        bad = failed or stalled
        title = (f"Your task “{goal}” failed{at}" if failed else
                 f"Your task “{goal}” hasn't moved in {_hours(age)}{at}" if stalled else
                 f"Your task “{goal}” is closed" if closed else
                 f"Your task “{goal}” is done" if done else f"Your task “{goal}” is moving again")
        out.append(Finding(f"health-{owner}-task-{t.get('instance_id')}", [owner], bad, title,
                           (f"Say “continue {goal}” to pick it up, or “close it” if it's no longer needed."
                            if bad else "")))
    if home is not None:
        bad = home.startswith("Home Assistant: token present but not answering")
        out.append(Finding("health-household-home", "admins", bad,
                           "Home Assistant isn't answering" if bad else "Home Assistant is answering again",
                           "Lights, the thermostat and the TVs can't be reached from titan." if bad else ""))
    if listeners is not None:   # a box only (gather leaves it None on titan)
        names = [f"port {d['port']}" + (f" ({d['name']})" if d.get("name") else "") for d in listeners[:5]]
        bad = bool(listeners)
        out.append(Finding(f"health-{admin_id}-listeners", [admin_id], bad,
                           ("Something on the box is listening on " + ", ".join(names) + " that shouldn't be") if bad
                           else "The box listens only where it should again",
                           ("The box only expects the Jellybean app's port, device discovery, DHCP and Tailscale on your "
                            "network. Its firewall keeps the rest out, but nothing else should be listening.") if bad else ""))
    for svc, up in services.items():
        out.append(Finding(f"health-household-service-{svc}", "admins", not up,
                           f"titan's {svc} is down" if not up else f"titan's {svc} is back up",
                           "host_status has the detail." if not up else ""))
    return out


def gather(now: float) -> dict[str, Any]:
    """The live signals (each read like capabilities does; a part that fails is left out)."""
    import capabilities as cap
    import household_identity as hh
    import cross_device as xd
    store = hh.get_store()
    sig: dict[str, Any] = {"people": store.people(), "macs": store.macs_for,
                           "live": lambda pid, key: xd.CHANNEL.live((pid, key)), "admin_id": hh.ADMIN_PERSON_ID}
    try:
        import connectors_runner as cx
        sig["sync"] = cx.status()
    except Exception:  # noqa: BLE001
        sig["sync"] = {}
    try:
        sig["home"] = cap.home_part()
    except Exception:  # noqa: BLE001
        sig["home"] = None
    try:
        import agent_runtime_lib as lib
        r = lib._runtime_request("workflow", "/v1/instances", "GET")
        sig["tasks"] = [i for i in (r.get("instances") if isinstance(r, dict) else r) or []
                        if str(i.get("instance_id", "")).startswith("task-")]   # plan_task's, not other workflows
    except Exception:  # noqa: BLE001
        sig["tasks"] = []
    try:
        import box_backup
        sig["backup"] = box_backup.status() if box_backup.pub_path().exists() else None   # a box with a phrase only
    except Exception:  # noqa: BLE001
        sig["backup"] = None
    sig["listeners"] = None
    if os.environ.get("BOX_SETUP", "0").strip() == "1":   # #407: a box's own listener check, never on titan
        try:
            import subprocess
            import box_firewall as fw
            text = subprocess.run(["ss", "-ltnupH"], capture_output=True, text=True, timeout=10).stdout
            sig["listeners"] = fw.unexpected(fw.listeners(text), int(os.environ.get("AGENT_API_PORT", fw.API_PORT)))
        except Exception:  # noqa: BLE001 - no reading: say nothing rather than a false "all clear"
            sig["listeners"] = None
    try:
        sig["services"] = {name: service_up(url) for name, url in cap.service_urls().items()}
    except Exception:  # noqa: BLE001
        sig["services"] = {}
    return sig


def service_up(url: str, check: Callable[..., bool] | None = None, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Up if any of SERVICE_TRIES health checks answers (SERVICE_GAP_S apart): a model server that's
    busy for a moment isn't down."""
    if check is None:
        import agent_runtime_lib as lib
        check = lib.health_ok
    for i in range(SERVICE_TRIES):
        if check(url, timeout=SERVICE_TIMEOUT_S):
            return True
        if i < SERVICE_TRIES - 1:
            sleep(SERVICE_GAP_S)
    return False


def _quiet(now: float) -> bool:
    """Inside quiet hours, household time."""
    from datetime import datetime
    import agent_runtime_lib as lib
    h = datetime.fromtimestamp(now, lib._local_now().tzinfo).hour
    start, end = QUIET
    return h >= start or h < end


def reconcile(found: list[Finding], state: dict, now: float, quiet: bool,
              send: Callable[..., Any]) -> list[str]:
    """Send a card for each change since the last card for that thing; returns what was sent.
    `state[key]` = {"problem": bool, "since": ts (when the current state began), "runs": how many runs
    in a row it's been so, "carded": bool|None}. A problem needs CONFIRM_RUNS runs; a recovery, one."""
    sent = []
    seen = set()
    for f in found:
        seen.add(f.key)
        s = state.get(f.key)
        if s is None or s["problem"] != f.problem:
            s = state[f.key] = {"problem": f.problem, "since": now, "runs": 1, "carded": s["carded"] if s else None}
        else:
            s["runs"] = s.get("runs", 1) + 1
        s["title"] = f.title   # the box's status light says it in words (#407): local state, the admin's eyes only
        if s["carded"] == f.problem:
            continue                          # already told
        if not f.problem and s["carded"] is None:
            s["carded"] = False               # fine since first look: nothing to say
            continue
        if f.problem and s["runs"] < CONFIRM_RUNS:
            continue                          # seen once: wait for the next run to confirm
        lasted_h = (now - s["since"]) / 3600
        if quiet and not (f.problem and lasted_h >= QUIET_OVERRIDE_H):
            continue                          # it waits for morning
        send(f.title, f.detail, kind="info", urgency=1 if f.problem else 0, source="device-health", to=f.to,
             key=f.key, expires_in=None if f.problem else 6 * 3600)
        s["carded"] = f.problem
        sent.append(f.key)
    for key in [k for k in state if k not in seen]:
        state.pop(key)                        # a device that was removed, a service no longer checked
    return sent


def run(dry_run: bool = False, now: float | None = None, sig: dict | None = None, state_path: Path = STATE_PATH,
        send: Callable[..., Any] | None = None, quiet: bool | None = None) -> dict[str, Any]:
    now = now or time.time()
    sig = sig or gather(now)
    found = findings(sig["people"], sig["macs"], sig["live"], sig.get("sync") or {}, sig.get("home"),
                     sig.get("services") or {}, sig["admin_id"], now, sig.get("tasks"), sig.get("backup"),
                     sig.get("listeners"))
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    if dry_run:
        return {"problems": [f.title for f in found if f.problem], "sent": []}
    if send is None:
        import proactive
        send = proactive.add_card
    sent = reconcile(found, state, now, _quiet(now) if quiet is None else quiet, send)
    import durable   # a power cut mid-write would reset every finding's two-run confirmation (#407)
    durable.atomic_write(state_path, json.dumps(state, indent=1))
    return {"problems": [f.title for f in found if f.problem], "sent": sent}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="say what's wrong; send and save nothing")
    a = ap.parse_args()
    r = run(dry_run=a.dry_run)
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
