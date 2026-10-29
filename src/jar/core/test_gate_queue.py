#!/usr/bin/env python3
# gate: hermetic   (temp queue dir, pinned clock, no eval units; touches nothing under /ai)
"""#945: the gate queue (gate_queue.sh) and the Mac-side runner (gate_run.sh), in the repo instead of only in /ai/tmp
and session scratchpads. Order, two lanes, solos, windows, the deploy hold, a gone caller, and a runner that gates a
committed tree and always releases its lane. Linux only (GNU stat, flock): it runs in titan's gate.

Run: python3 scripts/test_gate_queue.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GQ = HERE / "gate_queue.sh"
RUN = HERE / "gate_run.sh"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


# Static, so they run on the Mac too (10-11: five queued gates lost their heartbeat and left the queue).
_run = "\n".join(ln for ln in RUN.read_text().splitlines() if not ln.lstrip().startswith("#"))
_beats = [ln for ln in _run.splitlines() if '$GQ beat' in ln and not ln.lstrip().startswith("#")]
check("gate_run.sh: every heartbeat ssh reads </dev/null", bool(_beats) and all("</dev/null" in ln for ln in _beats),
      "; ".join(_beats))
check("gate_run.sh: the beat loop doesn't end when its sleep is killed (no `while sleep`)",
      "while sleep" not in _run and "while :; do sleep" in _run)
_loops = [f"{f.relative_to(HERE)}:{n}" for f in sorted(HERE.rglob("*.sh")) for n, ln in enumerate(f.read_text().splitlines(), 1)
          if "while sleep" in ln and "beat" in ln and not ln.lstrip().startswith("#")]
check("no heartbeat loop anywhere is `while sleep` (gate_run, main_health, coord/deploy)", _loops == [], ", ".join(_loops))
check("gate_run.sh copies the 10 slowest tests from titan's gate log into its own (#1514)",
      "sed -n '/^slowest 10/,+10p' $G.log" in RUN.read_text())
_sh = subprocess.run(["bash", "-c", 'GATE_BEAT_S=1; ( while :; do sleep "$GATE_BEAT_S" || sleep 1; kill -0 $$ 2>/dev/null || exit 0; '
                      'done ) & b=$!; sleep 0.3; pkill -P $b sleep; sleep 1.5; kill -0 $b 2>/dev/null && echo alive; kill $b'],
                     capture_output=True, text=True, timeout=20)
check("…the loop's shape survives a killed sleep (run here)", "alive" in _sh.stdout, _sh.stdout + _sh.stderr)

# #1588: a sleep that inherits the caller's captured stdout keeps its pipe open for a whole beat (60 s per subprocess.run).
_pipes = [f"{f.relative_to(HERE)}" for f in (RUN, HERE / "main_health.sh", HERE / "coord" / "deploy.sh")
          if "</dev/null >/dev/null 2>&1 &" not in f.read_text()]
check("every heartbeat loop writes to /dev/null, so it never holds a caller's pipe open", _pipes == [], ", ".join(_pipes))

# 10-14: a lane removed between `[ -d ]` and `stat` made `$(( now - $(stat …) ))` read "now - ", and bash ended the
# waiter (exit 1): under GATE_JOBS=4, "a solo runs alone" lost its next waiter that way. Ages go through age_s.
import re as _re  # noqa: E402
_gq = "\n".join(ln for ln in GQ.read_text().splitlines() if not ln.lstrip().startswith("#"))
_racy = [ln.strip() for ln in _gq.splitlines() if _re.search(r"\$\(\(.*\$\(stat", ln)]
check("gate_queue.sh: no arithmetic straight on a stat (a vanished file ends the wait loop)", _racy == [], "; ".join(_racy))

if sys.platform != "linux" or not shutil.which("flock"):
    print(f"skipped: needs Linux (GNU stat, flock); titan's gate runs it\n{len(failures)} failed")
    sys.exit(1 if failures else 0)

# The waiter polls every NAP seconds (GATE_QUEUE_SLEEP); a "keeps waiting" check lets it poll NEG (4 naps) and see nothing
# change. Both were 1 s and 2.5 s; the checks are the same, the real waits shorter (#1588).
NAP = "0.2"
NEG = 4 * float(NAP)

TAG = f"t{os.getpid()}"   # names unique to this run, so a real waiter on titan never matches


def nm(s: str) -> str:
    return f"{s}-{TAG}" if not s.startswith("solo-") else f"solo-{TAG}-{s[5:]}"


class Queue:
    def __init__(self, hhmm: str = "1200") -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="gq-"))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GATE_", "GIT_"))}
        self.env.update(GATE_QUEUE_DIR=str(self.dir), GATE_QUEUE_HHMM=hhmm, GATE_QUEUE_EVAL_UNITS="", GATE_QUEUE_SLEEP=NAP,
                        GATE_QUEUE_DATE="2026-10-20")   # a PDT day: the nightly is 07:25–08:10Z, the model eval 08:35–10:00Z
        self.procs: dict[str, subprocess.Popen] = {}

    def list(self, *names: str) -> None:
        (self.dir / "gate.queue").write_text("".join(n + "\n" for n in names))

    def queued(self) -> list[str]:
        p = self.dir / "gate.queue"
        return p.read_text().split() if p.exists() else []

    def wait(self, name: str) -> None:
        self.procs[name] = subprocess.Popen(["bash", str(GQ), "wait", name], env=self.env,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def cmd(self, *args: str) -> str:
        return subprocess.run(["bash", str(GQ), *args], env=self.env, capture_output=True, text=True, timeout=30).stdout

    def owners(self) -> list[str]:
        out = []
        for lane in ("gate.lock", "gate.lock.2"):
            f = self.dir / lane / "owner"
            out.append(f.read_text().split()[0] if f.exists() else "")
        return out

    def holds(self, name: str) -> bool:
        p = self.procs[name]
        return p.poll() == 0 and name in self.owners()

    def settle(self, cond, secs: float = 12.0) -> bool:
        end = time.time() + secs
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.25)
        return cond()

    def close(self) -> None:
        for p in self.procs.values():
            if p.poll() is None:
                p.kill()
            p.wait()
        shutil.rmtree(self.dir, ignore_errors=True)


print("order and two lanes")
q = Queue()
try:
    alex, robin, jordan = nm("alex"), nm("robin"), nm("jordan")
    q.list(alex, robin, jordan)
    for n in (alex, robin, jordan):
        q.wait(n)
        time.sleep(0.3)
    check("the first two listed take the two lanes", q.settle(lambda: q.holds(alex) and q.holds(robin)), str(q.owners()))
    time.sleep(NEG)
    check("the third keeps waiting", q.procs[jordan].poll() is None and jordan not in q.owners(), str(q.owners()))
    out = q.cmd("done", alex)
    check("done releases the holder's lane", "released" in out, out)
    check("done drops the holder's line", alex not in q.queued(), str(q.queued()))
    check("the next in line takes the freed lane", q.settle(lambda: q.holds(jordan)), str(q.owners()))
    ava = nm("ava")
    out = q.cmd("done", ava)
    check("done by a non-holder releases nothing", "nothing released" in out and set(q.owners()) == {robin, jordan}, out)
    show = q.cmd("show")
    check("show names both lanes", robin in show and jordan in show, show)
finally:
    q.close()

print("done while a name still waits: the wait leaves, never takes a lane (10-10: 50 min held unbeaten)")
q = Queue()
try:
    kate = nm("kate")
    q.list(kate)
    for lane in ("gate.lock", "gate.lock.2"):   # both lanes busy, so the wait can't take one yet
        (q.dir / lane).mkdir()
        (q.dir / lane / "owner").write_text(f"busy-{TAG} {os.getpid()}\n")
    q.cmd("beat", kate)
    q.wait(kate)
    time.sleep(NEG)
    out = q.cmd("done", kate)   # the owner stops its runner and runs done; the remote wait is still alive
    check("done for a waiting name releases no lane", "nothing released" in out, out)
    for lane in ("gate.lock", "gate.lock.2"):
        shutil.rmtree(q.dir / lane)   # the lanes free up
    check("the orphaned wait leaves the queue instead of taking a lane",
          q.settle(lambda: q.procs[kate].poll() == 3) and q.owners() == ["", ""], str(q.owners()))
    q.procs.pop(kate)
    time.sleep(1.1)
    q.wait(kate)   # the same name waits again later (a re-gate)
    check("a wait started after the done is unaffected", q.settle(lambda: q.holds(kate)), str(q.owners()))
    q.cmd("done", kate)
finally:
    q.close()

print("an unlisted name waits behind listed waiters")
q = Queue()
try:
    leo, hale, ava = nm("leo"), nm("hale"), nm("ava")
    q.list(leo, hale)
    for lane in ("gate.lock", "gate.lock.2"):   # both lanes busy with a live holder (this process)
        (q.dir / lane).mkdir()
        (q.dir / lane / "owner").write_text(f"busy-{TAG} {os.getpid()}\n")
    q.wait(ava)
    time.sleep(0.3)
    q.wait(leo)
    time.sleep(0.3)
    q.wait(hale)
    time.sleep(NEG)
    for lane in ("gate.lock", "gate.lock.2"):
        (q.dir / lane / "owner").unlink()
        (q.dir / lane).rmdir()
    check("the listed waiters take the lanes first", q.settle(lambda: q.holds(leo) and q.holds(hale)), str(q.owners()))
    check("the unlisted one is still waiting", q.procs[ava].poll() is None, str(q.owners()))
    q.cmd("done", leo)
    check("then the unlisted one goes", q.settle(lambda: q.holds(ava)), str(q.owners()))
finally:
    q.close()

print("a lane that vanishes mid-check doesn't end a wait")
q = Queue()
try:
    # A fake stat that removes the lane it's asked about and fails: the race (another gate's done between the loop's
    # `[ -d ]` and its stat), every time instead of once in a while under load.
    fake = q.dir / "bin"
    fake.mkdir()
    (fake / "stat").write_text('#!/bin/bash\nfor a in "$@"; do case "$a" in */gate.lock) rmdir "$a" 2>/dev/null; exit 1;; esac; done\n'
                               f'exec {shutil.which("stat")} "$@"\n')
    (fake / "stat").chmod(0o755)
    (q.dir / "gate.lock").mkdir()            # a leftover lane with no owner: the loop checks its age
    (q.dir / "gate.lock.2").mkdir()
    (q.dir / "gate.lock.2" / "owner").write_text(f"busy-{TAG} {os.getpid()}\n")
    q.env["PATH"] = f"{fake}:{q.env.get('PATH', '')}"
    ava = nm("ava")
    q.wait(ava)
    check("the waiter survives and takes the freed lane", q.settle(lambda: q.holds(ava)),
          f"rc={q.procs[ava].poll()} {q.owners()}")
    q.cmd("done", ava)
finally:
    q.close()

print("a done that lands mid-loop, as the lanes free up: the wait gives the lane back and leaves")
q = Queue()
try:
    kate = nm("kate")
    q.list(kate)
    for lane in ("gate.lock", "gate.lock.2"):
        (q.dir / lane).mkdir()
        (q.dir / lane / "owner").write_text(f"busy-{TAG} {os.getpid()}\n")
    # The window check (eval_windows.py, between the loop's tombstone check and its mkdir) is where the owner's done
    # lands, and the lanes free up, on its first call: the 10-14 race, every time.
    wdir = q.dir / "windows"
    wdir.mkdir()
    (wdir / "eval_windows.conf").write_text("LEAD_MIN 0\n")
    (wdir / "eval_windows.py").write_text(
        "import os, pathlib, shutil\n"
        "d = pathlib.Path(os.environ['GATE_QUEUE_DIR'])\n"
        "flag = d / 'windows' / 'fired'\n"
        "if not flag.exists():\n"
        "    flag.touch()\n"
        "    (d / 'gate.gone').mkdir(exist_ok=True)\n"
        f"    (d / 'gate.gone' / '{kate}').touch()\n"
        "    for lane in ('gate.lock', 'gate.lock.2'):\n"
        "        shutil.rmtree(d / lane, ignore_errors=True)\n")
    q.env["GATE_QUEUE_WINDOWS"] = str(wdir / "eval_windows.conf")
    q.wait(kate)
    check("the wait leaves (exit 3)", q.settle(lambda: q.procs[kate].poll() is not None) and q.procs[kate].poll() == 3,
          f"rc={q.procs[kate].poll()} {q.owners()}")
    check("…and holds no lane", q.owners() == ["", ""] and not (q.dir / "gate.lock").exists(), str(q.owners()))
finally:
    q.close()

print("a solo runs alone")
q = Queue()
try:
    solo, alex = nm("solo-ab"), nm("alex")
    q.list(solo, alex)
    q.wait(solo)
    check("a listed solo takes both lanes", q.settle(lambda: q.holds(solo) and q.owners() == [solo, solo]), str(q.owners()))
    q.wait(alex)
    time.sleep(NEG)
    check("nobody joins a solo", q.procs[alex].poll() is None and alex not in q.owners(), str(q.owners()))
    out = q.cmd("done", solo)
    check("done releases both of a solo's lanes", "gate.lock " in out + " " and "gate.lock.2" in out, out)
    check("then the next goes", q.settle(lambda: q.holds(alex)), str(q.owners()))
finally:
    q.close()
q = Queue()
try:
    solo = nm("solo-unlisted")
    q.wait(solo)
    time.sleep(NEG)
    check("an unlisted solo waits until it's listed", q.procs[solo].poll() is None and q.owners() == ["", ""], str(q.owners()))
    q.list(solo)
    check("and goes once listed", q.settle(lambda: q.holds(solo)), str(q.owners()))
finally:
    q.close()

print("refusals: windows and the model-eval hold")
for hhmm, why in (("0730", "the nightly window"), ("0715", "10 min before the nightly"), ("0900", "the model-eval window")):
    q = Queue(hhmm)
    try:
        alex = nm("alex")
        q.list(alex)
        q.wait(alex)
        time.sleep(NEG)
        check(f"no lane in {why}", q.procs[alex].poll() is None and q.owners() == ["", ""], str(q.owners()))
        out = q.cmd("drop", alex)
        check(f"drop takes it out of the queue ({hhmm})", alex not in q.queued(), out)
    finally:
        q.close()
q = Queue()
try:
    (q.dir / "deploy.hold").write_text(f"{int(time.time()) + 600} model-eval\n")
    alex = nm("alex")
    q.wait(alex)
    time.sleep(NEG)
    check("no lane while a model-eval hold is unexpired", q.procs[alex].poll() is None, str(q.owners()))
    (q.dir / "deploy.hold").write_text(f"{int(time.time()) - 5} model-eval\n")
    check("a lane once the hold has expired", q.settle(lambda: q.holds(alex)), str(q.owners()))
finally:
    q.close()

print("a waiter whose caller is gone leaves the queue")
q = Queue()
try:
    alex = nm("alex")
    q.list(alex)
    (q.dir / "gate.lock").mkdir()
    (q.dir / "gate.lock" / "owner").write_text(f"busy-{TAG} {os.getpid()}\n")
    (q.dir / "gate.lock.2").mkdir()
    (q.dir / "gate.lock.2" / "owner").write_text(f"busy2-{TAG} {os.getpid()}\n")
    out_f, pid_f = q.dir / "caller.out", q.dir / "waiter.pid"
    caller = subprocess.Popen(["bash", "-c", f'bash "{GQ}" wait {alex} > "{out_f}" 2>&1 & echo $! > "{pid_f}"; sleep 60'],
                              env=q.env)
    time.sleep(NEG)
    caller.kill()
    caller.wait()
    check("the orphaned waiter exits", q.settle(lambda: "caller is gone" in (out_f.read_text() if out_f.exists() else "")),
          out_f.read_text() if out_f.exists() else "")
    check("its marker is gone", not (q.dir / "gate.wait" / alex).exists())
finally:
    try:   # the orphan, if it didn't leave on its own (its own PID only)
        os.kill(int(pid_f.read_text()), 9)
    except (OSError, ValueError):
        pass
    q.close()

def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"got {got!r:.200}, expected {expected!r:.200}")


print("install: the queue on titan follows the repo (#945)")
q = Queue()
try:
    target = q.dir / "gate-queue.sh"
    env = dict(q.env, GATE_QUEUE_TARGET=str(target))
    inst = lambda: subprocess.run(["bash", str(GQ), "install"], env=env, capture_output=True, text=True, timeout=30)  # noqa: E731
    r = inst()
    eq("missing: installed, byte for byte, executable",
          (r.returncode, "installed" in r.stdout, target.read_bytes() == GQ.read_bytes(), os.access(target, os.X_OK)),
          (0, True, True, True))
    r = inst()
    eq("the same: left alone", (r.returncode, "current" in r.stdout), (0, True))
    target.write_text("#!/bin/bash\n# an older queue\necho old\n")
    held = open(target)   # a waiter's bash holds the file it's reading
    ino = target.stat().st_ino
    r = inst()
    eq("different: replaced by a new file (a new inode, moved into place)",
          (r.returncode, target.read_bytes() == GQ.read_bytes(), target.stat().st_ino != ino), (0, True, True))
    eq("…the reader that had the old one still reads the old one, whole", held.read(), "#!/bin/bash\n# an older queue\necho old\n")
    held.close()
    eq("no temp file left beside it", sorted(p.name for p in q.dir.iterdir() if p.name.startswith("gate-queue.sh.")), [])
finally:
    q.close()
dep = (HERE / "deploy_titan_node.sh").read_text()
i_ok, i_inst = dep.find('echo "==> deployed'), dep.find("bash scripts/gate_queue.sh install")
eq("the deploy installs it on titan, after the canary passed, and never rolls back for it",
      (0 < i_ok < i_inst, "gate_queue.sh install || echo" in dep, dep.find("REMOTE", i_inst) > i_inst), (True, True, True))

print("stale entries leave the queue (#945)")
q = Queue("0730")   # inside a window: waiters keep waiting, so "waiting" stays true
try:
    q.env["GATE_QUEUE_STALE_MIN"] = "1"
    idle, alex, robin, solo = nm("idle"), nm("alex"), nm("robin"), nm("solo-early")
    q.list(idle, robin, solo)
    (q.dir / "gate.lock").mkdir()
    (q.dir / "gate.lock" / "owner").write_text(f"{robin} {os.getpid()}\n")
    q.cmd("show")   # starts every listed name's clock
    seen = q.dir / "gate.seen"
    eq("show starts a clock for each listed name", sorted(p.name for p in seen.iterdir()) if seen.is_dir() else [],
       sorted([idle, robin, solo]))
    for n in (idle, robin, solo):   # the rest needs the clocks; without them, stop here (that check has failed)
        (seen / n).exists() or sys.exit(1)
    old = time.time() - 120
    for n in (idle, robin):
        os.utime(seen / n, (old, old))
    out = q.cmd("show")
    eq("an idle listed name past the limit is dropped, and says so", (idle in q.queued(), f"{idle} dropped" in out), (False, True))
    eq("a name holding a lane is kept, however old its clock", robin in q.queued(), True)
    eq("a name listed early (a solo before its start) is kept inside the limit", solo in q.queued(), True)
    with open(q.dir / "gate.queue", "a") as fh:
        fh.write(alex + "\n")
    q.wait(alex)
    time.sleep(NEG)
    os.utime(seen / alex, (old, old)) if (seen / alex).exists() else None
    time.sleep(NEG)   # its own wait loop touches its clock again
    q.cmd("show")
    eq("a name whose waiter is waiting is kept", alex in q.queued(), True)
    os.utime(seen / solo, (old, old))
    q.cmd("show")
    eq("…and the early solo is dropped once it has been idle past the limit", solo in q.queued(), False)
    q.cmd("drop", alex)
    eq("drop clears the name's clock", (seen / alex).exists(), False)
finally:
    q.close()

print("heartbeats: a stopped caller's lane is cleared whatever PID it names (#1407)")
T = int(time.time())


def lane(q: "Queue", lock: str, owner: str, age_s: int = 0) -> None:
    d = q.dir / lock
    d.mkdir()
    (d / "owner").write_text(f"{owner} {os.getpid()}\n")   # a live PID: the long-lived sshd of 10-10
    os.utime(d, (T - age_s, T - age_s))


def beat_at(q: "Queue", name: str, t: int) -> None:
    (q.dir / "gate.beat").mkdir(exist_ok=True)
    f = q.dir / "gate.beat" / name
    f.touch()
    os.utime(f, (t, t))


q = Queue()
try:
    orphan, robin, alex = nm("orphan"), nm("robin"), nm("alex")
    q.env["GATE_QUEUE_NOW"] = str(T)
    lane(q, "gate.lock", orphan, 600)
    lane(q, "gate.lock.2", robin, 600)
    beat_at(q, orphan, T - 6 * 60)      # stopped beating 6 min ago (the limit is 5)
    beat_at(q, robin, T - 50)           # beating
    q.wait(alex)
    check("a live-PID holder whose beat stopped: its lane is cleared and the next waiter takes it",
          q.settle(lambda: q.holds(alex)), str(q.owners()))
    eq("…the beating holder keeps its lane", q.owners()[1], robin)
    eq("…the waiter says why", "stopped its heartbeat" in (q.procs[alex].stdout.read() if q.procs[alex].poll() is not None else ""), True)
finally:
    q.close()

q = Queue()
try:
    old, alex = nm("old"), nm("alex")
    q.env["GATE_QUEUE_NOW"] = str(T)
    lane(q, "gate.lock", old, 3600)
    lane(q, "gate.lock.2", nm("old2"), 3600)
    q.wait(alex)
    time.sleep(NEG)
    check("a holder that never beats keeps the PID rules (opt-in): a live PID keeps its lane",
          q.procs[alex].poll() is None and q.owners() == [old, nm("old2")], str(q.owners()))
finally:
    q.close()

print("a solo deploy beating through 20+ minutes keeps both lanes")
q = Queue()
try:
    solo, alex = nm("solo-deploy"), nm("alex")
    q.list(solo, alex)
    for lk in ("gate.lock", "gate.lock.2"):
        lane(q, lk, solo, 22 * 60)      # both lanes, held 22 minutes
    beat_at(q, solo, T - 40)            # its caller still beating
    q.env["GATE_QUEUE_NOW"] = str(T)
    q.wait(alex)
    time.sleep(NEG)
    check("held 22 min, beat 40 s ago: both lanes stay the solo's", q.procs[alex].poll() is None and q.owners() == [solo, solo],
          str(q.owners()))
    q.procs[alex].kill()
    q.procs[alex].wait()
    q.env["GATE_QUEUE_NOW"] = str(T + 6 * 60)   # six minutes on, no beat since
    q.wait(alex)
    check("…its caller stops beating: both lanes cleared, and the next waiter takes one",
          q.settle(lambda: q.holds(alex)), str(q.owners()))
    eq("…the other lane is free", q.owners().count(solo), 0)
finally:
    q.close()

print("a waiter whose own caller stopped beating leaves the queue; beat, done and drop")
q = Queue()
try:
    alex, robin = nm("alex"), nm("robin")
    q.list(alex)
    q.env["GATE_QUEUE_NOW"] = str(T)
    lane(q, "gate.lock", nm("busy"), 0)
    lane(q, "gate.lock.2", nm("busy2"), 0)
    beat_at(q, alex, T - 10 * 60)
    q.wait(alex)
    check("the waiter exits 3, saying its caller's heartbeat stopped",
          q.settle(lambda: q.procs[alex].poll() == 3), str(q.procs[alex].poll()))
    eq("…and its marker is gone", (q.dir / "gate.wait" / alex).exists(), False)
    eq("beat creates the name's beat file, quietly", (q.cmd("beat", robin), (q.dir / "gate.beat" / robin).exists()), ("", True))
    q.cmd("drop", robin)
    eq("drop removes it", (q.dir / "gate.beat" / robin).exists(), False)
    q.cmd("beat", robin)
    shutil.rmtree(q.dir / "gate.lock")
    lane(q, "gate.lock", robin, 120)
    beat_at(q, robin, int(time.time()) - 120)
    eq("status shows a beating holder's last beat", "beat 2m ago" in q.cmd("status").splitlines()[0], True)
    q.cmd("done", robin)
    eq("done removes it", (q.dir / "gate.beat" / robin).exists(), False)
finally:
    q.close()


print("status: lanes, waiters, listed (#945)")
q = Queue("0730")   # inside the nightly window, so a new waiter keeps waiting
try:
    alex, robin, jordan = nm("alex"), nm("robin"), nm("jordan")
    q.list(robin, alex, jordan)
    (q.dir / "gate.lock").mkdir()
    (q.dir / "gate.lock" / "owner").write_text(f"{robin} {os.getpid()}\n")
    old = time.time() - 300
    os.utime(q.dir / "gate.lock", (old, old))
    q.wait(alex)
    time.sleep(NEG)
    lines = q.cmd("status").splitlines()
    eq("three lines: lanes, waiting, listed", [x.split(":")[0] for x in lines], ["lanes", "waiting", "listed"])
    eq("the lane line names the holder and its minutes", lines[0], f"lanes: {robin} (5m) | -")
    eq("the waiter, with how long it has waited", lines[1].startswith(f"waiting: {alex} (0m"), True)
    eq("the listed order: holding, waiting, idle with its minutes", lines[2],
       f"listed: {robin} (holding), {alex} (waiting), {jordan} (idle 0m)")
finally:
    q.close()

print("the runner (gate_run.sh, the titan side in this shell)")


def git(cwd: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=Robin", "-c", "user.email=robin@example.invalid",
                           "-c", "init.defaultBranch=main", *args], cwd=cwd, env=env, capture_output=True, text=True)


def repo(stub: str, env: dict) -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="gr-"))
    origin, work = root / "origin.git", root / "work"
    git(root, "init", "-q", "--bare", str(origin), env=env)
    git(root, "clone", "-q", str(origin), str(work), env=env)
    (work / "scripts").mkdir()
    (work / "scripts" / "run_deploy_gate.sh").write_text(stub)
    git(work, "add", "-A", env=env)
    git(work, "commit", "-qm", "base", env=env)
    git(work, "push", "-q", "origin", "HEAD:main", env=env)
    git(work, "checkout", "-qb", "topic", env=env)
    (work / "note.txt").write_text("topic\n")
    git(work, "add", "-A", env=env)
    git(work, "commit", "-qm", "topic", env=env)
    return root, work


PASS = 'echo "ok   scripts/test_alpha.py"\necho "ok   scripts/test_beta.py (found)"\n'
FAILS = 'echo "ok   scripts/test_alpha.py"\necho "FAIL scripts/test_beta.py"\necho "  boom"\nexit 1\n'
for stub, want_rc, word in ((PASS, 0, "PASSED"), (FAILS, 1, "FAILED")):
    q = Queue()
    root, work = repo(stub, q.env)
    try:
        tmp = q.dir / "host"
        tmp.mkdir()
        env = dict(q.env, GATE_HOST="local", GATE_QUEUE=f"bash {GQ}", GATE_TMP=str(tmp))
        name = nm("robin")
        r = subprocess.run(["bash", str(RUN), name, str(work)], env=env, capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        check(f"a {word.lower()} gate exits {want_rc}", r.returncode == want_rc, out)
        check(f"it reports GATE {word}", f"GATE {word}" in out, out)
        if want_rc:
            check("a failed gate names the failing test", "FAIL scripts/test_beta.py" in out, out)
        else:
            check("a passing gate counts its oks", "ok=2" in out, out)
        check(f"the lane is released ({word.lower()})", q.owners() == ["", ""], str(q.owners()))
        check(f"the shipped tree and log are removed ({word.lower()})", not any(tmp.iterdir()), str(list(tmp.iterdir())))
    finally:
        q.close()
        shutil.rmtree(root, ignore_errors=True)

# #1514: the runner hands the gate the tests the diff can affect (GATE_FIRST), chosen by scripts/affected_tests.py
SHOW_FIRST = 'echo "FAIL first=$(tr "\\n" , < "$GATE_FIRST")"\nexit 1\n'
q = Queue()
root, work = repo(SHOW_FIRST, q.env)
try:
    tmp = q.dir / "host"
    tmp.mkdir()
    env = dict(q.env, GATE_HOST="local", GATE_QUEUE=f"bash {GQ}", GATE_TMP=str(tmp))
    shutil.copy(HERE / "affected_tests.py", work / "scripts" / "affected_tests.py")
    (work / "scripts" / "test_alpha.py").write_text("print('x')\n")
    (work / "scripts" / "test_beta.py").write_text("print('x')\n")
    (work / "scripts" / "sweep_nightly.sh").write_text("#!/bin/bash\n")
    (work / "scripts" / "test_gamma.py").write_text("open('scripts/sweep_nightly.sh')\n")
    git(work, "add", "-A", env=env)
    git(work, "commit", "-qm", "files", env=env)
    git(work, "push", "-q", "origin", "HEAD:main", env=env)
    (work / "scripts" / "sweep_nightly.sh").write_text("#!/bin/bash\n# changed\n")
    git(work, "add", "-A", env=env)
    git(work, "commit", "-qm", "edit the script", env=env)
    r = subprocess.run(["bash", str(RUN), nm("alex"), str(work)], env=env, capture_output=True, text=True, timeout=120)
    check("the runner ships the affected tests to the gate as GATE_FIRST", "FAIL first=test_gamma.py," in r.stdout,
          r.stdout + r.stderr)
    check("…and says how many", "affected first: 1 tests" in r.stdout, r.stdout)
finally:
    q.close()
    shutil.rmtree(root, ignore_errors=True)

q = Queue()
root, work = repo(PASS, q.env)
try:
    tmp = q.dir / "host"
    tmp.mkdir()
    env = dict(q.env, GATE_HOST="local", GATE_QUEUE=f"bash {GQ}", GATE_TMP=str(tmp))
    (work / "note.txt").write_text("edited, not committed\n")
    name = nm("jordan")
    r = subprocess.run(["bash", str(RUN), name, str(work)], env=env, capture_output=True, text=True, timeout=60)
    check("a dirty worktree is refused before it queues", r.returncode == 1 and "uncommitted" in r.stdout
          and not (q.dir / "gate.wait" / name).exists(), r.stdout + r.stderr)
    r = subprocess.run(["bash", str(RUN), "Bad Name", str(work)], env=env, capture_output=True, text=True, timeout=30)
    check("a bad name is a usage error", r.returncode == 2, r.stdout + r.stderr)
finally:
    q.close()
    shutil.rmtree(root, ignore_errors=True)

# A generated route doc gone stale in the rebase is regenerated as one commit and gated; one that can't be fixed
# stops before the tests (10-10: whole lanes spent on it).
for label, stub_py, want_rc, want_ran, want_words in (
        ("regenerated", 'import sys, pathlib\nd = pathlib.Path("docs/api-routes.md")\n'
                        'if "--check" in sys.argv: sys.exit(0 if d.exists() and d.read_text() == "routes\\n" else 1)\n'
                        'd.parent.mkdir(exist_ok=True); d.write_text("routes\\n")\n', 0, True, "regenerated after the rebase"),
        ("unfixable", 'import sys\nsys.exit(1 if "--check" in sys.argv else 0)\n', 1, False, "still out of date")):
    q = Queue()
    ran = Path(tempfile.mkdtemp(prefix="gr-ran-")) / "ran"
    root, work = repo(f'touch "{ran}"\necho "ok   scripts/test_alpha.py"\n', q.env)
    try:
        tmp = q.dir / "host"
        tmp.mkdir()
        env = dict(q.env, GATE_HOST="local", GATE_QUEUE=f"bash {GQ}", GATE_TMP=str(tmp))
        (work / "scripts" / "api_routes.py").write_text(stub_py)
        git(work, "add", "-A", env=env)
        git(work, "commit", "-qm", "routes", env=env)
        before = git(work, "rev-list", "--count", "HEAD", env=env).stdout.strip()
        name = nm("ava")
        r = subprocess.run(["bash", str(RUN), name, str(work)], env=env, capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        after = git(work, "rev-list", "--count", "HEAD", env=env).stdout.strip()
        check(f"stale api-routes.md, {label}: exit {want_rc}, tests ran={want_ran}, says so",
              r.returncode == want_rc and ran.exists() == want_ran and want_words in out, out)
        if want_rc == 0:
            check("…as exactly one new commit", int(after) == int(before) + 1, f"{before} -> {after}")
        check(f"…and the lane is released ({label})", q.owners() == ["", ""], str(q.owners()))
    finally:
        q.close()
        shutil.rmtree(root, ignore_errors=True)

src = RUN.read_text()
check("the runner beats before its wait and keeps beating until it exits (#1407)",
      src.index('"$GQ beat $NAME"') < src.index('"$GQ wait $NAME"') and "kill $BEATER" in src.split("release()")[1].split("\n")[0])
print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
