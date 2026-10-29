#!/usr/bin/env python3
# gate: hermetic
"""Inline cites (jellybean#636, option B): after the reply is final, each sentence is aligned in code to the sources
cited_sources kept, by the same evidence per sentence (>= 3 shared content words, or >= 2 with a number or a name the
chunk has verbatim), tightened by #636: every number and every name or day in the chunk, >= 60% coverage, one
source a sentence. Wrong over missing: a sentence nothing backs gets no cite, an ambiguous anchor is dropped, and the
switch is off by default. Fictional notes; no runtime. Run: python3 scripts/test_cite_spans.py"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.pop("CITE_INLINE", None)
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


BACKUPS = {"source_id": "kb-backups", "title": "Backups",
           "text": "The nightly job backs up all Postgres databases at 02:15 to the NAS. It retains 14 days of backups "
                   "and prunes older archives every Sunday morning."}
CLINIC = {"source_id": "kb-clinic", "title": "Maple Animal Clinic",
          "text": "Hale Hardware is next door to Maple Animal Clinic on Adams Street. The clinic opens weekdays at "
                  "8 AM; Dr. Jordan sees the dogs on Tuesdays."}
TRASH = {"source_id": "kb-trash", "title": "Trash", "text": "Trash goes out Wednesday nights; recycling every other "
         "Wednesday. Bins live beside the garage."}
hits = [BACKUPS, CLINIC, TRASH]
sources = [{"source_id": "kb-backups", "title": "Backups", "snippet": ""},
           {"source_id": "kb-clinic", "title": "Maple Animal Clinic", "snippet": ""}]

print("── the switch ──")
check("off by default", lib.cite_inline_enabled(), False)
os.environ["CITE_INLINE"] = "1"
check("on with CITE_INLINE=1", lib.cite_inline_enabled(), True)
os.environ.pop("CITE_INLINE")

print("── aligned sentences ──")
reply = ("The nightly job backs up all Postgres databases at 02:15. Backups are kept for 14 days. "
         "I hope that helps with the move! Dr. Jordan sees the dogs on Tuesdays at Maple Animal Clinic.")
got = lib.cite_spans(reply, sources, hits)
check("each backed sentence cites its own source, 1-based into sources",
      [(c["n"], c["after"]) for c in got],
      [(1, "up all Postgres databases at 02:15"), (1, "Backups are kept for 14 days"),
       (2, "dogs on Tuesdays at Maple Animal Clinic")])
check("a sentence nothing backs gets nothing", any("helps" in c["after"] for c in got), False)
check("anchors are at most 40 characters, no end punctuation",
      all(len(c["after"]) <= 40 and not c["after"].endswith(".") for c in got), True)
check("only n and after go to an app", all(set(c) == {"n", "after"} for c in got), True)
ev = lib.cite_spans(reply, sources, hits, evidence=True)
check("the precision measure gets the sentence and the chunk", (ev[0]["sentence"].startswith("The nightly job"),
                                                               "02:15" in ev[0]["chunk"]), (True, True))

print("── the evidence bar ──")
two_num = lib.cite_spans("They're kept for 14 days.", sources, hits)
check(">= 2 shared with a number the chunk has verbatim backs it", [(c["n"], c["after"]) for c in two_num],
      [(1, "They're kept for 14 days")])
check("2 shared words without a number or a name don't", lib.cite_spans("Older archives go away eventually.", sources, hits), [])
check("a source that isn't cited is never cited inline (Trash backs it, but it isn't in sources)",
      lib.cite_spans("Trash goes out Wednesday nights, and recycling every other Wednesday.", sources, hits), [])
check("a 'nothing found' reply cites nothing",
      lib.cite_spans("I couldn't find anything about the Postgres databases at 02:15 or 14 days.", sources, hits), [])
check("no sources, no cites", lib.cite_spans(reply, [], hits), [])

print("── markdown and anchors ──")
md = ("**Backups:** the nightly job [backs up](https://x.y) all *Postgres* databases at `02:15`.\n"
      "- It retains 14 days of backups.\n")
got = lib.cite_spans(md, sources, hits)
check("anchors are the rendered text: links, code and emphasis stripped, list markers gone",
      [c["after"] for c in got], ["up all Postgres databases at 02:15", "It retains 14 days of backups"])
dup = "Backups are kept for 14 days. Backups are kept for 14 days."
check("an anchor that occurs twice is dropped (the app would put it on the first)", lib.cite_spans(dup, sources, hits), [])
check("a number must be a whole token: 14 isn't 140", lib.cite_spans("Backups are kept for 140 days.", sources, hits), [])
check("the plain text keeps sentence words", lib._cite_plain("# Title\n> quoted **bold** [link](u)"), "Title\nquoted bold link")

print("── limits ──")
many = " ".join(f"Shelf {chr(65 + i)}: the nightly job backs up Postgres databases at 02:15 to the NAS {chr(65 + i)}."
                for i in range(12))
check("at most 8 cites a reply", len(lib.cite_spans(many, sources, hits)), 8)
NAS = {"source_id": "kb-nas", "title": "The NAS", "text": "The NAS in the hall closet holds Postgres databases; the "
       "nightly job writes there at 02:15 and keeps 14 days."}
DUMPS = {"source_id": "kb-dumps", "title": "Dumps", "text": "Postgres databases are dumped by the nightly job at "
         "02:15, gzip, then copied off the host."}
three = [{"source_id": s, "title": s, "snippet": ""} for s in ("kb-backups", "kb-nas", "kb-dumps")]
got = lib.cite_spans("At 02:15 the job prunes older archives, keeps 14 days in the hall closet, and gzip dumps get copied "
                     "off the host.", three, [BACKUPS, NAS, DUMPS, CLINIC, TRASH])
check("a sentence stitched from three notes, none of which says it all, gets no cite (#636)", got, [])
both = lib.cite_spans("The nightly job backs up Postgres databases at 02:15.", three, [BACKUPS, NAS, DUMPS, CLINIC, TRASH])
check("notes that each back a sentence: one cite, the best", [c["n"] for c in both], [1])

print("── #636's tightening: a missing cite is fine, a wrong one isn't ──")
check("every number in the sentence is in the chunk (02:15 is, 03:00 isn't)",
      lib.cite_spans("The nightly job backs up Postgres databases at 02:15 and again at 03:00.", sources, hits), [])
check("a day the chunk doesn't have: it came from the calendar, not the note",
      lib.cite_spans("Dr. Jordan sees the dogs at Maple Animal Clinic tomorrow.", sources, hits), [])
check("a name the chunk doesn't have: from memory or the people store",
      lib.cite_spans("On Tuesdays Robin takes the dogs to Maple Animal Clinic.", sources, hits), [])
check("under 60% of the sentence in the chunk: no cite",
      lib.cite_spans("The nightly job backs up Postgres databases, compresses every snapshot, verifies checksums, "
                     "rotates encryption keys and emails a summary.", sources, hits), [])
check("a weekday the chunk has is fine", [c["n"] for c in lib.cite_spans(
      "Dr. Jordan sees the dogs on Tuesdays at Maple Animal Clinic.", sources, hits)], [2])

print("── the precision measure (cite_precision.py) ──")
import cite_precision as cp  # noqa: E402
check("the check agrees when the chunk states the sentence",
      cp.check("Backups are kept for 14 days.", BACKUPS["text"]), True)
check("…not when a number differs", cp.check("Backups are kept for 30 days.", BACKUPS["text"]), False)
check("…not when most of the sentence isn't in the chunk",
      cp.check("Postgres backups go to the cloud bucket nightly over the office fibre.", BACKUPS["text"]), False)
ev = [{"n": 1, "after": "a", "sentence": "Backups are kept for 14 days.", "chunk": BACKUPS["text"]},
      {"n": 1, "after": "b", "sentence": "Backups are kept for 30 days.", "chunk": BACKUPS["text"]}]
sc = cp.score(ev, cp.check)
check("precision is agreed / emitted, wrong ones listed", (sc["emitted"], sc["agreed"], sc["precision"], [w["after"] for w in sc["wrong"]]),
      (2, 1, 0.5, ["b"]))
check("nothing emitted: no precision, not 100%", cp.score([], cp.check)["precision"], None)

print("── one rule (titan-node#910) ──")
LONG = ("The nightly job backs up all the Postgres databases to the NAS and keeps them around for a while so that "
        "anything the household loses by mistake can usually be brought back later without much trouble at all.")
check("a long summary sentence (over 25 words) under 80% covered gets no cite",
      [c for c in lib.cite_spans(LONG, sources, hits) if "trouble" in c["after"]], [])
SHORT = "The nightly job backs up the Postgres databases to the NAS."
check("…the short claim in it still does", len(lib.cite_spans(SHORT, sources, hits)), 1)
mix = ("The nightly job backs up all Postgres databases at 02:15 to the NAS. They're kept for 14 days. " + LONG +
       " The clinic opens weekdays at 8 AM.")
ev = lib.cite_spans(mix, sources, hits, evidence=True)
check("every cite the emitter makes passes cite_check (the precision check can't disagree)",
      all(lib.cite_check(c["sentence"], c["chunk"]) for c in ev) and len(ev) >= 2, True)
import cite_precision  # noqa: E402
check("cite_precision's check is the emitter's rule", all(cite_precision.check(c["sentence"], c["chunk"]) for c in ev), True)
check("evidence names its source, for the per-surface split", sorted({c["source_id"] for c in ev}), ["kb-backups", "kb-clinic"])
check("surfaces: a runbook note vs the household KB",
      [cite_precision.surface({"source_id": x}) for x in ("note-runbook-backups", "kb-backups", "cx-mail-1")],
      ["runbook", "kb", "kb"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
