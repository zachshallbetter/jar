#!/usr/bin/env python3
# gate: hermetic
"""open_item over server mail (#446 v2: the server is the store, its row is the reference) and the references
after-check rebuild_check calls (#776's verification items): a reference opens its body for its owner, for this
answer only; another adult never finds the owner's private server mail, and reaches a shared device reference only
through the owner's card; a kid and a shared screen never. Fictional people; stand-ins for the server and the Mac.
Run: python3 scripts/test_open_server_mail.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-open-server-mail-"))
for k, v in {"REFERENCES_DB": "references.db", "ITEM_REFS_DB": "item_refs.db", "HOUSEHOLD_DB": "household.db",
             "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import item_open as io  # noqa: E402
import references as R  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


PEOPLE = [{"id": "alex", "name": "Alex Quarry", "role": "admin"}, {"id": "robin", "name": "Robin Quarry", "role": "adult"},
          {"id": "ava", "name": "Ava Quarry", "role": "kid"}]
BY = {p["id"]: p for p in PEOPLE}
SRV = {"alex": [{"id": "cx-mailsrv-aaaa", "owner": "alex", "audience": "private", "device_id": "server:icloud",
                 "kind": "mail", "native_id": "<deck-2@mail.example>", "content_hash": None,
                 "title": "Re: Deck stain colours", "dates": {"received": "2026-10-06"},
                 "server": {"account": "icloud", "folder": "INBOX", "uidvalidity": 9, "uid": 22}}]}
READS: list[tuple] = []


def server_body(owner, ref, n):
    READS.append((owner, ref["id"]))
    return {"found": True, "text": "The grey one, Saturday works. Card 4111 1111 1111 1111"[:n]}


def open_as(asker, role, ref="cx-mailsrv-aaaa", body=server_body, cards=None):
    return io.open_item({"ref": ref}, asker=asker, role=role, asker_name=str(asker).title(), people=BY,
                        holders=lambda o: [], route=io.fixture_route({"asleep": True}), derived=lambda r: [],
                        wrap=lambda t, s, f: f"⟦untrusted source={s}⟧{t}⟦/untrusted⟧", add_card=cards,
                        server_refs=lambda a: SRV.get(a, []), server_body=body, consent_timeout=0.0)


print("── open_item over server mail ──")
r = open_as("alex", "admin")
check("the owner: read from their mail server now, for this answer only, wrapped as mail",
      ("read from your mail server just now for this answer only (titan keeps no copy)" in r,
       "⟦untrusted source=mail⟧" in r, "Saturday works" in r, READS[-1]), (True, True, True, ("alex", "cx-mailsrv-aaaa")))
check("…redacted again before the model sees it", "4111 1111 1111 1111" in r, False)
n = len(READS)
check("another adult: it doesn't exist for them (no read, no card)", (open_as("robin", "adult"), len(READS) - n),
      (io.NOT_FOUND, 0))
check("a kid: the same", open_as("ava", "kid"), io.NOT_FOUND)
R.put("robin", "adult", "mac-robin", {"kind": "mail", "native_id": "m-0@mail.example", "title": "School trip form",
                                       "content_hash": "sha256:" + "1" * 64, "audience": "household"})
asked: list = []
check("a kid can't open (or ask for) another person's household mail: refused before any card (#446 v2 §3)",
      (io.open_item({"what": "school trip"}, asker="ava", role="kid", asker_name="Ava", people=BY, holders=lambda o: [
          {"device_id": "mac-robin", "name": "Robin's Mac", "platform": "mac", "device_tools": ["item_body"]}],
          route=io.fixture_route({}), derived=lambda r: [], wrap=lambda t, s, f: t, add_card=lambda *a, **k: asked.append(1),
          consent_timeout=0.0), asked), (io.KID_OTHER, []))
check("a shared screen: never", open_as(None, "household"), io.HOUSEHOLD)
check("gone from the server: said, no stale copy", "no longer on your mail server" in open_as(
      "alex", "admin", body=lambda o, r, n: {"found": False}), True)
check("the server down: said", "mail server didn't answer" in open_as("alex", "admin", body=lambda o, r, n: {"error": "unreachable"}),
      True)
check("picked by its words too (what=)", "for this answer only" in io.open_item(
      {"what": "deck stain"}, asker="alex", role="admin", asker_name="Alex", people=BY, holders=lambda o: [],
      route=io.fixture_route({}), derived=lambda r: [], wrap=lambda t, s, f: t, server_refs=lambda a: SRV.get(a, []),
      server_body=server_body), True)

print("── the after-check (references.live_check) ──")
R.put("robin", "adult", "mac-robin", {"kind": "mail", "native_id": "m-1@mail.example", "title": "Hale Water bill",
                                       "content_hash": "sha256:" + "0" * 64, "audience": "household"})
lib._item_holders = lambda owner: [{"device_id": "mac-robin", "name": "Robin's Mac", "platform": "mac",
                                    "device_tools": ["item_body"]}] if owner == "robin" else []
lib._server_mail_body = lambda o, r, n: server_body(o, r, n)
import proactive  # noqa: E402
WRITTEN: list = []
proactive.add_card = lambda *a, **k: WRITTEN.append(1)   # the real card path: must never be reached by the check
got = R.live_check(sample=5, people=PEOPLE, server_refs=lambda a: SRV.get(a, []))
check("shape: per viewer, ints and fixed reason keys only", (sorted(got), sorted(got["owner"])),
      (sorted(R.LIVE_VIEWERS), ["card", "error_reasons", "errors", "opened", "refused", "tried"]))
check("the owner: server mail opened; their Mac's mail offline out of a turn (a reason, not a failure of privacy)",
      (got["owner"]["tried"], got["owner"]["opened"], got["owner"]["error_reasons"]), (3, 1, {"device_offline": 2}))
check("another adult: opened none; each shared device mail asked its owner (a card, counted, not written); server mail not found",
      (got["other_adult"]["opened"], got["other_adult"]["card"], got["other_adult"]["refused"]), (0, 2, 3))
check("a kid: opened none, no card, refused every time", (got["kid"]["opened"], got["kid"]["card"], got["kid"]["refused"],
                                                          got["kid"]["tried"]), (0, 0, 3, 3))
check("a shared screen: the same", (got["screen"]["opened"], got["screen"]["card"], got["screen"]["refused"]), (0, 0, 3))
check("dry: no card was written or pushed (the real card path never called)", WRITTEN, [])
check("the reasons only ever come from the fixed set", {k for v in got.values() for k in v["error_reasons"]} <= set(R.LIVE_REASONS),
      True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
