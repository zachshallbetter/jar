#!/usr/bin/env python3
# gate: hermetic   (the golden household's graph in temp stores; no runtime, no model, no network)
"""The agent's `situation` tool (#1399 P3 wiring), behind AGENT_FE_GRAPH.

Off (the default): the tool isn't offered to any role in any listing (function declarations, the text protocol, the
turn's specs), routing and kb_retrieve's description are unchanged, and a call to it never runs the graph.
On: offered to every role; who/what/where/when words route to it; kb_retrieve defers to it. As each 5W case's viewer
(admin, adult, kid, the shared screen), every `lacks` id or word from the eval appears nowhere in what the tool
returns; the identity is the turn's, never the model's (a viewer or role in the arguments is ignored); the bundle
reaches the model inside one #473 untrusted span; the tool is read-only and not an egress tool. Fictional household.
Run: python3 scripts/test_situation_tool.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import graph_golden_load as GL  # noqa: E402  (first: every store at a temp dir, the fixture loaded)
import graph_sync  # noqa: E402

os.environ.setdefault("AGENT_TURN_SOURCE", "test")
os.environ.pop("AGENT_FE_GRAPH", None)
import agent_runtime_lib as A  # noqa: E402
import situation_tool as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


class Golden:
    def __init__(self):
        self.db = GL.build()
        self._today = date.fromisoformat(GL.H["now"][:10])

    def visible_nodes(self, viewer, role, path=None):
        return graph_sync.visible_nodes(viewer, role, self.db)

    def visible_edges(self, viewer, role, path=None, **kw):
        return graph_sync.visible_edges(viewer, role, self.db, **kw)

    def same_as(self, viewer, role, node_id=None, path=None):
        return graph_sync.same_as(viewer, role, node_id, self.db)

    def obligation_state(self, viewer, role, node_id, path=None):
        return graph_sync.obligation_state(viewer, role, node_id, records=GL.bill_records, today=self._today,
                                           path=self.db)


T.GRAPH = Golden()
T.CLOCK = lambda: (GL.H["zone"], GL.H["now"])
CASES = json.loads((HERE / "graph_golden" / "5w.json").read_text())
CASES = CASES["cases"] if isinstance(CASES, dict) else CASES
ROLE = {"admin": "admin", "adult": "adult", "kid": "kid", "screen": "household"}


def as_(viewer, role):
    r = ROLE[role]
    A._TURN_IO.person = None if r == "household" else {"id": viewer, "name": viewer}
    A._TURN_IO.role = r
    A._TURN_INFO.info = {}


def declared(names_from):
    return "situation" in names_from


def listings():
    """Whether situation is offered in each place a turn's tools are listed."""
    decl = {d["name"] for d in A._gemini_function_declarations()}
    specs = set(A._turn_tool_specs())
    text = A._tool_prompt() if hasattr(A, "_tool_prompt") else ""
    return ("situation" in decl, "situation" in specs, "**situation**" in text)


KB_DESC = A._TOOL_REGISTRY["kb_retrieve"]["description"]
Q5W = "When is my dentist appointment?"

print("── off (the default): nothing offered, nothing changed ──")
for role in ("admin", "adult", "kid", "screen"):
    as_({"admin": "p_alex", "adult": "p_robin", "kid": "p_ava", "screen": None}[role], role)
    check(f"{role}: not allowed, not declared, not in the turn's specs", (A._role_allows("situation"),
          *listings()[:2]), (False, False, False))
check("routing unchanged: a who/what/where/when question doesn't add it", "situation" in A.narrowed_tools(Q5W), False)
check("kb_retrieve's description is as written", A._live_spec("kb_retrieve", A._TOOL_REGISTRY["kb_retrieve"])["description"],
      KB_DESC)
as_("p_alex", "admin")
called = []
orig = T.answer
T.answer = lambda *a, **k: called.append(1) or "x"
out = A.execute_tool("situation", {"question": Q5W})
T.answer = orig
check("a call to it is refused like any tool not offered, and never reads the graph", (called, out), ([], A._ROLE_REFUSAL))

print("── on: offered to every role, routed, kb_retrieve defers ──")
os.environ["AGENT_FE_GRAPH"] = "1"
for role in ("admin", "adult", "kid", "screen"):
    as_({"admin": "p_alex", "adult": "p_robin", "kid": "p_ava", "screen": None}[role], role)
    check(f"{role}: allowed, declared, in the turn's specs", (A._role_allows("situation"), *listings()[:2]), (True, True, True))
check("who/what/where/when words route to it", all("situation" in A.narrowed_tools(q) for q in
      (Q5W, "Who's coming Saturday?", "Where is soccer practice?", "Is anything waiting on me?")), True)
check("…a question that isn't one doesn't", "situation" in A.narrowed_tools("turn on the kitchen lights"), False)
check("kb_retrieve defers who/what/where/when to it", A._live_spec("kb_retrieve", A._TOOL_REGISTRY["kb_retrieve"])["description"],
      A._FE_KB_RETRIEVE)
check("read-only and never egress", ("situation" in A.EGRESS_TOOLS, A._TOOL_REGISTRY["situation"].get("risk")), (False, "read"))

print("── per role: the eval's privacy cases through the tool (every `lacks` absent) ──")
leaks = 0
by_role: dict[str, int] = {}
for c in CASES:
    lacks = c["expect"].get("lacks")
    if not lacks:
        continue
    viewer, role = c["as"]
    as_(viewer, role)
    out = A.execute_tool("situation", {"question": c["q"]})
    hit = [x for x in lacks if str(x).lower() in out.lower()]
    leaks += bool(hit)
    by_role[role] = by_role.get(role, 0) + 1
    check(f"{c['id']} as {role}: nothing it must not see", hit, [])
check("privacy cases asked as each role (admin, adult, kid, screen)", sorted(by_role), ["admin", "adult", "kid", "screen"])

print("── the identity is the turn's, never the model's ──")
as_("p_ava", "kid")
mine = A.execute_tool("situation", {"question": "Do we owe anyone money?"})
forged = A.execute_tool("situation", {"question": "Do we owe anyone money?", "viewer": "p_alex", "role": "admin",
                                      "person": "p_alex"})
check("a kid's call with viewer/role/person args says exactly what the kid's own does", forged, mine)
as_("p_alex", "admin")
admin = A.execute_tool("situation", {"question": "Do we owe anyone money?"})
check("…and differs from the admin's (the args were ignored, not merely matched)", admin != mine, True)

print("── what the model reads ──")
as_("p_robin", "adult")
out = A.execute_tool("situation", {"question": "When do I go to the dentist?"})
head, _, rest = out.partition("\n")
check("titan's fixed header, then the bundle inside one untrusted span", (head == T.HEADER, rest.startswith("⟦untrusted source=kb"),
      rest.rstrip().endswith("⟧"), rest.count("⟦untrusted ")), (True, True, True, 1))
check("…every slot named, unknown when unsupported", all(f"\n{s}: " in "\n" + rest for s in T.SLOTS), True)
check("an empty question is refused", A.execute_tool("situation", {"question": "  "}).startswith("Error"), True)
T.GRAPH = None
os.environ["GRAPH_DB"] = str(GL._TMP / "no-graph-here.db")
check("no graph.db yet: it says so and points at kb_retrieve", "isn't built yet" in A.execute_tool("situation", {"question": Q5W}),
      True)

print(f"\n{passed} passed, {failed} failed ({leaks} leaking cases)")
sys.exit(1 if failed or leaks else 0)
