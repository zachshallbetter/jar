#!/usr/bin/env python3
# gate: hermetic
"""Installed runtime binaries (titan-node#1023, runtime_bin.py): install with an atomic symlink swap and the previous
release kept, an identical binary installs nothing, pruning keeps 3 and never what current or previous points at,
rollback swaps them, dry runs change nothing, preflight's unit rule, and migrating a unit (the live binary as it is;
reverted on a failed health check; refused for arguments or a binary replaced since it started). All in a temp dir
with fake systemd; nothing on the host is touched.
Run: python3 scripts/test_runtime_bin.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import runtime_bin as rb  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


tmp = Path(tempfile.mkdtemp(prefix="titan-rtbin-"))
root = tmp / "bin"
build = tmp / "build"
build.mkdir()


def binary(content: str, name: str = "posture-runtime") -> Path:
    p = build / name
    p.write_text("#!/bin/sh\necho " + content + "\n")
    os.chmod(p, 0o755)
    return p


def tree() -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


home = root / "posture-runtime"
cur = lambda: rb._target(home / "current")   # noqa: E731
prev = lambda: rb._target(home / "previous")   # noqa: E731

print("── install ──")
out = rb.install("posture-runtime", binary("v1"), root, now=1_000)
check("the first install: current and previous are the same release", (out["action"], cur() == prev(), cur().name),
      ("installed", True, "posture-runtime"))
check("…executable, with a BUILD file", (os.access(cur(), os.X_OK), (cur().parent / "BUILD").exists()), (True, True))
first = cur()
out = rb.install("posture-runtime", binary("v2"), root, now=2_000)
check("a new build: current is it, previous is the old one", (cur() != first, prev() == first), (True, True))
check("the links are relative (the tree can move)", os.readlink(home / "current").startswith("releases/"), True)
check("no temporary link is left behind", [p for p in tree() if ".new" in p or ".part" in p], [])
second = cur()
out = rb.install("posture-runtime", binary("v2"), root, now=3_000)
check("an identical binary installs nothing", (out["action"], cur() == second, prev() == first), ("unchanged", True, True))
for i, v in enumerate(("v3", "v4", "v5")):
    rb.install("posture-runtime", binary(v), root, now=4_000 + i)
check("pruned to 3 releases", len(list((home / "releases").iterdir())), 3)
check("…current and previous are among them", (cur().is_file(), prev().is_file()), (True, True))
rb.rollback("posture-runtime", root)   # now current is the older of the two
rb.prune(home, 1)
check("pruning to 1 still keeps both what current and previous point at", (cur().is_file(), prev().is_file(),
                                                                            len(list((home / "releases").iterdir()))),
      (True, True, 2))
rb.rollback("posture-runtime", root)
for i, v in enumerate(("v6", "v7")):
    rb.install("posture-runtime", binary(v), root, now=5_000 + i)

print("\n── rollback ──")
c0, p0 = cur(), prev()
out = rb.rollback("posture-runtime", root)
check("current <-> previous", (out["action"], cur() == p0, prev() == c0), ("rolled back", True, True))
rb.rollback("posture-runtime", root)
check("…and back again", (cur() == c0, prev() == p0), (True, True))
rb.install("lone-runtime", binary("x", "lone-runtime"), root)
check("a single release: rollback is a no-op, never a dangling link", rb.rollback("lone-runtime", root)["action"], "same")
try:
    rb.rollback("never-installed", root)
    check("rollback with nothing installed is refused", False, True)
except FileNotFoundError:
    check("rollback with nothing installed is refused", True, True)

print("\n── dry runs change nothing ──")
before = tree()
rb.install("posture-runtime", binary("v9"), root, dry_run=True)
rb.rollback("posture-runtime", root, dry_run=True)
rb.prune(home, 1, dry_run=True)
check("install, rollback and prune with --dry-run", tree(), before)

print("\n── refusals ──")
for bad in ("../etc", "Posture", "a b", ""):
    try:
        rb.install(bad, binary("z"), root)
        check(f"name {bad!r} refused", False, True)
    except ValueError:
        check(f"name {bad!r} refused", True, True)
try:
    rb.install("posture-runtime", build / "missing", root)
    check("a missing binary is refused", False, True)
except FileNotFoundError:
    check("a missing binary is refused", True, True)

print("\n── status ──")
st = {r["name"]: r for r in rb.status(root)}
check("every installed one, current and previous resolving", (st["posture-runtime"]["current_ok"],
                                                               st["posture-runtime"]["previous_ok"],
                                                               st["posture-runtime"]["releases"]), (True, True, 3))

print("\n── preflight's rule ──")
check("bin/<name>/current resolving: fine", rb.unit_note("posture-runtime", f"ExecStart={home}/current", root), None)
check("still in the build tree: NOT-MIGRATED (a warning)",
      rb.unit_note("posture-runtime", "ExecStart=/ai/projects/runtimes/posture-runtime/target/release/posture-runtime", root),
      "NOT-MIGRATED")
check("current not resolving: BIN-BROKEN", rb.unit_note("ghost-runtime", f"ExecStart={root}/ghost-runtime/current", root),
      "BIN-BROKEN")
check("anything else: UNIT!=rust (the clobber bug)", rb.unit_note("posture-runtime", "ExecStart=/ai/x/launch.sh", root),
      "UNIT!=rust")
src = (Path(__file__).parent / "preflight_check.py").read_text() + (Path(__file__).parent / "titan_preflight.sh").read_text()
check("both preflights use it", src.count("note = _unit_note(n, execln)"), 2)
check("…for brokers too (so warnings=0 can't hide an unmigrated broker)", src.count("bnote = _unit_note(n, bexec)"), 2)

print("\n── migrating a unit (fake systemd) ──")
units = tmp / "units"
units.mkdir()
live = tmp / "projects" / "workforce-runtime" / "target" / "release" / "workforce-runtime"
live.parent.mkdir(parents=True)
live.write_text("#!/bin/sh\necho live\n")
os.chmod(live, 0o755)
UNIT = f"[Service]\nUser=zach\nExecStart={live}\nRestart=on-failure\n"
(units / "workforce-runtime.service").write_text(UNIT)
calls: list[list[str]] = []


def fake_run(exe_for: str):
    def run(cmd):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "show"]:
            return "4242"
        if cmd[0] == "readlink":
            return exe_for
        return ""
    return run


def write(p, t):
    p.write_text(t)


dry = rb.migrate("workforce-runtime", "http://h", root, units, fake_run(str(live)), lambda u: True, write, dry_run=True)
check("dry run: nothing written, no restart", ((units / "workforce-runtime.service").read_text() == UNIT,
                                               (root / "workforce-runtime").exists(),
                                               any("restart" in c for c in calls)), (True, False, False))
out = rb.migrate("workforce-runtime", "http://h", root, units, fake_run(str(live)), lambda u: True, write)
check("migrated: the live binary as it is, ExecStart rewritten", (out["action"],
      rb.exec_line((units / "workforce-runtime.service").read_text()), rb.sha256(rb._target(root / "workforce-runtime" / "current"))
      == rb.sha256(live)), ("migrated", str(root / "workforce-runtime" / "current"), True))
check("…the rest of the unit untouched, the old one kept beside the release",
      ("User=zach" in (units / "workforce-runtime.service").read_text(), (root / "workforce-runtime" / "unit.before").read_text()),
      (True, UNIT))
check("…daemon-reload then restart", [c[1:3] for c in calls if c[0] == "sudo"][-2:],
      [["systemctl", "daemon-reload"], ["systemctl", "restart"]])
check("again: already migrated", rb.migrate("workforce-runtime", "http://h", root, units, fake_run(str(live)),
                                            lambda u: True, write)["action"], "already migrated")
(units / "ingestion-runtime.service").write_text(UNIT.replace("workforce", "ingestion"))
ilive = Path(str(live).replace("workforce", "ingestion"))
ilive.parent.mkdir(parents=True)
ilive.write_text("#!/bin/sh\necho i\n")
os.chmod(ilive, 0o755)
health = iter([False, True])
out = rb.migrate("ingestion-runtime", "http://h", root, units, fake_run(str(ilive)), lambda u: next(health), write)
check("unhealthy after the switch: the old unit is put back and restarted", (out["action"], out["healthy_after_revert"],
      (units / "ingestion-runtime.service").read_text() == UNIT.replace("workforce", "ingestion")), ("reverted", True, True))
(units / "policy-runtime.service").write_text(f"ExecStart={live} --secret-flag x\n")
for label, unit_text, exe in (("arguments on ExecStart", f"ExecStart={live} --flag x\n", str(live)),
                              ("not a target/release path", "ExecStart=/usr/bin/env thing\n", "/usr/bin/env"),
                              ("the running binary was replaced since it started", UNIT, str(live) + " (deleted)")):
    (units / "workforce2-runtime.service").write_text(unit_text.replace("workforce-runtime", "workforce2-runtime"))
    try:
        rb.migrate("workforce2-runtime", "http://h", root, units, fake_run(exe.replace("workforce-runtime", "workforce2-runtime")),
                   lambda u: True, write)
        check(f"refused: {label}", False, True)
    except (ValueError, RuntimeError) as exc:
        check(f"refused: {label}", "--flag" in str(exc) or "secret" in str(exc), False)

def root_only_run(exe_for: str):
    def run(cmd):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "show"]:
            return "4242"
        if cmd[0] == "readlink":
            return ""                    # another user's process: only root may read its exe link
        if cmd[:3] == ["sudo", "-n", "readlink"]:
            return exe_for
        return ""
    return run


(units / "posture2-runtime.service").write_text(UNIT.replace("workforce", "posture2"))
plive = Path(str(live).replace("workforce", "posture2"))
plive.parent.mkdir(parents=True)
plive.write_text("#!/bin/sh\necho p\n")
os.chmod(plive, 0o755)
out = rb.migrate("posture2-runtime", "http://h", root, units, root_only_run(str(plive)), lambda u: True, write)
check("another user's process: the exe link is read with sudo -n, and it migrates", out["action"], "migrated")

print("\n── freeing a port (#1034): never the unit's own process ──")


def port_run(main: str, holders: list[str], survives: set[str] = frozenset()):
    killed: list[list[str]] = []
    alive = set(holders)

    def run(cmd):
        if cmd[:2] == ["systemctl", "show"]:
            return main
        if "ss" in cmd:
            return "\n".join(f'LISTEN 0 1024 0.0.0.0:8200 0.0.0.0:* users:(("x",pid={p},fd=3))' for p in holders)
        if cmd[:3] == ["ps", "-o", "comm="]:
            return "uvicorn"
        if cmd[:3] == ["ps", "-o", "pid="]:
            return cmd[-1] if cmd[-1] in alive else ""
        if "kill" in cmd:
            killed.append(cmd[-2:])
            if cmd[-2] == "-KILL" or cmd[-1] not in survives:
                alive.discard(cmd[-1])
        return ""
    return run, killed


run, killed = port_run("4242", ["4242"])
check("the unit's own MainPID on its port is never touched", (rb.free_port("posture-runtime", 8200, run, lambda s: None), killed),
      ([], []))
run, killed = port_run("0", ["999"])
out = rb.free_port("posture-runtime", 8200, run, lambda s: None)
check("after the stop, a leftover holder is an orphan: TERM, and it goes", ([r["action"] for r in out], killed),
      (["terminated"], [["-TERM", "999"]]))
run, killed = port_run("0", ["777"], survives={"777"})
out = rb.free_port("posture-runtime", 8200, run, lambda s: None, grace=1.0)
check("…one that ignores TERM is KILLed after the grace", ([r["action"] for r in out], killed),
      (["killed"], [["-TERM", "777"], ["-KILL", "777"]]))
run, killed = port_run("4242", ["4242", "555"])
check("dry run: names the orphan (pid and process name only), kills nothing",
      ([(r["pid"], r["comm"], r["action"]) for r in rb.free_port("posture-runtime", 8200, run, lambda s: None, dry_run=True)],
       killed), ([(555, "uvicorn", "dry-run")], []))
for bad in (0, 70000):
    try:
        rb.free_port("posture-runtime", bad, port_run("0", [])[0])
        check(f"port {bad} refused", False, True)
    except ValueError:
        check(f"port {bad} refused", True, True)

print("\n── the deployer ──")
dep = (Path(__file__).parent / "deploy_rust_runtime.sh").read_text()
check("units run bin/<name>/current, never the build tree", ("ExecStart=/ai/runtimes/bin/${NAME}/current" in dep,
                                                             "ExecStart=${TARGET}/target/release" in dep), (True, False))
check("it installs before restarting, skips an unchanged binary, rolls back an unhealthy one",
      ("runtime_bin.py" in dep and "install '${NAME}'" in dep, 'UNCHANGED' in dep, "rollback '${NAME}'" in dep),
      (True, True, True))
check("health from the registry (some runtimes answer /v1/health)", "runtime-registry.json" in dep, True)
check("rsync leaves .claude/ and virtualenvs behind", all(x in dep for x in ("--exclude '.claude/'", "--exclude '.titan-venv/'")),
      True)
check("CLEAN_AFTER isn't on: the build tree stays for incremental builds", "cargo clean" in dep, False)
stop, free, start = (dep.find("sudo systemctl stop ${NAME}.service"), dep.find("runtime_bin.py free-port"),
                     dep.find("sudo systemctl start ${NAME}.service"))
check("#1034: the deployer stops the unit, frees only orphans, then starts it", (-1 < stop < free < start), True)
check("…the old inline hard kill of whatever held the port is gone", ('sudo kill "${orphan}"' in dep, "grep -oE 'pid=" in dep),
      (False, False))
check("…and reads the running exe with sudo (another user's process)", 'sudo -n readlink -f "/proc/' in dep, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
