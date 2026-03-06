# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Protocol-specific connection information builders.

Centralises the construction of canonical connection-info dicts that
compute-side agents consume.  Vendor personalities may reshape the output
if needed, but the canonical builders handle the common case.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apollo_gateway.core.db import Host, Mapping, TransportEndpoint


def build_iscsi_connection_info(
    mapping: "Mapping",
    endpoint: "TransportEndpoint",
    host: "Host",
) -> dict[str, Any]:
    """Build iSCSI connection information.

    Returns a dict containing the target IQN, portal addresses,
    LUN ID, and auth configuration.
    """
    targets = endpoint.targets_dict
    addresses = endpoint.addresses_dict
    auth = endpoint.auth_dict

    return {
        "protocol": "iscsi",
        "target_iqn": targets.get("target_iqn", ""),
        "portals": addresses.get("portals", []),
        "lun_id": mapping.underlay_id,
        "auth": auth,
        "host_iqns": host.iscsi_iqns,
    }


def build_fc_connection_info(
    mapping: "Mapping",
    endpoint: "TransportEndpoint",
    host: "Host",
) -> dict[str, Any]:
    """Build Fibre Channel connection information.

    Returns a dict containing the target WWPNs, LUN ID, and
    the host's initiator WWPNs.
    """
    targets = endpoint.targets_dict

    return {
        "protocol": "fc",
        "target_wwpns": targets.get("target_wwpns", []),
        "lun_id": mapping.lun_id,
        "initiator_wwpns": host.fc_wwpns,
    }


def build_nvmeof_connection_info(
    mapping: "Mapping",
    endpoint: "TransportEndpoint",
    host: "Host",
) -> dict[str, Any]:
    """Build NVMe-oF TCP connection information.

    Returns a dict containing the subsystem NQN, listener addresses,
    namespace ID, and the host's NVMe NQNs.
    """
    targets = endpoint.targets_dict
    addresses = endpoint.addresses_dict

    return {
        "protocol": "nvmeof_tcp",
        "subsystem_nqn": targets.get("subsystem_nqn", ""),
        "listeners": addresses.get("listeners", []),
        "nsid": mapping.underlay_id,
        "host_nqns": host.nvme_nqns,
    }


def build_connection_info(
    mapping: "Mapping",
    endpoint: "TransportEndpoint",
    host: "Host",
) -> dict[str, Any]:
    """Dispatch to the correct protocol builder based on endpoint protocol."""
    protocol = endpoint.protocol
    if protocol == "iscsi":
        return build_iscsi_connection_info(mapping, endpoint, host)
    elif protocol == "fc":
        return build_fc_connection_info(mapping, endpoint, host)
    elif protocol == "nvmeof_tcp":
        return build_nvmeof_connection_info(mapping, endpoint, host)
    else:
        return {"protocol": protocol, "error": f"unsupported protocol: {protocol}"}
