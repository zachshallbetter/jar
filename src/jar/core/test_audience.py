#!/usr/bin/env python3
"""audience.py: the one who-sees-what rule. Run: python3 scripts/test_audience.py"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import audience as A  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


P = [("zach", "admin"), ("p_mir", "adult"), ("p_ava", "kid"), ("household", "household")]
def who(aud, owner=None):
    return [p for p, r in P if A.visible(aud, p, r, owner=owner)]

check("private: the owner only", who("private", owner="zach"), ["zach"])
check("private with no owner: nobody", who("private"), [])
check("household: everyone", who("household"), ["zach", "p_mir", "p_ava", "household"])
check("adults", who("adults"), ["zach", "p_mir"])
check("admins: system info", who("admins"), ["zach"])
check("a list, plus its owner", who('["p_mir"]', owner="zach"), ["zach", "p_mir"])
check("internal caller sees all", A.visible("private", None, None, owner="zach"), True)
check("normalize names a list", A.normalize("Robin, zach"), '["robin", "zach"]')
check("normalize default", (A.normalize(None), A.normalize(None, default="private")), ("admins", "private"))
check("share private with Robin", A.widen("private", ["p_mir"]), '["p_mir"]')
check("share again adds", A.widen('["p_mir"]', ["p_ava"]), '["p_ava", "p_mir"]')
check("share with the household", A.widen('["p_mir"]', "household"), "household")
check("sharing never narrows", A.widen("household", ["p_mir"]), "household")
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
