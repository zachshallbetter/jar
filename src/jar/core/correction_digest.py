#!/usr/bin/env python3
"""The week's corrections: where the household told titan it got something wrong, and what titan ran.

Only the admin's own conversation is read ("chat"): the digest quotes it, and goes to the admin;
each person's words stay theirs (plan item 9).

The most useful signal about titan is a person correcting it ("no", "those are both for Robin",
"look back at my emails", "stop asking", the same request twice). This reads the conversation store
(agent_memory: titan_memory.db, read-only), finds likely corrections by rule, has titan's local code
model confirm each one and say what went wrong (nothing leaves titan), pairs it with the turn log's
line for the reply being corrected (tools, client, brain, time), and writes:

  <out>/<week>.md           the digest, newest first
  <out>/<week>-tasks.jsonl  DRAFT SkillOpt tasks (never added to a task bank by this script;
                            a person reviews them into scripts/skillopt/tasks via a PR)

and one card to the admin ("Titan's week: 6 corrections"), keyed by week. Weekly timer; the owner
installs it (runbook).

  python3 scripts/correction_digest.py [--days 7] [--dry-run] [--no-model]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import local_time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ops_ports import CODE_MODEL_URL  # noqa: E402

MEMORY_DB = __import__("agent_paths").memory_db()
TURN_LOG = Path(os.environ.get("AGENT_TURN_LOG", "/ai/logs/agent-turns.jsonl"))
OUT = Path(os.environ.get("CORRECTIONS_OUT", "/ai/datasets/corrections"))
MODEL_URL = os.environ.get("SKILLOPT_OPTIMIZER_URL", CODE_MODEL_URL + "/v1")
REPEAT_WINDOW_S = 20 * 60

# A user message that pushes back on the reply before it. Tuned on the 09-29/30 history: each
# pattern is a way Zach actually corrected titan.
_CORRECTION_RX = re.compile(
    r"^\s*(no\b|nope\b|not (quite|that|what)|that'?s (not|wrong|incorrect)|wrong\b|incorrect\b|actually\b)"
    r"|\b(those|that|these|it) (are|is|was|were) (both |all )?(for|not)\b"
    r"|\blook (again|back)\b|\bdouble.?check\b|\bwhy (isn'?t|is ?n'?t|wasn'?t|doesn'?t|won'?t) (it|this|that)\b|\bi (said|asked|told you)\b|\bstop asking\b"
    r"|\bdon'?t do that\b|\bwhy (did|didn'?t|would) you\b|\byou (didn'?t|forgot|missed|ignored)\b"
    r"|\bit should (be|have)\b|\bthat'?s not (right|it|true)\b|\bstill (wrong|not|no)\b|\bseriously\b",
    re.I)


@dataclass
class Correction:
    ts: float
    asked: str          # the user message the corrected reply answered
    reply: str          # the reply being corrected
    correction: str     # what the user said next
    why: str            # "pattern" | "repeat"
    turn: dict = field(default_factory=dict)   # the turn-log line for the reply (tools, client, brain, ms)
    what_went_wrong: str = ""
    task: dict | None = None


# Only the admin's own conversation: the digest quotes what was said and goes to the admin, and
# each person's words stay theirs (plan item 9). Another person's conversation is "chat:<id>"
# (household_identity.conv_id_for); it's never read here.
OWN_CONVERSATIONS = ("chat",)


def load_messages(since: float, db: Path = MEMORY_DB,
                  convs: tuple[str, ...] = OWN_CONVERSATIONS) -> list[tuple[str, str, str, float]]:
    """(conv_id, role, content, ts), oldest first, from the admin's own conversation only."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(convs))
        return con.execute(f"SELECT conv_id, role, content, ts FROM messages WHERE ts>? AND conv_id IN ({marks}) "
                           "ORDER BY ts", (since, *convs)).fetchall()
    finally:
        con.close()


def load_turns(since: float, path: Path = TURN_LOG) -> list[dict]:
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) > since and r.get("source") == "user":
                    out.append(r)
    except FileNotFoundError:
        pass
    return out


_GENERIC = frozenset("""tell more about that this what the and you your yes yeah yep okay please thanks
can could would will with for from have has had are was were its it's that's there here now then just""".split())


def _words(text: str) -> set[str]:
    """Content words: a repeat of "tell me more about that" is a follow-up, not a do-over."""
    return set(re.findall(r"[a-z0-9']{3,}", text.lower())) - _GENERIC


def _users_words(text: str) -> str:
    """The user's own words: Jellybean appends nudges ("\\n\\n(This needs a device tool…)")."""
    return re.split(r"\n\n\((?:This|Use the)\b", text or "", maxsplit=1)[0].strip()


def household_names() -> list[str]:
    """Each household member's first name and aliases, from the people store; [] when it can't be read."""
    try:
        import household_identity as hh
        out = []
        for p in hh.get_store().people():
            for nm in [str(p.get("name") or "").split(" ")[0], *(str(a) for a in p.get("aliases") or [])]:
                if len(nm.strip()) >= 2:
                    out.append(nm.strip())
        return out
    except Exception:  # noqa: BLE001 - the digest runs without names
        return []


def _owner_rx(names: list[str]) -> re.Pattern[str] | None:
    """ "those are both Robin's": a reply's item was someone else's (#733; was one hard-coded name)."""
    if not names:
        return None
    alt = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
    return re.compile(r"\b(those|that|these|it) (are|is|was|were) (both |all )?(" + alt + r")'?s\b", re.I)


def candidates(msgs: list[tuple[str, str, str, float]], names: list[str] | None = None) -> list[Correction]:
    """Rule-based: a user message matching _CORRECTION_RX (or saying it was a household member's) right after a
    reply, or a user message that repeats one of their own from the last 20 minutes (the reply didn't do it).
    names: the household's first names and aliases (default: the people store)."""
    owner = _owner_rx(household_names() if names is None else names)
    out: list[Correction] = []
    by_conv: dict[str, list[tuple[str, str, float]]] = {}
    for conv, role, content, ts in msgs:
        by_conv.setdefault(conv, []).append((role, content, ts))
    for rows in by_conv.values():
        for i, (role, content, ts) in enumerate(rows):
            if role != "user" or i < 2 or rows[i - 1][0] != "assistant" or rows[i - 2][0] != "user":
                continue
            said = _users_words(content)
            asked, reply = _users_words(rows[i - 2][1]), rows[i - 1][1]
            why = None
            if _CORRECTION_RX.search(said) or (owner and owner.search(said)):
                why = "pattern"
            else:
                w = _words(said)
                for r2, c2, t2 in rows[:i - 1]:
                    if r2 == "user" and ts - t2 < REPEAT_WINDOW_S and len(w) >= 3:
                        w2 = _words(_users_words(c2))
                        if w2 and len(w & w2) / len(w | w2) >= 0.8:
                            why = "repeat"
                            break
            if why:
                out.append(Correction(ts=rows[i - 1][2], asked=asked, reply=reply, correction=said, why=why))
    return out


def attach_turns(found: list[Correction], turns: list[dict]) -> None:
    """The turn-log line whose end is nearest the corrected reply (within 60 s)."""
    for c in found:
        best = min(turns, key=lambda r: abs(r["ts"] - c.ts), default=None)
        if best and abs(best["ts"] - c.ts) <= 60:
            c.turn = {k: best.get(k) for k in ("tool_names", "client", "brain", "route", "ms", "error") if best.get(k)}


_CONFIRM = """A household assistant replied to a person, and the person answered. Decide whether the answer
CORRECTS the assistant (the reply was wrong, incomplete, ignored something, or did the wrong thing),
and if so say in one sentence what went wrong, then draft a regression test. A new or different
question is NOT a correction. Asking the same thing again is a correction only if the reply didn't
answer it.

Person asked: {asked}
Assistant replied: {reply}
Person answered: {correction}
Tools the assistant used: {tools}

Answer with JSON only:
{{"correction": true|false,
  "what_went_wrong": "one sentence",
  "task": {{"question": "the person's original request, reworded to stand alone",
            "expect": {{"reply_any": [...], "reply_none": [...], "tools_any": [...], "tools_none": [...]}}}}}}
Use only the expect keys that apply; words in reply_any/reply_none are lowercase fragments."""


def confirm_with_model(c: Correction, url: str = MODEL_URL, timeout: int = 300) -> dict | None:
    body = {"model": "local", "temperature": 0.1, "max_tokens": 700, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": f"{local_time.now_line()}\nThis exchange: {local_time.ago(c.ts)}.\n\n" + _CONFIRM.format(
                asked=c.asked[:600], reply=c.reply[:900], correction=c.correction[:400],
                tools=", ".join(c.turn.get("tool_names") or []) or "unknown")}]}
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = json.loads(r.read())["choices"][0]["message"]["content"]
        return json.loads(text[text.find("{"):text.rfind("}") + 1])
    except Exception:  # noqa: BLE001 - a model hiccup leaves the rule's verdict standing
        return None


def digest(found: list[Correction], week: str) -> str:
    said = [c for c in found if c.why == "pattern"]
    again = [c for c in found if c.why != "pattern"]
    lines = [f"# Titan's corrections, week {week}", "",
             f"{len(said)} correction(s) said outright, {len(again)} request(s) asked again. Newest first. "
             f"Draft tasks are in `{week}-tasks.jsonl` (review before adding).", ""]
    for heading, group in (("Corrections", said), ("Asked again (lower confidence: tests and follow-ups land here too)", again)):
        if not group:
            continue
        lines += [f"## {heading}", ""]
        lines += _entries(group)
    return "\n".join(lines)


def _entries(found: list[Correction]) -> list[str]:
    lines: list[str] = []
    for c in sorted(found, key=lambda x: -x.ts):
        when = local_time.clock(c.ts, "%a %m-%d %H:%M %Z")
        t = c.turn
        meta = ", ".join(x for x in (t.get("client"), t.get("brain"), f"{t['ms'] / 1000:.0f}s" if t.get("ms") else None) if x)
        lines += [f"### {when}: {c.what_went_wrong or c.correction[:80]}",
                  f"- **Asked:** {c.asked[:300]}",
                  f"- **Titan:** {c.reply[:400]}",
                  f"- **Then:** {c.correction[:300]}  _({c.why})_",
                  f"- **Ran:** {', '.join(t.get('tool_names') or []) or 'no tools'}" + (f" ({meta})" if meta else ""), ""]
    return lines


def propose_claims(kept: list[Correction], force: bool = False) -> int:
    """Corrections of a fact titan holds become proposals with an ask to the admin (#358): the household
    approves, nothing changes on its own. Only with the world model on (or forced, for tests)."""
    import world
    if not (force or world.enabled()):
        return 0
    w = world.get()
    n = 0
    for c in kept:
        try:
            n += len([x for x in world.from_correction(w, world.ADMIN, "admin", None, c.reply, c.correction)
                      if x[2] == "proposed"])
        except Exception as exc:  # noqa: BLE001 - the digest still goes out
            print(f"[corrections] world proposal failed: {exc}", file=sys.stderr)
    return n


def run(days: int = 7, dry_run: bool = False, use_model: bool = True, msgs=None, turns=None,
        confirm: Callable[[Correction], dict | None] | None = None, send: Callable[..., dict] | None = None,
        out: Path = OUT, world_proposals: Any = None) -> dict[str, Any]:
    since = time.time() - days * 86400
    msgs = load_messages(since) if msgs is None else msgs
    turns = load_turns(since) if turns is None else turns
    found = candidates(msgs)
    attach_turns(found, turns)
    confirm = confirm or (confirm_with_model if use_model else None)
    kept: list[Correction] = []
    for c in found:
        verdict = confirm(c) if confirm else None
        if verdict is not None and verdict.get("correction") is False:
            continue
        if verdict:
            c.what_went_wrong = str(verdict.get("what_went_wrong") or "")[:200]
            task = verdict.get("task") if isinstance(verdict.get("task"), dict) else None
            if task and task.get("question"):
                c.task = {"id": f"corr-{int(c.ts)}", "question": str(task["question"])[:400],
                          "expect": {k: v for k, v in (task.get("expect") or {}).items()
                                     if k in ("reply_any", "reply_none", "reply_all", "tools_any", "tools_none", "tools_all")
                                     and isinstance(v, list) and v},
                          "draft": True, "from": datetime.fromtimestamp(c.ts, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}
        kept.append(c)
    week = time.strftime("%G-W%V")
    result = {"week": week, "candidates": len(found), "corrections": len(kept)}
    if not dry_run and world_proposals is not False:
        result["proposals"] = propose_claims(kept, force=bool(world_proposals))
    if dry_run:
        result["digest"] = digest(kept, week)
        return result
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{week}.md").write_text(digest(kept, week))
    tasks = [c.task for c in kept if c.task]
    (out / f"{week}-tasks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tasks))
    result.update(digest_path=str(out / f"{week}.md"), tasks=len(tasks))
    if kept:
        if send is None:
            import proactive
            send = proactive.add_card
        said = sorted((c for c in kept if c.why == "pattern"), key=lambda x: -x.ts) or sorted(kept, key=lambda x: -x.ts)
        top = "; ".join((c.what_went_wrong or c.correction)[:90] for c in said[:3])
        result["card"] = send(f"Titan's week: {len(kept)} correction{'s' if len(kept) != 1 else ''}",
                              f"Most recent: {top}. The full digest and draft tests are in {out}/{week}.md.",
                              kind="info", urgency=0, source="corrections", key=f"corrections-{week}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true", help="print the digest; write and send nothing")
    ap.add_argument("--no-model", action="store_true", help="rules only (no local-model confirmation)")
    a = ap.parse_args()
    r = run(a.days, a.dry_run, not a.no_model)
    print(r.pop("digest") if a.dry_run else json.dumps({k: v for k, v in r.items() if k != "card"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
