#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# Common shell utilities for E2E tests.
# Sourced by other scripts — not executed directly.

set -euo pipefail

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_TS() { date '+%Y-%m-%d %H:%M:%S'; }

log_info()  { echo "[$(_TS)] [E2E][INFO]  $*"; }
log_error() { echo "[$(_TS)] [E2E][ERROR] $*" >&2; }
log_step()  { echo ""; echo "[$(_TS)] [E2E][STEP]  ──── $* ────"; }

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "${expected}" != "${actual}" ]]; then
    log_error "ASSERT FAILED (${label}): expected='${expected}' actual='${actual}'"
    return 1
  fi
}

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if [[ "${haystack}" != *"${needle}"* ]]; then
    log_error "ASSERT FAILED (${label}): '${haystack}' does not contain '${needle}'"
    return 1
  fi
}

assert_file_exists() {
  local path="$1"
  if [[ ! -e "${path}" ]]; then
    log_error "ASSERT FAILED: file does not exist: ${path}"
    return 1
  fi
}

assert_http_ok() {
  local label="$1" url="$2"
  local code
  code=$(curl -sf -o /dev/null -w '%{http_code}' "${url}" 2>/dev/null || echo "000")
  if [[ "${code}" != "200" && "${code}" != "201" ]]; then
    log_error "ASSERT FAILED (${label}): HTTP ${code} from ${url}"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Waiters
# ---------------------------------------------------------------------------

wait_for_http() {
  local url="$1" timeout_sec="${2:-120}" label="${3:-}"
  local start_ts now_ts
  start_ts="$(date +%s)"
  while true; do
    if curl -sf -o /dev/null "${url}" 2>/dev/null; then
      return 0
    fi
    now_ts="$(date +%s)"
    if (( now_ts - start_ts > timeout_sec )); then
      log_error "Timed out waiting for ${label:-${url}} (${timeout_sec}s)"
      return 1
    fi
    sleep 2
  done
}

wait_for_port() {
  local host="$1" port="$2" timeout_sec="${3:-60}" label="${4:-}"
  local start_ts now_ts
  start_ts="$(date +%s)"
  while true; do
    if bash -c "echo >/dev/tcp/${host}/${port}" 2>/dev/null; then
      return 0
    fi
    now_ts="$(date +%s)"
    if (( now_ts - start_ts > timeout_sec )); then
      log_error "Timed out waiting for ${label:-${host}:${port}} (${timeout_sec}s)"
      return 1
    fi
    sleep 1
  done
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi

  log_info "Installing uv"
  apt-get install -y -qq curl ca-certificates >/dev/null 2>&1
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
  export PATH="${HOME}/.local/bin:${PATH}"

  if ! command -v uv >/dev/null 2>&1; then
    log_error "uv installation failed"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Data validation
# ---------------------------------------------------------------------------

sha256_verify() {
  local device="$1" mount_point="/mnt/e2e-verify" size_mb="${2:-8}"
  local write_hash read_hash

  log_info "Filesystem validation: mkfs + write ${size_mb}MiB + sha256 verify"
  mkfs.ext4 -F "${device}" >/dev/null 2>&1
  mkdir -p "${mount_point}"
  mount "${device}" "${mount_point}"

  dd if=/dev/urandom of="${mount_point}/testdata" bs=1M count="${size_mb}" \
     conv=fdatasync status=none 2>/dev/null
  write_hash=$(sha256sum "${mount_point}/testdata" | awk '{print $1}')
  sync

  # Drop page cache to force re-read from device
  echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true

  read_hash=$(sha256sum "${mount_point}/testdata" | awk '{print $1}')
  umount "${mount_point}"

  if [[ "${write_hash}" != "${read_hash}" ]]; then
    log_error "SHA-256 MISMATCH: write=${write_hash} read=${read_hash}"
    return 1
  fi
  log_info "SHA-256 OK: ${write_hash}"
}

# ---------------------------------------------------------------------------
# Cleanup management
# ---------------------------------------------------------------------------

_CLEANUP_FUNCS=()

register_cleanup() {
  _CLEANUP_FUNCS+=("$1")
}

run_cleanups() {
  local rc=$?
  set +e
  for (( i=${#_CLEANUP_FUNCS[@]}-1 ; i>=0 ; i-- )); do
    log_info "Cleanup: ${_CLEANUP_FUNCS[$i]}"
    eval "${_CLEANUP_FUNCS[$i]}" || true
  done
  return ${rc}
}

# ---------------------------------------------------------------------------
# Environment defaults
# ---------------------------------------------------------------------------

KEEP_VMS="${KEEP_VMS:-0}"
DESTROY_ON_SUCCESS="${DESTROY_ON_SUCCESS:-0}"
REUSE_VMS="${REUSE_VMS:-0}"
SCENARIOS_FILTER="${SCENARIOS_FILTER:-}"
