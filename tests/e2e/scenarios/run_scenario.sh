#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# Single-scenario runner.
# Called from the host by run_all.sh for each (driver, mode) combination.
#
# This script orchestrates a single E2E scenario:
#   1. Reset gateway state (wipe DB, restart)
#   2. Configure gateway (mode, topology)
#   3. Reset Cinder state (wipe DB)
#   4. Configure Cinder backend
#   5. Create volume type
#   6. Push configs to consumer
#   7. Run test_flow.sh inside consumer VM
#   8. Collect logs
#
# Usage:
#   run_scenario.sh <driver_dir> <mode> <gateway_vm> <consumer_vm> \
#                   <gateway_ip> <e2e_root> <gateway_root> [fc_root]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

source "${E2E_ROOT}/lib/common.sh"
source "${E2E_ROOT}/lib/lxd.sh"

DRIVER_DIR="$1"
MODE="$2"
GATEWAY_VM="$3"
CONSUMER_VM="$4"
GATEWAY_IP="$5"
E2E_DIR="$6"
GATEWAY_ROOT="$7"
FC_ROOT="${8:-}"

DRIVER_NAME="$(basename "${DRIVER_DIR}")"
GATEWAY_PORT="${GATEWAY_PORT:-8080}"
SVC_PASSWORD="${SVC_PASSWORD:-apollo_svc_pass}"
TARGET_IQN="${TARGET_IQN:-iqn.2026-03.com.lunacy:apollo.e2e.target}"
FC_TARGET_WWPN="${FC_TARGET_WWPN:-0x500a09c0ffe1aa01}"

# Determine topology file
if [[ "${MODE}" == "vhost" ]]; then
  TOPO_FILE="${E2E_DIR}/drivers/${DRIVER_NAME}/topo-vhost.yaml"
  ARRAY_NAME="svc-a"
  VHOST_DOMAIN="e2e.test"
else
  TOPO_FILE="${E2E_DIR}/drivers/${DRIVER_NAME}/topo.yaml"
  ARRAY_NAME="default"
  VHOST_DOMAIN=""
fi

BACKEND_CONF="${E2E_DIR}/drivers/${DRIVER_NAME}/cinder-backend.conf"

log_step "Scenario: ${DRIVER_NAME} / ${MODE}"

# ---------------------------------------------------------------------------
# Step 1: Reset + configure gateway
# ---------------------------------------------------------------------------
log_info "Resetting gateway state"
vm_exec "${GATEWAY_VM}" bash -c "
  pkill -f 'uvicorn apollo_gateway.main:app' 2>/dev/null || true
  sleep 1
  rm -f /root/apollo-gateway/apollo_gateway.db
"

# Push topo file
push_file "${GATEWAY_VM}" "${TOPO_FILE}" "/root/topo.yaml"

log_info "Starting gateway (mode=${MODE})"
if [[ "${MODE}" == "vhost" ]]; then
  vm_exec "${GATEWAY_VM}" bash -c "
    source /root/e2e-lib/common.sh
    source /root/e2e-lib/gateway.sh
    start_fake_spdk
    start_gateway /root/apollo-gateway vhost ${VHOST_DOMAIN}
  "
else
  vm_exec "${GATEWAY_VM}" bash -c "
    source /root/e2e-lib/common.sh
    source /root/e2e-lib/gateway.sh
    start_fake_spdk
    start_gateway /root/apollo-gateway non-vhost
  "
fi

# Apply topology
log_info "Applying topology"
vm_exec "${GATEWAY_VM}" bash -c "
  source /root/e2e-lib/common.sh
  source /root/e2e-lib/gateway.sh
  apply_topology /root/apollo-gateway /root/topo.yaml
"

# ---------------------------------------------------------------------------
# Step 2: Reset + configure Cinder
# ---------------------------------------------------------------------------
log_info "Configuring Cinder backend for ${DRIVER_NAME}"

# Push backend conf
push_file "${GATEWAY_VM}" "${BACKEND_CONF}" "/root/cinder-backend.conf"

# Determine which SVC subsystem Cinder should connect to
if [[ "${MODE}" == "vhost" ]]; then
  SVC_SAN_IP="${ARRAY_NAME}.$(vm_exec "${GATEWAY_VM}" hostname).${VHOST_DOMAIN}"
else
  SVC_SAN_IP="127.0.0.1"
fi

vm_exec "${GATEWAY_VM}" bash -c "
  source /root/e2e-lib/common.sh
  source /root/e2e-lib/openstack.sh
  reset_cinder_state
  configure_cinder_backend /root/cinder-backend.conf '${SVC_SAN_IP}' 22 '${SVC_PASSWORD}'
  start_cinder
"

# Create volume type → backend mapping
BACKEND_SECTION=$(grep -m1 '^\[' "${BACKEND_CONF}" | tr -d '[]')
VOLUME_TYPE="type-${DRIVER_NAME}"

vm_exec "${GATEWAY_VM}" bash -c "
  source /root/e2e-lib/common.sh
  source /root/e2e-lib/openstack.sh
  create_volume_type '${VOLUME_TYPE}' '${BACKEND_SECTION}'
"

# ---------------------------------------------------------------------------
# Step 3: Push openrc + driver configs to consumer
# ---------------------------------------------------------------------------
log_info "Pushing configs to consumer VM"

# Get fresh openrc with gateway IP (consumer connects to Keystone on gateway VM)
vm_exec "${GATEWAY_VM}" bash -c "
  source /root/e2e-lib/openstack.sh
  write_openrc /root/openrc 'http://${GATEWAY_IP}:5000/v3'
"

# Pull openrc from gateway and push to consumer
lxc file pull "${GATEWAY_VM}/root/openrc" "/tmp/e2e-openrc"
push_file "${CONSUMER_VM}" "/tmp/e2e-openrc" "/root/openrc"
rm -f "/tmp/e2e-openrc"

# Push driver verify script
push_file "${CONSUMER_VM}" \
  "${E2E_DIR}/drivers/${DRIVER_NAME}/verify.sh" \
  "/root/e2e-drivers/${DRIVER_NAME}/verify.sh"

# ---------------------------------------------------------------------------
# Step 4: Run test flow on consumer
# ---------------------------------------------------------------------------
log_info "Running test flow on consumer VM"

# Determine if FC is needed
NEEDS_FC="false"
if [[ "${DRIVER_NAME}" == *"fc"* ]]; then
  NEEDS_FC="true"
fi

vm_exec "${CONSUMER_VM}" env \
  DRIVER_NAME="${DRIVER_NAME}" \
  VOLUME_TYPE="${VOLUME_TYPE}" \
  GATEWAY_IP="${GATEWAY_IP}" \
  GATEWAY_PORT="${GATEWAY_PORT}" \
  TARGET_IQN="${TARGET_IQN}" \
  FC_TARGET_WWPN="${FC_TARGET_WWPN}" \
  ENABLE_FC="${NEEDS_FC}" \
  SVC_PASSWORD="${SVC_PASSWORD}" \
  bash /root/e2e-vm/test_flow.sh

log_info "Scenario ${DRIVER_NAME}/${MODE}: PASSED"
