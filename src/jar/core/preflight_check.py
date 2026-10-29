#!/usr/bin/env python3
"""Host-local fleet preflight — same checks as titan_preflight.sh, run directly on
the Titan host (no SSH) for the titan-preflight.timer. One-line journal summary;
exit 1 on any DOWN so `systemctl --failed` / OnFailure can react.
"""
import hashlib
import json
import re
import subprocess
import sys
import urllib.request

REG = "/ai/projects/titan-node/scripts/runtime-registry.json"
ALERT_LOG = "/ai/logs/titan-alerts.log"
STATE = "/ai/logs/.preflight-state"


def alert(msg: str) -> None:
    """Edge-triggered notification on preflight state change, via scripts/titan_alert.py
    (always the alert log; phone/webhook when /ai/config/alerts.env configures them)."""
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from titan_alert import alert as _deliver
        _deliver("Titan fleet", msg, priority="high" if msg.startswith("DOWN") else "default")
    except Exception:
        pass


def _last_state() -> str:
    try:
        return open(STATE).read().strip()
    except Exception:
        return "OK"


def _save_state(s: str) -> None:
    try:
        open(STATE, "w").write(s)
    except Exception:
        pass


def _unit_note(name, execln):
    """A Rust unit's ExecStart (#1023, runtime_bin.unit_note); the old rule if runtime_bin isn't deployed yet."""
    try:
        sys.path.insert(0, "/ai/projects/titan-node/scripts")
        import runtime_bin
        return runtime_bin.unit_note(name, execln)
    except Exception:
        return None if "target/release" in execln else "UNIT!=rust"


def sh(c):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def healthy(url):
    try:
        urllib.request.urlopen(url, timeout=4); return True
    except Exception:
        return False




DISK_FULL_PCT = 90


def host_disk_problems() -> dict[str, str]:
    """What the fleet checks never looked at: a configured mount that is missing, a filesystem
    over DISK_FULL_PCT, and a disk whose SMART health is failing or unreadable. The 26 TB
    /ai/datasets drive failed on 2026-09-15/16 and nothing noticed for two weeks. Runs as root
    (this unit has no User=), so smartctl can read the drives. Keys are stable so the alert is
    edge-triggered per problem."""
    import os
    problems: dict[str, str] = {}
    mounts = ["/"]
    try:
        for line in open("/etc/fstab"):
            f = line.split()
            if len(f) >= 3 and not f[0].startswith("#") and f[1].startswith("/") and f[2] not in ("swap", "none"):
                mounts.append(f[1])
    except OSError:
        pass
    for mp in dict.fromkeys(mounts):
        if not os.path.ismount(mp):
            problems["mount:" + mp] = "%s is configured in fstab but not mounted" % mp
            continue
        try:
            st = os.statvfs(mp)
            used = 100 - (st.f_bavail * 100 // st.f_blocks) if st.f_blocks else 0
        except OSError as exc:
            problems["mount:" + mp] = "%s unreadable: %s" % (mp, exc.strerror)
            continue
        if used > DISK_FULL_PCT:
            problems["full:" + mp] = "%s is %d%% full" % (mp, used)
    for dev in sh("lsblk -dnpo NAME,TYPE | awk '$2==\"disk\"{print $1}'").split():
        try:
            r = subprocess.run(["smartctl", "-H", "-n", "standby", dev], capture_output=True, text=True, timeout=30)
        except Exception as exc:  # noqa: BLE001 — no smartctl is a finding, not a crash
            problems["smart:" + dev] = "smartctl unavailable: %s" % exc
            continue
        if "STANDBY" in r.stdout:
            continue                       # asleep; checked on a later run
        if r.returncode & 0b11000 or "FAILED" in r.stdout:
            problems["smart:" + dev] = "%s SMART health FAILING" % dev
        elif r.returncode & 0b110:
            problems["smart:" + dev] = "%s SMART health unreadable (rc=%d)" % (dev, r.returncode)
    return problems


def label(entry: dict, broker: bool = False) -> str:
    """How a runtime or broker is named in the summary: "(fleet)" when titan-node itself never calls it (#1623)."""
    tags = (["broker"] if broker else []) + (["fleet"] if entry.get("callers") == "fleet" else [])
    return entry["name"] + ("(" + ",".join(tags) + ")" if tags else "")


def main() -> int:
    runtimes = json.load(open(REG))["runtimes"]
    fails = warns = 0
    notes_all = []
    for r in runtimes:
        n, k, h = r["name"], r["kind"], r["health_url"]
        up = healthy(h)
        enabled = sh("systemctl is-enabled %s.service" % n) or "missing"
        execln = sh("systemctl cat %s.service 2>/dev/null | grep -m1 ExecStart" % n)
        notes = []
        if k == "rust" and execln:   # #1023: bin/<name>/current; a build tree is NOT-MIGRATED (a warning)
            note = _unit_note(n, execln)
            if note:
                notes.append(note)
        if k == "python" and execln and "launch.sh" not in execln:
            notes.append("UNIT!=python")
        if enabled != "enabled":
            notes.append("NOT-ENABLED")
        if not up:
            notes.append("DOWN"); fails += 1
        elif notes:
            warns += 1
        if notes:
            notes_all.append("%s:%s" % (label(r), ",".join(notes)))

    # Brokers hold provider credentials (Jev, LLMs, images); the registry lists them "so preflight
    # covers them", but this timer-driven check (the one that alerts) only walked runtimes, so a
    # dead broker never paged. Same up/enabled checks as titan_preflight.sh.
    for b in json.load(open(REG)).get("brokers", []):
        n = b["name"]
        up = healthy(b["health_url"])
        enabled = sh("systemctl is-enabled %s.service" % n) or "missing"
        notes = []
        if b.get("kind") == "rust":   # #1023: brokers run from bin/<name>/current too; a build tree is NOT-MIGRATED
            bexec = sh("systemctl cat %s.service 2>/dev/null | grep -m1 ExecStart" % n)
            bnote = _unit_note(n, bexec) if bexec else None
            if bnote:
                notes.append(bnote)
        if enabled != "enabled":
            notes.append("NOT-ENABLED")
        if not up:
            notes.append("DOWN"); fails += 1
        elif notes:
            warns += 1
        if notes:
            notes_all.append("%s:%s" % (label(b, broker=True), ",".join(notes)))

    status = "OK" if fails == 0 else "FAIL"
    detail = (" | " + "; ".join(notes_all)) if notes_all else ""
    reg = json.load(open(REG))
    fleet = sum(1 for e in reg["runtimes"] + reg.get("brokers", []) if e.get("callers") == "fleet")
    summary = "preflight %s: down=%d warnings=%d%s (%d fleet services, no titan-node caller)" % (status, fails, warns, detail, fleet)
    print(summary)

    # Edge-triggered alerting: fire only on OK->FAIL and FAIL->OK transitions,
    # never on every 10-min tick, so a standing outage doesn't spam.
    prev = _last_state()
    if status == "FAIL" and prev != "FAIL":
        alert("DOWN — " + summary)
    elif status == "OK" and prev == "FAIL":
        alert("RECOVERED — " + summary)
    _save_state(status)

    # Host disks alert on their own edge-triggered state, so a disk problem never masks a
    # runtime going down (or the reverse).
    disk = host_disk_problems()
    print("disks: " + ("; ".join(disk.values()) if disk else "ok"))
    try:
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from titan_alert import EdgeAlert
        EdgeAlert("host-disks", "Titan disks").update(disk)
    except Exception:
        pass

    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
