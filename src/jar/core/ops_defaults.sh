# Operational defaults that more than one script reads (#1518). Source it: . "$(dirname "$0")/ops_defaults.sh"
# One home so the scripts can't drift apart (run_deploy_gate.sh defaulted to 1 while gate_run.sh and the deploy used 4).
# scripts/test_ops_constants.py fails when a default is written again elsewhere.

# Tests the deploy gate runs at a time (10-10: 507 s one at a time, 194 s at 4, same verdicts). Overridden by GATE_JOBS
# (run_deploy_gate.sh, gate_run.sh, main_health.sh) or DEPLOY_GATE_JOBS (deploy_titan_node.sh); GATE_JOBS=1 is the
# sequential run.
OPS_GATE_JOBS=4
