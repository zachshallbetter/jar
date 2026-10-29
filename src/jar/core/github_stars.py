#!/usr/bin/env python3
"""The admin's GitHub stars from the last year, synced daily, for the github_stars tool (#707).

GET /user/starred with the star+json media type gives each star's starred_at. Stars come newest first,
100 a page; paging stops at the first star older than GITHUB_STARS_DAYS (365). Each repo keeps public
metadata only: full_name, html_url, description, language, topics, stargazers_count, pushed_at,
starred_at, archived. Nothing else from GitHub is kept (no README, no owner profile).

Where they go:
  - the connectors runner, source "github_stars": one record node per repo (find_records type=github_star)
    with provenance, owner the admin, audience private, source_id the repo's full_name. A repo
    un-starred since the last sync, or starred more than a year ago by now, goes through the runner's
    deletion path. Star counts and the last push change every day, so they stay out of the record (a
    re-sync with nothing else changed is a no-op in the ledger).
  - GITHUB_STARS_DB (SQLite, 600): the current set with every kept field, which the github_stars tool
    reads. Replaced after each complete sync.

Only a complete listing changes anything: a failed page leaves both as they were. With the source turned
off for the admin (connectors_off), the sync writes nothing and empties the tool's store. No token: says
so and exits 0.

  python3 scripts/github_stars.py [--dry-run]     (titan-github-stars.timer, daily 04:15 PT)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

SOURCE = "github_stars"
NODE_KIND = "github_star"
TOKEN_FILE = os.environ.get("GITHUB_TOKEN_FILE", "/ai/secrets/github_token")
DAYS = int(os.environ.get("GITHUB_STARS_DAYS", "365"))
PER_PAGE = 100
MAX_PAGES = 50                     # 5,000 stars in a year is far past anyone's; stop there and keep what's stored
BATCH = 500                        # connectors_runner.MAX_ITEMS
DESC_MAX = 300
_NAME_RX = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TOPIC_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")

Api = Callable[[str], "tuple[int, Any]"]


def db_path() -> Path:
    return Path(os.environ.get("GITHUB_STARS_DB", "/ai/datasets/connectors/github_stars.db"))


def _admin() -> str:
    import household_identity as hh
    return hh.ADMIN_PERSON_ID


# ── GitHub ─────────────────────────────────────────────────────────────────────────────────────────
def _api(token: str) -> Api:
    def call(path: str) -> tuple[int, Any]:
        import egress
        egress.note("github")
        req = urllib.request.Request("https://api.github.com" + path, method="GET",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github.star+json",
                                              "User-Agent": "titan-github-stars"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else [])
        except urllib.error.HTTPError as exc:
            return exc.code, None
    return call


def _ts(stamp: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def keep(star: dict[str, Any]) -> dict[str, Any] | None:
    """One star as the public metadata titan keeps, or None when it isn't a usable repo."""
    repo = star.get("repo") if isinstance(star.get("repo"), dict) else None
    name = str((repo or {}).get("full_name") or "")
    if not repo or not _NAME_RX.match(name) or _ts(star.get("starred_at")) is None:
        return None
    try:
        stars = int(repo.get("stargazers_count") or 0)
    except (TypeError, ValueError):
        stars = 0
    url = str(repo.get("html_url") or "")
    return {"full_name": name,
            "html_url": url if url.startswith("https://github.com/") else f"https://github.com/{name}",
            "description": " ".join(str(repo.get("description") or "").split())[:DESC_MAX],
            "language": str(repo.get("language") or "")[:40],
            "topics": [t for t in (repo.get("topics") or []) if isinstance(t, str) and _TOPIC_RX.match(t)][:20],
            "stargazers_count": stars,
            "pushed_at": str(repo.get("pushed_at") or ""),
            "starred_at": str(star.get("starred_at")),
            "archived": bool(repo.get("archived"))}


class Incomplete(Exception):
    """The listing stopped before the cut-off: nothing is changed."""


def fetch(api: Api, days: int = DAYS, now: datetime | None = None) -> list[dict[str, Any]]:
    """Stars from the last `days`, newest first. Raises Incomplete when a page fails or paging runs out."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        st, body = api(f"/user/starred?per_page={PER_PAGE}&page={page}&sort=created&direction=desc")
        if st != 200 or not isinstance(body, list):
            raise Incomplete(f"GitHub answered HTTP {st} on page {page}")
        for s in body:
            at = _ts(s.get("starred_at")) if isinstance(s, dict) else None
            if at is None:
                continue
            if at < cutoff:
                return out
            k = keep(s)
            if k:
                out.append(k)
        if len(body) < PER_PAGE:
            return out
    raise Incomplete(f"more than {MAX_PAGES * PER_PAGE} stars in the window")


# ── the stores ─────────────────────────────────────────────────────────────────────────────────────
def _db(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fresh = not p.exists()
    con = sqlite3.connect(str(p))
    if fresh:
        os.chmod(p, 0o600)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS stars (
        owner TEXT NOT NULL, full_name TEXT NOT NULL, html_url TEXT, description TEXT, language TEXT, topics TEXT,
        stargazers_count INTEGER, pushed_at TEXT, starred_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (owner, full_name))""")
    con.execute("CREATE TABLE IF NOT EXISTS syncs (owner TEXT PRIMARY KEY, at REAL NOT NULL, count INTEGER NOT NULL)")
    return con


def _replace(owner: str, repos: list[dict[str, Any]], path: Path | None = None) -> None:
    con = _db(path)
    try:
        with con:
            con.execute("DELETE FROM stars WHERE owner=?", (owner,))
            con.executemany("INSERT OR REPLACE INTO stars VALUES (?,?,?,?,?,?,?,?,?,?)", [
                (owner, r["full_name"], r["html_url"], r["description"], r["language"], json.dumps(r["topics"]),
                 r["stargazers_count"], r["pushed_at"], r["starred_at"], int(r["archived"])) for r in repos])
            con.execute("INSERT OR REPLACE INTO syncs VALUES (?,?,?)", (owner, time.time(), len(repos)))
    finally:
        con.close()


def stored(owner: str, path: Path | None = None) -> tuple[list[dict[str, Any]], float | None]:
    """The owner's synced stars and when they were synced (None: never). Never creates the store."""
    p = path or db_path()
    if not p.exists():
        return [], None
    con = _db(p)
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM stars WHERE owner=? ORDER BY starred_at DESC", (owner,))]
        s = con.execute("SELECT at FROM syncs WHERE owner=?", (owner,)).fetchone()
    finally:
        con.close()
    for r in rows:
        r["topics"] = json.loads(r["topics"] or "[]")
        r["archived"] = bool(r["archived"])
    return rows, (float(s["at"]) if s else None)


def _ledger_keys(owner: str) -> set[str]:
    """The repos the connectors ledger holds live for the owner. Read-only: a dry run never creates the ledger."""
    import connectors_runner as cx
    p = Path(cx.LEDGER_PATH)
    if not p.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return {r[0] for r in con.execute("SELECT upstream_id FROM items WHERE source=? AND owner=? AND "
                                              "deleted_at IS NULL", (SOURCE, owner))}
        finally:
            con.close()
    except sqlite3.Error:
        return set()


def item(r: dict[str, Any]) -> dict[str, Any]:
    """A star as a connector item: a record node with the metadata that doesn't change daily."""
    fields = {"name": r["full_name"], "html_url": r["html_url"], "description": r["description"],
              "language": r["language"], "topics": r["topics"], "starred_at": r["starred_at"],
              "archived": r["archived"]}
    return {"source_id": r["full_name"], "observed_at": r["starred_at"], "kind": NODE_KIND, "title": r["full_name"],
            "account": "github", "fields": fields}


def sync(api: Api, *, dry_run: bool = False, runtime: Any = None, owner: str | None = None,
         days: int = DAYS, now: datetime | None = None) -> dict[str, Any]:
    """One sync. Returns counts (and, applied, the runner's own counts)."""
    import connectors_runner as cx
    who = owner or _admin()
    repos = fetch(api, days, now)
    have = _ledger_keys(who)
    now_keys = {r["full_name"] for r in repos}
    gone = sorted(have - now_keys)
    out: dict[str, Any] = {"starred_in_window": len(repos), "new": len(now_keys - have), "removed": len(gone),
                           "days": days, "dry_run": dry_run}
    if dry_run:
        return out
    if SOURCE in cx.sources_off(who):
        _replace(who, [])
        return {**out, "off": True, "new": 0, "removed": 0}
    items = [item(r) for r in repos] + [{"source_id": k, "deleted": True} for k in gone]
    totals: dict[str, Any] = {"written": 0, "unchanged": 0, "removed": 0, "forgotten": 0, "errors": []}
    for i in range(0, len(items), BATCH):
        got = cx.ingest(SOURCE, items[i:i + BATCH], runtime=runtime, owner=who)
        for k in ("written", "unchanged", "removed", "forgotten"):
            totals[k] += got.get(k, 0)
        totals["errors"] += got.get("errors", [])
    _replace(who, repos)
    return {**out, "ledger": {**totals, "errors": len(totals["errors"])}}


# ── the github_stars tool ──────────────────────────────────────────────────────────────────────────
Wrap = Callable[[str, str], str]


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(str(v)))))
    except (TypeError, ValueError):
        return default


def _month(v: str, now: datetime) -> tuple[datetime, datetime, str] | None:
    """A month the asker named ("2026-09", "last", "this") as [start, end) in household time, and its label."""
    v = v.strip().lower()
    if v in ("last", "last month", "previous"):
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (first - timedelta(days=1)).replace(day=1)
    elif v in ("this", "this month", "current"):
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        m = re.match(r"^(\d{4})-(\d{1,2})$", v)
        if not m or not 1 <= int(m.group(2)) <= 12:
            return None
        start = now.replace(year=int(m.group(1)), month=int(m.group(2)), day=1, hour=0, minute=0, second=0,
                            microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end, f"{start:%B %Y}"


def _zone() -> Any:
    try:
        import local_time
        return local_time.zone() or timezone.utc
    except Exception:  # noqa: BLE001
        return timezone.utc


def _ago(seconds: float) -> str:
    s = max(0.0, seconds)
    return f"{int(s // 86400)} d ago" if s >= 86400 else f"{int(s // 3600)} h ago" if s >= 3600 else f"{int(s // 60)} min ago"


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def answer(args: dict[str, Any], owner: str, wrap: Wrap, *, path: Path | None = None,
           now: datetime | None = None) -> str:
    """The github_stars tool: filters, grouping and top N over the stored stars, as plain text built here.
    Repo descriptions are the repo owners' text: each goes through `wrap` (the untrusted marker)."""
    rows, synced = stored(owner, path)
    if synced is None:
        return ("No GitHub stars synced yet. titan syncs the admin's stars from the last year daily at 04:15 "
                "(titan-github-stars.timer, with the GitHub token on titan).")
    now = (now or datetime.now(timezone.utc)).astimezone(_zone())
    said: list[str] = []
    topic = str(args.get("topic") or "").strip().lower().lstrip("#")
    if topic:
        rows = [r for r in rows if any(topic == t or topic.replace(" ", "-") == t for t in r["topics"])]
        said.append(f"topic {topic}")
    lang = str(args.get("language") or "").strip().lower()
    if lang:
        rows = [r for r in rows if (r["language"] or "").lower() == lang]
        said.append(f"language {args.get('language')}")
    words = [w for w in re.split(r"\s+", str(args.get("query") or "").strip().lower()) if w]
    if words:
        rows = [r for r in rows if all(w in f"{r['full_name']} {' '.join(r['topics'])} {r['description']}".lower()
                                       for w in words)]
        said.append(f"matching “{' '.join(words)}”")
    if args.get("month"):
        m = _month(str(args["month"]), now)
        if m is None:
            return "Not answered: month is YYYY-MM, \"last\" or \"this\"."
        start, end, label = m
        rows = [r for r in rows if (at := _ts(r["starred_at"])) is not None and start <= at < end]
        said.append(f"starred in {label}")
    elif args.get("days"):
        days = _int(args["days"], 30, 1, 3650)
        cut = now - timedelta(days=days)
        rows = [r for r in rows if (at := _ts(r["starred_at"])) is not None and at > cut]
        said.append(f"starred in the last {days} days")
    archived = sum(1 for r in rows if r["archived"])
    head = (f"GitHub stars (the admin's own, last {DAYS} days, synced {_ago(now.timestamp() - synced)}): {len(rows)} "
            + (f"repo{'s' if len(rows) != 1 else ''}" + (f", {'; '.join(said)}" if said else "")))
    if not rows:
        return head + ". None match."

    group = str(args.get("group") or "").strip().lower()
    if group in ("language", "languages", "lang"):
        c = Counter(r["language"] or "(no language)" for r in rows)
        return head + ".\nBy language: " + ", ".join(f"{k} {n}" for k, n in c.most_common(25)) + "."
    if group in ("topic", "topics"):
        c = Counter(t for r in rows for t in r["topics"])
        untagged = sum(1 for r in rows if not r["topics"])
        return (head + ".\nBy topic (a repo can have several): " + ", ".join(f"{k} {n}" for k, n in c.most_common(30))
                + (f"; {untagged} with no topics" if untagged else "") + ".")
    if group:
        return "Not answered: group is language or topic."

    by = str(args.get("sort") or "").strip().lower()
    if by in ("stars", "stargazers", "popular", "top"):
        rows = sorted(rows, key=lambda r: -int(r["stargazers_count"] or 0))
        order = "most-starred first"
    else:
        order = "newest star first"
    n = _int(args.get("limit"), 15, 1, 50)
    lines = [head + f" ({order}" + (f"; showing {n}" if len(rows) > n else "") + ")"
             + (f"; {archived} archived" if archived else "") + ":"]
    for r in rows[:n]:
        at = _ts(r["starred_at"])
        bits = [r["full_name"], r["language"] or "no language", f"★ {_k(int(r['stargazers_count'] or 0))}",
                f"starred {at.astimezone(now.tzinfo):%Y-%m-%d}" if at else "starred ?"]
        if r["archived"]:
            bits.append("archived")
        if r["topics"]:
            bits.append("topics: " + ", ".join(r["topics"][:6]))
        lines.append("- " + " · ".join(bits) + f" · {r['html_url']}")
        if r["description"]:
            lines.append("  " + wrap(r["description"], r["full_name"]))
    return "\n".join(lines)


def status_line(owner: str | None = None, path: Path | None = None, now: float | None = None) -> str:
    """For capabilities: whether the stars are synced, how many, and how long ago."""
    rows, synced = stored(owner or _admin(), path)
    if synced is None:
        return "GitHub stars: not synced yet (titan-github-stars.timer, daily 04:15 PT)"
    ago = (now or time.time()) - synced
    flag = ", STALE" if ago > 2 * 86400 else ""
    return (f"GitHub stars: {len(rows)} repo{'s' if len(rows) != 1 else ''} from the last {DAYS} days, synced "
            f"{_ago(ago)}{flag} (daily 04:15 PT)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and print counts only; write nothing")
    a = ap.parse_args()
    try:
        token = Path(TOKEN_FILE).read_text().strip()
    except OSError as exc:
        token = ""
        why = exc.strerror or type(exc).__name__
    else:
        why = "empty file"
    if not token:
        print(f"no GitHub token at {TOKEN_FILE} ({why}); nothing synced")
        return 0
    try:
        out = sync(_api(token), dry_run=a.dry_run)
    except Incomplete as exc:
        print(f"GitHub stars not synced: {exc}; nothing changed", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
