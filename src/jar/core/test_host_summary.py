#!/usr/bin/env python3
# gate: hermetic
"""host_summary() for Jellybean's watchdog and the titan MCP, with every input faked: /proc/meminfo, nvidia-smi,
`systemctl --failed`, `ip -j` addresses, the disks, the canary, and the boot check (boot_resilience.py) over a fake
systemctl and a small unit catalog. Checks the `network` and `boot` fields, the problem lines they add, that a host
without systemd or a catalog drops `boot` rather than raising a problem, the boot check's own CLI, and that the
titan MCP renders the real shape. Never the live host. Run: python3 scripts/test_host_summary.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-host-summary-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl", "WORLD_DIR": "world",
             "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
for var in ("ENGINEERING_TOKEN_FILE", "AGENT_ENGINEERING_TOKEN_FILE", "TITAN_AGENT_URL", "TITAN_ADDRESSES"):
    os.environ.pop(var, None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_runtime_lib as lib  # noqa: E402
import boot_resilience as BR  # noqa: E402
import titan_mcp as M  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


# ── fakes ────────────────────────────────────────────────────────────────────────────────────
MEMINFO = _TMP / "meminfo"
MEMINFO.write_text("MemTotal:       65000000 kB\nMemAvailable:   20971520 kB\n")
LOGS = _TMP / "logs"
LOGS.mkdir()
(LOGS / "agent-canary.json").write_text(json.dumps({"status": "OK", "ts": "2026-10-14T12:00:00Z"}))

# titan's real `ip -j -4 addr show scope global` shape (10-14): LAN, Tailscale, docker; plus a bridge and a
# second LAN address so the filters are exercised.
IP_JSON = json.dumps([
    {"ifindex": 2, "ifname": "enp9s0", "addr_info": [{"family": "inet", "local": "10.0.0.170", "prefixlen": 24,
                                                      "scope": "global"}]},
    {"ifindex": 3, "ifname": "wlp5s0", "addr_info": [{"family": "inet", "local": "10.0.0.171", "prefixlen": 24}]},
    {"ifindex": 4, "ifname": "tailscale0", "addr_info": [{"family": "inet", "local": "100.77.69.85", "prefixlen": 32}]},
    {"ifindex": 5, "ifname": "docker0", "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]},
    {"ifindex": 6, "ifname": "br-1a2b3c", "addr_info": [{"family": "inet", "local": "172.18.0.1", "prefixlen": 16}]},
])
NVIDIA = "0, 61, 9000, 12288\n1, 70, 11000, 12288\n2, 55, 100, 12288"
fake = {"ip": IP_JSON, "nvidia-smi": NVIDIA, "systemctl": ""}
seen_cmds: list[list[str]] = []


def fake_run_quiet(cmd, timeout=5):
    seen_cmds.append(cmd)
    return fake.get(cmd[0], "").strip()


class _Statvfs:
    f_blocks, f_frsize, f_bavail = 1000, 4096, 600       # 40% used


_real_path = lib.Path
lib.Path = lambda *a: MEMINFO if a == ("/proc/meminfo",) else _real_path(*a)
lib._run_quiet = fake_run_quiet
lib._fstab_mounts = lambda: []
lib._HOST_LOGS = str(LOGS)
_real_statvfs, _real_ismount = os.statvfs, os.path.ismount
os.statvfs = lambda mp: _Statvfs()
os.path.ismount = lambda mp: mp == "/"

# The boot check's catalog and a fake systemctl answering list-unit-files / get-default (and cat / is-enabled, so
# boot_resilience.sh can run against the same fake when it's in the tree).
UNITS = _TMP / "units.json"
UNITS.write_text(json.dumps({
    "bios": ["UEFI/BIOS: set Restore After AC Power Loss to Power On."],
    "platform": ["ssh.service", "tailscaled.service"],
    "platform_any_of": [["systemd-networkd.service", "NetworkManager.service"]],
    "always_on_services": ["titan-agent-api.service", "titan-local-agent.service"],
    "always_on_timers": ["titan-preflight.timer"],
    "always_on_paths": ["titan-model-switch.path"],
    "optional": ["titan-mail-idle.service"],
    "registry_units": True,
}))
REG = _TMP / "registry.json"
REG.write_text(json.dumps({"runtimes": [{"name": "fetch-runtime"}], "brokers": [{"name": "decision-broker"}]}))
STATE = _TMP / "systemd.json"
CTL = _TMP / "fake-systemctl"
CTL.write_text(f"""#!/usr/bin/env python3
import json, sys
st = json.load(open({str(STATE)!r}))
a = sys.argv[1:]
units = st["units"]
if a[:1] == ["get-default"]:
    print(st["default"]); sys.exit(0)
if a[:1] == ["list-unit-files"]:
    names = [x for x in a[1:] if not x.startswith("-")]
    rows = [(u, units[u]) for u in names if u in units]
    for u, s in rows:
        print(u.ljust(30), s, "enabled")
    sys.exit(0 if rows else 1)
if a[:1] == ["cat"]:
    sys.exit(0 if a[1] in units else 1)
if a[:1] == ["is-enabled"]:
    if a[1] not in units:
        print("not-found"); sys.exit(1)
    print(units[a[1]]); sys.exit(0 if units[a[1]] in ("enabled", "static", "indirect", "enabled-runtime") else 1)
sys.exit(2)
""")
CTL.chmod(0o755)
os.environ.update(BOOT_RESILIENCE_UNITS=str(UNITS), REGISTRY=str(REG), SYSTEMCTL=str(CTL))
HEALTHY = {"ssh.service": "enabled", "tailscaled.service": "enabled", "systemd-networkd.service": "enabled",
           "titan-agent-api.service": "enabled", "titan-local-agent.service": "enabled",
           "titan-preflight.timer": "enabled", "titan-model-switch.path": "enabled", "fetch-runtime.service": "enabled",
           "decision-broker.service": "enabled", "titan-mail-idle.service": "disabled"}


def systemd(units, default="multi-user.target"):
    STATE.write_text(json.dumps({"default": default, "units": units}))


# ── a healthy titan ──────────────────────────────────────────────────────────────────────────
systemd(HEALTHY)
s = lib.host_summary()
check("healthy: ok, no problems", (s["ok"], s["problems"]), (True, []))
check("network: hostname, LAN addresses (no docker or bridge), Tailscale apart",
      s["network"], {"hostname": __import__("socket").gethostname(), "ipv4": ["10.0.0.170", "10.0.0.171"],
                     "tailscale_ipv4": "100.77.69.85"})
check("boot: counts, target and BIOS lines (network group + 6 required + a runtime + a broker)",
      s["boot"], {"ok": 9, "not_enabled": 0, "missing": 0, "platform_fail": 0, "default_target": "multi-user.target",
                  "bios": ["UEFI/BIOS: set Restore After AC Power Loss to Power On."]})
check("the summary's other fields are untouched", (s["memory_free_gib"], len(s["gpus"]), s["disks"][0]["used_pct"],
                                                   s["canary"]["status"], s["failed_units"]), (20.0, 3, 40, "OK", []))
check("the network read is one `ip -j` call", [c for c in seen_cmds if c[0] == "ip"],
      [["ip", "-j", "-4", "addr", "show", "scope", "global"]])
check("the summary is plain JSON", json.loads(json.dumps(s)) == s, True)

# ── units off for boot ───────────────────────────────────────────────────────────────────────
systemd({**HEALTHY, "ssh.service": "disabled", "titan-agent-api.service": "masked",
         "titan-preflight.timer": "disabled", "titan-model-switch.path": "linked"}
        | {"decision-broker.service": "enabled"})
s = lib.host_summary()
check("not enabled: counted", s["boot"]["not_enabled"], 4)
check("…and one problem line naming the first three", s["problems"],
      ["not enabled for boot: ssh.service, titan-agent-api.service, titan-preflight.timer and 1 more "
       "(titan won't start them after a power cut)"])
check("…so the summary isn't ok", s["ok"], False)

systemd({**HEALTHY, "titan-local-agent.service": "enabled-runtime"})
s = lib.host_summary()
check("enabled --runtime is gone after a reboot: not enabled, named",
      (s["boot"]["not_enabled"], s["problems"]),
      (1, ["not enabled for boot: titan-local-agent.service (titan won't start them after a power cut)"]))

units = dict(HEALTHY)
del units["titan-local-agent.service"], units["fetch-runtime.service"]
systemd(units)
s = lib.host_summary()
check("missing unit files: counted, runtimes included", (s["boot"]["missing"], s["boot"]["ok"]), (2, 7))
check("…and named in one problem line", s["problems"],
      ["no unit file for boot unit: titan-local-agent.service, fetch-runtime.service"])

units = {**HEALTHY, "systemd-networkd.service": "disabled"}
systemd(units, default="rescue.target")
r = BR.check()
check("network group with nothing enabled: platform_fail, and default_target passes through",
      (BR.summary(r)["platform_fail"], BR.summary(r)["default_target"], r["platform_any_of_fail"]),
      (1, "rescue.target", ["network-stack:systemd-networkd.service=disabled NetworkManager.service=missing"]))
check("…no problem line from either (the spec: not_enabled or missing only)", lib.host_summary()["problems"], [])
check("optional units are reported with their state, never counted",
      (r["optional"], "titan-mail-idle.service" in str(BR.summary(r))), (["titan-mail-idle.service=disabled"], False))

# ── where the check can't run ────────────────────────────────────────────────────────────────
systemd({})
s = lib.host_summary()
check("systemctl lists nothing (no systemd here): boot is None, no problem", (s["boot"], s["ok"]), (None, True))
os.environ["SYSTEMCTL"] = str(_TMP / "no-such-systemctl")
check("no systemctl at all: boot is None", lib.host_summary()["boot"], None)
os.environ["SYSTEMCTL"] = str(CTL)
systemd(HEALTHY)
os.environ["BOOT_RESILIENCE_UNITS"] = str(_TMP / "no-units.json")
check("no catalog: boot is None, summary still ok", (lib.host_summary()["boot"], lib.host_summary()["ok"]), (None, True))
os.environ["BOOT_RESILIENCE_UNITS"] = str(UNITS)
os.environ["REGISTRY"] = str(_TMP / "no-registry.json")
check("no registry: the catalog's own units only", BR.summary(BR.check())["ok"], 7)
os.environ["REGISTRY"] = str(REG)

for label, out, want in (("no ip tool", "", {"ipv4": [], "tailscale_ipv4": None}),
                         ("ip prints junk", "not json", {"ipv4": [], "tailscale_ipv4": None}),
                         ("Tailscale only", json.dumps([{"ifname": "tailscale0",
                                                         "addr_info": [{"local": "100.101.1.2"}]}]),
                          {"ipv4": [], "tailscale_ipv4": "100.101.1.2"})):
    fake["ip"] = out
    n = lib.host_summary()["network"]
    check(f"network, {label}", {k: n[k] for k in ("ipv4", "tailscale_ipv4")}, want)
fake["ip"] = IP_JSON

# ── the boot check's CLI (the shape boot_resilience.sh --json prints) ──────────────────────────
def cli(*args):
    return subprocess.run([sys.executable, str(HERE / "boot_resilience.py"), *args], capture_output=True, text=True,
                          timeout=30, env=os.environ)


systemd(HEALTHY)
r = cli("check", "--json")
j = json.loads(r.stdout)
check("--json keys are the shell script's", sorted(j),
      sorted(["default_target", "ok", "missing_unit_file", "not_enabled", "platform_any_of_fail", "optional", "bios"]))
check("…every value but default_target a list", all(isinstance(v, list) for k, v in j.items() if k != "default_target"),
      True)
check("healthy: exit 0", r.returncode, 0)
units = dict(HEALTHY)
del units["fetch-runtime.service"]
systemd(units)
check("a runtime with no unit file is soft: exit 0", cli("check").returncode, 0)
del units["titan-local-agent.service"]
systemd(units)
r = cli("check")
check("an always-on service with no unit file is hard: exit 1, named", (r.returncode, "titan-local-agent" in r.stdout),
      (1, True))
systemd(HEALTHY, default="emergency.target")
check("a non-boot default target is hard", cli("check").returncode, 1)
check("anything but check [--json] is usage", cli("apply").returncode, 2)

# The shell script (#1608) and this twin must agree on the same fake host, once the script is in the tree.
sh = Path(os.environ.get("BOOT_RESILIENCE_SH") or HERE / "boot_resilience.sh")
if sh.is_file():
    units = {**HEALTHY, "ssh.service": "disabled", "systemd-networkd.service": "disabled"}
    del units["fetch-runtime.service"]
    systemd(units)
    shj = subprocess.run(["bash", str(sh), "check", "--json"], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "SUDO": ""})
    check("boot_resilience.sh and boot_resilience.py agree", json.loads(shj.stdout), BR.check())

# ── the titan MCP renders the real shape ─────────────────────────────────────────────────────
systemd({**HEALTHY, "ssh.service": "disabled"})
s = lib.host_summary()
txt = M.host_summary_text(s)
check("MCP: network line", "network: " + s["network"]["hostname"] + " 10.0.0.170 10.0.0.171, Tailscale 100.77.69.85"
      in txt, True)
check("MCP: boot line", "boot: default target multi-user.target; 8 ok, 1 not enabled, 0 missing" in txt, True)
check("MCP: the BIOS note", "Restore After AC Power Loss" in M.boot_text(s), True)
check("MCP: boot None reads as not reported", "doesn't report boot units yet" in M.boot_text({**s, "boot": None}), True)

# The real catalog (10-15): a box's own encrypted backups (box_backup.py) are a box feature titan doesn't run, so their
# timer is listed as optional, never required; required, it read as "no unit file" and titan's summary went unhealthy.
_real = json.loads((Path(__file__).resolve().parent / "boot_resilience_units.json").read_text())
_required = [u for k in ("platform", "always_on_services", "always_on_paths", "always_on_timers") for u in _real.get(k) or []]
check("the box-backup timer is optional on titan, not required",
      ("titan-box-backup.timer" in _required, "titan-box-backup.timer" in _real["optional"]), (False, True))

os.statvfs, os.path.ismount = _real_statvfs, _real_ismount
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
