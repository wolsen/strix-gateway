#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
#
# Strix Gateway installation + fake SPDK + SSH facade helpers.
# Sourced by gateway_setup.sh — runs INSIDE the gateway VM.
#
# Requires: common.sh sourced first

GATEWAY_PORT="${GATEWAY_PORT:-8080}"
SPDK_SOCK="${SPDK_SOCK:-/var/tmp/spdk.sock}"
SVC_PASSWORD="${SVC_PASSWORD:-strix_svc_pass}"
SVC_USER="${SVC_USER:-svc}"

# ---------------------------------------------------------------------------
# Fake SPDK JSON-RPC socket
# ---------------------------------------------------------------------------

install_fake_spdk() {
  log_info "Installing fake SPDK JSON-RPC socket at ${SPDK_SOCK}"
  cat > /usr/local/bin/strix-fake-spdk.py <<'PYEOF'
#!/usr/bin/env python3
"""Fake SPDK JSON-RPC socket for E2E testing.

Implements the minimum set of RPCs needed by Strix Gateway.
All state lives in-memory — reset by restarting the process.
"""
import json
import os
import socket
import threading

SOCK = os.environ.get("SPDK_SOCK", "/var/tmp/spdk.sock")

state = {
    "bdevs": {},
    "lvstores": set(),
    "portal_groups": [],
    "initiator_groups": [],
    "targets": {},
    "nvmf_transports": [],
    "nvmf_subsystems": {},
}

lock = threading.Lock()


def _ok(result, req_id):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(code, message, req_id):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _bdev_list(name=None):
    if name:
        bdev = state["bdevs"].get(name)
        return [bdev] if bdev else []
    return list(state["bdevs"].values())


def handle(method, params):
    if method == "bdev_get_bdevs":
        return _bdev_list(params.get("name") if params else None)

    if method == "bdev_malloc_create":
        name = params["name"]
        with lock:
            state["bdevs"][name] = {
                "name": name,
                "num_blocks": int(params.get("num_blocks", 0)),
                "block_size": int(params.get("block_size", 512)),
            }
        return name

    if method == "bdev_aio_create":
        name = params["name"]
        filename = params["filename"]
        block_size = int(params.get("block_size", 512))
        size = os.path.getsize(filename) if os.path.exists(filename) else 0
        num_blocks = size // block_size if block_size else 0
        with lock:
            state["bdevs"][name] = {
                "name": name, "filename": filename,
                "num_blocks": num_blocks, "block_size": block_size,
            }
        return name

    if method == "bdev_lvol_get_lvstores":
        lvs_name = params.get("lvs_name") if params else None
        if lvs_name:
            return [{"name": lvs_name}] if lvs_name in state["lvstores"] else []
        return [{"name": n} for n in sorted(state["lvstores"])]

    if method == "bdev_lvol_create_lvstore":
        lvs_name = params["lvs_name"]
        with lock:
            state["lvstores"].add(lvs_name)
        return lvs_name

    if method == "bdev_lvol_create":
        full_name = f"{params['lvs_name']}/{params['lvol_name']}"
        with lock:
            state["bdevs"][full_name] = {
                "name": full_name,
                "num_blocks": int(params.get("size_in_mib", 0)) * 2048,
                "block_size": 512,
            }
        return full_name

    if method == "bdev_lvol_delete":
        state["bdevs"].pop(params.get("name"), None)
        return True

    if method == "bdev_lvol_resize":
        return True

    if method == "iscsi_get_portal_groups":
        return state["portal_groups"]

    if method == "iscsi_create_portal_group":
        with lock:
            state["portal_groups"].append(
                {"tag": params["tag"], "portals": params.get("portals", [])}
            )
        return True

    if method == "iscsi_get_initiator_groups":
        return state["initiator_groups"]

    if method == "iscsi_create_initiator_group":
        with lock:
            state["initiator_groups"].append({
                "tag": params["tag"],
                "initiators": params.get("initiators", []),
                "netmasks": params.get("netmasks", []),
            })
        return True

    if method == "iscsi_get_target_nodes":
        return list(state["targets"].values())

    if method == "iscsi_create_target_node":
        name = params["name"]
        with lock:
            state["targets"][name] = {
                "name": name,
                "luns": [
                    {"lun_id": int(l.get("lun_id", 0)), "bdev_name": l.get("bdev_name")}
                    for l in params.get("luns", [])
                ],
            }
        return True

    if method == "iscsi_target_node_add_lun":
        name = params["name"]
        target = state["targets"].setdefault(name, {"name": name, "luns": []})
        target["luns"].append(
            {"lun_id": int(params.get("lun_id", 0)), "bdev_name": params.get("bdev_name")}
        )
        return True

    if method == "iscsi_delete_target_node":
        state["targets"].pop(params.get("name", ""), None)
        return True

    if method == "nvmf_get_transports":
        return state["nvmf_transports"]

    if method == "nvmf_create_transport":
        state["nvmf_transports"].append(params or {})
        return True

    if method == "nvmf_get_subsystems":
        nqn = params.get("nqn") if params else None
        if nqn:
            ss = state["nvmf_subsystems"].get(nqn)
            return [ss] if ss else []
        return list(state["nvmf_subsystems"].values())

    if method == "nvmf_create_subsystem":
        nqn = params["nqn"]
        state["nvmf_subsystems"][nqn] = {"nqn": nqn, "listen_addresses": [], "namespaces": []}
        return True

    if method == "nvmf_subsystem_add_listener":
        nqn = params["nqn"]
        ss = state["nvmf_subsystems"].setdefault(
            nqn, {"nqn": nqn, "listen_addresses": [], "namespaces": []}
        )
        ss["listen_addresses"].append(params.get("listen_address", {}))
        return True

    if method == "nvmf_subsystem_add_ns":
        nqn = params["nqn"]
        ss = state["nvmf_subsystems"].setdefault(
            nqn, {"nqn": nqn, "listen_addresses": [], "namespaces": []}
        )
        nsid = int(params.get("nsid", 1))
        ss["namespaces"].append({"nsid": nsid, "bdev_name": params.get("namespace", {}).get("bdev_name")})
        return nsid

    if method == "nvmf_subsystem_remove_ns":
        nqn = params["nqn"]
        nsid = int(params.get("nsid", 0))
        ss = state["nvmf_subsystems"].get(nqn)
        if ss:
            ss["namespaces"] = [ns for ns in ss.get("namespaces", []) if int(ns.get("nsid", -1)) != nsid]
        return True

    return True


def serve():
    if os.path.exists(SOCK):
        os.unlink(SOCK)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK)
    os.chmod(SOCK, 0o777)
    server.listen(64)

    while True:
        conn, _ = server.accept()
        with conn:
            raw = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw += chunk
                try:
                    req = json.loads(raw.decode())
                    break
                except json.JSONDecodeError:
                    continue
            if not raw:
                continue

            req_id = req.get("id")
            method = req.get("method", "")
            params = req.get("params", {})

            try:
                result = handle(method, params)
                resp = _ok(result, req_id)
            except Exception as exc:
                resp = _err(-32000, str(exc), req_id)

            conn.sendall(json.dumps(resp).encode())


if __name__ == "__main__":
    serve()
PYEOF
  chmod +x /usr/local/bin/strix-fake-spdk.py
}

start_fake_spdk() {
  log_info "Starting fake SPDK socket"
  pkill -f 'strix-fake-spdk.py' >/dev/null 2>&1 || true
  sleep 0.5
  rm -f "${SPDK_SOCK}"
  SPDK_SOCK="${SPDK_SOCK}" nohup python3 /usr/local/bin/strix-fake-spdk.py \
    > /var/log/strix-fake-spdk.log 2>&1 &
  sleep 1
  if [[ ! -S "${SPDK_SOCK}" ]]; then
    log_error "Fake SPDK socket not created at ${SPDK_SOCK}"
    cat /var/log/strix-fake-spdk.log
    return 1
  fi
  log_info "Fake SPDK socket ready"
}

# ---------------------------------------------------------------------------
# iSCSI target (for underlay)
# ---------------------------------------------------------------------------

setup_iscsi_target() {
  local target_iqn="${1:-iqn.2026-03.com.lunacy:strix.e2e.target}"
  local target_port="${2:-3260}"
  local lun_size_mb="${3:-512}"
  local backing_file="/var/lib/strix-e2e/target-lun.img"

  log_info "Setting up iSCSI target: iqn=${target_iqn} port=${target_port}"
  apt-get install -y -qq targetcli-fb >/dev/null 2>&1

  modprobe target_core_mod
  modprobe iscsi_target_mod

  mkdir -p /var/lib/strix-e2e
  truncate -s "${lun_size_mb}M" "${backing_file}"

  targetcli clearconfig confirm=True >/dev/null 2>&1
  targetcli /backstores/fileio create e2e_lun "${backing_file}" "${lun_size_mb}M" write_back=false
  targetcli /iscsi create "${target_iqn}"
  targetcli "/iscsi/${target_iqn}/tpg1/portals" create 0.0.0.0 "${target_port}" >/dev/null 2>&1 || true
  targetcli "/iscsi/${target_iqn}/tpg1/luns" create /backstores/fileio/e2e_lun 0
  targetcli "/iscsi/${target_iqn}/tpg1" set attribute \
    authentication=0 demo_mode_write_protect=0 \
    generate_node_acls=1 demo_mode_discovery=1
  targetcli saveconfig

  log_info "iSCSI target ready"
}

# ---------------------------------------------------------------------------
# Strix Gateway
# ---------------------------------------------------------------------------

install_gateway() {
  local gateway_root="${1:-/root/strix-gateway}"
    log_info "Installing Strix Gateway from ${gateway_root}"

    ensure_uv

  cd "${gateway_root}"
  rm -f strix_gateway.db

    uv venv --clear .venv >/dev/null 2>&1
    uv pip install --python .venv/bin/python -e . >/dev/null 2>&1

    log_info "Strix Gateway installed"
}

start_gateway() {
  local gateway_root="${1:-/root/strix-gateway}"
  local mode="${2:-non-vhost}"
  local vhost_domain="${3:-e2e.test}"
  local portal_ip="${4:-}"

    log_info "Starting Strix Gateway (mode=${mode}) on port ${GATEWAY_PORT}"

  cd "${gateway_root}"

  # Kill existing gateway
    pkill -f '[u]vicorn strix_gateway.main:app' >/dev/null 2>&1 || true
  sleep 1

  # Build env
  local gw_env=(
    "STRIX_SPDK_SOCKET_PATH=${SPDK_SOCK}"
    "STRIX_DATABASE_URL=sqlite+aiosqlite:///./strix_gateway.db"
    "STRIX_ISCSI_UNDERLAY_LUN_BASE=1"
  )

  if [[ -n "${portal_ip}" ]]; then
    gw_env+=("STRIX_ISCSI_PORTAL_IP=${portal_ip}")
  fi

  if [[ "${mode}" == "vhost" ]]; then
    gw_env+=(
      "STRIX_VHOST_ENABLED=true"
      "STRIX_VHOST_DOMAIN=${vhost_domain}"
    )
  fi

  nohup env "${gw_env[@]}" .venv/bin/uvicorn strix_gateway.main:app \
    --host 0.0.0.0 --port "${GATEWAY_PORT}" \
    --log-level info \
    > /var/log/strix-gateway.log 2>&1 &

  wait_for_http "http://127.0.0.1:${GATEWAY_PORT}/healthz" 60 "Gateway"
  log_info "Gateway ready"
}

reset_gateway() {
  local gateway_root="${1:-/root/strix-gateway}"
  log_info "Resetting Gateway state"
    pkill -f '[u]vicorn strix_gateway.main:app' >/dev/null 2>&1 || true
  sleep 1
  rm -f "${gateway_root}/strix_gateway.db"
}

apply_topology() {
  local gateway_root="${1:-/root/strix-gateway}"
  local topo_file="$2"

  log_info "Applying topology from ${topo_file}"
  cd "${gateway_root}"
    .venv/bin/strix --url "http://127.0.0.1:${GATEWAY_PORT}" apply -f "${topo_file}"
}

# ---------------------------------------------------------------------------
# SSH Facade for IBM SVC compatibility (Cinder connects here)
# ---------------------------------------------------------------------------

setup_ssh_facade() {
  local svc_user="${SVC_USER}"
  local svc_pass="${SVC_PASSWORD}"
  local subsystem="${2:-default}"

  log_info "Setting up SSH facade for IBM SVC compatibility"
  apt-get install -y -qq openssh-server >/dev/null 2>&1

  # Create svc user if it doesn't exist
  if ! id "${svc_user}" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "${svc_user}"
  fi
  echo "${svc_user}:${svc_pass}" | chpasswd

  # Determine path to the shell module
  local gateway_root="${1:-/root/strix-gateway}"
    local shell_script="${gateway_root}/.venv/bin/python -m strix_gateway.personalities.svc.shell --subsystem ${subsystem}"

    # E2E runs gateway from /root; allow the unprivileged svc user to traverse
    # the path so ForceCommand can execute the shell module.
    chmod o+rx /root "${gateway_root}" "${gateway_root}/.venv" "${gateway_root}/.venv/bin" 2>/dev/null || true

  # Write sshd_config for SVC facade
  # ForceCommand ensures any SSH connection as 'svc' runs through the shell
  cat > /etc/ssh/sshd_config.d/strix-svc.conf <<EOF
Match User ${svc_user}
    ForceCommand ${shell_script}
    PasswordAuthentication yes
    PubkeyAuthentication yes
    PermitTTY no
    X11Forwarding no
    AllowTcpForwarding no
EOF

  # Ensure PasswordAuthentication is possible
  sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || true

  # Ensure sshd is running
  systemctl restart ssh 2>/dev/null || service ssh restart 2>/dev/null || /usr/sbin/sshd 2>/dev/null || true

  log_info "SSH facade ready (user=${svc_user})"
}
