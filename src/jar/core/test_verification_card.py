#!/usr/bin/env python3
# gate: hermetic
"""The weekly verification card (10-04): built from needs-verification issues, never posted when there's nothing
or GitHub can't be read. Fake GitHub and a fake card writer.

Run: python3 scripts/test_verification_card.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import verification_card as vc  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


def api_with(n_jb, n_tn):
    def api(path):
        n = n_jb if "jellybean" in path else n_tn
        return {"items": [{"number": 100 + i, "title": f"Item {i}", "html_url": f"https://x/{i}"} for i in range(n)]}
    return api


cards = []
add = lambda *a, **k: cards.append((a, k))  # noqa: E731
out = vc.run(True, api=api_with(3, 1), add_card=add)
check("posts one card with the count", (out["posted"], out["count"], out["title"]), (True, 4, "4 things to check on your devices"))
a, k = cards[-1]
check("it replaces last week's (one key) and goes to the admin", (k["key"], k["to"], k["source"]), ("verify-weekly", "admin", "verification"))
check("a Walk me through it prompt", k["actions"][0]["label"], "Walk me through it")
check("lists items with their refs", "• Item 0 (jellybean#100)" in a[1], True)
out = vc.run(True, api=api_with(9, 3), add_card=add)
check("at most 8 shown, the rest counted", ("…and 4 more." in out["detail"], out["detail"].count("•")), (True, 8))
n = len(cards)
check("nothing awaiting: no card", (vc.run(True, api=api_with(0, 0), add_card=add)["posted"], len(cards)), (False, n))
check("GitHub unreadable: no card", (vc.run(True, api=lambda p: None, add_card=add)["why"], len(cards)), ("GitHub unreadable", n))
check("dry run doesn't post", (vc.run(False, api=api_with(1, 0), add_card=add)["posted"], len(cards)), (False, n))
check("singular title", vc.card_text([{"ref": "jellybean#1", "title": "X"}])[0], "1 thing to check on your devices")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
