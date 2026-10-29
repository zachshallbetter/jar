#!/usr/bin/env python3
# gate: hermetic
"""An engineering agent reads titan's system notes and host summary, and nothing else (#355): the engineering token resolves to a
read-only identity that reaches a few GET routes, sees unowned system notes, and never a household, personal or
shared item, in the KB or the world model."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="eng-")
os.environ["HOUSEHOLD_DB"] = os.path.join(_TMP, "household.db")    # never a real store
os.environ["KB_ACL_DB"] = os.path.join(_TMP, "kb_acl.db")
import audience as A  # noqa: E402
import household_identity as H  # noqa: E402
import kb_privacy as P  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


E = H.ENGINEERING
check("kb_privacy and household_identity agree on the role", P.ENGINEERING_ROLE, E)
check("not a person's role", E in H.ROLES, False)

# the credential
tok = "e" * 40
f = Path(_TMP) / "eng_token"
f.write_text(tok + "\n")
check("no token file: nobody is the engineering agent", H.resolve(tok, "m" * 40), None)
os.environ["AGENT_ENGINEERING_TOKEN_FILE"] = str(f)
ident = H.resolve(tok, "m" * 40)
check("the engineering token → the engineering identity", (ident.role, ident.person_id, ident.via), (E, E, E))
check("the master token is still the admin", H.resolve("m" * 40, "m" * 40).role, "admin")
f.write_text("short")
check("a short token is refused", H.resolve("short", "m" * 40), None)
f.write_text(tok)

# the routes: a few reads, nothing else
check("GET /v1/kb", H.route_allowed(E, "GET", "/v1/kb"), True)
check("GET /v1/kb/{id}", H.route_allowed(E, "GET", "/v1/kb/note-x"), True)
check("GET /v1/world/entities", H.route_allowed(E, "GET", "/v1/world/entities"), True)
check("GET /v1/host/summary (the titan MCP)", H.route_allowed(E, "GET", "/v1/host/summary"), True)
check("…still not an adult's or a kid's", [H.route_allowed(r, "GET", "/v1/host/summary") for r in ("adult", "kid", "household")],
      [False, False, False])
for m, p in (("POST", "/v1/prompt/stream"), ("POST", "/v1/kb"), ("DELETE", "/v1/kb/x"), ("GET", "/v1/memory/facts"),
             ("POST", "/v1/memory/recall"), ("GET", "/v1/schedule"), ("GET", "/v1/timeline"), ("GET", "/v1/me"),
             ("GET", "/v1/proactive"), ("GET", "/v1/claude-chats"), ("GET", "/v1/people")):
    check(f"{m} {p} refused", H.route_allowed(E, m, p), False)

# the KB: unowned system notes only
runbook = {"origin": "kb_seed_runbook.py", "domain": "titan", "owner": None, "audience": "admins"}
decision = {"origin": "kb_seed_decisions.py", "audience": "admins"}
record = {"source_type": "knowledge_record", "audience": "admins"}
check("sees the runbook", P.kb_visible(runbook, E, E, source_id="kb-runbook"), True)
check("sees a decision", P.kb_visible(decision, E, E, source_id="kb-decision"), True)
check("sees a knowledge record", P.kb_visible(record, E, E, source_id="kr-1"), True)
for label, md in (("a household note", {"owner": None, "audience": "household"}),
                  ("Zach's private note", {"owner": "zach", "audience": "private"}),
                  ("a note shared with the adults", {"owner": "robin", "audience": "adults"}),
                  ("an admins-only note that isn't titan's", {"owner": None, "audience": "admins"}),
                  ("Zach's own note about titan", {"owner": "zach", "audience": "private", "domain": "titan"}),
                  ("a legacy note (no owner: Zach's)", {}),
                  ("a mail item", {"owner": "zach", "audience": "private", "source_type": "connector:mail"})):
    check(f"never {label}", P.kb_visible(md, E, E, source_id="x"), False)
check("visible_acl alone never admits it", P.visible_acl(None, "household", E, E), False)
check("filter_hits keeps only system notes", [h["source_id"] for h in P.filter_hits(
    [{"source_id": "kb-runbook", "metadata": runbook}, {"source_id": "hh-1", "metadata": {"audience": "household"}},
     {"source_id": "n-1", "metadata": {"owner": "zach", "audience": "private"}}], E, E)], ["kb-runbook"])
check("the admin still sees the runbook", P.kb_visible(runbook, "zach", "admin", source_id="kb-runbook"), True)
check("an adult still doesn't (admin-only by default)", P.kb_visible(runbook, "robin", "adult", source_id="kb-runbook"),
      False)

# audiences (the world model, cards, todos all read through audience.visible)
for aud in ("household", "adults", "admins", "private", '["engineering"]'):
    check(f"audience {aud}: not visible", A.visible(aud, E, E, owner=None), False)

# the world model: nothing from any bubble
os.environ.setdefault("AGENT_TZ", "America/Los_Angeles")
import world as W  # noqa: E402
B = W.Bubbles(Path(_TMP) / "world")
B.sync("household", [W.Obs(entity="maple-animal-clinic", etype="org", name="Maple Animal Clinic", attr="phone",
                           value="(555) 0142", observed_at="2026-09-01", kind="household", ref="hh#phone",
                           origin="household.yaml", label="the household file", owner=None, audience="household",
                           aliases=(("vet", ""),), bubble="household", facet="home")])
check("a kid finds the vet (control)", B.find("the vet's number", "ava", "kid"), ["household:maple-animal-clinic"])
check("the engineering agent doesn't", B.find("the vet's number", E, E), [])
check("…nor its facts", B.facts("household:maple-animal-clinic", E, E), [])
check("…nor any page", W.entity_list(B, E, E)["entities"], [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
