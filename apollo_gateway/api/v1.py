# FILE: apollo_gateway/api/v1.py
"""Apollo Gateway v1 REST API routes."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.config import settings
from apollo_gateway.core.capabilities import assert_protocol_allowed
from apollo_gateway.core.db import ExportContainer, Host, Mapping, Pool, Subsystem, Volume, get_session
from apollo_gateway.core.faults import FaultInjectionError, check_fault
from apollo_gateway.core.models import (
    ConnectionInfoIscsi,
    ConnectionInfoNvmeof,
    HostCreate,
    HostResponse,
    MappingCreate,
    MappingResponse,
    PoolCreate,
    PoolResponse,
    Protocol,
    VolumeCreate,
    VolumeExtend,
    VolumeResponse,
    VolumeStatus,
)
from apollo_gateway.spdk.ensure import (
    allocate_lun,
    allocate_nsid,
    delete_lvol,
    ensure_iscsi_export,
    ensure_iscsi_mapping,
    ensure_lvol,
    ensure_nvmef_export,
    ensure_nvmef_mapping,
    ensure_pool,
    resize_lvol,
)
from apollo_gateway.spdk import iscsi as iscsi_rpc
from apollo_gateway.spdk import nvmf as nvmf_rpc

logger = logging.getLogger("apollo_gateway.api.v1")

router = APIRouter(prefix="/v1", tags=["v1"])

DbSession = Annotated[AsyncSession, Depends(get_session)]


def _spdk(request: Request):
    return request.app.state.spdk_client


async def _resolve_default_subsystem(db: AsyncSession) -> Subsystem:
    """Return the 'default' subsystem. Raises 500 if it doesn't exist."""
    result = await db.execute(select(Subsystem).where(Subsystem.name == "default"))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(
            status_code=500,
            detail="Default subsystem not found. Gateway may still be initialising.",
        )
    return sub


async def _resolve_subsystem(db: AsyncSession, name_or_id: str) -> Subsystem:
    """Resolve a subsystem by name or UUID. Raises 404 if not found."""
    result = await db.execute(select(Subsystem).where(Subsystem.id == name_or_id))
    sub = result.scalar_one_or_none()
    if sub is not None:
        return sub
    result = await db.execute(select(Subsystem).where(Subsystem.name == name_or_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail=f"Subsystem '{name_or_id}' not found")
    return sub


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

    # Resolve subsystem
    if body.subsystem:
        sub = await _resolve_subsystem(db, body.subsystem)
    else:
        sub = await _resolve_default_subsystem(db)

    # Check for duplicate name within this subsystem
    existing = await db.execute(
        select(Pool).where(Pool.subsystem_id == sub.id, Pool.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Pool '{body.name}' already exists in subsystem '{sub.name}'",
        )

    pool = Pool(
        name=body.name,
        subsystem_id=sub.id,
        backend_type=body.backend_type,
        size_mb=body.size_mb,
        aio_path=body.aio_path,
    )
    db.add(pool)
    await db.flush()  # get the id

    client = _spdk(request)
    try:
        await asyncio.to_thread(ensure_pool, client, pool, sub.name)
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
    subsystem: Optional[str] = Query(default=None),
):
    stmt = select(Pool)
    if subsystem:
        sub = await _resolve_subsystem(db, subsystem)
        stmt = stmt.where(Pool.subsystem_id == sub.id)
    result = await db.execute(stmt)
    return [PoolResponse.model_validate(p) for p in result.scalars().all()]


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------

@router.post("/volumes", response_model=VolumeResponse, status_code=status.HTTP_201_CREATED)
async def create_volume(body: VolumeCreate, request: Request, db: DbSession):
    await check_fault("create_volume")

    pool_result = await db.execute(select(Pool).where(Pool.id == body.pool_id))
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise HTTPException(status_code=404, detail=f"Pool {body.pool_id} not found")

    # Inherit subsystem from pool
    sub = pool.subsystem

    volume = Volume(
        name=body.name,
        subsystem_id=sub.id,
        pool_id=body.pool_id,
        size_mb=body.size_mb,
        status=VolumeStatus.creating,
    )
    db.add(volume)
    await db.flush()

    client = _spdk(request)
    try:
        bdev_name = await asyncio.to_thread(ensure_lvol, client, volume, pool.name, sub.name)
        volume.bdev_name = bdev_name
        volume.status = VolumeStatus.available
    except Exception as exc:
        logger.error("Failed to create lvol for volume %s: %s", volume.id, exc)
        volume.status = VolumeStatus.error
        await db.commit()
        raise HTTPException(status_code=500, detail=f"SPDK error: {exc}")

    await db.commit()
    await db.refresh(volume)
    return VolumeResponse.model_validate(volume)


@router.get("/volumes", response_model=list[VolumeResponse])
async def list_volumes(
    db: DbSession,
    subsystem: Optional[str] = Query(default=None),
):
    stmt = select(Volume)
    if subsystem:
        sub = await _resolve_subsystem(db, subsystem)
        stmt = stmt.where(Volume.subsystem_id == sub.id)
    result = await db.execute(stmt)
    return [VolumeResponse.model_validate(v) for v in result.scalars().all()]


@router.get("/volumes/{volume_id}", response_model=VolumeResponse)
async def get_volume(volume_id: str, db: DbSession):
    result = await db.execute(select(Volume).where(Volume.id == volume_id))
    volume = result.scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Volume {volume_id} not found")
    return VolumeResponse.model_validate(volume)


@router.delete("/volumes/{volume_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_volume(volume_id: str, request: Request, db: DbSession):
    await check_fault("delete_volume")

    result = await db.execute(select(Volume).where(Volume.id == volume_id))
    volume = result.scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Volume {volume_id} not found")

    # Check no active mappings
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

    if body.new_size_mb <= volume.size_mb:
        raise HTTPException(status_code=400, detail="new_size_mb must be larger than current size")

    volume.status = VolumeStatus.extending
    await db.flush()

    client = _spdk(request)
    try:
        await asyncio.to_thread(resize_lvol, client, volume.bdev_name, body.new_size_mb)
        volume.size_mb = body.new_size_mb
        volume.status = VolumeStatus.available
    except Exception as exc:
        logger.error("Failed to resize lvol %s: %s", volume.bdev_name, exc)
        volume.status = VolumeStatus.error
        await db.commit()
        raise HTTPException(status_code=500, detail=f"SPDK error: {exc}")

    await db.commit()
    await db.refresh(volume)
    return VolumeResponse.model_validate(volume)


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

@router.post("/hosts", response_model=HostResponse, status_code=status.HTTP_201_CREATED)
async def create_host(body: HostCreate, db: DbSession):
    await check_fault("create_host")

    host = Host(name=body.name, iqn=body.iqn, nqn=body.nqn)
    db.add(host)
    await db.commit()
    await db.refresh(host)
    return HostResponse.model_validate(host)


@router.get("/hosts", response_model=list[HostResponse])
async def list_hosts(db: DbSession):
    result = await db.execute(select(Host))
    return [HostResponse.model_validate(h) for h in result.scalars().all()]


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

@router.post("/mappings", response_model=MappingResponse, status_code=status.HTTP_201_CREATED)
async def create_mapping(body: MappingCreate, request: Request, db: DbSession):
    await check_fault("create_mapping")

    # Validate volume
    vol_result = await db.execute(select(Volume).where(Volume.id == body.volume_id))
    volume = vol_result.scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Volume {body.volume_id} not found")
    if volume.status not in (VolumeStatus.available, VolumeStatus.in_use):
        raise HTTPException(status_code=409, detail=f"Volume status is {volume.status}, expected available/in_use")

    # Validate host
    host_result = await db.execute(select(Host).where(Host.id == body.host_id))
    host = host_result.scalar_one_or_none()
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host {body.host_id} not found")

    # Resolve subsystem from volume
    sub_result = await db.execute(select(Subsystem).where(Subsystem.id == volume.subsystem_id))
    sub = sub_result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=500, detail="Volume has no associated subsystem")

    # Validate protocol is allowed by subsystem
    assert_protocol_allowed(sub, body.protocol.value)

    # Find or create ExportContainer for (protocol, host_id, subsystem_id)
    ec_result = await db.execute(
        select(ExportContainer).where(
            ExportContainer.protocol == body.protocol,
            ExportContainer.host_id == body.host_id,
            ExportContainer.subsystem_id == sub.id,
        )
    )
    ec = ec_result.scalar_one_or_none()

    client = _spdk(request)

    if ec is None:
        if body.protocol == Protocol.iscsi:
            portal_ip = settings.iscsi_portal_ip
            portal_port = settings.iscsi_portal_port
        else:
            portal_ip = settings.nvmef_portal_ip
            portal_port = settings.nvmef_portal_port

        ec = ExportContainer(
            subsystem_id=sub.id,
            protocol=body.protocol,
            host_id=body.host_id,
            portal_ip=portal_ip,
            portal_port=portal_port,
        )
        db.add(ec)
        await db.flush()  # get ec.id

        # Assign IQN/NQN incorporating subsystem name for disambiguation
        if body.protocol == Protocol.iscsi:
            ec.target_iqn = f"{settings.iqn_prefix}:{sub.name}:{ec.id}"
        else:
            ec.target_nqn = f"{settings.nqn_prefix}:{sub.name}:{ec.id}"
        await db.flush()

        # Create SPDK target/subsystem
        try:
            if body.protocol == Protocol.iscsi:
                await asyncio.to_thread(ensure_iscsi_export, client, ec, settings)
            else:
                profile_dict = json.loads(sub.capability_profile)
                from apollo_gateway.core.personas import merge_profile
                profile = merge_profile(sub.persona, profile_dict)
                await asyncio.to_thread(
                    ensure_nvmef_export, client, ec, settings,
                    profile.model, f"APOLLO-{sub.name[:8].upper()}"
                )
        except Exception as exc:
            logger.error("Failed to create export container %s: %s", ec.id, exc)
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"SPDK error: {exc}")

    # Allocate LUN / NSID
    existing_maps_result = await db.execute(
        select(Mapping).where(Mapping.export_container_id == ec.id)
    )
    existing_maps = existing_maps_result.scalars().all()

    if body.protocol == Protocol.iscsi:
        used_luns = [m.lun_id for m in existing_maps if m.lun_id is not None]
        lun_id = allocate_lun(used_luns)
        ns_id = None
    else:
        used_nsids = [m.ns_id for m in existing_maps if m.ns_id is not None]
        ns_id = allocate_nsid(used_nsids)
        lun_id = None

    mapping = Mapping(
        subsystem_id=sub.id,
        volume_id=body.volume_id,
        host_id=body.host_id,
        export_container_id=ec.id,
        protocol=body.protocol,
        lun_id=lun_id,
        ns_id=ns_id,
    )
    db.add(mapping)
    await db.flush()

    # Attach in SPDK
    try:
        if body.protocol == Protocol.iscsi:
            await asyncio.to_thread(ensure_iscsi_mapping, client, mapping, volume, ec)
        else:
            await asyncio.to_thread(ensure_nvmef_mapping, client, mapping, volume, ec)
    except Exception as exc:
        logger.error("Failed to attach volume %s to mapping %s: %s", volume.id, mapping.id, exc)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"SPDK error: {exc}")

    # Mark volume in_use
    volume.status = VolumeStatus.in_use
    await db.commit()
    await db.refresh(mapping)
    return MappingResponse.model_validate(mapping)


@router.delete("/mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mapping(mapping_id: str, request: Request, db: DbSession):
    await check_fault("delete_mapping")

    map_result = await db.execute(select(Mapping).where(Mapping.id == mapping_id))
    mapping = map_result.scalar_one_or_none()
    if mapping is None:
        raise HTTPException(status_code=404, detail=f"Mapping {mapping_id} not found")

    ec = mapping.export_container
    volume = mapping.volume
    client = _spdk(request)

    try:
        if mapping.protocol == Protocol.iscsi:
            await asyncio.to_thread(
                iscsi_rpc.delete_target_node, client, ec.target_iqn
            )
        else:
            await asyncio.to_thread(nvmf_rpc.remove_namespace, client, ec.target_nqn, mapping.ns_id)
    except Exception as exc:
        logger.error("Failed to remove SPDK mapping %s: %s", mapping_id, exc)
        raise HTTPException(status_code=500, detail=f"SPDK error: {exc}")

    await db.delete(mapping)

    # If no more mappings on this volume, reset volume status
    remaining = await db.execute(
        select(Mapping).where(Mapping.volume_id == volume.id)
    )
    if not remaining.scalars().first():
        volume.status = VolumeStatus.available

    await db.commit()


@router.get("/mappings", response_model=list[MappingResponse])
async def list_mappings(
    db: DbSession,
    subsystem: Optional[str] = Query(default=None),
):
    stmt = select(Mapping)
    if subsystem:
        sub = await _resolve_subsystem(db, subsystem)
        stmt = stmt.where(Mapping.subsystem_id == sub.id)
    result = await db.execute(stmt)
    return [MappingResponse.model_validate(m) for m in result.scalars().all()]


@router.get("/mappings/{mapping_id}/connection-info")
async def get_connection_info(mapping_id: str, db: DbSession):
    map_result = await db.execute(select(Mapping).where(Mapping.id == mapping_id))
    mapping = map_result.scalar_one_or_none()
    if mapping is None:
        raise HTTPException(status_code=404, detail=f"Mapping {mapping_id} not found")

    ec = mapping.export_container

    if mapping.protocol == Protocol.iscsi:
        portal = f"{ec.portal_ip}:{ec.portal_port}"
        return ConnectionInfoIscsi(
            driver_volume_type="iscsi",
            data={
                "target_iqn": ec.target_iqn,
                "target_portal": portal,
                "target_lun": mapping.lun_id,
                "access_mode": "rw",
                "discard": True,
            },
        )
    else:
        portal = f"{ec.portal_ip}:{ec.portal_port}"
        return ConnectionInfoNvmeof(
            driver_volume_type="nvmeof",
            data={
                "target_nqn": ec.target_nqn,
                "transport_type": "tcp",
                "target_portal": portal,
                "ns_id": mapping.ns_id,
                "access_mode": "rw",
            },
        )
