#!/usr/bin/env python3
# gate: hermetic
"""Per-viewer items vectors for the apps (#1060, iOS): GET /v1/items for now, time, unsorted, a person's page and a
project, and GET /v1/projects, for the admin (Alex), another adult (Robin) and a kid (Ava), plus now and time on a
shared screen, through the real route over one fictional house. Saved as golden vectors in
scripts/evalsets/surfaces/items/items.<view>.<viewer>.json (`--write` rewrites them); this test fails on any drift.
Privacy edges: the admin on a kid's page, a screen on any person's page (404, like a missing person), and a non-member asking for a
private project (refused exactly like a missing one, so it never says the project exists).
Generated ids (people, to-dos, reminders, projects, chats) are replaced by stable names, and the clock is fixed, so a
vector only changes when the surface does. Run: python3 scripts/test_surfaces_items_viewers.py [--write]"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

T0 = 1791565200.0   # 2026-10-09T15:00:00Z, fixed for every store and the route
_real_time = time.time
time.time = lambda: T0
_TMP = Path(tempfile.mkdtemp(prefix="titan-items-vectors-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db",
             "SCHEDULED_DB": "scheduled.db", "REFERENCES_DB": "references.db", "WORLD_DIR": "world",
             "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt", "FENCES_DB": "fences.db", "TASKS_DB": "tasks.db",
             "SITTER_DB": "sitter.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db", "PUSH_DB": "push.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "ITEM_REFS_DB": "item_refs.db", "SURFACES_DB": "surfaces.db", "ITEMS_LATENCY_LOG": "items_latency.jsonl",
             "MAIL_BILLS_DB": "mail_bills.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE"):
    os.environ.pop(k, None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
VECTORS = HERE / "evalsets" / "surfaces" / "items"   # route answers ({status, body}), apart from the bare surfaces
WRITE = "--write" in sys.argv
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import projects  # noqa: E402
import scheduled  # noqa: E402
import spaces  # noqa: E402
import todos  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo as _Z  # noqa: E402
lib._local_now = lambda: _dt.fromtimestamp(T0, _Z("America/Los_Angeles"))   # the route's clock, fixed too
lib.kb_sources = lambda *a, **k: []          # no KB runtime here: notes and mail are empty for everyone
lib.record_nodes = lambda *a, **k: ([], True)
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


# person ids made deterministic (the fingerprint and resolution id hash them): token_hex(4) only, the rest stays real
import secrets as _secrets  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402
_pseq = iter(range(1, 1000))
hh.secrets = _NS(**{n: getattr(_secrets, n) for n in dir(_secrets) if not n.startswith("_")})
hh.secrets.token_hex = lambda n=None: f"{next(_pseq):08x}" if n == 4 else _secrets.token_hex(n)
# the house: Alex the admin (the setup key), Robin an adult, Ava a kid (Robin's and Alex's), a shared screen
alex = hh.ADMIN_PERSON_ID
c.patch(f"/v1/people/{alex}", json={"name": "Alex Quarry"}, headers=M)
ROBIN, robin = pair("Robin Quarry", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Quarry", "kid", "Ava's iPhone")
SCREEN, _screen = pair("Living room", "household", "Living room TV", platform="tvos")
c.patch(f"/v1/people/{ava}", json={"parents": [robin, alex]}, headers=M)
VIEW = {"alex": M, "robin": ROBIN, "ava": AVA, "screen": SCREEN}
IDS: dict[str, str] = {alex: "p-alex", robin: "p-robin", ava: "p-ava"}

H = 3600
import uuid  # noqa: E402
_seq = iter(range(1, 1 << 20))
uuid.uuid4 = lambda: uuid.UUID(int=next(_seq) << 100)   # deterministic ids from here on (to-dos, projects, sessions)


def seed_at(k):
    """Seeding: each write at its own fixed second, so no order rests on a random id or on how often code reads it."""
    time.time = lambda: T0 - 3 * H + k


for k, (who, pid, title) in enumerate((("alex", alex, "Renew the parking permit"), ("robin", robin, "Call the dentist"),
                                     ("ava", ava, "Finish the reading log"), ("alex", "household", "Bins out tonight"))):
    seed_at(10 + k)
    IDS[todos.add(title, pid, created_by=who)["id"]] = f"todo-{who}" if pid != "household" else "todo-house"
for key, (pid, what, at, aud) in {"sch-alex": (alex, "Water the plants", T0 + 26 * H, "household"),
                                  "sch-robin": (robin, "Pay Hale Water", T0 + 2 * H, "private"),
                                  "sch-ava": (ava, "Pack the swim bag", T0 + 20 * H, "private")}.items():
    seed_at(20 + len(IDS))
    IDS[scheduled.add(pid, what, "notify", {"text": "x"}, at, audience=aud)["id"]] = key
seed_at(40)
shared = spaces.create_project(alex, "admin", "Kitchen remodel", audience="adults")
seed_at(41)
secret = spaces.create_project(robin, "adult", "Surprise party", audience="private")
seed_at(42)
IDS[shared["id"]], IDS[secret["id"]] = "pr-kitchen", "pr-party"
projects.file(alex, "admin", f"todo:{next(k for k, v in IDS.items() if v == 'todo-alex')}", shared["id"])
projects.file(robin, "adult", f"todo:{next(k for k, v in IDS.items() if v == 'todo-robin')}", secret["id"])
for k, (who, pid, text) in enumerate((("alex", alex, "Which tiles did we pick for the backsplash?"),
                                     ("robin", robin, "Ideas for the party playlist"))):
    seed_at(50 + k)
    api._get_memory().add_message(hh.conv_id_for(pid), "user", text)
# 10-10 (#1496: Unsorted lists only what has a suggestion): each adult gets one chat filed in their project and a
# second, unfiled chat with an accepted record link to it, so each adult's Unsorted has exactly one suggested member and
# the per-person checks (here and in the apps' NowPagesViewerMatrixTests) have something real to check.
import chat_links  # noqa: E402
import conversations  # noqa: E402
import hooks  # noqa: E402
_edges = []
for k, (who, pid, proj, filed_text, new_text) in enumerate((
        ("alex", alex, shared["id"], "Grout colour for the kitchen", "the grout colour again"),
        ("robin", robin, secret["id"], "Cake for the party", "the cake again"))):
    seed_at(60 + 2 * k)
    api._get_memory().add_message(hh.conv_id_for(pid), "user", filed_text, thread=f"th-{who}-filed")
    seed_at(61 + 2 * k)
    api._get_memory().add_message(hh.conv_id_for(pid), "user", new_text, thread=f"th-{who}-new")
    convs = conversations.list_for(api._get_memory()._db, hh.conv_id_for(pid), 10)
    c_filed = next(x["id"] for x in convs if filed_text in (x.get("title") or ""))
    c_new = next(x["id"] for x in convs if new_text in (x.get("title") or ""))
    projects.file(pid, "admin" if who == "alex" else "adult", f"chat:{c_filed}", proj)
    _edges.append(chat_links.Edge(*sorted((chat_links.item_id(c_new), chat_links.item_id(c_filed))), "record",
                                  "accepted", pid, ["same record touched"], {"hours_apart": 1.0}, "x"))
_lcon = hooks.connect()
chat_links.save(_lcon, _edges)
_lcon.close()
time.time = lambda: T0   # the route: one fixed instant


def norm(obj):
    """Stable names for generated ids (people and conversation keys; the rest are made deterministic above)."""
    s = json.dumps(obj, sort_keys=True)
    for raw in sorted(IDS, key=len, reverse=True):
        s = s.replace(raw, IDS[raw])
    # the one stamp read from the real clock (time.gmtime()), pinned to the fixed instant
    s = re.sub(r'"generated_at": "[^"]*"', '"generated_at": "2026-10-09T15:00:00Z"', s)
    for u in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s):   # session ids
        UUIDS.setdefault(u, f"uuid-{len(UUIDS) + 1}")
        s = s.replace(u, UUIDS[u])
    out = json.loads(s)
    if isinstance(out, dict) and isinstance(out.get("dependencies"), list):   # listed in raw-id order: sort by name
        out["dependencies"] = sorted(out["dependencies"], key=lambda d: json.dumps(d, sort_keys=True))
    return out


UUIDS: dict[str, str] = {}


def vector(name, got):
    p = VECTORS / f"{name}.json"
    if WRITE:
        p.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    check(f"golden vector {name}", got, json.loads(p.read_text()) if p.exists() else None)


def get(who, path):
    r = c.get(path, headers=VIEW[who])
    return {"status": r.status_code, "body": norm(r.json())}


TIME = "&from=2026-10-09&to=2026-10-15"
OTHER = {"alex": robin, "robin": alex, "ava": alex}
got: dict[str, dict] = {}
for who in ("alex", "robin", "ava"):
    got[f"items.now.{who}"] = get(who, "/v1/items?view=now")
    got[f"items.time.{who}"] = get(who, "/v1/items?view=time" + TIME)
    got[f"items.unsorted.{who}"] = get(who, "/v1/items?view=unsorted")
    got[f"items.people.{who}"] = get(who, f"/v1/items?view=person:{OTHER[who]}")
    got[f"items.project.{who}"] = get(who, f"/v1/items?view=project:{shared['id']}")
    got[f"items.projects.{who}"] = {"not_sss": "GET /v1/projects: the plain project list, not a ResolvedSurface",
                                    **get(who, "/v1/projects")}
got["items.now.screen"] = get("screen", "/v1/items?view=now")
got["items.time.screen"] = get("screen", "/v1/items?view=time" + TIME)
# privacy edges (Coordination, 10-09)
got["items.people.alex-ava"] = get("alex", f"/v1/items?view=person:{ava}")
got["items.people.screen"] = get("screen", f"/v1/items?view=person:{alex}")
got["items.project.private.alex"] = get("alex", f"/v1/items?view=project:{secret['id']}")
got["items.project.missing.alex"] = get("alex", "/v1/items?view=project:pr-doesnotexist")

print("── golden vectors ──")
for name, v in got.items():
    vector(name, v)

print("── every surface answer validates against the jb-1 schema (S0) ──")
from jsonschema import Draft7Validator  # noqa: E402
SCHEMA = Draft7Validator(json.loads((HERE.parent / "docs" / "surfaces.schema.json").read_text()))
for name, v in got.items():
    if v["status"] == 200 and "not_sss" not in v:
        errs = sorted(e.message[:120] for e in SCHEMA.iter_errors(v["body"]))
        check(f"{name}: valid", errs[:2], [])

print("── the privacy edges ──")
scr = got["items.people.screen"]
check("a shared screen gets no person's page: the same 404 as a missing person",
      (scr, get("screen", "/v1/items?view=person:p-nobody")), (scr, scr))
check("…a 404", scr["status"], 404)
priv, miss = got["items.project.private.alex"], got["items.project.missing.alex"]
check("a non-member asking for a private project gets exactly what a missing id gets",
      (priv["status"], json.dumps(priv["body"]).replace("pr-party", "X")),
      (miss["status"], json.dumps(miss["body"]).replace("pr-doesnotexist", "X")))
flat = lambda v: json.dumps(v)  # noqa: E731
check("Robin's private project and to-do never reach Alex, Ava or the screen",
      [n for n, v in got.items() if not n.endswith(".robin") and any(w in flat(v) for w in ("Surprise party", "Call the dentist"))
       and n != "items.project.private.alex"], [])
check("…nor does the private project's name leak through the refusal", "Surprise party" in flat(priv), False)
check("Alex's private to-do never reaches Robin, Ava or the screen (the project is the adults', the to-do Alex's)",
      sorted(n for n, v in got.items() if "Renew the parking permit" in flat(v) and not n.endswith((".alex", "alex-ava"))
             and ".alex" not in n), [])
check("the kid sees no adults' project", "Kitchen remodel" in flat(got["items.projects.ava"]) or
      got["items.project.ava"]["status"] == 200 and "Kitchen remodel" in flat(got["items.project.ava"]), False)
check("the screen sees the household's things only", [w for w in ("Renew the parking permit", "Call the dentist",
      "Finish the reading log", "Pay Hale Water", "Pack the swim bag", "Kitchen remodel")
      if w in flat(got["items.now.screen"]) + flat(got["items.time.screen"])], [])
check("each person's chat stays theirs", ([n for n, v in got.items() if "backsplash" in flat(v)],
                                         [n for n, v in got.items() if "playlist" in flat(v)]),
      (["items.time.alex"], ["items.time.robin"]))   # (10-10: a plain chat isn't a filing backlog, so not in now/unsorted)
check("fixtures use fictional names only", [n for n in ("Zach", "Miranda", "Sabrina", "Conor", "Kate")
                                             if n in flat(got)], [])
time.time = _real_time
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
