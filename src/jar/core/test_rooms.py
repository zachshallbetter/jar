#!/usr/bin/env python3
# gate: hermetic   (temp stores; Home Assistant's areas passed in, never asked; nothing live)
"""Settings › Rooms (titan-node#1511): the household's rooms as a store keyed to Home Assistant's areas (read, never
written), with add, rename and remove. The screens in a room follow a rename and lose a removed room (named in the
result: household screens only, never a person's own device, which would say where they are). Rename and remove undo
in one token. A renamed HA room still finds its area for occupancy and for room hints. Kids see the rooms with the
operations unavailable; a shared screen gets none. Fictional household.
Run: python3 scripts/test_rooms.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-rooms-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "ROOMS_DB": "rooms.db", "SURFACES_DB": "surfaces.db",
             "WORLD_DIR": "world"}.items():
    os.environ[k] = str(_TMP / v)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import household_identity as hh  # noqa: E402
import room_state  # noqa: E402
import rooms as R  # noqa: E402
import surfaces as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


st = hh.get_store()
for pid, name, role in (("p_robin", "Robin", "adult"), ("p_ava", "Ava", "kid"), ("screen_k", "Kitchen screen", "household"),
                        ("screen_l", "Living screen", "household")):
    st._db.execute("INSERT OR IGNORE INTO people (id, name, role, created_at) VALUES (?,?,?,0)", (pid, name, role))
for did, pid, name, room in (("tv-k", "screen_k", "Kitchen TV", "Kitchen"), ("tv-l", "screen_l", "Living TV", "Living room"),
                             ("ph-r", "p_robin", "Robin's iPhone", "Kitchen")):
    st._db.execute("INSERT INTO devices (id, person_id, name, platform, token_sha256, created_at, room) VALUES (?,?,?,?,?,0,?)",
                   (did, pid, name, "ios", "t-" + did, room))
st._db.commit()
ADMIN = hh.ADMIN_PERSON_ID
HA = ["Kitchen", "Family Room", "Garage"]


def surface(person, role):
    return S.rooms_surface(person, role, R.sync(HA, st.device_rooms()), S._screens(st), 1_791_640_000)


def names(env):
    return [m["slots"]["title"] for g in env["groups"] for m in g["members"]]


def room_id(name):
    return next(r["id"] for r in R.live() if r["name"] == name)


def room_of(did):
    return next(d["room"] for p in st.people() for d in p["devices"] if d["id"] == did)


print("── the store: Home Assistant's areas, read; the rooms screens name; one name each ──")
env = surface(ADMIN, "admin")
check("HA areas and the screens' rooms are the rooms, by name", names(env), ["Family Room", "Garage", "Kitchen", "Living room"])
metas = {m["slots"]["title"]: m["slots"]["meta"] for m in env["groups"][0]["members"]}
check("…each says where it came from", (metas["Kitchen"], metas["Living room"]), ("Home Assistant area", "Added here"))
check("a screen's room that's also an HA area is one room (the area)", R.area_of().get("kitchen"), "Kitchen")
again = R.sync(HA, st.device_rooms())
check("listing again adds nothing", len(again), 4)
for bad, why in (("", "empty"), ("x" * 31, "too long"), ("<script>", "not plain words")):
    try:
        R.add(bad)
        got = "added"
    except R.RoomError as exc:
        got = exc.status
    check(f"a name that's {why}: 400", got, 400)
try:
    R.add("  kitchen ")
    got = "added"
except R.RoomError as exc:
    got = exc.status
check("a name another room has (case and spacing ignored): 409", got, 409)

print("── counts never say where a person is ──")
counts = {m["slots"]["title"]: m["slots"]["trailing"]["value"] for m in env["groups"][0]["members"]}
check("Kitchen counts its one household screen, not Robin's iPhone in it", counts["Kitchen"], "1")

print("── add, rename, remove (an adult's), with undo ──")
added = S.room_change("p_robin", "adult", st, "add", name="Study")["room"]
check("an adult adds a room", (added["name"], added["ha_area"]), ("Study", None))
out = S.room_change(ADMIN, "admin", st, "rename", room_id=room_id("Kitchen"), name="Galley")
check("rename: the room, and the household screens that followed (never Robin's iPhone)",
      (out["room"]["name"], out["screens"]), ("Galley", ["Kitchen TV"]))
check("…every device in it follows the room, Robin's iPhone included (just not listed)",
      (room_of("tv-k"), room_of("ph-r")), ("Galley", "Galley"))
check("…the HA area is unchanged, so its sensors are still found", R.area_of().get("galley"), "Kitchen")
check("…and HA doesn't bring 'Kitchen' back as a second room", sorted(r["name"] for r in R.sync(HA, st.device_rooms())),
      ["Family Room", "Galley", "Garage", "Living room", "Study"])
undo = S.undo(ADMIN, out["token"])
check("undo puts the name back, and the screens with it", (undo["room"]["name"], room_of("tv-k"), room_of("ph-r")),
      ("Kitchen", "Kitchen", "Kitchen"))
out = S.room_change(ADMIN, "admin", st, "remove", room_id=room_id("Living room"))
check("remove: the household screens in it lose their room, and are named", (out["screens"], room_of("tv-l")),
      (["Living TV"], None))
gone = S.room_change(ADMIN, "admin", st, "remove", room_id=room_id("Garage"))
check("removing an HA area: it doesn't come back by itself", "Garage" in [r["name"] for r in R.sync(HA, st.device_rooms())],
      False)
check("undo the Living room removal: the room and its screen are back", (S.undo(ADMIN, out["token"])["room"]["name"],
      room_of("tv-l")), ("Living room", "Living room"))
try:
    S.undo("p_robin", gone["token"])
    got = "undone"
except S.Refused as exc:
    got = exc.status
check("an undo token is its owner's only: another adult's undo of it is 404", got, 404)
check("…its owner's: the Garage is back", S.undo(ADMIN, gone["token"])["room"]["name"], "Garage")

print("── who may ──")
kid = surface("p_ava", "kid")
check("a kid sees the rooms", names(kid), names(surface(ADMIN, "admin")))
check("…every operation unavailable, saying why", {(o["exposure"], (o.get("reason") or {}).get("key")) for o in kid["operations"]},
      {("unavailable", "kids.propose_only")})
for role, who in (("kid", "p_ava"), ("household", "screen_k")):
    try:
        S.room_change(who, role, st, "add", name="Den")
        got = "changed"
    except S.Refused as exc:
        got = exc.status
    check(f"{role}: can't change rooms (403)", got, 403)
try:
    surface("screen_k", "household")
    got = "listed"
except S.Refused as exc:
    got = exc.status
check("a shared screen gets no Settings › Rooms (403)", got, 403)
check("the route table: adults change, kids and screens don't; screens don't list",
      (hh.route_allowed("adult", "POST", "/v1/rooms"), hh.route_allowed("kid", "POST", "/v1/rooms"),
       hh.route_allowed("kid", "DELETE", "/v1/rooms/rm-1"), hh.route_allowed("household", "PATCH", "/v1/rooms/rm-1"),
       hh.route_allowed("kid", "GET", "/v1/surfaces/rooms"), hh.route_allowed("household", "GET", "/v1/surfaces/rooms")),
      (True, False, False, False, True, False))

print("── occupancy follows the area, not titan's name ──")
S.room_change(ADMIN, "admin", st, "rename", room_id=room_id("Kitchen"), name="Galley")
found = [{"area": "kitchen", "kind": "presence", "on": True, "available": True}]
room_state._cache.update({"at": 1_791_640_000, "good_at": 1_791_640_000, "sensors": found})
row = room_state.states(["Galley"], now=1_791_640_000, now_local=__import__("datetime").datetime(2026, 10, 10, 12),
                        area_of=R.area_of(), rooms_file=str(_TMP / "none.json"))[0]
check("Galley (HA's Kitchen) reads the Kitchen's presence sensor", row["state"], "occupied")
row = room_state.states(["Galley"], now=1_791_640_000, now_local=__import__("datetime").datetime(2026, 10, 10, 12),
                        rooms_file=str(_TMP / "none.json"))[0]
check("…without the mapping it wouldn't (what #1511 fixes)", row["state"] != "occupied", True)

print("── Home Assistant is never written ──")
src = (HERE / "rooms.py").read_text()
check("rooms.py never calls Home Assistant", ("home_assistant" in src, "requests" in src, "urlopen" in src), (False, False, False))
api = (HERE / "agent_api_server.py").read_text()
blk = api[api.index("def _rooms_synced"):api.index('@app.post("/v1/surfaces/undo")')]
check("the rooms routes only read HA's areas (ha._areas)", ("ha._areas(" in blk, "ha._call" in blk, "POST" in blk.split("@app")[0]),
      (True, False, False))

print("── the store unavailable: hints keep working on HA's areas; Settings says so instead of editing nothing ──")
blk2 = api[api.index("def _rooms_synced"):api.index('@app.get("/v1/surfaces/rooms")')]
check("_rooms_synced falls back to HA's areas as they are (as before #1511)",
      'return [{"id": None, "name": a, "ha_area": a} for a in areas or []]' in blk2, True)
srf = api[api.index('@app.get("/v1/surfaces/rooms")'):api.index("async def _room_change")]
check("…and the Settings surface answers 503 rather than offering operations on rooms with no id",
      ('if any(r.get("id") is None for r in room_list):' in srf, "status_code=503" in srf), (True, True))

print("── golden vectors (fixed rooms, screens and now; the Mac builds its fixtures from these) ──")
VECTORS = HERE / "evalsets" / "surfaces"
WRITE = "--write" in sys.argv
vdb = _TMP / "vector-rooms.db"
ids = iter(f"rm-v{i}" for i in range(1, 100))
real_new_id, R._new_id = R._new_id, lambda: next(ids)
try:
    vrooms = R.sync(["Kitchen", "Family Room"], ["Kitchen", "Playroom"], path=vdb, now=1_791_331_200)
finally:
    R._new_id = real_new_id
vscreens = {"kitchen": 1, "playroom": 1}


def vector(name, got):
    p = VECTORS / f"{name}.json"
    if WRITE:
        p.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    want = json.loads(p.read_text()) if p.exists() else None
    check(f"golden vector {name}", got, want)


vector("rooms.admin", S.rooms_surface("alex", "admin", vrooms, vscreens, 1_791_331_200))
vector("rooms.kid", S.rooms_surface("ava", "kid", vrooms, vscreens, 1_791_331_200))
adm = json.loads((VECTORS / "rooms.admin.json").read_text()) if (VECTORS / "rooms.admin.json").exists() else {"operations": []}
fields = {o["verb"]: o.get("fields") for o in adm["operations"] if o["subject"]["id"] in ("g-rooms", "rm-v1")}
check("add and rename declare their fields (the generic sheet); rename pre-fills the name; remove declares none",
      (fields.get("add"), fields.get("rename"), fields.get("remove")),
      ([{"key": "name", "label": "Name", "kind": "text", "required": True}],
       [{"key": "name", "label": "Name", "kind": "text", "required": True, "value": "Kitchen"}], None))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
