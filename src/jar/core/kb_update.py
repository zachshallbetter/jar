#!/usr/bin/env python3
"""Update a knowledge-base note's title / body / category from the Jellybean KB browser.

Re-indexes the note in the retrieval runtime under the same id (an upsert), keeping its type and
metadata. Until 2026-09-28 this PATCHed the legacy ingestion /v1/knowledge API, which no longer exists.

Usage (JSON {title?, body?, domain?} on stdin):
    echo '{"body":"# New content"}' | python3 kb_update.py [--person ID --role ROLE] <note_id>

Who may edit (kb_privacy.py): the note's owner, or the admin. A note the caller can't see is "no note"
(the API answers 404); one they can see but don't own is refused (403). The note keeps its owner and
audience (and its other metadata, read from its chunks: the source list carries none).

Notes from household.yaml (ids starting "hh-") are refused: the next `titan kb import` would put the
file's version back, so the edit belongs in the file.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_query import FILE_MSG, FILE_OWNED, _chunks, _identity  # noqa: E402  (first: kb_hygiene also edits sys.path)
import agent_runtime_lib as A  # noqa: E402
import kb_hygiene as H  # noqa: E402
import kb_privacy as P  # noqa: E402


def main() -> int:
    argv, person, role = _identity(sys.argv[1:])
    rid = argv[0] if argv else ""
    if not rid:
        print(json.dumps({"error": "note id required"})); return 2
    if rid.startswith(FILE_OWNED):
        print(json.dumps({"error": FILE_MSG})); return 1
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"bad json: {exc}"})); return 2
    try:
        src = next((s for s in H.list_sources() if s["source_id"] == rid), None)
        text, md = _chunks(rid) if src is not None else ("", {})
        owner, aud = P.acl_of(md, rid)
        if src is None or not P.visible_acl(owner, aud, person, role):
            print(json.dumps({"error": f"no note with id {rid}", "status": 404})); return 1
        if not P.can_edit(owner, aud, person, role):
            print(json.dumps({"error": "only the person who saved it (or the admin) can edit it", "status": 403})); return 1
        title = payload["title"] if isinstance(payload.get("title"), str) and payload["title"].strip() else src.get("title", "")
        body = payload["body"] if isinstance(payload.get("body"), str) else text
        if isinstance(payload.get("domain"), str):
            md["domain"] = payload["domain"].strip().lower()
        if not body.strip():
            print(json.dumps({"error": "body can't be empty"})); return 1
        A._kb_index(rid, title, body, src.get("source_type") or "note", md, owner=owner, audience=aud)
        print(json.dumps({"ok": True, "id": rid}))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
