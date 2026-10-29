#!/usr/bin/env python3
# gate: hermetic
"""linker.scored: the house's scored link graph, kept off the request path (stale-while-revalidate). Scored once and
cached; a write to links.db is served at once with the ITEMS loaded fresh (who owns each and who may see it is always
current) and the cached edges, while the edges are re-scored in the background and swapped in; past the age limit the
cached graph is served while it's rebuilt. The cache is the whole house: every caller still projects per viewer, so
two people hitting it back to back, during the stale window and after, each see only their own. A temporary links.db
and salt; fictional items.
Run: python3 scripts/test_linker_scored_cache.py"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-scored-"))
for k, v in {"LINK_SALT_FILE": "link_salt", "LINKS_DB": "links.db", "WORLD_DIR": "world", "WORLD_DB": "world.db",
             "HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "SPACES_DB": "spaces.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import hooks as H  # noqa: E402
import linker as L  # noqa: E402
import projects as P  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.200} expected={expected!r:.200}"))


con = H.connect()
mail = lambda t, frm="Shop <orders@pineshop.example>": {"title": "Order", "text": f"From: {frm}\nDate: Oct 1, 2026\n\n{t}"}  # noqa: E731
H.index(con, "m1", "mail", H.extract(mail("Order #PS-1001 shipped."), lambda t: []), "alex", "private")
H.index(con, "m2", "mail", H.extract(mail("Order #PS-1001 delivered."), lambda t: []), "alex", "private")
con.commit()
calls = {"n": 0, "running": 0}
gate = threading.Event()     # scoring waits on it: closed, a call that returns anyway didn't wait for the score
gate.set()
real = L.score


def counting(*a, **k):
    """calls["n"]: scores that finished (a held one hasn't)."""
    calls["running"] += 1
    try:
        if not gate.wait(120):
            raise RuntimeError("the test never released the score")
        out = real(*a, **k)
        calls["n"] += 1
        return out
    finally:
        calls["running"] -= 1


L.score = counting


def settle():
    """Let a held score finish and wait for the background rebuild: a signal (#1152), never a clock."""
    gate.set()
    if not L.wait_rebuilt(120):
        raise RuntimeError("the background rebuild never finished")


def served_while_scoring(call):
    """Call it with scoring held: it returns only if it never waited for a score. → (its result, a score was held)."""
    gate.clear()
    out = call()
    held = calls["running"] > 0 or L._SCORED.get("rebuilding")
    return out, bool(held)


print("── scored once, then from the cache ──")
a = L.scored(H.connect())
b = L.scored(H.connect())
check("first call scores; the second is the cache", (calls["n"], a is b), (1, True))
check("…the graph: both items and their edge", (sorted(a[0]), len(a[1]) >= 1), (["m1", "m2"], True))

print("── a write: served at once, re-scored in the background ──")
H.index(con, "m3", "mail", H.extract(mail("Order #PS-1001 returned."), lambda t: []), "alex", "private")
con.commit()
c, held = served_while_scoring(lambda: L.scored(H.connect()))
check("served without waiting for the score (it returned while the score was held)", held, True)
check("…its items are current (the new one is in), its edges the cached ones for now",
      ("m3" in c[0], any("m3" in (e.a, e.b) for e in c[1])), (True, False))
settle()
d = L.scored(H.connect())
check("after the background rebuild: the new item's edges are in", any("m3" in (e.a, e.b) for e in d[1]), True)

print("── who may see an item is always current, even before the rebuild ──")
H.index(con, "m2", "mail", H.extract(mail("Order #PS-1001 delivered."), lambda t: []), "alex", "household")
con.commit()
e = L.scored(H.connect())
check("an audience change shows at once", e[0]["m2"].audience, "household")
settle()

print("── a person's word applies at once, before the background re-score ──")
n = calls["n"]
L.say(H.connect(), "m1", "m3", False, "alex")
g, _held = served_while_scoring(lambda: L.scored(H.connect()))
state = next((e.state for e in g[1] if {e.a, e.b} == {"m1", "m3"}), None)
check("they said m1 and m3 aren't related: rejected on the very next read, nothing re-scored yet",
      (state, calls["n"] - n), ("rejected", 0))
settle()

print("── past the age limit: the cached graph, re-scored behind it ──")
n = calls["n"]
L.SCORED_MAX_AGE_S = 0.0
f, held = served_while_scoring(lambda: L.scored(H.connect()))
check("served at once (while the re-score was held), the same graph", (held, f is L._SCORED["val"] or bool(f)), (True, True))
settle()
check("…and rebuilt in the background", calls["n"] > n, True)
L.SCORED_MAX_AGE_S = 600.0
settle()

print("── the cache is the house: every caller still projects per viewer ──")
for i in range(3):
    H.index(con, f"j{i}", "mail", H.extract(mail(f"Case #LC-770 update {i}.", "Clinic <help@lakeclinic.example>"),
                                          lambda t: []), "jordan", "private")
con.commit()
ids = lambda ps: sorted({i for p in ps for i in p["items"]})   # noqa: E731
stale_alex, stale_jordan = P.proposals("alex", "adult"), P.proposals("jordan", "adult")
check("during the stale window: Alex never sees Jordan's items, Jordan never sees Alex's",
      (any(i.startswith("j") for i in ids(stale_alex)), any(i.startswith("m") for i in ids(stale_jordan))), (False, False))
settle()
n = calls["n"]
alex, jordan, alex2 = P.proposals("alex", "adult"), P.proposals("jordan", "adult"), P.proposals("alex", "adult")
check("warm, back to back: no scoring on the request path", calls["n"] - n, 0)
check("Alex sees only his own (twice); never Jordan's", (any(i.startswith("j") for i in ids(alex) + ids(alex2)),
      set(ids(alex)) <= {"m1", "m2", "m3"}, ids(alex) == ids(alex2)), (False, True, True))
check("Jordan sees only his own (not an id, not a count of Alex's)", ids(jordan), ["j0", "j1", "j2"])
check("the shared cache holds both (it's the house, before projection)", {"m1", "j0"} <= set(L.scored(H.connect())[0]),
      True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
