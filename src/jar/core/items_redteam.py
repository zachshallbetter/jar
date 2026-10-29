#!/usr/bin/env python3
"""The items surface's privacy red team, nightly (Coordination, 10-07): GET /v1/items for every view × viewer.

A fictional household (the Hales: Leo the admin, Alex and Robin and Jordan adults, Ava a kid, a living-room screen) is
seeded into throwaway stores in a child process: to-dos, scheduled actions, file references, chats, KB notes, triaged
mail, tasks, private projects and a household project with private items filed into it. Never a real store: every
store path points into a temp dir, and the KB, task and record runtimes are in-process fakes.

Two checks per viewer, over every view (now, time, unsorted, person:<each>, project:<each>) and the other per-viewer
lists the apps read (projects, proposals, cards, to-dos, scheduled, references, documents, household people):
  canaries   another person's private titles carry a marker that viewer must never see, anywhere in the JSON
             (members, slots, sentence, next, people, counts, reasons, ids)
  noninterference  the viewer's view rendered from the full household equals the same view rendered with everything
             they may not see removed from the stores. Any difference is information about hidden items reaching
             them: a count, a "3 things wait", a next step, a name in a sentence ("Jordan drives" from Alex's texts in
             the mock), an order. Ids are compared through the titles they belong to (stores number items differently
             once some are missing); timestamps and fingerprints are dropped.

Prints "items privacy leaks: N/M" (the nightly's line; any N > 0 pages like world privacy) and appends one line to
--log. Counts and fictional names only.

  python3 scripts/items_redteam.py [--source manual|nightly] [--log /ai/logs/items-redteam.jsonl]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
VIEWERS = ("admin", "alex", "kid", "screen")
PEOPLE = (("alex", "Alex Hale", "adult"), ("robin", "Robin Hale", "adult"), ("jordan", "Jordan Hale", "adult"),
          ("ava", "Ava Hale", "kid"))
CANARY = {k: f"zq{k}priv" for k in ("leo", "alex", "robin", "jordan", "ava")}
# who each viewer is, as a person key ("screen": nobody, household only)
VIEWER_PERSON = {"admin": "leo", "alex": "alex", "kid": "ava", "screen": None}


def items_for() -> list[dict[str, Any]]:
    """The fixture: every item with the people who may see it. "sees": person keys; "*" everyone incl. the screen."""
    out: list[dict[str, Any]] = []
    for p in ("leo", "alex", "robin", "jordan", "ava"):
        c = CANARY[p]
        own = {p}
        out += [
            {"kind": "todo", "owner": p, "title": f"{c} todo: Jordan drives Ava to soccer", "sees": own},
            {"kind": "scheduled", "owner": p, "title": f"{c} reminder: pay the fence quote", "sees": own},
            {"kind": "chat", "owner": p, "title": f"{c} chat about the weekend", "sees": own, "project": f"{p} project"},
            {"kind": "chat", "owner": p, "title": f"{c} chat, unfiled", "sees": own},
            {"kind": "note", "owner": p, "title": f"{c} note", "sees": own},
            {"kind": "mail", "owner": p, "title": f"{c} mail from the school", "sees": own},
            {"kind": "task", "owner": p, "title": f"{c} task", "sees": own},
            {"kind": "project", "owner": p, "title": f"{p} project", "sees": own},
            {"kind": "card", "owner": p, "title": f"{c} card: the fence quote is due", "sees": own},
        ]
        if p != "ava":
            out.append({"kind": "reference", "owner": p, "title": f"{c} file", "sees": own})
    out += [
        {"kind": "todo", "owner": "household", "title": "Bins out tonight", "sees": {"*"}},
        {"kind": "scheduled", "owner": "alex", "title": "Water the plants", "sees": {"*"}, "audience": "household"},
        {"kind": "note", "owner": "alex", "title": "Wifi for guests", "sees": {"*"}, "audience": "household"},
        {"kind": "card", "owner": "alex", "title": "Bins go out tonight", "sees": {"*"}, "audience": "household"},
        # a household project of Alex's, with Alex's private to-do and a household to-do filed into it: the others see
        # the project and the household item, never the private one, its count or its words
        {"kind": "project", "owner": "alex", "title": "Hale garden", "sees": {"leo", "alex", "robin", "jordan", "ava"},
         "audience": "household"},
        {"kind": "file", "item": f"{CANARY['alex']} todo: Jordan drives Ava to soccer", "project": "Hale garden",
         "sees": {"alex"}},
        {"kind": "file", "item": "Bins out tonight", "project": "Hale garden", "sees": {"*"}},
    ]
    return out


def visible(item: dict[str, Any], viewer: str) -> bool:
    s = item["sees"]
    who = VIEWER_PERSON[viewer]
    return "*" in s or (who is not None and who in s)


# ── the child: seed, render ───────────────────────────────────────────────────────────────────────────────────────────

# Any other store-like setting: pointed into the child's own directory too (10-09: the red team set 25 stores and the
# rest, tasks, grants, surfaces, chores and more, reached the child from the gate's shared TEST_STATE, or on titan's
# nightly from the live defaults). The gate's own export block names every store it isolates; we isolate all of them.
_STORE_SUFFIX = re.compile(r"(_DB|_DIR|_LOG|_CACHE|_FILE|_JSONL|_LEDGER|_BANK|_CASES|_AUDIT|_STATE|_ROOT|_OUT|_PREFS)$")


def _gate_store_names() -> set[str]:
    """Every store the deploy gate gives a throwaway path (its export block's NAME="$TEST_STATE/…")."""
    try:
        text = (Path(__file__).resolve().parent / "deploy_titan_node.sh").read_text()
    except OSError:
        return set()
    start = text.find("export DAILY_LOG_DIR=")
    if start < 0:
        return set()
    block = []
    for ln in text[start:].splitlines():
        block.append(ln)
        if not ln.rstrip().endswith("\\"):
            break
    return set(re.findall(r'\b([A-Z][A-Z0-9_]*)="?\$\{?TEST_STATE\b', "\n".join(block)))


def _isolate(tmp: Path, explicit: dict[str, str]) -> None:
    """This child's stores: the named ones, then every other store-like setting, all under tmp (never a shared or live
    path, set or defaulted)."""
    for k, v in explicit.items():
        os.environ[k] = str(tmp / v)
    other = tmp / "other"
    other.mkdir(exist_ok=True)
    for k in (_gate_store_names() | {k for k in os.environ if _STORE_SUFFIX.search(k)}) - set(explicit):
        path = other / k.lower()
        if k.endswith("_DIR") or k.endswith("_ROOT"):
            path.mkdir(exist_ok=True)
        os.environ[k] = str(path)


def _child(omit_for: str | None, render_for: list[str], now_ts: float) -> dict[str, Any]:
    tmp = Path(tempfile.mkdtemp(prefix="titan-items-redteam-"))
    _isolate(tmp, {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
                 "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
                 "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db",
                 "SCHEDULED_DB": "scheduled.db", "REFERENCES_DB": "references.db", "WORLD_DIR": "world",
                 "SITTER_DB": "sitter.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db", "PUSH_DB": "push.db",
                 "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
                 "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt", "FENCES_DB": "fences.db",
                 "MEMORY_DB": "memory.db", "TITLES_DB": "titles.db"})
    os.environ["MEMORY_HYBRID"] = "0"
    for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE", "CONV_CONTINUE"):
        os.environ.pop(k, None)
    # Person and device ids come from secrets: the same sequence in every build, so ids line up across builds
    import secrets
    n = [0]

    def det(nbytes: int = 16) -> str:
        n[0] += 1
        return f"{n[0]:0{max(2, (nbytes or 16) * 2)}x}"[-max(2, (nbytes or 16) * 2):]
    secrets.token_hex = det
    secrets.token_urlsafe = lambda nbytes=16: "t" + det(nbytes)
    sys.path.insert(0, str(HERE))
    import titan_alert
    titan_alert.alert = lambda *a, **k: None
    from fastapi.testclient import TestClient
    import agent_api_server as api
    import agent_runtime_lib as lib
    import household_identity as hh
    import kb_privacy
    import projects
    import references
    import scheduled
    import spaces
    import todos
    api._AGENT_TOKEN = "master-redteam-token-0123456789"
    hh.reset_store(hh.HouseholdStore(tmp / "api.db"))
    c = TestClient(api.app)
    M = {"X-Agent-Token": api._AGENT_TOKEN}
    try:
        hh.get_store().update_person(hh.ADMIN_PERSON_ID, name="Leo Hale")
    except Exception:  # noqa: BLE001 - the admin keeps its seeded name; ids are what matter
        pass

    def pair(name, role, device, platform="ios"):
        s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
        cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
        return {"X-Agent-Token": cl["token"]}, s["person"]["id"]
    H: dict[str, Any] = {"leo": (M, hh.ADMIN_PERSON_ID, "admin")}
    for key, name, role in PEOPLE:
        h, pid = pair(name, role, f"{name.split()[0]}'s iPhone")
        H[key] = (h, pid, role)
    tv_h, _ = pair("Living room", "household", "Living room TV", platform="tvos")
    c.patch(f"/v1/people/{H['ava'][1]}", json={"parents": [H["robin"][1], H["alex"][1]]}, headers=M)
    pid_of = {k: v[1] for k, v in H.items()}
    role_of = {k: v[2] for k, v in H.items()}
    # the clock: one "now" for every build (the parent's), so time-relative groups agree
    now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
    lib._local_now = lambda: now_dt
    tomorrow = now_ts + 86400

    keep = [it for it in items_for() if omit_for is None or visible(it, omit_for)]
    KB: dict[str, dict] = {}
    TASKS: list[dict] = []
    titles: dict[str, str] = {}      # title → items-API id, for filing
    proj_ids: dict[str, str] = {}
    mem = api._get_memory()
    for it in keep:
        k, o, t = it["kind"], it.get("owner"), it.get("title", "")
        if k == "project":
            pr = spaces.create_project(pid_of[o], role_of[o], t, audience=it.get("audience"))
            proj_ids[t] = pr["id"]
    for it in keep:
        k, o, t = it["kind"], it["owner"] if "owner" in it else None, it.get("title", "")
        opid = pid_of.get(o) if o else None
        if k == "todo":
            r = todos.add(t, "household" if o == "household" else opid, created_by=o)
            titles[t] = f"todo:{r['id']}"
        elif k == "scheduled":
            # each at its own minute: a tie is broken by store id, and ids differ between builds (an order that came
            # from that would read as a leak; 10-07)
            scheduled.add(opid, t, "notify", {"text": "x"}, tomorrow + (sum(map(ord, t)) % 600) * 60,
                          audience=it.get("audience", "private"))
        elif k == "reference":
            references.put(opid, role_of[o], f"dev-{o}", {"kind": "file", "native_id": f"n-{o}", "title": t})
        elif k == "chat":
            # a chat's time is its own (by title), not the moment this build happened to seed it
            import agent_memory
            real_time = agent_memory.time
            ts = now_ts - 3600 - (sum(map(ord, t)) % 360) * 60
            agent_memory.time = type("PinnedTime", (), {"time": staticmethod(lambda: ts)})
            try:
                mem.add_message(hh.conv_id_for(opid), "user", t, thread=f"th-{o}-{sum(map(ord, t))}")
            finally:
                agent_memory.time = real_time
            import conversations
            cid = next(x["id"] for x in conversations.list_for(mem._db, hh.conv_id_for(opid), 50) if x["title"] == t)
            titles[t] = f"chat:{cid}"
            if it.get("project") in proj_ids:
                spaces.tag(opid, cid, proj_ids[it["project"]], None)
        elif k == "note":
            sid = f"note-{abs(hash(t)) % 10**8}"
            KB[sid] = {"source_id": sid, "title": t, "source_type": "note",
                       "metadata": {"owner": opid, "audience": it.get("audience", "private")}}
            kb_privacy.set_acl(sid, opid, it.get("audience", "private"), by=opid)
        elif k == "mail":
            sid = f"cx-mail-{abs(hash(t)) % 10**8}"
            KB[sid] = {"source_id": sid, "title": t, "source_type": "connector:mail", "owner": opid,
                       "metadata": {"owner": opid, "audience": "private", "saved": now_dt.isoformat()}}
            kb_privacy.set_acl(sid, opid, "private", by=opid)
        elif k == "task":
            TASKS.append({"id": f"t-{o}", "goal": t, "status": "running", "owner": opid, "updated_at": "1",
                          "moved_at": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "steps": [{"title": "a", "state": "done"}, {"title": "b", "state": "current"}]})
    import proactive
    for it in keep:
        if it["kind"] == "card":
            to = ["household"] if it.get("audience") == "household" else [pid_of[it["owner"]]]
            proactive.add_card(it["title"], "", source="redteam", to=to, key=f"rt-{sum(map(ord, it['title']))}")
    for it in keep:
        if it["kind"] == "file" and it["item"] in titles and it["project"] in proj_ids:
            owner = next((x["owner"] for x in items_for() if x.get("title") == it["item"]), None)
            who = pid_of["alex"] if owner in (None, "household") else pid_of[owner]
            projects.file(who, "adult", titles[it["item"]], proj_ids[it["project"]])
    lib.kb_sources = lambda *a, **k: [dict(v) for v in KB.values()]
    lib._runtime_request = lambda svc, path, method="GET", payload=None, timeout=30: {
        "chunks": [{"chunk_index": 0, "chunk_text": "x",
                    "metadata": KB.get(path.split("/sources/")[1].split("/")[0], {}).get("metadata", {})}]}
    lib.record_nodes = lambda t, **k: ([], True)
    api._lib_cli = lambda action, who, role, timeout: json.dumps({"tasks": [x for x in TASKS if who != "-" and x["owner"] == who]})

    # --inject (the red team's own self-test): a deliberate leak, so a run that finds nothing is known to be able to
    #   todos   every to-do list to everyone (a projection bypass: canaries and noninterference both fire)
    #   count   Now's sentence counts every to-do in the house as waiting (a derived number only: no hidden string)
    #   cards   every card to everyone (the cards list beside the items surface)
    inject = os.environ.get("ITEMS_REDTEAM_INJECT", "")
    if inject == "todos":
        all_lists = sorted({r[0] for r in todos._db().execute("SELECT DISTINCT owner FROM todos")}) if hasattr(todos, "_db") else []
        todos.lists_visible = lambda pid, role, _all=all_lists: _all
    elif inject == "cards":
        proactive.visible_to = lambda to, person, role: True     # every card to everyone
    elif inject == "count":
        import items as _items
        real_sentence = _items.now_sentence

        def counting(r, call_me, now, tz):
            out = real_sentence(r, call_me, now, tz)
            hidden = todos._db().execute("SELECT COUNT(*) FROM todos").fetchone()[0] if hasattr(todos, "_db") else 0
            out["facts"] = {**out.get("facts", {}), "waiting_on_you": out.get("facts", {}).get("waiting_on_you", 0) + hidden}
            return out
        _items.now_sentence = counting
    heads = {"admin": M, "alex": H["alex"][0], "kid": H["ava"][0], "screen": tv_h}
    today = now_dt.date()
    views = ["now", "unsorted", f"time&from={today}&to={today + timedelta(days=6)}"] + \
        [f"person:{pid_of[k]}" for k in ("leo", "alex", "robin", "jordan", "ava")] + \
        [f"project-name:{name}" for name in sorted({x["title"] for x in items_for() if x["kind"] == "project"})]
    out: dict[str, Any] = {}
    others = ("/v1/projects", "/v1/projects/proposals", "/v1/proactive", "/v1/todos", "/v1/scheduled", "/v1/references",
              "/v1/documents", "/v1/household/people")
    for v in render_for:
        out[v] = {}
        for path in others:          # the other per-viewer lists the apps read beside /v1/items (10-07)
            r = c.get(path, headers=heads[v])
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            out[v][path] = {"status": r.status_code, "body": generic(body, proj_ids) if r.status_code == 200 else None,
                            "raw": r.text}
        for view in views:
            q = view
            if view.startswith("project-name:"):
                pname = view.split(":", 1)[1]
                if pname not in proj_ids:
                    out[v][view] = {"status": 404}     # it doesn't exist in this build: must look like one they can't see
                    continue
                q = f"project:{proj_ids[pname]}"
            r = c.get(f"/v1/items?view={q}", headers=heads[v])
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            out[v][view] = {"status": r.status_code, "body": canonical(body, proj_ids) if r.status_code == 200 else None,
                            "raw": r.text}
    return out


_DROP = ("resolutionId", "fingerprint", "generated_at", "revision")
_PATH_RX = re.compile(r'(/v1/(?:todos|scheduled|conversations|tasks|references|items|projects|kb)/)[^/"]+')


_ID_KEY = re.compile(r"(^|_)(id|ids|key|token|uuid|card_id|ref_id|conversation)$")
_TIME_KEY = re.compile(r"(^|_)(at|ts|now|time|created|updated|modified|expires|since|until|seen|last|run_at|due)(_at)?$")


def generic(body: Any, proj_ids: dict[str, str]) -> Any:
    """For bodies without a title beside every id (the other lists): ids and times blanked (stores number and stamp
    items differently per build), project ids by name, every list sorted. Counts, titles and names still compare."""
    names = {pid: "⟨project " + n + "⟩" for n, pid in proj_ids.items()}

    def walk(o: Any, key: str = "") -> Any:
        if isinstance(o, dict):
            return {k: walk(v, k) for k, v in o.items() if not _TIME_KEY.search(k.lower())}
        if isinstance(o, list):
            return sorted((walk(v, key) for v in o), key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
        if isinstance(o, str):
            if o in names:
                return names[o]
            if _ID_KEY.search(key.lower()):
                return "<id>"
        return o
    return walk(body)


def canonical(body: Any, proj_ids: dict[str, str]) -> Any:
    """Ids → the titles they belong to; timestamps and fingerprints dropped; groups' members and operations sorted."""
    ids: dict[str, str] = {}
    for g in body.get("groups", []) if isinstance(body, dict) else []:
        for m in g.get("members", []):
            sid = (m.get("subject") or {}).get("id")
            if sid:
                ids[sid] = "⟨" + str((m.get("slots") or {}).get("title") or "?") + "⟩"
    for x in ("waiting_on_you", "waiting_on_others", "moving", "next"):
        for b in (body.get(x) or []) if isinstance(body, dict) and isinstance(body.get(x), list) else []:
            if isinstance(b, dict) and b.get("id"):
                ids.setdefault(b["id"], "⟨" + str(b.get("title") or "?") + "⟩")
    for name, pid in proj_ids.items():
        ids[pid] = "⟨project " + name + "⟩"

    def strip(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in _DROP}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    s = json.dumps(strip(body), sort_keys=True, ensure_ascii=False)
    for k in sorted(ids, key=len, reverse=True):
        s = s.replace(k, ids[k])
    s = _PATH_RX.sub(r"\1<id>", s)
    o = json.loads(s)
    if isinstance(o, dict):
        for g in o.get("groups", []):
            g["members"] = sorted(g.get("members", []), key=lambda m: json.dumps(m, sort_keys=True))
        if isinstance(o.get("operations"), list):
            o["operations"] = sorted(o["operations"], key=lambda m: json.dumps(m, sort_keys=True))
        for x in ("dependencies",):
            if isinstance(o.get(x), list):
                o[x] = sorted(o[x], key=lambda m: json.dumps(m, sort_keys=True))
    return o


# ── the parent: builds, compare, score ────────────────────────────────────────────────────────────────────────────────

def _run_child(omit_for: str | None, render_for: list[str], now_ts: float) -> dict[str, Any]:
    p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", json.dumps(
        {"omit_for": omit_for, "render_for": render_for, "now": now_ts})], capture_output=True, text=True, timeout=900)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(f"build failed (exit {p.returncode}): {p.stderr.strip()[-400:]}")


def _diff(a: Any, b: Any, path: str = "") -> list[str]:
    if type(a) is not type(b):
        return [path or "/"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff(a.get(k), b.get(k), f"{path}/{k}") if k in a and k in b else [f"{path}/{k}"]
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path} (length {len(a)} vs {len(b)})"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in _diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [path or "/"]


def run(now_ts: float | None = None) -> dict[str, Any]:
    now_ts = float(now_ts if now_ts is not None else int(time.time() // 3600) * 3600 + 1800)
    full = _run_child(None, list(VIEWERS), now_ts)
    checks, leaks = 0, []
    for v in VIEWERS:
        never = [CANARY[p] for p in CANARY if p != VIEWER_PERSON[v]]
        reduced = _run_child(v, [v], now_ts)[v]
        for view, got in full[v].items():
            checks += 2
            hit = [s for s in never if s in (got.get("raw") or "")]
            if hit:
                leaks.append({"viewer": v, "view": view.split("&")[0], "check": "canary", "detail": sorted(hit)})
            r = reduced.get(view, {})
            if got.get("status") != r.get("status"):
                leaks.append({"viewer": v, "view": view.split("&")[0], "check": "noninterference",
                              "detail": [f"status {got.get('status')} vs {r.get('status')} without the hidden items"]})
            elif got.get("body") is not None:
                d = _diff(got["body"], r.get("body"))
                if d:
                    leaks.append({"viewer": v, "view": view.split("&")[0], "check": "noninterference", "detail": d[:6]})
    return {"checks": checks, "leaks": leaks}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="items surface privacy red team (counts and fictional names only)")
    ap.add_argument("--source", default="manual")
    ap.add_argument("--log")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.child:
        spec = json.loads(a.child)
        out = _child(spec["omit_for"], spec["render_for"], spec["now"])
        print(json.dumps(out, ensure_ascii=False))
        return 0
    try:
        r = run()
    except Exception as exc:  # noqa: BLE001 - no score is reported as such (the nightly treats it as unproven)
        print(f"items red team did not finish: {exc}")
        return 1
    n = len({(x["viewer"], x["view"]) for x in r["leaks"]})
    total = r["checks"] // 2
    for x in r["leaks"]:
        print(f"  {x['viewer']} · {x['view']} · {x['check']}: {', '.join(map(str, x['detail']))}")
    print(f"items privacy leaks: {n}/{total}")
    if a.log:
        fd = os.open(a.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "source": a.source,
                                 "leaks": n, "views": total, "detail": r["leaks"][:20]}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
