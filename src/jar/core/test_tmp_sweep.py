#!/usr/bin/env python3
# gate: hermetic
"""titan's /tmp (10-10 deep review: 53 GB of test leftovers, +4.8 GB/day, never aged). tmp_sweep.sh removes only what
is provably abandoned (this user's, untouched for its age inside and out, open by nothing, not a dotfile, socket,
systemd-private or ssh dir); --dry-run removes nothing; and run_deploy_gate.sh gives each gate run its own TMPDIR,
gone when the run ends. Every path here is a temp dir. Run: python3 scripts/test_tmp_sweep.py"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
SWEEP = HERE / "tmp_sweep.sh"
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


# a Unix socket's path is at most ~104 bytes on macOS: under the gate's long per-run TMPDIR the test's socket wouldn't
# bind ("AF_UNIX path too long"), so a long temp base gives way to /tmp (removed at the end either way)
T = Path(tempfile.mkdtemp(prefix="sweep-test-", dir=None if len(tempfile.gettempdir()) <= 40 else "/tmp"))
OLD = time.time() - 3 * 86400


def old(p: Path) -> Path:
    for q in [p, *p.rglob("*")] if p.is_dir() else [p]:
        os.utime(q, (OLD, OLD), follow_symlinks=False)
    return p


def make() -> dict[str, Path]:
    d = T / "tmp"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    out = {}
    (d / "titan-items-redteam-a1").mkdir(); (d / "titan-items-redteam-a1" / "x.db").write_text("x")
    out["old_dir"] = old(d / "titan-items-redteam-a1")
    (d / "tmpab12cd").write_text("y")
    out["old_file"] = old(d / "tmpab12cd")
    (d / "fresh-run").mkdir(); (d / "fresh-run" / "now.txt").write_text("z")
    out["fresh"] = d / "fresh-run"
    (d / "mixed").mkdir(); (d / "mixed" / "old.txt").write_text("o"); old(d / "mixed")
    (d / "mixed" / "new.txt").write_text("n")    # something inside changed recently: keep the whole entry
    os.utime(d / "mixed", (OLD, OLD))
    out["mixed"] = d / "mixed"
    (d / ".X11-unix").mkdir(); out["dot"] = old(d / ".X11-unix")
    (d / "systemd-private-abc").mkdir(); out["sysd"] = old(d / "systemd-private-abc")
    (d / "held").mkdir(); (d / "held" / "f.log").write_text("h"); out["held"] = old(d / "held")
    return out


def sweep(*args, age="1440"):
    env = {**os.environ, "TMP_SWEEP_DIR": str(T / "tmp"), "TMP_SWEEP_AGE_MIN": age}
    return subprocess.run(["bash", str(SWEEP), *args], env=env, capture_output=True, text=True, timeout=120)


p = make()
holder = subprocess.Popen([sys.executable, "-c", f"f=open({str(p['held'] / 'f.log')!r}); import time; time.sleep(60)"])
time.sleep(0.5)
sock = socket.socket(socket.AF_UNIX); sock_path = T / "tmp" / "agent.sock"; sock.bind(str(sock_path)); old(sock_path)
try:
    r = sweep("--dry-run")
    check("--dry-run names what it would remove and removes nothing",
          ("would remove titan-items-redteam-a1" in r.stdout, p["old_dir"].exists(), "would remove 2 entries" in r.stdout),
          (True, True, True))
    r = sweep()
    check("an old test dir and an old temp file are removed", (p["old_dir"].exists(), p["old_file"].exists()), (False, False))
    check("…a fresh entry is kept", p["fresh"].exists(), True)
    check("…an old dir with something new inside is kept", p["mixed"].exists(), True)
    check("…a dotfile dir and systemd's private dir are kept", (p["dot"].exists(), p["sysd"].exists()), (True, True))
    check("…a socket is kept", sock_path.exists(), True)
    check("…an old dir a process still has a file open in is kept", p["held"].exists(), True)
    check("…and it says how many and how much", "removed 2 entries" in r.stdout, True)
finally:
    holder.kill(); holder.wait(); sock.close()

if not Path("/proc/self/fd").exists():   # (titan has /proc; this Mac check proves the refusal path)
    make()
    nolsof = ":".join(d for d in os.environ["PATH"].split(":") if not (Path(d) / "lsof").exists())
    r = subprocess.run(["bash", str(SWEEP)], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PATH": nolsof, "TMP_SWEEP_DIR": str(T / "tmp"), "TMP_SWEEP_AGE_MIN": "1440"})
    check("no way to see open files (no /proc, no lsof): it refuses and removes nothing",
          (r.returncode, (T / "tmp" / "titan-items-redteam-a1").exists()), (3, True))

print("── each gate run's TMPDIR is its own, and gone at the end ──")
R = T / "checkout"
(R / "scripts").mkdir(parents=True)
for f in ("run_deploy_gate.sh", "gate_plan.sh", "gate_env.sh"):
    shutil.copy(HERE / f, R / "scripts" / f)
(R / "scripts" / "gate_live_tests.txt").write_text("")
(R / "scripts" / "gate_serial_tests.txt").write_text("")
(R / "scripts" / "deploy_titan_node.sh").write_text('export DAILY_LOG_DIR="$TEST_STATE/daily"\n'
                                                  'run_test scripts/test_where.py || rollback "where"\n')
seen = T / "seen.txt"
(R / "scripts" / "test_where.py").write_text(
    "import tempfile, pathlib\n"
    f"d = tempfile.mkdtemp(prefix='titan-leftover-'); pathlib.Path({str(seen)!r}).write_text(d)\n"
    "print('1 passed, 0 failed')\n")
root = T / "gate-root"
r = subprocess.run(["bash", str(R / "scripts" / "run_deploy_gate.sh"), str(R)], capture_output=True, text=True, timeout=120,
                   env={**os.environ, "GATE_TMP_ROOT": str(root), "GATE_JOBS": "2"})
made = Path(seen.read_text()) if seen.exists() else None
check("the gate passed its stub test", (r.returncode, "ok   scripts/test_where.py" in r.stdout or "ok scripts/test_where.py" in r.stdout),
      (0, True))
check("a test's mkdtemp landed inside this run's temp root", bool(made) and str(made).startswith(str(root / "run.")), True)
check("…and the run's temp is gone when the gate ends (no leftover)", (made.exists() if made else None,
      sorted(x.name for x in root.iterdir()) if root.exists() else []), (False, []))
check("the deploy installs the daily sweep", "bash scripts/tmp_sweep.sh install" in (HERE / "deploy_titan_node.sh").read_text(), True)

shutil.rmtree(T, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
