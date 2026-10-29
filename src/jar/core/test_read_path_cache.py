#!/usr/bin/env python3
# gate: hermetic
"""The read path's caches (10-10: /v1/items p50 316 ms → 2.2 s, the canary ~2.3 s → ~11 s): the household timeline,
record_nodes(type) and the KB's titles are read from the runtimes once per TTL and shared by every viewer as RAW rows,
each caller filtering for its own viewer on every call; the coordinator key is parsed once and a token reused until a
minute before it expires. Proved mechanically against a stub runtime that counts requests (the real _runtime_request,
a throwaway Ed25519 key), plus: a cached read the admin filled never shows an adult, a kid or a shared screen anything
it wouldn't have shown them fresh. Temporary stores; fictional people.
Run: TZ=UTC python3 scripts/test_read_path_cache.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-read-cache-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "MAIL_SERVER_DB": "mail_server.db", "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt",
             "WORLD_DIR": "world", "WORLD_DB": "world.db", "SPACES_DB": "spaces.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("AGENT_FENCES", None)
os.environ.pop("COORDINATOR_PRIVATE_KEY", None)

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

KEY_FILE = _TMP / "coordinator.pem"


def write_key() -> Ed25519PrivateKey:
    k = Ed25519PrivateKey.generate()
    KEY_FILE.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    return k


KEY = write_key()
os.environ["COORDINATOR_PRIVATE_KEY_FILE"] = str(KEY_FILE)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import linker  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


ADMIN = hh.ADMIN_PERSON_ID
NOW = time.time()


def iso(ago_h: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - ago_h * 3600))


def ev(i: int, text: str, person: str | None, aud: str | None, ago_h: float, etype: str = "household.note",
       **pl) -> dict:
    p = {"text": text, **({"person": person} if person else {}), **({"audience": aud} if aud else {}), **pl}
    return {"event_id": f"e{i}", "event_type": etype, "timestamp": iso(ago_h), "payload": p, "stream_sequence": seq()}


_SEQ = [0]


def seq() -> int:
    """The runtime's own row id: the cursor pages by it (never by position), so a removed row moves nothing."""
    _SEQ[0] += 1
    return _SEQ[0]


# The stream, oldest first as the runtime keeps it. Admin's private and adults-only entries, Robin's private one, the
# household's; three are from the last 48 hours.
EVENTS = [ev(1, "Admin's private tax note", ADMIN, "private", 200),
          ev(2, "Trash went out", ADMIN, "household", 150),
          ev(3, "Robin's private diary line", "robin", "private", 100),
          ev(4, "Adults: the mortgage call", ADMIN, "adults", 90),
          ev(5, "Admin's private gift idea for Ava", ADMIN, "private", 10),
          ev(6, "Leo's soccer moved to Hale Park", "alex", "household", 5),
          ev(7, "Alex's private pharmacy pickup", "alex", "private", 1)]
NODES = {"bill": [{"node_id": "bill-admin", "node_type": "bill",
                   "data": {"title": "Admin's private card bill", "vendor": "Pine Card",
                            "provenance": {"owner": ADMIN, "audience": "private"}}},
                  {"node_id": "bill-house", "node_type": "bill",
                   "data": {"title": "Harbor Water", "vendor": "Harbor Water",
                            "provenance": {"owner": ADMIN, "audience": "household"}}},
                  {"node_id": "bill-robin", "node_type": "bill",
                   "data": {"title": "Robin's phone bill", "vendor": "Jordan Mobile",
                            "provenance": {"owner": "robin", "audience": "private"}}}],
         "receipt": []}
SOURCES = [{"source_id": "note-wifi", "title": "Wi-Fi for guests", "source_type": "note"},
           {"source_id": "note-gift", "title": "Gift ideas for Ava", "source_type": "note"}]
PAGE = 3              # the stub's page size (the runtime's is 500), so paging and cursors are exercised
REQS: list[tuple[str, str]] = []
AUTH: list[str] = []
LOCK = threading.Lock()


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _note(self) -> tuple[str, dict]:
        u = urllib.parse.urlsplit(self.path)
        with LOCK:
            REQS.append((self.command, self.path))
            AUTH.append(self.headers.get("Authorization", ""))
        return u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    def do_GET(self):
        path, q = self._note()
        if path == "/v1/events/streams/household-timeline":
            after = int(q.get("cursor", 0))
            page = [e for e in EVENTS if e["stream_sequence"] > after][:PAGE]
            self._send({"events": page, "next_cursor": page[-1]["stream_sequence"] if len(page) == PAGE else None,
                        "stream_stats": {"event_count": len(EVENTS)}})
        elif path == "/v1/nodes":
            self._send({"nodes": [] if q.get("after") else NODES.get(q.get("node_type", ""), []), "next": None})
        elif path == "/v1/retrieval/sources":
            self._send({"sources": SOURCES if q.get("offset", "0") == "0" else []})
        else:
            self._send({})

    def do_POST(self):
        path, _q = self._note()
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if path == "/v1/events":
            EVENTS.append({"event_id": f"e{len(EVENTS) + 1}", "event_type": body.get("event_type"),
                           "timestamp": iso(0), "payload": body.get("payload") or {}, "stream_sequence": seq()})
        self._send({})

    def do_PATCH(self):
        self._note()
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send({})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
for svc in ("event", "record", "retrieval"):
    lib._RUNTIME_BASES[svc] = BASE
reads = {"key": 0}
_real_key = lib._coordinator_private_key


def counting_key() -> bytes:
    reads["key"] += 1
    return _real_key()


lib._coordinator_private_key = counting_key


def n_reqs(fn):
    """(result, the requests it made)."""
    before = len(REQS)
    out = fn()
    return out, REQS[before:]


def as_viewer(pid: str, role: str) -> None:
    lib._TURN_IO.person = {"id": pid}
    lib._TURN_IO.role = role


def stale_all() -> None:
    """Every cache entry past its TTL, as if the time had passed (nothing written)."""
    for held in lib._TIMELINE.values():
        held["at"] -= 10 ** 6
    for k, v in list(lib._RECORDS.items()):
        lib._RECORDS[k] = (v[0] - 10 ** 6, *v[1:])
    lib._KB_TITLES["at"] = 1e-9


ids = lambda evs: [e.get("event_id") or e.get("id") for e in evs]  # noqa: E731

print("── the timeline: read once per TTL, every viewer filtered on every call ──")
got, r = n_reqs(lib._timeline_events)
check("the first read pages the stream (7 events, 3 a page: 3 requests)", len(r), 3)
check("…all of it, oldest first", ids(got), [f"e{i}" for i in range(1, 8)])
got, r = n_reqs(lib._timeline_events)
check("a second read within the TTL makes no request", (len(r), ids(got)), (0, [f"e{i}" for i in range(1, 8)]))
got[0]["payload"]["text"] = "changed by a caller"
got[1]["event_id"] = "changed"
check("a caller changing its copy never changes the cache",
      (lib._timeline_events()[0]["payload"]["text"], lib._timeline_events()[1]["event_id"]),
      ("Admin's private tax note", "e2"))

print("\n── one cache, four viewers: a hit serves each only what a fresh read would ──")
as_viewer(ADMIN, "admin")
admin_view, r = n_reqs(lib._visible_events)
check("the admin's turn: their private, adults' and household entries, and nobody else's private",
      (ids(admin_view), len(r)), (["e1", "e2", "e4", "e5", "e6"], 0))
as_viewer("robin", "adult")
check("an adult (Robin) right after, from the same cache: hers, adults', household; none of the admin's private",
      ids(lib._visible_events()), ["e2", "e3", "e4", "e6"])
as_viewer("ava", "kid")
check("a kid (Ava): the household's only (no adults-only, no one's private)", ids(lib._visible_events()), ["e2", "e6"])
as_viewer("household", "household")
check("a shared screen: the household's only", ids(lib._visible_events()), ["e2", "e6"])
lib._read_cache_on, _on = (lambda: False), lib._read_cache_on
fresh = {}
for pid, role in ((ADMIN, "admin"), ("robin", "adult"), ("ava", "kid"), ("household", "household")):
    as_viewer(pid, role)
    fresh[role] = ids(lib._visible_events())
lib._read_cache_on = _on
cached = {}
for pid, role in ((ADMIN, "admin"), ("robin", "adult"), ("ava", "kid"), ("household", "household")):
    as_viewer(pid, role)
    cached[role] = ids(lib._visible_events())
check("…exactly what each would get with no cache at all", cached, fresh)

print("\n── GET /v1/timeline (timeline_for): newest first, visibility checked only as far as the limit ──")
for pid, role, want in ((ADMIN, "admin", ["e6", "e5", "e4"]), ("robin", "adult", ["e6", "e4", "e3"]),
                        ("ava", "kid", ["e6", "e2"]), ("household", "household", ["e6", "e2"])):
    got, r = n_reqs(lambda: lib.timeline_for(pid, role, limit=3))
    check(f"{role}: their newest three from the admin-filled cache, no request", ([e["id"] for e in got], len(r)),
          (want, 0))
seen = []
_vis = lib._event_visible
lib._event_visible = lambda e, p, ro: (seen.append(e["event_id"]), _vis(e, p, ro))[1]
lib.timeline_for("ava", "kid", limit=1)
lib._event_visible = _vis
check("a kid's limit=1: checked newest first and stopped at the first they may see", seen, ["e7", "e6"])
check("…and the full list for a kid matches a check of every entry",
      [e["id"] for e in lib.timeline_for("ava", "kid", limit=200)], ["e6", "e2"])
check("before= still pages back", [e["id"] for e in lib.timeline_for(ADMIN, "admin", before=iso(50))],
      ["e4", "e2", "e1"])

print("\n── the situational context: the 48-hour cut comes before the fence check ──")
seen = []
lib._event_visible = lambda e, p, ro: (seen.append(e["event_id"]), _vis(e, p, ro))[1]
as_viewer("robin", "adult")
recent = lib._visible_events(since=NOW - 48 * 3600)
lib._event_visible = _vis
check("only the last 48 hours are checked (3 of 7), and Robin sees the household's of them",
      (sorted(seen), ids(recent)), (["e5", "e6", "e7"], ["e6"]))

print("\n── staleness: a TTL, a write clears it, the next read is the stream's tail only ──")
EVENTS.append(ev(8, "Jordan dropped off the casserole", "alex", "household", 0.5))
EVENTS.append(ev(9, "Hale Park field closed", "alex", "household", 0.2))
got, r = n_reqs(lib._timeline_events)
check("another process's append is not read inside the TTL", (len(r), ids(got)[-1]), (0, "e7"))
stale_all()
got, r = n_reqs(lib._timeline_events)
check("past the TTL: only from the last page on (2 requests, both from a cursor; the stream from its start: 4)",
      (len(r), all("cursor=" in p for _m, p in r)), (2, True))
check("…and the new entries are there", ids(got)[-2:], ["e8", "e9"])
as_viewer("alex", "adult")
lib._runtime_request("event", "/v1/events", "POST",
                     {"event_type": "household.retracted", "stream_key": "household-timeline",
                      "payload": {"retracts": "e9", "person": "alex", "audience": "household"}})
got, r = n_reqs(lib._timeline_events)
check("a write through _runtime_request: the next read (inside the TTL) reads the tail again and applies it",
      ("e9" in ids(got), len(r) >= 1), (False, True))
EVENTS.pop(1)     # not what an append-only stream does: the runtime's own count says so
stale_all()
got, r = n_reqs(lib._timeline_events)
check("an event count that disagrees: the whole stream is read again", (any("cursor=" not in p for _m, p in r),
                                                                         "e2" in ids(got)), (True, False))
EVENTS.insert(1, {**ev(2, "Trash went out", ADMIN, "household", 150), "stream_sequence": 2})

print("\n── a stand-in runtime is never cached ──")
calls = {"n": 0}
_rr = lib._runtime_request
lib._runtime_request = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), {"events": [EVENTS[1]]})[1]
lib._timeline_events()
lib._timeline_events()
lib.record_nodes("bill")
lib.record_nodes("bill")
lib._runtime_request = _rr
check("two timeline reads and two record reads: four requests", calls["n"], 4)

print("\n── record_nodes(type): one read per TTL, every viewer filtered after ──")
lib._reads_changed("record")
(nodes, complete), r = n_reqs(lambda: lib.record_nodes("bill"))
check("the first read asks the runtime", (len(r), len(nodes), complete), (1, 3, True))
(nodes2, _c), r = n_reqs(lambda: lib.record_nodes("bill"))
check("a second read within the TTL makes no request", (len(r), [n["node_id"] for n in nodes2]),
      (0, ["bill-admin", "bill-house", "bill-robin"]))
nodes2[0]["data"]["provenance"]["audience"] = "household"
check("a caller changing its copy never changes the cache",
      lib.record_nodes("bill")[0][0]["data"]["provenance"]["audience"], "private")


def bills(pid, role):
    return [n["node_id"] for t in ("bill", "receipt") for n in lib.record_nodes(t)[0] if lib._node_visible(n, pid, role)]


check("the admin's bills fill it: their own and the household's", bills(ADMIN, "admin"), ["bill-admin", "bill-house"])
_, r = n_reqs(lambda: (bills("robin", "adult"), bills("ava", "kid"), bills("household", "household")))
check("…then an adult, a kid and a screen read the same cache (no request)", len(r), 0)
check("…Robin: hers and the household's, never the admin's private card bill", bills("robin", "adult"),
      ["bill-house", "bill-robin"])
check("…a kid: the household's only", bills("ava", "kid"), ["bill-house"])
check("…a shared screen: the household's only", bills("household", "household"), ["bill-house"])
NODES["bill"][1]["data"]["provenance"]["audience"] = "private"
lib._runtime_request("record", "/v1/nodes/bill-house", "PATCH", {"data": {"provenance": {"audience": "private"}}})
check("an audience narrowed through _runtime_request applies at once: the kid no longer sees it",
      bills("ava", "kid"), [])
NODES["bill"][1]["data"]["provenance"]["audience"] = "household"
lib._reads_changed("record")

print("\n── titles: only the ids asked for, from short caches ──")
m = sqlite3.connect(os.environ["MAIL_SERVER_DB"])
m.execute("CREATE TABLE messages (item_id TEXT, subject TEXT, code INTEGER)")
m.executemany("INSERT INTO messages VALUES (?, ?, 0)", [(f"mail-{i}", f"Subject {i}") for i in range(50)])
m.commit()
m.close()
got, r = n_reqs(lambda: linker.titles_for(["note-wifi", "bill-house", "mail-7", "nobody"]))
check("titles_for: a KB title, a bill's, a mail subject; nothing for an unknown id",
      got, {"note-wifi": "Wi-Fi for guests", "bill-house": "Harbor Water", "mail-7": "Subject 7"})
check("…one KB listing and one read per record type (bill, receipt), whatever the ids",
      sorted(p.split("?")[0] for _m, p in r), ["/v1/nodes", "/v1/nodes", "/v1/retrieval/sources"])
got, r = n_reqs(lambda: linker.titles_for(["note-gift", "mail-9"]))
check("…the next call within the TTL makes no request", (got, len(r)),
      ({"note-gift": "Gift ideas for Ava", "mail-9": "Subject 9"}, 0))
full, r = n_reqs(lambda: linker.labels({})[0])
check("labels() still has the whole house's titles (thread names weigh phrases against all of them), from the caches",
      (len(full), full.get("mail-49"), full.get("note-gift"), len(r)), (55, "Subject 49", "Gift ideas for Ava", 0))
check("titles_for gives each id the title labels() would",
      linker.titles_for(["note-wifi", "bill-robin", "mail-3"]),
      {k: full[k] for k in ("note-wifi", "bill-robin", "mail-3")})
lib.feed_sources_changed()
_, r = n_reqs(lambda: linker.titles_for(["note-wifi"]))
check("a saved note (feed_sources_changed) reads the KB titles again", [p.split("?")[0] for _m, p in r],
      ["/v1/retrieval/sources"])

print("\n── the coordinator key and token ──")
lib._COORD_KEY.update(fp=None, key=None)
lib._COORD_TOKENS.clear()
reads["key"] = 0
del AUTH[:]
for _ in range(6):
    lib._runtime_request("record", "/v1/nodes?node_type=contact&limit=500", "GET")
check("six requests: the key file read once", reads["key"], 1)
check("…and one token for all six", len(set(AUTH)), 1)
tok = AUTH[-1].split(" ", 1)[1]
h, c, sig = tok.split(".")
import base64  # noqa: E402
claims = json.loads(base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)))
KEY.public_key().verify(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)), f"{h}.{c}".encode())
check("…a valid EdDSA coordinator token with more than the reuse margin left",
      (claims["type"], claims["role"], claims["exp"] - int(time.time()) > lib.TOKEN_REUSE_MARGIN_S),
      ("coordinator", "coordinator", True))
time.sleep(0.01)
KEY = write_key()
os.utime(KEY_FILE, ns=(time.time_ns(), time.time_ns() + 1_000_000))
lib._runtime_request("record", "/v1/nodes?node_type=contact&limit=500", "GET")
tok2 = AUTH[-1].split(" ", 1)[1]
h2, c2, sig2 = tok2.split(".")
KEY.public_key().verify(base64.urlsafe_b64decode(sig2 + "=" * (-len(sig2) % 4)), f"{h2}.{c2}".encode())
check("a rotated key file (new mtime): read again, and a new token signed with the new key",
      (reads["key"], tok2 != tok), (2, True))
lib.TOKEN_REUSE_MARGIN_S = 10 ** 6
lib._runtime_request("record", "/v1/nodes?node_type=contact&limit=500", "GET")
time.sleep(1.1)       # a new second: a token minted fresh has its own iat (Ed25519 signs deterministically)
lib._runtime_request("record", "/v1/nodes?node_type=contact&limit=500", "GET")
check("a token inside the margin is never reused (a fresh one each), the key still not re-read",
      (len({a for a in AUTH[-2:]}), reads["key"]), (2, 2))
lib.TOKEN_REUSE_MARGIN_S = 60
check("another coordinator id gets its own token", lib._mint_coordinator_token("alex-agent") != lib._mint_coordinator_token("robin-agent"), True)
os.environ["COORDINATOR_PRIVATE_KEY_FILE"] = str(_TMP / "missing.pem")
try:
    lib._mint_coordinator_token("titan-local-agent")
    check("a missing key file still refuses", "minted", "refused")
except RuntimeError as exc:
    check("a missing key file still refuses (no cached key or token is used)", "unreadable" in str(exc), True)
os.environ["COORDINATOR_PRIVATE_KEY_FILE"] = str(KEY_FILE)

srv.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
