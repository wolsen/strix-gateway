#!/usr/bin/env bash
# scripts/lxd/functional_test_lxd.sh
#
# End-to-end functional test for Apollo Gateway using LXD VMs.
#
# Launches a gateway VM (runs the apollo-gateway snap — SPDK + FastAPI) and a
# consumer VM (iSCSI + NVMeoF initiator).  The consumer drives storage
# configuration via the REST API, then connects via iSCSI and NVMeoF to
# validate real block I/O.  A second phase validates vhost multiplexing.
#
# Requirements:
#   - LXD with VM support (lxc, qemu)
#   - snapcraft (to build the snap, unless SNAP_FILE is provided)
#
# Usage:
#   ./scripts/lxd/functional_test_lxd.sh
#
#   # Skip snap build — use a pre-built snap:
#   SNAP_FILE=./apollo-gateway_0.1.0_amd64.snap ./scripts/lxd/functional_test_lxd.sh
#
#   # Keep VMs after test for debugging:
#   KEEP_VMS=1 ./scripts/lxd/functional_test_lxd.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] [INFO] $*"; }
err() { echo "[$(ts)] [ERROR] $*" >&2; }

# lxc launch/init require a PTY for the image download progress bar;
# without one they silently return exit-code 1 even on success.
# Wrap these calls with `script -q` to provide a pseudo-terminal.
lxc_pty() { script -q -c "lxc $*" /dev/null; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ---------------------------------------------------------------------------
# Tunables (override via environment)
# ---------------------------------------------------------------------------

GATEWAY_VM="${GATEWAY_VM:-apollo-gw-test}"
CONSUMER_VM="${CONSUMER_VM:-apollo-consumer-test}"
IMAGE="${LXD_IMAGE:-ubuntu:24.04}"
KEEP_VMS="${KEEP_VMS:-0}"

VM_MEMORY_GW="${VM_MEMORY_GW:-8GiB}"
VM_CPUS_GW="${VM_CPUS_GW:-4}"
VM_MEMORY_CONSUMER="${VM_MEMORY_CONSUMER:-4GiB}"
VM_CPUS_CONSUMER="${VM_CPUS_CONSUMER:-2}"

SNAP_FILE="${SNAP_FILE:-}"

ISCSI_TARGET_IQN="iqn.2026-02.lunacysystems.apollo:generic-arr:tgt"
NVMEOF_TARGET_NQN="nqn.2026-02.io.lunacysystems:apollo:svc-arr:tgt"
ISCSI_PORT=3260
NVMEOF_PORT=4420

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

cleanup() {
  local rc=$?

  if [[ "${KEEP_VMS}" == "1" ]]; then
    log "KEEP_VMS=1, leaving VMs running: ${GATEWAY_VM}, ${CONSUMER_VM}"
    return
  fi

  log "Cleaning up LXD VMs"
  lxc delete -f "${CONSUMER_VM}" >/dev/null 2>&1 || true
  lxc delete -f "${GATEWAY_VM}" >/dev/null 2>&1 || true

  if [[ ${rc} -ne 0 ]]; then
    err "Functional test FAILED (exit code ${rc})"
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "Missing required command: $1"
    exit 1
  fi
}

require_cmd lxc

# ---------------------------------------------------------------------------
# Phase 0: Ensure snap is available
# ---------------------------------------------------------------------------

if [[ -z "${SNAP_FILE}" ]]; then
  # Look for an existing snap in the repo root
  SNAP_FILE="$(ls -1 "${REPO_ROOT}"/apollo-gateway_*.snap 2>/dev/null | head -1 || true)"
fi

if [[ -z "${SNAP_FILE}" || ! -f "${SNAP_FILE}" ]]; then
  log "No pre-built snap found — building with snapcraft"
  require_cmd snapcraft
  (cd "${REPO_ROOT}" && snapcraft)
  SNAP_FILE="$(ls -1 "${REPO_ROOT}"/apollo-gateway_*.snap | head -1)"
fi

if [[ ! -f "${SNAP_FILE}" ]]; then
  err "Snap build failed or file not found"
  exit 1
fi

SNAP_FILE="$(readlink -f "${SNAP_FILE}")"
log "Using snap: ${SNAP_FILE}"

# ---------------------------------------------------------------------------
# Phase 1: Launch VMs
# ---------------------------------------------------------------------------

log "Launching VMs (${GATEWAY_VM}, ${CONSUMER_VM}) from ${IMAGE}"
lxc_pty launch "${IMAGE}" "${GATEWAY_VM}" --vm \
  -c security.secureboot=false \
  -c limits.memory="${VM_MEMORY_GW}" \
  -c limits.cpu="${VM_CPUS_GW}"
lxc_pty launch "${IMAGE}" "${CONSUMER_VM}" --vm \
  -c security.secureboot=false \
  -c limits.memory="${VM_MEMORY_CONSUMER}" \
  -c limits.cpu="${VM_CPUS_CONSUMER}"

wait_for_vm_agent() {
  local vm_name="$1"
  local timeout_sec="${2:-240}"
  local start_ts now_ts

  start_ts="$(date +%s)"
  while true; do
    if lxc exec "${vm_name}" -- true >/dev/null 2>&1; then
      return 0
    fi
    now_ts="$(date +%s)"
    if (( now_ts - start_ts > timeout_sec )); then
      err "Timed out waiting for VM agent in ${vm_name}"
      return 1
    fi
    sleep 2
  done
}

log "Waiting for VM agents"
wait_for_vm_agent "${GATEWAY_VM}"
wait_for_vm_agent "${CONSUMER_VM}"

wait_for_cloud_init() {
  local vm_name="$1"
  local timeout_sec="${2:-300}"
  local start_ts now_ts status
  start_ts="$(date +%s)"
  while true; do
    status="$(lxc exec "${vm_name}" -- cloud-init status 2>/dev/null || true)"
    if echo "${status}" | grep -q 'done'; then
      return 0
    fi
    if echo "${status}" | grep -q 'error'; then
      err "cloud-init reported error in ${vm_name}"
      return 1
    fi
    now_ts="$(date +%s)"
    if (( now_ts - start_ts > timeout_sec )); then
      err "Timed out waiting for cloud-init in ${vm_name}"
      return 1
    fi
    sleep 5
  done
}

log "Waiting for cloud-init completion"
wait_for_cloud_init "${GATEWAY_VM}"
wait_for_cloud_init "${CONSUMER_VM}"

# ---------------------------------------------------------------------------
# Phase 2: Gateway setup
# ---------------------------------------------------------------------------

log "Pushing snap to gateway VM"
lxc file push "${SNAP_FILE}" "${GATEWAY_VM}/root/apollo-gateway.snap"

log "Installing snap on gateway VM"
# snap install may need to download prerequisites (snapd, core24) from the
# store — transient network errors in LXD VMs are common.  Retry up to 3 times.
for attempt in 1 2 3; do
  if lxc exec "${GATEWAY_VM}" -- snap install --dangerous --devmode /root/apollo-gateway.snap; then
    break
  fi
  if [[ "${attempt}" -eq 3 ]]; then
    err "snap install failed after 3 attempts"
    exit 1
  fi
  log "snap install attempt ${attempt} failed — retrying in 30s"
  sleep 30
done

log "Creating backing files for disk-backed pools"
lxc exec "${GATEWAY_VM}" -- mkdir -p /var/snap/apollo-gateway/common/pools
lxc exec "${GATEWAY_VM}" -- truncate -s 10G /var/snap/apollo-gateway/common/pools/pool-generic.img
lxc exec "${GATEWAY_VM}" -- truncate -s 10G /var/snap/apollo-gateway/common/pools/pool-svc.img

log "Starting apollo-gateway snap services"
lxc exec "${GATEWAY_VM}" -- snap start apollo-gateway

# Wait for SPDK socket
log "Waiting for SPDK socket"
if ! lxc exec "${GATEWAY_VM}" -- bash -c \
  'timeout 60 bash -c "until [ -S /var/snap/apollo-gateway/common/run/spdk.sock ]; do sleep 1; done"'
then
  err "Timed out waiting for SPDK socket"
  lxc exec "${GATEWAY_VM}" -- snap logs apollo-gateway.spdk-tgt -n 50 || true
  exit 1
fi

# Wait for gateway healthz
log "Waiting for gateway API (healthz)"
if ! lxc exec "${GATEWAY_VM}" -- bash -c \
  'for i in $(seq 1 60); do curl -sf http://localhost:8080/healthz >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1'
then
  err "Timed out waiting for gateway healthz"
  lxc exec "${GATEWAY_VM}" -- snap logs apollo-gateway.apollo-gateway -n 50 || true
  exit 1
fi

log "Gateway is healthy"

# ---------------------------------------------------------------------------
# Phase 3: Gather consumer info
# ---------------------------------------------------------------------------

GATEWAY_IP="$(lxc exec "${GATEWAY_VM}" -- bash -lc \
  "ip -4 -o addr show dev enp5s0 2>/dev/null | awk '{print \$4}' | cut -d/ -f1 | head -n1")"
if [[ -z "${GATEWAY_IP}" ]]; then
  GATEWAY_IP="$(lxc exec "${GATEWAY_VM}" -- bash -lc \
    "ip -4 -o addr show scope global | awk '{print \$4}' | cut -d/ -f1 | head -n1")"
fi

if [[ -z "${GATEWAY_IP}" ]]; then
  err "Could not determine gateway VM IPv4 address"
  exit 1
fi

log "Gateway VM IP: ${GATEWAY_IP}"

log "Pushing apollo-gateway repository into consumer VM"
lxc file push -r "${REPO_ROOT}" "${CONSUMER_VM}/root/"

log "Disabling automatic apt services on consumer VM"
lxc exec "${CONSUMER_VM}" -- bash -c \
  'systemctl stop apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service 2>/dev/null || true
   systemctl mask apt-daily.service apt-daily-upgrade.service unattended-upgrades.service 2>/dev/null || true
   # Wait for any in-flight dpkg/apt to finish
   while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
      || fuser /var/lib/dpkg/lock >/dev/null 2>&1 \
      || fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do
     echo "Waiting for apt/dpkg lock..."
     sleep 3
   done'

log "Installing iSCSI, NVMe tools, and extra kernel modules on consumer VM"
if ! lxc exec "${CONSUMER_VM}" -- bash -c \
  'export DEBIAN_FRONTEND=noninteractive \
   && apt-get update -q \
   && apt-get install -y -q open-iscsi nvme-cli curl jq \
        linux-modules-extra-$(uname -r)'; then
  err "apt-get failed, retrying after 15s..."
  sleep 15
  lxc exec "${CONSUMER_VM}" -- bash -c \
    'export DEBIAN_FRONTEND=noninteractive \
     && apt-get update -q \
     && apt-get install -y -q open-iscsi nvme-cli curl jq \
          linux-modules-extra-$(uname -r)'
fi

# Ubuntu 24.04 uses GenerateName=yes instead of a static IQN.
# Generate a persistent IQN if one isn't already set.
lxc exec "${CONSUMER_VM}" -- bash -c '
  if ! grep -q "^InitiatorName=" /etc/iscsi/initiatorname.iscsi 2>/dev/null; then
    IQN="$(iscsi-iname)"
    echo "InitiatorName=${IQN}" > /etc/iscsi/initiatorname.iscsi
    systemctl restart iscsid 2>/dev/null || true
  fi'
CONSUMER_IQN="$(lxc exec "${CONSUMER_VM}" -- bash -c \
  "grep '^InitiatorName=' /etc/iscsi/initiatorname.iscsi | cut -d= -f2")"
if [[ -z "${CONSUMER_IQN}" ]]; then
  err "Could not read consumer iSCSI IQN"
  exit 1
fi
log "Consumer iSCSI IQN: ${CONSUMER_IQN}"

# Ensure NVMe host NQN exists
lxc exec "${CONSUMER_VM}" -- bash -c \
  'if [ ! -f /etc/nvme/hostnqn ]; then
    mkdir -p /etc/nvme
    uuidgen | xargs -I{} printf "nqn.2014-08.org.nvmexpress:uuid:%s\n" {} > /etc/nvme/hostnqn
  fi'
CONSUMER_NQN="$(lxc exec "${CONSUMER_VM}" -- cat /etc/nvme/hostnqn)"
log "Consumer NVMeoF host NQN: ${CONSUMER_NQN}"

# ---------------------------------------------------------------------------
# Phase 4: Run main functional test
# ---------------------------------------------------------------------------

log "Pushing consumer test script"
lxc file push "${REPO_ROOT}/scripts/lxd/vm_consumer_test.sh" "${CONSUMER_VM}/root/vm_consumer_test.sh"
lxc exec "${CONSUMER_VM}" -- chmod +x /root/vm_consumer_test.sh

log "Running consumer functional test"
# Run the test inside the VM with output to a log file to avoid SIGPIPE
# issues with lxc exec when there is no PTY.
lxc exec "${CONSUMER_VM}" -- bash -c "
  env GATEWAY_IP='${GATEWAY_IP}' \
      CONSUMER_IQN='${CONSUMER_IQN}' \
      CONSUMER_NQN='${CONSUMER_NQN}' \
      ISCSI_TARGET_IQN='${ISCSI_TARGET_IQN}' \
      NVMEOF_TARGET_NQN='${NVMEOF_TARGET_NQN}' \
      ISCSI_PORT='${ISCSI_PORT}' \
      NVMEOF_PORT='${NVMEOF_PORT}' \
  bash /root/vm_consumer_test.sh > /root/consumer_test.log 2>&1
  echo \$? > /root/consumer_test.rc"
lxc exec "${CONSUMER_VM}" -- cat /root/consumer_test.log || true
consumer_rc="$(lxc exec "${CONSUMER_VM}" -- cat /root/consumer_test.rc 2>/dev/null || echo 1)"
if [[ "${consumer_rc}" != "0" ]]; then
  err "Consumer functional test FAILED (exit code ${consumer_rc})"
  exit 1
fi

log "Main functional test PASSED"

# ---------------------------------------------------------------------------
# Phase 5: Vhost validation
# ---------------------------------------------------------------------------

log "Stopping gateway for vhost mode reconfiguration"
lxc exec "${GATEWAY_VM}" -- snap stop apollo-gateway.apollo-gateway

log "Configuring vhost mode via snap set"
# Batch all vhost settings in a single snap set to avoid the configure hook
# restarting the service with incomplete configuration (e.g. vhost-enabled=true
# but vhost-domain not yet set).
lxc exec "${GATEWAY_VM}" -- snap set apollo-gateway \
  vhost-enabled=true \
  vhost-domain=test.local \
  vhost-hostname-override=apollo-gw

log "Restarting gateway in vhost mode"
# The configure hook will have already restarted the service; ensure it is up.
lxc exec "${GATEWAY_VM}" -- snap start apollo-gateway.apollo-gateway 2>/dev/null || true

log "Waiting for gateway API (vhost mode, HTTPS port 443)"
if ! lxc exec "${GATEWAY_VM}" -- bash -c \
  'for i in $(seq 1 60); do curl -skf https://localhost:443/healthz >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1'
then
  err "Timed out waiting for gateway healthz in vhost mode"
  lxc exec "${GATEWAY_VM}" -- snap logs apollo-gateway.apollo-gateway -n 50 || true
  exit 1
fi

log "Gateway healthy in vhost mode — running vhost validation"

lxc file push "${REPO_ROOT}/scripts/lxd/vm_vhost_test.sh" "${CONSUMER_VM}/root/vm_vhost_test.sh"
lxc exec "${CONSUMER_VM}" -- chmod +x /root/vm_vhost_test.sh

lxc exec "${CONSUMER_VM}" -- bash -c "
  env GATEWAY_IP='${GATEWAY_IP}' \
  bash /root/vm_vhost_test.sh > /root/vhost_test.log 2>&1
  echo \$? > /root/vhost_test.rc"
lxc exec "${CONSUMER_VM}" -- cat /root/vhost_test.log || true
vhost_rc="$(lxc exec "${CONSUMER_VM}" -- cat /root/vhost_test.rc 2>/dev/null || echo 1)"
if [[ "${vhost_rc}" != "0" ]]; then
  err "Vhost validation FAILED (exit code ${vhost_rc})"
  exit 1
fi

log "Vhost validation PASSED"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo "[$(ts)] [PASS] Apollo Gateway LXD functional test complete"
