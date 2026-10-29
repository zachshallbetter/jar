#!/usr/bin/env bash
# boot_resilience.sh — make titan come back by itself after brownouts and reboots.
#
# Layers (in order):
#   1. BIOS "restore AC power loss" = Power On  (hardware; listed, never auto-set)
#   2. Platform units enabled (ssh, tailscale, docker, nvidia-persistenced, network)
#   3. Titan always-on services / timers / paths enabled (WantedBy=multi-user.target)
#   4. Runtime + broker units from runtime-registry.json enabled
#
# Usage (on titan, or via `bash scripts/titan.sh boot ...` from the Mac):
#   boot_resilience.sh check [--json]     report only (default); exit 1 if anything required is missing
#   boot_resilience.sh apply              enable --now every required unit that exists but isn't enabled
#   boot_resilience.sh install            copy differing server-setup units, daemon-reload, then apply
#
# SYSTEMD_DIR / SYSTEMCTL / SUDO / BOOT_RESILIENCE_UNITS / REGISTRY are for tests.
# Written for bash 3.2+ (macOS /bin/bash) as well as bash 5 on titan.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
UNITS_JSON="${BOOT_RESILIENCE_UNITS:-$SCRIPTS/boot_resilience_units.json}"
REGISTRY="${REGISTRY:-$SCRIPTS/runtime-registry.json}"
SRC="${BOOT_RESILIENCE_SRC:-$ROOT/server-setup}"
DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
FIREWALL_DST="${BOOT_RESILIENCE_FIREWALL_DST:-/etc/titan/firewall.nft}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
# shellcheck disable=SC2086
SUDO="${SUDO-sudo -n}"

cmd="${1:-check}"
json=0
if [ "$#" -gt 0 ]; then shift; fi
for a in "$@"; do
  case "$a" in
    --json) json=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

if [ ! -f "$UNITS_JSON" ]; then
  echo "boot resilience: missing $UNITS_JSON" >&2
  exit 1
fi

# --- helpers ---------------------------------------------------------------

unit_state() {
  # prints: missing | enabled | disabled | static | alias | generated | indirect | masked | bad
  local u="$1"
  if ! $SYSTEMCTL cat "$u" >/dev/null 2>&1; then
    echo missing
    return 0
  fi
  # is-enabled exits non-zero for disabled; capture text either way
  local out
  out="$($SYSTEMCTL is-enabled "$u" 2>/dev/null || true)"
  if [ -n "$out" ]; then
    echo "$out"
  else
    echo bad
  fi
}

is_active() {
  $SYSTEMCTL is-active --quiet "$1" 2>/dev/null
}

enable_now() {
  local u="$1"
  # shellcheck disable=SC2086
  $SUDO $SYSTEMCTL enable --now "$u"
}

copy_unit_if_needed() {
  local name="$1"
  local src="$SRC/$name"
  local dst="$DIR/$name"
  [ -f "$src" ] || return 1
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    return 2
  fi
  # shellcheck disable=SC2086
  $SUDO install -m 0644 "$src" "$dst"
  return 0
}

# newline-separated lists (bash 3.2-safe; empty means no lines)
PLATFORM_LIST=""
SERVICES_LIST=""
TIMERS_LIST=""
PATH_UNITS_LIST=""
OPTIONAL_LIST=""
BIOS_LIST=""
ANY_OF_JSON="[]"
REQUIRED_LIST=""

load_lists() {
  eval "$(python3 - "$UNITS_JSON" "$REGISTRY" <<'PY'
import json, sys, shlex
u = json.load(open(sys.argv[1]))
reg_path = sys.argv[2]
platform = list(u.get("platform") or [])
any_of = u.get("platform_any_of") or []
services = list(u.get("always_on_services") or [])
timers = list(u.get("always_on_timers") or [])
paths = list(u.get("always_on_paths") or [])
optional = list(u.get("optional") or [])
bios = list(u.get("bios") or [])
reg_units = []
if u.get("registry_units"):
    try:
        d = json.load(open(reg_path))
        for r in list(d.get("runtimes") or []) + list(d.get("brokers") or []):
            reg_units.append(r["name"] + ".service")
    except OSError:
        pass

def uniq(xs):
    seen = set(); out = []
    for x in xs:
        if x not in seen:
            seen.add(x); out.append(x)
    return out

services = uniq(services + reg_units)

def emit_nl(name, xs):
    # Assign a $'...'-style value via printf %q of the joined string with real newlines
    body = "\n".join(xs)
    print("%s=%s" % (name, shlex.quote(body)))

emit_nl("PLATFORM_LIST", platform)
emit_nl("SERVICES_LIST", services)
emit_nl("TIMERS_LIST", timers)
emit_nl("PATH_UNITS_LIST", paths)
emit_nl("OPTIONAL_LIST", optional)
emit_nl("BIOS_LIST", bios)
print("ANY_OF_JSON=" + shlex.quote(json.dumps(any_of)))
required = platform + services + timers + paths
emit_nl("REQUIRED_LIST", uniq(required))
PY
)"
}

# Iterate lines calling a function name with the line as $1
for_lines() {
  local list="$1"
  local fn="$2"
  [ -n "$list" ] || return 0
  local oldifs="$IFS" line
  IFS='
'
  # shellcheck disable=SC2086
  set -- $list
  IFS="$oldifs"
  for line in "$@"; do
    [ -n "$line" ] || continue
    "$fn" "$line"
  done
}

OK_LIST=""
MISS_LIST=""
DIS_LIST=""
OPT_REPORT=""
ANY_FAIL=""

append_nl() {
  # append_nl varname value  (bash 3.2-safe; no empty-array tricks)
  local _n="$1" _v="$2" _cur
  eval "_cur=\${$_n-}"
  if [ -n "$_cur" ]; then
    eval "$_n=\"\$(printf '%s\\n%s' \"\$_cur\" \"\$_v\")\""
  else
    eval "$_n=\"\$_v\""
  fi
}

classify_one() {
  local u="$1" st
  st="$(unit_state "$u")"
  case "$st" in
    enabled|enabled-runtime|static|indirect)
      append_nl OK_LIST "$u"
      ;;
    missing)
      append_nl MISS_LIST "$u"
      ;;
    *)
      append_nl DIS_LIST "$u=$st"
      ;;
  esac
}

optional_one() {
  local u="$1" st
  st="$(unit_state "$u")"
  append_nl OPT_REPORT "$u=$st"
}

check_any_of_groups() {
  # Avoid a pipe/subshell so append_nl updates the parent lists.
  local groups group_csv found states oldifs u st
  groups="$(python3 - "$ANY_OF_JSON" <<'PY'
import json, sys
for g in json.loads(sys.argv[1]):
    print(",".join(g))
PY
)"
  [ -n "$groups" ] || return 0
  oldifs="$IFS"
  IFS='
'
  # shellcheck disable=SC2086
  set -- $groups
  IFS="$oldifs"
  for group_csv in "$@"; do
    [ -n "$group_csv" ] || continue
    found=0
    states=""
    oldifs="$IFS"
    IFS=','
    # shellcheck disable=SC2086
    set -- $group_csv
    IFS="$oldifs"
    for u in "$@"; do
      st="$(unit_state "$u")"
      if [ -n "$states" ]; then states="$states $u=$st"; else states="$u=$st"; fi
      case "$st" in
        enabled|enabled-runtime|static|indirect) found=1 ;;
      esac
    done
    if [ "$found" -eq 1 ]; then
      append_nl OK_LIST "network-stack:$states"
    else
      append_nl ANY_FAIL "network-stack:$states"
    fi
  done
}

gather() {
  OK_LIST=""; MISS_LIST=""; DIS_LIST=""; OPT_REPORT=""; ANY_FAIL=""
  check_any_of_groups
  for_lines "$REQUIRED_LIST" classify_one
  for_lines "$OPTIONAL_LIST" optional_one
}

count_lines() {
  local s="${1-}"
  if [ -z "$s" ]; then echo 0; return; fi
  printf '%s\n' "$s" | grep -c . || true
}

default_target=""

report() {
  local n_ok n_miss n_dis n_any
  n_ok="$(count_lines "$OK_LIST")"
  n_miss="$(count_lines "$MISS_LIST")"
  n_dis="$(count_lines "$DIS_LIST")"
  n_any="$(count_lines "$ANY_FAIL")"
  if [ "$json" -eq 1 ]; then
    OK_LIST="$OK_LIST" MISS_LIST="$MISS_LIST" DIS_LIST="$DIS_LIST" \
    ANY_FAIL="$ANY_FAIL" OPT_REPORT="$OPT_REPORT" BIOS_LIST="$BIOS_LIST" \
    DEFAULT_TARGET="$default_target" python3 - <<'PY'
import json, os
def lines(k):
    v = os.environ.get(k) or ""
    return [x for x in v.splitlines() if x]
print(json.dumps({
    "default_target": os.environ.get("DEFAULT_TARGET") or "unknown",
    "ok": lines("OK_LIST"),
    "missing_unit_file": lines("MISS_LIST"),
    "not_enabled": lines("DIS_LIST"),
    "platform_any_of_fail": lines("ANY_FAIL"),
    "optional": lines("OPT_REPORT"),
    "bios": lines("BIOS_LIST"),
}, indent=2))
PY
    return 0
  fi
  echo "boot resilience: default-target=$default_target"
  echo "  ok:              $n_ok"
  echo "  missing unit:    $n_miss"
  echo "  not enabled:     $n_dis"
  echo "  network any-of:  $n_any failing groups"
if [ -n "$MISS_LIST" ]; then
    echo "missing unit files (install unit or deploy runtime first):"
    printf '%s\n' "$MISS_LIST" | sed 's/^/  - /'
  fi
  if [ -n "$DIS_LIST" ]; then
    echo "installed but not enabled for boot:"
    printf '%s\n' "$DIS_LIST" | sed 's/^/  - /'
  fi
  if [ -n "$ANY_FAIL" ]; then
    echo "platform network stack not enabled:"
    printf '%s\n' "$ANY_FAIL" | sed 's/^/  - /'
  fi
  echo "optional (not required):"
  if [ -n "$OPT_REPORT" ]; then
    printf '%s\n' "$OPT_REPORT" | sed 's/^/  - /'
  else
    echo "  (none)"
  fi
  echo "BIOS / power (operator, once per board):"
  if [ -n "$BIOS_LIST" ]; then
    printf '%s\n' "$BIOS_LIST" | sed 's/^/  - /'
  fi
  if [ "$default_target" != "multi-user.target" ] && [ "$default_target" != "graphical.target" ]; then
    echo "WARNING: default target is '$default_target' (want multi-user.target)"
  fi
}

do_apply() {
  local changed=0 u st
  if [ "$default_target" != "multi-user.target" ] && [ "$default_target" != "graphical.target" ]; then
    # shellcheck disable=SC2086
    $SUDO $SYSTEMCTL set-default multi-user.target
    echo "set-default multi-user.target"
    changed=1
    default_target="multi-user.target"
  fi

  apply_one() {
    local u="$1" st
    st="$(unit_state "$u")"
    case "$st" in
      missing)
        echo "skip (no unit file): $u"
        return 0
        ;;
      enabled|enabled-runtime|static|indirect)
        case "$u" in
          *.timer|*.path)
            if ! is_active "$u"; then
              # shellcheck disable=SC2086
              if $SUDO $SYSTEMCTL start "$u"; then
                echo "started $u"
                changed=1
              fi
            fi
            ;;
        esac
        return 0
        ;;
      *)
        if enable_now "$u"; then
          echo "enabled $u"
          changed=1
        else
          echo "FAILED enable $u" >&2
        fi
        ;;
    esac
  }
  for_lines "$REQUIRED_LIST" apply_one

# platform any-of: enable the first existing unit in each group if none enabled
  local groups group_csv need oldifs
  groups="$(python3 - "$ANY_OF_JSON" <<'PY'
import json, sys
for g in json.loads(sys.argv[1]):
    print(",".join(g))
PY
)"
  if [ -n "$groups" ]; then
    oldifs="$IFS"
    IFS='
'
    # shellcheck disable=SC2086
    set -- $groups
    IFS="$oldifs"
    for group_csv in "$@"; do
      [ -n "$group_csv" ] || continue
      oldifs="$IFS"; IFS=','; set -- $group_csv; IFS="$oldifs"
      need=1
      for u in "$@"; do
        st="$(unit_state "$u")"
        case "$st" in
          enabled|enabled-runtime|static|indirect) need=0; break ;;
        esac
      done
      [ "$need" -eq 1 ] || continue
      for u in "$@"; do
        st="$(unit_state "$u")"
        [ "$st" = "missing" ] && continue
        if enable_now "$u"; then
          echo "enabled $u (network stack)"
          changed=1
        fi
        break
      done
    done
  fi

  if [ "$changed" -eq 0 ]; then
    echo "boot resilience: nothing to enable"
  else
    echo "boot resilience: apply done"
  fi
}

do_install() {
  local changed=0 u bare rc
  # Build candidate list in python to avoid associative arrays
  local candidates
  candidates="$(python3 - "$SERVICES_LIST" "$TIMERS_LIST" "$PATH_UNITS_LIST" <<'PY'
import sys
seen=set(); out=[]
def add(xs):
    for x in (xs or "").splitlines():
        x=x.strip()
        if x and x not in seen:
            seen.add(x); out.append(x)
add(sys.argv[1]); add(sys.argv[2]); add(sys.argv[3])
add("titan-firewall.nft")
for t in (sys.argv[2] or "").splitlines():
    t=t.strip()
    if t.endswith(".timer"):
        add(t[:-6]+".service")
print("\n".join(out))
PY
)"
  install_one() {
    local u="$1" rc=0
    [ -f "$SRC/$u" ] || return 0
    case "$u" in
      *.nft)
        local fw_dir
        fw_dir="$(dirname "$FIREWALL_DST")"
        # shellcheck disable=SC2086
        $SUDO install -d -m 0755 "$fw_dir"
        if [ ! -f "$FIREWALL_DST" ] || ! cmp -s "$SRC/$u" "$FIREWALL_DST"; then
          # shellcheck disable=SC2086
          $SUDO install -m 0644 "$SRC/$u" "$FIREWALL_DST"
          echo "installed $FIREWALL_DST"
          changed=1
        fi
        ;;
      *)
        rc=0
        copy_unit_if_needed "$u" || rc=$?
        if [ "$rc" -eq 0 ]; then
          echo "installed $u"
          changed=1
        fi
        ;;
    esac
  }
  for_lines "$candidates" install_one
  if [ "$changed" -eq 1 ]; then
    # shellcheck disable=SC2086
    $SUDO $SYSTEMCTL daemon-reload
  fi
  do_apply
}

# --- main -----------------------------------------------------------------

load_lists
default_target="$($SYSTEMCTL get-default 2>/dev/null || echo unknown)"
gather

case "$cmd" in
  check|"")
    report
    hard=0
    [ -z "$DIS_LIST" ] || hard=1
    [ -z "$ANY_FAIL" ] || hard=1
    if [ "$default_target" != "multi-user.target" ] && [ "$default_target" != "graphical.target" ]; then
      hard=1
    fi
    # missing runtime/broker unit files are soft; other missing always-on units are hard
    if [ -n "$MISS_LIST" ]; then
      while IFS= read -r u; do
        [ -n "$u" ] || continue
        case "$u" in
          *-runtime.service|*-broker.service|llm-model-broker.service|image-broker.service|decision-broker.service)
            ;;
          *)
            hard=1
            ;;
        esac
      done <<EOF
$MISS_LIST
EOF
    fi
    exit "$hard"
    ;;
  apply)
    do_apply
    gather
    report
    ;;
  install)
    do_install
    gather
    report
    ;;
  *)
    echo "usage: $0 check|apply|install [--json]" >&2
    exit 2
    ;;
esac
