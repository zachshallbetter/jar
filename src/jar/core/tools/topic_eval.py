#!/usr/bin/env python3
"""Topic precision on titan (titan-node#951 (e)): before a similar topic can ever help accept an edge (phase 2), how
often is a topic-only candidate really about the same matter?

Samples the linker's topic-only candidates (a similar topic and nothing typed) and as many random pairs, and asks the
local judge (:8198, the same one cite_precision uses; nothing leaves titan) whether each pair is about the same matter
(the same order, bill, project or conversation), from each item's title and its KB text (for mail, the envelope's
summary: an evaluation read, never stored). Prints counts and precision only, never a title or text.

  python3 scripts/tools/topic_eval.py [--n 40] [--seed 7]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROMPT = """Two items from one household's mail and records. Are they about the SAME matter (the same order, bill,
account issue, project or conversation), not just the same kind of thing? Answer JSON {{"same": true}} or
{{"same": false}} only.

A: {a}

B: {b}"""


def judge(a: str, b: str) -> bool | None:
    import cite_precision as C
    body = json.dumps({"messages": [{"role": "user", "content": "/no_think\n" + PROMPT.format(a=a[:1200], b=b[:1200])}],
                       "temperature": 0, "max_tokens": 40}).encode()
    req = urllib.request.Request(C.JUDGE_URL + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read())["choices"][0]["message"]["content"]
        m = re.search(r'"same"\s*:\s*(true|false)', re.sub(r"<think>.*?</think>", "", out, flags=re.S))
        return (m.group(1) == "true") if m else None
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    import hooks
    import linker
    con = hooks.connect()
    items = linker.load(con)
    ctx = linker.topic_ctx(con)
    if not ctx:
        print(json.dumps({"error": "no topic vectors yet"}))
        return 1
    edges = linker.score(items, linker.statements(con), ctx)
    topic_only = [e for e in edges if e.state == "candidate" and not e.typed and e.measured.get("topic", {}).get("near")]
    titles, _, _ = linker.labels(items)
    texts: dict[str, str] = {}
    try:
        import kb_query
    except Exception:  # noqa: BLE001
        kb_query = None

    def describe(i: str) -> str:
        if i not in texts:
            body = ""
            if kb_query is not None:
                try:
                    body = (kb_query._chunks(i)[0] or "")[:800]
                except Exception:  # noqa: BLE001
                    body = ""
            texts[i] = f"{titles.get(i) or '(no title)'}\n{body}".strip()
        return texts[i]
    rnd = random.Random(a.seed)
    sample = rnd.sample(topic_only, min(a.n, len(topic_only)))
    ids = sorted(items)
    rand_pairs = [tuple(rnd.sample(ids, 2)) for _ in range(len(sample))]
    res = {"topic_only_candidates": len(topic_only), "judged": 0, "same": 0, "unclear": 0, "random_judged": 0,
           "random_same": 0}
    for e in sample:
        v = judge(describe(e.a), describe(e.b))
        if v is None:
            res["unclear"] += 1
            continue
        res["judged"] += 1
        res["same"] += v
    for x, y in rand_pairs:
        v = judge(describe(x), describe(y))
        if v is not None:
            res["random_judged"] += 1
            res["random_same"] += v
    res["precision"] = round(res["same"] / res["judged"], 3) if res["judged"] else None
    res["random_rate"] = round(res["random_same"] / res["random_judged"], 3) if res["random_judged"] else None
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
