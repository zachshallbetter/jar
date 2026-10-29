#!/usr/bin/env python3
"""Weekly review of real conversations (goal items 4 and 5, 10-01).

It drafts findings like the 09-30 phone review from titan's own records. A person or a session
confirms or strikes each one; nothing here changes titan.

  turns    /ai/logs/agent-turns.jsonl, only source=user (no eval, canary or test turns): errors,
           fallbacks, budget hits, slow turns and slow device tools.
  words    titan_memory.db messages lined up with those turns (within 10 s): your corrections ("no",
           "that's wrong", "I said"), questions asked twice, titan's own "Correction:" lines (the
           claim check), promises to check back, raw errors and tool names in replies.
  memory   the 50 newest stored facts: which today's filters would refuse, which repeat another,
           and the rest listed for a person to confirm. The share that holds up is the number.

Privacy: the draft is for the admin, so it quotes only the admin's own conversation ("chat",
household_identity.conv_id_for). Every other conversation (another person's "chat:<id>", or one
with no known owner) is counted, never quoted. Excerpts are redacted (redact_personal) and at
most 120 chars, and the draft is written mode 600. Likewise, facts are listed only for the admin
(person zach, or none); other people's facts are counted, never shown.

  python3 scripts/turn_review.py [--days 7] [--out DIR] [--no-log]

Writes DIR/turn-review-YYYYMMDD.md (default /ai/logs/turn-review) and appends one JSON line to
/ai/logs/real-turn-review.jsonl: {ts, days, turns, errors, fallbacks, budget_hits, slow, corrections,
repeats, claim_corrections, promises, raw_errors, tool_names, facts_sampled, facts_filtered,
facts_duplicate, facts_to_confirm}.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_time  # noqa: E402

TURNS = Path(os.environ.get("AGENT_TURN_LOG", "/ai/logs/agent-turns.jsonl"))
MEMDB = __import__("agent_paths").memory_db()
LOG = Path(os.environ.get("TURN_REVIEW_LOG", "/ai/logs/real-turn-review.jsonl"))
SLOW_TURN_MS = 30_000
SLOW_TOOL_MS = 10_000
TOOL_FAIL_MIN = 3   # a tool that failed this often in the window is a candidate (#651)
ALIGN_S = 10

CORRECTION_RX = re.compile(r"^\s*(?:no\b|nope\b|wrong\b|that'?s (?:not|wrong|incorrect)|actually\b|i said\b|i meant\b|"
                           r"not what i|you didn'?t\b|it didn'?t\b|it'?s not\b|still (?:not|doesn'?t|isn'?t)\b|why did you\b)",
                           re.I)
PROMISE_RX = re.compile(r"\bI'?ll (?:check back|let you know|follow up|keep an eye|get back to you|remind you later)", re.I)
RAW_ERROR_RX = re.compile(r"HTTP Error \d{3}|Traceback|Exception:|something went wrong|\btimed out\b", re.I)
CLAIM_RX = re.compile(r"^Correction: ", re.M)


def _excerpt(text: str, n: int = 120) -> str:
    import redact
    t = " ".join(redact.redact_personal(text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


def _when(ts: float) -> str:
    """In the household's time zone (titan runs on UTC), so a reader can line items up with deploys."""
    from datetime import datetime
    import local_time
    return datetime.fromtimestamp(ts, local_time.zone()).strftime("%a %m-%d %H:%M")


def turn_stats(turns: list[dict]) -> dict:
    """Content-free: what the turn log says about real turns."""
    errors: dict[str, list[float]] = collections.defaultdict(list)
    fallbacks: dict[str, list[float]] = collections.defaultdict(list)
    for t in turns:
        if t.get("error"):
            errors[str(t["error"])[:70]].append(t["ts"])
        if t.get("fallback"):
            fallbacks[str(t["fallback"])[:70]].append(t["ts"])
    slow_tools: dict[str, list[int]] = collections.defaultdict(list)
    for t in turns:
        for name, ms in _tool_times(t):
            if ms >= SLOW_TOOL_MS:
                slow_tools[name].append(int(ms))
    tool_fails: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for t in turns:   # #651: [tool, first words of a failed result (quotes and numbers taken out)]
        for pair in t.get("tool_fails") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                tool_fails[str(pair[0])][str(pair[1])] += 1
    ms = sorted(int(t.get("ms") or 0) for t in turns)
    return {"turns": len(turns), "errors": errors, "fallbacks": fallbacks, "tool_fails": tool_fails,
            "budget_hits": sum(1 for t in turns if t.get("budget_hit")),
            "slow": [t for t in turns if int(t.get("ms") or 0) >= SLOW_TURN_MS],
            "slow_tools": slow_tools,
            "p50": ms[len(ms) // 2] if ms else 0, "p95": ms[int(len(ms) * 0.95)] if ms else 0,
            "brains": collections.Counter(t.get("brain") or "none" for t in turns)}


def _tool_times(t: dict[str, Any]) -> list[tuple[str, float]]:
    """A turn's device_times as (tool, ms), less the person's time (the scorecard's rule): the turn log's
    [name, ms, asked_ms] takes off how long the app's question was up (#619); an older app's [name, ms]
    loses the turn's confirm_ms off its slowest call; a {name: ms} map is older fixtures'."""
    dt = t.get("device_times")
    if isinstance(dt, dict):
        return [(str(k), float(v)) for k, v in dt.items() if isinstance(v, (int, float))]
    out, said = [], False
    for pair in dt or []:
        try:
            ms = float(pair[1])
            if len(pair) > 2:
                ms, said = max(0.0, ms - float(pair[2])), True
            out.append([str(pair[0]), ms])
        except (TypeError, ValueError, IndexError):
            continue
    wait = 0.0 if said else float(t.get("confirm_ms") or 0)
    if wait and out:
        slowest = max(out, key=lambda x: x[1])
        slowest[1] = max(0.0, slowest[1] - wait)
    return [(n, ms) for n, ms in out]


def _admin_conv() -> str:
    try:
        import household_identity as hh
        return hh.conv_id_for(hh.ADMIN_PERSON_ID)
    except Exception:  # noqa: BLE001
        return "chat"


def word_signals(msgs: list[tuple], real_ts: list[float], quotable: str | None = None,
                 scheduled_ts: list[float] | None = None) -> dict:
    """msgs: (conv_id, role, content, ts), oldest first. Only messages within ALIGN_S of a real turn.
    Items are (ts, text, before), with text and before None outside the `quotable` conversation."""
    quotable = quotable if quotable is not None else _admin_conv()
    scheduled_ts = scheduled_ts or []

    def scheduled(ts: float) -> bool:   # "I'll remind you at 5" is honest when the turn scheduled it (#263)
        i = bisect.bisect_left(scheduled_ts, ts - ALIGN_S)
        return i < len(scheduled_ts) and scheduled_ts[i] <= ts + ALIGN_S
    def real(ts: float, after: float = ALIGN_S) -> bool:
        i = bisect.bisect_left(real_ts, ts - ALIGN_S)
        return i < len(real_ts) and real_ts[i] <= ts + after
    by_conv: dict[str, list[tuple]] = collections.defaultdict(list)
    for m in msgs:
        by_conv[m[0]].append(m)
    out = {k: [] for k in ("corrections", "repeats", "claim_corrections", "promises", "raw_errors", "tool_names")}
    try:
        import turn_eval
        named = turn_eval.named_tools
    except Exception:  # noqa: BLE001
        named = lambda text, q="": []   # noqa: E731
    for conv_key, conv in by_conv.items():
        mine = conv_key == quotable
        last_user: dict[str, float] = {}
        prev = None
        for conv_id, role, content, ts in conv:
            if role == "user" and not real(ts, after=180):   # a turn logs when it ends
                prev = (conv_id, role, content, ts)
                continue
            if role == "user":
                said = content.split("[Attached", 1)[0].strip()
                # the attachment is part of the question: "What's in this file?" about three PDFs isn't a repeat
                key = " ".join(content.split("Its text", 1)[0].lower().split())[:400]
                if prev and prev[1] == "assistant" and ts - prev[3] < 1800 and CORRECTION_RX.search(said) and real(prev[3]):
                    out["corrections"].append((ts, said, prev[2]) if mine else (ts, None, None))
                if key and key in last_user and ts - last_user[key] < 900:
                    out["repeats"].append((ts, said if mine else None, None))
                last_user[key] = ts
            elif role == "assistant" and real(ts):
                if CLAIM_RX.search(content):
                    out["claim_corrections"].append((ts, content[content.index("Correction: "):] if mine else None, None))
                if PROMISE_RX.search(content) and not scheduled(ts):
                    out["promises"].append((ts, content if mine else None, None))
                if RAW_ERROR_RX.search(content):
                    out["raw_errors"].append((ts, content if mine else None, None))
                q = prev[2] if prev and prev[1] == "user" else ""
                shown = named(content, q)
                if shown:
                    out["tool_names"].append((ts, ", ".join(shown), None))   # tool names are titan's, not anyone's words
            prev = (conv_id, role, content, ts)
    return out


def memory_sample(rows: list[tuple], n: int = 50) -> dict:
    """rows: (id, text, person_id, ts), newest first. Today's filters (agent_memory._keep_extracted)
    and near-duplicates mark what wouldn't be stored now; the rest is for a person to confirm."""
    import agent_memory as am
    sample = rows[:n]
    filtered, dup, confirm, others = [], [], [], 0
    seen: list[str] = []
    for fid, text, person, _ts in sample:
        if person not in (None, "", "zach"):
            others += 1
            continue
        if not am._keep_extracted(text, ""):
            filtered.append((fid, text))
        elif am._near_duplicate(text, seen):
            dup.append((fid, text))
        else:
            confirm.append((fid, text))
        seen.append(text)
    return {"sampled": len(sample), "filtered": filtered, "duplicate": dup, "confirm": confirm, "others": others}


def render(days: int, ts: dict, ws: dict, mem: dict, claims: list[str] | None = None) -> str:
    L = [f"# Real-turn review, last {days} days (draft, {local_time.day(time.time())})", "",
         "Drafted by `scripts/turn_review.py` from titan's records. Each item is a candidate: confirm it "
         "(it becomes a finding with an owner and a check) or strike it with the reason. Times are the "
         "household's; check an item's last time against the deploys before calling it current.", "",
         "## Turns", "",
         f"- {ts['turns']} real turns. Median {ts['p50'] / 1000:.1f} s, 95th percentile {ts['p95'] / 1000:.1f} s. "
         f"Brains: {', '.join(f'{k} {v}' for k, v in ts['brains'].most_common())}.",
         f"- Errors: {sum(map(len, ts['errors'].values()))}. Fallbacks: {sum(map(len, ts['fallbacks'].values()))}. "
         f"Budget hits: {ts['budget_hits']}. Turns over {SLOW_TURN_MS // 1000} s: {len(ts['slow'])}.", ""]
    n = 0

    def cand(claim: str, evidence: list[str]) -> None:
        nonlocal n
        n += 1
        L.append(f"**D-{n:02d}.** {claim}")
        if claims is not None:
            claims.append(claim)   # counts and claims only: the review queue and any ticket never quote anyone
        L.extend(f"  - {e}" for e in evidence[:5])
        if len(evidence) > 5:
            L.append(f"  - …and {len(evidence) - 5} more")
        L.append("")

    L += ["## Candidates", ""]
    def span(tss: list[float]) -> str:
        return _when(min(tss)) + ("" if len(tss) == 1 else f" to {_when(max(tss))}")
    for err, tss in sorted(ts["errors"].items(), key=lambda x: -len(x[1]))[:5]:
        cand(f"{len(tss)} turn(s) ended in an error: “{err}” ({span(tss)}).", [])
    for fb, tss in sorted(ts["fallbacks"].items(), key=lambda x: -len(x[1]))[:3]:
        cand(f"{len(tss)} turn(s) fell back: “{fb}” ({span(tss)}).", [])
    if ts["budget_hits"]:
        cand(f"{ts['budget_hits']} turn(s) ran out of time budget.", [])
    for tool, ms in sorted(ts["slow_tools"].items(), key=lambda x: -len(x[1]))[:5]:
        cand(f"`{tool}` took {SLOW_TOOL_MS // 1000} s or more {len(ms)} time(s) (slowest {max(ms) / 1000:.0f} s).", [])
    for tool, heads in sorted(ts.get("tool_fails", {}).items(), key=lambda x: -sum(x[1].values()))[:5]:
        n_fail = sum(heads.values())
        if n_fail >= TOOL_FAIL_MIN:
            cand(f"`{tool}` failed {n_fail} time(s); most often “{heads.most_common(1)[0][0]}”.", [])
    labels = {
        "corrections": "You corrected titan right after a reply",
        "repeats": "The same question was asked twice within 15 minutes (the first answer didn't land, or the app resent it)",
        "claim_corrections": "The claim check corrected titan's own reply",
        "promises": "Titan promised to check back (it can't)",
        "raw_errors": "A raw error reached a reply",
        "tool_names": "A reply showed tool names",
    }
    for key, label in labels.items():
        items = ws[key]
        if items:
            groups: dict[tuple, list[float]] = collections.defaultdict(list)   # identical lines collapse
            hidden = [t for t, text, _ in items if text is None]
            for t, text, before in items:
                if text is not None:
                    groups[(_excerpt(text), _excerpt(before, 80) if before else "")].append(t)
            ev = []
            for (text, before), tss in sorted(groups.items(), key=lambda x: -max(x[1])):
                line = f"{span(tss)}{f' (×{len(tss)})' if len(tss) > 1 else ''}: “{text}”"
                ev.append(line + (f" (after: “{before}”)" if before else ""))
            if hidden:
                ev.append(f"{len(hidden)} in other people's conversations ({span(hidden)}), counted, not quoted")
            cand(f"{label}: {len(items)} time(s) ({span([t for t, _, _ in items])}).", ev)
    if n == 0:
        L += ["None this week.", ""]

    real = len(mem["confirm"])
    shown = mem["sampled"] - mem["others"]
    L += ["## Memory sample", "",
          f"The {mem['sampled']} newest facts. {mem['others']} belong to other people, counted and not shown. "
          f"Of your {shown}: {len(mem['filtered'])} would be refused by today's filters, {len(mem['duplicate'])} repeat "
          f"another, and {real} look durable. Confirm or strike each; the share that holds up is the number.", ""]
    for title, rows in (("Would be refused now", mem["filtered"]), ("Repeats", mem["duplicate"]),
                        ("To confirm", mem["confirm"])):
        if rows:
            L.append(f"### {title}")
            L.extend(f"- [ ] `{fid[:8]}` {_excerpt(text)}" for fid, text in rows)
            L.append("")
    return "\n".join(L)


def recent_summary(hours: int = 24, path: Path | None = None, now: float | None = None) -> str:
    """titan's recent real turns, content-free (the recent_turns tool, #349): counts, timing, errors,
    fallbacks, budget hits, slow Mac tools. Never what anyone said."""
    now = now or time.time()
    since = now - hours * 3600
    turns = []
    try:
        with (path or TURNS).open() as fh:
            for line in fh:
                try:
                    t = json.loads(line)
                except ValueError:
                    continue
                if t.get("source") == "user" and t.get("ts", 0) >= since:
                    turns.append(t)
    except OSError:
        return "No turn log to read."
    if not turns:
        return f"No real turns in the last {hours} hours."
    s = turn_stats(turns)
    lines = [f"Last {hours} h: {s['turns']} turns; median {s['p50'] / 1000:.1f} s, 95th percentile {s['p95'] / 1000:.1f} s; "
             f"brains: {', '.join(f'{k} {v}' for k, v in s['brains'].most_common())}."]
    for label, d in (("Errors", s["errors"]), ("Fallbacks", s["fallbacks"])):
        if d:
            lines.append(f"{label}: " + "; ".join(f"{len(v)}× {k}" for k, v in sorted(d.items(), key=lambda x: -len(x[1]))[:4]))
    if s["budget_hits"]:
        lines.append(f"Ran out of time: {s['budget_hits']}.")
    if s["slow"]:
        lines.append(f"Over {SLOW_TURN_MS // 1000} s: {len(s['slow'])}.")
    if s.get("tool_fails"):
        lines.append("Tools that failed most: " + "; ".join(f"{k} {sum(v.values())}×" for k, v in sorted(
            s["tool_fails"].items(), key=lambda x: -sum(x[1].values()))[:4]))
    if s["slow_tools"]:
        lines.append("Slow Mac tools: " + "; ".join(f"{k} {len(v)}× (max {max(v) / 1000:.0f} s)"
                                                    for k, v in sorted(s["slow_tools"].items(), key=lambda x: -len(x[1]))[:4]))
    if len(lines) == 1:
        lines.append("No errors, fallbacks or time-outs.")
    return "\n".join(lines)


def queue_week(claims: list[str], mem: dict, send: Any = None, db: Any = None) -> int:
    """This week's candidates and sampled facts into the review queue (review_queue, #349), and one card to
    the admins when anything was added. Returns how many were added."""
    import review_queue as rq
    week = local_time.day(time.time())
    facts = ([{"id": fid, "text": t, "suggest": "forget"} for fid, t in mem["filtered"] + mem["duplicate"]]
             + [{"id": fid, "text": t, "suggest": "keep"} for fid, t in mem["confirm"]])
    added = rq.add_week(week, [{"text": c} for c in claims], facts, db=db)
    if added:
        n_find = sum(1 for r in rq.pending(db) if r["kind"] == "finding")
        n_fact = sum(1 for r in rq.pending(db) if r["kind"] == "fact")
        if send is None:
            import proactive
            send = proactive.add_card
        send(f"This week's review: {n_find} finding{'' if n_find == 1 else 's'} and {n_fact} fact{'' if n_fact == 1 else 's'} to check",
             "From the week's real conversations and titan's newest memories. Keep or forget each fact (forgetting "
             "can be undone); confirm a finding to make it a ticket, or strike it.",
             kind="report", urgency=1, source="turn-review", to="admins", key=f"review-{week}",
             actions=[{"label": "Review now", "kind": "prompt", "value": "Show this week's review"}])
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="/ai/logs/turn-review")
    ap.add_argument("--no-log", action="store_true")
    a = ap.parse_args()
    since = time.time() - a.days * 86400
    turns = []
    with TURNS.open() as fh:
        for line in fh:
            try:
                t = json.loads(line)
            except ValueError:
                continue
            if t.get("source") == "user" and t.get("ts", 0) >= since:
                turns.append(t)
    db = sqlite3.connect(f"file:{MEMDB}?mode=ro", uri=True)
    msgs = db.execute("SELECT conv_id, role, content, ts FROM messages WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    facts = db.execute("SELECT id, text, person_id, ts FROM facts ORDER BY ts DESC").fetchall()
    sched = sorted(t["ts"] for t in turns if "schedule_action" in (t.get("tool_names") or []))
    ts, ws, mem = (turn_stats(turns), word_signals(msgs, sorted(t["ts"] for t in turns), scheduled_ts=sched),
                   memory_sample(facts))
    claims: list[str] = []
    md = render(a.days, ts, ws, mem, claims)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = out / f"turn-review-{local_time.day(time.time()).replace('-', '')}.md"
    path.write_text(md)
    os.chmod(path, 0o600)
    summary = {"ts": time.time(), "days": a.days, "turns": ts["turns"], "errors": sum(map(len, ts["errors"].values())),
               "fallbacks": sum(map(len, ts["fallbacks"].values())), "budget_hits": ts["budget_hits"], "slow": len(ts["slow"]),
               **{k: len(v) for k, v in ws.items()}, "facts_sampled": mem["sampled"] - mem["others"],
               "facts_filtered": len(mem["filtered"]), "facts_duplicate": len(mem["duplicate"]),
               "facts_to_confirm": len(mem["confirm"])}
    if not a.no_log:   # a real weekly run: the items go to the review queue, and the admin gets one card
        summary["queued"] = queue_week(claims, mem)
    print(f"draft: {path}")
    print(json.dumps(summary))
    if not a.no_log:
        try:
            with LOG.open("a") as f:
                f.write(json.dumps(summary) + "\n")
        except OSError as exc:
            print(f"(couldn't append to {LOG}: {exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
