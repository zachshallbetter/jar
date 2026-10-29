#!/usr/bin/env python3
# gate: hermetic
"""The A/B readout with its A/A band (titan-node#1144): fictional per-turn files in a temp dir, counts only.
  - a category's drop counts only beyond |C-A| (the control's swing); with no control, every drop is flagged;
  - a case is flagged only when A-B is beyond |A-C|;
  - --set wants_schedule keeps only the questions that get the schedule digest;
  - nothing a turn said (question, reply, expected) is ever printed;
  - ab_run.sh parses, archives each arm, refuses the eval windows, deletes the per-turn files and only its own hold.
Run: python3 scripts/test_ab_readout.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ab_readout as R  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


SECRET = "zz-reply-text-never-printed"
D = Path(tempfile.mkdtemp(prefix="titan-ab-readout-"))


def write(arm, n, client, rows):
    with open(D / f"{arm}-{n}-{client}.jsonl", "w") as fh:
        for name, cat, q, ok in rows:
            fh.write(json.dumps({"brain": "local", "name": name, "category": cat, "ok": ok, "question": q,
                                 "reply": SECRET, "expected": SECRET, "problems": [], "tools": [], "ms": 1000}) + "\n")


SCHED = "Do I have any conflicts tomorrow?"     # wants_schedule
OTHER = "What's the most recent thing on the household timeline?"
# 3 passes. calendar: A 2,2,2  B 1,1,1 (drop 1.0)  C 2,1,2 (swing 0.33) → beyond the band
# knowledge: A 1,0,1  B 0,0,1 (drop 0.33)  C 0,0,0 (swing 0.67) → inside the band
for n in (1, 2, 3):
    write("A", n, "iphone", [("c1", "calendar", SCHED, True), ("c2", "calendar", SCHED, True),
                             ("k1", "knowledge", OTHER, n != 2)])
    write("B", n, "iphone", [("c1", "calendar", SCHED, True), ("c2", "calendar", SCHED, False),
                             ("k1", "knowledge", OTHER, n == 3)])
    write("C", n, "iphone", [("c1", "calendar", SCHED, True), ("c2", "calendar", SCHED, n != 2),
                             ("k1", "knowledge", OTHER, False)])
(D / "turns-7773.jsonl").write_text("{}\n")      # not an arm file: ignored

r = R.readout(R.load(str(D)))
cats = {c["category"]: c for g in r["groups"] for c in g["categories"]}
check("means per pass", (cats["calendar"]["A"], cats["calendar"]["B"], cats["calendar"]["C"]), (2.0, 1.0, 1.67))
check("the band is |C-A|, at least one flip (1/passes)", (cats["calendar"]["band"], cats["knowledge"]["band"]), (0.33, 0.67))
check("calendar's drop is beyond its band: flagged", cats["calendar"]["drop"], True)
check("knowledge's drop is inside its band: not flagged", (cats["knowledge"]["delta"], cats["knowledge"]["drop"]), (-0.33, False))
check("…so the drops list holds calendar only", [d["category"] for d in r["drops"]], ["calendar"])
check("case c2 (A 3/3, B 0/3, A' 2/3) is flagged; k1 (A 2/3, B 1/3: one flip) isn't",
      [(c["case"], c["drop"]) for c in r["case_flags"]], [("c2", True)])
s = R.readout(R.load(str(D), "wants_schedule"))
check("--set wants_schedule: only the schedule questions count",
      sorted({c["category"] for g in s["groups"] for c in g["categories"]}), ["calendar"])
for arm in "C":
    for n in (1, 2, 3):
        (D / f"{arm}-{n}-iphone.jsonl").unlink()
nc = R.readout(R.load(str(D)))
check("no control: drops beyond one flip are flagged (calendar -1.0), a single flip isn't (knowledge -0.33)",
      ([d["category"] for d in nc["drops"]], nc["control"]), (["calendar"], False))
out = subprocess.run([sys.executable, str(HERE / "ab_readout.py"), str(D)], capture_output=True, text=True).stdout
check("the CLI prints counts and the verdict, never what a turn said",
      ("DROPS beyond the band: local iphone calendar -1.00" in out, SECRET in out,
       SCHED in out, OTHER in out), (True, False, False, False))

# the first A/A/A run (10-07): B ran A's code, dropped one flip where C happened not to move; that's noise, not a drop
E = Path(tempfile.mkdtemp(prefix="titan-ab-aaa-"))
for n in (1, 2, 3):
    for arm, ok in (("A", n != 3), ("B", n == 1), ("C", n != 3)):   # A 2/3, B 1/3, C 2/3
        with open(E / f"{arm}-{n}-mac.jsonl", "w") as fh:
            fh.write(json.dumps({"brain": "local", "name": "s1", "category": "schedule", "ok": ok, "question": OTHER,
                                 "reply": SECRET, "ms": 1}) + "\n")
aaa = R.readout(R.load(str(E)))
check("A/A/A: one flip with C still (band 0 → the one-flip floor) is not a drop, and the case isn't flagged",
      (aaa["drops"], aaa["case_flags"], aaa["groups"][0]["categories"][0]["band"]), ([], [], 0.33))

print("\n── ab_run.sh ──")
sh = (HERE / "ab_run.sh").read_text()
check("it parses", subprocess.run(["bash", "-n", str(HERE / "ab_run.sh")]).returncode, 0)
check("three boxes: A, B, and the control running A's code",
      ('declare -A REV=([A]=$A [B]=$B [C]=$A)' in sh, "--no-control" in sh), (True, True))
check("refuses inside the eval windows (it asks the one list, #1518)", 'gate_queue.sh" window' in sh and "refused: inside an eval window" in sh, True)
check("deletes the per-turn files (they hold replies) and the code copies",
      ('rm -f -- "$D"/[ABC]-*.jsonl' in sh, 'rm -rf -- "$D"/code-*' in sh), (True, True))
check("removes the hold only if it's still its own", '[ "$tag" = "$TAG" ]' in sh, True)
check("every arm is checked by the same harness (the deploy checkout's truth_eval)", 'TE=$REPO/scripts/truth_eval.py' in sh, True)

# #1144: --start is the next occurrence, never "already past because the HHMM is bigger" (a 10:15 run started at 23:27)
import shutil  # noqa: E402
fn = sh[sh.index("next_at() {"):].split("\n", 1)[0]
gnu = next((d for d in ("date", "gdate") if shutil.which(d) and subprocess.run([d, "-u", "-d", "@0", "+%s"],
                                                                                capture_output=True).returncode == 0), None)
if gnu:   # titan (the gate) has GNU date; a Mac without coreutils can't run this probe
    shim = "" if gnu == "date" else 'date() { gdate "$@"; }\n'
    probe = subprocess.run(["bash", "-c", shim + fn + '\nnow=$(date -u +%s); t=$(next_at "$(date -u -d @$((now - 3600)) +%H:%M)"); '
                            'u=$(next_at "$(date -u -d @$((now + 3600)) +%H:%M)"); echo $(( (t - now) / 60 )) $(( (u - now) / 60 ))'],
                           capture_output=True, text=True).stdout.split()
    check("a start an hour ago today means tomorrow (~23 h away); one in an hour means today",
          (1370 <= int(probe[0]) <= 1381, 50 <= int(probe[1]) <= 61), (True, True))
else:
    print("  (no GNU date here: the next-occurrence probe runs on titan's gate)")
check("the hold is the next --hold-until too, not 'today' in the past", 'until=$(next_at "$HOLD")' in sh, True)


print("── #402: a box-profile arm has no Gemini: its gemini rows are left out, and said so ──")
F = Path(tempfile.mkdtemp(prefix="titan-ab-boxlive-"))
for arm, brains in (("A", {"gemini": True, "local": True}), ("B", {"gemini": False, "local": True}), ("C", {"gemini": True, "local": True})):
    for n in (1, 2):
        with open(F / f"{arm}-{n}-iphone.jsonl", "w") as fh:
            for brain, ok in brains.items():
                fh.write(json.dumps({"brain": brain, "name": "c1", "category": "calendar", "ok": ok, "question": OTHER,
                                     "reply": SECRET, "problems": [], "ms": 1000}) + "\n")
(F / "profiles.json").write_text('{"A": "live", "B": "box", "C": "live"}\n')
bl = R.readout(R.load(str(F)))
check("no gemini DROP from the box arm (0 by design)", [d for d in bl["drops"] if d["brain"] == "gemini"], [])
check("…its gemini group has no B", [bool(g["B"]) for g in bl["groups"] if g["brain"] == "gemini"], [False])
check("…and no case flag for it", [c for c in bl["case_flags"] if c["brain"] == "gemini"], [])
check("the readout says what wasn't compared", "(not compared: B (box profile: no Gemini" in R.render(bl), True)
check("the local comparison is untouched", [(g["brain"], bool(g["B"])) for g in bl["groups"] if g["brain"] == "local"], [("local", True)])
(F / "profiles.json").unlink()
check("without profiles.json (an older run), nothing is skipped", bool([d for d in R.readout(R.load(str(F)))["drops"]
      if d["brain"] == "gemini"]), True)
sh2 = (Path(__file__).parent / "ab_run.sh").read_text()
check("ab_run writes each arm's profile", 'profiles.json' in sh2 and '"${PROF[B]}"' in sh2, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
