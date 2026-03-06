# FILE: apollo_gateway/api/v1.py
"""Apollo Gateway v1 REST API routes.

v0.2 — Array/TransportEndpoint model, service-layer mappings, attachments.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.config import settings
from apollo_gateway.core.db import Array, Host, Mapping, Pool, TransportEndpoint, Volume, get_session
from apollo_gateway.core.faults import FaultInjectionError, check_fault
from apollo_gateway.core.models import (
    AttachmentsResponse,
    HostCreate,
    HostResponse,
    HostUpdate,
    MappingCreate,
    MappingResponse,
    PoolCreate,
    PoolResponse,
    Protocol,
    SvcRunRequest,
    SvcRunResponse,
    VolumeCreate,
    VolumeExtend,
    VolumeResponse,
    VolumeStatus,
)
from apollo_gateway.core.services import create_mapping as svc_create_mapping
from apollo_gateway.core.services import delete_mapping as svc_delete_mapping
from apollo_gateway.core.services import get_host_attachments
from apollo_gateway.spdk.ensure import (
    delete_lvol,
    ensure_lvol,
    ensure_pool,
    resize_lvol,
)
from apollo_gateway.spdk.rpc import SPDKError

logger = logging.getLogger("apollo_gateway.api.v1")

router = APIRouter(prefix="/v1", tags=["v1"])

DbSession = Annotated[AsyncSession, Depends(get_session)]


def _spdk(request: Request):
    return request.app.state.spdk_client


async def _resolve_default_array(db: AsyncSession) -> Array:
    """Return the 'default' array. Raises 500 if missing."""
    result = await db.execute(select(Array).where(Array.name == "default"))
    arr = result.scalar_one_or_none()
    if arr is None:
        raise HTTPException(
            status_code=500,
            detail="Default array not found. Gateway may still be initialising.",
        )
    return arr


async def _resolve_array(db: AsyncSession, name_or_id: str) -> Array:
    """Resolve an array by name or UUID. 404 if not found."""
    result = await db.execute(select(Array).where(Array.id == name_or_id))
    arr = result.scalar_one_or_none()
    if arr is not None:
        return arr
    result = await db.execute(select(Array).where(Array.name == name_or_id))
    arr = result.scalar_one_or_none()
    if arr is None:
        raise HTTPException(status_code=404, detail=f"Array '{name_or_id}' not found")
    return arr


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

@router.post("/pools", response_model=PoolResponse, status_code=status.HTTP_201_CREATED)
async def create_pool(body: PoolCreate, request: Request, db: DbSession):
    await check_fault("create_pool")

    from apollo_gateway.core.models import PoolBackendType
    if body.backend_type == PoolBackendType.malloc and body.size_mb is None:
        raise HTTPException(status_code=400, detail="size_mb required for malloc backend")
    if body.backend_type == PoolBackendType.aio_file and not body.aio_path:
        raise HTTPException(status_code=400, detail="aio_path required for aio_file backend")

    # Resolve array — pools are created under the default array unless
    # the caller uses the /arrays/{id}/pools endpoint.
    arr = await _resolve_default_array(db)

    existing = await db.execute(
        select(Pool).where(Pool.array_id == arr.id, Pool.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Pool '{body.name}' already exists in array '{arr.name}'",
        )

    pool = Pool(
        name=body.name,
        array_id=arr.id,
        backend_type=body.backend_type,
        size_mb=body.size_mb,
        aio_path=body.aio_path,
    )
    db.add(pool)
    await db.flush()

    client = _spdk(request)
    try:
        await asyncio.to_thread(ensure_pool, client, pool, arr.name)
    except Exception as exc:
        logger.error("Failed to create pool %s in SPDK: %s", pool.id, exc)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"SPDK error: {exc}")

    await db.commit()
    await db.refresh(pool)
    return PoolResponse.model_validate(pool)


@router.get("/pools", response_model=list[PoolResponse])
async def list_pools(
    db: DbSession,
    array: Optional[str] = Query(default=None),
):
    stmt = select(Pool)
    if array:
        arr = await _resolve_array(db, array)
        stmt = stmt.where(Pool.array_id == arr.id)
    result = await db.execute(stmt)
    return [PoolResponse.model_validate(p) for p in result.scalars().all()]


@router.get("/pools/{pool_id}", response_model=PoolResponse)
async def get_pool(pool_id: str, db: DbSession):
    result = await db.execute(select(Pool).where(Pool.id == pool_id))
    pool = result.scalar_one_or_none()
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool {pool_id} not found")
    return PoolResponse.model_validate(pool)


@router.delete("/pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pool(pool_id: str, db: DbSession):
    result = await db.execute(select(Pool).where(Pool.id == pool_id))
    pool = result.scalar_one_or_none()
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool {pool_id} not found")

    vols_result = await db.execute(select(Volume).where(Volume.pool_id == pool_id))
    if vols_result.scalars().first():
        raise HTTPException(status_code=409, detail="Pool has volumes; delete them first")

    await db.delete(pool)
    await db.commit()


# ---------------------------------------------------------------------------
# Volumes  (API: size_gb,  DB: size_mb)
# ---------------------------------------------------------------------------

@router.post("/volumes", response_model=VolumeResponse, status_code=status.HTTP_201_CREATED)
async def create_volume(body: VolumeCreate, request: Request, db: DbSession):
    await check_fault("create_volume")

    pool_result = await db.execute(select(Pool).where(Pool.id == body.pool_id))
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool {body.pool_id} not found")

    # Inherit array from pool
    arr_result = await db.execute(select(Array).where(Array.id == pool.array_id))
    arr = arr_result.scalar_one_or_none()
    if arr is None:
        raise HTTPException(status_code=500, detail="Pool has no associated array")

    size_mb = body.size_gb * 1024

    volume = Volume(
        name=body.name,
        array_id=arr.id,
        pool_id=body.pool_id,
        size_mb=size_mb,
        status=VolumeStatus.creating,
    )
    db.add(volume)
    await db.flush()

    client = _spdk(request)
    try:
        bdev_name = await asyncio.to_thread(ensure_lvol, client, volume, pool.name, arr.name)
        volume.bdev_name = bdev_name
        volume.status = VolumeStatus.available
    except Exception as exc:
        logger.error("Failed to create lvol for volume %s: %s", volume.id, exc)
        volume.status = VolumeStatus.error
        await db.commit()
        raise HTTPException(status_code=500, detail=f"SPDK error: {exc}")

    await db.commit()
    await db.refresh(volume)
    return VolumeResponse.from_orm_volume(volume)


@router.get("/volumes", response_model=list[VolumeResponse])
async def list_volumes(
    db: DbSession,
    array: Optional[str] = Query(default=None),
):
    stmt = select(Volume)
    if array:
        arr = await _resolve_array(db, array)
        stmt = stmt.where(Volume.array_id == arr.id)
    result = await db.execute(stmt)
    return [VolumeResponse.from_orm_volume(v) for v in result.scalars().all()]


@router.get("/volumes/{volume_id}", response_model=VolumeResponse)
async def get_volume(volume_id: str, db: DbSession):
    result = await db.execute(select(Volume).where(Volume.id == volume_id))
    volume = result.scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Volume {volume_id} not found")
    return VolumeResponse.from_orm_volume(volume)


@router.delete("/volumes/{volume_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_volume(volume_id: str, request: Request, db: DbSession):
    await check_fault("delete_volume")

    result = await db.execute(select(Volume).where(Volume.id == volume_id))
    volume = result.scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Volume {volume_id} not found")

    maps_result = await db.execute(select(Mapping).where(Mapping.volume_id == volume_id))
    if maps_result.scalars().first():
        raise HTTPException(status_code=409, detail="Volume has active mappings; unmap first")

    volume.status = VolumeStatus.deleting
    await db.flush()

    client = _spdk(request)
    if volume.bdev_name:
        try:
            await asyncio.to_thread(delete_lvol, client, volume.bdev_name)
        except Exception as exc:
            logger.error("Failed to delete lvol %s: %s", volume.bdev_name, exc)
            volume.status = VolumeStatus.error
            await db.commit()
            raise HTTPException(status_code=500, detail=f"SPDK error: {exc}")

    await db.delete(volume)
    await db.commit()


@router.post("/volumes/{volume_id}/extend", response_model=VolumeResponse)
async def extend_volume(volume_id: str, body: VolumeExtend, request: Request, db: DbSession):
    await check_fault("extend_volume")

    result = await db.execute(select(Volume).where(Volume.id == volume_id))
    volume = result.scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Volume {volume_id} not found")

    new_size_mb = body.new_size_gb * 1024
    if new_size_mb <= volume.size_mb:
        raise HTTPException(status_code=400, detail="new_size_gb must be larger than current size")

    volume.status = VolumeStatus.extending
    await db.flush()

    client = _spdk(request)
    try:
        await asyncio.to_thread(resize_lvol, client, volume.bdev_name, new_size_mb)
        volume.size_mb = new_size_mb
        volume.status = VolumeStatus.available
    except Exception as exc:
        logger.error("Failed to resize lvol %s: %s", volume.bdev_name, exc)
        volume.status = VolumeStatus.error
        await db.commit()
        raise HTTPException(status_code=500, detail=f"SPDK error: {exc}")

    await db.commit()
    await db.refresh(volume)
    return VolumeResponse.from_orm_volume(volume)


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

@router.post("/hosts", response_model=HostResponse, status_code=status.HTTP_201_CREATED)
async def create_host(body: HostCreate, db: DbSession):
    await check_fault("create_host")

    host = Host(
        name=body.name,
        initiators_iscsi_iqns=json.dumps(body.initiators_iscsi_iqns),
        initiators_nvme_host_nqns=json.dumps(body.initiators_nvme_host_nqns),
        initiators_fc_wwpns=json.dumps(body.initiators_fc_wwpns),
    )
    db.add(host)
    await db.commit()
    await db.refresh(host)
    return HostResponse.from_orm_host(host)


@router.get("/hosts", response_model=list[HostResponse])
async def list_hosts(db: DbSession):
    result = await db.execute(select(Host))
    return [HostResponse.from_orm_host(h) for h in result.scalars().all()]


@router.get("/hosts/{host_id}", response_model=HostResponse)
async def get_host(host_id: str, db: DbSession):
    result = await db.execute(select(Host).where(Host.id == host_id))
    host = result.scalar_one_or_none()
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host {host_id} not found")
    return HostResponse.from_orm_host(host)


@router.patch("/hosts/{host_id}", response_model=HostResponse)
async def update_host(host_id: str, body: HostUpdate, db: DbSession):
    result = await db.execute(select(Host).where(Host.id == host_id))
    host = result.scalar_one_or_none()
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host {host_id} not found")
    if body.initiators_iscsi_iqns is not None:
        host.initiators_iscsi_iqns = json.dumps(body.initiators_iscsi_iqns)
    if body.initiators_nvme_host_nqns is not None:
        host.initiators_nvme_host_nqns = json.dumps(body.initiators_nvme_host_nqns)
    if body.initiators_fc_wwpns is not None:
        host.initiators_fc_wwpns = json.dumps(body.initiators_fc_wwpns)
    await db.commit()
    await db.refresh(host)
    return HostResponse.from_orm_host(host)


@router.delete("/hosts/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_host(host_id: str, db: DbSession):
    result = await db.execute(select(Host).where(Host.id == host_id))
    host = result.scalar_one_or_none()
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host {host_id} not found")
    maps_result = await db.execute(select(Mapping).where(Mapping.host_id == host_id))
    if maps_result.scalars().first():
        raise HTTPException(
            status_code=409,
            detail=f"Host {host_id} has active mappings; remove them first",
        )
    await db.delete(host)
    await db.commit()


# ---------------------------------------------------------------------------
# Attachments (compute-side agent polling)
# ---------------------------------------------------------------------------

@router.get("/hosts/{host_id}/attachments", response_model=AttachmentsResponse)
async def host_attachments(host_id: str, db: DbSession):
    """Return all active attachments for a host.

    This is the primary polling endpoint for the compute-side agent.
    """
    result = await db.execute(select(Host).where(Host.id == host_id))
    host = result.scalar_one_or_none()
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host {host_id} not found")
    return await get_host_attachments(db, host_id)


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

@router.post("/mappings", response_model=MappingResponse, status_code=status.HTTP_201_CREATED)
async def create_mapping(body: MappingCreate, request: Request, db: DbSession):
    await check_fault("create_mapping")
    client = _spdk(request)
    try:
        mapping = await svc_create_mapping(
            db, client, settings,
            host_id=body.host_id,
            volume_id=body.volume_id,
            persona_endpoint_id=body.persona_endpoint_id,
            underlay_endpoint_id=body.underlay_endpoint_id,
            persona_protocol=body.persona_protocol,
            underlay_protocol=body.underlay_protocol,
        )
    except ValueError as exc:
        # Status mismatch → 409, other validation → 400
        code = 409 if "status" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (RuntimeError, SPDKError) as exc:
        raise HTTPException(status_code=500, detail=f"SPDK error: {exc}")
    await db.commit()
    return MappingResponse.model_validate(mapping)


@router.delete("/mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mapping_route(mapping_id: str, request: Request, db: DbSession):
    await check_fault("delete_mapping")
    client = _spdk(request)
    try:
        await svc_delete_mapping(db, client, mapping_id)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    await db.commit()


@router.get("/mappings", response_model=list[MappingResponse])
async def list_mappings(
    db: DbSession,
    array: Optional[str] = Query(default=None),
):
    stmt = select(Mapping)
    if array:
        arr = await _resolve_array(db, array)
        # Filter mappings by volume.array_id (via join)
        stmt = stmt.join(Volume, Mapping.volume_id == Volume.id).where(Volume.array_id == arr.id)
    result = await db.execute(stmt)
    return [MappingResponse.model_validate(m) for m in result.scalars().all()]


# ---------------------------------------------------------------------------
# SVC façade run
# ---------------------------------------------------------------------------

@router.post("/svc/run", response_model=SvcRunResponse)
async def svc_run(body: SvcRunRequest, request: Request, db: DbSession):
    """Execute an IBM SVC façade command against a named array."""
    import io
    import json as _json

    from apollo_gateway.compat.ibm_svc.handlers import SvcContext, dispatch
    from apollo_gateway.core.personas import merge_profile

    await check_fault("svc_run")
    arr = await _resolve_array(db, body.array)
    spdk = _spdk(request)
    profile_data = _json.loads(arr.profile) if isinstance(arr.profile, str) else arr.profile
    profile = merge_profile(arr.vendor, profile_data)
    ctx = SvcContext(
        session=db,
        spdk=spdk,
        array_id=arr.id,
        array_name=arr.name,
        effective_profile=profile.model_dump(),
    )
    out_buf, err_buf = io.StringIO(), io.StringIO()
    exit_code = await dispatch(body.command, ctx, stdout=out_buf, stderr=err_buf)
    return SvcRunResponse(
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
        exit_code=exit_code,
    )
