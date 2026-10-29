#!/usr/bin/env python3
"""Whether titan comes back by itself after a power cut: the read-only check behind boot_resilience.sh.

  python3 scripts/boot_resilience.py check [--json]    exit 1 when something required isn't enabled

The units come from boot_resilience_units.json (platform, a network-stack any-of group, always-on services, timers
and paths, the runtime registry's units, optional units and the BIOS notes). `check()` returns the shape
`boot_resilience.sh check --json` prints — lists, not counts:
  {default_target, ok[], missing_unit_file[], not_enabled[], platform_any_of_fail[], optional[], bios[]}
and `summary()` folds that into the counts `host_summary()` carries. Two systemctl calls in all (list-unit-files
over every unit, then get-default), so the host summary can run it on every poll. Nothing here changes a unit;
enabling stays with `boot_resilience.sh apply`.

BOOT_RESILIENCE_UNITS / REGISTRY / SYSTEMCTL point the check elsewhere (the tests)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# Enabled for the next boot. Not "enabled-runtime": `systemctl enable --runtime` lasts only until a reboot, the very
# thing this check is for, so it reads as not enabled.
GOOD = ("enabled", "static", "indirect")
# A runtime or broker that was never deployed on this host is soft: it has no unit file until its deploy.
SOFT_MISSING_SUFFIXES = ("-runtime.service", "-broker.service")


def _units_path() -> Path:
    return Path(os.environ.get("BOOT_RESILIENCE_UNITS") or HERE / "boot_resilience_units.json")


def _registry_path() -> Path:
    return Path(os.environ.get("REGISTRY") or HERE / "runtime-registry.json")


def _systemctl(*args: str) -> str:
    try:
        return subprocess.run([os.environ.get("SYSTEMCTL") or "systemctl", *args], capture_output=True, text=True,
                              timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _uniq(xs: list[str]) -> list[str]:
    return list(dict.fromkeys(xs))


def catalog() -> dict[str, Any]:
    """The unit lists from boot_resilience_units.json, with the registry's runtimes and brokers added to services."""
    u = json.loads(_units_path().read_text())
    services = list(u.get("always_on_services") or [])
    if u.get("registry_units"):
        try:
            reg = json.loads(_registry_path().read_text())
            services += [r["name"] + ".service" for r in list(reg.get("runtimes") or []) + list(reg.get("brokers") or [])]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    required = _uniq(list(u.get("platform") or []) + _uniq(services) + list(u.get("always_on_timers") or [])
                     + list(u.get("always_on_paths") or []))
    return {"required": required, "any_of": [list(g) for g in u.get("platform_any_of") or []],
            "optional": list(u.get("optional") or []), "bios": list(u.get("bios") or [])}


def unit_states(units: list[str]) -> dict[str, str]:
    """{unit: state} from one `systemctl list-unit-files`; a unit with no file is 'missing'. No answer at all (no
    systemd here, or systemctl failed) raises OSError rather than calling every unit missing."""
    states = {u: "missing" for u in units}
    if not units:
        return states
    listed = _systemctl("list-unit-files", "--no-legend", "--plain", "--no-pager", *units)
    if not listed.strip():
        raise OSError("systemctl list-unit-files gave no answer")
    for ln in listed.splitlines():
        f = ln.split()
        if len(f) >= 2 and f[0] in states:
            states[f[0]] = f[1]
    return states


def check() -> dict[str, Any]:
    """The boot check's report, in boot_resilience.sh's --json shape. Raises OSError/ValueError without a catalog
    or an answer from systemctl."""
    cat = catalog()
    states = unit_states(_uniq(cat["required"] + [u for g in cat["any_of"] for u in g] + cat["optional"]))
    out: dict[str, Any] = {"default_target": _systemctl("get-default").strip() or "unknown", "ok": [],
                           "missing_unit_file": [], "not_enabled": [], "platform_any_of_fail": [], "optional": [],
                           "bios": cat["bios"]}
    for group in cat["any_of"]:
        line = "network-stack:" + " ".join(f"{u}={states[u]}" for u in group)
        out["ok" if any(states[u] in GOOD for u in group) else "platform_any_of_fail"].append(line)
    for u in cat["required"]:
        st = states[u]
        if st in GOOD:
            out["ok"].append(u)
        elif st == "missing":
            out["missing_unit_file"].append(u)
        else:
            out["not_enabled"].append(f"{u}={st}")
    out["optional"] = [f"{u}={states[u]}" for u in cat["optional"]]
    return out


def hard_fail(report: dict[str, Any]) -> bool:
    """check's exit status: anything not enabled, a failing network group, a non-boot default target, or a missing
    unit file other than a runtime's or broker's."""
    return bool(report["not_enabled"] or report["platform_any_of_fail"]
                or report["default_target"] not in ("multi-user.target", "graphical.target")
                or any(not u.endswith(SOFT_MISSING_SUFFIXES) for u in report["missing_unit_file"]))


def summary(report: dict[str, Any]) -> dict[str, Any]:
    """The host summary's `boot`: counts, the default target and the BIOS lines."""
    return {"ok": len(report.get("ok") or []), "not_enabled": len(report.get("not_enabled") or []),
            "missing": len(report.get("missing_unit_file") or []),
            "platform_fail": len(report.get("platform_any_of_fail") or []),
            "default_target": report.get("default_target") or "unknown", "bios": list(report.get("bios") or [])}


def _names(xs: list[str], most: int = 3) -> str:
    names = [x.split("=", 1)[0] for x in xs]
    return ", ".join(names[:most]) + (f" and {len(names) - most} more" if len(names) > most else "")


def problems(report: dict[str, Any]) -> list[str]:
    """The host summary's problem lines for a check() report: units not enabled, or with no unit file, by name."""
    out = []
    if report.get("not_enabled"):
        out.append(f"not enabled for boot: {_names(report['not_enabled'])} (titan won't start them after a power cut)")
    if report.get("missing_unit_file"):
        out.append(f"no unit file for boot unit: {_names(report['missing_unit_file'])}")
    return out


def _text(r: dict[str, Any]) -> str:
    lines = [f"boot resilience: default-target={r['default_target']}",
             f"  ok:              {len(r['ok'])}",
             f"  missing unit:    {len(r['missing_unit_file'])}",
             f"  not enabled:     {len(r['not_enabled'])}",
             f"  network any-of:  {len(r['platform_any_of_fail'])} failing groups"]
    for head, key in (("missing unit files (install unit or deploy runtime first):", "missing_unit_file"),
                      ("installed but not enabled for boot:", "not_enabled"),
                      ("platform network stack not enabled:", "platform_any_of_fail")):
        if r[key]:
            lines += [head] + [f"  - {x}" for x in r[key]]
    lines += ["optional (not required):"] + ([f"  - {x}" for x in r["optional"]] or ["  (none)"])
    lines += ["BIOS / power (operator, once per board):"] + [f"  - {x}" for x in r["bios"]]
    if r["default_target"] not in ("multi-user.target", "graphical.target"):
        lines.append(f"WARNING: default target is '{r['default_target']}' (want multi-user.target)")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--json"]
    if args not in ([], ["check"]) or any(a.startswith("-") and a != "--json" for a in argv):
        print("usage: boot_resilience.py check [--json]", file=sys.stderr)
        return 2
    try:
        r = check()
    except (OSError, ValueError) as exc:
        print(f"boot resilience: can't check ({_units_path()}): {exc}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2) if "--json" in argv else _text(r))
    return 1 if hard_fail(r) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
