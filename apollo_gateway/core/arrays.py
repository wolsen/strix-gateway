# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Core service — Array lifecycle.

Centralises array CRUD so that both REST routes and vendor façades delegate
here instead of inlining DB queries.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.core.db import Array, Pool
from apollo_gateway.core.exceptions import (
    AlreadyExistsError,
    NotFoundError,
    ResourceInUseError,
    ValidationError,
)
from apollo_gateway.core.personas import merge_profile

logger = logging.getLogger("apollo_gateway.core.arrays")


async def resolve_array(
    session: AsyncSession,
    name_or_id: str,
) -> Array:
    """Look up an array by UUID (tried first) or name.

    Raises :class:`NotFoundError` if not found.
    """
    result = await session.execute(select(Array).where(Array.id == name_or_id))
    arr = result.scalar_one_or_none()
    if arr is not None:
        return arr
    result = await session.execute(select(Array).where(Array.name == name_or_id))
    arr = result.scalar_one_or_none()
    if arr is None:
        raise NotFoundError("Array", name_or_id)
    return arr


async def get_default_array(session: AsyncSession) -> Array:
    """Return the 'default' array.  Raises :class:`NotFoundError` if missing."""
    result = await session.execute(select(Array).where(Array.name == "default"))
    arr = result.scalar_one_or_none()
    if arr is None:
        raise NotFoundError("Array", "default")
    return arr


async def create_array(
    session: AsyncSession,
    *,
    name: str,
    vendor: str = "generic",
    profile: dict[str, Any] | None = None,
) -> Array:
    """Create a new array.

    Raises :class:`AlreadyExistsError` if the name is taken.
    """
    existing = await session.execute(select(Array).where(Array.name == name))
    if existing.scalar_one_or_none():
        raise AlreadyExistsError("Array", name)

    arr = Array(
        name=name,
        vendor=vendor,
        profile=json.dumps(profile or {}),
    )
    session.add(arr)
    await session.flush()
    return arr


async def list_arrays(session: AsyncSession) -> list[Array]:
    """Return all arrays."""
    result = await session.execute(select(Array))
    return list(result.scalars().all())


async def get_array(session: AsyncSession, array_id: str) -> Array:
    """Get a single array by ID or name.  Raises :class:`NotFoundError`."""
    return await resolve_array(session, array_id)


async def delete_array(session: AsyncSession, array_id: str) -> None:
    """Delete an array.

    Raises :class:`ValidationError` for the default array,
    :class:`ResourceInUseError` if the array has pools.
    """
    arr = await resolve_array(session, array_id)

    if arr.name == "default":
        raise ResourceInUseError(
            "Array", arr.name, "the 'default' array cannot be deleted"
        )

    pools_result = await session.execute(select(Pool).where(Pool.array_id == arr.id))
    pools = pools_result.scalars().all()
    if pools:
        raise ResourceInUseError(
            "Array", arr.name,
            f"has {len(pools)} pool(s); delete all pools and volumes first",
        )

    await session.delete(arr)
    await session.flush()


async def get_capabilities(session: AsyncSession, array_id: str) -> dict:
    """Return the effective capability profile for an array."""
    arr = await resolve_array(session, array_id)
    overrides = arr.profile_dict
    effective = merge_profile(arr.vendor, overrides)
    return {
        "array_id": arr.id,
        "array_name": arr.name,
        "vendor": arr.vendor,
        "effective_profile": effective.model_dump(),
    }
