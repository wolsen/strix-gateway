# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Array CRUD, transport-endpoint management, and capability reporting."""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.core.db import Array, Pool, TransportEndpoint, Volume, get_session, get_session_factory
from apollo_gateway.core.models import (
    ArrayCreate,
    ArrayView,
    CapabilitiesView,
    PoolResponse,
    TransportEndpointCreate,
    TransportEndpointView,
)
from apollo_gateway.core.personas import merge_profile

logger = logging.getLogger("apollo_gateway.api.arrays")

router = APIRouter(prefix="/v1/arrays", tags=["arrays"])

DbSession = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_array(db: AsyncSession, name_or_id: str) -> Array:
    """Look up an array by UUID (tried first) or name. Raises 404 if not found."""
    result = await db.execute(select(Array).where(Array.id == name_or_id))
    arr = result.scalar_one_or_none()
    if arr is not None:
        return arr
    result = await db.execute(select(Array).where(Array.name == name_or_id))
    arr = result.scalar_one_or_none()
    if arr is None:
        raise HTTPException(status_code=404, detail=f"Array '{name_or_id}' not found")
    return arr


async def _refresh_vhost_state(request: Request) -> None:
    """Rebuild vhost registry and re-sync TLS after array changes."""
    registry = getattr(request.app.state, "vhost_registry", None)
    if registry is None:
        return
    await registry.rebuild(get_session_factory())
    mgr = getattr(request.app.state, "tls_manager", None)
    sni = getattr(request.app.state, "sni_router", None)
    if mgr and sni:
        from apollo_gateway.config import settings

        mappings = {info.name: info.fqdn for info in registry.all_mappings().values()}
        mgr.sync_tls_assets(
            mappings,
            tls_mode=settings.tls_mode,
            hostname_override=settings.vhost_hostname_override,
            domain=settings.vhost_domain,
        )
        sni.reload(list(mappings.values()))


def _array_to_view(arr: Array) -> ArrayView:
    return ArrayView(
        id=arr.id,
        name=arr.name,
        vendor=arr.vendor,
        profile=json.loads(arr.profile) if isinstance(arr.profile, str) else arr.profile,
        created_at=arr.created_at,
        updated_at=arr.updated_at,
    )


def _ep_to_view(ep: TransportEndpoint) -> TransportEndpointView:
    return TransportEndpointView(
        id=ep.id,
        array_id=ep.array_id,
        protocol=ep.protocol,
        targets=json.loads(ep.targets) if isinstance(ep.targets, str) else ep.targets,
        addresses=json.loads(ep.addresses) if isinstance(ep.addresses, str) else ep.addresses,
        auth=json.loads(ep.auth) if isinstance(ep.auth, str) else ep.auth,
        created_at=ep.created_at,
    )


# ---------------------------------------------------------------------------
# Array CRUD
# ---------------------------------------------------------------------------

@router.post("", response_model=ArrayView, status_code=status.HTTP_201_CREATED)
async def create_array(body: ArrayCreate, request: Request, db: DbSession):
    existing = await db.execute(select(Array).where(Array.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Array '{body.name}' already exists")

    arr = Array(
        name=body.name,
        vendor=body.vendor,
        profile=json.dumps(body.profile),
    )
    db.add(arr)
    await db.commit()
    await db.refresh(arr)
    logger.info("Created array '%s' (vendor=%s)", arr.name, arr.vendor)
    await _refresh_vhost_state(request)
    return _array_to_view(arr)


@router.get("", response_model=list[ArrayView])
async def list_arrays(db: DbSession):
    result = await db.execute(select(Array))
    return [_array_to_view(a) for a in result.scalars().all()]


@router.get("/{array_id}", response_model=ArrayView)
async def get_array(array_id: str, db: DbSession):
    arr = await _resolve_array(db, array_id)
    return _array_to_view(arr)


@router.delete("/{array_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_array(
    array_id: str,
    request: Request,
    db: DbSession,
    force: bool = Query(default=False),
):
    arr = await _resolve_array(db, array_id)

    if arr.name == "default":
        raise HTTPException(status_code=409, detail="Cannot delete the 'default' array")

    pools_result = await db.execute(select(Pool).where(Pool.array_id == arr.id))
    pools = pools_result.scalars().all()
    if pools:
        raise HTTPException(
            status_code=409,
            detail=f"Array '{arr.name}' has {len(pools)} pool(s). "
                   "Delete all pools and volumes before deleting an array.",
        )

    await db.delete(arr)
    await db.commit()
    logger.info("Deleted array '%s'", arr.name)
    await _refresh_vhost_state(request)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

@router.get("/{array_id}/capabilities", response_model=CapabilitiesView)
async def get_capabilities(array_id: str, db: DbSession):
    arr = await _resolve_array(db, array_id)
    overrides = json.loads(arr.profile) if isinstance(arr.profile, str) else arr.profile
    effective = merge_profile(arr.vendor, overrides)
    return CapabilitiesView(
        array_id=arr.id,
        array_name=arr.name,
        vendor=arr.vendor,
        effective_profile=effective.model_dump(),
    )


# ---------------------------------------------------------------------------
# Transport Endpoints (nested under array)
# ---------------------------------------------------------------------------

@router.post(
    "/{array_id}/endpoints",
    response_model=TransportEndpointView,
    status_code=status.HTTP_201_CREATED,
)
async def create_endpoint(array_id: str, body: TransportEndpointCreate, db: DbSession):
    arr = await _resolve_array(db, array_id)

    ep = TransportEndpoint(
        array_id=arr.id,
        protocol=body.protocol.value,
        targets=json.dumps(body.targets),
        addresses=json.dumps(body.addresses),
        auth=json.dumps(body.auth),
    )
    db.add(ep)
    await db.commit()
    await db.refresh(ep)
    logger.info("Created endpoint %s (protocol=%s) on array '%s'", ep.id, ep.protocol, arr.name)
    return _ep_to_view(ep)


@router.get("/{array_id}/endpoints", response_model=list[TransportEndpointView])
async def list_endpoints(array_id: str, db: DbSession):
    arr = await _resolve_array(db, array_id)
    result = await db.execute(
        select(TransportEndpoint).where(TransportEndpoint.array_id == arr.id)
    )
    return [_ep_to_view(ep) for ep in result.scalars().all()]


@router.get("/{array_id}/endpoints/{endpoint_id}", response_model=TransportEndpointView)
async def get_endpoint(array_id: str, endpoint_id: str, db: DbSession):
    arr = await _resolve_array(db, array_id)
    result = await db.execute(
        select(TransportEndpoint).where(
            TransportEndpoint.id == endpoint_id,
            TransportEndpoint.array_id == arr.id,
        )
    )
    ep = result.scalar_one_or_none()
    if ep is None:
        raise HTTPException(status_code=404, detail=f"Endpoint {endpoint_id} not found")
    return _ep_to_view(ep)


@router.delete("/{array_id}/endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(array_id: str, endpoint_id: str, db: DbSession):
    arr = await _resolve_array(db, array_id)
    result = await db.execute(
        select(TransportEndpoint).where(
            TransportEndpoint.id == endpoint_id,
            TransportEndpoint.array_id == arr.id,
        )
    )
    ep = result.scalar_one_or_none()
    if ep is None:
        raise HTTPException(status_code=404, detail=f"Endpoint {endpoint_id} not found")
    await db.delete(ep)
    await db.commit()
    logger.info("Deleted endpoint %s from array '%s'", endpoint_id, arr.name)


# ---------------------------------------------------------------------------
# Pool binding (attach / detach pool ↔ array)
# ---------------------------------------------------------------------------

@router.post(
    "/{array_id}/pools/{pool_id}",
    response_model=PoolResponse,
    status_code=status.HTTP_200_OK,
)
async def attach_pool_to_array(array_id: str, pool_id: str, db: DbSession):
    """Re-attach an existing pool to a different array."""
    arr = await _resolve_array(db, array_id)

    pool_result = await db.execute(select(Pool).where(Pool.id == pool_id))
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool {pool_id} not found")

    dup = await db.execute(
        select(Pool).where(Pool.array_id == arr.id, Pool.name == pool.name)
    )
    if dup.scalar_one_or_none() and pool.array_id != arr.id:
        raise HTTPException(
            status_code=409,
            detail=f"Array '{arr.name}' already has a pool named '{pool.name}'",
        )

    pool.array_id = arr.id
    await db.commit()
    await db.refresh(pool)
    return PoolResponse.model_validate(pool)


@router.delete(
    "/{array_id}/pools/{pool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def detach_pool_from_array(array_id: str, pool_id: str, db: DbSession):
    """Detach a pool from its array (only if the pool has no volumes)."""
    arr = await _resolve_array(db, array_id)

    pool_result = await db.execute(
        select(Pool).where(Pool.id == pool_id, Pool.array_id == arr.id)
    )
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise HTTPException(
            status_code=404,
            detail=f"Pool {pool_id} not found in array '{arr.name}'",
        )

    vols = await db.execute(select(Volume).where(Volume.pool_id == pool.id))
    if vols.scalars().first():
        raise HTTPException(
            status_code=409,
            detail=f"Pool '{pool.name}' has volumes. Delete volumes first.",
        )

    default_result = await db.execute(select(Array).where(Array.name == "default"))
    default = default_result.scalar_one_or_none()
    if default is None or default.id == arr.id:
        raise HTTPException(
            status_code=409,
            detail="Cannot detach pool: no alternative array available",
        )

    pool.array_id = default.id
    await db.commit()
