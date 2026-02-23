# FILE: apollo_gateway/spdk/ensure.py
"""Idempotent ensure_* functions that reconcile desired DB state into SPDK.

Each function checks whether the resource already exists in SPDK before
attempting creation, making them safe to call on repeated reconcile passes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apollo_gateway.spdk import iscsi as iscsi_rpc
from apollo_gateway.spdk import nvmf as nvmf_rpc
from apollo_gateway.spdk.rpc import SPDKClient, SPDKError

if TYPE_CHECKING:
    from apollo_gateway.core.db import ExportContainer, Mapping, Pool, Volume
    from apollo_gateway.config import Settings

logger = logging.getLogger("apollo_gateway.spdk.ensure")


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


def allocate_nsid(used: list[int]) -> int:
    """Return the smallest integer ≥ 1 not in *used* (NSID 0 is reserved)."""
    used_set = set(used)
    candidate = 1
    while candidate in used_set:
        candidate += 1
    return candidate


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


def ensure_pool(client: SPDKClient, pool: Pool) -> None:
    """Ensure backing bdev and lvol store exist for *pool*."""
    from apollo_gateway.core.models import PoolBackendType

    backing_bdev = f"apollo-pool-{pool.id}"

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
        else:
            raise ValueError(f"Unknown backend_type: {pool.backend_type}")
    else:
        logger.debug("Backing bdev %s already exists", backing_bdev)

    if not _lvstore_exists(client, pool.name):
        logger.info("Creating lvstore %s on bdev %s", pool.name, backing_bdev)
        client.call("bdev_lvol_create_lvstore", {
            "bdev_name": backing_bdev,
            "lvs_name": pool.name,
        })
    else:
        logger.debug("lvstore %s already exists", pool.name)


# ---------------------------------------------------------------------------
# Volume (lvol)
# ---------------------------------------------------------------------------

def _lvol_bdev_name(pool_name: str, volume_id: str) -> str:
    return f"{pool_name}/apollo-vol-{volume_id}"


def ensure_lvol(client: SPDKClient, volume: Volume, pool_name: str) -> str:
    """Ensure the lvol for *volume* exists. Returns full bdev name."""
    lvol_name = f"apollo-vol-{volume.id}"
    full_name = _lvol_bdev_name(pool_name, volume.id)

    if not _bdev_exists(client, full_name):
        logger.info("Creating lvol %s in lvstore %s (%d MiB)", lvol_name, pool_name, volume.size_mb)
        client.call("bdev_lvol_create", {
            "lvol_name": lvol_name,
            "size_in_mib": volume.size_mb,
            "lvs_name": pool_name,
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
# Export container (iSCSI target / NVMe-oF subsystem)
# ---------------------------------------------------------------------------

def ensure_iscsi_export(client: SPDKClient, ec: ExportContainer, settings: Settings) -> None:
    """Ensure iSCSI infrastructure and a target node exist for *ec*."""
    iscsi_rpc.ensure_portal_group(client, settings.iscsi_portal_ip, settings.iscsi_portal_port)
    iscsi_rpc.ensure_initiator_group(client)

    if not iscsi_rpc.target_node_exists(client, ec.target_iqn):
        iscsi_rpc.create_target_node(client, ec.target_iqn, luns=[])
    else:
        logger.debug("iSCSI target %s already exists", ec.target_iqn)


def ensure_nvmef_export(client: SPDKClient, ec: ExportContainer, settings: Settings) -> None:
    """Ensure NVMe-oF TCP transport and subsystem exist for *ec*."""
    nvmf_rpc.ensure_transport(client)

    if not nvmf_rpc.subsystem_exists(client, ec.target_nqn):
        nvmf_rpc.create_subsystem(client, ec.target_nqn)
        nvmf_rpc.add_listener(client, ec.target_nqn, settings.nvmef_portal_ip, settings.nvmef_portal_port)
    else:
        logger.debug("NVMe-oF subsystem %s already exists", ec.target_nqn)


# ---------------------------------------------------------------------------
# Mapping (attach LUN / namespace)
# ---------------------------------------------------------------------------

def ensure_iscsi_mapping(
    client: SPDKClient,
    mapping: Mapping,
    volume: Volume,
    ec: ExportContainer,
) -> None:
    """Ensure the volume's bdev is attached as the correct LUN on the target."""
    existing_luns = iscsi_rpc.get_lun_ids_on_target(client, ec.target_iqn)
    if mapping.lun_id not in existing_luns:
        iscsi_rpc.add_lun(client, ec.target_iqn, volume.bdev_name, mapping.lun_id)
    else:
        logger.debug("LUN %d already attached to %s", mapping.lun_id, ec.target_iqn)


def ensure_nvmef_mapping(
    client: SPDKClient,
    mapping: Mapping,
    volume: Volume,
    ec: ExportContainer,
) -> None:
    """Ensure the volume's bdev is attached as the correct namespace in the subsystem."""
    existing_nsids = nvmf_rpc.get_nsids(client, ec.target_nqn)
    if mapping.ns_id not in existing_nsids:
        nvmf_rpc.add_namespace(client, ec.target_nqn, volume.bdev_name, mapping.ns_id)
    else:
        logger.debug("NSID %d already in subsystem %s", mapping.ns_id, ec.target_nqn)
