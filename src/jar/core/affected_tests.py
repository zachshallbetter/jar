#!/usr/bin/env python3
"""Which gate tests a change can affect (#1514): an import graph over scripts/.

    python3 scripts/affected_tests.py [--root DIR] [--dir scripts] PATH...      PATH: changed files, relative to DIR

Prints the names of the test files (test_*.py, one per line, sorted) that are affected by the changed paths:
  - a test that imports, directly or through any chain of modules, a changed .py file (every import counts, at
    any depth in a file, so an import inside a function loads the same module);
  - a changed test itself;
  - a test that names a changed file, whether .py or not (a .sh, .conf, .json, .timer, .md which tests read as text,
    or a script they run as a subprocess), by its repo path or its file name, in its own source.
It only orders a gate (run_deploy_gate.sh GATE_FIRST): the full plan still runs after these, so a miss here costs
speed, never coverage. It selects among files that exist and never adds a live or quarantined test: the gate keeps
only the names its own plan holds.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


def module_files(base: Path) -> dict[str, Path]:
    """Dotted module name -> file, for every .py under base (a package directory is its __init__.py)."""
    mods: dict[str, Path] = {}
    for p in sorted(base.rglob("*.py")):
        parts = list(p.relative_to(base).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            mods[".".join(parts)] = p
    return mods


def imports_of(path: Path, name: str, mods: dict[str, Path]) -> set[Path]:
    """The in-repo files this file imports (anywhere in it), each with its parent packages' __init__ files."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return set()
    here = name.split(".") if path.name == "__init__.py" else name.split(".")[:-1]
    wanted: set[str] = set()

    def add(dotted: str) -> None:   # the module, and each parent package that runs its __init__ first
        parts = dotted.split(".")
        for i in range(1, len(parts) + 1):
            wanted.add(".".join(parts[:i]))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(here) - (node.level - 1)
                if keep < 0:
                    continue
                base = ".".join(here[:keep] + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if base:
                add(base)
            for a in node.names:   # "from pkg import mod": mod may be a submodule
                sub = f"{base}.{a.name}" if base else a.name
                if a.name != "*" and sub in mods:
                    add(sub)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            fn = node.func
            called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if called in ("import_module", "__import__") and node.args[0].value in mods:
                add(node.args[0].value)
    return {mods[m] for m in wanted if m in mods and mods[m] != path}


def names_file(text: str, rel: str) -> bool:
    """Does this source name the changed file: by its repo path, or by its file name as a whole word."""
    if rel in text:
        return True
    base = rel.rsplit("/", 1)[-1]
    return re.search(r"(?<![\w.\-/])" + re.escape(base) + r"(?![\w\-])", text) is not None


def affected(root: Path, changed: list[str], scripts: str = "scripts") -> list[str]:
    base = root / scripts
    mods = module_files(base)
    by_path = {p.resolve(): p for p in mods.values()}
    rev: dict[Path, set[Path]] = {}   # file -> the files that import it
    for m, p in mods.items():
        for d in imports_of(p, m, mods):
            rev.setdefault(d, set()).add(p)
    seeds = {by_path[q] for q in ((root / c).resolve() for c in changed if c.endswith(".py")) if q in by_path}
    hit, todo = set(seeds), list(seeds)
    while todo:
        for p in rev.get(todo.pop(), ()):
            if p not in hit:
                hit.add(p)
                todo.append(p)
    tests = sorted(base.glob("test_*.py"))
    out = {p.name for p in tests if p in hit}
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in tests}
    for c in changed:
        out.update(p.name for p, t in texts.items() if names_file(t, c))
    return sorted(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Print the gate tests a change can affect")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--dir", default="scripts", help="the directory under root holding the modules and tests")
    ap.add_argument("paths", nargs="*", help="changed files, relative to root")
    a = ap.parse_args(argv)
    for name in affected(Path(a.root).resolve(), a.paths, a.dir):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
