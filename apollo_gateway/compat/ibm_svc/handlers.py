# FILE: apollo_gateway/compat/ibm_svc/handlers.py
"""IBM SVC command implementations.

Each handler is an ``async`` function with the signature::

    async def _<name>(ctx: SvcContext, pc: ParsedCommand) -> str

It returns the text that should be written to stdout (may be empty for
``svctask`` commands that succeed silently).  On failure it raises a
:class:`~apollo_gateway.compat.ibm_svc.errors.SvcError` subclass.

All query handlers filter by ``ctx.subsystem_id`` so that two subsystems
may share pool/volume names without collision.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.config import settings as _settings
from apollo_gateway.core.db import ExportContainer, Host, Mapping, Pool, Volume
from apollo_gateway.core.models import Protocol, VolumeStatus
from apollo_gateway.spdk import iscsi as iscsi_rpc
from apollo_gateway.spdk.ensure import (
    allocate_lun,
    delete_lvol,
    ensure_iscsi_export,
    ensure_iscsi_mapping,
    ensure_lvol,
    resize_lvol,
)
from apollo_gateway.spdk.rpc import SPDKClient

from apollo_gateway.compat.ibm_svc.errors import (
    SvcAlreadyExistsError,
    SvcError,
    SvcInvalidArgError,
    SvcNotFoundError,
    SvcUnknownCommandError,
)
from apollo_gateway.compat.ibm_svc.format import format_delim, format_table
from apollo_gateway.compat.ibm_svc.parse import ParsedCommand, optional_flag, require_flag

logger = logging.getLogger("apollo_gateway.compat.ibm_svc")


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------

@dataclass
class SvcContext:
    """Execution context passed to every handler.

    All handlers must filter DB queries by ``subsystem_id`` to provide
    isolation between subsystems.
    """

    session: AsyncSession
    spdk: SPDKClient
    subsystem_id: str
    subsystem_name: str
    effective_profile: dict = field(default_factory=dict)
    protocols_enabled: list[str] = field(default_factory=lambda: ["iscsi", "nvmeof_tcp"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mb_to_gb_str(size_mb: int) -> str:
    """Convert MiB to a ``X.XXGB`` string as IBM SVC displays it."""
    return f"{size_mb / 1024:.2f}GB"


def _volume_status(status: str) -> str:
    """Map Apollo VolumeStatus → IBM SVC online/offline."""
    return "online" if status in (
        VolumeStatus.available,
        VolumeStatus.in_use,
        VolumeStatus.extending,
    ) else "offline"


def _host_iqns(host: Host) -> list[str]:
    """Return all IQNs stored in host.iqn (comma-separated)."""
    if not host.iqn:
        return []
    return [q.strip() for q in host.iqn.split(",") if q.strip()]


# ---------------------------------------------------------------------------
# svcinfo handlers
# ---------------------------------------------------------------------------

async def _lssystem(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lssystem — gateway identity with capability profile fields."""
    delim = pc.delim or "!"
    profile = ctx.effective_profile
    version = profile.get("version", "8.4.0.0")
    model = profile.get("model", "apollo-gateway")
    fields = {
        "id": "0",
        "name": ctx.subsystem_name,
        "location": "local",
        "partnership": "",
        "total_mdisk_capacity": "0.00TB",
        "space_in_mdisk_grps": "0.00TB",
        "space_allocated_to_vdisks": "0.00TB",
        "total_free_space": "0.00TB",
        "total_vdiskcopy_capacity": "0.00TB",
        "total_used_capacity": "0.00TB",
        "total_overallocation": "0",
        "total_vdisk_capacity": "0.00TB",
        "code_level": f"{version} (build 156.8.2209261126000)",
        "product_name": model,
        "console_IP": "127.0.0.1",
        "id_alias": "0000000000000000",
        "iscsi_auth_method": "none",
        "iscsi_chap_secret": "",
    }
    return format_delim(fields, delim)


async def _lsmdiskgrp(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsmdiskgrp [<pool_name>] [-delim <d>]"""
    session = ctx.session
    name_or_id: Optional[str] = pc.positional[0] if pc.positional else None
    delim = pc.delim or "!"

    if name_or_id is not None:
        result = await session.execute(
            select(Pool).where(Pool.name == name_or_id, Pool.subsystem_id == ctx.subsystem_id)
        )
        pool = result.scalar_one_or_none()
        if pool is None:
            raise SvcNotFoundError(f"mdiskgrp '{name_or_id}'")
        fields = {
            "id": pool.id,
            "name": pool.name,
            "status": "online",
            "mdisk_count": "1",
            "vdisk_count": str(len(pool.volumes)),
            "capacity": _mb_to_gb_str(pool.size_mb or 0),
            "extent_size": "256",
            "free_capacity": _mb_to_gb_str(pool.size_mb or 0),
            "virtual_capacity": "0.00GB",
            "used_capacity": "0.00GB",
            "real_capacity": "0.00GB",
            "overallocation": "0",
            "warning": "0",
            "easy_tier": "off",
            "easy_tier_status": "balanced",
            "compression_active": "no",
            "compression_virtual_capacity": "0.00MB",
            "compression_compressed_capacity": "0.00MB",
            "compression_uncompressed_capacity": "0.00MB",
        }
        return format_delim(fields, delim)

    # List pools in this subsystem only
    result = await session.execute(
        select(Pool).where(Pool.subsystem_id == ctx.subsystem_id)
    )
    pools = result.scalars().all()
    rows = [
        {
            "id": p.id,
            "name": p.name,
            "status": "online",
            "mdisk_count": "1",
            "vdisk_count": str(len(p.volumes)),
            "capacity": _mb_to_gb_str(p.size_mb or 0),
            "extent_size": "256",
            "free_capacity": _mb_to_gb_str(p.size_mb or 0),
            "virtual_capacity": "0.00GB",
            "compression_active": "no",
        }
        for p in pools
    ]
    return format_table(rows)


async def _lsvdisk(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsvdisk [<vdisk_name>] [-delim <d>]"""
    session = ctx.session
    name_or_id: Optional[str] = pc.positional[0] if pc.positional else None
    delim = pc.delim or "!"

    if name_or_id is not None:
        result = await session.execute(
            select(Volume).where(
                Volume.name == name_or_id,
                Volume.subsystem_id == ctx.subsystem_id,
            )
        )
        volume = result.scalar_one_or_none()
        if volume is None:
            raise SvcNotFoundError(f"vdisk '{name_or_id}'")
        pool = volume.pool
        fields = {
            "id": volume.id,
            "name": volume.name,
            "IO_group_id": "0",
            "IO_group_name": "io_grp0",
            "status": _volume_status(volume.status),
            "mdisk_grp_id": pool.id if pool else "",
            "mdisk_grp_name": pool.name if pool else "",
            "capacity": _mb_to_gb_str(volume.size_mb),
            "type": "striped",
            "formatted": "no",
            "mdisk_id": "",
            "mdisk_name": "",
            "FC_id": "",
            "FC_name": "",
            "RC_id": "",
            "RC_name": "",
            "vdisk_UID": volume.id.replace("-", "").upper().zfill(32),
            "throttling": "0",
            "preferred_node_id": "1",
            "fast_write_state": "empty",
            "cache": "readwrite",
            "udid": "",
            "fc_map_count": "0",
            "sync_rate": "50",
            "copy_count": "1",
            "se_copy_count": "0",
            "filesystem": "",
            "mirror_write_priority": "latency",
            "RC_change": "no",
        }
        return format_delim(fields, delim)

    # List volumes in this subsystem only
    result = await session.execute(
        select(Volume).where(Volume.subsystem_id == ctx.subsystem_id)
    )
    volumes = result.scalars().all()
    rows = []
    for v in volumes:
        pool = v.pool
        rows.append({
            "id": v.id,
            "name": v.name,
            "IO_group_id": "0",
            "IO_group_name": "io_grp0",
            "status": _volume_status(v.status),
            "mdisk_grp_id": pool.id if pool else "",
            "mdisk_grp_name": pool.name if pool else "",
            "capacity": _mb_to_gb_str(v.size_mb),
            "type": "striped",
            "FC_id": "",
            "FC_name": "",
            "RC_id": "",
            "RC_name": "",
            "vdisk_UID": v.id.replace("-", "").upper().zfill(32),
            "fc_map_count": "0",
            "copy_count": "1",
            "fast_write_state": "empty",
            "se_copy_count": "0",
            "RC_change": "no",
        })
    return format_table(rows)


async def _lshost(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lshost [<host_name>] [-delim <d>]

    Hosts are global (not subsystem-scoped) per v0 design.
    """
    session = ctx.session
    name_or_id: Optional[str] = pc.positional[0] if pc.positional else None
    delim = pc.delim or "!"

    if name_or_id is not None:
        result = await session.execute(select(Host).where(Host.name == name_or_id))
        host = result.scalar_one_or_none()
        if host is None:
            raise SvcNotFoundError(f"host '{name_or_id}'")
        iqns = _host_iqns(host)
        fields = {
            "id": host.id,
            "name": host.name,
            "port_count": str(len(iqns)),
            "type": "generic",
            "mask": "1111111111111111",
            "iogrp_count": "1",
            "status": "online",
            "site_id": "",
            "site_name": "",
        }
        for i, iqn in enumerate(iqns):
            fields[f"iscsi_name_{i}"] = iqn
        if not iqns:
            fields["iscsi_name"] = ""
        return format_delim(fields, delim)

    # List all hosts (global)
    result = await session.execute(select(Host))
    hosts = result.scalars().all()
    rows = []
    for h in hosts:
        iqns = _host_iqns(h)
        rows.append({
            "id": h.id,
            "name": h.name,
            "port_count": str(len(iqns)),
            "iscsi_name": iqns[0] if iqns else "",
            "status": "online",
            "type": "generic",
        })
    return format_table(rows)


async def _lshostvdiskmap(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lshostvdiskmap <host_name> [-delim <d>]"""
    if not pc.positional:
        raise SvcInvalidArgError("lshostvdiskmap requires a host name argument")
    host_name = pc.positional[0]
    session = ctx.session

    host_result = await session.execute(select(Host).where(Host.name == host_name))
    host = host_result.scalar_one_or_none()
    if host is None:
        raise SvcNotFoundError(f"host '{host_name}'")

    maps_result = await session.execute(
        select(Mapping).where(
            Mapping.host_id == host.id,
            Mapping.subsystem_id == ctx.subsystem_id,
        )
    )
    mappings = maps_result.scalars().all()

    rows = []
    for m in mappings:
        vol = m.volume
        rows.append({
            "id": m.id,
            "name": str(m.lun_id if m.lun_id is not None else ""),
            "SCSI_id": str(m.lun_id if m.lun_id is not None else ""),
            "host_id": host.id,
            "host_name": host.name,
            "vdisk_id": vol.id,
            "vdisk_name": vol.name,
            "vdisk_UID": vol.id.replace("-", "").upper().zfill(32),
            "IO_group_id": "0",
            "IO_group_name": "io_grp0",
            "mapping_type": "private",
            "lun_id": str(m.lun_id if m.lun_id is not None else ""),
        })
    return format_table(rows)


async def _lsvdiskhostmap(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsvdiskhostmap <vdisk_name> [-delim <d>]"""
    if not pc.positional:
        raise SvcInvalidArgError("lsvdiskhostmap requires a vdisk name argument")
    vdisk_name = pc.positional[0]
    session = ctx.session

    vol_result = await session.execute(
        select(Volume).where(
            Volume.name == vdisk_name,
            Volume.subsystem_id == ctx.subsystem_id,
        )
    )
    volume = vol_result.scalar_one_or_none()
    if volume is None:
        raise SvcNotFoundError(f"vdisk '{vdisk_name}'")

    maps_result = await session.execute(
        select(Mapping).where(
            Mapping.volume_id == volume.id,
            Mapping.subsystem_id == ctx.subsystem_id,
        )
    )
    mappings = maps_result.scalars().all()

    rows = []
    for m in mappings:
        h = m.host
        rows.append({
            "id": m.id,
            "name": str(m.lun_id if m.lun_id is not None else ""),
            "SCSI_id": str(m.lun_id if m.lun_id is not None else ""),
            "host_id": h.id,
            "host_name": h.name,
            "vdisk_id": volume.id,
            "vdisk_name": volume.name,
            "vdisk_UID": volume.id.replace("-", "").upper().zfill(32),
            "IO_group_id": "0",
            "IO_group_name": "io_grp0",
            "mapping_type": "private",
            "lun_id": str(m.lun_id if m.lun_id is not None else ""),
        })
    return format_table(rows)


# ---------------------------------------------------------------------------
# svctask handlers
# ---------------------------------------------------------------------------

async def _mkvdisk(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask mkvdisk -name <name> -size <num> -unit gb -mdiskgrp <pool>"""
    name = require_flag(pc, "name")
    size_str = require_flag(pc, "size")
    unit = optional_flag(pc, "unit", "gb").lower()
    mdiskgrp = require_flag(pc, "mdiskgrp")

    try:
        size_num = int(size_str)
    except ValueError:
        raise SvcInvalidArgError(f"-size must be an integer, got '{size_str}'")

    if unit != "gb":
        raise SvcInvalidArgError(f"only -unit gb is supported (got '{unit}')")

    size_mb = size_num * 1024
    session = ctx.session

    # Check volume name uniqueness within this subsystem
    dup_result = await session.execute(
        select(Volume).where(
            Volume.name == name,
            Volume.subsystem_id == ctx.subsystem_id,
        )
    )
    if dup_result.scalar_one_or_none():
        raise SvcAlreadyExistsError(f"vdisk '{name}'")

    # Find pool by name within this subsystem
    pool_result = await session.execute(
        select(Pool).where(
            Pool.name == mdiskgrp,
            Pool.subsystem_id == ctx.subsystem_id,
        )
    )
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise SvcNotFoundError(f"mdiskgrp '{mdiskgrp}'")

    # Create Volume record
    volume = Volume(
        name=name,
        subsystem_id=ctx.subsystem_id,
        pool_id=pool.id,
        size_mb=size_mb,
        status=VolumeStatus.creating,
    )
    session.add(volume)
    await session.flush()  # get volume.id

    # Provision in SPDK
    try:
        bdev_name = await asyncio.to_thread(
            ensure_lvol, ctx.spdk, volume, pool.name, ctx.subsystem_name
        )
        volume.bdev_name = bdev_name
        volume.status = VolumeStatus.available
    except Exception as exc:
        volume.status = VolumeStatus.error
        await session.commit()
        raise SvcError(f"SPDK error provisioning vdisk: {exc}") from exc

    await session.commit()
    logger.info("mkvdisk: created vdisk '%s' id=%s size=%dMiB", name, volume.id, size_mb)
    return f"Virtual Disk, id [{volume.id}], successfully created"


async def _rmvdisk(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask rmvdisk <vdisk_name>"""
    if not pc.positional:
        raise SvcInvalidArgError("rmvdisk requires a vdisk name")
    vdisk_name = pc.positional[0]
    session = ctx.session

    result = await session.execute(
        select(Volume).where(
            Volume.name == vdisk_name,
            Volume.subsystem_id == ctx.subsystem_id,
        )
    )
    volume = result.scalar_one_or_none()
    if volume is None:
        raise SvcNotFoundError(f"vdisk '{vdisk_name}'")

    # Refuse if active mappings exist
    maps_result = await session.execute(
        select(Mapping).where(Mapping.volume_id == volume.id)
    )
    if maps_result.scalars().first():
        raise SvcInvalidArgError(
            f"vdisk '{vdisk_name}' has active host mappings; remove them first"
        )

    volume.status = VolumeStatus.deleting
    await session.flush()

    if volume.bdev_name:
        try:
            await asyncio.to_thread(delete_lvol, ctx.spdk, volume.bdev_name)
        except Exception as exc:
            volume.status = VolumeStatus.error
            await session.commit()
            raise SvcError(f"SPDK error deleting vdisk: {exc}") from exc

    await session.delete(volume)
    await session.commit()
    logger.info("rmvdisk: deleted vdisk '%s'", vdisk_name)
    return ""


async def _expandvdisksize(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask expandvdisksize -size <num> -unit gb <vdisk_name>

    Adds *size* GB to the current vdisk capacity (SVC expand-by semantics).
    """
    size_str = require_flag(pc, "size")
    unit = optional_flag(pc, "unit", "gb").lower()

    if not pc.positional:
        raise SvcInvalidArgError("expandvdisksize requires a vdisk name")
    vdisk_name = pc.positional[0]

    try:
        size_num = int(size_str)
    except ValueError:
        raise SvcInvalidArgError(f"-size must be an integer, got '{size_str}'")

    if unit != "gb":
        raise SvcInvalidArgError(f"only -unit gb is supported (got '{unit}')")

    session = ctx.session
    result = await session.execute(
        select(Volume).where(
            Volume.name == vdisk_name,
            Volume.subsystem_id == ctx.subsystem_id,
        )
    )
    volume = result.scalar_one_or_none()
    if volume is None:
        raise SvcNotFoundError(f"vdisk '{vdisk_name}'")

    new_size_mb = volume.size_mb + (size_num * 1024)
    volume.status = VolumeStatus.extending
    await session.flush()

    try:
        await asyncio.to_thread(resize_lvol, ctx.spdk, volume.bdev_name, new_size_mb)
        volume.size_mb = new_size_mb
        volume.status = VolumeStatus.available
    except Exception as exc:
        volume.status = VolumeStatus.error
        await session.commit()
        raise SvcError(f"SPDK error expanding vdisk: {exc}") from exc

    await session.commit()
    logger.info("expandvdisksize: expanded '%s' to %dMiB", vdisk_name, new_size_mb)
    return ""


async def _mkhost(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask mkhost -name <host>"""
    name = require_flag(pc, "name")
    session = ctx.session

    # Enforce name uniqueness (hosts are global)
    dup_result = await session.execute(select(Host).where(Host.name == name))
    if dup_result.scalar_one_or_none():
        raise SvcAlreadyExistsError(f"host '{name}'")

    host = Host(name=name)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    logger.info("mkhost: created host '%s' id=%s", name, host.id)
    return f"Host, id [{host.id}], successfully created"


async def _rmhost(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask rmhost <host_name>"""
    if not pc.positional:
        raise SvcInvalidArgError("rmhost requires a host name")
    host_name = pc.positional[0]
    session = ctx.session

    result = await session.execute(select(Host).where(Host.name == host_name))
    host = result.scalar_one_or_none()
    if host is None:
        raise SvcNotFoundError(f"host '{host_name}'")

    # Refuse if active mappings exist
    maps_result = await session.execute(
        select(Mapping).where(Mapping.host_id == host.id)
    )
    if maps_result.scalars().first():
        raise SvcInvalidArgError(
            f"host '{host_name}' has active volume mappings; remove them first"
        )

    await session.delete(host)
    await session.commit()
    logger.info("rmhost: deleted host '%s'", host_name)
    return ""


async def _addhostport(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask addhostport -host <host> (-iscsiname <iqn> | -fcwwpn <wwpn>)"""
    host_name = require_flag(pc, "host")
    iscsiname = pc.flags.get("iscsiname", "")
    fcwwpn = pc.flags.get("fcwwpn", "")

    if not iscsiname and not fcwwpn:
        raise SvcInvalidArgError("either -iscsiname or -fcwwpn is required")

    session = ctx.session
    result = await session.execute(select(Host).where(Host.name == host_name))
    host = result.scalar_one_or_none()
    if host is None:
        raise SvcNotFoundError(f"host '{host_name}'")

    if iscsiname:
        existing = _host_iqns(host)
        if iscsiname not in existing:
            existing.append(iscsiname)
        host.iqn = ",".join(existing)
    elif fcwwpn:
        existing_wwpns = [w.strip() for w in (host.nqn or "").split(",") if w.strip()]
        if fcwwpn not in existing_wwpns:
            existing_wwpns.append(fcwwpn)
        host.nqn = ",".join(existing_wwpns)

    await session.commit()
    logger.info("addhostport: host '%s' iqn=%s wwpn=%s", host_name, iscsiname, fcwwpn)
    return ""


async def _mkvdiskhostmap(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask mkvdiskhostmap -host <host> <vdisk_name>"""
    host_name = require_flag(pc, "host")
    if not pc.positional:
        raise SvcInvalidArgError("mkvdiskhostmap requires a vdisk name argument")
    vdisk_name = pc.positional[0]
    session = ctx.session

    # Locate volume by name within this subsystem
    vol_result = await session.execute(
        select(Volume).where(
            Volume.name == vdisk_name,
            Volume.subsystem_id == ctx.subsystem_id,
        )
    )
    volume = vol_result.scalar_one_or_none()
    if volume is None:
        raise SvcNotFoundError(f"vdisk '{vdisk_name}'")

    if volume.status not in (VolumeStatus.available, VolumeStatus.in_use):
        raise SvcInvalidArgError(
            f"vdisk '{vdisk_name}' is not in a mappable state (status: {volume.status})"
        )

    # Locate host by name (global)
    host_result = await session.execute(select(Host).where(Host.name == host_name))
    host = host_result.scalar_one_or_none()
    if host is None:
        raise SvcNotFoundError(f"host '{host_name}'")

    # Duplicate-mapping guard (within subsystem)
    dup_result = await session.execute(
        select(Mapping).where(
            Mapping.volume_id == volume.id,
            Mapping.host_id == host.id,
            Mapping.subsystem_id == ctx.subsystem_id,
        )
    )
    if dup_result.scalar_one_or_none():
        raise SvcAlreadyExistsError(
            f"mapping of vdisk '{vdisk_name}' to host '{host_name}'"
        )

    # Find or create iSCSI ExportContainer for this host in this subsystem
    ec_result = await session.execute(
        select(ExportContainer).where(
            ExportContainer.protocol == Protocol.iscsi,
            ExportContainer.host_id == host.id,
            ExportContainer.subsystem_id == ctx.subsystem_id,
        )
    )
    ec = ec_result.scalar_one_or_none()

    if ec is None:
        ec = ExportContainer(
            subsystem_id=ctx.subsystem_id,
            protocol=Protocol.iscsi,
            host_id=host.id,
            portal_ip=_settings.iscsi_portal_ip,
            portal_port=_settings.iscsi_portal_port,
        )
        session.add(ec)
        await session.flush()  # get ec.id
        ec.target_iqn = f"{_settings.iqn_prefix}:{ctx.subsystem_name}:{ec.id}"
        await session.flush()

        try:
            await asyncio.to_thread(ensure_iscsi_export, ctx.spdk, ec, _settings)
        except Exception as exc:
            await session.rollback()
            raise SvcError(f"SPDK error creating iSCSI export: {exc}") from exc

    # Allocate LUN ID
    existing_maps_result = await session.execute(
        select(Mapping).where(Mapping.export_container_id == ec.id)
    )
    used_luns = [
        m.lun_id
        for m in existing_maps_result.scalars().all()
        if m.lun_id is not None
    ]
    lun_id = allocate_lun(used_luns)

    # Persist Mapping record
    mapping = Mapping(
        subsystem_id=ctx.subsystem_id,
        volume_id=volume.id,
        host_id=host.id,
        export_container_id=ec.id,
        protocol=Protocol.iscsi,
        lun_id=lun_id,
        ns_id=None,
    )
    session.add(mapping)
    await session.flush()

    # Attach in SPDK
    try:
        await asyncio.to_thread(ensure_iscsi_mapping, ctx.spdk, mapping, volume, ec)
    except Exception as exc:
        await session.rollback()
        raise SvcError(f"SPDK error attaching LUN: {exc}") from exc

    volume.status = VolumeStatus.in_use
    await session.commit()
    logger.info(
        "mkvdiskhostmap: mapped vdisk '%s' → host '%s' lun=%d",
        vdisk_name, host_name, lun_id,
    )
    return f"Virtual Disk to Host mapping, id [{lun_id}], successfully created"


async def _rmvdiskhostmap(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask rmvdiskhostmap -host <host> <vdisk_name>"""
    host_name = require_flag(pc, "host")
    if not pc.positional:
        raise SvcInvalidArgError("rmvdiskhostmap requires a vdisk name argument")
    vdisk_name = pc.positional[0]
    session = ctx.session

    vol_result = await session.execute(
        select(Volume).where(
            Volume.name == vdisk_name,
            Volume.subsystem_id == ctx.subsystem_id,
        )
    )
    volume = vol_result.scalar_one_or_none()
    if volume is None:
        raise SvcNotFoundError(f"vdisk '{vdisk_name}'")

    host_result = await session.execute(select(Host).where(Host.name == host_name))
    host = host_result.scalar_one_or_none()
    if host is None:
        raise SvcNotFoundError(f"host '{host_name}'")

    map_result = await session.execute(
        select(Mapping).where(
            Mapping.volume_id == volume.id,
            Mapping.host_id == host.id,
            Mapping.subsystem_id == ctx.subsystem_id,
        )
    )
    mapping = map_result.scalar_one_or_none()
    if mapping is None:
        raise SvcNotFoundError(
            f"mapping of vdisk '{vdisk_name}' to host '{host_name}'"
        )

    ec = mapping.export_container

    # Remove SPDK iSCSI target (v0: delete whole target node)
    try:
        if ec.target_iqn:
            await asyncio.to_thread(iscsi_rpc.delete_target_node, ctx.spdk, ec.target_iqn)
    except Exception as exc:
        raise SvcError(f"SPDK error removing iSCSI target: {exc}") from exc

    await session.delete(mapping)

    # If no remaining mappings for this volume, set it back to available
    remaining_result = await session.execute(
        select(Mapping).where(Mapping.volume_id == volume.id)
    )
    if not remaining_result.scalars().first():
        volume.status = VolumeStatus.available

    await session.commit()
    logger.info("rmvdiskhostmap: unmapped vdisk '%s' from host '%s'", vdisk_name, host_name)
    return ""


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------

SVCINFO_HANDLERS: dict[str, object] = {
    "lssystem": _lssystem,
    "lsmdiskgrp": _lsmdiskgrp,
    "lsvdisk": _lsvdisk,
    "lshost": _lshost,
    "lshostvdiskmap": _lshostvdiskmap,
    "lsvdiskhostmap": _lsvdiskhostmap,
}

SVCTASK_HANDLERS: dict[str, object] = {
    "mkvdisk": _mkvdisk,
    "rmvdisk": _rmvdisk,
    "expandvdisksize": _expandvdisksize,
    "mkhost": _mkhost,
    "rmhost": _rmhost,
    "addhostport": _addhostport,
    "mkvdiskhostmap": _mkvdiskhostmap,
    "rmvdiskhostmap": _rmvdiskhostmap,
}
