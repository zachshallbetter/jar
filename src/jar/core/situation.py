#!/usr/bin/env python3
"""situation(query, viewer, role): who / what / where / when / why from the relational graph, in code (#1399 P3; spec
docs/relational-fe-graph.md §8). No model call: the model only narrates the bundle later.

  1. Anchor   the query's people, orgs and places by name and alias over the viewer's visible nodes ("I", "me", "my"
              are the viewer), its time by the household zone ("Saturday morning", "tomorrow", "this week", "the 18th",
              "right now"), and its content words.
  2. Expand   breadth-first from the anchors over the viewer's visible edges (typed and accepted measured; candidates
              never), depth 3, a 60-node budget.
  3. Read     every situation node reached (an event, an obligation, a scenario, an item with dates) scored by its
              relevance to the anchors, then fe_reading's freshness and imminence; top_k picks.
  4. Fill     the five slots from the winner's edges, each value with the chain it stands on and the weakest rung.
     "Who did we come with?", "who's here with us?" with no place named anchor to the viewer's own latest arrival
              (HERE_HOURS, not since left): the household people on that place's items and plans that day, the asker left
              out (#1396).
  5. Gaps     a slot with no supported answer is "unknown", never a guess; conflicting values are "contested" with
              both chains.

The graph is read only through the contract (visible_nodes / visible_edges), passed in as `graph`: graph_sync's
module (P2, #1425). The bundle names nodes by their native ids (p_alex, pl-cedar,
household:harbor-mobile, cx-…), never anything the viewer can't see: every value comes from their visible subgraph.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import fe_reading as fe

RUNGS = ("observed", "attributed", "explained", "predicted")   # strongest first
DEPTH, BUDGET, TOP_K = 3, 60, 3
HERE_HOURS = 6                                  # "where you are": an arrival within this many hours, not since left
HOUSEHOLD_ROLES = ("admin", "adult", "kid")
_HERE_WITH = re.compile(r"\b(?:come|came|coming|arrived|here|going|went)\s+with\b|\bwho(?:'s| is| are)\s+(?:here|with (?:me|us))\b|\bwho are we with\b")
_STOP = set("""a an the is are was were be been am i me my mine we us our you your it its this that these those of to for
in on at by with from and or but do does did doing done what whats who whos where wheres when whens why how which
anything anyone any there here about up any so not now right go time exactly tell think""".split())
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def me_id(viewer: str, nodes: dict[str, Any]) -> str:
    """The viewer's own person node: graph_sync names people by their bare id (p_alex); the contract allows
    person:<pid> too. Either works here."""
    return viewer if viewer in nodes else f"person:{viewer}"


def _native(nid: str) -> str:
    kind, _, rest = nid.partition(":")
    return rest if kind in ("person", "org", "place", "item", "scenario") else nid


def _weakest(rungs) -> str | None:
    rs = [r for r in rungs if r in RUNGS]
    return max(rs, key=RUNGS.index) if rs else None


def _words(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9$][a-z0-9$.\-']*", (text or "").lower().replace("\u2019", "'"))
    out = []
    for w in raw:
        w = w.strip(".'")
        base = w[:-2] if w.endswith("'s") else w   # "what's", "who's", "ava's"
        if w and w not in _STOP and base not in _STOP:
            out.append(w)
    return out


def _stem(w: str) -> str:
    w = w.strip(".'?!,").removesuffix("'s")
    if w in ("pickup", "pickups"):
        return "pick"
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _local(iso: str | None, zone: ZoneInfo) -> datetime | None:
    if not iso or not isinstance(iso, str):
        return None   # a list or anything else isn't one time (graph_edges' attrs.when is a list: callers pick)
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return d.astimezone(zone) if d.tzinfo else d.replace(tzinfo=zone)


def summary_time(text: str, day: datetime) -> datetime | None:
    """The last clock time an item's summary states ("moved … from 3:00 to 4:00 pm"), on the event's day. Only ever
    makes a slot contested (an ask), never changes a time (KB, 10-10: no structured reschedule source yet)."""
    times = re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?=[\s.,;]|$)", text.lower())
    pm = "pm" in text.lower() or "p.m." in text.lower()
    hits = [(int(h), int(m or 0), ap) for h, m, ap in times if (m or ap) and 0 < int(h) <= 23]
    if not hits:
        return None
    h, m, ap = hits[-1]
    if (ap.startswith("p") or (not ap and pm)) and h < 12:
        h += 12
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def _hm(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


# ── 1. anchors ───────────────────────────────────────────────────────────────────────────────────

def _names(node: dict[str, Any]) -> list[str]:
    a = node.get("attrs") or {}
    out = [node.get("label") or "", a.get("name") or "", a.get("label") or ""] + list(a.get("aliases") or [])
    if node["kind"] == "person" and node.get("label"):
        out.append(node["label"].split()[0])   # a first name
    return [n for n in dict.fromkeys(x.strip() for x in out) if n]


def _mentions(q: str, name: str) -> bool:
    return bool(re.search(r"(?<![\w])" + re.escape(name.lower()) + r"(?:'s)?(?![\w])", q.lower()))


def anchors(q: str, viewer: str, nodes: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """{person, org, place}: node ids the query names. A longer name wins over a shorter one inside it."""
    out: dict[str, list[str]] = {"person": [], "org": [], "place": []}
    hits = []
    for nid, n in nodes.items():
        if n["kind"] not in out:
            continue
        for name in _names(n):
            if len(name) >= 2 and _mentions(q, name):
                hits.append((len(name), nid, n["kind"], name.lower()))
    hits.sort(reverse=True)
    taken: list[str] = []
    for _, nid, kind, name in hits:
        if any(name in t and name != t for t in taken):
            continue   # "Harbor" inside "Harbor Mobile Billing"
        if nid not in out[kind]:
            out[kind].append(nid)
        taken.append(name)
    ql = q.lower()
    me = me_id(viewer, nodes)
    if re.search(r"\b(i|me|my|mine|i'm|am i)\b", ql) and me in nodes:
        if me not in out["person"]:
            out["person"].append(me)
    # a place by its kind when the query names only the kind ("the school", "the clinic"), if it's the only one
    for word in re.findall(r"\b(?:the|my|our)\s+(school|clinic|park|home|studio|office|dentist)\b", ql):
        kind = {"dentist": "clinic"}.get(word, word)
        same = [nid for nid, n in nodes.items() if n["kind"] == "place" and (n.get("attrs") or {}).get("kind") == kind]
        if len(same) == 1 and same[0] not in out["place"]:
            out["place"].append(same[0])
    return out


_FAMILY = {"kid": "child", "kids": "child", "child": "child", "children": "child", "son": "child", "daughter": "child",
           "mom": "parent", "mum": "parent", "dad": "parent", "parents": "parent", "parent": "parent"}


def family(q: str, viewer: str, edges: list[dict[str, Any]], me: str | None = None) -> list[str]:
    """'my kids', 'my daughter', 'my parents', 'the kids': people related to the viewer by parent_of, from the viewer's
    visible edges. 'the kids' with no 'my' is every child the viewer can see."""
    out = []
    for m in re.finditer(r"\b(my|our|the)\s+(kids?|child(?:ren)?|son|daughter|mom|mum|dad|parents?)\b", q.lower()):
        rel = _FAMILY[m.group(2)]
        pe = [e for e in edges if e["type"] == "parent_of"]
        if rel == "child":
            mine = [e["b"] for e in pe if e["a"] == (me or f"person:{viewer}")]
            out += mine if m.group(1) in ("my", "our") and mine else [e["b"] for e in pe]
        else:
            out += [e["a"] for e in pe if e["b"] == (me or f"person:{viewer}")]
    return list(dict.fromkeys(out))


def time_window(q: str, now: datetime) -> tuple[datetime, datetime] | None:
    """The query's time, in the household zone: [start, end)."""
    s = q.lower()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    win = None
    if "right now" in s or re.search(r"\bnow\b", s):
        return (now - timedelta(hours=12), now + timedelta(minutes=1))
    if "this week" in s:
        return (day0, day0 + timedelta(days=7))
    if "next week" in s:
        nxt = day0 + timedelta(days=7 - now.weekday())
        return (nxt, nxt + timedelta(days=7))
    if "weekend" in s:
        sat = day0 + timedelta(days=(5 - now.weekday()) % 7)
        if "next weekend" in s:
            sat += timedelta(days=7)
        return (sat, sat + timedelta(days=2))
    months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
              "november", "december"]
    m = re.search(r"\b(" + "|".join(mo[:3] for mo in months) + r")[a-z]*\.?\s+(\d{1,2})\b", s) or \
        re.search(r"\b(\d{1,2})/(\d{1,2})\b", s)
    if m:
        try:
            mon = int(m.group(1)) if m.group(1).isdigit() else [mo[:3] for mo in months].index(m.group(1)) + 1
            t = day0.replace(month=mon, day=int(m.group(2)))
            return (t, t + timedelta(days=1))
        except (ValueError, IndexError):
            pass
    if "tomorrow" in s:
        win = (day0 + timedelta(days=1), day0 + timedelta(days=2))
    elif "today" in s or "tonight" in s:
        win = (day0, day0 + timedelta(days=1))
    m = re.search(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", s)
    if m:
        d = int(m.group(1))
        try:
            t = day0.replace(day=d)
        except ValueError:
            t = None
        if t is not None:
            if t < day0:
                t = (t + timedelta(days=32)).replace(day=d)
            win = (t, t + timedelta(days=1))
    for i, wd in enumerate(_WEEKDAYS):
        if re.search(rf"\b{wd}\b", s):
            ahead = (i - now.weekday()) % 7
            t = day0 + timedelta(days=ahead)
            win = (t, t + timedelta(days=1))
    if win:
        a, b = win
        if "morning" in s:
            return (a.replace(hour=5), a.replace(hour=12))
        if "afternoon" in s:
            return (a.replace(hour=12), a.replace(hour=17))
        if "evening" in s or "tonight" in s:
            return (a.replace(hour=17), b)
    return win


# ── the bundle ───────────────────────────────────────────────────────────────────────────────────

class Bundle:
    def __init__(self) -> None:
        self.slots: dict[str, Any] = {}
        self.chains: list[dict[str, Any]] = []
        self.rungs: list[str] = []
        self.slot_rungs: dict[str, list[str]] = {}
        self.via: list[str] = []

    def put(self, slot: str, values: list[str], chain: list[dict[str, Any]] | None = None) -> None:
        vals = [v for v in dict.fromkeys(values) if v]
        if not vals:
            return
        cur = self.slots.get(slot)
        if isinstance(cur, dict) and "is" in cur:
            cur["is"] = list(dict.fromkeys(cur["is"] + vals))
        else:
            self.slots[slot] = {"is": vals}
        for e in chain or []:
            self.cite(e)
            if e.get("rung"):
                self.slot_rungs.setdefault(slot, []).append(e["rung"])

    def rung(self, slot: str, r: str) -> None:
        self.slot_rungs.setdefault(slot, []).append(r)
        self.rungs.append(r)

    def contest(self, slot: str, options: list[tuple[str, dict[str, Any]]]) -> None:
        self.slots[slot] = {"contested": [{"value": v, "chain": self._line(e)} for v, e in options]}
        for _, e in options:
            self.cite(e)
            if e.get("rung"):
                self.slot_rungs.setdefault(slot, []).append(e["rung"])

    def cite(self, e: dict[str, Any]) -> None:
        self.chains.append(self._line(e))
        if e.get("rung"):
            self.rungs.append(e["rung"])
        for p in e.get("prov") or []:
            ref = p.get("ref")
            if ref and p.get("source") == "hooks" and ":" in ref:
                ref = ref.split(":", 1)[0]   # a hook row on its item: cite the item
            refs = ref.split("|") if ref and p.get("source") in ("links", "link_statements") else [ref]
            for r in refs:   # a link row names both of its items ("a|b")
                if r and r not in self.via and (p.get("source") not in ("people", "places", "world", "links")
                                                or "|" in (ref or "")):
                    self.via.append(r)

    def cite_item(self, iid: str, rung: str = "observed") -> None:
        if _native(iid) not in self.via:
            self.via.append(_native(iid))
        self.rungs.append(rung)

    @staticmethod
    def _line(e: dict[str, Any]) -> dict[str, Any]:
        return {"a": _native(e["a"]), "type": e["type"], "b": _native(e["b"]), "rung": e.get("rung"),
                "why": e.get("why"), "prov": [p.get("ref") for p in e.get("prov") or []]}

    def out(self, asked: set[str]) -> dict[str, Any]:
        """Every slot without a supported answer is "unknown". The rung is the weakest of the chains behind the
        slots the question asked ("why" asks for all of them)."""
        for s in ("who", "what", "where", "when"):
            self.slots.setdefault(s, "unknown")
        if "why" in asked:
            r = _weakest(self.rungs)
        else:
            r = _weakest([x for s in asked for x in self.slot_rungs.get(s, [])]) or _weakest(self.rungs)
        return {**self.slots, "why": self.chains, "via": self.via, "rung": r}


# ── 2–4. expand, read, fill ──────────────────────────────────────────────────────────────────────

def asked_slots(q: str) -> set[str]:
    s = q.lower()
    out = {w for w in ("who", "what", "where", "when", "why") if re.search(rf"\b{w}", s)}
    if re.search(r"\b(how|is|are|did|do|does|any)\b", s):
        out.add("what")
    if "what time" in s:
        out.add("when")
    if re.search(r"\b(we|us|our)\b", s):
        out.add("who")   # "what are we doing": who is part of the answer
    return out or {"what"}


class Situation:
    def __init__(self, graph: Any, viewer: str, role: str, zone: str, now: str) -> None:
        self.g, self.viewer, self.role = graph, viewer, role
        self.zone = ZoneInfo(zone)
        self.now = _local(now, self.zone)
        self.nodes = graph.visible_nodes(viewer, role)
        self.me = me_id(viewer, self.nodes)
        self.edges = graph.visible_edges(viewer, role)
        self.out_e: dict[str, list[dict]] = {}
        self.in_e: dict[str, list[dict]] = {}
        for e in self.edges:
            self.out_e.setdefault(e["a"], []).append(e)
            self.in_e.setdefault(e["b"], []).append(e)

    # helpers
    def node(self, nid: str) -> dict[str, Any] | None:
        return self.nodes.get(nid)

    def edges_of(self, nid: str, types=None, direction="any") -> list[dict]:
        es = (self.out_e.get(nid, []) if direction in ("any", "out") else []) + \
             (self.in_e.get(nid, []) if direction in ("any", "in") else [])
        return [e for e in es if not types or e["type"] in types]

    def text_of(self, nid: str) -> str:
        n = self.nodes.get(nid) or {}
        a = n.get("attrs") or {}
        parts = [n.get("label") or "", a.get("summary") or "", a.get("title") or "", a.get("goal") or "",
                 a.get("role") or "", a.get("location_text") or "", " ".join(self.g.decisions(n) if hasattr(self.g, "decisions") else [])]
        return " ".join(p for p in parts if p)

    def term_hits(self, nid: str, terms: list[str]) -> int:
        words = {_stem(w) for w in _words(self.text_of(nid))}
        return sum(1 for t in terms if _stem(t) in words)

    def expand(self, seeds: list[str]) -> dict[str, int]:
        """Breadth-first over visible edges: node → depth, typed edges before measured, within the budget."""
        seen = {s: 0 for s in seeds if s in self.nodes}
        frontier = list(seen)
        for d in range(1, DEPTH + 1):
            nxt = []
            for nid in frontier:
                es = sorted(self.edges_of(nid), key=lambda e: e["plane"] != "typed")
                for e in es:
                    other = e["b"] if e["a"] == nid else e["a"]
                    if other not in seen and other in self.nodes and len(seen) < BUDGET:
                        seen[other] = d
                        nxt.append(other)
            frontier = nxt
        return seen

    def place_of(self, ev: str) -> tuple[list[str], list[dict]]:
        es = self.edges_of(ev, ["at"], "out")
        return [_native(e["b"]) for e in es], es

    def start_of(self, nid: str) -> datetime | None:
        a = (self.nodes.get(nid) or {}).get("attrs") or {}
        return _local(a.get("start") or a.get("due"), self.zone)

    # the reading: relevance × fe_reading's time terms
    def score(self, cands: list[str], anc: dict[str, list[str]], terms: list[str], win, reach: dict[str, int]) -> list[str]:
        now_ms = self.now.timestamp() * 1000
        scores: dict[str, float] = {}
        anchor_ids = set(anc["person"] + anc["org"] + anc["place"])
        for c in cands:
            rel = 0.0
            touch = {(e["b"] if e["a"] == c else e["a"]) for e in self.edges_of(c)}
            about = [e["a"] for e in self.edges_of(c, ["about"], "in")]
            for a in about:
                touch |= {(e["b"] if e["a"] == a else e["a"]) for e in self.edges_of(a)}
            rel += 3.0 * len(anchor_ids & (touch | {c}))
            th = self.term_hits(c, terms) + sum(self.term_hits(a, terms) for a in about)
            rel += 1.0 * th
            if terms and not win and th == 0 and not (anchor_ids & {e["b"] for e in self.edges_of(c, ["at"], "out")}):
                continue   # content words named, none of them here (an allergy note isn't Ava's dentist)
            t = self.start_of(c)
            if win:
                if not t or not (win[0] <= t < win[1]):
                    continue
                rel += 2.0
            if anc["place"] and self.nodes[c]["kind"] == "event":
                if not set(anc["place"]) & {e["b"] for e in self.edges_of(c, ["at"], "out")}:
                    continue
            if rel <= 0:
                continue
            if c not in reach and anchor_ids:
                rel *= 0.5
            when = t.timestamp() * 1000 if t else now_ms
            timing = 0.5 + 0.5 * max(fe.imminence(when, now_ms, 14 * fe.DAY_MS), fe.freshness(when, now_ms, 7 * fe.DAY_MS))
            scores[c] = rel * timing
        return [c for c, _ in fe.top_k(scores, budget=1.0, k=TOP_K)] if scores else []

    # slot filling for an event
    def fill_event(self, ev: str, b: Bundle, q: str, anc: dict[str, list[str]]) -> None:
        n = self.nodes[ev]
        a = n["attrs"]
        about = self.edges_of(ev, ["about"], "in")
        b.put("what", [a.get("title") or n["label"]] + [self.nodes[e["a"]]["attrs"].get("summary") or ""
                                                       for e in about if e["a"] in self.nodes])
        for e in about:
            b.cite(e)
        whos = self.edges_of(ev, ["attends"], "in")
        people = [e for e in whos if self.nodes.get(e["a"], {}).get("kind") == "person"]
        if re.search(r"pick(ing)?\b.*\bup\b", q.lower()) and anc["person"]:
            people = [e for e in people if e["a"] not in anc["person"]]
        orgs = [e for e in self.edges_of(ev, ["organises"], "in") if e["a"] in self.nodes]
        if re.search(r"\b(organi[sz]\w*|set (it )?up|schedul\w*|book\w*|arrang\w*|host\w*|made)\b", q.lower()) and orgs:
            b.put("who", [_native(e["a"]) for e in orgs], orgs)
        elif people:
            b.put("who", [_native(e["a"]) for e in people], people)
        elif orgs:
            b.put("who", [_native(e["a"]) for e in orgs], orgs)
        places, at = self.place_of(ev)
        if places:
            b.put("where", places, at)
        elif a.get("location_text"):
            b.put("where", [a["location_text"]])
            b.rung("where", "observed")   # the calendar's own words
        start = _local(a.get("start"), self.zone)
        if start:
            moved = [e for e in about if (e.get("asserts") or {}).get("start")
                     and _local(e["asserts"]["start"], self.zone) != start]
            for e in about:
                if e in moved or e["a"] not in self.nodes:
                    continue
                said = summary_time(self.nodes[e["a"]]["attrs"].get("summary") or "", start)
                if said and said != start:
                    moved.append({**e, "asserts": {"start": said.isoformat()}, "rung": "explained"})
            cal = next((e for e in about if (self.nodes.get(e["a"]) or {}).get("attrs", {}).get("source") == "calendar"), None)
            if moved:
                opts = [(_hm(start), cal or {"a": ev, "type": "start", "b": ev, "rung": "observed", "prov": []})]
                opts += [(_hm(_local(e["asserts"]["start"], self.zone)), e) for e in moved]
                b.contest("when", opts)
            else:
                b.put("when", [_hm(start)], [cal] if cal else None)

    def fill_item_dates(self, iid: str, b: Bundle, terms: list[str]) -> bool:
        """An item's own dated mentions (a meeting's 'fall cleanup' on the 24th): when, and where when a related
        item names a place for the same moment."""
        a = self.nodes[iid]["attrs"]
        best, hit = None, 0
        for d in a.get("dates") or []:
            h = sum(1 for t in terms if _stem(t) in {_stem(w) for w in _words(d.get("what") or "")})
            if h > hit:
                best, hit = d, h
        if not best:
            return False
        b.put("when", [best["when"][:16]])
        b.put("what", [best.get("what") or ""])
        b.cite_item(iid)
        for e in self.edges_of(iid, ["related"]):
            other = e["b"] if e["a"] == iid else e["a"]
            oa = (self.nodes.get(other) or {}).get("attrs") or {}
            if any((d.get("when") or "")[:16] == best["when"][:16] for d in oa.get("dates") or []) and oa.get("location"):
                pl = self.resolve_place(oa["location"])
                if pl:
                    b.put("where", [pl], [e])
                    b.cite_item(other)
        return True

    def here_with(self, q: str, b: Bundle) -> bool:
        """'Who did we come with?', 'who's here with us?' with no place named: the place is where the viewer is (their
        latest arrival, #1396). The answer is the household people on that place's items and plans that day, the asker
        left out; a non-household counterparty who wrote about it is "invited by". False when the viewer isn't
        anywhere (no arrival within HERE_HOURS), so the question falls through to what the graph knows otherwise."""
        arrivals = [e for e in self.edges_of(self.me, ["located_at"], "out") if e.get("valid_from")]
        seen = [(t, e) for e in arrivals for t in [_local(e["valid_from"], self.zone)]
                if t and timedelta(0) <= self.now - t <= timedelta(hours=HERE_HOURS)]
        if not seen:
            return False
        arrived, here = max(seen, key=lambda x: x[0])
        # a later departure (valid_to) from the same place means they have left
        if any(e["b"] == here["b"] and (t := _local(e.get("valid_to"), self.zone)) and t > arrived
               for e in self.edges_of(self.me, ["located_at"], "out")):
            return False
        place, day = here["b"], arrived.date()
        # items that name the place on that day, and the thread-mates linked to them
        items = []
        for e in self.edges_of(place, ["mentions"], "in"):
            n = self.nodes.get(e["a"])
            t = _local(((n or {}).get("attrs") or {}).get("observed_at"), self.zone)
            if n and n["kind"] == "item" and t and t.date() == day:
                items.append(e["a"])
        cluster = list(dict.fromkeys(items))
        for i in list(cluster):
            for e in self.edges_of(i, ["related", "replies_to"]):
                other = e["b"] if e["a"] == i else e["a"]
                if other in self.nodes and other not in cluster:
                    cluster.append(other)
        # events at the place that day
        events = []
        for e in self.edges_of(place, ["at"], "in"):
            t = self.start_of(e["a"])
            if t and t.date() == day:
                events.append(e["a"])
        household, invited = [], []
        for src in cluster + events:
            for e in self.edges_of(src):
                if e["type"] not in ("from", "to", "mentions", "attends", "organises"):
                    continue
                other = e["b"] if e["a"] == src else e["a"]
                p = self.nodes.get(other)
                if not p or p["kind"] != "person" or other == self.me:
                    continue
                if (p.get("attrs") or {}).get("role") in HOUSEHOLD_ROLES:
                    household.append(e)
                elif e["type"] == "from":
                    invited.append(e)
        b.put("where", [_native(place)], [here])
        if household:
            b.put("who", [_native(e["b"] if e["a"] in cluster + events else e["a"]) for e in household], household)
        if invited:
            b.put("what", ["invited by " + (self.nodes[e["b"]].get("label") or _native(e["b"])) for e in invited], invited)
        return True

    def resolve_place(self, text: str) -> str | None:
        t = (text or "").strip().lower()
        for nid, n in self.nodes.items():
            if n["kind"] == "place" and t in [x.lower() for x in _names(n)]:
                return _native(nid)
        return None

    def places_in(self, text: str) -> list[str]:
        out = []
        for nid, n in self.nodes.items():
            if n["kind"] == "place" and any(_mentions(text, x) for x in _names(n) if len(x) > 3):
                out.append(_native(nid))
        return out


def situation(query: str, viewer: str, role: str, graph: Any, *, zone: str, now: str) -> dict[str, Any]:
    s = Situation(graph, viewer, role, zone, now)
    q = query.strip()
    ql = q.lower()
    b = Bundle()
    asked = asked_slots(q)

    if q == "@proactive":
        # the engine raises a proposal only for a completed event no calendar or proposal covers: here, the existing
        # proposals are the answer, and nothing new is raised beside them
        props = [n["attrs"].get("proposal") or nid.split("event:ep-", 1)[1] for nid, n in s.nodes.items()
                 if n["kind"] == "event" and (n["attrs"].get("proposal") or nid.startswith("event:ep-"))]
        return {"proposals": props, "raised": [], "why": [], "via": [], "rung": None}

    anc = anchors(q, viewer, s.nodes)
    for p in family(q, viewer, s.edges, s.me):
        if p in s.nodes and p not in anc["person"]:
            anc["person"].append(p)
    win = time_window(q, s.now)
    terms = [w for w in _words(q) if not any(_mentions(w, x) for nid in anc["person"] + anc["org"] + anc["place"]
                                               for x in _names(s.nodes[nid]))]

    # "the same as": a private record the viewer's view merged into a node (graph_sync.same_as, told only to its owner)
    if "same as" in ql or "the same" in ql:
        for m in (graph.same_as(viewer, role) if hasattr(graph, "same_as") else []):
            if _mentions(q, m["name"]) and m["node_id"] in s.nodes:
                b.put("who", [_native(m["node_id"])])
                b.put("what", [f"same: {m['name']} is {s.nodes[m['node_id']]['label']} ({m.get('why') or 'merged'})"])
                b.rung("what", "attributed")
        return b.out(asked)

    # "who did we come with", "who's here with us": no place named, so the place is where the viewer is now (#1396)
    if _HERE_WITH.search(ql) and not anc["place"] and not [p for p in anc["person"] if p != s.me]:
        if s.here_with(q, b):
            return b.out(asked | {"who"})

    # parents
    if "parent" in ql and anc["person"]:
        kid = next((p for p in anc["person"] if p != s.me), anc["person"][0])
        es = s.edges_of(kid, ["parent_of"], "in")
        b.put("who", [_native(e["a"]) for e in es], es)
        return b.out(asked | {"who"})

    # obligations: what's waiting, paid, due
    obs = [nid for nid, n in s.nodes.items() if n["kind"] == "obligation"]
    if re.search(r"\b(waiting on me|owe|pay|paid|due|bill)\b", ql) and not re.search(r"\brelated\b", ql):
        mine = obs
        if anc["org"]:
            labels = [s.nodes[o]["label"].lower() for o in anc["org"]]
            mine = [o for o in obs if (s.nodes[o]["attrs"].get("account_label") or "").lower() in labels]
        state = {o: (graph.obligation_state(viewer, role, o) if hasattr(graph, "obligation_state") else None) or {}
                 for o in obs}
        if "waiting on me" in ql or "owe" in ql:
            open_ = [o for o in mine if state[o].get("state") == "open"]
            if not open_ and mine:
                # nothing open: say so, with what settled recently (the last 30 days), so "paid" is shown, not assumed
                b.put("what", ["nothing waiting"])
                cut = (s.now - timedelta(days=30)).date().isoformat()
                for o in mine:
                    st = state[o]
                    if st.get("state") == "paid" and (st.get("on") or "") >= cut:
                        b.put("what", [f"{s.nodes[o]['attrs'].get('account_label') or s.nodes[o]['label']} paid {st['on']}"])
                        b.rung("what", "attributed")
                return b.out(asked)
            mine = open_
        for o in mine:
            a = {**s.nodes[o]["attrs"], **({"state": state[o]["state"], "settled_on": state[o].get("on")} if state[o] else {})}
            items = s.edges_of(o, ["about"], "in")
            for iid in state[o].get("items") or []:   # the cycle's mail, read time (graph_sync.obligation_state)
                b.cite_item(iid)
            if re.search(r"\bdue\b", ql):
                b.put("when", [a["due"]], items)
            elif re.search(r"\bpa(y|id)\b", ql):
                st = "paid" if a.get("state") == "paid" else a.get("state")
                b.put("what", [st, a.get("settled_on") or ""], items + s.edges_of(o, ["paid"], "in"))
            else:
                b.put("what", [a["account_label"], f"${a['amount']:.2f}", a["due"]], items)
        return b.out(asked)

    # related or not: two things named, a link between them (a person's "not related" included)
    if re.search(r"\brelated\b", ql):
        sides = []
        for part in re.split(r"\brelated to\b|\band\b", ql):
            pt = [w for w in _words(part) if w not in ("related",)]
            hits = {i: s.term_hits(i, pt) for i, n in s.nodes.items() if n["kind"] == "item"}
            top = max(hits.values(), default=0)
            if top:
                sides.append([i for i, h in hits.items() if h == top])
        if len(sides) == 2:
            said = graph.visible_edges(viewer, role, states=("rejected", "verified", "accepted"))
            pairs = [{x, y} for x in sides[0] for y in sides[1] if x != y]
            links = [e for e in said if e["type"] == "related" and {e["a"], e["b"]} in pairs]
            link = next((e for e in links if e["state"] == "rejected"), None) or (links[0] if links else None)
            if link and link["state"] == "rejected":
                b.put("what", ["not related (you said so)"])
                b.rungs.append("observed")
            elif link:
                b.put("what", ["related: " + (link.get("why") or "")], [link])
            else:
                b.put("what", ["no link known"])
        return b.out(asked)

    # a task waiting on something
    if re.search(r"\btask\b", ql):
        for nid, n in s.nodes.items():
            if n["kind"] != "scenario":
                continue
            deps = s.edges_of(nid, ["depends_on"], "out")
            if not anc["org"] or set(anc["org"]) & {e["b"] for e in deps}:
                b.put("what", [n["attrs"]["goal"]], deps[:1])
                b.cite_item(nid)
        return b.out(asked)

    # where someone is right now: place events only (a home address is not where they are)
    if re.search(r"\bwhere'?s?\b", ql) and anc["person"] and (win and win[1] - win[0] <= timedelta(hours=13)
                                                              or re.search(r"\bwhere'?s \w+\??$", ql)):
        who = next((p for p in anc["person"] if p != s.me), anc["person"][0])
        seen = [e for e in s.edges_of(who, ["located_at"], "out") if e.get("valid_from")]
        if seen:
            e = max(seen, key=lambda x: x["valid_from"])
            b.put("where", [_native(e["b"])], [e])
            b.put("when", [_hm(_local(e["valid_from"], s.zone))])
        return b.out(asked | {"where"})

    # "who is <name>": a person by name, with what's known of them
    if re.match(r"who(?:'s| is)\b", ql) and anc["person"] and not re.search(r"\b(pick\w*|coming|going)\b", ql):
        p = anc["person"][0]
        b.put("who", [_native(p)])
        if s.nodes[p]["attrs"].get("role"):
            b.put("what", [s.nodes[p]["attrs"]["role"]])
        b.rungs.append("attributed")
        return b.out(asked)

    # meetings: decisions, and what falls to the viewer
    if re.search(r"\b(decide|decision|board|on the hook)\b", ql):
        for nid, n in s.nodes.items():
            if n["kind"] != "item":
                continue
            dec = [s.nodes[e["b"]]["label"] for e in s.edges_of(nid, ["decided"], "out") if e["b"] in s.nodes]
            if not dec:
                continue
            if anc["org"] and not set(anc["org"]) & {e["b"] for e in s.edges_of(nid)}:
                if not any(s.term_hits(nid, [w]) for w in ("club", "board")):
                    continue
            if re.search(r"\bwhen'?s?\b|\bnext\b", ql) and s.fill_item_dates(nid, b, terms):
                return b.out(asked)
            if "on the hook" in ql or re.search(r"\b(am i|my)\b", ql):
                me = (s.nodes.get(s.me) or {}).get("label", "").split()[0]
                mine = [d for d in dec if me and me.lower() in d.lower()]
                b.put("what", mine)
                if mine:
                    b.put("who", [viewer])
            else:
                b.put("what", dec)
            b.cite_item(nid)
            for e in s.edges_of(nid, ["decided"], "out"):
                b.rung("what", e.get("rung") or "observed")
        return b.out(asked)

    # events and dated items
    events = [nid for nid, n in s.nodes.items() if n["kind"] == "event"]
    seeds = anc["person"] + anc["org"] + anc["place"]
    reach = s.expand(seeds) if seeds else {}
    if re.search(r"\b(say|said|text|told)\b", ql) and anc["person"]:
        sender = [p for p in anc["person"] if p != s.me] or anc["person"]
        sent = {e["a"] for e in s.edges if e["type"] == "from" and e["b"] in sender}
        events = [ev for ev in events if sent & {e["a"] for e in s.edges_of(ev, ["about"], "in")}]
    listing = bool(win) and bool(re.search(r"\b(happening|schedule|calendar|agenda|plans?|on)\b", ql)) \
        and not (anc["person"] or anc["org"] or anc["place"]) and not [t for t in terms if t not in
                                                                        ("happening", "schedule", "calendar", "agenda",
                                                                         "plans", "plan", "planned", "on", "week",
                                                                         "weekend", "today", "tomorrow", "next")]
    best = s.score(events, anc, terms, win, reach)
    if best and not ("this week" in ql or listing):
        s.fill_event(best[0], b, q, anc)
        return b.out(asked)
    if best and ("this week" in ql or listing):
        for ev in best + [e for e in events if e not in best]:
            t = s.start_of(ev)
            if t and win and win[0] <= t < win[1]:
                b.put("what", [s.nodes[ev]["label"]])
                b.cite_item(ev.split(":", 1)[1], "observed")
        return b.out(asked)

    # dated mentions in items (a meeting's next date)
    for nid, n in s.nodes.items():
        if n["kind"] == "item" and n["attrs"].get("dates") and s.term_hits(nid, terms) and s.fill_item_dates(nid, b, terms):
            return b.out(asked)

    # items by content: a note, a text, a mail thread (and the org it's from)
    if True:
        scored = []
        for nid, n in s.nodes.items():
            if n["kind"] != "item":
                continue
            h = s.term_hits(nid, terms)
            if anc["org"] and set(anc["org"]) & {e["b"] for e in s.edges_of(nid, ["from", "mentions", "about"], "out")}:
                h += 2
            if anc["person"] and set(anc["person"]) & {e["b"] for e in s.edges_of(nid, ["mentions", "from"], "out")}:
                h += 1
            if h >= 2 or (h >= 1 and len(terms) <= 2):
                scored.append((h, nid))
        scored.sort(reverse=True)
        if scored:
            top = scored[0][0]
            picked = [nid for h, nid in scored if h >= max(2, top - 1)] or [scored[0][1]]
            for nid in picked:
                n = s.nodes[nid]
                b.put("what", [n["attrs"].get("summary") or n["label"]])
                b.cite_item(nid)
                # links among the picked items, and any typed same-identifier link (an order number shared with a text
                # whose body isn't in the graph): strong evidence on its own, so it's cited
                rel = [e for e in s.edges_of(nid, ["related"])
                       if (e["a"] in picked and e["b"] in picked) or (e.get("plane") == "typed" and e["state"] == "verified")]
                for e in rel:
                    b.cite(e)
                pls = s.places_in(n["attrs"].get("summary") or "")
                if pls:
                    b.put("where", pls)
                orgs = [e for e in s.edges_of(nid, ["from"], "out") if (s.nodes.get(e["b"]) or {}).get("kind") == "org"]
                if orgs and "who" in asked | {"who"} and anc["org"]:
                    b.put("who", [_native(e["b"]) for e in orgs if e["b"] in anc["org"]])
    return b.out(asked)
