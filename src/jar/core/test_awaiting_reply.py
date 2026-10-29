#!/usr/bin/env python3
# gate: hermetic
"""Mail waiting on a reply (#1644, mail_server.awaiting_reply and the mail_awaiting_reply tool): inbox mail from someone
the owner writes to (a person, not a mailer), 2 to 21 days old, still the newest in its thread. Their own mail only;
names and subjects reach the model wrapped. Fixture headers in a temporary store, no server. Fictional people (Alex the
admin, Robin, Jordan; a garden newsletter). Run: python3 scripts/test_awaiting_reply.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-awaiting-"))
os.environ.update({"MAIL_SERVER": "1", "MAIL_SERVER_DB": str(_TMP / "mail.db"), "HOUSEHOLD_DB": str(_TMP / "household.db"),
                   "SPACES_DB": str(_TMP / "spaces.db"), "CAPABILITY_SWITCHES": str(_TMP / "switches.json"),
                   "PROACTIVE_DB": str(_TMP / "proactive.db"), "MCP_SERVERS": str(_TMP / "none.json"),
                   "AGENT_TURN_SOURCE": "user"})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_server as M  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


NOW = time.mktime((2026, 10, 15, 12, 0, 0, 0, 0, 0)) - time.timezone
UID = {"n": 0}


def at(days):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - days * 86400))


def put(owner, account, folder, mid, thread, sender, subject, days, code=0):
    UID["n"] += 1
    con = M._db(Path(os.environ["MAIL_SERVER_DB"]))
    con.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, sender, "
                "subject, sent_at, code) VALUES (?,?,1,?,?,?,?,?,?,?,?,?)",
                (account, folder, UID["n"], owner, f"i-{mid}", mid, thread, sender, subject, at(days), code))
    con.commit()
    con.close()


ROBIN, JORDAN, ALEX = "Robin Hale <robin@example.org>", "Jordan Ava <jordan@example.org>", "Alex <alex@example.org>"
put("alex", "a1", "Sent Messages", "<s1>", "t-old", ALEX, "Re: lunch", 40)          # Alex writes to Robin…
put("alex", "a1", "INBOX", "<r0>", "t-old", ROBIN, "lunch", 41)
put("alex", "a1", "Sent Messages", "<s2>", "t-old2", ALEX, "Re: chairs", 40)        # …and to Jordan
put("alex", "a1", "INBOX", "<j0>", "t-old2", JORDAN, "chairs", 41)
put("alex", "a1", "INBOX", "<r1>", "t1", ROBIN, "Saturday plans?", 5)               # waiting
put("alex", "a1", "INBOX", "<r2>", "t2", ROBIN, "Dentist ride", 6)                  # answered from Sent after
put("alex", "a1", "Sent Messages", "<s3>", "t2", ALEX, "Re: Dentist ride", 5)
put("alex", "a1", "INBOX", "<n1>", "t3", "Garden <news@garden.example>", "Bulbs for fall", 4)   # a mailer: never
put("alex", "a1", "INBOX", "<r3>", "t4", ROBIN, "Quick one", 1)                     # too new
put("alex", "a1", "INBOX", "<r4>", "t5", ROBIN, "Old thing", 30)                    # too old
put("alex", "a1", "INBOX", "<j1>", "t6", JORDAN, "Picnic?", 8)                      # Jordan followed up: the newest counts
put("alex", "a1", "INBOX", "<j2>", "t6", JORDAN, "Re: Picnic?", 3)
put("alex", "a1", "INBOX", "<c1>", "t7", ROBIN, "Your code is 123456", 4, code=1)   # a sign-in code: never
put("robin", "a2", "Sent Messages", "<x1>", "u0", ROBIN, "Re: hi", 40)
put("robin", "a2", "INBOX", "<x0>", "u0", ALEX, "hi", 41)
put("robin", "a2", "INBOX", "<x2>", "u1", ALEX, "For Robin only", 5)                 # Robin's own mailbox

got = M.awaiting_reply("alex", now=NOW)
check("waiting on Alex: Robin's Saturday and Jordan's follow-up, newest first; nothing answered, no mailer, no code, "
      "not too new or too old", [(g["sender"], g["subject"], g["days"]) for g in got],
      [("Jordan Ava", "Re: Picnic?", 3), ("Robin Hale", "Saturday plans?", 5)])
check("Robin's mailbox is Robin's: never in Alex's", any("Robin only" in g["subject"] for g in got), False)
check("Robin's own: what waits on Robin", [g["subject"] for g in M.awaiting_reply("robin", now=NOW)], ["For Robin only"])
check("nobody or no store: nothing", (M.awaiting_reply("", now=NOW), M.awaiting_reply("alex", db=_TMP / "none.db", now=NOW)),
      ([], []))

print("── the tool ──")
import agent_runtime_lib as lib  # noqa: E402
M_aw = M.awaiting_reply
M.awaiting_reply = lambda person, **kw: M_aw(person, now=NOW)
lib._kb_who = lambda: ("alex", "admin")
lib._TURN_IO.person, lib._TURN_IO.role = {"id": "alex"}, "admin"
out = lib._execute_tool("mail_awaiting_reply", {})
nonce = lib._TURN_IO.untrusted_nonce
check("the owner gets who's waiting, the names and subjects wrapped as other people's text",
      (out.startswith("2 waiting on your reply"), nonce in out, out.index("Saturday plans") > out.index(nonce),
       "never send anything" in out), (True, True, True, True))
lib._kb_who = lambda: ("ava", "kid")
check("a kid: not theirs to read", lib._execute_tool("mail_awaiting_reply", {}).startswith("That's for the mailbox's owner"), True)
check("offered to adults and the admin, never a kid or a shared screen",
      [(setattr(lib._TURN_IO, "role", r), lib._role_allows("mail_awaiting_reply"))[1] for r in ("admin", "adult", "kid", "household")],
      [True, True, False, False])
os.environ["MAIL_SERVER"] = "0"
lib._TURN_IO.role = "admin"
check("not offered until titan reads mail from the servers", lib._role_allows("mail_awaiting_reply"), False)
import tool_descriptors as td  # noqa: E402
d = td.lookup("mail_awaiting_reply")
check("a read of mail, from titan's own store: nothing leaves", (d.domain, d.risk, d.egress), ("mail", "read", ()))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
