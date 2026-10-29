#!/usr/bin/env python3
# gate: hermetic
"""The nightly's alerts (10-06: a world-privacy leak, reported 3 nights running, went unnoticed inside bundled
"still failing, changed" alerts). Any privacy problem pages on its own, urgent, every night it persists; a night under
the bar sends one summary with infrastructure errors apart from real misses; harness breakage stays edge-triggered,
under a fresh state name so the old bundled state can't announce a false "recovered". Fake alert and edge: nothing
is sent. Run: python3 scripts/test_nightly_alerts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import household_eval_nightly as N  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


OUT = """== turn eval ==
  cal-tomorrow: error: TimeoutError: timed out (0/1)
  bills-due: error: HTTP 502 from the agent (0/1)
  weather-rain: said it would rain, forecast dry (0/1)
== truth eval ==
  t-007: RemoteDisconnected while reading (0/1)
  t-019: wrong date for the recital (0/1)
"""

print("── infra errors apart from real misses ──")
infra, real = N.split_failures(OUT)
check("timeouts, 5xx and dropped connections are infra; wrong answers are real", (infra, real),
      (["cal-tomorrow", "bills-due", "t-007"], ["weather-rain", "t-019"]))
check("the summary line", N.summary_line(213, 293, ["x"] * 78, ["a", "b"]), "213/293: 78 errors (infra), 2 real misses (a, b)")
check("…singular, and no infra", N.summary_line(9, 10, [], ["a"]), "9/10: 1 real miss (a)")


class Edge:
    def __init__(self):
        self.calls = []

    def update(self, problems):
        self.calls.append(dict(problems))
        return "down" if problems else None


def night(problems, rate, floor=0.85, summary="s"):
    sent, edge = [], Edge()
    kinds = N.notify(problems, rate=rate, floor=floor, summary=summary,
                     alert=lambda title, msg, **kw: sent.append((title, kw.get("priority"))), edge=edge)
    return kinds, sent, edge.calls


print("\n── who hears what ──")
kinds, sent, edge = night({"world-privacy": "leaked to a viewer in 2/8", "score": "x", "units": "u failing"}, 0.72)
check("a privacy leak pages on its own, urgent, titled PRIVACY", [s for s in sent if "PRIVACY" in s[0]],
      [("Titan PRIVACY: world-privacy", "urgent")])
check("…the night under its bar gets one summary", [s for s in sent if "under its bar" in s[0]],
      [("Titan household eval under its bar", "default")])
check("…the edge alert sees only harness problems (never privacy or the score)", edge, [{"units": "u failing"}])
check("…all three went", kinds, ["privacy", "summary", "edge"])
k2, s2, _ = night({"world-privacy": "leaked to a viewer in 2/8"}, 0.95)
check("the same leak the next night pages again (level, not edge)", [s for s in s2 if "PRIVACY" in s[0]],
      [("Titan PRIVACY: world-privacy", "urgent")])
k3, s3, _ = night({"privacy-eval": "did not produce a score"}, 0.9)
check("a privacy eval that didn't run pages too (no proof there was no leak)", [s[1] for s in s3], ["urgent"])
k4, s4, e4 = night({}, 0.97)
check("a good night: nothing sent", (k4, s4, e4), ([], [], [{}]))
k5, s5, _ = night({"score": "s"}, 0.5, summary="146/293: 140 errors (infra), 7 real misses")
check("a deploy-mid-eval night says so in its summary", s5 and "under its bar" in s5[0][0], True)

print("\n── #1517: new misses and chronic ones, by case name ──")
H = [{"failing": ["a", "b"], "total": 300}, {"real": ["a", "b", "c"], "total": 305}, {"real": ["a", "c"], "total": 310}]
check("a miss 3 nights running is chronic; one that passed last night is new",
      N.new_and_chronic(H, ["a", "b", "d"]), (["b", "d"], ["a"]))
check("…totals changing every night doesn't hide a streak (the old check needed equal totals)",
      N.new_and_chronic(H, ["c"]), ([], ["c"]))
check("…a miss that skipped a night starts over", N.new_and_chronic(H, ["b"]), (["b"], []))
check("…two nights isn't chronic yet", N.new_and_chronic(H[-1:], ["a"]), ([], []))
check("…with no history nothing is new (the first night has nothing to compare)", N.new_and_chronic([], ["a"]), ([], []))
check("…lines from before #1517 count by their failing list", N.new_and_chronic([{"failing": ["x"]}, {"failing": ["x"]}], ["x"]),
      ([], ["x"]))
check("…a 10-night streak is still one chronic name", N.new_and_chronic([{"real": ["x"]}] * 10, ["x"]), ([], ["x"]))

print("\n── wired into the nightly ──")
src = Path(N.__file__).read_text()
check("main() calls notify with titan_alert and a fresh edge state name",
      ("notify(problems," in src, 'EdgeAlert("household-eval-harness"' in src, 'EdgeAlert("household-eval", ' in src),
      (True, True, False))
check("the history line keeps infra errors and real misses", ('"infra_errors": len(infra)' in src, '"real_misses": len(real)' in src),
      (True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
