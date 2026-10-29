#!/usr/bin/env python3
"""One items surface for every view of the main window (#1084, epic #1088): Now, Time, a project, a person, Unsorted.

Every kind becomes an **item** with one envelope:
  {id, kind, owner, audience, title, time: {at | due | span}, state: {open | waiting(who) | moving(k/n) | done},
   projects: [...], links: [...], route}
read from the stores that already exist (todos, tasks, scheduled actions, routines, references, chats, KB notes).
Nothing moves. Device-local calendar events stay on the device: the Mac and iPhone merge EventKit into Time and Now
(#1062 §10), and titan supplies everything else.

**Access projection first** (docs/surfaces.md §6): every reader is the store's own per-viewer reader, called with the
viewer, and only what it returns is ever resolved. So every count, group and string below is built from items the
viewer can see; nothing is resolved and hidden afterwards. A shared screen sees household items only.

The output is a ResolvedSurface (docs/surfaces.md §2, profile jb-1): groups with SSS roles, members with slots and
basis, the operations their existing routes carry, sizeClass after projection, and a fingerprint over accessible
inputs only.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from surfaces import label

RESOLVER = "items@1"
VIEWS = ("now", "time", "unsorted")          # plus project:<id> and person:<id>
FILEABLE = ("chat",)                         # kinds Unsorted and needs-a-home consider: listed only with a suggestion
# kinds that can be filed under a project (#1169): each carries a `file` operation where the viewer may file it
FILE_KINDS = ("chat", "todo", "task", "scheduled", "event", "file", "mail", "bill", "note", "scan", "message")
# kinds Now offers Later on (#1233): a per-viewer snooze, like a card's Later (#1039)
LATER_KINDS = ("todo", "scheduled", "task")


def may_file(it: dict[str, Any], viewer: str | None, role: str) -> bool:
    """Who may file (or unfile) an item under a project (#1169): its owner, always; any adult (or the admin) a
    household or adults item; a kid only their own; nobody someone else's private or named item. Filing never changes
    who sees the item. The routes check the same rule on the item as the caller sees it."""
    if not viewer or role not in ("admin", "adult", "kid"):
        return False
    owner, aud = str(it.get("owner") or "").lower(), str(it.get("audience") or "private")
    if owner and owner == str(viewer).lower():
        return True
    if role == "kid":
        return False
    return aud in ("household", "adults") or owner == "household"


def is_safety(it: dict[str, Any]) -> bool:
    """#1233: Lock Up and alarms are cards (proactive.SAFETY_SOURCES), never items, and a scheduled action can't touch
    a lock, alarm, garage door or camera (scheduled.refusal). One that does anyway (a row older than that rule) is
    marked `safety` by its adapter and never carries Later (jellybean#927's card rule)."""
    return bool(it.get("safety"))


def may_later(it: dict[str, Any], viewer: str | None, role: str) -> bool:
    """Who may put an item off on their own Now (#1233): a person (never a shared screen), on a to-do, a scheduled
    action or a task that isn't done, never a safety item. It hides the item for them alone."""
    return bool(viewer) and role in ("admin", "adult", "kid") and it.get("kind") in LATER_KINDS \
        and (it.get("state") or {}).get("is") != "done" and not is_safety(it)


Reader = Callable[[], list[dict[str, Any]]]


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _iso(v: Any) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(v)


def _ts(v: str | None) -> float | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


# ── adapters: a store's own (already projected) rows → items ────────────────────────────────────────────────────────

def from_todo(r: dict[str, Any]) -> dict[str, Any]:
    owner = r.get("for") or ""
    return {"id": f"todo:{r['id']}", "kind": "todo", "owner": owner,
            "audience": "household" if owner == "household" else "private", "title": r.get("title") or "",
            "time": {}, "state": {"is": "done"} if r.get("done_at") else {"is": "open"}, "projects": [], "links": [],
            "rev": str(r.get("done_at") or r.get("created_at") or ""),
            "ops": ["open", "done"] if not r.get("done_at") else ["open"],
            "routes": {"done": ("POST", f"/v1/todos/{r['id']}/done")}}


_SCHED = {"pending": "open", "running": "moving", "done": "done", "failed": "done", "cancelled": "done"}


def _hands_off(r: dict[str, Any]) -> bool:
    """A scheduled row that would touch a lock, alarm, garage door or camera: scheduled.refusal's own test."""
    if r.get("tool") not in ("home_control", "run_shortcut", "run_applescript", "run_shell"):
        return False
    import scheduled
    return bool(scheduled._HANDS_OFF_RX.search(json.dumps(r.get("args") or {}, default=str)))


def from_scheduled(r: dict[str, Any]) -> dict[str, Any]:
    st = _SCHED.get(str(r.get("state") or ""), "open")
    # card_key (#1235): the card about this row (a repeating series keeps its first row's key); matched, never served
    return {"id": f"scheduled:{r['id']}", "card_key": r.get("card_key") or f"sched-{r['id']}", "kind": "scheduled",
            "owner": r.get("owner") or "",
            "audience": r.get("audience") or "private", "title": r.get("what") or "",
            "time": {"at": _iso(r.get("run_at"))} if r.get("run_at") else {}, "state": {"is": st},
            "projects": [], "links": [], "rev": str(r.get("updated_at") or ""),
            "ops": ["open", "remove"] if st == "open" else ["open"],
            "routes": {"remove": ("DELETE", f"/v1/scheduled/{r['id']}")},
            **({"safety": True} if _hands_off(r) else {})}


def from_routine(r: dict[str, Any], next_at: str | None) -> dict[str, Any]:
    return {"id": f"routine:{r['id']}", "kind": "routine", "owner": r.get("owner") or r.get("person") or "",
            "audience": r.get("audience") or "private", "title": r.get("name") or "",
            "time": {"at": next_at} if next_at else {}, "state": {"is": "open"}, "projects": [], "links": [],
            "rev": str(r.get("updated_at") or ""), "ops": ["open"], "routes": {}}


def from_task(t: dict[str, Any]) -> dict[str, Any]:
    """A task, and its plan steps (#1108): step k/n; when titan advances it on its own (#343), the next time it acts
    (next_at); halted, it waits on its owner (who)."""
    steps = t.get("steps") or []
    k = sum(1 for s in steps if s.get("state") == "done")
    status = str(t.get("status") or "")
    auto = t.get("autonomy") or {}
    if status == "completed":
        state: dict[str, Any] = {"is": "done"}
    elif any(s.get("state") == "blocked" for s in steps) or auto.get("halted"):
        state = {"is": "waiting", "who": t.get("owner") or None}
    else:
        state = {"is": "moving", "step": k + 1 if steps else None, "of": len(steps) or None}
        if auto.get("next_at"):
            state["next_at"] = auto["next_at"]
    return {"id": f"task:{t['id']}", "kind": "task", "owner": t.get("owner") or "", "audience": "private",
            "title": t.get("goal") or "", "time": {"at": t.get("moved_at")} if t.get("moved_at") else {},
            "state": state, "projects": [], "links": [], "rev": str(t.get("updated_at") or ""), "ops": ["open"],
            "routes": {}, "steps": plan_steps(t)}


def plan_steps(t: dict[str, Any]) -> list[dict[str, Any]]:
    """#1142: a task's steps for the timeline, {title, state, who, next_at}. who: its owner on a blocked step, titan on
    the steps titan advances on its own (opted in, not halted), else none; next_at: on the current step, when titan
    acts next."""
    auto = t.get("autonomy") or {}
    on_its_own = bool(auto.get("on")) and not auto.get("halted")
    out = []
    for st in t.get("steps") or []:
        state = str(st.get("state") or "pending")
        who = (t.get("owner") or None) if state == "blocked" else "titan" if on_its_own and state in ("current", "pending") \
            else None
        out.append({"title": str(st.get("title") or "")[:160], "state": state, "who": who,
                    "next_at": auto.get("next_at") if on_its_own and state == "current" and auto.get("next_at") else None})
    return out


_BILL_STATE = {"resolved": "done", "failed": "waiting"}


# Bills in Now (Zach 10-10: "57 things wait on you", nearly all bills, one row per cycle): one row per account, its
# latest actionable cycle, "+N earlier" for the rest; actionable = failed in the last 30 days, or due from 14 days ago to
# 14 days ahead. Everything else stays on the account's own page (account@1), never in Now.
BILL_FAILED_DAYS = 30
BILL_DUE_DAYS = 14
BILL_WORDS = {"failed": "Payment failed", "overdue_unconfirmed": "Overdue?", "due": "Due", "partly_paid": "Partly paid",
              "contested": "Check this", "stale": "Old", "resolved": "Paid"}


def _actionable(t: Any, today: date) -> bool:
    if t.state == "failed":
        last = max((p.when for p in getattr(t, "pieces", []) if getattr(p, "kind", "") == "failed"), default=t.cycle)
        return (today - last).days <= BILL_FAILED_DAYS
    if t.state in ("due", "overdue_unconfirmed", "partly_paid", "contested"):
        due = t.due or t.cycle
        return -BILL_DUE_DAYS <= (due - today).days <= BILL_DUE_DAYS
    return False


def bills_now(threads: Any, today: date, facets: dict[str, dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """One bill item per account (and per owner and audience: a shared bill's count never includes someone's private
    cycles): its latest actionable cycle, "+N earlier" for its other unpaid cycles. An account with nothing actionable
    has no item here (its page shows its history)."""
    groups: dict[tuple, list[Any]] = {}
    for t in threads:
        groups.setdefault((t.account, t.owner or "", t.audience or "private"), []).append(t)
    out = []
    for _key, ts in groups.items():
        act = sorted((t for t in ts if _actionable(t, today)), key=lambda t: t.cycle)
        if not act:
            continue
        it = from_bill(act[-1])
        f = ((facets or {}).get(str(act[-1].owner or "")) or {}).get(f"vendor:{str(act[-1].vendor or '').strip().lower()}")
        if f:
            it["facet"] = f   # the owner's own facet map (#354: personal / work / …): filing words, never who sees it
        earlier = sum(1 for t in ts if t is not act[-1] and t.state != "resolved")
        if earlier:
            it["earlier"] = earlier
        out.append(it)
    return out


def from_bill(t: Any) -> dict[str, Any]:
    """#1108: one obligation thread (obligations.py, #960's lens): its vendor, amount, due date and state."""
    st = _BILL_STATE.get(t.state, "open")
    due = t.due.isoformat() + "T17:00:00Z" if t.due else None
    return {"id": f"bill:{t.account}:{t.cycle.isoformat()}", "kind": "bill", "owner": t.owner or "",
            "audience": t.audience or "private", "title": t.vendor,
            "time": {"due": due, "all_day": True} if due else {},
            "state": {"is": st, **({"who": t.owner} if st == "waiting" else {})}, "projects": [], "links": [],
            "rev": f"{t.state}:{t.amount}", "ops": ["open"], "routes": {},
            "amount": t.amount, "bill_state": t.state}


_REPLY_RX = re.compile(r"^(?:(?:re|fwd?|aw|wg|sv|tr|antw)\s*(?:\[\d+\])?\s*[:\uff1a]\s*)+", re.I)
THREAD_PREFIX = "cx-mailthread-"        # mail_server.thread_sid: a thread's own KB entry, its derived view (#446 v2)


def mail_subject(s: Any) -> str:
    """A mail subject as a name (#1497): every leading Re:/Fwd: (any number, any case, "Re[2]:" too) gone, spaces
    collapsed. "(no subject)" when nothing is left."""
    return _REPLY_RX.sub("", " ".join(str(s or "").split())).strip() or "(no subject)"


def from_mail(src: dict[str, Any], owner: str) -> dict[str, Any]:
    """#1108: a mail triage kept (personal or action, now in the KB): its owner's alone, its subject cleaned as any
    untrusted name, never its body. Since #446 v2 the KB holds one entry per thread: it is the thread (#1497), and
    Open reads it (titan's derived view, GET /v1/kb/{id})."""
    import untrusted
    md = src.get("metadata") or {}
    at = md.get("observed_at") or md.get("saved")
    sid = str(src["source_id"])
    return {"id": f"mail:{sid}", "kind": "mail", "owner": owner, "audience": "private",
            "title": untrusted.clean_name(mail_subject(src.get("title") or "mail"), limit=160),
            # "received", not "at": mail isn't scheduled, so it never fills Time; Now shows the last two days' own
            "time": {"received": str(at)} if at else {}, "state": {"is": "open"}, "projects": [], "links": [],
            "rev": str(src.get("updated_at") or at or ""), "ops": ["open"], "routes": {"open": ("GET", f"/v1/kb/{sid}")},
            **({"thread": sid} if sid.startswith(THREAD_PREFIX) else {})}


def from_event(ev: dict[str, Any]) -> dict[str, Any]:
    """#1108: a household calendar event titan reads itself (connectors). An app merging EventKit dedupes by
    (title, start, calendar)."""
    t: dict[str, Any] = {"at": ev.get("start")}
    if ev.get("end"):
        t["end"] = ev["end"]
    if ev.get("all_day"):
        t["all_day"] = True
    return {"id": f"event:{ev['id']}", "kind": "event", "owner": "household", "audience": "household",
            "title": ev.get("title") or "", "time": t, "state": {"is": "open"}, "projects": [], "links": [],
            "rev": str(ev.get("start") or ""), "ops": ["open"], "routes": {}, "calendar": ev.get("calendar")}


def from_reference(r: dict[str, Any]) -> dict[str, Any]:
    dates = r.get("dates") or {}
    return {"id": f"ref:{r['id']}", "kind": "file", "owner": r.get("owner") or "",
            "audience": r.get("audience") or "private", "title": r.get("title") or "",
            "time": {"at": _iso(dates.get("modified") or dates.get("created"))} if dates else {},
            "state": {"is": "open"}, "projects": [], "links": [], "rev": str(r.get("updated_at") or ""),
            "ops": ["open"], "routes": {}}


def from_chat(c: dict[str, Any], owner: str, filed: dict[str, Any] | None) -> dict[str, Any]:
    proj = (filed or {}).get("project")
    return {"id": f"chat:{c['id']}", "kind": "chat", "owner": owner, "audience": "private",
            "title": c.get("title") or "", "time": {"at": c.get("last_at")} if c.get("last_at") else {},
            "state": {"is": "open"}, "projects": [proj] if proj else [], "links": [],
            "rev": str(c.get("last_at") or ""), "ops": ["open", "file"],
            "routes": {"file": ("PATCH", f"/v1/conversations/{c['id']}")}}


def from_note(n: dict[str, Any], owner: str | None, audience: str) -> dict[str, Any]:
    md = n.get("metadata") or {}
    return {"id": f"note:{n['source_id']}", "kind": "note", "owner": owner or "", "audience": audience,
            "title": n.get("title") or "", "time": {"at": md.get("saved")} if md.get("saved") else {},
            "state": {"is": "open"}, "projects": [], "links": [], "rev": str(n.get("updated_at") or md.get("saved") or ""),
            "ops": ["open"], "routes": {}}


def from_linked(it: Any, title: str | None, projects: list[str], mail: dict[str, str] | None = None) -> dict[str, Any]:
    """#1085: a linker item filed under a project (a mail, a message, a scan…): shown on the project page to whoever
    may see it, by its own audience. The linker knows its day, not its time: an all-day item.
    A mail (#1497) says where it opens, from `mail` (the reader's lookup in the stores that hold it): `thread`, its
    thread's KB entry (server mail: mail_server.thread_sid), opened there; or `reference`, the device reference of a
    Mac-held mail, opened on the device that has it. A mail neither knows has no Open (never a dead button)."""
    at = it.at.isoformat() + "T12:00:00Z" if it.at else None
    out = {"id": it.item_id, "kind": it.source, "owner": it.owner or "", "audience": it.audience or "private",
           "title": title or it.source, "time": {"at": at, "all_day": True} if at else {}, "state": {"is": "open"},
           "projects": projects, "links": [], "rev": at or "", "ops": ["open"], "routes": {},
           "parties": sorted(it.keys.get("party:entity", ())) if hasattr(it, "keys") else []}
    if it.source != "mail":
        return out
    import untrusted
    out["title"] = untrusted.clean_name(mail_subject(title or ""), limit=160)
    m = mail or {}
    if str(it.item_id).startswith(THREAD_PREFIX):
        m = {**m, "thread": str(it.item_id)}
    if m.get("thread"):
        out["thread"] = m["thread"]
        out["routes"]["open"] = ("GET", f"/v1/kb/{m['thread']}")
    elif m.get("reference"):
        out["routes"]["open"] = ("GET", f"/v1/references/{m['reference']}")
        out["params"] = {"open": {"reference": m["reference"]}}
    else:
        out["ops"] = []
    return out


_AUD_RANK = {"private": 0, "admins": 1, "adults": 3, "household": 4}


def narrowest(audiences: list[Any]) -> str:
    """The narrowest of some items' audiences (#1497: a thread shown as one member is seen as its most private mail):
    private wins; named lists intersect (nobody left: private); else admins, adults, household in that order. Anything
    unreadable is private."""
    lists: list[set[str]] = []
    best = None
    for a in audiences:
        a = str(a or "private")
        if a in _AUD_RANK:
            r = _AUD_RANK[a]
        else:
            try:
                lists.append({str(x).lower() for x in json.loads(a)})
            except (ValueError, TypeError):
                return "private"
            r = 2
        best = r if best is None else min(best, r)
    if best is None or best == 0 or (lists and best < 2):
        return "private"
    if lists:
        common = set.intersection(*lists)
        return json.dumps(sorted(common)) if common else "private"
    return next(k for k, v in _AUD_RANK.items() if v == best)


def _mail_when(it: dict[str, Any]) -> float:
    return _ts(it["time"].get("received") or it["time"].get("at")) or 0.0


def collapse_threads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One member per mail thread (#1497: a project page showed a thread's "Re: …" mails as identical rows). Mails
    sharing a thread key (their thread's KB entry id, from the stores' own thread ids, never subjects) become one:
    the newest mail's id and title, the thread's latest date, `count` mails, Open on the thread, every project any of
    them is filed under, and the narrowest audience among them. Only the viewer's projected items reach here, so the
    count and date say nothing about mail they can't see. Everything else passes through."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    out: list[dict[str, Any]] = []
    for it in items:
        if it["kind"] == "mail" and it.get("thread"):
            groups.setdefault((str(it["owner"]).lower(), it["thread"]), []).append(it)
        else:
            out.append(it)
    for (_owner, key), its in groups.items():
        newest = max(its, key=lambda x: (_mail_when(x), x["id"]))
        m = {**newest, "routes": dict(newest["routes"]), "ops": list(newest["ops"])}
        mails = [x for x in its if x["id"] not in (key, f"mail:{key}")]
        m["count"] = max(1, len(mails))
        named = [x["title"] for x in sorted(its, key=lambda x: -_mail_when(x)) if x["title"] != "(no subject)"]
        m["title"] = named[0] if named else newest["title"]
        m["projects"] = sorted({p for x in its for p in x["projects"]})
        m["audience"] = narrowest([x.get("audience") for x in its])
        m["thread_items"] = sorted(x["id"] for x in its)
        m["routes"]["open"] = ("GET", f"/v1/kb/{key}")
        if "open" not in m["ops"]:
            m["ops"].insert(0, "open")
        m.pop("params", None)
        out.append(m)
    return out


def audience_slot(it: dict[str, Any], viewer: str | None, role: str) -> str:
    """#1142: the item's own audience as this viewer sees it: private (only you), adults, household or named (some
    people, never who). Only the item's own; a shared item never says a narrower one exists elsewhere."""
    aud, owner = str(it.get("audience") or "private"), str(it.get("owner") or "").lower()
    me = str(viewer or "").lower()
    if aud == "household":
        return "household"
    if role == "household":   # a shared screen (T7, 10-07): the item's real audience, never "household" for one
        return "adults" if aud == "adults" else "named"   # narrower; no one is "you" there, so never "private"
    if aud == "adults":
        return "adults"
    if aud == "private":
        return "private"
    if aud == "admins":   # the admin's alone when it's theirs or no one's; with someone else's, two people
        return "private" if role == "admin" and owner in ("", me) else "named"
    try:
        ids = {str(x).lower() for x in json.loads(aud)}
    except ValueError:
        return "private"
    return "private" if me and ids <= {me} and owner in ("", me) else "named"


# ── views ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _when(it: dict[str, Any]) -> float | None:
    t = it.get("time") or {}
    return _ts(t.get("at") or t.get("due"))


def _midnight(d: date, tz: Any) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()


def _span(it: dict[str, Any], tz: Any) -> tuple[float, float | None, bool] | None:
    """(start, exclusive end or None for a moment, all-day) in the household's time (#1161). An all-day or date-only
    item is read as its local dates, never through UTC (a date-only "2026-10-12" is the 12th in Los Angeles too); its
    end, when given, is exclusive (a calendar's all-day end), and a missing or not-later one means that one day."""
    t = it.get("time") or {}
    at = t.get("at") or t.get("due")
    if not at:
        return None
    if t.get("all_day") or len(str(at)) == 10:
        try:
            sd = date.fromisoformat(str(at)[:10])
            ed = date.fromisoformat(str(t["end"])[:10]) if t.get("end") else sd + timedelta(days=1)
        except ValueError:
            return None
        if ed <= sd:
            ed = sd + timedelta(days=1)
        return _midnight(sd, tz), _midnight(ed, tz), True
    st, en = _ts(at), _ts(t.get("end"))
    if st is None:
        return None
    return st, (en if en is not None and en > st else None), False


def _day(ts: float, tz: timezone) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def _member(it: dict[str, Any], rule: str, inputs: list[str], kind: str, priority: int) -> dict[str, Any]:
    st = it["state"]["is"]
    tone = {"waiting": "attention", "moving": "info", "done": "positive"}.get(st)
    slots: dict[str, Any] = {"title": it["title"], "actions": [f"op-{v}-{it['id']}" for v in it["ops"]]}
    if it["time"].get("at") or it["time"].get("due"):
        slots["meta"] = it["time"].get("at") or it["time"].get("due")     # a time; the app formats it
        if it["time"].get("all_day") or len(str(slots["meta"])) == 10:
            # #1155: a day, not a time (a bill's due date, an all-day or date-only event): the date as stored, never
            # an hour made from it (17:00Z read 10:00 in Los Angeles)
            slots["all_day"], slots["meta"] = True, str(slots["meta"])[:10]
    if tone:
        slots["status"] = {"text": st, "tone": tone}                     # a state key; the app owns the words
    if it["state"].get("of"):
        slots["subtitle"] = f"{it['state']['step']}/{it['state']['of']}"
    if it["kind"] == "bill":
        if it.get("amount") is not None:
            slots["trailing"] = {"value": f"${it['amount']:,.2f}"}
        slots["status"] = {"text": BILL_WORDS.get(str(it.get("bill_state")), "Bill"), "tone": tone or "neutral"}
        sub = [it["facet"].capitalize()] if it.get("facet") else []
        if it.get("earlier"):
            sub.append(f"+{it['earlier']} earlier")
        if sub:
            slots["subtitle"] = " · ".join(sub)
    if it["kind"] == "mail" and it["time"].get("received"):
        slots["meta"] = it["time"]["received"]
    if it["kind"] == "mail" and it.get("count", 1) > 1:
        slots["count"] = it["count"]                                    # a thread's mails, shown as one (#1497)
    return {"subject": {"kind": it["kind"], "id": it["id"]}, "basis": [{"rule": rule, "inputs": inputs, "kind": kind}],
            "priority": priority, "slots": slots, "state": "eligible"}


def _ops(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for it in items:
        for v in it["ops"]:
            route = it["routes"].get(v)
            out.append({"id": f"op-{v}-{it['id']}", "verb": v, "subject": {"kind": it["kind"], "id": it["id"]},
                        "exposure": "available",
                        "contract": f"jellybean.{it['kind']}.{v}@1",
                        **({"route": {"method": route[0], "path": route[1]}} if route else {}),
                        **({"params": it["params"][v]} if (it.get("params") or {}).get(v) else {}),
                        "verification": "none",
                        "reversibility": "reversible" if v in ("open", "file", "unfile", "done", "later") else "compensatable",
                        **({"recovery": ["undo"]} if v in ("file", "unfile") and it["kind"] != "chat" else {})})
    return out


def _day_text(d: str) -> str:
    """A day group's English heading, as the apps draw it: "Thu Oct 8"."""
    x = date.fromisoformat(d)
    return f"{x.strftime('%a %b')} {x.day}"


def _size(n: int) -> str:
    return "few" if n <= 5 else "dozens" if n < 100 else "hundreds"


def _brief(it: dict[str, Any]) -> dict[str, Any]:
    out = {"id": it["id"], "kind": it["kind"], "title": it["title"], "owner": it["owner"]}
    if it["time"].get("at") or it["time"].get("due"):
        out["at"] = it["time"].get("at") or it["time"].get("due")
    if it["state"].get("of"):
        out["step"], out["of"] = it["state"]["step"], it["state"]["of"]
    for k in ("next_at", "who"):
        if it["state"].get(k):
            out[k] = it["state"][k]
    return out


def rank(items: list[dict[str, Any]], viewer: str | None, now: float) -> dict[str, list[dict[str, Any]]]:
    """Ranked, per viewer, from items the viewer can see: what waits on you (yours, waiting or late), what's yours
    next by time, what waits on others (theirs, waiting, shared with you) and what titan moves on its own."""
    live = [it for it in items if it["state"]["is"] != "done"]
    mine = [it for it in live if viewer is not None and it["owner"] == viewer]

    def due_key(it):
        w = _when(it)
        return (w is None, w or 0, it["id"])
    on_you = sorted([it for it in mine if it["state"]["is"] == "waiting" or
                     (it["kind"] in ("todo", "scheduled", "bill") and (w := _when(it)) is not None and w < now)],
                    key=lambda it: (it["state"]["is"] != "waiting", *due_key(it)))
    yours_next = sorted([it for it in mine if it not in on_you and it["state"]["is"] == "open"
                         and it["kind"] in ("todo", "scheduled", "bill")], key=due_key)
    on_others = sorted([it for it in live if it["state"]["is"] == "waiting" and it["owner"] != viewer
                        and it["owner"] not in ("", "household")], key=due_key)
    moving = sorted([it for it in live if it["state"]["is"] == "moving"], key=due_key)
    return {"waiting_on_you": [_brief(it) for it in on_you], "next": [_brief(it) for it in on_you + yours_next],
            "waiting_on_others": [_brief(it) for it in on_others], "moving": [_brief(it) for it in moving]}


def spoken_when(at: str | None, now: float, tz: timezone, all_day: bool = False) -> str:
    """How people say a time, in the household's zone: "today 17:00", "tomorrow 9:00", "Saturday 9:00" within a
    week, "Oct 18" beyond; an all-day item just its day."""
    ts = _ts(at)
    if ts is None:
        return ""
    d, today_ = datetime.fromtimestamp(ts, tz), datetime.fromtimestamp(now, tz).date()
    days = (d.date() - today_).days
    clock = "" if all_day else f" {d.hour}:{d.minute:02d}"
    if days == 0:
        return "today" + clock
    if days == 1:
        return "tomorrow" + clock
    if 1 < days < 7:
        return d.strftime("%A") + clock
    return d.strftime("%b ") + str(d.day)


def _part_of_day(now: float, tz: timezone) -> str:
    h = datetime.fromtimestamp(now, tz).hour
    return "morning" if 5 <= h < 12 else "afternoon" if h < 18 else "evening"


def now_sentence(r: dict[str, list], call_me: str | None, now: float, tz: timezone) -> dict[str, Any]:
    """#1086: "Good afternoon, Alex. 3 things wait on you; titan is moving 3 along on its own." The facts are counted
    in code from the viewer's own projected lists; the text is the template's (a model may smooth it later, never
    change a fact)."""
    facts = {"part_of_day": _part_of_day(now, tz), "name": call_me, "waiting_on_you": len(r["waiting_on_you"]),
             "moving": len(r["moving"])}
    n, m = facts["waiting_on_you"], facts["moving"]
    text = f"Good {facts['part_of_day']}" + (f", {call_me}" if call_me else "") + ". "
    text += (f"{n} thing{'s' if n != 1 else ''} wait{'s' if n == 1 else ''} on you" if n else "Nothing waits on you")
    text += f"; titan is moving {m} along on its own." if m else "."
    return {"key": "items.now.sentence", "facts": facts, "text": text, "source": "template"}


def project_sentence(name: str, its: list[dict[str, Any]], r: dict[str, list], names: dict[str, str],
                     now: float, tz: timezone = timezone.utc) -> dict[str, Any]:
    """#1086: a project's live sentence from its visible items only: what's open, the next thing coming, what waits
    on you and on whom else (a name only for an item shared with you)."""
    live = [it for it in its if it["state"]["is"] != "done"]
    timed = sorted((it for it in live if (w := _when(it)) is not None and w >= now), key=lambda it: (_when(it), it["id"]))
    nxt = timed[0] if timed else None
    facts = {"project": name, "open": len(live), "next": _brief(nxt) if nxt else None,
             "waiting_on_you": len(r["waiting_on_you"]),
             "waiting_on": sorted({names.get(x["owner"], "") for x in r["waiting_on_others"]} - {""})}
    text = f"{name}: {len(live)} open"
    if nxt:
        text += f", next {nxt['title']} {spoken_when(facts['next']['at'], now, tz, bool(nxt['time'].get('all_day')))}"
    text += "."
    if facts["waiting_on_you"]:
        text += f" {facts['waiting_on_you']} wait{'s' if facts['waiting_on_you'] == 1 else ''} on you."
    if facts["waiting_on"]:
        text += f" Waiting on {', '.join(facts['waiting_on'])}."
    return {"key": "items.project.sentence", "facts": facts, "text": text, "source": "template"}


def resolve(view: str, viewer: str | None, role: str, readers: dict[str, Reader], *, now: float,
            tz: timezone = timezone.utc, start: str | None = None, end: str | None = None,
            project_ok: Callable[[str], bool] | None = None, people: set[str] | None = None,
            call_me: str | None = None, names: dict[str, str] | None = None,
            project_name: Callable[[str], str] | None = None,
            related: Callable[[str], list[dict[str, Any]]] | None = None,
            suggest: Callable[[list[dict[str, Any]]], dict[str, dict[str, Any]]] | None = None,
            auto_filed: list[dict[str, Any]] | None = None,
            project_people: Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
            name_source: Callable[[str], str] | None = None,
            snoozed: set[str] | None = None) -> dict[str, Any]:
    """The ResolvedSurface for one view. `readers` are the stores' own per-viewer readers, already bound to this
    viewer (access projection first): this function never sees an item the viewer can't. `suggest` (#1087,
    file_suggest.for_items bound to the viewer) gets the projected items and returns at most one project per unfiled
    item: its reason becomes the member's subtitle, and the item's file operation carries the project. `auto_filed`
    (file_suggest.recent_auto): what titan filed for this viewer lately, shown in Now as "Filed in … · Undo".
    `project_people(pid, items)` (#1142): who's on a project page, given only the project items this viewer sees;
    `name_source(pid)`: person, earned or description. `snoozed` (#1233): the item ids this viewer put off with Later
    and still hidden at `now` (proactive.snoozed_items); Now leaves them out for this viewer alone."""
    if role == "household":
        viewer = None
    items: list[dict[str, Any]] = []
    unknown: list[str] = []
    # each store's reader at once: a request costs its slowest reader, not their sum (10-06: now p50 3.7 s)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(len(readers), 12))) as pool:
        futures = {name: pool.submit(readers[name]) for name in sorted(readers)}
    for name in sorted(futures):
        try:
            items += futures[name].result()
        except Exception:  # noqa: BLE001 - a source down is unknown, never "nothing there" (SSS §4)
            unknown.append(name)
    items = collapse_threads(items)          # #1497: a mail thread is one member, in every view
    items.sort(key=lambda it: it["id"])
    later_hidden = 0
    if view == "now" and viewer is not None and snoozed:
        # #1233: put off by this viewer until later: gone from their Now (members, counts, sentence), nobody else's
        kept = [it for it in items if it["id"] not in snoozed]
        later_hidden, items = len(items) - len(kept), kept
    for it in items:   # #1169: file / unfile where this viewer may, on every kind that files
        if it["kind"] not in FILE_KINDS:
            continue
        ops_ = [v for v in it["ops"] if v not in ("file", "unfile")]
        if may_file(it, viewer, role):
            ops_.append("file")
            if it["kind"] != "chat":
                it["routes"]["file"] = ("POST", f"/v1/items/{it['id']}/file")
            if project_ok and any(project_ok(p_) for p_ in it["projects"]):
                ops_.append("unfile")
                it["routes"]["unfile"] = ("POST", f"/v1/items/{it['id']}/unfile")
        it["ops"] = ops_
    if view == "now":   # #1233: Later on Now's to-dos, scheduled actions and tasks, per viewer; never a screen's
        for it in items:
            if may_later(it, viewer, role) and "later" not in it["ops"]:
                it["ops"].append("later")
                it["routes"]["later"] = ("POST", f"/v1/items/{it['id']}/later")
    groups: list[dict[str, Any]] = []
    project_ops: list[dict[str, Any]] = []
    extra: dict[str, Any] = {}
    lens: dict[str, Any] = {"view": view}
    today = _day(now, tz)

    if view == "now":
        r = rank(items, viewer, now)
        extra = {k: r[k] for k in ("waiting_on_you", "waiting_on_others", "moving")}
        extra["next"] = r["next"][:5]
        extra["sentence"] = now_sentence(r, call_me if viewer else None, now, tz)
        taken = {x["id"] for k in ("waiting_on_you", "waiting_on_others", "moving") for x in r[k]}
        by_id = {it["id"]: it for it in items}
        live = [it for it in items if it["state"]["is"] != "done" and it["id"] not in taken]
        todays = sorted((it for it in live if (w := _when(it)) is not None and w >= now and _day(w, tz) == today),
                        key=lambda it: (_when(it), it["id"]))
        undated = [it for it in live if it["kind"] == "todo" and _when(it) is None
                   and (viewer is None or it["owner"] in (viewer, "household"))]
        due_soon = sorted((it for it in live if it["kind"] == "bill" and it not in todays and (w := _when(it)) is not None
                           and now <= w < now + 7 * 86400), key=lambda it: (_when(it), it["id"]))
        new_mail = sorted((it for it in live if it["kind"] == "mail" and viewer is not None and it["owner"] == viewer
                           and (r_ := _ts(it["time"].get("received"))) is not None and now - r_ < 2 * 86400),
                          key=lambda it: -(_ts(it["time"].get("received")) or 0))[:5]
        homeless = sorted((it for it in live if it["kind"] in FILEABLE and not it["projects"] and viewer is not None
                           and it["owner"] == viewer and (w := _when(it)) is not None and now - w < 86400),
                          key=lambda it: -(_when(it) or 0))[:3]
        for gid, role_, key, its, rule, inputs, kind, prio in (
                ("g-waiting-on-you", "attention", "items.now.waiting_on_you", [by_id[x["id"]] for x in r["waiting_on_you"]],
                 "waits-on-you", ["owner", "state", "time"], "derived", 1),
                ("g-today", "primary", "items.now.today", todays, "today", ["time"], "declared", 1),
                ("g-open", "supporting", "items.now.open", undated, "open-todo", ["state"], "declared", 2),
                ("g-due-soon", "supporting", "items.now.due_soon", due_soon, "due-within-a-week", ["time"], "declared", 2),
                ("g-moving", "supporting", "items.now.moving", [by_id[x["id"]] for x in r["moving"]],
                 "moving-on-its-own", ["state"], "observed", 2),
                ("g-new-mail", "supporting", "items.now.new_mail", new_mail, "kept-mail", ["owner", "time"],
                 "declared", 3),
                ("g-waiting-on-others", "supporting", "items.now.waiting_on_others",
                 [by_id[x["id"]] for x in r["waiting_on_others"]], "waits-on-others", ["owner", "state"], "derived", 3),
                ("g-needs-a-home", "supporting", "items.now.needs_a_home", homeless, "not-filed", ["projects"],
                 "declared", 3)):
            if its or gid == "g-today":
                groups.append({"id": gid, "role": role_, "label": label(key),
                               "members": [_member(it, rule, inputs, kind, prio) for it in its], "ordering": "time"})
    elif view == "time":
        try:
            s = datetime.fromisoformat(start).replace(tzinfo=tz).timestamp() if start else \
                datetime.fromisoformat(today).replace(tzinfo=tz).timestamp()
            e = datetime.fromisoformat(end).replace(tzinfo=tz).timestamp() + 86400 if end else s + 7 * 86400
        except ValueError:
            raise Refused(400, "from and to are YYYY-MM-DD")
        if e <= s or e - s > 62 * 86400:
            raise Refused(400, "to must be after from, at most 62 days later")
        lens.update({"from": _day(s, tz), "to": _day(e - 1, tz)})
        # #1161: a member in every day it overlaps inside [from, to], one that began before `from` too. A day d holds
        # it when start < d's end and end > d's start (household time); a moment (no end) is in its own day only.
        days: dict[str, list[tuple[tuple, dict[str, Any]]]] = {}
        for it in items:
            sp = _span(it, tz)
            if sp is None:
                continue
            st, en, all_day = sp
            if en is None:
                if s <= st < e:
                    days.setdefault(_day(st, tz), []).append(((1, st, it["id"]), _member(it, "in-range", ["time"],
                                                                                       "declared", 1)))
                continue
            first = datetime.fromtimestamp(st, tz).date()
            last = datetime.fromtimestamp(en - 1, tz).date()         # the end is exclusive
            n = (last - first).days + 1
            # #1163: only the days inside both the span and [from, to]; a span's length never costs a request
            lo = max(first, datetime.fromtimestamp(s, tz).date())
            hi = min(last, datetime.fromtimestamp(e - 1, tz).date())
            for k in range((hi - lo).days + 1):
                d_ = lo + timedelta(days=k)
                i = (d_ - first).days + 1
                m = _member(it, "in-range", ["time"], "declared", 1)
                if n > 1:
                    end_iso = str(it["time"]["end"])
                    m["slots"]["end"] = end_iso[:10] if all_day else end_iso
                    m["slots"]["span"] = {"day": i, "of": n}
                    if i > 1:   # a later day of a span: all day for that day, no clock time drawn
                        m["slots"]["all_day"], m["slots"]["meta"] = True, d_.isoformat()
                days.setdefault(d_.isoformat(), []).append(((0 if i > 1 else 1, st, it["id"]), m))
        for d in sorted(days):
            groups.append({"id": f"g-day-{d}", "role": "primary" if d == today else "supporting",
                           "label": label("items.time.day", "Today" if d == today else _day_text(d), date=d),
                           "members": [m for _, m in sorted(days[d], key=lambda x: x[0])], "ordering": "time"})
    elif view.startswith("project:"):
        pid = view.split(":", 1)[1]
        if not project_ok or not project_ok(pid):
            raise Refused(404, "no such project")              # one the viewer can't see doesn't exist to them
        its = [it for it in items if pid in it["projects"]]
        r = rank(its, viewer, now)
        extra = {"related": [{k: v for k, v in x.items() if k != "items"} for x in (related(pid) if related else [])],
                 "people": project_people(pid, its) if project_people else [],
                 "name_source": name_source(pid) if name_source else "person",
                 "next": r["next"][0] if r["next"] else None,
                 "sentence": project_sentence(project_name(pid) if project_name else pid, its, r, names or {}, now, tz)}
        # #1173: make things here. Add a to-do in the project, or Ask "kept with this project": the turn files its
        # chat and whatever it makes (to-dos, plans, scheduled actions) under the project.
        if viewer is not None:
            for oid, verb, contract, method, path in (
                    (f"op-add-todo-{pid}", "add", "jellybean.todo.add@1", "POST", "/v1/todos"),
                    (f"op-ask-{pid}", "ask", "jellybean.project.ask@1", "POST", "/v1/prompt/stream")):
                project_ops.append({"id": oid, "verb": verb, "subject": {"kind": "project", "id": pid},
                                    "exposure": "available", "contract": contract, "route": {"method": method, "path": path},
                                    "params": {"project": pid}, "verification": "none",
                                    "reversibility": "reversible" if verb == "add" else "compensatable",
                                    **({"recovery": ["undo"]} if verb == "add" else {})})
        # #1180: items the link graph ties to this project's members, suggested, never filed (its own group)
        linked_here = sorted((it for it in items if (it.get("suggested") or {}).get("project") == pid
                              and not it["projects"]), key=lambda it: it["id"])
        if linked_here:
            mem = []
            for it in linked_here:
                m = _member(it, "linked-to-project", ["links"], "derived", 3)
                m["slots"]["subtitle"] = it["suggested"]["reason"]
                m["suggestion"] = {k: it["suggested"][k] for k in ("project", "state", "auto")}
                mem.append(m)
            groups.append({"id": "g-linked", "role": "relationship", "label": label("items.project.linked"),
                           "members": mem, "ordering": "strongest"})
        groups.append({"id": "g-project", "role": "primary", "label": label("items.project"),
                       "members": [_member(it, "filed-under", ["projects"], "declared", 1) for it in its],
                       "ordering": "id", **({"operations": [o["id"] for o in project_ops]} if project_ops else {})})
    elif view.startswith("person:"):
        who = view.split(":", 1)[1]
        # a shared screen has no person pages (Coordination, 10-09: the TV shows Now and Time only): the same 404 as a
        # missing person, so it says nothing about who's in the house
        if role == "household" or people is None or who not in people:
            raise Refused(404, "no such person")
        its = [it for it in items if it["owner"] == who and it["state"]["is"] != "done"]
        groups.append({"id": "g-person", "role": "primary", "label": label("items.person"),
                       "members": [_member(it, "theirs", ["owner"], "declared", 1) for it in its],
                       "ordering": "id"})
    elif view == "unsorted":
        # chats without a home, and (#1175) the viewer's own items that already carry a File-it suggestion
        its = [it for it in items if (it["kind"] in FILEABLE or it.get("suggested")) and not it["projects"]
               and it["state"]["is"] != "done" and (viewer is None or it["owner"] == viewer)]
        groups.append({"id": "g-unsorted", "role": "primary", "label": label("items.unsorted"),
                       "members": [_member(it, "not-filed", ["projects"], "declared", 2)
                                   for it in sorted(its, key=lambda it: -(_when(it) or 0))],
                       "ordering": "newest"})
    else:
        raise Refused(400, "view is now, time, unsorted, project:<id> or person:<id>")

    undo_ops: list[dict[str, Any]] = []
    if view == "now" and auto_filed and viewer is not None:
        by_id_ = {it["id"]: it for it in items}
        mem = []
        for a in sorted(auto_filed, key=lambda a: -a["at"]):
            it = by_id_.get(a["item"])
            pname = project_name(a["project"]) if project_name else ""
            if not it or not pname or a["project"] not in it["projects"]:
                continue        # only what this viewer sees, filed where they can see
            m = _member(it, "filed-by-titan", ["links"], "derived", 3)
            m["slots"]["subtitle"] = f"Filed in {pname}"
            m["slots"]["actions"] = [x for x in m["slots"]["actions"] if not x.startswith("op-file-")] + [f"op-undo-{it['id']}"]
            mem.append(m)
            undo_ops.append({"id": f"op-undo-{it['id']}", "verb": "undo", "subject": {"kind": it["kind"], "id": it["id"]},
                             "exposure": "available", "contract": "jellybean.project.undo@1",
                             "route": {"method": "POST", "path": "/v1/projects/undo"}, "params": {"token": a["token"]},
                             "verification": "none", "reversibility": "reversible"})
        if mem:
            groups.append({"id": "g-filed-for-you", "role": "supporting", "label": label("items.now.filed_for_you"),
                           "members": mem, "ordering": "time"})
    suggested: dict[str, dict[str, Any]] = {}
    if view in ("now", "unsorted"):
        homes = {m["subject"]["id"] for g in groups if g["id"] in ("g-needs-a-home", "g-unsorted") for m in g["members"]}
        if homes and suggest is not None:
            try:
                suggested = {k: v for k, v in suggest(items).items() if k in homes}
            except Exception:  # noqa: BLE001 - no suggestion is never wrong; the items still show
                suggested = {}
        # suggestions a reader already made (#1175: linker items, on the viewer's projected graph)
        suggested.update({it["id"]: it["suggested"] for it in items if it.get("suggested") and it["id"] in homes})
        for g in groups:
            for m in g["members"]:
                sg = suggested.get(m["subject"]["id"])
                if sg and g["id"] in ("g-needs-a-home", "g-unsorted"):
                    m["slots"].setdefault("subtitle", sg["reason"])
                    m["basis"].append({"rule": "suggested-project", "inputs": ["links"], "kind": "derived"})
                    m["suggestion"] = {"project": sg["project"], "state": sg["state"], "auto": sg["auto"]}
        # Zach, 10-10: "Not everything is a project … sometimes just questions, requests, etc happen." A chat with no
        # home is finished as it is, and lives in History (threads@1), not in a filing backlog: Unsorted and Now's
        # needs-a-home keep only what has a real suggestion of where it belongs.
        for g in groups:
            if g["id"] in ("g-needs-a-home", "g-unsorted"):
                g["members"] = [m for m in g["members"] if m["subject"]["id"] in suggested]
        # The group stays even when empty: shipped apps (0.13.62) read items.now.needs_a_home (shipped_keys.json). Dropping
        # it needs a release that no longer reads it, and its own ticket.
    if unknown:
        groups.append({"id": "g-unknown", "role": "unknown", "label": label("items.unknown"), "members": [],
                       "ordering": "none", "sources": unknown})
    by_id_all = {it["id"]: it for it in items}
    seen_cache: dict[str, bool] = {}

    def _seen(p_: str) -> bool:
        if p_ not in seen_cache:
            seen_cache[p_] = bool(project_ok and project_ok(p_))
        return seen_cache[p_]
    for g in groups:
        for m in g["members"]:
            it_ = by_id_all.get(m["subject"]["id"])
            if it_ is None:
                continue
            m["slots"]["audience"] = audience_slot(it_, viewer, role)
            # #1149: the projects it's filed under that this viewer may see (ids only; the app names them from
            # GET /v1/projects). One they can't see is left out, so neither its id nor that it exists shows.
            m["slots"]["projects"] = [p_ for p_ in it_.get("projects") or [] if _seen(p_)]
            if it_.get("steps"):
                m["steps"] = it_["steps"]
    shown_ids = {m["subject"]["id"] for g in groups for m in g["members"]}
    shown = [it for it in items if it["id"] in shown_ids]
    n = len(shown_ids)
    deps = sorted((it["id"], it["rev"]) for it in shown)
    gran = 300 if view == "now" else 3600
    ops = _ops(shown) + undo_ops + project_ops
    for it in items:   # #1180: a linked-to-project member's File carries its project
        if (it.get("suggested") or {}).get("project") and it["id"] not in suggested and it["id"] in shown_ids:
            suggested[it["id"]] = it["suggested"]
    for op in ops:
        sg = suggested.get(op["subject"]["id"]) if op["verb"] == "file" else None
        if sg:
            op["params"] = {"project": sg["project"]}
    fp_in = {"deps": deps, "unknown": unknown, "lens": lens,
             "suggested": sorted((k, v["project"], v["state"]) for k, v in suggested.items()),
             "undo": sorted(o["id"] for o in undo_ops), "viewer": viewer, "role": role, "now": int(now // gran) * gran,
             "people": sorted((str(x.get("id")), str(x.get("role"))) for x in extra.get("people") or []) if view.startswith("project:") else [],
             "name_source": extra.get("name_source"),
             "resolver": RESOLVER}
    return {"protocol": "sss", "protocolVersion": "0.1.0", "profileVersion": "jb-1",
            "resolutionId": "rs-" + hashlib.sha256(json.dumps(fp_in, sort_keys=True).encode()).hexdigest()[:16],
            "profile": "temporal" if view in ("now", "time") else "collection",
            "purpose": "monitor" if view == "now" else "consume",
            **({"subject": {"kind": view.split(":")[0], "id": view.split(":", 1)[1]}} if ":" in view else {}),
            "accessProjection": {"viewer": viewer, "role": role, "rule": "per-store readers (audience.py, todos, "
                                                                          "kb_privacy, spaces)"},
            "groups": groups, "operations": ops, **extra,
            "states": {"withheld": 0, "unknown": len(unknown)}, "sizeClass": _size(n),
            "provenance": {"resolver": RESOLVER, "inputs": sorted(readers) + ["now", "viewer"],
                           "generated_at": _iso(now)},
            "explanation": {"included": [{"group": g["id"], "rule": (g["members"][0]["basis"][0]["rule"]
                                                                     if g["members"] else None), "count": len(g["members"])}
                                         for g in groups],
                            "excluded": [{"rule": "later", "count": later_hidden}] if later_hidden else [],
                            "ordering": [g["ordering"] for g in groups]},
            "dependencies": [{"subject": {"id": i}, "revision": r} for i, r in deps],
            "fingerprint": "sha256:" + hashlib.sha256(json.dumps(fp_in, sort_keys=True).encode()).hexdigest()}
