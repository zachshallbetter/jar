#!/usr/bin/env python3
"""Structured knowledge-base access behind the API's /v1/kb routes (agent_api_server.py _kb_cmd).

The KB is the retrieval runtime's source index (what kb_retrieve searches). Until 2026-09-28 this
read the legacy ingestion /v1/knowledge API, which the Rust ingestion runtime doesn't have, so the
Jellybean browser showed nothing and couldn't add or delete notes.

Run on the Titan host with the agent env sourced:

    set -a; . /ai/projects/titan-local-agent/agent.env; set +a
    python3 scripts/kb_query.py [--person ID --role ROLE] list
    python3 scripts/kb_query.py [--person ID --role ROLE] get <id>
    python3 scripts/kb_query.py [--person ID --role ROLE] delete <id>
    echo '{"domain":"household","title":"Plumber","content":"AquaFix","share":"household"}' | python3 scripts/kb_query.py create
    echo '{"to":"Robin"}' | python3 scripts/kb_query.py --person zach --role admin share <id>
    echo '{"from":"Robin"}' | python3 scripts/kb_query.py --person zach --role admin unshare <id>

Prints JSON: list → {"items": [{"id","title","category","tags","preview","body","owner","audience",
"visible_to","can_edit"}]}; get → one item; delete/create/share → {"ok": true, ...} or
{"error": "...", "status": 403|404}.

Who sees what (kb_privacy.py): the API passes the caller's --person and --role, and every command
acts only on items that person may see (an item they can't see is a 404, not a 403). Delete and
share need the owner or the admin. Without --person/--role (an operator on the host) it sees all.

Notes written by `titan kb import` (ids starting "hh-") belong to household.yaml: deleting one here
would be undone by the next import, so it is refused with a message saying to edit the file.
"""
import concurrent.futures as _cf
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as A  # noqa: E402
import kb_hygiene as H  # noqa: E402
import kb_privacy as P  # noqa: E402

FILE_OWNED = "hh-"
FILE_MSG = "This note comes from household.yaml — edit that file and run: titan kb import ~/.config/titan/household.yaml --apply"


def _category(src: dict) -> str:
    md = src.get("metadata") or {}
    return str(md.get("domain") or md.get("section") or src.get("source_type") or "").strip().lower()


def _chunks(sid: str) -> tuple[str, dict]:
    """(text, metadata) of one source, from its chunks (the source list carries no metadata)."""
    r = A._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(sid, safe='')}/chunks", "GET")
    chunks = sorted(r.get("chunks") or [], key=lambda c: c.get("chunk_index", 0))
    md = dict((chunks[0].get("metadata") or {}) if chunks else {})
    return "\n\n".join(c.get("chunk_text", "") for c in chunks), md


def _item(src: dict, body: str, md: dict, owner, aud: str, person, role) -> dict:
    body = (body or "").strip()
    tags = [t for t in (md.get("origin"), md.get("section")) if t]
    return {
        "id": src["source_id"],
        "title": src.get("title") or "(untitled)",
        "category": _category({**src, "metadata": md}),
        "tags": tags,
        "preview": body[:180].replace("\n", " "),
        "body": body[:8000],
        "owner": owner,
        "audience": aud,
        "visible_to": P.who_sees(owner, aud, person),
        "can_edit": P.can_edit(owner, aud, person, role),
        # When it was saved through titan (the household's date, YYYY-MM-DD), else a synced item's own date:
        # what "when did I tell you about …" answers from (#422/#423).
        "saved": str(md.get("saved") or "")[:10] or None,
        "observed_at": str(md.get("observed_at") or "")[:25] or None,
    }


def kb_list(person: str | None = None, role: str | None = None) -> list[dict]:
    sources = H.list_sources()
    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda s: _chunks(s["source_id"]), sources))
    rows = P.acl_rows(s["source_id"] for s in sources)
    items = []
    for src, (body, md) in zip(sources, got):
        owner, aud = P.acl_of(md, src["source_id"], rows)
        # fences too (#356): the KB browser's list carries each note's body
        if P.kb_visible(md, person, role, source_id=src["source_id"], rows=rows,
                        text=f"{src.get('title') or ''}\n{body}"):
            items.append(_item(src, body, md, owner, aud, person, role))
    return items


def _find(rid: str, person, role) -> tuple[dict | None, dict, str, dict | None]:
    """(source, metadata, text, error) for an item this person may see; a 404 error otherwise."""
    src = next((s for s in H.list_sources() if s["source_id"] == rid), None)
    if src is None:
        return None, {}, "", {"error": f"no entry {rid}", "status": 404}
    body, md = _chunks(rid)
    if not P.kb_visible(md, person, role, source_id=rid, text=f"{src.get('title') or ''}\n{body}"):
        return None, {}, "", {"error": f"no entry {rid}", "status": 404}
    return src, md, body, None


def kb_get(rid: str, person: str | None = None, role: str | None = None) -> dict:
    src, md, body, err = _find(rid, person, role)
    if err:
        return err
    owner, aud = P.acl_of(md, rid)
    return {"ok": True, "item": _item(src, body, md, owner, aud, person, role)}


def kb_delete(rid: str, person: str | None = None, role: str | None = None) -> dict:
    if rid.startswith(FILE_OWNED):
        return {"error": FILE_MSG, "status": 400}
    src, md, _body, err = _find(rid, person, role)
    if err:
        return err
    if not P.can_edit(*P.acl_of(md, rid), person, role):
        return {"error": "only the person who saved it (or the admin) can delete it", "status": 403}
    A._runtime_request("retrieval", f"/v1/retrieval/sources/{urllib.parse.quote(rid, safe='')}", "DELETE")
    P.drop_acl(rid)
    return {"ok": True}


def kb_create(domain: str, title: str, content: str, person: str | None = None, role: str | None = None,
              share: object = None) -> dict:
    title = (title or "").strip()
    if not title or not (content or "").strip():
        return {"error": "title and content are required"}
    owner = person or P.LEGACY_OWNER
    share = None if str(share or "").strip().lower() in ("", "private") else share
    add, why = (P.targets(share) if share else (None, None))
    if why:
        return {"error": why, "status": 400}
    aud = __import__("audience").widen("private", add) if add else "private"
    sid = P.scoped_id("note-", re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "untitled", owner)
    A._kb_index(sid, title, content.strip(), "note", {"origin": "jellybean", "domain": (domain or "").strip().lower()},
                owner=owner, audience=aud, pin="replace" if add else "keep")   # keep: a re-save never un-shares
    return {"ok": True, "id": sid, "audience": aud, "visible_to": P.who_sees(owner, aud, owner)}


def kb_share(rid: str, payload: dict, person: str | None, role: str | None, undo: bool = False) -> dict:
    src, md, _body, err = _find(rid, person, role)
    if err:
        return err
    raw = payload.get("from" if undo else "to")
    add = None
    if raw:
        add, why = P.targets(raw)
        if why:
            return {"error": why, "status": 400}
    elif not undo:
        return {"error": "say who to share it with: {\"to\": \"Robin\" | \"adults\" | \"household\"}", "status": 400}
    if undo:
        new, said = P.unshare(rid, md, add if isinstance(add, list) else None, person, role)
    else:
        new, said = P.share(rid, md, add, person, role, text=f"{src.get('title') or ''}\n{_body}")
    if new is None:
        return {"error": said, "status": 404 if said == "not found" else 403}
    owner, aud = P.acl_of(md, rid)
    return {"ok": True, "id": rid, "audience": aud, "visible_to": P.who_sees(owner, aud, person), "said": said}


def _identity(argv: list[str]) -> tuple[list[str], str | None, str | None]:
    person = role = None
    rest = []
    it = iter(argv)
    for a in it:
        if a == "--person":
            person = next(it, None) or None
        elif a == "--role":
            role = next(it, None) or None
        else:
            rest.append(a)
    if role == "household":
        person = None   # a shared device has no notes of its own
    return rest, person, role


def main() -> int:
    argv, person, role = _identity(sys.argv[1:])
    cmd = argv[0] if argv else "list"
    arg = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "list":
            print(json.dumps({"items": kb_list(person, role)}))
        elif cmd in ("get", "delete") and arg:
            out = (kb_get if cmd == "get" else kb_delete)(arg, person, role)
            print(json.dumps(out))
            return 0 if out.get("ok") else 1
        elif cmd in ("share", "unshare") and arg:
            out = kb_share(arg, json.loads(sys.stdin.read() or "{}"), person, role, undo=(cmd == "unshare"))
            print(json.dumps(out))
            return 0 if out.get("ok") else 1
        elif cmd == "create":
            p = json.load(sys.stdin)  # {domain, title, content, share?}
            out = kb_create(p.get("domain", ""), p.get("title", ""), p.get("content", ""), person, role,
                            p.get("share") or p.get("audience"))
            print(json.dumps(out))
            return 0 if out.get("ok") else 1
        else:
            print(json.dumps({"error": f"unknown command: {cmd}"}))
            return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc), "status": 500}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
