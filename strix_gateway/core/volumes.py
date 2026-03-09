# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Core service — Volume lifecycle.

Centralises volume CRUD + SPDK lvol operations so that REST routes,
vendor façades, and topology-apply all delegate here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.core.db import Array, Mapping, Pool, Volume
from strix_gateway.core.exceptions import (
    AlreadyExistsError,
    BackendError,
    NotFoundError,
    ResourceInUseError,
    ValidationError,
)
from strix_gateway.core.models import VolumeStatus

if TYPE_CHECKING:
    from strix_gateway.spdk.rpc import SPDKClient

logger = logging.getLogger("strix_gateway.core.volumes")


async def create_volume(
    session: AsyncSession,
    spdk: "SPDKClient",
    *,
    name: str,
    pool_id: str,
    size_mb: int,
) -> Volume:
    """Create a volume and provision the lvol in SPDK.

    Parameters
    ----------
    size_mb:
        Size in MiB.  Callers that work in GB should convert before calling.

    Raises :class:`NotFoundError` if the pool doesn't exist,
    :class:`BackendError` on SPDK failure.
    """
    from strix_gateway.spdk.ensure import ensure_lvol

    pool_result = await session.execute(select(Pool).where(Pool.id == pool_id))
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise NotFoundError("Pool", pool_id)

    arr_result = await session.execute(select(Array).where(Array.id == pool.array_id))
    arr = arr_result.scalar_one_or_none()
    if arr is None:
        raise NotFoundError("Array", pool.array_id)

    # Duplicate name check (scoped to array)
    dup_result = await session.execute(
        select(Volume).where(Volume.name == name, Volume.array_id == arr.id)
    )
    if dup_result.scalar_one_or_none():
        raise AlreadyExistsError("Volume", name)

    volume = Volume(
        name=name,
        array_id=arr.id,
        pool_id=pool_id,
        size_mb=size_mb,
        status=VolumeStatus.creating,
    )
    session.add(volume)
    await session.flush()

    try:
        bdev_name = await asyncio.to_thread(ensure_lvol, spdk, volume, pool.name, arr.name)
        volume.bdev_name = bdev_name
        volume.status = VolumeStatus.available
    except Exception as exc:
        logger.error("Failed to create lvol for volume %s: %s", volume.id, exc)
        volume.status = VolumeStatus.error
        raise BackendError(str(exc), cause=exc)

    return volume


async def get_volume(session: AsyncSession, volume_id: str) -> Volume:
    """Get a volume by ID.  Raises :class:`NotFoundError`."""
    result = await session.execute(select(Volume).where(Volume.id == volume_id))
    vol = result.scalar_one_or_none()
    if vol is None:
        raise NotFoundError("Volume", volume_id)
    return vol


async def get_volume_by_name(
    session: AsyncSession,
    name: str,
    array_id: str,
) -> Volume:
    """Get a volume by name within an array.  Raises :class:`NotFoundError`."""
    result = await session.execute(
        select(Volume).where(Volume.name == name, Volume.array_id == array_id)
    )
    vol = result.scalar_one_or_none()
    if vol is None:
        raise NotFoundError("Volume", name)
    return vol


async def list_volumes(
    session: AsyncSession,
    array_id: Optional[str] = None,
) -> list[Volume]:
    """List volumes, optionally filtered by array."""
    stmt = select(Volume)
    if array_id:
        stmt = stmt.where(Volume.array_id == array_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def delete_volume(
    session: AsyncSession,
    spdk: "SPDKClient",
    volume_id: str,
) -> None:
    """Delete a volume and its SPDK lvol.

    Raises :class:`ResourceInUseError` if the volume has active mappings,
    :class:`BackendError` on SPDK failure.
    """
    from strix_gateway.spdk.ensure import delete_lvol

    volume = await get_volume(session, volume_id)

    maps_result = await session.execute(
        select(Mapping).where(Mapping.volume_id == volume_id)
    )
    if maps_result.scalars().first():
        raise ResourceInUseError("Volume", volume_id, "has active mappings; unmap first")

    volume.status = VolumeStatus.deleting
    await session.flush()

    if volume.bdev_name:
        try:
            await asyncio.to_thread(delete_lvol, spdk, volume.bdev_name)
        except Exception as exc:
            logger.error("Failed to delete lvol %s: %s", volume.bdev_name, exc)
            volume.status = VolumeStatus.error
            raise BackendError(str(exc), cause=exc)

    await session.delete(volume)
    await session.flush()


async def extend_volume(
    session: AsyncSession,
    spdk: "SPDKClient",
    volume_id: str,
    new_size_mb: int,
) -> Volume:
    """Extend a volume to an absolute new size (in MiB).

    Raises :class:`ValidationError` if the new size is not larger.
    """
    from strix_gateway.spdk.ensure import resize_lvol

    volume = await get_volume(session, volume_id)

    if new_size_mb <= volume.size_mb:
        raise ValidationError("new size must be larger than current size")

    volume.status = VolumeStatus.extending
    await session.flush()

    try:
        await asyncio.to_thread(resize_lvol, spdk, volume.bdev_name, new_size_mb)
        volume.size_mb = new_size_mb
        volume.status = VolumeStatus.available
    except Exception as exc:
        logger.error("Failed to resize lvol %s: %s", volume.bdev_name, exc)
        volume.status = VolumeStatus.error
        raise BackendError(str(exc), cause=exc)

    return volume


async def expand_volume_by_delta(
    session: AsyncSession,
    spdk: "SPDKClient",
    volume_id: str,
    delta_mb: int,
) -> Volume:
    """Expand a volume by a delta amount (SVC semantics: 'expandvdisksize').

    Internally converts to absolute size and delegates to :func:`extend_volume`.
    """
    volume = await get_volume(session, volume_id)
    new_size_mb = volume.size_mb + delta_mb
    return await extend_volume(session, spdk, volume_id, new_size_mb)
