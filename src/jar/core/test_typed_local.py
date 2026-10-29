#!/usr/bin/env python3
# gate: hermetic   (a fake llama-server function; nothing is called over the network)
"""typed_local (#941): a schema-constrained answer, the ensure check, one retry told why, and how the three local
deciders (mail triage, mail bills, envelopes) go through it. Hermetic.

Run: python3 scripts/test_typed_local.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import typed_local as tl  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {detail}")


def fake(*replies, model="qwen-test"):
    calls = []

    def post(url, payload, timeout):
        calls.append(payload)
        return {"model": model, "choices": [{"message": {"content": replies[min(len(calls) - 1, len(replies) - 1)]}}]}
    return post, calls


S = tl.obj({"category": tl.enum(("a", "b"))}, required=("category",))
post, calls = fake('{"category": "a"}')
got, model = tl.ask("Sort it.", S, "http://127.0.0.1:8010", post=post)
check("a well-formed answer comes back parsed, with the server's model id", got == {"category": "a"} and model == "qwen-test")
rf = calls[0]["response_format"]
check("the request carries the schema as json_schema (llama-server makes it a grammar)",
      rf["type"] == "json_schema" and rf["json_schema"]["schema"] == S, json.dumps(rf))
check("no_think by default: the prompt says so and thinking is off",
      calls[0]["messages"][0]["content"].endswith(" /no_think") and calls[0]["chat_template_kwargs"] == {"enable_thinking": False})

post, calls = fake('{"summary": ""}', '{"summary": "A bill from Harbor Gas, $40 due Oct 9."}')
got, _ = tl.ask("Summarise.", tl.obj({"summary": {"type": "string"}}, ("summary",)), "u", post=post,
                ensure=lambda o: None if o["summary"].strip() else "the summary is empty")
check("an answer ensure rejects is retried once, told why", len(calls) == 2 and "the summary is empty" in
      calls[1]["messages"][-1]["content"] and got == {"summary": "A bill from Harbor Gas, $40 due Oct 9."}, str(calls[-1]["messages"]))
check("the retry carries the rejected answer as the assistant's turn", calls[1]["messages"][1]["role"] == "assistant")

post, calls = fake('{"summary": ""}')
got, _ = tl.ask("Summarise.", tl.obj({"summary": {"type": "string"}}), "u", post=post,
                ensure=lambda o: "empty" if not o.get("summary") else None)
check("two rejected answers → None (the caller's fallback runs), never a third call", got is None and len(calls) == 2)

post, calls = fake("<think>hm</think> Sure! {\"category\": \"b\"} hope that helps", )
got, _ = tl.ask("x", S, "u", post=post)
check("a thinking block and stray prose around the object are ignored", got == {"category": "b"})

post, calls = fake("not json at all")
got, _ = tl.ask("x", S, "u", post=post, retries=0)
check("no JSON object and no retries → None", got is None and len(calls) == 1)


def boom(url, payload, timeout):
    raise OSError("connection refused")
try:
    tl.ask("x", S, "u", post=boom)
    check("a server error is raised (the caller tries its next model)", False)
except OSError:
    check("a server error is raised (the caller tries its next model)", True)

# The three deciders, through the helper (their live paths; a fake server stands in for llama-server).
import mail_triage  # noqa: E402
import mail_bills  # noqa: E402
import envelopes  # noqa: E402

orig = tl._post
seen = []


def server(url, payload, timeout):
    name = payload["response_format"]["json_schema"]["name"]
    if name == "mail_category":
        return {"choices": [{"message": {"content": '{"category": "action"}'}}]}
    if name == "money_mail":
        return {"choices": [{"message": {"content": json.dumps({"type": "bill", "vendor": "Harbor Gas", "amount": 40.0,
                                                               "due_date": "2026-10-09"})}}]}
    return {"model": "qwen-test", "choices": [{"message": {"content": '{"summary": "Harbor Gas asks for $40 by Oct 9."}'}}]}


tl._post = server
try:
    check("mail triage: the category comes back through the enum", mail_triage.classify("Your bill", "Pay $40 by Oct 9") == "action")
    raw = mail_bills.local_llm("Below is one email …")
    f = mail_bills.extract("Your Harbor Gas bill", "Amount due $40.00 by 2026-10-09", lambda p: raw)
    check("mail bills: the schema answer still goes through extract()'s checks",
          bool(f) and f.get("doc_type") == "bill" and f.get("vendor") == "Harbor Gas", str(f))
    s, ev, how = envelopes.summarise("Harbor Gas", "Amount due $40 by Oct 9", "connector:mail", url="http://127.0.0.1:8010")
    check("envelopes: the summary comes back through the helper", how == "model" and "Harbor Gas" in s and ev == "model:qwen-test", f"{s} {ev} {how}")
finally:
    tl._post = orig

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
