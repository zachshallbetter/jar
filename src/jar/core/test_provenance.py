#!/usr/bin/env python3
"""#393: one provenance shape and one digest. Hermetic."""
from __future__ import annotations

import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os_acl, tempfile as _tf_acl  # noqa: E402
_os_acl.environ.setdefault("KB_ACL_DB", _os_acl.path.join(_tf_acl.mkdtemp(prefix="titan-acl-"), "kb_acl.db"))  # #1273: ingest writes sharing rows; never titan's own
import provenance as pv  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


check("canonical JSON: sorted keys, no whitespace, UTF-8", pv.canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}')
check("the digest ignores key order", pv.digest({"b": 1, "a": [1, 2]}) == pv.digest({"a": [1, 2], "b": 1}))
check("the digest format is sha256:<64 hex>", pv.is_digest(pv.digest({})) and not pv.is_digest("abc"))
check("a fixed vector stays fixed", pv.digest({"a": 1}) == "sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862")

# The grant contract's args digest (#395/#382): NFC, and the vector the apps must reproduce.
t, _ = pv.ARGS_DIGEST_VECTOR
check("the shared args-digest vector", pv.args_digest(t["tool"], t["args"]) == pv.ARGS_DIGEST_VECTOR[1]
      == "sha256:94ee7f923a07d5f4e54d9c8b0dfc91e21fb8f742e1d26584dd152eec5e532809")
dec = unicodedata.normalize("NFD", "café")
check("composed and decomposed text digest the same (NFC)",
      pv.args_digest("send_message", {"text": dec}) == pv.args_digest("send_message", {"text": "café"}))
check("a different recipient is a different digest",
      pv.args_digest("send_message", {"to": "Ava", "text": "x"}) != pv.args_digest("send_message", {"to": "Leo", "text": "x"}))

p = pv.make("mail", upstream_id="m1@x", item_id="cx-mail-1", observed_at="2026-09-29T17:30:33Z", owner="zach", of={"t": 1})
check("make: the one shape, absent fields left out", set(p) == {"source", "upstream_id", "item_id", "observed_at", "owner",
                                                                  "audience", "kind", "digest"} and p["kind"] == "observed", str(p))
check("make: an epoch time becomes ISO", pv.make("chat", observed_at=0)["observed_at"] == "1970-01-01T00:00:00Z")
try:
    pv.make("x", kind="guessed")
    check("an unknown kind is refused", False)
except ValueError:
    check("an unknown kind is refused", True)
try:
    pv.make("x", observed_at="yesterday")
    check("a non-ISO date is refused", False)
except ValueError:
    check("a non-ISO date is refused", True)
check("an extension keeps its own fields", pv.make("world", origin="household.yaml", label="vet")["origin"] == "household.yaml")

N = pv.normalize
c = N({"source": "mail", "source_id": "m1@x", "observed_at": "2026-09-29", "account": "a", "synced_at": "2026-10-01T00:00:00Z",
       "owner": "zach", "audience": "private"})
check("connector prov: source_id is the upstream id", c["upstream_id"] == "m1@x" and "item_id" not in c and c["kind"] == "observed", str(c))
mb = N({"source": "mail", "source_id": "cx-mail-abc", "owner": "zach", "audience": "private", "extracted_by": "local"})
check("mail-bill prov: a cx- id is titan's item id, and it's derived", mb["item_id"] == "cx-mail-abc" and "upstream_id" not in mb
      and mb["kind"] == "derived", str(mb))
check("chat records are declared", N({"source": "chat", "owner": "zach", "audience": "private"})["kind"] == "declared")
kb = N({"saved": "2026-09-30T10:00:00Z", "origin": "phone", "owner": "zach", "audience": "household"})
check("KB note metadata: saved is when it was true, origin the source", kb["observed_at"] == "2026-09-30T10:00:00Z"
      and kb["source"] == "phone" and kb["audience"] == "household", str(kb))
w = N({"source": "people", "kind": "stated", "ref": "p_zach", "origin": "household.yaml", "label": "Zach's birthday",
       "observed_at": "2026-01-01", "prior": None, "by": "zach", "owner": "zach", "audience": "household"})
check("world evidence: its own kind kept as evidence_kind; origin, label and by kept", w["evidence_kind"] == "stated"
      and w["origin"] == "household.yaml" and w["label"] == "Zach's birthday" and w["kind"] == "declared" and w["item_id"] == "p_zach", str(w))
check("new-shape records pass through", N(p)["digest"] == p["digest"] and N(p)["item_id"] == "cx-mail-1")
check("nothing in, nothing out", N(None) == {} and N({}) == {})

# Adopted: connectors write the new fields beside the old ones; mail-bill records say derived.
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "connectors_runner.py")).read()
check("connectors write upstream_id, item_id, kind and digest", all(f'"{k}": ' in src for k in ("upstream_id", "item_id", "kind", "digest")))
mbs = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mail_bills.py")).read()
check("mail-bill records are kind derived", '"kind": "derived"' in mbs)

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all provenance checks passed'}")
sys.exit(1 if failures else 0)
