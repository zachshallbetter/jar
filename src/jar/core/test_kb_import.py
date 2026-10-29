#!/usr/bin/env python3
"""
Unit tests for kb_import.build(): stable ids, secrets kept out by default, empty entries skipped.
Hermetic: no runtime calls.

Run: python3 scripts/test_kb_import.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import yaml
import kb_import as k

DOC = yaml.safe_load("""
people:
  - {name: Alex, relation: partner, birthday: 1990-02-27, allergies: [penicillin]}
  - {name: "", relation: ""}
contacts:
  - {role: vet, name: Maple Animal Clinic, phone: 555-0100}
  - {role: vet, name: Maple Animal Clinic, phone: 555-0199}
access:
  - {name: Front door, code: "4417", kept_in: 1Password}
network:
  - {name: HomeNet, password: maplesky}
schedules:
  - {when: Tuesday night}
""")


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f": {detail}" if detail and not cond else ""))
    return bool(cond)


def run():
    ok = True
    notes, problems = k.build(DOC, include_secrets=False)
    ids = sorted(notes)
    ok &= check("stable ids", "hh-people-alex-partner" in notes and "hh-contacts-vet-maple-animal-clinic" in notes, ids)
    ok &= check("duplicate names get a suffix", "hh-contacts-vet-maple-animal-clinic-2" in notes, ids)
    ok &= check("empty entry skipped", not any("people-" in i and "alex" not in i for i in ids), ids)
    ok &= check("nameless entry reported", any("schedules" in p for p in problems), problems)
    door = notes["hh-access-front-door"]["text"]
    ok &= check("secret kept out by default", "4417" not in door and "kept off the knowledge base (in 1Password)" in door, door)
    ok &= check("wifi password kept out", "maplesky" not in notes["hh-network-wi-fi-homenet"]["text"], ids)
    ok &= check("not marked sensitive when kept out", not notes["hh-access-front-door"]["metadata"]["sensitive"])
    notes2, _ = k.build(DOC, include_secrets=True)
    door2 = notes2["hh-access-front-door"]["text"]
    ok &= check("--include-secrets stores the value", "Code: 4417" in door2, door2)
    ok &= check("... and marks the note sensitive", notes2["hh-access-front-door"]["metadata"]["sensitive"])
    import agent_runtime_lib as lib
    ok &= check("titan's Gemini mask catches the stored code", "4417" not in lib._SecretVault().shield(door2), door2)
    alex = notes["hh-people-alex-partner"]["text"]
    ok &= check("dates and lists rendered", "Birthday: 1990-02-27" in alex and "Allergies: penicillin" in alex, alex)
    later = k.render("people", DOC["people"][0], False, "2031-01-01")[1]
    ok &= check("re-import on another day is unchanged", k._norm(alex) == k._norm(later) and alex != later)
    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
