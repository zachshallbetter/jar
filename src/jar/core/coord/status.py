#!/usr/bin/env python3
"""The status board (#1519, item 3): one plain-text board of the gate queue, the last gate results, open PRs, deploys,
main's health, the next eval window and the Mac build queue. `--json` prints the same data. Read-only everywhere: titan
is read with one ssh call of cat/ls/stat/tail/date (it takes no lane, beats nothing, writes nothing, prints no process
args or env), GitHub through `gh ... list`, and the Mac queue by reading its files. Every source fails soft: a section
that can't be read says "unavailable".

  bash scripts/coord/status.sh [--json] [--last N] [--no-titan] [--no-prs]

Overrides (tests): STATUS_SSH (ssh command, default "ssh -o BatchMode=yes -o ConnectTimeout=8 titan"), STATUS_GH (default
"gh"), STATUS_REMOTE_TMP (/ai/tmp), STATUS_REMOTE_LOGS (/ai/logs), STATUS_REMOTE_QUEUE (the queue script on titan),
STATUS_GATE_LOGS (space-separated globs of gate_run.sh logs on this Mac), STATUS_REPOS (OWNER/NAME list),
STATUS_JB_QUEUE_DIR (the Mac build queue's directory), STATUS_NOW (pinned epoch seconds)."""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
import eval_windows  # noqa: E402
import local_time  # noqa: E402

UNAVAILABLE = {"status": "unavailable"}
DEFAULT_LOGS = "/private/tmp/claude-*/*/*/scratchpad/gate-*.log /tmp/coord-gates/gate-*.log"
DEFAULT_REPOS = "zachshallbetter/titan-node zachshallbetter/jellybean"
SECTIONS = ("queue", "gates", "prs", "deploys", "main", "window", "mac_queue")


def now() -> int:
    return int(os.environ.get("STATUS_NOW") or datetime.now(timezone.utc).timestamp())


def fmt_ts(ts) -> str:
    """"Sat 10-10 06:50 PDT [13:50Z]": the household zone with its label, UTC in brackets."""
    try:
        t = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"
    z = local_time.zone()
    loc = t.astimezone(z) if z else t
    return f"{loc.strftime('%a %m-%d %H:%M %Z')} [{t.strftime('%H:%MZ')}]"


def fmt_age(sec) -> str:
    if sec is None:
        return "-"
    sec = max(int(sec), 0)
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def run(cmd: list[str], timeout: int = 40, stdin_null: bool = True) -> str | None:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL if stdin_null else None)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


# --- titan: one read-only ssh call ------------------------------------------------------------------------------------

def remote_script(tmp: str, logs: str, queue: str) -> str:
    """cat/ls/stat/tail/date only. The `window` verb of the queue script only reads the clock and eval_windows.conf."""
    q = shlex.quote
    return f"""cd {q(tmp)} 2>/dev/null || {{ echo '@@nodir'; exit 0; }}
mt() {{ stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }}
echo '@@now'; date +%s
echo '@@queue'; cat gate.queue 2>/dev/null
echo '@@wait'; for f in gate.wait/*; do [ -f "$f" ] && echo "${{f##*/}} $(mt "$f")"; done
echo '@@beat'; for f in gate.beat/*; do [ -f "$f" ] && echo "${{f##*/}} $(mt "$f")"; done
echo '@@lock1'; [ -d gate.lock ] && echo "held $(mt gate.lock/owner || mt gate.lock) $(cut -d' ' -f1 gate.lock/owner 2>/dev/null)"
echo '@@lock2'; [ -d gate.lock.2 ] && echo "held $(mt gate.lock.2/owner || mt gate.lock.2) $(cut -d' ' -f1 gate.lock.2/owner 2>/dev/null)"
echo '@@hold'; head -c 120 deploy.hold 2>/dev/null
echo '@@passed'; tail -n 40 {q(logs)}/gate-passed.tsv 2>/dev/null
echo '@@deploys'; tail -n 5 {q(logs)}/deploy-gate.jsonl 2>/dev/null
echo '@@health'; tail -n 1 {q(logs)}/main-health.jsonl 2>/dev/null
echo '@@red'; [ -f main.red ] && {{ echo present; head -c 300 main.red; }}
echo '@@window'; {q(queue)} window 2>&1 | head -n 3; echo "rc=${{PIPESTATUS[0]}}"
echo '@@end'
"""


def read_titan() -> dict[str, list[str]] | None:
    ssh = shlex.split(os.environ.get("STATUS_SSH", "ssh -o BatchMode=yes -o ConnectTimeout=8 titan"))
    script = remote_script(os.environ.get("STATUS_REMOTE_TMP", "/ai/tmp"), os.environ.get("STATUS_REMOTE_LOGS", "/ai/logs"),
                           os.environ.get("STATUS_REMOTE_QUEUE", "/ai/tmp/gate-queue.sh"))
    out = run(ssh + [script])
    if not out or "@@end" not in out or "@@nodir" in out:
        return None
    sec: dict[str, list[str]] = {}
    cur = None
    for ln in out.splitlines():
        if ln.startswith("@@"):
            cur = ln[2:]
            sec[cur] = []
        elif cur is not None:
            sec[cur].append(ln)
    return sec


def pairs(lines: list[str]) -> dict[str, int]:
    out = {}
    for ln in lines:
        p = ln.split()
        if len(p) == 2 and p[1].isdigit():
            out[p[0]] = int(p[1])
    return out


def build_queue(sec: dict, t_now: int) -> dict:
    names = [ln.strip() for ln in sec.get("queue", []) if ln.strip()]
    wait, beat = pairs(sec.get("wait", [])), pairs(sec.get("beat", []))
    lanes = []
    for i, key in enumerate(("lock1", "lock2"), 1):
        ln = (sec.get(key) or [""])[0].split()
        if ln[:1] == ["held"]:
            since = int(ln[1]) if len(ln) > 1 and ln[1].isdigit() else None
            holder = ln[2] if len(ln) > 2 else ""
            lanes.append({"lane": i, "held": True, "holder": holder or "(ownerless)", "since": since,
                          "held_s": t_now - since if since else None,
                          "beat_age_s": t_now - beat[holder] if holder in beat else None})
        else:
            lanes.append({"lane": i, "held": False})
    holders = {la["holder"] for la in lanes if la.get("held")}
    waiting = []
    for n in names:
        if n in holders:
            continue
        fresh = n in wait and t_now - wait[n] < 60
        waiting.append({"name": n, "waiting": fresh, "beat_age_s": t_now - beat[n] if n in beat else None})
    hold = (sec.get("hold") or [""])[0].split()
    hold_info = None
    if hold and hold[0].isdigit():
        hold_info = {"until": int(hold[0]), "active": int(hold[0]) > t_now, "reason": " ".join(hold[1:])[:80]}
    return {"lanes": lanes, "waiting": waiting, "deploy_hold": hold_info}


# --- gate logs on this Mac ---------------------------------------------------------------------------------------------

HEAD_RX = re.compile(r"^(\S+): head ([0-9a-f]{7,40})\b")
OK_RX = re.compile(r"^ok=(\d+)")
EXIT_RX = re.compile(r"^exit=(\d+)")


def parse_gate_log(path: str) -> dict | None:
    try:
        st = os.stat(path)
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"^(?:\[[^\]]*\] )?([a-z0-9][a-z0-9-]*): waiting for its turn", text, re.M)
    if not m:
        return None
    name = m.group(1)
    heads = [h.group(2) for h in (HEAD_RX.match(ln) for ln in text.splitlines()) if h and h.group(1) == name]
    verdict = ""
    if "GATE PASSED" in text:
        verdict = "passed"
    elif "GATE FAILED" in text or "REBASE CONFLICT" in text:
        verdict = "failed"
    ok = exit_rc = None
    slowest: list[str] = []
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if OK_RX.match(ln):
            ok = int(OK_RX.match(ln).group(1))
        if EXIT_RX.match(ln):
            exit_rc = int(EXIT_RX.match(ln).group(1))
        if ln.startswith("slowest 10"):
            slowest = [x.strip() for x in lines[i + 1:i + 11] if x.strip()]
    merged = re.search(r"automerge #(\d+): MERGED (\w+)", text)
    return {"name": name, "log": path, "head": heads[-1] if heads else "", "heads": heads, "verdict": verdict,
            "ok": ok, "exit": exit_rc, "ended": int(st.st_mtime), "slowest": slowest,
            "merged": f"#{merged.group(1)} {merged.group(2)[:8]}" if merged else ""}


def read_gate_logs() -> list[dict]:
    seen, out = set(), []
    for pat in os.environ.get("STATUS_GATE_LOGS", DEFAULT_LOGS).split():
        for p in glob.glob(os.path.expanduser(pat)):
            if p not in seen:
                seen.add(p)
                g = parse_gate_log(p)
                if g:
                    out.append(g)
    out.sort(key=lambda g: g["ended"], reverse=True)
    return out


def passed_rows(lines: list[str]) -> list[dict]:
    rows = []
    for ln in lines:
        p = ln.split("\t")
        if len(p) >= 5 and p[1].isdigit():
            rows.append({"tree": p[0][:8], "at": int(p[1]), "tests": int(p[2]) if p[2].isdigit() else None, "name": p[4].strip()})
    return rows


def build_gates(sec: dict | None, logs: list[dict], last: int, queue: dict | None, t_now: int) -> dict:
    recorded = {r["name"] for r in passed_rows(sec.get("passed", []))} if sec else set()
    active = set()
    if queue:
        active = {la["holder"] for la in queue["lanes"] if la.get("held")} | {w["name"] for w in queue["waiting"]}
    results = []
    for g in logs:
        if g["verdict"]:
            state = "verified" if g["verdict"] == "passed" and g["name"] in recorded else g["verdict"]
        else:
            state = "running" if g["name"] in active else "incomplete"
        results.append({**{k: g[k] for k in ("name", "head", "ok", "exit", "slowest", "merged", "log")},
                        "state": state, "ended": g["ended"], "ended_at": fmt_ts(g["ended"])})
    shown = results[:last]
    passed = passed_rows(sec.get("passed", []))[-last:] if sec else None
    return {"results": shown, "passed_tsv": passed[::-1] if passed is not None else UNAVAILABLE}


# --- PRs ---------------------------------------------------------------------------------------------------------------

def read_prs(logs: list[dict], gate_results: list[dict]) -> dict:
    gh = shlex.split(os.environ.get("STATUS_GH", "gh"))
    state_of = {}
    for r in gate_results:
        state_of[(r["name"], r["log"])] = r["state"]
    repos = {}
    for repo in os.environ.get("STATUS_REPOS", DEFAULT_REPOS).split():
        out = run(gh + ["pr", "list", "-R", repo, "--state", "open", "--limit", "40", "--json",
                        "number,title,labels,isDraft,headRefOid,headRefName"], timeout=40)
        try:
            prs = json.loads(out) if out else None
        except ValueError:
            prs = None
        if not isinstance(prs, list):
            repos[repo.split("/")[-1]] = UNAVAILABLE
            continue
        rows = []
        for p in prs:
            labels = [(lb.get("name") if isinstance(lb, dict) else str(lb)) for lb in p.get("labels", [])]
            kind = ("draft" if p.get("isDraft") else "needs-zach" if "needs-zach" in labels
                    else "ready" if "ready" in labels else "open")
            if p.get("isDraft") and "needs-zach" in labels:
                kind = "draft,needs-zach"
            head = str(p.get("headRefOid") or "")
            gate = "none"
            for g in logs:     # newest first
                if g["heads"] and any(head.startswith(h) or h.startswith(head[:len(h)]) for h in g["heads"] if head):
                    st = state_of.get((g["name"], g["log"]), g["verdict"] or "incomplete")
                    gate = f"{st} ({g['name']})"
                    break
            rows.append({"number": p.get("number"), "title": str(p.get("title", ""))[:70], "kind": kind,
                         "head": head[:8], "branch": p.get("headRefName", ""), "gate": gate})
        repos[repo.split("/")[-1]] = rows
    return repos


# --- deploys, main, window, Mac queue ----------------------------------------------------------------------------------

def build_deploys(sec: dict) -> list:
    rows = []
    for ln in sec.get("deploys", []):
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        rows.append({"at": d.get("ts"), "at_text": fmt_ts(d.get("ts")), "commit": str(d.get("commit", "")),
                     "reused": d.get("reused"), "tests": d.get("tests"), "gate_s": d.get("gate_s"), "jobs": d.get("jobs")})
    return rows[::-1]


def build_main(sec: dict) -> dict:
    red = sec.get("red", [])
    last = None
    if sec.get("health"):
        try:
            h = json.loads(sec["health"][-1])
            ts = eval_windows_ts(h.get("ts"))
            last = {"at": ts, "at_text": fmt_ts(ts) if ts else "unknown", "head": h.get("head"), "rc": h.get("rc"),
                    "ok": h.get("ok"), "fail": h.get("fail")}
        except ValueError:
            last = None
    return {"red": {"is_red": bool(red), "detail": " ".join(red[1:]).strip()[:200] if red else ""}, "last_check": last}


def eval_windows_ts(v) -> int | None:
    if isinstance(v, (int, float)):
        return int(v)
    d = local_time.parse_iso(v)
    return int(d.timestamp()) if d else None


def build_window(sec: dict, t_now: int) -> dict:
    inside = None
    for ln in sec.get("window", []):
        if ln.startswith("inside"):
            inside = ln.strip()
    rc = next((ln for ln in sec.get("window", []) if ln.startswith("rc=")), "")
    conf = SCRIPTS / "eval_windows.conf"
    text = conf.read_text()
    lead = 12
    for ln in text.splitlines():
        p = ln.split("#", 1)[0].split()
        if len(p) == 2 and p[0] == "LEAD_MIN" and p[1].isdigit():
            lead = int(p[1])
    t0 = datetime.fromtimestamp(t_now, tz=timezone.utc)
    nxt = None
    for k in range(0, 3):
        day = t0.date() + timedelta(days=k)
        base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        for a, b, name in eval_windows.windows_for(text, day):
            s = base + timedelta(minutes=int(a[:2]) * 60 + int(a[2:]))
            e = base + timedelta(minutes=int(b[:2]) * 60 + int(b[2:]))
            if e > t0 and (nxt is None or s < nxt[0]):
                nxt = (s, e, name)
        if nxt:
            break
    nxt_d = None
    if nxt:
        s, e, name = nxt
        nxt_d = {"name": name, "start": int(s.timestamp()), "end": int(e.timestamp()), "start_text": fmt_ts(s.timestamp()),
                 "end_text": fmt_ts(e.timestamp()), "no_gates_from": fmt_ts((s - timedelta(minutes=lead)).timestamp()),
                 "starts_in_s": int((s - t0).total_seconds())}
    return {"now": inside or ("outside" if rc == "rc=1" else "unknown"), "lead_min": lead, "next": nxt_d}


def build_mac_queue() -> dict:
    if sys.platform != "darwin":
        return {"status": "not on the Mac"}
    d = Path(os.environ.get("STATUS_JB_QUEUE_DIR", "/tmp"))
    try:
        owner = ""
        lock = d / "jb-build.lock"
        if lock.is_dir():
            try:
                owner = (lock / "owner").read_text().strip()
            except OSError:
                owner = "(ownerless)"
        names = []
        qf = d / "jb-build.queue"
        if qf.exists():
            names = [ln.strip() for ln in qf.read_text().splitlines() if ln.strip()]
        rows = []
        for n in names:
            try:
                pr = (d / "jb-build.prio" / n).read_text().strip()
            except OSError:
                pr = ""
            if pr not in ("release", "fix", "feature"):
                pr = "release" if n.startswith("release-") else "fix" if n.startswith("fix-") else "feature"
            rows.append({"name": n, "priority": pr, "holding": n == (owner.split() or [""])[0]})
        return {"lock": owner or "free", "queue": rows}
    except OSError:
        return UNAVAILABLE


# --- the board ---------------------------------------------------------------------------------------------------------

def collect(last: int, titan: bool = True, prs: bool = True) -> dict:
    t_now = now()
    sec = read_titan() if titan else None
    logs = read_gate_logs()
    board: dict = {"generated": {"at": t_now, "text": fmt_ts(t_now)}}

    def soft(key, fn):
        try:
            board[key] = fn()
        except Exception:      # a source must never crash the board
            board[key] = dict(UNAVAILABLE)

    soft("queue", lambda: build_queue(sec, t_now) if sec else dict(UNAVAILABLE))
    queue = board["queue"] if "lanes" in board["queue"] else None
    soft("gates", lambda: build_gates(sec, logs, last, queue, t_now))
    gate_all = build_gates(sec, logs, 10 ** 6, queue, t_now)["results"]
    soft("prs", (lambda: read_prs(logs, gate_all)) if prs else (lambda: dict(UNAVAILABLE)))
    soft("deploys", lambda: build_deploys(sec)[:last] if sec else dict(UNAVAILABLE))
    soft("main", lambda: build_main(sec) if sec else dict(UNAVAILABLE))
    soft("window", lambda: build_window(sec, t_now) if sec else build_window({}, t_now))
    soft("mac_queue", build_mac_queue)
    return board


def is_bad(v) -> bool:
    return isinstance(v, dict) and v.get("status") == "unavailable"


def render(b: dict) -> str:
    o = [f"STATUS BOARD  {b['generated']['text']}", ""]
    o.append("GATE QUEUE (titan)")
    q = b["queue"]
    if is_bad(q):
        o.append("  unavailable")
    else:
        for la in q["lanes"]:
            if la["held"]:
                o.append(f"  lane {la['lane']}: running  {la['holder']}  held {fmt_age(la['held_s'])}"
                         f"  heartbeat {fmt_age(la['beat_age_s']) + ' ago' if la['beat_age_s'] is not None else 'none'}")
            else:
                o.append(f"  lane {la['lane']}: free")
        for i, w in enumerate(q["waiting"], 1):
            o.append(f"  {i}. queued   {w['name']}  {'waiting' if w['waiting'] else 'idle'}"
                     f"  heartbeat {fmt_age(w['beat_age_s']) + ' ago' if w['beat_age_s'] is not None else 'none'}")
        if not q["waiting"]:
            o.append("  no one queued")
        h = q["deploy_hold"]
        if h:
            o.append(f"  deploy.hold: {'active' if h['active'] else 'expired'} until {fmt_ts(h['until'])} {h['reason']}".rstrip())
    o += ["", "GATE RESULTS (logs on this Mac; verified = recorded in gate-passed.tsv)"]
    g = b["gates"]
    if is_bad(g):
        o.append("  unavailable")
    else:
        if not g["results"]:
            o.append("  none found")
        for r in g["results"]:
            tail = f"  ok={r['ok']}" if r["ok"] is not None else ""
            o.append(f"  {r['state']:<10} {r['name']}  head {r['head'] or '-'}{tail}  {r['ended_at']}"
                     + (f"  merged {r['merged']}" if r["merged"] else ""))
        sl = next((r for r in g["results"] if r["slowest"]), None)
        if sl:
            o.append(f"  slowest 10 of {sl['name']} (seconds):")
            o += ["    " + s for s in sl["slowest"]]
        o.append("  gate-passed.tsv (newest first):")
        if is_bad(g["passed_tsv"]):
            o.append("    unavailable")
        for r in ([] if is_bad(g["passed_tsv"]) else g["passed_tsv"]):
            o.append(f"    tree {r['tree']}  tests {r['tests']}  {r['name']}  {fmt_ts(r['at'])}")
    o += ["", "OPEN PRS"]
    p = b["prs"]
    if is_bad(p):
        o.append("  unavailable")
    else:
        for repo, rows in p.items():
            o.append(f"  {repo}")
            if is_bad(rows):
                o.append("    unavailable")
                continue
            if not rows:
                o.append("    none open")
            for r in rows:
                o.append(f"    #{r['number']:<5} {r['kind']:<10} head {r['head']}  gate: {r['gate']}  {r['title']}")
    o += ["", "DEPLOYS (/ai/logs/deploy-gate.jsonl)"]
    d = b["deploys"]
    if is_bad(d):
        o.append("  unavailable")
    else:
        for r in d or []:
            o.append(f"  {r['commit']}  reused={str(r['reused']).lower()}  tests={r['tests']}  gate {r['gate_s']}s  {r['at_text']}")
        if not d:
            o.append("  none logged")
    o += ["", "MAIN HEALTH"]
    m = b["main"]
    if is_bad(m):
        o.append("  unavailable")
    else:
        o.append("  main.red: " + (f"RED {m['red']['detail']}" if m["red"]["is_red"] else "absent (green)"))
        lc = m["last_check"]
        o.append("  last check: " + (f"head {lc['head']} rc={lc['rc']} ok={lc['ok']} fail={lc['fail'] or '-'}  {lc['at_text']}"
                                      if lc else "unavailable"))
    o += ["", "EVAL WINDOW"]
    w = b["window"]
    if is_bad(w):
        o.append("  unavailable")
    else:
        o.append(f"  now: {w['now']}")
        n = w["next"]
        if n:
            o.append(f"  next: {n['name']}  {n['start_text']} to {n['end_text']}  (in {fmt_age(n['starts_in_s'])})")
            o.append(f"  no gates from {n['no_gates_from']} (lead {w['lead_min']} min)")
    o += ["", "MAC BUILD QUEUE"]
    mq = b["mac_queue"]
    if is_bad(mq) or "queue" not in mq:
        o.append("  " + (mq.get("status") or "unavailable"))
    else:
        o.append(f"  lock: {mq['lock']}")
        for r in mq["queue"]:
            o.append(f"  {'running' if r['holding'] else 'queued '}  {r['name']}  [{r['priority']}]")
        if not mq["queue"]:
            o.append("  no one queued")
    return "\n".join(o) + "\n"


def main(argv: list[str]) -> int:
    args = list(argv)
    as_json = "--json" in args
    last = 5
    if "--last" in args:
        i = args.index("--last")
        try:
            last = max(1, int(args[i + 1]))
        except (IndexError, ValueError):
            print("usage: status.sh [--json] [--last N] [--no-titan] [--no-prs]", file=sys.stderr)
            return 2
    b = collect(last, titan="--no-titan" not in args, prs="--no-prs" not in args)
    sys.stdout.write(json.dumps(b, indent=2) + "\n" if as_json else render(b))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
