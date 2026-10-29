#!/usr/bin/env bash
# tmp_sweep.sh [--dry-run | install] (10-10 deep review): titan's /tmp held 53 GB of test leftovers, +4.8 GB/day, and
# Ubuntu's tmp.conf never ages /tmp (only a reboot clears it). This removes a top-level entry only when ALL hold:
#   it belongs to this user; it isn't a dotfile, a socket, systemd-private-* or ssh-*; nothing inside it changed in
#   TMP_SWEEP_AGE_MIN minutes (default 2 days); and no process has a file open under it (lsof).
# New leftovers stop at the source: run_deploy_gate.sh gives each gate run its own TMPDIR and removes it.
#   tmp_sweep.sh             sweep, printing a count and the bytes freed (names only with --verbose)
#   tmp_sweep.sh --dry-run   say what it would remove, remove nothing
#   tmp_sweep.sh install     the deploy's step: install server-setup/titan-tmp-sweep.{service,timer} when they differ
# TMP_SWEEP_DIR / TMP_SWEEP_AGE_MIN / TMP_SWEEP_OWNER are for the tests.
set -u
DIR=${TMP_SWEEP_DIR:-/tmp}
AGE=${TMP_SWEEP_AGE_MIN:-2880}
OWNER=${TMP_SWEEP_OWNER:-$(id -un)}
case "${1:-}" in
  install)
    SYSTEMCTL=${SYSTEMCTL:-systemctl}; SUDO=${SUDO-sudo -n}; SD=${SYSTEMD_DIR:-/etc/systemd/system}
    SRC="$(cd "$(dirname "$0")/.." && pwd)/server-setup"; changed=0
    for u in titan-tmp-sweep.service titan-tmp-sweep.timer; do
      [ -f "$SRC/$u" ] || { echo "tmp sweep unit: $SRC/$u missing" >&2; exit 1; }
      cmp -s "$SRC/$u" "$SD/$u" && continue
      $SUDO install -m 0644 "$SRC/$u" "$SD/$u" || { echo "tmp sweep unit: couldn't install $u" >&2; exit 1; }
      changed=1
    done
    [ "$changed" = 1 ] && { $SUDO $SYSTEMCTL daemon-reload || exit 1; }
    if ! $SYSTEMCTL is-enabled --quiet titan-tmp-sweep.timer 2>/dev/null || \
       ! $SYSTEMCTL is-active --quiet titan-tmp-sweep.timer 2>/dev/null; then
      $SUDO $SYSTEMCTL enable --now titan-tmp-sweep.timer || { echo "tmp sweep unit: couldn't enable the timer" >&2; exit 1; }
      changed=1
    fi
    [ "$changed" = 1 ] && echo "tmp sweep timer installed" || echo "tmp sweep timer current"
    exit 0 ;;
  --dry-run|-n) DRY=1 ;;
  ""|--verbose) DRY=0 ;;
  *) echo "usage: $0 [--dry-run | --verbose | install]" >&2; exit 2 ;;
esac
VERBOSE=0; [ "${1:-}" = --verbose ] && VERBOSE=1
# top-level names something still has open (lsof prints paths, never arguments)
REAL=$(cd "$DIR" 2>/dev/null && pwd -P || echo "$DIR")   # open paths come back resolved (/tmp may be a symlink)
# What's open, as paths: /proc on Linux (every fd, cwd and mapped file of this user's processes; titan has no lsof),
# lsof elsewhere. With neither, nothing can be proven unused, so the sweep refuses rather than guesses.
open_paths() {
  if [ -d /proc/self/fd ]; then
    for pd in /proc/[0-9]*; do
      [ -O "$pd" ] || continue
      for l in "$pd"/fd/* "$pd"/cwd; do readlink "$l" 2>/dev/null; done
      awk '$6 ~ /^\// {print $6}' "$pd"/maps 2>/dev/null
    done
  elif command -v lsof >/dev/null; then
    lsof -nP -u "$OWNER" -Fn 2>/dev/null | sed -n 's/^n//p'
  else
    echo "tmp sweep: can't see open files (no /proc, no lsof); refusing" >&2; exit 3
  fi
}
paths=$(open_paths) || exit 3
open=$(printf '%s\n' "$paths" | sed -n -e "s#^$DIR/##p" -e "s#^$REAL/##p" | cut -d/ -f1 | sort -u)
n=0; bytes=0
while IFS= read -r -d '' p; do
  name=${p##*/}
  case "$name" in .*|systemd-private-*|ssh-*) continue ;; esac
  [ -S "$p" ] && continue
  [ -n "$open" ] && grep -qxF -- "$name" <<<"$open" && continue
  [ -n "$(find "$p" -mmin "-$AGE" -print -quit 2>/dev/null)" ] && continue   # anything inside changed recently
  sz=$(du -sk "$p" 2>/dev/null | cut -f1); bytes=$((bytes + ${sz:-0}))
  n=$((n + 1))
  if [ "$DRY" = 1 ]; then echo "would remove $name"; continue; fi
  rm -rf -- "$p"
  [ "$VERBOSE" = 1 ] && echo "removed $name"
done < <(find "$DIR" -mindepth 1 -maxdepth 1 -user "$OWNER" ! -mmin "-$AGE" -print0 2>/dev/null)
echo "tmp sweep: $([ "$DRY" = 1 ] && echo would remove || echo removed) $n entries, $((bytes / 1024)) MB, in $DIR (older than $((AGE / 60)) h, owner $OWNER)"
