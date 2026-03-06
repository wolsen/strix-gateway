# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""IBM SVC command implementations.

Each handler is an ``async`` function with the signature::

    async def _<name>(ctx: SvcContext, pc: ParsedCommand) -> str

It returns the text that should be written to stdout (may be empty for
``svctask`` commands that succeed silently).  On failure it raises a
:class:`~apollo_gateway.compat.ibm_svc.errors.SvcError` subclass.

All query handlers filter by ``ctx.array_id`` so that two arrays
may share pool/volume names without collision.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional, TextIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.config import settings as _settings
from apollo_gateway.core.db import Host, Mapping, Pool, TransportEndpoint, Volume
from apollo_gateway.core.models import DesiredState, Protocol, VolumeStatus
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

    All handlers must filter DB queries by ``array_id`` to provide
    isolation between arrays.
    """

    session: AsyncSession
    spdk: SPDKClient
    array_id: str
    array_name: str
    effective_profile: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mb_to_gb_str(size_mb: int) -> str:
    """Convert MiB to a ``X.XXGB`` string as IBM SVC displays it."""
    return f"{size_mb / 1024:.2f}GB"


def _mb_to_tb_str(size_mb: int) -> str:
    """Convert MiB to a ``X.XXTB`` string as IBM SVC displays it."""
    return f"{size_mb / (1024 * 1024):.2f}TB"


def _mb_to_mb_str(size_mb: int) -> str:
    """Convert MiB to a ``X.XXMB`` string as IBM SVC displays it."""
    return f"{size_mb:.2f}MB"


def _feature(ctx: SvcContext, name: str, default: bool = False) -> bool:
    """Read a boolean feature flag from the effective capability profile."""
    return ctx.effective_profile.get("features", {}).get(name, default)


def _volume_status(status: str) -> str:
    """Map Apollo VolumeStatus → IBM SVC online/offline."""
    return "online" if status in (
        VolumeStatus.available,
        VolumeStatus.in_use,
        VolumeStatus.extending,
    ) else "offline"


def _host_iqns(host: Host) -> list[str]:
    """Return all IQNs stored in host.initiators_iscsi_iqns (JSON list)."""
    raw = host.initiators_iscsi_iqns
    if not raw:
        return []
    return _json.loads(raw) if isinstance(raw, str) else raw


def _host_wwpns(host: Host) -> list[str]:
    """Return all FC WWPNs stored in host.initiators_fc_wwpns (JSON list)."""
    raw = host.initiators_fc_wwpns
    if not raw:
        return []
    return _json.loads(raw) if isinstance(raw, str) else raw


# ---------------------------------------------------------------------------
# svcinfo handlers
# ---------------------------------------------------------------------------

async def _lssystem(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lssystem — gateway identity with capability profile fields."""
    delim = pc.delim or "!"
    session = ctx.session
    profile = ctx.effective_profile
    version = profile.get("version", "8.4.0.0")
    model = profile.get("model", "apollo-gateway")

    # Aggregate capacity from pools/volumes in this array
    result = await session.execute(
        select(Pool).where(Pool.array_id == ctx.array_id)
    )
    pools = result.scalars().all()
    total_capacity_mb = sum(p.size_mb or 0 for p in pools)
    allocated_mb = sum(v.size_mb for p in pools for v in p.volumes)
    free_mb = total_capacity_mb - allocated_mb
    overallocation = str(int(allocated_mb / total_capacity_mb * 100)) if total_capacity_mb > 0 else "0"

    fields = {
        "id": "0",
        "name": ctx.array_name,
        "location": "local",
        "partnership": "",
        "total_mdisk_capacity": _mb_to_tb_str(total_capacity_mb),
        "space_in_mdisk_grps": _mb_to_tb_str(total_capacity_mb),
        "space_allocated_to_vdisks": _mb_to_tb_str(allocated_mb),
        "total_free_space": _mb_to_tb_str(free_mb),
        "total_vdiskcopy_capacity": _mb_to_tb_str(allocated_mb),
        "total_used_capacity": _mb_to_tb_str(allocated_mb),
        "total_overallocation": overallocation,
        "total_vdisk_capacity": _mb_to_tb_str(allocated_mb),
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

    compression = _feature(ctx, "compression")
    easy_tier = _feature(ctx, "easy_tier")

    if name_or_id is not None:
        result = await session.execute(
            select(Pool).where(Pool.name == name_or_id, Pool.array_id == ctx.array_id)
        )
        pool = result.scalar_one_or_none()
        if pool is None:
            raise SvcNotFoundError(f"mdiskgrp '{name_or_id}'")
        pool_cap_mb = pool.size_mb or 0
        used_mb = sum(v.size_mb for v in pool.volumes)
        free_mb = pool_cap_mb - used_mb
        overallocation = str(int(used_mb / pool_cap_mb * 100)) if pool_cap_mb > 0 else "0"
        fields = {
            "id": pool.id,
            "name": pool.name,
            "status": "online",
            "mdisk_count": "1",
            "vdisk_count": str(len(pool.volumes)),
            "capacity": _mb_to_gb_str(pool_cap_mb),
            "extent_size": "256",
            "free_capacity": _mb_to_gb_str(free_mb),
            "virtual_capacity": _mb_to_gb_str(used_mb),
            "used_capacity": _mb_to_gb_str(used_mb),
            "real_capacity": _mb_to_gb_str(used_mb),
            "overallocation": overallocation,
            "warning": "0",
            "easy_tier": "on" if easy_tier else "off",
            "easy_tier_status": "balanced" if easy_tier else "inactive",
            "compression_active": "yes" if compression else "no",
            "compression_virtual_capacity": _mb_to_mb_str(used_mb) if compression else "0.00MB",
            "compression_compressed_capacity": _mb_to_mb_str(used_mb) if compression else "0.00MB",
            "compression_uncompressed_capacity": _mb_to_mb_str(used_mb) if compression else "0.00MB",
        }
        return format_delim(fields, delim)

    # List pools in this array only
    result = await session.execute(
        select(Pool).where(Pool.array_id == ctx.array_id)
    )
    pools = result.scalars().all()
    rows = []
    for p in pools:
        pool_cap_mb = p.size_mb or 0
        used_mb = sum(v.size_mb for v in p.volumes)
        free_mb = pool_cap_mb - used_mb
        rows.append({
            "id": p.id,
            "name": p.name,
            "status": "online",
            "mdisk_count": "1",
            "vdisk_count": str(len(p.volumes)),
            "capacity": _mb_to_gb_str(pool_cap_mb),
            "extent_size": "256",
            "free_capacity": _mb_to_gb_str(free_mb),
            "virtual_capacity": _mb_to_gb_str(used_mb),
            "compression_active": "yes" if compression else "no",
        })
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
                Volume.array_id == ctx.array_id,
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

    # List volumes in this array only
    result = await session.execute(
        select(Volume).where(Volume.array_id == ctx.array_id)
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

    Hosts are global (not array-scoped) per v0 design.
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
        wwpns = _host_wwpns(host)
        total_ports = len(iqns) + len(wwpns)
        fields = {
            "id": host.id,
            "name": host.name,
            "port_count": str(total_ports),
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
        for i, wwpn in enumerate(wwpns):
            fields[f"WWPN_{i}"] = wwpn
        if not wwpns:
            fields["WWPN"] = ""
        return format_delim(fields, delim)

    # List all hosts (global)
    result = await session.execute(select(Host))
    hosts = result.scalars().all()
    rows = []
    for h in hosts:
        iqns = _host_iqns(h)
        wwpns = _host_wwpns(h)
        total_ports = len(iqns) + len(wwpns)
        rows.append({
            "id": h.id,
            "name": h.name,
            "port_count": str(total_ports),
            "iscsi_name": iqns[0] if iqns else "",
            "WWPN": wwpns[0] if wwpns else "",
            "status": "online",
            "type": "generic",
        })
    return format_table(rows)


async def _lsportfc(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsportfc [-delim <d>] [-filtervalue …]

    Returns the FC target ports available on this array.  The Cinder
    Storwize FC driver calls this to discover target WWPNs for zoning.
    We return one row per WWPN listed in the array's FC transport
    endpoint.
    """
    session = ctx.session
    delim = pc.delim
    filtervalue = pc.flags.get("filtervalue", "")

    # Find all FC endpoints for this array
    ep_result = await session.execute(
        select(TransportEndpoint).where(
            TransportEndpoint.protocol == Protocol.fc,
            TransportEndpoint.array_id == ctx.array_id,
        )
    )
    fc_endpoints = ep_result.scalars().all()

    rows = []
    port_idx = 0
    for ep in fc_endpoints:
        targets = _json.loads(ep.targets) if isinstance(ep.targets, str) else ep.targets
        for wwpn in targets.get("target_wwpns", []):
            row = {
                "id": str(port_idx),
                "fc_io_port_id": str(port_idx),
                "port_id": str(port_idx),
                "type": "fc",
                "port_speed": "32Gb",
                "node_id": "1",
                "node_name": f"node{port_idx}",
                "WWPN": wwpn,
                "nportid": "010000",
                "status": "active",
                "attachment": "switch",
                "adapter_location": "0",
                "adapter_port_id": "0",
            }
            # Apply -filtervalue if present (e.g. "status:active")
            if filtervalue:
                include = True
                for clause in filtervalue.split(":"):
                    pass  # Cinder only uses status:active — accept all
                if not include:
                    continue
            rows.append(row)
            port_idx += 1

    if delim is not None and len(rows) == 1:
        return format_delim(rows[0], delim)
    return format_table(rows)


async def _lsfabric(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsfabric -host <host_name> [-delim <d>]

    Returns the FC fabric connections between a host's WWPN(s) and the
    array's target port WWPNs.  The Cinder Storwize FC driver calls this
    during ``initialize_connection`` to build the list of
    target-WWPNs-per-initiator.
    """
    host_name = pc.flags.get("host", "")
    delim = pc.delim
    session = ctx.session

    if not host_name:
        raise SvcInvalidArgError("lsfabric requires -host <host_name>")

    # Look up host
    host_result = await session.execute(select(Host).where(Host.name == host_name))
    host = host_result.scalar_one_or_none()
    if host is None:
        raise SvcNotFoundError(f"host '{host_name}'")

    host_wwpns = _host_wwpns(host)

    # Get all FC target port WWPNs for this array
    ep_result = await session.execute(
        select(TransportEndpoint).where(
            TransportEndpoint.protocol == Protocol.fc,
            TransportEndpoint.array_id == ctx.array_id,
        )
    )
    fc_endpoints = ep_result.scalars().all()
    target_wwpns: list[str] = []
    for ep in fc_endpoints:
        targets = _json.loads(ep.targets) if isinstance(ep.targets, str) else ep.targets
        target_wwpns.extend(targets.get("target_wwpns", []))

    # Produce a cross-product fabric entry: each host WWPN ↔ each target WWPN
    rows = []
    for h_wwpn in host_wwpns:
        for t_wwpn in target_wwpns:
            rows.append({
                "remote_wwpn": h_wwpn,
                "remote_nportid": "020000",
                "name": host.name,
                "host_name": host.name,
                "local_wwpn": t_wwpn,
                "local_port": "1",
                "local_nportid": "010000",
                "state": "active",
                "type": "host",
            })

    if delim is not None and len(rows) == 1:
        return format_delim(rows[0], delim)
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
        select(Mapping)
        .join(Volume, Mapping.volume_id == Volume.id)
        .where(
            Mapping.host_id == host.id,
            Volume.array_id == ctx.array_id,
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
            Volume.array_id == ctx.array_id,
        )
    )
    volume = vol_result.scalar_one_or_none()
    if volume is None:
        raise SvcNotFoundError(f"vdisk '{vdisk_name}'")

    maps_result = await session.execute(
        select(Mapping).where(
            Mapping.volume_id == volume.id,
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

    # Check volume name uniqueness within this array
    dup_result = await session.execute(
        select(Volume).where(
            Volume.name == name,
            Volume.array_id == ctx.array_id,
        )
    )
    if dup_result.scalar_one_or_none():
        raise SvcAlreadyExistsError(f"vdisk '{name}'")

    # Find pool by name within this array
    pool_result = await session.execute(
        select(Pool).where(
            Pool.name == mdiskgrp,
            Pool.array_id == ctx.array_id,
        )
    )
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise SvcNotFoundError(f"mdiskgrp '{mdiskgrp}'")

    # Create Volume record
    volume = Volume(
        name=name,
        array_id=ctx.array_id,
        pool_id=pool.id,
        size_mb=size_mb,
        status=VolumeStatus.creating,
    )
    session.add(volume)
    await session.flush()  # get volume.id

    # Provision in SPDK
    try:
        bdev_name = await asyncio.to_thread(
            ensure_lvol, ctx.spdk, volume, pool.name, ctx.array_name
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
            Volume.array_id == ctx.array_id,
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
            Volume.array_id == ctx.array_id,
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

    host = Host(
        name=name,
        initiators_iscsi_iqns="[]",
        initiators_nvme_host_nqns="[]",
        initiators_fc_wwpns="[]",
    )
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
        host.initiators_iscsi_iqns = _json.dumps(existing)
    elif fcwwpn:
        existing_wwpns = _host_wwpns(host)
        if fcwwpn not in existing_wwpns:
            existing_wwpns.append(fcwwpn)
        host.initiators_fc_wwpns = _json.dumps(existing_wwpns)

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

    # Locate volume by name within this array
    vol_result = await session.execute(
        select(Volume).where(
            Volume.name == vdisk_name,
            Volume.array_id == ctx.array_id,
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

    # Duplicate-mapping guard (within array)
    dup_result = await session.execute(
        select(Mapping).where(
            Mapping.volume_id == volume.id,
            Mapping.host_id == host.id,
        )
    )
    if dup_result.scalar_one_or_none():
        raise SvcAlreadyExistsError(
            f"mapping of vdisk '{vdisk_name}' to host '{host_name}'"
        )

    # --- Resolve endpoints (FC-aware) ---
    # If the host has FC WWPNs and the array has an FC endpoint, use
    # FC-persona-over-iSCSI-underlay.  Otherwise fall back to iSCSI-only.
    host_wwpns = _host_wwpns(host)

    fc_ep = None
    if host_wwpns:
        fc_result = await session.execute(
            select(TransportEndpoint).where(
                TransportEndpoint.protocol == Protocol.fc,
                TransportEndpoint.array_id == ctx.array_id,
            )
        )
        fc_ep = fc_result.scalar_one_or_none()

    # Find or create iSCSI TransportEndpoint for this array (always needed
    # as the underlay — or as persona+underlay when no FC endpoint exists)
    iscsi_ep_result = await session.execute(
        select(TransportEndpoint).where(
            TransportEndpoint.protocol == Protocol.iscsi,
            TransportEndpoint.array_id == ctx.array_id,
        )
    )
    iscsi_ep = iscsi_ep_result.scalar_one_or_none()

    if iscsi_ep is None:
        target_iqn = f"{_settings.iqn_prefix}:{ctx.array_name}"
        iscsi_ep = TransportEndpoint(
            array_id=ctx.array_id,
            protocol=Protocol.iscsi.value,
            targets=_json.dumps({"target_iqn": target_iqn}),
            addresses=_json.dumps({
                "portals": [f"{_settings.iscsi_portal_ip}:{_settings.iscsi_portal_port}"],
            }),
            auth=_json.dumps({"method": "none"}),
        )
        session.add(iscsi_ep)
        await session.flush()

        try:
            await asyncio.to_thread(ensure_iscsi_export, ctx.spdk, iscsi_ep, _settings)
        except Exception as exc:
            await session.rollback()
            raise SvcError(f"SPDK error creating iSCSI export: {exc}") from exc

    # Persona endpoint: FC endpoint if available, otherwise iSCSI
    persona_ep = fc_ep if fc_ep is not None else iscsi_ep
    # Underlay endpoint: always iSCSI
    underlay_ep = iscsi_ep

    # Allocate LUN ID (per host + persona endpoint)
    existing_maps_result = await session.execute(
        select(Mapping).where(
            Mapping.host_id == host.id,
            Mapping.persona_endpoint_id == persona_ep.id,
        )
    )
    used_luns = [
        m.lun_id
        for m in existing_maps_result.scalars().all()
        if m.lun_id is not None
    ]
    lun_id = allocate_lun(used_luns)

    # Allocate underlay ID (LUN on iSCSI target)
    underlay_maps_result = await session.execute(
        select(Mapping).where(Mapping.underlay_endpoint_id == underlay_ep.id)
    )
    used_underlay_ids = [
        m.underlay_id
        for m in underlay_maps_result.scalars().all()
        if m.underlay_id is not None
    ]
    underlay_id = allocate_lun(used_underlay_ids)

    # Persist Mapping record
    mapping = Mapping(
        volume_id=volume.id,
        host_id=host.id,
        persona_endpoint_id=persona_ep.id,
        underlay_endpoint_id=underlay_ep.id,
        lun_id=lun_id,
        underlay_id=underlay_id,
        desired_state=DesiredState.attached,
        revision=1,
    )
    session.add(mapping)
    await session.flush()

    # Attach in SPDK (always iSCSI underlay)
    try:
        await asyncio.to_thread(ensure_iscsi_mapping, ctx.spdk, mapping, volume, underlay_ep)
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
            Volume.array_id == ctx.array_id,
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
        )
    )
    mapping = map_result.scalar_one_or_none()
    if mapping is None:
        raise SvcNotFoundError(
            f"mapping of vdisk '{vdisk_name}' to host '{host_name}'"
        )

    underlay_ep = mapping.underlay_endpoint
    underlay_targets = _json.loads(underlay_ep.targets) if isinstance(underlay_ep.targets, str) else underlay_ep.targets

    # Remove SPDK iSCSI target
    try:
        target_iqn = underlay_targets.get("target_iqn", "")
        if target_iqn:
            await asyncio.to_thread(iscsi_rpc.delete_target_node, ctx.spdk, target_iqn)
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
    "lsportfc": _lsportfc,
    "lsfabric": _lsfabric,
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


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def dispatch(
    cmd_str: str,
    ctx: SvcContext,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    """Parse *cmd_str*, call the matching handler, write output, return exit code.

    Parameters
    ----------
    cmd_str:
        Raw SVC command string (e.g. ``"svcinfo lssystem"``).
    ctx:
        Pre-initialised :class:`SvcContext` carrying a live
        ``AsyncSession`` and ``SPDKClient``.
    stdout:
        Stream for normal output.  Defaults to ``sys.stdout``.
    stderr:
        Stream for error output.  Defaults to ``sys.stderr``.

    Returns
    -------
    int
        Exit code: ``0`` on success, ``1`` on any error.
    """
    from apollo_gateway.compat.ibm_svc.parse import parse_ssh_command

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    try:
        pc = parse_ssh_command(cmd_str)
    except SvcError as exc:
        print(str(exc), file=err)
        return exc.exit_code

    if pc.verb == "svcinfo":
        table = SVCINFO_HANDLERS
    else:
        table = SVCTASK_HANDLERS

    handler = table.get(pc.subcommand)
    if handler is None:
        exc = SvcUnknownCommandError(f"{pc.verb} {pc.subcommand}")
        print(str(exc), file=err)
        return exc.exit_code

    try:
        output = await handler(ctx, pc)  # type: ignore[operator]
        if output:
            print(output, file=out)
        return 0
    except SvcError as exc:
        print(str(exc), file=err)
        return exc.exit_code
    except Exception as exc:
        logger.exception("Unhandled error in handler %s %s", pc.verb, pc.subcommand)
        print(f"Internal error: {exc}", file=err)
        return 1
