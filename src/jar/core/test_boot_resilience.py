#!/usr/bin/env python3
# gate: hermetic
"""boot_resilience.sh: after a brownout titan must power on (BIOS) and every required unit must be
enabled. This exercises check/apply/install against a fake systemctl and a temp unit tree — never
the live host. Run: python3 scripts/test_boot_resilience.py"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "boot_resilience.sh"
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(("  [PASS] " if ok else "  [FAIL] ") + label + ("" if ok else f": got={got!r:.500} expected={expected!r:.500}"))


T = Path(tempfile.mkdtemp(prefix="boot-resilience-"))
SD = T / "systemd"
SRC = T / "src"
SD.mkdir()
SRC.mkdir()
STATE = T / "state.json"
STATE.write_text(json.dumps({
    "default": "multi-user.target",
    "units": {
        "ssh.service": {"enabled": "disabled", "active": "inactive", "file": True},
        "tailscaled.service": {"enabled": "enabled", "active": "active", "file": True},
        "docker.service": {"enabled": "enabled", "active": "active", "file": True},
        "nvidia-persistenced.service": {"enabled": "enabled", "active": "active", "file": True},
        "systemd-networkd.service": {"enabled": "enabled", "active": "active", "file": True},
        "titan-agent-api.service": {"enabled": "disabled", "active": "inactive", "file": True},
        "titan-local-agent.service": {"enabled": "enabled", "active": "active", "file": True},
        "titan-preflight.timer": {"enabled": "disabled", "active": "inactive", "file": True},
        "fetch-runtime.service": {"enabled": "enabled", "active": "active", "file": True},
    },
}))

FAKE_CTL = T / "fake-systemctl"
FAKE_CTL.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
STATE={STATE!s}
SDIR={SD!s}
python3 - "$STATE" "$SDIR" "$@" <<'PY'
import json, sys
from pathlib import Path
state_path = Path(sys.argv[1])
sdir = Path(sys.argv[2])
args = sys.argv[3:]
st = json.loads(state_path.read_text())
units = st.setdefault("units", {{}})

def save():
    state_path.write_text(json.dumps(st, indent=2))

def has_file(u):
    info = units.get(u) or {{}}
    if info.get("file"):
        return True
    return (sdir / u).is_file()

def ensure(u):
    info = units.setdefault(u, {{}})
    if (sdir / u).is_file():
        info["file"] = True
    return info

if not args:
    sys.exit(0)
cmd = args[0]
if cmd == "get-default":
    print(st.get("default", "multi-user.target")); sys.exit(0)
if cmd == "set-default":
    st["default"] = args[1]; save(); sys.exit(0)
if cmd == "cat":
    u = args[1]
    if not has_file(u):
        sys.exit(1)
    print(f"# unit {{u}}"); sys.exit(0)
if cmd == "is-enabled":
    u = args[1]
    if not has_file(u):
        print("not-found"); sys.exit(1)
    info = ensure(u)
    print(info.get("enabled", "disabled"))
    sys.exit(0 if info.get("enabled") in ("enabled", "static", "indirect", "enabled-runtime") else 1)
if cmd == "is-active":
    u = args[1]
    info = ensure(u) if has_file(u) else {{}}
    print(info.get("active", "inactive"))
    sys.exit(0 if info.get("active") == "active" else 3)
if cmd == "enable":
    now = False
    rest = []
    for a in args[1:]:
        if a == "--now":
            now = True
        elif a.startswith("-"):
            continue
        else:
            rest.append(a)
    for u in rest:
        info = ensure(u)
        info["file"] = True
        info["enabled"] = "enabled"
        if now:
            info["active"] = "active"
    save(); sys.exit(0)
if cmd == "start":
    for u in args[1:]:
        if u.startswith("-"):
            continue
        info = ensure(u)
        info["active"] = "active"
    save(); sys.exit(0)
if cmd == "daemon-reload":
    sys.exit(0)
print("unhandled", args, file=sys.stderr); sys.exit(2)
PY
""")
FAKE_CTL.chmod(FAKE_CTL.stat().st_mode | stat.S_IXUSR)

# minimal catalog: platform + one service + one timer + registry unit
units_json = T / "units.json"
units_json.write_text(json.dumps({
    "bios": ["set AC Power Loss to Power On"],
    "platform": ["ssh.service", "tailscaled.service", "docker.service", "nvidia-persistenced.service"],
    "platform_any_of": [["systemd-networkd.service", "NetworkManager.service"]],
    "always_on_services": ["titan-agent-api.service", "titan-local-agent.service"],
    "always_on_timers": ["titan-preflight.timer"],
    "always_on_paths": [],
    "optional": ["titan-mail-idle.service"],
    "registry_units": True,
}))
reg = T / "registry.json"
reg.write_text(json.dumps({
    "runtimes": [{"name": "fetch-runtime", "kind": "rust", "port": 1, "health_url": "http://127.0.0.1/"}],
    "brokers": [],
}))

# source units for install
(SRC / "titan-agent-api.service").write_text("[Unit]\nDescription=api\n[Install]\nWantedBy=multi-user.target\n")
(SRC / "titan-preflight.timer").write_text("[Timer]\nOnCalendar=*:0/10\n[Install]\nWantedBy=timers.target\n")
(SRC / "titan-preflight.service").write_text("[Unit]\nDescription=preflight\n[Service]\nType=oneshot\nExecStart=/bin/true\n")
(SRC / "titan-firewall.nft").write_text("table inet titan_fw {}\n")

env = {
    **os.environ,
    "SYSTEMCTL": str(FAKE_CTL),
    "SYSTEMD_DIR": str(SD),
    "SUDO": "",
    "BOOT_RESILIENCE_UNITS": str(units_json),
    "REGISTRY": str(reg),
    "BOOT_RESILIENCE_SRC": str(SRC),
    "BOOT_RESILIENCE_FIREWALL_DST": str(T / "etc-titan" / "firewall.nft"),
}


def run(*args, check_rc=None):
    r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
    if check_rc is not None and r.returncode != check_rc:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
    return r


print("── check finds disabled units ──")
r = run("check")
check("check exits non-zero when units are disabled", r.returncode != 0, True)
check("check names ssh as not enabled", "ssh.service=disabled" in r.stdout, True)
check("check names agent-api as not enabled", "titan-agent-api.service=disabled" in r.stdout, True)
check("check prints the BIOS reminder", "AC Power Loss" in r.stdout, True)

print("── apply enables them ──")
r = run("apply")
check("apply exits 0", r.returncode, 0)
st = json.loads(STATE.read_text())
check("ssh is now enabled", st["units"]["ssh.service"]["enabled"], "enabled")
check("agent-api is now enabled and active", (
    st["units"]["titan-agent-api.service"]["enabled"],
    st["units"]["titan-agent-api.service"]["active"],
), ("enabled", "active"))
check("preflight timer is enabled", st["units"]["titan-preflight.timer"]["enabled"], "enabled")

print("── check is clean after apply ──")
r = run("check")
check("check exits 0 once everything required is enabled", r.returncode, 0)

print("── install copies unit files and firewall ──")
# mark a unit missing so install must copy it
st = json.loads(STATE.read_text())
st["units"].pop("titan-agent-api.service", None)
STATE.write_text(json.dumps(st))
# drop the file so unit_state sees missing via cat failure — fake needs no file key
r = run("install")
check("install wrote the agent-api unit into SYSTEMD_DIR", (SD / "titan-agent-api.service").exists(), True)
check("install wrote the firewall rules", (T / "etc-titan" / "firewall.nft").exists(), True)
check("install mentions the firewall path", "firewall.nft" in r.stdout, True)
# after install, unit file exists on disk; fake still needs the unit registered — install calls enable
st = json.loads(STATE.read_text())
check("install re-enabled agent-api in fake systemctl", st["units"].get("titan-agent-api.service", {}).get("enabled"), "enabled")

print("── json report ──")
r = run("check", "--json")
check("check --json is valid JSON", bool(json.loads(r.stdout).get("bios")), True)

print("── titan.sh wires the subcommand ──")
titan_sh = (HERE / "titan.sh").read_text()
check("titan.sh documents titan boot", "titan boot" in titan_sh, True)
check("titan.sh case has boot)", "\n  boot)\n" in titan_sh, True)
check("deploy note stays true: boot_resilience is not auto-forced by deploy",
      "boot_resilience" not in (HERE / "deploy_titan_node.sh").read_text(), True)

shutil.rmtree(T, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
