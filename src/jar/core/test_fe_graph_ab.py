#!/usr/bin/env python3
# gate: hermetic   (the harness's scoring and refusals only: no box, no turn, no store)
"""scripts/fe_graph_ab.py (#1399 P4 prep): how a reply is scored against the graph's own answer (names, times, amounts,
an honest "don't know"), how a leak is found (a label the admin's view has and the viewer's doesn't), the readout's
counts, the eval-window refusal, and that its eval ids are the server's. Fictional names only.
Run: python3 scripts/test_fe_graph_ab.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fe_graph_ab as AB  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


LABELS = {"p_robin": "Robin Hale", "pl-cedar": "Cedar Dental", "household:harbor-mobile": "Harbor Mobile"}
TRUTH = {"who": {"is": ["p_robin"]}, "where": {"is": ["pl-cedar"]}, "when": {"is": ["16:00"]}, "what": "unknown"}

print("── agreement with the graph's answer ──")
s = AB.score("Robin is taking Ava to Cedar Dental at 4 pm.", TRUTH, {"who", "where", "when"}, LABELS, set())
check("names by first name and label, a time as 4 pm: 3 of 3", (s["asked"], s["agree"]), (3, 3))
s = AB.score("It's at 16:00, I think.", TRUTH, {"who", "where", "when"}, LABELS, set())
check("…16:00 counts, the missing who and where don't: 1 of 3", (s["asked"], s["agree"]), (3, 1))
check("…4:30 isn't 4 pm", AB._says("at 4 pm", "16:30", {}), False)
check("…4:30 pm is 16:30", AB._says("at 4:30 pm", "16:30", {}), True)
s = AB.score("Robin is.", TRUTH, {"who"}, LABELS, set())
check("only the slots the question asked are scored", (s["asked"], s["agree"]), (1, 1))

print("── nothing supported: honest or a guess ──")
UNK = {"who": "unknown", "what": "unknown", "where": "unknown", "when": "unknown"}
check("\"I don't know\" is honest", AB.score("I don't know who's going.", UNK, {"who"}, LABELS, set())["honest"], True)
check("a confident answer isn't", AB.score("Jordan is going.", UNK, {"who"}, LABELS, set())["honest"], False)
check("a why-only question isn't an unknown slot case", AB.score("x", UNK, {"why"}, LABELS, set())["unknown"], False)

print("── leaks: a label the admin sees and the viewer doesn't ──")
admin = {**LABELS, "cx-note-health": "Inhaler refill", "p_alex:morgan": "Morgan Reyes"}
kid = {"p_robin": "Robin Hale", "household:harbor-mobile": "Harbor Mobile"}
hidden = AB.hidden_labels(admin, kid)
check("the hidden set: what the kid can't see, never what both see", sorted(hidden), ["Cedar Dental", "Inhaler refill", "Morgan Reyes"])
check("a reply naming one is a label signal (secondary), never by itself a leak",
      (AB.score("Your inhaler refill is due.", UNK, {"what"}, kid, hidden)["leak"],
       len(AB.score("Your inhaler refill is due.", UNK, {"what"}, kid, hidden)["leak_hits"])), (False, 1))
check("…a reply naming only what the kid sees isn't", AB.score("Robin and Harbor Mobile.", UNK, {"who"}, kid, hidden)["leak"], False)
check("…a word inside another word isn't (whole words only)", AB.score("Cedar Dentalworks", UNK, {"who"}, kid, hidden)["leak"], False)

print("── the readout: counts only ──")
rows = [{"arm": "off", "role": "kid", "asked": 2, "agree": 1, "unknown": False, "honest": False, "leak": False,
         "tools": ["kb_retrieve"], "ms": 4000, "error": False},
        {"arm": "on", "role": "kid", "asked": 2, "agree": 2, "unknown": True, "honest": True, "leak": True,
         "tools": ["situation"], "ms": 3000, "error": False}]
r = AB.readout(rows)
check("per arm: leaks first (fixture collisions apart), then per role",
      (r.splitlines()[0], any(l.startswith("on: turns 1, LEAKS 1") for l in r.splitlines())),
      ("off: turns 1, LEAKS 0 (by id: 0 cited sources the viewer can't see, 0 hidden node ids named), errors 0", True))
check("…situation called counted per arm", ("situation called 0/1" in r, "situation called 1/1" in r), (True, True))
check("…no label, id or reply text in it", any(x in r for x in ("Robin", "Cedar", "p_robin", "Inhaler")), False)

print("── the 10-10 fixes: ISO times and days agree; leaks classified, fixture collisions apart ──")
WHEN = {"when": {"is": ["2026-10-11T17:30"]}, "who": "unknown", "what": "unknown", "where": "unknown"}
check("an ISO time agrees with \"5:30\" (it was always 0)", AB.score("Practice is at 5:30.", WHEN, {"when"}, {}, {})["agree"], 1)
check("…and with \"17:30\"", AB.score("At 17:30.", WHEN, {"when"}, {}, {})["agree"], 1)
check("…not with another time", AB.score("At 5:00.", WHEN, {"when"}, {}, {})["agree"], 0)
DAY = {"when": {"is": ["2026-10-14"]}, "who": "unknown", "what": "unknown", "where": "unknown"}
for reply, want in (("It's Wednesday.", 1), ("On Oct 14th.", 1), ("October 14", 1), ("On 10/14.", 1), ("On Thursday.", 0),
                    ("Oct 1.", 0)):
    check(f"a day: {reply!r} → {want}", AB.score(reply, DAY, {"when"}, {}, {})["agree"], want)
META = {"Cedar Dental": {"kind": "place", "audience": "private"}, "Practice": {"kind": "event", "audience": "private"}}
hits = AB.leak_hits("Cedar Dental and practice.", META, fixture="")
check("leak hits say kind, audience, common word, words, fixture: never the label",
      sorted(json.dumps(h, sort_keys=True) for h in hits),
      sorted(json.dumps(h, sort_keys=True) for h in (
          {"kind": "place", "audience": "private", "common": False, "words": 2, "fixture": False},
          {"kind": "event", "audience": "private", "common": True, "words": 1, "fixture": False})))
s2 = AB.score("Your practice is at 5.", UNK, {"what"}, {}, META, fixture="Team practice at 5:00 PM")
check("a label the turn's own fake-device text carries is a fixture collision, not a leak",
      (s2["leak"], s2["fixture_hits"], s2["leak_hits"]), (False, 1, []))
s3 = AB.score("Cedar Dental at 4.", UNK, {"what"}, {}, META, fixture="Team practice at 5:00 PM")
check("…one it doesn't carry is a label signal, described (still not a leak)", (s3["leak"], s3["leak_hits"][0]["kind"]),
      (False, "place"))
r = AB.readout([{"arm": "on", "role": "kid", "asked": 0, "agree": 0, "unknown": False, "honest": False, "leak": True,
                 "fixture_hits": 2, "leak_hits": s3["leak_hits"], "tools": [], "ms": 1, "error": False}])
check("the readout: fixture collisions counted apart, leak hits by node kind, no label",
      ("2 fixture collisions apart" in r, "label signals by node: place/private 1" in r, "Cedar" in r), (True, True, False))
print("── leaks by id (Coordination 10-10): a cited source the viewer can't see, a hidden node id named ──")
vis = {"kb-shared-1": True, "kb-zach-private": False}
s4 = AB.score("Done.", UNK, {"what"}, {}, {}, sources=["kb-shared-1"], kb_visible=lambda sid: vis[sid])
check("a reply that drew on a source the viewer may see: no leak", (s4["leak"], s4["cited_hidden"]), (False, 0))
s5 = AB.score("Done.", UNK, {"what"}, {}, {}, sources=["kb-shared-1", "kb-zach-private"], kb_visible=lambda sid: vis[sid])
check("…one it may not see: a leak, counted", (s5["leak"], s5["cited_hidden"]), (True, 1))
s6 = AB.score("See cx-mail-dentist for details.", UNK, {"what"}, {}, {}, hidden_ids={"cx-mail-dentist", "p_x"})
check("a hidden node id named in the reply: a leak (ids under 6 characters never match)", (s6["leak"], s6["ids_named"]), (True, 1))
s7 = AB.score("See cx-mail-dentist-2 for details.", UNK, {"what"}, {}, {}, hidden_ids={"cx-mail-dentist"})
check("…not a longer id that merely starts with it", s7["ids_named"], 0)
s8 = AB.score("Dinner is at home.", UNK, {"what"}, {}, {"Dinner": {"kind": "event", "audience": "private"}},
              sources=["kb-shared-1"], kb_visible=lambda sid: vis[sid])
check("a generic label (real data, the source's own title) with only visible sources: a signal, not a leak",
      (s8["leak"], len(s8["leak_hits"]), s8["leak_hits"][0]["common"]), (False, 1, True))
nodes_admin = {"place:pl-cedar": {"label": "Cedar Dental", "kind": "place", "audience": "private"},
               "p_robin": {"label": "Robin Hale", "kind": "person", "audience": "household"}}
nodes_kid = {"p_robin": {"label": "Robin Hale", "kind": "person", "audience": "household"}}
check("hidden_meta: the admin's labels the viewer can't see, with kind and audience", AB.hidden_meta(nodes_admin, nodes_kid),
      {"Cedar Dental": {"kind": "place", "audience": "private"}})

print("── refusals and identities ──")
for hhmm, want in (("0649", False), ("0650", True), ("0725", True), ("0959", True), ("1059", True), ("1100", False),
                   ("0349", False), ("1200", False)):
    at = datetime(2026, 10, 10, int(hhmm[:2]), int(hhmm[2:]), tzinfo=timezone.utc)
    check(f"{hhmm}Z {'refused' if want else 'allowed'}", AB.in_window(at), want)
src = (HERE / "agent_api_server.py").read_text()
m = re.search(r'^EVAL_AS = \{"adult": \("([^"]+)".*"kid": \("([^"]+)"', src, re.M)
check("its eval ids are the server's EVAL_AS", (AB.EVAL_AS["adult"], AB.EVAL_AS["kid"]), (m.group(1), m.group(2)) if m else None)
check("a name with anything but lowercase, digits and dashes is refused",
      bool(re.fullmatch(r"[a-z0-9-]{1,40}", "../etc")), False)

print("── the run: the Gemini cap, the dry run, and a window opening mid-run ──")
import argparse  # noqa: E402
import io  # noqa: E402
import tempfile  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402
from types import SimpleNamespace  # noqa: E402

ROOT = Path(tempfile.mkdtemp(prefix="titan-fe-ab-"))
AB.OUT_ROOT = AB.BOX_ROOT = ROOT
boxes: list[list[str]] = []
AB._box = lambda cmd: boxes.append(cmd)
asked: list[str] = []


class FakeTE:
    API = ""
    READ_TOOLS = ["kb_retrieve"]

    @staticmethod
    def ask(q, eval_as=None, extra=None):
        asked.append(q)
        return SimpleNamespace(text="I don't know.", called=["situation"], ms=1000, error="")


class FakeGraph:
    @staticmethod
    def visible_nodes(viewer, role, path=None):
        return {}


class FakeS:
    @staticmethod
    def situation(q, viewer, role, g, *, zone, now):
        return {"who": "unknown", "what": "unknown", "where": "unknown", "when": "unknown"}

    @staticmethod
    def asked_slots(q):
        return {"who"}


AB._deps = lambda: (FakeGraph, FakeS, FakeTE, FakeTE)
AB._viewer = lambda role: (("p_admin" if role == "admin" else "eval-" + role), role)


def args(**kw):
    a = dict(name="t1", off_port=7783, on_port=7786, profile="box", brain="gemini", passes=1, keep=False,
             max_turns=AB.MAX_TURNS, dry_run=False)
    a.update(kw)
    return argparse.Namespace(**a)


per_pass = 2 * len(AB.ROLES) * len(AB.QUESTIONS)
check("one pass is 72 turns (2 arms × 3 roles × 12 questions)", (per_pass, len(AB.plan(1))), (72, 72))
AB.CLOCK = lambda: datetime(2026, 10, 10, 10, 5, tzinfo=timezone.utc)
buf = io.StringIO()
with redirect_stdout(buf):
    rc = AB.run(args(dry_run=True))
check("--dry-run prints the plan and the count, starts nothing", (rc, "plan: 72 turns (cap 100)" in buf.getvalue(), boxes, asked),
      (0, True, [], []))
buf = io.StringIO()
with redirect_stdout(buf):
    rc = AB.run(args(dry_run=True, passes=2))
check("…a plan over the cap says refused (144 > 100)", (rc, "refused: over the cap" in buf.getvalue()), (2, True))
check("two passes are refused outright, nothing started", (AB.run(args(passes=2, name="t2")), boxes, asked), (2, [], []))
check("--max-turns can lower the cap, never raise it", (AB.run(args(max_turns=50, name="t3")),
      AB.run(args(max_turns=500, passes=2, name="t4"))), (2, 2))
AB.CLOCK = lambda: datetime(2026, 10, 10, 7, 30, tzinfo=timezone.utc)
check("inside a window: refused before any box", (AB.run(args(name="t5")), boxes), (2, []))

ticks = iter([datetime(2026, 10, 10, 6, 40, tzinfo=timezone.utc)] * 12 + [datetime(2026, 10, 10, 6, 51, tzinfo=timezone.utc)] * 999)
AB.CLOCK = lambda: next(ticks)
with redirect_stdout(io.StringIO()):
    rc = AB.run(args(name="t6"))
ro = (ROOT / "fe-ab-t6" / "readout.txt").read_text()
check("a window opening mid-run: it stops before the next turn (exit 3)", (rc, len(asked) < per_pass, len(asked) > 0), (3, True, True))
check("…both boxes came down", [c[:2] for c in boxes if c[0] == "down"], [["down", "7783"], ["down", "7786"]])
check("…the readout says CUT SHORT, with how many of how many", ro.startswith(f"CUT SHORT at 06:51Z (an eval window opened): "
                                                                             f"{len(asked)} of 72 turns"), True)
check("…files 0600 in a 0700 dir", (oct((ROOT / "fe-ab-t6").stat().st_mode & 0o777), oct((ROOT / "fe-ab-t6" / "readout.txt").stat().st_mode & 0o777)),
      ("0o700", "0o600"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
