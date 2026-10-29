#!/usr/bin/env bash
# Fleet preflight: catches the failure classes we actually hit —
#   - dead-but-"enabled" runtimes (health, not just systemd state)
#   - a unit whose ExecStart doesn't match its registry kind (the clobber bug)
#   - runtimes not enabled (reboot-unsafe)
#   - coordinator-secret drift (a runtime/agent signed with the wrong secret)
#   - a value with an inline comment in agent.env or a unit's EnvironmentFile (systemd keeps the comment in the
#     value, so a switch silently stays off: FETCH_BROWSER_POOL #882, AGENT_GEMINI_THINKING #940). Key names only.
#   - a host zone that isn't UTC (#1569: the timers that name no zone, and every log stamp, assume it)
# Run BEFORE any fleet-wide mutation, and as a scheduled check. Exit != 0 on a hard fail.
#
# Usage: bash scripts/titan_preflight.sh   (override host with TITAN_SSH_ALIAS=...)
set -uo pipefail
SSH_ALIAS="${TITAN_SSH_ALIAS:-titan}"

ssh "$SSH_ALIAS" bash -s <<'REMOTE'
python3 - <<'PY'
import json, os, subprocess, urllib.request, re, hashlib, sys
REG = "/ai/projects/titan-node/scripts/runtime-registry.json"
_reg = json.load(open(REG))
runtimes = _reg["runtimes"]
brokers = _reg.get("brokers", [])

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


fails = warns = 0
print("%-20s %-4s %-6s %-9s %s" % ("runtime", "kind", "health", "enabled", "notes"))
print("-" * 72)
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
    print("%-20s %-4s %-6s %-9s %s" % (n, k[:4], "ok" if up else "DOWN", enabled[:9], ",".join(notes)))

# Brokers hold provider credentials, so an unnoticed dead broker is a bigger
# deal than a dead runtime. They are unit-managed but outside `titan runtime`.
for b in brokers:
    n, h = b["name"], b["health_url"]
    up = healthy(h)
    enabled = sh("systemctl is-enabled %s.service" % n) or "missing"
    notes = ["broker"]
    if b.get("kind") == "rust":   # #1023: brokers run from bin/<name>/current too; a build tree is NOT-MIGRATED
        bexec = sh("systemctl cat %s.service 2>/dev/null | grep -m1 ExecStart" % n)
        bnote = _unit_note(n, bexec) if bexec else None
        if bnote:
            notes.append(bnote)
    if enabled != "enabled":
        notes.append("NOT-ENABLED")
    if not up:
        notes.append("DOWN"); fails += 1
    elif len(notes) > 1:
        warns += 1
    print("%-20s %-4s %-6s %-9s %s" % (n, b.get("kind", "")[:4], "ok" if up else "DOWN",
                                       enabled[:9], ",".join(notes)))

agent_env = "/ai/projects/titan-local-agent/agent.env"
akf = ""
try:
    for l in open(agent_env):
        if l.startswith("COORDINATOR_PRIVATE_KEY_FILE="):
            akf = l.split("=", 1)[1].strip().strip("'\"")
except Exception:
    pass
print("-" * 72)
if akf and os.access(akf, os.R_OK):
    print("agent coordinator credential: Ed25519 key file %s -> OK" % akf)
else:
    print("agent coordinator credential: NONE (%s) -> agent KB/graph tools cannot authenticate"
          % ("key file %s unreadable" % akf if akf else "no COORDINATOR_PRIVATE_KEY_FILE"))
    warns += 1
# #940: systemd's EnvironmentFile keeps "# comment" as part of a value. Flag a quoted value with anything after its
# closing quote ('low'# note) or an unquoted value with " #" in it. A bare "#" inside a value (a password, a URL
# fragment) is legal and isn't flagged. Only key names are printed, never values.
def env_lines(path):
    try:
        return open(path).read().splitlines()
    except PermissionError:
        out = subprocess.run(["sudo", "-n", "cat", path], capture_output=True, text=True, timeout=10)
        return out.stdout.splitlines() if out.returncode == 0 else None
    except Exception:
        return None

def inline_comment_keys(lines):
    bad = []
    for l in lines or []:
        t = l.strip()
        if not t or t.startswith(("#", ";")) or "=" not in t:
            continue
        k, v = t.split("=", 1)
        v = v.strip()
        if v[:1] in ("'", '"'):
            end = v.find(v[0], 1)
            if end > 0 and v[end + 1:].strip():
                bad.append(k.strip())
        elif re.search(r"\s#", v):
            bad.append(k.strip())
    return bad

env_files = {agent_env}
for u in [r["name"] for r in runtimes] + [b["name"] for b in brokers]:
    for m in re.finditer(r"^EnvironmentFile=-?(\S+)", sh("systemctl cat %s.service 2>/dev/null" % u), re.M):
        env_files.add(m.group(1))
for f in sorted(env_files):
    keys = inline_comment_keys(env_lines(f))
    if keys:
        warns += 1
        print("inline comment in a value (systemd keeps it): %s -> %s (move the comment to its own line)"
              % (f, ", ".join(sorted(set(keys)))))
# #1569: the host runs in UTC; a zone-less timer or a log stamp would otherwise drift by the offset.
def host_zone_note():
    """None when the host zone is Etc/UTC (or UTC); else a one-line warning."""
    zone = sh("timedatectl show -p Timezone --value")
    if zone in ("Etc/UTC", "UTC"):
        return None
    return "host timezone is %s, expected Etc/UTC (timedatectl set-timezone Etc/UTC)" % (zone or "unreadable")

_zn = host_zone_note()
if _zn:
    warns += 1
    print(_zn)
print("summary: down=%d warnings=%d" % (fails, warns))
sys.exit(1 if fails else 0)
PY
REMOTE
