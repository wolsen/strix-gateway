#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# iSCSI-specific verification for the Hitachi HBSD iSCSI scenario.
# Sourced by test_flow.sh after os-brick connect returns a device path.
#
# Expected env: DEVICE_PATH, TARGET_IQN

verify_iscsi() {
  log_info "Verifying iSCSI session (Hitachi HBSD)"

  # Check active iSCSI session
  local sessions
  sessions="$(iscsiadm -m session 2>/dev/null || echo "")"
  if [[ -z "${sessions}" ]]; then
    log_error "No active iSCSI sessions"
    return 1
  fi
  log_info "Active iSCSI sessions: $(echo "${sessions}" | wc -l)"

  # Verify target IQN in sessions
  if [[ -n "${TARGET_IQN:-}" ]]; then
    if echo "${sessions}" | grep -q "${TARGET_IQN}"; then
      log_info "Target IQN found in active sessions: ${TARGET_IQN}"
    else
      log_error "Target IQN '${TARGET_IQN}' not found in sessions"
      echo "${sessions}"
      return 1
    fi
  fi

  # Verify block device exists
  if [[ -n "${DEVICE_PATH:-}" && -b "${DEVICE_PATH}" ]]; then
    log_info "Block device verified: ${DEVICE_PATH}"
  elif [[ -n "${DEVICE_PATH:-}" ]]; then
    log_error "Expected block device not found: ${DEVICE_PATH}"
    return 1
  fi

  log_info "iSCSI verification passed"
}
