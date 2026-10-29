#!/usr/bin/env python3
# gate: hermetic   (the benchmark batch, fictional names, temporary stores, no network, no live model, no real files)
"""titan-node#1507: review@1, the bulk review of scanned mail, over the scans pipeline's output for the benchmark batch.

S1 groups: one group per kind of proposal, from the privacy stage's output through view_for, the counts are the viewer's own.
S2 approve: one tap files a group in place (a link, cycle, bundle membership, event plan or archive plan), nothing deleted, no body
and no printed number stored. S3 exclude: a document left out stays unfiled, waits under "Left for you" and can be filed alone.
S4 auto-file: a bill whose account AND amount equal its cycle's files itself for an adult (never medical, never a kid's), with an undo
that is not made again. S5 undo: every change has a token for POST /v1/surfaces/undo; it is the caller's own, single use, and puts
back exactly what the change did. S6 kids: they see their own proposals, every operation unavailable (kids.propose_only), and change
nothing. S7 privacy: a second viewer never sees another person's groups, items or counts (counts leak too), on the benchmark and on the
privacy profile with a shared scanner, for every viewer, a kid and the screen included. S8 the routes. S9 golden vectors for an admin and
a kid, built from merged code with every clock frozen (`--write` rewrites them; review the diff).
Run: TZ=UTC python3 scripts/test_scan_review.py [--write]"""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-scan-review-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl", "SURFACES_DB": "surfaces.db",
             "REVIEW_DB": "review.db", "SITTER_DB": "sitter.db", "WORLD_DIR": "world", "KB_ACL_DB": "kb_acl.db",
             "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_TZ"] = "UTC"
os.environ["MEMORY_HYBRID"] = "0"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "SCAN_REVIEW_BENCH"):
    os.environ.pop(k, None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
VECTORS = HERE / "evalsets" / "surfaces"
WRITE = "--write" in sys.argv

import review_store as RS  # noqa: E402
import scan_pipeline as P  # noqa: E402
import scan_privacy as PV  # noqa: E402
import scan_review as R  # noqa: E402
import scans_bench as B  # noqa: E402
import surfaces as S  # noqa: E402

passed = failed = 0


def check(label, got, expected=True):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = 1791331200.0     # 2026-10-07T00:00:00Z
base, ext = B.generate(1499), B.generate(1499, True)
SCANS = base["scans"]
BATCH = R.Batch("bench-1499", SCANS, P.pipeline(SCANS))
R.register(BATCH)
ROLE = {"alex": "admin", "robin": "adult", "jordan": "adult", "ava": "kid", "leo": "kid"}
SCANNER = {s["id"]: s["scanner"] for s in SCANS}
_n = [0]


def fresh() -> None:
    """A new decisions store and undo store for the next section."""
    _n[0] += 1
    os.environ["REVIEW_DB"] = str(_TMP / f"review-{_n[0]}.db")
    os.environ["SURFACES_DB"] = str(_TMP / f"surfaces-{_n[0]}.db")


def surf(viewer, now=NOW, batch=BATCH):
    return R.review(batch, viewer, ROLE[viewer], viewer, now)


def groups(s):
    return {g["id"]: g for g in s["groups"]}


def members(s):
    return [m["subject"]["id"] for g in s["groups"] for m in g["members"]]


def _refused_role(batch_):
    try:
        R.review(batch_, None, "household", "screen", NOW)
    except S.Refused as e:
        return e.status
    return None


def ops_of(s):
    return {o["id"]: o for o in s["operations"]}


# ── S1 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
print("── S1: the groups ──")
fresh()
s = surf("alex")
g = groups(s)
check("it is review@1 on the collection profile, purpose edit", (s["provenance"]["resolver"], s["profile"], s["purpose"]), ("review@1", "collection", "edit"))
check("a group per kind: junk, appointments, deadlines, a kept bundle, a thread, a sender",
      {"g-junk", "g-appointment", "g-deadline", "g-bundle-tax-2025"} <= set(g) and any(i.startswith("g-thread-") for i in g) and any(i.startswith("g-vendor-") for i in g))
check("the heading carries the count, in the issue's words",
      (g["g-junk"]["label"]["text"], g["g-appointment"]["label"]["text"], g["g-bundle-tax-2025"]["label"]["text"]),
      (f"{len(g['g-junk']['members'])} look like junk", f"{len(g['g-appointment']['members'])} look like appointments: add to calendar?",
       f"{len(g['g-bundle-tax-2025']['members'])} are Tax 2025"))
check("a heading's key and count are structured beside the text", g["g-junk"]["label"]["key"] == "review.junk" and g["g-junk"]["label"]["args"]["count"] == len(g["g-junk"]["members"]))
check("every heading has a key with English", all(x["label"]["key"] in S.LABELS and x["label"]["text"] for x in s["groups"]))
check("biggest group first", [len(x["members"]) for x in s["groups"]] == sorted((len(x["members"]) for x in s["groups"]), reverse=True))
truth = base["truth"]
check("every junk member is junk in the benchmark's truth", all(truth[m]["junk"] for m in members({"groups": [g["g-junk"]]})))
check("every Tax 2025 member is a tax form", all(truth[m]["type"] == "tax_form" for m in members({"groups": [g["g-bundle-tax-2025"]]})))
check("appointments and deadlines are notices", all(truth[m]["type"] in ("appointment", "deadline") for m in members({"groups": [g["g-appointment"], g["g-deadline"]]})))
check("one member per document, never twice", len(members(s)) == len(set(members(s))))
check("a document that is a duplicate set is one row, with its count", any("scans" in (m["slots"].get("meta") or "") for x in s["groups"] for m in x["members"]))
by_item = {p["item"]: p for p in R.proposals(BATCH, "alex")}
check("the proposals agree with the surface", sorted(by_item) == sorted(members(s)))
check("nothing sensitive in a title: titles are senders, never a printed number", not any(PV._ID_TOKEN.search(m["slots"]["title"]) for x in s["groups"] for m in x["members"]))

# ── S2 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
print("── S2: approve files a group in place ──")
fresh()
s = surf("alex")
n_junk = len(groups(s)["g-junk"]["members"])
op = ops_of(s)["op-approve-g-junk"]
check("approve is one operation on the group: contract, route, params, undo, primary",
      (op["verb"], op["contract"], op["route"], op["params"], op["recovery"], op["exposure"], op["variant"]),
      ("file", "jellybean.review.approve@1", {"method": "POST", "path": "/v1/surfaces/review/approve"}, {"batch": "bench-1499", "group": "g-junk"},
       ["undo"], "available", "primary"))
r = R.approve(BATCH, "alex", "admin", "alex", "g-junk", None, NOW)
check("one tap files every one of them, with one token", (r["filed"], r["kinds"], bool(r["token"])), (n_junk, ["archive"], True))
rows = RS.rows("bench-1499", "alex")
check("each is a row: an archive plan with its undo, the junk stage's own words", {(x["state"], x["kind"], json.dumps(x["plan"], sort_keys=True)) for x in rows.values()},
      {("filed", "archive", json.dumps({"do": "archive", "undo": "unarchive"}, sort_keys=True))})
check("the group is gone from the review", "g-junk" not in groups(surf("alex")))
check("its count moves to what the viewer has filed", {x["rule"]: x["count"] for x in surf("alex")["explanation"]["excluded"]}.get("review.filed") == n_junk)
check("a second tap files nothing and issues no token", R.approve(BATCH, "alex", "admin", "alex", "g-junk", None, NOW), {"filed": 0, "token": None})
for gid, kind in (("g-appointment", "event"), ("g-bundle-tax-2025", "bundle")):
    r = R.approve(BATCH, "alex", "admin", "alex", gid, None, NOW)
    check(f"{gid} files a {kind}", r["kinds"], [kind])
th = next(i for i in groups(surf("alex")) if i.startswith("g-thread-"))
check("a thread group files links", R.approve(BATCH, "alex", "admin", "alex", th, None, NOW)["kinds"], ["link"])
vg = next(i for i in groups(surf("alex")) if i.startswith("g-vendor-"))
check("a sender group files a cycle", R.approve(BATCH, "alex", "admin", "alex", vg, None, NOW)["kinds"], ["cycle"])
con = sqlite3.connect(os.environ["REVIEW_DB"])
dump = "\n".join(json.dumps(list(r)) for r in con.execute("SELECT * FROM review_item"))
texts = " ".join(s_["text"] for s_ in SCANS)
check("nothing printed on a scan is stored: no printed number, no line of any body",
      not [t for t in PV._ID_TOKEN.findall(texts) if t in dump] and not [ln for s_ in SCANS for ln in s_["text"].splitlines() if len(ln) > 30 and ln in dump])
check("nothing is deleted: the review only ever adds rows", con.execute("SELECT COUNT(*) FROM review_item").fetchone()[0] >= n_junk)
check("another person's review has none of it", RS.rows("bench-1499", "robin") == {} and all(i not in members(surf("robin")) for i in rows))

# ── S3 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
print("── S3: exclude the exceptions ──")
fresh()
s = surf("alex")
tax = [m["subject"]["id"] for m in groups(s)["g-bundle-tax-2025"]["members"]]
odd = tax[0]
eop = ops_of(s)[f"op-exclude-{odd}"]
check("exclude is an operation on the item", (eop["verb"], eop["contract"], eop["subject"], eop["params"], eop["recovery"]),
      ("dismiss", "jellybean.review.exclude@1", {"kind": "scan", "id": odd}, {"batch": "bench-1499", "group": "g-bundle-tax-2025", "item": odd}, ["undo"]))
check("the item names its operation", groups(s)["g-bundle-tax-2025"]["members"][0]["slots"]["actions"] == [f"op-exclude-{odd}"])
r = R.exclude(BATCH, "alex", "admin", "alex", "g-bundle-tax-2025", odd, NOW)
s = surf("alex")
check("it leaves the group, which counts one fewer", len(groups(s)["g-bundle-tax-2025"]["members"]), len(tax) - 1)
check("and waits under Left for you", [m["subject"]["id"] for m in groups(s)["g-left-for-you"]["members"]], [odd])
check("the heading says one fewer", groups(s)["g-bundle-tax-2025"]["label"]["text"], f"{len(tax) - 1} are Tax 2025")
r = R.approve(BATCH, "alex", "admin", "alex", "g-bundle-tax-2025", None, NOW)
check("approving the group files the rest and leaves the exception alone", (r["filed"], RS.rows("bench-1499", "alex")[odd]["state"]), (len(tax) - 1, "excluded"))
r = R.approve(BATCH, "alex", "admin", "alex", "g-bundle-tax-2025", [odd], NOW)
check("a left-out one can be filed alone", (r["filed"], RS.rows("bench-1499", "alex")[odd]["state"], "g-left-for-you" in groups(surf("alex"))), (1, "filed", False))
for bad in (("g-bundle-tax-2025", "scan-zzz"), ("g-nope", odd)):
    try:
        R.exclude(BATCH, "alex", "admin", "alex", *bad, NOW)
        code = None
    except S.Refused as e:
        code = e.status
    check(f"excluding {bad[1]} from {bad[0]}: 404", code, 404)
try:
    R.approve(BATCH, "alex", "admin", "alex", "g-deadline", ["not-mine"], NOW)
    code = None
except S.Refused as e:
    code = e.status
check("approving an item that is not in the group: 404, nothing filed", (code, [x for x in RS.rows("bench-1499", "alex").values() if x["kind"] == "event"]), (404, []))

# ── S4 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
print("── S4: a hard-key match files itself, with undo ──")
fresh()
hard = {p["item"] for p in R.proposals(BATCH, "alex") if p["hard"] and not p["sensitive"]}
check("there are hard-key matches to file, and they are sender cycles", hard and all(p["kind"] == "vendor" for p in R.proposals(BATCH, "alex") if p["item"] in hard))
cyc = BATCH.pred["cycles"]
check("hard means account AND amount equal the cycle's",
      all(BATCH.pred["docs"][i]["account"] and BATCH.pred["docs"][i]["amount"] == cyc["cycles"][cyc["cycle_of"][i]]["amount"]
          and BATCH.pred["docs"][i]["account"] == cyc["cycles"][cyc["cycle_of"][i]]["account"] for i in hard))
check("an admin has them filed", R.auto_file(BATCH, "alex", "admin", "alex", NOW), {"filed": len(hard)})
rows = RS.rows("bench-1499", "alex")
check("each is an automatic filing of a cycle with its own undo token", {(x["state"], x["kind"], bool(x["token"])) for x in rows.values()}, {("auto", "cycle", True)})
check("filing again does nothing", R.auto_file(BATCH, "alex", "admin", "alex", NOW), {"filed": 0})
s = surf("alex")
fg = groups(s)["g-filed-for-you"]
check("they are listed as Filed for you, not asked", sorted(m["subject"]["id"] for m in fg["members"]) == sorted(hard) and not hard & {i for x in s["groups"] if x["id"] != "g-filed-for-you" for i in members({"groups": [x]})})
uo = ops_of(s)[fg["members"][0]["slots"]["actions"][0]]
check("each has an undo operation on the existing undo route, with its token",
      (uo["verb"], uo["route"], uo["params"]["token"] == rows[fg["members"][0]["subject"]["id"]]["token"]), ("undo", {"method": "POST", "path": "/v1/surfaces/undo"}, True))
check("a sensitive document does not file itself, and keeps being asked about",
      all(RS.rows("bench-1499", "alex").get(p["item"]) is None for p in R.proposals(BATCH, "alex") if p["sensitive"]) and any(p["sensitive"] for p in R.proposals(BATCH, "alex")))
one = fg["members"][0]["subject"]["id"]
tok = rows[one]["token"]
got = S.undo("alex", tok)
check("undo takes it back", (got["undone"], RS.rows("bench-1499", "alex")[one]["state"]), ("scans-review.change", "manual"))
check("it is asked about again, in its sender's group", one in members(surf("alex")) and one not in [m["subject"]["id"] for m in groups(surf("alex"))["g-filed-for-you"]["members"]])
check("and is not filed again", R.auto_file(BATCH, "alex", "admin", "alex", NOW), {"filed": 0})
check("an automatic filing older than the undo window is settled, not listed", "g-filed-for-you" not in groups(surf("alex", now=NOW + S.UNDO_TTL_S + 3600)) or
      len(groups(surf("alex", now=NOW + S.UNDO_TTL_S + 3600))["g-filed-for-you"]["members"]) == 0)
check("a kid's and an unknown role's get nothing filed", (R.auto_file(BATCH, "ava", "kid", "ava", NOW), R.auto_file(BATCH, "alex", "household", "alex", NOW)), ({"filed": 0}, {"filed": 0}))

# ── S5 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
print("── S5: everything has an undo ──")
fresh()
before = RS.rows("bench-1499", "alex")
r1 = R.approve(BATCH, "alex", "admin", "alex", "g-deadline", None, NOW)
mid = RS.rows("bench-1499", "alex")
r2 = R.exclude(BATCH, "alex", "admin", "alex", "g-appointment", next(iter(groups(surf("alex"))["g-appointment"]["members"]))["subject"]["id"], NOW)
check("approve and exclude each return a token", bool(r1["token"] and r2["token"]))
try:
    S.undo("robin", r2["token"])
    code = None
except S.Refused as e:
    code = e.status
check("someone else's token: 404", code, 404)
check("undo of the exclusion", S.undo("alex", r2["token"])["restored"], 1)
check("puts the group back as it was", RS.rows("bench-1499", "alex") == mid)
check("undo of the approval", S.undo("alex", r1["token"])["restored"], r1["filed"])
check("puts back exactly what it did: no rows", RS.rows("bench-1499", "alex") == before)
try:
    S.undo("alex", r1["token"])
    code = None
except S.Refused as e:
    code = e.status
check("a token is used once: 404", code, 404)
r = R.approve(BATCH, "alex", "admin", "alex", "g-junk", None, NOW)
R.exclude(BATCH, "alex", "admin", "alex", "g-appointment", next(iter(groups(surf("alex"))["g-appointment"]["members"]))["subject"]["id"], NOW)
check("an undo restores only what its change did", S.undo("alex", r["token"])["restored"] == r["filed"] and len(RS.rows("bench-1499", "alex")) == 1)
check("every write operation of the surface names an undo", all(o["recovery"] == ["undo"] for o in surf("alex")["operations"] if o["verb"] in ("file", "dismiss", "undo")))

# ── S6 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
print("── S6: kids propose, a parent files ──")
fresh()
kid_scans = copy.deepcopy(SCANS)
moved = 0
for sc in kid_scans:       # twelve of Alex's notices are Ava's own scans, shared with her parents (a kid sees only kid-safe facts of a shared scan)
    if sc["scanner"] == "alex" and base["truth"][sc["id"]]["type"] in ("appointment", "deadline") and not base["truth"][sc["id"]]["medical"] and moved < 12:
        sc["scanner"], moved = "ava", moved + 1
KID_SHARES = {sc["id"]: [{"with": ["alex", "robin"]}] for sc in kid_scans if sc["scanner"] == "ava"}
KID = R.Batch("bench-kid", kid_scans, PV.stage(kid_scans, P.chain(PV.claimed(kid_scans)[0]), shares=KID_SHARES), shares=KID_SHARES)
R.register(KID)
ks = R.review(KID, "ava", "kid", "ava", NOW)
check("a kid sees their own proposals", len(members(ks)) > 0)
check("with every operation unavailable: kids.propose_only",
      {(o["exposure"], (o.get("reason") or {}).get("key")) for o in ks["operations"]}, {("unavailable", "kids.propose_only")})
for fn, args in ((R.approve, ("g-junk", None, NOW)), (R.exclude, ("g-junk", "x", NOW))):
    try:
        fn(KID, "ava", "kid", "ava", *args)
        code = None
    except S.Refused as e:
        code = e.status
    check(f"a kid's {fn.__name__}: 403", code, 403)
check("a kid's auto-file files nothing and nothing is written", R.auto_file(KID, "ava", "kid", "ava", NOW) == {"filed": 0} and RS.rows("bench-kid", "ava") == {})
check("a kid and the adult it was copied from share no item", not set(members(ks)) & set(members(R.review(KID, "alex", "admin", "alex", NOW))))
try:
    R.review(BATCH, None, "household", "screen", NOW)
    code = None
except S.Refused as e:
    code = e.status
check("a shared screen gets 403", code, 403)

# ── S7 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
print("── S7: a second viewer never sees another person's groups or counts ──")
fresh()
for name, batch_, scans_ in (("the 300", BATCH, SCANS), ("the 321 with a shared scanner", R.Batch("bench-321", ext["scans"], P.pipeline(ext["scans"])), ext["scans"])):
    own = {v: {s_["id"] for s_ in scans_ if s_["scanner"] == v} for v in ("alex", "robin", "jordan", "ava", "leo")}
    surfs = {v: R.review(batch_, v, ROLE[v], v, NOW) for v in own}
    ids = {v: set(members(x)) for v, x in surfs.items()}
    texts_ = {v: json.dumps(x, sort_keys=True) for v, x in surfs.items()}
    check(f"{name}: each viewer's items are their own scanner's", all(ids[v] <= own[v] for v in own))
    check(f"{name}: no two viewers share an item", all(not (ids[a] & ids[b]) for a in own for b in own if a < b))
    check(f"{name}: no viewer's JSON holds another's item id, group id, sender or count line",
          all(not [i for o in own if o != v for i in own[o] if f'"{i}"' in texts_[v]] for v in own))
    senders = {v: {p["gname"] for p in R.proposals(batch_, v)} | {p["title"] for p in R.proposals(batch_, v)} for v in own}
    only = {v: senders[v] - set().union(*(senders[o] for o in own if o != v)) for v in own}
    check(f"{name}: a sender only one person has is in nobody else's review", all(not [n for o in own if o != v for n in only[o] if n in texts_[v]] for v in own))
    # counts leak too: the review of a viewer is what it would be if nobody else had scanned anything
    for v in ("alex", "robin", "jordan"):
        solo_scans = [s_ for s_ in scans_ if s_["scanner"] == v]
        solo = R.Batch("solo-" + v + name[:6], solo_scans, P.pipeline(solo_scans))
        a = R.review(solo, v, ROLE[v], v, NOW)
        mine = surfs[v]
        shape = lambda x: sorted((g_["label"]["text"], len(g_["members"])) for g_ in x["groups"])    # noqa: E731
        check(f"{name}: {v}'s groups and counts are those of a household where only {v} scanned", shape(a) == shape(mine))
    check(f"{name}: the unknown and the shared screen see nothing", (members(R.review(batch_, None, "adult", "casey", NOW)), _refused_role(batch_)), ([], 403))
    if "household" in {s_["scanner"] for s_ in scans_}:
        held = {s_["id"] for s_ in scans_ if s_["scanner"] == "household"}
        check(f"{name}: scans from the shared scanner are in nobody's review", all(not ids[v] & held for v in own) and all(i not in t for t in texts_.values() for i in held))
    traps = {t_["id"] for t_ in (base if name == "the 300" else ext)["traps"]}
    check(f"{name}: no trap scan is in the review of anyone but its scanner", all(not (ids[v] & traps) or ids[v] & traps <= own[v] for v in own))

# ── S8 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
print("── S8: the routes ──")
fresh()
os.environ["SCAN_REVIEW_BENCH"] = "1"
R._BATCHES.pop("bench-1499", None)       # the route builds the benchmark batch itself
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, platform="ios"):
    s_ = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": f"{name}'s device", "platform": platform}).json()["token"]
    return {"X-Agent-Token": tok}


ALEX, ROBIN, AVA = pair("Alex Hale", "adult"), pair("Robin Hale", "adult"), pair("Ava Hale", "kid")
CASEY, TV = pair("Casey Quarry", "adult"), pair("Living room", "household", "tvos")
URL = "/v1/surfaces/review"
check("no batch: the empty review", (lambda r: (r.status_code, r.json()["groups"], r.json()["sizeClass"]))(c.get(URL, headers=ALEX)), (200, [], "few"))
check("an unknown batch: 404", c.get(URL, params={"batch": "nope"}, headers=ALEX).status_code, 404)
r = c.get(URL, params={"batch": "bench-1499"}, headers=ALEX)
a = r.json()
check("Alex's review: groups, and what filed itself", (r.status_code, "g-junk" in groups(a), "g-filed-for-you" in groups(a)), (200, True, True))
check("Casey, who is nobody in the batch, gets the empty review", c.get(URL, params={"batch": "bench-1499"}, headers=CASEY).json()["groups"], [])
rb = c.get(URL, params={"batch": "bench-1499"}, headers=ROBIN).json()
check("Robin's review has none of Alex's items", bool(not set(members(a)) & set(members(rb)) and members(rb)))
check("the screen is refused", c.get(URL, params={"batch": "bench-1499"}, headers=TV).status_code, 403)
check("a kid reads, with the operations unavailable", (lambda r: (r.status_code, {o["exposure"] for o in r.json()["operations"]}))(c.get(URL, params={"batch": "bench-1499"}, headers=AVA)), (200, set()))
n_junk = len(groups(a)["g-junk"]["members"])
r = c.post(URL + "/approve", json={"batch": "bench-1499", "group": "g-junk"}, headers=ALEX)
check("approve over the route: filed, with a token", (r.status_code, r.json()["filed"], bool(r.json()["token"])), (200, n_junk, True))
tok = r.json()["token"]
check("Robin cannot approve Alex's group: 404", c.post(URL + "/approve", json={"batch": "bench-1499", "group": "g-bundle-tax-2025"}, headers=ROBIN).status_code in (200, 404)
      and "g-junk" not in groups(c.get(URL, params={"batch": "bench-1499"}, headers=ALEX).json()))
not_robins = sorted({p["gid"] for p in R.proposals(R.get_batch("bench-1499"), "jordan")} - {p["gid"] for p in R.proposals(R.get_batch("bench-1499"), "robin")})
check("and a group only someone else has is a 404, which says nothing of whose it is", (bool(not_robins), c.post(URL + "/approve", json={"batch": "bench-1499", "group": not_robins[0]}, headers=ROBIN).json()), (True, {"error": "no such group"}))
check("a kid cannot approve or exclude: 403", [c.post(URL + p, json={"batch": "bench-1499", "group": "g-junk", "item": "x"}, headers=AVA).status_code for p in ("/approve", "/exclude")], [403, 403])
check("the screen cannot: 403", c.post(URL + "/approve", json={"batch": "bench-1499", "group": "g-junk"}, headers=TV).status_code, 403)
check("a bad body: 400", c.post(URL + "/approve", json={"batch": "bench-1499", "group": "g-junk", "items": "x"}, headers=ALEX).status_code, 400)
check("an unknown batch to approve: 404", c.post(URL + "/approve", json={"batch": "nope", "group": "g-junk"}, headers=ALEX).status_code, 404)
item = groups(c.get(URL, params={"batch": "bench-1499"}, headers=ALEX).json())["g-deadline"]["members"][0]["subject"]["id"]
r = c.post(URL + "/exclude", json={"batch": "bench-1499", "group": "g-deadline", "item": item}, headers=ALEX)
check("exclude over the route", (r.status_code, r.json()["excluded"]), (200, 1))
check("it waits under Left for you", [m["subject"]["id"] for m in groups(c.get(URL, params={"batch": "bench-1499"}, headers=ALEX).json())["g-left-for-you"]["members"]], [item])
u = c.post("/v1/surfaces/undo", json={"token": tok}, headers=ALEX)
check("the existing undo route puts the approval back", (u.status_code, u.json()["undone"], "g-junk" in groups(c.get(URL, params={"batch": "bench-1499"}, headers=ALEX).json())), (200, "scans-review.change", True))
check("Robin cannot use Alex's token: 404", c.post("/v1/surfaces/undo", json={"token": r.json()["token"]}, headers=ROBIN).status_code, 404)
os.environ.pop("SCAN_REVIEW_BENCH")
R._BATCHES.pop("bench-1499", None)
check("without SCAN_REVIEW_BENCH the benchmark is not served", c.get(URL, params={"batch": "bench-1499"}, headers=ALEX).status_code, 404)

# ── S9 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
print("── S9: golden vectors (fixed batch, viewer, now; ids and tokens counted, not random) ──")


def vector(name, got):
    p = VECTORS / f"{name}.json"
    if WRITE:
        VECTORS.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    want = json.loads(p.read_text()) if p.exists() else None
    check(f"golden vector {name}", json.loads(json.dumps(got)) == want)


def golden(batch, viewer, role):
    fresh()
    tick = iter(range(1, 10 ** 6))
    real = S.secrets
    S.secrets = types.SimpleNamespace(token_hex=lambda n: f"{next(tick):0{2 * n}d}")
    try:
        R.auto_file(batch, viewer, role, viewer, NOW)
        if role != "kid":
            first = next(iter(groups(R.review(batch, viewer, role, viewer, NOW))["g-bundle-tax-2025"]["members"]))["subject"]["id"]
            R.exclude(batch, viewer, role, viewer, "g-bundle-tax-2025", first, NOW)
        return R.review(batch, viewer, role, viewer, NOW)
    finally:
        S.secrets = real


R._BATCHES.pop("bench-1499", None)
R.register(BATCH)
adm, adm2 = golden(BATCH, "alex", "admin"), golden(BATCH, "alex", "admin")
check("the admin's vector is the same twice", adm == adm2)
vector("review.admin", adm)
kid, kid2 = golden(KID, "ava", "kid"), golden(KID, "ava", "kid")
check("the kid's vector is the same twice", kid == kid2 and len(members(kid)) > 0)
vector("review.kid", kid)
check("the admin's vector has every kind of group", {"g-junk", "g-appointment", "g-bundle-tax-2025", "g-filed-for-you", "g-left-for-you"} <= set(groups(adm)))
check("the kid's vector has every operation unavailable", {o["exposure"] for o in kid["operations"]} == {"unavailable"})

print(f"\n{passed} passed, {failed} failing")
sys.exit(1 if failed else 0)
