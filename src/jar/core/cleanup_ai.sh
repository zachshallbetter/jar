#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-report}"
if [[ $# -gt 0 ]]; then
  shift
fi

AI_ROOT="${AI_ROOT:-/ai}"
LOG_DAYS=14
TMP_DAYS=7
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --logs-days)
      LOG_DAYS="$2"
      shift 2
      ;;
    --tmp-days)
      TMP_DAYS="$2"
      shift 2
      ;;
    --apply)
      APPLY=1
      shift
      ;;
    *)
      echo "Usage: $0 [report|prune] [--logs-days N] [--tmp-days N] [--apply]" >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "${AI_ROOT}" ]]; then
  if [[ "${MODE}" = "report" ]]; then
    echo "logs_root=${AI_ROOT}/logs"
    echo "logs_status=missing"
    echo "tmp_root=${AI_ROOT}/tmp"
    echo "tmp_status=missing"
    exit 0
  fi
  echo "AI root missing: ${AI_ROOT}" >&2
  exit 1
fi

collect_paths() {
  local root="$1"
  local age_days="$2"
  if [[ ! -d "${root}" ]]; then
    return
  fi
  find "${root}" -mindepth 1 -mtime "+${age_days}" -print 2>/dev/null | sort
}

report_bucket() {
  local label="$1"
  local root="$2"
  local age_days="$3"
  local paths
  paths="$(collect_paths "${root}" "${age_days}")"
  local count=0
  local size_kb=0
  if [[ -n "${paths}" ]]; then
    count="$(printf '%s\n' "${paths}" | sed '/^$/d' | wc -l | tr -d ' ')"
    size_kb="$(printf '%s\n' "${paths}" | du -sk 2>/dev/null | awk '{sum += $1} END {print sum + 0}')"
  fi
  echo "${label}_root=${root}"
  echo "${label}_age_days=${age_days}"
  echo "${label}_candidate_count=${count}"
  echo "${label}_candidate_size_kb=${size_kb}"
  if [[ -n "${paths}" ]]; then
    echo "${label}_candidates:"
    printf '%s\n' "${paths}" | sed 's/^/  /'
  fi
}

case "${MODE}" in
  report)
    report_bucket logs "${AI_ROOT}/logs" "${LOG_DAYS}"
    report_bucket tmp "${AI_ROOT}/tmp" "${TMP_DAYS}"
    ;;
  prune)
    report_bucket logs "${AI_ROOT}/logs" "${LOG_DAYS}"
    report_bucket tmp "${AI_ROOT}/tmp" "${TMP_DAYS}"
    if [[ "${APPLY}" -ne 1 ]]; then
      echo "prune_applied=no"
      echo "Re-run with --apply to delete the listed candidates."
      exit 0
    fi
    while IFS= read -r path; do
      [[ -z "${path}" ]] && continue
      rm -rf "${path}"
    done < <(collect_paths "${AI_ROOT}/logs" "${LOG_DAYS}")
    while IFS= read -r path; do
      [[ -z "${path}" ]] && continue
      rm -rf "${path}"
    done < <(collect_paths "${AI_ROOT}/tmp" "${TMP_DAYS}")
    echo "prune_applied=yes"
    ;;
  *)
    echo "Unknown mode: ${MODE}" >&2
    echo "Usage: $0 [report|prune] [--logs-days N] [--tmp-days N] [--apply]" >&2
    exit 1
    ;;
esac
