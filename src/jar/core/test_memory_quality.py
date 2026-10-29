#!/usr/bin/env python3
# gate: hermetic
"""Memory quality (#1605) and corrections that stick (#1613 part 2).

What it checks, on fictional chats with a fake extractor model:
  rules       facts are written to the person ("The user pays…" → "You pay…"); one-offs are dropped; a wish about
              titan goes to that person's dev notes, never their facts; another adult's health detail is never kept;
              a standing rule is kept as the rule ("Ava with Mom is a standing family event, don't check against it")
  corrections a correction supersedes the old fact (one row, the old one restorable), with or without the model's
              REPLACES, including a terse "No, red and baby blue"; an addition ("too") never replaces; a changed value
              nobody called a correction never replaces; an older third-person fact is the same fact as its
              second-person wording (no duplicate); a correction keeps its row's group and pin
  editing     a person edits their own fact in place (same id, the previous wording returned); never another's
  privacy     PATCH and GET /v1/memory/dev_notes over the real routes: each person reads and edits only their own; a
              shared screen and the engineering agent get nothing; a kid has their own; recall never carries another
              person's facts or anyone's dev notes; delete-my-data takes dev notes with it and Cancel brings them back
People: Alex (the admin), Robin and Jordan (adults), Ava and Leo (kids), Hale (the kids' other parent, not in the house).
Temporary stores; the clock is pinned and ticks one second per read. Run it under TZ=UTC and TZ=America/Los_Angeles.
Run: python3 scripts/test_memory_quality.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-memquality-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "SPACES_DB": "spaces.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "WORLD_DIR": "world",
             "ME_DELETE_DIR": "me-delete", "CLAUDE_CHATS_DB": "claude_chats.db", "CAPABILITY_SWITCHES": "switches.json",
             "HELD_ACTIONS_DB": "held.db", "MCP_SERVERS": "none.json",
             "MEMORY_EXTRACT_LOG": "memory-extract.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["MEMORY_GROUP_FACTS"] = "0"     # grouping has its own test; this one fakes only the extractor
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_memory as am  # noqa: E402
import household_identity as hh  # noqa: E402
import local_time  # noqa: E402


class _Clock:
    """A pinned clock that ticks one second per read, so "newest first" is deterministic."""
    def __init__(self, start: float):
        self.t = start

    def time(self) -> float:
        self.t += 1.0
        return self.t

    def __getattr__(self, name):   # anything else (sleep, monotonic) from the real module
        import time as _t
        return getattr(_t, name)


NOW = 1_792_000_000.0                       # 2026-10-15, fixed
am.time = _Clock(NOW)
local_time.now_line = lambda at=None: "Now: Thursday, October 15, 2026, 9:00 AM."

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


ALEX, ROBIN, JORDAN, AVA, LEO = am.OWNER, "p_robin", "p_jordan", "p_ava", "p_leo"
P = [{"id": ALEX, "name": "Alex Hale", "role": "admin"}, {"id": ROBIN, "name": "Robin", "role": "adult"},
     {"id": JORDAN, "name": "Jordan", "role": "adult"},
     {"id": AVA, "name": "Ava", "role": "kid", "other_parent": "Hale"},
     {"id": LEO, "name": "Leo", "role": "kid", "other_parent": "Hale"}]

print("── the rules, as code ──")
for third, second in (("The user pays for downtown parking with ParkMobile.", "You pay for downtown parking with ParkMobile."),
                      ("The user's sister Jordan lives in Boise.", "Your sister Jordan lives in Boise."),
                      ("The user watches the news at 6.", "You watch the news at 6."),
                      ("The user is vegetarian.", "You are vegetarian."),
                      ("The user and their household recycle on Tuesdays.", "You and your household recycle on Tuesdays."),
                      ("Robin likes window seats.", "Robin likes window seats.")):
    check(f"second person: {third[:38]}", am.second_person(third), second)
for line, want in (("The user wants the assistant to be able to answer anything about the house.", True),
                   ("You would like the app to show a dashboard of chores.", True),
                   ("The user is building a graph of the household.", True),
                   ("Robin prefers window seats on long flights.", False),
                   ("You want to visit Boise in the spring.", False)):
    check(f"a wish about titan is a dev note: {line[:40]}", am.product_wish(line), want)
for line, want in (("You picked up groceries at Costco yesterday.", True), ("Ava's recital is on 2026-10-22.", True),
                   ("You take the trash out on Tuesday nights.", False), ("Leo has swim every Thursday.", False)):
    check(f"one-off: {line[:40]}", am.one_off(line), want)
check("another adult's health detail is never kept in someone else's memory",
      [am.other_adults_private(x, P, ROBIN) for x in ("Jordan's prescriptions are at the Birch Rd pharmacy.",
                                                      "Your prescriptions are at the Birch Rd pharmacy.",
                                                      "Leo's therapist is Dr. Hale.", "Jordan likes tea.")],
      [True, False, False, False])
check("a standing rule is kept as the rule itself",
      am.standing_rule("The user instructed that Ava's time with Mom is a standing family event, so the calendar "
                       "should not be checked against it."),
      "Ava's time with Mom is a standing family event, so the calendar should not be checked against it.")
check("…and an imperative rule stays as written",
      am.standing_rule("Don't flag Leo's weekends with Hale as conflicts."), "Don't flag Leo's weekends with Hale as conflicts.")
check("'never' alone is not a rule (a reported happening stays the non-fact it is)",
      am.standing_rule("The user mentioned that Ava never got a message."), None)
check("who a fact is about: the person however it's written, nobody for an article",
      [am._subject(x) for x in ("The user drinks coffee.", "You drink tea.", "Your sister lives in Boise.",
                                "The balloons are red.", "Robin swims.", "The family hikes.")],
      ["you", "you", "you", "", "robin", "household"])
B_OLD = "The balloons for Ava's party are red and navy."
B_NEW = "The balloons for Ava's party are red and baby blue."
check("a terse correction supersedes (#1613: 'No, red and baby blue')",
      am.superseded(B_NEW, [("f1", B_OLD)], "USER: No, red and baby blue"), ("f1", B_OLD))
check("a fuller wording supersedes without a correction word",
      am.superseded("The balloons for Ava's party are red and baby blue.", [("f1", "The balloons for Ava's party are red and blue.")],
                    "USER: make it red and baby blue please"), ("f1", "The balloons for Ava's party are red and blue."))
check("an addition never supersedes ('too')",
      am.superseded("Ava has ballet on Thursdays.", [("f2", "Ava has ballet on Mondays.")], "USER: Ava has ballet on Thursdays too"),
      None)
check("a changed value nobody called a correction never supersedes",
      am.superseded("Ava has ballet on Thursdays.", [("f2", "Ava has ballet on Mondays.")], "USER: Ava has ballet on Thursdays"),
      None)
check("another subject never supersedes (Ava's ballet is not Leo's)",
      am.superseded("Leo has ballet on Thursdays now.", [("f2", "Ava has ballet on Mondays.")],
                    "USER: no, Leo has ballet on Thursdays now"), None)
check("words the person never said never supersede",
      am.superseded("The balloons for Ava's party are red and gold.", [("f1", B_OLD)], "USER: No, red and baby blue"), None)

# ── extraction end to end, a fake model ──
mem = am.AgentMemory()
_real_get_store = hh.get_store
hh.get_store = lambda: type("S", (), {"people": staticmethod(lambda: P)})()
PROMPTS: list[str] = []


def extract(person, user_lines, model_lines, conv=None):
    conv = conv or f"chat:{person}"
    mem._llm_call = lambda prompt, max_tokens=512: (PROMPTS.append(prompt), "\n".join(model_lines))[1]
    for line in user_lines:
        mem.add_message(conv, "user", line)
    mem._extract_facts_l1(conv, person_id=person)


def facts(person):
    return sorted(mem.get_facts(limit=1000, person_id=person))


def fid(person, text):
    r = mem._db.execute("SELECT id FROM facts WHERE person_id=? AND text=?", (person, text)).fetchone()
    return r[0] if r else None


print("── extraction keeps durable second-person facts only ──")
extract(ROBIN, ["I pay for downtown parking with ParkMobile.", "Got groceries at Costco yesterday.",
                "I wish the assistant could read the school portal for me.",
                "Jordan's prescriptions are at the Birch Rd pharmacy."],
        ["The user pays for downtown parking with ParkMobile.", "The user picked up groceries at Costco yesterday.",
         "The user wants the assistant to be able to read the school portal.",
         "Jordan's prescriptions are at the Birch Rd pharmacy."])
check("stored in the second person; the one-off, the wish and Jordan's health detail are not facts",
      facts(ROBIN), ["You pay for downtown parking with ParkMobile."])
check("the wish is in Robin's own dev notes", [n["text"] for n in mem.dev_notes(person_id=ROBIN)],
      ["The user wants the assistant to be able to read the school portal."])
check("…and nobody else's", [mem.dev_notes(person_id=p) for p in (ALEX, JORDAN, AVA)], [[], [], []])
check("the prompt asks for the second person, standing rules and terse corrections",
      all(x in PROMPTS[-1] for x in ("second person", "standing rule", "No, red and baby blue")), True)
mem._db.execute("INSERT INTO facts (id, conv_id, text, confidence, ts, person_id) VALUES ('old-park', 'c', ?, 1, ?, ?)",
                ("The user parks at the Main St garage on weekdays.", NOW - 86400, ROBIN))
mem._db.commit()
n0 = len(facts(ROBIN))
extract(ROBIN, ["Same as always, the Main St garage on weekdays."], ["You park at the Main St garage on weekdays."])
check("an older third-person fact is the same fact as its second-person wording: no duplicate", len(facts(ROBIN)), n0)

print("── corrections stick ──")
mem.add_fact("c", "The user drinks coffee every morning.", person_id=ROBIN)
mem.add_fact("c", "The user drinks coffee every morning.", person_id=JORDAN)
old_coffee = fid(ROBIN, "The user drinks coffee every morning.")
known_n = [t for t in mem.get_facts(limit=40, person_id=ROBIN)].index("The user drinks coffee every morning.") + 1
extract(ROBIN, ["I switched to tea in the mornings instead of coffee."],
        [f"REPLACES {known_n}: You drink tea every morning instead of coffee."])
check("REPLACES: one morning-drink fact, in Robin's words", [f for f in facts(ROBIN) if "morning" in f],
      ["You drink tea every morning instead of coffee."])
check("…the old one restorable", mem.restore_fact(old_coffee, person_id=ROBIN), True)
mem.forget_fact(old_coffee, person_id=ROBIN)
check("…and Jordan's own coffee fact untouched", "The user drinks coffee every morning." in facts(JORDAN), True)

mem.add_fact("c", B_OLD, person_id=ROBIN)
old_b = fid(ROBIN, B_OLD)
mem.set_group(old_b, "preference", person_id=ROBIN)
mem.set_pinned(old_b, True, person_id=ROBIN)
extract(ROBIN, ["No, red and baby blue"], [B_NEW])
check("#1613: 'No, red and baby blue' without REPLACES: the balloon details are updated, one row",
      [f for f in facts(ROBIN) if "balloons" in f], [B_NEW])
row = mem._db.execute("SELECT grp, pinned FROM facts WHERE person_id=? AND text=?", (ROBIN, B_NEW)).fetchone()
check("…the corrected row keeps its group and pin (one row per thing in Settings)", (row["grp"], row["pinned"]),
      ("preference", 1))
check("…the old colours restorable", mem._db.execute("SELECT COUNT(*) FROM forgotten_facts WHERE id=?", (old_b,)).fetchone()[0], 1)

mem.add_fact("c", "Ava has ballet on Mondays.", person_id=ROBIN)
extract(ROBIN, ["Ava has ballet on Thursdays too."], ["Ava has ballet on Thursdays."])
check("an addition never replaces: both days kept", sorted(f for f in facts(ROBIN) if "ballet" in f),
      ["Ava has ballet on Mondays.", "Ava has ballet on Thursdays."])

print("── a standing rule is kept (#1613) ──")
extract(ROBIN, ["Ava with Mom is a standing family event, don't check against it."],
        ["The user instructed that Ava's time with Mom is a standing family event and should not be checked against."])
rule = "Ava's time with Mom is a standing family event and should not be checked against."
check("kept as the rule, in the decision group", (rule in facts(ROBIN), mem._db.execute(
    "SELECT grp FROM facts WHERE text=?", (rule,)).fetchone()[0]), (True, "decision"))
n0 = len(facts(ROBIN))
extract(ROBIN, ["Remember, Ava with Mom is a standing family event."],
        ["Ava's time with Mom is a standing family event and should not be checked against."])
check("said again: still one row", len(facts(ROBIN)), n0)
check("recalled when it matters", rule in mem.recall("Is Ava's time with Mom a conflict on the calendar?",
                                                     person_id=ROBIN).prependContext, True)

print("── editing ──")
park = fid(ROBIN, "You pay for downtown parking with ParkMobile.")
got = mem.edit_fact(park, "You pay for downtown parking with the city app.", person_id=ROBIN)
check("edit in place: same id, the previous wording returned",
      (got["id"], got["previous"], fid(ROBIN, "You pay for downtown parking with the city app.")),
      (park, "You pay for downtown parking with ParkMobile.", park))
check("another person's fact: refused, unchanged",
      (mem.edit_fact(park, "Robin pays nothing.", person_id=JORDAN), fid(ROBIN, "You pay for downtown parking with the city app.")),
      (None, park))
check("no words: refused", mem.edit_fact(park, "  ", person_id=ROBIN), None)

print("── the one-time cleanup: proposals in the person's own queue, never applied unasked ──")
import memory_review as R  # noqa: E402
wish = (mem.add_fact("c", "The user wants titan to be able to plan the week automatically.", person_id=JORDAN),
        fid(JORDAN, "The user wants titan to be able to plan the week automatically."))[1]
trip = (mem.add_fact("c", "The user drove Leo to Boise yesterday.", person_id=JORDAN),
        fid(JORDAN, "The user drove Leo to Boise yesterday."))[1]
before_j, before_r = facts(JORDAN), facts(ROBIN)
got = R.cleanup(mem, [p for p in P if p["id"] == JORDAN], now=NOW)
check("Jordan: a wish, a one-off and the third-person rewrite, as proposals; nothing changed",
      ((got["wish"], got["one_off"], got["voice"]), facts(JORDAN)), ((1, 1, 1), before_j))
q = {i["kind"]: i for i in R.queue(mem, JORDAN, "adult", now=NOW)}
check("Robin's queue holds none of Jordan's", [i for i in R.queue(mem, ROBIN, "adult", now=NOW)
                                              if i["kind"] in ("wish", "one_off", "voice")], [])
R.resolve(mem, JORDAN, "adult", q["wish"]["id"], "to_dev_note", now=NOW)
check("to_dev_note: the wish leaves Jordan's facts for Jordan's dev notes",
      (wish in {f["id"] for f in mem.list_facts(limit=1000, person_id=JORDAN)},
       [n["text"] for n in mem.dev_notes(person_id=JORDAN)]),
      (False, ["The user wants titan to be able to plan the week automatically."]))
r_voice = R.resolve(mem, JORDAN, "adult", q["voice"]["id"], "rewrite_all", now=NOW)
check("rewrite_all: Jordan's facts in the second person, same ids; Robin's untouched",
      (r_voice["rewritten"] >= 1, "You drink coffee every morning." in facts(JORDAN), fid(JORDAN, "You drink coffee every morning.")
       is not None, facts(ROBIN) == before_r), (True, True, True, True))
try:
    R.resolve(mem, ROBIN, "adult", q["one_off"]["id"], "forget", now=NOW)
    status = "resolved"
except R.Refused as exc:
    status = exc.status
check("another person can't resolve Jordan's item; the trip fact stays", (status, trip in {
    f["id"] for f in mem.list_facts(limit=1000, person_id=JORDAN)}), (404, True))

print("── the routes: each person's own, nobody else's ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

hh.get_store = _real_get_store
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
api._get_memory = lambda: mem
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, platform="ios"):
    s_ = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": f"{name}'s device",
                                         "platform": platform}).json()["token"]
    return {"X-Agent-Token": tok}, next(p["id"] for p in st.people() if p["name"] == name)


RH, r_id = pair("Robin Hale", "adult")
JH, j_id = pair("Jordan Hale", "adult")
AH, a_id = pair("Ava Hale", "kid")
TV, _ = pair("Living room", "household", "tvos")
for pid, fact_text, note in ((r_id, "You keep the spare key with Jordan.", "Robin wants the app to track chores."),
                             (j_id, "You take the 7:40 bus on weekdays.", "Jordan wants titan to read the bus schedule."),
                             (a_id, "Your favorite color is green.", "Ava wants the assistant to tell jokes.")):
    mem.add_fact("c", fact_text, person_id=pid)
    mem.add_dev_note("c", note, person_id=pid)
r_fact, j_fact, a_fact = (fid(r_id, "You keep the spare key with Jordan."), fid(j_id, "You take the 7:40 bus on weekdays."),
                          fid(a_id, "Your favorite color is green."))

r = c.patch(f"/v1/memory/facts/{r_fact}", json={"text": "You keep the spare key with Leo's teacher."}, headers=RH)
check("PATCH text: the owner edits in place", (r.status_code, r.json().get("id"), r.json().get("previous")),
      (200, r_fact, "You keep the spare key with Jordan."))
check("…one row in their list", [f["text"] for f in c.get("/v1/memory/facts?detail=1", headers=RH).json()["facts"]],
      ["You keep the spare key with Leo's teacher."])
check("PATCH another person's fact: 404 for an adult, a kid and the admin; nothing changed",
      ([c.patch(f"/v1/memory/facts/{j_fact}", json={"text": "changed"}, headers=h).status_code for h in (RH, AH, M)],
       fid(j_id, "You take the 7:40 bus on weekdays.")), ([404, 404, 404], j_fact))
check("…nor can its pin be set", c.patch(f"/v1/memory/facts/{j_fact}", json={"pinned": True}, headers=RH).status_code, 404)
check("a shared screen: 403", c.patch(f"/v1/memory/facts/{a_fact}", json={"text": "Blue."}, headers=TV).status_code, 403)
check("a kid edits their own", c.patch(f"/v1/memory/facts/{a_fact}", json={"text": "Your favorite color is purple."},
                                       headers=AH).status_code, 200)
check("bad bodies: 400", [c.patch(f"/v1/memory/facts/{r_fact}", json=b, headers=RH).status_code
                          for b in ({}, {"text": "x"}, {"text": 5}, {"text": "y" * 600})], [400, 400, 400, 400])
check("pin and text together", c.patch(f"/v1/memory/facts/{r_fact}", json={"pinned": True, "text": "You keep the spare key "
                                       "under the blue pot."}, headers=RH).json(),
      {"id": r_fact, "text": "You keep the spare key under the blue pot.", "previous": "You keep the spare key with Leo's teacher.",
       "pinned": True})


def notes(h):
    r = c.get("/v1/memory/dev_notes", headers=h)
    return [n["text"] for n in r.json()["notes"]] if r.status_code == 200 else r.status_code


check("GET dev_notes: each person their own only", (notes(RH), notes(JH), notes(AH)),
      (["Robin wants the app to track chores."], ["Jordan wants titan to read the bus schedule."],
       ["Ava wants the assistant to tell jokes."]))
check("…the admin's token reads only the admin's own (none of Robin's, Jordan's or Ava's)",
      [n for n in notes(M) if any(x in n for x in ("Robin", "Jordan", "Ava"))], [])
check("…a shared screen: 403", notes(TV), 403)
check("…never the engineering agent, never a kid's Watch",
      (hh.route_allowed(hh.ENGINEERING, "GET", "/v1/memory/dev_notes"), hh.watch_route_allowed("GET", "/v1/memory/dev_notes"),
       hh.route_allowed(hh.ENGINEERING, "PATCH", "/v1/memory/facts/x")), (False, False, False))


def recall(h, q):
    return c.post("/v1/memory/recall", json={"query": q}, headers=h).json()["prependContext"]


check("recall: Jordan never gets Robin's spare key; Robin never gets Jordan's bus",
      ("spare key" in recall(JH, "where is the spare key"), "7:40" in recall(RH, "which bus on weekdays")), (False, False))
check("recall: nobody's dev notes reach a turn", any("track chores" in recall(h, "track chores app")
                                                    for h in (RH, JH, M)), False)

print("── delete-my-data takes dev notes with it ──")
import me_delete  # noqa: E402
parts = me_delete.Parts(mem)
tomb = me_delete._tomb(r_id, create=True)
got = parts.stash_memory(r_id, hh.conv_id_for(r_id), tomb)
parts.drop_memory(r_id, hh.conv_id_for(r_id), tomb)
tomb.commit()
check("stashed and gone: Robin's dev notes; Jordan's stay", (got.get("dev_notes"), mem.dev_notes(person_id=r_id),
                                                             len(mem.dev_notes(person_id=j_id))), (1, [], 1))
parts.restore_memory(tomb)
check("Cancel brings them back", [n["text"] for n in mem.dev_notes(person_id=r_id)], ["Robin wants the app to track chores."])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
