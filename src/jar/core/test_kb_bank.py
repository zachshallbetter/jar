#!/usr/bin/env python3
# gate: hermetic
"""The KB eval bank (#862): cases generated per kind from the KB's own items, real questions joined to what titan
cited, misses added by hand; retired when their items leave; scored per kind (recall@3, wrong zone) against a baseline;
a new miss and a kind below baseline reach the scorecard. Made-up items and questions, throwaway files, no runtime.
Run: python3 scripts/test_kb_bank.py"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kbbank-"))
os.environ["KB_BANK"] = str(_TMP / "bank.jsonl")
sys.path.insert(0, str(Path(__file__).parent))
import kb_bank as B  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")


SRC = [
    {"source_id": "cx-calendar-aa", "source_type": "connector:calendar", "title": "Ava's ballet recital"},
    {"source_id": "cx-mail-bb", "source_type": "connector:mail", "title": "Re: Three Websites for Hale Studio"},
    {"source_id": "note-cc", "source_type": "note", "title": "Garbage and recycling schedule"},
    {"source_id": "web-dd", "source_type": "web", "title": "Spotlight Memory | Example Blog"},
    {"source_id": "note-runbook-ee", "source_type": "knowledge_record", "title": "Titan runbook — Sandbox boxes: A/B runs"},
]
META = {"cx-calendar-aa": {"source": "calendar", "owner": "zach"}, "cx-mail-bb": {"source": "mail", "owner": "zach"},
        "note-cc": {"owner": "zach"}, "web-dd": {"url": "https://example.org", "owner": "zach"},
        "note-runbook-ee": {"origin": "kb_seed_runbook.py", "owner": None}}
meta = lambda sid: META.get(sid, {})  # noqa: E731

print("── generated from the KB's own items ──")
TEXT = {"note-runbook-ee": "Backups run nightly at 02:15 and restore.sh brings one back."}
ASKS = []


def ask_q(text):
    ASKS.append(text)
    return "When do backups run each night?" if "Backups" in text else None


gen = B.generate(SRC, meta, text_of=TEXT.get, ask_q=ask_q)
check("one case per item here, one per kind", sorted(c["kind"] for c in gen), ["calendar", "facts", "mail", "system", "web"])
check("the answer key is the item each case was made from", sorted(c["expect"][0] for c in gen),
      sorted(s["source_id"] for s in SRC))
check("salient words: no 'Re:', no site suffix, no 'Titan runbook'",
      (B.salient("Re: Three Websites for Hale Studio"), B.salient("Spotlight Memory | Example Blog"),
       B.salient("Titan runbook — Sandbox boxes: A/B runs")), ("Three Websites Hale", "Spotlight Memory", "Sandbox boxes runs"))
check("questions take real shapes", all(c["q"].endswith(("?", ".")) and len(c["q"].split()) >= 3 for c in gen), True)
check("at most PER_ZONE per kind", len(B.generate([{"source_id": f"note-{i}", "source_type": "note", "title": f"Note number {i} topic"}
                                                   for i in range(40)], lambda s: {})), B.PER_ZONE)

print("── real questions joined to what titan cited ──")
mem = _TMP / "memory.db"
m = sqlite3.connect(str(mem))
m.execute("CREATE TABLE messages (id TEXT, conv_id TEXT, role TEXT, content TEXT, ts REAL)")
m.execute("INSERT INTO messages VALUES ('1','c','user','When is the recycling picked up?',?)", (time.time(),))
m.execute("INSERT INTO messages VALUES ('2','c','user','Tell me a joke',?)", (time.time(),))
m.commit(); m.close()
tr = _TMP / "trace.db"
t = sqlite3.connect(str(tr))
t.execute("CREATE TABLE queries (id INTEGER PRIMARY KEY, turn_id TEXT, at REAL, person TEXT, path TEXT, qhash TEXT, qwords INT, corrected INT)")
t.execute("CREATE TABLE hits (query INT, rank INT, source_id TEXT, source_type TEXT, score REAL, stage TEXT, cited INT, rerank REAL)")
qh = lambda s: hashlib.sha256(s.strip().lower().encode()).hexdigest()[:16]  # noqa: E731
t.execute("INSERT INTO queries VALUES (1,'t',?,'zach','passive',?,6,0)", (time.time(), qh("When is the recycling picked up?")))
t.execute("INSERT INTO hits VALUES (1,1,'note-cc','note',0.8,'shown',1,NULL)")
t.execute("INSERT INTO queries VALUES (2,'t',?,'zach','passive',?,3,0)", (time.time(), qh("Tell me a joke")))
t.execute("INSERT INTO hits VALUES (2,1,'web-dd','web',0.4,'shown',0,NULL)")
t.commit(); t.close()
cit = B.cited(tr, mem, meta)
check("a cited question becomes a case keyed to what was cited; an uncited one doesn't",
      [(c["q"], c["expect"], c["kind"]) for c in cit], [("When is the recycling picked up?", ["note-cc"], "facts")])

print("── #390: cases a search can't answer on its own ──")
dup = [{"source_id": "cx-mail-s1", "source_type": "connector:mail", "title": "Security alert"},
       {"source_id": "cx-mail-s2", "source_type": "connector:mail", "title": "Security  alert"},
       {"source_id": "cx-mail-s3", "source_type": "connector:mail", "title": "Garden club newsletter"}]
g = {c["q"]: c["expect"] for c in B.generate(dup, lambda sid: {"source": "mail", "owner": "zach"})}
check("a title two mails share expects either; a unique one just its own",
      sorted(g.values()), [["cx-mail-s1", "cx-mail-s2"], ["cx-mail-s1", "cx-mail-s2"], ["cx-mail-s3"]])
check("a follow-up that names nothing isn't a question on its own",
      [B.meaningful(q) for q in ("And the chat?", "What about it?", "When is the recycling picked up?",
                                 "Any mail about Hale Studio?")], [False, False, True, True])
m = sqlite3.connect(str(mem))
m.execute("INSERT INTO messages VALUES ('3','c','user','And the chat?',?)", (time.time(),))
m.commit(); m.close()
t = sqlite3.connect(str(tr))
t.execute("INSERT INTO queries VALUES (3,'t',?,'zach','passive',?,3,0)", (time.time(), qh("And the chat?")))
t.execute("INSERT INTO hits VALUES (3,1,'cx-mail-bb','connector:mail',0.5,'shown',1,NULL)")
t.commit(); t.close()
check("a cited follow-up never becomes a case", [c["q"] for c in B.cited(tr, mem, meta)], ["When is the recycling picked up?"])
t = sqlite3.connect(str(tr))
t.execute("INSERT INTO queries VALUES (4,'t-77',?,'zach','passive',?,6,0)", (time.time(), qh("Where is the recycling depot?")))
t.execute("INSERT INTO queries VALUES (5,'t-77',?,'zach','search',?,3,0)", (time.time(), qh("recycling depot location")))
t.execute("INSERT INTO hits VALUES (5,1,'note-cc','note',0.7,'shown',1,NULL)")
t.commit(); t.close()
m = sqlite3.connect(str(mem))
m.execute("INSERT INTO messages VALUES ('4','c','user','Where is the recycling depot?',?)", (time.time(),))
m.commit(); m.close()
check("the model's own cited search takes its turn's question (its query hash is never the person's words)",
      [(c["q"], c["expect"]) for c in B.cited(tr, mem, meta) if "depot" in c["q"]], [("Where is the recycling depot?", ["note-cc"])])
B.save([{"id": "cit-old", "q": "And the chat?", "expect": ["cx-mail-bb"], "zone": "sync", "kind": "mail", "origin": "cited",
         "owner": "zach"}])
B.harvest(SRC, meta, cited_cases=[])
check("…and one already in the bank is dropped at the next harvest", any(c["id"] == "cit-old" for c in B.load()), False)
B.save([])

print("── runbook parts: specific questions, whole families (#371, 10-05) ──")
book = [{"source_id": f"rb-{i}", "source_type": "knowledge_record", "title": t} for i, t in enumerate((
    "Titan runbook — Reliability — GPU layout", "Titan runbook — Reliability — GPU layout (2)",
    "Titan runbook — Reliability — Disks", "Titan runbook — First thing, every time: verify live state"))]
BOOK_TEXT = {"rb-0": "GPU1 runs the household model on :8010.", "rb-1": "GPU0 and GPU2 run the code model on :8198.",
             "rb-2": "The Seagate disk died; datasets moved to NVMe.", "rb-3": "Check the canary before any change."}
QS = {"rb-0": "Which port does the household model use?", "rb-1": "Which GPUs run the code model?",
      "rb-2": "Where do the datasets live now?", "rb-3": "What do I check before a change?"}
runbook_meta = lambda sid: {"origin": "kb_seed_runbook.py", "owner": None}  # noqa: E731
ASKS.clear()
gb = {c["q"]: c["expect"] for c in B.generate(book, runbook_meta, text_of=BOOK_TEXT.get,
                                              ask_q=lambda t: next(QS[k] for k, v in BOOK_TEXT.items() if v == t))}
check("a runbook note's question is written from its own text, never a title template (#1225)",
      sorted(gb), sorted(QS.values()))
check("…no '{k}' template exists for the system zone", "system" in B._TEMPLATES, False)
check("…and a part still expects its whole family (the numbered parts)", gb["Which port does the household model use?"],
      ["rb-0", "rb-1"])
Path(str(os.environ["KB_BANK"]) + ".questions.json").unlink()   # no cached questions for the next two
check("no text, or no question from the model (and none cached): no system case (never a fallback to the title)",
      (B.generate(book, runbook_meta), B.generate(book, runbook_meta, text_of=BOOK_TEXT.get, ask_q=lambda t: None)), ([], []))
check("…nor a non-question back", B.generate(book, runbook_meta, text_of=BOOK_TEXT.get, ask_q=lambda t: "{k} stuff"), [])
B.generate(book, runbook_meta, text_of=BOOK_TEXT.get, ask_q=lambda t: next(QS[k] for k, v in BOOK_TEXT.items() if v == t))
ASKS.clear()
again = B.generate(book, runbook_meta, text_of=BOOK_TEXT.get, ask_q=lambda t: ASKS.append(t) or "Unused question here?")
check("questions are cached per note and text: unchanged notes ask nothing, keep their question and id",
      (ASKS, sorted(c["q"] for c in again)), ([], sorted(QS.values())))
BOOK_TEXT["rb-2"] = "The datasets live on a 600 GB NVMe volume."
changed = B.generate(book, runbook_meta, text_of=BOOK_TEXT.get, ask_q=lambda t: ASKS.append(t) or "How big is the datasets volume?")
check("a changed note is asked again and makes a new case id", (len(ASKS), "How big is the datasets volume?" in {c["q"] for c in changed},
      len({c["id"] for c in changed} & {c["id"] for c in again})), (1, True, 3))
check("the question cache is private (0600)", oct(Path(str(os.environ["KB_BANK"]) + ".questions.json").stat().st_mode & 0o777), "0o600")
try:
    B.passage_question("x", url="https://api.example.com")
    check("passage questions: never a hosted model", "asked", "refused")
except ValueError:
    check("passage questions: never a hosted model", "refused", "refused")
check("other kinds keep their templates", any(c["q"] == "When is Dentist?" or c["q"].startswith(("When is", "What time", "Is "))
                                               for c in B.generate([{"source_id": "cx-calendar-q1", "source_type": "connector:calendar",
                                                                     "title": "Dentist"}], lambda sid: {"source": "calendar"})), True)
cur = {c["q"]: c["expect"] for c in B.curated(book)}
check("curated runbook questions resolve to their section's family", (cur.get("Which GPU does the local model use?"),
      cur.get("What do I verify before touching titan?")), (["rb-0", "rb-1"], ["rb-3"]))
check("…a section the KB doesn't have yet makes no case", "How do I deploy titan-node?" in cur, False)
check("…written without household names", any(n in q for q, _ in B.CURATED for n in ("Zach", "Miranda", "Conor", "Kate",
                                                                                         "Sabrina")), False)

print("── harvest: misses kept, gone items retired, 0600 ──")
B.add("Is this interesting? https://example.org/r/HomeLab", ["web-dd"], meta, note="zone replay 10-04")
st = B.harvest(SRC, meta, cited_cases=cit)
check("generated + cited + the miss + the curated runbook case that resolves here (no text_of here: the runbook note "
      "makes no generated case, #1225)",
      (st["cases"], st["origin generated"], st["origin cited"], st["origin miss"], st["origin curated"]), (7, 4, 1, 1, 1))
check("the bank is private", oct(Path(os.environ["KB_BANK"]).stat().st_mode & 0o777), "0o600")
st = B.harvest([s for s in SRC if s["source_id"] != "web-dd"], meta, cited_cases=[])
check("the page left the KB: its generated case and the miss are retired; the cited case stays",
      (st["retired"], sorted(c["origin"] for c in B.load())), (1, ["cited", "curated", "generated", "generated", "generated"]))
B.harvest(SRC, meta, cited_cases=cit)
B.add("Is this interesting? https://example.org/r/HomeLab", ["web-dd"], meta)

print("── scoring per kind ──")
cases = B.load()
right = lambda q, c: [{"source_id": c["expect"][0]}, {"source_id": "x"}]  # noqa: E731
wrong = lambda q, c: [{"source_id": "x"}] if c["kind"] == "mail" else right(q, c)  # noqa: E731
r1 = B.score(cases, right, lambda o: "admin")
check("all found", (r1["found"], r1["cases"], r1["misses"]), (len(cases), len(cases), []))
check("wrong zone counted where the plan misses the answer's zone (the generated mail question routes to mail)",
      r1["by_kind"]["mail"]["wrong_zone"], 0)
check("first score sets the baseline", B.against_baseline(r1), [])
ops = [{"id": "cur-1", "q": "How do scheduled actions work?", "expect": ["seed-1"], "zone": "system", "kind": "system"}]
check("wrong zone judged on the search plan (its runbook slot), the plan query() runs; never for a role without the zone",
      (B.score(ops, right, lambda o: "admin")["wrong_zone"], B.score(ops, right, lambda o: "kid")["wrong_zone"],
       B.score(ops, right, lambda o: "admin", about_titan=lambda q: True)["wrong_zone"]), (0, 1, 0))
r2 = B.score(cases, wrong, lambda o: "admin")
check("mail drops: a miss, and below baseline", (len(r2["misses"]), B.against_baseline(r2)), (1, ["mail 0% < baseline 100%"]))
check("a log line names case ids, never the question", any(" " in x for x in r2["misses"]), False)

print("── the scorecard flags it ──")
import scorecard  # noqa: E402
logs = _TMP / "logs"
logs.mkdir()
base = {"found": 10, "found_total": 10, "not_stored": 3, "not_stored_total": 3, "failing": []}
(logs / "retrieval-eval.jsonl").write_text(json.dumps({**base, "bank": {k: r1[k] for k in ("by_kind", "cases", "found", "wrong_zone", "misses")} | {"below_baseline": []}}) + "\n"
                                           + json.dumps({**base, "bank": {k: r2[k] for k in ("by_kind", "cases", "found", "wrong_zone", "misses")} | {"below_baseline": B.against_baseline(r2)}}) + "\n")
rows = scorecard._jsonl(logs / "retrieval-eval.jsonl")
check("a kind below baseline and the new miss are scorecard misses", [m.split(":")[0] for m in scorecard.bank_misses(rows)],
      ["KB bank below baseline", "KB bank"])
check("a quiet night: none", scorecard.bank_misses(rows[:1]), [])

print("── identifier cases (#1123) ──")
SRC = [{"source_id": "cx-mail-1", "title": "Your order shipped"}, {"source_id": "cx-mail-2", "title": "Order update"},
       {"source_id": "note-1", "title": "Printer"}, {"source_id": "note-2", "title": "Lease"}]
TXT = {"cx-mail-1": "Order PS-1001-77 shipped. Track it online.", "cx-mail-2": "Order PS-1001-77 delivered; see ZX-4410.",
       "note-1": "The printer shows E0451 when the tray jams.", "note-2": "Signed copy saved as lease-2026.pdf."}
idc = B.identifiers(SRC, lambda sid: {"source": "mail"} if sid.startswith("cx-mail") else {}, TXT.get)
check("one case per identifier found in exactly one item; a shared one (two mails) left out",
      sorted((c["q"], c["expect"][0]) for c in idc),
      sorted([("Anything about E0451?", "note-1"), ("Anything about ZX-4410?", "cx-mail-2"),
              ("Anything about lease-2026.pdf?", "note-2")]))
check("origin and kind", sorted({(c["origin"], c["kind"]) for c in idc}),
      [("identifier", "facts"), ("identifier", "mail")])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
