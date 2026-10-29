#!/usr/bin/env python3
# gate: hermetic
"""kb_sources reads every page of the KB's source list (10-02 audit: five readers took one page of 1000 at 770), and
record_nodes every record node of a type with the record runtime's after/next cursor (#345).

Run: python3 scripts/test_kb_sources.py
"""
import os
import sys
import tempfile

os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


ALL = [{"source_id": f"s{i}", "title": f"t{i}"} for i in range(2350)]
seen = []


def paged(svc, path, method="GET", payload=None, **k):
    seen.append(path)
    q = dict(x.split("=") for x in path.split("?")[1].split("&"))
    a = int(q.get("offset", 0))
    return {"sources": ALL[a:a + int(q["limit"])]}


check("every source across pages", len(lib.kb_sources(paged)), 2350)
check("three requests, the last one short", len(seen), 3)
seen.clear()
check("an exact multiple stops at the empty page", len(lib.kb_sources(lambda *a, **k: paged(*a, **k) if "offset=2000" not in a[1]
                                                                    else {"sources": []}, page=1000)), 2000)
check("a runtime that ignores offset: one page, no loop", len(lib.kb_sources(lambda *a, **k: {"sources": ALL[:1000]})), 1000)
check("a failed read is empty, not an error", lib.kb_sources(lambda *a, **k: None), [])

print("\n── record nodes, paged with after/next (#345) ──")
NODES = [{"node_id": f"n{i:04d}"} for i in range(1234)]
asked = []


def rec(svc, path, method="GET", payload=None, **k):
    asked.append(path)
    q = dict(x.split("=", 1) for x in path.split("?")[1].split("&"))
    after, lim = q.get("after", ""), int(q["limit"])
    rest = [n for n in NODES if n["node_id"] > after]
    page = rest[:lim]
    return {"nodes": page, "next": page[-1]["node_id"] if len(page) == lim else None}


got, complete = lib.record_nodes("bill", rec)
check("every node across pages, complete", (len(got), complete, len({n["node_id"] for n in got})), (1234, True, 1234))
check("three requests", len(asked), 3)
check("a runtime without paging: a full page, no cursor → partial",
      lib.record_nodes("bill", lambda *a, **k: {"nodes": NODES[:500]})[1], False)
check("a short page: complete", lib.record_nodes("bill", lambda *a, **k: {"nodes": NODES[:7]}), (NODES[:7], True))
check("extra query terms ride along", "field=start" in (lambda: (lib.record_nodes("appointment", rec, extra="field=start"), asked[-1])[1])(), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
