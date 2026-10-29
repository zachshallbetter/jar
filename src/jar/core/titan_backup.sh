#!/usr/bin/env bash
# Nightly backup of titan's state to the PNY SSD (/ai/tmp), a different physical disk from the
# NVMe that holds the live data. Run as root by titan-backup.service (titan-backup.timer, 02:15).
#
#   <dest>/pg/<db>.dump      every non-template Postgres database (pg_dump -Fc), each verified
#                            with pg_restore --list
#   <dest>/pg/globals.sql    roles and grants (pg_dumpall --globals-only; mode 600)
#   <dest>/kb-snapshot.json  KB notes + record-runtime nodes (kb_hygiene snapshot; restorable
#                            with kb_hygiene rollback)
#   <dest>/titan_memory.db   the agent's conversation memory (SQLite online backup)
#   <dest>/sqlite/*.db       other SQLite stores (online backup): everything in /ai/datasets/connectors
#                            (connectors ledger, proactive store, reflection letters/dreams), the world
#                            model's bubbles in connectors/world/ (world-<bubble>.db), and each
#                            runtime's state/*.db
#   <dest>/ledgers/*.jsonl   the decision broker's shadow + usage ledgers (the Decider readout data)
#   <dest>/secrets.tar.gz    agent.env, /ai/secrets, /ai/config, runtime service.env (mode 600)
#   <dest>/MANIFEST          sizes and sha256 of everything above
#
# Keeps 14 days. Writes /ai/logs/backup.json and alerts (titan_alert) on failure and recovery.
# Restore a database (the folder is root-only, so pipe it in):
#   sudo cat <dest>/pg/<db>.dump | sudo -u postgres pg_restore --clean --if-exists -d <db>
set -uo pipefail

ROOT=/ai/tmp/backups/nightly
KEEP_DAYS="${TITAN_BACKUP_KEEP_DAYS:-14}"
NODE=/ai/projects/titan-node
AGENT_ENV=/ai/projects/titan-local-agent/agent.env
STAMP="$(date -u +%Y%m%d-%H%M%SZ)"
DEST="$ROOT/$STAMP"
STATUS=/ai/logs/backup.json
problems=()

fail() { problems+=("$1"); echo "FAIL: $1" >&2; }

mountpoint -q /ai/tmp || { fail "/ai/tmp is not mounted (backups would land on the root disk)"; }
if [ ${#problems[@]} -eq 0 ]; then
  install -d -m 2770 -o root -g titanops "$ROOT" "$DEST" "$DEST/pg"

  # Postgres: every database, custom format, verified.
  dbs=$(runuser -u postgres -- psql -XAtc "select datname from pg_database where not datistemplate and datname <> 'postgres' order by 1" 2>/dev/null) \
    || fail "could not list Postgres databases"
  for db in $dbs; do
    if runuser -u postgres -- pg_dump -Fc -d "$db" -f "/tmp/titan-backup-$db.dump" 2>/tmp/titan-backup-err \
       && runuser -u postgres -- pg_restore --list "/tmp/titan-backup-$db.dump" >/dev/null 2>&1; then
      mv "/tmp/titan-backup-$db.dump" "$DEST/pg/$db.dump"
    else
      fail "pg_dump $db: $(head -c 200 /tmp/titan-backup-err)"; rm -f "/tmp/titan-backup-$db.dump"
    fi
  done
  runuser -u postgres -- pg_dumpall --globals-only > "$DEST/pg/globals.sql" 2>/dev/null || fail "pg_dumpall --globals-only"
  chmod 600 "$DEST/pg/globals.sql"

  # KB notes + records, through the runtimes' own APIs (as zach, with the agent's credential).
  runuser -u zach -- bash -c "cd $NODE && set -a && . $AGENT_ENV && set +a && python3 scripts/kb_hygiene.py snapshot --out $DEST/kb-snapshot.json" >/dev/null 2>&1 \
    || fail "KB snapshot"

  # Agent memory (SQLite online backup: safe while the agent API has it open).
  runuser -u zach -- python3 -c "
import sqlite3, sys
src = sqlite3.connect('/home/zach/.config/jellybean/memory/titan_memory.db', timeout=30)
dst = sqlite3.connect(sys.argv[1]); src.backup(dst); dst.close(); src.close()" "$DEST/titan_memory.db" 2>/dev/null \
    || fail "agent memory backup"

  # Other SQLite stores. Online backup API, so it's safe while the API/runtimes have them open.
  install -d -m 2770 -o root -g titanops "$DEST/sqlite" "$DEST/ledgers"
  runuser -u zach -- python3 - "$DEST/sqlite" <<'PYSQL' || fail "SQLite store backups"
import glob, os, sqlite3, sys
dest, bad = sys.argv[1], []
# world/ holds the world model's bubbles (one file per person, household, system): world-<bubble>.db
sources = (glob.glob("/ai/datasets/connectors/*.db") + glob.glob("/ai/datasets/connectors/world/*.db")
           + glob.glob("/ai/projects/runtimes/*/state/*.db"))
for src in sorted(sources):
    parent = os.path.basename(os.path.dirname(src))
    if parent == "state":   # /ai/projects/runtimes/<runtime>/state/x.db -> <runtime>-x.db
        parent = os.path.basename(os.path.dirname(os.path.dirname(src)))
    out = os.path.join(dest, f"{parent}-{os.path.basename(src)}")
    try:
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        d = sqlite3.connect(out); s.backup(d); d.close(); s.close()
    except Exception as exc:
        bad.append(f"{src}: {exc}")
if bad:
    print("; ".join(bad), file=sys.stderr); sys.exit(1)
PYSQL
  for f in /ai/projects/runtimes/decision-broker/shadow.jsonl /ai/projects/runtimes/decision-broker/usage.jsonl; do
    [ -f "$f" ] && { cp -p "$f" "$DEST/ledgers/decision-broker-$(basename "$f")" || fail "copy $f"; }
  done

  # Secrets and config needed to rebuild the fleet.
  tar -czf "$DEST/secrets.tar.gz" --ignore-failed-read "$AGENT_ENV" /ai/secrets /ai/config /etc/fstab \
      /ai/projects/runtimes/*/service.env 2>/dev/null || fail "secrets archive"
  chmod 600 "$DEST/secrets.tar.gz"

  (cd "$DEST" && find . -type f ! -name MANIFEST -printf '%s\t%p\n' | sort -k2 > MANIFEST \
     && find . -type f ! -name MANIFEST -exec sha256sum {} + | sort -k2 >> MANIFEST)
  chmod -R g+r,o-rwx "$DEST"; chmod 600 "$DEST/pg/globals.sql" "$DEST/secrets.tar.gz"

  # Retention: drop nightly folders older than KEEP_DAYS (only ones this script created).
  find "$ROOT" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9][01][0-9][0-3][0-9]-*' -mtime +"$KEEP_DAYS" -exec rm -rf {} +
fi

size=$(du -sh "$DEST" 2>/dev/null | cut -f1)
python3 - "$STATUS" "$STAMP" "$DEST" "${size:-0}" "${problems[@]}" <<'PY'
import json, sys, time
path, stamp, dest, size, *problems = sys.argv[1:]
json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stamp": stamp, "dest": dest, "size": size,
           "ok": not problems, "problems": problems}, open(path, "w"), indent=1)
sys.path.insert(0, "/ai/projects/titan-node/scripts")
from titan_alert import EdgeAlert
EdgeAlert("backup", "Titan nightly backup").update({f"backup{i}": p for i, p in enumerate(problems)})
PY
[ ${#problems[@]} -eq 0 ] && echo "backup ok: $DEST ($size)" || { echo "backup FAILED: ${problems[*]}"; exit 1; }
