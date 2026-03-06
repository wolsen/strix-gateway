#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# FC-specific verification for the SVC FC driver scenario.
# Sourced by test_flow.sh after os-brick connect / agent reconcile.
#
# Expected env: DEVICE_PATH, FC_TARGET_WWPN

verify_fc() {
  log_info "Verifying FC attach"

  # Check apollo_fc module is loaded
  if ! lsmod | grep -q apollo_fc; then
    log_error "apollo_fc kernel module not loaded"
    return 1
  fi
  log_info "apollo_fc module loaded"

  # Check for virtual FC HBA
  local fc_host_num
  fc_host_num="$(get_fc_host_num)"
  if [[ -z "${fc_host_num}" ]]; then
    log_error "No apollo_fc SCSI host found"
    return 1
  fi
  log_info "FC virtual HBA: host${fc_host_num}"

  # Check for SCSI devices under the apollo_fc host
  local scsi_devs
  scsi_devs="$(ls /sys/class/scsi_host/host${fc_host_num}/device/target*/ 2>/dev/null | wc -l || echo 0)"
  log_info "SCSI targets under host${fc_host_num}: ${scsi_devs}"

  # Verify block device
  if [[ -n "${DEVICE_PATH:-}" && -b "${DEVICE_PATH}" ]]; then
    log_info "Block device verified: ${DEVICE_PATH}"
  elif [[ -n "${DEVICE_PATH:-}" ]]; then
    # For FC, the device may appear under /dev/apollo-fc/ symlinks
    local alt_dev
    alt_dev="$(ls /dev/apollo-fc/* 2>/dev/null | head -n1 || echo "")"
    if [[ -n "${alt_dev}" && -b "${alt_dev}" ]]; then
      log_info "Block device found via alt path: ${alt_dev}"
      DEVICE_PATH="${alt_dev}"
    else
      log_error "No block device found (expected: ${DEVICE_PATH})"
      return 1
    fi
  fi

  log_info "FC verification passed"
}
