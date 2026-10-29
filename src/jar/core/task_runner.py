#!/usr/bin/env python3
"""Tasks that advance on their own, slice 2: the opt-in and the runner (titan-node#343; scenarios.py is the store).
Behind AGENT_TASK_AUTONOMY (off). The owner session's notes on the shape are followed throughout:
  - one step per run, through run_task's one-step path, as the task's owner;
  - before it: what changed in the task's dependencies since the last step (connector changes() and the world's
    World.changes, one row shape), and the consistency that decides whether the step runs;
  - the daily cap is checked and counted atomically (scenarios.take_step);
  - nothing steps, and no card goes out, in quiet hours (22:00–07:00 household time);
  - receipts are content-free: status codes, step names (the owner's own), tool names, ids, counts; never what a
    tool returned or anyone's text;
  - three halts in a row on the same blocker stop the task, with one card;
  - a dependency that can't be read is worded from the owner's own plan, never whose fence it is.
"""
from __future__ import annotations

import os
import posixpath
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import scenarios as sc

QUIET = (22, 7)
DAILY_HOUR = 9
DEFAULT_WATCH = ["start", "end", "location", "due_date", "amount", "date", "time"]


def _local(now: float) -> datetime:
    import local_time
    return datetime.fromtimestamp(now, local_time.zone())


def quiet(now: float) -> bool:
    h = _local(now).hour
    return h >= QUIET[0] or h < QUIET[1]


def next_due(cadence: str, now: float) -> float | None:
    """daily: the next 9:00 household time; after_each: now (the runner still honours quiet hours and the cap)."""
    if cadence == "after_each":
        return now
    if cadence == "daily":
        t = _local(now)
        nine = t.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
        return (nine if t < nine else nine + timedelta(days=1)).timestamp()
    return None


def resolve_deps(names: list[str], owner: str, role: str, *, world_find: Callable[[str], list[str]],
                 item_find: Callable[[str], list[str]]) -> dict[str, Any]:
    """The entities and synced items the task names, through the owner's own view; each watched on the fields a
    plan usually relies on (when, where, how much)."""
    ents, items = [], []
    for n in names:
        ents += [e for e in world_find(n) if e not in ents]
        items += [i for i in item_find(n) if i not in items]
    watch = {i: list(DEFAULT_WATCH) for i in items}
    watch.update({e: [] for e in ents})   # an entity's changes are disclosed; set watch fields to freeze on them
    return {"entities": ents, "items": items, "watch": watch}


def opt_in(task_id: str, owner: str, role: str, goal: str, cadence: str | None, *, daily_cap: int | None = None,
           depends_on: list[str] | None = None, issue_grant: Callable[..., dict], resolve: Callable[[list[str]], dict],
           now: float | None = None) -> str:
    """"Keep going on it daily": the scenario, its grant (titan, E0/E1, 30 days), and when the next step is due.
    A change to one already set keeps what the call leaves out (None): its cadence, cap and, above all, what it
    depends on, which is what makes it stop and ask (10-03 audit: "let it do 2 a day" emptied the watched deps)."""
    if not sc.enabled():
        return "Not changed: tasks advancing on their own are switched off on titan for now."
    if not sc.owner_allowed(owner):
        return "Not changed: tasks advancing on their own are being tried on one account first, not this one yet."
    now = now or time.time()
    if cadence == "off":
        s = sc.get(task_id)
        if s:
            sc.update(task_id, cadence="off", next_due=None)
        return "Stopped: the task won't advance on its own any more; it waits for you."
    s = sc.get(task_id)
    if s:
        deps = resolve(depends_on) if depends_on is not None else (s.get("deps") or resolve([]))
        cadence = cadence or (s["cadence"] if s.get("cadence") not in (None, "off") else "daily")
        daily_cap = daily_cap if daily_cap is not None else (s.get("daily_cap") or 1)
    else:
        deps, cadence, daily_cap = resolve(depends_on or []), cadence or "daily", daily_cap or 1
        s = sc.create(task_id, owner, goal, deps=deps, cadence=cadence, daily_cap=daily_cap, now=now)
    g = issue_grant(owner, ("task_step", f"task:{task_id}"), now)
    sc.update(task_id, cadence=cadence, daily_cap=max(1, int(daily_cap)), deps=deps, grant_id=g["id"],
              next_due=next_due(cadence, now), state="active")
    when = "every morning at 9" if cadence == "daily" else "one step after another"
    watched = len(deps["items"]) + len(deps["entities"])
    return (f"“{goal}” will advance on its own, {when}, at most {max(1, int(daily_cap))} step(s) a day, never at "
            f"night. Each step's result comes as a card. If something it depends on changes in a way that affects "
            f"the plan, it stops and asks you" + (f" (watching {watched} things it depends on)" if watched else "")
            + ". Say “stop the task” any time. (Don't run a step now: the first one comes on schedule.)")


def _changes(s: dict[str, Any], since: float, changes_fn: Callable, world_fn: Callable) -> list[dict] | None:
    deps = s.get("deps") or {}
    try:
        rows = []
        if deps.get("items"):
            rows += changes_fn(since, item_ids=deps["items"], person=s["owner"], role=None)
        if deps.get("entities"):
            rows += world_fn(deps["entities"], since, s["owner"], None)
        return sorted(rows, key=lambda r: r.get("at") or 0)
    except Exception:  # noqa: BLE001 - unreadable: the task stops at the boundary
        return None


CHAIN_STEPS = 4          # #343: at most this many read-only steps after a run's first one
CHAIN_SECONDS = 180.0    # …and within this much wall time across the run


READ_TOOLS = ("read_file",)   # #395: device reads a task's steps may be granted (item reads come with ITEM_REFERENCES)
READS_DAYS = 30


def clean_folder(p: Any) -> str | None:
    """A folder in the owner's home folder, as "~/…" (never the home folder itself, never ".."); None otherwise."""
    p = str(p or "").strip()
    if not p or ".." in p.replace("\\", "/").split("/"):
        return None
    p = re.sub(r"^/Users/[^/]+(?=/|$)", "~", p)
    if not p.startswith("~/"):
        return None
    p = posixpath.normpath(p)
    return p if p.startswith("~/") and len(p) > 2 else None


def within(path: Any, folder: str) -> bool:
    """Whether a path the step asked for lies inside the granted folder (the same "~/" spelling; never "..")."""
    p = str(path or "").strip()
    if not p or ".." in p.replace("\\", "/").split("/"):
        return False
    p = posixpath.normpath(re.sub(r"^/Users/[^/]+(?=/|$)", "~", p))
    return p == folder or p.startswith(folder.rstrip("/") + "/")


def allow_reads(task_id: str, owner: str, role: str, tools: list[str], folder: Any, *,
                issue_reads_grant: Callable[..., dict], now: float | None = None) -> str:
    """#395: the owner lets their task's steps read files on their own Mac, inside one folder, for READS_DAYS: a
    separate titan grant (E0, the named reads only) and the scope kept with the task. Never a change, never another
    person's device."""
    s = sc.get(task_id)
    if not s or s.get("owner") != owner:
        return "Not changed: that task isn't yours, or isn't set to advance on its own."
    if role not in ("admin", "adult"):
        return "Not changed: only an adult can let a task read files."
    bad = [t for t in tools if t not in READ_TOOLS]
    if bad or not tools:
        return "Not changed: a task's steps may only be allowed to read files (nothing that changes them)."
    f = clean_folder(folder)
    if not f:
        return "Not changed: name one folder in your home folder for it to read, like ~/Documents/School."
    now = now or time.time()
    g = issue_reads_grant(owner, ("task_step", f"task:{task_id}:reads"), sorted(set(tools)), now)
    until = float(g.get("expires_at") or now + READS_DAYS * 86400)
    deps = dict(s.get("deps") or {})
    deps["reads"] = {"grant_id": g["id"], "tools": sorted(set(tools)), "folder": f, "until": until}
    sc.update(task_id, deps=deps)
    return (f"Its steps may read files in {f} on your Mac until {_local(until):%b %-d}: nothing outside that folder, "
            "and never a change. Each run's card lists what it read. Say “stop its file reads” any time.")


def stop_reads(task_id: str, owner: str, *, revoke: Callable[[str], Any]) -> str:
    """The owner takes the reads back (one tap from the task's card): the grant revoked, the scope gone; the next step
    has no device at all."""
    s = sc.get(task_id)
    if not s or s.get("owner") != owner:
        return "Not changed: that task isn't yours."
    deps = dict(s.get("deps") or {})
    r = deps.pop("reads", None)
    if not r:
        return "It wasn't reading any files."
    try:
        revoke(r["grant_id"])
    finally:
        sc.update(task_id, deps=deps)
    return "Stopped: its steps won't read any files on your Mac."


def reads_of(s: dict[str, Any], now: float, reads_ok: Callable[[str, str], bool]) -> dict[str, Any] | None:
    """The reads this task's next step may make: granted, not past their date, and the grant still good (a revoke stops
    the next step's reads)."""
    r = (s.get("deps") or {}).get("reads")
    if not r or float(r.get("until") or 0) <= now or not reads_ok(r.get("grant_id") or "", s["owner"]):
        return None
    return r


_READ_LINE_RX = re.compile(r"^Read on your Mac: (.+)$", re.M)


def _reads_named(result: str) -> list[str]:
    return [n.strip() for m in _READ_LINE_RX.finditer(result or "") for n in m.group(1).split(",") if n.strip()]


def chain_on() -> bool:
    """TASK_CHAIN=1: quick read-only steps run back to back within one run (#343). Off by default."""
    return os.environ.get("TASK_CHAIN", "").strip() == "1"


def _status_of(result: str) -> str:
    if "— needs a change;" in result:
        return "needs_write"
    if "— needs you:" in result:
        return "blocked"
    if "task is completed" in result:
        return "done"
    if "⏸" in result or "couldn't run now" in result:
        return "failed"
    return "advanced"


def _steps_named(result: str) -> list[str]:
    """The owner's own step names from run_task's lines (never the result text after the dash)."""
    return [m.group(1).strip() for m in re.finditer(r"^[✓⏸] (.+?) — ", result or "", re.M)][:5]


# Why a task stopped advancing on its own, in the words its cards use (jellybean#228).
HALT_WORDS = {
    "step_needs_you": "A step needs you.",
    "dependency_changed": "Something it depends on changed.",
    "dependency_unreadable": "It can't read something it depends on.",
    "owner_unknown": "Its owner couldn't be looked up.",
}


def autonomy(task_id: str, db: Any = None) -> dict[str, Any] | None:
    """How a task advances on its own, for the task list: {on, cadence, next_at, halted, reason}; None when it was
    never opted in. Content-free: a reason is a fixed phrase, never the step's text."""
    s = sc.get(task_id, db)
    if not s:
        return None
    state, cadence = s.get("state"), s.get("cadence") or "off"
    on = cadence != "off" and state in ("active", "waiting")
    halted = state in ("waiting", "stopped")
    reason = None
    if state == "stopped":
        reason = ("It hit the same problem three times in a row." if sc.should_stop(task_id, db)
                  else "Its permission to run on its own ended.")
    elif state == "waiting":
        reason = HALT_WORDS.get(sc.last_halt(task_id, db) or "", "It's waiting on you.")
    nd = s.get("next_due")
    next_at = (_local(nd).isoformat(timespec="seconds")
               if on and state == "active" and nd else None)
    return {"on": on, "cadence": cadence if cadence != "off" else None, "next_at": next_at, "halted": halted,
            "reason": reason}


def run_due(now: float | None = None, *, execute_step: Callable[[dict, str, dict], str], card: Callable[..., Any],
            changes_fn: Callable, world_fn: Callable, describe: Callable[[list[dict]], str],
            grant_use: Callable[[str, str, str], str | None], person_of: Callable[[str], dict | None],
            db: Any = None, current_step: Callable[[str], tuple[str, str] | None] | None = None,
            cap_card: Callable[..., Any] | None = None,
            reads_ok: Callable[[str, str], bool] | None = None) -> list[dict[str, Any]]:
    """One pass: every due task takes at most one step. execute_step(scenario, context, person) runs run_task's one
    step as the owner and returns its text. person_of(owner) is the owner's person record, None when it can't be
    looked up (store down, person removed): then no step at all, never a role defaulted upward. Returns
    content-free outcomes."""
    if not sc.enabled():
        return []
    now = now or time.time()
    if quiet(now):
        return []   # no steps and no cards at night; the morning pass picks it up
    out = []
    for s in sc.due(now, db):
        tid, owner = s["task_id"], s["owner"]
        if not sc.owner_allowed(owner):
            continue   # outside the trial's owners: left exactly as it is
        try:
            person = person_of(owner)
        except Exception:  # noqa: BLE001
            person = None
        if not person or not person.get("role"):
            # Fail closed (owner's review): a halt on record, no step, no card (there's no one known to send it to).
            e0 = sc.envelope(s, "next", 1, grant_digest=s.get("grant_id"), changes=[], now=now)
            sc.record(e0, sc.halt("owner_unknown", [], [], owner), db=db)
            out.append({"task_id": tid, "status": "halted", "reason": "owner_unknown"})
            continue
        since = s.get("last_step_at") or s.get("created_at") or 0
        ch = _changes(s, since, changes_fn, world_fn)
        env = sc.envelope(s, "next", 1, grant_digest=s.get("grant_id"), changes=ch, now=now)
        key = f"task-{tid}"
        if sc.should_stop(tid, db):
            sc.update(tid, state="stopped", next_due=None, db=db)
            card(f"Stopped: {s['goal']}", "It hit the same problem three times in a row, so it stopped. Say “continue "
                 "the task” when it's sorted.", owner=owner, key=key, urgency=1)
            out.append({"task_id": tid, "status": "stopped"})
            continue
        if env["consistency"] in ("unknown", "conflicted"):
            code = env["consistency"]
            reason = ("dependency_unreadable" if code == "unknown" else "dependency_changed")
            if sc.same_blocker(tid, reason, env["context_sha256"], db):
                sc.update(tid, state="waiting", db=db)
                out.append({"task_id": tid, "status": "waiting"})
                continue
            sc.record(env, sc.halt(reason, [], [], owner), db=db)
            sc.update(tid, state="waiting", db=db)
            if code == "unknown":
                detail = "I can't read something this task depends on right now, so it's waiting. Say “continue” to go on anyway."
            else:
                detail = ("This changed since the last step:\n" + describe(env["deciding"]) +
                          "\nSay “continue” to go on with the plan as it is, or tell me what to change.")
            card(f"Waiting on you: {s['goal']}", detail, owner=owner, key=key, urgency=2,
                 actions=[{"label": "Continue", "kind": "prompt", "value": f"Continue the task {tid}"},
                          {"label": "Stop", "kind": "prompt", "value": f"Stop the task {tid}"}])
            out.append({"task_id": tid, "status": "waiting", "reason": reason})
            continue
        why = grant_use(s.get("grant_id") or "", owner, tid)
        if why:
            sc.update(tid, state="stopped", next_due=None, db=db)
            card(f"Stopped: {s['goal']}", "Its permission to run on its own ended (" + why + "). Say “keep going on "
                 "it daily” to start again.", owner=owner, key=key, urgency=1)
            out.append({"task_id": tid, "status": "stopped", "reason": "grant"})
            continue
        # a step with a cap to approve (jellybean#707): never run uncapped; the owner gets one slider card
        cap = None
        cur = current_step(tid) if current_step is not None else None
        if cur:
            import step_caps
            cap = step_caps.need(tid, cur[0], db=db)
            if cap and cap["state"] != "approved":
                if cap_card is not None:
                    step_caps.ask(cap, cap_card, db=db)
                sc.update(tid, state="waiting", db=db)
                out.append({"task_id": tid, "status": "needs_cap"})
                continue
        if not sc.take_step(tid, s.get("daily_cap") or 1, now=now, db=db):
            sc.update(tid, next_due=next_due("daily", now + 60), db=db)
            out.append({"task_id": tid, "status": "capped"})
            continue
        ctx = _context(s, env, describe)
        reads = reads_of(s, now, reads_ok) if reads_ok is not None else None
        if cap:
            import step_caps
            ctx += "\n" + step_caps.context_line(cap)
        result = (execute_step(s, ctx, person, reads=reads) if reads else execute_step(s, ctx, person)) or ""
        status = _status_of(result)
        if cap:
            import step_caps
            above = step_caps.over(cap, result)
            if above:
                # over the approved limit: not accepted, the task halts and says so (the amounts, never the text)
                env_o = {"status": "over_cap", "steps": [], "changes_disclosed": len(env["changes"])}
                env_o.update(sc.halt("over_cap", [], [], owner))
                sc.record(env, env_o, db=db)
                sc.update(tid, state="waiting", last_step_at=now, db=db)
                card(f"Over the limit: {s['goal']}",
                     f"The step came back above the limit you set ({step_caps.fmt(cap['unit'], cap['value'])}): "
                     f"{', '.join(step_caps.fmt(cap['unit'], v) for v in above[:3])}. Nothing was accepted; "
                     "tell me what to change.", owner=owner, key=key, urgency=2,
                     actions=[{"label": "Stop", "kind": "prompt", "value": f"Stop the task {tid}"}])
                out.append({"task_id": tid, "status": "over_cap"})
                continue
        chained: list[str] = []
        if status == "advanced" and chain_on():
            status, result, chained, env = _chain(s, tid, owner, person, env, now, execute_step=execute_step,
                                                  changes_fn=changes_fn, world_fn=world_fn, describe=describe,
                                                  grant_use=grant_use, current_step=current_step, db=db,
                                                  first=result, reads_ok=reads_ok)
        outcome = {"status": status, "steps": _steps_named(result), "changes_disclosed": len(env["changes"])}
        if status == "blocked":
            outcome.update(sc.halt("step_needs_you", [], outcome["steps"], owner))
        sc.record(env, outcome, db=db)
        sc.update(tid, last_step_at=now, db=db, **({"state": "done", "next_due": None} if status == "done" else
                                                   {"state": "waiting"} if status == "blocked" else
                                                   {"next_due": next_due(s["cadence"], now + 60)}))
        lead = (sc.since_block(env["changes"], describe) + "\n\n") if env["consistency"] == "stale_safe" else ""
        title = {"done": "Done", "blocked": "Waiting on you", "failed": "Couldn't take a step"}.get(status, "Step done")
        read_names = _reads_named(result)
        r_now = (s.get("deps") or {}).get("reads")
        read_line = (f" Read on your Mac: {', '.join(read_names)}." if read_names else "")
        stop_reads_action = ([{"label": "Stop file reads", "kind": "prompt", "value": f"Stop the file reads for task {tid}"}]
                             if r_now else [])
        if chained:
            # one card for the run (#343): the steps taken, by their titles and a count, never what a step read
            names = _steps_named(result) or chained
            title = {"done": "Done", "blocked": "Waiting on you"}.get(status, f"{len(names)} steps done")
            body = "Steps taken: " + "; ".join(names) + "." + read_line
            if status == "needs_write":
                body += " The next step makes a change, so it runs at the next scheduled step."
            card(f"{title}: {s['goal']}", lead + body, owner=owner, key=key, urgency=2 if status == "blocked" else 1,
                 actions=[{"label": "Stop", "kind": "prompt", "value": f"Stop the task {tid}"}] + stop_reads_action)
        else:
            card(f"{title}: {s['goal']}", lead + result[:600] + (read_line if read_names and read_line.strip() not in
                                                                  result[:600] else ""), owner=owner, key=key,
                 urgency=2 if status == "blocked" else 1,
                 actions=[{"label": "Stop", "kind": "prompt", "value": f"Stop the task {tid}"}] + stop_reads_action)
        out.append({"task_id": tid, "status": status, **({"chained": len(chained)} if chained else {})})
    return out


def _chain(s: dict[str, Any], tid: str, owner: str, person: dict, env: dict[str, Any], now: float, *,
           execute_step: Callable[..., str], changes_fn: Callable, world_fn: Callable, describe: Callable,
           grant_use: Callable[[str, str, str], str | None], current_step: Callable | None, db: Any,
           first: str, clock: Callable[[], float] = time.monotonic,
           reads_ok: Callable[[str, str], bool] | None = None) -> tuple[str, str, list[str], dict[str, Any]]:
    """#343: after a run's first step advanced, more steps back to back, each in read-only mode (run_task's chained
    tools: E0 only, no egress, no device), until one needs a change (left for the next scheduled run), needs the
    person, ends the task, fails, or a budget (CHAIN_STEPS, CHAIN_SECONDS) runs out. Every chained step re-runs the
    guards a single step has (should_stop, the grant, a cap to approve, quiet hours) and reads a fresh envelope:
    anything conflicted or unknown about its dependencies ends the chain (the next run handles it as today); stale but
    safe continues. → (status, the run's combined result lines, chained step names, the latest envelope)."""
    t0 = clock()
    results, names, status = [first], _steps_named(first), "advanced"
    last = now
    for _ in range(CHAIN_STEPS):
        elapsed = clock() - t0
        if elapsed >= CHAIN_SECONDS or quiet(now + elapsed) or sc.should_stop(tid, db):
            break
        if grant_use(s.get("grant_id") or "", owner, tid):
            break
        if current_step is not None:
            cur = current_step(tid)
            if cur:
                import step_caps
                cap = step_caps.need(tid, cur[0], db=db)
                if cap and cap["state"] != "approved":
                    break
        ch = _changes(s, last, changes_fn, world_fn)
        env = sc.envelope(s, "next", 1, grant_digest=s.get("grant_id"), changes=ch, now=now + elapsed)
        if env["consistency"] in ("unknown", "conflicted"):
            break
        reads = reads_of(sc.get(tid, db) or s, now + elapsed, reads_ok) if reads_ok is not None else None
        result = (execute_step(s, _context(s, env, describe), person, chained=True, reads=reads) if reads else
                  execute_step(s, _context(s, env, describe), person, chained=True)) or ""
        st = _status_of(result)
        results.append(result)
        names += _steps_named(result)
        last = now + (clock() - t0)
        if st != "advanced":
            status = st
            break
    return status, "\n".join(results), names, env


def _context(s: dict[str, Any], env: dict[str, Any], describe: Callable[[list[dict]], str]) -> str:
    """The step prompt's scenario: the person's answers, and what changed (the plan still holds)."""
    parts = [f"This task advances on its own ({s['cadence']}); it's {s['owner']}'s."]
    if s.get("answers"):
        parts.append("What they told you about it:\n" + "\n".join(f"- {a['text']}" for a in s["answers"][-5:]))
    if env["consistency"] == "stale_safe" and env["changes"]:
        parts.append(sc.since_block(env["changes"], describe) + "\nTake it into account; the plan still holds.")
    return "\n\n".join(parts)
