#!/usr/bin/env bash
# Deploy titan-node to the Titan host by git pull — no more copying files over SSH.
#
# The host checkout (/ai/projects/titan-node) fetches with a read-only deploy key
# (~/.ssh/titan_node_deploy on the host, remote git@github-titan-node:…). This script:
#   1. refuses if your local main has commits that aren't on origin (push first)
#   2. runs fleet preflight
#   3. on the host: refuses a dirty checkout, fast-forwards to origin/main
#   4. runs the agent test suites; restarts titan-agent-api; runs the agent canary
#   5. if tests or the canary fail, resets to the previous commit and restarts again
#
# Usage: bash scripts/deploy_titan_node.sh [--dry-run] [--now] [--force-during-eval]
#   It refuses while a nightly eval runs on titan (eval_guard.sh: titan-household-eval / titan-model-eval active),
#   because a restart spoils the eval; --force-during-eval is for emergencies.
#   It waits (up to DEPLOY_QUIET_MAX_S, default 900) until no one has sent titan a message for 3 minutes,
#   because a restart cuts off a reply mid-stream (#618: 10 restarts while Zach was on the road). --now skips
#   that wait. A hold file (/ai/tmp/deploy.hold: "<until epoch> <who: why>", e.g. a run that a restart would
#   spoil) refuses the deploy until it expires or is removed; --now does not override it.
# systemd units under server-setup/ are NOT installed by this — copy those deliberately. The one exception:
# titan-graph-sync (#1447), installed after the canary by scripts/graph_sync_unit.sh install.
set -euo pipefail
. "$(dirname "$0")/ops_defaults.sh"   # OPS_GATE_JOBS (#1518)
SSH_ALIAS="${TITAN_SSH_ALIAS:-titan}"
DRY=""; NOW=""; FORCE_EVAL=""
for a in "$@"; do case "$a" in --dry-run) DRY="--dry-run" ;; --now) NOW=1 ;; --force-during-eval) FORCE_EVAL=1 ;;
  *) echo "unknown option: $a" >&2; exit 2 ;; esac; done
cd "$(dirname "$0")/.."
# shellcheck source=eval_guard.sh
. scripts/eval_guard.sh

git fetch -q origin
if [ -n "$(git rev-list origin/main..main 2>/dev/null)" ]; then
  echo "local main has unpushed commits — push (or merge the PR) first" >&2; exit 1
fi
echo "==> preflight"
bash scripts/titan_preflight.sh | tail -1
eval_guard "$SSH_ALIAS" "$FORCE_EVAL" || exit $?

ssh "$SSH_ALIAS" DRY="$DRY" NOW="$NOW" DEPLOY_QUIET_MAX_S="${DEPLOY_QUIET_MAX_S:-900}" \
  DEPLOY_GATE_JOBS="${DEPLOY_GATE_JOBS:-$OPS_GATE_JOBS}" bash -s <<'REMOTE'
set -euo pipefail
cd /ai/projects/titan-node
# A fixture box's switch (box_fixture.py, #1399 P4) never reaches the live agent: refuse before anything changes.
if grep -qE '^[[:space:]]*(export[[:space:]]+)?BOX_FIXTURE[[:space:]]*=' /ai/projects/titan-local-agent/agent.env 2>/dev/null; then
  echo "!!! agent.env sets BOX_FIXTURE (a sandbox box's switch): refusing to deploy the live agent with it" >&2; exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "host checkout is dirty — refusing to deploy:" >&2; git status --short >&2; exit 1
fi
git fetch -q origin
old=$(git rev-parse HEAD); new=$(git rev-parse origin/main)
if [ "$old" = "$new" ]; then echo "==> host already at $(git log --oneline -1)"; exit 0; fi
echo "==> incoming:"; git log --oneline "$old..$new" | sed 's/^/    /'
HOLD=/ai/tmp/deploy.hold
if [ -f "$HOLD" ]; then
  read -r until why < "$HOLD" || true
  if [ "${until:-0}" -gt "$(date +%s)" ] 2>/dev/null; then
    echo "!!! deploys are held until $(date -d "@$until" -u +%H:%M) UTC: ${why:-no reason given} ($HOLD)" >&2; exit 1
  fi
  rm -f "$HOLD"   # expired
fi
# Seconds since the last message a person sent (eval, canary and background turns don't count).
quiet() { python3 - <<'PY'
import json, time
last = 0.0
try:
    with open("/ai/logs/agent-turns.jsonl", "rb") as f:
        f.seek(0, 2); f.seek(max(0, f.tell() - 2_000_000))
        for line in f.read().decode("utf-8", "replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("source") == "user":
                last = max(last, float(r.get("ts") or 0))
except OSError:
    pass
print(int(time.time() - last))
PY
}
if [ "$DRY" = "--dry-run" ]; then echo "(dry run — nothing changed; last message $(quiet)s ago)"; exit 0; fi
if [ -z "$NOW" ]; then
  waited=0
  while [ "$(quiet)" -lt 180 ]; do
    if [ "$waited" -ge "$DEPLOY_QUIET_MAX_S" ]; then
      echo "!!! someone is still talking to titan after ${waited}s; not restarting mid-conversation (rerun later, or --now)" >&2; exit 1
    fi
    [ "$waited" -eq 0 ] && echo "==> someone sent titan a message in the last 3 min; waiting for a quiet window"
    sleep 30; waited=$((waited + 30))
  done
fi
# Copy every SQLite store first (#386): the incoming code may change a schema, and timers run it on the
# real stores as soon as the checkout moves. The incoming version of the helper, so the first deploy has it.
git show origin/main:scripts/backup_store.py | python3 - --keep 10 || { echo "!!! store backup failed: not deploying" >&2; exit 1; }
git merge --ff-only -q origin/main

rollback() {
  echo "!!! $1 — rolling back to ${old:0:7}" >&2
  git reset -q --hard "$old"
  sudo systemctl restart titan-agent-api
  exit 1
}
set -a; . /ai/projects/titan-local-agent/agent.env; set +a
# The tests run in a subshell: their throwaway stores and pinned-off switches never reach the restart, the
# canary or the seeding below, which see agent.env as live does (10-02 audit: the canary ran with the world
# model, fences and autonomy off). A rollback inside it exits the subshell; `|| exit 1` ends the deploy.
# #1473: the gate runs 4 tests at a time through run_deploy_gate.sh, which reads its plan and exports from the block
# below (3 deploys in shadow judged identically, 10-10: 426 = 426 tests, ~645 s → ~323 s). DEPLOY_GATE_JOBS=1 runs
# the block itself, one test at a time (the escape hatch). Either way any FAIL rolls back naming the failing test,
# and each failure keeps its last 60 lines, scrubbed, in a 0600 log under /ai/logs.
# From here to the canary, any command that stops the script unexpectedly rolls back (10-10 15:2xZ: a failed
# assignment under set -e ended the deploy after the checkout moved and before any rollback, leaving the new tree
# under the old process). Cleared after the canary: the best-effort steps below never roll back.
trap 'rollback "the deploy stopped unexpectedly at line $LINENO"' ERR
gate_t0=$SECONDS; gate_tests=null; gate_log="${DEPLOY_GATE_LOG:-/ai/logs/deploy-gate.jsonl}"
if [ "${DEPLOY_GATE_JOBS}" = 1 ]; then
(
# Tests must never write titan's real stores (the daily-log test once did): point every
# file-backed store at a throwaway directory for the test run.
TEST_STATE="$(mktemp -d)"
export DAILY_LOG_DIR="$TEST_STATE/daily" FEATURE_COUNTS="$TEST_STATE/feature-counts.jsonl" MAIL_IDLE_LOCK_DIR="$TEST_STATE/mail_idle" CONNECTORS_LEDGER="$TEST_STATE/ledger.db" PROACTIVE_DB="$TEST_STATE/proactive.db" \
       REFLECTION_DB="$TEST_STATE/reflection.db" ROUTINES_DB="$TEST_STATE/routines.db" \
       HOUSEHOLD_PREFS="$TEST_STATE/household.json" HOUSEHOLD_DB="$TEST_STATE/household.db" TODOS_DB="$TEST_STATE/todos.db" HA_TOKEN_FILE="$TEST_STATE/no-ha-token" AGENT_TURN_SOURCE=test \
       KB_ACL_DB="$TEST_STATE/kb_acl.db" PLACES_DB="$TEST_STATE/places.db" PUSH_DB="$TEST_STATE/push.db" \
       SCHEDULED_DB="$TEST_STATE/scheduled.db" WORLD_DB="$TEST_STATE/world.db" AGENT_WORLD_MODEL=0 LINK_TOPICS=0 \
       FENCES_DB="$TEST_STATE/fences.db" WORLD_DIR="$TEST_STATE/world" AGENT_FENCES=0 \
       TASKS_DB="$TEST_STATE/tasks.db" GRANTS_DB="$TEST_STATE/grants.db" GRANTS_AUDIT="$TEST_STATE/grants-audit.jsonl" \
       REVIEW_DB="$TEST_STATE/review.db" RETRIEVAL_TRACE_DB="$TEST_STATE/trace.db" AGENT_TASK_AUTONOMY=0 AGENT_TASK_AUTONOMY_OWNERS= \
       AGENT_WORLD_SITUATION=0 AGENT_GEMINI_SCHEDULE_PREFETCH=0 KB_RERANK=0 \
       AGENT_JEV_ROUTER=0 AGENT_JEV_TURN_GATES=0 CITE_INLINE=0 TASK_LOG_DB="$TEST_STATE/task_log.db" \
       TITAN_RECORD_URL=http://127.0.0.1:9/record TITAN_RETRIEVAL_URL=http://127.0.0.1:9/retrieval TITAN_EVENT_URL=http://127.0.0.1:9/event \
       TITAN_WORKFLOW_URL=http://127.0.0.1:9/workflow TITAN_INGESTION_URL=http://127.0.0.1:9/ingestion TITAN_DOCUMENT_URL=http://127.0.0.1:9/document \
       TITAN_FETCH_URL=http://127.0.0.1:9/fetch TITAN_EPISTEMIC_URL=http://127.0.0.1:9/epistemic \
       AGENT_MEMORY_DIR="$TEST_STATE/memory" AGENT_HISTORY_FILE="$TEST_STATE/history.jsonl" CONNECTORS_DIR="$TEST_STATE/connectors" \
       CUSTODY_DB="$TEST_STATE/custody.db" HELD_ACTIONS_DB="$TEST_STATE/held.db" KIDS_CHORES_DB="$TEST_STATE/kids_chores.db" \
       FAMILY_MEETING_DB="$TEST_STATE/family_meeting.db" SPACES_DB="$TEST_STATE/spaces.db" REFERENCES_DB="$TEST_STATE/references.db" ITEM_REFS_DB="$TEST_STATE/item_refs.db" MAIL_TRIAGE_DB="$TEST_STATE/mail_triage.db" MAIL_TRIAGE_CASES="$TEST_STATE/mail-triage-cases.jsonl" KB_BANK="$TEST_STATE/kb-bank.jsonl" CAPTURE_DB="$TEST_STATE/capture.db" KB_LIFECYCLE_LOG="$TEST_STATE/kb-lifecycle.jsonl" KB_LIFECYCLE_BACKUP_DIR="$TEST_STATE/kb-lifecycle-backups" OUR_YEAR_DB="$TEST_STATE/our_year.db" MAIL_SERVER_DB="$TEST_STATE/mail_server.db" MAIL_ACCOUNTS_DIR="$TEST_STATE/mail-accounts" MAIL_KEEP_FILE="$TEST_STATE/mail_keep.json" YOUTUBE_ACCOUNTS_DIR="$TEST_STATE/youtube-accounts" \
       MAIL_BILLS_DB="$TEST_STATE/mail_bills.db" MAIL_SUGGESTIONS_DB="$TEST_STATE/mail_suggestions.db" CONTINUITY_DB="$TEST_STATE/continuity.db" \
       CLAUDE_CHATS_DB="$TEST_STATE/claude_chats.db" EVAL_BANK_DB="$TEST_STATE/eval_bank.db" GENERATED_DIR="$TEST_STATE/generated" \
       GENERATED_IMAGES_DB="$TEST_STATE/generated_images.db" ENVELOPE_BACKUP_DIR="$TEST_STATE/env_backups" LINK_SALT_FILE="$TEST_STATE/link_salt" LINKS_DB="$TEST_STATE/links.db" LINKS_BACKUP_DIR="$TEST_STATE/links_backups" EXPORT_DIR="$TEST_STATE/exports" CHAT_LINKS_CYCLE_LOG="$TEST_STATE/chat-links-cycle.jsonl" KB_QUESTIONS_CACHE="$TEST_STATE/kb_questions.json" ME_DELETE_DIR="$TEST_STATE/me_delete" WORLD_BACKUP_DIR="$TEST_STATE/world_backups" CHAT_TITLES_DB="$TEST_STATE/chat_titles.db" NOW_FEED_CACHE="$TEST_STATE/now_feed_cache.json" ABOUT_DB="$TEST_STATE/about.db" AGENT_CANARY_LAST="$TEST_STATE/agent-canary.json" CHAT_LINKS_LOCAL_URL="http://127.0.0.1:9" RUNTIME_BIN_ROOT="$TEST_STATE/runtime_bin" SYSTEM_WORLD_LOG="$TEST_STATE/system-world.jsonl" \
       KB_HYGIENE_BACKUP_DIR="$TEST_STATE/kb_hygiene_backups" STORE_BACKUP_DIR="$TEST_STATE/store_backups" \
       UPLOADS_DIR="$TEST_STATE/uploads" UPLOADS_DB="$TEST_STATE/uploads.db" \
       GITHUB_STARS_DB="$TEST_STATE/github_stars.db" GITHUB_TOKEN_FILE="$TEST_STATE/no-github-token" \
       SITTER_DB="$TEST_STATE/sitter.db" HANDOFF_DB="$TEST_STATE/handoff.db" ROOM_HOLD= TV_UNLOCK_DB="$TEST_STATE/tv_unlock.db" \
       LOCK_UP_DB="$TEST_STATE/lock_up.db" HA_LOCK_UP= HA_CAMERA_SNAPSHOTS= HA_CAMERA_LIVE= \
       DEVICE_HEALTH_STATE="$TEST_STATE/device_health.json" CORRECTIONS_OUT="$TEST_STATE/corrections" \
       KB_BACKUP_DIR="$TEST_STATE/kb_backups" AGENT_TURN_LOG="$TEST_STATE/agent-turns.jsonl" ENGINEERING_AUDIT_LOG="$TEST_STATE/engineering-reads.jsonl" SURFACES_DB="$TEST_STATE/surfaces.db" ITEMS_LATENCY_LOG="$TEST_STATE/items-latency.jsonl" MEMORY_EXTRACT_LOG="$TEST_STATE/memory-extract.jsonl" \
       TURN_REVIEW_LOG="$TEST_STATE/real-turn-review.jsonl" RETRIEVAL_EVAL_LOG="$TEST_STATE/retrieval-eval.jsonl" CITE_PRECISION_LOG="$TEST_STATE/cite-precision.jsonl" \
       WORLD_EVAL_LOG="$TEST_STATE/world-eval.jsonl" SCORECARD_LOGS="$TEST_STATE/logs" \
       AUTONOMOUS_CODER_CALIBRATION_DB="$TEST_STATE/coder_calibration.db" AUTONOMOUS_CODER_ROOT="$TEST_STATE/coder" \
       LOCAL_MODEL_DIR="$TEST_STATE/local-model.d" LOCAL_MODEL_LEDGER="$TEST_STATE/models.jsonl" LOCAL_AGENT_ENV="$TEST_STATE/agent.env" \
       TITAN_DEPLOY_HOLD="$TEST_STATE/deploy.hold" MODEL_EVAL_DIR="$TEST_STATE/model-eval" \
       AGENT_SPEECH_DIR="$TEST_STATE/speech" AGENT_SPEAK_LOCAL=0 SERVER_IDENTITY_KEY="$TEST_STATE/server-identity.key" \
       BOX_SETUP=0 BOX_SETUP_DIR="$TEST_STATE/box-setup" \
       BOX_BACKUP_DIR="$TEST_STATE/box-backups" BOX_BACKUP_PUB="$TEST_STATE/box-backup.pub.json" BOX_BACKUP_STAGE="$TEST_STATE/box-backup-stage" BOX_BACKUP_SIGN_KEY="$TEST_STATE/box-backup.sign.key" BOX_BACKUP_VERIFIED="$TEST_STATE/box-backup.verified.json" \
       MAIL_TRIAGE=0 ITEM_ENVELOPES=0 ENVELOPE_SINCE= KB_ZONES= KB_LIFECYCLE= AGENT_EFFORT_AUTO= AGENT_NOTE_CLAIMS= EVAL_NAMES_FILE=/dev/null \
       NEXT_STOP_CARDS=0 BILL_DUE_CARDS=0 MAIL_SERVER= MAIL_SERVER_KB= MAIL_SERVER_PASSES= MAIL_SERVER_WRITES= \
       MAIL_SERVER_REPLACES_MAC= MAIL_SERVER_RETIRE= \
       RESET_STORES_DIR="$TEST_STATE/reset_stores" RESET_BACKUP_DIR="$TEST_STATE/reset_backups" GRAPH_DB="$TEST_STATE/graph.db" USAGE_CONFIG="$TEST_STATE/usage_config.json" ROOMS_DB="$TEST_STATE/rooms.db"
# (10-02 audit: every store a test could reach is redirected here; a new *_DB/_DIR env var belongs on this list.)
# (Runtimes on a dead port and the step log in the throwaway dir: 10-02 audit found 90 test "PGE" bill nodes in the live
# record runtime, a test bill note in the KB, and test task steps pushed to Zach as "Tasks today: 59 steps done".)
# (Jev off: the gate's turns had reached the real decision broker, 675 source=test decisions 09-30..10-02.)
# (Live switches are pinned off too: agent.env's own values, e.g. a trial's AGENT_TASK_AUTONOMY=1, are for the
# running agent, never for the tests; 10-01 a live trial flag failed test_scenarios and rolled a deploy back, and
# 10-02 AGENT_WORLD_SITUATION=1 failed test_world the same way. A new live switch gets pinned here when it's added.)
# A failing gate test leaves its last 60 lines, scrubbed, in a 0600 log under /ai/logs, and the path is printed
# (10-05: "!!! test_place_regions.py failed" said nothing about which check). Never the environment.
# Each test runs with an allowlisted environment only (gate_env.sh, #996): agent.env's live switches never reach it.
. scripts/gate_env.sh
gate_env_build
keep_fail() {
  local name="$1" log
  log="/ai/logs/deploy-fail-$(date -u +%Y%m%d-%H%M%S)-${name%.py}.log"
  printf '%s\n' "$2" | python3 scripts/deploy_fail_log.py --out "$log" && echo "!!! $name: its output is in $log" >&2
}
# run_test scripts/test_x.py ["pattern"]: passes on exit 0, and with a pattern also when the output has it.
run_test() {
  local f="$1" out rc
  out=$(gate_python "$f" 2>&1); rc=$?
  if [ -z "${2:-}" ] && [ $rc -eq 0 ]; then return 0; fi
  if [ -n "${2:-}" ] && grep -q -- "$2" <<<"$out"; then return 0; fi
  keep_fail "$(basename "$f")" "$out"
  return 1
}
run_test scripts/test_agent.py "0 failed" || rollback "agent self-test failed"
run_test scripts/test_brain_router.py || rollback "brain router tests failed"
run_test scripts/test_turn_gates.py || rollback "turn gate tests failed"
run_test scripts/test_skillopt_hooks.py || rollback "skillopt eval hook tests failed"
run_test scripts/test_skillopt_harness.py || rollback "skillopt harness tests failed"
run_test scripts/test_verify_deletions.py || rollback "deletion check tests failed"
run_test scripts/test_verify_calendar_writes.py || rollback "calendar write check tests failed"
run_test scripts/test_household_coordination.py || rollback "household coordination tests failed"
run_test scripts/test_correction_digest.py || rollback "correction digest tests failed"
run_test scripts/test_write_honesty.py || rollback "write honesty tests failed"
run_test scripts/test_capabilities.py || rollback "capabilities tests failed"
run_test scripts/test_location_contract.py || rollback "location contract tests failed"
run_test scripts/test_device_health.py || rollback "device health tests failed"
run_test scripts/test_privacy_eval.py || rollback "privacy eval tests failed"
run_test scripts/test_home_assistant.py || rollback "home assistant tests failed"
run_test scripts/test_credential_guard.py || rollback "credential guard tests failed"
run_test scripts/test_phone_turn_truth.py || rollback "phone-turn truth tests failed"
run_test scripts/test_calendar_owners.py || rollback "calendar owner tests failed"
run_test scripts/test_kb_import.py || rollback "kb import tests failed"
run_test scripts/test_memory_facts.py || rollback "memory fact tests failed"
run_test scripts/test_embed_trim.py " 0 failed" || rollback "embed trim tests failed"
run_test scripts/test_host_awareness.py || rollback "host awareness tests failed"
run_test scripts/test_name_images.py || rollback "name_images tests failed"
run_test scripts/test_claude_chats.py || rollback "Claude chats tests failed"
run_test scripts/test_plain_words.py || rollback "plain words tests failed"
run_test scripts/test_local_dates.py || rollback "local date tests failed"
run_test scripts/test_references.py || rollback "references tests failed"
run_test scripts/test_capability_summary.py || rollback "capability summary tests failed"
run_test scripts/test_public_facts.py || rollback "public facts tests failed"
run_test scripts/test_school_facet.py || rollback "school facet tests failed"
run_test scripts/test_kids_screen.py || rollback "kids screen tests failed"
run_test scripts/test_chores.py || rollback "chores tests failed"
run_test scripts/test_home_screen.py || rollback "home screen tests failed"
run_test scripts/test_world_seed.py || rollback "world seed tests failed"
run_test scripts/test_reply_action_blocks.py || rollback "reply action block tests failed"
run_test scripts/test_private_peek.py || rollback "private peek tests failed"
run_test scripts/test_family_meeting.py || rollback "family meeting tests failed"
run_test scripts/test_kb_sources.py || rollback "kb_sources paging tests failed"
run_test scripts/test_calendar_series.py || rollback "calendar series tests failed"
run_test scripts/test_bills_timeline_truth.py || rollback "bills and timeline truth tests failed"
run_test scripts/test_custody.py || rollback "custody tests failed"
run_test scripts/test_calendar_timeline.py || rollback "calendar timeline tests failed (#636)"
run_test scripts/test_eval_checks.py || rollback "eval check tests failed"
run_test scripts/test_local_overflow.py || rollback "local overflow tests failed"
run_test scripts/test_retrieval_eval.py || rollback "retrieval eval tests failed"
run_test scripts/test_scorecard.py || rollback "scorecard tests failed"
run_test scripts/test_no_guessed_time.py || rollback "guessed-time tests failed"
run_test scripts/test_conversations.py || rollback "conversations tests failed"
run_test scripts/test_search_quality.py || rollback "search quality tests failed"
run_test scripts/test_skills_consistency.py || rollback "skills consistency tests failed"
run_test scripts/test_verified_claims.py || rollback "claim verification tests failed"
run_test scripts/test_local_long_chat.py || rollback "local long-chat tests failed"
run_test scripts/test_claim_checks.py || rollback "reply check tests failed"
run_test scripts/test_provenance.py || rollback "provenance tests failed"
run_test scripts/test_changes.py || rollback "changes tests failed"
run_test scripts/test_grants.py || rollback "grant tests failed"
run_test scripts/test_scenarios.py || rollback "scenario tests failed"
run_test scripts/test_degenerate_reply.py || rollback "degenerate reply tests failed"
run_test scripts/test_schedule_digest.py || rollback "schedule digest tests failed"
run_test scripts/test_task_steps.py || rollback "task step tests failed"
run_test scripts/test_mail_bills.py || rollback "mail bill tests failed"
run_test scripts/test_mail_triage.py || rollback "mail triage tests failed"
run_test scripts/test_mail_invites.py || rollback "mail invite tests failed"
run_test scripts/test_event_signals.py || rollback "event signal tests failed"
run_test scripts/test_true_weekdays.py || rollback "weekday check tests failed"
run_test scripts/test_keep_history.py || rollback "keep-history tests failed"
run_test scripts/test_plain_failure.py || rollback "plain failure tests failed"
run_test scripts/test_eval_live_guard.py || rollback "household eval live-guard tests failed"
run_test scripts/test_rule_records.py || rollback "rule record tests failed"
run_test scripts/test_seed_decisions.py || rollback "decision seeding tests failed"
run_test scripts/test_attachment_carry.py || rollback "attachment carry tests failed"
run_test scripts/test_turn_review.py || rollback "turn review tests failed"
run_test scripts/test_review_loop.py || rollback "review loop tests failed"
run_test scripts/test_redact.py || rollback "redaction tests failed"
run_test scripts/test_titan_alert.py || rollback "alert tests failed"
run_test scripts/test_preflight_disks.py || rollback "preflight disk tests failed"
run_test scripts/test_reply_detail.py || rollback "reply detail tests failed"
run_test scripts/test_connectors.py || rollback "connectors tests failed"
run_test scripts/test_proactive.py || rollback "proactive loop tests failed"
run_test scripts/test_kb_seed_runbook.py || rollback "runbook KB tests failed"
run_test scripts/test_autonomous_coder.py " 0 failed" || rollback "autonomous coder tests failed"
run_test scripts/test_reflection.py || rollback "nightly reflection tests failed"
run_test scripts/test_routines.py || rollback "routines tests failed"
run_test scripts/test_schedule_lines.py || rollback "schedule lines tests failed"
run_test scripts/test_weather.py || rollback "weather tests failed"
run_test scripts/test_now_everywhere.py " 0 failed" || rollback "date and time tests failed"
run_test scripts/test_eval_pins.py " 0 failed" || rollback "eval pin tests failed"
run_test scripts/test_continuity_api.py " 0 failed" || rollback "continuity (sync, context) tests failed"
run_test scripts/test_household_identity.py " 0 failed" || rollback "household identity tests failed"
run_test scripts/test_cross_device.py " 0 failed" || rollback "cross-device tests failed"
run_test scripts/test_audience.py " 0 failed" || rollback "audience tests failed"
run_test scripts/test_kb_privacy.py " 0 failed" || rollback "KB privacy tests failed"
run_test scripts/test_cards.py " 0 failed" || rollback "card tests failed"
run_test scripts/test_todos.py " 0 failed" || rollback "todo tests failed"
run_test scripts/test_scheduled.py " 0 failed" || rollback "scheduled action tests failed"
run_test scripts/test_timeline_api.py " 0 failed" || rollback "timeline api tests failed"
run_test scripts/test_push.py " 0 failed" || rollback "push tests failed"
run_test scripts/test_live_activity.py " 0 failed" || rollback "Live Activity tests failed"
run_test scripts/test_remember_failed.py " 0 failed" || rollback "remember-failed tests failed"
run_test scripts/test_place_routines.py " 0 failed" || rollback "place routine tests failed"
run_test scripts/test_privacy_timeline.py " 0 failed" || rollback "privacy (timeline, records, routines) tests failed"
run_test scripts/test_todo_issues.py " 0 failed" || rollback "todo issue tests failed"
run_test scripts/test_honest_turns.py " 0 failed" || rollback "honest turn tests failed"
run_test scripts/test_prompt_prefix.py || rollback "prompt prefix tests failed"
run_test scripts/test_world.py " 0 failed" || rollback "world model tests failed"
run_test scripts/test_fences.py " 0 failed" || rollback "fences tests failed"
run_test scripts/test_backup_store.py " 0 failed" || rollback "store backup tests failed"
# Every other scripts/test_*.py marked "# gate: hermetic" runs too, so a new hermetic test needs no line here
# (#350). Opt-in, never a deny-list: an unmarked test (it may touch live titan) never runs in a deploy, and
# test_gate_discovery fails until every test is listed here, marked hermetic, or on scripts/gate_live_tests.txt.
# Judged like the lines above: exit code, and no "N failed" with N > 0.
for f in scripts/test_*.py; do
  b=$(basename "$f")
  grep -q "scripts/$b" scripts/deploy_titan_node.sh && continue
  grep -v '^#' scripts/gate_live_tests.txt | awk '{print $1}' | grep -qx "$b" && continue
  grep -q '^# gate: hermetic' "$f" || continue   # opt-in only: an unmarked test never runs in a deploy
  out=$(gate_python "$f" 2>&1) || { keep_fail "$b" "$out"; rollback "$b failed"; }
  if echo "$out" | grep -qE "(^|[^0-9])[1-9][0-9]* failed"; then keep_fail "$b" "$out"; rollback "$b failed"; fi
done
rm -rf "$TEST_STATE"
) || exit 1
else
  # Too few tests judged is a failure too (Coordination's review of #1486): run_deploy_gate exits 0 when no line says
  # FAIL, so an empty plan (a broken export or plan extraction, a wrong directory) or an early stop would pass a
  # deploy on 0 tests. The floor: 300, or 90% of the last deploy's count when the timing log has one. A reused pass
  # (below) must meet it too.
  gate_floor="${DEPLOY_GATE_MIN_TESTS:-300}"
  # (no log yet on the first parallel deploy: tail fails, and under pipefail that must not end the script; 10-10 it did)
  gate_last=$( { tail -n 1 "$gate_log" 2>/dev/null || true; } | sed -nE 's/.*"tests": ([0-9]+).*/\1/p')
  if [ -n "$gate_last" ] && [ $((gate_last * 9 / 10)) -gt "$gate_floor" ]; then gate_floor=$((gate_last * 9 / 10)); fi
  # #1514: gate once per tree. A clean checkout whose tree passed this same full gate on titan recently (a PR gate
  # through gate_run.sh, or main-health), under the same python, reuses that pass: same tests, same host, same tree.
  # DEPLOY_GATE_REUSE=0 forces a run; a dirty checkout, an unknown tree, a stale pass or too few tests runs it.
  gate_tree=$(git rev-parse 'HEAD^{tree}' 2>/dev/null || true); gate_reused=""
  if [ "${DEPLOY_GATE_REUSE:-1}" = 1 ] && [ -n "$gate_tree" ] && [ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    gate_reused=$(bash scripts/gate_passed.sh find "$gate_tree" 2>/dev/null || true)
    if [ -n "$gate_reused" ] && [ "$(cut -f1 <<<"$gate_reused")" -lt "$gate_floor" ]; then gate_reused=""; fi
  fi
  if [ -n "$gate_reused" ]; then
  gate_tests=$(cut -f1 <<<"$gate_reused")
  echo "==> gate: reused $(cut -f2 <<<"$gate_reused")'s pass of this tree (${gate_tree:0:12}, $gate_tests tests); DEPLOY_GATE_REUSE=0 re-runs it"
  else
  gate_out="$(mktemp)"; chmod 600 "$gate_out"
  gate_rc=0
  GATE_JOBS="${DEPLOY_GATE_JOBS}" GATE_KEEP_FAIL=1 timeout 1800 bash scripts/run_deploy_gate.sh "$PWD" \
    > "$gate_out" 2>&1 < /dev/null || gate_rc=$?
  grep -E '^(FAIL |!!! )' "$gate_out" >&2 || true          # the verdicts and log paths only, never a test's output
  gate_first=$(awk '/^FAIL /{print $2; exit}' "$gate_out")
  gate_tests=$(grep -cE '^(ok|FAIL) ' "$gate_out" || true)
  rm -f "$gate_out"
  if [ "$gate_rc" -eq 124 ]; then rollback "the gate timed out (30 min, ${DEPLOY_GATE_JOBS} tests at a time)"; fi
  if [ "$gate_rc" -ne 0 ]; then
    if [ -n "$gate_first" ]; then rollback "$(basename "$gate_first") failed"; fi
    rollback "the gate failed (exit $gate_rc) without naming a test"
  fi
  if [ "${gate_tests:-0}" -lt "$gate_floor" ]; then rollback "the gate judged only ${gate_tests:-0} tests (at least $gate_floor expected)"; fi
  echo "==> gate: $gate_tests tests passed, ${DEPLOY_GATE_JOBS} at a time, in $((SECONDS - gate_t0))s"
  fi
fi
gate_s=$((SECONDS - gate_t0))
. scripts/ops_ports.sh   # OPS_AGENT_API_PORT (#1518)
sudo systemctl restart titan-agent-api
for _ in $(seq 1 30); do curl -sf -m 3 "http://127.0.0.1:$OPS_AGENT_API_PORT/health" >/dev/null && break; sleep 1; done
curl -sf -m 3 "http://127.0.0.1:$OPS_AGENT_API_PORT/health" >/dev/null || rollback "agent API did not come back"
python3 scripts/agent_canary.py --no-alert >/tmp/deploy-canary.txt 2>&1 || rollback "canary failed: $(cat /tmp/deploy-canary.txt)"
trap - ERR   # deployed and canaried: nothing below rolls back
echo "==> deployed $(git log --oneline -1) · $(cat /tmp/deploy-canary.txt)"
# the gate queue on titan follows the repo (#945): replaced only when it differs, never a reason to roll back
bash scripts/gate_queue.sh install || echo "==> warning: the gate queue wasn't installed (deploy kept)"
# the graph's 10-minute sync (#1447): units copied only when they differ, the timer enabled; never a reason to roll back
bash scripts/graph_sync_unit.sh install || echo "==> warning: the graph sync timer wasn't installed (deploy kept)"
# #1461: an existing graph.db moves to node_sources in one transaction (idempotent); readers handle either shape
python3 scripts/graph_sync.py --migrate 2>&1 | tr -d '\n' || echo "==> warning: the graph.db migration didn't run (the next sync rebuilds it)"
echo
bash scripts/main_health.sh install || echo "==> warning: the main health timer wasn't installed (deploy kept)"   # #1516
bash scripts/clockshift/canary.sh install || echo "==> warning: the clock canary timer wasn't installed (deploy kept)"   # #1515
bash scripts/tmp_sweep.sh install || echo "==> warning: the tmp sweep timer wasn't installed (deploy kept)"   # 10-10 deep review
# Best effort: the agent answers questions about titan from the current runbook.
python3 scripts/kb_seed_runbook.py --quiet 2>&1 | tail -1 || echo "(runbook KB refresh failed)"
# Best effort: the first local-model turn after a prompt change would re-read ~8k tokens (11-45 s).
python3 scripts/warm_local.py 2>&1 | tail -1 || echo "(local cache warm-up failed)"
# Rules live in Decisions and the runbook only (09-30: a June "Who you are" record answered wrongly).
# The code is already live and tested; a stray record fails the run loudly so a person clears it.
# Decisions: written when changed, retired (backed up) when no longer listed (#351), before the stray check.
python3 scripts/kb_seed_decisions.py --quiet 2>&1 | tail -1 || echo "(decisions refresh failed)"
# #1473: one timing line per deploy (counts only), so a slower gate shows: when, which commit, how many at a time,
# its seconds and how many tests it judged. Never a reason to stop.
printf '{"ts": %s, "commit": "%s", "jobs": %s, "gate_s": %s, "tests": %s, "reused": %s}\n' "$(date +%s)" \
  "$(git rev-parse --short HEAD)" "${DEPLOY_GATE_JOBS}" "$gate_s" "${gate_tests:-null}" \
  "$([ -n "${gate_reused:-}" ] && echo true || echo false)" >> "$gate_log" 2>/dev/null || true
python3 scripts/rule_records.py || { echo "!!! stray rule records in the KB (listed above)" >&2; exit 1; }
REMOTE
