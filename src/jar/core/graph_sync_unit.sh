#!/usr/bin/env bash
# titan-graph-sync (#1447): the relational graph kept current by graph_sync --since every 10 minutes (spec §9).
#
#   graph_sync_unit.sh guard     the unit's ExecCondition: exit 1 (this run is skipped, not failed) while
#                                titan-household-eval or titan-model-eval is active, activating or reloading
#   graph_sync_unit.sh install   the deploy's step after the canary: copy server-setup/titan-graph-sync.{service,timer}
#                                into systemd only when they differ, daemon-reload, and enable --now the timer. It never
#                                restarts or runs the service itself; a run in progress finishes on the old unit.
#
# SYSTEMD_DIR, SYSTEMCTL and SUDO are for the tests (defaults /etc/systemd/system, systemctl, sudo -n).
set -u
UNITS="titan-graph-sync.service titan-graph-sync.timer"
EVAL_UNITS="titan-household-eval.service titan-model-eval.service"
SYSTEMCTL=${SYSTEMCTL:-systemctl}
SUDO=${SUDO-sudo -n}
DIR=${SYSTEMD_DIR:-/etc/systemd/system}
SRC="$(cd "$(dirname "$0")/.." && pwd)/server-setup"

case "${1:-}" in
  guard)
    for u in $EVAL_UNITS; do
      case "$($SYSTEMCTL show -p ActiveState --value "$u" 2>/dev/null)" in
        active|activating|reloading) echo "graph sync skipped: ${u%.service} is running"; exit 1 ;;
      esac
    done
    exit 0 ;;
  install)
    changed=0
    for u in $UNITS; do
      [ -f "$SRC/$u" ] || { echo "graph sync unit: $SRC/$u missing" >&2; exit 1; }
      cmp -s "$SRC/$u" "$DIR/$u" && continue
      $SUDO install -m 0644 "$SRC/$u" "$DIR/$u" || { echo "graph sync unit: couldn't install $u" >&2; exit 1; }
      changed=1
    done
    if [ "$changed" = 1 ]; then
      $SUDO $SYSTEMCTL daemon-reload || { echo "graph sync unit: daemon-reload failed" >&2; exit 1; }
    fi
    if ! $SYSTEMCTL is-enabled --quiet titan-graph-sync.timer 2>/dev/null || \
       ! $SYSTEMCTL is-active --quiet titan-graph-sync.timer 2>/dev/null; then
      $SUDO $SYSTEMCTL enable --now titan-graph-sync.timer || { echo "graph sync unit: couldn't enable the timer" >&2; exit 1; }
      changed=1
    fi
    [ "$changed" = 1 ] && echo "graph sync timer installed" || echo "graph sync timer current" ;;
  *) echo "usage: $0 guard|install" >&2; exit 2 ;;
esac
