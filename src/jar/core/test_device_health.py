#!/usr/bin/env python3
"""
Tests for device health (scripts/device_health.py): what counts as a problem, who it goes to (the
device's owner; admins for household-wide things), edge-triggering (a change, never a repeat), quiet
hours, and the state file. Hermetic: every signal and the card sender are faked.

Run: python3 scripts/test_device_health.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))

import device_health as dh  # noqa: E402

NOW = 1_790_900_000.0


def iso(hours_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - hours_ago * 3600))


def nohours(x):
    """A card title without its hour count (later runs see older timestamps)."""
    import re
    return re.sub(r"\d+ (h|days)", "N h", str(x))


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def signals(phone_h=1, mac_h=0.1, mac_wake=True, cal_h=2, home="Home Assistant: connected (1 thermostat)", down=(),
            robin_phone_h=1, kitchen_h=1, battery=None, on_ac=None, disk=None):
    """down: titan services that are down (the rest of SERVICES are up)."""
    people = [{"id": "zach", "name": "Zach", "role": "admin",
               "devices": [{"id": "dz", "name": "Alex's iPhone", "platform": "ios", "last_seen": iso(phone_h)}]},
              {"id": "p_m", "name": "Robin", "role": "adult",
               "devices": [{"id": "dm", "name": "Robin's iPhone", "platform": "ios", "last_seen": iso(robin_phone_h)}]},
              {"id": "p_k", "name": "Kitchen", "role": "household",
               "devices": [{"id": "dk", "name": "Kitchen iPad", "platform": "ipados", "last_seen": iso(kitchen_h)}]},
              {"id": "p_c", "name": "Leo", "role": "kid", "devices": []}]
    mac = {"device_key": "mk", "name": "MacBook Pro", "updated_at": iso(mac_h),
           **({"mac_address": "aa:bb:cc:dd:ee:ff"} if mac_wake else {}),
           "battery_pct": battery, "on_ac": on_ac, "disk_free_gb": disk}
    return {"people": people, "macs": lambda pid: [mac] if pid == "zach" else [], "live": lambda pid, key: False,
            "sync": {"calendar": {"items": 170, "last_sync": iso(cal_h)}, "mail": {"items": 0}},
            "home": home, "services": {svc: svc not in down for svc in SERVICES}, "admin_id": "zach"}


SERVICES = ("Decider", "ingestion", "retrieval")


def run():
    ok = True
    with tempfile.TemporaryDirectory() as d:
        st = Path(d) / "state.json"
        cards = []

        def send(title, detail, **kw):
            cards.append((title, kw["to"], kw["key"], kw["urgency"]))

        def go(sig, quiet=False, now=NOW):
            cards.clear()
            return dh.run(now=now, sig=sig, state_path=st, send=send, quiet=quiet)

        clock = [NOW]

        def go2(sig, quiet=False):
            """Two runs 15 minutes apart: a problem becomes a card on the second (CONFIRM_RUNS)."""
            clock[0] += 900
            go(sig, quiet=quiet, now=clock[0])
            first = list(cards)
            clock[0] += 900
            go(sig, quiet=quiet, now=clock[0])
            return first
        def fresh():
            """A new scenario: no history, then one healthy look (which says nothing)."""
            if st.exists():
                st.unlink()
            go(signals())

        print("\n── all fine: nothing to say ──")
        go(signals())
        ok &= check("no cards on a healthy first look", cards, [])

        print("\n── a problem: one card, to the owner ──")
        go2(signals(phone_h=30))
        ok &= check("the admin's phone quiet 30 h → Zach only", cards,
                    [("iPhone hasn't reached titan in 30 h", ["zach"], "health-zach-device-dz", 1)])
        go2(signals(phone_h=31))
        ok &= check("never a repeat", cards, [])
        clock[0] += 900
        go(signals(phone_h=0.1), now=clock[0])
        ok &= check("cleared → one quiet 'back' card, same key", cards,
                    [("iPhone is back", ["zach"], "health-zach-device-dz", 0)])

        print("\n── each person's devices are theirs ──")
        fresh()
        go2(signals(robin_phone_h=40))
        ok &= check("Robin's phone → Robin only", [c[1] for c in cards], [["p_m"]])
        fresh()
        go2(signals(kitchen_h=40))
        ok &= check("a shared household device → admins", [(c[1], c[0][:24]) for c in cards],
                    [("admins", "Kitchen iPad hasn't reac")])

        ok &= check("titles name the device, no \"Your\" and no owner's possessive on their own card",
                    [dh.device_label("Alex's MacBook Pro"), dh.device_label("Robin Hale’s iPad"),
                     dh.device_label("MacBook Pro"), dh.device_label("Apple TV", "Living Room", True),
                     dh.device_label("Kitchen iPad", "Kitchen", True)],
                    ["MacBook Pro", "iPad", "MacBook Pro", "Living Room Apple TV", "Kitchen iPad"])

        print("\n── the Mac, sync, Home Assistant, services ──")
        fresh()
        go(signals(mac_h=8, mac_wake=True))
        ok &= check("a Mac that can be woken isn't a problem", cards, [])
        go2(signals(mac_h=8, mac_wake=False))
        ok &= check("can't be reached or woken", [nohours(c[0]) for c in cards],
                    ["MacBook Pro hasn't checked in for N h and can't be woken"])
        fresh()
        go2(signals(cal_h=14))
        ok &= check("calendar sync stopped → the admin (his Mac syncs)", [(nohours(c[0]), c[1]) for c in cards],
                    [("Calendar sync from your Mac stopped N h ago", ["zach"])])
        fresh()
        go2(signals(cal_h=14, home="Home Assistant: token present but not answering (TimeoutError)", down=("ingestion",)))
        ok &= check("household-wide → admins", sorted((nohours(c[0]), str(c[1])) for c in cards),
                    [("Calendar sync from your Mac stopped N h ago", "['zach']"), ("Home Assistant isn't answering", "admins"),
                     ("titan's ingestion is down", "admins")])
        ok &= check("a source that never synced isn't 'stopped'", any("Mail" in c[0] for c in cards), False)

        print("\n── mail: nothing new is the signal (#648) ──")
        def mail_findings(sync_h, new_h):
            sync = {"mail": {"items": 50, "last_sync": iso(sync_h), "last_new": iso(new_h) if new_h is not None else None}}
            return [f for f in dh.findings([], lambda pid: [], lambda p, k: False, sync, None, {}, "zach", NOW)
                    if "sync-mail" in f.key]
        f = mail_findings(1, 30)
        ok &= check("syncing, but no new mail for 30 h: a problem, saying Mail may be closed",
                    (f[0].problem, nohours(f[0].title), "is the Mail app open on the Mac?" in f[0].detail),
                    (True, "No new mail has reached titan for N h", True))
        f = mail_findings(1, 2)
        ok &= check("new mail 2 h ago: fine ('running again' only when something new came)", (f[0].problem, f[0].title),
                    (False, "Mail sync is running again"))
        f = mail_findings(14, 30)
        ok &= check("no sync at all: the old 'stopped' card wins", (f[0].problem, nohours(f[0].title)),
                    (True, "Mail sync from your Mac stopped N h ago"))
        f = mail_findings(1, None)
        ok &= check("no record of new items yet (older ledger): judged by the sync, as before", f[0].problem, False)

        print("\n── the Mac says why (jellybean#413) ──")
        def src_findings(level, since_h, new_h=30):
            mac = {"device_key": "mk", "name": "MacBook Pro", "updated_at": iso(0.1),
                   "sources": {"mail": {"level": level, "since": NOW - since_h * 3600}}}
            sync = {"mail": {"items": 50, "last_sync": iso(1), "last_new": iso(new_h)}}
            return [f for f in dh.findings([{"id": "zach", "name": "Zach", "role": "admin", "devices": []}],
                                           lambda pid: [mac] if pid == "zach" else [], lambda p, k: False, sync,
                                           None, {}, "zach", NOW) if "mail" in f.key]
        f = {x.key.split("-")[2]: x for x in src_findings("waiting", 3)}
        ok &= check("Mail closed for 3 h: its own card, to the Mac's owner",
                    (f["source"].problem, f["source"].title, f["source"].to),
                    (True, "Mail isn't open on your MacBook Pro, so new mail isn't reaching titan", ["zach"]))
        ok &= check("…and the quieter 'no new mail' card isn't sent as well", f["sync"].problem, False)
        f = {x.key.split("-")[2]: x for x in src_findings("waiting", 0.5, new_h=1)}
        ok &= check("closed only half an hour: not yet", f["source"].problem, False)
        f = {x.key.split("-")[2]: x for x in src_findings("denied", 0.1, new_h=1)}
        ok &= check("access denied: asks for the OK at once", (f["source"].problem, f["source"].title),
                    (True, "Jellybean needs your OK to read Mail on your MacBook Pro"))
        f = {x.key.split("-")[2]: x for x in src_findings("granted", 5, new_h=1)}
        ok &= check("granted: fine", (f["source"].problem, f["source"].title),
                    (False, "Mail is reaching titan again from your MacBook Pro"))

        print("\n── quiet hours ──")
        fresh()
        go(signals(phone_h=30), quiet=True)
        ok &= check("a new problem waits for morning", cards, [])
        go(signals(phone_h=33), quiet=True, now=NOW + 3.5 * 3600)   # confirmed (2nd run) and lasted 3 h
        ok &= check("… unless it has lasted 3 h", [c[2] for c in cards], ["health-zach-device-dz"])
        fresh()
        go2(signals(phone_h=30))
        clock[0] += 900
        go(signals(phone_h=0.1), quiet=True, now=clock[0])
        ok &= check("a cleared one waits too", cards, [])
        clock[0] += 900
        go(signals(phone_h=0.1), now=clock[0])
        ok &= check("… and goes in the morning", [c[0] for c in cards], ["iPhone is back"])

        print("\n── no flapping (10-01: one slow answer became 'titan's Decider is down') ──")
        fresh()
        clock[0] += 900
        go(signals(down=("Decider",)), now=clock[0])
        ok &= check("down once: no card yet", cards, [])
        clock[0] += 900
        go(signals(), now=clock[0])
        ok &= check("up again next run: still nothing", cards, [])
        first = go2(signals(down=("Decider",)))
        ok &= check("down two runs in a row: one card, on the second", (first, [c[0] for c in cards]),
                    ([], ["titan's Decider is down"]))
        clock[0] += 900
        go(signals(), now=clock[0])
        ok &= check("back up: cleared on the first good run", [c[0] for c in cards], ["titan's Decider is back up"])
        tries, slept = [], []
        ok &= check("a service answering on the 3rd try is up",
                    dh.service_up("u", check=lambda u, timeout: tries.append(timeout) or len(tries) == 3,
                                  sleep=slept.append), True)
        ok &= check("… with a 3 s timeout and 2 s between tries", (tries, slept), ([3.0, 3.0, 3.0], [2.0, 2.0]))
        ok &= check("never answering: down", dh.service_up("u", check=lambda u, timeout: False, sleep=lambda s: None),
                    False)

        print("\n── battery and disk (v2: the Mac's heartbeat) ──")
        _fresh = fresh

        def fresh():   # this section's readings are 6 minutes old at the start: keep the clock with them
            clock[0] = NOW
            _fresh()
        fresh()
        go2(signals(battery=12, on_ac=False, disk=120.0))
        ok &= check("12% and not plugged in → him", [(c[0], c[1]) for c in cards],
                    [("MacBook Pro is at 12% and not plugged in", ["zach"])])
        clock[0] += 900
        go(signals(battery=12, on_ac=True, disk=120.0), now=clock[0])
        ok &= check("plugged in → cleared", [c[0] for c in cards], ["MacBook Pro battery is fine again"])
        fresh()
        go2(signals(battery=12, on_ac=True))
        ok &= check("low but charging isn't a problem", cards, [])
        fresh()
        go2(signals(disk=6.4))
        ok &= check("6.4 GB free → him", [c[0] for c in cards], ["MacBook Pro has 6.4 GB free"])
        fresh()
        go2(signals(mac_h=2, battery=5, on_ac=False, disk=2.0))
        ok &= check("a reading over an hour old never cards", cards, [])
        fresh()
        go2(signals(battery=None, on_ac=None, disk=None))
        ok &= check("an older Jellybean (no readings): nothing", cards, [])

        print("\n── stalled tasks (09-29: stuck at step 1 for ~8 h, nothing noticed) ──")
        def task(tid, hours, status="running", owner="p_m", step="s0"):
            return {"instance_id": tid, "status": status, "current_step_key": step, "updated_at": iso(hours),
                    "metadata": {"owner": owner, "goal": "Plan Ava's birthday"} if owner else {},
                    "input": {"goal": "Sort out the car registration"}}
        sig_t = lambda tasks: {**signals(), "tasks": tasks}
        clock[0] = NOW
        if st.exists():
            st.unlink()
        go(sig_t([task("task-1", 1)]), now=clock[0])
        go2(sig_t([task("task-1", 8)]))
        ok &= check("idle 8 h → its owner, with the step", [(c[0], c[1]) for c in cards],
                    [("Your task “Plan Ava's birthday” hasn't moved in 8 h at step 1", ["p_m"])])
        clock[0] += 900
        go(sig_t([task("task-1", 0.1, step="s1")]), now=clock[0])
        ok &= check("it moves → cleared", [c[0] for c in cards], ["Your task “Plan Ava's birthday” is moving again"])
        clock[0] = NOW
        st.unlink()
        go(sig_t([task("task-2", 1, owner=None)]), now=clock[0])
        go2(sig_t([task("task-2", 9, owner=None)]))
        ok &= check("no owner recorded (older tasks) → the admin, from its input goal",
                    [(c[0].startswith("Your task “Sort out the car registration” hasn't moved in"), c[1]) for c in cards],
                    [(True, ["zach"])])
        clock[0] = NOW
        st.unlink()
        go2(sig_t([task("task-3", 30, status="completed"), task("task-4", 1, status="failed", step="s2")]))
        ok &= check("a finished task never cards; a failed one does", [c[0] for c in cards],
                    ["Your task “Plan Ava's birthday” failed at step 3"])
        clock[0] = NOW
        st.unlink()
        go2(sig_t([task("task-5", 20, status="paused")]))
        ok &= check("a closed task (close_task pauses it) never cards, however old", cards, [])
        clock[0] = NOW
        st.unlink()
        go(sig_t([task("task-6", 1)]), now=clock[0])
        go2(sig_t([task("task-6", 8)]))
        clock[0] += 900
        go(sig_t([task("task-6", 8.3, status="paused")]), now=clock[0])
        ok &= check("closing a stalled task clears its card", [c[0] for c in cards],
                    ["Your task “Plan Ava's birthday” is closed"])

        ok &= check("a runtime's nanosecond timestamp is read", round(dh._age_h("2026-09-30T08:03:46.487314493+00:00",
                    dh._age_h and __import__("datetime").datetime.fromisoformat("2026-09-30T10:03:46+00:00").timestamp()), 2), 2.0)

        print("\n── a box's backups: the owner opens one with the phrase every 30 days (#1187) ──")
        F = lambda b: [f for f in dh.findings([], lambda pid: [], lambda p, k: False, {}, None, {}, "zach", NOW,  # noqa: E731
                                              backup=b) if "backup" in f.key]
        due = F({"backups": 3, "verified_days": None, "verify_due": True})
        ok &= check("never checked: one problem, to the admin only (the phrase's holder), owner-keyed",
                    [(f.problem, f.to, f.key, f.title) for f in due],
                    [(True, ["zach"], "health-zach-backup-verify", "Check a backup with your recovery phrase")])
        ok &= check("…it says why and what to run", ("never been opened" in due[0].detail, "box_backup.py verify" in due[0].detail),
                    (True, True))
        ok &= check("31 days: due, and says how long", "31 days" in F({"backups": 3, "verified_days": 31.2,
                                                                         "verify_due": True})[0].detail, True)
        ok &= check("checked recently: fine (a recovery clears an open card)", [f.problem for f in F(
                    {"backups": 3, "verified_days": 2, "verify_due": False})], [False])
        ok &= check("no backups yet, or no box backups at all (titan): nothing",
                    (F({"backups": 0, "verified_days": None, "verify_due": False}), F(None)), ([], []))
        cards.clear()
        sig_b = {**signals(), "backup": {"backups": 3, "verified_days": None, "verify_due": True}}
        stb = Path(d) / "state-backup.json"
        dh.run(now=NOW, sig=sig_b, state_path=stb, send=send, quiet=False)
        first = [c for c in cards if "backup" in c[2]]
        dh.run(now=NOW + 900, sig=sig_b, state_path=stb, send=send, quiet=False)
        ok &= check("as a card: after the second run (no flapping), once, to the admin only",
                    ([c for c in cards if "backup" in c[2]], first),
                    ([("Check a backup with your recovery phrase", ["zach"], "health-zach-backup-verify", 1)], []))
        cards.clear()
        dh.run(now=NOW + 1800, sig=sig_b, state_path=stb, send=send, quiet=False)
        ok &= check("…and never repeated while it stays due", [c for c in cards if "backup" in c[2]], [])

        print("\n── dry run ──")
        before = st.read_text()
        r = dh.run(dry_run=True, now=NOW, sig=signals(phone_h=30), state_path=st, send=send)
        ok &= check("says what's wrong, sends and saves nothing", (r["problems"], st.read_text() == before),
                    (["iPhone hasn't reached titan in 30 h"], True))

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
