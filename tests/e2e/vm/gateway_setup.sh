#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# Gateway VM one-time setup.
# Runs INSIDE the gateway VM. Installs:
#   1. Fake SPDK socket
#   2. iSCSI target (underlay)
#   3. Apollo Gateway
#   4. SSH facade for SVC compatibility
#   5. Keystone (SQLite)
#   6. Cinder (SQLite + RabbitMQ)
#
# After this script completes, the gateway VM is ready to run scenarios.
# The scenario runner will call configure/start functions for each test.
#
# Environment variables:
#   GATEWAY_PORT         (default: 8080)
#   SVC_PASSWORD         (default: apollo_svc_pass)
#   OS_PASSWORD          (default: admin)
#   TARGET_IQN           (default: iqn.2026-03.com.lunacy:apollo.e2e.target)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source libraries (pushed into VM at /root/e2e-lib/)
source /root/e2e-lib/common.sh
source /root/e2e-lib/gateway.sh
source /root/e2e-lib/openstack.sh

export DEBIAN_FRONTEND=noninteractive

GATEWAY_PORT="${GATEWAY_PORT:-8080}"
SVC_PASSWORD="${SVC_PASSWORD:-apollo_svc_pass}"
TARGET_IQN="${TARGET_IQN:-iqn.2026-03.com.lunacy:apollo.e2e.target}"

trap 'log_error "Gateway setup FAILED at line $LINENO"; exit 1' ERR

# ---------------------------------------------------------------------------
log_step "Phase 1: System packages"
# ---------------------------------------------------------------------------
apt-get update -qq
apt-get install -y -qq \
  python3 python3-pip python3-venv python3-dev \
  libffi-dev libssl-dev pkg-config \
  curl jq openssh-server targetcli-fb \
  rabbitmq-server >/dev/null 2>&1

# Ensure RabbitMQ is running
systemctl enable --now rabbitmq-server 2>/dev/null || true

# ---------------------------------------------------------------------------
log_step "Phase 2: Fake SPDK + iSCSI target"
# ---------------------------------------------------------------------------
install_fake_spdk
start_fake_spdk
setup_iscsi_target "${TARGET_IQN}" 3260 512

# ---------------------------------------------------------------------------
log_step "Phase 3: Apollo Gateway"
# ---------------------------------------------------------------------------
install_gateway /root/apollo-gateway
setup_ssh_facade /root/apollo-gateway

# ---------------------------------------------------------------------------
log_step "Phase 4: Keystone"
# ---------------------------------------------------------------------------
install_keystone

# ---------------------------------------------------------------------------
log_step "Phase 5: Cinder"
# ---------------------------------------------------------------------------
register_cinder_service
install_cinder

# Write openrc for consumer to use
write_openrc /root/openrc

log_step "Gateway VM setup complete"
log_info "Services installed: fake-spdk, iscsi-target, gateway, ssh-facade, keystone, cinder"
log_info "Scenarios will configure + start gateway and cinder per-test"
