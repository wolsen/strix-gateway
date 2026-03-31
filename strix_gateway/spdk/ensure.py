# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Idempotent ensure_* functions that reconcile desired DB state into SPDK.

Each function checks whether the resource already exists in SPDK before
attempting creation, making them safe to call on repeated reconcile passes.

SPDK naming conventions (v0.2 — array-scoped):
  - Backing bdev:   strix-pool-{pool.id}
  - Lvol store:     {array_name}.{pool_name}
  - Lvol bdev:      {array_name}.{pool_name}/strix-vol-{volume_id}
  - iSCSI IQN:      stored on TransportEndpoint.targets.target_iqn
  - NVMe NQN:       stored on TransportEndpoint.targets.subsystem_nqn
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from strix_gateway.spdk import iscsi as iscsi_rpc
from strix_gateway.spdk import nvmf as nvmf_rpc
from strix_gateway.spdk.rpc import SPDKClient, SPDKError

if TYPE_CHECKING:
    from strix_gateway.core.db import Mapping, Pool, TransportEndpoint, Volume
    from strix_gateway.config import Settings

logger = logging.getLogger("strix_gateway.spdk.ensure")


# ---------------------------------------------------------------------------
# LUN / NSID allocation helpers (pure functions — easy to unit-test)
# ---------------------------------------------------------------------------

def allocate_lun(used: list[int]) -> int:
    """Return the smallest non-negative integer not in *used*."""
    used_set = set(used)
    candidate = 0
    while candidate in used_set:
        candidate += 1
    return candidate


def allocate_lun_from_base(used: list[int], base: int = 0) -> int:
    """Return the smallest available LUN ID starting at *base*."""
    normalized = [u - base for u in used if u >= base]
    return allocate_lun(normalized) + base


def allocate_nsid(used: list[int]) -> int:
    """Return the smallest integer ≥ 1 not in *used* (NSID 0 is reserved)."""
    used_set = set(used)
    candidate = 1
    while candidate in used_set:
        candidate += 1
    return candidate


# ---------------------------------------------------------------------------
# SPDK naming helpers
# ---------------------------------------------------------------------------

def _lvstore_name(array_name: str, pool_name: str) -> str:
    """Lvol store name: '{array_name}.{pool_name}'."""
    return f"{array_name}.{pool_name}"


def _lvol_bdev_name(array_name: str, pool_name: str, volume_id: str) -> str:
    """Full SPDK bdev name for an lvol: '{lvstore}/strix-vol-{volume_id}'."""
    return f"{_lvstore_name(array_name, pool_name)}/strix-vol-{volume_id}"


# ---------------------------------------------------------------------------
# Pool (bdev + lvstore)
# ---------------------------------------------------------------------------

def _bdev_exists(client: SPDKClient, name: str) -> bool:
    try:
        result = client.call("bdev_get_bdevs", {"name": name})
        return bool(result)
    except SPDKError:
        return False


def _lvstore_exists(client: SPDKClient, lvs_name: str) -> bool:
    try:
        result = client.call("bdev_lvol_get_lvstores", {"lvs_name": lvs_name})
        return bool(result)
    except SPDKError:
        return False


def ensure_pool(client: SPDKClient, pool: Pool, array_name: str) -> None:
    """Ensure backing bdev and lvol store exist for *pool*.

    Parameters
    ----------
    client:
        Connected :class:`~strix_gateway.spdk.rpc.SPDKClient`.
    pool:
        ORM :class:`~strix_gateway.core.db.Pool` instance.
    array_name:
        Name of the owning array (used in lvol store naming).
    """
    from strix_gateway.core.models import PoolBackendType

    backing_bdev = f"strix-pool-{pool.id}"
    lvs_name = _lvstore_name(array_name, pool.name)

    if not _bdev_exists(client, backing_bdev):
        if pool.backend_type == PoolBackendType.malloc:
            if not pool.size_mb:
                raise ValueError(f"Pool {pool.id} malloc backend requires size_mb")
            block_size = 512
            num_blocks = (pool.size_mb * 1024 * 1024) // block_size
            logger.info("Creating malloc bdev %s (%d MiB)", backing_bdev, pool.size_mb)
            client.call("bdev_malloc_create", {
                "name": backing_bdev,
                "num_blocks": num_blocks,
                "block_size": block_size,
            })
        elif pool.backend_type == PoolBackendType.aio_file:
            if not pool.aio_path:
                raise ValueError(f"Pool {pool.id} aio_file backend requires aio_path")
            logger.info("Creating AIO bdev %s -> %s", backing_bdev, pool.aio_path)
            client.call("bdev_aio_create", {
                "name": backing_bdev,
                "filename": pool.aio_path,
                "block_size": 512,
            })
            # Query SPDK for the actual bdev size (determined by the file)
            bdevs = client.call("bdev_get_bdevs", {"name": backing_bdev})
            if bdevs:
                num_blocks = bdevs[0].get("num_blocks", 0)
                block_size = bdevs[0].get("block_size", 512)
                pool.size_mb = (num_blocks * block_size) // (1024 * 1024)
        else:
            raise ValueError(f"Unknown backend_type: {pool.backend_type}")
    else:
        logger.debug("Backing bdev %s already exists", backing_bdev)

    if not _lvstore_exists(client, lvs_name):
        logger.info("Creating lvstore %s on bdev %s", lvs_name, backing_bdev)
        client.call("bdev_lvol_create_lvstore", {
            "bdev_name": backing_bdev,
            "lvs_name": lvs_name,
        })
    else:
        logger.debug("lvstore %s already exists", lvs_name)


# ---------------------------------------------------------------------------
# Volume (lvol)
# ---------------------------------------------------------------------------

def ensure_lvol(
    client: SPDKClient,
    volume: Volume,
    pool_name: str,
    array_name: str,
) -> str:
    """Ensure the lvol for *volume* exists. Returns full bdev name.

    Parameters
    ----------
    client:
        Connected :class:`~strix_gateway.spdk.rpc.SPDKClient`.
    volume:
        ORM :class:`~strix_gateway.core.db.Volume` instance.
    pool_name:
        Name of the pool (used for lvstore lookup).
    array_name:
        Name of the owning array (used in bdev naming).
    """
    lvol_name = f"strix-vol-{volume.id}"
    full_name = _lvol_bdev_name(array_name, pool_name, volume.id)
    lvs_name = _lvstore_name(array_name, pool_name)

    if not _bdev_exists(client, full_name):
        logger.info(
            "Creating lvol %s in lvstore %s (%d MiB)",
            lvol_name, lvs_name, volume.size_mb,
        )
        client.call("bdev_lvol_create", {
            "lvol_name": lvol_name,
            "size_in_mib": volume.size_mb,
            "lvs_name": lvs_name,
        })
    else:
        logger.debug("lvol %s already exists", full_name)

    return full_name


def delete_lvol(client: SPDKClient, bdev_name: str) -> None:
    """Delete an lvol bdev by its full name (lvstore/lvol)."""
    logger.info("Deleting lvol %s", bdev_name)
    try:
        client.call("bdev_lvol_delete", {"name": bdev_name})
    except SPDKError as exc:
        if "not found" in exc.message.lower():
            logger.debug("lvol %s already absent", bdev_name)
        else:
            raise


def resize_lvol(client: SPDKClient, bdev_name: str, new_size_mb: int) -> None:
    """Resize an existing lvol to *new_size_mb* MiB."""
    logger.info("Resizing lvol %s to %d MiB", bdev_name, new_size_mb)
    client.call("bdev_lvol_resize", {
        "name": bdev_name,
        "size_in_mib": new_size_mb,
    })


# ---------------------------------------------------------------------------
# Export (iSCSI target / NVMe-oF subsystem) — now driven by TransportEndpoint
# ---------------------------------------------------------------------------

def _ep_targets(ep: TransportEndpoint) -> dict:
    """Parse endpoint.targets (JSON str or dict).  Returns a dict always."""
    raw = json.loads(ep.targets) if isinstance(ep.targets, str) else ep.targets
    if isinstance(raw, list):
        return {}
    return raw or {}


def _ep_addresses(ep: TransportEndpoint) -> dict:
    """Parse endpoint.addresses (JSON str or dict).  Returns a dict always."""
    raw = json.loads(ep.addresses) if isinstance(ep.addresses, str) else ep.addresses
    if isinstance(raw, list):
        return {"portals": raw} if raw else {}
    return raw or {}


def ensure_iscsi_export(client: SPDKClient, ep: TransportEndpoint, settings: Settings) -> None:
    """Ensure iSCSI portal and initiator groups exist.

    The target node itself is created lazily by :func:`ensure_iscsi_mapping`
    because SPDK requires at least one LUN at target-creation time.
    """
    addresses = _ep_addresses(ep)
    portals = addresses.get("portals", [f"{settings.iscsi_portal_ip}:{settings.iscsi_portal_port}"])
    if portals:
        first = portals[0]
        ip, _, port = first.rpartition(":")
        iscsi_rpc.ensure_portal_group(client, ip or settings.iscsi_portal_ip, int(port or settings.iscsi_portal_port))
    else:
        iscsi_rpc.ensure_portal_group(client, settings.iscsi_portal_ip, settings.iscsi_portal_port)
    iscsi_rpc.ensure_initiator_group(client)


def ensure_nvmef_export(
    client: SPDKClient,
    ep: TransportEndpoint,
    settings: Settings,
    model_number: str = "Strix Gateway",
    serial_number: str = "STRIX0001",
) -> None:
    """Ensure NVMe-oF TCP transport and subsystem exist for *ep*."""
    targets = _ep_targets(ep)
    target_nqn = targets.get("subsystem_nqn", "")
    if not target_nqn:
        logger.warning("NVMe-oF endpoint %s has no subsystem_nqn in targets", ep.id)
        return

    nvmf_rpc.ensure_transport(client)

    if not nvmf_rpc.subsystem_exists(client, target_nqn):
        nvmf_rpc.create_subsystem(
            client, target_nqn,
            model_number=model_number,
            serial_number=serial_number,
        )
        addresses = _ep_addresses(ep)
        listeners = addresses.get("listeners", [f"{settings.nvmef_portal_ip}:{settings.nvmef_portal_port}"])
        for listener in listeners:
            ip, _, port = listener.rpartition(":")
            nvmf_rpc.add_listener(
                client, target_nqn,
                ip or settings.nvmef_portal_ip,
                int(port or settings.nvmef_portal_port),
            )
    else:
        logger.debug("NVMe-oF subsystem %s already exists", target_nqn)


# ---------------------------------------------------------------------------
# Mapping (attach LUN / namespace) — now driven by Mapping + TransportEndpoint
# ---------------------------------------------------------------------------

def ensure_iscsi_mapping(
    client: SPDKClient,
    mapping: Mapping,
    volume: Volume,
    ep: TransportEndpoint,
) -> None:
    """Ensure the volume's bdev is attached as the correct LUN on the target.

    If the iSCSI target node does not exist yet it is created here with this
    LUN as its initial member.
    """
    targets = _ep_targets(ep)
    target_iqn = targets.get("target_iqn", "")
    if not target_iqn:
        logger.warning("iSCSI endpoint %s has no target_iqn", ep.id)
        return

    lun_id = mapping.underlay_id

    if not iscsi_rpc.target_node_exists(client, target_iqn):
        iscsi_rpc.create_target_node(
            client,
            target_iqn,
            luns=[{"bdev_name": volume.bdev_name, "lun_id": lun_id}],
        )
        return

    existing_luns = iscsi_rpc.get_lun_ids_on_target(client, target_iqn)
    if lun_id not in existing_luns:
        iscsi_rpc.add_lun(client, target_iqn, volume.bdev_name, lun_id)
    else:
        logger.debug("LUN %d already attached to %s", lun_id, target_iqn)


def ensure_nvmef_mapping(
    client: SPDKClient,
    mapping: Mapping,
    volume: Volume,
    ep: TransportEndpoint,
) -> None:
    """Ensure the volume's bdev is attached as the correct namespace."""
    targets = _ep_targets(ep)
    target_nqn = targets.get("subsystem_nqn", "")
    if not target_nqn:
        logger.warning("NVMe-oF endpoint %s has no subsystem_nqn", ep.id)
        return

    nsid = mapping.underlay_id
    existing_nsids = nvmf_rpc.get_nsids(client, target_nqn)
    if nsid not in existing_nsids:
        nvmf_rpc.add_namespace(client, target_nqn, volume.bdev_name, nsid)
    else:
        logger.debug("NSID %d already in subsystem %s", nsid, target_nqn)
