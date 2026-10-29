#!/usr/bin/env bash
# Reusable deployer for a Rust runtime from the canonical (group-nested) tree.
# Captures the pattern proven by fetch-runtime + record-runtime so each further
# Rust runtime is one command. Idempotent-ish: re-runs rebuild + restart.
#
# Usage:
#   bash scripts/deploy_rust_runtime.sh <name> <local-source-dir> <port> [--db] [--force-during-eval]
#   Refuses while a nightly eval runs on titan (eval_guard.sh); --force-during-eval is for emergencies.
#
#   <name>          runtime + systemd unit name (e.g. event-runtime); binary is
#                   assumed to be target/release/<name>
#   <local-source>  canonical source dir on this Mac (e.g.
#                   ~/Projects/Titan/Systems/Runtimes/state/event-runtime)
#   <port>          bind port (set via PORT, which every state runtime honors)
#   --db            provision a dedicated Postgres role+db (<name with - as _>)
#                   and inject DATABASE_URL (TCP/scram)
#
# Prereqs on host: Rust toolchain (cargo on PATH), Postgres (if --db), the shared
# COORDINATOR_JWT_SECRET (read from an existing runtime's service.env).
# NOTE: stop any process already on <port> first (e.g. a legacy Python runtime:
#   bash scripts/titan.sh runtime stop <name>).
set -euo pipefail

NAME="${1:?runtime name}"; SRC="${2:?local source dir}"; PORT="${3:?port}"; DBFLAG=""; FORCE_EVAL=""
for a in "${@:4}"; do case "$a" in --db) DBFLAG="--db" ;; --force-during-eval) FORCE_EVAL=1 ;;
  *) echo "unknown option: $a" >&2; exit 2 ;; esac; done
SSH_ALIAS="${TITAN_SSH_ALIAS:-titan}"
TARGET="/ai/projects/runtimes/${NAME}"
DBNAME="${NAME//-/_}"
[ -d "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }

# shellcheck source=eval_guard.sh
. "$(dirname "$0")/eval_guard.sh"
eval_guard "$SSH_ALIAS" "$FORCE_EVAL" || exit $?

echo "==> [1/6] rsync source -> ${SSH_ALIAS}:${TARGET}"
# .claude/ (a Mac session's worktrees) and virtualenvs aren't source: #1022 found both rsynced into deploy dirs.
rsync -az --exclude 'target/' --exclude '.git/' --exclude '.wt/' --exclude '.claude/' --exclude '.titan-venv/' \
  --exclude '.venv/' "${SRC%/}/" "${SSH_ALIAS}:${TARGET}/"

echo "==> [2/6] provision service.env (+ Postgres db if --db)"
ssh "$SSH_ALIAS" NAME="$NAME" PORT="$PORT" TARGET="$TARGET" DBNAME="$DBNAME" DBFLAG="$DBFLAG" bash -s <<'REMOTE'
set -euo pipefail
# Tolerate a reference runtime that has already migrated off the shared HS256
# secret — an EdDSA-only deployment has no COORDINATOR_JWT_SECRET to copy.
SEC=""   # HS256 retired 2026-09-29: never carried into service.env (still MANAGED, so a redeploy strips it)
PUBKEY=/ai/config/coordinator_ed25519_public.pem
DBLINE=""
if [ "$DBFLAG" = "--db" ]; then
  cd /tmp
  PW=$(openssl rand -hex 16)
  if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DBNAME'" | grep -q 1; then
    sudo -u postgres psql -q -c "ALTER ROLE \"$DBNAME\" LOGIN PASSWORD '$PW';"
  else
    sudo -u postgres psql -q -c "CREATE ROLE \"$DBNAME\" LOGIN PASSWORD '$PW';"
  fi
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" | grep -q 1 \
    || sudo -u postgres createdb -O "$DBNAME" "$DBNAME"
  DBLINE="DATABASE_URL=postgres://$DBNAME:$PW@127.0.0.1:5432/$DBNAME"
fi
{
  echo "# $NAME (Rust) — deployed by deploy_rust_runtime.sh"
  echo "PORT=$PORT"
  # `[ -n "$X" ] && echo` is an && list: when the test fails it returns 1, and
  # under `set -e` that aborted this whole block before service.env was ever
  # written — silently breaking every deploy that did not pass --db.
  if [ -n "$DBLINE" ]; then echo "$DBLINE"; fi
  # Runtimes verify EdDSA coordinator tokens against this key. Pass the path,
  # not the PEM: systemd EnvironmentFile cannot carry a multi-line value.
  if [ -f "$PUBKEY" ]; then echo "COORDINATOR_PUBLIC_KEY_FILE=$PUBKEY"; fi
} > "$TARGET/service.env.new"

# Merge, do not clobber. This used to be `> service.env`, which truncated the
# file on every deploy and dropped every runtime-specific setting with it —
# engine wiring, model paths, tuning knobs, anything not in the managed set
# above. The operator then had to remember to re-add them or the runtime came
# back up misconfigured. Managed keys are rewritten; everything else is kept.
if [ -f "$TARGET/service.env" ]; then
  # DATABASE_URL is managed only when this run provisions it (--db). A redeploy without --db
  # used to filter the existing DATABASE_URL out and never write it back, leaving the runtime
  # with no database.
  MANAGED='^(PORT|COORDINATOR_JWT_SECRET|COORDINATOR_PUBLIC_KEY_FILE)='
  if [ -n "$DBLINE" ]; then MANAGED='^(PORT|DATABASE_URL|COORDINATOR_JWT_SECRET|COORDINATOR_PUBLIC_KEY_FILE)='; fi
  kept=$(grep -vE "$MANAGED" "$TARGET/service.env" | grep -vE '^#|^$' || true)
  if [ -n "$kept" ]; then
    printf '%s\n' "$kept" >> "$TARGET/service.env.new"
    echo "    preserved $(printf '%s\n' "$kept" | wc -l | tr -d ' ') existing setting(s)"
  fi
fi
mv "$TARGET/service.env.new" "$TARGET/service.env"
if [ ! -f "$PUBKEY" ]; then
  echo "    FATAL: $PUBKEY not available — runtime cannot authenticate" >&2
  exit 1
fi
echo "    service.env written (db=${DBFLAG:-none}, hs256=$([ -n "$SEC" ] && echo yes || echo no), eddsa=$([ -f "$PUBKEY" ] && echo yes || echo no))"
REMOTE

echo "==> [3/6] cargo build --release (this can take a few minutes)"
ssh "$SSH_ALIAS" "cd '$TARGET' && cargo build --release 2>&1 | tail -2"
ssh "$SSH_ALIAS" "test -x '$TARGET/target/release/${NAME}'" \
  || { echo "binary $TARGET/target/release/${NAME} not found (binary name != runtime name?)" >&2; exit 1; }

# The unit runs the installed copy, never the build tree (#1023): /ai/runtimes/bin/<name>/current, with the one it
# replaces kept as previous. An identical binary installs nothing and skips the restart.
RTBIN="python3 /ai/projects/titan-node/scripts/runtime_bin.py"
ssh "$SSH_ALIAS" "sudo install -d -o zach -g titanops -m 0755 /ai/runtimes/bin"
INSTALLED=$(ssh "$SSH_ALIAS" "$RTBIN install '${NAME}' '$TARGET/target/release/${NAME}' --source 'deploy_rust_runtime.sh from ${SRC##*/}'") \
  || { echo "install into /ai/runtimes/bin failed" >&2; exit 1; }
echo "$INSTALLED" | grep -q '"action": "unchanged"' && UNCHANGED=1 || UNCHANGED=0

echo "==> [4/6] install + enable systemd unit"
# PORT was missing here. The orphan check below dereferences it under `set -u`,
# so this block aborted before `systemctl enable --now` ever ran — the new
# binary was never started. Step 5 then health-checked the port, got an answer
# from the *old* process still holding it, and printed "healthy" and "done".
# Every green deploy this script reported was a false one.
ssh "$SSH_ALIAS" NAME="$NAME" TARGET="$TARGET" PORT="$PORT" UNCHANGED="$UNCHANGED" bash -s <<'REMOTE'
set -euo pipefail
sudo tee /etc/systemd/system/${NAME}.service >/dev/null <<UNIT
[Unit]
Description=Titan ${NAME} (Rust)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=zach
Group=titanops
WorkingDirectory=${TARGET}
EnvironmentFile=${TARGET}/service.env
ExecStart=/ai/runtimes/bin/${NAME}/current
Restart=on-failure
RestartSec=5
StandardOutput=append:/ai/logs/${NAME}.service.log
StandardError=append:/ai/logs/${NAME}.service.log

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable ${NAME}.service
# The running binary is another user's process: its exe link needs root to read (#1031).
mainexe=$(sudo -n readlink -f "/proc/$(systemctl show -p MainPID --value ${NAME}.service)/exe" 2>/dev/null || true)
if [ "$UNCHANGED" = "1" ] && systemctl is-active --quiet ${NAME}.service \
   && [ "$mainexe" = "$(readlink -f /ai/runtimes/bin/${NAME}/current)" ]; then
  echo "  binary unchanged and already running: no restart"
else
  # Stop the unit through systemd first (#1034): its own process is never an "orphan" to hard-kill. Whatever
  # still holds the port after that isn't this unit (a legacy uvicorn, say), and is stopped by PID: TERM, then KILL.
  # `enable --now` would leave a running unit alone, so the old binary stayed resident: stop, then start.
  sudo systemctl stop ${NAME}.service
  python3 /ai/projects/titan-node/scripts/runtime_bin.py free-port "${NAME}" "${PORT}"
  sudo systemctl start ${NAME}.service
fi
REMOTE

echo "==> [5/6] health check"
# The registry's health URL (several runtimes answer /v1/health); /health on the port when it isn't listed.
HEALTH=$(ssh "$SSH_ALIAS" "python3 -c 'import json,sys; r=json.load(open(\"/ai/projects/titan-node/scripts/runtime-registry.json\")); print(next((x[\"health_url\"] for x in r[\"runtimes\"]+r.get(\"brokers\",[]) if x[\"name\"]==sys.argv[1]), \"\"))' '${NAME}'" 2>/dev/null || true)
HEALTH="${HEALTH:-http://127.0.0.1:${PORT}/health}"
OK=0
for i in $(seq 1 15); do
  if ssh "$SSH_ALIAS" "curl -fsS --max-time 3 '${HEALTH}'" 2>/dev/null; then
    echo " <- ${NAME} healthy"; OK=1; break
  fi
  sleep 2
done
if [ "$OK" != "1" ] && [ "$UNCHANGED" != "1" ]; then
  # A new binary that doesn't come up: put the previous one back and stop (#1023).
  echo "==> ${NAME} unhealthy: rolling back to the previous release" >&2
  ssh "$SSH_ALIAS" "$RTBIN rollback '${NAME}' && sudo systemctl restart ${NAME}.service"
  exit 1
fi

echo "==> [6/6] done: ${NAME} on :${PORT} (systemd ${NAME}.service)"
