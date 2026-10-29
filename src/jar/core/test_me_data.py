#!/usr/bin/env python3
# gate: hermetic
"""Settings › You (#1079): an adult signs out their own other device, never someone else's (a kid goes through a
parent); GET /v1/me/data counts only the caller's rows; Download everything archives only the caller's own chats,
memories and notes (others' shared items by title only), kept 24 h; kids and screens refused. Two adults export over
the real routes with dev notes present for both (#1690): each archive holds only its owner's dev notes and memories.
Fictional household (Alex, Robin, Jordan, Ava); temporary stores.
Run: python3 scripts/test_me_data.py"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-me-data-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "EXPORT_DIR": "exports", "AGENT_MEMORY_DIR": "memory",
             "ROUTINES_DB": "routines.db", "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json",
             "AGENT_TURN_LOG": "turns.jsonl", "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "SPACES_DB": "spaces.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "WORLD_DIR": "world", "ME_DELETE_DIR": "me-delete", "CLAUDE_CHATS_DB": "claude_chats.db",
             "CAPABILITY_SWITCHES": "switches.json", "HELD_ACTIONS_DB": "held.db", "MCP_SERVERS": "none.json",
             "CHAT_TITLES_DB": "titles.db", "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt",
             "CONTINUITY_DB": "continuity.db", "MEMORY_EXTRACT_LOG": "memory-extract.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import household_identity as hh  # noqa: E402
import me_data as D  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


st = hh.get_store()
robin = st.add_person("Robin Hale", "adult")["id"]
ava = st.add_person("Ava Hale", "kid")["id"]
for did, pid in (("d-robin-mac", robin), ("d-robin-phone", robin), ("d-ava-ipad", ava), ("d-ava-phone", ava)):
    st._db.execute("INSERT INTO devices (id, person_id, name, platform, token_sha256, created_at) VALUES (?,?,?,?,?,?)",
                   (did, pid, did, "ios", "sha-" + did, time.time()))
st._db.commit()


def ident(pid, role, device):
    return hh.Identity(person={"id": pid, "name": pid, "role": role}, device={"id": device, "name": device, "platform": "ios"},
                       role=role, via="device")


print("── signing out a device ──")
R = ident(robin, "adult", "d-robin-mac")
check("an adult signs out their own other device", st.may_revoke(R, "d-robin-phone"), True)
check("…never someone else's", st.may_revoke(R, "d-ava-ipad"), False)
check("the device itself still signs itself out (as today)", st.may_revoke(R, "d-robin-mac"), True)
check("a kid can't sign out their own other device (a parent does)", st.may_revoke(ident(ava, "kid", "d-ava-ipad"), "d-ava-phone"),
      False)
check("an unknown device: no", st.may_revoke(R, "d-nope"), False)

print("── counts: the caller's own ──")
SH = {"notes": {"private": 3, "household": 2, "adults": 0, "admins": 0, "named": 1},
      "facts": {"private": 5, "household": 1, "adults": 0, "admins": 0, "named": 0}, "grants": [{"kind": "scan"}]}
check("memories, chats, notes, standing shares", D.counts(robin, "adult", facts=12, chats=7, sharing=SH),
      {"memories": 12, "chats": 7, "notes": 6, "standing_shares": 5})
check("notes: the ones still in the KB, as the export counts them, when given",
      D.counts(robin, "adult", facts=12, chats=7, sharing=SH, notes=4)["notes"], 4)
try:
    D.counts("house", "household", facts=0, chats=0, sharing=SH)
    check("a shared screen: 403", "counted", 403)
except D.Refused as e:
    check("a shared screen: 403", e.status, 403)

print("── download everything ──")
CHATS = [({"id": "th-1", "title": "Trip to the coast", "started_at": "a", "last_at": "b"},
          [{"role": "user", "text": "Book the coast cabin", "at": "a"}, {"role": "assistant", "text": "Done.", "at": "b"}])]
MEMS = [{"id": "f1", "text": "Robin prefers window seats."}]
NOTES = [{"id": "note-robin-1", "title": "Packing list", "text": "Sunscreen, hats."}]
got = D.build(robin, "adult", chats=lambda: CHATS, memories=lambda: MEMS, notes=lambda: NOTES,
              shared_titles=lambda: ["Recycling days"])
check("built", (got["state"], got["counts"]), ("ready", {"chats": 1, "messages": 2, "memories": 1, "forgotten_memories": 0, "notes": 1}))
zf = zipfile.ZipFile(io.BytesIO(D.path_of(robin, "adult").read_bytes()))
data = json.loads(zf.read("data.json"))
chat = json.loads(zf.read("chats/th-1.json"))
mems = json.loads(zf.read("memories.json"))
check("the caller's own chats, memories and notes",
      (chat["title"], [m["text"] for m in mems["memories"]], [n["title"] for n in data["notes"]]),
      ("Trip to the coast", ["Robin prefers window seats."], ["Packing list"]))
check("nothing capped, and it says so", (data["truncated"], data["counts"]["messages"]), (False, 2))
check("…someone else's shared item: its title only", data["shared_with_you"], [{"title": "Recycling days"}])
check("Markdown beside it", sorted(n for n in zf.namelist() if n.endswith(".md")),
      ["README.md", "chats/th-1.md", "memories.md", "notes/note-robin-1.md"])
BIG = [({"id": f"th-{i}", "title": f"Chat {i}"}, [{"role": "user", "text": f"m{j}", "at": "a"} for j in range(600)])
       for i in range(250)]
big = D.build(robin, "adult", chats=lambda: iter(BIG), memories=lambda: [], notes=lambda: [], shared_titles=lambda: [])
check("250 chats of 600 messages each: every one, every message (no 200/500 caps)",
      (big["counts"]["chats"], big["counts"]["messages"], big["truncated"]), (250, 150000, False))
check("GET reports the counts and truncated", (D.state(robin, "adult")["counts"]["chats"], D.state(robin, "adult")["truncated"]),
      (250, False))
got = D.build(robin, "adult", chats=lambda: CHATS, memories=lambda: MEMS, notes=lambda: NOTES,
              shared_titles=lambda: ["Recycling days"], forgotten=lambda: [{"id": "f0", "text": "Old wifi password note."}])
zf = zipfile.ZipFile(io.BytesIO(D.path_of(robin, "adult").read_bytes()))
check("forgotten memories are in it too (restorable until purged)",
      json.loads(zf.read("memories.json"))["forgotten"][0]["text"], "Old wifi password note.")
f = D.path_of(robin, "adult")
check("private to the person (0600, folder 0700)", (oct(f.stat().st_mode & 0o777), oct(f.parent.stat().st_mode & 0o777)),
      ("0o600", "0o700"))
check("state: ready, with when it goes", D.state(robin, "adult")["state"], "ready")
D.build(robin, "adult", chats=lambda: [], memories=lambda: [], notes=lambda: [], shared_titles=lambda: [])
check("one archive per person: a new one replaces the old", len(list(f.parent.glob("export-*.zip"))), 1)
check("after 24 h it's gone", D.state(robin, "adult", now=time.time() + D.EXPORT_KEEP_S + 60)["state"], "none")
for who, role, code in ((ava, "kid", 403), ("house", "household", 403)):
    try:
        D.build(who, role, chats=lambda: CHATS, memories=lambda: MEMS, notes=lambda: NOTES, shared_titles=lambda: [])
        check(f"{role}: refused", "built", code)
    except D.Refused as e:
        check(f"{role}: refused", (e.status, e.detail if role == "kid" else ""), (code, "ask a parent" if role == "kid" else ""))
check("the routes: adults export, kids may see their counts, screens neither",
      ([hh.route_allowed(r, "POST", "/v1/me/export") for r in ("adult", "kid", "household")],
       [hh.route_allowed(r, "GET", "/v1/me/data") for r in ("adult", "kid", "household")]),
      ([True, False, False], [True, True, False]))

print("── two adults export, dev notes present for both (#1690) ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_memory as am  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
api._lib.kb_sources = lambda *a, **k: []      # no KB notes in this check: memories and dev notes only
mem = am.AgentMemory()
api._get_memory = lambda: mem
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(body):
    s_ = c.post("/v1/pair/start", json=body, headers=M).json()
    r = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": "laptop", "platform": "mac"}).json()
    return {"X-Agent-Token": r["token"]}


RH = pair({"person_id": robin})
JH = pair({"new_person": {"name": "Jordan Hale", "role": "adult"}})
jordan = next(p["id"] for p in st.people() if p["name"] == "Jordan Hale")
OWN = {robin: ("You keep the spare key under the blue pot.", ["Robin wants the app to track chores.",
                                                               "Robin wants titan to read the school portal."]),
       jordan: ("You take the 7:40 bus on weekdays.", ["Jordan wants titan to text when the bus is late."]),
       hh.ADMIN_PERSON_ID: ("You water the plants on Sundays.", ["Alex wants a dark theme on the TV."])}
for pid, (fact, dev) in OWN.items():
    mem.add_fact("c", fact, person_id=pid)
    for n in dev:
        mem.add_dev_note("c", n, person_id=pid)


def export(h):
    r = c.post("/v1/me/export", headers=h)
    z = zipfile.ZipFile(io.BytesIO(c.get("/v1/me/export?download=1", headers=h).content))
    m = json.loads(z.read("memories.json"))
    return r.status_code, sorted(x["text"] for x in m["memories"]), sorted(x["text"] for x in m.get("dev_notes") or [])


for name, h, pid in (("Robin", RH, robin), ("Jordan", JH, jordan)):
    check(f"{name}'s export: built, their own memory and every one of their own dev notes, nobody else's",
          export(h), (201, [OWN[pid][0]], sorted(OWN[pid][1])))
check("…both people's dev notes are still there (an export copies, never moves)",
      [len(mem.dev_notes(person_id=p)) for p in (robin, jordan, hh.ADMIN_PERSON_ID)], [2, 1, 1])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
