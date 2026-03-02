# FILE: apollo_gateway/api/subsystems.py
"""Subsystem CRUD endpoints and capability reporting."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.core.db import Pool, Subsystem, Volume, get_session
from apollo_gateway.core.models import (
    CapabilitiesView,
    PoolResponse,
    SubsystemCreate,
    SubsystemUpdate,
    SubsystemView,
)
from apollo_gateway.core.personas import merge_profile

logger = logging.getLogger("apollo_gateway.api.subsystems")

router = APIRouter(prefix="/v1/subsystems", tags=["subsystems"])

DbSession = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_subsystem(db: AsyncSession, name_or_id: str) -> Subsystem:
    """Look up a subsystem by UUID (tried first) or name. Raises 404 if not found."""
    # Try by id first
    result = await db.execute(select(Subsystem).where(Subsystem.id == name_or_id))
    sub = result.scalar_one_or_none()
    if sub is not None:
        return sub
    # Fallback: by name
    result = await db.execute(select(Subsystem).where(Subsystem.name == name_or_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail=f"Subsystem '{name_or_id}' not found")
    return sub


def _subsystem_to_view(sub: Subsystem) -> SubsystemView:
    return SubsystemView(
        id=sub.id,
        name=sub.name,
        persona=sub.persona,
        protocols_enabled=json.loads(sub.protocols_enabled),
        capability_profile=json.loads(sub.capability_profile),
        created_at=sub.created_at,
        updated_at=sub.updated_at,
    )


# ---------------------------------------------------------------------------
# Subsystem CRUD
# ---------------------------------------------------------------------------

@router.post("", response_model=SubsystemView, status_code=status.HTTP_201_CREATED)
async def create_subsystem(body: SubsystemCreate, db: DbSession):
    # Validate protocols_enabled values
    valid_protocols = {"iscsi", "nvmeof_tcp"}
    bad = [p for p in body.protocols_enabled if p not in valid_protocols]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown protocols: {bad}. Valid: {sorted(valid_protocols)}")

    # Check uniqueness
    existing = await db.execute(select(Subsystem).where(Subsystem.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Subsystem '{body.name}' already exists")

    sub = Subsystem(
        name=body.name,
        persona=body.persona,
        protocols_enabled=json.dumps(body.protocols_enabled),
        capability_profile=json.dumps(body.capability_profile),
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    logger.info("Created subsystem '%s' (persona=%s)", sub.name, sub.persona)
    return _subsystem_to_view(sub)


@router.get("", response_model=list[SubsystemView])
async def list_subsystems(db: DbSession):
    result = await db.execute(select(Subsystem))
    return [_subsystem_to_view(s) for s in result.scalars().all()]


@router.get("/{subsystem_id}", response_model=SubsystemView)
async def get_subsystem(subsystem_id: str, db: DbSession):
    sub = await _resolve_subsystem(db, subsystem_id)
    return _subsystem_to_view(sub)


@router.patch("/{subsystem_id}", response_model=SubsystemView)
async def update_subsystem(subsystem_id: str, body: SubsystemUpdate, db: DbSession):
    sub = await _resolve_subsystem(db, subsystem_id)
    if body.persona is not None:
        sub.persona = body.persona
    if body.protocols_enabled is not None:
        valid_protocols = {"iscsi", "nvmeof_tcp"}
        bad = [p for p in body.protocols_enabled if p not in valid_protocols]
        if bad:
            raise HTTPException(status_code=400, detail=f"Unknown protocols: {bad}. Valid: {sorted(valid_protocols)}")
        sub.protocols_enabled = json.dumps(body.protocols_enabled)
    if body.capability_profile is not None:
        sub.capability_profile = json.dumps(body.capability_profile)
    await db.commit()
    await db.refresh(sub)
    logger.info("Updated subsystem '%s'", sub.name)
    return _subsystem_to_view(sub)


@router.delete("/{subsystem_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subsystem(
    subsystem_id: str,
    db: DbSession,
    force: bool = Query(default=False),
):
    sub = await _resolve_subsystem(db, subsystem_id)

    # Reject deletion of the default subsystem
    if sub.name == "default":
        raise HTTPException(status_code=409, detail="Cannot delete the 'default' subsystem")

    # Check for attached pools
    pools_result = await db.execute(select(Pool).where(Pool.subsystem_id == sub.id))
    pools = pools_result.scalars().all()
    if pools and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Subsystem '{sub.name}' has {len(pools)} pool(s). "
                   "Delete pools first or pass ?force=true.",
        )
    # force=true still requires pools to be empty (we don't cascade delete storage)
    if pools:
        raise HTTPException(
            status_code=409,
            detail=f"Subsystem '{sub.name}' has {len(pools)} pool(s). "
                   "Delete all pools and volumes before deleting a subsystem.",
        )

    await db.delete(sub)
    await db.commit()
    logger.info("Deleted subsystem '%s'", sub.name)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

@router.get("/{subsystem_id}/capabilities", response_model=CapabilitiesView)
async def get_capabilities(subsystem_id: str, db: DbSession):
    sub = await _resolve_subsystem(db, subsystem_id)
    overrides = json.loads(sub.capability_profile)
    effective = merge_profile(sub.persona, overrides)
    return CapabilitiesView(
        subsystem_id=sub.id,
        subsystem_name=sub.name,
        persona=sub.persona,
        protocols_enabled=json.loads(sub.protocols_enabled),
        effective_profile=effective.model_dump(),
    )


# ---------------------------------------------------------------------------
# Pool binding (attach / detach pool ↔ subsystem)
# ---------------------------------------------------------------------------

@router.post(
    "/{subsystem_id}/pools/{pool_id}",
    response_model=PoolResponse,
    status_code=status.HTTP_200_OK,
)
async def attach_pool_to_subsystem(subsystem_id: str, pool_id: str, db: DbSession):
    """Re-attach an existing pool to a different subsystem.

    Validates that the destination subsystem doesn't already have a pool
    with the same name (composite uniqueness constraint).
    """
    sub = await _resolve_subsystem(db, subsystem_id)

    pool_result = await db.execute(select(Pool).where(Pool.id == pool_id))
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool {pool_id} not found")

    # Check name uniqueness within target subsystem
    dup = await db.execute(
        select(Pool).where(Pool.subsystem_id == sub.id, Pool.name == pool.name)
    )
    if dup.scalar_one_or_none() and pool.subsystem_id != sub.id:
        raise HTTPException(
            status_code=409,
            detail=f"Subsystem '{sub.name}' already has a pool named '{pool.name}'",
        )

    pool.subsystem_id = sub.id
    await db.commit()
    await db.refresh(pool)
    return PoolResponse.model_validate(pool)


@router.delete(
    "/{subsystem_id}/pools/{pool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def detach_pool_from_subsystem(subsystem_id: str, pool_id: str, db: DbSession):
    """Detach a pool from its subsystem (only if the pool has no volumes)."""
    sub = await _resolve_subsystem(db, subsystem_id)

    pool_result = await db.execute(
        select(Pool).where(Pool.id == pool_id, Pool.subsystem_id == sub.id)
    )
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise HTTPException(
            status_code=404,
            detail=f"Pool {pool_id} not found in subsystem '{sub.name}'",
        )

    vols = await db.execute(select(Volume).where(Volume.pool_id == pool.id))
    if vols.scalars().first():
        raise HTTPException(
            status_code=409,
            detail=f"Pool '{pool.name}' has volumes. Delete volumes first.",
        )

    # Re-attach to default subsystem (detach = move to default)
    default_result = await db.execute(select(Subsystem).where(Subsystem.name == "default"))
    default = default_result.scalar_one_or_none()
    if default is None or default.id == sub.id:
        raise HTTPException(
            status_code=409,
            detail="Cannot detach pool: no alternative subsystem available",
        )

    pool.subsystem_id = default.id
    await db.commit()
