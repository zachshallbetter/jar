#!/usr/bin/env python3
"""Keep the runbook in the household KB, one note per `## ` section of docs/runbook.md (a long section as several, at
its sub-heads: parts(), titan-node#912), so the
agent can answer questions about titan itself ("how does the nightly backup work?") from the
current text, not from hand-written seed notes that drift.

Notes are titled "Titan runbook — <section>" and upserted by title. A note whose text hasn't
changed is left alone; a section that was removed from the runbook has its note deleted.
Sections that look like they contain a secret are skipped, never indexed.

It also keeps the household's own projects in the KB (#1613: JB called Jar "a sandbox script" and expanded FE as
"front-end"): one short note per file in docs/kb-projects/ (Jar, the Fundamental Engine, ASE, Jellybean, titan), the
file's "# " line its title. They are Zach's projects, so each note is the admin's own and private (owner
household_identity.ADMIN_PERSON_ID, audience "private"), and a plain note (source type "note", origin
"household_projects"), not one of titan's system notes: those are searched only for questions about titan itself
(zones.plan), and "what's FE?" isn't one. Ids are "hp-<file>"; only those are ever touched, and a file removed from the
folder has its note deleted. Edit the file, not the note: the next deploy writes the file's text back.

The deploy script runs this after a successful deploy. By hand, on titan:
    set -a; . /ai/projects/titan-local-agent/agent.env; set +a
    python3 /ai/projects/titan-node/scripts/kb_seed_runbook.py [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNBOOK = HERE.parent / "docs" / "runbook.md"
PREFIX = "Titan runbook — "
META = {"origin": "kb_seed_runbook.py", "domain": "titan", "tags": ["titan", "runbook"]}

# Values that must never land in the KB. Placeholders like <unguessable-topic> don't match.
_SECRET_RX = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}"
    r"|\bAIza[0-9A-Za-z_-]{30,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|ntfy\.sh/[A-Za-z0-9_-]{16,}"
    r"|\b(?:password|token|secret|api[_-]?key)\s*[=:]\s*['\"]?[A-Za-z0-9/+_-]{16,}", re.I)


def sections(text: str) -> list[tuple[str, str]]:
    """[(title, body)] for each `## ` section; text before the first heading is dropped."""
    out: list[tuple[str, str]] = []
    cur: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur is not None:
                out.append((cur, "\n".join(buf).strip()))
            cur, buf = line[3:].replace("`", "").strip(), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out.append((cur, "\n".join(buf).strip()))
    return [(PREFIX + t, b) for t, b in out if b]


PART_MAX = 1100   # about one retrieval chunk (the runtime cuts at 1200 characters)
_LEAD_RX = re.compile(r"^(?:#{3,6}\s+(?P<h>.+?)\s*$|(?:\d+\.\s+)?\*\*(?P<b>[^*\n]{2,80}?)\*\*)")


def parts(title: str, body: str) -> list[tuple[str, str]]:
    """A long section as notes that each fit one chunk (titan-node#912). Split at its sub-heads (### headings and
    bold-lead lines, "**GPU layout.**" or "5. **Runtimes are …**"), neighbours merged while they fit. Each part is
    titled "<section> — <sub-head>" and starts with that line, so a cited chunk carries its own context (10-04: a
    sentence about :8198 was judged unsupported because the service's name sat in another chunk of the section)."""
    if len(body) <= PART_MAX:
        return [(title, body)]
    pieces: list[tuple[str, list[str]]] = [("", [])]
    for line in body.splitlines():
        m = _LEAD_RX.match(line)
        if m and pieces[-1][1]:
            pieces.append(((m.group("h") or m.group("b") or "").strip().rstrip(".:"), [line]))
        else:
            if m and not pieces[-1][0]:
                pieces[-1] = ((m.group("h") or m.group("b") or "").strip().rstrip(".:"), pieces[-1][1])
            pieces[-1][1].append(line)
    merged: list[tuple[str, str]] = []
    for head, lines in pieces:
        text = "\n".join(lines).strip()
        if not text:
            continue
        if merged and len(merged[-1][1]) + len(text) + 2 <= PART_MAX:
            merged[-1] = (merged[-1][0], merged[-1][1] + "\n\n" + text)
        else:
            merged.append((head, text))
    # a piece still too long (no sub-heads inside): its paragraphs in groups that fit, each with the same heading
    fitted: list[tuple[str, str]] = []
    for head, text in merged:
        if len(text) <= PART_MAX:
            fitted.append((head, text))
            continue
        group = ""
        for para in [x.strip() for x in re.split(r"\n\s*\n", text) if x.strip()]:
            if group and len(group) + len(para) + 2 > PART_MAX:
                fitted.append((head, group))
                group = para
            else:
                group = f"{group}\n\n{para}" if group else para
        if group:
            fitted.append((head, group))
    merged = fitted
    out: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for head, text in merged:
        t = f"{title} — {head}" if head else title
        seen[t] = seen.get(t, 0) + 1
        if seen[t] > 1:
            t = f"{t} ({seen[t]})"
        out.append((t, f"{t[len(PREFIX):]}\n\n{text}"))
    return out


def notes(text: str) -> list[tuple[str, str]]:
    """Every runbook note the KB should hold: each `## ` section, a long one as its parts(). The one list the seeder
    writes and rule_records.py checks against (10-04: the check knew only sections and called 43 parts stray)."""
    return [p for t, b in sections(text) for p in parts(t, b)]


PROJECTS_DIR = HERE.parent / "docs" / "kb-projects"
PROJECT_ID_PREFIX = "hp-"
PROJECT_SOURCE_TYPE = "note"
PROJECT_AUDIENCE = "private"
PROJECT_META = {"origin": "household_projects", "domain": "projects", "tags": ["project"]}


def project_owner() -> str:
    """The admin person: the household's projects are Zach's own (#1613)."""
    import household_identity as hh
    return hh.ADMIN_PERSON_ID


def project_notes(folder: Path | None = None) -> list[dict]:
    """One write per docs/kb-projects/*.md, in name order: {sid, title, text, source_type, metadata, owner, audience}.
    The title is the file's first "# " line and the text the whole file with that line as plain words, so a cited
    chunk carries the project's name. A file without a "# " first line or a body is left out."""
    out: list[dict] = []
    owner = project_owner()
    for f in sorted(Path(folder or PROJECTS_DIR).glob("*.md")):
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        if not lines or not lines[0].startswith("# "):
            continue
        title = lines[0][2:].strip()
        body = "\n".join(lines[1:]).strip()
        if not title or not body:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", f.stem.lower()).strip("-")
        out.append({"sid": PROJECT_ID_PREFIX + slug, "title": title, "text": f"{title}\n\n{body}",
                    "source_type": PROJECT_SOURCE_TYPE, "metadata": {**PROJECT_META, "project": slug},
                    "owner": owner, "audience": PROJECT_AUDIENCE})
    return out


def plan_projects(notes_: list[dict], existing: set[str], texts: dict[str, str]) -> dict[str, list]:
    """Like plan(), for the project notes, keyed by id: {'write': [note], 'same': [sid], 'skip': [sid],
    'delete': [sid]}. existing: the KB's "hp-" ids; texts maps them to their indexed text."""
    p: dict[str, list] = {"write": [], "same": [], "skip": [], "delete": []}
    wanted = {n["sid"] for n in notes_}
    for n in notes_:
        if _SECRET_RX.search(n["text"]):
            p["skip"].append(n["sid"])
        elif n["sid"] in existing and _norm(texts.get(n["sid"], "")) == _norm(n["text"]):
            p["same"].append(n["sid"])
        else:
            p["write"].append(n)
    p["delete"] = sorted(s for s in existing if s.startswith(PROJECT_ID_PREFIX) and s not in wanted)
    return p


def _norm(t: str) -> str:
    """Whitespace-free, because the KB splits long notes into chunks mid-word and joining them
    back adds a break there ("de cision"), so a section would look changed on every deploy."""
    return "".join(t.split())


def plan(secs: list[tuple[str, str]], existing: dict[str, str], texts: dict[str, str]) -> dict[str, list]:
    """What to do: {'write': [(title, body)], 'same': [title], 'skip': [title], 'delete': [(sid, title)]}.
    existing maps title → source_id for current runbook notes; texts maps source_id → indexed text."""
    p: dict[str, list] = {"write": [], "same": [], "skip": [], "delete": []}
    wanted = set()
    for title, body in secs:
        wanted.add(title)
        if _SECRET_RX.search(body):
            p["skip"].append(title)
            continue
        sid = existing.get(title)
        if sid and _norm(texts.get(sid, "")) == _norm(body):
            p["same"].append(title)
        else:
            p["write"].append((title, body))
    p["delete"] = [(sid, t) for t, sid in existing.items() if t not in wanted]
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="one summary line")
    a = ap.parse_args()

    sys.path.insert(0, str(HERE))
    import agent_runtime_lib as lib
    import kb_hygiene

    secs = notes(RUNBOOK.read_text(encoding="utf-8"))
    sources = kb_hygiene.list_sources()
    existing = {s["title"]: s["source_id"] for s in sources if str(s.get("title", "")).startswith(PREFIX)}
    texts = {sid: kb_hygiene.source_text(sid) for sid in existing.values()}
    p = plan(secs, existing, texts)
    try:
        rev = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        rev = ""
    if not a.quiet:
        for t, _ in p["write"]:
            print(f"  write   {t}")
        for t in p["skip"]:
            print(f"  SKIP    {t} (looks like it contains a secret)")
        for _, t in p["delete"]:
            print(f"  delete  {t}")
    if not a.dry_run:
        for title, body in p["write"]:
            slug = re.sub(r"[^a-z0-9]+", "-", title[len(PREFIX):].lower()).strip("-")
            slug = (slug[:50] + "-" + hashlib.sha1(slug.encode()).hexdigest()[:6]) if len(slug) > 60 else (slug or "section")
            kb_hygiene.upsert_by_title(title, body, "knowledge_record", {**META, "rev": rev},
                                       "note-runbook-" + slug, existing=sources)
        for sid, _ in p["delete"]:
            lib._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}", "DELETE")

    # The household's projects (#1613): the admin's own private notes, by id.
    pn = project_notes()
    have = {s["source_id"] for s in sources if str(s.get("source_id", "")).startswith(PROJECT_ID_PREFIX)}
    pp = plan_projects(pn, have, {sid: kb_hygiene.source_text(sid) for sid in have})
    if not a.quiet:
        for n in pp["write"]:
            print(f"  write   {n['title']} ({n['sid']})")
        for sid in pp["skip"]:
            print(f"  SKIP    {sid} (looks like it contains a secret)")
        for sid in pp["delete"]:
            print(f"  delete  {sid}")
    if not a.dry_run:
        for n in pp["write"]:
            lib._kb_index(n["sid"], n["title"], n["text"], n["source_type"], {**n["metadata"], "rev": rev},
                          owner=n["owner"], audience=n["audience"], pin="keep")
        for sid in pp["delete"]:
            lib._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}", "DELETE")
    print(f"runbook KB: {len(p['write'])} written, {len(p['same'])} unchanged, {len(p['delete'])} deleted, "
          f"{len(p['skip'])} skipped; projects: {len(pp['write'])} written, {len(pp['same'])} unchanged, "
          f"{len(pp['delete'])} deleted, {len(pp['skip'])} skipped{' (dry run)' if a.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
