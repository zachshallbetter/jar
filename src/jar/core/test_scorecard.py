#!/usr/bin/env python3
"""The scorecard's areas on fixed inputs: each target can miss, and each says why.

Run: python3 scripts/test_scorecard.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
logs = Path(tempfile.mkdtemp())
os.environ["SCORECARD_LOGS"] = str(logs)
os.environ["AGENT_TURN_LOG"] = str(logs / "turns-test.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"

import scorecard as sc  # noqa: E402

# The disks are titan's: a test machine's own (a Mac at 90%) is not what the checks are about.
import collections  # noqa: E402
sc.shutil.disk_usage = lambda mount: collections.namedtuple("usage", "total used free")(100, 40, 60)

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


now = time.time()
print("\n── service: real turns only ──")
turns = ([{"ts": now, "source": "jellybean", "ms": 2000, "brain": "gemini"}] * 45
         + [{"ts": now, "source": "jellybean", "ms": 3000, "error": "HTTPError: HTTP Error 400: Bad Request"}] * 5
         + [{"ts": now, "source": "canary", "ms": 99000, "error": "x"}] * 20)
s = sc.service(6, turns)
check("canaries don't count", s["turns"], 50)
check("10% errors misses, with the kind", (s["ok"], "HTTPError: HTTP Error N: Bad Request ×5" in s["misses"][0]), (False, True))
s = sc.service(6, [{"ts": now, "source": "iphone", "ms": 1000 + i * 100, "fallback": "local failed: 400" if i < 1 else None}
                   for i in range(40)])
check("a healthy window passes (1 fallback in 40)", (s["ok"], s["fallback_pct"]), (True, 2.5))
check("no turns: nothing to miss", sc.service(6, [])["ok"], True)
narrow = [{"ts": now, "source": "iphone", "ms": 1000, "tools_declared": 27, "tools_widened": i < 2} for i in range(25)]
sv = sc.service(6, narrow)
check("#683: the widened rate among narrowed turns, under 10% is fine", (sv["narrowed"], sv["widened"], sv["widened_pct"], sv["ok"]),
      (25, 2, 8.0, True))
sv = sc.service(6, [{**r, "tools_widened": i < 5} for i, r in enumerate(narrow)])
check("…over 10% is a miss", (sv["ok"], any("tools widened" in m for m in sv["misses"])), (False, True))
check("…and the summary line says it", "tools widened 20.0% of 25" in sc._summary("service", sv), True)
check("…too few narrowed turns to judge: no miss", sc.service(6, narrow[:10] and [{**r, "tools_widened": True} for r in narrow[:10]])["ok"], True)

print("\n── system: units, disks, GPUs, canary ──")
(logs / "agent-canary.jsonl").write_text("\n".join(json.dumps(
    {"ts": "2099-01-01T00:00:00", "status": "OK" if i > 1 else "FAIL", "checks": {"kb": {"ok": i > 1}}}) for i in range(10)) + "\n")
fake = {"systemctl": "titan-agent-api.service loaded failed failed Titan agent\n",
        "nvidia-smi": "0, 88, 1000, 12000\n1, 60, 500, 12000\n"}
sc.OUT.write_text(json.dumps({"system": {"gpus": [{"gpu": 0, "temp_c": 86}]}}) + "\n")   # GPU0 was hot last run too
r = sc.system(6, lambda cmd: fake.get(cmd[0], ""))
check("a failed unit, a hot GPU and a confirmed canary failure all miss", (r["ok"], any("titan-agent-api" in m for m in r["misses"]),
      any("GPU0 at 88" in m for m in r["misses"]), any("canary OK 80.0% (kb)" in m for m in r["misses"])), (False, True, True, True))

sc.OUT.write_text(json.dumps({"system": {"gpus": [{"gpu": 0, "temp_c": 70}]}}) + "\n")
hot_once = sc.system(6, lambda cmd: "0, 86, 1000, 12000\n" if cmd[0] == "nvidia-smi" else "")
check("one hot reading (an eval's load) isn't a miss", [m for m in hot_once["misses"] if "GPU" in m], [])
check("…90 °C is, at once", [m for m in sc.system(6, lambda cmd: "0, 91, 1000, 12000\n" if cmd[0] == "nvidia-smi" else "")["misses"]
                             if "GPU" in m], ["GPU0 at 91 °C"])

# #355: the system bubble's last health check
(logs / "system-world.jsonl").write_text(json.dumps({"ts": time.time(), "services": 29, "down": ["decision-broker"],
                                                     "port_differs": ["code-model"]}) + "\n")
rs = sc.system(6, lambda cmd: "")
check("a service down and a runbook port that differs are misses; the summary counts them",
      ([m for m in rs["misses"] if m.startswith(("services down", "runbook ports"))], rs["services"],
       "services 28/29 up, runbook port drift 1" in sc._summary("system", rs)),
      (["services down: decision-broker", "runbook ports differ from what runs: code-model (a doc ticket)"],
       {"n": 29, "down": 1, "port_differs": 1}, True))
# #355: an engineering agent's reads, counted; any non-system item is a miss
(logs / "engineering-reads.jsonl").write_text("\n".join(json.dumps(r) for r in (
    {"ts": time.time(), "route": "/v1/kb", "items": 4, "leaks": 0},
    {"ts": time.time(), "route": "/v1/world/entities", "items": 30, "leaks": 1},
    {"ts": 0, "route": "/v1/kb", "items": 2, "leaks": 5})) + "\n")
re_ = sc.system(6, lambda cmd: "")
check("an engineering leak in the window is a miss; an old one isn't counted",
      ([m for m in re_["misses"] if m.startswith("engineering")], re_["engineering"]),
      (["engineering reads returned 1 non-system items (dropped; engineering-reads.jsonl)"], {"reads": 2, "leaks": 1}))
(logs / "engineering-reads.jsonl").unlink()
check("no engineering reads: nothing to miss", sc.system(6, lambda cmd: "")["engineering"], {"reads": 0, "leaks": 0})
(logs / "system-world.jsonl").unlink()
sc.OUT.unlink()
(logs / "agent-canary.jsonl").write_text("\n".join(json.dumps({"ts": "2099-01-01T00:00:00", "status": st, "checks": {
    "agent_api": {"ok": st == "OK"}}}) for st in ["OK"] * 40 + ["FAIL", "OK"] * 3) + "\n")
r = sc.system(6, lambda cmd: "")
check("one-off failures (a deploy's restart) don't count; they're blips", (r["ok"], r["canary_blips"], r["canary_ok_pct"]), (True, 3, 100.0))
(logs / "agent-canary.jsonl").write_text("\n".join(json.dumps({"ts": "2099-01-01T00:00:00", "status": st, "checks": {
    "kb": {"ok": st == "OK"}}}) for st in ["OK"] * 40 + ["FAIL", "FAIL"]) + "\n")
check("two in a row do", sc.system(6, lambda cmd: "")["misses"], ["canary OK 95.2% (kb)"])

print("\n── service: the slow Mac tools are named ──")
slow = [{"ts": now, "source": "jellybean", "ms": 30000, "device_times": [["get_screen", 22000], ["get_field_state", 300]]}] * 5 \
    + [{"ts": now, "source": "jellybean", "ms": 2000}] * 15
s = sc.service(6, slow)
check("p95 names the slowest tool", (s["slow_tools"]["get_screen"], "slowest Mac tools at p95: get_screen 22.0 s" in s["misses"][0]), (22.0, True))

print("\n── service: the items surface's latency per view (#1181) ──")
_now = time.time()
(logs / "items-latency.jsonl").write_text("\n".join(json.dumps(r) for r in
    [{"ts": _now, "view": "now", "role": "adult", "ms": 90, "status": 200}] * 20
    + [{"ts": _now, "view": "unsorted", "role": "admin", "ms": 400, "status": 200}] * 12
    + [{"ts": _now, "view": "project", "role": "adult", "ms": 900, "status": 200}] * 3
    + [{"ts": _now, "view": "now", "role": "kid", "ms": 5000, "status": 403}] * 5) + "\n")
sv = sc.service(6, [])
check("a view over budget with enough requests is a miss; under budget, too few, or refused aren't",
      ([m for m in sv["misses"] if "items surface" in m], sv["items_latency"]["now"]["p95_ms"], sv["items_latency"]["project"]["n"]),
      (["items surface over its 250 ms p95 budget: unsorted p95 400 ms (#1181)"], 90, 3))
check("…the summary names each view's p95", "items p95 now 90 ms" in sc._summary("service", sv), True)
(logs / "items-latency.jsonl").unlink()
print("\n── memory: junk, duplicates, guesses ──")
m = sc.memory({"zach": ["Ava does ballet on Mondays and Wednesdays.", "Ava takes ballet on Mondays and Wednesdays.",
                        "The user is asking about the weather.", "The user must avoid peanuts because of an allergy."]},
              "# User Persona\n\nZach lives in Spokane. He is likely a parent.\n")
z = m["people"]["zach"]
check("junk, a near-duplicate pair and a guessing sentence", (z["junk"], z["dup_pairs"], m["persona_guesses"], m["ok"]), (1, 1, 1, False))
clean = sc.memory({"robin": ["Robin is vegetarian.", "Leo does ballet on Mondays."]}, "# User Persona\n\nRobin is vegetarian.\n")
check("clean memory passes", clean["ok"], True)

print("\n── knowledge probes ──")
good = {"what is the gate code for the boat storage unit": [],
        "when does the garbage go out": [{"chunk_text": "Garbage goes out every Wednesday night.", "metadata": {}}], "T-Mobile bill": [{"chunk_text": "T-Mobile bill $254.45", "metadata": {}}],
        "Ava ballet": [{"chunk_text": "Ballet\nMon, Oct 5, 2026, 6:15 PM\n(+4 more dates of this event)", "metadata": {}}],
        "what does Robin like": [{"chunk_text": "Robin likes hiking", "metadata": {"source": "mail"}}]}
k = sc.knowledge(lambda q, n, passive: good[q])
check("the right answers pass", (k["ok"], k["probes"]), (True, {p[0]: "ok" for p in sc.PROBES}))
bad = dict(good, **{"what is the gate code for the boat storage unit": [{"chunk_text": "Rule: events posted…", "metadata": {}}],
                    "what does Robin like": [{"chunk_text": "IMG_1.HEIC", "metadata": {"source": "downloads"}}]})
k = sc.knowledge(lambda q, n, passive: bad[q])
check("noise and a download miss, by name", sorted(m.split(":")[0] for m in k["misses"]), ["no-downloads-in-context", "nothing-stored"])
check("a probe that raises is a miss, not a crash",
      sc.knowledge(lambda q, n, passive: 1 / 0)["probes"]["bill"], "error: ZeroDivisionError")

(logs / "retrieval-eval.jsonl").write_text(json.dumps({"ts": 1, "found": 9, "found_total": 10, "not_stored": 3,
                                                         "not_stored_total": 3, "failing": ["dentist"]}) + "\n")
k = sc.knowledge(lambda q, n, passive: good[q])
check("the nightly retrieval eval's miss is the knowledge area's", (k["ok"], k["misses"]),
      (False, ["nightly retrieval 9/10 found, 3/3 not stored: dentist"]))
(logs / "retrieval-eval.jsonl").write_text(json.dumps({"ts": 2, "found": 10, "found_total": 10, "not_stored": 3,
                                                         "not_stored_total": 3, "failing": []}) + "\n")
check("… and a clean night passes", sc.knowledge(lambda q, n, passive: good[q])["ok"], True)

W = lambda **kw: json.dumps({"ts": 3, "source": "nightly", "lookups": 17, "kb_only": 13, "world": 17, "score": 1.0,  # noqa: E731
                             "privacy_ok": True, "privacy_errors": 0, "household_kid": 2, "household_kid_total": 2,
                             "household_kid_ok": True, **kw}) + "\n"
(logs / "world-eval.jsonl").write_text(W())
k = sc.knowledge(lambda q, n, passive: good[q])
check("a clean nightly world eval passes, and is reported", (k["ok"], k["world"]["world"], k["world"]["privacy_ok"]), (True, 17, True))
(logs / "world-eval.jsonl").write_text(W(privacy_ok=False))
check("a world-model leak is a knowledge miss", sc.knowledge(lambda q, n, passive: good[q])["misses"],
      ["nightly world eval: the world model leaked to a viewer"])
(logs / "world-eval.jsonl").write_text(W(world=12) + W(world=12, source="manual") .replace('"world": 12', '"world": 17'))
check("below KB-only is a miss; a manual run after it doesn't count",
      sc.knowledge(lambda q, n, passive: good[q])["misses"], ["nightly world eval: world 12/17 below KB-only 13/17"])
(logs / "world-eval.jsonl").write_text(W(household_kid=1, household_kid_ok=False))
check("a kid missing household facts is a miss", sc.knowledge(lambda q, n, passive: good[q])["misses"],
      ["nightly world eval: a kid missed household facts (1/2)"])
(logs / "world-eval.jsonl").write_text(W(source="manual", privacy_ok=False))
check("only manual runs: not counted", sc.knowledge(lambda q, n, passive: good[q])["world"], None)
(logs / "world-eval.jsonl").unlink()

print("\n── response: the nightly truth eval (#385) ──")
T = lambda brain, score, ts, src="nightly", misses=(): json.dumps({"ts": ts, "source": src, "brain": brain, "score": score,  # noqa: E731
                                                                 "total": 38, "misses": list(misses)})
(logs / "truth-eval.jsonl").write_text("\n".join([T("gemini", 35, 1), T("local", 25, 1), T("gemini", 34, 2),
                                                  T("local", 21, 2, misses=["tomorrow_list", "when_dentist"]),
                                                  T("local", 10, 3, src="manual")]) + "\n")
(logs / "household-eval.jsonl").write_text(json.dumps({"ts": "x", "passed": 59, "total": 59, "failing": []}) + "\n")
rr = sc.response(turns=[])
check("each brain's latest nightly, with the night before", (rr["truth"]["gemini"]["score"], rr["truth"]["gemini"]["previous"],
      rr["truth"]["local"]["score"], rr["truth"]["local"]["previous"]), (34, 35, 21, 25))
check("a drop of 3+ is a miss, a drop of 1 isn't; manual runs don't count", rr["misses"],
      ["truth [local] 21/38, down from 25: tomorrow_list, when_dentist"])
check("…and the summary says both", "truth gemini 34/38, truth local 21/38" in sc._summary("response", rr), True)
(logs / "truth-eval.jsonl").unlink()

print("\n── response: the nightly eval ──")
(logs / "household-eval.jsonl").write_text(json.dumps({"ts": "x", "passed": 56, "total": 59, "failing": ["a", "b", "c"]}) + "\n")
r = sc.response()
check("56/59 is under 95%", (r["ok"], r["misses"][0]), (False, "nightly 56/59: a, b, c"))

print("\n── response: what the reply checks did (watched, never a miss) ──")
T = [{"source": "user", "answer_corrections": 1, "untrusted_spans": 2},
     {"source": "user", "degenerate_retries": 1, "injections_disclosed": 1, "held_writes": 2, "untrusted_spans": 1},
     {"source": "user"}, {"source": "eval", "injections_disclosed": 1, "held_writes": 1}, {"source": "canary", "answer_corrections": 3}]
rc = sc.reply_checks(6, T)
check("real turns only, per check: turns touched and total",
      (rc["turns"], rc["read_others_text"], rc["answer_corrections"], rc["held_writes"], rc["injections_disclosed"],
       rc["date_corrections"]), (3, 2, {"turns": 1, "total": 1}, {"turns": 1, "total": 2}, {"turns": 1, "total": 1},
                                 {"turns": 0, "total": 0}))
r = sc.response(hours=6, turns=T)
check("in the response area, and never a miss by itself", (r["replies"] == rc, r["misses"]), (True, ["nightly 56/59: a, b, c"]))
check("the summary line", sc._replies_line(rc), "; replies over 3 turns (2 read others' text): 1 retried, 1 corrected, "
      "0 weekdays fixed, 1 injections disclosed, 1 writes held")
check("no real turns: no line", sc._replies_line(sc.reply_checks(6, [{"source": "eval"}])), "")
check("content-free: counts only", all(isinstance(v, (int, dict)) for v in rc.values()), True)

# 10-01 22:57: an unanswered confirmation ran to its 120 s timeout; that wait is the person's, not titan's.
waited = [{"source": "user", "ms": 132837, "confirm_ms": 120000,
           "device_times": [["list_calendar_events", 6937], ["update_calendar_event", 120000]]}] + \
         [{"source": "user", "ms": 4000, "device_times": [["update_calendar_event", 3000]]}] * 19
sv = sc.service(6, waited)
check("a confirmation's wait isn't titan's time: p95 and the slow tools leave it out",
      (sv["p95_s"] < 20, sv["slow_tools"].get("update_calendar_event", 0) < 10), (True, True))

# #619: an app that says how long its question was up (asked_ms) gets exactly that taken off, not the whole write.
told = [{"source": "user", "ms": 47635, "confirm_ms": 43696, "asked_ms": 43000,
         "device_times": [["list_reminders", 103], ["create_reminder", 43696, 43000]]}] * 20
st = sc.service(6, told)
check("asked_ms: the person's time comes off the turn and the tool, the rest stays",
      (st["p95_s"], st["slow_tools"]["create_reminder"], st["slow_tools"]["list_reminders"]), (4.6, 0.7, 0.1))
slow_save = [{"source": "user", "ms": 30000, "confirm_ms": 25000, "asked_ms": 0,
              "device_times": [["create_reminder", 25000, 0]]}] * 20
ss = sc.service(6, slow_save)
check("asked_ms 0: a slow write with no question is titan's time", (ss["p95_s"], ss["slow_tools"]["create_reminder"]),
      (30.0, 25.0))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
