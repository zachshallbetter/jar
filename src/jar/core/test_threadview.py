#!/usr/bin/env python3
# gate: hermetic
"""The thread view (#951 (g), rule 8): one build() for the tool's text and the apps' JSON (GET /v1/threads), per
viewer, never an identifier value, and the person's correction (POST /v1/threads/relate). Fictional items only; a
temporary links.db and salt.
Run: python3 scripts/test_threadview.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-threadview-test-"))
for k, v in {"LINK_SALT_FILE": "link_salt", "LINKS_DB": "links.db", "WORLD_DIR": "world", "WORLD_DB": "world.db",
             "HOUSEHOLD_DB": "household.db", "KB_ACL_DB": "kb_acl.db", "AGENT_MEMORY_DIR": "memory", "FENCES_DB": "fences.db",
             "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl", "CONNECTORS_LEDGER": "ledger.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import hooks as H  # noqa: E402
import linker as L  # noqa: E402
import threadview as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


con = H.connect()
TITLES = {}


def ENTS(text):
    return ["org:harbor-mobile"] if "harbor mobile" in text.lower() else []


def add(iid, src, item, owner="alex", aud="private"):
    TITLES[iid] = item.get("title") or ""
    H.index(con, iid, src, H.extract(item, ENTS), owner, aud)


def mail(frm, day, body, title):
    return {"title": title, "text": f"From: {frm}\nDate: {day}\nSubject: {title}\n\n{body}"}


for i in range(40):   # an ordinary background, so a shared amount can be rare
    add(f"bg-{i}", "mail", mail(f"News <hi@shop{i % 6}.example>", f"2026-08-{(i % 27) + 1:02d}", f"Total ${10 + i}.00", "Receipt"))
add("text-1", "text", {"text": "HarborMobile: device for order hm-2048-7731 received.", "at": "2026-09-27", "sender": "45021"})
add("mail-1", "mail", mail("Harbor Mobile <orders@harbormobile.com>", "Sep 28, 2026",
                           "Your trade-in for Order #HM-2048-7731 was received. A credit of $250.00 posts soon.", "Trade-in received"))
add("mail-2", "mail", mail("Harbor Mobile <billing@harbormobile.com>", "Oct 14, 2026", "A $250.00 credit was applied.",
                           "Credit applied"))
add("bridge-a", "mail", {"title": "Ticket opened", "text": "Your ticket TK-55102 is open.", "at": "2026-09-01"})
add("bridge-h", "mail", {"title": "Escalation", "text": "Ticket TK-55102 is now case CS-77310.", "at": "2026-09-02"}, owner="robin")
add("bridge-b", "mail", {"title": "Case update", "text": "Case CS-77310 was resolved.", "at": "2026-09-03"})
L.labels = lambda items: (TITLES, {}, {"org:harbor-mobile": "Harbor Mobile"})
import audience  # noqa: E402
import fences  # noqa: E402


class _Open:
    def admits(self, *a, **k):
        return True


fences.get = lambda: _Open()
TODAY = date(2026, 10, 15)

print("── the view: data the apps render ──")
v = T.build("alex", "admin", "harbor mobile trade-in", today=TODAY)
check("status, filters, threads, unlinked, held, unsorted", sorted(v), ["filters", "held", "status", "threads", "unlinked",
                                                                        "unsorted"])
t = v["threads"][0]
check("a thread: id, name, description, cohesion, items, edges, fringe",
      sorted(t), ["cohesion", "description", "edges", "fringe", "id", "items", "name"])
check("…its items in date order, each id, kind, date, title", [(i["id"], i["kind"], i["date"]) for i in t["items"]],
      [("text-1", "text", "2026-09-27"), ("mail-1", "mail", "2026-09-28"), ("mail-2", "mail", "2026-10-14")])
check("…each edge: state, why, typed, measured", sorted(t["edges"][0]), ["a", "b", "measured", "state", "typed", "why"])
check("the thread id is stable for the same members", T.thread_id(["mail-2", "text-1", "mail-1"]), t["id"])
blob = json.dumps(v)
check("never an identifier value (order, short code, address)", [x for x in ("2048", "7731", "45021", "orders@", "billing@")
                                                                 if x in blob], [])
check("JSON all the way down", json.loads(blob) == v, True)

print("── the tool's text is a projection of the same view ──")
txt = T.render_text(v)
check("filters, the thread, its links", (txt.splitlines()[0].startswith("Filters: "), "Thread: Harbor Mobile" in txt,
      "linked (accepted)" in txt), (True, True, True))

print("── per viewer ──")
ti = lambda v: [sorted(i["id"] for i in t["items"]) for t in v["threads"]]   # noqa: E731
check("Alex: A and B aren't a thread without the item only Robin can see",
      ti(T.build("alex", "admin", "ticket opened", today=TODAY)), [])
check("a kid gets none", T.build("ava", "kid", "trade-in", today=TODAY)["status"], "not_shown")
check("a shared screen gets none", T.build("household", "household", "trade-in", today=TODAY)["status"], "not_shown")
check("an unknown axis is a bad request", T.build("alex", "admin", "x", hold="mood", today=TODAY)["status"], "bad_hold")
u = T.build("alex", "admin", all_=True, today=TODAY)["unsorted"]
check("everything, unsorted: only what Alex may see", ("bridge-h" in {i["id"] for i in u}, len(u) > 40), (False, True))

print("── the person's correction (rule 5) ──")
r = T.relate("alex", "admin", "text-1", "mail-1", False)
check("'not related' is recorded and the edge is rejected", (r["related"], r["state"]), (False, "rejected"))
check("…the next view splits the text off", [i["id"] for i in T.build("alex", "admin", "harbor mobile", today=TODAY)
                                             ["threads"][0]["items"]], ["mail-1", "mail-2"])
check("'related' makes it verified", T.relate("alex", "admin", "text-1", "mail-1", True)["state"], "verified")
for args, code in ((("alex", "admin", "bridge-a", "bridge-h", True), 404), (("ava", "kid", "text-1", "mail-1", True), 403),
                   (("alex", "admin", "text-1", "text-1", True), 400)):
    try:
        T.relate(*args)
        check(f"refused {code}", None, code)
    except T.Refused as e:
        check(f"refused {code}: {e.detail}", e.status, code)

print("── the routes ──")
import household_identity as hh  # noqa: E402
check("adults may read and correct; kids and shared screens may not",
      [hh.route_allowed(r, m, p) for r, m, p in (("adult", "GET", "/v1/threads"), ("admin", "POST", "/v1/threads/relate"),
                                                  ("kid", "GET", "/v1/threads"), ("household", "POST", "/v1/threads/relate"))],
      [True, True, False, False])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
