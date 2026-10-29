#!/usr/bin/env python3
"""
Unit tests for AgentMemory fact de-duplication: duplicates are skipped, a changed value
replaces the stale fact, unrelated facts are kept. Hermetic: a temporary memory dir, no
embeddings.

Run: python3 scripts/test_memory_facts.py
"""
import os
import sys
import tempfile

os.environ["AGENT_MEMORY_DIR"] = tempfile.mkdtemp(prefix="titan-mem-test-")
os.environ["MEMORY_HYBRID"] = "0"
os.environ["MEMORY_GROUP_FACTS"] = "0"   # this file fakes the extractor's model; grouping has its own test
os.environ["MEMORY_EXTRACT_LOG"] = os.path.join(os.environ["AGENT_MEMORY_DIR"], "memory-extract.jsonl")
sys.path.insert(0, os.path.dirname(__file__))

import agent_memory  # noqa: E402


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def facts(m):
    return sorted(m.get_facts(person_id=agent_memory.OWNER))


def run():
    ok = True
    m = agent_memory.AgentMemory()

    print("\n── duplicates are skipped ──")
    m.add_fact("c1", "The household vet is Dr. Patel at Maple Animal Clinic, phone 555-0142.", person_id=agent_memory.OWNER)
    m.add_fact("c1", "The household vet is Dr. Patel at Maple Animal Clinic, phone 555-0142", person_id=agent_memory.OWNER)
    ok &= check("same fact twice → one row", len(facts(m)), 1)

    print("\n── a changed value replaces the stale fact ──")
    m.add_fact("c2", "The household vet is Dr. Patel at Maple Animal Clinic, phone 555-0199.", person_id=agent_memory.OWNER)
    ok &= check("still one row", len(facts(m)), 1)
    ok &= check("newest number kept", "555-0199" in facts(m)[0], True)
    ok &= check("stale number gone", any("555-0142" in f for f in facts(m)), False)

    m.add_fact("c2", "Gate code is 4417.", person_id=agent_memory.OWNER)
    m.add_fact("c3", "Gate code is 9032.", person_id=agent_memory.OWNER)
    ok &= check("short fact updates too (words match, value differs)",
                [f for f in facts(m) if "Gate code" in f], ["Gate code is 9032."])

    print("\n── different facts are both kept ──")
    m.add_fact("c4", "Trash and recycling go out Tuesday nights.", person_id=agent_memory.OWNER)
    m.add_fact("c4", "The kids' school pickup is at 3:15 on weekdays.", person_id=agent_memory.OWNER)
    ok &= check("unrelated facts kept", len(facts(m)), 4)
    m.add_fact("c5", "The garage door code is 1234.", person_id=agent_memory.OWNER)
    ok &= check("same shape, different subject → new fact", len(facts(m)), 5)

    print("\n── pin, forget, restore (the mind view) ──")
    m.add_fact("pin-test", "The router lives in the hall closet.", person_id=agent_memory.OWNER)
    m.add_fact("pin-test", "Grandma's birthday is on March 3rd.", person_id=agent_memory.OWNER)
    rows = {r["text"]: r for r in m.list_facts(50, person_id=agent_memory.OWNER)}
    router, bday = rows["The router lives in the hall closet."], rows["Grandma's birthday is on March 3rd."]
    ok &= check("list_facts carries ids and pinned=False", (bool(router["id"]), router["pinned"]), (True, False))
    ok &= check("pin", m.set_pinned(bday["id"], True, person_id=agent_memory.OWNER), True)
    ok &= check("pinned facts come first", m.list_facts(50, person_id=agent_memory.OWNER)[0]["id"], bday["id"])
    r = m.recall("where is the router", person_id=agent_memory.OWNER)
    ok &= check("a pinned fact rides along in an unrelated recall",
                ("birthday" in (r.prependContext or ""), "router" in (r.prependContext or "")), (True, True))
    ok &= check("forget", m.forget_fact(router["id"], person_id=agent_memory.OWNER), True)
    ok &= check("forgotten fact leaves recall and the list",
                ("hall closet" in (m.recall("where is the router", person_id=agent_memory.OWNER).prependContext or ""),
                 any(x["id"] == router["id"] for x in m.list_facts(50, person_id=agent_memory.OWNER))), (False, False))
    ok &= check("forgotten fact is restorable", (m.restore_fact(router["id"], person_id=agent_memory.OWNER), "hall closet" in (m.recall("router", person_id=agent_memory.OWNER).prependContext or "")), (True, True))
    ok &= check("unknown ids are reported", (m.set_pinned("nope", True, person_id=agent_memory.OWNER), m.forget_fact("nope", person_id=agent_memory.OWNER), m.restore_fact("nope", person_id=agent_memory.OWNER)), (False, False, False))

    print("\n── extraction: local only, strict, deduped ──")
    import urllib.request as _ur
    calls = []
    real_urlopen = _ur.urlopen
    class _Resp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        import json as _j
        return _Resp(_j.dumps({"choices": [{"message": {"content": "<think>x</think>Mara is the user's partner.\nThe user's partner is Mara.\nMara is the user's partner."}}]}).encode())
    saved_key = os.environ.get("GEMINI_API_KEY")
    try:
        os.environ["GEMINI_API_KEY"] = "test-key-not-real"
        _ur.urlopen = fake_urlopen
        m.add_message("pin-test", "user", "My partner Mara handles the vet appointments.")
        m.add_message("pin-test", "assistant", "Got it.")
        before = len(m.get_facts(limit=1000, person_id=agent_memory.OWNER))
        m._extract_facts_l1("pin-test", person_id=agent_memory.OWNER)
        added = [f for f in m.get_facts(limit=1000, person_id=agent_memory.OWNER)][: len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)) - before]
        ok &= check("extraction goes to the local model even with a Gemini key", all("googleapis" not in u for u in calls) and bool(calls), True)
        ok &= check("thinking stripped, duplicates skipped (add_fact also folds near-duplicates)",
                    (added, any("think" in f for f in m.get_facts(limit=1000, person_id=agent_memory.OWNER))), (["Mara is your partner."], False))   # written to the person (#1605)
        def none_urlopen(req, timeout=None):
            import json as _j
            return _Resp(_j.dumps({"choices": [{"message": {"content": "NONE"}}]}).encode())
        _ur.urlopen = none_urlopen
        n0 = len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)); m._extract_facts_l1("pin-test", person_id=agent_memory.OWNER)
        ok &= check("NONE stores nothing", len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)), n0)
        print("\n── only the user's words are material; dated and momentary facts are dropped ──")
        sent = []
        def echo_urlopen(req, timeout=None):
            import json as _j
            sent.append(_j.loads(req.data.decode())["messages"][-1]["content"])
            return _Resp(_j.dumps({"choices": [{"message": {"content":
                "The user has a Quinn appointment at 3:00 PM on Thursday, October 1.\n"
                "The user's kids are with their mom until Thursday morning.\n"
                "The user is looking at a window titled \"Claude\" in the Claude desktop app.\n"
                "The user has a scheduled appointment for Ballet today at 5:15 PM.\n"
                "The household lives in Spokane, Washington."}}]}).encode())
        _ur.urlopen = echo_urlopen
        m.add_message("cal-test", "user", "What's on tomorrow?")
        m.add_message("cal-test", "assistant", "Tomorrow: Quinn Appt at 3:00 PM and Leo Skate Night at 6:00 PM. Which city are you in?")
        m.add_message("cal-test", "user", "Spokane")
        n0 = len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)); m._extract_facts_l1("cal-test", person_id=agent_memory.OWNER)
        new = m.get_facts(limit=1000, person_id=agent_memory.OWNER)[: len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)) - n0]
        ok &= check("the extractor never sees the assistant's calendar text", "Skate Night" in sent[-1], False)
        ok &= check("an assistant question stays as context", "ASSISTANT (asked): Which city are you in?" in sent[-1], True)
        ok &= check("dated, relative and screen facts are dropped", sorted(new),
                    ["The household lives in Spokane, Washington.", "Your kids are with their mom until Thursday morning."])
        sent.clear()
        m.add_message("quiet", "assistant", "Here is your schedule.")
        m._extract_facts_l1("quiet", person_id=agent_memory.OWNER)
        ok &= check("no user words → no call", sent, [])
        ok &= check("asked: last question only", agent_memory._asked("Done. Two things. Want me to save it? Or not"), "Want me to save it?")

        print("\n── 10-01: code keeps only durable facts; wants stay wants; corrections replace ──")
        def batch_urlopen(lines):
            def f(req, timeout=None):
                import json as _j
                sent.append(_j.loads(req.data.decode())["messages"][-1]["content"])
                return _Resp(_j.dumps({"choices": [{"message": {"content": "\n".join(lines)}}]}).encode())
            return f
        _ur.urlopen = batch_urlopen([
            "The user is looking for information about the leakers.",          # asked about
            "The user mentioned that Ava never got a message.",               # a conversation act
            "The user and Ava have already left for the ballet.",             # momentary
            "The user cannot initiate actions at a future time on their own.", # the assistant's limit, misfiled
            "The user has completed the integration of Alexa.",                # a want stored as done
            "The user needs to pick up Leo from school by 3:15 PM.",         # a task
            "The user had leftover turkey chili for dinner.",                  # the timeline's
            "The user and their household have a child who spends time with their father during certain weeks.",
            "The user and their household have a child who spends time with their father during specific weeks of the year.",
            "Ava does ballet on Mondays and Wednesdays.",
            "The household takes the trash out on Tuesday nights."])
        m.add_message("q", "user", "I'd like to get all of the Alexa integrated as well. Ava does ballet Mondays and Wednesdays.")
        n0 = len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)); m._extract_facts_l1("q", person_id=agent_memory.OWNER)
        new = sorted(m.get_facts(limit=1000, person_id=agent_memory.OWNER)[: len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)) - n0])
        ok &= check("only the durable facts are stored, a near-duplicate folded", new,
                    sorted(["Ava does ballet on Mondays and Wednesdays.", "The household takes the trash out on Tuesday nights.",
                            "You and your household have a child who spends time with their father during certain weeks."]))
        ok &= check("the extractor sees the known facts, numbered", "Known facts:\n[1]" in sent[-1], True)
        m.add_fact("bill", "The user's T-Mobile bill is $400 a month.", person_id=agent_memory.OWNER)
        _ur.urlopen = batch_urlopen(["REPLACES 1: The user's October T-Mobile bill is $254.45, due October 27th."])
        sent.clear()
        m.add_message("bill", "user", "The total should be 254.45 on Oct 27th. We don't have past due balances.")
        m._extract_facts_l1("bill", person_id=agent_memory.OWNER)
        fs = m.get_facts(limit=1000, person_id=agent_memory.OWNER)
        n = int(next(l for l in sent[-1].splitlines() if "$400" in l).split("]")[0].strip("["))
        ok &= check("[1] in the prompt is the newest known fact (the $400)", n, 1)
        ok &= check("a correction replaces what it corrects (09-30: $400 came back)",
                    (any("$400" in f for f in fs), any("254.45" in f for f in fs)), (False, True))
        _ur.urlopen = batch_urlopen(["REPLACES 99: Something about nothing at all."])
        n0 = len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)); m._extract_facts_l1("bill", person_id=agent_memory.OWNER)
        ok &= check("a REPLACES of an unknown number forgets nothing", len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)) >= n0, True)

        print("\n── what each filter must never drop (the owner's review, 10-01) ──")
        keepers = ["The user must avoid peanuts because of an allergy.", "The user needs to take blood-pressure medication daily.",
                   "The household must keep the gate closed for the dog.", "Ava is allergic to eggs, so no eggs for breakfast.",
                   "The user prefers Claude for work writing.", "Ava does ballet on Mondays.", "Leo does ballet on Mondays."]
        ok &= check("durable constraints, diets and products are kept", [agent_memory._keep_extracted(k, "") for k in keepers], [True] * 7)
        ok &= check("different people, numbers or days never fold",
                    (agent_memory._near_duplicate("Ava does ballet on Mondays.", ["Leo does ballet on Mondays."]),
                     agent_memory._near_duplicate("The dog gets a checkup every 6 months.", ["The dog gets a checkup every 12 months."]),
                     agent_memory._near_duplicate("Ava does ballet on Mondays.", ["Ava does ballet on Tuesdays."]),
                     agent_memory._near_duplicate("Ava does ballet on Mondays.", ["Ava has ballet on Mondays."])),
                    (False, False, False, True))
        ok &= check("a credential's path is infrastructure, not memory",
                    agent_memory._keep_extracted("The user needs the agent token, located at /ai/config/agent_api_token.txt on titan.", ""), False)
        ok &= check("household routines about meals are kept (the owner's review)",
                    [agent_memory._keep_extracted(x, "") for x in ("Tuesday is taco night for dinner.",
                     "The household freezes leftovers on Sundays.")], [True, True])
        ok &= check("a meal however it's phrased is the timeline's (live 10-01)",
                    [agent_memory._keep_extracted(x, "") for x in ("There was leftover chili for dinner.",
                     "The family had pizza for dinner.", "Ava is allergic to eggs, so no eggs for breakfast.")], [False, False, True])
        ok &= check("a deadline still makes it a task", agent_memory._keep_extracted("The user needs to pick up Leo from school by 3:15 PM.", ""), False)
        m.add_fact("garden", "The household grows tomatoes in the back garden.", person_id=agent_memory.OWNER)
        _ur.urlopen = batch_urlopen(["REPLACES 1: Robin's car is a blue Subaru."])
        m.add_message("car", "user", "Robin drives a blue Subaru.")
        m._extract_facts_l1("car", person_id=agent_memory.OWNER)
        fs = m.get_facts(limit=1000, person_id=agent_memory.OWNER)
        ok &= check("a REPLACES with nothing in common forgets nothing, stores the new fact",
                    (any("tomatoes" in f for f in fs), any("Subaru" in f for f in fs)), (True, True))

        print("\n── what was stored reaches the world model (#358) ──")
        heard = []
        hook = lambda added, corrected, user_text, ev: heard.append((added, corrected, user_text, ev))
        m.add_fact("vet", "The vet's number is 555-0142.", person_id=agent_memory.OWNER)
        _ur.urlopen = batch_urlopen(["REPLACES 1: The vet's number is now 555-0199.", "Ava's ballet teacher is Ms. Ruiz."])
        m.add_message("vet", "user", "No, the vet's number is 555-0199 now. Ava's ballet teacher is Ms. Ruiz.")
        m._extract_facts_l1("vet", on_facts=hook, person_id=agent_memory.OWNER)
        ok &= check("a correction is heard as (old, new), a new fact as added",
                    (heard[0][1], heard[0][0]), ([("The vet's number is 555-0142.", "The vet's number is now 555-0199.")],
                                                  ["Ava's ballet teacher is Ms. Ruiz."]))
        ok &= check("…with the person's own words and the extractor's name",
                    ("555-0199" in heard[0][2], heard[0][3].startswith("model:")), (True, True))
        heard.clear()
        _ur.urlopen = batch_urlopen(["NONE"])
        m._extract_facts_l1("vet", on_facts=hook, person_id=agent_memory.OWNER)
        ok &= check("nothing stored: nothing heard", heard, [])

        m.add_fact("maple health", "Robin's prescriptions are picked up at Maple Health on Birch Rd.", person_id=agent_memory.OWNER)
        _ur.urlopen = batch_urlopen(["REPLACES 1: Robin's prescriptions are picked up at Maple Health on Birch Rd."])
        m.add_message("maple health", "user", "the prescriptions are at Maple Health on Birch")
        n0 = len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)); m._extract_facts_l1("maple health", person_id=agent_memory.OWNER)
        ok &= check("a REPLACES with the same words changes nothing (10-02: one fact churned 6 times)",
                    (len(m.get_facts(limit=1000, person_id=agent_memory.OWNER)), sum("Birch" in f for f in m.get_facts(limit=1000, person_id=agent_memory.OWNER))), (n0, 1))

        sent.clear()
        _ur.urlopen = batch_urlopen(["NONE"])
        m.add_message("card", "user", agent_memory.CARD_PROMPT_MARK + ' Create a calendar event "Dentist" on 2026-10-06.')
        m.add_message("card", "user", "My dentist is Dr. Park.")
        m._extract_facts_l1("card", person_id=agent_memory.OWNER)
        ok &= check("a suggestion card's tap never reaches fact extraction; the person's own line does",
                    ("Create a calendar event" in sent[-1], "Dr. Park" in sent[-1]), (False, True))

        print("\n── the persona restates; it never guesses ──")
        _ur.urlopen = batch_urlopen(["The user lives in Spokane. They are likely a parent, suggesting a busy household. "
                                     "Their October bill is $254.45. This indicates responsible financial management."])
        for i in range(5):
            m.add_fact("p", f"Persona fact number {i} about the household garden.", person_id=agent_memory.OWNER)
        m._refresh_persona()
        persona = agent_memory._PERSONA_MD.read_text()
        ok &= check("guesses dropped, facts kept", ("Spokane" in persona, "254.45" in persona, "likely" in persona,
                    "indicates" in persona), (True, True, False, False))
    finally:
        _ur.urlopen = real_urlopen
        if saved_key is None: os.environ.pop("GEMINI_API_KEY", None)
        else: os.environ["GEMINI_API_KEY"] = saved_key

    print("\n── the roster is the people store's; one-off events aren't memory's (10-02 audit) ──")
    P = [{"id": "zach", "name": "Zach Hale", "role": "admin"}, {"id": "rob", "name": "Robin", "role": "adult"},
         {"id": "leo", "name": "Leo", "role": "kid", "other_parent": "their dad"},
         {"id": "ava", "name": "Ava", "role": "kid", "other_parent": "their dad"}]
    for t in ("The user's household has a child named Robin.", "The user's household has a child named Leo.",
              "Robin is the user's daughter.", "Ava is Zach's girlfriend.", "The household includes Jordan, named after her aunt."):
        ok &= check(f"roster's to say: {t[:40]}", agent_memory.contradicts_roster(t, P), True)
    for t in ("Robin is allergic to shellfish.", "Ava does ballet on Mondays and Wednesdays.",
              "The user and their household have a child who spends time with their father during certain weeks.",
              "Leo's school is Lincoln Elementary.",
              # #1092: something named that isn't a household member is memory's
              "The family has a cat named Biscuit.", "Ava has a piano teacher named Ms. Ortiz.",
              "Leo has a friend called Sam.", "The user has a sister named Jordan.", "The family has a dog called Pepper.",
              "The family's insurance includes dental cover."):
        ok &= check(f"kept: {t[:40]}", agent_memory.contradicts_roster(t, P), False)
    for t in ("We have a daughter named Ava.", "The user has a son called Leo.", "Zach has a wife named Robin.",
              "The household has two members named Robin and Zach."):
        ok &= check(f"roster's to say (#1092): {t[:40]}", agent_memory.contradicts_roster(t, P), True)
    for t in ("The user has a dentist appointment on Friday.", "The user went to Costco yesterday.",
              "The plumber comes Oct 7 at 3 PM.", "Ava's recital is on 2026-10-09."):
        ok &= check(f"one-off: {t[:40]}", agent_memory.one_off(t), True)
    for t in ("The household takes the trash out on Tuesday nights.", "Ava has ballet every Thursday.",
              "The kids are with their dad until Thursday morning.", "Leo is allergic to peanuts."):
        ok &= check(f"not one-off: {t[:40]}", agent_memory.one_off(t), False)
    ok &= check("the prompt's roster", agent_memory.roster_block(P),
                "The household (already known; never write a fact about who is in it or what anyone is to it): adults: "
                "Zach, Robin; kids: Leo, Ava. The kids' other parent (their dad) lives elsewhere and is none of the "
                "adults here.")
    real_urlopen2 = _ur.urlopen
    try:
        import household_identity as _hh
        saved_store = _hh.get_store
        _hh.get_store = lambda: type("S", (), {"people": staticmethod(lambda: P)})()
        _ur.urlopen = batch_urlopen(["The user's household has a child named Robin.",
                                     "The user has a dentist appointment on Friday.",
                                     "Robin prefers oat milk in her coffee."])
        m.add_message("roster", "user", "Robin takes oat milk. Dentist Friday. Robin and the kids are home.")
        m._extract_facts_l1("roster", person_id=agent_memory.OWNER)
        got = [f for f in m.get_facts(limit=1000, person_id=agent_memory.OWNER) if f in ("The user's household has a child named Robin.",
               "The user has a dentist appointment on Friday.", "Robin prefers oat milk in her coffee.")]
        ok &= check("only the durable, roster-safe fact is stored", got, ["Robin prefers oat milk in her coffee."])
        ok &= check("the extractor saw the roster", "adults: Zach, Robin; kids: Leo, Ava" in sent[-1], True)
        row = m._db.execute("SELECT confidence FROM facts WHERE text=?", ("Robin prefers oat milk in her coffee.",)).fetchone()
        ok &= check("a model-read fact isn't stored at 1.0", row[0], agent_memory.EXTRACTED_CONFIDENCE)
        import memory_audit
        before = memory_audit.audit(m, P)["by_person"].get("zach", {})
        m.add_fact("old", "The user's household has a child named Leo.", person_id=agent_memory.OWNER)
        m.add_fact("old", "The user went to Costco yesterday.", person_id=agent_memory.OWNER)
        dry = memory_audit.audit(m, P)
        after = dry["by_person"].get("zach", {})
        ok &= check("audit: counts, nothing moved", ({k: after.get(k, 0) - before.get(k, 0) for k in ("roster", "one_off")},
                    dry["applied"]), ({"roster": 1, "one_off": 1}, False))
        memory_audit.audit(m, P, apply=True)
        moved = {r[0] for r in m._db.execute("SELECT text FROM forgotten_facts")}
        ok &= check("--apply moves them aside, restorable", ("The user's household has a child named Leo." in moved,
                    "The user went to Costco yesterday." in moved, "Robin prefers oat milk in her coffee." in m.get_facts(limit=1000, person_id=agent_memory.OWNER)),
                    (True, True, True))
    finally:
        _ur.urlopen = real_urlopen2
        _hh.get_store = saved_store

    print("\n── corrections must stand on what was said; extraction on Qwen3.6 with the 8B as fallback (#1090) ──")
    U = lambda *lines: "\n".join("USER: " + l for l in lines)   # noqa: E731
    for old, new, said, want in (
            ("The user drinks coffee every morning.", "The user drinks tea instead of coffee.",
             U("I switched from coffee to tea, the coffee thing is old news."), ""),
            ("Robin goes to a yoga class on Tuesdays.", "Robin goes swimming on Thursdays instead of yoga.",
             U("Robin stopped going to the Tuesday yoga class, she does Thursday swimming instead now."), ""),
            # shares only who it's about: stored as new, the yoga fact stays (safe: nothing true is forgotten)
            ("Robin goes to a yoga class on Tuesdays.", "Robin now swims on Thursdays.",
             U("Robin stopped going to the Tuesday yoga class, she does Thursday swimming instead now."), "unrelated"),
            ("Jordan's birthday is in March.", "Jordan is vegetarian.",
             U("Jordan went vegetarian last month, so plan dinners around that."), "unrelated"),
            ("Ava goes to Lincoln Middle School.", "Ava goes to Westview Middle School.",
             U("Actually Ava isn't at Lincoln anymore, she transferred to Westview Middle this fall."), ""),
            ("Groceries are delivered on Sundays.", "Groceries are delivered on Saturdays.",
             U("We adopted a cat on Saturday, her name is Biscuit."), "unmentioned"),
            ("Jordan's birthday is in March.", "Jordan's birthday is in April.",
             U("Jordan's birthday is in March right?", "What should we get her?"), "invented"),
            ("The user is allergic to penicillin.", "Leo is allergic to tree nuts.",
             U("Leo turned out to be allergic to tree nuts."), "other-subject"),
            ("The family hikes most weekends.", "The family does not hike most weekends.",
             U("New rule in our house: no screens at the dinner table, for everyone."), "unmentioned"),
            ("The user's T-Mobile bill is $400 a month.", "The user's October T-Mobile bill is $254.45, due October 27th.",
             U("The total should be 254.45 on Oct 27th. We don't have past due balances."), "")):
        ok &= check(f"correction: {new[:40]}", agent_memory.correction_problem(old, new, said), want)
    import json as _j
    real_urlopen3 = _ur.urlopen
    calls: list[str] = []

    def primary_down(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.startswith(agent_memory.MEMORY_EXTRACT_URL):
            raise OSError("connection refused")
        return _Resp(_j.dumps({"choices": [{"message": {"content": "Leo is learning the cello."}}]}).encode())
    try:
        os.environ.pop("GEMINI_API_KEY", None)
        _ur.urlopen = primary_down
        mx = agent_memory.AgentMemory()
        mx.add_message("x", "user", "Leo started learning the cello this month, he loves it.")
        mx._extract_facts_l1("x", person_id=agent_memory.OWNER)
        row = mx._db.execute("SELECT extractor FROM facts WHERE text LIKE '%cello%'").fetchone()
        ok &= check("primary first, then the fallback", (calls[0].startswith(agent_memory.MEMORY_EXTRACT_URL),
                    len(calls) == 2), (True, True))
        ok &= check("the stored fact says the fallback read it", row[0] if row else None,
                    "model:" + os.environ.get("AGENT_LOCAL_MODEL_NAME", "qwen3-8b"))
        calls.clear()
        _ur.urlopen = lambda req, timeout=None: (calls.append(req.full_url), _Resp(_j.dumps(
            {"choices": [{"message": {"content": "Leo's cello teacher is Ms. Ortiz."}}]}).encode()))[1]
        mx.add_message("x", "user", "His cello teacher is Ms. Ortiz.")
        mx._extract_facts_l1("x", person_id=agent_memory.OWNER)
        row = mx._db.execute("SELECT extractor FROM facts WHERE text LIKE '%Ortiz%'").fetchone()
        ok &= check("primary up: only it is asked, and the fact says so",
                    (len(calls), row[0] if row else None), (1, "model:" + agent_memory.MEMORY_EXTRACT_MODEL))
        ok &= check("a fact said directly has no extractor",
                    (mx.add_fact("x", "The user said this directly.", person_id=agent_memory.OWNER),
                     mx._db.execute("SELECT extractor FROM facts WHERE text LIKE '%directly%'").fetchone()[0]), (None, None))
    finally:
        _ur.urlopen = real_urlopen3

    print("\n── each extraction logs its counts, never its text (#1090 follow-up) ──")
    import json as _j2
    real_urlopen4 = _ur.urlopen
    log = agent_memory.MEMORY_EXTRACT_LOG
    try:
        open(log, "w").close()
        ml = agent_memory.AgentMemory()
        ml.add_fact("lg", "Leo plays soccer on Saturdays.", person_id=agent_memory.OWNER)
        ml.add_fact("lg", "The family lives in Portland.", person_id=agent_memory.OWNER)
        ml.add_message("lg", "user", "Leo's soccer moved to Sundays this season, and we got a dog called Pepper.")
        reply = "\n".join(["Leo plays soccer on Saturdays.", "The family lives in Portland.",   # two echoes
                           "REPLACES 2: Leo plays soccer on Sundays.", "The family has a dog called Pepper.",
                           "REPLACES 1: The family lives in Seattle."])                     # never mentioned: blocked
        _ur.urlopen = lambda req, timeout=None: _Resp(_j2.dumps({"choices": [{"message": {"content": reply}}]}).encode())
        ml._extract_facts_l1("lg", person_id=agent_memory.OWNER)
        rec = _j2.loads(open(log).read().splitlines()[-1])
        ok &= check("one line, the counts", {k: rec[k] for k in ("raw_lines", "echoes", "stored", "replaced")},
                    {"raw_lines": 5, "echoes": 2, "stored": 1, "replaced": 1})
        ok &= check("a blocked correction is counted by why (Portland was never mentioned)", rec["blocked"], {"unmentioned": 1})
        ok &= check("the extractor and timing are there, and no fact text",
                    ("extractor" in rec, "ms" in rec, "Pepper" in open(log).read(), "Seattle" in open(log).read()),
                    (True, True, False, False))
    finally:
        _ur.urlopen = real_urlopen4

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
