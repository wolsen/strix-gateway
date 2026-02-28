# FILE: apollo_gateway/core/reconcile.py
"""Startup reconciler — replays desired DB state into SPDK.

Called once during the FastAPI lifespan before the server starts accepting
requests.  Every ensure_* call is idempotent so re-running is safe.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apollo_gateway.config import Settings
from apollo_gateway.core.db import ExportContainer, Mapping, Pool, Subsystem, Volume
from apollo_gateway.core.models import Protocol, VolumeStatus
from apollo_gateway.spdk import iscsi as iscsi_rpc
from apollo_gateway.spdk import nvmf as nvmf_rpc
from apollo_gateway.spdk.ensure import (
    ensure_iscsi_export,
    ensure_iscsi_mapping,
    ensure_lvol,
    ensure_nvmef_export,
    ensure_nvmef_mapping,
    ensure_pool,
)
from apollo_gateway.spdk.rpc import SPDKClient

logger = logging.getLogger("apollo_gateway.reconcile")


async def reconcile(
    spdk_client: SPDKClient,
    session_factory: async_sessionmaker,
    settings: Settings,
) -> None:
    """Replay all desired state from the database into SPDK."""
    logger.info("Starting reconciliation")

    async with session_factory() as session:
        # 1. Shared iSCSI infrastructure
        try:
            await asyncio.to_thread(
                iscsi_rpc.ensure_portal_group,
                spdk_client,
                settings.iscsi_portal_ip,
                settings.iscsi_portal_port,
            )
            await asyncio.to_thread(iscsi_rpc.ensure_initiator_group, spdk_client)
        except Exception as exc:
            logger.warning("Could not reconcile iSCSI infrastructure: %s", exc)

        # 2. NVMe-oF TCP transport
        try:
            await asyncio.to_thread(nvmf_rpc.ensure_transport, spdk_client)
        except Exception as exc:
            logger.warning("Could not reconcile NVMe-oF transport: %s", exc)

        # 3. Load subsystems for name lookup
        subs_result = await session.execute(select(Subsystem))
        subsystems = {s.id: s for s in subs_result.scalars().all()}

        # 4. Pools
        pools_result = await session.execute(select(Pool))
        pools = {p.id: p for p in pools_result.scalars().all()}
        for pool in pools.values():
            sub = subsystems.get(pool.subsystem_id)
            subsystem_name = sub.name if sub else "default"
            try:
                await asyncio.to_thread(ensure_pool, spdk_client, pool, subsystem_name)
            except Exception as exc:
                logger.error("Failed to reconcile pool %s: %s", pool.id, exc)

        # 5. Volumes (skip those in terminal error state)
        vols_result = await session.execute(
            select(Volume).where(Volume.status != VolumeStatus.error)
        )
        volumes = {v.id: v for v in vols_result.scalars().all()}
        for vol in volumes.values():
            pool = pools.get(vol.pool_id)
            if pool is None:
                logger.warning("Volume %s references unknown pool %s", vol.id, vol.pool_id)
                continue
            sub = subsystems.get(vol.subsystem_id)
            subsystem_name = sub.name if sub else "default"
            try:
                bdev_name = await asyncio.to_thread(
                    ensure_lvol, spdk_client, vol, pool.name, subsystem_name
                )
                if vol.bdev_name != bdev_name:
                    vol.bdev_name = bdev_name
            except Exception as exc:
                logger.error("Failed to reconcile volume %s: %s", vol.id, exc)

        # 6. Export containers
        ecs_result = await session.execute(select(ExportContainer))
        ecs = {ec.id: ec for ec in ecs_result.scalars().all()}
        for ec in ecs.values():
            try:
                if ec.protocol == Protocol.iscsi:
                    await asyncio.to_thread(ensure_iscsi_export, spdk_client, ec, settings)
                else:
                    await asyncio.to_thread(ensure_nvmef_export, spdk_client, ec, settings)
            except Exception as exc:
                logger.error("Failed to reconcile export container %s: %s", ec.id, exc)

        # 7. Mappings
        maps_result = await session.execute(select(Mapping))
        for mapping in maps_result.scalars().all():
            vol = volumes.get(mapping.volume_id)
            ec = ecs.get(mapping.export_container_id)
            if vol is None or ec is None:
                continue
            try:
                if mapping.protocol == Protocol.iscsi:
                    await asyncio.to_thread(ensure_iscsi_mapping, spdk_client, mapping, vol, ec)
                else:
                    await asyncio.to_thread(ensure_nvmef_mapping, spdk_client, mapping, vol, ec)
            except Exception as exc:
                logger.error("Failed to reconcile mapping %s: %s", mapping.id, exc)

        await session.commit()

    logger.info("Reconciliation complete")
