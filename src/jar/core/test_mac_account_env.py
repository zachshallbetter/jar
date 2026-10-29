#!/usr/bin/env python3
# gate: hermetic   (temp account files, ledger and store; nothing connects)
"""#955 cut-over without touching a person's 0600 credentials file (Coordination, 10-09): which Mac mail account a server
account replaces comes from MAIL_SERVER_MAC_ACCOUNT (not a secret: the Mac's account name as titan's ledger already
carries it), and `cutover-check` refuses the cut-over unless each server account names exactly one Mac account the ledger
has seen. The file's own field still wins. Fictional people only.
Run: python3 scripts/test_mac_account_env.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mac-account-"))
os.environ["MAIL_SERVER_DB"] = str(_TMP / "mail_server.db")
os.environ["CONNECTORS_LEDGER"] = str(_TMP / "ledger.db")
os.environ.pop("MAIL_SERVER_MAC_ACCOUNT", None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import connectors_runner as R  # noqa: E402
import mail_server as M  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


one = _TMP / "one"
one.mkdir()


def account(d, name, **extra):
    p = d / f"{name}.json"
    p.write_text(json.dumps({"owner": "alex", "provider": "icloud", "user": "alex@example.invalid", "password": "x", **extra}))
    p.chmod(0o600)
    return p


ROLE = {"alex": "admin"}.get
account(one, "alex-icloud")
check("no field, no env: no mac_account", M.load_account(one / "alex-icloud.json", ROLE)["mac_account"], "")
os.environ["MAIL_SERVER_MAC_ACCOUNT"] = "iCloud"
check("a bare name with one server account: it", M.load_account(one / "alex-icloud.json", ROLE)["mac_account"], "iCloud")
two = _TMP / "two"
two.mkdir()
account(two, "alex-icloud")
account(two, "alex-work")
check("a bare name with two server accounts: ambiguous, none", M.load_account(two / "alex-icloud.json", ROLE)["mac_account"], "")
os.environ["MAIL_SERVER_MAC_ACCOUNT"] = "alex-icloud=iCloud,alex-work=Work Mail"
check("named pairs: each its own", [M.load_account(two / f"{n}.json", ROLE)["mac_account"] for n in ("alex-icloud", "alex-work")],
      ["iCloud", "Work Mail"])
account(one, "alex-icloud", mac_account="Personal")
check("the file's own field wins over the env", M.load_account(one / "alex-icloud.json", ROLE)["mac_account"], "Personal")

print("── MAIL_SERVER_FOLDERS (decision (c): Sent and Archive, without opening the file) ──")
os.environ.pop("MAIL_SERVER_MAC_ACCOUNT", None)
account(one, "alex-icloud", folders=["INBOX"])
check("unset: the file's own list", M.load_account(one / "alex-icloud.json", ROLE)["folders"], ["INBOX"])
os.environ["MAIL_SERVER_FOLDERS"] = "INBOX, Archive ,Sent Messages"
check("a bare list with one server account: it, trimmed", M.load_account(one / "alex-icloud.json", ROLE)["folders"],
      ["INBOX", "Archive", "Sent Messages"])
os.environ["MAIL_SERVER_FOLDERS"] = "Auto"
check("auto: INBOX plus the marked archive and sent folders", M.load_account(one / "alex-icloud.json", ROLE)["folders"], ["auto"])
check("a bare value with two server accounts: ambiguous, the file's", M.load_account(two / "alex-icloud.json", ROLE)["folders"],
      ["INBOX"])
os.environ["MAIL_SERVER_FOLDERS"] = "alex-icloud=INBOX,Archive;alex-work=auto"
check("named per account", [M.load_account(two / f"{n}.json", ROLE)["folders"] for n in ("alex-icloud", "alex-work")],
      [["INBOX", "Archive"], ["auto"]])
os.environ.pop("MAIL_SERVER_FOLDERS")

print("── cutover-check ──")
con = R._db(_TMP / "ledger.db")
for i, acct in enumerate(("iCloud", "iCloud", "Household")):
    con.execute("INSERT INTO items (source, source_id, content_hash, item_id, account, synced_at, owner) VALUES "
                "('mail', ?, 'h', ?, ?, 0, 'alex')", (f"s{i}", f"cx-{i}", acct))
con.commit()
con.close()
os.environ["MAIL_SERVER_MAC_ACCOUNT"] = "icloud"
account(one, "alex-icloud")   # no field again
ok, lines = M.cutover_check("alex", one, _TMP / "ledger.db", ROLE)
check("one server account naming a Mac account the ledger has seen (case aside): ok", (ok, "2 items seen" in lines[0]), (True, True))
os.environ["MAIL_SERVER_MAC_ACCOUNT"] = "Gmail"
ok, lines = M.cutover_check("alex", one, _TMP / "ledger.db", ROLE)
check("a name the ledger has never seen: refused", (ok, "matches no Mac mail account" in lines[0]), (False, True))
os.environ.pop("MAIL_SERVER_MAC_ACCOUNT")
ok, lines = M.cutover_check("alex", one, _TMP / "ledger.db", ROLE)
check("none set: refused, saying what to set", (ok, "MAIL_SERVER_MAC_ACCOUNT" in lines[0]), (False, True))
ok, _ = M.cutover_check("robin", one, _TMP / "ledger.db", ROLE)
check("someone with no server account: refused", ok, False)
os.environ["MAIL_SERVER_MAC_ACCOUNT"] = "iCloud"
r = subprocess.run([sys.executable, str(HERE / "mail_server.py"), "cutover-check", "--owner", "alex"], capture_output=True,
                   text=True, timeout=60, env={**os.environ, "MAIL_ACCOUNTS_DIR": str(two), "HOUSEHOLD_DB": str(_TMP / "hh.db")})
check("the CLI refuses (exit 1), saying why, not a crash", (r.returncode, "REFUSED" in r.stdout, "Traceback" in r.stderr),
      (1, True, False))
check("…and prints no address", "example.invalid" in r.stdout + r.stderr, False)
ok, lines = M.cutover_check("alex", two, _TMP / "ledger.db", ROLE)
check("two server accounts with a bare name: refused (ambiguous: none set)", (ok, all("MAIL_SERVER_MAC_ACCOUNT" in x for x in lines)),
      (False, True))
ok, lines = M.cutover_check("alex", one, _TMP / "no-ledger.db", ROLE)
check("no ledger: refused, saying so", (ok, "no connectors ledger" in lines[0]), (False, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
