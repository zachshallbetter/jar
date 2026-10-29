#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A person's YouTube liked videos and named playlists, read-only (titan-node#470), against a fake YouTube API, with
fictional videos only: paging, deleted/private videos skipped, a video in two lists kept once, anything added more
than a year ago left out, a video no longer liked pruned through the runner's deletion path, nothing changed on a
failed page, the source turned off writes nothing. Read-only: only GETs and the token refresh leave. Watch Later is
refused by name (the API returns it empty). The one-time sign-in writes the refresh token into the 0600 account
file and never shows it. Off until an account file exists.
Run: python3 scripts/test_youtube_sync.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-youtube-"))
for k, v in {"CONNECTORS_LEDGER": "ledger.db", "HOUSEHOLD_DB": "household.db", "YOUTUBE_ACCOUNTS_DIR": "accounts",
             "AGENT_MEMORY_DIR": "memory", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))

import connectors_runner as cx  # noqa: E402
import youtube_sync as Y  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def it(vid, title, added, channel="Quarry Cooking"):
    return {"snippet": {"title": title, "publishedAt": added, "videoOwnerChannelTitle": channel,
                        "description": "SECRET-DESCRIPTION-TEXT (the uploader's words)", "resourceId": {"videoId": vid}},
            "contentDetails": {"videoId": vid}}


LISTS = {
    "LL": [it("aaaaaaaaaa1", "Sourdough in an hour", "2026-10-01T09:00:00Z"),
           it("aaaaaaaaaa2", "Fixing a leaky tap", "2026-09-20T09:00:00Z", "Slate DIY"),
           it("aaaaaaaaaa3", "Deleted video", "2026-09-10T09:00:00Z"),
           it("aaaaaaaaaa4", "Private video", "2026-09-09T09:00:00Z"),
           it("aaaaaaaaaa5", "Old favourite", "2025-06-01T09:00:00Z")],      # more than a year ago
    "PLlumen0000watchlater1": [it("aaaaaaaaaa2", "Fixing a leaky tap", "2026-09-25T09:00:00Z", "Slate DIY"),
                               it("aaaaaaaaaa6", "Bike gears explained", "2026-10-02T09:00:00Z", "Fern Cycles")],
}
CALLS: list[tuple[str, str]] = []
FAIL_PAGE = {"on": False}
REFRESH = "rt-fictional-TULIP"


def fake_http(method, url, data, headers):
    CALLS.append((method, url.split("?")[0]))
    if url == Y.TOKEN_URL:
        form = dict(urllib.parse.parse_qsl(data.decode()))
        if form.get("refresh_token") == REFRESH:
            return {"access_token": "at-fictional"}
        if form.get("code") == "code-ok":
            return {"refresh_token": REFRESH}
        return {}
    assert headers.get("Authorization") == "Bearer at-fictional"
    q = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
    items = LISTS[q["playlistId"]]
    start = int(q.get("pageToken") or 0)
    if FAIL_PAGE["on"] and start:
        raise Y.YouTubeError("YouTube answered HTTP 503")
    out = {"items": items[start:start + 2]}
    if start + 2 < len(items):
        out["nextPageToken"] = str(start + 2)
    return out


ACCTS = Path(os.environ["YOUTUBE_ACCOUNTS_DIR"])
ROLES = {"alex": "admin", "robin": "adult", "ava": "kid"}


def account_file(name, owner="robin", **extra):
    ACCTS.mkdir(mode=0o700, exist_ok=True)
    p = ACCTS / f"{name}.json"
    p.write_text(json.dumps({"owner": owner, "client_id": "cid-fictional", "client_secret": "cs-fictional", **extra}))
    os.chmod(p, 0o600)
    return p


WRITES: list[tuple[str, str, str]] = []


def runtime(service, path, method, payload):
    WRITES.append((service, method, path))
    return {"id": "n-1", "node_id": "n-1"}


LEDGER: dict[str, set[str]] = {"robin": set()}


def run(**kw):
    return Y.sync(connect=lambda a: fake_http, runtime=runtime, role_of=ROLES.get, now=NOW,
                  keys=lambda owner: set(LEDGER.get(owner, set())), **kw)


print("── off until an account file exists ──")
check("no account files: nothing", run(), {})

print("\n── account files ──")
p = account_file("youtube", playlists=["LL", "PLlumen0000watchlater1"])
a = Y.load_account(p, ROLES.get)
check("an account: lists, no token until sign-in", (a["playlists"], a["refresh_token"]), (["LL", "PLlumen0000watchlater1"], ""))
for extra, why in (({"playlists": ["WL"]}, "Watch Later"), ({"playlists": ["../x"]}, "playlists"),
                   ({"client_secret": ""}, "client_secret")):
    q = account_file("bad", **extra)
    try:
        Y.load_account(q, ROLES.get)
        check(f"refused: {why}", "loaded", "refused")
    except Y.AccountError as exc:
        check(f"refused: {why}", why in str(exc), True)
    q.unlink()
q = account_file("kid", owner="ava")
try:
    Y.load_account(q, ROLES.get)
    check("a kid's account: refused", "loaded", "refused")
except Y.AccountError:
    check("a kid's account: refused", True, True)
q.unlink()
os.chmod(p, 0o644)
check("a file others can read: refused, with a reason", "0600" in run()["youtube"]["error"], True)
os.chmod(p, 0o600)
check("not signed in yet: skipped with a reason", "not signed in" in run()["youtube"]["error"], True)

print("\n── the one-time sign-in ──")
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
shown: list[str] = []
res: dict = {}
t = threading.Thread(target=lambda: res.update(ok=Y.auth(p, http=fake_http, port=port, wait=10, show=shown.append)))
t.start()
while not shown:
    pass
state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(shown[0]).query))["state"]
urllib.request.urlopen(f"http://127.0.0.1:{port}/?state={state}&code=code-ok", timeout=5).read()
t.join(15)
check("signed in: the token is in the 0600 file", (res.get("ok"), json.loads(p.read_text())["refresh_token"],
                                                   oct(p.stat().st_mode & 0o777)), (True, REFRESH, "0o600"))
check("…the link asks for youtube.readonly only, and shows no token",
      ("youtube.readonly" in shown[0], "scope=" + urllib.parse.quote(Y.SCOPE, safe="") in shown[0], REFRESH in shown[0]),
      (True, True, False))

print("\n── the sync ──")
CALLS.clear()
dry = run(dry_run=True)["youtube"]
check("dry run: counts, nothing written", (dry["videos"], dry["new"], dry["older_than_keep"], WRITES), (3, 3, 1, []))
out = run()["youtube"]
check("written: 3 videos (deleted/private skipped, the year-old one left out, the shared one once)",
      (out["videos"], out["ledger"]["written"], out["ledger"]["errors"]), (3, 3, 0))
con = cx._db()
rows = {r[0]: json.loads(r[1] or "{}") for r in con.execute("SELECT upstream_id, fields FROM items WHERE source='youtube'")} \
    if "fields" in [c[1] for c in con.execute("PRAGMA table_info(items)")] else {}
con.close()
check("…as the person's own items (the ledger has them, owner robin)", sorted(Y._ledger_keys("robin")),
      ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa6"])
store = b"".join(x.read_bytes() for x in _TMP.rglob("*.db"))
check("the uploader's description is kept nowhere", b"SECRET-DESCRIPTION-TEXT" in store, False)
check("only GETs and the token refresh left", sorted({m for m, u in CALLS}), ["GET", "POST"])
check("…every POST was the token refresh", {u for m, u in CALLS if m == "POST"}, {Y.TOKEN_URL})
merged = Y.item({"video_id": "aaaaaaaaaa2", "title": "t", "channel": "c", "url": "u", "added_at": "x",
                 "lists": ["LL", "PLlumen0000watchlater1"]})
check("a video in two lists is one item listing both", merged["fields"]["lists"], ["LL", "PLlumen0000watchlater1"])

print("\n── pruning ──")
LEDGER["robin"] = {"aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa6", "bbbbbbbbbb9"}   # bbbbbbbbbb9: unliked since
WRITES.clear()
out = run()["youtube"]
check("a video no longer in any list goes through the deletion path", (out["removed"], out["ledger"]["removed"] >= 0),
      (1, True))
FAIL_PAGE["on"] = True
check("a failed page changes nothing (and says so)", "503" in run()["youtube"]["error"], True)
FAIL_PAGE["on"] = False
cx.set_sources_off("robin", ["youtube"])
check("the source turned off for the person: nothing written", (run()["youtube"].get("off"), ), (True,))

print("\n── read-only ──")
yt = Y.YouTube({"name": "x", "client_id": "c", "client_secret": "s", "refresh_token": REFRESH}, fake_http)
for meth, url in (("POST", Y.API + "/playlistItems"), ("DELETE", Y.API + "/playlistItems?id=x"),
                  ("PUT", Y.API + "/videos/rate"), ("GET", "https://evil.example/x")):
    try:
        yt._http(meth, url, None, {})
        check(f"{meth} {url.split('/')[-1][:20]}: refused", "sent", "refused")
    except PermissionError:
        check(f"{meth} {url.split('/')[-1][:20]}: refused", True, True)

print("\n── the daily timer ──")
_SETUP = Path(__file__).resolve().parent.parent / "server-setup"
unit = (_SETUP / "titan-youtube-sync.service").read_text()
check("the unit runs sync, off until an account file exists",
      ("youtube_sync.py sync" in unit, "ConditionPathExistsGlob=/ai/secrets/youtube/*.json" in unit, "--dry-run" in unit),
      (True, True, False))
timer = (_SETUP / "titan-youtube-sync.timer").read_text()
import re as _re  # noqa: E402
from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo as _Z  # noqa: E402
hh, mm = map(int, _re.search(r"OnCalendar=\*-\*-\* (\d\d):(\d\d):00 America/Los_Angeles", timer).groups())
utc = [(_dt(2026, mo, 6, hh, mm, tzinfo=_Z("America/Los_Angeles")).astimezone(_Z("UTC"))) for mo in (1, 7)]
mins = [u.hour * 60 + u.minute for u in utc]
check("daily, clear of the eval windows (03:25–03:50Z, 08:35–09:05Z) in summer and winter, with its 5 min jitter",
      any(lo - 5 <= m + d <= hi for m in mins for d in (0, 5) for lo, hi in ((205, 230), (515, 545))), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
