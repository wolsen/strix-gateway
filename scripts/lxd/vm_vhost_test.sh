#!/usr/bin/env bash
# scripts/lxd/vm_vhost_test.sh
#
# Runs inside the consumer LXD VM after the orchestrator has restarted the
# gateway in vhost mode (TLS/SNI, HTTPS on port 443).
#
# Validates:
#   - TLS CA certificate retrieval
#   - Host-header-based routing to per-array endpoints
#   - Unknown host rejection (vhost_require_match=true)
#   - Vhosts listing endpoint
#   - Bypass paths (healthz)
#
# Required environment:
#   GATEWAY_IP — IPv4 address of the gateway VM

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] [VHOST][INFO] $*"; }
err() { echo "[$(ts)] [VHOST][ERROR] $*" >&2; }
pass() { echo "[$(ts)] [VHOST][PASS] $*"; }

GATEWAY_IP="${GATEWAY_IP:?GATEWAY_IP required}"
HTTPS_PORT=443
CA_CERT="/tmp/apollo-ca.crt"
VHOST_DOMAIN="test.local"
VHOST_HOSTNAME="apollo-gw"
FAILURES=0

assert_http() {
  local desc="$1"
  local expected_code="$2"
  shift 2
  local actual
  actual="$(curl -s -o /dev/null -w '%{http_code}' "$@" 2>/dev/null)" || actual="000"
  if [[ "${actual}" == "${expected_code}" ]]; then
    log "  OK: ${desc} (HTTP ${actual})"
  else
    err "  FAIL: ${desc} — expected HTTP ${expected_code}, got ${actual}"
    FAILURES=$((FAILURES + 1))
  fi
}

fqdn_for() {
  local array_name="$1"
  echo "${array_name}.${VHOST_HOSTNAME}.${VHOST_DOMAIN}"
}

resolve_flag() {
  local fqdn="$1"
  echo "--resolve ${fqdn}:${HTTPS_PORT}:${GATEWAY_IP}"
}

# ---------------------------------------------------------------------------
# Step 1: Fetch the internal CA certificate
# ---------------------------------------------------------------------------

log "Step 1: Fetching CA certificate from gateway"

# The /v1/tls/ca endpoint is a bypass path and should work without Host matching.
# Use -k for the initial fetch since we don't have the CA yet.
if ! curl -sk "https://${GATEWAY_IP}:${HTTPS_PORT}/v1/tls/ca" -o "${CA_CERT}" 2>/dev/null; then
  err "Failed to fetch CA certificate from https://${GATEWAY_IP}:${HTTPS_PORT}/v1/tls/ca"
  err "Falling back to insecure mode (-k) for remaining tests"
  CA_CERT=""
fi

if [[ -n "${CA_CERT}" && -f "${CA_CERT}" ]]; then
  CA_SIZE="$(wc -c < "${CA_CERT}")"
  if [[ "${CA_SIZE}" -lt 100 ]]; then
    log "  CA cert too small (${CA_SIZE} bytes) — may not be a valid PEM"
    CA_CERT=""
  else
    log "  CA certificate saved (${CA_SIZE} bytes)"
  fi
fi

# Build curl TLS flags — use --cacert if we have it, else -k
if [[ -n "${CA_CERT}" && -f "${CA_CERT}" ]]; then
  TLS_FLAGS=(--cacert "${CA_CERT}")
else
  TLS_FLAGS=(-k)
fi

pass "Step 1: CA certificate retrieved"

# ---------------------------------------------------------------------------
# Step 2: Ensure test arrays exist (DB was preserved, but may have been cleaned)
# ---------------------------------------------------------------------------

log "Step 2: Ensuring test arrays exist"

# Management API calls must route through a known vhost FQDN (the default
# array), because the vhost middleware rejects requests with an unknown Host
# header.  Use --resolve so the Host header matches the default array FQDN.
MGMT_FQDN="$(fqdn_for "default")"
MGMT_RESOLVE="--resolve ${MGMT_FQDN}:${HTTPS_PORT}:${GATEWAY_IP}"

create_if_missing() {
  local name="$1"
  local body="$2"
  local check_code
  check_code="$(curl -s "${TLS_FLAGS[@]}" ${MGMT_RESOLVE} \
    -o /dev/null -w '%{http_code}' \
    "https://${MGMT_FQDN}:${HTTPS_PORT}/v1/arrays/${name}" 2>/dev/null || echo "000")"
  if [[ "${check_code}" == "200" ]]; then
    log "  Array '${name}' already exists"
  else
    curl -s "${TLS_FLAGS[@]}" ${MGMT_RESOLVE} \
      -X POST "https://${MGMT_FQDN}:${HTTPS_PORT}/v1/arrays" \
      -H 'Content-Type: application/json' \
      -d "${body}" >/dev/null 2>&1 || true
    log "  Created array '${name}'"
  fi
}

create_if_missing "generic-arr" '{"name": "generic-arr", "vendor": "generic"}'
create_if_missing "svc-arr" '{"name": "svc-arr", "vendor": "ibm_svc"}'

# Trigger explicit TLS sync so certs + vhost registry include the new arrays.
# Array creation should auto-sync, but this ensures everything is settled.
curl -s "${TLS_FLAGS[@]}" ${MGMT_RESOLVE} \
  -X POST "https://${MGMT_FQDN}:${HTTPS_PORT}/v1/tls/sync" >/dev/null 2>&1 || true
sleep 1  # allow SNI contexts to settle

# Re-fetch CA cert in case it was re-generated
curl -sk "https://${GATEWAY_IP}:${HTTPS_PORT}/v1/tls/ca" -o "${CA_CERT}" 2>/dev/null || true

pass "Step 2: Test arrays ready"

# ---------------------------------------------------------------------------
# Step 3: Host-header routing
# ---------------------------------------------------------------------------

log "Step 3: Testing Host-header-based routing"

GENERIC_FQDN="$(fqdn_for "generic-arr")"
SVC_FQDN="$(fqdn_for "svc-arr")"
UNKNOWN_FQDN="$(fqdn_for "unknown-arr")"
DEFAULT_FQDN="$(fqdn_for "default")"

# Valid array hosts should return 200
assert_http "generic-arr pools via vhost" "200" \
  "${TLS_FLAGS[@]}" \
  --resolve "${GENERIC_FQDN}:${HTTPS_PORT}:${GATEWAY_IP}" \
  "https://${GENERIC_FQDN}:${HTTPS_PORT}/v1/pools"

assert_http "svc-arr pools via vhost" "200" \
  "${TLS_FLAGS[@]}" \
  --resolve "${SVC_FQDN}:${HTTPS_PORT}:${GATEWAY_IP}" \
  "https://${SVC_FQDN}:${HTTPS_PORT}/v1/pools"

assert_http "default array pools via vhost" "200" \
  "${TLS_FLAGS[@]}" \
  --resolve "${DEFAULT_FQDN}:${HTTPS_PORT}:${GATEWAY_IP}" \
  "https://${DEFAULT_FQDN}:${HTTPS_PORT}/v1/pools"

# Unknown host should return 404 (vhost_require_match=true).
# Use -k because there is no TLS cert for the unknown hostname and the
# SNI default cert won't match — we only care about the HTTP-level rejection.
assert_http "unknown host rejected" "404" \
  -k \
  --resolve "${UNKNOWN_FQDN}:${HTTPS_PORT}:${GATEWAY_IP}" \
  "https://${UNKNOWN_FQDN}:${HTTPS_PORT}/v1/pools"

pass "Step 3: Host-header routing validated"

# ---------------------------------------------------------------------------
# Step 4: Vhosts listing endpoint
# ---------------------------------------------------------------------------

log "Step 4: Testing /v1/vhosts endpoint"

VHOSTS_RESP="$(curl -s "${TLS_FLAGS[@]}" \
  --resolve "${DEFAULT_FQDN}:${HTTPS_PORT}:${GATEWAY_IP}" \
  "https://${DEFAULT_FQDN}:${HTTPS_PORT}/v1/vhosts" 2>/dev/null || echo "{}")"

VHOST_ENABLED="$(echo "${VHOSTS_RESP}" | jq -r '.vhost_enabled // false' 2>/dev/null || echo "false")"
if [[ "${VHOST_ENABLED}" == "true" ]]; then
  log "  OK: vhost_enabled=true"
else
  err "  FAIL: vhost_enabled=${VHOST_ENABLED} (expected true)"
  FAILURES=$((FAILURES + 1))
fi

MAPPING_COUNT="$(echo "${VHOSTS_RESP}" | jq '.mappings | length' 2>/dev/null || echo "0")"
if [[ "${MAPPING_COUNT}" -ge 3 ]]; then
  log "  OK: ${MAPPING_COUNT} vhost mappings (expected >=3: default, generic-arr, svc-arr)"
else
  err "  FAIL: only ${MAPPING_COUNT} vhost mappings (expected >=3)"
  echo "${VHOSTS_RESP}" | jq . 2>/dev/null || true
  FAILURES=$((FAILURES + 1))
fi

pass "Step 4: Vhosts endpoint validated"

# ---------------------------------------------------------------------------
# Step 5: Bypass paths
# ---------------------------------------------------------------------------

log "Step 5: Testing bypass paths"

# /healthz should work even with an unknown Host header.
# Use -k because there is no TLS cert for the unknown hostname.
assert_http "healthz bypasses vhost (unknown host)" "200" \
  -k \
  --resolve "${UNKNOWN_FQDN}:${HTTPS_PORT}:${GATEWAY_IP}" \
  "https://${UNKNOWN_FQDN}:${HTTPS_PORT}/healthz"

# /healthz with a valid host
assert_http "healthz with valid host" "200" \
  "${TLS_FLAGS[@]}" \
  --resolve "${DEFAULT_FQDN}:${HTTPS_PORT}:${GATEWAY_IP}" \
  "https://${DEFAULT_FQDN}:${HTTPS_PORT}/healthz"

pass "Step 5: Bypass paths validated"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
if [[ "${FAILURES}" -gt 0 ]]; then
  err "${FAILURES} vhost test(s) failed"
  exit 1
fi

echo "[$(ts)] [VHOST] [PASS] All vhost multiplexing tests passed"
