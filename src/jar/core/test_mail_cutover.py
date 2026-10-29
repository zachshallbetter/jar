#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The mail cut-over (titan-node#955 step 6, gaps 3 and 4). Gap 3: with MAIL_SERVER_REPLACES_MAC=1, the Mac's mail for an
account the server connector reads ("mac_account") is skipped at ingest, counted as replaced; other accounts still come.
Gap 4: retiring the Mac's KB copies of mails whose server twin is in the KB, a dry run by default; --apply needs
MAIL_SERVER_RETIRE=1 too; a 0600 backup first (on the shred line); only twin-backed items go; bill records repoint.
Temp stores, stubbed KB and record runtime, fictional mail.
Run: python3 scripts/test_mail_cutover.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-cutover-"))
os.environ.update({"MAIL_SERVER": "1", "HOUSEHOLD_DB": str(_TMP / "household.db"), "CONNECTORS_LEDGER": str(_TMP / "ledger.db"),
                   "MAIL_SERVER_DB": str(_TMP / "mail.db"), "KB_LIFECYCLE_BACKUP_DIR": str(_TMP / "backups"),
                   "MAIL_ACCOUNTS_DIR": str(_TMP / "accounts"), "MAIL_TRIAGE": "0", "LINKS_DB": str(_TMP / "links.db"),
                   "LINK_SALT_FILE": str(_TMP / "link_salt")})
for k in ("MAIL_SERVER_REPLACES_MAC", "MAIL_SERVER_RETIRE", "ITEM_REFERENCES"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))

import os as _os_acl, tempfile as _tf_acl  # noqa: E402
_os_acl.environ.setdefault("KB_ACL_DB", _os_acl.path.join(_tf_acl.mkdtemp(prefix="titan-acl-"), "kb_acl.db"))  # #1273: ingest writes sharing rows; never titan's own
import connectors_runner as R  # noqa: E402
import mail_cutover as C  # noqa: E402
import mail_server as M  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


ACCTS = Path(os.environ["MAIL_ACCOUNTS_DIR"])
ACCTS.mkdir(mode=0o700)
f = ACCTS / "icloud.json"
f.write_text(json.dumps({"owner": "zach", "provider": "icloud", "user": "alex@mail.example", "password": "x",
                         "mac_account": "iCloud"}))
os.chmod(f, 0o600)
ROLES = {"zach": "admin", "robin": "adult"}
M._role_of = ROLES.get
CALLS: list = []


def runtime(service, path, method, payload):
    CALLS.append((service, method, path))
    return {}


def items(account, n, prefix):
    return [{"source_id": f"{prefix}{i}@{account.lower()}.example", "title": f"{account} mail {i}", "text": f"Hello {i}",
             "account": account, "observed_at": "2026-10-01T09:00:00Z", "kind": "mail"} for i in range(n)]


print("── gap 3: the Mac's copy of a connected account is skipped at ingest ──")
st = R.ingest("mail", items("iCloud", 2, "a") + items("Gmail", 1, "g"), runtime=runtime)
check("off: everything comes in as before", (st["written"], st.get("replaced", 0)), (3, 0))
os.environ["MAIL_SERVER_REPLACES_MAC"] = "1"
st = R.ingest("mail", items("iCloud", 2, "b") + items("Gmail", 1, "h"), runtime=runtime)
check("on: the iCloud mails are replaced, Gmail-on-the-Mac still comes", (st["written"], st["replaced"]), (1, 2))
st = R.ingest("mail", items("icloud ", 1, "c"), runtime=runtime)
check("…the account name matches however it's spaced or cased", st["replaced"], 1)
st = R.ingest("mail", items("iCloud", 1, "d"), runtime=runtime, owner="robin")
check("…only for the account's own owner (Robin's iCloud on her Mac isn't Zach's)", (st["written"], st.get("replaced", 0)), (1, 0))
st = R.ingest("calendar", [{"source_id": "e1", "title": "x", "text": "y", "account": "iCloud"}], runtime=runtime)
check("…and only mail", st.get("replaced", 0), 0)
os.environ.pop("MAIL_SERVER_REPLACES_MAC")

print("\n── gap 4: retiring the Mac's KB twins ──")
L = R._db()
L.execute("DELETE FROM items")
mac = {}
for i, (up, acct, kb) in enumerate((("t1@hale.example", "iCloud", 1), ("t2@hale.example", "iCloud", 1),
                                    ("solo@hale.example", "iCloud", 1), ("t3@hale.example", "iCloud", 0),
                                    ("g1@gmail.example", "Gmail", 1))):
    iid = R.item_id("mail", up)
    mac[up] = iid
    L.execute("INSERT INTO items (source, source_id, content_hash, item_id, kb, node, event_id, account, observed_at, "
              "synced_at, deleted_at, owner, upstream_id) VALUES ('mail',?,?,?,?,0,NULL,?,NULL,0,NULL,'zach',?)",
              (up, "h", iid, kb, acct, up))
L.commit()
L.close()
S = M._db()
for uid, (mid, kb) in enumerate((("<t1@hale.example>", 1), ("<T2@hale.example>", 1), ("<t3@hale.example>", 1),
                                 ("<s9@hale.example>", 0)), 1):
    S.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, kb) "
              "VALUES ('icloud','INBOX',1,?,'zach',?,?,?,?)", (uid, M.item_id("zach", "icloud", mid), mid, mid, kb))
S.commit()
S.close()
KB = {iid: {"source_id": iid, "title": f"Mac copy {up}", "source_type": "connector:mail", "metadata": {"owner": "zach"},
            "text": f"the whole mail {up}"} for up, iid in mac.items()}
DELETED, PATCHED = [], []
RECORDS = {"bill": [{"node_id": "bill-hale-1", "data": {"vendor": "Hale Water", "provenance": {"source_id": mac["t1@hale.example"]}}},
                    {"node_id": "bill-other", "data": {"vendor": "Other", "provenance": {"source_id": "cx-mail-elsewhere"}}}],
           "receipt": [], "statement": []}
KW = dict(kb_source=KB.get, kb_delete=DELETED.append, records=lambda t: RECORDS.get(t, []),
          patch=lambda nid, data: PATCHED.append((nid, data)))
dry = C.retire("zach", "icloud", **KW)
check("dry run: counts only; only twin-backed Mac KB mails would go",
      {k: dry[k] for k in ("mac_in_kb", "with_twin", "without_twin", "records_to_repoint", "apply")},
      {"mac_in_kb": 3, "with_twin": 2, "without_twin": 1, "records_to_repoint": 1, "apply": False})
check("…and nothing changed", (DELETED, PATCHED, list((_TMP / "backups").glob("*")) if (_TMP / "backups").exists() else []),
      ([], [], []))
check("…no id or title in what it prints", any("hale" in json.dumps(v) for v in dry.values()), False)
try:
    C.retire("zach", "icloud", apply=True, **KW)
    check("--apply without the second key: refused", "retired", "refused")
except C.CutoverError:
    check("--apply without MAIL_SERVER_RETIRE=1: refused, nothing changed", (DELETED, PATCHED), ([], []))
os.environ["MAIL_SERVER_RETIRE"] = "1"
got = C.retire("zach", "icloud", apply=True, now=1_791_216_000.0, **KW)
check("applied: the two twins left the KB", (got["retired"], sorted(DELETED)), (2, sorted([mac["t1@hale.example"], mac["t2@hale.example"]])))
check("…the Mac mail without a twin, the one not in the KB, and Gmail stay", any(mac[u] in DELETED for u in
      ("solo@hale.example", "t3@hale.example", "g1@gmail.example")), False)
backup = _TMP / "backups" / got["backup"]
lines = [json.loads(x) for x in backup.read_text().splitlines()]
check("the backup came first: whole items, 0600, on the shred line",
      (sorted(x["source_id"] for x in lines), lines[0]["text"].startswith("the whole mail"), oct(backup.stat().st_mode & 0o777),
       got["backup"].startswith("kb-cleanup-")), (sorted(DELETED), True, "0o600", True))
import backup_retention as BR  # noqa: E402
check("…which backup_retention shreds at 30 days", any(backup.match(g) for g in BR.LIFECYCLE_DIR_PATTERNS), True)
L = sqlite3.connect(os.environ["CONNECTORS_LEDGER"])
row = L.execute("SELECT kb, content_hash, deleted_at FROM items WHERE item_id=?", (mac["t1@hale.example"],)).fetchone()
stones = L.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0]
L.close()
check("the ledger row stays, with no hash and no tombstone (turning the Mac's sync back on writes it again)",
      (row[0], row[1], row[2], stones), (0, "", None, 0))
check("the bill filed from the Mac copy now points at the server item (the old id kept)",
      [(n, d["provenance"]["source_id"], d["provenance"]["was"]) for n, d in PATCHED],
      [("bill-hale-1", M.item_id("zach", "icloud", "<t1@hale.example>"), mac["t1@hale.example"])])
try:
    C.retire("zach", "gmail", **KW)
    check("an account without mac_account: refused", "planned", "refused")
except C.CutoverError:
    check("an account that isn't connected with a mac_account: refused", True, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
