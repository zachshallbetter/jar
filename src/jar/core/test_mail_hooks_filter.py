#!/usr/bin/env python3
# gate: hermetic
"""Only mail triage keeps reaches the link index (#951, owner call 10-05): the backfill had hooked every mail and
threads/open_obligations slowed to 30-50 min. A model verdict when there is one (personal, action, record); else
rules from the mail's own headers and sender: bulk mail is out unless its subject is a reply or names a bill, order,
delivery, appointment... Both server paths (older mail, hooks only; derived mail) and the one-off backfill. Every
mail is marked, so a skipped one isn't fetched again. Fictional mail; throwaway stores; no IMAP, no model.
Run: python3 scripts/test_mail_hooks_filter.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailhooks-"))
os.environ["MAIL_SERVER"] = "1"
os.environ["LINKS_DB"] = str(_TMP / "links.db")
os.environ["LINK_SALT_FILE"] = str(_TMP / "link_salt")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["MAIL_TRIAGE_DB"] = str(_TMP / "mail_triage.db")
sys.path.insert(0, str(Path(__file__).parent))

import mail_server as M  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def raw(headers: dict[str, str], body: str = "Hello.") -> bytes:
    h = {"From": "Robin <robin@example.com>", "Subject": "Dinner", "Message-ID": "<m1@example.com>",
         "Date": "Mon, 5 Oct 2026 10:00:00 +0000", **headers}
    return ("\r\n".join(f"{k}: {v}" for k, v in h.items()) + "\r\n\r\n" + body).encode()


print("── bulk by its own headers ──")
check("a person's mail isn't bulk", M.bulk_headers(raw({})), False)
for h in ({"List-Unsubscribe": "<mailto:u@lists.example>"}, {"List-Id": "<news.example>"}, {"Precedence": "bulk"},
          {"Precedence": "list"}, {"Auto-Submitted": "auto-generated"}):
    check(f"bulk: {list(h)[0]}: {list(h.values())[0]}", M.bulk_headers(raw(h)), True)
check("Auto-Submitted: no isn't", M.bulk_headers(raw({"Auto-Submitted": "no"})), False)
check("a header-looking line in the body doesn't count", M.bulk_headers(raw({}, "List-Unsubscribe: <x>")), False)
check("garbage never raises", M.bulk_headers(b"\xff\xfe not mail"), False)

print("\n── the verdict ──")
for cat, want in (("personal", True), ("action", True), ("record", True), ("newsletter", False), ("marketing", False),
                  ("notification", False), ("code", False)):
    check(f"triage said {cat}", M.hook_worthy(cat, "Weekly picks", "news@quarry.example", True), want)
cases = [
    ("a person, no bulk headers", "Dinner Saturday?", "Robin <robin@example.com>", False, True),
    ("a newsletter", "This week at Lumen", "Lumen <news@lumen.example>", True, False),
    ("marketing from no-reply", "Big savings inside", "no-reply@slate.example", False, False),
    ("an order confirmation from no-reply", "Your order has shipped", "no-reply@cobalt.example", True, True),
    ("a bill from an automated sender", "Your Lumen Power bill is ready", "billing@lumen.example", True, True),
    ("a payment receipt", "Payment received", "noreply@fern.example", False, True),
    ("a reply on a list thread", "Re: pickup times", "Robin <robin@example.com>", True, True),
    ("an appointment reminder", "Reminder: dentist appointment", "notifications@pike.example", True, True),
    ("a notification without a cue", "New sign-in to your feed", "notifications@slate.example", True, False),
]
for label, subj, sender, bulk, want in cases:
    check(f"no verdict: {label}", M.hook_worthy(None, subj, sender, bulk), want)

print("\n── the older-mail path (hooks only, no model) ──")
con = M._db(_TMP / "mail_server.db")
rows = []
mails = [(1, "Dinner Saturday?", "Robin <robin@example.com>", {}),
         (2, "This week at Lumen", "Lumen <news@lumen.example>", {"List-Unsubscribe": "<mailto:u@lumen.example>"}),
         (3, "Your order has shipped", "no-reply@cobalt.example", {"List-Unsubscribe": "<x>"}),
         (4, "Big savings inside", "no-reply@slate.example", {})]
for uid, subj, sender, h in mails:
    con.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, refs, thread_id, "
                "sender, subject, sent_at) VALUES ('a','INBOX',1,?, 'p_alex', ?, ?, '[]', ?, ?, ?, '2026-01-01T00:00:00Z')",
                (uid, f"mail-{uid}", f"<m{uid}@x>", f"t{uid}", sender, subj))
con.commit()
rows = con.execute("SELECT * FROM messages ORDER BY uid").fetchall()
RAWS = {uid: raw({"Subject": subj, "From": sender, **h}) for uid, subj, sender, h in mails}


class Conn:
    def fetch(self, uids, what):
        return {u: RAWS[u] for u in uids}


hooked: list[str] = []
real_hooks = M._hooks
M._hooks = lambda r, text, md, to=None: hooked.append(r["item_id"]) or 3   # to: its To/Cc (#1399)
n = M._hook_only(con, Conn(), {"name": "a", "owner": "p_alex"}, "INBOX", rows)
check("only the person's mail and the order are indexed", (n, hooked), (2, ["mail-1", "mail-3"]))
check("every one is marked, so none is fetched again",
      con.execute("SELECT COUNT(*) FROM messages WHERE hooked_at IS NOT NULL").fetchone()[0], 4)
M._hooks = real_hooks

print("\n── the derived path and the one-off backfill use the same rule ──")
src = Path(M.__file__).read_text()
check("derive gates its hooks on the verdict (the model's category when triage ran)",
      'if hook_worthy(cat, r["subject"], r["sender"], bulk) else 0' in src, True)
check("the one-off backfill too", 'if hook_worthy(r["category"], r["subject"], r["sender"], bulk):' in src, True)
check("hooks are indexed in exactly those three places, each behind hook_worthy", src.count("_hooks(r, text, "), 3)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
