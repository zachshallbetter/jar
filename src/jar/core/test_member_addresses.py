#!/usr/bin/env python3
# gate: hermetic   (temp stores and salt; a fictional credentials file that must never be opened)
"""Members' own addresses, keyed (#1424): from a Sent folder's From (the most frequent, and aliases seen at least
MIN_SEEN times) and a calendar's owner identity, keyed in the same expression they're read in, stored as the people
row's addr_keys. Never a credentials file, never a Reply-To or an Inbox From, a shared mailbox to neither, and no raw
address in any store, output or exception. Fictional people and addresses.
Run: python3 scripts/test_member_addresses.py
"""
from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-member-addrs-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "MAIL_SERVER_DB": "mail_server.db", "CONNECTORS_LEDGER": "ledger.db",
             "LINK_SALT_FILE": "salt", "MAIL_ACCOUNTS_DIR": "secrets-mail", "AGENT_MEMORY_DIR": "memory",
             "LINKS_DB": "links.db", "GRAPH_DB": "graph.db"}.items():
    os.environ[k] = str(_TMP / v)
CREDS = _TMP / "secrets-mail"
CREDS.mkdir()
(CREDS / "alex.json").write_text(json.dumps({"owner": "x", "user": "alex.quarry@mail.example.org", "password": "pw"}))

# Nothing may open the credentials folder: open(), os.open() and Path reads all fail loudly on it.
_open, _osopen = builtins.open, os.open
TOUCHED: list[str] = []


def _guard(path) -> None:
    if str(CREDS) in os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else False:
        TOUCHED.append("credentials")
        raise PermissionError("the mail credentials must never be opened")


builtins.open = lambda f, *a, **k: (_guard(f), _open(f, *a, **k))[1]
os.open = lambda f, *a, **k: (_guard(f), _osopen(f, *a, **k))[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import connectors_runner as C  # noqa: E402
import hooks  # noqa: E402
import household_identity as hh  # noqa: E402
import mail_server  # noqa: E402
import member_addresses as M  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


store = hh.get_store()
alex = hh.ADMIN_PERSON_ID
store.ensure_admin() if hasattr(store, "ensure_admin") else None
if not store.person(alex):
    store._db.execute("INSERT INTO people (id, name, role, created_at) VALUES (?, 'Alex Quarry', 'admin', 0)", (alex,))
robin = store.add_person("Robin Quarry", "adult")["id"]
jordan = store.add_person("Jordan Hale", "adult")["id"]
K = lambda a: hooks.keyed("addr", a)  # noqa: E731

# mail_server.db: Sent and Inbox mail for Alex and Robin
con = mail_server._db(Path(os.environ["MAIL_SERVER_DB"]))
rows = []
uid = 0
def mail(owner, folder, sender, n=1):  # noqa: E302
    global uid
    for _ in range(n):
        uid += 1
        rows.append(("acct-" + owner, folder, 1, uid, owner, f"cx-m{uid}", f"<m{uid}@x>", f"t{uid}", sender, "s"))
mail(alex, "Sent", "Alex Quarry <alex.quarry@mail.example.org>", 6)        # the main address
mail(alex, "[Gmail]/Sent Mail", "a.quarry@example.org", 3)                  # an alias on his domain, seen 3 times
mail(alex, "Sent", "Pine Park Club Board <board@pineparkclub.example>", 4)  # sent AS the club's board: never his
mail(alex, "Sent", "alex.q@otherjob.example.com", 3)                        # another domain: not taken as his
mail(alex, "Sent", "alex@harbormobile.example", 3)                          # an org's domain (the world model's)
mail(alex, "Sent Messages", "ALEX@club.example.com", 1)                     # a one-off: not his
mail(alex, "INBOX", "Robin Quarry <robin.q@lakes.example.org>", 9)          # an Inbox From: never
mail(alex, "Sent", "quarry.home@example.org", 3)                            # shared: both send from it
mail(robin, "Sent Items", "Robin Quarry <robin.q@lakes.example.org>", 4)
mail(robin, "Sent Items", "quarry.home@example.org", 3)
mail(robin, "Sent Items", "Reply-To: not-a-from@example.org", 1)            # not an address line of its own
mail(jordan, "Sent", "Jordan Hale <jordan.hale.fixture@gmail.com>", 5)     # a public mail provider's address
mail(jordan, "Sent", "robin.quarry.fixture@gmail.com", 3)                   # sending AS another Gmail: never his
con.executemany("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, sender, "
                "subject) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
con.commit()
con.close()

# the ledger: Robin's calendar names its owner by address; Alex's by a name only
L = C._db(Path(os.environ["CONNECTORS_LEDGER"]))
for i, (owner, cal_owner) in enumerate([(robin, "robin.q@lakes.example.org")] * 3 + [(alex, "Alex Quarry")] * 3):
    L.execute("INSERT INTO items (source, source_id, content_hash, item_id, synced_at, owner) VALUES "
              "('calendar', ?, 'h', ?, 0, ?)", (f"ev{i}", f"cx-ev{i}", owner))
    L.execute("INSERT INTO versions (source, source_id, item_id, change, fields, at) VALUES ('calendar', ?, ?, 'add', ?, 0)",
              (f"ev{i}", f"cx-ev{i}", json.dumps({"calendar_owner": cal_owner})))
L.commit()
L.close()

print("── the plan ──")
_real_org_domains = M.org_domains
M.org_domains = lambda root=None: {"harbormobile.example"}   # the world model knows Harbor Mobile's domain
p = M.plan(store)
check("Alex: the main Sent address and the same-domain alias seen 3 times; not the one-off, not an Inbox From, not "
      "the shared one", set(p["keys"][alex]), {K("alex.quarry@mail.example.org"), K("a.quarry@example.org")})
check("…never the club's board@ he sends as, another domain's address, or an org's domain (KB's review)",
      {K("board@pineparkclub.example"), K("alex.q@otherjob.example.com"), K("alex@harbormobile.example")} &
      set(p["keys"][alex]), set())
check("…and those are counted as skipped (role, other domain, org, one-off)", p["skipped_alias"] >= 4, True)
check("a top address on a public provider (gmail.com) keeps no aliases: sending as another Gmail isn't his (KB)",
      p["keys"][jordan], [K("jordan.hale.fixture@gmail.com")])
check("Robin: her Sent address (and her calendar's owner, the same key); not the shared mailbox",
      set(p["keys"][robin]), {K("robin.q@lakes.example.org")})
check("the shared mailbox (both send from it) goes to neither, and is counted", p["shared"], 1)
check("a calendar owner given as a name isn't an address: nothing from it for Alex", p["by_source"]["calendar"].get(alex), None)
check("…an address one is: Robin's calendar", p["by_source"]["calendar"].get(robin), 1)

print("── stored keyed only ──")
M.apply(p, store)
check("addr_keys on the people rows", {k: set(v) for k, v in store.addr_keys().items()},
      {alex: set(p["keys"][alex]), robin: set(p["keys"][robin]), jordan: set(p["keys"][jordan])})
try:
    store.set_addr_keys(robin, ["robin.q@lakes.example.org"])
    refused, msg = False, ""
except ValueError as exc:
    refused, msg = True, str(exc)
check("a raw address is refused, and the message doesn't repeat it", (refused, "@" in msg), (True, False))
import re  # noqa: E402
ADDR = re.compile(rb"[\w.+-]+@[\w-]+\.[\w.-]+")   # an address's shape (a bare 0x40 is in every SQLite file header)


def addresses_in(name: str) -> list[bytes]:
    return [m for f in [_TMP / name, *_TMP.glob(name + "-*")] if f.exists() for m in ADDR.findall(f.read_bytes())]


check("no address anywhere in household.db (and its WAL)", addresses_in("household.db"), [])
for name in ("links.db", "graph.db"):
    check(f"…nor in {name} (if this pass wrote one)", addresses_in(name), [])
check("people() (what the API returns) carries no keys and no address",
      [x for x in json.dumps(store.people()) .split('"') if "@" in x or x in p["keys"][alex]], [])

print("── org domains from the world model ──")
W = _TMP / "world"
W.mkdir()
w = sqlite3.connect(W / "household.db")
w.executescript("CREATE TABLE entities (id TEXT, type TEXT, name TEXT); CREATE TABLE claims (entity_id TEXT, attr TEXT, "
                "value TEXT); CREATE TABLE aliases (entity_id TEXT, alias TEXT);"
                "INSERT INTO entities VALUES ('lakeside', 'org', 'Lakeside Clinic'), ('robin-q', 'person', 'Robin');"
                "INSERT INTO claims VALUES ('lakeside', 'website', 'https://www.lakesideclinic.example/billing'),"
                "('robin-q', 'email', 'robin.q@lakes.example.org');"
                "INSERT INTO aliases VALUES ('lakeside', 'billing.lakesideclinic.example'), ('lakeside', 'Lakeside');")
w.commit()
w.close()
check("an org's website and domain-shaped alias give its registrable domain; a person's email claim never does",
      _real_org_domains(W), {"lakesideclinic.example"})

print("── the CLI: counts only ──")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    M.main(["--apply"])
check("its output has no address and no key", ("@" in out.getvalue(), any(k in out.getvalue() for k in p["keys"][alex])),
      (False, False))
M.apply({"keys": {alex: [], robin: p["keys"][robin], jordan: p["keys"][jordan]}}, store)
check("re-applying with none clears a member's keys (a dropped address goes)", alex in store.addr_keys(), False)

print("── never the credentials ──")
check("nothing opened the mail credentials folder", TOUCHED, [])
check("the module never calls mail_server.accounts / load_account (they read the credentials)",
      any(w in Path(M.__file__).read_text() for w in ("accounts(", "load_account", "ACCOUNTS_DIR")), False)

builtins.open, os.open = _open, _osopen
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
