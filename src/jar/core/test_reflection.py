#!/usr/bin/env python3
"""
Unit tests for the nightly reflection (scripts/reflection.py): gather collects one local day from
every source with stable ids and local times, redacts secrets and stays under its budget; the
letter and dream keep only refs that exist; an empty day stores the empty state without a model
call; the model call is local only (Gemini and Jev are never touched, a non-loopback URL is
refused); re-running a day replaces it; and the API routes serve it behind the agent token.
Hermetic: the timeline, the model and every store are fakes or temp files.

Run: python3 scripts/test_reflection.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
os.environ["AGENT_TZ"] = "America/Los_Angeles"

import os as _os_acl, tempfile as _tf_acl  # noqa: E402
_os_acl.environ.setdefault("KB_ACL_DB", _os_acl.path.join(_tf_acl.mkdtemp(prefix="titan-acl-"), "kb_acl.db"))  # #1273: ingest writes sharing rows; never titan's own
import agent_runtime_lib as lib
import reflection as rf

DAY, PREV = "2026-09-22", "2026-09-15"   # a Tuesday, and the Tuesday before


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def at(day, hh, mm=0):
    return rf._bounds(day)[0] + hh * 3600 + mm * 60


def iso_utc(epoch):   # the event runtime's shape: chrono RFC 3339 with nanoseconds
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".123456789+00:00"


def make_stores(tmp: Path):
    pdb, ledger, mem = tmp / "proactive.db", tmp / "ledger.db", tmp / "memory" / "titan_memory.db"
    import proactive
    proactive._jobs_db(pdb).close()
    con = sqlite3.connect(pdb)
    con.executemany("INSERT INTO cards (id, source, kind, title, detail, urgency, action_prompt, created_at, acked_at, actions) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", [
                        ("pc-new", "agent", "suggestion", "Filter due", "", 1, None, at(DAY, 9, 15), None, "[]"),
                        ("pc-done", "trigger:mail", "report", "PG&E bill arrived", "", 2, None, at(DAY, 11), at(DAY, 12, 30), "[]"),
                        ("pc-old", "agent", "info", "Renew passport", "", 1, None, at(DAY, -30), at(DAY, 14), "[]"),
                        ("pc-stale", "agent", "info", "Call the plumber", "", 1, None, at(DAY, -50), None, "[]"),
                        ("pc-other", "agent", "info", "Tomorrow's thing", "", 1, None, at(DAY, 30), None, "[]")])
    con.executemany("INSERT INTO triggers (id, source, kind, summary, dedupe_key, received_at, outcome, reason, "
                    "card_id, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("tr-1", "git", "commit", "titan: refactor", "k1", at(DAY, 10), "logged", "", None, at(DAY, 10)),
        ("tr-2", "downloads", "file", "build output", "k2", at(DAY, 10, 5), "dropped", "noise", None, at(DAY, 10, 5)),
        ("tr-3", "mail", "mail", "invoice", "k3", at(DAY, 13), "error", "timeout talking to model", None, at(DAY, 13)),
        ("tr-4", "card-reply", "reply", 'The user answered "Yes" to your card "Deploy?"', "k4", at(DAY, 15), "no_action", "", None, at(DAY, 15))])
    con.executemany("INSERT INTO device_jobs (id, check_name, requested_by, requested_at, claimed_at, finished_at, ok, "
                    "exit_code, output, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("job-ok", "git-status", "agent", at(DAY, 8), at(DAY, 8), at(DAY, 8, 1), 1, 0, "clean", 1200),
        ("job-bad", "jellybean-build", "agent", at(DAY, 16), at(DAY, 16), at(DAY, 16, 2), 0, 1, "error", 41000),
        ("job-wait", "disk-check", "agent", at(DAY, 22), None, None, None, None, None, None)])
    con.commit()
    con.close()
    import connectors_runner as cx
    cx._db(ledger).close()
    con = sqlite3.connect(ledger)
    rows = [("calendar", f"c{i}", "h", f"calendar-c{i}", 1, 0, None, None, None, at(DAY, 6), None) for i in range(3)]
    rows += [("fixture", "smoke-1", "h", "fixture-1", 1, 0, None, None, None, at(DAY, 6), at(DAY, 6, 1)),
             ("daily", DAY, "h", "daily-x", 1, 0, None, None, None, at(DAY, 7), None),
             ("mail", "m1", "h", "mail-m1", 1, 0, None, None, None, at(DAY, -2), None)]
    con.executemany("INSERT INTO items (source, source_id, content_hash, item_id, kb, node, event_id, account, "
                    "observed_at, synced_at, deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    mem.parent.mkdir(parents=True)
    con = sqlite3.connect(mem)
    con.execute("CREATE TABLE facts (id TEXT PRIMARY KEY, conv_id TEXT, text TEXT, confidence REAL, ts REAL)")
    con.executemany("INSERT INTO facts VALUES (?,?,?,?,?)", [
        ("f1", "c", "The garage code is 4417", 1.0, at(DAY, 18)),
        ("f2", "c", "Old fact", 1.0, at(DAY, -40))])
    con.commit()
    con.close()
    return pdb, ledger, mem


def fake_timeline(streams):
    def fn(stream):
        return streams.get(stream, [])
    return fn


def events():
    return {
        "household-timeline": [
            {"event_id": "e1", "timestamp": iso_utc(at(DAY, 7, 2)), "payload": {"text": "Read out the morning brief"}},
            {"event_id": "e2", "timestamp": iso_utc(at(DAY, 7, 30)), "payload": {"text": "Bins put off: rain"}},
            {"event_id": "e3", "timestamp": iso_utc(at(DAY, 9)), "payload": {"text": "Reminder set for Thursday"}, "corrected": True},
            {"event_id": "e-old", "timestamp": iso_utc(at(DAY, -3)), "payload": {"text": "yesterday's entry"}},
            {"event_id": "e-pub", "published_at": iso_utc(at(DAY, 19)), "payload": {"text": "no occurred_at, has published_at"}},
            {"event_id": "e-obs", "payload": {"text": "only observed_at", "observed_at": iso_utc(at(DAY, 20))}},
            {"event_id": "e-none", "payload": {"text": "undatable"}},
            {"event_id": "p1", "timestamp": iso_utc(at(PREV, 9)), "payload": {"text": "Standup at 9:00"}}],
        "dev-activity": [{"event_id": "d1", "timestamp": iso_utc(at(DAY, 14)), "payload": {"text": "jellybean abc: review ok"}}],
    }


def guard_hosted():
    """Make every hosted-model entry point explode, and return a restore function."""
    import jev_client
    def boom(*a, **k):
        raise AssertionError("hosted model called from reflection")
    saved = {n: getattr(lib, n) for n in ("_gemini", "_gemini_call", "_gemini_search", "_run_gemini_agent", "_jev_client")}
    saved_jev = jev_client.decide
    for n in saved:
        setattr(lib, n, boom)
    jev_client.decide = boom

    def restore():
        for n, f in saved.items():
            setattr(lib, n, f)
        jev_client.decide = saved_jev
    return restore


def run():
    ok = True
    tmp = Path(tempfile.mkdtemp())
    pdb, ledger, mem = make_stores(tmp)
    db = tmp / "reflection.db"
    src = dict(timeline=fake_timeline(events()), proactive_db=pdb, ledger=ledger, memory_db=mem)

    print("\n── timestamps ──")
    ok &= check("chrono nanoseconds", rf._parse_ts("2026-09-29T17:02:03.123456789+00:00"), 1790701323.123456789)
    ok &= check("Z suffix", rf._parse_ts("2026-09-29T17:02:03Z"), 1790701323.0)
    ok &= check("offset", rf._parse_ts("2026-09-29T10:02:03-07:00"), 1790701323.0)
    ok &= check("garbage", rf._parse_ts("yesterday"), None)

    print("\n── gather ──")
    mat = rf.gather(DAY, **src)
    by = {i["id"]: i for i in mat["items"]}
    ok &= check("timeline entry with local time", (by["ev-e1"]["time"], by["ev-e1"]["kind"]), ("07:02", "timeline"))
    ok &= check("other days excluded", any(k in by for k in ("ev-e-old", "ev-p1", "card-pc-other")), False)
    ok &= check("published_at / observed_at fallbacks", (by["ev-e-pub"]["time"], by["ev-e-obs"]["time"]), ("19:00", "20:00"))
    ok &= check("undatable event skipped", "ev-e-none" in by, False)
    ok &= check("correction flagged", "corrected later" in by["ev-e3"]["title"], True)
    ok &= check("dev activity", by["ev-d1"]["kind"], "dev")
    ok &= check("open card", "not cleared yet" in by["card-pc-new"]["title"], True)
    ok &= check("card cleared same day", "you cleared it at 12:30" in by["card-pc-done"]["title"], True)
    ok &= check("older card cleared today", by["ack-pc-old"]["time"], "14:00")
    ok &= check("older open card listed as waiting", by["card-pc-stale"]["kind"], "waiting")
    ok &= check("card reply", by["trig-tr-4"]["kind"], "reply")
    ok &= check("trigger error is a failure", by["trig-tr-3"]["kind"], "failure")
    ok &= check("trigger summary", "Looked at 4 incoming events" in by[f"triggers-{DAY}"]["title"], True)
    ok &= check("checks: pass / fail / pending",
                (by["job-job-ok"]["title"], by["job-job-bad"]["kind"], "hasn't reported" in by["job-job-wait"]["title"]),
                ("Check git-status passed in 1s", "failure", True))
    ok &= check("deleted (forgotten) sync items don't count", f"ingest-fixture-{DAY}" in by, False)
    ok &= check("ingest counts per source, daily skipped",
                (by[f"ingest-calendar-{DAY}"]["title"], f"ingest-daily-{DAY}" in by, f"ingest-mail-{DAY}" in by),
                ("Synced 3 items from calendar", False, False))
    ok &= check("memory fact saved that day", "fact-f1" in by and "fact-f2" not in by, True)
    ok &= check("secret redacted", ("4417" in json.dumps(mat), "(hidden)" in by["fact-f1"]["title"]), (False, True))
    day_ts = [i["ts"] for i in mat["items"] if i["kind"] != "waiting"]
    ok &= check("items in time order, older open cards last", (day_ts == sorted(day_ts), mat["items"][-1]["kind"]),
                (True, "waiting"))
    ok &= check("waiting cards don't count as material", mat["count"], len(mat["items"]) - 1)
    ok &= check("timeline gap reported, not fatal",
                rf.gather(DAY, **{**src, "timeline": lambda s: (_ for _ in ()).throw(RuntimeError("down"))})["gaps"][:1],
                ["household-timeline unavailable (down)"])

    print("\n── timeline paging ──")
    pages, orig_req = [], lib._runtime_request
    try:
        def paged(svc, path, method="POST", payload=None, timeout=30):
            pages.append(path)
            if "cursor=" not in path:
                return {"events": [{"event_id": "a", "payload": {"text": "first page"}}], "next_cursor": 500}
            return {"events": [{"event_id": "b", "payload": {"text": "recent"}}], "next_cursor": None}
        lib._runtime_request = paged
        got = [e["event_id"] for e in lib._timeline_events()]
    finally:
        lib._runtime_request = orig_req
    ok &= check("follows next_cursor past the first 500", (got, pages[-1].endswith("?cursor=500")), (["a", "b"], True))

    print("\n── gather cap ──")
    many = {"household-timeline": [{"event_id": f"n{i}", "timestamp": iso_utc(at(DAY, 6) + i * 30),
                                    "payload": {"text": "x" * 400}} for i in range(500)]}
    big = rf.gather(DAY, **{**src, "timeline": fake_timeline(many)})
    size = sum(len(rf._line(i)) + 1 for i in big["items"])
    ok &= check("material within budget", size <= rf.MATERIAL_CHARS, True)
    ok &= check("item count capped", len(big["items"]) <= rf.MAX_ITEMS, True)
    ok &= check("titles clipped", max(len(i["title"]) for i in big["items"]) <= rf.TITLE_CHARS, True)
    ok &= check("failures survive the cap", "job-job-bad" in {i["id"] for i in big["items"]}, True)

    print("\n── letter: refs validated, stored, replaced ──")
    seen = {}

    def llm(system, user, schema, **kw):
        seen.update(system=system, user=user, schema=schema, kw=kw)
        return {"paragraphs": [[{"text": "Morning was quiet.", "ref": None},
                                {"text": "I read out the brief at 7:02.", "ref": "ev-e1"},
                                {"text": "I also washed the car.", "ref": "ev-made-up"}],
                               [{"text": "Nothing is waiting on you.", "ref": None}]]}, "qwen3-8b"
    r = rf.write_letter(DAY, llm=llm, db=db, **src)
    ok &= check("known ref kept", r["paragraphs"][0][1], {"text": "I read out the brief at 7:02.", "ref": "ev-e1"})
    ok &= check("unknown ref dropped, text kept", r["paragraphs"][0][2], {"text": "I also washed the car.", "ref": None})
    ok &= check("dropped count", r["dropped_refs"], 1)
    ok &= check("log carries id/time/kind/title", set(r["log"][0]), {"id", "time", "kind", "title"})
    ok &= check("prompt carries ids and times", "[ev-e1] 07:02 morning timeline" in seen["user"], True)
    ok &= check("schema constrains refs to gathered ids",
                seen["schema"]["properties"]["paragraphs"]["items"]["items"]["properties"]["ref"]["anyOf"][0]["enum"][:1],
                [mat["items"][0]["id"]])
    got = rf.get_letter(DAY, db=db)
    ok &= check("stored", (got["model"], got["material_count"], got["weekday"]), ("qwen3-8b", mat["count"], "Tuesday"))
    rf.write_letter(DAY, llm=lambda *a, **k: ({"paragraphs": [[{"text": "Rewritten.", "ref": None}]]}, "m2"), db=db, **src)
    con = sqlite3.connect(db)
    ok &= check("re-run replaces the day", con.execute("SELECT COUNT(*) FROM letters").fetchone()[0], 1)
    con.close()
    latest = rf.get_letter(db=db)["paragraphs"]
    ok &= check("latest is the rewrite", latest[0][0], {"text": "Rewritten.", "ref": None})
    ok &= check("closing line computed from open cards", latest[-1][-1]["text"].startswith("3 things are waiting on you"), True)
    ok &= check("model's own 'waiting' line replaced",
                [x["text"] for p in r["paragraphs"] for x in p].count("Nothing is waiting on you."), 0)
    ok &= check("prompt forbids the closing line", "closing line is added separately" in seen["system"], True)
    ok &= check("prompt gives the day's span", "Entries run from 06:00 to 22:" in seen["user"], True)

    print("\n── letter: refs repaired, code changes collapsed ──")
    items = {"a": {"id": "a", "time": "07:02", "title": "Read out the morning brief"},
             "b": {"id": "b", "time": "19:52", "title": "The user answered Works to the button card"},
             "c": {"id": "c", "time": "20:11", "title": "Check jellybean-selftests passed in 30s"}}
    paras = [[{"text": "You answered Works at 19:52.", "ref": "a"},
              {"text": "There were many dev updates, including new tools.", "ref": "b"},
              {"text": "The selftests passed again.", "ref": "c"}]]
    n = rf._fix_refs(paras, items)
    ok &= check("a sentence's time picks its entry", paras[0][0]["ref"], "b")
    ok &= check("ref sharing no words with its entry dropped", paras[0][1]["ref"], None)
    ok &= check("matching ref kept", (paras[0][2]["ref"], n), ("c", 2))
    dev = [{"id": f"d{i}", "ts": 100.0 + i, "time": "20:3" + str(i), "kind": "dev", "priority": 2,
            "title": f"{'titan-node' if i else 'jellybean'} abc{i}: change {i}"} for i in range(4)]
    shown, alias = rf._collapse_dev(dev + [{"id": "t", "ts": 50.0, "time": "08:00", "kind": "timeline",
                                           "priority": 1, "title": "bins out"}], "2026-09-29")
    ok &= check("dev changes collapse to one entry", [i["id"] for i in shown], ["t", "dev-2026-09-29"])
    ok &= check("summary names counts per repo", "4 code changes landed (titan-node 3, jellybean 1)" in shown[1]["title"], True)
    ok &= check("summary ref aliases the latest change", alias, {"dev-2026-09-29": "d3"})
    ok &= check("two changes stay as they are", rf._collapse_dev(dev[:2], "x")[1], {})
    ok &= check("closing: nothing waiting", rf._closing([], set())["text"], "Nothing is waiting on you.")
    one = rf._closing([{"id": "card-x", "title": "Still waiting on you (from Mon Sep 28): Loop test"}], {"card-x"})
    ok &= check("closing: one thing, linked", one, {"text": "One thing is waiting on you: Loop test.", "ref": "card-x"})
    ok &= check("dry run stores nothing",
                (rf.write_letter(DAY, llm=llm, db=db, dry_run=True, **src)["dry_run"], rf.get_letter(DAY, db=db)["model"]),
                (True, "m2"))

    print("\n── letter length ──")
    long = {"paragraphs": [[{"text": "word " * 30, "ref": None}] * 8] * 4 + [[{"text": "extra", "ref": None}]]}
    long["paragraphs"][-2] = long["paragraphs"][-2][:7] + [{"text": "Nothing is waiting on you.", "ref": None}]
    paras, _ = rf.validate_letter(long, set())
    words = sum(len(s["text"].split()) for p in paras for s in p)
    ok &= check("at most 4 paragraphs", len(paras) <= 4, True)
    ok &= check("word cap enforced", words <= 230, True)
    ok &= check("closing sentence survives", paras[-1][-1]["text"], "Nothing is waiting on you.")

    print("\n── empty day: no model call ──")
    def no_call(*a, **k):
        raise AssertionError("model called for an empty day")
    empty_src = dict(timeline=fake_timeline({}), proactive_db=tmp / "none.db", ledger=tmp / "none2.db", memory_db=tmp / "none3.db")
    r = rf.write_letter("2026-01-05", llm=no_call, db=db, **empty_src)
    ok &= check("empty state stored", (r["empty"], r["paragraphs"], r["model"], r["empty_text"]), (True, [], None, rf.EMPTY_LETTER))
    ok &= check("empty day readable", rf.get_letter("2026-01-05", db=db)["empty"], True)
    d = rf.write_dream("2026-01-05", llm=no_call, db=db, **empty_src)
    ok &= check("empty dream stored without a call", (d["empty"], d["findings"], d["question"]), (True, [], ""))
    down = {**empty_src, "timeline": lambda s: (_ for _ in ()).throw(RuntimeError("event runtime down"))}
    for name, fn in (("letter", rf.write_letter), ("dream", rf.write_dream)):
        try:
            fn("2026-01-07", llm=no_call, db=db, **down)
            ok &= check(f"unreadable sources: no empty {name} stored", False, True)
        except RuntimeError as exc:
            ok &= check(f"unreadable sources: no empty {name} stored", "event runtime down" in str(exc), True)
    ok &= check("nothing stored for that day", (rf.get_letter("2026-01-07", db=db), rf.get_dream("2026-01-07", db=db)), (None, None))
    ok &= check("stale open card alone isn't letter material",
                rf.write_letter("2026-01-06", llm=no_call, db=db, **{**empty_src, "proactive_db": pdb})["empty"], True)

    print("\n── local only ──")
    restore = guard_hosted()
    calls = []
    orig_http = lib._http_json
    saved_env = {k: os.environ.get(k) for k in ("LOCAL_LLM_URL", "AGENT_ENDPOINT")}

    def fake_http(url, payload=None, timeout=30):
        calls.append((url, payload))
        if url.endswith("/v1/models"):
            return {"data": [{"id": "Qwen3-8B-Q5"}]}
        if "findings" in json.dumps(payload["response_format"]):
            content = {"findings": [{"icon": "repeat", "text": "Standup ran again.", "ref": "ev-p1"}],
                       "question": "", "action_prompt": ""}
        else:
            content = {"paragraphs": [[{"text": "I read the brief.", "ref": "ev-e1"}]]}
        return {"choices": [{"message": {"content": "<think></think>" + json.dumps(content)}}]}
    try:
        os.environ.pop("LOCAL_LLM_URL", None)
        os.environ.pop("AGENT_ENDPOINT", None)
        lib._http_json = fake_http
        r = rf.write_letter(DAY, db=db, **src)
        d = rf.write_dream(DAY, db=db, **src)
        ok &= check("letter via the local model", (r["model"], r["paragraphs"][0][0]["ref"]), ("Qwen3-8B-Q5", "ev-e1"))
        ok &= check("dream via the local model", d["findings"][0]["icon"], "repeat")
        ok &= check("every call went to loopback :8010", all(u.startswith("http://127.0.0.1:8010/") for u, _ in calls), True)
        chat = [p for u, p in calls if u.endswith("/chat/completions")]
        ok &= check("/no_think appended", all(p["messages"][-1]["content"].endswith("/no_think") for p in chat), True)
        ok &= check("grammar-constrained json_schema", {p["response_format"]["type"] for p in chat}, {"json_schema"})
        ok &= check("temperature 0.4", {p["temperature"] for p in chat}, {0.4})
        ok &= check("dream prompt has last week's same weekday", "[ev-p1] 09:00" in chat[-1]["messages"][-1]["content"], True)
        n = len(calls)
        os.environ["LOCAL_LLM_URL"] = "https://api.example.com"
        try:
            rf.write_letter(DAY, db=db, **src)
            ok &= check("remote LLM URL refused", False, True)
        except RuntimeError as exc:
            ok &= check("remote LLM URL refused", ("local model only" in str(exc), len(calls) == n), (True, True))
    finally:
        lib._http_json = orig_http
        restore()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\n── dream validation ──")
    raw = {"findings": [{"icon": "rocket", "text": "Standup slid 30 min later, third week running.", "ref": "ev-p1"},
                        {"icon": "alert", "text": "Build failed.", "ref": "nope"},
                        {"icon": "check", "text": "", "ref": None},
                        {"icon": "code", "text": "a", "ref": None}, {"icon": "home", "text": "b", "ref": None},
                        {"icon": "moon", "text": "c", "ref": None}],
           "question": "Move the standup reminder to 9:30?", "action_prompt": ""}
    out, dropped = rf.validate_dream(raw, {"ev-p1"})
    ok &= check("unknown icon → moon", out["findings"][0]["icon"], "moon")
    ok &= check("unknown ref dropped", (out["findings"][1]["ref"], dropped), (None, 1))
    ok &= check("empty text skipped, max 4", len(out["findings"]), 4)
    ok &= check("question without action prompt is dropped", (out["question"], out["action_prompt"]), ("", ""))
    filler, _ = rf.validate_dream({"findings": [
        {"icon": "calendar", "text": "No prior data to compare with for the same weekday a week earlier.", "ref": None},
        {"icon": "alert", "text": "Ava's pickup moved to 3:30 again.", "ref": None}]}, set())
    ok &= check("'no prior data' is not a finding", [f["text"] for f in filler["findings"]],
                ["Ava's pickup moved to 3:30 again."])
    # #387: findings about titan's own plumbing are dropped, and so is its material
    plumbing, n_drop = rf.validate_dream({"findings": [
        {"icon": "alert", "text": "Three git skipped events in the afternoon; same time as last week's failures.", "ref": None},
        {"icon": "code", "text": "The build check failed twice after a deploy.", "ref": None},
        {"icon": "calendar", "text": "The dentist moved from Tuesday to Thursday.", "ref": None}]}, set())
    ok &= check("plumbing findings dropped, the household's kept (#387)",
                ([f["text"] for f in plumbing["findings"]], n_drop), (["The dentist moved from Tuesday to Thursday."], 2))
    ok &= check("dream material: the household's, not titan's",
                [rf._household(i) for i in ({"kind": "timeline", "title": "Ava's ballet moved to 4:30"},
                                            {"kind": "failure", "title": "Skipped an event from git (hourly cap): x"},
                                            {"kind": "card", "title": "I raised a card: jellybean-build failed"},
                                            {"kind": "card", "title": "I raised a card: Water bill due Friday"},
                                            {"kind": "ingest", "title": "Synced 12 items from mail"},
                                            {"kind": "memory", "title": "Remembered: Leo's skate night is Thursdays"})],
                [True, False, False, True, False, True])
    import proactive as _pa
    saved_cat = _pa.catalog
    try:
        _pa.catalog = lambda db=None: [{"name": "git-status"}, {"name": "jellybean-selftests"}]
        acts = rf._dream_actions()
        ok &= check("dream questions limited to real actions, incl. the Mac's named checks",
                    ("add a reminder" in acts, "git-status, jellybean-selftests" in acts), (True, True))
    finally:
        _pa.catalog = saved_cat
    d = rf.write_dream(DAY, db=db, llm=lambda *a, **k: ({"findings": [], "question": "", "action_prompt": ""}, "m"), **src)
    ok &= check("nothing noteworthy → empty", (d["empty"], rf.get_dream(DAY, db=db)["empty"]), (True, True))
    d = rf.write_dream(DAY, db=db, llm=lambda *a, **k: ({"findings": [{"icon": "clock", "text": "Standup moved.", "ref": "ev-e1"}],
                                                         "question": "Set a reminder?", "action_prompt": "Remind me at 9:25"}, "m"), **src)
    ok &= check("re-run replaces the dream", (rf.get_dream(db=db)["question"], rf.get_dream(db=db)["empty"]), ("Set a reminder?", False))

    print("\n── API routes ──")
    try:
        from fastapi.testclient import TestClient
        import agent_api_server as api
    except Exception as exc:  # the API needs fastapi/httpx; skip where they aren't installed
        print(f"  [SKIP] API not testable here: {exc}")
    else:
        import proactive
        import connectors_runner as cx
        saved = (rf.DB_PATH, proactive.DB_PATH, cx.LEDGER_PATH, lib._timeline_events, rf._local_llm, api._AGENT_TOKEN,
                 os.environ.get("AGENT_MEMORY_DIR"))
        try:
            rf.DB_PATH, proactive.DB_PATH, cx.LEDGER_PATH = tmp / "api.db", pdb, ledger
            os.environ["AGENT_MEMORY_DIR"] = str(mem.parent)
            lib._timeline_events = fake_timeline(events())
            rf._local_llm = lambda *a, **k: ({"paragraphs": [[{"text": "I read the brief.", "ref": "ev-e1"}]],
                                              "findings": [{"icon": "check", "text": "Brief read.", "ref": "ev-e1"}],
                                              "question": "", "action_prompt": ""}, "local")
            api._AGENT_TOKEN = "test-token"
            c = TestClient(api.app)
            h = {"X-Agent-Token": "test-token"}
            ok &= check("token required", c.get("/v1/letter").status_code, 401)
            ok &= check("no letter yet → 404", c.get("/v1/letter", headers=h).status_code, 404)
            ok &= check("bad day → 400", c.get("/v1/letter?day=tuesday", headers=h).status_code, 400)
            r = c.post("/v1/letter/run", json={"day": DAY}, headers=h)
            ok &= check("letter run", (r.status_code, r.json()["paragraphs"][0][0]["ref"]), (200, "ev-e1"))
            g = c.get("/v1/letter", headers=h).json()
            ok &= check("GET latest letter with log", (g["day"], len(g["log"]) > 5, g["written_time"] != ""), (DAY, True, True))
            ok &= check("GET by day", c.get(f"/v1/letter?day={DAY}", headers=h).json()["day"], DAY)
            r = c.post("/v1/dream/run", json={"day": DAY}, headers=h)
            ok &= check("dream run", (r.status_code, r.json()["findings"][0]["icon"]), (200, "check"))
            ok &= check("GET dream", set(c.get("/v1/dream", headers=h).json()) >= {"findings", "question", "action_prompt", "written_time"}, True)
            ok &= check("empty body run defaults the day", c.post("/v1/letter/run", headers=h).status_code, 200)
        finally:
            (rf.DB_PATH, proactive.DB_PATH, cx.LEDGER_PATH, lib._timeline_events, rf._local_llm, api._AGENT_TOKEN, mdir) = saved
            if mdir is None:
                os.environ.pop("AGENT_MEMORY_DIR", None)
            else:
                os.environ["AGENT_MEMORY_DIR"] = mdir

    print("\n── a dream's question that was done since (10-02: Costco) ──")
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    logs = tmp / "daily"
    logs.mkdir(exist_ok=True)
    (logs / "2026-10-02.md").write_text("- 05:10 Added the reminder “Milk”\n- 07:12 Added the reminder “Costco grocery list”\n")
    w = _dt(2026, 10, 2, 5, 30, tzinfo=_Z("America/Los_Angeles")).timestamp()
    later = _dt(2026, 10, 2, 10, 0, tzinfo=_Z("America/Los_Angeles")).timestamp()
    q = "Want me to add a reminder for the Costco grocery list? Add a reminder for the Costco grocery list"
    ok &= check("done after the dream: dropped", rf._done_since(q, w, logs, later), True)
    ok &= check("done before the dream doesn't count", rf._done_since("Want me to add a reminder for milk?", w, logs, later), False)
    ok &= check("something else: kept", rf._done_since("Want me to add the dentist on Friday?", w, logs, later), False)
    ok &= check("a later day counts too", (
        (logs / "2026-10-03.md").write_text("- 08:00 Added the event “Dentist on Friday”\n"),
        rf._done_since("Want me to add the dentist on Friday?", w, logs, later + 86400))[1], True)

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
