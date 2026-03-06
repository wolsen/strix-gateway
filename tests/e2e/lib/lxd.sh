#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# LXD VM lifecycle helpers for E2E tests.
# Sourced by other scripts — not executed directly.

# Requires: common.sh sourced first

LXD_IMAGE="${LXD_IMAGE:-ubuntu:24.04}"
VM_MEMORY="${VM_MEMORY:-4GiB}"
VM_CPUS="${VM_CPUS:-2}"

# ---------------------------------------------------------------------------
# VM management
# ---------------------------------------------------------------------------

create_vm() {
  local name="$1"
  local image="${2:-${LXD_IMAGE}}"
  local cpus="${3:-${VM_CPUS}}"
  local memory="${4:-${VM_MEMORY}}"

  log_info "Creating VM '${name}' (${image}, ${cpus} CPUs, ${memory} RAM)"
  lxc launch "${image}" "${name}" --vm \
    -c security.secureboot=false \
    -c limits.memory="${memory}" \
    -c limits.cpu="${cpus}"
}

vm_exists() {
  lxc info "$1" >/dev/null 2>&1
}

wait_vm_ready() {
  local name="$1" timeout_sec="${2:-240}"
  local start_ts now_ts
  start_ts="$(date +%s)"
  log_info "Waiting for VM agent in '${name}'"
  while true; do
    if lxc exec "${name}" -- true >/dev/null 2>&1; then
      log_info "VM agent ready in '${name}', waiting for cloud-init"
      lxc exec "${name}" -- cloud-init status --wait >/dev/null 2>&1
      log_info "VM '${name}' fully ready"
      return 0
    fi
    now_ts="$(date +%s)"
    if (( now_ts - start_ts > timeout_sec )); then
      log_error "Timed out waiting for VM agent in '${name}' (${timeout_sec}s)"
      return 1
    fi
    sleep 2
  done
}

destroy_vm() {
  local name="$1"
  if vm_exists "${name}"; then
    log_info "Destroying VM '${name}'"
    lxc delete -f "${name}" >/dev/null 2>&1 || true
  fi
}

get_vm_ip() {
  local name="$1"
  local ip
  ip="$(lxc exec "${name}" -- bash -lc \
    "ip -4 -o addr show dev enp5s0 2>/dev/null | awk '{print \$4}' | cut -d/ -f1 | head -n1")"
  if [[ -z "${ip}" ]]; then
    ip="$(lxc exec "${name}" -- bash -lc \
      "ip -4 -o addr show scope global | awk '{print \$4}' | cut -d/ -f1 | head -n1")"
  fi
  echo "${ip}"
}

# ---------------------------------------------------------------------------
# File push / repo packaging
# ---------------------------------------------------------------------------

push_repos() {
  local vm_name="$1"
  local gateway_root="$2"
  local fc_root="${3:-}"

  local gw_archive="/tmp/e2e-apollo-gateway.tgz"
  local fc_archive="/tmp/e2e-apollo-fc.tgz"

  log_info "Packaging apollo-gateway for '${vm_name}'"
  tar -C "$(dirname "${gateway_root}")" \
    --exclude='apollo-gateway/.venv' \
    --exclude='apollo-gateway/.git' \
    --exclude='apollo-gateway/**/__pycache__' \
    --exclude='apollo-gateway/.pytest_cache' \
    --exclude='apollo-gateway/build' \
    --exclude='apollo-gateway/dist' \
    -czf "${gw_archive}" "$(basename "${gateway_root}")"

  lxc file push "${gw_archive}" "${vm_name}/root/apollo-gateway.tgz"
  lxc exec "${vm_name}" -- bash -lc "cd /root && tar -xzf apollo-gateway.tgz"

  if [[ -n "${fc_root}" && -d "${fc_root}" ]]; then
    log_info "Packaging apollo-fc for '${vm_name}'"
    tar -C "$(dirname "${fc_root}")" \
      --exclude='apollo-fc/.venv' \
      --exclude='apollo-fc/.git' \
      --exclude='apollo-fc/**/__pycache__' \
      --exclude='apollo-fc/.pytest_cache' \
      --exclude='apollo-fc/build' \
      --exclude='apollo-fc/dist' \
      -czf "${fc_archive}" "$(basename "${fc_root}")"

    lxc file push "${fc_archive}" "${vm_name}/root/apollo-fc.tgz"
    lxc exec "${vm_name}" -- bash -lc "cd /root && tar -xzf apollo-fc.tgz"
    rm -f "${fc_archive}"
  fi

  rm -f "${gw_archive}"
}

push_file() {
  local vm_name="$1" local_path="$2" remote_path="$3"
  lxc file push "${local_path}" "${vm_name}${remote_path}"
}

vm_exec() {
  local vm_name="$1"; shift
  lxc exec "${vm_name}" -- "$@"
}

vm_exec_script() {
  local vm_name="$1" script_path="$2"; shift 2
  local remote_script="/root/$(basename "${script_path}")"
  push_file "${vm_name}" "${script_path}" "${remote_script}"
  vm_exec "${vm_name}" chmod +x "${remote_script}"
  vm_exec "${vm_name}" env "$@" bash "${remote_script}"
}
