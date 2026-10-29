#!/usr/bin/env python3
"""Load the golden household into a fixture box's stores (sandbox_box.sh up --fixture): every store the loader writes,
graph.db built as of the fixture's day, and box_fixture's marker. Nothing real is read or written.
Run: python3 scripts/box_fixture_load.py <the box's stores dir>   → counts per store kind
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: box_fixture_load.py <stores dir>", file=sys.stderr)
        return 2
    stores = Path(argv[0]).resolve()
    if str(stores).startswith("/ai/datasets"):
        print("refused: a fixture is never loaded into the live stores", file=sys.stderr)
        return 2
    stores.mkdir(parents=True, exist_ok=True)
    os.environ["GRAPH_GOLDEN_DIR"] = str(stores)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import graph_golden_load as GL   # loads every store under GRAPH_GOLDEN_DIR at import
    import box_fixture
    db = GL.build()
    box_fixture.write_marker(stores)
    for p in stores.rglob("*"):
        if p.is_file():
            os.chmod(p, 0o600)
    print(json.dumps({"stores": str(stores), "graph": db.name, "people": len(GL.H["people"]),
                      "items": len(GL.H.get("items") or []), "now": GL.H["now"], "zone": GL.H["zone"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
