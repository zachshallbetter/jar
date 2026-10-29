#!/usr/bin/env python3
"""Every model titan asks gets the household's date and time (10-01: Zach, "make sure the agent is
always aware what time and date it is"). Hermetic: every model and network call is replaced by a
recorder, and the clock is fixed at Wednesday Sep 30 2026 22:30 Pacific, when UTC is already
Oct 1 (the evening the weather tool called tomorrow "Today").

Run: python3 scripts/test_now_everywhere.py
"""
import io
import json
import os
import sys
import tempfile
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(__file__))
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ["AGENT_MEMORY_DIR"] = tempfile.mkdtemp()

import local_time  # noqa: E402

FIXED = datetime(2026, 9, 30, 22, 30, tzinfo=local_time.zone())
local_time.now = lambda name=None: FIXED          # the household's clock, fixed
local_time.today = lambda name=None: FIXED.date()

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0
WANT = "Now: Wednesday, September 30 2026 — 10:30 PM PDT (tomorrow is Thursday, October 1)"


def check(label, ok, detail=""):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  {detail}"))


def has_now(text):
    text = str(text).replace("\\u2014", "\u2014")   # recorded JSON escapes the dash
    return WANT in text and "2026-10-01" not in text


# --- the line itself ---------------------------------------------------------------------------
check("the line: weekday, date, time, zone and tomorrow", local_time.now_line() == WANT, local_time.now_line())
check("lib's clock is the household's", lib._local_now() == FIXED, str(lib._local_now()))
check("no zero-padded day (the model copied \"October 01\")",
      local_time.now_line(datetime(2026, 10, 1, 9, 5, tzinfo=local_time.zone())), "Now: Thursday, October 1 2026 — 9:05 AM PDT (tomorrow is Friday, October 2)")
check("an early-morning hour says AM (the model read 02:31 as 2:31 PM)",
      "2:31 AM PDT" in local_time.now_line(datetime(2026, 10, 1, 2, 31, tzinfo=local_time.zone())), True)
ts = FIXED.timestamp()
check("ago: minutes", local_time.ago(ts - 300) == "5 min ago", local_time.ago(ts - 300))
check("ago: hours today", local_time.ago(ts - 3 * 3600) == "3 h ago", local_time.ago(ts - 3 * 3600))
check("ago: yesterday", local_time.ago(ts - 24 * 3600) == "yesterday 22:30", local_time.ago(ts - 24 * 3600))
check("ago: older", local_time.ago(ts - 3 * 86400) == "Sun Sep 27 22:30", local_time.ago(ts - 3 * 86400))
check("a zone that won't load falls back (and says so)", local_time.zone("Not/AZone") is None)

# --- every turn: the per-turn system message ---------------------------------------------------
orig_ctx = lib._situational_context
lib._situational_context = lambda query=None: lib._lt.now_line(lib._local_now()) + "\nRecent: something"
msgs = lib._with_situation([{"role": "system", "content": "identity"},
                            {"role": "system", "content": "[SITUATION — old]\nNow: Monday"},
                            {"role": "user", "content": "what day is it"}])
sit = [m for m in msgs if m["content"].startswith("[SITUATION")]
check("a turn: one fresh situation, after the first system message",
      len(sit) == 1 and msgs[1] is sit[0] and has_now(sit[0]["content"]), json.dumps(msgs)[:300])


def boom(query=None):
    raise RuntimeError("KB down")


lib._situational_context = boom
msgs = lib._with_situation([{"role": "system", "content": "identity"}, {"role": "user", "content": "hi"}])
check("a turn whose context fails still gets the date and time",
      any(m["role"] == "system" and has_now(m["content"]) for m in msgs), json.dumps(msgs)[:300])
lib._situational_context = lambda query=None: "Recent: only this"
msgs = lib._with_situation([{"role": "user", "content": "hi"}])
check("context without a Now line gets one", has_now(msgs[0]["content"]) and "Recent: only this" in msgs[0]["content"],
      msgs[0]["content"])
lib._situational_context = orig_ctx

# Trimming for the local model keeps system messages (so the Now line survives a long chat).
long = [{"role": "system", "content": "identity"}, {"role": "system", "content": "[SITUATION]\n" + WANT}]
long += [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 4000} for i in range(60)]
fit = lib._fit_local_context(long, 8000)
check("a long chat trimmed for the local model keeps the Now line", any(WANT in (m.get("content") or "") for m in fit),
      f"{len(fit)} messages left")

# --- background model calls --------------------------------------------------------------------
sent = []
lib._gemini = lambda model, parts, **kw: (sent.append(json.dumps(parts)) or
                                          {"candidates": [{"content": {"parts": [{"text": json.dumps(
                                              {"doc_type": "bill", "vendor": "PGE", "summary": "s"})}]}}]})
lib._http_json = lambda url, payload=None, **kw: (sent.append(url + " " + json.dumps(payload)) or
                                                  {"choices": [{"message": {"content": "- summary"}}]})

os.environ["GEMINI_API_KEY"] = "test-key"
sent.clear()
lib._summarize_turns([{"role": "user", "content": "remind me tomorrow"}, {"role": "assistant", "content": "ok"}])
check("summaries (Gemini): the date, and dates instead of 'tomorrow'", sent and has_now(sent[-1]) and "never" in sent[-1], sent[-1:])
os.environ.pop("GEMINI_API_KEY")
os.environ.pop("LOCAL_LLM_URL", None)
os.environ["AGENT_ENDPOINT"] = "http://127.0.0.1:8010"
sent.clear()
lib._summarize_turns([{"role": "user", "content": "remind me tomorrow"}])
check("summaries (local): the date, on the local model's port (:8010, not :8080)",
      sent and has_now(sent[-1]) and sent[-1].startswith("http://127.0.0.1:8010/"), sent[-1:])

bill = os.path.join(tempfile.mkdtemp(), "bill.pdf")
open(bill, "w").write("x")
lib._document_extract_text = lambda path: "PGE. Amount due $84.20. Due Oct 5."
os.environ["GEMINI_API_KEY"] = "test-key"
sent.clear()
try:
    lib.execute_tool("extract_bill", {"path": bill})
except Exception:  # noqa: BLE001 - only the prompt matters here
    pass
check("bills: the date, so 'Due Oct 5' gets the right year", sent and has_now(sent[0]), sent[:1])

import urllib.request as _ur  # noqa: E402
bodies = []


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None, **kw):
    bodies.append((getattr(req, "full_url", str(req)), (getattr(req, "data", b"") or b"").decode()))
    return _Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}], "answers": {},
                             "choices": [{"message": {"content": "{\"correction\": false}"}}]}).encode())


_ur.urlopen = fake_urlopen
bodies.clear()
try:
    lib._gemini_search("what's on tonight")
except Exception:  # noqa: BLE001
    pass
check("web search: 'tonight' is read against today", bodies and has_now(bodies[-1][1]), bodies[-1:])

import proactive  # noqa: E402
proactive.urllib.request.urlopen = fake_urlopen
bodies.clear()
try:
    proactive._decider_triage({"source": "calendar", "kind": "event", "summary": "Dentist tomorrow 9:00"})
except Exception:  # noqa: BLE001
    pass
state = json.loads(bodies[-1][1]).get("state", {}) if bodies else {}
check("triage: the Decider knows when 'now' is", has_now(state.get("now", "")), state)

import correction_digest as cd  # noqa: E402
cd.urllib.request.urlopen = fake_urlopen
bodies.clear()
try:
    cd.confirm_with_model(cd.Correction(ts=ts - 7200, asked="what's tomorrow", reply="Friday", correction="no, Thursday", why="pattern"))
except Exception:  # noqa: BLE001
    pass
check("corrections digest: the date and when the exchange was", bodies and has_now(bodies[-1][1]) and "2 h ago" in bodies[-1][1],
      bodies[-1:])

import agent_memory  # noqa: E402
prompts = []
mem = agent_memory.AgentMemory()
mem._llm_call = lambda prompt, max_tokens=512: (prompts.append(prompt) or "NONE")
for i in range(agent_memory.L1_EXTRACT_EVERY_N):
    mem.add_message("c1", "user", f"We moved Ava's lesson to next Thursday ({i})")
mem._extract_facts_l1("c1", person_id=agent_memory.OWNER)
check("fact extraction: 'next Thursday' has a reference date", prompts and has_now(prompts[-1]), prompts[-1:])

# A recalled fact says when it was noted (a day or a year old reads differently).
mem.add_fact("c1", "Ava's piano lesson moved to Thursdays", person_id=agent_memory.OWNER)
mem._db.execute("UPDATE facts SET ts=? WHERE text=?", (ts - 86400, "Ava's piano lesson moved to Thursdays"))
mem.add_fact("c1", "The furnace filter is 16x25x1", person_id=agent_memory.OWNER)
mem._db.execute("UPDATE facts SET ts=? WHERE text=?", (ts - 400 * 86400, "The furnace filter is 16x25x1"))
mem._db.commit()
mem._bm25 = None
ctx = mem.recall("when is Ava's piano lesson? what size is the furnace filter?", person_id=agent_memory.OWNER).prependContext
check("recall: a fact noted yesterday says so", "Ava's piano lesson moved to Thursdays (noted yesterday)" in ctx, ctx)
check("recall: an old fact gives its year", "The furnace filter is 16x25x1 (noted Aug 26, 2025)" in ctx, ctx)

# --- knowledge: a note says when it was saved --------------------------------------------------
import kb_privacy  # noqa: E402
posted = []
orig_post = lib._runtime_post
lib._runtime_post = lambda svc, path, body, **kw: (posted.append(body) or {"ok": True})
lib._kb_index("note-x", "Wi-Fi", "Wi-Fi\n\nLadon-Guest", "note", {"origin": "test"})
lib._runtime_post = orig_post
check("a saved note is stamped with the household's date (not UTC's)", posted[-1]["metadata"].get("saved"), "2026-09-30")
hits = [{"citation_ref": "note-wifi", "chunk_text": "Ladon-Guest", "metadata": {"saved": "2026-09-29"}},
        {"citation_ref": "note-old", "chunk_text": "16x25x1", "metadata": {"saved": "2025-08-26"}},
        {"citation_ref": "seed", "chunk_text": "runbook"}]
b = kb_privacy.bundle(hits, FIXED)
check("search results say when each note was saved",
      ("[note-wifi · saved yesterday, Tue Sep 29]" in b, "[note-old · saved Tue Aug 26, 2025]" in b, "[seed] runbook" in b), (True, True, True))
check("passages too", "(saved yesterday, Tue Sep 29)" in lib._format_passages([{"title": "Wi-Fi", "chunk_text": "x", "metadata": {"saved": "2026-09-29"}}]), True)
check("a synced item says when it's dated (#396)", "(dated yesterday, Tue Sep 29)" in lib._format_passages(
      [{"title": "Mail: water bill", "chunk_text": "x", "metadata": {"observed_at": "2026-09-29T16:00:00Z"}}]))
print("── #627: every label says the weekday and date (titan's own model read a bare \"today\" as the day before) ──")
for saved, want in [("2026-09-30", "saved today, Wed Sep 30"), ("2026-09-29", "saved yesterday, Tue Sep 29"),
                    ("2026-09-25", "saved Fri Sep 25"), ("2025-08-26", "saved Tue Aug 26, 2025")]:
    check(f"date_label {saved}", kb_privacy.date_label({"metadata": {"saved": saved}}, FIXED), want)
check("a future observed_at (a calendar item) is a plain day", kb_privacy.date_label(
      {"metadata": {"observed_at": "2026-10-09T16:00:00Z"}}, FIXED), "dated Fri Oct 9")
check("…a note's saved wins over observed_at", "(saved yesterday, Tue Sep 29)" in lib._format_passages(
      [{"title": "n", "chunk_text": "x", "metadata": {"saved": "2026-09-29", "observed_at": "2026-01-01T00:00:00Z"}}]))
check("…and neither: no label", "(dated" not in lib._format_passages([{"title": "n", "chunk_text": "x", "metadata": {}}])
      and "(saved" not in lib._format_passages([{"title": "n", "chunk_text": "x", "metadata": {}}]))

# --- weather: the place's own today, not the host's UTC date -----------------------------------
import weather  # noqa: E402
forecast = {"current": {"time": "2026-09-30T22:30", "temperature_2m": 58, "weather_code": 2, "wind_speed_10m": 6},
            "daily": {"time": ["2026-09-30", "2026-10-01", "2026-10-02"], "weather_code": [2, 61, 0],
                      "temperature_2m_max": [70, 64, 72], "temperature_2m_min": [52, 50, 51],
                      "precipitation_probability_max": [5, 80, 0]}}
weather.date = type("UTCDate", (date,), {"today": classmethod(lambda cls: date(2026, 10, 1))})   # the host is UTC
out = weather.forecast({"name": "Portland", "latitude": 45.5, "longitude": -122.6, "timezone": "America/Los_Angeles"},
                       fetch=lambda url: forecast)
lines = out.splitlines()
check("weather at 22:30 Pacific: Today is Sep 30", any(l.startswith("Today (Wed Sep 30)") for l in lines), out)
check("weather: Tomorrow is Oct 1", any(l.startswith("Tomorrow (Thu Oct 1)") for l in lines), out)

# --- history times -----------------------------------------------------------------------------
import agent_api_server as api  # noqa: E402
h = [{"role": "user", "content": "a", "at": ts - 26 * 3600}, {"role": "assistant", "content": "b", "at": ts - 13 * 3600}]
line = api._history_when(h)
check("history: first and last, with the 'that day' note", line ==
      "Earlier messages in this chat: the first yesterday 20:30, the last 13 h ago. "
      "Words like \"today\" or \"tomorrow\" in them meant that day, not this one.", line)
check("history: today only, no note", api._history_when([{"role": "user", "content": "a", "at": ts - 300}])
      == "Earlier messages in this chat: the last 5 min ago.", api._history_when([{"role": "user", "content": "a", "at": ts - 300}]))
check("history: milliseconds too", api._history_when([{"role": "user", "content": "a", "at": (ts - 300) * 1000}])
      == "Earlier messages in this chat: the last 5 min ago.")
check("history: no times, no line", api._history_when([{"role": "user", "content": "a"}]) is None)
check("history: junk ignored", api._history_when([{"role": "user", "content": "a", "at": "soon"}, "x"]) is None)

check("history: a time out of range never fails the turn",
      api._history_when([{"role": "user", "content": "a", "at": 1.7e18}, {"role": "user", "content": "b", "at": float("inf")}]) is None)

# Across a DST change (Nov 1 2026, 02:00 PDT → 01:00 PST) "ago" counts real time.
dst_now = datetime(2026, 11, 1, 1, 30, tzinfo=local_time.zone(), fold=1)        # 01:30 PST
check("ago across DST: 01:30 PDT seen at 01:30 PST is an hour ago",
      local_time.ago(datetime(2026, 11, 1, 1, 30, tzinfo=local_time.zone(), fold=0).timestamp(), at=dst_now) == "1 h ago",
      local_time.ago(datetime(2026, 11, 1, 1, 30, tzinfo=local_time.zone(), fold=0).timestamp(), at=dst_now))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
