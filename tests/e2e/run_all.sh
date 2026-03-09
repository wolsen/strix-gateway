#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# Top-level E2E test orchestrator.
#
# Boots 2 LXD VMs (gateway + consumer), sets up both, then iterates over the
# scenario matrix running each scenario one at a time. Exits 0 only if every
# scenario passes.
#
# Usage:
#   ./run_all.sh [--keep-vms] [--reuse-vms] [--filter <regex>]
#
# Environment:
#   KEEP_VMS=1            - keep VMs on failure (default: destroy)
#   REUSE_VMS=1           - skip VM creation if they already exist
#   DESTROY_ON_SUCCESS=1  - destroy VMs even on success (default: keep)
#   SCENARIOS_FILTER=...  - regex to select subset of scenarios
#   LXD_IMAGE=...         - LXD image (default: ubuntu:24.04)
#   GATEWAY_ROOT=...      - path to strix-gateway repo (auto-detected)
#   FC_ROOT=...           - path to strix-fc repo (auto-detected)
set -euo pipefail

E2E_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${E2E_ROOT}/lib/common.sh"
source "${E2E_ROOT}/lib/lxd.sh"

MATRIX_FILE="${E2E_ROOT}/scenarios/matrix.env"
declare -a SCENARIO_ENTRIES=()

if [[ -f "${MATRIX_FILE}" ]]; then
  source "${MATRIX_FILE}"
fi

if declare -p SCENARIOS >/dev/null 2>&1; then
  SCENARIO_ENTRIES=("${SCENARIOS[@]}")
else
  while IFS= read -r line; do
    [[ -z "${line}" || "${line}" == "#"* ]] && continue
    SCENARIO_ENTRIES+=("${line}")
  done < "${MATRIX_FILE}"
fi

# ── Argument parsing ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-vms)         KEEP_VMS=1; shift ;;
    --reuse-vms)        REUSE_VMS=1; shift ;;
    --filter)           SCENARIOS_FILTER="$2"; shift 2 ;;
    --destroy-on-success) DESTROY_ON_SUCCESS=1; shift ;;
    -h|--help)
      echo "Usage: $0 [--keep-vms] [--reuse-vms] [--filter <regex>] [--destroy-on-success]"
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ── Path discovery ──────────────────────────────────────────────────────────
# The E2E dir lives inside strix-gateway. Walk up to find the repo root.
GATEWAY_ROOT="${GATEWAY_ROOT:-$(cd "${E2E_ROOT}/../.." && pwd)}"
FC_ROOT="${FC_ROOT:-$(cd "${GATEWAY_ROOT}/../strix-fc" 2>/dev/null && pwd || echo "")}"

if [[ ! -f "${GATEWAY_ROOT}/pyproject.toml" ]]; then
  log_error "Cannot locate strix-gateway repo (expected at ${GATEWAY_ROOT})"
  exit 1
fi

log_info "strix-gateway : ${GATEWAY_ROOT}"
log_info "strix-fc     : ${FC_ROOT:-<not found>}"

# ── VM names ────────────────────────────────────────────────────────────────
GATEWAY_VM="e2e-gw-$$"
CONSUMER_VM="e2e-con-$$"

# ── Cleanup handler ─────────────────────────────────────────────────────────
_cleanup_vms() {
  if [[ "${KEEP_VMS:-0}" == "1" ]]; then
    log_info "KEEP_VMS=1 → VMs preserved: ${GATEWAY_VM}, ${CONSUMER_VM}"
    return 0
  fi
  log_info "Destroying VMs …"
  destroy_vm "${GATEWAY_VM}" || true
  destroy_vm "${CONSUMER_VM}" || true
}

register_cleanup "_cleanup_vms"

# ── Create / reuse VMs ──────────────────────────────────────────────────────
log_step "Preparing LXD VMs"

if [[ "${REUSE_VMS:-0}" == "1" ]] && vm_exists "${GATEWAY_VM}" && vm_exists "${CONSUMER_VM}"; then
  log_info "Reusing existing VMs"
else
  # Create fresh VMs in parallel
  create_vm "${GATEWAY_VM}" "${LXD_IMAGE:-ubuntu:24.04}" &
  create_vm "${CONSUMER_VM}" "${LXD_IMAGE:-ubuntu:24.04}" &
  wait

  wait_vm_ready "${GATEWAY_VM}"
  wait_vm_ready "${CONSUMER_VM}"
fi

GATEWAY_IP="$(get_vm_ip "${GATEWAY_VM}")"
CONSUMER_IP="$(get_vm_ip "${CONSUMER_VM}")"
log_info "Gateway IP : ${GATEWAY_IP}"
log_info "Consumer IP: ${CONSUMER_IP}"

# ── Push repos + lib scripts ───────────────────────────────────────────────
log_step "Pushing repos to VMs"

push_repos "${GATEWAY_VM}" "${GATEWAY_ROOT}" "${FC_ROOT}"
push_repos "${CONSUMER_VM}" "${GATEWAY_ROOT}" "${FC_ROOT}"

# Push E2E lib scripts to both VMs
for vm in "${GATEWAY_VM}" "${CONSUMER_VM}"; do
  vm_exec "${vm}" mkdir -p /root/e2e-lib /root/e2e-vm /root/e2e-drivers
  for lib in common.sh lxd.sh openstack.sh gateway.sh consumer.sh; do
    push_file "${vm}" "${E2E_ROOT}/lib/${lib}" "/root/e2e-lib/${lib}"
  done
done

# Push VM setup + test scripts to their VMs
push_file "${GATEWAY_VM}" "${E2E_ROOT}/vm/gateway_setup.sh" "/root/e2e-vm/gateway_setup.sh"
push_file "${CONSUMER_VM}" "${E2E_ROOT}/vm/consumer_setup.sh" "/root/e2e-vm/consumer_setup.sh"
push_file "${CONSUMER_VM}" "${E2E_ROOT}/vm/test_flow.sh" "/root/e2e-vm/test_flow.sh"

# ── Determine if any FC scenario is selected ────────────────────────────────
_matrix_needs_fc() {
  local filter="${SCENARIOS_FILTER:-}"
  local entry driver mode enable_fc
  for entry in "${SCENARIO_ENTRIES[@]}"; do
    read -r driver mode enable_fc <<< "${entry}"
    [[ -z "${driver}" || "${driver}" == "#"* ]] && continue
    if [[ -n "${filter}" ]] && ! echo "${driver}/${mode}" | grep -qE "${filter}"; then
      continue
    fi
    if [[ "${enable_fc}" == "true" ]]; then
      return 0
    fi
  done
  return 1
}

ENABLE_FC_GLOBAL="false"
if _matrix_needs_fc; then
  ENABLE_FC_GLOBAL="true"
fi

# ── Run gateway setup ──────────────────────────────────────────────────────
log_step "Setting up gateway VM"
vm_exec "${GATEWAY_VM}" env \
  GATEWAY_IP="${GATEWAY_IP}" \
  CONSUMER_IP="${CONSUMER_IP}" \
  bash /root/e2e-vm/gateway_setup.sh

# ── Run consumer setup ─────────────────────────────────────────────────────
log_step "Setting up consumer VM"
vm_exec "${CONSUMER_VM}" env \
  GATEWAY_IP="${GATEWAY_IP}" \
  CONSUMER_IP="${CONSUMER_IP}" \
  ENABLE_FC="${ENABLE_FC_GLOBAL}" \
  bash /root/e2e-vm/consumer_setup.sh

# ── Run scenarios ──────────────────────────────────────────────────────────
log_step "Running scenario matrix"

declare -a RESULTS=()
PASS=0
FAIL=0
SKIP=0

for entry in "${SCENARIO_ENTRIES[@]}"; do
  read -r driver mode enable_fc <<< "${entry}"
  [[ -z "${driver}" || "${driver}" == "#"* ]] && continue

  scenario_tag="${driver}/${mode}"

  # Filter check
  if [[ -n "${SCENARIOS_FILTER:-}" ]] && ! echo "${scenario_tag}" | grep -qE "${SCENARIOS_FILTER}"; then
    log_info "SKIP (filtered): ${scenario_tag}"
    RESULTS+=("SKIP  ${scenario_tag}")
    ((SKIP++)) || true
    continue
  fi

  # FC requirement check
  if [[ "${enable_fc}" == "true" ]] && [[ -z "${FC_ROOT}" ]]; then
    log_info "SKIP (no strix-fc): ${scenario_tag}"
    RESULTS+=("SKIP  ${scenario_tag}")
    ((SKIP++)) || true
    continue
  fi

  driver_dir="${E2E_ROOT}/drivers/${driver}"
  if [[ ! -d "${driver_dir}" ]]; then
    log_error "Driver directory not found: ${driver_dir}"
    RESULTS+=("FAIL  ${scenario_tag} (missing driver dir)")
    ((FAIL++)) || true
    continue
  fi

  log_step ">>> Scenario: ${scenario_tag}"

  if bash "${E2E_ROOT}/scenarios/run_scenario.sh" \
    "${driver_dir}" "${mode}" \
    "${GATEWAY_VM}" "${CONSUMER_VM}" \
    "${GATEWAY_IP}" "${E2E_ROOT}" \
    "${GATEWAY_ROOT}" "${FC_ROOT}"; then
    RESULTS+=("PASS  ${scenario_tag}")
    ((PASS++)) || true
  else
    RESULTS+=("FAIL  ${scenario_tag}")
    ((FAIL++)) || true
    log_error "Scenario FAILED: ${scenario_tag}"
    # Collect logs
    log_info "Collecting gateway logs …"
    vm_exec "${GATEWAY_VM}" bash -c "
      journalctl -u strix-gateway --no-pager -n 100 2>/dev/null || true
      cat /var/log/cinder/cinder-all.log 2>/dev/null | tail -200 || true
    " || true
    log_info "Collecting consumer logs …"
    vm_exec "${CONSUMER_VM}" bash -c "
      journalctl --no-pager -n 100 2>/dev/null || true
    " || true
  fi

done

# ── Summary ─────────────────────────────────────────────────────────────────
log_step "Results Summary"
echo ""
echo "╔════════════════════════════════════════════════════╗"
printf "║  %-48s  ║\n" "E2E Test Results"
echo "╠════════════════════════════════════════════════════╣"
for r in "${RESULTS[@]}"; do
  printf "║  %-48s  ║\n" "${r}"
done
echo "╠════════════════════════════════════════════════════╣"
printf "║  PASS: %-3d   FAIL: %-3d   SKIP: %-3d              ║\n" "${PASS}" "${FAIL}" "${SKIP}"
echo "╚════════════════════════════════════════════════════╝"
echo ""

# ── Cleanup ─────────────────────────────────────────────────────────────────
if [[ "${FAIL}" -eq 0 ]]; then
  if [[ "${DESTROY_ON_SUCCESS:-0}" == "1" ]]; then
    log_info "All scenarios passed — destroying VMs"
    destroy_vm "${GATEWAY_VM}" || true
    destroy_vm "${CONSUMER_VM}" || true
    # Deregister cleanup since we already destroyed
    CLEANUP_HANDLERS=()
  else
    log_info "All scenarios passed — VMs preserved: ${GATEWAY_VM}, ${CONSUMER_VM}"
    KEEP_VMS=1  # prevent cleanup trap from destroying
  fi
  exit 0
else
  log_error "${FAIL} scenario(s) FAILED"
  exit 1
fi
