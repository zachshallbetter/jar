#!/usr/bin/env python3
# gate: hermetic
"""Mail and scans as references plus derived facts (titan-node#446 step 2): the flag off keeps today's KB path; on, a
new item writes a reference (never its body) and its derived records, its text waits only for the derivation passes
and then goes; an item already in the KB stays on its path; the world model's facts outlive the text.
Run: python3 scripts/test_item_refs.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-itemrefs-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "CONNECTORS_LEDGER": "ledger.db", "REFERENCES_DB": "references.db",
             "ITEM_REFS_DB": "item_refs.db", "AGENT_MEMORY_DIR": "memory", "KB_ACL_DB": "kb_acl.db",
             "HOUSEHOLD_PREFS": "household.json", "MAIL_BILLS_DB": "mail_bills.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("ITEM_REFERENCES", None)
sys.path.insert(0, str(Path(__file__).parent))
import connectors_runner as cx  # noqa: E402
import item_refs  # noqa: E402
import references  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")


calls: list[tuple[str, str, str, dict | None]] = []


def runtime(service, path, method, payload):
    calls.append((service, path, method, payload))
    return {}


def kb_writes(source_id=None):
    return [c for c in calls if c[1] == "/v1/retrieval/index" and (source_id is None or c[3]["source_id"] == source_id)]


MAIL = {"source_id": "m-1", "observed_at": "2026-10-03T08:00:00Z", "kind": "email", "title": "Your PG&E bill",
        "text": "Your PG&E bill of $84.10 is due Oct 12. Call (509) 555-0142 with questions.",
        "fields": {"vendor": "PG&E"}, "timeline": "A PG&E bill arrived"}

print("── off (the default): today's path ──")
check("ITEM_REFERENCES unset: off for mail and scans", (item_refs.enabled("mail"), item_refs.enabled("scans")), (False, False))
cx.ingest("mail", [MAIL], runtime=runtime, owner="zach", device_id="dev-mac")
iid = cx.item_id("mail", "m-1")
check("the body goes to the KB as before", len(kb_writes(iid)), 1)
check("no reference", references.visible("zach", "admin"), [])

print("── on: a new mail is a reference plus derived facts ──")
os.environ["ITEM_REFERENCES"] = "mail,scans"
calls.clear()
new = {**MAIL, "source_id": "m-2", "title": "Your water bill"}
st = cx.ingest("mail", [new], runtime=runtime, owner="zach", device_id="dev-mac")
iid2 = cx.item_id("mail", "m-2")
check("written, with no KB body", (st["written"], kb_writes(iid2)), (1, []))
refs = references.visible("zach", "admin")
check("one reference: whose, which device, the device's id, a hash, a title, a date",
      [(r["kind"], r["native_id"], r["device_id"], r["title"], r["dates"], r["content_hash"][:7]) for r in refs],
      [("mail", "m-2", "dev-mac", "Your water bill", {"received": "2026-10-03T08:00:00Z"}, "sha256:")])
ok_nobody = all("$84" not in json.dumps(r) and "555-0142" not in json.dumps(r) for r in refs)
check("…and nothing of the body in it", ok_nobody, True)
import hashlib  # noqa: E402
check("its hash is the synced text's, as sent (the Mac can rebuild it)", refs[0]["content_hash"],
      "sha256:" + hashlib.sha256(new["text"].encode()).hexdigest())
node = next(c for c in calls if c[1] == "/v1/nodes" and c[3]["node_id"] == iid2)
check("the derived record stays, citing the reference", (node[3]["data"]["vendor"], node[3]["data"]["provenance"]["reference"]),
      ("PG&E", refs[0]["id"]))
check("the timeline line stays", any(c[1] == "/v1/events" or "event" in c[1] for c in calls), True)
check("the text waits for the derivation passes (redacted, 0600)",
      (item_refs.pending_text(iid2) is not None, oct(os.stat(_TMP / "item_refs.db").st_mode & 0o777)), (True, "0o600"))
ps = item_refs.pending_sources("mail")
check("a pending item reads like a KB source, with its owner and reference",
      [(p["source_id"], p["source_type"], p["metadata"]["owner"], p["metadata"]["reference"]) for p in ps],
      [(iid2, "connector:mail", "zach", refs[0]["id"])])
read = item_refs.text_of(lambda sid: "KB TEXT")
check("a pass reads pending text first, the KB's otherwise", (read(iid2)[:9], read("other")), ("Your PG&E", "KB TEXT"))

print("── an item already in the KB stays on its path until the migration (step 4) ──")
calls.clear()
cx.ingest("mail", [{**MAIL, "text": MAIL["text"] + " (updated)"}], runtime=runtime, owner="zach", device_id="dev-mac")
check("a changed m-1 is re-indexed in the KB, not turned into a reference", (len(kb_writes(iid)), item_refs.is_reference(iid)),
      (1, False))
check("…so one item is never both", len(references.visible("zach", "admin")), 1)

print("── the derivation passes, then the text goes ──")
check("bills only: it waits", (item_refs.mark("bills", [iid2]), item_refs.pending_text(iid2) is not None), (0, True))
check("…and suggestions: it still waits for the world model", (item_refs.mark("suggestions", [iid2]),
      item_refs.pending_text(iid2) is not None), (0, True))
check("…and the world model: gone", (item_refs.mark("world", [iid2]), item_refs.pending_text(iid2)), (1, None))
check("its reference and derived row stay", (len(references.visible("zach", "admin")), item_refs.is_reference(iid2)), (1, True))
os.environ["MAIL_SUGGESTIONS"] = "0"
check("suggestions off: not waited for", item_refs.required("mail"), {"bills", "world"})
os.environ.pop("MAIL_SUGGESTIONS")
cx.ingest("scans", [{"source_id": "s-1", "observed_at": "2026-10-01", "kind": "scan", "title": "Lease",
                     "text": "Lease renewal for unit 4"}], runtime=runtime, owner="zach", device_id="dev-mac")
sid = cx.item_id("scans", "s-1")
check("a scan waits for the world model only", (item_refs.required("scans"), item_refs.mark("world", [sid])), ({"world"}, 1))
cx.ingest("mail", [{**MAIL, "source_id": "m-3"}], runtime=runtime, owner="zach", device_id="dev-mac")
iid3 = cx.item_id("mail", "m-3")
check("unread for longer than the TTL: gone anyway", (item_refs.sweep(now=time.time() + 49 * 3600),
                                                      item_refs.pending_text(iid3)), (1, None))

print("── the world model's facts outlive the text ──")
import world as W  # noqa: E402
cx.ingest("mail", [{**MAIL, "source_id": "m-4"}], runtime=runtime, owner="zach", device_id="dev-mac")
iid4 = cx.item_id("mail", "m-4")
fake = W.Obs(entity="pg-e", etype="org", name="PG&E", attr="phone", value="(509) 555-0142",
             observed_at="2026-10-03", kind="mail", ref=iid4, origin="mail:pg-e")
saved = W.note_observations
W.note_observations = lambda w, rows, roles, mail=False: [fake] if any(r[0] == iid4 for r in rows) else []
try:
    into: dict = {}
    n = W.reference_obs(object(), {}, into)
    check("read once from the pending text, then marked", (n, [o.value for o in into.get("mail", [])]), (1, ["(509) 555-0142"]))
    item_refs.mark("bills", [iid4]); item_refs.mark("suggestions", [iid4])
    check("…every pass has read it: the text is gone", item_refs.pending_text(iid4), None)
    into = {}
    W.reference_obs(object(), {}, into)
    check("the next night: the kept observation still comes, without the text",
          [(o.ref, o.value) for o in into.get("mail", [])], [(iid4, "(509) 555-0142")])
finally:
    W.note_observations = saved

print("── a pending mail's owner reaches its bill (mail_bills.with_meta) ──")
import mail_bills  # noqa: E402
got = mail_bills.with_meta([{"source_id": "x-1", "source_type": "connector:mail", "metadata": {"owner": "p_ab12cd34"}}],
                           acl={})
check("the pending item's own owner, not the admin", got[0]["metadata"]["owner"], "p_ab12cd34")

print("── a deleted item: its reference and text go ──")
cx.ingest("mail", [{**MAIL, "source_id": "m-5"}], runtime=runtime, owner="zach", device_id="dev-mac")
iid5 = cx.item_id("mail", "m-5")
before = len(references.visible("zach", "admin"))
cx.ingest("mail", [{"source_id": "m-5", "deleted": True}], runtime=runtime, owner="zach", device_id="dev-mac")
check("deleted: one fewer reference, no derived row, no text",
      (len(references.visible("zach", "admin")), item_refs.is_reference(iid5), item_refs.pending_text(iid5)),
      (before - 1, False, None))

print("── the dry run reads only ──")
os.environ.pop("ITEM_REFERENCES", None)
led = _TMP / "ledger.db"
size = led.stat().st_size
rep = item_refs.dry_run(7, ledger=led, kb_text=lambda sid: "x" * 100, bills_seen=lambda: {iid: "bill-1"})
check("per source: items, references, what's kept", (rep["mail"]["items"] >= 1, sorted(rep["mail"]["kept"]),
      rep["scans"]["items"]), (True, ["bill or receipt", "fields node", "timeline line", "world observations"], 1))
check("…and nothing written", led.stat().st_size, size)

cx.ingest("mail", [{**MAIL, "source_id": "m-6"}], runtime=runtime, owner="zach", device_id="dev-mac")
iid6 = cx.item_id("mail", "m-6")
cx.forget("mail", apply=True, runtime=runtime, owner="zach")
check("forgetting the source: its references, derived rows and text go",
      (references.visible("zach", "admin", kind="mail"), item_refs.is_reference(iid6), item_refs.pending_text(iid6)),
      ([], False, None))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
