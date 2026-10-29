#!/usr/bin/env python3
"""The thread view (titan-node#951 (g), rule 8: one substrate, many projections).

build() is the one place a viewer's threads are put together: the threads tool's text (render_text), the apps' JSON
(GET /v1/threads), and later cards and the TV all read it, so they can't disagree. Per viewer: audience and fences,
and a hidden item never bridges two visible ones (linker.threads_for). Never an identifier value: items carry their
id, kind, date and the title the viewer may see; edges carry their state, why, typed relations and measured amounts.

  build(pid, role, about="", hold="", all_=False, today=None) → dict (docs/threads-api.md)
  render_text(view) → the threads tool's reply
  relate(pid, role, a, b, related) → the person's correction (it outranks the linker)
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any

SHOWN_THREADS = 5        # in the text; the JSON carries up to MAX_THREADS
MAX_THREADS = 20
_STOP_WORDS = ("my", "the", "with", "about", "going", "on")


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _visible_fn(pid: str | None, role: str | None):
    import audience as _aud
    import fences
    fs = fences.get()
    return lambda it: (_aud.visible(it.audience, pid, role, owner=it.owner)
                       and fs.admits(pid, role, it.owner, item=it.item_id))


def thread_id(item_ids: list[str]) -> str:
    """Stable for the same members, whatever order they come in."""
    return "th-" + hashlib.sha1("\0".join(sorted(item_ids)).encode()).hexdigest()[:12]


def build(pid: str | None, role: str | None, about: str = "", hold: str = "", all_: bool = False,
          today: date | None = None) -> dict[str, Any]:
    import facets
    import hooks
    import linker
    if role in ("kid", "household"):
        return {"status": "not_shown", "message": "Threads aren't shown here."}
    axis = str(hold or "").strip().lower()
    if axis and axis not in facets.AXES:
        return {"status": "bad_hold", "message": f"hold is one of: {', '.join(facets.AXES)}."}
    import local_time
    today = today or local_time.today()
    items, edges = linker.scored(hooks.connect())   # cached until links.db changes
    if not items:
        return {"status": "none", "message": "Nothing is linked yet."}
    visible = _visible_fn(pid, role)
    titles, vendors, names = linker.labels(items)
    seen = {i for i, it in items.items() if visible(it)}

    def item(i: str) -> dict[str, Any]:
        it = items[i]
        return {"id": i, "kind": it.source, "date": it.at.isoformat() if it.at else None, "title": titles.get(i) or None}

    def edge(e: Any) -> dict[str, Any]:
        return {"a": e.a, "b": e.b, "state": e.state, "why": e.why, "typed": list(e.typed), "measured": dict(e.measured)}

    if all_:
        order = sorted(seen, key=lambda i: (items[i].at or date.min), reverse=True)
        return {"status": "ok", "filters": [], "threads": [], "unlinked": [], "held": None,
                "unsorted": [item(i) for i in order]}
    q = str(about or "").strip().lower()
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9'-]+", q) if w not in _STOP_WORDS]
    ents: set[str] = set()
    try:
        import world
        ents = set(world.get().find(q, pid, role) or [])
    except Exception:  # noqa: BLE001
        pass
    joined = "".join(words)

    def seeds_for(i: str) -> bool:
        it = items[i]
        t = (titles.get(i) or "").lower()
        if words and all(w in t for w in words):
            return True
        if joined and any(joined in d.replace(".", "") or d.split(".")[0] in joined for d in it.keys.get("party:domain", ())):
            return True
        if words and any(all(w in v for w in words) for v in it.keys.get("party:vendor", ())):
            return True
        return bool(ents & it.keys.get("party:entity", set()))
    # rule 9: the question's facet chips; without any, the title, vendor and entity match above
    domains = {d for i in seen for d in items[i].keys.get("party:domain", ())}
    cs = facets.chips(q, today, vendors, domains, ents, names)
    seeds = facets.narrow(items, seen, cs) if cs else {i for i in seen if seeds_for(i)}
    filters = [{"axis": c.axis, "label": c.label} for c in cs]
    if not seeds:
        return {"status": "none", "filters": filters, "message": f"Nothing linked about {about or 'that'} that you can see."}
    all_ths = linker.threads_for(items, edges, visible, titles, names, vendors)
    ths = [t for t in all_ths if seeds & set(t.items)][:MAX_THREADS]
    alone = seeds - {i for t in ths for i in t.items}
    out_threads = []
    for t in ths:
        fringe, shown = [], set()
        for e in t.fringe:
            other = e.b if e.a in t.items else e.a
            if other in t.items or other in shown or other not in seen:
                continue
            shown.add(other)
            fringe.append({"item": item(other), "edge": edge(e)})
        out_threads.append({"id": thread_id(t.items), "name": t.name, "description": t.description, "cohesion": t.cohesion,
                            "items": [item(i) for i in t.items], "edges": [edge(e) for e in t.edges], "fringe": fringe})
    held = None
    if axis:
        label, others = facets.hold(axis, seeds | {i for t in ths for i in t.items}, items, seen)
        groups: dict[str | None, dict[str, Any]] = {}
        for i in sorted(others, key=lambda i: (items[i].at or date.min), reverse=True):
            t = next((t for t in all_ths if i in t.items), None)
            key = thread_id(t.items) if t else None
            g = groups.setdefault(key, {"thread_id": key, "head": (t.name or t.description) if t else None, "items": []})
            g["items"].append(item(i))
        held = {"axis": axis, "label": label, "count": len(others), "groups": list(groups.values())}
    return {"status": "ok", "filters": filters, "threads": out_threads, "unlinked": [item(i) for i in sorted(alone)],
            "held": held, "unsorted": None}


def _label(it: dict[str, Any]) -> str:
    day = date.fromisoformat(it["date"]).strftime("%b %-d") if it.get("date") else "undated"
    return f"{day} · {it['kind']} · {it.get('title') or '(no title)'}"


def render_text(view: dict[str, Any]) -> str:
    """The threads tool's reply, from the view: the same words the tool has always said."""
    if view.get("status") != "ok":
        return view.get("message") or "Nothing linked."
    if view.get("unsorted") is not None:
        us = view["unsorted"]
        return f"Everything, unsorted ({len(us)}):\n" + "\n".join(f"- {_label(i)}" for i in us[:40])
    out = []
    if view.get("filters"):
        out.append("Filters: " + " · ".join(f"{f['axis']} = {f['label']}" for f in view["filters"]))
    for t in view["threads"][:SHOWN_THREADS]:
        out.append(f"Thread: {t['name'] or t['description']}" + ("" if t["name"] else " (not named yet)"))
        out += [f"- {_label(i)}" for i in t["items"]]
        out += [f"  linked ({e['state']}): {e['why']}" for e in t["edges"][:6]]
        out += [f"  possibly related (candidate, not confirmed): {_label(f['item'])} — {f['edge']['why']}"
                for f in t["fringe"][:3]]
    if view.get("unlinked"):
        out.append("Not linked to anything yet:")
        out += [f"- {_label(i)}" for i in view["unlinked"][:8]]
    h = view.get("held")
    if h:
        out.append(f"Holding {h['label']} ({h['axis']}); {h['count']} other item{'s' if h['count'] != 1 else ''}:")
        for g in h["groups"][:8]:
            out.append(f"  {g['head'] or 'not linked'}:")
            out += [f"  - {_label(i)}" for i in g["items"][:6]]
    return "\n".join(out)


def relate(pid: str | None, role: str | None, a: str, b: str, related: bool, source: str = "correction") -> dict[str, Any]:
    """The person says two items are (or aren't) related: a statement that outranks the linker (rule 5). Only for
    items they may see; never for kids or a shared screen. Two conversations ("chat-…", #1016): only the asker's own.
    source: "baseline" (a labelling pass) or "correction" (the default)."""
    import chat_links
    import hooks
    import linker
    if role in ("kid", "household"):
        raise Refused(403, "threads aren't shown here")
    a, b = str(a or "").strip(), str(b or "").strip()
    if not a or not b or a == b:
        raise Refused(400, "two different items are required")
    source = str(source or "correction").strip().lower()
    if source not in linker.CLIENT_SOURCES:
        raise Refused(400, "source is baseline or correction")
    con = hooks.connect()
    if a.startswith(chat_links.PREFIX) or b.startswith(chat_links.PREFIX):
        mine = {r[0] for r in con.execute("SELECT DISTINCT item_id FROM hooks WHERE source=? AND owner=? AND item_id IN (?,?)",
                                          (chat_links.SOURCE, str(pid or ""), a, b))}
        if mine != {a, b}:   # someone else's conversation, a conversation and a mail, or no such item: all the same
            raise Refused(404, "no such items")
        linker.say(con, a, b, bool(related), str(pid or ""), source)
        return {"a": a, "b": b, "related": bool(related), "state": "verified" if related else "rejected"}
    items = linker.load(con)
    visible = _visible_fn(pid, role)
    if a not in items or b not in items or not (visible(items[a]) and visible(items[b])):
        raise Refused(404, "no such items")
    linker.say(con, a, b, bool(related), str(pid or ""), source)
    st = {(e.a, e.b): e.state for e in linker.score(items, linker.statements(con))}.get(linker._pair(a, b))
    return {"a": a, "b": b, "related": bool(related), "state": st}
