# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""IBM SVC command implementations.

Each handler is an ``async`` function with the signature::

    async def _<name>(ctx: SvcContext, pc: ParsedCommand) -> str

It returns the text that should be written to stdout (may be empty for
``svctask`` commands that succeed silently).  On failure it raises a
:class:`~strix_gateway.personalities.svc.errors.SvcError` subclass.

All query handlers filter by ``ctx.array_id`` so that two arrays
may share pool/volume names without collision.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Optional, TextIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.config import settings as _settings
from strix_gateway.core.db import Host, Mapping, Pool, TransportEndpoint, Volume
from strix_gateway.core.exceptions import (
    AlreadyExistsError,
    BackendError,
    CoreError,
    InvalidStateError,
    NotFoundError,
    ResourceInUseError,
)
from strix_gateway.core.models import Protocol, VolumeStatus
from strix_gateway.core import (
    endpoints as endpoints_svc,
    hosts as hosts_svc,
    mappings as mappings_svc,
    volumes as volumes_svc,
)
from strix_gateway.spdk.rpc import SPDKClient

from strix_gateway.personalities.svc.errors import (
    SvcAlreadyExistsError,
    SvcError,
    SvcInvalidArgError,
    SvcNotFoundError,
    SvcUnknownCommandError,
)
from strix_gateway.personalities.svc.format import format_delim, format_table
from strix_gateway.personalities.svc.parse import ParsedCommand, optional_flag, require_flag

logger = logging.getLogger("strix_gateway.personalities.svc")


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


def _mb_to_bytes_str(size_mb: int) -> str:
    """Convert MiB to raw bytes string (for ``-bytes`` flag)."""
    return str(size_mb * 1024 * 1024)


def _feature(ctx: SvcContext, name: str, default: bool = False) -> bool:
    """Read a boolean feature flag from the effective capability profile."""
    return ctx.effective_profile.get("features", {}).get(name, default)


def _volume_status(status: str) -> str:
    """Map Strix VolumeStatus → IBM SVC online/offline."""
    return "online" if status in (
        VolumeStatus.available,
        VolumeStatus.in_use,
        VolumeStatus.extending,
    ) else "offline"


def _host_iqns(host: Host) -> list[str]:
    """Return all IQNs stored in host.initiators_iscsi_iqns."""
    return host.iscsi_iqns


def _host_wwpns(host: Host) -> list[str]:
    """Return all FC WWPNs stored in host.initiators_fc_wwpns."""
    return host.fc_wwpns


def _core_to_svc(exc: CoreError) -> SvcError:
    """Translate a CoreError into the closest SvcError subclass."""
    if isinstance(exc, NotFoundError):
        return SvcNotFoundError(str(exc))
    if isinstance(exc, AlreadyExistsError):
        return SvcAlreadyExistsError(str(exc))
    if isinstance(exc, (InvalidStateError, ResourceInUseError)):
        return SvcInvalidArgError(str(exc))
    if isinstance(exc, BackendError):
        return SvcError(str(exc))
    return SvcError(str(exc))


async def _ensure_iscsi_endpoint(session: AsyncSession, ctx: SvcContext) -> TransportEndpoint:
    """Find or create an iSCSI TransportEndpoint for the context's array.

    SVC behaviour: the first ``mkvdiskhostmap`` auto-provisions an iSCSI
    transport endpoint when one does not yet exist.  This is a vendor-specific
    convenience — the core mapping service requires endpoints to be
    pre-created.
    """
    import json as _json

    result = await session.execute(
        select(TransportEndpoint).where(
            TransportEndpoint.protocol == Protocol.iscsi,
            TransportEndpoint.array_id == ctx.array_id,
        )
    )
    ep = result.scalar_one_or_none()
    if ep is not None:
        return ep

    target_iqn = f"{_settings.iqn_prefix}:{ctx.array_name}"
    ep = TransportEndpoint(
        array_id=ctx.array_id,
        protocol=Protocol.iscsi.value,
        targets=_json.dumps({"target_iqn": target_iqn}),
        addresses=_json.dumps({
            "portals": [f"{_settings.iscsi_portal_ip}:{_settings.iscsi_portal_port}"],
        }),
        auth=_json.dumps({"method": "none"}),
    )
    session.add(ep)
    await session.flush()

    # Wire the SPDK iSCSI export for the new endpoint
    import asyncio
    from strix_gateway.spdk.ensure import ensure_iscsi_export

    try:
        await asyncio.to_thread(ensure_iscsi_export, ctx.spdk, ep, _settings)
    except Exception as exc:
        await session.rollback()
        raise SvcError(f"SPDK error creating iSCSI export: {exc}") from exc

    return ep


# ---------------------------------------------------------------------------
# svcinfo handlers
# ---------------------------------------------------------------------------

async def _lssystem(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lssystem — gateway identity with capability profile fields."""
    delim = pc.delim or "!"
    session = ctx.session
    profile = ctx.effective_profile
    version = profile.get("version", "8.4.0.0")
    model = profile.get("model", "strix-gateway")

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
        "topology": "standard",
        "iscsi_auth_method": "none",
        "iscsi_chap_secret": "",
    }
    return format_delim(fields, delim)


async def _lslicense(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lslicense [-delim <d>] — license information."""
    delim = pc.delim or "!"
    profile = ctx.effective_profile
    compression = _feature(ctx, "compression")
    fields = {
        "used_flash": "0",
        "used_remote": "0",
        "used_virtualization": "0",
        "license_compression_enclosures": "1" if compression else "0",
        "license_compression_capacity": "0",
    }
    return format_delim(fields, delim)


async def _lsguicapabilities(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsguicapabilities [-delim <d>] — GUI capability flags."""
    delim = pc.delim or "!"
    fields = {
        "license_scheme": "lnx",
        "product_key": "",
    }
    return format_delim(fields, delim)


async def _lsiogrp(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsiogrp [-delim <d>] — list IO groups."""
    delim = pc.delim or "!"
    rows = [
        {
            "id": "0",
            "name": "io_grp0",
            "node_count": "4",
            "vdisk_count": "0",
            "host_count": "0",
        },
    ]
    return format_table(rows, delim)


async def _lsnode(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsnode [<node>] [-delim <d>] — list or show storage nodes."""
    session = ctx.session
    delim = pc.delim or "!"
    node_id: Optional[str] = pc.positional[0] if pc.positional else None

    # Resolve the iSCSI target IQN from this array's endpoint
    ep_result = await session.execute(
        select(TransportEndpoint).where(
            TransportEndpoint.protocol == Protocol.iscsi,
            TransportEndpoint.array_id == ctx.array_id,
        )
    )
    ep = ep_result.scalar_one_or_none()
    iscsi_name = ""
    if ep:
        iscsi_name = ep.targets_dict.get("target_iqn", "")

    node = {
        "id": "1",
        "name": "node1",
        "UPS_serial_number": "",
        "WWNN": "5005076400C0A000",
        "status": "online",
        "IO_group_id": "0",
        "IO_group_name": "io_grp0",
        "config_node": "yes",
        "UPS_unique_id": "",
        "iscsi_name": iscsi_name,
        "iscsi_alias": "",
        "panel_name": "01-1",
        "enclosure_id": "1",
        "canister_id": "1",
        "enclosure_serial_number": "",
        "site_id": "",
        "site_name": "",
    }

    if node_id is not None:
        return format_delim(node, delim)

    return format_table([node], delim)


async def _lsip(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsip [-delim <d>] [-filtervalue portset_name=X] — IP addresses."""
    session = ctx.session
    delim = pc.delim or "!"
    filtervalue = pc.flags.get("filtervalue", "")

    # Parse portset filter
    filter_portset = None
    if filtervalue:
        for part in filtervalue.split(":"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k == "portset_name":
                    filter_portset = v

    # Resolve iSCSI portal IP from endpoint
    ep_result = await session.execute(
        select(TransportEndpoint).where(
            TransportEndpoint.protocol == Protocol.iscsi,
            TransportEndpoint.array_id == ctx.array_id,
        )
    )
    ep = ep_result.scalar_one_or_none()
    ip_addr = _settings.iscsi_portal_ip
    if ep:
        addrs = ep.addresses_dict
        portals = addrs.get("portals", [])
        if portals:
            # Extract IP from "ip:port" format
            ip_addr = portals[0].rsplit(":", 1)[0]

    row = {
        "id": "0",
        "node_id": "1",
        "node_name": "node1",
        "IP_address": ip_addr,
        "mask": "255.255.255.0",
        "gateway": "",
        "portset_id": "0",
        "portset_name": "portset0",
        "IP_address_6": "",
        "prefix_6": "",
    }

    if filter_portset and row["portset_name"] != filter_portset:
        return format_table([], delim)

    return format_table([row], delim)


async def _lstargetportfc(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lstargetportfc [-delim <d>] [-filtervalue …] — FC target ports."""
    session = ctx.session
    delim = pc.delim or "!"

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
        targets = ep.targets_dict
        for wwpn in targets.get("target_wwpns", []):
            rows.append({
                "id": str(port_idx),
                "fc_io_port_id": str(port_idx),
                "current_node_id": "1",
                "current_node_name": "node1",
                "WWPN": wwpn,
                "host_io_permitted": "yes",
            })
            port_idx += 1

    return format_table(rows, delim)


async def _lsfcportsetmember(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsfcportsetmember [-delim <d>] — FC portset members."""
    session = ctx.session
    delim = pc.delim or "!"

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
        targets = ep.targets_dict
        for _wwpn in targets.get("target_wwpns", []):
            rows.append({
                "id": str(port_idx),
                "fc_io_port_id": str(port_idx),
                "portset_id": "0",
                "portset_name": "portset64",
            })
            port_idx += 1

    return format_table(rows, delim)


async def _lsmdiskgrp(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsmdiskgrp [<pool_name>] [-delim <d>] [-bytes]"""
    session = ctx.session
    name_or_id: Optional[str] = pc.positional[0] if pc.positional else None
    delim = pc.delim or "!"
    use_bytes = "bytes" in pc.flags

    compression = _feature(ctx, "compression")
    easy_tier = _feature(ctx, "easy_tier")

    cap_fmt = _mb_to_bytes_str if use_bytes else _mb_to_gb_str

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
            "capacity": cap_fmt(pool_cap_mb),
            "extent_size": "256",
            "free_capacity": cap_fmt(free_mb),
            "virtual_capacity": cap_fmt(used_mb),
            "used_capacity": cap_fmt(used_mb),
            "real_capacity": cap_fmt(used_mb),
            "overallocation": overallocation,
            "warning": "0",
            "easy_tier": "on" if easy_tier else "off",
            "easy_tier_status": "balanced" if easy_tier else "inactive",
            "compression_active": "yes" if compression else "no",
            "compression_virtual_capacity": _mb_to_mb_str(used_mb) if compression else "0.00MB",
            "compression_compressed_capacity": _mb_to_mb_str(used_mb) if compression else "0.00MB",
            "compression_uncompressed_capacity": _mb_to_mb_str(used_mb) if compression else "0.00MB",
            "site_id": "",
            "site_name": "",
            "data_reduction": "no",
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
            "capacity": cap_fmt(pool_cap_mb),
            "extent_size": "256",
            "free_capacity": cap_fmt(free_mb),
            "virtual_capacity": cap_fmt(used_mb),
            "compression_active": "yes" if compression else "no",
        })
    return format_table(rows, delim)


async def _lsvdisk(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsvdisk [<vdisk_name>] [-delim <d>] [-bytes]"""
    session = ctx.session
    name_or_id: Optional[str] = pc.positional[0] if pc.positional else None
    delim = pc.delim or "!"
    cap_fmt = _mb_to_bytes_str if "bytes" in pc.flags else _mb_to_gb_str

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
            "capacity": cap_fmt(volume.size_mb),
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
            "capacity": cap_fmt(v.size_mb),
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
    return format_table(rows, delim)


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
    return format_table(rows, delim)


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
        targets = ep.targets_dict
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
    return format_table(rows, delim or "!")


async def _lsiscsiauth(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsiscsiauth [-delim <d>]

    Returns iSCSI CHAP authentication info for all hosts.
    """
    delim = pc.delim or "!"
    result = await ctx.session.execute(select(Host))
    all_hosts = result.scalars().all()

    rows: list[dict[str, str]] = []
    for h in all_hosts:
        rows.append({
            "name": h.name,
            "iscsi_auth_method": "none",
            "iscsi_chap_secret": "",
        })
    return format_table(rows, delim)


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
        targets = ep.targets_dict
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
    return format_table(rows, delim or "!")


async def _lshostvdiskmap(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lshostvdiskmap <host_name> [-delim <d>]"""
    if not pc.positional:
        raise SvcInvalidArgError("lshostvdiskmap requires a host name argument")
    host_name = pc.positional[0]
    delim = pc.delim or "!"
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
    return format_table(rows, delim)


async def _lsvdiskhostmap(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svcinfo lsvdiskhostmap <vdisk_name> [-delim <d>]"""
    if not pc.positional:
        raise SvcInvalidArgError("lsvdiskhostmap requires a vdisk name argument")
    vdisk_name = pc.positional[0]
    delim = pc.delim or "!"
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
    return format_table(rows, delim)


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

    try:
        volume = await volumes_svc.create_volume(
            session, ctx.spdk,
            name=name,
            pool_id=pool.id,
            size_mb=size_mb,
        )
    except CoreError as exc:
        raise _core_to_svc(exc) from exc

    await session.commit()
    logger.info("mkvdisk: created vdisk '%s' id=%s size=%dMiB", name, volume.id, size_mb)
    # SVC returns a numeric id; derive one from the UUID for CLI compatibility
    numeric_id = str(abs(hash(volume.id)) % 1000000)
    return f"Virtual Disk, id [{numeric_id}], successfully created"


async def _rmvdisk(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask rmvdisk <vdisk_name>"""
    if not pc.positional:
        raise SvcInvalidArgError("rmvdisk requires a vdisk name")
    vdisk_name = pc.positional[0]
    session = ctx.session

    try:
        volume = await volumes_svc.get_volume_by_name(session, vdisk_name, ctx.array_id)
        await volumes_svc.delete_volume(session, ctx.spdk, volume.id)
    except CoreError as exc:
        raise _core_to_svc(exc) from exc

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

    delta_mb = size_num * 1024
    session = ctx.session

    try:
        volume = await volumes_svc.get_volume_by_name(session, vdisk_name, ctx.array_id)
        await volumes_svc.expand_volume_by_delta(session, ctx.spdk, volume.id, delta_mb)
    except CoreError as exc:
        raise _core_to_svc(exc) from exc

    await session.commit()
    logger.info("expandvdisksize: expanded '%s' by %dMiB", vdisk_name, delta_mb)
    return ""


async def _mkhost(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask mkhost -name <host> [-iscsiname <iqn>] [-hbawwpn <wwpn>] [-force]"""
    name = require_flag(pc, "name")
    iscsiname = optional_flag(pc, "iscsiname", "")
    hbawwpn = optional_flag(pc, "hbawwpn", "")
    session = ctx.session

    try:
        host = await hosts_svc.create_host(session, name=name)
        # Attach initial port if provided with the host creation command
        if iscsiname:
            await hosts_svc.add_host_port(session, host.id, port_type="iscsi", port_value=iscsiname)
        if hbawwpn:
            await hosts_svc.add_host_port(session, host.id, port_type="fc", port_value=hbawwpn)
    except CoreError as exc:
        raise _core_to_svc(exc) from exc

    await session.commit()
    logger.info("mkhost: created host '%s' id=%s", name, host.id)
    numeric_id = str(abs(hash(host.id)) % 1000000)
    return f"Host, id [{numeric_id}], successfully created"


async def _rmhost(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask rmhost <host_name>"""
    if not pc.positional:
        raise SvcInvalidArgError("rmhost requires a host name")
    host_name = pc.positional[0]
    session = ctx.session

    try:
        host = await hosts_svc.get_host_by_name(session, host_name)
        await hosts_svc.delete_host(session, host.id)
    except CoreError as exc:
        raise _core_to_svc(exc) from exc

    await session.commit()
    logger.info("rmhost: deleted host '%s'", host_name)
    return ""


async def _addhostport(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask addhostport -force (-iscsiname <iqn> | -hbawwpn <wwpn>) <host>"""
    if not pc.positional:
        raise SvcInvalidArgError("addhostport requires a host name")
    host_name = pc.positional[0]
    iscsiname = pc.flags.get("iscsiname", "")
    hbawwpn = pc.flags.get("hbawwpn", "")

    if not iscsiname and not hbawwpn:
        raise SvcInvalidArgError("either -iscsiname or -hbawwpn is required")

    session = ctx.session

    try:
        host = await hosts_svc.get_host_by_name(session, host_name)
        if iscsiname:
            await hosts_svc.add_host_port(session, host.id, port_type="iscsi", port_value=iscsiname)
        elif hbawwpn:
            await hosts_svc.add_host_port(session, host.id, port_type="fc", port_value=hbawwpn)
    except CoreError as exc:
        raise _core_to_svc(exc) from exc

    await session.commit()
    logger.info("addhostport: host '%s' iqn=%s wwpn=%s", host_name, iscsiname, hbawwpn)
    return ""


async def _chhost(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask chhost [-chapsecret <secret>] [-site <site>] [-nosite] <host>

    Modifies host properties.  Gateway ignores the values but must accept
    the command so the Cinder SVC driver can set CHAP secrets.
    """
    # The command takes no-output on success (run_ssh_assert_no_output).
    return ""


async def _mkvdiskhostmap(ctx: SvcContext, pc: ParsedCommand) -> str:
    """svctask mkvdiskhostmap -host <host> <vdisk_name>"""
    host_name = require_flag(pc, "host")
    if not pc.positional:
        raise SvcInvalidArgError("mkvdiskhostmap requires a vdisk name argument")
    vdisk_name = pc.positional[0]
    session = ctx.session

    try:
        volume = await volumes_svc.get_volume_by_name(session, vdisk_name, ctx.array_id)
        host = await hosts_svc.get_host_by_name(session, host_name)

        # Duplicate-mapping guard
        existing = await mappings_svc.find_mapping_by_host_and_volume(
            session, host.id, volume.id,
        )
        if existing:
            raise AlreadyExistsError(
                "mapping", f"vdisk '{vdisk_name}' to host '{host_name}'"
            )

        # SVC-specific: auto-create iSCSI endpoint if none exists on the array.
        # The iSCSI endpoint is always needed (as underlay, or as both
        # persona + underlay when no FC endpoint is available).
        await _ensure_iscsi_endpoint(session, ctx)

        mapping = await mappings_svc.create_mapping(
            session, ctx.spdk, _settings,
            host_id=host.id,
            volume_id=volume.id,
            # FC-aware auto-resolution handled by core mappings
        )
    except CoreError as exc:
        raise _core_to_svc(exc) from exc

    await session.commit()
    lun_id = mapping.lun_id
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

    try:
        volume = await volumes_svc.get_volume_by_name(session, vdisk_name, ctx.array_id)
        host = await hosts_svc.get_host_by_name(session, host_name)

        mapping = await mappings_svc.find_mapping_by_host_and_volume(
            session, host.id, volume.id,
        )
        if mapping is None:
            raise NotFoundError(
                "mapping", f"vdisk '{vdisk_name}' to host '{host_name}'"
            )

        await mappings_svc.delete_mapping(session, ctx.spdk, mapping.id)
    except CoreError as exc:
        raise _core_to_svc(exc) from exc

    await session.commit()
    logger.info("rmvdiskhostmap: unmapped vdisk '%s' from host '%s'", vdisk_name, host_name)
    return ""


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------

SVCINFO_HANDLERS: dict[str, object] = {
    "lssystem": _lssystem,
    "lslicense": _lslicense,
    "lsguicapabilities": _lsguicapabilities,
    "lsiogrp": _lsiogrp,
    "lsnode": _lsnode,
    "lsip": _lsip,
    "lsmdiskgrp": _lsmdiskgrp,
    "lsvdisk": _lsvdisk,
    "lshost": _lshost,
    "lsiscsiauth": _lsiscsiauth,
    "lsportfc": _lsportfc,
    "lstargetportfc": _lstargetportfc,
    "lsfcportsetmember": _lsfcportsetmember,
    "lsfabric": _lsfabric,
    "lshostvdiskmap": _lshostvdiskmap,
    "lsvdiskhostmap": _lsvdiskhostmap,
}

SVCTASK_HANDLERS: dict[str, object] = {
    "mkvdisk": _mkvdisk,
    "rmvdisk": _rmvdisk,
    "expandvdisksize": _expandvdisksize,
    "mkhost": _mkhost,
    "chhost": _chhost,
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
    from strix_gateway.personalities.svc.parse import parse_ssh_command

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
