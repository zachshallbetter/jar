#!/usr/bin/env python3
"""The parallel week (titan-node#955 step 6): does the mail server connector see what the Mac's mail sync sees?

Read-only. Mail is matched by Message-ID across the two stores: the Mac's rows in the connector ledger (source "mail",
whose source_id is the Message-ID without brackets) and the server connector's store (mail_server.py). Per day
(the day the mail was sent, UTC) and for one owner, it counts:
  both          the Mac synced it and the server has it
  server_only   on the server, never synced by the Mac (what the Mac missed, or before it started)
  mac_only      synced by the Mac, not on the server in the folders it reads (what the server connector misses)
plus the Mac rows the 10-03 cleanup tombstoned, and code mails. Counts only: never an id, subject or address.

Where the Mac-only mails are (--locate, the default for the daily run): the server connector reads INBOX only until
Zach decides whether titan reads Archive and Sent (#955, 10-06), and the Mac syncs every mailbox. So each Mac-only
Message-ID (at most MAX_LOCATE) is looked up read-only on the owner's IMAP accounts and counted by the kind of folder
it's in: inbox, sent, archive, junk, trash, drafts, other, or absent. Nothing is fetched or kept but that count.
mac_only_inbox (inbox + absent: what the INBOX read should have had) is the run sheet's gate meanwhile; a run that
couldn't look counts every Mac-only mail there.

The window starts where both stores have mail (the later of the two earliest dates), so neither side's history
before the other began counts against it. The Mac's rows for other providers (Gmail on the Mac, while only iCloud is
connected here) are left out by account name prefix.

  python3 scripts/mail_compare.py [--owner zach] [--account-prefix icl] [--since YYYY-MM-DD] [--append FILE]
  python3 scripts/mail_compare.py --run-sheet /ai/datasets/connectors/mail-compare.jsonl [--week-start 2026-10-06]
      the step-6 verdict, per sent-day of the parallel week (exit 0 = GO)
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _norm(mid: str) -> str:
    return str(mid or "").strip().strip("<>").strip().lower()


MAX_LOCATE = 40
KINDS = ("inbox", "sent", "archive", "junk", "trash", "drafts", "other", "absent")
_FLAG_KINDS = (("\\sent", "sent"), ("\\archive", "archive"), ("\\all", "archive"), ("\\junk", "junk"),
               ("\\trash", "trash"), ("\\drafts", "drafts"))
_NAME_KINDS = (("sent", "sent"), ("archive", "archive"), ("all mail", "archive"), ("junk", "junk"),
               ("spam", "junk"), ("trash", "trash"), ("deleted", "trash"), ("drafts", "drafts"))


def folder_kind(flags: set[str], name: str) -> str:
    if name.upper() == "INBOX":
        return "inbox"
    for flag, kind in _FLAG_KINDS:
        if flag in flags:
            return kind
    low = name.lower()
    return next((kind for word, kind in _NAME_KINDS if word in low), "other")


def locate(mids: list[str], owner: str, accounts: list[dict[str, Any]] | None = None,
           connect: Any = None) -> dict[str, str]:
    """Which kind of folder each Message-ID is in on the owner's IMAP accounts (read-only: LIST, EXAMINE, UID SEARCH
    HEADER). → {mid: kind}; "absent" when no folder has it. Gmail/Outlook accounts aren't looked in."""
    import mail_server
    accts = [a for a in (accounts if accounts is not None else mail_server.accounts())
             if a["owner"] == owner and a.get("provider") not in ("gmail", "outlook")]
    left = [m for m in dict.fromkeys(mids) if m][:MAX_LOCATE]
    out: dict[str, str] = {}
    for acct in accts:
        if not left:
            break
        conn = mail_server.open_account(acct, **({"connect": connect} if connect else {}))
        try:
            listed = [(f, n) for f, n in conn.folders() if "\\noselect" not in f]
            listed.sort(key=lambda fn: fn[1].upper() != "INBOX")       # INBOX first
            for flags, name in listed:
                if not left:
                    break
                conn.examine(name)
                for m in list(left):
                    if conn.uids("HEADER", "Message-ID", mail_server._quote(m)):
                        out[m] = folder_kind(flags, name)
                        left.remove(m)
        finally:
            conn.logout()
    for m in left:
        out.setdefault(m, "absent")
    return out


def compare(*, owner: str = "zach", account_prefix: str = "icl", since: str | None = None,
            ledger: Path | None = None, store: Path | None = None,
            locate_fn: Any = None) -> dict[str, Any]:
    """locate_fn(mids) → {mid: kind}: where the Mac-only mails are (None: not looked; all count as INBOX misses)."""
    import connectors_runner
    import mail_server
    L = _ro(Path(ledger or connectors_runner.LEDGER_PATH))
    S = _ro(Path(store or mail_server.DB))
    try:
        mac = [(_norm(sid), str(obs or ""), bool(dele)) for sid, acct, obs, dele in L.execute(
            "SELECT source_id, account, observed_at, deleted_at FROM items WHERE source='mail' AND owner=?", (owner,))
            if str(acct or "").lower().startswith(account_prefix.lower())]
        srv = [(_norm(mid), str(at or ""), bool(code)) for mid, at, code in S.execute(
            "SELECT message_id, sent_at, code FROM messages WHERE owner=?", (owner,))]
    finally:
        L.close()
        S.close()
    starts = [min((d for _, d, _ in rows if d), default="") for rows in (mac, srv)]
    start = since or (max(starts) if all(starts) else "")
    mac_ids = {m for m, _, _ in mac}
    srv_ids = {m for m, _, _ in srv}
    days: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for mid, at, code in srv:
        if at[:10] >= start[:10]:
            d = days[at[:10]]
            d["both" if mid in mac_ids else "server_only"] += 1
            d["codes"] += code
    only = [(mid, at) for mid, at, _gone in mac if at[:10] >= start[:10] and mid not in srv_ids]
    where: dict[str, str] = {}
    located = False
    if locate_fn is not None and only:
        try:
            where = locate_fn([m for m, _ in only])
            located = True
        except Exception as exc:  # noqa: BLE001 - a lookup that fails counts every Mac-only mail as a miss
            print(f"locate failed ({type(exc).__name__}): every Mac-only mail counts as an INBOX miss", file=sys.stderr)
    kinds: collections.Counter = collections.Counter()
    for mid, at, gone in mac:
        if at[:10] >= start[:10]:
            if mid not in srv_ids:
                days[at[:10]]["mac_only"] += 1
                k = where.get(mid, "inbox")       # not looked (or past MAX_LOCATE): counts as a miss
                kinds[k] += 1
                if k in ("inbox", "absent"):
                    days[at[:10]]["mac_only_inbox"] += 1
                # #955 run sheet: still in INBOX on the server but not in the connector's store is a real miss;
                # in no folder at all is mail deleted upstream after the Mac synced it (explained, never a miss)
                days[at[:10]]["mac_only_missed" if k == "inbox" else ("mac_only_absent" if k == "absent" else "mac_only_filed")] += 1
            days[at[:10]]["mac_tombstoned"] += gone
    keys = ("both", "server_only", "mac_only", "mac_only_inbox", "mac_only_missed", "mac_only_absent", "mac_only_filed",
            "mac_tombstoned", "codes")
    per = {d: {k: days[d][k] for k in keys} for d in sorted(days)}
    total = {k: sum(v[k] for v in per.values()) for k in keys}
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "owner": owner, "since": start[:10],
            "total": total, "days": per, "located": located, "mac_only_where": {k: kinds[k] for k in KINDS if kinds[k]},
            "scope": "inbox",
            "match": total["mac_only_inbox"] == 0 and (total["both"] + total["mac_only"]) > 0}


def run_sheet(lines: list[str], week_start: str, days: int = 7) -> dict[str, Any]:
    """#955 step 6, judged per sent-day (10-07): for each day of the parallel week, the newest run's count of that day's
    Mac mail still in the server's INBOX yet missing from the connector's store (mac_only_missed). The cumulative
    mac_only_inbox can't do it: one mail deleted upstream on 10-05 (in no folder now) kept every later day at 1.
    Deleted-upstream (absent) and filed-elsewhere mail are shown, never counted as misses. A line from before the split
    counts its mac_only_inbox as missed. Also needs a run logged on each of the 7 days."""
    from datetime import date, timedelta
    runs: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        day = str(r.get("at") or "")[:10]
        if day:
            runs[day] = r
    week = [(date.fromisoformat(week_start) + timedelta(days=k)).isoformat() for k in range(days)]
    latest = runs[max(runs)] if runs else {}
    per = latest.get("days") or {}
    rows = []
    for d in week:
        v = per.get(d) or {}
        missed = v.get("mac_only_missed", v.get("mac_only_inbox", v.get("mac_only", 0)))
        row = {"day": d, "run_logged": d in runs, "missed": missed, "absent": v.get("mac_only_absent", 0),
               "filed": v.get("mac_only_filed", 0), "both": v.get("both", 0), "server_only": v.get("server_only", 0)}
        # a day neither side saw any mail on measured nothing: "missed 0" there is vacuous (10-10: the household reset
        # took the Mac's side away, so the week's last days would have read as clean). Unmeasured is never GO.
        row["measured"] = any(row[k] for k in ("missed", "absent", "filed", "both", "server_only"))
        rows.append(row)
    logged = all(r["run_logged"] for r in rows)
    clean = all(r["missed"] == 0 for r in rows)
    measured = all(r["measured"] for r in rows)
    return {"week": [week[0], week[-1]], "from_run": latest.get("at"), "located": latest.get("located"), "days": rows,
            "logged": logged, "clean": clean, "measured": measured, "go": logged and clean and measured}


def summary(lines: list[str], days: int = 7) -> dict[str, Any]:
    """The run sheet's gate (#955 step 6) from the daily log: the last run of each of the last `days` days, and whether
    every one had no INBOX mail of the Mac's missing from the server (mac_only_inbox = 0; mac_only_all is every
    mailbox). Consecutive days are required."""
    from datetime import date, timedelta
    by_day: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        day = str(r.get("at") or "")[:10]
        if day:
            by_day[day] = r       # the last run of the day wins
    picked = sorted(by_day)[-days:]
    # INBOX-scoped (#955, 10-06) until Zach decides on Archive and Sent: a run from before mac_only_inbox existed
    # counts all its Mac-only mail
    rows = [{"day": d, "mac_only": by_day[d]["total"].get("mac_only_inbox", by_day[d]["total"].get("mac_only", 0)),
             "mac_only_all": by_day[d]["total"].get("mac_only", 0), "both": by_day[d]["total"].get("both", 0),
             "server_only": by_day[d]["total"].get("server_only", 0)} for d in picked]
    consecutive = len(picked) == days and all(
        date.fromisoformat(b) - date.fromisoformat(a) == timedelta(days=1) for a, b in zip(picked, picked[1:]))
    clean = all(r["mac_only"] == 0 for r in rows)
    return {"days": rows, "consecutive": consecutive, "clean": clean, "go": consecutive and clean and len(rows) == days}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--owner", default="zach")
    ap.add_argument("--account-prefix", default="icl", help="the Mac's account name prefix for this provider")
    ap.add_argument("--since", help="YYYY-MM-DD (default: where both stores have mail)")
    ap.add_argument("--append", help="also append the result as one JSON line to this file (0600)")
    ap.add_argument("--summary", metavar="LOG", help="read a daily log (--append's) and print the 7-day verdict instead")
    ap.add_argument("--no-locate", action="store_true", help="don't look up where the Mac-only mails are (no IMAP)")
    ap.add_argument("--run-sheet", metavar="LOG", help="the step-6 verdict per sent-day of the parallel week (10-07)")
    ap.add_argument("--week-start", default="2026-10-06", help="the parallel week's first day (for --run-sheet)")
    a = ap.parse_args()
    if a.run_sheet:
        got = run_sheet(Path(a.run_sheet).read_text().splitlines() if Path(a.run_sheet).exists() else [], a.week_start)
        print(f"parallel week {got['week'][0]} – {got['week'][1]}, judged from the run at {got['from_run']}"
              + ("" if got["located"] else " (Mac-only mail not located: every one counts as missed)"))
        for r in got["days"]:
            print(f"  {r['day']}  missed {r['missed']}  deleted upstream {r['absent']}  filed elsewhere {r['filed']}"
                  f"  both {r['both']}  server_only {r['server_only']}" + ("" if r["run_logged"] else "  (no run logged)")
                  + ("" if r["measured"] else "  (unmeasured: neither side saw mail)"))
        why = ", ".join(w for w, bad in (("a day has no run logged", not got["logged"]),
                                         ("a day had Mac INBOX mail the connector missed", not got["clean"]),
                                         ("a day measured nothing (neither side saw mail)", not got["measured"])) if bad)
        print("GO: every day of the week logged and measured, no INBOX mail missed" if got["go"] else f"NOT YET ({why})")
        return 0 if got["go"] else 1
    if a.summary:
        got = summary(Path(a.summary).read_text().splitlines() if Path(a.summary).exists() else [])
        for r in got["days"]:
            print(f"  {r['day']}  mac_only (inbox) {r['mac_only']}  (all mailboxes {r['mac_only_all']})  both {r['both']}"
                  f"  server_only {r['server_only']}")
        why = ("" if got["go"] else " (" + ", ".join(w for w, bad in (
            (f"{len(got['days'])} of 7 days logged", len(got["days"]) < 7),
            ("days aren't consecutive", len(got["days"]) == 7 and not got["consecutive"]),
            ("a day had Mac INBOX mail missing from the server", not got["clean"])) if bad) + ")")
        print(("GO: 7 consecutive days, INBOX mac_only = 0 on all" if got["go"] else "NOT YET") + why)
        return 0
    got = compare(owner=a.owner, account_prefix=a.account_prefix, since=a.since,
                  locate_fn=None if a.no_locate else (lambda mids: locate(mids, a.owner)))
    print(f"since {got['since']}: " + ", ".join(f"{k} {v}" for k, v in got["total"].items())
          + ("  (every Mac INBOX mail is on the server)" if got["match"] else ""))
    if got["mac_only_where"]:
        print("  Mac-only, by folder kind" + ("" if got["located"] else " (not looked up)") + ": "
              + ", ".join(f"{k} {n}" for k, n in got["mac_only_where"].items()))
    for d, v in got["days"].items():
        print(f"  {d}  " + "  ".join(f"{k} {n}" for k, n in v.items()))
    if a.append:
        fd = os.open(a.append, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(got) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
