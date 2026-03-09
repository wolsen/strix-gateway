#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# Consumer VM one-time setup.
# Runs INSIDE the consumer VM. Installs:
#   1. iSCSI initiator + os-brick
#   2. OpenStack CLI + Cinder client
#   3. FC kernel modules + strix-fcctl (if ENABLE_FC=true)
#
# After this script completes, the consumer VM is ready to run scenarios.
#
# Environment variables:
#   ENABLE_FC  (default: false)
set -euo pipefail

source /root/e2e-lib/common.sh
source /root/e2e-lib/consumer.sh

export DEBIAN_FRONTEND=noninteractive
ENABLE_FC="${ENABLE_FC:-false}"

trap 'log_error "Consumer setup FAILED at line $LINENO"; exit 1' ERR

# ---------------------------------------------------------------------------
log_step "Phase 1: Base dependencies"
# ---------------------------------------------------------------------------
install_consumer_deps

# ---------------------------------------------------------------------------
log_step "Phase 2: FC modules (if enabled)"
# ---------------------------------------------------------------------------
if [[ "${ENABLE_FC}" == "true" ]]; then
  install_fc_modules /root/strix-fc
fi

# ---------------------------------------------------------------------------
log_step "Phase 3: Record initiator identities"
# ---------------------------------------------------------------------------
ISCSI_IQN="$(get_iscsi_iqn)"
log_info "iSCSI IQN: ${ISCSI_IQN}"
echo "${ISCSI_IQN}" > /root/iscsi_iqn

if [[ "${ENABLE_FC}" == "true" ]]; then
  FC_WWPNS="$(get_fc_wwpns)"
  FC_HOST_NUM="$(get_fc_host_num)"
  log_info "FC WWPNs: ${FC_WWPNS}"
  log_info "FC host number: ${FC_HOST_NUM}"
  echo "${FC_WWPNS}" > /root/fc_wwpns
  echo "${FC_HOST_NUM}" > /root/fc_host_num
fi

log_step "Consumer VM setup complete"
