#!/usr/bin/env python3
# gate: hermetic   (fake queue dirs and logs in temp dirs, ssh and gh stubbed through env; touches nothing under /ai)
"""#1519 item 3: scripts/coord/status.sh, the one status board. A fake titan (queue, waiters, beats, lanes, logs) in a temp
dir behind an ssh stub that runs the board's remote command locally, a gh stub, fake Mac gate logs, and a fake Mac build
queue. Checks every section of the text board and the JSON shape, that each source fails soft, that nothing is written
and that the remote command only reads.

Run: TZ=UTC python3 scripts/test_coord_status.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATUS = HERE / "coord" / "status.sh"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail[-900:] if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def touch(p: Path, text: str = "", age: int = 0) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    t = time.time() - age
    os.utime(p, (t, t))


def snapshot(root: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*") if p.is_file()}


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        tmp, logs, mac, scratch, jb = root / "tmp", root / "logs", root / "mac", root / "scratch", root / "jb"
        for p in (tmp, logs, mac, scratch, jb):
            p.mkdir()
        now = int(time.time())
        # the fake titan
        touch(tmp / "gate.queue", "holder-a\nholder-b\nwaiter-c\nwaiter-d\n")
        (tmp / "gate.lock").mkdir()
        touch(tmp / "gate.lock" / "owner", "holder-a 4242 extra\n", age=600)
        (tmp / "gate.lock.2").mkdir()
        touch(tmp / "gate.lock.2" / "owner", "holder-b 4243\n", age=120)
        touch(tmp / "gate.beat" / "holder-a", age=30)
        touch(tmp / "gate.beat" / "holder-b", age=200)
        touch(tmp / "gate.beat" / "waiter-c", age=10)
        touch(tmp / "gate.wait" / "waiter-c", age=5)
        touch(tmp / "gate.wait" / "waiter-d", age=900)
        touch(logs / "gate-passed.tsv", f"{'a' * 40}\t{now - 3600}\t460\t3.10.12\tolder\n{'b' * 40}\t{now - 60}\t465\t3.10.12\tholder-b\n")
        touch(logs / "deploy-gate.jsonl",
              '{"ts": %d, "commit": "1111111", "jobs": 4, "gate_s": 120, "tests": 460, "reused": false}\n'
              '{"ts": %d, "commit": "2222222", "jobs": 4, "gate_s": 0, "tests": 465, "reused": true}\n' % (now - 900, now - 100))
        touch(logs / "main-health.jsonl", '{"ts": "2026-10-11T11:10:31Z", "head": "c39fcaf6", "rc": 0, "ok": 450, "fail": ""}\n')
        # the fake Mac gate logs
        slow = "slowest 10 (seconds):\n" + "".join(f"     {30 - i}.00  scripts/test_s{i}.py\n" for i in range(10))
        touch(scratch / "gate-pass.log", "[10:00Z] holder-b: waiting for its turn\nholder-b: head deadbeef12 Subject\nexit=0\nok=465\n"
              + slow + "[10:03Z] holder-b: GATE PASSED\nautomerge #7: MERGED 99887766aa (gated deadbeef12)\n", age=60)
        touch(scratch / "gate-fail.log", "[09:00Z] failer-x: waiting for its turn\nfailer-x: head cafe0001 S\nexit=1\nok=3\nFAIL scripts/test_a.py\n"
              "[09:02Z] failer-x: GATE FAILED\n", age=500)
        touch(scratch / "gate-run.log", "[09:30Z] holder-a: waiting for its turn\nholder-a: head f00dbabe S\n", age=20)
        touch(scratch / "gate-noise.log", "not a gate log\n", age=10)
        # the fake Mac build queue
        touch(jb / "jb-build.queue", "fix-thing\nmy-feature\n")
        (jb / "jb-build.lock").mkdir()
        touch(jb / "jb-build.lock" / "owner", "fix-thing 777\n")
        # gh stub
        prs = {"zachshallbetter/titan-node": [
            {"number": 11, "title": "ready one", "labels": [{"name": "ready"}], "isDraft": False, "headRefOid": "deadbeef12" + "0" * 30, "headRefName": "a"},
            {"number": 12, "title": "wip", "labels": [], "isDraft": True, "headRefOid": "1" * 40, "headRefName": "b"},
            {"number": 13, "title": "ask", "labels": [{"name": "needs-zach"}], "isDraft": False, "headRefOid": "cafe0001" + "0" * 32, "headRefName": "c"}],
            "zachshallbetter/jellybean": [
            {"number": 21, "title": "jb", "labels": [], "isDraft": False, "headRefOid": "2" * 40, "headRefName": "d"}]}
        (root / "prs.json").write_text(json.dumps(prs))
        gh = root / "gh"
        gh.write_text('#!/bin/bash\nr=; while [ $# -gt 0 ]; do [ "$1" = -R ] && r=$2; shift; done\n'
                      'python3 -c "import json,sys;print(json.dumps(json.load(open(sys.argv[1]))[sys.argv[2]]))" ' + str(root / "prs.json") + ' "$r"\n')
        ssh = root / "ssh"
        ssh.write_text('#!/bin/bash\nfor a; do last=$a; done\nbash -c "$last"\n')
        gh.chmod(0o755), ssh.chmod(0o755)
        env = {**os.environ, "TZ": "UTC", "AGENT_TZ": "America/Los_Angeles", "STATUS_SSH": str(ssh), "STATUS_GH": str(gh),
               "STATUS_REMOTE_TMP": str(tmp), "STATUS_REMOTE_LOGS": str(logs), "STATUS_REMOTE_QUEUE": f"{HERE / 'gate_queue.sh'}",
               "GATE_QUEUE_DIR": str(tmp), "GATE_QUEUE_HHMM": "0730", "GATE_QUEUE_EVAL_UNITS": "",
               "STATUS_GATE_LOGS": str(scratch / "gate-*.log"), "STATUS_JB_QUEUE_DIR": str(jb),
               "STATUS_NOW": str(now), "GATE_QUEUE_DATE": ""}
        env.pop("GATE_QUEUE_DATE")
        before = snapshot(root)
        r = subprocess.run(["bash", str(STATUS)], env=env, capture_output=True, text=True, timeout=120)
        out = r.stdout
        check("text board exits 0", r.returncode == 0, r.stderr)
        for h in ("GATE QUEUE", "GATE RESULTS", "OPEN PRS", "DEPLOYS", "MAIN HEALTH", "EVAL WINDOW", "MAC BUILD QUEUE"):
            check(f"section {h}", h in out, out)
        check("lane 1 holder with heartbeat age", "lane 1: running  holder-a  held 10m  heartbeat 30s ago" in out, out)
        check("lane 2 holder with heartbeat age", "lane 2: running  holder-b  held 2m  heartbeat 3m ago" in out, out)
        check("waiting names in queue order", out.index("1. queued   waiter-c") < out.index("2. queued   waiter-d"), out)
        check("a stale waiter marker reads idle", "waiter-d  idle" in out, out)
        check("verified (gate PASSED and recorded)", "verified   holder-b  head deadbeef12" in out, out)
        check("failed result", "failed     failer-x" in out, out)
        check("a running log whose name holds a lane", "running    holder-a" in out, out)
        check("the noise log is ignored", "noise" not in out, out)
        check("merged note", "merged #7 99887766" in out, out)
        check("slowest 10 lines", out.count("scripts/test_s") == 10, out)
        check("gate-passed.tsv rows", "tree bbbbbbbb  tests 465  holder-b" in out, out)
        check("PR ready with a verified gate", "#11" in out and "ready" in out and "gate: verified (holder-b)" in out, out)
        check("PR draft", "draft" in out and "#12" in out, out)
        check("PR needs-zach with a failed gate", "needs-zach" in out and "gate: failed (failer-x)" in out, out)
        check("jellybean PR listed", "jellybean\n    #21" in out, out)
        check("deploy rows", "2222222  reused=true  tests=465" in out and "1111111  reused=false  tests=460" in out, out)
        check("main green and last check", "main.red: absent (green)" in out and "head c39fcaf6 rc=0 ok=450" in out, out)
        check("inside the window from the queue's own verb", "now: inside the eval window" in out and "Z nightly" in out, out[out.index("EVAL WINDOW"):])
        # the next window from the real clock: whichever of the two Pacific-pinned windows starts first after now
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from zoneinfo import ZoneInfo as _Z
        _la = _Z("America/Los_Angeles")
        # status.py's own rule: the first window that hasn't ended yet, so inside a window it's the one running now
        # (10-16 09:09Z, inside model-eval, this test looked for the next one to START and failed)
        _day = lambda k: _dt.combine((_dt.fromtimestamp(now, _la) + _td(days=k)).date(), _dt.min.time(), _la)  # noqa: E731
        _wins = [(_day(k).replace(hour=h, minute=m), _day(k).replace(hour=eh, minute=em))
                 for k in (-1, 0, 1, 2) for (h, m), (eh, em) in (((0, 25), (1, 10)), ((1, 35), (3, 0)))]
        _next = min(a for a, b in _wins if b.timestamp() > now).astimezone(_tz.utc).strftime("[%H:%MZ]")
        check("next window in UTC and household zone", _next in out and ("PDT" in out or "PST" in out), f"{_next}\n{out}")
        check("Mac queue", "lock: fix-thing 777" in out and "running  fix-thing  [fix]" in out and "queued   my-feature  [feature]" in out
              or sys.platform != "darwin", out)
        check("run wrote nothing", snapshot(root) == before)

        # JSON shape
        r = subprocess.run(["bash", str(STATUS), "--json", "--last", "2"], env=env, capture_output=True, text=True, timeout=120)
        j = json.loads(r.stdout)
        check("json sections", set(j) == {"generated", "queue", "gates", "prs", "deploys", "main", "window", "mac_queue"}, str(list(j)))
        check("json lanes", [la["holder"] for la in j["queue"]["lanes"]] == ["holder-a", "holder-b"]
              and j["queue"]["lanes"][0]["beat_age_s"] in (30, 31, 32), json.dumps(j["queue"]))
        check("json waiting order", [w["name"] for w in j["queue"]["waiting"]] == ["waiter-c", "waiter-d"])
        check("json --last limits results", len(j["gates"]["results"]) == 2 and len(j["deploys"]) == 2)
        check("json result fields", {"name", "head", "state", "ok", "ended", "ended_at", "slowest"} <= set(j["gates"]["results"][0]))
        check("json pr fields", {"number", "kind", "head", "gate"} <= set(j["prs"]["titan-node"][0]))
        check("json main", j["main"]["red"]["is_red"] is False and j["main"]["last_check"]["head"] == "c39fcaf6")
        check("json window", j["window"]["next"]["name"] and j["window"]["next"]["start_text"].endswith("Z]"))

        # main.red present
        touch(tmp / "main.red", "abcd1234 scripts/test_broken.py\n")
        out2 = subprocess.run(["bash", str(STATUS)], env=env, capture_output=True, text=True, timeout=120).stdout
        check("main.red shows RED and the failing test", "RED abcd1234 scripts/test_broken.py" in out2, out2)

        # fail soft: ssh down, gh down, no logs, bad paths
        bad = {**env, "STATUS_SSH": "false", "STATUS_GH": "false", "STATUS_GATE_LOGS": str(root / "none" / "*.log")}
        r = subprocess.run(["bash", str(STATUS)], env=bad, capture_output=True, text=True, timeout=120)
        check("fail soft exits 0", r.returncode == 0, r.stderr)
        check("titan sections say unavailable", out_unavailable(r.stdout, "GATE QUEUE") and out_unavailable(r.stdout, "DEPLOYS")
              and out_unavailable(r.stdout, "MAIN HEALTH") and out_unavailable(r.stdout, "OPEN PRS"), r.stdout)
        check("the eval window still shows its schedule without titan", "next:" in r.stdout, r.stdout)
        jb2 = json.loads(subprocess.run(["bash", str(STATUS), "--json"], env=bad, capture_output=True, text=True, timeout=120).stdout)
        check("json unavailable shape", jb2["queue"] == {"status": "unavailable"} and jb2["deploys"] == {"status": "unavailable"}
              and jb2["prs"] == {"status": "unavailable"} or all(v == {"status": "unavailable"} for v in jb2["prs"].values()), str(jb2["prs"]))

    # the remote command only reads
    sys.path.insert(0, str(HERE / "coord"))
    sys.path.insert(0, str(HERE))
    import status  # noqa: E402
    script = status.remote_script("/ai/tmp", "/ai/logs", "/ai/tmp/gate-queue.sh")
    for bad_word in ("rm ", "touch ", "mkdir", " > ", ">>", "mv ", "kill", "ps ", "pgrep", "environ", "cmdline", "env "):
        check(f"remote command has no {bad_word.strip()!r}", bad_word not in script.replace("2>/dev/null", "").replace("2>&1", ""))
    check("remote command only runs the queue's window verb", script.count("gate-queue.sh") == 1 and "window" in script)
    print(f"{'FAILED ' + str(len(failures)) if failures else 'all ok'}")
    return 1 if failures else 0


def out_unavailable(text: str, head: str) -> bool:
    i = text.index(head)
    return "unavailable" in text[i:i + 200].split("\n\n")[0]


if __name__ == "__main__":
    sys.exit(main())
