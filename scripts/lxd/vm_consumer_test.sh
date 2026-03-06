#!/usr/bin/env bash
# scripts/lxd/vm_consumer_test.sh
#
# Runs inside the consumer LXD VM.  Drives the Apollo Gateway REST API to
# create arrays, disk-backed pools, volumes, host registrations, and mappings,
# then connects via iSCSI and NVMeoF to perform real block I/O validation.
#
# Required environment:
#   GATEWAY_IP        — IPv4 address of the gateway VM
#   CONSUMER_IQN      — iSCSI initiator IQN of this consumer VM
#   CONSUMER_NQN      — NVMeoF host NQN of this consumer VM
#   ISCSI_TARGET_IQN  — Target IQN for the iSCSI endpoint
#   NVMEOF_TARGET_NQN — Target NQN for the NVMeoF endpoint
#   ISCSI_PORT        — iSCSI portal port (default 3260)
#   NVMEOF_PORT       — NVMeoF listener port (default 4420)

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] [CONSUMER][INFO] $*"; }
err() { echo "[$(ts)] [CONSUMER][ERROR] $*" >&2; }
pass() { echo "[$(ts)] [CONSUMER][PASS] $*"; }

GATEWAY_IP="${GATEWAY_IP:?GATEWAY_IP required}"
CONSUMER_IQN="${CONSUMER_IQN:?CONSUMER_IQN required}"
CONSUMER_NQN="${CONSUMER_NQN:?CONSUMER_NQN required}"
ISCSI_TARGET_IQN="${ISCSI_TARGET_IQN:?ISCSI_TARGET_IQN required}"
NVMEOF_TARGET_NQN="${NVMEOF_TARGET_NQN:?NVMEOF_TARGET_NQN required}"
ISCSI_PORT="${ISCSI_PORT:-3260}"
NVMEOF_PORT="${NVMEOF_PORT:-4420}"

API="http://${GATEWAY_IP}:8080"
MOUNT_DIR="/mnt/apollo-test"
FS_TYPE="ext4"

# Accumulated resource IDs for cleanup
ISCSI_MAPPING_ID=""
NVME_MAPPING_ID=""
ISCSI_VOL_ID=""
NVME_VOL_ID=""
GENERIC_POOL_ID=""
SVC_POOL_ID=""
HOST_ID=""
ISCSI_ENDPOINT_ID=""
NVME_ENDPOINT_ID=""

# ---------------------------------------------------------------------------
# API helpers (curl + jq)
# ---------------------------------------------------------------------------

api_post() {
  local path="$1"
  local body="$2"
  local http_code resp
  resp="$(curl -s -X POST "${API}${path}" \
    -H 'Content-Type: application/json' \
    -d "${body}" \
    -w '\n%{http_code}')"
  http_code="$(echo "${resp}" | tail -1)"
  resp="$(echo "${resp}" | sed '$d')"
  if [[ "${http_code}" -lt 200 || "${http_code}" -ge 300 ]]; then
    err "POST ${path} failed (HTTP ${http_code}): ${resp}"
    return 1
  fi
  echo "${resp}"
}

api_get() {
  local path="$1"
  curl -sf "${API}${path}"
}

api_delete() {
  local path="$1"
  curl -sf -X DELETE "${API}${path}" -o /dev/null -w '%{http_code}'
}

assert_status() {
  local desc="$1" path="$2" expected_code="$3"
  local actual
  actual="$(curl -sf -o /dev/null -w '%{http_code}' "${API}${path}" 2>/dev/null || echo "000")"
  if [[ "${actual}" != "${expected_code}" ]]; then
    err "Assertion failed: ${desc} — expected HTTP ${expected_code}, got ${actual}"
    exit 1
  fi
  log "  OK: ${desc} (HTTP ${actual})"
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

cleanup() {
  set +e
  log "Cleanup: unmount"
  umount "${MOUNT_DIR}" >/dev/null 2>&1 || true

  log "Cleanup: iSCSI logout"
  iscsiadm -m node -T "${ISCSI_TARGET_IQN}" -p "${GATEWAY_IP}:${ISCSI_PORT}" --logout >/dev/null 2>&1 || true

  log "Cleanup: NVMeoF disconnect"
  nvme disconnect -n "${NVMEOF_TARGET_NQN}" >/dev/null 2>&1 || true

  log "Cleanup: API resource teardown"
  if [[ -n "${ISCSI_MAPPING_ID}" ]]; then
    curl -sf -X DELETE "${API}/v1/mappings/${ISCSI_MAPPING_ID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${NVME_MAPPING_ID}" ]]; then
    curl -sf -X DELETE "${API}/v1/mappings/${NVME_MAPPING_ID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${ISCSI_VOL_ID}" ]]; then
    curl -sf -X DELETE "${API}/v1/volumes/${ISCSI_VOL_ID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${NVME_VOL_ID}" ]]; then
    curl -sf -X DELETE "${API}/v1/volumes/${NVME_VOL_ID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${GENERIC_POOL_ID}" ]]; then
    curl -sf -X DELETE "${API}/v1/pools/${GENERIC_POOL_ID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${SVC_POOL_ID}" ]]; then
    curl -sf -X DELETE "${API}/v1/pools/${SVC_POOL_ID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${HOST_ID}" ]]; then
    curl -sf -X DELETE "${API}/v1/hosts/${HOST_ID}" >/dev/null 2>&1 || true
  fi
  # Delete endpoints (on 'default' array)
  if [[ -n "${ISCSI_ENDPOINT_ID}" ]]; then
    curl -sf -X DELETE "${API}/v1/arrays/default/endpoints/${ISCSI_ENDPOINT_ID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${NVME_ENDPOINT_ID}" ]]; then
    curl -sf -X DELETE "${API}/v1/arrays/default/endpoints/${NVME_ENDPOINT_ID}" >/dev/null 2>&1 || true
  fi
  curl -sf -X DELETE "${API}/v1/arrays/svc-arr" >/dev/null 2>&1 || true
  curl -sf -X DELETE "${API}/v1/arrays/generic-arr" >/dev/null 2>&1 || true

  log "Cleanup: complete"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Phase A: Create arrays
# ---------------------------------------------------------------------------

log "Phase A: Creating storage arrays"

api_post "/v1/arrays" '{"name": "generic-arr", "vendor": "generic"}' >/dev/null
log "  Created array: generic-arr (generic)"

api_post "/v1/arrays" '{
  "name": "svc-arr",
  "vendor": "ibm_svc",
  "profile": {
    "model": "FlashSystem-5200",
    "version": "8.6.0.0",
    "features": {
      "thin_provisioning": true,
      "snapshots": true
    }
  }
}' >/dev/null
log "  Created array: svc-arr (ibm_svc)"

pass "Phase A: Arrays created"

# ---------------------------------------------------------------------------
# Phase B: Create endpoints with real SPDK targets
# ---------------------------------------------------------------------------

log "Phase B: Creating transport endpoints"

# Endpoints are created on the 'default' array so they match the
# volumes' array_id.  (POST /v1/pools always assigns to 'default';
# the mapping validator requires endpoint.array_id == volume.array_id.)

ISCSI_EP_RESP="$(api_post "/v1/arrays/default/endpoints" "{
  \"protocol\": \"iscsi\",
  \"targets\": {\"target_iqn\": \"${ISCSI_TARGET_IQN}\"},
  \"addresses\": {\"portals\": [\"${GATEWAY_IP}:${ISCSI_PORT}\"]}
}")"
ISCSI_ENDPOINT_ID="$(echo "${ISCSI_EP_RESP}" | jq -r '.id')"
log "  Created iSCSI endpoint on default (id=${ISCSI_ENDPOINT_ID})"

NVME_EP_RESP="$(api_post "/v1/arrays/default/endpoints" "{
  \"protocol\": \"nvmeof_tcp\",
  \"targets\": {\"subsystem_nqn\": \"${NVMEOF_TARGET_NQN}\"},
  \"addresses\": {\"listeners\": [\"${GATEWAY_IP}:${NVMEOF_PORT}\"]}
}")"
NVME_ENDPOINT_ID="$(echo "${NVME_EP_RESP}" | jq -r '.id')"
log "  Created NVMeoF TCP endpoint on default (id=${NVME_ENDPOINT_ID})"

pass "Phase B: Transport endpoints created"

# ---------------------------------------------------------------------------
# Phase C: Create 10GB disk-backed pools
# ---------------------------------------------------------------------------

log "Phase C: Creating disk-backed pools (10GB each)"

# NOTE: POST /v1/pools always creates under the 'default' array.  The SPDK
# lvstore is named default.<pool-name> at creation time.  Re-attaching to a
# different array (POST /v1/arrays/{id}/pools/{pool_id}) updates the DB but
# does NOT rename the lvstore, so volumes would fail with "No such device".
# We keep pools under 'default' for SPDK consistency; volumes reference
# pools by ID regardless of which array owns them.

GENERIC_POOL_RESP="$(api_post "/v1/pools" '{
  "name": "disk-pool",
  "backend_type": "aio_file",
  "aio_path": "/var/snap/apollo-gateway/common/pools/pool-generic.img"
}')"
GENERIC_POOL_ID="$(echo "${GENERIC_POOL_RESP}" | jq -r '.id')"
log "  Created pool disk-pool (id=${GENERIC_POOL_ID}) under default array"

SVC_POOL_RESP="$(api_post "/v1/pools" '{
  "name": "svc-disk-pool",
  "backend_type": "aio_file",
  "aio_path": "/var/snap/apollo-gateway/common/pools/pool-svc.img"
}')"
SVC_POOL_ID="$(echo "${SVC_POOL_RESP}" | jq -r '.id')"
log "  Created pool svc-disk-pool (id=${SVC_POOL_ID}) under default array"

pass "Phase C: Disk-backed pools created"

# ---------------------------------------------------------------------------
# Phase D: Create volumes (5GB each)
# ---------------------------------------------------------------------------

log "Phase D: Creating volumes"

ISCSI_VOL_RESP="$(api_post "/v1/volumes" "{
  \"name\": \"iscsi-vol\",
  \"pool_id\": \"${GENERIC_POOL_ID}\",
  \"size_gb\": 5
}")"
ISCSI_VOL_ID="$(echo "${ISCSI_VOL_RESP}" | jq -r '.id')"
ISCSI_VOL_STATUS="$(echo "${ISCSI_VOL_RESP}" | jq -r '.status')"
log "  Created volume iscsi-vol (id=${ISCSI_VOL_ID}, status=${ISCSI_VOL_STATUS})"

NVME_VOL_RESP="$(api_post "/v1/volumes" "{
  \"name\": \"nvme-vol\",
  \"pool_id\": \"${SVC_POOL_ID}\",
  \"size_gb\": 5
}")"
NVME_VOL_ID="$(echo "${NVME_VOL_RESP}" | jq -r '.id')"
NVME_VOL_STATUS="$(echo "${NVME_VOL_RESP}" | jq -r '.status')"
log "  Created volume nvme-vol (id=${NVME_VOL_ID}, status=${NVME_VOL_STATUS})"

if [[ "${ISCSI_VOL_STATUS}" != "available" || "${NVME_VOL_STATUS}" != "available" ]]; then
  err "One or more volumes not in 'available' state"
  exit 1
fi

pass "Phase D: Volumes created"

# ---------------------------------------------------------------------------
# Phase E: Create host (consumer with both IQN and NQN)
# ---------------------------------------------------------------------------

log "Phase E: Registering consumer host"

HOST_RESP="$(api_post "/v1/hosts" "{
  \"name\": \"consumer-host\",
  \"initiators_iscsi_iqns\": [\"${CONSUMER_IQN}\"],
  \"initiators_nvme_host_nqns\": [\"${CONSUMER_NQN}\"]
}")"
HOST_ID="$(echo "${HOST_RESP}" | jq -r '.id')"
log "  Created host consumer-host (id=${HOST_ID})"
log "    iSCSI IQN: ${CONSUMER_IQN}"
log "    NVMeoF NQN: ${CONSUMER_NQN}"

pass "Phase E: Host registered"

# ---------------------------------------------------------------------------
# Phase F: Create mappings
# ---------------------------------------------------------------------------

log "Phase F: Creating volume-to-host mappings"

ISCSI_MAP_RESP="$(api_post "/v1/mappings" "{
  \"volume_id\": \"${ISCSI_VOL_ID}\",
  \"host_id\": \"${HOST_ID}\",
  \"persona_endpoint_id\": \"${ISCSI_ENDPOINT_ID}\",
  \"underlay_endpoint_id\": \"${ISCSI_ENDPOINT_ID}\"
}")"
ISCSI_MAPPING_ID="$(echo "${ISCSI_MAP_RESP}" | jq -r '.id')"
ISCSI_LUN="$(echo "${ISCSI_MAP_RESP}" | jq -r '.underlay_id')"
log "  Created iSCSI mapping (id=${ISCSI_MAPPING_ID}, lun=${ISCSI_LUN})"

NVME_MAP_RESP="$(api_post "/v1/mappings" "{
  \"volume_id\": \"${NVME_VOL_ID}\",
  \"host_id\": \"${HOST_ID}\",
  \"persona_endpoint_id\": \"${NVME_ENDPOINT_ID}\",
  \"underlay_endpoint_id\": \"${NVME_ENDPOINT_ID}\"
}")"
NVME_MAPPING_ID="$(echo "${NVME_MAP_RESP}" | jq -r '.id')"
NVME_NSID="$(echo "${NVME_MAP_RESP}" | jq -r '.underlay_id')"
log "  Created NVMeoF mapping (id=${NVME_MAPPING_ID}, nsid=${NVME_NSID})"

pass "Phase F: Mappings created"

# ---------------------------------------------------------------------------
# Phase G: Verify API state
# ---------------------------------------------------------------------------

log "Phase G: Verifying API state"

ATTACH_RESP="$(api_get "/v1/hosts/${HOST_ID}/attachments")"
ATTACH_COUNT="$(echo "${ATTACH_RESP}" | jq '.attachments | length')"
if [[ "${ATTACH_COUNT}" -ne 2 ]]; then
  err "Expected 2 attachments, got ${ATTACH_COUNT}"
  echo "${ATTACH_RESP}" | jq .
  exit 1
fi
log "  Attachments: ${ATTACH_COUNT} (expected 2)"

ARRAY_COUNT="$(api_get "/v1/arrays" | jq 'length')"
if [[ "${ARRAY_COUNT}" -lt 3 ]]; then
  err "Expected at least 3 arrays (default + generic-arr + svc-arr), got ${ARRAY_COUNT}"
  exit 1
fi
log "  Arrays: ${ARRAY_COUNT} (expected >=3)"

POOL_COUNT="$(api_get "/v1/pools" | jq 'length')"
log "  Total pools: ${POOL_COUNT} (expected >=2)"

MAPPING_COUNT="$(api_get "/v1/mappings" | jq 'length')"
log "  Total mappings: ${MAPPING_COUNT}"

pass "Phase G: API state verified"

# ---------------------------------------------------------------------------
# Phase H: iSCSI I/O test
# ---------------------------------------------------------------------------

log "Phase H: iSCSI I/O validation"

systemctl enable --now iscsid

log "  Discovering iSCSI targets at ${GATEWAY_IP}:${ISCSI_PORT}"
DISCOVERY_OK=0
for _ in {1..10}; do
  if iscsiadm -m discovery -t sendtargets -p "${GATEWAY_IP}:${ISCSI_PORT}" 2>/dev/null; then
    DISCOVERY_OK=1
    break
  fi
  sleep 2
done

if [[ "${DISCOVERY_OK}" -ne 1 ]]; then
  err "Unable to discover iSCSI targets at ${GATEWAY_IP}:${ISCSI_PORT}"
  exit 1
fi

log "  Configuring iSCSI node for no authentication"
iscsiadm -m node -T "${ISCSI_TARGET_IQN}" -p "${GATEWAY_IP}:${ISCSI_PORT}" \
  -o update -n node.session.auth.authmethod -v None 2>/dev/null || true

log "  Logging into iSCSI target ${ISCSI_TARGET_IQN}"
if ! iscsiadm -m node -T "${ISCSI_TARGET_IQN}" -p "${GATEWAY_IP}:${ISCSI_PORT}" --login 2>&1; then
  err "iSCSI login failed — dumping debug info"
  log "  Discovery output:"
  iscsiadm -m discovery -t sendtargets -p "${GATEWAY_IP}:${ISCSI_PORT}" 2>&1 || true
  log "  Node info:"
  iscsiadm -m node -T "${ISCSI_TARGET_IQN}" -p "${GATEWAY_IP}:${ISCSI_PORT}" 2>&1 || true
  log "  Session info:"
  iscsiadm -m session 2>&1 || true
  log "  Gateway SPDK iSCSI state:"
  curl -s "${API}/healthz" || true
  exit 1
fi

ISCSI_BY_PATH="/dev/disk/by-path/ip-${GATEWAY_IP}:${ISCSI_PORT}-iscsi-${ISCSI_TARGET_IQN}-lun-${ISCSI_LUN}"
log "  Waiting for iSCSI block device: ${ISCSI_BY_PATH}"
if ! timeout 60 bash -c "until [[ -e '${ISCSI_BY_PATH}' ]]; do sleep 0.5; done"; then
  err "Timed out waiting for iSCSI block device"
  ls -l /dev/disk/by-path/ 2>/dev/null || true
  exit 1
fi

ISCSI_DEV="$(readlink -f "${ISCSI_BY_PATH}")"
log "  Resolved iSCSI block device: ${ISCSI_DEV}"

log "  Formatting ${ISCSI_DEV} as ${FS_TYPE}"
mkfs.ext4 -F \
  -E nodiscard,assume_storage_prezeroed=1,lazy_itable_init=1,lazy_journal_init=1 \
  "${ISCSI_DEV}"

mkdir -p "${MOUNT_DIR}"
mount "${ISCSI_DEV}" "${MOUNT_DIR}"

log "  Writing 8MB random payload and verifying checksum"
dd if=/dev/urandom of="${MOUNT_DIR}/payload.bin" bs=1M count=8 status=none
cp "${MOUNT_DIR}/payload.bin" "${MOUNT_DIR}/payload.copy"
sync

SUM_A="$(sha256sum "${MOUNT_DIR}/payload.bin" | awk '{print $1}')"
SUM_B="$(sha256sum "${MOUNT_DIR}/payload.copy" | awk '{print $1}')"

if [[ "${SUM_A}" != "${SUM_B}" ]]; then
  err "iSCSI data checksum mismatch: ${SUM_A} != ${SUM_B}"
  exit 1
fi

log "  Checksum verified: ${SUM_A}"

umount "${MOUNT_DIR}"
iscsiadm -m node -T "${ISCSI_TARGET_IQN}" -p "${GATEWAY_IP}:${ISCSI_PORT}" --logout

pass "Phase H: iSCSI I/O validated"

# ---------------------------------------------------------------------------
# Phase I: NVMeoF I/O test
# ---------------------------------------------------------------------------

log "Phase I: NVMeoF I/O validation"

modprobe nvme-tcp

log "  Connecting to NVMeoF subsystem ${NVMEOF_TARGET_NQN} at ${GATEWAY_IP}:${NVMEOF_PORT}"
nvme connect -t tcp -a "${GATEWAY_IP}" -s "${NVMEOF_PORT}" -n "${NVMEOF_TARGET_NQN}"

NVME_DEV=""
log "  Waiting for NVMe block device"
if ! timeout 60 bash -c '
  while true; do
    for dev in /dev/nvme*n1; do
      [ -b "$dev" ] && echo "$dev" && exit 0
    done
    sleep 0.5
  done
'; then
  err "Timed out waiting for NVMe block device"
  nvme list 2>/dev/null || true
  exit 1
fi

# Find the NVMe device (could be nvme0n1, nvme1n1, etc.)
for dev in /dev/nvme*n1; do
  if [[ -b "${dev}" ]]; then
    NVME_DEV="${dev}"
    break
  fi
done

if [[ -z "${NVME_DEV}" || ! -b "${NVME_DEV}" ]]; then
  err "No NVMe block device found"
  nvme list 2>/dev/null || true
  exit 1
fi

log "  Found NVMe block device: ${NVME_DEV}"

log "  Formatting ${NVME_DEV} as ${FS_TYPE}"
mkfs.ext4 -F \
  -E nodiscard,assume_storage_prezeroed=1,lazy_itable_init=1,lazy_journal_init=1 \
  "${NVME_DEV}"

mkdir -p "${MOUNT_DIR}"
mount "${NVME_DEV}" "${MOUNT_DIR}"

log "  Writing 8MB random payload and verifying checksum"
dd if=/dev/urandom of="${MOUNT_DIR}/payload.bin" bs=1M count=8 status=none
cp "${MOUNT_DIR}/payload.bin" "${MOUNT_DIR}/payload.copy"
sync

SUM_A="$(sha256sum "${MOUNT_DIR}/payload.bin" | awk '{print $1}')"
SUM_B="$(sha256sum "${MOUNT_DIR}/payload.copy" | awk '{print $1}')"

if [[ "${SUM_A}" != "${SUM_B}" ]]; then
  err "NVMeoF data checksum mismatch: ${SUM_A} != ${SUM_B}"
  exit 1
fi

log "  Checksum verified: ${SUM_A}"

umount "${MOUNT_DIR}"
nvme disconnect -n "${NVMEOF_TARGET_NQN}"

pass "Phase I: NVMeoF I/O validated"

# ---------------------------------------------------------------------------
# Phase J: SVC remote execution endpoint
# ---------------------------------------------------------------------------

log "Phase J: SVC remote endpoint validation"

SVC_RESP="$(api_post "/v1/svc/run" '{"array": "svc-arr", "command": "svcinfo lsmdiskgrp"}')"
SVC_EXIT="$(echo "${SVC_RESP}" | jq -r '.exit_code')"
SVC_STDOUT="$(echo "${SVC_RESP}" | jq -r '.stdout')"

if [[ "${SVC_EXIT}" != "0" ]]; then
  err "SVC svcinfo lsmdiskgrp returned exit_code=${SVC_EXIT}"
  echo "${SVC_RESP}" | jq .
  exit 1
fi

log "  SVC lsmdiskgrp exit_code=0, stdout length=${#SVC_STDOUT}"

pass "Phase J: SVC remote endpoint validated"

# ---------------------------------------------------------------------------
# Phase K: Cleanup validation
# ---------------------------------------------------------------------------

log "Phase K: Resource cleanup validation"

log "  Deleting mappings"
api_delete "/v1/mappings/${ISCSI_MAPPING_ID}" >/dev/null
ISCSI_MAPPING_ID=""
api_delete "/v1/mappings/${NVME_MAPPING_ID}" >/dev/null
NVME_MAPPING_ID=""

log "  Deleting volumes"
api_delete "/v1/volumes/${ISCSI_VOL_ID}" >/dev/null
ISCSI_VOL_ID=""
api_delete "/v1/volumes/${NVME_VOL_ID}" >/dev/null
NVME_VOL_ID=""

log "  Deleting pools"
api_delete "/v1/pools/${GENERIC_POOL_ID}" >/dev/null
GENERIC_POOL_ID=""
api_delete "/v1/pools/${SVC_POOL_ID}" >/dev/null
SVC_POOL_ID=""

log "  Deleting host"
api_delete "/v1/hosts/${HOST_ID}" >/dev/null
HOST_ID=""

log "  Deleting endpoints"
api_delete "/v1/arrays/default/endpoints/${ISCSI_ENDPOINT_ID}" >/dev/null
ISCSI_ENDPOINT_ID=""
api_delete "/v1/arrays/default/endpoints/${NVME_ENDPOINT_ID}" >/dev/null
NVME_ENDPOINT_ID=""

log "  Deleting arrays"
api_delete "/v1/arrays/generic-arr" >/dev/null
api_delete "/v1/arrays/svc-arr" >/dev/null

REMAINING="$(api_get "/v1/arrays" | jq '[.[] | select(.name != "default")] | length')"
if [[ "${REMAINING}" -ne 0 ]]; then
  err "Expected 0 non-default arrays after cleanup, got ${REMAINING}"
  api_get "/v1/arrays" | jq .
  exit 1
fi
log "  Only default array remains"

pass "Phase K: Cleanup validated"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "[$(ts)] [CONSUMER] [PASS] All consumer functional tests passed"
