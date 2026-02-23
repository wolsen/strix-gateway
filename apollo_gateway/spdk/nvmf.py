# FILE: apollo_gateway/spdk/nvmf.py
"""NVMe-oF RPC helpers.

All functions are synchronous wrappers around SPDKClient.call().
If an RPC method shape is uncertain it is isolated here for easy adjustment.
"""

from __future__ import annotations

import logging
from typing import Any

from apollo_gateway.spdk.rpc import SPDKClient, SPDKError

logger = logging.getLogger("apollo_gateway.spdk.nvmf")

# SPDK error code returned when a transport already exists
_EEXIST_CODE = -32602  # invalid params — covers "already exists" in SPDK


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def ensure_transport(client: SPDKClient) -> None:
    """Ensure a TCP NVMe-oF transport exists.  Idempotent."""
    try:
        client.call("nvmf_create_transport", {"trtype": "TCP"})
        logger.info("NVMe-oF TCP transport created")
    except SPDKError as exc:
        if "already exists" in exc.message.lower() or exc.code == _EEXIST_CODE:
            logger.debug("NVMe-oF TCP transport already present")
        else:
            raise


# ---------------------------------------------------------------------------
# Subsystem management
# ---------------------------------------------------------------------------

def get_subsystems(client: SPDKClient) -> list[dict[str, Any]]:
    result = client.call("nvmf_get_subsystems")
    return result if result else []


def subsystem_exists(client: SPDKClient, nqn: str) -> bool:
    return any(s.get("nqn") == nqn for s in get_subsystems(client))


def create_subsystem(client: SPDKClient, nqn: str) -> None:
    """Create an NVMe-oF subsystem that allows any host."""
    logger.info("Creating NVMe-oF subsystem %s", nqn)
    client.call("nvmf_create_subsystem", {
        "nqn": nqn,
        "allow_any_host": True,
        "serial_number": "APOLLO0001",
        "model_number": "Apollo Gateway",
    })


def add_listener(client: SPDKClient, nqn: str, ip: str, port: int) -> None:
    """Add a TCP listener to a subsystem."""
    logger.info("Adding NVMe-oF listener %s:%d to subsystem %s", ip, port, nqn)
    client.call("nvmf_subsystem_add_listener", {
        "nqn": nqn,
        "listen_address": {
            "trtype": "TCP",
            "adrfam": "IPv4",
            "traddr": ip,
            "trsvcid": str(port),
        },
    })


def add_namespace(client: SPDKClient, nqn: str, bdev_name: str, nsid: int) -> None:
    """Add a namespace (bdev) to a subsystem."""
    logger.info("Adding namespace nsid=%d bdev=%s to subsystem %s", nsid, bdev_name, nqn)
    client.call("nvmf_subsystem_add_ns", {
        "nqn": nqn,
        "namespace": {
            "bdev_name": bdev_name,
            "nsid": nsid,
        },
    })


def remove_namespace(client: SPDKClient, nqn: str, nsid: int) -> None:
    """Remove a namespace from a subsystem."""
    logger.info("Removing namespace nsid=%d from subsystem %s", nsid, nqn)
    try:
        client.call("nvmf_subsystem_remove_ns", {"nqn": nqn, "nsid": nsid})
    except SPDKError as exc:
        if "not found" in exc.message.lower():
            logger.debug("Namespace nsid=%d already absent from %s", nsid, nqn)
        else:
            raise


def delete_subsystem(client: SPDKClient, nqn: str) -> None:
    """Delete an NVMe-oF subsystem."""
    logger.info("Deleting NVMe-oF subsystem %s", nqn)
    try:
        client.call("nvmf_delete_subsystem", {"nqn": nqn})
    except SPDKError as exc:
        if "not found" in exc.message.lower():
            logger.debug("Subsystem %s already absent", nqn)
        else:
            raise


def get_namespaces(client: SPDKClient, nqn: str) -> list[dict[str, Any]]:
    """Return the namespace list for a given subsystem NQN."""
    subsystems = get_subsystems(client)
    for s in subsystems:
        if s.get("nqn") == nqn:
            return s.get("namespaces", [])
    return []


def get_nsids(client: SPDKClient, nqn: str) -> list[int]:
    """Return the list of NSIDs currently used in ``nqn``."""
    return [ns["nsid"] for ns in get_namespaces(client, nqn)]
