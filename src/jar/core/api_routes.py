#!/usr/bin/env python3
"""Every route of the agent API, read from agent_api_server.py's decorators, as docs/api-routes.md.

The file is generated so it can't drift (10-03: about 60 routes were in no doc). The deploy gate's
test_api_routes.py fails when it's stale.
Run: python3 scripts/api_routes.py            (rewrite docs/api-routes.md)
     python3 scripts/api_routes.py --check    (exit 1 if it's out of date)
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "scripts" / "agent_api_server.py"
DOC = ROOT / "docs" / "api-routes.md"
METHODS = ("get", "post", "put", "patch", "delete")


def routes(src: str) -> list[tuple[str, str, str]]:
    """(METHOD, path, first docstring line) for each @app.<method>("path") in source order."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = (ast.get_docstring(node) or "").strip().split("\n")[0].strip()
        for d in node.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in METHODS
                    and isinstance(d.func.value, ast.Name) and d.func.value.id == "app"
                    and d.args and isinstance(d.args[0], ast.Constant)):
                out.append((d.func.attr.upper(), d.args[0].value, doc, node.lineno))
    out.sort(key=lambda r: r[3])
    header = header_notes(src)
    return [(m, p, header.get((m, p)) or doc) for m, p, doc, _ in out]


def header_notes(src: str) -> dict[tuple[str, str], str]:
    """The server header's own one-liners ("GET  /v1/kb  — list the KB entries…"), which win over a docstring."""
    notes = {}
    for line in (ast.get_docstring(ast.parse(src)) or "").splitlines():
        m = re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+(/\S+)\s+—\s+(.*)$", line.strip())
        if m:
            notes[(m.group(1), m.group(2))] = m.group(3).strip()
    return notes


def group(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "v1":
        return parts[1]
    return parts[0] if parts else "/"


def render(rs: list[tuple[str, str, str]]) -> str:
    lines = [
        "# Agent API routes",
        "",
        "Generated from `scripts/agent_api_server.py` by `python3 scripts/api_routes.py`; don't edit by hand.",
        "The deploy gate (`test_api_routes.py`) fails when this is out of date. Auth: every route except",
        "`/health` and `POST /v1/pair/claim` needs `X-Agent-Token`, and non-admin roles reach only the routes",
        "`household_identity.ROUTE_ROLES` lists. Shapes and examples are in the server's header and the",
        "runbook; this is the complete list.",
        "",
        f"{len(rs)} routes.",
    ]
    seen: dict[str, list[tuple[str, str, str]]] = {}
    for r in rs:
        seen.setdefault(group(r[1]), []).append(r)
    for g, items in seen.items():
        lines += ["", f"## {g}", "", "| Method | Path | What |", "|---|---|---|"]
        for m, p, doc in items:
            lines.append(f"| {m} | `{p}` | {doc.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    text = render(routes(SERVER.read_text()))
    if "--check" in sys.argv:
        if not DOC.exists() or DOC.read_text() != text:
            print("docs/api-routes.md is out of date: run python3 scripts/api_routes.py", file=sys.stderr)
            return 1
        return 0
    DOC.write_text(text)
    print(f"wrote {DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
