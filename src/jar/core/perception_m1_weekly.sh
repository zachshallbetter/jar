#!/usr/bin/env bash
# Perception M1/M2 weekly run (titan-node#700, decided 10-09): export the week's HA history and adults' own place
# events to derived 15-minute windows (read-only; nothing raw kept; files purged after 30 days), then score them with
# rolling-origin baselines and the computed sufficiency gate, appending one counts-only line to the log.
# CPU only (no model is trained or run here), outside every eval window (Sundays 10:00 household time).
# The research code is a checkout of perception-is-how-we-experience (branch household-m1) at PERCEPTION_REPO.
set -euo pipefail
REPO=${PERCEPTION_REPO:-/ai/projects/perception}
OUT=${PERCEPTION_M1_DIR:-/ai/datasets/perception/m1}
LOG=${PERCEPTION_M1_LOG:-/ai/logs/perception-m1.jsonl}
export CUDA_VISIBLE_DEVICES=""                    # never a GPU
export PERCEPTION_M1_DIR="$OUT"
[ -f "$REPO/household/m2_eval.py" ] || { echo "no perception checkout at $REPO (household-m1 with m2_eval.py)" >&2; exit 1; }
cd "$REPO"
python3 household/m1_export.py
latest=$(ls -1 "$OUT"/windows-*.jsonl 2>/dev/null | sort | tail -1)
[ -n "$latest" ] || { echo "the export wrote no windows file" >&2; exit 1; }
python3 household/m2_eval.py "$latest" --log "$LOG" > /dev/null
tail -1 "$LOG"
