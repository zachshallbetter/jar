# shellcheck shell=bash
# The gate tests' environment (titan-node#996): an allowlist, never what the deploy's shell happens to hold.
# deploy_titan_node.sh sources agent.env for the restart and the canary; before this, its tests inherited every live
# switch agent.env sets unless the export block pinned it off (10-01, 10-02, 10-05: a live flag judged the deploy's tests
# differently from the PR gate's). Now each test runs under `env -i` with only:
#   the base   what a process needs (PATH, HOME, user, locale, TZ, a temp dir)
#   the block  every name the deploy's export block sets (its throwaway stores, dead-port runtimes, pinned switches),
#              with the values the block gave them
# deploy_titan_node.sh and run_deploy_gate.sh both source this, so a PR gate and the deploy gate run the same
# environment by construction. A new live switch never reaches a test unless the export block names it.
#   . scripts/gate_env.sh; gate_env_build; gate_python scripts/test_x.py

GATE_BASE_VARS="PATH HOME USER LOGNAME LANG LC_ALL LC_CTYPE TZ TMPDIR TEST_STATE"

gate_env_names() {   # the allowlist, one name a line: the base, plus every NAME= in the deploy's export block
  local f="${1:-scripts/deploy_titan_node.sh}"
  {
    tr ' ' '\n' <<<"$GATE_BASE_VARS"
    sed -n '/^export DAILY_LOG_DIR=/,/[^\\]$/p' "$f" | grep -oE '(^|[[:space:]])[A-Z][A-Z0-9_]*=' | tr -d ' \t='
  } | grep -v '^$' | sort -u
}

gate_env_build() {   # GATE_ENV=(NAME=value …): the allowlisted names this shell has set (an empty value stays: a pin)
  GATE_ENV=()
  local n
  for n in $(gate_env_names "$@"); do
    if [ -n "${!n+x}" ]; then GATE_ENV+=("$n=${!n}"); fi
  done
}

gate_python() {      # python3 with only the allowlisted environment
  env -i "${GATE_ENV[@]}" python3 "$@"
}
