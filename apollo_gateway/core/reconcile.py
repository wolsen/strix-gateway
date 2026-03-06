# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Startup reconciler — replays desired DB state into SPDK.

Called once during the FastAPI lifespan before the server starts accepting
requests.  Every ensure_* call is idempotent so re-running is safe.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apollo_gateway.config import Settings
from apollo_gateway.core.db import Array, Mapping, Pool, TransportEndpoint, Volume
from apollo_gateway.core.models import DesiredState, Protocol, VolumeStatus
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

        # 3. Load arrays for name lookup
        arrays_result = await session.execute(select(Array))
        arrays = {a.id: a for a in arrays_result.scalars().all()}

        # 4. Pools
        pools_result = await session.execute(select(Pool))
        pools = {p.id: p for p in pools_result.scalars().all()}
        for pool in pools.values():
            arr = arrays.get(pool.array_id)
            array_name = arr.name if arr else "default"
            try:
                await asyncio.to_thread(ensure_pool, spdk_client, pool, array_name)
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
            arr = arrays.get(vol.array_id)
            array_name = arr.name if arr else "default"
            try:
                bdev_name = await asyncio.to_thread(
                    ensure_lvol, spdk_client, vol, pool.name, array_name
                )
                if vol.bdev_name != bdev_name:
                    vol.bdev_name = bdev_name
            except Exception as exc:
                logger.error("Failed to reconcile volume %s: %s", vol.id, exc)

        # 6. Transport endpoints
        eps_result = await session.execute(select(TransportEndpoint))
        eps = {ep.id: ep for ep in eps_result.scalars().all()}
        for ep in eps.values():
            try:
                if ep.protocol == Protocol.iscsi:
                    await asyncio.to_thread(ensure_iscsi_export, spdk_client, ep, settings)
                elif ep.protocol == Protocol.nvmeof_tcp:
                    await asyncio.to_thread(ensure_nvmef_export, spdk_client, ep, settings)
                # FC endpoints have no SPDK-side state to reconcile
            except Exception as exc:
                logger.error("Failed to reconcile endpoint %s: %s", ep.id, exc)

        # 7. Mappings (only those in 'attached' desired state)
        maps_result = await session.execute(
            select(Mapping).where(Mapping.desired_state == DesiredState.attached)
        )
        for mapping in maps_result.scalars().all():
            vol = volumes.get(mapping.volume_id)
            underlay_ep = eps.get(mapping.underlay_endpoint_id)
            if vol is None or underlay_ep is None:
                continue
            try:
                if underlay_ep.protocol == Protocol.iscsi:
                    await asyncio.to_thread(
                        ensure_iscsi_mapping, spdk_client, mapping, vol, underlay_ep,
                    )
                elif underlay_ep.protocol == Protocol.nvmeof_tcp:
                    await asyncio.to_thread(
                        ensure_nvmef_mapping, spdk_client, mapping, vol, underlay_ep,
                    )
            except Exception as exc:
                logger.error("Failed to reconcile mapping %s: %s", mapping.id, exc)

        await session.commit()

    logger.info("Reconciliation complete")
