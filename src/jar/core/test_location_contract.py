#!/usr/bin/env python3
"""
The location-routine contract (notes/location-coordination-2026-09-30.md, v1), end to end through the
real API: an iPhone posts arrived/left a named place; the person's own place routines suggest
themselves (a card to them only); a household routine's card names no one; a resend of the same id
does nothing; one nudge per routine per 10 minutes; no coordinates are kept; a household device
can't post; another person never sees someone's arrival; removing the device forgets the person's
events. Hermetic: throwaway stores, test devices paired into a temp household.

Run: python3 scripts/test_location_contract.py
"""
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
_TMP = Path(tempfile.mkdtemp(prefix="loc-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "PLACES_DB": "places.db", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HA_TOKEN_FILE": "no-ha-token", "TODOS_DB": "todos.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_TURN_SOURCE"] = "test"

import household_identity as hh  # noqa: E402
import places  # noqa: E402
import proactive  # noqa: E402
import routines  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

MASTER = "master-test-token-location-01"


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def run():
    ok = True
    api._AGENT_TOKEN = MASTER
    hh.reset_store(hh.HouseholdStore(_TMP / "household.db"))
    c = TestClient(api.app)
    M = {"X-Agent-Token": MASTER}

    def pair(person, name, platform="ios"):
        body = {"person_id": person} if isinstance(person, str) else {"new_person": person}
        code = c.post("/v1/pair/start", json=body, headers=M).json()
        r = c.post("/v1/pair/claim", json={"code": code["code"], "device_name": name, "platform": platform}).json()
        return {"X-Agent-Token": r["token"]}, r["person"]["id"], r["device"]["id"]

    zach, _, zach_dev = pair("zach", "Zach's iPhone")
    robin, mid, _ = pair({"name": "Robin", "role": "adult"}, "Robin's iPhone")
    kitchen, _, _ = pair({"name": "Kitchen", "role": "household"}, "Kitchen iPad", "ipados")

    routines.save({"name": "Unwind", "steps": ["Dim the lights", "Play something calm"], "audience": "private",
                   "trigger": {"type": "place", "place": "home", "on": "arrive"}}, person="zach", role="admin")
    routines.save({"name": "Porch light", "steps": ["Turn on the porch light"], "audience": "household",
                   "trigger": {"type": "place", "place": "Home", "on": "arrived"}}, person="zach", role="admin")

    def post(h, **body):
        return c.post("/v1/presence/place", headers=h, json=body).status_code

    def cards_for(pid, role):
        return [x["title"] + " | " + x.get("detail", "") for x in proactive.list_cards(person=pid, role=role)]

    print("\n── an arrival suggests the person's routines ──")
    ok &= check("204", post(zach, id="e1", event="arrived", place="home", at="2026-10-01T18:02:00-07:00"), 204)
    zc = cards_for("zach", "admin")
    ok &= check("Zach's private routine, to him", any(t.startswith("Time for Unwind") for t in zc), True)
    ok &= check("the household routine too", any(t.startswith("Time for Porch light") for t in zc), True)
    mc = cards_for(mid, "adult")
    ok &= check("Robin never sees his private one", any("Unwind" in t for t in mc), False)
    porch = [t for t in mc if "Porch light" in t]
    ok &= check("the household one reaches her, naming no one", (len(porch), "Zach" in porch[0] if porch else None),
                (1, False))

    print("\n── resends, debounce, and what's kept ──")
    n = len(proactive.list_cards())
    ok &= check("the same id again: 204, nothing new", (post(zach, id="e1", event="arrived", place="home"),
                                                        len(proactive.list_cards())), (204, n))
    post(zach, id="e2", event="arrived", place="home")
    ok &= check("a new arrival within 10 minutes: no second nudge", len(places.recent("zach")), 2)
    ok &= check("… and no new card", len(proactive.list_cards()), n)
    post(zach, id="e3", event="arrived", place="home", lat=47.66, lng=-117.42, address="1 Main St")
    import sqlite3
    con = sqlite3.connect(os.environ["PLACES_DB"])
    cols = [r[1] for r in con.execute("PRAGMA table_info(place_events)")]
    blob = " ".join(str(v) for row in con.execute("SELECT * FROM place_events") for v in row)
    con.close()
    ok &= check("no coordinates or address are ever kept", (any(k in cols for k in ("lat", "lng", "address")),
                                                          "47.66" in blob or "Main St" in blob), (False, False))

    print("\n── who may post, and whose events are whose ──")
    ok &= check("a shared household device can't", post(kitchen, id="k1", event="arrived", place="home"), 403)
    ok &= check("a bad event is refused", post(zach, id="b1", event="teleported", place="home"), 400)
    ok &= check("a place name over 40 characters is refused", post(zach, id="b2", event="arrived", place="x" * 41), 400)
    post(robin, id="m1", event="left", place="Work")
    ok &= check("Robin's events are hers", ([e["place"] for e in places.recent(mid)], [e["id"] for e in places.recent("zach")
                                                                                      if e["id"] == "m1"]), (["Work"], []))
    ok &= check("her leaving work doesn't nudge his arrival routines", any("Unwind" in t for t in cards_for(mid, "adult")), False)

    print("\n── leaving the household forgets them ──")
    r = c.delete(f"/v1/devices/{zach_dev}", headers=M)
    ok &= check("device removed", r.status_code, 200)
    ok &= check("his place events are gone, hers stay", (places.recent("zach"), len(places.recent(mid))), ([], 1))

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
