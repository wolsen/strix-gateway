# FILE: apollo_gateway/topology/apply.py
"""Apply a validated topology specification to a live Apollo Gateway instance.

Resources are created in dependency order:
  1. Subsystems
  2. Pools (SPDK: backing bdev + lvol store)
  3. Hosts
  4. Volumes (SPDK: lvol)
  5. Mappings (SPDK: LUN / namespace attachment)

Existing resources are skipped (idempotent).

Example::

    spec = load_yaml("examples/ci/single_svc.yaml")
    errors = validate(spec)
    if not errors:
        summary = await apply_topology(spec, session, spdk_client, settings)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.core.db import (
    ExportContainer,
    Host,
    Mapping,
    Pool,
    Subsystem,
    Volume,
)
from apollo_gateway.core.models import PoolBackendType
from apollo_gateway.core.personas import merge_profile
from apollo_gateway.spdk import ensure as spdk_ensure
from apollo_gateway.topology.schema import TopologySpec

if TYPE_CHECKING:
    from apollo_gateway.config import Settings
    from apollo_gateway.spdk.rpc import SPDKClient

logger = logging.getLogger("apollo_gateway.topology.apply")


async def apply_topology(
    spec: TopologySpec,
    session: AsyncSession,
    spdk_client: "SPDKClient",
    settings: "Settings",
) -> dict[str, Any]:
    """Apply *spec* to the running gateway instance.

    Parameters
    ----------
    spec:
        Validated :class:`~apollo_gateway.topology.schema.TopologySpec`.
    session:
        Active :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
    spdk_client:
        Connected :class:`~apollo_gateway.spdk.rpc.SPDKClient`.
    settings:
        Gateway :class:`~apollo_gateway.config.Settings`.

    Returns
    -------
    dict
        Summary with keys ``subsystems``, ``pools``, ``hosts``, ``volumes``,
        ``mappings`` — each mapping to a dict with ``created`` and ``skipped``
        counts.
    """
    summary: dict[str, dict[str, int]] = {
        "subsystems": {"created": 0, "skipped": 0},
        "pools": {"created": 0, "skipped": 0},
        "hosts": {"created": 0, "skipped": 0},
        "volumes": {"created": 0, "skipped": 0},
        "mappings": {"created": 0, "skipped": 0},
    }

    # ---- 1. Subsystems -------------------------------------------------------
    subsystem_objs: dict[str, Subsystem] = {}
    for sub_spec in spec.subsystems:
        existing = (await session.execute(
            select(Subsystem).where(Subsystem.name == sub_spec.name)
        )).scalar_one_or_none()

        if existing:
            logger.debug("Subsystem '%s' already exists — skipping", sub_spec.name)
            subsystem_objs[sub_spec.name] = existing
            summary["subsystems"]["skipped"] += 1
        else:
            cap_override: dict = {}
            if sub_spec.capability_profile:
                cap_override = sub_spec.capability_profile.model_dump(exclude_none=True)

            new_sub = Subsystem(
                name=sub_spec.name,
                persona=sub_spec.persona,
                protocols_enabled=json.dumps(sub_spec.protocols),
                capability_profile=json.dumps(cap_override),
            )
            session.add(new_sub)
            await session.flush()
            subsystem_objs[sub_spec.name] = new_sub
            summary["subsystems"]["created"] += 1
            logger.info("Created subsystem '%s' (persona=%s)", sub_spec.name, sub_spec.persona)

    # ---- 2. Pools ------------------------------------------------------------
    pool_objs: dict[str, Pool] = {}   # pool_name → Pool (last writer wins)
    for pool_spec in spec.pools:
        sub = subsystem_objs.get(pool_spec.subsystem)
        if sub is None:
            logger.warning("Pool '%s': subsystem '%s' not found — skipping",
                           pool_spec.name, pool_spec.subsystem)
            continue

        existing = (await session.execute(
            select(Pool).where(
                Pool.subsystem_id == sub.id,
                Pool.name == pool_spec.name,
            )
        )).scalar_one_or_none()

        if existing:
            logger.debug("Pool '%s' already exists in '%s' — skipping",
                         pool_spec.name, pool_spec.subsystem)
            pool_objs[pool_spec.name] = existing
            summary["pools"]["skipped"] += 1
        else:
            size_mb = int(pool_spec.size_gb * 1024)
            backend_type = (
                PoolBackendType.malloc if pool_spec.backend == "malloc"
                else PoolBackendType.aio_file
            )
            new_pool = Pool(
                name=pool_spec.name,
                subsystem_id=sub.id,
                backend_type=backend_type.value,
                size_mb=size_mb if pool_spec.backend == "malloc" else None,
                aio_path=pool_spec.aio_path,
            )
            session.add(new_pool)
            await session.flush()
            spdk_ensure.ensure_pool(spdk_client, new_pool, sub.name)
            pool_objs[pool_spec.name] = new_pool
            summary["pools"]["created"] += 1
            logger.info("Created pool '%s' in subsystem '%s'", pool_spec.name, sub.name)

    # ---- 3. Hosts ------------------------------------------------------------
    host_objs: dict[str, Host] = {}
    for host_spec in spec.hosts:
        existing = (await session.execute(
            select(Host).where(Host.name == host_spec.name)
        )).scalar_one_or_none()

        if existing:
            logger.debug("Host '%s' already exists — skipping", host_spec.name)
            host_objs[host_spec.name] = existing
            summary["hosts"]["skipped"] += 1
        else:
            iqn = host_spec.iqns[0] if host_spec.iqns else None
            nqn = host_spec.nqns[0] if host_spec.nqns else None
            new_host = Host(name=host_spec.name, iqn=iqn, nqn=nqn)
            session.add(new_host)
            await session.flush()
            host_objs[host_spec.name] = new_host
            summary["hosts"]["created"] += 1
            logger.info("Created host '%s'", host_spec.name)

    # ---- 4. Volumes ----------------------------------------------------------
    volume_objs: dict[str, Volume] = {}
    for vol_spec in spec.volumes:
        pool = pool_objs.get(vol_spec.pool)
        if pool is None:
            logger.warning("Volume '%s': pool '%s' not found — skipping",
                           vol_spec.name, vol_spec.pool)
            continue

        sub = subsystem_objs.get(pool.subsystem.name)
        if sub is None:
            logger.warning("Volume '%s': subsystem not found — skipping", vol_spec.name)
            continue

        existing = (await session.execute(
            select(Volume).where(
                Volume.subsystem_id == sub.id,
                Volume.name == vol_spec.name,
            )
        )).scalar_one_or_none()

        if existing:
            logger.debug("Volume '%s' already exists — skipping", vol_spec.name)
            volume_objs[vol_spec.name] = existing
            summary["volumes"]["skipped"] += 1
        else:
            size_mb = int(vol_spec.size_gb * 1024)
            new_vol = Volume(
                name=vol_spec.name,
                subsystem_id=sub.id,
                pool_id=pool.id,
                size_mb=size_mb,
                status="creating",
            )
            session.add(new_vol)
            await session.flush()

            bdev_name = spdk_ensure.ensure_lvol(
                spdk_client, new_vol, pool.name, sub.name
            )
            new_vol.bdev_name = bdev_name
            new_vol.status = "available"
            volume_objs[vol_spec.name] = new_vol
            summary["volumes"]["created"] += 1
            logger.info("Created volume '%s' (%d MiB) in pool '%s'",
                        vol_spec.name, size_mb, pool.name)

    # ---- 5. Mappings ---------------------------------------------------------
    for map_spec in spec.mappings:
        host = host_objs.get(map_spec.host)
        vol = volume_objs.get(map_spec.volume)
        if host is None or vol is None:
            logger.warning(
                "Mapping host='%s' volume='%s': one or more resources missing — skipping",
                map_spec.host, map_spec.volume,
            )
            continue

        pool = pool_objs.get(vol.pool.name)
        if pool is None:
            logger.warning("Mapping: pool for volume '%s' not found — skipping", map_spec.volume)
            continue

        sub = subsystem_objs.get(pool.subsystem.name)
        if sub is None:
            logger.warning("Mapping: subsystem not found — skipping")
            continue

        # Check if mapping already exists
        existing_mapping = (await session.execute(
            select(Mapping).where(
                Mapping.subsystem_id == sub.id,
                Mapping.volume_id == vol.id,
                Mapping.host_id == host.id,
                Mapping.protocol == map_spec.protocol,
            )
        )).scalar_one_or_none()

        if existing_mapping:
            logger.debug("Mapping already exists — skipping")
            summary["mappings"]["skipped"] += 1
            continue

        # Find or create export container
        ec = (await session.execute(
            select(ExportContainer).where(
                ExportContainer.subsystem_id == sub.id,
                ExportContainer.protocol == map_spec.protocol,
                ExportContainer.host_id == host.id,
            )
        )).scalar_one_or_none()

        if ec is None:
            portal_ip = (
                settings.iscsi_portal_ip
                if map_spec.protocol == "iscsi"
                else settings.nvmef_portal_ip
            )
            portal_port = (
                settings.iscsi_portal_port
                if map_spec.protocol == "iscsi"
                else settings.nvmef_portal_port
            )
            ec = ExportContainer(
                subsystem_id=sub.id,
                protocol=map_spec.protocol,
                host_id=host.id,
                portal_ip=portal_ip,
                portal_port=portal_port,
            )
            session.add(ec)
            await session.flush()

            if map_spec.protocol == "iscsi":
                ec.target_iqn = f"{settings.iqn_prefix}:{sub.name}:{ec.id}"
                spdk_ensure.ensure_iscsi_export(spdk_client, ec, settings)
            else:
                profile = merge_profile(sub.persona, json.loads(sub.capability_profile))
                ec.target_nqn = f"{settings.nqn_prefix}:{sub.name}:{ec.id}"
                spdk_ensure.ensure_nvmef_export(
                    spdk_client, ec, settings,
                    model_number=profile.model,
                    serial_number=f"APOLLO-{sub.name[:8].upper()}",
                )

        # Allocate LUN / NSID
        existing_maps = (await session.execute(
            select(Mapping).where(Mapping.export_container_id == ec.id)
        )).scalars().all()

        if map_spec.protocol == "iscsi":
            used_ids = [m.lun_id for m in existing_maps if m.lun_id is not None]
            lun_id = spdk_ensure.allocate_lun(used_ids)
            new_mapping = Mapping(
                subsystem_id=sub.id,
                volume_id=vol.id,
                host_id=host.id,
                export_container_id=ec.id,
                protocol=map_spec.protocol,
                lun_id=lun_id,
            )
            session.add(new_mapping)
            await session.flush()
            spdk_ensure.ensure_iscsi_mapping(spdk_client, new_mapping, vol, ec)
        else:
            used_ids = [m.ns_id for m in existing_maps if m.ns_id is not None]
            ns_id = spdk_ensure.allocate_nsid(used_ids)
            new_mapping = Mapping(
                subsystem_id=sub.id,
                volume_id=vol.id,
                host_id=host.id,
                export_container_id=ec.id,
                protocol=map_spec.protocol,
                ns_id=ns_id,
            )
            session.add(new_mapping)
            await session.flush()
            spdk_ensure.ensure_nvmef_mapping(spdk_client, new_mapping, vol, ec)

        summary["mappings"]["created"] += 1
        logger.info("Created mapping: host='%s' volume='%s' protocol='%s'",
                    map_spec.host, map_spec.volume, map_spec.protocol)

    await session.commit()
    return summary
