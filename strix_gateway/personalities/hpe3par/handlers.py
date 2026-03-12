# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""HPE 3PAR InForm OS CLI command implementations.

Each handler is an ``async`` function with the signature::

    async def _<name>(ctx: Hpe3parContext, pc: ParsedCommand) -> str

It returns the text that should be written to stdout (may be empty for
mutating commands that succeed silently).  On failure it raises a
:class:`~strix_gateway.personalities.hpe3par.errors.Hpe3parError`.

All query handlers filter by ``ctx.array_id`` for multi-array isolation.
"""

from __future__ import annotations

import json as _json
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional, TextIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.config import settings as _settings
from strix_gateway.core.db import Pool, TransportEndpoint, Volume
from strix_gateway.core.exceptions import (
    AlreadyExistsError,
    CoreError,
    NotFoundError,
)
from strix_gateway.core.models import Protocol, VolumeStatus
from strix_gateway.core import (
    endpoints as endpoints_svc,
    hosts as hosts_svc,
    mappings as mappings_svc,
    pools as pools_svc,
    volumes as volumes_svc,
)
from strix_gateway.spdk.rpc import SPDKClient

from strix_gateway.personalities.hpe3par.errors import (
    Hpe3parError,
    Hpe3parInvalidArgError,
    Hpe3parNotFoundError,
    Hpe3parUnknownCommandError,
    core_to_3par,
)
from strix_gateway.personalities.hpe3par.format import format_detail, format_table
from strix_gateway.personalities.hpe3par.parse import (
    ParsedCommand,
    optional_flag,
    parse_command,
    require_flag,
)

logger = logging.getLogger("strix_gateway.personalities.hpe3par")


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------

@dataclass
class Hpe3parContext:
    """Execution context passed to every handler.

    All handlers must filter DB queries by ``array_id`` for isolation.
    """

    session: AsyncSession
    spdk: SPDKClient
    array_id: str
    array_name: str
    effective_profile: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mb_to_mib_str(size_mb: int) -> str:
    return str(size_mb)


def _volume_state(status: str) -> str:
    """Map Strix VolumeStatus → 3PAR state string."""
    return "normal" if status in (
        VolumeStatus.available,
        VolumeStatus.in_use,
        VolumeStatus.extending,
    ) else "failed"


async def _ensure_iscsi_endpoint(session: AsyncSession, ctx: Hpe3parContext) -> TransportEndpoint:
    """Find or create an iSCSI TransportEndpoint for the array.

    Mirrors the SVC auto-provisioning pattern: the first ``createvlun``
    call ensures an iSCSI endpoint exists.
    """
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

    import asyncio
    from strix_gateway.spdk.ensure import ensure_iscsi_export

    try:
        await asyncio.to_thread(ensure_iscsi_export, ctx.spdk, ep, _settings)
    except Exception as exc:
        await session.rollback()
        raise Hpe3parError(f"SPDK error creating iSCSI export: {exc}") from exc

    return ep


# ---------------------------------------------------------------------------
# show* handlers (read-only)
# ---------------------------------------------------------------------------

async def _showsys(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """showsys — display system information and capacity."""
    session = ctx.session
    profile = ctx.effective_profile

    result = await session.execute(
        select(Pool).where(Pool.array_id == ctx.array_id)
    )
    pools = list(result.scalars().all())

    total_mb = sum(p.size_mb or 0 for p in pools)
    # Compute allocated by querying volumes
    vol_result = await session.execute(
        select(Volume).where(Volume.array_id == ctx.array_id)
    )
    volumes = list(vol_result.scalars().all())
    allocated_mb = sum(v.size_mb for v in volumes)
    free_mb = total_mb - allocated_mb

    fields = {
        "System Name": ctx.array_name,
        "System Model": profile.get("model", "3PAR-stub"),
        "Serial Number": str(abs(hash(ctx.array_id)) % 10000000),
        "System Version": profile.get("version", "3.3.1"),
        "Total Capacity MiB": str(total_mb),
        "Allocated Capacity MiB": str(allocated_mb),
        "Free Capacity MiB": str(free_mb),
        "Number of Nodes": "2",
    }
    return format_detail(fields)


async def _showcpg(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """showcpg [<cpg_name>] — list CPGs (pools) or show one."""
    session = ctx.session
    cpg_name = pc.positional[0] if pc.positional else None

    if cpg_name:
        result = await session.execute(
            select(Pool).where(
                Pool.name == cpg_name,
                Pool.array_id == ctx.array_id,
            )
        )
        pool = result.scalar_one_or_none()
        if pool is None:
            raise Hpe3parNotFoundError(f"cpg '{cpg_name}'")

        vol_result = await session.execute(
            select(Volume).where(Volume.pool_id == pool.id)
        )
        volumes = list(vol_result.scalars().all())
        used_mb = sum(v.size_mb for v in volumes)

        fields = {
            "Id": "0",
            "Name": pool.name,
            "Total MiB": str(pool.size_mb or 0),
            "Used MiB": str(used_mb),
            "Free MiB": str((pool.size_mb or 0) - used_mb),
            "Num VVs": str(len(volumes)),
        }
        return format_detail(fields)

    result = await session.execute(
        select(Pool).where(Pool.array_id == ctx.array_id)
    )
    pools = list(result.scalars().all())
    rows = []
    for idx, p in enumerate(pools):
        vol_result = await session.execute(
            select(Volume).where(Volume.pool_id == p.id)
        )
        volumes = list(vol_result.scalars().all())
        used_mb = sum(v.size_mb for v in volumes)
        rows.append({
            "Id": str(idx),
            "Name": p.name,
            "Total_MiB": str(p.size_mb or 0),
            "Used_MiB": str(used_mb),
            "Free_MiB": str((p.size_mb or 0) - used_mb),
            "Num_VVs": str(len(volumes)),
        })
    return format_table(rows)


async def _showvv(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """showvv [<vv_name>] — list volumes or show one."""
    session = ctx.session
    vv_name = pc.positional[0] if pc.positional else None

    if vv_name:
        try:
            volume = await volumes_svc.get_volume_by_name(session, vv_name, ctx.array_id)
        except CoreError as exc:
            raise core_to_3par(exc) from exc

        pool = await pools_svc.get_pool(session, volume.pool_id)
        fields = {
            "Id": str(abs(hash(volume.id)) % 100000),
            "Name": volume.name,
            "Prov": "tpvv",
            "Type": "base",
            "CPG": pool.name,
            "Size_MiB": str(volume.size_mb),
            "State": _volume_state(volume.status),
        }
        return format_detail(fields)

    result = await session.execute(
        select(Volume).where(Volume.array_id == ctx.array_id)
    )
    volumes = list(result.scalars().all())
    rows = []
    for idx, v in enumerate(volumes):
        pool = await pools_svc.get_pool(session, v.pool_id)
        rows.append({
            "Id": str(idx),
            "Name": v.name,
            "Prov": "tpvv",
            "Type": "base",
            "CPG": pool.name,
            "Size_MiB": str(v.size_mb),
            "State": _volume_state(v.status),
        })
    return format_table(rows)


async def _showhost(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """showhost [<host_name>] — list hosts or show one."""
    session = ctx.session
    host_name = pc.positional[0] if pc.positional else None

    if host_name:
        try:
            host = await hosts_svc.get_host_by_name(session, host_name)
        except CoreError as exc:
            raise core_to_3par(exc) from exc

        iqns = host.iscsi_iqns
        wwpns = host.fc_wwpns
        fields: dict = {
            "Id": str(abs(hash(host.id)) % 100000),
            "Name": host.name,
            "Persona": "5",
            "Num_FC_Paths": str(len(wwpns)),
            "Num_iSCSI_Paths": str(len(iqns)),
        }
        for i, wwpn in enumerate(wwpns):
            fields[f"FC_Path_{i}"] = wwpn
        for i, iqn in enumerate(iqns):
            fields[f"iSCSI_Path_{i}"] = iqn
        return format_detail(fields)

    hosts = await hosts_svc.list_hosts(session)
    rows = []
    for idx, h in enumerate(hosts):
        rows.append({
            "Id": str(idx),
            "Name": h.name,
            "Persona": "5",
            "Num_Paths": str(len(h.fc_wwpns) + len(h.iscsi_iqns)),
        })
    return format_table(rows)


async def _showvlun(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """showvlun [-host <host>] — list VLUNs (mappings) optionally filtered by host."""
    session = ctx.session
    host_filter = optional_flag(pc, "host", "")

    if host_filter:
        try:
            host = await hosts_svc.get_host_by_name(session, host_filter)
        except CoreError as exc:
            raise core_to_3par(exc) from exc
        mappings = await mappings_svc.list_mappings_by_host(session, host.id)
    else:
        mappings = await mappings_svc.list_mappings(session, array_id=ctx.array_id)

    rows = []
    for m in mappings:
        vol = await volumes_svc.get_volume(session, m.volume_id)
        host = await hosts_svc.get_host(session, m.host_id)
        rows.append({
            "Lun": str(m.lun_id),
            "VVName": vol.name,
            "HostName": host.name,
            "Status": "active",
        })
    return format_table(rows)


async def _showport(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """showport [-type iscsi|fc] — list storage ports."""
    session = ctx.session
    port_type = optional_flag(pc, "type", "")

    if port_type == "iscsi":
        eps = await endpoints_svc.list_endpoints(session, array_id=ctx.array_id, protocol="iscsi")
    elif port_type == "fc":
        eps = await endpoints_svc.list_endpoints(session, array_id=ctx.array_id, protocol="fc")
    else:
        eps = await endpoints_svc.list_endpoints(session, array_id=ctx.array_id)

    rows = []
    for idx, ep in enumerate(eps):
        node = idx // 2
        slot = 0 if ep.protocol == "fc" else 2
        port = idx % 2
        targets = ep.targets_dict
        if ep.protocol == "fc":
            for wwpn in targets.get("target_wwpns", []):
                rows.append({
                    "N:S:P": f"{node}:{slot}:{port}",
                    "Mode": "target",
                    "State": "ready",
                    "Protocol": "FC",
                    "WWPN/iSCSI_Name": wwpn,
                })
        else:
            iqn = targets.get("target_iqn", "")
            addrs = ep.addresses_dict
            for portal in addrs.get("portals", [""]):
                rows.append({
                    "N:S:P": f"{node}:{slot}:{port}",
                    "Mode": "target",
                    "State": "ready",
                    "Protocol": "iSCSI",
                    "WWPN/iSCSI_Name": iqn,
                    "IP_Addr": portal.split(":")[0] if portal else "",
                })
    return format_table(rows)


# ---------------------------------------------------------------------------
# create*/remove*/grow*/set* handlers (mutating)
# ---------------------------------------------------------------------------

async def _createvv(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """createvv [-tpvv] <name> <cpg> <size>

    Size is in MiB by default.  The ``-tpvv`` flag requests thin provisioning
    (always used by the Cinder driver).
    """
    if len(pc.positional) < 3:
        raise Hpe3parInvalidArgError("createvv requires: <name> <cpg> <size>")

    name = pc.positional[0]
    cpg_name = pc.positional[1]
    size_str = pc.positional[2]

    try:
        size_mb = int(size_str)
    except ValueError:
        raise Hpe3parInvalidArgError(f"size must be an integer, got '{size_str}'")

    if size_mb <= 0:
        raise Hpe3parInvalidArgError("size must be positive")

    session = ctx.session

    pool_result = await session.execute(
        select(Pool).where(
            Pool.name == cpg_name,
            Pool.array_id == ctx.array_id,
        )
    )
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise Hpe3parNotFoundError(f"cpg '{cpg_name}'")

    try:
        await volumes_svc.create_volume(
            session, ctx.spdk,
            name=name,
            pool_id=pool.id,
            size_mb=size_mb,
        )
    except CoreError as exc:
        raise core_to_3par(exc) from exc

    await session.commit()
    logger.info("createvv: created volume '%s' in cpg '%s' size=%dMiB", name, cpg_name, size_mb)
    return ""


async def _removevv(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """removevv [-f] <name>"""
    if not pc.positional:
        raise Hpe3parInvalidArgError("removevv requires a volume name")
    name = pc.positional[0]
    session = ctx.session

    try:
        volume = await volumes_svc.get_volume_by_name(session, name, ctx.array_id)
        await volumes_svc.delete_volume(session, ctx.spdk, volume.id)
    except CoreError as exc:
        raise core_to_3par(exc) from exc

    await session.commit()
    logger.info("removevv: deleted volume '%s'", name)
    return ""


async def _growvv(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """growvv <name> <size_MiB>

    Expand volume by the given delta (3PAR grow-by semantics).
    """
    if len(pc.positional) < 2:
        raise Hpe3parInvalidArgError("growvv requires: <name> <size_MiB>")
    name = pc.positional[0]
    size_str = pc.positional[1]

    try:
        delta_mb = int(size_str)
    except ValueError:
        raise Hpe3parInvalidArgError(f"size must be an integer, got '{size_str}'")

    session = ctx.session

    try:
        volume = await volumes_svc.get_volume_by_name(session, name, ctx.array_id)
        await volumes_svc.expand_volume_by_delta(session, ctx.spdk, volume.id, delta_mb)
    except CoreError as exc:
        raise core_to_3par(exc) from exc

    await session.commit()
    logger.info("growvv: expanded '%s' by %dMiB", name, delta_mb)
    return ""


async def _createhost(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """createhost [-persona <id>] <name> <WWN|iSCSI_name> [<WWN2> ...]

    The persona flag is accepted but ignored (Strix uses generic persona).
    Additional positional args after the name are initiator addresses.
    """
    if not pc.positional:
        raise Hpe3parInvalidArgError("createhost requires a host name")

    name = pc.positional[0]
    initiators = pc.positional[1:]  # Remaining args are WWPNs or IQNs

    session = ctx.session

    # Determine port type from initiator format
    iscsi_iqns: list[str] = []
    fc_wwpns: list[str] = []
    for init in initiators:
        if ":" in init and "." in init:
            # Looks like an IQN (iqn.2005-03.com.example:...)
            iscsi_iqns.append(init)
        elif init.startswith("iqn."):
            iscsi_iqns.append(init)
        else:
            # Assume FC WWPN
            fc_wwpns.append(init.upper().replace(":", ""))

    try:
        host = await hosts_svc.create_host(session, name=name)
        for iqn in iscsi_iqns:
            await hosts_svc.add_host_port(session, host.id, port_type="iscsi", port_value=iqn)
        for wwpn in fc_wwpns:
            await hosts_svc.add_host_port(session, host.id, port_type="fc", port_value=wwpn)
    except CoreError as exc:
        raise core_to_3par(exc) from exc

    await session.commit()
    logger.info("createhost: created host '%s' iqns=%s wwpns=%s", name, iscsi_iqns, fc_wwpns)
    return ""


async def _removehost(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """removehost <name>"""
    if not pc.positional:
        raise Hpe3parInvalidArgError("removehost requires a host name")
    name = pc.positional[0]
    session = ctx.session

    try:
        host = await hosts_svc.get_host_by_name(session, name)
        await hosts_svc.delete_host(session, host.id)
    except CoreError as exc:
        raise core_to_3par(exc) from exc

    await session.commit()
    logger.info("removehost: deleted host '%s'", name)
    return ""


async def _sethost(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """sethost -add <WWN|iSCSI_name> <host_name>

    Add an initiator to an existing host.
    """
    if len(pc.positional) < 2:
        raise Hpe3parInvalidArgError("sethost requires: <initiator> <host_name>")
    if "add" not in pc.boolean_flags:
        raise Hpe3parInvalidArgError("sethost requires -add flag")

    initiator = pc.positional[0]
    host_name = pc.positional[1]
    session = ctx.session

    if initiator.startswith("iqn.") or (":" in initiator and "." in initiator):
        port_type = "iscsi"
        port_value = initiator
    else:
        port_type = "fc"
        port_value = initiator.upper().replace(":", "")

    try:
        host = await hosts_svc.get_host_by_name(session, host_name)
        await hosts_svc.add_host_port(session, host.id, port_type=port_type, port_value=port_value)
    except CoreError as exc:
        raise core_to_3par(exc) from exc

    await session.commit()
    logger.info("sethost -add: host '%s' port=%s:%s", host_name, port_type, port_value)
    return ""


async def _createvlun(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """createvlun <vv_name> <lun> <host_name>

    Map a volume to a host.  The ``lun`` argument is accepted for
    compatibility but the actual LUN is allocated by the core service.
    """
    if len(pc.positional) < 3:
        raise Hpe3parInvalidArgError("createvlun requires: <vv_name> <lun> <host_name>")

    vv_name = pc.positional[0]
    # lun_requested = pc.positional[1]  — accepted but not enforced
    host_name = pc.positional[2]
    session = ctx.session

    try:
        volume = await volumes_svc.get_volume_by_name(session, vv_name, ctx.array_id)
        host = await hosts_svc.get_host_by_name(session, host_name)

        existing = await mappings_svc.find_mapping_by_host_and_volume(
            session, host.id, volume.id,
        )
        if existing:
            raise AlreadyExistsError("vlun", f"volume '{vv_name}' to host '{host_name}'")

        await _ensure_iscsi_endpoint(session, ctx)

        mapping = await mappings_svc.create_mapping(
            session, ctx.spdk, _settings,
            host_id=host.id,
            volume_id=volume.id,
        )
    except CoreError as exc:
        raise core_to_3par(exc) from exc

    await session.commit()
    logger.info("createvlun: mapped '%s' → host '%s' lun=%d", vv_name, host_name, mapping.lun_id)
    return ""


async def _removevlun(ctx: Hpe3parContext, pc: ParsedCommand) -> str:
    """removevlun [-f] <vv_name> <lun> <host_name>"""
    if len(pc.positional) < 3:
        raise Hpe3parInvalidArgError("removevlun requires: <vv_name> <lun> <host_name>")

    vv_name = pc.positional[0]
    host_name = pc.positional[2]
    session = ctx.session

    try:
        volume = await volumes_svc.get_volume_by_name(session, vv_name, ctx.array_id)
        host = await hosts_svc.get_host_by_name(session, host_name)

        mapping = await mappings_svc.find_mapping_by_host_and_volume(
            session, host.id, volume.id,
        )
        if mapping is None:
            raise NotFoundError("vlun", f"volume '{vv_name}' to host '{host_name}'")

        await mappings_svc.delete_mapping(session, ctx.spdk, mapping.id)
    except CoreError as exc:
        raise core_to_3par(exc) from exc

    await session.commit()
    logger.info("removevlun: unmapped '%s' from host '%s'", vv_name, host_name)
    return ""


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

COMMAND_HANDLERS = {
    "showsys": _showsys,
    "showcpg": _showcpg,
    "showvv": _showvv,
    "showhost": _showhost,
    "showvlun": _showvlun,
    "showport": _showport,
    "createvv": _createvv,
    "removevv": _removevv,
    "growvv": _growvv,
    "createhost": _createhost,
    "removehost": _removehost,
    "sethost": _sethost,
    "createvlun": _createvlun,
    "removevlun": _removevlun,
}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def dispatch(
    cmd_str: str,
    ctx: Hpe3parContext,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    """Parse *cmd_str*, call the matching handler, write output, return exit code."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    try:
        pc = parse_command(cmd_str)
    except Hpe3parError as exc:
        print(str(exc), file=err)
        return exc.exit_code

    handler = COMMAND_HANDLERS.get(pc.command)
    if handler is None:
        exc = Hpe3parUnknownCommandError(pc.command)
        print(str(exc), file=err)
        return exc.exit_code

    try:
        output = await handler(ctx, pc)
        if output:
            print(output, file=out)
        return 0
    except Hpe3parError as exc:
        print(str(exc), file=err)
        return exc.exit_code
    except Exception as exc:
        logger.exception("Unhandled error in handler %s", pc.command)
        print(f"Internal error: {exc}", file=err)
        return 1
