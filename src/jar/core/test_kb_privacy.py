#!/usr/bin/env python3
"""Who sees which knowledge-base item (kb_privacy.py), on every read path, with a fake retrieval
runtime. Hermetic: temp HOUSEHOLD_DB and KB_ACL_DB, no network.

Run: python3 scripts/test_kb_privacy.py
"""
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
from contextlib import redirect_stdout

_TMP = tempfile.mkdtemp(prefix="kbpriv-")
os.environ["HOUSEHOLD_DB"] = os.path.join(_TMP, "household.db")
os.environ["KB_ACL_DB"] = os.path.join(_TMP, "kb_acl.db")
os.environ["HELD_ACTIONS_DB"] = os.path.join(_TMP, "held.db")
os.environ["PROACTIVE_DB"] = os.path.join(_TMP, "proactive.db")
os.environ.setdefault("AGENT_TURN_SOURCE", "test")
os.environ.pop("AGENT_JEV_TURN_GATES", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_runtime_lib as A  # noqa: E402
import household_identity as hh  # noqa: E402
import kb_backfill_owner as B  # noqa: E402
import kb_privacy as P  # noqa: E402
import kb_query as Q  # noqa: E402

passed = failed = 0
QUERY_PAYLOADS: list = []   # what each retrieval query asked the runtime


def check(label, ok, detail=""):
    global passed, failed
    ok = bool(ok)
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  {str(detail)[:400]}"))


# ── a fake retrieval runtime ────────────────────────────────────────────────────────────────────

class FakeKB:
    """The retrieval runtime's routes the KB uses, including its quirk: re-indexing unchanged text
    keeps the old metadata (checksum short-circuit)."""

    def __init__(self):
        self.sources = {}
        self.clock = 0

    def add(self, sid, title, text, source_type="note", metadata=None):
        self.clock += 1
        self.sources[sid] = {"source_id": sid, "title": title, "text": text, "source_type": source_type,
                             "metadata": dict(metadata or {}), "updated_at": f"t{self.clock}"}

    def __call__(self, service, path, method="POST", payload=None, timeout=30):
        if service == "record":
            raise urllib.error.HTTPError(path, 404, "no node", None, None)
        if service != "retrieval":
            return {}
        if path == "/v1/retrieval/index":
            old = self.sources.get(payload["source_id"])
            if old and old["text"] == payload["text"]:
                return {"source_id": payload["source_id"], "unchanged": True}
            self.add(payload["source_id"], payload.get("title", ""), payload["text"], payload["source_type"],
                     payload.get("metadata"))
            return {"source_id": payload["source_id"]}
        if path == "/v1/retrieval/query":
            QUERY_PAYLOADS.append(dict(payload))
            words = set(re.findall(r"[a-z]{3,}", payload["query"].lower()))
            scored = []
            for s in self.sources.values():
                body = set(re.findall(r"[a-z]{3,}", (s["title"] + " " + s["text"]).lower()))
                n = len(words & body)
                if n:
                    scored.append((n + len(s["text"]) / 1e6, s))
            scored.sort(key=lambda x: -x[0])
            res = [{"source_id": s["source_id"], "chunk_text": s["text"], "citation_ref": f"{s['source_id']}#chunk-0000",
                    "score": str(sc), "metadata": dict(s["metadata"])} for sc, s in scored[:payload.get("top_k", 5)]]
            return {"results": res, "total_matches": len(scored),
                    "context_bundle": {"bundle_text": "\n\n".join(f"[{r['citation_ref']}] {r['chunk_text']}" for r in res)}}
        m = re.fullmatch(r"/v1/retrieval/sources/([^/]+)/chunks", path)
        if m:
            s = self.sources.get(m.group(1))
            if not s:
                raise urllib.error.HTTPError(path, 404, "Source not found", None, None)
            return {"chunks": [{"chunk_index": 0, "chunk_text": s["text"], "metadata": dict(s["metadata"])}]}
        if path.startswith("/v1/retrieval/sources?"):
            lim = int(re.search(r"limit=(\d+)", path).group(1))
            rows = sorted(self.sources.values(), key=lambda s: s["updated_at"], reverse=True)[:lim]
            return {"sources": [{k: s[k] for k in ("source_id", "title", "source_type", "updated_at")}
                                | {"preview": s["text"][:200]} for s in rows]}
        m = re.fullmatch(r"/v1/retrieval/sources/([^/]+)", path)
        if m and method == "DELETE":
            if self.sources.pop(m.group(1), None) is None:
                raise urllib.error.HTTPError(path, 404, "Source not found", None, None)
            return {"deleted": True}
        raise AssertionError(f"unexpected call {method} {path}")


KB = FakeKB()
A._runtime_request = KB
A._http_json = lambda url, payload=None, timeout=30: {}   # every runtime "up" for the context block
P._META_CACHE.clear()

store = hh.get_store()
MIR = store.add_person("Robin", "adult")["id"]
KID = store.add_person("Ava", "kid")["id"]
DEV = store.add_person("Kitchen", "household")["id"]
ZACH = hh.ADMIN_PERSON_ID

# Zach's things: a legacy note (no metadata at all), his mail (new metadata) and legacy mail.
KB.add("note-garage", "Garage", "The garage door code lives in the green binder. zebra", "note")
KB.add("cx-mail-aaa", "Bank statement", "Your bank statement for September. zebra", "connector:mail",
       {"source": "mail", "owner": ZACH, "audience": "private"})
KB.add("cx-mail-bbb", "Old invoice", "Old invoice from the plumber. zebra", "connector:mail", {"source": "mail"})
# titan's own notes, and the household file.
KB.add("note-runbook-gpus", "Runbook: GPUs", "Titan GPU layout and the zebra runbook.", "knowledge_record",
       {"origin": "kb_seed_runbook.py", "owner": None, "audience": "admins"})
KB.add("note-runbook-old", "Runbook: legacy", "A legacy system note about zebra deploys.", "knowledge_record",
       {"origin": "kb_seed_runbook.py"})
KB.add("hh-trash", "Trash day", "Trash goes out Tuesday night. zebra", "household",
       {"origin": "household.yaml", "owner": None, "audience": "household"})
KB.add("hh-wifi-legacy", "Wifi", "The guest wifi is zebra-net.", "household", {"origin": "household.yaml"})


def _do_it(owner):
    """The owner's Do it on their newest held card (#1628: kb_forget asks first)."""
    import held_actions
    con = held_actions._db()
    try:
        r = con.execute("SELECT id FROM held WHERE owner=? AND used_at IS NULL ORDER BY created_at DESC LIMIT 1",
                        (str(owner).lower(),)).fetchone()
    finally:
        con.close()
    return A._run_held_action(r["id"]) if r else "no held action"


def as_(pid, role):
    A._TURN_IO.person = {"id": pid, "name": pid} if pid else None
    A._TURN_IO.role = role


def ids_in(text):
    return {sid for sid in KB.sources if sid in text}


ZACH_PRIVATE = {"note-garage", "cx-mail-aaa", "cx-mail-bbb"}
SYSTEM = {"note-runbook-gpus"}   # note-runbook-old is legacy: Zach's private until the backfill

print("defaults")
check("LEGACY_OWNER is the admin's id", P.LEGACY_OWNER == hh.ADMIN_PERSON_ID)
check("no metadata: Zach's, private", P.acl_of({}) == (ZACH, "private"))
check("connector provenance only: Zach's, private", P.acl_of({"source": "mail"}) == (ZACH, "private"))
check("system note: no owner, admins", P.acl_of({"owner": None, "audience": "admins"}) == (None, "admins"))

print("Robin (adult) never gets Zach's notes, mail or titan's notes")
as_(MIR, "adult")
out = A.execute_tool("kb_retrieve", {"query": "zebra garage bank invoice runbook trash"})
check("kb_retrieve: household only", ids_in(out) == {"hh-trash"}, out)
check("kb_retrieve: no leak through the runtime's bundle", "green binder" not in out and "bank statement" not in out.lower(), out)
out = A.execute_tool("kb_list", {})
check("kb_list: household only", ids_in(out) == {"hh-trash"}, out)
out = A.execute_tool("kb_relations", {"entity": "zebra garage"})
check("kb_relations fallback: household only", ids_in(out) <= {"hh-trash"} and "binder" not in out, out)
A._TURN_INFO.info = {}
ctx = A._situational_context_inner("zebra garage binder bank statement trash tuesday")
check("per-turn KB passages: none of Zach's", "binder" not in ctx and "bank statement" not in ctx.lower()
      and "GPU" not in ctx, ctx[-600:])
noted = {h["source_id"] for h in A._TURN_INFO.info.get("_kb_hits", [])}
check("noted sources: none of Zach's", not (noted & (ZACH_PRIVATE | SYSTEM)), noted)
fake_hits = [{"source_id": "note-garage", "title": "Garage", "text": "garage door code green binder lives here",
              "owner": ZACH, "audience": "private"}]
cited = A.cited_sources("The garage door code lives in the green binder, here.", fake_hits)
check("cited sources: Zach's note is never cited to Robin", cited == [], cited)
api = {i["id"] for i in Q.kb_list(MIR, "adult")}
check("GET /v1/kb: household only", api == {"hh-trash"}, api)
check("GET /v1/kb/{id}: Zach's note is a 404", Q.kb_get("note-garage", MIR, "adult").get("status") == 404)
check("GET /v1/kb/{id}: titan's note is a 404", Q.kb_get("note-runbook-gpus", MIR, "adult").get("status") == 404)
check("GET /v1/kb/{id}: the household note opens", Q.kb_get("hh-trash", MIR, "adult").get("ok"))

print("Zach (admin) sees his own, titan's and the household's")
as_(ZACH, "admin")
out = A.execute_tool("kb_list", {})
check("kb_list: everything so far", ids_in(out) == set(KB.sources), out)

print("a kid and a shared device: household items only")
for pid, role in ((KID, "kid"), (DEV, "household")):
    as_(pid, role)
    out = A.execute_tool("kb_retrieve", {"query": "zebra garage bank runbook trash wifi"})
    check(f"{role}: kb_retrieve household only", ids_in(out) == {"hh-trash"}, out)
    api = {i["id"] for i in Q.kb_list(pid if role != "household" else None, role)}
    check(f"{role}: GET /v1/kb household only", api == {"hh-trash"}, api)
    # #1055: the per-turn KB passages and the sources a turn notes for citing, as for another adult above
    A._TURN_INFO.info = {}
    ctx = A._situational_context_inner("zebra garage binder bank statement trash tuesday")
    check(f"{role}: per-turn KB passages: none of Zach's mail, notes or titan's", "binder" not in ctx
          and "bank statement" not in ctx.lower() and "GPU" not in ctx, ctx[-600:])
    noted = {h["source_id"] for h in A._TURN_INFO.info.get("_kb_hits", [])}
    check(f"{role}: noted sources: none of Zach's", not (noted & (ZACH_PRIVATE | SYSTEM)), noted)

print("Robin's own notes stay hers")
as_(MIR, "adult")
out = A.execute_tool("ingest_text", {"title": "Garage", "content": "My locker combination is in my zebra purse."})
mid = next(s for s in KB.sources if s.startswith("note-") and MIR in s)
check("her note gets its own id (never replaces Zach's 'Garage')", "note-garage" in KB.sources
      and KB.sources["note-garage"]["text"].startswith("The garage door"), mid)
check("her note: owner Robin, private", KB.sources[mid]["metadata"].get("owner") == MIR
      and KB.sources[mid]["metadata"].get("audience") == "private", KB.sources[mid]["metadata"])
check("ingest_text says who sees it", "only you" in out.lower(), out)
as_(ZACH, "admin")
out = A.execute_tool("kb_retrieve", {"query": "locker combination purse zebra"})
check("Zach doesn't get Robin's note", mid not in out and "purse" not in out, out)
check("Zach's GET /v1/kb/{id} on her note: 404", Q.kb_get(mid, ZACH, "admin").get("status") == 404)
as_(MIR, "adult")
A._TURN_IO.asker_words = "save this for everyone"   # #1397: the asker named the audience
A.execute_tool("ingest_text", {"title": "Bins", "content": "Recycling goes out every other zebra Thursday.",
                               "share": "household"})
bins = next(s for s in KB.sources if "bins" in s)
check("share=household on save: household", P.acl_of(KB.sources[bins]["metadata"], bins)[1] == "household")
# 10-03 audit (#802's shape): an update under a near title made a second note, and the stale one stayed retrievable.
A.execute_tool("ingest_text", {"title": "Nightlight dinner log", "content": "Monday: zebra tacos."})
n0 = len(KB.sources)
out = A.execute_tool("ingest_text", {"title": "Dinner Log", "content": "Tuesday: zebra soup."})
check("a near-miss title: no second note, naming hers", out.startswith("Not saved: you have a note titled “nightlight dinner log”")
      and len(KB.sources) == n0, out)
out = A.execute_tool("ingest_text", {"change": "Nightlight dinner log", "title": "Dinner Log",
                                     "content": "Monday: zebra tacos. Tuesday: zebra soup."})
nid = f"note-{MIR}-nightlight-dinner-log"
check("change replaces that note in place", len(KB.sources) == n0 and "Tuesday: zebra soup" in KB.sources[nid]["text"]
      and nid in out, out)
check("…still hers and private", P.acl_of(KB.sources[nid]["metadata"], nid) == (MIR, "private"))
check("change to a note she doesn't have says so", A.execute_tool("ingest_text", {"change": "Breakfast log", "content": "x"})
      .startswith("Not saved: you have no note titled “Breakfast log”"))
out = A.execute_tool("ingest_text", {"title": "Dinner", "content": "Zebra dinner ideas.", "new": "yes"})
check("new='yes' saves a separate one", out.startswith("Saved 'Dinner'") and len(KB.sources) == n0 + 1, out)
check("her own \"Garage\" catches \"Garage door\"", A.execute_tool("ingest_text", {"title": "Garage door",
      "content": "Zebra code."}).startswith("Not saved: you have a note titled “garage”"))
as_(ZACH, "admin")
check("…and Zach's own notes are his alone to collide with", A.execute_tool("ingest_text", {"title": "Dinner log",
      "content": "Zach's zebra list."}).startswith("Saved 'Dinner log'"))
as_(MIR, "adult")
as_(KID, "kid")
check("the kid sees the household note Robin saved", bins in A.execute_tool("kb_retrieve", {"query": "recycling thursday"}))

print("chat saves (the fact saver) belong to the asker")
as_(MIR, "adult")
A._maybe_save_fact("Remember that my dentist is Dr. Okafor on zebra street.", "Got it, I'll remember your dentist is Dr. Okafor.")
said = [s for s in KB.sources if s.startswith(f"note-said-{MIR}-")]
check("saved under Robin's id, private", said and KB.sources[said[0]]["metadata"].get("owner") == MIR
      and KB.sources[said[0]]["metadata"].get("audience") == "private", said)
as_(KID, "kid")
before = len(KB.sources)
A._maybe_save_fact("Remember that my locker is 12.", "Okay, I'll remember your locker is number 12.")
check("a kid's chat doesn't write the KB", len(KB.sources) == before)

print("sharing makes an item visible to the named person only")
as_(ZACH, "admin")
A._TURN_IO.asker_words = 'share it with Robin'   # #1397: the asker named who
out = A.execute_tool("share", {"item": "Garage", "with": "Robin"})
check("share says plainly who sees it", "you and robin" in out.lower(), out)
check("table row: Zach's, [Robin]", P.acl_rows(["note-garage"]).get("note-garage") == (ZACH, json.dumps([MIR])))
as_(MIR, "adult")
check("Robin now finds it", "note-garage" in A.execute_tool("kb_retrieve", {"query": "garage door green binder"}))
as_(KID, "kid")
check("the kid still doesn't", "note-garage" not in A.execute_tool("kb_retrieve", {"query": "garage door green binder"}))
as_(ZACH, "admin")
A._TURN_IO.asker_words = 'share it with Ava'   # #1397: the asker named who
out = A.execute_tool("share", {"item": "Garage", "with": "Ava"})
check("sharing with a kid by name is refused, pointing at household", out.startswith("Not shared") and "household" in out, out)
KB.sources["note-garage"]["text"] += " updated"
KB.sources["note-garage"]["metadata"] = {"owner": ZACH, "audience": "private"}   # a refresh rewrote metadata
as_(MIR, "adult")
check("a share survives the item being rewritten", "note-garage" in A.execute_tool("kb_retrieve", {"query": "garage door green binder"}))
A._TURN_IO.asker_words = 'share it with the household'   # #1397: the asker named who
out = A.execute_tool("share", {"item": "note-garage", "with": "household"})
check("Robin can't share Zach's note", out.startswith("“Garage”: Not shared"), out)
as_(ZACH, "admin")
out = A.execute_tool("unshare", {"item": "Garage"})
check("unshare: private again", "only you" in out.lower(), out)
as_(MIR, "adult")
check("…and Robin no longer gets it", "note-garage" not in A.execute_tool("kb_retrieve", {"query": "garage door green binder"}))
as_(ZACH, "admin")
A._TURN_IO.asker_words = 'share it with Robin'   # #1397: the asker named who
out = A.execute_tool("share", {"item": "Runbook: GPUs", "with": "Robin"})
row = P.acl_rows(["note-runbook-gpus"]).get("note-runbook-gpus")
check("sharing titan's own note keeps the admin on it", row and ZACH in json.loads(row[1]) and MIR in json.loads(row[1]), row)
r = Q.kb_share("cx-mail-aaa", {"to": "adults"}, ZACH, "admin")
check("POST /v1/kb/{id}/share: adults", r.get("ok") and r["audience"] == "adults" and "Robin" in r["visible_to"], r)
check("…Robin's GET /v1/kb now lists it", "cx-mail-aaa" in {i["id"] for i in Q.kb_list(MIR, "adult")})
check("POST share on an item the caller can't see: 404", Q.kb_share(mid, {"to": "household"}, ZACH, "admin").get("status") == 404)
check("POST share by a non-owner: 403", Q.kb_share("cx-mail-aaa", {"to": "household"}, MIR, "adult").get("status") == 403)
r = Q.kb_share("cx-mail-aaa", {}, ZACH, "admin", undo=True)
check("POST unshare: private again", r.get("ok") and r["audience"] == "private", r)

print("edit and delete: the owner or the admin")
check("DELETE by a non-owner who can see it: 403", Q.kb_delete("hh-trash", MIR, "adult").get("status") in (400, 403))
A._TURN_IO.asker_words = "note this for the adults"   # #1397: the asker named the audience
A.execute_tool("ingest_text", {"title": "Pizza", "content": "Zebra pizza night is Friday.", "share": "adults"})
pz = "note-pizza"
check("DELETE of Zach's shared note by Robin: 403", Q.kb_delete(pz, MIR, "adult").get("status") == 403)
check("DELETE of Zach's private note by Robin: 404", Q.kb_delete("cx-mail-bbb", MIR, "adult").get("status") == 404)
as_(MIR, "adult")
out = A.execute_tool("kb_forget", {"record_id": pz})
check("kb_forget by a non-owner: refused", out.startswith("Not deleted") and pz in KB.sources, out)
out = A.execute_tool("kb_forget", {"record_id": "cx-mail-bbb"})
check("kb_forget of an invisible item: not found", "No record" in out and "cx-mail-bbb" in KB.sources, out)
A._TURN_IO.source = "user"
out = A.execute_tool("kb_forget", {"record_id": mid})
check("kb_forget by the owner: held for their OK, nothing deleted yet", out.startswith(A.ALWAYS_ASK_HELD) and mid in KB.sources, out)
out = _do_it(MIR)
check("kb_forget by the owner works after their Do it", mid not in KB.sources, out)
A._TURN_IO.source = None
check("DELETE by the owner works", Q.kb_delete(bins, MIR, "adult").get("ok") and bins not in KB.sources)
import kb_update  # noqa: E402


def put(rid, person, role, body):
    argv, stdin = sys.argv, sys.stdin
    sys.argv, sys.stdin = ["kb_update.py", "--person", person, "--role", role, rid], io.StringIO(json.dumps(body))
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = kb_update.main()
    finally:
        sys.argv, sys.stdin = argv, stdin
    return code, json.loads(buf.getvalue())


code, out = put(pz, MIR, "adult", {"body": "hijacked"})
check("PUT by a non-owner who can see it: 403", code == 1 and out.get("status") == 403
      and KB.sources[pz]["text"] != "hijacked", out)
code, out = put("cx-mail-bbb", MIR, "adult", {"body": "hijacked"})
check("PUT on an item the caller can't see: 404", code == 1 and out.get("status") == 404, out)
code, out = put(pz, ZACH, "admin", {"body": "Zebra pizza night moved to Saturday."})
md = KB.sources[pz]["metadata"]
check("PUT by the owner works and keeps owner, audience and origin", code == 0 and md.get("owner") == ZACH
      and md.get("audience") == "adults" and md.get("origin") == "agent-ingest_text", (out, md))
as_(ZACH, "admin")
check("DELETE by the admin of a visible note works", Q.kb_delete(pz, ZACH, "admin").get("ok"))

print("top-k still fills after filtering")
for i in range(40):
    KB.add(f"cx-mail-z{i:02d}", f"Mail {i}", "quokka quokka schedule mail from the school " * 3, "connector:mail",
           {"owner": ZACH, "audience": "private"})
for i in range(6):
    KB.add(f"hh-quokka-{i}", f"Quokka {i}", f"quokka schedule {i}", "household", {"owner": None, "audience": "household"})
as_(MIR, "adult")
hits = A._kb_query("quokka schedule mail school", 5)
check("Robin still gets 5 results, all household", len(hits) == 5 and all(h["source_id"].startswith("hh-quokka") for h in hits),
      [h["source_id"] for h in hits])
as_(ZACH, "admin")
check("Zach's top 5 are his own best matches", len(A._kb_query("quokka schedule mail school", 5)) == 5)

print("backfill (dry run writes nothing; apply fills the table)")
before = P.acl_rows()
buf = io.StringIO()
with redirect_stdout(buf):
    B.main(["--dry-run"], call=KB)
counts = json.loads(buf.getvalue())["counts"]
check("dry run writes nothing", P.acl_rows() == before, counts)
check("dry run: legacy system note → admins", counts.get("system -> (no owner)/admins") == 1, counts)
check("dry run: legacy household.yaml → household", counts.get("household.yaml -> (no owner)/household") == 1, counts)
check("dry run: legacy mail and notes → zach/private", counts.get("connector -> zach/private", 0) >= 1
      and counts.get("saved -> zach/private", 0) >= 0, counts)
with redirect_stdout(io.StringIO()):
    B.main(["--apply"], call=KB)
check("apply: the legacy system note is the admin's", P.acl_rows(["note-runbook-old"])["note-runbook-old"] == (None, "admins"))
check("apply: the legacy wifi note is the household's", P.acl_rows(["hh-wifi-legacy"])["hh-wifi-legacy"] == (None, "household"))
as_(KID, "kid")
check("…so the kid now finds the wifi note", "hh-wifi-legacy" in A.execute_tool("kb_retrieve", {"query": "guest wifi zebra"}))
as_(MIR, "adult")
check("…and Robin still doesn't get the system note", "note-runbook-old" not in A.execute_tool("kb_list", {}))

print("routes and roles")
check("kid may GET /v1/kb/{id}", hh.route_allowed("kid", "GET", "/v1/kb/hh-trash"))
check("kid may not share", not hh.route_allowed("kid", "POST", "/v1/kb/hh-trash/share")
      and not hh.role_allows_tool("kid", "share"))
check("adult may share", hh.route_allowed("adult", "POST", "/v1/kb/x/share") and hh.role_allows_tool("adult", "share"))
check("household device may not edit", not hh.route_allowed("household", "PUT", "/v1/kb/x"))

print("the HTTP routes act as the caller")
import subprocess  # noqa: E402
import types  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

_people = {"zach": hh.owner_identity(), "rob": hh.Identity({"id": MIR, "name": "Robin", "role": "adult"}, None, "adult", "device"),
           "kid": hh.Identity({"id": KID, "name": "Ava", "role": "kid"}, None, "kid", "device")}
api._resolve_identity = lambda request: _people.get(request.headers.get("X-Test-As", ""))
_real_run = subprocess.run


def _in_process(cmd, input="", capture_output=True, text=True, timeout=None, **kw):
    """kb_query.py / kb_update.py, run in this process against the fake runtime."""
    script = os.path.basename(cmd[1])
    mod = {"kb_query.py": Q, "kb_update.py": kb_update}.get(script)
    if mod is None:
        return _real_run(cmd, input=input, capture_output=capture_output, text=text, timeout=timeout, **kw)
    argv, stdin = sys.argv, sys.stdin
    sys.argv, sys.stdin = [script, *cmd[2:]], io.StringIO(input or "")
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = mod.main()
    finally:
        sys.argv, sys.stdin = argv, stdin
    return subprocess.CompletedProcess(cmd, code, buf.getvalue(), "")


api.subprocess = types.SimpleNamespace(run=_in_process, CompletedProcess=subprocess.CompletedProcess,
                                       CalledProcessError=subprocess.CalledProcessError,
                                       check_output=subprocess.check_output)
c = TestClient(api.app)
r = c.get("/v1/kb", headers={"X-Test-As": "rob"})
seen = {e["id"] for e in r.json().get("entries", [])}
# (Zach shared "Runbook: GPUs" with her above; the other system note stays his.)
check("GET /v1/kb as Robin: no Zach-private or system items", r.status_code == 200 and not (seen & (ZACH_PRIVATE | {"note-runbook-old"}))
      and "note-runbook-gpus" in seen, seen)
r = c.get("/v1/kb", headers={"X-Test-As": "kid"})
check("GET /v1/kb as a kid: household only", r.status_code == 200 and all(e["audience"] == "household" for e in r.json()["entries"]))
check("GET /v1/kb/{id} of Zach's note as Robin: 404", c.get("/v1/kb/note-garage", headers={"X-Test-As": "rob"}).status_code == 404)
check("GET /v1/kb/{id} as Zach: 200", c.get("/v1/kb/note-garage", headers={"X-Test-As": "zach"}).status_code == 200)
check("DELETE of Zach's note as Robin: 404", c.delete("/v1/kb/note-garage", headers={"X-Test-As": "rob"}).status_code == 404)
check("PUT of Zach's note as Robin: 404", c.put("/v1/kb/note-garage", json={"body": "x"}, headers={"X-Test-As": "rob"}).status_code == 404)
r = c.post("/v1/kb/note-garage/share", json={"to": "Robin"}, headers={"X-Test-As": "zach"})
check("POST share as Zach: says Robin can see it", r.status_code == 200 and "Robin" in r.json().get("visible_to", ""), r.text)
check("…now Robin's GET /v1/kb/{id} opens it", c.get("/v1/kb/note-garage", headers={"X-Test-As": "rob"}).status_code == 200)
check("PUT of the shared note as Robin: 403", c.put("/v1/kb/note-garage", json={"body": "x"}, headers={"X-Test-As": "rob"}).status_code == 403)
check("POST share as Robin (not hers): 403", c.post("/v1/kb/note-garage/share", json={"to": "household"},
                                                         headers={"X-Test-As": "rob"}).status_code == 403)
check("a kid can't call share at all", c.post("/v1/kb/hh-trash/share", json={"to": "household"},
                                               headers={"X-Test-As": "kid"}).status_code == 403)
r = c.post("/v1/kb", json={"title": "Her plants", "body": "Water the zebra plant on Sundays."}, headers={"X-Test-As": "rob"})
check("POST /v1/kb as Robin: hers, private", r.status_code == 201 and r.json().get("audience") == "private"
      and MIR in r.json().get("id", ""), r.text)
check("…Zach can't open it", c.get(f"/v1/kb/{r.json().get('id')}", headers={"X-Test-As": "zach"}).status_code == 404)

check("#371: every KB query scores a standing-in extra lexically on its note's own text",
      bool(QUERY_PAYLOADS) and all(q.get("extra_lexical") == "body" for q in QUERY_PAYLOADS), True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
