#!/usr/bin/env python3
"""One tool for finding files: `files` with a mode (#1251 decision C, Zach 10-08).

The box's local 8B picked among three file tools and often took the wrong one (turn_eval find-content-path and
find-content-capped: file_list or find_files where the question needed search_file_contents). The model now sees one
tool, `files`, and says which kind of look it wants:

  mode      runs (unchanged, under its own name)   where
  contents  search_file_contents                    the asker's Mac: text inside files
  name      find_files                              the asker's Mac: files by name
  titan     file_list                               titan: a folder's listing

What this never does:
  - decide who may read what. A `files` call becomes the old tool's call at the one chokepoint
    (agent_runtime_lib._execute_tool_checked) before any check runs, so each mode goes through exactly the checks its
    old tool had: the role table (file_list is the admin's; a kid has none of the three), the device bridge and
    cross-device routing to the asker's own Mac, the wrong-machine guard, the credential guard and another person's
    uploads (#713, #1071), the eval allowlist and fixtures.
  - change what the Mac sees. The device bridge still gets find_files and search_file_contents by those names, with
    those tools' own arguments; mapping happens on titan.
  - widen a turn's tool set. A `files` call is allowed in a narrowed turn (a tag, a task step, a background turn) only
    when its mode's old tool is in that turn's set.

The old names stay callable as hidden aliases for one release (existing turns, skills, scheduled actions and tests);
only `files` is declared to either brain.
"""
from __future__ import annotations

from typing import Any

MERGED = "files"
# mode → the tool it runs, in the order the description shows them (contents first: the miss this fixes)
MODES: dict[str, str] = {"contents": "search_file_contents", "name": "find_files", "titan": "file_list"}
ALIASES = frozenset(MODES.values())
MODE_OF = {tool: mode for mode, tool in MODES.items()}
# Words a model may use for a mode (the 8B guesses); anything else is asked again with the modes named.
_MODE_WORDS = {
    "contents": "contents", "content": "contents", "text": "contents", "inside": "contents", "grep": "contents",
    "search": "contents", "search_file_contents": "contents",
    "name": "name", "names": "name", "filename": "name", "filenames": "name", "find": "name", "find_files": "name",
    "titan": "titan", "list": "titan", "titan_list": "titan", "file_list": "titan",
}
# What each mode passes on: (its argument, the `files` arguments that fill it, first one set wins)
_ARGS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "contents": (("query", ("query",)), ("path", ("path", "folder")), ("glob", ("glob", "pattern")),
                 ("case_sensitive", ("case_sensitive",)), ("limit", ("limit",)), ("context", ("context",))),
    "name": (("query", ("query",)), ("folder", ("folder", "path"))),
    "titan": (("path", ("path", "folder")), ("pattern", ("pattern", "glob"))),
}

_MODE_LINES = {
    "contents": ("contents: search INSIDE text files on the user's Mac for some text (like grep: plain substring, no "
                 "regex): which file defines, mentions or contains something. Returns 'path:line: snippet' grouped by "
                 "file, newest first. Skips secret files (.env, keys, ~/.ssh…), binaries, node_modules/.git/build "
                 "folders and ~/Library. Without Spotlight it reads at most 20,000 files, so pass the folder the "
                 "question implies (e.g. ~/Projects/app); the result says what it searched and where it stopped."),
    "name": ("name: find files on the user's Mac by FILENAME: Spotlight when it's indexing, otherwise a walk of the "
             "common folders (~/Downloads, ~/Documents, ~/Desktop, ~/Projects…), which can miss files elsewhere. Up "
             "to 30 results, most relevant first. Give the filename or a distinctive part of it; for a resume try "
             "'resume' or 'CV'."),
    "titan": ("titan: list a folder ON TITAN, the household server (titan's own code, configs and logs; never the "
              "user's Mac or phone), filtered by an optional glob."),
}
_PARAM_TEXT = {
    "mode": None,   # built from the modes on offer
    "query": "contents/name: the text to find inside files, or part of the filename (one line)",
    "path": None,   # built from the modes on offer
    "glob": "Optional: only files whose name matches, e.g. *.swift or **/*.py",
    "case_sensitive": "Optional, contents: true for case-sensitive",
    "limit": "Optional, contents: max lines (default 50, max 200)",
    "context": "Optional, contents: 0-3 lines of context around each match (like grep -C)",
}
_PARAMS_FOR = {"contents": ("mode", "query", "path", "glob", "case_sensitive", "limit", "context"),
               "name": ("mode", "query", "path"),
               "titan": ("mode", "path", "glob")}


def modes_word(modes: list[str]) -> str:
    return " | ".join(modes)


def spec(modes: list[str]) -> dict[str, Any] | None:
    """The `files` entry for a turn that may use these modes (in MODES order), or None when it may use none. A mode is
    offered only when its old tool would have been: the description never names a look this asker can't make."""
    modes = [m for m in MODES if m in modes]
    if not modes:
        return None
    mac = [m for m in modes if m != "titan"]
    head = ("Find files and what's in them" + (" on the user's Mac" if mac and "titan" not in modes else "")
            + ". Pick the mode by what the question asks about: words INSIDE files (contents) or a file's NAME (name)"
            if mac else "List a folder on titan, the household server")
    desc = head + ". " + " ".join(f"mode={_MODE_LINES[m]}" for m in modes)   # one line: the tool index parses entries by line
    opener = (["read_file to open a Mac file"] if mac else []) + (["file_read to open a titan file"] if "titan" in modes else [])
    desc += " Use " + " and ".join(opener) + ". Never asks the user."
    where = []
    if "contents" in modes:
        where.append("contents: the folder or file to search, ~ allowed (default ~; narrow it for speed)")
    if "name" in modes:
        where.append("name: optional folder to search in")
    if "titan" in modes:
        where.append("titan: the folder on titan")
    params: dict[str, str] = {}
    for m in modes:
        for p in _PARAMS_FOR[m]:
            if p in params:
                continue
            if p == "mode":
                params[p] = modes_word(modes)
            elif p == "path":
                params[p] = "; ".join(where)
            else:
                params[p] = _PARAM_TEXT[p]
    if "query" in params and "titan" in modes and not mac:
        params.pop("query")
    return {"risk": "read", "description": desc, "parameters": params}


def resolve(args: dict[str, Any]) -> tuple[str | None, dict[str, Any], str | None]:
    """A `files` call as (the old tool, its arguments, None), or (None, {}, the reason) when the mode can't be told.
    Only the old tool's own arguments are passed on (never `mode`), so its call, its dedupe key and what the Mac gets
    are what the old tool's call would have been."""
    a = dict(args or {})
    if isinstance(a.get("args"), dict):   # the text protocol's {"tool": …, "args": {…}} wrapper
        a = {**{k: v for k, v in a.items() if k != "args"}, **a["args"]}
    raw = str(a.get("mode") or a.get("action") or "").strip().lower().replace("-", "_").replace(" ", "_")
    mode = _MODE_WORDS.get(raw)
    if not raw:
        # No mode: only an unmistakable shape is run (its old tool's own arguments); anything else is asked again.
        if a.get("query") and any(k in a for k in ("glob", "context", "case_sensitive")):
            mode = "contents"
        elif a.get("query") and "folder" in a:
            mode = "name"
        elif not a.get("query") and a.get("path") and "pattern" in a:
            mode = "titan"
    if mode is None:
        why = f"“{raw}” isn't a mode" if raw else "no mode was given"
        return None, {}, (f"Not run: {why}. Call files again with mode: contents (text inside files), name (files by "
                          "name) or titan (a folder on titan).")
    out: dict[str, Any] = {}
    for arg, sources in _ARGS[mode]:
        for s in sources:
            if s in a and a[s] not in (None, ""):
                out[arg] = a[s]
                break
    return MODES[mode], out, None


def shown(tool: str) -> str:
    """How a reply to the model names an old tool: the `files` mode it is now (the model never sees the old names)."""
    return f"files (mode {MODE_OF[tool]})" if tool in MODE_OF else tool
