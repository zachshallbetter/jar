#!/usr/bin/env python3
"""The agent's `situation` read tool (#1399 P3 wiring; spec docs/relational-fe-graph.md §8–§9), behind AGENT_FE_GRAPH
(default off; turning it on is Zach's call).

situation.situation() answers who / what / where / when / why over the asker's own visible subgraph (graph_sync's
visible_nodes / visible_edges). This module only runs it for the turn's asker and renders the bundle for the model:

  - the identity is always the turn's own (the caller passes _kb_who()'s person and role), never anything the model
    supplied: a viewer or role in the tool's arguments is ignored;
  - every value in the bundle comes from items and people (names, labels, an item's summary, a why line), so the whole
    bundle goes to the model inside ONE #473 untrusted span (`wrap`), as other KB reads do; only the fixed header and
    the slot names are titan's own words;
  - read-only, no egress: graph.db is opened read-only, nothing is written, nothing leaves titan.

Run (hermetic): python3 scripts/test_situation_tool.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

FLAG = "AGENT_FE_GRAPH"
SLOTS = ("who", "what", "where", "when")
# The tests (and the A/B harness's fixtures) point these at a fixture graph and clock; live: graph_sync and the
# household zone's now.
GRAPH: Any = None
CLOCK: Callable[[], tuple[str, str]] | None = None
HEADER = ("From the household graph, for you only (each value with the chain it stands on). \"unknown\" means "
          "nothing supports an answer: say so or ask one question, never guess. \"contested\" lists both chains.")


def enabled() -> bool:
    return os.environ.get(FLAG, "0") == "1"


def _graph() -> Any:
    if GRAPH is not None:
        return GRAPH
    import graph_sync
    return graph_sync


def _clock() -> tuple[str, str]:
    """(zone name, now as ISO) in the household's zone."""
    if CLOCK is not None:
        return CLOCK()
    import local_time
    z = local_time.zone()
    now = local_time.now()
    return (getattr(z, "key", None) or local_time.DEFAULT_TZ), now.isoformat()


def _built() -> bool:
    """A graph to read: the injected one, or graph.db on disk (graph_sync's --full has run)."""
    if GRAPH is not None:
        return True
    import graph_sync
    return Path(os.environ.get("GRAPH_DB") or graph_sync.DB).exists()


def render(bundle: dict[str, Any]) -> str:
    """The bundle as lines: each slot's value (or unknown / contested with both chains), the why-chains with the item
    ids they cite, and the weakest rung."""
    lines = []
    for s in SLOTS:
        v = bundle.get(s, "unknown")
        if isinstance(v, dict) and "contested" in v:
            opts = "; ".join(f"{o.get('value')} (via {o.get('chain', {}).get('a')} {o.get('chain', {}).get('type')} "
                             f"{o.get('chain', {}).get('b')})" for o in v["contested"])
            lines.append(f"{s}: contested: {opts}")
        elif isinstance(v, dict) and "is" in v:
            lines.append(f"{s}: " + ", ".join(str(x) for x in v["is"]))
        else:
            lines.append(f"{s}: unknown")
    for c in (bundle.get("why") or [])[:12]:
        why = f" ({c.get('why')})" if c.get("why") else ""
        lines.append(f"why: {c.get('a')} {c.get('type')} {c.get('b')} [{c.get('rung') or 'unrated'}]{why}")
    if bundle.get("via"):
        lines.append("items: " + ", ".join(str(x) for x in bundle["via"][:20]))
    if bundle.get("rung"):
        lines.append(f"weakest rung: {bundle['rung']}")
    for k in ("proposals", "raised"):
        if bundle.get(k):
            lines.append(f"{k}: " + ", ".join(str(x) for x in bundle[k]))
    return "\n".join(lines)


def answer(question: str, viewer: str | None, role: str, *, wrap: Callable[[str], str]) -> str:
    """The tool's result for the turn's asker. viewer None (a shared screen) reads as the household's view."""
    q = " ".join(str(question or "").split())[:300]
    if not q:
        return "Error: question is required (who, what, where or when, in the person's words)."
    if not role or (viewer is None and role != "household"):
        return "The household graph answers for a person or a shared screen, and this turn has neither."
    graph = _graph()
    if not _built():
        return "The household graph isn't built yet. Use kb_retrieve for this one."
    import situation as S
    zone, now = _clock()
    try:
        bundle = S.situation(q, viewer or "household", role, graph, zone=zone, now=now)
    except Exception as exc:  # noqa: BLE001 - a graph that can't be read: the KB path still answers
        return f"The household graph couldn't be read ({type(exc).__name__}). Use kb_retrieve for this one."
    return HEADER + "\n" + wrap(render(bundle))


def bundle_json(bundle: dict[str, Any]) -> str:
    """For the harness: the bundle as JSON (ids and values; never logged)."""
    return json.dumps(bundle, default=str, sort_keys=True)
