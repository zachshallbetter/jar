#!/usr/bin/env python3
# gate: hermetic
"""S3's comparator (docs/surfaces.md §8; scripts/surfaces_s3.py): titan's expected lists come from the saved golden
vectors only; an app's file that renders the same membership, groups and operations passes; every kind of difference
is named by ids and roles only (never a title or text, so it can go in a PR body); two apps must cover the same vectors.
Synthetic app files; no route, no model.
Run: python3 scripts/test_surfaces_s3.py"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import surfaces_s3 as S3  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


names = S3.vector_names()
check("every saved surface vector is covered, the frozen keys file isn't one",
      (len(names) >= 13, "shipped_keys" in names, {"cards.admin", "threads.alex", "brief.alex"} <= set(names)), (True, False, True))
exp = S3.expected()
check("expected lists come from the saved vectors, verbatim (no live route)",
      exp["results"][names.index("cards.screen")]["groups"],
      [{"id": g["id"], "role": g["role"], "members": [m["subject"]["id"] for m in g["members"]]}
       for g in json.loads((S3.VECTORS / "cards.screen.json").read_text())["groups"]])
TITLES = {m["slots"].get("title") for n in names for g in S3.load(n)["groups"]
          for m in g["members"] if m.get("slots", {}).get("title")}


def strings(x):
    if isinstance(x, dict):
        return {s for v in x.values() for s in strings(v)}
    if isinstance(x, list):
        return {s for v in x for s in strings(v)}
    return {x} if isinstance(x, str) else set()


IDS = {m["subject"]["id"] for n in names for g in S3.load(n)["groups"] for m in g["members"]}
check("the expected lists carry ids, roles and exposures only: no title is ever a value (unless it is itself an id)",
      sorted(t for t in TITLES & strings(exp) if t not in IDS), [])


def app(name, results):
    return {"s3": 1, "app": name, "build": "0.13.61", "results": results}


perfect = copy.deepcopy(exp["results"])
ok, lines = S3.compare([app("mac", perfect)])
check("an app that renders exactly titan's surface: equal", (ok, lines[-1]), (True, f"mac: {len(names)} vector(s) compared, 0 difference(s); not emitted: 0"))
ok, lines = S3.compare([app("mac", perfect), app("ios", copy.deepcopy(perfect))])
check("two apps, both exact: equal", ok, True)


def broken(fn, vec="cards.admin"):
    r = copy.deepcopy(perfect)
    fn(next(x for x in r if x["vector"] == vec))
    ok_, lines_ = S3.compare([app("ios", r)])
    return ok_, [x for x in lines_ if not x.startswith("ios: ")]


def drop_member(v):
    v["groups"][1]["members"].pop()


def add_op(v):
    v["operations"].append({"id": "op-invented", "exposure": "available"})


def exposure(v):
    v["operations"][0]["exposure"] = "available" if v["operations"][0]["exposure"] != "available" else "withheld"


def role(v):
    v["groups"][0]["role"] = "primary"


def reorder(v):
    v["groups"][1]["members"].reverse()


def drop_group(v):
    v["groups"].pop()


def swap_groups(v):
    v["groups"][0], v["groups"][1] = v["groups"][1], v["groups"][0]


admin = next(x for x in perfect if x["vector"] == "cards.admin")
g1, last_member = admin["groups"][1]["id"], admin["groups"][1]["members"][-1]
op0 = admin["operations"][0]
check("a member left out: named by group and id", broken(drop_member),
      (False, [f"ios cards.admin: group {g1}: missing members {last_member}"]))
check("an operation titan doesn't have: named", broken(add_op), (False, ["ios cards.admin: operation op-invented: not in titan's surface"]))
check("a wrong exposure: named, with titan's", broken(exposure)[1][0].startswith(f"ios cards.admin: operation {op0['id']}: exposure "), True)
check("a wrong role: named, with titan's", broken(role),
      (False, [f"ios cards.admin: group {admin['groups'][0]['id']}: role primary (titan: {admin['groups'][0]['role']})"]))
check("members in a different order", broken(reorder), (False, [f"ios cards.admin: group {g1}: members in a different order"]))
check("a group left out", broken(drop_group), (False, [f"ios cards.admin: group {admin['groups'][-1]['id']}: missing"]))
check("groups in a different order", broken(swap_groups), (False, ["ios cards.admin: groups: in a different order"]))
ok, lines = S3.compare([app("tv", [{"vector": "cards.nope", "groups": [], "operations": []}])])
check("a vector titan doesn't have: named", (ok, lines[0]), (False, "tv cards.nope: no such vector in titan's evalsets"))
ok, lines = S3.compare([{"app": "mac", "results": []}])
check("not an S3 file: refused plainly", (ok, lines[0]), (False, "mac: not an S3 file (s3: 1 and a results list expected)"))
ok, lines = S3.compare([app("mac", perfect), app("ios", [r for r in perfect if r["vector"] != "threads.alex"])])
check("two apps rendering different surfaces: each matches titan, so equal where they overlap; the gap is reported only",
      (ok, [x for x in lines if "doesn't emit" in x or x.startswith("shared:")]),
      (True, [f"shared: {len(names) - 1} vector(s) compared by every app",
              "ios: doesn't emit threads.alex (another app does; not a failure)"]))
r = copy.deepcopy(perfect)
r[0]["groups"][0]["title"] = "anything an app sends"
check("whatever else an app sends is ignored, and never echoed", any("anything an app sends" in x for x in S3.compare([app("mac", r)])[1]), False)
every = [x for f in (drop_member, add_op, exposure, role, reorder, drop_group, swap_groups) for x in broken(f)[1]]
check("no diff line ever carries a title", [x for x in every for t in TITLES - IDS if len(t) > 6 and t in x], [])

print("── a read-only view: draws_operations false ──")
brief = next(x for x in perfect if x["vector"] == "brief.alex")
n_ops = len(brief["operations"])
reader = dict(copy.deepcopy(brief), operations=[], draws_operations=False)
ok, lines = S3.compare([app("mac", [reader])])
check("declared read-only, no operations: equal, and titan's operations are counted as not drawn",
      (ok, [x for x in lines if "not drawn" in x]),
      (True, [f"mac brief.alex: {n_ops} operation(s) not drawn (declared read-only)"]))
check("…the count is real (the brief carries operations)", n_ops > 0, True)
bad = dict(copy.deepcopy(reader), operations=[{"id": brief["operations"][0]["id"], "exposure": "available"}])
ok, lines = S3.compare([app("mac", [bad])])
check("declared read-only but emits an operation: fails, named", (ok, [x for x in lines if "declared" in x and "emitted" in x]),
      (False, ["mac brief.alex: declared draws_operations false, but emitted 1 operation(s)"]))
drift = dict(copy.deepcopy(reader))
drift["groups"][0]["members"] = drift["groups"][0]["members"][::-1] if len(drift["groups"][0]["members"]) > 1 else ["x"]
ok, lines = S3.compare([app("mac", [drift])])
check("declared read-only still compares groups and members exactly", ok, False)
undeclared = dict(copy.deepcopy(brief), operations=[])
ok, lines = S3.compare([app("mac", [undeclared])])
check("no declaration: today's strict check (every operation missing is a failure)",
      (ok, sum(1 for x in lines if x.endswith(": missing") and " operation " in x)), (False, n_ops))
flagged_true = dict(copy.deepcopy(brief), draws_operations=True)
check("draws_operations true is the same as no declaration (strict)", S3.compare([app("mac", [flagged_true])])[0], True)

print("── a group the app doesn't draw: omits_groups ──")
deck = next(x for x in perfect if x["vector"] == "cards.admin")
done = next(g for g in deck["groups"] if g["id"] == "g-done-today")
om = copy.deepcopy(deck)
om["groups"] = [g for g in om["groups"] if g["id"] != "g-done-today"]
om["omits_groups"] = ["g-done-today"]
ok, lines = S3.compare([app("ios", [om])])
check("declared omitted and not drawn: equal, the group counted as not drawn",
      (ok, [x for x in lines if "not drawn (declared)" in x]),
      (True, [f"ios cards.admin: group g-done-today not drawn (declared), {len(done['members'])} member(s)"]))
emitted = copy.deepcopy(deck)
emitted["omits_groups"] = ["g-done-today"]
ok, lines = S3.compare([app("ios", [emitted])])
check("declared omitted but drawn anyway: fails, named", (ok, [x for x in lines if "declared omitted" in x]),
      (False, ["ios cards.admin: group g-done-today: declared omitted, but emitted"]))
silent = copy.deepcopy(deck)
silent["groups"] = [g for g in silent["groups"] if g["id"] != "g-done-today"]
ok, lines = S3.compare([app("ios", [silent])])
check("an undeclared missing group still fails, as before", (ok, [x for x in lines if "g-done-today" in x]),
      (False, ["ios cards.admin: group g-done-today: missing"]))
both = copy.deepcopy(om)
both["groups"][0]["members"] = both["groups"][0]["members"][1:] + ["x-extra"]
check("omitting one group never loosens the others", S3.compare([app("ios", [both])])[0], False)

print("── an omitted group's members' operations aren't drawn either ──")
th = next(x for x in perfect if x["vector"] == "threads.alex")
raw_th = json.loads((S3.VECTORS / "threads.alex.json").read_text())
other = next(g for g in th["groups"] if g["id"] == "g-other")
subj = {o["id"]: o["subject"]["id"] for o in raw_th["operations"]}
other_ops = {oid for oid, sid in subj.items() if sid in set(other["members"])}
app_th = copy.deepcopy(th)
app_th["groups"] = [g for g in app_th["groups"] if g["id"] != "g-other"]
app_th["operations"] = [o for o in app_th["operations"] if o["id"] not in other_ops]
app_th["omits_groups"] = ["g-other"]
ok, lines = S3.compare([app("ios", [app_th])])
check("g-other omitted, its members' operations not drawn: equal, the operations counted",
      (ok, [x for x in lines if "inside omitted groups" in x]),
      (True, [f"ios threads.alex: {len(other_ops)} operation(s) not drawn (declared, inside omitted groups)"]))
check("…the count is real", len(other_ops) > 0, True)
leak = copy.deepcopy(app_th)
first = sorted(other_ops)[0]
leak["operations"].append({"id": first, "exposure": "available"})
ok, lines = S3.compare([app("ios", [leak])])
check("drawing an operation of a member that lives only in an omitted group fails, named",
      (ok, [x for x in lines if "in a declared omitted group, but emitted" in x]),
      (False, [f"ios threads.alex: operation {first}: in a declared omitted group, but emitted"]))
sch = next(x for x in perfect if x["vector"] == "schedule.alex")
raw_sch = json.loads((S3.VECTORS / "schedule.alex.json").read_text())
ov = next(g for g in sch["groups"] if g["id"] == "g-overlaps")
shared_members = set(ov["members"])
check("schedule.alex's overlaps members also sit in a drawn day group", all(
    any(m in g["members"] for g in sch["groups"] if g["id"] != "g-overlaps") for m in shared_members), True)
shared_ops = {o["id"] for o in raw_sch["operations"] if o["subject"]["id"] in shared_members}
app_s = copy.deepcopy(sch)
app_s["groups"] = [g for g in app_s["groups"] if g["id"] != "g-overlaps"]
app_s["omits_groups"] = ["g-overlaps"]
check("omitting a group whose members are also drawn elsewhere: their operations stay required (all present: equal)",
      S3.compare([app("mac", [app_s])])[0], True)
app_s2 = copy.deepcopy(app_s)
app_s2["operations"] = [o for o in app_s2["operations"] if o["id"] not in shared_ops]
ok, lines = S3.compare([app("mac", [app_s2])])
check("…and dropping them still fails as missing (strict)", (ok, sum(1 for x in lines if x.endswith(": missing"))),
      (False, len(shared_ops)))
check("no omits_groups: unchanged strict behaviour", S3.compare([app("ios", [dict(copy.deepcopy(th), operations=[])])])[0], False)

print("── a verb the app doesn't offer: omits_operations ──")
docs = next(x for x in perfect if x["vector"] == "documents.household")
raw_docs = json.loads((S3.VECTORS / "documents.household.json").read_text())
asks = {o["id"] for o in raw_docs["operations"] if o["verb"] == "ask"}
others = {o["id"] for o in raw_docs["operations"] if o["verb"] != "ask"}
check("documents.household has asks and other verbs to tell apart", (len(asks) > 0, len(others) > 0), (True, True))
files = copy.deepcopy(docs)
files["operations"] = [o for o in files["operations"] if o["id"] not in asks]
files["omits_operations"] = ["ask"]
ok, lines = S3.compare([app("ios", [files])])
check("the Files extension declares ask and draws no asks: equal, the asks counted as not drawn",
      (ok, [x for x in lines if "(declared: " in x]),
      (True, [f"ios documents.household: {len(asks)} operation(s) not drawn (declared: ask)"]))
asked = copy.deepcopy(files)
one = sorted(asks)[0]
asked["operations"].append({"id": one, "exposure": "available"})
ok, lines = S3.compare([app("ios", [asked])])
check("declared ask but emits one anyway: fails, named",
      (ok, [x for x in lines if "declared omitted, but emitted" in x]),
      (False, [f"ios documents.household: operation {one}: verb ask declared omitted, but emitted"]))
no_open = copy.deepcopy(files)
dropped = sorted(others)[0]
no_open["operations"] = [o for o in no_open["operations"] if o["id"] != dropped]
ok, lines = S3.compare([app("ios", [no_open])])
check("other verbs stay strict: a missing open still fails", (ok, [x for x in lines if x.endswith(": missing")]),
      (False, [f"ios documents.household: operation {dropped}: missing"]))
wrong = copy.deepcopy(files)
wrong["operations"][0]["exposure"] = "hidden" if wrong["operations"][0]["exposure"] != "hidden" else "available"
check("…and so does another verb's exposure", S3.compare([app("ios", [wrong])])[0], False)
undeclared = copy.deepcopy(docs)
undeclared["operations"] = [o for o in undeclared["operations"] if o["id"] not in asks]
ok, lines = S3.compare([app("ios", [undeclared])])
check("no declaration: missing asks fail, as before", (ok, sum(1 for x in lines if x.endswith(": missing"))),
      (False, len(asks)))
groups_off = copy.deepcopy(files)
groups_off["groups"][0]["members"] = groups_off["groups"][0]["members"][::-1]
check("omitting a verb never loosens groups or order",
      S3.compare([app("ios", [groups_off])])[0], len(groups_off["groups"][0]["members"]) < 2)
none_there = copy.deepcopy(th)
none_there["omits_operations"] = ["ask"]
ok, lines = S3.compare([app("ios", [none_there])])
check("declaring a verb the vector doesn't have (threads.alex has no ask): equal, counted as 0",
      (ok, [x for x in lines if "(declared: " in x]), (True, ["ios threads.alex: 0 operation(s) not drawn (declared: ask)"]))
with_group = copy.deepcopy(app_th)
th_opens = {o["id"] for o in raw_th["operations"] if o["verb"] == "open"}
check("threads.alex has opens both inside g-other and outside it",
      (len(th_opens & other_ops) > 0, len(th_opens - other_ops) > 0), (True, True))
with_group["operations"] = [o for o in with_group["operations"] if o["id"] not in th_opens]
with_group["omits_operations"] = ["open"]
ok, lines = S3.compare([app("ios", [with_group])])
check("with omits_groups too: an open inside an omitted group is counted once, by the group",
      (ok, [x for x in lines if "(declared: " in x]),
      (True, [f"ios threads.alex: {len(th_opens - other_ops)} operation(s) not drawn (declared: open)"]))

print("── the items vectors (evalsets/surfaces/items: {status, body}) ──")
names = S3.vector_names()
items_names = [n for n in names if n.startswith("items.")]
check("each 200 surface is a vector named items.<view>.<viewer>", ("items.now.alex" in items_names,
      "items.time.screen" in items_names, "items.people.alex-ava" in items_names), (True, True, True))
nc = S3.not_comparable()
check("refusals and the plain project list are not comparable, and never vectors",
      (sorted(set(nc) & set(names)), all(n in nc for n in ("items.people.screen", "items.project.missing.alex",
                                                           "items.project.private.alex", "items.projects.alex"))), ([], True))
wrapped = json.loads((S3.VECTORS / "items" / "items.now.alex.json").read_text())
check("a vector is its unwrapped body", S3.load("items.now.alex"), wrapped["body"])
exp = S3.expected(["items.now.alex"])
check("expected: the body's lists, and the not-comparable names", (exp["results"][0]["vector"],
      exp["results"][0]["groups"] == S3.lists(wrapped["body"])["groups"], exp["not_comparable"] == nc), ("items.now.alex", True, True))
mine = S3.expected(["items.now.robin"])["results"][0]
check("an app that renders items.now.robin as titan does: equal", S3.compare([app("ios", [mine])])[0], True)
off = copy.deepcopy(mine)
if off["groups"] and off["groups"][0]["members"]:
    off["groups"][0]["members"] = off["groups"][0]["members"][1:]
check("…a missing member: not equal", S3.compare([app("ios", [off])])[0], not mine["groups"] or not mine["groups"][0]["members"])
ok, lines = S3.compare([app("ios", [{"vector": "items.projects.alex", "groups": [], "operations": []}])])
check("emitting a not-comparable one is named as such", (ok, [x for x in lines if "not comparable" in x]),
      (False, ["ios items.projects.alex: not comparable (a refusal or the plain project list, not a surface)"]))

print("── the command line ──")
with tempfile.TemporaryDirectory() as d:
    good, bad = Path(d) / "mac.json", Path(d) / "ios.json"
    good.write_text(json.dumps(app("mac", perfect)))
    rb = copy.deepcopy(perfect)
    drop_member(next(x for x in rb if x["vector"] == "cards.admin"))
    bad.write_text(json.dumps(app("ios", rb)))
    run = lambda *a: subprocess.run([sys.executable, str(HERE / "surfaces_s3.py"), *a], capture_output=True, text=True)  # noqa: E731
    g, b = run("compare", str(good)), run("compare", str(good), str(bad))
    check("compare: exit 0 and 'S3: equal' when equal", (g.returncode, g.stdout.strip().splitlines()[-1]), (0, "S3: equal"))
    check("compare: exit 1 and the diff when not", (b.returncode, "missing members" in b.stdout, b.stdout.strip().splitlines()[-1]),
          (1, True, "S3: NOT equal"))
    e = run("expected", "cards.screen")
    check("expected: the apps' own reference, same shape", (e.returncode, json.loads(e.stdout)["results"][0]["vector"]), (0, "cards.screen"))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
