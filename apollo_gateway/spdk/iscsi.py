# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""iSCSI RPC helpers.

All functions are synchronous wrappers around SPDKClient.call().
If an RPC method shape is uncertain it is isolated here for easy adjustment.
"""

from __future__ import annotations

import logging
from typing import Any

from apollo_gateway.spdk.rpc import SPDKClient, SPDKError

logger = logging.getLogger("apollo_gateway.spdk.iscsi")

_PORTAL_GROUP_TAG = 1
_INITIATOR_GROUP_TAG = 1


# ---------------------------------------------------------------------------
# Infrastructure: portal group + initiator group
# ---------------------------------------------------------------------------

def get_portal_groups(client: SPDKClient) -> list[dict[str, Any]]:
    result = client.call("iscsi_get_portal_groups")
    return result if result else []


def ensure_portal_group(client: SPDKClient, ip: str, port: int) -> None:
    """Ensure portal group tag=1 exists listening on ip:port."""
    groups = get_portal_groups(client)
    for g in groups:
        if g.get("tag") == _PORTAL_GROUP_TAG:
            logger.debug("iSCSI portal group %d already exists", _PORTAL_GROUP_TAG)
            return

    logger.info("Creating iSCSI portal group tag=%d %s:%d", _PORTAL_GROUP_TAG, ip, port)
    client.call("iscsi_create_portal_group", {
        "tag": _PORTAL_GROUP_TAG,
        "portals": [{"host": ip, "port": str(port)}],
    })


def get_initiator_groups(client: SPDKClient) -> list[dict[str, Any]]:
    result = client.call("iscsi_get_initiator_groups")
    return result if result else []


def ensure_initiator_group(client: SPDKClient) -> None:
    """Ensure initiator group tag=1 exists allowing any initiator."""
    groups = get_initiator_groups(client)
    for g in groups:
        if g.get("tag") == _INITIATOR_GROUP_TAG:
            logger.debug("iSCSI initiator group %d already exists", _INITIATOR_GROUP_TAG)
            return

    logger.info("Creating iSCSI initiator group tag=%d (ANY)", _INITIATOR_GROUP_TAG)
    client.call("iscsi_create_initiator_group", {
        "tag": _INITIATOR_GROUP_TAG,
        "initiators": ["ANY"],
        "netmasks": ["ANY"],
    })


# ---------------------------------------------------------------------------
# Target node management
# ---------------------------------------------------------------------------

def get_target_nodes(client: SPDKClient) -> list[dict[str, Any]]:
    result = client.call("iscsi_get_target_nodes")
    return result if result else []


def target_node_exists(client: SPDKClient, iqn: str) -> bool:
    nodes = get_target_nodes(client)
    return any(n.get("name") == iqn for n in nodes)


def create_target_node(
    client: SPDKClient,
    iqn: str,
    luns: list[dict[str, Any]] | None = None,
) -> None:
    """Create an iSCSI target node.

    ``iqn`` is the full target IQN.  SPDK uses the last component after the
    final colon as the internal name; the IQN itself is used in ``name``.
    ``luns`` is a list of ``{"bdev_name": str, "lun_id": int}``.
    """
    logger.info("Creating iSCSI target node %s", iqn)
    client.call("iscsi_create_target_node", {
        "name": iqn,
        "alias_name": iqn,
        "pg_ig_maps": [{"pg_tag": _PORTAL_GROUP_TAG, "ig_tag": _INITIATOR_GROUP_TAG}],
        "luns": luns or [],
        "queue_depth": 64,
        "disable_chap": True,
        "require_chap": False,
        "mutual_chap": False,
        "chap_group": 0,
        "header_digest": False,
        "data_digest": False,
    })


def add_lun(client: SPDKClient, iqn: str, bdev_name: str, lun_id: int) -> None:
    """Add a LUN (bdev) to an existing iSCSI target node."""
    logger.info("Adding LUN %d bdev=%s to target %s", lun_id, bdev_name, iqn)
    client.call("iscsi_target_node_add_lun", {
        "name": iqn,
        "bdev_name": bdev_name,
        "lun_id": lun_id,
    })


def delete_target_node(client: SPDKClient, iqn: str) -> None:
    """Delete an iSCSI target node by its full IQN."""
    logger.info("Deleting iSCSI target node %s", iqn)
    try:
        client.call("iscsi_delete_target_node", {"name": iqn})
    except SPDKError as exc:
        # Tolerate "not found" during cleanup
        if "not found" in exc.message.lower() or exc.code == -32602:
            logger.debug("Target %s already absent: %s", iqn, exc.message)
        else:
            raise


def get_lun_ids_on_target(client: SPDKClient, iqn: str) -> list[int]:
    """Return the list of LUN IDs currently attached to ``iqn``."""
    nodes = get_target_nodes(client)
    for node in nodes:
        if node.get("name") == iqn:
            return [lun["lun_id"] for lun in node.get("luns", [])]
    return []
