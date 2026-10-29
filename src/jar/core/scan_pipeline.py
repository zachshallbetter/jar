#!/usr/bin/env python3
"""The scans stages as one chain (titan-node#1498): envelope, dedupe, junk, cycles, threads, notices, bundles, and the privacy check over all of it (#1508).

Each stage is a function of the scans and the previous stage's result (`stage(scans, pred)`), and each module's own
`pipeline` is that stage on top of the stages it was written for, so every one still scores on its own. Here they run in
order, so a bill is on its cycle, a claim letter is in its thread and a notice has its event key in one prediction.

  python3 scripts/scans_score.py --pipeline scan_pipeline:pipeline
  python3 scripts/scans_score.py --pipeline scan_pipeline:pipeline --write scripts/evalsets/scans/pipeline.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_bundles as BU  # noqa: E402
import scan_cycles as C  # noqa: E402
import scan_dedupe as D  # noqa: E402
import scan_envelope as E  # noqa: E402
import scan_junk as J  # noqa: E402
import scan_notices as N  # noqa: E402
import scan_privacy as PV  # noqa: E402
import scan_threads as T  # noqa: E402

STAGES = (("envelope", None), ("dedupe", D.stage), ("junk", J.stage), ("cycles", C.stage), ("threads", T.stage), ("notices", N.stage), ("bundles", BU.stage),
          ("privacy", PV.stage))


def chain(scans: list[dict], people: dict[str, dict] | None = None) -> dict:
    """The seven stages that derive things, as they ran before the privacy check (#1508): what the check is held against.
    `people` is the household ({id: {role, name, parents}}); the benchmark's household when None."""
    pred = E.pipeline(scans)
    for name, stage in STAGES[1:-1]:
        pred = stage(scans, pred, people=people) if name == "notices" and people is not None else stage(scans, pred)
    return pred


def pipeline(scans: list[dict], people: dict[str, dict] | None = None) -> dict:
    """The scorer's pipeline: the chain over the scans of the household's people, then the privacy check over everything it
    derived. A scan from a scanner nobody owns (the shared household scanner) derives nothing: it is held until an adult claims it."""
    mine, _ = PV.claimed(scans, people)
    return PV.stage(scans, chain(mine, people), people=people)
