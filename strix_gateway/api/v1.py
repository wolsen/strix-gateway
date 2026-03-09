# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Apollo Gateway v1 REST API routes.

v0.3 — Routes delegate to canonical core services.  No inline DB queries
or SPDK calls remain in this module.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.config import settings
from strix_gateway.core.db import get_session
from strix_gateway.core.exceptions import (
    AlreadyExistsError,
    BackendError,
    CoreError,
    InvalidStateError,
    NotFoundError,
    ResourceInUseError,
    ValidationError,
)
from strix_gateway.core.faults import check_fault
from strix_gateway.core.models import (
    AttachmentsResponse,
    HostCreate,
    HostResponse,
    HostUpdate,
    MappingCreate,
    MappingResponse,
    PoolCreate,
    PoolResponse,
    SvcRunRequest,
    SvcRunResponse,
    VolumeCreate,
    VolumeExtend,
    VolumeResponse,
)
from strix_gateway.core import (
    arrays as arrays_svc,
    hosts as hosts_svc,
    mappings as mappings_svc,
    pools as pools_svc,
    volumes as volumes_svc,
)
from strix_gateway.personalities.errors import core_to_http
from strix_gateway.personalities.svc.handlers import SvcContext, dispatch as svc_dispatch
from strix_gateway.core.personas import merge_profile

logger = logging.getLogger("strix_gateway.api.v1")

router = APIRouter(prefix="/v1", tags=["v1"])

DbSession = Annotated[AsyncSession, Depends(get_session)]


def _spdk(request: Request):
    return request.app.state.spdk_client


def _raise(exc: CoreError) -> None:
    """Translate a core exception to an HTTP exception and raise it."""
    raise core_to_http(exc)


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

@router.post("/pools", response_model=PoolResponse, status_code=status.HTTP_201_CREATED)
async def create_pool(body: PoolCreate, request: Request, db: DbSession):
    await check_fault("create_pool")
    try:
        arr = await arrays_svc.get_default_array(db)
        pool = await pools_svc.create_pool(
            db, _spdk(request),
            name=body.name,
            array_id=arr.id,
            backend_type=body.backend_type,
            size_mb=body.size_mb,
            aio_path=body.aio_path,
        )
    except CoreError as exc:
        _raise(exc)
    await db.commit()
    await db.refresh(pool)
    return PoolResponse.model_validate(pool)


@router.get("/pools", response_model=list[PoolResponse])
async def list_pools(
    db: DbSession,
    array: Optional[str] = Query(default=None),
):
    try:
        array_id = None
        if array:
            arr = await arrays_svc.resolve_array(db, array)
            array_id = arr.id
        result = await pools_svc.list_pools(db, array_id=array_id)
    except CoreError as exc:
        _raise(exc)
    return [PoolResponse.model_validate(p) for p in result]


@router.get("/pools/{pool_id}", response_model=PoolResponse)
async def get_pool(pool_id: str, db: DbSession):
    try:
        pool = await pools_svc.get_pool(db, pool_id)
    except CoreError as exc:
        _raise(exc)
    return PoolResponse.model_validate(pool)


@router.delete("/pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pool(pool_id: str, db: DbSession):
    try:
        await pools_svc.delete_pool(db, pool_id)
    except CoreError as exc:
        _raise(exc)
    await db.commit()


# ---------------------------------------------------------------------------
# Volumes  (API: size_gb,  DB: size_mb)
# ---------------------------------------------------------------------------

@router.post("/volumes", response_model=VolumeResponse, status_code=status.HTTP_201_CREATED)
async def create_volume(body: VolumeCreate, request: Request, db: DbSession):
    await check_fault("create_volume")
    try:
        volume = await volumes_svc.create_volume(
            db, _spdk(request),
            name=body.name,
            pool_id=body.pool_id,
            size_mb=body.size_gb * 1024,
        )
    except CoreError as exc:
        await db.commit()  # persist error-state volume so it can be cleaned up
        _raise(exc)
    await db.commit()
    await db.refresh(volume)
    return VolumeResponse.from_orm_volume(volume)


@router.get("/volumes", response_model=list[VolumeResponse])
async def list_volumes(
    db: DbSession,
    array: Optional[str] = Query(default=None),
):
    try:
        array_id = None
        if array:
            arr = await arrays_svc.resolve_array(db, array)
            array_id = arr.id
        result = await volumes_svc.list_volumes(db, array_id=array_id)
    except CoreError as exc:
        _raise(exc)
    return [VolumeResponse.from_orm_volume(v) for v in result]


@router.get("/volumes/{volume_id}", response_model=VolumeResponse)
async def get_volume(volume_id: str, db: DbSession):
    try:
        volume = await volumes_svc.get_volume(db, volume_id)
    except CoreError as exc:
        _raise(exc)
    return VolumeResponse.from_orm_volume(volume)


@router.delete("/volumes/{volume_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_volume(volume_id: str, request: Request, db: DbSession):
    await check_fault("delete_volume")
    try:
        await volumes_svc.delete_volume(db, _spdk(request), volume_id)
    except CoreError as exc:
        _raise(exc)
    await db.commit()


@router.post("/volumes/{volume_id}/extend", response_model=VolumeResponse)
async def extend_volume(volume_id: str, body: VolumeExtend, request: Request, db: DbSession):
    await check_fault("extend_volume")
    try:
        volume = await volumes_svc.extend_volume(
            db, _spdk(request), volume_id, body.new_size_gb * 1024,
        )
    except CoreError as exc:
        await db.commit()  # persist error-state volume
        _raise(exc)
    await db.commit()
    await db.refresh(volume)
    return VolumeResponse.from_orm_volume(volume)


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

@router.post("/hosts", response_model=HostResponse, status_code=status.HTTP_201_CREATED)
async def create_host(body: HostCreate, db: DbSession):
    await check_fault("create_host")
    try:
        host = await hosts_svc.create_host(
            db,
            name=body.name,
            iscsi_iqns=body.initiators_iscsi_iqns,
            nvme_nqns=body.initiators_nvme_host_nqns,
            fc_wwpns=body.initiators_fc_wwpns,
        )
    except CoreError as exc:
        _raise(exc)
    await db.commit()
    await db.refresh(host)
    return HostResponse.from_orm_host(host)


@router.get("/hosts", response_model=list[HostResponse])
async def list_hosts(db: DbSession):
    hosts = await hosts_svc.list_hosts(db)
    return [HostResponse.from_orm_host(h) for h in hosts]


@router.get("/hosts/{host_id}", response_model=HostResponse)
async def get_host(host_id: str, db: DbSession):
    try:
        host = await hosts_svc.get_host(db, host_id)
    except CoreError as exc:
        _raise(exc)
    return HostResponse.from_orm_host(host)


@router.patch("/hosts/{host_id}", response_model=HostResponse)
async def update_host(host_id: str, body: HostUpdate, db: DbSession):
    try:
        host = await hosts_svc.update_host_initiators(
            db, host_id,
            iscsi_iqns=body.initiators_iscsi_iqns,
            nvme_nqns=body.initiators_nvme_host_nqns,
            fc_wwpns=body.initiators_fc_wwpns,
        )
    except CoreError as exc:
        _raise(exc)
    await db.commit()
    await db.refresh(host)
    return HostResponse.from_orm_host(host)


@router.delete("/hosts/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_host(host_id: str, db: DbSession):
    try:
        await hosts_svc.delete_host(db, host_id)
    except CoreError as exc:
        _raise(exc)
    await db.commit()


# ---------------------------------------------------------------------------
# Attachments (compute-side agent polling)
# ---------------------------------------------------------------------------

@router.get("/hosts/{host_id}/attachments", response_model=AttachmentsResponse)
async def host_attachments(host_id: str, db: DbSession):
    """Return all active attachments for a host.

    This is the primary polling endpoint for the compute-side agent.
    """
    try:
        return await mappings_svc.get_host_attachments(db, host_id)
    except CoreError as exc:
        _raise(exc)


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

@router.post("/mappings", response_model=MappingResponse, status_code=status.HTTP_201_CREATED)
async def create_mapping(body: MappingCreate, request: Request, db: DbSession):
    await check_fault("create_mapping")
    try:
        mapping = await mappings_svc.create_mapping(
            db, _spdk(request), settings,
            host_id=body.host_id,
            volume_id=body.volume_id,
            persona_endpoint_id=body.persona_endpoint_id,
            underlay_endpoint_id=body.underlay_endpoint_id,
            persona_protocol=body.persona_protocol,
            underlay_protocol=body.underlay_protocol,
        )
    except CoreError as exc:
        _raise(exc)
    await db.commit()
    return MappingResponse.model_validate(mapping)


@router.delete("/mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mapping_route(mapping_id: str, request: Request, db: DbSession):
    await check_fault("delete_mapping")
    try:
        await mappings_svc.delete_mapping(db, _spdk(request), mapping_id)
    except CoreError as exc:
        _raise(exc)
    await db.commit()


@router.get("/mappings", response_model=list[MappingResponse])
async def list_mappings(
    db: DbSession,
    array: Optional[str] = Query(default=None),
):
    try:
        array_id = None
        if array:
            arr = await arrays_svc.resolve_array(db, array)
            array_id = arr.id
        result = await mappings_svc.list_mappings(db, array_id=array_id)
    except CoreError as exc:
        _raise(exc)
    return [MappingResponse.model_validate(m) for m in result]


# ---------------------------------------------------------------------------
# SVC façade run (remote shell execution)
# ---------------------------------------------------------------------------

@router.post("/svc/run", response_model=SvcRunResponse)
async def svc_run(body: SvcRunRequest, request: Request, db: DbSession):
    import io

    await check_fault("svc_run")
    try:
        arr = await arrays_svc.resolve_array(db, body.array)
    except CoreError as exc:
        _raise(exc)

    profile = merge_profile(arr.vendor, arr.profile_dict)
    ctx = SvcContext(
        session=db,
        spdk=_spdk(request),
        array_id=arr.id,
        array_name=arr.name,
        effective_profile=profile.model_dump(),
    )

    out_buf, err_buf = io.StringIO(), io.StringIO()
    exit_code = await svc_dispatch(body.command, ctx, stdout=out_buf, stderr=err_buf)
    return SvcRunResponse(
        stdout=out_buf.getvalue(),
        stderr=err_buf.getvalue(),
        exit_code=exit_code,
    )

