#!/usr/bin/env python3
# gate: hermetic
"""Projects are threads that earned a name (#1085): titan proposes from the viewer's own threads (#951's linker), never
creates silently; keep, dismiss (it stays dismissed as the cluster grows), rename, file, unfile, split and merge, each
with one undo token that puts back exactly what it changed; groups are projects too; a project page shows each item only
to who may see it. #951's trade-in becomes a proposal with nothing trade-in-specific. Fictional people (Alex and Jordan,
adults; Ava, a kid); a temporary links.db, salt, spaces DB and household; no model.
Run: python3 scripts/test_projects.py"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

_TMP = Path(tempfile.mkdtemp(prefix="titan-projects-"))
for k, v in {"LINK_SALT_FILE": "link_salt", "LINKS_DB": "links.db", "WORLD_DIR": "world", "WORLD_DB": "world.db",
             "SPACES_DB": "spaces.db", "HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json",
             "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import hooks as H  # noqa: E402
import projects as P  # noqa: E402
import spaces  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


ALEX, JORDAN, AVA = "alex", "jordan", "ava"
PEOPLE = [{"id": ALEX, "role": "adult", "name": "Alex Hale"}, {"id": JORDAN, "role": "adult", "name": "Jordan Hale"},
          {"id": AVA, "role": "kid", "name": "Ava Hale"}]

print("── #951's trade-in becomes a proposal, from the real linker ──")
con = H.connect()


def add(iid, src, item, owner=ALEX, aud="private"):
    H.index(con, iid, src, H.extract(item, lambda t: ["org:harbor-mobile"] if "harbor mobile" in t.lower() else []),
            owner, aud)


def mail(frm, day, body, title=""):
    return {"title": title, "text": f"From: {frm}\nDate: {day}\nSubject: {title}\n\n{body}"}


rnd = random.Random(7)
DOMAINS = ["lakesideclinic.org", "harborgas.com", "brightlinehost.com", "maplelibrary.org", "riverschool.edu",
           "cedarpharmacy.com", "northwindair.com", "pinegrocer.com"]
for i in range(150):     # the house's own corpus, so rarity means something
    d = rnd.choice(DOMAINS)
    day = f"2026-{rnd.randint(7, 10):02d}-{rnd.randint(1, 28):02d}"
    amt = f"${rnd.choice([12, 18, 24, 35, 49, 61, 75, 99])}.{rnd.choice(['00', '99', '50'])}"
    add(f"bg-{i}", "mail", mail(f"News <hello@{d}>", day, f"Your receipt total {amt}. Thanks for visiting.", "Receipt"))
add("text-1", "text", {"text": "HarborMobile: we got the device for order hm-2048-7731. Credit in 1-2 cycles.",
                       "at": "2026-09-27", "sender": "45021"})
add("mail-tradein", "mail", mail("Harbor Mobile <orders@mail.harbormobile.com>", "Sep 28, 2026",
                                 "Your trade-in for Order #HM-2048-7731 was received. A credit of $250.00 posts by Oct 15.",
                                 "Your trade-in was received"))
add("mail-credit", "mail", mail("Harbor Mobile <billing@harbormobile.com>", "Oct 14, 2026",
                                "A $250.00 trade-in credit was applied to your account.", "Your trade-in credit was applied"))
add("mail-otherorder", "mail", mail("Harbor Mobile <orders@harbormobile.com>", "Oct 2, 2026",
                                    "Order HM-3001-1150 shipped. Total $59.99.", "Your order shipped"))
con.commit()
props = P.proposals(ALEX, "adult")
trade = [p for p in props if {"text-1", "mail-tradein", "mail-credit"} <= set(p["items"])]
check("Alex gets the trade-in as one proposal (a text and two mails), named or described by the thread itself",
      (len(trade), sorted(trade[0]["items"]) if trade else None, bool(trade and trade[0]["name"])),
      (1, ["mail-credit", "mail-tradein", "text-1"], True))
check("…the other Harbor Mobile order isn't in it", any("mail-otherorder" in p["items"] for p in trade), False)
check("Jordan, who can't see Alex's private items, gets no proposal of them",
      [p for p in P.proposals(JORDAN, "adult") if "text-1" in p["items"]], [])
check("kids and shared screens get none", (P.proposals(AVA, "kid"), P.proposals(None, "household")), ([], []))
kept = P.keep(ALEX, "adult", trade[0]["id"], people=PEOPLE)
check("keep: a project made by a proposal, its name's source said, its items filed, an undo token",
      (kept["origin"], kept["name_source"] in ("earned", "description"), kept["filed"], kept["audience"],
       kept["undo"].startswith("pu-")), ("proposal", True, 3, "private", True))
check("…and it's no longer proposed", [p for p in P.proposals(ALEX, "adult") if "text-1" in p["items"]], [])

print("── proposals over fake threads: dismissal sticks, the default audience ──")
ITEMS = {i: NS(owner=o) for i, o in (("a1", ALEX), ("a2", ALEX), ("a3", JORDAN), ("a4", ALEX), ("b1", ALEX), ("b2", ALEX),
                                     ("b3", ALEX))}
TH = [NS(items=["a1", "a2", "a3"], name="Science fair", description="3 items", cohesion=0.9),
      NS(items=["b1", "b2"], name=None, description="2 items", cohesion=0.9)]
fake = lambda: (TH, ITEMS, lambda it: True)   # noqa: E731
ps = P.proposals(ALEX, "adult", threads=fake)
check("only threads of at least 3 items are proposed", [p["name"] for p in ps], ["Science fair"])
P.dismiss(ALEX, "adult", ps[0]["id"], threads=fake)
TH[0] = NS(items=["a1", "a2", "a3", "a4"], name="Science fair", description="4 items", cohesion=0.9)
check("dismissed: it stays dismissed when the cluster grows", P.proposals(ALEX, "adult", threads=fake), [])
check("…for Alex only; Jordan's own view can still propose it", len(P.proposals(JORDAN, "adult", threads=fake)), 1)
pj = P.proposals(JORDAN, "adult", threads=fake)[0]
kj = P.keep(JORDAN, "adult", pj["id"], threads=fake, people=PEOPLE)
check("kept: private by default; the other adults its items involve come back as a one-tap suggestion",
      (kj["audience"], kj["suggested_audience"]), ("private", [ALEX]))
check("…each suggested person with the name to show, so the app never offers to share with someone it can't name",
      kj["suggested_people"], [{"id": ALEX, "name": next(str(x.get("name") or "").split(" ")[0] for x in PEOPLE if x["id"] == ALEX)}])
check("…and never a kid, nor the keeper", any(x["id"] in (AVA, JORDAN) for x in kj["suggested_people"]), False)

print("── shared: a titan-made name only from what everyone it's shared with can see ──")
import linker  # noqa: E402
LI = {"m-private": linker.Item("m-private", "mail", None, ALEX, "private"),
      "ev-house": linker.Item("ev-house", "calendar", None, ALEX, "household"),
      "ev-house2": linker.Item("ev-house2", "calendar", None, ALEX, "household")}
TITLES = {"m-private": "Biopsy results from Lakeside", "ev-house": "Soccer at Harbor Park",
          "ev-house2": "Soccer snacks at Harbor Park"}
SEEN = []


def namer(ids):   # a stand-in for the linker's naming: the words the given items' titles share
    SEEN.append(sorted(ids))
    words = [set(TITLES[i].split()) for i in ids]
    return " ".join(sorted(set.intersection(*words))) if words else None


rk = {"namer": namer, "loader": lambda: (LI, [])}
TH2 = [NS(items=["m-private", "ev-house", "ev-house2"], name="Biopsy results Soccer", description="3 items", cohesion=0.9)]
ITEMS2 = {"m-private": NS(owner=ALEX), "ev-house": NS(owner=ALEX), "ev-house2": NS(owner=ALEX)}
fk2 = lambda: (TH2, ITEMS2, lambda it: True)   # noqa: E731
pp = P.proposals(ALEX, "adult", threads=fk2)[0]
shared_kept = P.keep(ALEX, "adult", pp["id"], audience=[ALEX, JORDAN], threads=fk2, people=PEOPLE, rename_kw=rk)
check("kept and shared with Jordan: renamed from the household events only, nothing only the private mail had",
      (SEEN[-1], shared_kept["name"], any(w in shared_kept["name"] for w in ("Biopsy", "results", "Lakeside"))),
      (["ev-house", "ev-house2"], "Harbor Park Soccer", False))
for a, b in (("m-private", "m-p2"), ("ev-house", "ev-h3"), ("ev-house2", "ev-h4")):
    LI[b] = linker.Item(b, LI[a].source, None, ALEX, LI[a].audience)
    TITLES[b] = TITLES[a]
TH3 = [NS(items=["m-p2", "ev-h3", "ev-h4"], name="Biopsy results Soccer", description="3 items", cohesion=0.9)]
fk3 = lambda: (TH3, {i: NS(owner=ALEX) for i in TH3[0].items}, lambda it: True)   # noqa: E731
private_kept = P.get(P.keep(ALEX, "adult", P.proposals(ALEX, "adult", threads=fk3)[0]["id"], threads=fk3,
                            people=PEOPLE, rename_kw=rk)["id"])
check("kept private: its own name stands (only the keeper sees it)", private_kept["name"], "Biopsy results Soccer")
spaces.update_project(private_kept["id"], ALEX, "adult", audience="household")
got = P.reshare(private_kept["id"], PEOPLE, **rk)
check("shared later (PATCH audience): renamed the same way", (got["name"], got["name_source"]),
      ("Harbor Park Soccer", "earned"))
P.named_by_person(private_kept["id"])
spaces.update_project(private_kept["id"], ALEX, "adult", name="Soccer + checkup")
check("a name a person typed stays as typed when shared", P.reshare(private_kept["id"], PEOPLE, **rk)["name"],
      "Soccer + checkup")
only_private = P.name_for_audience(["m-private", "ev-house"], [(ALEX, "adult"), (JORDAN, "adult")], namer=namer,
                                   loader=lambda: (LI, []))
check("fewer than two items everyone can see: the neutral name, never one built from private items",
      only_private, (P.NEUTRAL_NAME, "description"))

print("── rename, file, unfile, split, merge, undo ──")
pr = spaces.create_project(ALEX, "adult", "Kitchen redo")
check("a person's project: origin person", P.get(pr["id"])["origin"], "person")
P.named_by_person(kept["id"])
check("renamed by a person: named by them from now on", P.get(kept["id"])["name_source"], "person")
f1 = P.file(ALEX, "adult", "todo:7", pr["id"])
f2 = P.file(ALEX, "adult", "chat:c-1", pr["id"])
check("file a to-do and a chat (the chat in spaces' tags, as the Mac files it)",
      (P.filings().get("todo:7"), spaces.tags_for(ALEX).get("c-1", {}).get("project")), (pr["id"], pr["id"]))
P.undo(ALEX, f2["undo"])
check("undo a chat's filing: it's back where it was (nowhere)", spaces.tags_for(ALEX).get("c-1"), None)
P.file(ALEX, "adult", "chat:c-1", pr["id"])
P.file(ALEX, "adult", "ref:r-1", pr["id"])
sp = P.split(ALEX, "adult", pr["id"], ["todo:7", "ref:r-1"], "Kitchen paint")
check("split: those items move to a new project with the same audience",
      (sp["moved"], P.filings()["todo:7"], P.filings()["ref:r-1"], sp["project"]["audience"]),
      (2, sp["project"]["id"], sp["project"]["id"], "private"))
P.undo(ALEX, sp["undo"])
check("undo the split: the items are back, the new project archived",
      (P.filings()["todo:7"], P.filings()["ref:r-1"], spaces.get_project(sp["project"]["id"])), (pr["id"], pr["id"], None))
other = spaces.create_project(ALEX, "adult", "House")
mg = P.merge(ALEX, "adult", pr["id"], other["id"])
check("merge: everything moves (the chat too), the old project is archived",
      (mg["moved"], P.filings()["todo:7"], spaces.tags_for(ALEX)["c-1"]["project"], spaces.get_project(pr["id"])),
      (3, other["id"], other["id"], None))
P.undo(ALEX, mg["undo"])
check("undo the merge: all back, the project restored",
      (P.filings()["todo:7"], spaces.tags_for(ALEX)["c-1"]["project"], bool(spaces.get_project(pr["id"]))),
      (pr["id"], pr["id"], True))
u = P.unfile(ALEX, "adult", "todo:7")
check("unfile, then undo", (P.filings().get("todo:7"), P.undo(ALEX, u["undo"])["items"], P.filings().get("todo:7")),
      (None, 1, pr["id"]))
try:
    P.undo(JORDAN, f1["undo"])
    check("someone else's undo token: 404", "undone", 404)
except P.Refused as e:
    check("someone else's undo token: 404", e.status, 404)
for label, fn, status in (("Jordan can't file into Alex's private project (404, not 403)",
                           lambda: P.file(JORDAN, "adult", "todo:9", pr["id"]), 404),
                          ("Jordan can't split Alex's project", lambda: P.split(JORDAN, "adult", pr["id"], ["todo:7"], "x"), 404),
                          ("a shared screen files nothing", lambda: P.file(None, "household", "todo:9", pr["id"]), 403)):
    try:
        fn()
        check(label, "done", status)
    except P.Refused as e:
        check(label, e.status, status)

print("── groups are projects too ──")
g = spaces.create_group(ALEX, "adult", "Parents", [ALEX, JORDAN])
P.file(ALEX, "adult", "chat:c-2", g["id"])
P.file(JORDAN, "adult", "todo:11", g["id"])
check("a chat filed under a group gets its group tag; another kind its filing",
      (spaces.tags_for(ALEX)["c-2"], P.filings()["todo:11"]), ({"project": None, "group": g["id"]}, g["id"]))
m = P.membership(ALEX)
check("membership: Alex's view files both under the group (each still shows only to who may see it)",
      (m.get("chat:c-2"), m.get("todo:11")), ([g["id"]], [g["id"]]))
check("…Jordan's view doesn't carry Alex's chat tag", P.membership(JORDAN).get("chat:c-2"), None)

print("── related threads on a project page (#1109) ──")
E = linker.Edge
RI = {"ev1": linker.Item("ev1", "calendar", None, ALEX, "household"),
      "ev2": linker.Item("ev2", "calendar", None, ALEX, "household"),
      "m1": linker.Item("m1", "mail", None, ALEX, "private"), "m2": linker.Item("m2", "mail", None, ALEX, "private"),
      "h1": linker.Item("h1", "record", None, ALEX, "household"), "h2": linker.Item("h2", "record", None, ALEX, "household"),
      "j1": linker.Item("j1", "mail", None, JORDAN, "private"), "j2": linker.Item("j2", "mail", None, JORDAN, "private")}
RE = [E("m1", "m2", "accepted", ["same order number"], {}, "both name order HM-2048"),
      E("ev1", "m1", "candidate", ["same sender"], {}, "the same sender as Harbor Clinic results"),   # Alex's private mail
      E("h1", "h2", "accepted", ["same order number"], {}, "both name order SC-11"),
      E("ev2", "h1", "accepted", ["same order number"], {}, "the same order number SC-11"),
      E("j1", "j2", "accepted", ["same case number"], {}, "both name case J-7"),
      E("ev2", "j1", "candidate", ["same sender"], {}, "the same sender as Jordan's lawyer")]
view = lambda who, role="adult": (lambda: (RI, RE, __import__("threadview")._visible_fn(who, role), ({}, {}, {})))  # noqa: E731
rp = spaces.create_project(ALEX, "adult", "Soccer season", audience="household")
P.file(ALEX, "adult", "ev1", rp["id"])
P.file(ALEX, "adult", "ev2", rp["id"])
mine = P.related(rp["id"], ALEX, "adult", view=view(ALEX))
check("Alex is offered both threads he can see all of, the stronger link first, each with its why",
      [(r["count"], r["state"], r["why"]) for r in mine],
      [(2, "accepted", "the same order number SC-11"), (2, "candidate", "the same sender as Harbor Clinic results")])
theirs = P.related(rp["id"], JORDAN, "adult", view=view(JORDAN))
check("Jordan (in the household) is offered the household thread and his own case, never the thread through Alex's "
      "private mail: no why, no name, no count of it", ([r["why"] for r in theirs], "Harbor" in json.dumps(theirs)),
      (["the same order number SC-11", "the same sender as Jordan's lawyer"], False))
check("…and Jordan's own private case never shows up for Alex", any("J-7" in r["why"] or "lawyer" in r["why"] for r in mine),
      False)
check("kids and shared screens are offered none", (P.related(rp["id"], AVA, "kid", view=view(AVA, "kid")),
                                                   P.related(rp["id"], None, "household")), ([], []))
sc = next(r for r in mine if r["state"] == "accepted")
ln = P.relate(rp["id"], sc["thread"], ALEX, "adult", True, view=view(ALEX))
check("Link files the thread's items, with one undo token", (P.filings().get("h1"), P.filings().get("h2"),
                                                             ln["undo"].startswith("pu-")), (rp["id"], rp["id"], True))
check("…a linked thread is no longer offered", [r["state"] for r in P.related(rp["id"], ALEX, "adult", view=view(ALEX))],
      ["candidate"])
P.undo(ALEX, ln["undo"])
check("undo the link: unfiled, offered again", (P.filings().get("h1"), len(P.related(rp["id"], ALEX, "adult", view=view(ALEX)))),
      (None, 2))
P.relate(rp["id"], sc["thread"], JORDAN, "adult", False, view=view(JORDAN))
check("Not related sticks for this project (for everyone on it)",
      ([r["state"] for r in P.related(rp["id"], ALEX, "adult", view=view(ALEX))],
       [r["why"] for r in P.related(rp["id"], JORDAN, "adult", view=view(JORDAN))]),
      (["candidate"], ["the same sender as Jordan's lawyer"]))
try:
    P.relate(rp["id"], next(r for r in mine if r["state"] == "candidate")["thread"], JORDAN, "adult", True, view=view(JORDAN))
    check("Jordan can't link a thread he isn't offered", "linked", 404)
except P.Refused as e:
    check("Jordan can't link a thread he isn't offered", e.status, 404)
for i in range(4):
    RI[f"x{i}a"] = linker.Item(f"x{i}a", "record", None, ALEX, "household")
    RI[f"x{i}b"] = linker.Item(f"x{i}b", "record", None, ALEX, "household")
    RE += [E(f"x{i}a", f"x{i}b", "accepted", ["same order number"], {}, f"both name order X-{i}"),
           E("ev1", f"x{i}a", "accepted", ["same order number"], {}, f"the same order X-{i}")]
check("at most 3", len(P.related(rp["id"], ALEX, "adult", view=view(ALEX))), 3)

import household_identity as hh  # noqa: E402
check("routes: adults and kids may file; a shared screen may not",
      [hh.route_allowed(r, "POST", "/v1/items/todo:7/file") for r in ("admin", "adult", "kid", "household")],
      [True, True, True, False])
print("── #1177: titan's operations on proposals and kept projects ──")
check("a proposal: Keep and Not a project, with their routes, for an adult",
      [(o["verb"], o["route"]["method"], o["route"]["path"]) for o in P.proposal_ops({"id": "pp-1"}, ALEX, "adult")],
      [("add", "POST", "/v1/projects/proposals/pp-1/keep"), ("remove", "POST", "/v1/projects/proposals/pp-1/dismiss")])
check("…none for a kid or a shared screen", (P.proposal_ops({"id": "pp-1"}, AVA, "kid"), P.proposal_ops({"id": "pp-1"}, None,
      "household")), ([], []))
check("keep's response: who Share would offer, by name, and the keeper's operations",
      (kj["suggested_people"], sorted((o["verb"], o.get("params")) for o in kj["operations"])),
      ([{"id": ALEX, "name": "Alex"}], [("edit", None), ("share", {"audience": [ALEX]}), ("undo", {"token": kj["undo"]})]))
check("…a keep with nobody to suggest offers no Share", sorted(o["verb"] for o in kept["operations"]), ["edit", "undo"])
proj = P.get(kj["id"])
check("someone else's project: an adult who isn't its owner gets no Rename or Share",
      P.manage_ops(proj, ALEX, "adult", [JORDAN]), [])
check("…the admin may", sorted(o["verb"] for o in P.manage_ops(proj, "zz-admin", "admin", [ALEX])), ["edit", "share"])
check("…a kid and a screen never", (P.manage_ops(proj, AVA, "kid", [ALEX]), P.manage_ops(proj, None, "household")), ([], []))
check("Share's audience is exactly the suggested people, nobody else",
      next(o for o in P.manage_ops(proj, JORDAN, "adult", [ALEX]) if o["verb"] == "share")["params"], {"audience": [ALEX]})
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
