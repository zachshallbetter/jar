#!/usr/bin/env python3
"""Privacy, Track B (plan item 9): each person's timeline, records, todos, routines and dev activity
stay theirs (scripts/audience.py), sharing takes one step, and anything from before owners counts as
Zach's and private. Hermetic: temp stores; the event and record runtimes are an in-memory fake.

  timeline     log_event / Mac actions / connector lines are the person's (private); trigger notes and
               dev events titan's own (admins); an explicit household event everyone's
  records      provenance owner on every node; find_records, update_record, kb_relations filtered
  dev          dev activity in context and recall_events: the admin only, whatever the words
  todos        list_todos / complete_todo for someone else's list are refused
  coordination a person's own overlaps go to them only; others' events are "busy"
  letter       Zach's own timeline items and cards for him only
  routines     owner + audience; edit/delete by owner, admin or a household device
  small        services-down line admin only; per-device "earlier" bucket; card replies to the replier

Run: python3 scripts/test_privacy_timeline.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-privacy-tl-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha-token")
os.environ.setdefault("AGENT_TZ", "America/Los_Angeles")
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ["AGENT_TURN_SOURCE"] = "user"   # log_event skips test turns; these are fake "real" ones
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import audience  # noqa: E402
import connectors_runner as cx  # noqa: E402
import household_coordination as hc  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive as pa  # noqa: E402
import reflection as rf  # noqa: E402
import routines as rt  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")
    return good


# ---------------------------------------------------------------------------- the fake runtimes

class Fake:
    """The event runtime (streams) and the record runtime (nodes, edges) in memory."""

    def __init__(self):
        self.streams: dict[str, list] = {}
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.kb: dict[str, dict] = {}
        self.n = 0

    def __call__(self, svc, path, method="POST", payload=None, timeout=30):
        self.n += 1
        if svc == "event" and method == "POST" and path == "/v1/events":
            ev = {"event_id": payload.get("event_id") or f"ev{self.n}", "event_type": payload["event_type"],
                  "payload": payload["payload"], "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self.streams.setdefault(payload["stream_key"], []).append(ev)
            return {}
        if svc == "event" and path.startswith("/v1/events/streams/"):
            return {"events": list(self.streams.get(path.rsplit("/", 1)[1].split("?")[0], []))}
        if svc == "retrieval":
            if method == "POST" and path == "/v1/retrieval/index":
                self.kb[payload["source_id"]] = payload
            return {}
        if svc == "record":
            if path == "/v1/nodes" and method == "POST":
                if payload["node_id"] in self.nodes:
                    raise RuntimeError("HTTP Error 409: Conflict")
                self.nodes[payload["node_id"]] = {"node_id": payload["node_id"], "node_type": payload["node_type"],
                                                  "data": json.loads(json.dumps(payload["data"]))}
                return {"status": "ok"}
            if path.startswith("/v1/nodes?") and method == "GET":
                q = dict(x.split("=", 1) for x in path.split("?", 1)[1].split("&"))
                return {"nodes": [n for n in self.nodes.values() if not q.get("node_type") or n["node_type"] == q["node_type"]]}
            if path.startswith("/v1/nodes/"):
                nid = urllib.parse.unquote(path.rsplit("/", 1)[1])
                if nid not in self.nodes:
                    raise RuntimeError("HTTP Error 404: Not Found")
                if method == "GET":
                    return json.loads(json.dumps(self.nodes[nid]))
                if method == "PATCH":
                    self.nodes[nid]["data"].update(payload["data"])
                    return {"status": "ok"}
            if path == "/v1/edges" and method == "POST":
                self.edges.append({"source_id": payload["source_id"], "target_id": payload["target_id"],
                                   "relation_type": payload["relation_type"]})
                return {"status": "ok"}
            if path.startswith("/v1/edges/trace/"):
                nid = path.rsplit("/", 1)[1]
                if nid not in self.nodes:
                    raise urllib.error.HTTPError(path, 404, "not anchored", {}, None)
                es = [e for e in self.edges if nid in (e["source_id"], e["target_id"])]
                ids = {nid} | {e["source_id"] for e in es} | {e["target_id"] for e in es}
                return {"edges": es, "nodes": [self.nodes[i] for i in ids if i in self.nodes]}
        return {}


import urllib.parse  # noqa: E402

fake = Fake()
lib._runtime_request = fake


def _all_down(url, timeout=None, **kw):
    raise RuntimeError("down")


lib._http_json = _all_down   # every runtime "down": the services line shows up (for the admin)

store = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(store)
MIR = store.add_person("Robin", "adult")["id"]
KID = store.add_person("Leo", "kid")["id"]
HOUSE = store.add_person("Kitchen", "household")["id"]
T = lib.execute_tool


def as_(role, pid=None, name=None, device=None):
    lib._TURN_IO.role = role
    lib._TURN_IO.person = {"id": pid, "name": name} if pid else None
    lib._TURN_IO.device_id = device


def ctx(query=None):
    return lib._situational_context_inner(query)


# ---------------------------------------------------------------------------- 1. timeline

print("── 1. timeline: who wrote it, who sees it ──")
as_("admin", "zach", "Zach")
T("log_event", {"event": "Changed the furnace filter"})
lib._log_mac_action("send_message", {"to": "x", "body": "y"}, "Sent.")
as_("adult", MIR, "Robin")
T("log_event", {"event": "Booked the dentist for Thursday"})
T("log_event", {"event": "Plumber fixed the kitchen sink", "for": "household"})
pa._timeline("Trigger looked at a delivery note")                     # titan's own: admins
pa._timeline("Reviewed commit abc123", lib.DEV_STREAM)                 # dev: admins
# Calendar lines the connector posted before #636 (it posts none now): each the pushing person's, private, and
# placed at the occurrence in its upstream id, never its sync time.
_now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
for _sid, _who, _line in (("z1", "zach", "Zach's calendar: Board meeting"), ("m1", MIR, "Robin's calendar: Yoga class")):
    fake.streams["household-timeline"].append({
        "event_id": f"cx-calendar-{_sid}-t", "event_type": "household.note", "timestamp": _now_iso,
        "payload": {"text": _line, "source": "calendar", "kind": "observed", "upstream_id": f"{_sid}@{_now_iso}",
                    "person": _who, "owner": _who, "audience": "private"}})
fake.streams["household-timeline"].append({"event_id": "legacy1", "event_type": "household.note",
                                           "payload": {"text": "Legacy entry from before owners"},
                                           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
fake.streams["household-timeline"].append({"event_id": "shared1", "event_type": "household.note",
                                           "payload": {"text": "Shared: anniversary dinner booked", "person": "zach",
                                                       "audience": audience.widen("private", [MIR])},
                                           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
pl = {e["payload"]["text"]: e["payload"] for e in fake.streams["household-timeline"]}
check("log_event: the person's, private", (pl["Changed the furnace filter"]["person"], pl["Changed the furnace filter"]["audience"]),
      ("zach", "private"))
check("a logged Mac action: the turn's person, private", (pl["Sent a message"]["person"], pl["Sent a message"]["audience"]),
      ("zach", "private"))
check("an explicit household event", (pl["Plumber fixed the kitchen sink"]["person"], pl["Plumber fixed the kitchen sink"]["audience"]),
      (MIR, "household"))
check("a trigger note: admins", pl["Trigger looked at a delivery note"]["audience"], "admins")
check("a dev event: admins", fake.streams[lib.DEV_STREAM][0]["payload"]["audience"], "admins")
check("a connector line: the pushing person, private",
      (pl["Robin's calendar: Yoga class"]["person"], pl["Robin's calendar: Yoga class"]["audience"]), (MIR, "private"))

ZACH_ONLY = ("furnace filter", "Sent a message", "Trigger looked", "Board meeting", "Legacy entry")
as_("adult", MIR, "Robin")
c = ctx("what happened today?")
check("Robin's context: her own, the household's, what's shared with her",
      [x in c for x in ("Booked the dentist", "Plumber fixed", "anniversary dinner")], [True] * 3)
# Calendar sync lines stay on the timeline but out of the per-turn context (09-30: 236 of 253 entries
# were sync lines); who sees them is checked through recall_events below.
check("calendar sync lines aren't in the per-turn context", "Yoga class" in c, False)
check("… none of Zach's (events, Mac actions, trigger notes, his connector lines, legacy)",
      [x in c for x in ZACH_ONLY], [False] * 5)
r = T("recall_events", {})
check("Robin's recall_events: the same rule", ([x in r for x in ZACH_ONLY], "Booked the dentist" in r), ([False] * 5, True))
check("… her calendar line is there, Zach's isn't", ("Yoga class" in r, "Board meeting" in r), (True, False))
as_("admin", "zach", "Zach")
c = ctx("what happened today?")
ZACH_CTX = tuple(x for x in ZACH_ONLY if x != "Board meeting")   # his calendar sync line: recall_events only
check("Zach's context: his own, titan's, the household's, legacy", [x in c for x in ZACH_CTX + ("Plumber fixed",)], [True] * 5)
rz = T("recall_events", {})
check("Zach's recall_events: his calendar line, not Robin's", ("Board meeting" in rz, "Yoga class" in rz), (True, False))
check("… not Robin's private ones", ("Booked the dentist" in c, "Yoga class" in c), (False, False))
as_("kid", KID, "Leo")
c = ctx("what happened today?")
check("a kid sees household events only",
      ("Plumber fixed" in c, any(x in c for x in ZACH_ONLY + ("Booked the dentist", "Yoga", "anniversary"))), (True, False))
as_("household", HOUSE, "Kitchen", device="d-kitchen")
c = ctx("anything new?")
check("a shared device: household only", ("Plumber fixed" in c, "Booked the dentist" in c, "furnace" in c), (True, False, False))

print("\n── 1b. the daily log, per person ──")
day = lib._local_now().strftime("%Y-%m-%d")
daily = Path(os.environ["DAILY_LOG_DIR"])
zfile, mfile = lib.DAILY_DIR / f"{day}.md", lib.DAILY_DIR / MIR / f"{day}.md"
check("Zach's daily file holds only his", (zfile.exists(), "dentist" in zfile.read_text(), "furnace" in zfile.read_text()),
      (True, False, True))
check("Robin's has its own file", (mfile.exists(), "dentist" in mfile.read_text(), "furnace" in mfile.read_text()),
      (True, True, False))
notes = {k: v for k, v in fake.kb.items() if v["metadata"].get("source") == "daily"}
owners = sorted((v["metadata"]["owner"], v["metadata"]["audience"]) for v in notes.values())
check("no KB note for anyone's day (10-03: the daily log is titan's own activity; the files stay per person)", owners, [])

# ---------------------------------------------------------------------------- 2. records

print("\n── 2. records: an owner on every node ──")
fake.nodes["bill-acme-legacy"] = {"node_id": "bill-acme-legacy", "node_type": "bill",
                                  "data": {"vendor": "ACME Legacy", "amount": 12}}
as_("admin", "zach", "Zach")
T("save_contact", {"name": "Dr Patel", "phone": "555-0101", "relation": "dentist"})
check("Zach's contact: his, private", fake.nodes["contact-dr-patel"]["data"]["provenance"]["owner"], "zach")
check("Zach sees the legacy bill", "ACME Legacy" in T("find_records", {"type": "bill"}), True)
as_("adult", MIR, "Robin")
check("Robin: no legacy bill", T("find_records", {"type": "bill"}), "No matching records found.")
check("Robin: not Zach's contact", T("find_records", {"type": "contact"}), "No matching records found.")
check("Robin can't edit Zach's contact (it isn't there for her)",
      T("update_record", {"record_id": "contact-dr-patel", "updates": "phone=555-9999"}),
      "No record found with id contact-dr-patel.")
check("… and it's unchanged", fake.nodes["contact-dr-patel"]["data"]["phone"], "555-0101")
out = T("save_contact", {"name": "Dr Patel", "phone": "555-0202", "relation": "dentist"})
check("her own Dr Patel is a separate record", (f"contact-dr-patel-{MIR}" in out, fake.nodes["contact-dr-patel"]["data"]["phone"]),
      (True, "555-0101"))
check("… hers, private", fake.nodes[f"contact-dr-patel-{MIR}"]["data"]["provenance"], {"source": "chat", "owner": MIR, "audience": "private"})
check("the shared 'dentist' hub now includes her (she named it)", fake.nodes["dentist"]["data"]["provenance"]["audience"],
      json.dumps([MIR]))
r = T("kb_relations", {"entity": "dentist"})
check("Robin's graph walk: her contact, not Zach's", (f"contact-dr-patel-{MIR}" in r, "contact-dr-patel —" in r), (True, False))
check("update_record can't change the owner", (T("update_record", {"record_id": f"contact-dr-patel-{MIR}", "updates": "owner=zach, notes=hi"}),
      fake.nodes[f"contact-dr-patel-{MIR}"]["data"]["provenance"]["owner"]), (f"Updated contact-dr-patel-{MIR}: notes=hi", MIR))
T("save_contact", {"name": "Joe Pipes", "relation": "plumber", "for": "household"})
check("a household contact", fake.nodes["contact-joe-pipes"]["data"]["provenance"]["audience"], "household")
as_("admin", "zach", "Zach")
r = T("kb_relations", {"entity": "dentist"})
check("Zach's walk: his contact, not Robin's private one", ("contact-dr-patel —" in r, MIR in r), (True, False))
check("Zach sees the household contact", "Joe Pipes" in T("find_records", {"type": "contact"}), True)
as_("kid", KID, "Leo")
check("a kid's walk: household only", ("contact-joe-pipes" in T("kb_relations", {"entity": "plumber"}),
                                       "contact-dr-patel" in T("kb_relations", {"entity": "dentist"})), (True, False))

print("\n── 2b. sharing a record: one step ──")
as_("adult", MIR, "Robin")
check("Robin can't share Zach's", T("update_record", {"record_id": "contact-dr-patel", "updates": "share=household"}),
      "No record found with id contact-dr-patel.")
as_("admin", "zach", "Zach")
check("Zach shares his with Robin", T("update_record", {"record_id": "contact-dr-patel", "updates": "share=Robin"}),
      "Updated contact-dr-patel: shared with=Robin")
check("… audience widened, owner kept", fake.nodes["contact-dr-patel"]["data"]["provenance"],
      {"source": "chat", "owner": "zach", "audience": json.dumps([MIR])})
as_("adult", MIR, "Robin")
check("Robin now sees it (hers, the household's, his)", T("find_records", {"type": "contact"}).split(":")[0], "Found 3 record(s)")
check("… but can't re-share it", T("update_record", {"record_id": "contact-dr-patel", "updates": "share=household"}),
      "Not shared: only the person who saved contact-dr-patel can share it.")
as_("admin", "zach", "Zach")
T("update_record", {"record_id": "bill-acme-legacy", "updates": "share=household"})
check("sharing a legacy record makes Zach its owner first",
      fake.nodes["bill-acme-legacy"]["data"]["provenance"], {"owner": "zach", "audience": "household"})
as_("adult", MIR, "Robin")
check("… and the household sees it", "ACME Legacy" in T("find_records", {"type": "bill"}), True)
cx.ingest("mail", [{"source_id": "mm1", "title": "Her mail", "fields": {"subject": "x"}, "links": [["from", "School"]]}],
          runtime=fake, ledger=_TMP / "ledger.db", owner=MIR)
check("a connector node: the pushing person's", fake.nodes[cx.item_id("mail", f"{MIR}:mm1")]["data"]["provenance"]["owner"], MIR)
check("… its linked entity too", fake.nodes["school"]["data"]["provenance"]["owner"], MIR)

# ---------------------------------------------------------------------------- 3. dev activity

print("\n── 2c. editing a contact never makes a second one (10-03 audit, #802's shape) ──")
as_("adult", MIR, "Robin")
T("update_record", {"record_id": "contact-joe-pipes", "updates": "phone=555-0303"})
n0 = len(fake.nodes)
out = T("save_contact", {"name": "Jo Pipes", "phone": "555-0404"})
check("a near-miss name: no new contact, and it names the one it meant",
      (out.startswith("Didn't save: “Joe Pipes” already exists"), len(fake.nodes)), (True, n0))
out = T("save_contact", {"name": "Pipes", "phone": "555-0404"})
check("…a surname alone too", out.startswith("Didn't save: “Joe Pipes”"), True)
out = T("save_contact", {"change": "joe pipes", "phone": "555-0404"})
jp = fake.nodes["contact-joe-pipes"]["data"]
check("change updates that contact in place, keeping its relation and its household audience",
      (out.startswith("Updated contact: Joe Pipes"), jp["phone"], jp["provenance"]["audience"], len(fake.nodes)),
      (True, "555-0404", "household", n0))
T("save_contact", {"change": "Joe Pipes", "name": "Joe Pipes Plumbing"})
check("change with a name renames it, same record, same number",
      (fake.nodes["contact-joe-pipes"]["data"]["name"], fake.nodes["contact-joe-pipes"]["data"]["phone"]),
      ("Joe Pipes Plumbing", "555-0404"))
T("save_contact", {"change": "Dr Patel", "phone": "555-0707"})
check("Zach's Dr Patel shared with her and her own: change edits hers",
      (fake.nodes[f"contact-dr-patel-{MIR}"]["data"]["phone"], fake.nodes["contact-dr-patel"]["data"]["phone"]),
      ("555-0707", "555-0101"))
T("save_contact", {"name": "Dr Patel", "email": "patel@example.com"})
check("…and so does the same name without change", ("email" in fake.nodes[f"contact-dr-patel-{MIR}"]["data"],
      "email" in fake.nodes["contact-dr-patel"]["data"]), (True, False))
n1 = len(fake.nodes)
check("change to a contact she doesn't have says so, and makes nothing",
      (T("save_contact", {"change": "Dr Hale", "phone": "555-0000"}).startswith(
          "Couldn't save the contact: there's no contact called “Dr Hale”"), len(fake.nodes)), (True, n1))
out = T("save_contact", {"name": "Pipes", "phone": "555-0505", "new": "yes"})
check("new='yes' makes a separate one, private to the asker", (out.startswith("Saved contact: Pipes"),
      fake.nodes["contact-pipes"]["data"]["provenance"]["audience"]), (True, "private"))
check("a different person isn't near", T("save_contact", {"name": "Ava Stone", "phone": "555-0606"}).startswith("Saved contact"),
      True)
as_("admin", "zach", "Zach")

print("\n── 3. dev activity: the admin only ──")
as_("adult", MIR, "Robin")
c = ctx("the build failed, can you check the tests and the titan runtime code?")
check("Robin never gets dev activity, whatever the words", ("Recent dev activity" in c, "abc123" in c), (False, False))
check("recall_events which=dev refused for her", "Reviewed commit" in T("recall_events", {"which": "dev"}), False)
as_("admin", "zach", "Zach")
c = ctx("the build failed, can you check the tests?")
check("Zach gets it on a code question", "Reviewed commit abc123" in c, True)
check("… and recall_events which=dev", "Reviewed commit abc123" in T("recall_events", {"which": "dev"}), True)

# ---------------------------------------------------------------------------- 4. todos

print("\n── 4. todos in chat ──")
as_("admin", "zach", "Zach")
T("add_todo", {"title": "Renew the car registration"})
check("adding to Robin's list stays allowed", T("add_todo", {"title": "Call the school", "for": "Robin"}),
      "Added “Call the school” to Robin's list.")
check("Zach can't list hers", T("list_todos", {"for": "Robin"}).startswith("Not done: Robin's list is theirs"), True)
check("… or tick hers off", T("complete_todo", {"todo": "school", "for": "Robin"}).startswith("Not done:"), True)
as_("adult", MIR, "Robin")
check("Robin can't list Zach's", T("list_todos", {"for": "Zach"}).startswith("Not done:"), True)
check("… or complete his", T("complete_todo", {"todo": "registration", "for": "Zach"}).startswith("Not done:"), True)
check("her own list works", "Call the school" in T("list_todos", {"for": "me"}), True)
as_("admin", None)   # the CLI: Zach, no person
check("a CLI turn still lists Zach's own", "Renew the car registration" in T("list_todos", {"for": "me"}), True)

# ---------------------------------------------------------------------------- 5. coordination

print("\n── 5. coordination ──")
PEOPLE = [{"id": "zach", "name": "Zach", "role": "admin", "age": None, "calendars": []},
          {"id": MIR, "name": "Robin", "role": "adult", "age": None, "calendars": ["rob@example.com"]},
          {"id": KID, "name": "Leo", "role": "kid", "age": 6, "calendars": []}]
NODES = [{"node_id": "a", "data": {"title": "Therapy", "start": "2026-10-07T21:00:00Z", "end": "2026-10-07T22:00:00Z",
                                   "calendar": "rob@example.com", "calendar_owner": "mine"}},
         {"node_id": "b", "data": {"title": "Pilates", "start": "2026-10-07T21:30:00Z", "end": "2026-10-07T22:30:00Z",
                                   "calendar": "rob@example.com", "calendar_owner": "mine"}},
         {"node_id": "c", "data": {"title": "Leo swim", "start": "2026-10-08T01:00:00Z", "end": "2026-10-08T02:00:00Z",
                                   "calendar": "Leo", "calendar_owner": "mine"}},
         {"node_id": "d", "data": {"title": "Investor call", "start": "2026-10-08T00:45:00Z", "end": "2026-10-08T02:00:00Z",
                                   "calendar": "Zach", "calendar_owner": "mine"}},
         {"node_id": "e", "data": {"title": "Book club", "start": "2026-10-08T00:30:00Z", "end": "2026-10-08T02:00:00Z",
                                   "calendar": "rob@example.com", "calendar_owner": "mine",
                                   "provenance": {"owner": MIR, "audience": "adults"}}}]
sent = []
hc.run(date(2026, 10, 7), nodes=NODES, people=PEOPLE, send=lambda t, d, **kw: sent.append((t, d, kw)) or {"id": "c"})
to = {json.dumps(kw["to"]): d for _, d, kw in sent}
check("Robin's own overlap goes to her only", ("Therapy" in to.get(json.dumps([MIR]), ""), any("Therapy" in d for k, d in to.items() if k != json.dumps([MIR]))),
      (True, False))
check("the driver card names a private event as busy, a shared one by title",
      ("Zach is busy" in to.get('"adults"', ""), "Investor call" in to.get('"adults"', ""), "Book club" in to.get('"adults"', "")),
      (True, False, True))
check("every card keyed by the day", {kw["key"] for _, _, kw in sent}, {"coordination-2026-10-07"})

# ---------------------------------------------------------------------------- 6. the nightly letter

print("\n── 6. the nightly letter: Zach's ──")
pdb = _TMP / "letter-proactive.db"
pa.add_card("Your dentist moved", "x", db=pdb, to=[MIR])
pa.add_card("Disk is filling up", "x", db=pdb)
pa.add_card("Dinner at 6", "x", db=pdb, to="household")
con = pa._db(pdb)
con.execute("INSERT INTO triggers (id, source, kind, summary, dedupe_key, received_at, outcome, person) "
            "VALUES ('t1','card-reply','reply','Robin answered Yes to her card','k1',?, 'reported', ?)", (time.time(), MIR))
con.execute("INSERT INTO triggers (id, source, kind, summary, dedupe_key, received_at, outcome, person) "
            "VALUES ('t2','card-reply','reply','Zach answered Later','k2',?, 'reported', 'zach')", (time.time(),))
con.commit()
con.close()
g = rf.gather(timeline=lib._timeline_events, proactive_db=pdb, ledger=_TMP / "none.db", memory_db=_TMP / "none.db")
titles = " | ".join(i["title"] for i in g["items"])
check("Zach's items: his own, the household's, titan's", [x in titles for x in ("furnace filter", "Plumber fixed", "Disk is filling", "Dinner at 6", "Zach answered Later")],
      [True] * 5)
check("… none of Robin's (events, her card, her reply)",
      [x in titles for x in ("Booked the dentist", "Yoga", "Your dentist moved", "Robin answered")], [False] * 4)

# ---------------------------------------------------------------------------- 7. routines

print("\n── 7. routines ──")
rdb = _TMP / "routines-test.db"
con = sqlite3.connect(rdb)
con.execute("""CREATE TABLE routines (id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT, steps TEXT NOT NULL, schedule TEXT,
        enabled INTEGER NOT NULL DEFAULT 1, created_at REAL, updated_at REAL, last_nudged TEXT)""")
con.execute("INSERT INTO routines (id, name, steps) VALUES ('rt-old', 'Goodnight', '[\"Lock up\"]')")
con.commit()
con.close()
legacy = rt.get("rt-old", rdb)
check("an existing routine: the household's, no owner", (legacy["audience"], legacy["owner"]), ("household", None))
check("everyone sees it, a kid too", (bool(rt.get("rt-old", rdb, person=MIR, role="adult")), bool(rt.get("rt-old", rdb, person=KID, role="kid"))),
      (True, True))
err = None
try:
    rt.save({"id": "rt-old", "name": "Goodnight", "steps": ["Unlock everything"]}, rdb, person=MIR, role="adult")
except PermissionError as exc:
    err = str(exc)
check("an adult can't change someone else's household routine", bool(err), True)
try:
    rt.delete("rt-old", rdb, person=MIR, role="adult")
    err = None
except PermissionError as exc:
    err = str(exc)
check("… or delete it", bool(err), True)
check("a household device may change a household routine",
      rt.save({"id": "rt-old", "name": "Goodnight", "steps": ["Lock up", "Lights off"]}, rdb, person=HOUSE, role="household")["steps"],
      ["Lock up", "Lights off"])
mine = rt.save({"name": "My meds", "steps": ["Take the blue pill"], "audience": "private",
                "schedule": {"time": "08:00"}}, rdb, person=MIR, role="adult")
check("Robin's own routine: hers, private", (mine["owner"], mine["audience"]), (MIR, "private"))
check("Zach doesn't see it", [r["name"] for r in rt.list_all(rdb, person="zach", role="admin")], ["Goodnight"])
check("a kid sees household routines only", [r["name"] for r in rt.list_all(rdb, person=KID, role="kid")], ["Goodnight"])
check("its card goes to her", rt.card_audience(mine), [MIR])
check("the household routine's card: the household", rt.card_audience(rt.get("rt-old", rdb)), "household")
shared = rt.save({"id": mine["id"], "name": "My meds", "steps": ["Take the blue pill"], "audience": ["zach"]}, rdb,
                 person=MIR, role="adult")
check("sharing widens it", (shared["audience"], "My meds" in [r["name"] for r in rt.list_all(rdb, person="zach", role="admin")]),
      (json.dumps(["zach"]), True))
check("… its card goes to both", rt.card_audience(shared), sorted([MIR, "zach"]))
err = None
try:
    rt.delete(mine["id"], rdb, person=KID, role="kid")
except PermissionError as exc:
    err = str(exc)
check("a kid can't delete it (doesn't even see it)", (err, rt.get(mine["id"], rdb) is not None), (None, True))
check("the admin may delete the household routine", rt.delete("rt-old", rdb, person="zach", role="admin"), True)
cards = []
pa_add = pa.add_card
pa.add_card = lambda *a, **kw: cards.append(kw.get("to")) or {"id": "x"}
try:
    rt._card(shared)
finally:
    pa.add_card = pa_add
check("the nudge card goes to the routine's audience", cards, [sorted([MIR, "zach"])])
as_("adult", MIR, "Robin")
os.environ["ROUTINES_DB"] = str(rdb)
rt.DB_PATH = rdb
check("the tool lists only hers", "My meds" in T("list_routines", {}), True)
as_("kid", KID, "Leo")
check("… a kid's tool list doesn't show it", "My meds" in T("list_routines", {}), False)

# ---------------------------------------------------------------------------- 8. small items

print("\n── 8. small items ──")
as_("admin", "zach", "Zach")
check("services-down line: the admin", "Services down" in ctx("hi"), True)
as_("adult", MIR, "Robin")
check("… not other adults", "Services down" in ctx("hi"), False)
as_("kid", KID, "Leo")
check("… nor kids", "Services down" in ctx("hi"), False)

as_("household", HOUSE, "Kitchen", device="d-kitchen")
lib._TURN_IO.outcomes = [{"tool": "home_control", "ok": True, "write": True, "text": "Kitchen lights off"}]
lib._remember_outcomes()
lib._TURN_IO.outcomes = None
check("a household device remembers its own turns", "Kitchen lights off" in (lib._recent_outcomes_line() or ""), True)
as_("household", HOUSE, "Kitchen", device="d-livingroom")
check("… another household device doesn't see them", lib._recent_outcomes_line(), None)

cdb = _TMP / "reply-proactive.db"
card = pa.add_card("Pick a dinner", "x", db=cdb, to="adults",
                   actions=[{"label": "Pasta", "kind": "reply"}])
got = []
pa.card_action(card["id"], "a0", db=cdb, person=MIR, role="adult", trigger=lambda ev: got.append(ev) or {})
check("a card reply carries who replied", (got[0]["person"], got[0]["role"]), (MIR, "adult"))
fake.streams["household-timeline"] = []
pa.submit_trigger(got[0], triage=lambda ev: {"actionable": {"probabilities": {"no": 0.1}}, "urgency": {"score": 1}},
                  agent=lambda p: "NOTIFY\nPasta it is; I added it to the plan.", db=cdb, background=False)
mine = [c for c in pa.list_cards(db=cdb, person=MIR, role="adult") if c["source"] == "trigger:card-reply"]
zachs = [c for c in pa.list_cards(db=cdb, person="zach", role="admin") if c["source"] == "trigger:card-reply"]
check("its outcome card goes to the person who replied", (len(mine), len(zachs), mine[0]["to"] if mine else None),
      (1, 0, json.dumps([MIR])))
ev = fake.streams["household-timeline"][-1]["payload"]
check("… and its timeline line is theirs", (ev["person"], ev["audience"]), (MIR, "private"))

lib._TURN_IO.role = lib._TURN_IO.person = lib._TURN_IO.device_id = None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
