# Sourced by deploy_titan_node.sh and deploy_rust_runtime.sh: no deploy while a nightly eval runs on titan.
# 10-06: two restarts landed inside the 03:30 nightly (an agent deploy and a runtime restart), and a restart mid-eval
# spoils its numbers. `eval_guard <ssh alias> [force]` returns 0 when no eval unit is active, 3 when one is (with
# the reason on stderr), 1 when titan can't be asked; a non-empty force (--force-during-eval) lets it through with a
# warning, for an emergency.
EVAL_UNITS="titan-household-eval.service titan-model-eval.service"

eval_guard() {
  local alias="$1" force="${2:-}" states busy="" u s
  if ! states=$(ssh "$alias" "systemctl show -p ActiveState --value $EVAL_UNITS" 2>/dev/null); then
    if [ -n "$force" ]; then
      echo "!!! couldn't ask titan whether an eval is running; deploying anyway (--force-during-eval)" >&2
      return 0
    fi
    echo "!!! couldn't ask titan whether an eval is running; refusing (--force-during-eval to deploy anyway)" >&2
    return 1
  fi
  set -- $EVAL_UNITS
  for s in $states; do
    u="${1:-?}"
    [ $# -gt 0 ] && shift
    case "$s" in active|activating|reloading) busy="$busy ${u%.service}" ;; esac
  done
  if [ -n "$busy" ]; then
    if [ -n "$force" ]; then
      echo "!!! deploying while an eval runs on titan:$busy (--force-during-eval)" >&2
      return 0
    fi
    echo "!!! an eval is running on titan:$busy. A restart now would spoil it. Wait until it finishes," \
         "or pass --force-during-eval for an emergency." >&2
    return 3
  fi
  return 0
}
