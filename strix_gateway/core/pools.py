# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Core service — Pool lifecycle.

Centralises pool CRUD + SPDK pool provisioning so that REST routes,
vendor façades, and topology-apply all delegate here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.core.db import Array, Pool, Volume
from strix_gateway.core.exceptions import (
    AlreadyExistsError,
    BackendError,
    NotFoundError,
    ResourceInUseError,
    ValidationError,
)

if TYPE_CHECKING:
    from strix_gateway.spdk.rpc import SPDKClient

logger = logging.getLogger("strix_gateway.core.pools")


async def create_pool(
    session: AsyncSession,
    spdk: "SPDKClient",
    *,
    name: str,
    array_id: str,
    backend_type: str,
    size_mb: Optional[int] = None,
    aio_path: Optional[str] = None,
    vendor_metadata: dict | None = None,
) -> Pool:
    """Create a pool and provision it in SPDK.

    Raises :class:`ValidationError` for bad inputs,
    :class:`AlreadyExistsError` if the name is taken on the array,
    :class:`BackendError` on SPDK failure.
    """
    from strix_gateway.core.models import PoolBackendType
    from strix_gateway.spdk.ensure import ensure_pool

    if backend_type == PoolBackendType.malloc and size_mb is None:
        raise ValidationError("size_mb required for malloc backend")
    if backend_type == PoolBackendType.aio_file and not aio_path:
        raise ValidationError("aio_path required for aio_file backend")

    # Resolve array
    arr_result = await session.execute(select(Array).where(Array.id == array_id))
    arr = arr_result.scalar_one_or_none()
    if arr is None:
        raise NotFoundError("Array", array_id)

    existing = await session.execute(
        select(Pool).where(Pool.array_id == arr.id, Pool.name == name)
    )
    if existing.scalar_one_or_none():
        raise AlreadyExistsError("Pool", name)

    pool = Pool(
        name=name,
        array_id=arr.id,
        backend_type=backend_type,
        size_mb=size_mb,
        aio_path=aio_path,
        vendor_metadata=json.dumps(vendor_metadata) if vendor_metadata else "{}",
    )
    session.add(pool)
    await session.flush()

    try:
        await asyncio.to_thread(ensure_pool, spdk, pool, arr.name)
    except Exception as exc:
        logger.error("Failed to create pool %s in SPDK: %s", pool.id, exc)
        raise BackendError(str(exc), cause=exc)

    return pool


async def get_pool(session: AsyncSession, pool_id: str) -> Pool:
    """Get a pool by ID.  Raises :class:`NotFoundError`."""
    result = await session.execute(select(Pool).where(Pool.id == pool_id))
    pool = result.scalar_one_or_none()
    if pool is None:
        raise NotFoundError("Pool", pool_id)
    return pool


async def list_pools(
    session: AsyncSession,
    array_id: Optional[str] = None,
) -> list[Pool]:
    """List pools, optionally filtered by array."""
    stmt = select(Pool)
    if array_id:
        stmt = stmt.where(Pool.array_id == array_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def delete_pool(session: AsyncSession, pool_id: str) -> None:
    """Delete a pool.  Raises :class:`ResourceInUseError` if it has volumes."""
    pool = await get_pool(session, pool_id)

    vols_result = await session.execute(select(Volume).where(Volume.pool_id == pool_id))
    if vols_result.scalars().first():
        raise ResourceInUseError("Pool", pool_id, "has volumes; delete them first")

    await session.delete(pool)
    await session.flush()


async def list_pools_with_stats(
    session: AsyncSession,
    array_id: str,
) -> list[dict]:
    """List pools for an array with capacity statistics.

    Returns dicts with pool + volume_count + used_capacity_mb.
    """
    pools = await list_pools(session, array_id=array_id)
    result = []
    for pool in pools:
        vol_result = await session.execute(
            select(
                func.count(Volume.id),
                func.coalesce(func.sum(Volume.size_mb), 0),
            ).where(Volume.pool_id == pool.id)
        )
        row = vol_result.one()
        result.append({
            "pool": pool,
            "volume_count": row[0],
            "used_capacity_mb": row[1],
        })
    return result
