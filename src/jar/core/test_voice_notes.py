#!/usr/bin/env python3
# gate: hermetic
"""Voice notes to the household (jellybean#313): an .m4a/.aac upload kept a week, at most a minute, POST
/v1/voice-notes, a card with Play for everyone it's for (the shared household devices too), the audio served to them
(and nobody else) and never to a turn's tools, a transcript only when the sender sent one, kids sending while the
household allows it, never from a shared household device. Throwaway stores; made-up people.
Run: python3 scripts/test_voice_notes.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-voice-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "TASK_LOG_DB": "task_log.db", "UPLOADS_DB": "uploads.db", "UPLOADS_DIR": "uploads"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import uploads  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom\x00\x00\x00\x08free" + b"\x00" * 64

print("── the audio type ──")
check("an .m4a is a voice note's type", uploads.kind_of("note.m4a", M4A[:64]), "audio/mp4")
check("an .aac (ADTS) too", uploads.kind_of("note.aac", b"\xff\xf1\x50\x80" + b"\x00" * 60), "audio/aac")
check("an .m4a that isn't one is refused", uploads.kind_of("note.m4a", b"%PDF-1.4" + b"\x00" * 56), None)
check("a voice note is kept a week, other uploads 72 h", (uploads.ttl("audio/mp4"), uploads.ttl("application/pdf")),
      (7 * 86400, 72 * 3600))
code, big = uploads.save("p_alex", "long.m4a", M4A + b"\x00" * uploads.VOICE_MAX_BYTES)
check("longer than a minute's worth of bytes: 413", code, 413)

print("── the route ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": "ios"}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Hale", "adult", "Alex's iPhone")
ROBIN, robin = pair("Robin Hale", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
KIT, kit = pair("Kitchen", "household", "Kitchen iPad")
admin = next(str(p["id"]) for p in st.people() if p.get("role") == "admin")   # the store's own admin


def upload(h, name="note.m4a", data=M4A):
    r = c.post(f"/v1/files?name={name}", content=data, headers=h)
    return r.status_code, r.json()


code, up = upload(ALEX)
check("Alex uploads a voice note, kept a week", (code, up.get("type"), up["expires_at"] - uploads.record(up["id"])["ts"] > 6 * 86400),
      (201, "audio/mp4", True))
r = c.post("/v1/voice-notes", json={"file_id": up["id"], "to": "household", "seconds": 12}, headers=ALEX)
check("sent to the household (everyone but the sender, the shared devices too)", (r.status_code, sorted(r.json().get("to", []))),
      (201, sorted([admin, robin, ava, kit])))
cards = [x for x in proactive.list_cards() if x["id"] == r.json().get("card_id")]
card = cards[0] if cards else {}
check("one card with Play's data, no transcript", (card.get("title"), card.get("detail"), card.get("data"), card.get("urgency")),
      ("Voice note from Alex", "", {"kind": "voice_note", "file_id": up["id"], "seconds": 12}, 2))
check("for the whole household", card.get("to"), "household")
check("the Kitchen iPad (and the TV) show it", proactive.visible_to(card.get("to"), kit, "household"), True)
check("it retires with the audio, in about a week", 6 * 86400 < (card.get("expires_at") or 0) - card.get("created_at", 0) <= 7 * 86400 + 5,
      True)
check("the Kitchen iPad can play it", c.get(f"/v1/files/uploads/{up['id']}", headers=KIT).status_code, 200)
path = uploads.record(up["id"])["path"]
check("a turn's tools never read it, even for Robin", uploads.may_read(path, robin), False)
check("Robin can play it", c.get(f"/v1/files/uploads/{up['id']}", headers=ROBIN).status_code, 200)
check("Alex (its sender) too", c.get(f"/v1/files/uploads/{up['id']}", headers=ALEX).status_code, 200)

code, priv = upload(ALEX)
r = c.post("/v1/voice-notes", json={"file_id": priv["id"], "to": "adults", "seconds": 5, "transcript": "Dinner  is at 6"},
           headers=ALEX)
check("to the grown-ups: no kid", sorted(r.json().get("to")), sorted([admin, robin]))
check("…and the card says adults", [x for x in proactive.list_cards() if x["id"] == r.json()["card_id"]][0]["to"], "adults")
check("Ava can't fetch the grown-ups' note", c.get(f"/v1/files/uploads/{priv['id']}", headers=AVA).status_code, 404)
card2 = [x for x in proactive.list_cards() if x["id"] == r.json().get("card_id")][0]
check("the transcript only because Alex sent one", card2["detail"], "Dinner is at 6")

code, long = upload(ALEX)
check("over 60 seconds: refused", c.post("/v1/voice-notes", json={"file_id": long["id"], "to": "household", "seconds": 61},
                                          headers=ALEX).status_code, 400)
r = c.post("/v1/voice-notes", json={"file_id": long["id"], "to": [robin], "seconds": 3}, headers=ALEX)
check("to one person", (r.status_code, r.json().get("to")), (201, [robin]))
check("…who can play it; Ava can't", (c.get(f"/v1/files/uploads/{long['id']}", headers=ROBIN).status_code,
                                      c.get(f"/v1/files/uploads/{long['id']}", headers=AVA).status_code), (200, 404))

print("── the week ──")
t0 = uploads.record(up["id"])["ts"]
_, doc = uploads.save("p_alex", "list.txt", b"milk", now=t0)
uploads.sweep(t0 + 4 * 86400)
check("four days on: the voice note stays, a 72 h upload is gone",
      (uploads.record(up["id"]) is not None, uploads.record(doc["id"]) is None), (True, True))
check("eight days on: gone too", uploads.for_viewer(up["id"], robin, t0 + 8 * 86400), None)

print("── who may send ──")
g = c.get("/v1/household/voice-notes", headers=AVA).json()
check("kids may send by default", g["kids_may_send"], True)
check("who Ava can pick: everyone but her and the Kitchen iPad", sorted(x["id"] for x in g["people"]), sorted([admin, alex, robin]))
check("…with names and roles", next(x for x in g["people"] if x["id"] == robin), {"id": robin, "name": "Robin Hale", "role": "adult"})
code, kid = upload(AVA)
check("a kid to the whole household: fine", c.post("/v1/voice-notes", json={"file_id": kid["id"], "to": "household"},
                                                     headers=AVA).status_code, 201)
check("a kid can't change the setting", c.patch("/v1/household/voice-notes", json={"kids_may_send": False},
                                                headers=AVA).status_code, 403)
check("an adult turns kids' notes off", c.patch("/v1/household/voice-notes", json={"kids_may_send": False},
                                                headers=ROBIN).json(), {"kids_may_send": False})
code, kid2 = upload(AVA)
check("then a kid's note is refused", c.post("/v1/voice-notes", json={"file_id": kid2["id"], "to": "adults"},
                                             headers=AVA).status_code, 403)
check("a bad setting body: 400", c.patch("/v1/household/voice-notes", json={"kids_may_send": "no"},
                                         headers=ROBIN).status_code, 400)
check("…and back on", c.patch("/v1/household/voice-notes", json={"kids_may_send": True}, headers=ROBIN).json(),
      {"kids_may_send": True})
check("the setting lives in household.json", json.loads(Path(os.environ["HOUSEHOLD_PREFS"]).read_text())["kids_send_voice_notes"],
      True)
check("a shared household device: refused", c.post("/v1/voice-notes", json={"file_id": up["id"], "to": "household"},
                                                     headers=KIT).status_code, 403)
check("someone else's upload: refused", c.post("/v1/voice-notes", json={"file_id": up["id"], "to": "household"},
                                                 headers=ROBIN).status_code, 400)
code, txt = upload(ALEX, "list.txt", b"milk")
check("not audio: refused", c.post("/v1/voice-notes", json={"file_id": txt["id"], "to": "household"},
                                     headers=ALEX).status_code, 400)
check("an unknown person: refused", c.post("/v1/voice-notes", json={"file_id": up["id"], "to": ["p_nobody"]},
                                             headers=ALEX).status_code, 400)

print("── Keep: a recipient keeps the words, privately (titan-node#859) ──")
import agent_runtime_lib as lib  # noqa: E402
import voice_notes  # noqa: E402
kept: list = []
lib._kb_index = lambda sid, title, text, st, md=None, owner=None, audience=None, pin=None: kept.append(
    {"id": sid, "title": title, "text": text, "st": st, "md": md, "owner": owner, "audience": audience}) or {}
check("a note without words has no Keep", card.get("actions"), [])
code, kn = upload(ALEX, "keep.m4a")
r = c.post("/v1/voice-notes", json={"file_id": kn["id"], "to": [robin, ava], "seconds": 4,
                                    "transcript": "Pick up the cake from Leo's bakery at 5"}, headers=ALEX)
kid_ = r.json()["card_id"]
kcard = next(x for x in proactive.list_cards() if x["id"] == kid_)
check("a note with words has one Keep button", [(a["id"], a["label"], a["kind"]) for a in kcard["actions"]], [("keep", "Keep", "reply")])
check("nothing is kept until someone presses it", kept, [])
out = c.post(f"/v1/proactive/{kid_}/action", json={"action_id": "keep"}, headers=ROBIN).json()
check("Robin keeps it", (out.get("ok"), out.get("said")),
      (True, "Kept: the words of Alex's voice note are in your knowledge base, just for you."))
k = kept[-1] if kept else {}
check("…into Robin's own knowledge base, private, as a voice note", (k.get("id"), k.get("owner"), k.get("audience"), k.get("st")),
      (voice_notes.keep_id(kid_, robin), robin, "private", "voice-note"))
check("…the words and who sent it; never the recording", ("Pick up the cake from Leo's bakery at 5" in k.get("text", ""),
      k.get("title", "").startswith("Voice note from Alex, "), kn["id"] in json.dumps(k), k.get("md", {}).get("sender")),
      (True, True, False, "Alex"))
check("the card stays (Play still works until it expires)", any(x["id"] == kid_ and x.get("acked_at") is None
                                                                 for x in proactive.list_cards(include_acked=True, person=robin, role="adult")), True)
c.post(f"/v1/proactive/{kid_}/action", json={"action_id": "keep"}, headers=ROBIN)
check("Keep twice: the same one item", (len(kept), kept[-1]["id"]), (2, kept[0]["id"]))
n = len(kept)
out = c.post(f"/v1/proactive/{kid_}/action", json={"action_id": "keep"}, headers=AVA).json()
check("a kid can't keep it (kids don't write the knowledge base)", (out.get("ok"), out.get("said"), len(kept)),
      (False, "Keeping voice notes is for adults on their own devices.", n))
out = c.post(f"/v1/proactive/{kid_}/action", json={"action_id": "keep"}, headers=ALEX).json()
check("the sender never had the card, so can't keep it here", (out.get("ok"), len(kept)), (False, n))
check("Robin's and someone else's kept copy would be separate items", voice_notes.keep_id(kid_, robin) != voice_notes.keep_id(kid_, alex), True)
check("a card without words: nothing to keep", voice_notes.keep({"id": "x", "detail": "  ", "sender": "Alex"}, robin, "adult", index=kept.append),
      (False, "This voice note has no words to keep, only the recording."))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
