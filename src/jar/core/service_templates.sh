#!/usr/bin/env bash
set -euo pipefail

AI_ROOT="${AI_ROOT:-/ai}"

usage() {
  cat <<'EOF'
Usage:
  titan service init <sqlite|postgres|filestore> <service_name> [--root PATH]
  titan service status <service_name> [--root PATH]
  titan service help

Examples:
  titan service init sqlite notes
  titan service init postgres app-db
  titan service init filestore exports
  titan service status notes
EOF
}

service_root_for() {
  local root="$1"
  local name="$2"
  printf '%s/projects/%s' "$root" "$name"
}

ensure_service_group() {
  if getent group titanops >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

apply_group_access() {
  local path="$1"
  if ensure_service_group; then
    chgrp -R titanops "$path"
    chmod -R g+rwX "$path"
    find "$path" -type d -exec chmod g+s {} +
  fi
}

write_common_files() {
  local service_root="$1"
  local name="$2"
  local kind="$3"

  cat > "${service_root}/README.md" <<EOF
# ${name}

This directory was scaffolded by Titan as a ${kind} service template.

## Layout

- config: service configuration
- state: service state or local database files
- logs: service-local logs if you keep them here

## Notes

- keep code under /ai/projects
- keep durable bulk data under /ai/datasets
- keep temporary work under /ai/tmp
EOF

  cat > "${service_root}/service.env" <<EOF
SERVICE_NAME=${name}
SERVICE_KIND=${kind}
SERVICE_ROOT=${service_root}
AI_ROOT=${AI_ROOT}
EOF
}

init_sqlite() {
  local root="${AI_ROOT}"
  local name="$1"
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --root)
        root="$2"
        shift 2
        ;;
      *)
        echo "Usage: titan service init sqlite <service_name> [--root PATH]" >&2
        exit 1
        ;;
    esac
  done

  local service_root
  service_root="$(service_root_for "$root" "$name")"

  mkdir -p \
    "${service_root}/config" \
    "${service_root}/state" \
    "${service_root}/logs" \
    "${root}/tmp/${name}" \
    "${root}/datasets/databases/${name}"

  write_common_files "$service_root" "$name" "sqlite"
  cat > "${service_root}/config/sqlite.env" <<EOF
SQLITE_DB_PATH=${service_root}/state/${name}.sqlite3
SQLITE_TMP_DIR=${root}/tmp/${name}
EOF
  touch "${service_root}/state/${name}.sqlite3"
  apply_group_access "$service_root"
  apply_group_access "${root}/tmp/${name}"
  apply_group_access "${root}/datasets/databases/${name}"

  echo "service_kind=sqlite"
  echo "service_name=${name}"
  echo "service_root=${service_root}"
  echo "sqlite_db=${service_root}/state/${name}.sqlite3"
  echo "sqlite_tmp=${root}/tmp/${name}"
}

init_postgres() {
  local root="${AI_ROOT}"
  local name="$1"
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --root)
        root="$2"
        shift 2
        ;;
      *)
        echo "Usage: titan service init postgres <service_name> [--root PATH]" >&2
        exit 1
        ;;
    esac
  done

  local service_root
  service_root="$(service_root_for "$root" "$name")"

  mkdir -p \
    "${service_root}/config" \
    "${service_root}/postgres" \
    "${root}/datasets/backups/${name}" \
    "${root}/tmp/${name}"

  write_common_files "$service_root" "$name" "postgres"
  cat > "${service_root}/config/postgres.env" <<EOF
POSTGRES_DATA_DIR=${service_root}/postgres
POSTGRES_BACKUP_DIR=${root}/datasets/backups/${name}
POSTGRES_TMP_DIR=${root}/tmp/${name}
EOF

  apply_group_access "$service_root"
  apply_group_access "${root}/datasets/backups/${name}"
  apply_group_access "${root}/tmp/${name}"

  echo "service_kind=postgres"
  echo "service_name=${name}"
  echo "service_root=${service_root}"
  echo "postgres_data=${service_root}/postgres"
  echo "postgres_backup=${root}/datasets/backups/${name}"
  echo "postgres_tmp=${root}/tmp/${name}"
}

init_filestore() {
  local root="${AI_ROOT}"
  local name="$1"
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --root)
        root="$2"
        shift 2
        ;;
      *)
        echo "Usage: titan service init filestore <service_name> [--root PATH]" >&2
        exit 1
        ;;
    esac
  done

  local service_root
  service_root="$(service_root_for "$root" "$name")"

  mkdir -p \
    "${service_root}/config" \
    "${root}/datasets/files/${name}" \
    "${root}/datasets/files/${name}/processed" \
    "${root}/tmp/${name}/incoming"

  write_common_files "$service_root" "$name" "filestore"
  cat > "${service_root}/config/filestore.env" <<EOF
FILESTORE_ROOT=${root}/datasets/files/${name}
FILESTORE_INCOMING=${root}/tmp/${name}/incoming
FILESTORE_PROCESSED=${root}/datasets/files/${name}/processed
EOF

  apply_group_access "$service_root"
  apply_group_access "${root}/datasets/files/${name}"
  apply_group_access "${root}/tmp/${name}"

  echo "service_kind=filestore"
  echo "service_name=${name}"
  echo "service_root=${service_root}"
  echo "filestore_root=${root}/datasets/files/${name}"
  echo "filestore_incoming=${root}/tmp/${name}/incoming"
}

status_service() {
  local root="${AI_ROOT}"
  local name="$1"
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --root)
        root="$2"
        shift 2
        ;;
      *)
        echo "Usage: titan service status <service_name> [--root PATH]" >&2
        exit 1
        ;;
    esac
  done

  local service_root
  service_root="$(service_root_for "$root" "$name")"
  local kind="unknown"
  if [[ -f "${service_root}/service.env" ]]; then
    kind="$(awk -F= '/^SERVICE_KIND=/{print $2}' "${service_root}/service.env" | tail -n 1)"
  fi

  echo "service_name=${name}"
  echo "service_kind=${kind}"
  echo "service_root=${service_root}"
  for path in \
    "${service_root}" \
    "${service_root}/config" \
    "${service_root}/state" \
    "${service_root}/logs" \
    "${service_root}/postgres" \
    "${root}/datasets/backups/${name}" \
    "${root}/datasets/files/${name}" \
    "${root}/datasets/files/${name}/processed" \
    "${root}/tmp/${name}" \
    "${root}/tmp/${name}/incoming"; do
    if [[ -e "$path" ]]; then
      echo "path[${path}]=present"
    fi
  done
}

main() {
  local subcommand="${1:-help}"
  shift || true

  case "$subcommand" in
    help|-h|--help)
      usage
      ;;
    init)
      local kind="${1:-}"
      local name="${2:-}"
      if [[ -z "${kind}" || -z "${name}" ]]; then
        usage >&2
        exit 1
      fi
      shift 2
      case "$kind" in
        sqlite)
          init_sqlite "$name" "$@"
          ;;
        postgres)
          init_postgres "$name" "$@"
          ;;
        filestore)
          init_filestore "$name" "$@"
          ;;
        *)
          echo "Unknown service kind: ${kind}" >&2
          usage >&2
          exit 1
          ;;
      esac
      ;;
    status)
      local name="${1:-}"
      if [[ -z "${name}" ]]; then
        usage >&2
        exit 1
      fi
      shift
      status_service "$name" "$@"
      ;;
    *)
      echo "Unknown subcommand: ${subcommand}" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
