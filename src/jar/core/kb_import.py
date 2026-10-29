#!/usr/bin/env python3
"""Load the household facts file (household.yaml) into the knowledge base as stable notes.

The file lives on the Mac, outside git (~/.config/titan/household.yaml, template in
docs/household.example.yaml). Each entry becomes one note with a stable id
(hh-<section>-<name>), so editing the file updates notes instead of adding duplicates, and
deleting an entry deletes its note. Only notes this loader owns (id prefix "hh-") are ever
touched.

Secrets (codes, passwords, PINs, account numbers) are left OUT of the knowledge base unless you
pass --include-secrets: the Gemini brain's masking only catches keyword + value-with-a-digit, so a
password like "maplesky" would reach Gemini in a KB passage. Without the flag the note records
that the value exists and where it is kept (e.g. `kept_in: 1Password`).

  titan kb import ~/.config/titan/household.yaml              # dry run: what would change
  titan kb import ~/.config/titan/household.yaml --apply      # back up, then write
  python3 scripts/kb_import.py - --apply < household.yaml     # on the host (agent env sourced)

Dry runs print titles and field names only, never values.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

PREFIX = "hh-"
SOURCE_TYPE = "household"
BACKUP_DIR = Path(os.environ.get("KB_HYGIENE_BACKUP_DIR", "/ai/backups"))
SECRET_KEYS = re.compile(r"^(code|password|passcode|passphrase|pin|combination|account(_number)?|routing_number|"
                         r"\w+_(code|password|pin))$")

# section -> how to title an entry (first key present wins) and an optional label for the title
TITLES = {
    "people": ("name", None), "pets": ("name", "Pet"), "contacts": ("name", None), "utilities": ("provider", None),
    "schedules": ("name", None), "access": ("name", None), "network": ("name", "Wi-Fi"), "vehicles": ("name", "Vehicle"),
    "appliances": ("name", None), "dates": ("name", "Date"), "notes": ("title", None),
}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:48] or "entry"


def label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def is_secret(key: str) -> bool:
    return bool(SECRET_KEYS.match(key.lower()))


def blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)


def entry_title(section: str, e: dict) -> str | None:
    key, prefix = TITLES.get(section, ("name", None))
    name = e.get(key) or e.get("name") or e.get("title")
    if blank(name):
        return None
    if section == "contacts" and not blank(e.get("role")):
        return f"{str(e['role']).strip().capitalize()}: {name}"
    if section == "utilities" and not blank(e.get("service")):
        return f"{str(e['service']).strip().capitalize()}: {name}"
    if section == "people" and not blank(e.get("relation")):
        return f"{name} ({e['relation']})"
    return f"{prefix}: {name}" if prefix else str(name)


def fmt(v) -> str:
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, list):
        return ", ".join(fmt(x) for x in v if not blank(x))
    if isinstance(v, dict):
        return "; ".join(f"{label(k)}: {fmt(x)}" for k, x in v.items() if not blank(x))
    return str(v).strip()


def render(section: str, e: dict, include_secrets: bool, today: str) -> tuple[str, str, bool, list[str]]:
    """-> (title, text, has_secret_in_text, fields) for one entry."""
    title = entry_title(section, e)
    if not title:
        return None, "", False, []
    lines, fields, secret_in_text = [title], [], False
    for k, v in e.items():
        if blank(v) or k in ("title",):
            continue
        fields.append(k)
        if is_secret(k):
            if include_secrets:
                # "<Name> <keyword>: value" keeps the keyword next to the value, which is what the
                # Gemini-bound mask looks for.
                lines.append(f"{label(k)}: {fmt(v)}")
                secret_in_text = True
            else:
                lines.append(f"{label(k)}: kept off the knowledge base"
                             + (f" (in {e['kept_in']})" if not blank(e.get("kept_in")) else ""))
            continue
        if k == "kept_in":
            continue
        lines.append(f"{label(k)}: {fmt(v)}")
    lines.append(f"Source: household.yaml ({section}), imported {today}")
    return title, "\n".join(lines), secret_in_text, fields


def build(doc: dict, include_secrets: bool) -> tuple[dict[str, dict], list[str]]:
    today = time.strftime("%Y-%m-%d")
    notes, problems, seen = {}, [], set()
    for section, entries in (doc or {}).items():
        if blank(entries):
            continue
        if not isinstance(entries, list):
            problems.append(f"{section}: expected a list of entries")
            continue
        for i, e in enumerate(entries):
            if not isinstance(e, dict) or all(blank(v) for v in e.values()):
                continue
            title, text, secret, fields = render(section, e, include_secrets, today)
            if not title:
                problems.append(f"{section}[{i}]: no name/title — skipped")
                continue
            sid = f"{PREFIX}{slug(section)}-{slug(title)}"
            n = 2
            while sid in seen:
                sid = f"{PREFIX}{slug(section)}-{slug(title)}-{n}"
                n += 1
            seen.add(sid)
            notes[sid] = {"title": title, "text": text, "section": section, "fields": fields,
                          "metadata": {"origin": "household.yaml", "section": section, "sensitive": secret}}
    return notes, problems


def main() -> int:
    ap = argparse.ArgumentParser(description="load household.yaml into the knowledge base")
    ap.add_argument("file", help="path to household.yaml, or - for stdin")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--include-secrets", action="store_true", help="store codes/passwords in note text")
    a = ap.parse_args()
    raw = sys.stdin.read() if a.file == "-" else Path(a.file).expanduser().read_text()
    doc = yaml.safe_load(raw) or {}
    notes, problems = build(doc, a.include_secrets)

    import agent_runtime_lib as lib   # after parsing, so a YAML error fails fast without runtime setup
    import kb_hygiene as kh
    existing = {s["source_id"]: s for s in kh.list_sources() if s["source_id"].startswith(PREFIX)}
    current = {sid: kh.source_text(sid) for sid in existing}
    adds = [sid for sid in notes if sid not in existing]
    updates = [sid for sid in notes if sid in existing and _norm(current[sid]) != _norm(notes[sid]["text"])]
    removes = [sid for sid in existing if sid not in notes]
    same = len(notes) - len(adds) - len(updates)

    for p in problems:
        print(f"  ! {p}")
    for tag, ids, src in (("ADD", adds, notes), ("UPDATE", updates, notes), ("REMOVE", removes, existing)):
        for sid in ids:
            n = src[sid]
            extra = f"  [{', '.join(n['fields'])}]" if tag != "REMOVE" else ""
            print(f"  {tag:6} {n['title']}{extra}")
    held = sum(1 for n in notes.values() for f in n["fields"] if is_secret(f))
    print(f"{len(notes)} entries · {len(adds)} new · {len(updates)} changed · {same} unchanged · {len(removes)} removed"
          + (f" · {held} secret field(s) {'stored' if a.include_secrets else 'kept out'}" if held else ""))
    if not a.apply:
        print("dry run — nothing written. Re-run with --apply.")
        return 0
    if updates or removes:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        bk = BACKUP_DIR / f"kb-import-{time.strftime('%Y%m%d-%H%M%SZ', time.gmtime())}.jsonl"
        with open(bk, "w") as fh:
            for sid in updates + removes:
                fh.write(json.dumps({**existing[sid], "text": current[sid]}) + "\n")
        os.chmod(bk, 0o640)
        print(f"backed up {len(updates) + len(removes)} note(s) to {bk} (restore: kb_hygiene.py restore --backup {bk})")
    for sid in adds + updates:
        n = notes[sid]
        # household.yaml is for everyone, kids and shared devices too (kb_privacy.py).
        lib._kb_index(sid, n["title"], n["text"], SOURCE_TYPE, n["metadata"], owner=None,
                      audience="household", pin="keep")
    for sid in removes:
        lib._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}", "DELETE")
    print(f"wrote {len(adds) + len(updates)}, removed {len(removes)}")
    return 0


def _norm(t: str) -> str:
    # ignore the "imported <date>" line so re-running on an unchanged file changes nothing
    return re.sub(r"imported \d{4}-\d{2}-\d{2}", "", re.sub(r"\s+", " ", t or "")).strip()


if __name__ == "__main__":
    raise SystemExit(main())
