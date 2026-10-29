#!/usr/bin/env python3
"""Offline tests for kb_seed_runbook: section split, unchanged/skip/delete planning, secret guard,
and that the real runbook splits cleanly and has no section the guard would skip.

Run: python3 scripts/test_kb_seed_runbook.py
"""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(__file__))
os.environ["KB_ACL_DB"] = os.path.join(tempfile.mkdtemp(prefix="titan-seed-"), "kb_acl.db")   # never the live table

import kb_seed_runbook as k

fails = 0


def check(label, got, expected):
    global fails
    ok = got == expected
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


doc = "# Title\nintro\n## First `thing`\nalpha\n\n## Empty\n\n## Backups\nnightly at 03:10\n"
secs = k.sections(doc)
check("sections split, empty dropped, backticks stripped", [t for t, _ in secs],
      [k.PREFIX + "First thing", k.PREFIX + "Backups"])
check("body kept", secs[1][1], "nightly at 03:10")

existing = {k.PREFIX + "First thing": "s1", k.PREFIX + "Gone": "s2"}
p = k.plan(secs, existing, {"s1": "alpha  "})
check("unchanged text left alone", p["same"], [k.PREFIX + "First thing"])
check("a mid-word chunk break isn't a change",
      k.plan([(k.PREFIX + "D", "Unit: decision-broker.service")], {k.PREFIX + "D": "s9"},
             {"s9": "Unit: de\ncision-broker.service"})["same"], [k.PREFIX + "D"])
check("new section written", [t for t, _ in p["write"]], [k.PREFIX + "Backups"])
check("removed section deleted", p["delete"], [("s2", k.PREFIX + "Gone")])

for bad in ("key: -----BEGIN OPENSSH PRIVATE KEY-----", "TITAN_ALERT_NTFY_URL=https://ntfy.sh/abcdefghijklmnopqrst",
            "api_key = 'AbCdEf0123456789XyZ0'", "gemini AIzaSyA1234567890abcdefghijklmnopqrstu"):
    check(f"secret guard skips: {bad[:30]}", k.plan([(k.PREFIX + "S", bad)], {}, {})["skip"], [k.PREFIX + "S"])
check("placeholder is fine", k.plan([(k.PREFIX + "S", "https://ntfy.sh/<unguessable-topic>")], {}, {})["skip"], [])

real = k.sections(k.RUNBOOK.read_text(encoding="utf-8"))
check("real runbook has sections", len(real) >= 5, True)
check("no real section trips the secret guard", k.plan(real, {}, {})["skip"], [])

# titan-node#912: a long section as notes that each fit a chunk, every one carrying its heading
short = k.parts(k.PREFIX + "Backups", "nightly at 03:10")
check("a short section stays one note, unchanged", short, [(k.PREFIX + "Backups", "nightly at 03:10")])
para = "word " * 120
long_body = ("Intro line about the section.\n\n**GPU layout.** " + para + "\n\n" + para +
             "\n\n1. **Never run a fleet change without preflight**, ever.\n\n### Disks\n" + "disk " * 60)
ps = k.parts(k.PREFIX + "Reliability", long_body)
check("split at bold leads, numbered bold leads and ### headings",
      [t[len(k.PREFIX):] for t, _ in ps][:4],
      ["Reliability", "Reliability — GPU layout", "Reliability — GPU layout (2)", "Reliability — Never run a fleet change without preflight"])
check("every part fits one chunk", all(len(b) <= k.PART_MAX + 80 for _, b in ps), True)
check("every part starts with its own heading line", all(b.startswith(t[len(k.PREFIX):] + "\n\n") for t, b in ps), True)
check("titles are unique", len({t for t, _ in ps}), len(ps))
check("nothing is lost", "".join(long_body.split()) in "".join("".join(b.split("\n\n", 1)[1] for _, b in ps).split()) or
      all(w in " ".join(b for _, b in ps) for w in ("Intro", "GPU", "preflight", "Disks")), True)
rp = [x for t, b in real for x in k.parts(t, b)]
check("the real runbook: more, smaller notes", (len(rp) > len(real), sum(len(b) > 1200 for _, b in rp) < len(real)), (True, True))
gpu = [b for _, b in rp if "titan-code-model` (Qwen3.6-35B-A3B" in b]
check("the code model's facts sit in one self-contained note (#912)", len(gpu) == 1 and all(x in gpu[0] for x in
      (":8198", "GPU0", "GPU2", "titan-code-model")), True)

# #1613: the household's own projects, one note per docs/kb-projects/*.md, the admin's own and private
import household_identity as hh
import kb_privacy
import zones
pn = k.project_notes()
check("one note per project file", [n["sid"] for n in pn],
      ["hp-ase", "hp-fundamental-engine", "hp-jar", "hp-jellybean", "hp-titan"])
check("owner is the admin person, audience owner only", {(n["owner"], n["audience"]) for n in pn},
      {(hh.ADMIN_PERSON_ID, "private")})
check("each note is 5-12 lines and dated", all(5 <= len(n["text"].split("\n\n", 1)[1].splitlines()) <= 12
                                              and "As of 2026-" in n["text"] for n in pn), True)
check("each note starts with its title", all(n["text"].startswith(n["title"] + "\n\n") for n in pn), True)
check("no project note trips the secret guard", k.plan_projects(pn, set(), {})["skip"], [])
by = {n["sid"]: n for n in pn}
check("FE is the Fundamental Engine, never front end", all(w in by["hp-fundamental-engine"]["text"]
      for w in ("Fundamental Engine", "FE", "never \"front end\"")), True)
check("Jar says it isn't a sandbox script or titan's hardware", all(w in by["hp-jar"]["text"]
      for w in ("Jar", "not a sandbox script", "not titan's own server hardware")), True)
check("ASE's note covers the Swift migration", "Swift migration" in by["hp-ase"]["title"], True)
for n in pn:
    md = kb_privacy.stamp(n["metadata"], n["owner"], n["audience"])
    who = [kb_privacy.visible_acl(*kb_privacy.acl_of(md), person, role)
           for person, role in ((hh.ADMIN_PERSON_ID, "admin"), ("robin", "adult"), ("leo", "kid"), (None, "household"))]
    check(f"{n['sid']}: only the admin sees it", who, [True, False, False, False])
    check(f"{n['sid']}: an engineering agent doesn't", kb_privacy.kb_visible(md, None, kb_privacy.ENGINEERING_ROLE,
                                                                             source_id=n["sid"]), False)
    check(f"{n['sid']}: a household fact, not a system note", (zones.zone_of(md, n["sid"], n["source_type"]),
                                                               kb_privacy.system_meta(md)), ("facts", False))
check("'what's FE?' searches the facts zone", "facts" in [z for z, _ in zones.plan("what's FE?", "admin", "zach")], True)

pp = k.plan_projects(pn, {"hp-jar", "hp-old", "note-mine"}, {"hp-jar": by["hp-jar"]["text"].replace("\n", "\n ")})
check("unchanged project note left alone", pp["same"], ["hp-jar"])
check("the rest written", sorted(n["sid"] for n in pp["write"]), sorted(s for s in by if s != "hp-jar"))
check("a removed project's note deleted, never anyone else's", pp["delete"], ["hp-old"])
with tempfile.TemporaryDirectory() as d:
    open(os.path.join(d, "a.md"), "w").write("# Alpha\n\nAs of 2026-01-01.\napi_key = 'AbCdEf0123456789XyZ0'\n")
    open(os.path.join(d, "b.md"), "w").write("no heading here\n")
    open(os.path.join(d, "c.md"), "w").write("# Empty\n")
    tn = k.project_notes(d)
    check("only files with a heading and a body", [n["sid"] for n in tn], ["hp-a"])
    check("a secret in a project file is skipped", k.plan_projects(tn, set(), {})["skip"], ["hp-a"])

print(f"\n{'All tests passed.' if not fails else f'{fails} FAILED.'}")
sys.exit(1 if fails else 0)
