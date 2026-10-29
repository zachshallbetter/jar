#!/usr/bin/env python3
# gate: hermetic
"""Taking triaged-out mail out of the link index (#951, owner call 10-05): only mail items go, with their hooks, topic
vectors and edges (logged), after a 0600 backup; statements and other items stay. Fictional mail; a temporary links.db.
Run: python3 scripts/test_links_prune.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-links-prune-test-"))
for k, v in {"LINK_SALT_FILE": "link_salt", "LINKS_DB": "links.db", "LINKS_BACKUP_DIR": "backups",
             "WORLD_DIR": "world", "WORLD_DB": "world.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import hooks as H  # noqa: E402
import linker as L  # noqa: E402
import links_prune as P  # noqa: E402
import topics as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


con = H.connect()


def mail(iid, frm, body, owner="alex"):
    H.index(con, iid, "mail", H.extract({"text": f"From: {frm}\nDate: 2026-09-20\n\n{body}"}), owner, "private")


mail("cx-mail-keep-1", "billing@harborgas.example", "Your bill $84.12 for account 5531-0042 is due.")
mail("cx-mail-keep-2", "billing@harborgas.example", "Payment of $84.12 received, account 5531-0042.")
mail("cx-mail-promo-1", "deals@harborgas.example", "Save on smart thermostats.")
mail("cx-mail-promo-2", "deals@harborgas.example", "Spring savings: thermostats.")
H.index(con, "rec-1", "record", H.extract({"title": "Harbor Gas bill", "vendor": "Harbor Gas", "amount": "84.12",
                                            "at": "2026-09-20", "derived_from": "cx-mail-keep-1"}), "alex", "private")
import numpy as np  # noqa: E402
for i in ("cx-mail-keep-1", "cx-mail-keep-2", "cx-mail-promo-1", "cx-mail-promo-2"):
    T.put(con, i, np.ones(4, np.float32))
L.save(con, L.score(L.load(con), L.statements(con)))
L.say(con, "cx-mail-promo-1", "cx-mail-keep-1", False, "alex")
before = P.counts(con)

print("── counts only ──")
ids = {"cx-mail-promo-1", "cx-mail-promo-2", "rec-1", "nope-1"}
r = P.prune(con, ids, apply=False)
check("why each listed id would go or stay", r["by_reason"], {"removed": 2, "kept: record item": 1, "not in the index": 1})
check("a dry run changes nothing", P.counts(con), before)

print("── apply ──")
bk = P.backup(Path(os.environ["LINKS_DB"]))
check("the backup is 0600 and holds every item", (oct(bk.stat().st_mode & 0o777),
                                                    sorted(x[0] for x in __import__("sqlite3").connect(str(bk)).execute(
                                                        "SELECT DISTINCT item_id FROM hooks"))),
      ("0o600", sorted(["cx-mail-keep-1", "cx-mail-keep-2", "cx-mail-promo-1", "cx-mail-promo-2", "rec-1"])))
r = P.prune(con, ids, apply=True)
left = {x[0] for x in con.execute("SELECT DISTINCT item_id FROM hooks")}
check("only the listed mail went; the record stayed", sorted(left), ["cx-mail-keep-1", "cx-mail-keep-2", "rec-1"])
check("their topic vectors went", sorted(x[0] for x in con.execute("SELECT item_id FROM topics")),
      ["cx-mail-keep-1", "cx-mail-keep-2"])
check("no edge touches a removed item", con.execute(
    "SELECT COUNT(*) FROM links WHERE a LIKE 'cx-mail-promo%' OR b LIKE 'cx-mail-promo%'").fetchone()[0], 0)
check("each removed edge is in the log, with why",
      con.execute("SELECT COUNT(*) FROM link_log WHERE why LIKE 'removed from the index%'").fetchone()[0],
      sum(r["edges_touching"].values()))
check("the kept mail and the record still link", any(e.state in ("verified", "accepted") for e in
                                                      L.score(L.load(con), L.statements(con))
                                                      if {e.a, e.b} == {"cx-mail-keep-1", "rec-1"}), True)
check("the person's statement stays", con.execute("SELECT COUNT(*) FROM link_statements").fetchone()[0], 1)

print("── the command prints counts, never ids ──")
lst = _TMP / "drop.txt"
lst.write_text("cx-mail-keep-2\n")
out = subprocess.run([sys.executable, str(Path(__file__).parent / "links_prune.py"), "--ids", str(lst)],
                     capture_output=True, text=True, env=os.environ)
rep = json.loads(out.stdout)
check("dry run by default", (rep["apply"], rep["by_reason"]), (False, {"removed": 1}))
check("no id in the output", "cx-mail" in out.stdout, False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
