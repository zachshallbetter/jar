#!/usr/bin/env python3
"""A cap for a task step, approved on a slider (jellybean#707; the contract is on that issue, approved by Coordination
10-05: ceilings as written, caps in tasks.db, and never an amount the person didn't see).

  declare   plan_task's `caps` ("2: usd 40; 4: count 3"): a step that spends or counts something gets a cap titan
            proposes, checked in code: unit usd or count, 0 < value ≤ the ceiling (usd 1000, count 50).
  ask       the runner never runs such a step without an approved cap: it waits, and one card goes to the task's
            owner, "Set a limit for “<step>”", whose Approve button says the amount it approves ("Approve up to $40")
            and sends it ("cap:<id>:40"). The app's slider (min..max in steps, from the default) sends its own number
            in place of that one. A tap without a number is refused: never an amount the person didn't see.
  answer    only the task's owner, an adult or the admin; the number within min..max, snapped to the step; kept with
            who, which device and when. Not now: the step keeps waiting; it never runs uncapped.
  enforce   the step runs with "stay within up to $40" in its context, and its result is checked in code: an amount
            (usd) or a quantity (count) above the cap marks it over cap, not accepted, and the task halts.
Nothing here moves money: titan has no purchase or payment tool, and a cap grants nothing; it only limits what titan
may plan or suggest.
"""
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable

CEILING = {"usd": 1000, "count": 50}
OWNERS = frozenset({"admin", "adult"})
SOURCE = "approve-cap"


def step_of(unit: str, top: float) -> int:
    return 1 if unit == "count" else (5 if top <= 200 else 10)


def fmt(unit: str, v: float) -> str:
    return f"${v:,.0f}" if unit == "usd" else f"{int(v)}"


def _db(db: Path | None = None):
    import scenarios as sc
    con = sc._db(db)
    con.execute("""CREATE TABLE IF NOT EXISTS caps (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, step_key TEXT NOT NULL,
        step_title TEXT NOT NULL, owner TEXT NOT NULL, unit TEXT NOT NULL, proposed REAL NOT NULL, min REAL NOT NULL,
        max REAL NOT NULL, step REAL NOT NULL, state TEXT NOT NULL, value REAL, set_by TEXT, device TEXT, at REAL,
        created_at REAL NOT NULL, UNIQUE (task_id, step_key))""")
    if "via" not in {c[1] for c in con.execute("PRAGMA table_info(caps)")}:
        con.execute("ALTER TABLE caps ADD COLUMN via TEXT")   # jellybean#900: "watch" for a tap relayed from the Watch
    return con


_SPEC_RX = re.compile(r"^\s*(\d+)\s*[:=]\s*(usd|\$|dollars?|count|items?|qty)\s*\$?\s*(\d+(?:\.\d+)?)\s*$", re.I)


def parse(spec: str, n_steps: int) -> tuple[dict[str, tuple[str, float]], list[str]]:
    """plan_task's caps → ({step_key: (unit, proposed)}, problems). Step numbers are 1-based, as the plan lists them."""
    out: dict[str, tuple[str, float]] = {}
    bad: list[str] = []
    for part in [p for p in re.split(r"[;\n]", str(spec or "")) if p.strip()]:
        m = _SPEC_RX.match(part)
        if not m:
            bad.append(f"couldn't read “{part.strip()[:40]}” (e.g. '2: usd 40')")
            continue
        i, unit, v = int(m.group(1)), m.group(2).lower(), float(m.group(3))
        unit = "usd" if unit in ("usd", "$", "dollar", "dollars") else "count"
        if not 1 <= i <= n_steps:
            bad.append(f"step {i} isn't in the plan")
        elif not 0 < v <= CEILING[unit]:
            bad.append(f"step {i}: {fmt(unit, v)} is outside 1..{fmt(unit, CEILING[unit])}")
        else:
            out[f"s{i - 1}"] = (unit, v)
    return out, bad


def declare(task_id: str, owner: str, step_key: str, step_title: str, unit: str, proposed: float,
            db: Path | None = None, now: float | None = None) -> str:
    top = min(CEILING[unit], max(proposed * 2, step_of(unit, proposed) * 4))
    st = step_of(unit, top)
    cid = "cap-" + secrets.token_hex(6)
    con = _db(db)
    try:
        con.execute("INSERT OR REPLACE INTO caps (id, task_id, step_key, step_title, owner, unit, proposed, min, max, step, "
                    "state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'needed', ?)",
                    (cid, task_id, step_key, step_title[:120], owner, unit, _snap(proposed, st, st, top), st, top, st,
                     now or time.time()))
        con.commit()
    finally:
        con.close()
    return cid


def _snap(v: float, lo: float, step: float, hi: float) -> float:
    return max(lo, min(hi, round(v / step) * step))


def need(task_id: str, step_key: str, db: Path | None = None) -> dict[str, Any] | None:
    """The step's cap row, or None when the step has no cap to approve."""
    con = _db(db)
    try:
        r = con.execute("SELECT * FROM caps WHERE task_id=? AND step_key=?", (task_id, step_key)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def card(row: dict[str, Any]) -> tuple[str, str, list[dict[str, str]], dict[str, Any]]:
    """The card's fixed wording, its two buttons and its slider data."""
    u, d = row["unit"], row["proposed"]
    shown = fmt(u, d)
    return (f"Set a limit for “{row['step_title']}”",
            "Titan will plan and suggest within this. Nothing is bought or paid.",
            [{"id": "approve", "label": f"Approve up to {shown}", "kind": "reply", "value": f"cap:{row['id']}:{d:g}"},
             {"id": "decline", "label": "Not now", "kind": "reply", "value": f"nocap:{row['id']}"}],
            {"kind": "approve_cap", "cap_id": row["id"], "unit": u, "min": row["min"], "max": row["max"],
             "step": row["step"], "default": d, "format": "Up to $" + "{value}" if u == "usd" else "Up to {value}"})


def ask(row: dict[str, Any], send: Callable[..., Any], db: Path | None = None) -> bool:
    """One card to the owner, once. → sent now."""
    if row["state"] != "needed":
        return False
    title, detail, actions, data = card(row)
    send(title, detail, kind="info", urgency=2, source=SOURCE, to=[row["owner"]], key=f"cap:{row['id']}",
         actions=actions, data=data)
    con = _db(db)
    try:
        con.execute("UPDATE caps SET state='asked' WHERE id=?", (row["id"],))
        con.commit()
    finally:
        con.close()
    return True


def answer(value: str, person: str | None, role: str | None, device: dict[str, Any] | None = None, *,
           db: Path | None = None, now: float | None = None, via: str | None = None) -> tuple[bool, str]:
    """A tap: cap:<id>:<number> or nocap:<id>. (done, said). via "watch" (jellybean#900) is kept on the approval."""
    parts = str(value or "").split(":")
    verb, cid = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
    if verb not in ("cap", "nocap"):
        return False, "titan didn't understand that."
    con = _db(db)
    try:
        r = con.execute("SELECT * FROM caps WHERE id=?", (cid,)).fetchone()
        if not r or r["owner"] != str(person or "") or role not in OWNERS:
            return False, "That isn't there any more."
        if r["state"] == "approved":
            return False, "That limit is set already."
        if verb == "nocap":
            return True, "OK: that step waits; it won't run without a limit."
        if len(parts) < 3:
            return False, "Update the app to set a limit: this version can't show the amount."
        try:
            v = float(parts[2])
        except ValueError:
            return False, "That isn't a number."
        if not r["min"] <= v <= r["max"]:
            return False, f"Pick between {fmt(r['unit'], r['min'])} and {fmt(r['unit'], r['max'])}."
        v = _snap(v, r["min"], r["step"], r["max"])
        con.execute("UPDATE caps SET state='approved', value=?, set_by=?, device=?, at=?, via=? WHERE id=?",
                    (v, str(person), str((device or {}).get("id") or ""), now or time.time(), via, cid))
        con.commit()
        task_id = r["task_id"]
    finally:
        con.close()
    try:   # the task picks the step up on its next pass
        import scenarios as sc
        sc.update(task_id, state="active", next_due=(now or time.time()), db=db)
    except Exception:  # noqa: BLE001
        pass
    return True, f"Limit set: up to {fmt(r['unit'], v)}. (Nothing was bought or paid.)"


# An approval relayed from the Watch without the amount it showed (jellybean#900): the Watch has no slider, so its
# button carries the amount ("Approve up to $40") and sends it; a tap without it never approves the suggestion unseen.
WATCH_NEEDS_AMOUNT = "Set the limit on your iPhone: that tap didn't say the amount."


def context_line(row: dict[str, Any]) -> str:
    return (f"This step has an approved limit: stay within up to {fmt(row['unit'], row['value'])}"
            f"{' in total' if row['unit'] == 'usd' else ''}. Don't plan or suggest anything above it.")


_USD_RX = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?")
_COUNT_RX = re.compile(r"(?i)\b(?:qty|quantity|x)\s*:?\s*(\d+)\b|\b(\d+)\s*(?:x|items?|tickets?|units?|pairs?|pcs|pieces)\b")


def over(row: dict[str, Any], *texts: str) -> list[float]:
    """The amounts (usd) or quantities (count) in these texts above the approved cap."""
    cap = float(row.get("value") or 0)
    found: list[float] = []
    for t in texts:
        t = str(t or "")
        if row["unit"] == "usd":
            found += [float(m.group(1).replace(",", "")) for m in _USD_RX.finditer(t)]
        else:
            found += [float(m.group(1) or m.group(2)) for m in _COUNT_RX.finditer(t)]
    return [v for v in found if v > cap]
