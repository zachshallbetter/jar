#!/usr/bin/env python3
# gate: hermetic
"""Every one of titan's own tools declares what it is (#1628 slice 2, tool_descriptors.py): a domain, a risk, where its
data goes, how it's undone and whether it always asks. The test fails when a tool has no descriptor, a field is out of
vocabulary, or a descriptor disagrees with the rules titan enforces today (the role table, the Watch's list, the egress
list). Also: titan has words for every one of the Mac's trust domains. Definitions only; nothing runs.
Run: python3 scripts/test_tool_descriptors.py"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_runtime_lib as lib  # noqa: E402
import egress  # noqa: E402
import household_identity as hh  # noqa: E402
import tool_descriptors as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}" + ("" if good else f": got={got!r:.300} expected={expected!r:.300}"))


registry = set(lib._TOOL_REGISTRY)
described = set(T.DESCRIPTORS)

print("── coverage ──")
check("every titan tool has a descriptor", sorted(registry - described), [])
check("no descriptor for a tool that no longer exists", sorted(described - registry), [])
check("no descriptor for a device tool (the Mac and phone govern those)", sorted(described & set(lib._DEVICE_TOOLS)), [])

print("── vocabulary ──")
check("domains are titan's own", sorted({d.domain for d in T.DESCRIPTORS.values()} - set(T.DOMAINS)), [])
check("risks are read, write or dangerous", sorted({d.risk for d in T.DESCRIPTORS.values()} - set(T.RISKS)), [])
check("egress kinds are egress.KINDS", sorted({k for d in T.DESCRIPTORS.values() for k in d.egress} - set(egress.KINDS)), [])
check("a read has no undo", sorted(n for n, d in T.DESCRIPTORS.items() if d.risk == "read" and d.undo is not None), [])
check("a change says how it's undone, or 'none'",
      sorted(n for n, d in T.DESCRIPTORS.items() if d.risk != "read" and not d.undo), [])
check("anything that can't be undone and is dangerous always asks, or is titan's own system",
      sorted(n for n, d in T.DESCRIPTORS.items()
             if d.risk == "dangerous" and d.undo == "none" and not d.always_ask and d.domain != "system"), [])

print("── agrees with what titan enforces today ──")
check("every tool in the 'about' turn's egress list declares egress",
      sorted(n for n in lib.EGRESS_TOOLS if not T.DESCRIPTORS[n].egress), [])
check("roles come from the role table, admin always included",
      all(T.roles(n) == (frozenset({"admin"}) | hh.TOOL_ROLES.get(n, frozenset())) for n in registry), True)
check("roles agree with role_allows_tool", sorted(
    n for n in registry for r in ("admin", "adult", "kid", "household") if (r in T.roles(n)) != hh.role_allows_tool(r, n)), [])
check("the Watch reaches exactly its own list", sorted(n for n in registry if "watch" in T.surfaces(n)),
      sorted(hh.WATCH_TOOLS & registry))
check("every tool reaches the other surfaces", all(set(T.SURFACES) - {"watch"} <= T.surfaces(n) for n in registry), True)
check("what confirms on the person's own device today always asks",
      [T.DESCRIPTORS[n].always_ask for n in ("lock_up", "mail_move", "mail_move_undo")], [True, True, True])
check("deleting from the knowledge base is permanent and always asks",
      (T.DESCRIPTORS["kb_forget"].risk, T.DESCRIPTORS["kb_forget"].undo, T.DESCRIPTORS["kb_forget"].always_ask),
      ("dangerous", "none", True))
check("the sandbox stays a read with no egress", (T.DESCRIPTORS["run_python"].risk, T.DESCRIPTORS["run_python"].egress), ("read", ()))
check("describe() carries every field", sorted(T.describe("web_search")),
      ["always_ask", "domain", "egress", "id", "risk", "roles", "surfaces", "undo"])
check("describe() of an unknown tool is None", T.describe("mystery_tool"), None)

print("── titan's words for the Mac's trust domains ──")
tree = ast.parse((HERE / "agent_api_server.py").read_text())
consts = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
          for t in n.targets if isinstance(t, ast.Name) and t.id in ("_DOMAIN_TEXT", "MAC_TRUST_DOMAINS")}
check("the Mac has 23 trust domains", len(consts.get("MAC_TRUST_DOMAINS", ())), 23)
check("every Mac trust domain has words (no raw names in DEVICE POLICY)",
      sorted(d for d in consts.get("MAC_TRUST_DOMAINS", ()) if d not in consts.get("_DOMAIN_TEXT", {})), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
