# FILE: apollo_gateway/spdk/ensure.py
"""Idempotent ensure_* functions that reconcile desired DB state into SPDK.

Each function checks whether the resource already exists in SPDK before
attempting creation, making them safe to call on repeated reconcile passes.

SPDK naming conventions (v1 — subsystem-scoped):
  - Backing bdev:   apollo-pool-{pool.id}
  - Lvol store:     {subsystem_name}.{pool_name}
  - Lvol bdev:      {subsystem_name}.{pool_name}/apollo-vol-{volume_id}
  - iSCSI IQN:      {prefix}:{subsystem_name}:{export_container_id}
  - NVMe NQN:       {prefix}:{subsystem_name}:{export_container_id}
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
# SPDK naming helpers
# ---------------------------------------------------------------------------

def _lvstore_name(subsystem_name: str, pool_name: str) -> str:
    """Lvol store name: '{subsystem_name}.{pool_name}'."""
    return f"{subsystem_name}.{pool_name}"


def _lvol_bdev_name(subsystem_name: str, pool_name: str, volume_id: str) -> str:
    """Full SPDK bdev name for an lvol: '{lvstore}/apollo-vol-{volume_id}'."""
    return f"{_lvstore_name(subsystem_name, pool_name)}/apollo-vol-{volume_id}"


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


def ensure_pool(client: SPDKClient, pool: Pool, subsystem_name: str) -> None:
    """Ensure backing bdev and lvol store exist for *pool*.

    Parameters
    ----------
    client:
        Connected :class:`~apollo_gateway.spdk.rpc.SPDKClient`.
    pool:
        ORM :class:`~apollo_gateway.core.db.Pool` instance.
    subsystem_name:
        Name of the owning subsystem (used in lvol store naming).
    """
    from apollo_gateway.core.models import PoolBackendType

    backing_bdev = f"apollo-pool-{pool.id}"
    lvs_name = _lvstore_name(subsystem_name, pool.name)

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
    subsystem_name: str,
) -> str:
    """Ensure the lvol for *volume* exists. Returns full bdev name.

    Parameters
    ----------
    client:
        Connected :class:`~apollo_gateway.spdk.rpc.SPDKClient`.
    volume:
        ORM :class:`~apollo_gateway.core.db.Volume` instance.
    pool_name:
        Name of the pool (used for lvstore lookup).
    subsystem_name:
        Name of the owning subsystem (used in bdev naming).
    """
    lvol_name = f"apollo-vol-{volume.id}"
    full_name = _lvol_bdev_name(subsystem_name, pool_name, volume.id)
    lvs_name = _lvstore_name(subsystem_name, pool_name)

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
# Export container (iSCSI target / NVMe-oF subsystem)
# ---------------------------------------------------------------------------

def ensure_iscsi_export(client: SPDKClient, ec: ExportContainer, settings: Settings) -> None:
    """Ensure iSCSI portal and initiator groups exist.

    The target node itself is created lazily by :func:`ensure_iscsi_mapping`
    because SPDK requires at least one LUN at target-creation time.
    """
    iscsi_rpc.ensure_portal_group(client, settings.iscsi_portal_ip, settings.iscsi_portal_port)
    iscsi_rpc.ensure_initiator_group(client)


def ensure_nvmef_export(
    client: SPDKClient,
    ec: ExportContainer,
    settings: Settings,
    model_number: str = "Apollo Gateway",
    serial_number: str = "APOLLO0001",
) -> None:
    """Ensure NVMe-oF TCP transport and subsystem exist for *ec*.

    Parameters
    ----------
    model_number:
        NVMe subsystem model string (from capability profile).
    serial_number:
        NVMe subsystem serial number string.
    """
    nvmf_rpc.ensure_transport(client)

    if not nvmf_rpc.subsystem_exists(client, ec.target_nqn):
        nvmf_rpc.create_subsystem(
            client, ec.target_nqn,
            model_number=model_number,
            serial_number=serial_number,
        )
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
    """Ensure the volume's bdev is attached as the correct LUN on the target.

    If the iSCSI target node does not exist yet it is created here with this
    LUN as its initial member, because SPDK requires at least one LUN at
    target-creation time.
    """
    if not iscsi_rpc.target_node_exists(client, ec.target_iqn):
        iscsi_rpc.create_target_node(
            client,
            ec.target_iqn,
            luns=[{"bdev_name": volume.bdev_name, "lun_id": mapping.lun_id}],
        )
        return

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
