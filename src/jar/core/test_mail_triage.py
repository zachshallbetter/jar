#!/usr/bin/env python3
# gate: hermetic
"""Mail triage at ingest (10-03 KB cleanup): with MAIL_TRIAGE=1 a code mail is kept nowhere, every other mail waits
out of the KB, and the triage pass indexes personal and action mail only, keeps records for mail_bills and
mail_suggestions until both have read them, and drops the rest. The model is faked.
Run: python3 scripts/test_mail_triage.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailtriage-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "CONNECTORS_LEDGER": "ledger.db", "AGENT_MEMORY_DIR": "memory",
             "KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_PREFS": "household.json", "DAILY_LOG_DIR": "daily",
             "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl", "MAIL_TRIAGE_DB": "mail_triage.db",
             "ITEM_REFS_DB": "item_refs.db", "REFERENCES_DB": "references.db"}.items():
    os.environ[k] = str(_TMP / v)
for k in ("ITEM_REFERENCES", "MAIL_TRIAGE", "MAIL_SUGGESTIONS"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import connectors_runner as cx  # noqa: E402
import mail_triage as mt  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


calls: list[tuple] = []


def rt(service, path, method, payload):
    calls.append((service, path, method, payload))
    return {}


def indexed():
    return [c[3]["source_id"] for c in calls if c[1] == "/v1/retrieval/index"]


def deleted():
    return [c[1].rsplit("/", 1)[1] for c in calls if c[2] == "DELETE" and "/retrieval/sources/" in c[1]]


def mail(sid, title, text, **kw):
    return {"source_id": sid, "title": title, "text": text, "fields": {"from": kw.get("sender", "Robin Hale")},
            "timeline": f"Mail: {title}", **({"deleted": True} if kw.get("deleted") else {})}


def ledger_titles(iid):
    con = sqlite3.connect(os.environ["CONNECTORS_LEDGER"])
    try:
        return [r[0] for r in con.execute("SELECT title FROM versions WHERE item_id=?", (iid,))]
    finally:
        con.close()


def ledger_kb(iid):
    con = sqlite3.connect(os.environ["CONNECTORS_LEDGER"])
    try:
        r = con.execute("SELECT kb FROM items WHERE item_id=?", (iid,)).fetchone()
        return r[0] if r else None
    finally:
        con.close()


print("── the code rule ──")
check("codes, magic links, confirm-your-email: caught", [mt.is_code(t, b) for t, b in (
    ("Your Hale Bank verification code", "Use 482913 to sign in."),
    ("Sign in to Leafline", "Here is your magic link to sign in."),
    ("Confirm your email for the Jordan Garden Club", "Tap to confirm."),
    ("482 913 is your Leafline code", ""),
    ("Login [4471]", ""))], [True] * 5)
check("a note, an order, a breach notice: not codes", [mt.is_code(t, b) for t, b in (
    ("Dinner Friday?", "Robin: dinner at ours Friday at 7?"),
    ("Your order has shipped", "Order 1124-88 is on its way."),
    ("Account data access attempt", "Someone asked for your data. If this wasn't you, contact us."))], [False] * 3)

print("── off: mail goes into the KB as before ──")
calls.clear()
cx.ingest("mail", [mail("m-0", "Dinner Friday?", "Robin: dinner at ours Friday at 7?")], runtime=rt, owner="zach")
check("MAIL_TRIAGE unset: indexed at once, nothing queued", (indexed(), mt.status()["by_category"]),
      ([cx.item_id("mail", "m-0")], {}))

os.environ["MAIL_TRIAGE"] = "1"
print("── on: a code mail is kept nowhere ──")
calls.clear()
code_id = cx.item_id("mail", "m-1")
st = cx.ingest("mail", [mail("m-1", "Your Hale Bank verification code", "Use 482913 to sign in.")], runtime=rt, owner="zach")
check("not indexed, no record node, no timeline line, not queued",
      (st["written"], indexed(), [c[1] for c in calls if c[1] in ("/v1/nodes", "/v1/events")], mt.category_of(code_id)),
      (1, [], [], None))
check("the ledger keeps only a masked title", ledger_titles(code_id), [mt.MASKED_TITLE])
check("the subject is nowhere in the ledger", "Hale Bank" in Path(os.environ["CONNECTORS_LEDGER"]).read_bytes().decode("latin-1"),
      False)

print("── on: a code mail that reached the KB before goes ──")
calls.clear()
os.environ.pop("MAIL_TRIAGE")
cx.ingest("mail", [mail("m-2", "Sign in to Leafline", "Here is your link.")], runtime=rt, owner="zach")
os.environ["MAIL_TRIAGE"] = "1"
calls.clear()
cx.ingest("mail", [mail("m-2", "Sign in to Leafline", "Here is your magic link to sign in. (resent)")], runtime=rt, owner="zach")
check("its KB copy and record node are deleted", (deleted(), [c[1] for c in calls if c[2] == "DELETE" and "/nodes/" in c[1]]),
      ([cx.item_id("mail", "m-2")], [f"/v1/nodes/{cx.item_id('mail', 'm-2')}"]))

print("── on: other mail waits for triage ──")
calls.clear()
items = {"m-3": ("Dinner Friday?", "Robin: dinner at ours Friday at 7?", "personal"),
         "m-4": ("Card declined: Leafline renewal", "Your payment of $12.00 failed. Update your card by 10-10.", "action"),
         "m-5": ("Your Leafline receipt", "Paid $12.00 on 10-03. Order 1124-88.", "record"),
         "m-6": ("The Garden Weekly: 10 bulbs for fall", "This week in the garden...", "newsletter"),
         "m-7": ("40% off everything", "Sale ends Sunday.", "marketing"),
         "m-8": ("Jordan sent you a message on Leafline", "Log in to read it.", "notification")}
cx.ingest("mail", [mail(s, t, b) for s, (t, b, _c) in items.items()], runtime=rt, owner="zach")
ids = {s: cx.item_id("mail", s) for s in items}
check("nothing indexed at ingest; all six queued", (indexed(), mt.status()["by_category"].get("waiting")), ([], 6))
check("the record nodes and timeline lines stay as before", len([c for c in calls if c[1] == "/v1/nodes"]), 6)

print("── the pass ──")
by_title = {t: c for t, _b, c in items.values()}


def fake_llm(prompt):
    for t, c in by_title.items():
        if f"Subject: {t}" in prompt:
            return json.dumps({"category": c})
    return "?"


calls.clear()
dry = mt.run(False, llm=fake_llm, index=lambda *a: cx.index_triaged(*a, runtime=rt))
check("a dry run counts and changes nothing", (dry["personal"], dry["record"], indexed(), mt.status()["by_category"].get("waiting")),
      (1, 1, [], 6))
import kb_privacy as _P0  # noqa: E402
_P0.set_acl(ids["m-4"], "someone-else", "household", by="test", why="stale")   # a row left from before (#1274)
st = mt.run(True, llm=fake_llm, index=lambda *a: cx.index_triaged(*a, runtime=rt))
check("personal and action indexed, nothing else", sorted(indexed()), sorted([ids["m-3"], ids["m-4"]]))
check("…as connector:mail, private to their owner", [(c[3]["source_type"], c[3]["metadata"]["owner"], c[3]["metadata"]["audience"])
                                                     for c in calls if c[1] == "/v1/retrieval/index"][:1],
      [("connector:mail", "zach", "private")])
check("the ledger marks them in the KB (a later deletion removes them)", (ledger_kb(ids["m-3"]), ledger_kb(ids["m-5"])), (1, 0))
import kb_privacy as _P  # noqa: E402
check("…each with its sharing-table row: its owner, private (#1273); nothing for mail that wasn't kept",
      (_P.acl_rows([ids["m-3"]]).get(ids["m-3"]), _P.acl_rows([ids["m-4"]]).get(ids["m-4"]), ids["m-6"] in _P.acl_rows([ids["m-6"]])),
      (("zach", "private"), ("zach", "private"), False))
check("…and a kept mail entering the KB replaced the stale broader row it found (#1274)", _P.acl_rows([ids["m-4"]])[ids["m-4"]],
      ("zach", "private"))
check("a record keeps its text for the two passes", (mt.record_text(ids["m-5"]) or "")[:9], "Paid $12.")
check("newsletter, marketing, notification: never served as records", [mt.record_text(ids[s]) for s in ("m-6", "m-7", "m-8")],
      [None, None, None])
check("…held for the owner's weekly digest (#861), with their sender and a borderline score",
      sorted((d["item_id"], d["category"]) for d in mt.dropped("zach", 0)),
      sorted([(ids["m-6"], "newsletter"), (ids["m-7"], "marketing"), (ids["m-8"], "notification")]))

print("── records reach mail_bills and mail_suggestions ──")
srcs = mt.record_sources()
check("shaped like the KB's source list, owner kept", [(s["source_id"], s["source_type"], s["metadata"]["owner"]) for s in srcs],
      [(ids["m-5"], "connector:mail", "zach")])
read = mt.text_of(lambda sid: "from the KB")
check("text_of: the record's text, else the KB's", (read(ids["m-5"])[:9], read(ids["m-3"])), ("Paid $12.", "from the KB"))
mt.mark("bills", [ids["m-5"]])
check("read by bills only: kept", mt.record_text(ids["m-5"]) is not None, True)
mt.mark("suggestions", [ids["m-5"]])
check("read by both: its text goes", (mt.record_text(ids["m-5"]), mt.record_sources()), (None, []))

mt.sweep(now=time.time() + (mt.DIGEST_HOLD_DAYS + 1) * 86400)
check("dropped mail after the digest hold is gone after the hold: no text, no subject, no sender", (mt.dropped("zach", 0), mt.held(ids["m-7"])), ([], None))

print("── the model down: it waits, then expires unindexed ──")
calls.clear()
cx.ingest("mail", [mail("m-9", "Lunch Tuesday?", "Ava: lunch Tuesday at noon?")], runtime=rt, owner="zach")
down = lambda p: (_ for _ in ()).throw(OSError("connection refused"))  # noqa: E731
st = mt.run(True, llm=down, index=lambda *a: cx.index_triaged(*a, runtime=rt))
check("unreached: still waiting", (st["unreached"], mt.category_of(cx.item_id("mail", "m-9"))), (1, None))
st = mt.run(True, llm=down, index=lambda *a: cx.index_triaged(*a, runtime=rt), now=time.time() + (mt.UNTRIAGED_TTL_H + 1) * 3600)
check("past the TTL: expired, never indexed", (st["expired"], indexed(), mt.category_of(cx.item_id("mail", "m-9"))),
      (1, [], "expired"))
check("classify: garbage is no answer", (mt.classify("x", "y", lambda p: "personal!"), mt.classify("x", "y", lambda p: '{"category": "spam"}')),
      (None, None))

import http.server, threading  # noqa: E401,E402


class _Llm(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"choices": [{"message": {"content": '{"category": "personal"}'}}]}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), _Llm)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["MAIL_TRIAGE_URL"], os.environ["MAIL_TRIAGE_FALLBACK_URL"] = "http://127.0.0.1:1", f"http://127.0.0.1:{srv.server_port}"
check("the code model down: the fallback model answers", mt.classify("Dinner?", "Robin: dinner?"), "personal")
os.environ["MAIL_TRIAGE_FALLBACK_URL"] = "http://127.0.0.1:1"
check("both down: no answer (it waits)", mt.classify("Dinner?", "Robin: dinner?"), None)
srv.shutdown()

print("── a deleted mail leaves the queue; one deleted before triage is never indexed ──")
cx.ingest("mail", [mail("m-10", "Pickup at 3", "Leo: can you get the kids at 3?")], runtime=rt, owner="zach")
cx.ingest("mail", [mail("m-10", "Pickup at 3", "", deleted=True)], runtime=rt, owner="zach")
check("gone from the queue", mt.category_of(cx.item_id("mail", "m-10")), None)
check("index_triaged refuses a deleted mail", cx.index_triaged(cx.item_id("mail", "m-10"), "Pickup at 3", "x", {}, runtime=rt),
      False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
