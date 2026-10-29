#!/usr/bin/env python3
"""#394 (connector half): every version of a synced item's structured fields is kept, and changes() says what
changed since a time, for one viewer. Hermetic: a temp ledger, the runtimes faked."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import os as _os_acl, tempfile as _tf_acl  # noqa: E402
_os_acl.environ.setdefault("KB_ACL_DB", _os_acl.path.join(_tf_acl.mkdtemp(prefix="titan-acl-"), "kb_acl.db"))  # #1273: ingest writes sharing rows; never titan's own
import changes as ch  # noqa: E402
import connectors_runner as cr  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


led = Path(tempfile.mkdtemp()) / "ledger.db"
fake = lambda *a, **k: {}   # noqa: E731


def event(title, start, place=None, text="note text"):
    return {"source_id": "ev-1", "kind": "event", "title": title, "text": text,
            "fields": {"start": start, "end": start.replace("18:00", "21:00"), **({"location": place} if place else {})},
            "observed_at": "2026-10-01T10:00:00Z"}


t0 = time.time() - 1
cr.ingest("calendar", [event("Ava's party", "2026-10-03T18:00")], runtime=fake, ledger=led)
cr.ingest("calendar", [event("Ava's party", "2026-10-03T18:00", text="only the notes changed")], runtime=fake, ledger=led)
cr.ingest("calendar", [event("Ava's party", "2026-10-04T18:00", place="Riverside Park")], runtime=fake, ledger=led)
con = sqlite3.connect(led)
rows = con.execute("SELECT change, title, fields FROM versions ORDER BY at").fetchall()
check("each structural change and the creation are versions", [r[0] for r in rows] == ["created", "changed", "changed"], str(rows))
check("versions keep fields, never body text", all("note text" not in (r[2] or "") and "only the notes" not in (r[2] or "") for r in rows))

ACL = {}
ADMIT = lambda *a, **k: True   # noqa: E731


def chg(*a, **k):
    k.setdefault("fence", ADMIT)
    return ch.changes(*a, **k)
iid = cr.item_id("calendar", "ev-1")
c = chg(t0, person="zach", role="admin", ledger=led, acl={})
check("changes: created, then the move (a body-only re-sync is not a change)",
      [x["change"] for x in c] == ["created", "changed"], str(c))
mv = c[-1]["diff"]
check("the move is a structured diff", mv.get("start") == ["2026-10-03T18:00", "2026-10-04T18:00"]
      and mv.get("location") == [None, "Riverside Park"], str(mv))
check("describe is a plain view of it", "“Ava's party” changed: end" in ch.describe(c) or "start 2026-10-03T18:00 → 2026-10-04T18:00" in ch.describe(c),
      ch.describe(c))
check("since filters", chg(time.time() + 5, person="zach", role="admin", ledger=led, acl={}) == [])
check("item_ids filters", chg(t0, item_ids=["cx-other"], person="zach", role="admin", ledger=led, acl={}) == [])

# Visibility: Zach's private calendar isn't Robin's to see, until it's shared with the household.
check("another person's private item: invisible", chg(t0, person="p_robin", role="adult", ledger=led, acl={}) == [])
check("shared with the household: visible", len(chg(t0, person="p_robin", role="adult", ledger=led,
                                                          acl={iid: ("zach", "household")})) == 2)

# Fences (on since 10-01): a change whose text touches a fence the viewer isn't through is left out; the
# judged text is the title and both versions' fields; the owner is passed through.
seen = []
def surprise_fence(person, role, owner, *, text, item):
    seen.append((person, owner, text, item))
    return "party" not in text.lower() or person == owner
check("a fenced change is hidden from the household", chg(t0, person="p_robin", role="adult", ledger=led,
      acl={iid: ("zach", "household")}, fence=surprise_fence) == [])
check("the fence judges the title and both versions' fields, with the owner", seen and seen[-1][1] == "zach"
      and "Riverside Park" in seen[-1][2] and "2026-10-03T18:00" in seen[-1][2] and seen[-1][3] == iid, str(seen[-1:]))
check("and never hides the owner's own change", len(chg(t0, person="zach", role="admin", ledger=led, acl={},
                                                        fence=surprise_fence)) == 2)
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "changes.py")).read()
check("by default it asks fences.get().admits, like the KB", "fences.get().admits(person, role, owner, text=text, item=item)" in src)

cr.ingest("calendar", [{"source_id": "ev-1", "deleted": True, "deleted_reason": "deleted"}], runtime=fake, ledger=led)
c = chg(t0, person="zach", role="admin", ledger=led, acl={})
check("a removal is a change too", c[-1]["change"] == "removed" and c[-1]["title"] == "Ava's party", str(c[-1]))

# Pruning keeps VERSIONS_DAYS.
con.execute("UPDATE versions SET at = ? WHERE change='created'", (time.time() - (cr.VERSIONS_DAYS + 1) * 86400,))
con.commit()
cr.ingest("calendar", [event("New", "2026-10-09T18:00") | {"source_id": "ev-2"}], runtime=fake, ledger=led)
check("versions older than the keep window are pruned",
      con.execute("SELECT COUNT(*) FROM versions WHERE change='created' AND source_id='ev-1'").fetchone()[0] == 0)
check("no ledger: nothing", chg(0, person="zach", role="admin", ledger=Path("/nonexistent/x.db")) == [])

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all changes checks passed'}")
sys.exit(1 if failures else 0)
