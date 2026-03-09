#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# Keystone + Cinder minimal installation helpers.
# Sourced by gateway_setup.sh — runs INSIDE the gateway VM.
#
# Installs Keystone (SQLite, fernet tokens) and Cinder (SQLite, RabbitMQ)
# from PyPI with the absolute minimum configuration required for e2e tests.

# Requires: common.sh sourced first

KEYSTONE_CONF="/etc/keystone/keystone.conf"
KEYSTONE_DB="/var/lib/keystone/keystone.db"
KEYSTONE_PORT="${KEYSTONE_PORT:-5000}"

CINDER_CONF="/etc/cinder/cinder.conf"
CINDER_DB="/var/lib/cinder/cinder.db"
CINDER_PORT="${CINDER_PORT:-8776}"

OS_PASSWORD="${OS_PASSWORD:-admin}"
OS_REGION="${OS_REGION:-RegionOne}"

# ---------------------------------------------------------------------------
# Keystone
# ---------------------------------------------------------------------------

install_keystone() {
  log_step "Installing Keystone"

  apt-get update -qq
  apt-get install -y -qq \
     python3-dev python3-venv \
    libffi-dev libssl-dev pkg-config \
    rabbitmq-server >/dev/null 2>&1

  ensure_uv

  # Use a shared venv for all OpenStack services
  if [[ ! -d /opt/openstack/.venv ]]; then
     uv venv /opt/openstack/.venv >/dev/null 2>&1
  fi

    uv pip install --python /opt/openstack/.venv/bin/python \
    'cryptography<46' \
    'pyOpenSSL>=24.2.1' \
    keystone \
    python-openstackclient \
    uwsgi \
    PyMySQL >/dev/null 2>&1

  mkdir -p /etc/keystone /var/lib/keystone /var/log/keystone

  cat > "${KEYSTONE_CONF}" <<EOF
[DEFAULT]
log_file = /var/log/keystone/keystone.log
use_stderr = false

[database]
connection = sqlite:///${KEYSTONE_DB}?timeout=60

[token]
provider = fernet

[cache]
enabled = false
EOF

  log_info "Running keystone-manage db_sync"
  /opt/openstack/.venv/bin/keystone-manage db_sync

  /opt/openstack/.venv/bin/keystone-manage fernet_setup \
    --keystone-user root --keystone-group root
  /opt/openstack/.venv/bin/keystone-manage credential_setup \
    --keystone-user root --keystone-group root

  log_info "Bootstrapping Keystone"
  /opt/openstack/.venv/bin/keystone-manage bootstrap \
    --bootstrap-password "${OS_PASSWORD}" \
    --bootstrap-admin-url "http://127.0.0.1:${KEYSTONE_PORT}/v3/" \
    --bootstrap-internal-url "http://127.0.0.1:${KEYSTONE_PORT}/v3/" \
    --bootstrap-public-url "http://127.0.0.1:${KEYSTONE_PORT}/v3/" \
    --bootstrap-region-id "${OS_REGION}"

  log_info "Starting Keystone via uwsgi on port ${KEYSTONE_PORT}"
  local wsgi_wrapper="/opt/openstack/keystone-public.wsgi"
  cat > "${wsgi_wrapper}" <<'EOF'
from keystone.server.wsgi import initialize_public_application

application = initialize_public_application()
EOF

  nohup /opt/openstack/.venv/bin/uwsgi \
    --http "0.0.0.0:${KEYSTONE_PORT}" \
    --wsgi-file "${wsgi_wrapper}" \
    --processes 1 \
    --threads 1 \
    --master \
    --die-on-term \
    > /var/log/keystone/uwsgi.log 2>&1 &

  wait_for_http "http://127.0.0.1:${KEYSTONE_PORT}/v3/" 120 "Keystone"
  log_info "Keystone ready on port ${KEYSTONE_PORT}"
}

# ---------------------------------------------------------------------------
# openrc export (for CLI commands)
# ---------------------------------------------------------------------------

export_openrc() {
  local keystone_url="${1:-http://127.0.0.1:${KEYSTONE_PORT}/v3}"
  export OS_AUTH_URL="${keystone_url}"
  export OS_PROJECT_NAME="admin"
  export OS_USERNAME="admin"
  export OS_PASSWORD="${OS_PASSWORD}"
  export OS_USER_DOMAIN_ID="default"
  export OS_PROJECT_DOMAIN_ID="default"
  export OS_IDENTITY_API_VERSION=3
  export OS_REGION_NAME="${OS_REGION}"
}

write_openrc() {
  local target="${1:-/root/openrc}"
  local keystone_url="${2:-http://127.0.0.1:${KEYSTONE_PORT}/v3}"
  cat > "${target}" <<EOF
export OS_AUTH_URL=${keystone_url}
export OS_PROJECT_NAME=admin
export OS_USERNAME=admin
export OS_PASSWORD=${OS_PASSWORD}
export OS_USER_DOMAIN_ID=default
export OS_PROJECT_DOMAIN_ID=default
export OS_IDENTITY_API_VERSION=3
export OS_REGION_NAME=${OS_REGION}
EOF
}

# ---------------------------------------------------------------------------
# Register Cinder service in Keystone
# ---------------------------------------------------------------------------

register_cinder_service() {
  local cinder_url="${1:-http://127.0.0.1:${CINDER_PORT}/v3}"
  log_info "Registering Cinder service + endpoints in Keystone"

  export_openrc
  local osc="/opt/openstack/.venv/bin/openstack"

  if ! ${osc} project show service >/dev/null 2>&1; then
    ${osc} project create --domain default service >/dev/null
  fi

  if ! ${osc} user show cinder >/dev/null 2>&1; then
    ${osc} user create --domain default --password "${OS_PASSWORD}" cinder >/dev/null
  fi

  if ! ${osc} service show cinderv3 >/dev/null 2>&1; then
    ${osc} service create --name cinderv3 --description "OpenStack Block Storage" volumev3 >/dev/null
  fi

  ${osc} role add --project service --user cinder admin >/dev/null 2>&1 || true

  for iface in public internal admin; do
    if ! ${osc} endpoint list --service cinderv3 --interface "${iface}" -f value -c ID | grep -q .; then
      ${osc} endpoint create --region "${OS_REGION}" volumev3 "${iface}" \
        "${cinder_url}/%(project_id)s" >/dev/null
    fi
  done

  log_info "Cinder service registered"
}

# ---------------------------------------------------------------------------
# Cinder
# ---------------------------------------------------------------------------

install_cinder() {
  log_step "Installing Cinder"

  ensure_uv

    uv pip install --python /opt/openstack/.venv/bin/python \
    cinder \
    python-cinderclient >/dev/null 2>&1

  mkdir -p /etc/cinder /var/lib/cinder /var/log/cinder

  log_info "Cinder packages installed"
}

configure_cinder_backend() {
  local backend_conf_file="$1"
  local gateway_ip="${2:-127.0.0.1}"
  local gateway_ssh_port="${3:-22}"
  local svc_password="${4:-strix_svc_pass}"

  log_info "Configuring Cinder with backend from ${backend_conf_file}"

  # Extract backend section name from the conf file (first [section] header)
  local backend_name
  backend_name="$(grep -m1 '^\[' "${backend_conf_file}" | tr -d '[]')"

  cat > "${CINDER_CONF}" <<EOF
[DEFAULT]
log_file = /var/log/cinder/cinder.log
use_stderr = false
transport_url = rabbit://guest:guest@127.0.0.1:5672/
enabled_backends = ${backend_name}
auth_strategy = keystone
state_path = /var/lib/cinder
my_ip = 127.0.0.1
api_paste_config = /opt/openstack/.venv/etc/cinder/api-paste.ini

[database]
connection = sqlite:///${CINDER_DB}

[keystone_authtoken]
www_authenticate_uri = http://127.0.0.1:${KEYSTONE_PORT}/v3
auth_url = http://127.0.0.1:${KEYSTONE_PORT}/v3
memcached_servers = localhost:11211
auth_type = password
project_domain_id = default
user_domain_id = default
project_name = service
username = cinder
password = ${OS_PASSWORD}

[oslo_concurrency]
lock_path = /var/lib/cinder/lock

EOF

  # Append the driver-specific backend section, substituting variables
  sed \
    -e "s|\${GATEWAY_IP}|${gateway_ip}|g" \
    -e "s|\${GATEWAY_SSH_PORT}|${gateway_ssh_port}|g" \
    -e "s|\${SVC_PASSWORD}|${svc_password}|g" \
    "${backend_conf_file}" >> "${CINDER_CONF}"

  log_info "Cinder configured with backend '${backend_name}'"
}

start_cinder() {
  log_info "Running cinder-manage db sync"
  /opt/openstack/.venv/bin/cinder-manage db sync >/dev/null 2>&1

  # Kill any previously running Cinder services.
  pkill -f '[c]inder-(all|api|scheduler|volume)' >/dev/null 2>&1 || true
  sleep 1

  if [[ -x /opt/openstack/.venv/bin/cinder-all ]]; then
    log_info "Starting cinder-all (background)"
    nohup /opt/openstack/.venv/bin/cinder-all \
      --config-file "${CINDER_CONF}" \
      > /var/log/cinder/cinder-all.log 2>&1 &
  else
    log_info "Starting cinder-api + cinder-scheduler + cinder-volume"
    nohup /opt/openstack/.venv/bin/cinder-api \
      --config-file "${CINDER_CONF}" \
      > /var/log/cinder/cinder-api.log 2>&1 &
    nohup /opt/openstack/.venv/bin/cinder-scheduler \
      --config-file "${CINDER_CONF}" \
      > /var/log/cinder/cinder-scheduler.log 2>&1 &
    nohup /opt/openstack/.venv/bin/cinder-volume \
      --config-file "${CINDER_CONF}" \
      > /var/log/cinder/cinder-volume.log 2>&1 &
  fi

  wait_for_port "127.0.0.1" "${CINDER_PORT}" 120 "Cinder API"
  log_info "Cinder API ready on port ${CINDER_PORT}"
}

restart_cinder() {
  pkill -f '[c]inder-(all|api|scheduler|volume)' >/dev/null 2>&1 || true
  sleep 2
  start_cinder
}

reset_cinder_state() {
  log_info "Resetting Cinder state (wipe DB + re-sync)"
  pkill -f '[c]inder-(all|api|scheduler|volume)' >/dev/null 2>&1 || true
  sleep 1
  rm -f "${CINDER_DB}"
  /opt/openstack/.venv/bin/cinder-manage db sync >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Volume type management
# ---------------------------------------------------------------------------

create_volume_type() {
  local type_name="$1"
  local backend_name="$2"
  export_openrc
  local osc="/opt/openstack/.venv/bin/openstack"

  log_info "Creating volume type '${type_name}' → backend '${backend_name}'"
  ${osc} volume type create "${type_name}" >/dev/null 2>&1 || true
  ${osc} volume type set "${type_name}" \
    --property volume_backend_name="${backend_name}" >/dev/null 2>&1
}
