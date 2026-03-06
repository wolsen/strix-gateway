#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# Consumer VM helpers — os-brick, iSCSI initiator, apollo-fc modules.
# Sourced by consumer_setup.sh — runs INSIDE the consumer VM.
#
# Requires: common.sh sourced first

ENABLE_FC="${ENABLE_FC:-false}"

# ---------------------------------------------------------------------------
# Base setup
# ---------------------------------------------------------------------------

install_consumer_deps() {
  log_step "Installing consumer dependencies"

  apt-get update -qq
  apt-get install -y -qq \
    python3-dev python3-pip python3-venv \
    open-iscsi \
    libffi-dev libssl-dev \
    jq curl >/dev/null 2>&1

  # iSCSI initiator daemon
  systemctl enable --now iscsid 2>/dev/null || true

  if [[ ! -d /opt/consumer/.venv ]]; then
    python3 -m venv /opt/consumer/.venv
    /opt/consumer/.venv/bin/pip install --upgrade pip setuptools wheel >/dev/null
  fi

  /opt/consumer/.venv/bin/pip install \
    os-brick \
    python-openstackclient \
    python-cinderclient >/dev/null 2>&1

  log_info "Consumer base deps installed"
}

# ---------------------------------------------------------------------------
# FC kernel modules (optional)
# ---------------------------------------------------------------------------

install_fc_modules() {
  local fc_root="${1:-/root/apollo-fc}"

  if [[ "${ENABLE_FC}" != "true" ]]; then
    log_info "Skipping FC module build (ENABLE_FC != true)"
    return 0
  fi

  log_step "Building FC kernel modules"

  apt-get install -y -qq \
    build-essential \
    "linux-headers-$(uname -r)" >/dev/null 2>&1

  cd "${fc_root}/src/apollo_fc"
  make clean >/dev/null 2>&1 || true
  make >/dev/null 2>&1

  cd "${fc_root}/src/dm_apollo_fc"
  make clean >/dev/null 2>&1 || true
  make >/dev/null 2>&1

  log_info "Loading apollo_fc + dm_apollo_fc"
  insmod "${fc_root}/src/apollo_fc/apollo_fc.ko"
  insmod "${fc_root}/src/dm_apollo_fc/dm_apollo_fc.ko"

  log_info "Installing apollo-fcctl"
  cd "${fc_root}/userspace"
  /opt/consumer/.venv/bin/pip install -e . >/dev/null 2>&1

  log_info "FC modules loaded and apollo-fcctl installed"
}

unload_fc_modules() {
  local fc_root="${1:-/root/apollo-fc}"
  log_info "Unloading FC modules"
  rmmod dm_apollo_fc 2>/dev/null || true
  rmmod apollo_fc 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Initiator identities
# ---------------------------------------------------------------------------

get_iscsi_iqn() {
  cat /etc/iscsi/initiatorname.iscsi 2>/dev/null | grep -oP 'InitiatorName=\K.*' || echo ""
}

get_fc_wwpns() {
  # From apollo_fc virtual HBA sysfs
  local host_num
  host_num="$(get_fc_host_num)"
  if [[ -n "${host_num}" ]]; then
    cat "/sys/class/fc_host/host${host_num}/port_name" 2>/dev/null || echo ""
  fi
}

get_fc_host_num() {
  for host_dir in /sys/class/scsi_host/host*; do
    if [[ -f "${host_dir}/proc_name" ]]; then
      local proc_name
      proc_name="$(cat "${host_dir}/proc_name")"
      if [[ "${proc_name}" == "apollo_fc" ]]; then
        basename "${host_dir}" | sed 's/host//'
        return
      fi
    fi
  done
}

# ---------------------------------------------------------------------------
# os-brick connect/disconnect (Python helper)
# ---------------------------------------------------------------------------

osbrick_connect_iscsi() {
  local target_portal="$1"
  local target_iqn="$2"
  local target_lun="${3:-0}"

  /opt/consumer/.venv/bin/python3 <<PYEOF
import json, sys
from os_brick.initiator import connector as brick_connector

conn_props = {
    "driver_volume_type": "iscsi",
    "data": {
        "target_portal": "${target_portal}",
        "target_iqn": "${target_iqn}",
        "target_lun": ${target_lun},
        "volume_id": "e2e-test-vol",
    },
}
initiator = brick_connector.InitiatorConnector.factory("ISCSI", None, use_multipath=False)
device_info = initiator.connect_volume(conn_props["data"])
print(json.dumps(device_info))
PYEOF
}

osbrick_disconnect_iscsi() {
  local target_portal="$1"
  local target_iqn="$2"
  local target_lun="${3:-0}"

  /opt/consumer/.venv/bin/python3 <<PYEOF
from os_brick.initiator import connector as brick_connector

conn_props = {
    "target_portal": "${target_portal}",
    "target_iqn": "${target_iqn}",
    "target_lun": ${target_lun},
    "volume_id": "e2e-test-vol",
}
initiator = brick_connector.InitiatorConnector.factory("ISCSI", None, use_multipath=False)
try:
    initiator.disconnect_volume(conn_props, None)
except Exception as e:
    print(f"Disconnect warning: {e}")
PYEOF
}

osbrick_connect_fc() {
  local target_wwpn="$1"
  local target_lun="${2:-0}"

  /opt/consumer/.venv/bin/python3 <<PYEOF
import json, sys
from os_brick.initiator import connector as brick_connector

conn_props = {
    "driver_volume_type": "fibre_channel",
    "data": {
        "target_discovered": True,
        "target_wwn": ["${target_wwpn}"],
        "target_lun": ${target_lun},
        "target_wwns": ["${target_wwpn}"],
        "target_luns": [${target_lun}],
        "targets": [("${target_wwpn}", ${target_lun})],
        "volume_id": "e2e-test-vol",
        "access_mode": "rw",
    },
}
initiator = brick_connector.InitiatorConnector.factory("FIBRE_CHANNEL", None, use_multipath=False)
device_info = initiator.connect_volume(conn_props["data"])
print(json.dumps(device_info))
PYEOF
}

osbrick_disconnect_fc() {
  local target_wwpn="$1"
  local target_lun="${2:-0}"

  /opt/consumer/.venv/bin/python3 <<PYEOF
from os_brick.initiator import connector as brick_connector

conn_props = {
    "target_discovered": True,
    "target_wwn": ["${target_wwpn}"],
    "target_lun": ${target_lun},
    "target_wwns": ["${target_wwpn}"],
    "target_luns": [${target_lun}],
    "targets": [("${target_wwpn}", ${target_lun})],
    "volume_id": "e2e-test-vol",
    "access_mode": "rw",
}
initiator = brick_connector.InitiatorConnector.factory("FIBRE_CHANNEL", None, use_multipath=False)
try:
    initiator.disconnect_volume(conn_props, None)
except Exception as e:
    print(f"Disconnect warning: {e}")
PYEOF
}
