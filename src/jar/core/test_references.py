#!/usr/bin/env python3
# gate: hermetic
"""References (#446 step 1): pointers to items on a person's device, never their bodies; written by the owner's own
device, read by the owner and whoever the audience admits; derived facts may cite one.

Run: python3 scripts/test_references.py
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="titan-refs-test-")
os.environ["REFERENCES_DB"] = os.path.join(_TMP, "references.db")
os.environ["HOUSEHOLD_DB"] = os.path.join(_TMP, "household.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import provenance  # noqa: E402
import references as R  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


def err(f, *a, **k):
    try:
        f(*a, **k)
        return None
    except R.RefError as e:
        return e.status


bill = {"kind": "mail", "native_id": "<abc@mail.pge.com>", "title": "PG&E bill", "content_hash": "sha256:" + "a" * 64,
        "dates": {"received": "2026-10-01", "due": "2026-10-20", "secret": "x"}}
r = R.put("zach", "admin", "mac1", bill)
check("written: an id, the owner, private by default", (R.REF_ID_RX.match(r["id"]) is not None, r["owner"], r["audience"]),
      (True, "zach", "private"))
check("only the known date keys kept", r["dates"], {"due": "2026-10-20", "received": "2026-10-01"})
check("the same item again updates it, one record", (R.put("zach", "admin", "mac1", {**bill, "title": "PG&E bill (Oct)"})["id"],
                                                     len(R.visible("zach", "admin"))), (r["id"], 1))

print("\n── never a body ──")
for k in ("body", "text", "content", "html", "image", "snippet"):
    check(f"refused: {k}", err(R.put, "zach", "admin", "mac1", {**bill, k: "Dear customer…"}), 400)

print("\n── who writes ──")
check("no device (the master token): refused", err(R.put, "zach", "admin", None, bill), 403)
check("a shared device: refused", err(R.put, "household", "household", "tv1", bill), 403)
check("bad kind", err(R.put, "zach", "admin", "mac1", {**bill, "kind": "secrets"}), 400)
check("someone else's: not found", err(R.patch, r["id"], "p_mir", "adult", "mirphone", {"title": "x"}), 404)
check("can't change what it points to", err(R.patch, r["id"], "zach", "admin", "mac1", {"native_id": "other"}), 400)
check("the owner's device updates title and audience",
      (lambda x: (x["title"], x["audience"]))(R.patch(r["id"], "zach", "admin", "iphone1", {"title": "PG&E", "audience": "adults"})),
      ("PG&E", "adults"))

print("\n── who reads ──")
R.put("p_mir", "adult", "mirphone", {"kind": "scan", "native_id": "IMG_0042", "title": "Warranty"})
R.put("zach", "admin", "mac1", {"kind": "file", "native_id": "Documents/tax.pdf", "title": "Taxes"})
R.put("zach", "admin", "mac1", {"kind": "photo", "native_id": "PH-1", "title": "Recital", "audience": "household"})
titles = lambda pid, role, kind=None: sorted(x["title"] for x in R.visible(pid, role, kind))   # noqa: E731
check("Zach: his own (all), not Robin's private scan", titles("zach", "admin"), ["PG&E", "Recital", "Taxes"])
check("Robin: hers, the adults' bill, the household photo; never Zach's private file", titles("p_mir", "adult"),
      ["PG&E", "Recital", "Warranty"])
check("a kid: the household photo only", titles("p_ava", "kid"), ["Recital"])
check("a shared screen: the household photo only", titles("household", "household"), ["Recital"])
check("filter by kind", titles("zach", "admin", "mail"), ["PG&E"])

print("\n── a derived fact cites it ──")
p = provenance.make("mail", owner="zach", kind="derived", reference=r["id"])
check("provenance carries the reference", p["reference"], r["id"])
try:
    provenance.make("mail", reference="not-an-id")
    check("a bad reference id refused", False, True)
except ValueError:
    check("a bad reference id refused", True, True)

print("\n── routes ──")
import household_identity as hh  # noqa: E402
check("GET for everyone; POST/PATCH for people, not shared screens",
      (hh.route_allowed("household", "GET", "/v1/references"), hh.route_allowed("household", "POST", "/v1/references"),
       hh.route_allowed("kid", "POST", "/v1/references"), hh.route_allowed("adult", "PATCH", f"/v1/references/{r['id']}"),
       hh.route_allowed("adult", "DELETE", f"/v1/references/{r['id']}")),
      (True, False, True, True, False))
check("the store file is private", oct(os.stat(os.environ["REFERENCES_DB"]).st_mode & 0o777), "0o600")

print("── file paths and the owner's own list (#1135) ──")
for bad in ("/Users/alex/tax.pdf", "~/tax.pdf", "../etc/passwd", "Documents//x.pdf", ".ssh/id_ed25519", "Documents/.env",
            "Library/Keychains/login.keychain-db", "work/server.pem", ".aws/credentials", "a/./b.pdf", "C:/x.pdf"):
    try:
        R.clean_file_path(bad)
        check(f"refused: {bad}", "kept", 400)
    except R.RefError as e:
        check(f"refused: {bad}", e.status, 400)
check("a home-relative path is kept", R.clean_file_path("Documents/Lease 2026.pdf"), "Documents/Lease 2026.pdf")
R.put("p_robin", "adult", "mac-r", {"kind": "file", "native_id": "Documents/Lease.pdf", "title": "Lease \u202e.pdf\n"})
R.put("p_jordan", "adult", "mac-j", {"kind": "file", "native_id": "Documents/Bike.pdf", "title": "Bike"})
mine = R.mine("p_robin", "adult")
check("only the caller's own, never the native id itself", (len(mine), mine[0]["native"], "native_id" in mine[0],
                                                             any(x["title"] == "Bike" for x in mine)), (1, "path", False, False))
check("the title is cleaned (no control characters, one line)", "\n" not in (mine[0]["title"] or ""), True)
try:
    R.mine("screen", "household")
    check("a shared screen: 403", "listed", 403)
except R.RefError as e:
    check("a shared screen: 403", e.status, 403)
import household_identity as hh  # noqa: E402
check("route: adults and kids, never a screen", [hh.route_allowed(r, "GET", "/v1/me/references")
                                                  for r in ("admin", "adult", "kid", "household")], [True, True, True, False])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
