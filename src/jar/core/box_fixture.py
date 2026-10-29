#!/usr/bin/env python3
"""A fixture box (#1399 P4's golden-household accuracy run): a sandbox box whose stores are the golden household
(scripts/graph_golden/household.json), never copies of the real ones. Only in such a box may an eval turn be asked AS
one of the fixture's people ("eval_as": {"person": "p_robin"}): that is an impersonation path, so all of these must hold
(Coordination 10-10), and anything else is refused (403, never run as the asker):

  - BOX_FIXTURE=1 in the API's environment (sandbox_box.sh --fixture sets it; the deploy refuses an agent.env that
    sets it, so the live agent never runs with it);
  - the turn is an eval turn (source "eval") from the admin token;
  - the loader's marker is in the box's stores (beside HOUSEHOLD_DB): MARKER, whose content is household.json's digest,
    so a hand-made marker or another fixture doesn't pass;
  - the person is one of the fixture's people (FIXTURE_IDS) AND in this box's household store: a real household id is
    refused even in a fixture box.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
HOUSEHOLD = HERE / "graph_golden" / "household.json"
MARKER = ".box-fixture"


def household_digest() -> str:
    return "sha256:" + hashlib.sha256(HOUSEHOLD.read_bytes()).hexdigest()


def fixture_people() -> dict[str, dict[str, Any]]:
    return {str(p["id"]): p for p in json.loads(HOUSEHOLD.read_text())["people"]}


FIXTURE_IDS = frozenset(fixture_people())


def stores_dir() -> Path | None:
    db = os.environ.get("HOUSEHOLD_DB")
    return Path(db).parent if db else None


def write_marker(stores: Path) -> Path:
    """The loader's mark on a fixture box's stores (0600)."""
    p = stores / MARKER
    p.write_text(household_digest() + "\n")
    os.chmod(p, 0o600)
    return p


def active() -> tuple[bool, str]:
    """(whether this process runs a fixture box, why not)."""
    if os.environ.get("BOX_FIXTURE", "") != "1":
        return False, "not a fixture box"
    d = stores_dir()
    if d is None:
        return False, "no household store configured"
    try:
        got = (d / MARKER).read_text().strip()
    except OSError:
        return False, "the stores carry no fixture marker"
    if got != household_digest():
        return False, "the stores' fixture marker doesn't match the golden household"
    return True, ""


def person(want: Any, source: str | None, role: str | None, store: Any) -> tuple[dict[str, str] | None, str]:
    """eval_as {"person": id} → ({id, name, role} to run the turn as, "") or (None, why it's refused)."""
    pid = str((want or {}).get("person") or "").strip() if isinstance(want, dict) else ""
    if not pid:
        return None, "no person named"
    if source != "eval" or role != "admin":
        return None, "only an eval turn with the admin token may name a fixture person"
    ok, why = active()
    if not ok:
        return None, why
    if pid not in FIXTURE_IDS:
        return None, "only the golden household's people may be named"
    try:
        p = store.person(pid)
    except Exception:  # noqa: BLE001
        p = None
    if not p:
        return None, "that person isn't in this box's household"
    return {"id": pid, "name": str(p.get("name") or pid), "role": str(p.get("role") or "")}, ""
