#!/usr/bin/env python3
# gate: hermetic
"""#366 groundwork: signal B (A's recency-decayed mentions + a bump for entities the person's live items name), the
prospective A/B log behind WORLD_ACTIVE_LOG (off by default; written off the request path), and measure_active_ab.
Only entities the asker may see; the log is ids only, in the asker's own bubble. Context injection is unchanged.
Fictional household (Alex an adult, Robin an adult); a temporary world with a fixed clock; no model.
Run: python3 scripts/test_active_ab.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-active-ab-"))
for k, v in {"WORLD_DIR": "world", "HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json",
             "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ.pop("WORLD_ACTIVE_LOG", None)
sys.path.insert(0, str(Path(__file__).parent))
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = 1791331200.0
clock = {"t": NOW}
w = W.Bubbles(_TMP / "w", clock=lambda: clock["t"])


def obs(entity, etype, name, attr, value, bubble="household", owner=None, audience="household"):
    return W.Obs(entity=entity, etype=etype, name=name, attr=attr, value=value, observed_at="2026-09-01", kind="household",
                 ref=f"hh#{entity}#{attr}", origin="household.yaml", label="the household file", owner=owner,
                 audience=audience, bubble=bubble)


w.sync("hh", [obs("maple-animal-clinic", "org", "Maple Animal Clinic", "phone", "(555) 0142"),
              obs("harbor-gas", "org", "Harbor Gas", "phone", "(555) 0188"),
              obs("pine-shop", "org", "Pine Shop", "phone", "(555) 0199")])
w.sync("robin-own", [obs("lakeside-dental", "org", "Lakeside Dental", "phone", "(555) 0177", bubble="robin",
                         owner="robin", audience="private")])
clinic, gas, pine, dental = ("household:maple-animal-clinic", "household:harbor-gas", "household:pine-shop",
                             "robin:lakeside-dental")
for cid, hours_ago in ((gas, 1), (gas, 2), (pine, 3), (clinic, 200)):
    clock["t"] = NOW - hours_ago * 3600
    W.record_mention(w, cid, "alex")
clock["t"] = NOW

print("── A and B ──")
a = [c for c, _ in W.active(w, "alex", "adult", k=2)]
check("A: the recency-decayed baseline", a, [gas, pine])
b = [c for c, _ in W.active_b(w, "alex", "adult", ["Call Maple Animal Clinic about the vaccine"], k=2)]
check("B: an entity a live item names is bumped into the top", clinic in b, True)
check("B with no live items is A", [c for c, _ in W.active_b(w, "alex", "adult", [], k=2)], a)
check("B never ranks an entity the asker can't see, even when a live title names it",
      dental in [c for c, _ in W.active_b(w, "alex", "adult", ["Lakeside Dental cleaning"], k=5)], False)

print("── the prospective log ──")
got = W.log_active(w, "alex", "adult", [clinic], NOW, titles=["Call Maple Animal Clinic about the vaccine"], k=2)
check("a turn that named the clinic: A missed it, B held it", (clinic in got["a"], clinic in got["b"]), (False, True))
W.log_active(w, "alex", "adult", [gas], NOW, titles=[], k=2)
m = W.measure_active_ab(w, "alex")
check("measure: rates for both", (m["predicted"], m["a_hits"], m["b_hits"]), (2, 1, 2))
rows = w.world("alex").con.execute("SELECT * FROM active_log").fetchall()
check("the log is in Alex's own bubble, never the household's",
      (len(rows), w.world("household").con.execute("SELECT COUNT(*) FROM active_log").fetchone()[0]), (2, 0))
check("…entity ids only (no title, no question)", any("vaccine" in json.dumps(dict(r)) or "Maple" in json.dumps(dict(r))
                                                     for r in rows), False)
check("another person has none", W.measure_active_ab(w, "robin")["turns"], 0)

print("── the turn path: off by default, off the request path when on ──")
n0 = w.world("alex").con.execute("SELECT COUNT(*) FROM active_log").fetchone()[0]
W.context(w, "what's Maple Animal Clinic's number", "alex", "adult", gaps=True)
check("flag off: a real turn logs nothing", w.world("alex").con.execute("SELECT COUNT(*) FROM active_log").fetchone()[0], n0)
os.environ["WORLD_ACTIVE_LOG"] = "1"
import active_signals  # noqa: E402
active_signals.live_titles = lambda person, role, limit=60: ["Call Maple Animal Clinic"]
t = time.monotonic()
W.context(w, "what's Maple Animal Clinic's number", "alex", "adult", gaps=True)
for th in [x for x in threading.enumerate() if x.name == "active-log"]:
    th.join(10)
check("flag on: the turn logs one row (written in its own thread)",
      w.world("alex").con.execute("SELECT COUNT(*) FROM active_log").fetchone()[0], n0 + 1)
check("an eval-style call (no gaps) never logs", (W.context(w, "the clinic", "alex", "adult"), [x for x in threading.enumerate()
                                                                                              if x.name == "active-log"])[1], [])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
