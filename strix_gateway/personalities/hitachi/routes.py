# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Hitachi Configuration Manager REST API routes.

All routes live under ``/ConfigurationManager/v1/objects``.  Mutating
operations return ``202 Accepted`` with a ``Location`` header pointing
to the job resource — the job is always already completed because Strix
core operations are synchronous.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.core.db import (
    Host,
    Mapping,
    Pool,
    TransportEndpoint,
    Volume,
    get_session,
    get_session_factory,
)
from strix_gateway.core.exceptions import CoreError, NotFoundError, ValidationError
from strix_gateway.core import (
    hosts as hosts_svc,
    mappings as mappings_svc,
    pools as pools_svc,
    volumes as volumes_svc,
)
from strix_gateway.personalities.hitachi.jobs import JobTracker
from strix_gateway.personalities.hitachi.models import (
    AddHostIscsiRequest,
    AddIscsiNameRequest,
    AddWwnRequest,
    CreateHostGroupRequest,
    CreateIscsiTargetRequest,
    CreateLdevRequest,
    CreateLunRequest,
    ExpandLdevRequest,
    ModifyLdevRequest,
)
from strix_gateway.personalities.hitachi.sessions import SessionStore
from strix_gateway.personalities.hitachi.translate import HitachiIdMapper

logger = logging.getLogger("strix_gateway.personalities.hitachi.routes")

router = APIRouter(prefix="/ConfigurationManager/v1/objects")

# Rebuilding mapper state on every request can overload SQLite-backed E2E
# environments and cause transient transport failures. Refresh periodically.
_MAPPER_REFRESH_INTERVAL_SEC = 5.0

DbSession = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def _get_array_id(request: Request) -> str:
    """Extract the canonical array ID from the vhost state."""
    array_info = request.scope.get("state", {}).get("array")
    if array_info is None:
        raise ValidationError("No array context — request must arrive via vendor vhost")
    return array_info.id


def _get_state(request: Request):
    """Resolve application state for personality routes.

    Requests can be dispatched through middleware stacks where `request.app`
    and `scope['app']` differ. Prefer request.app, then fall back to scope app.
    """
    if hasattr(request.app, "state"):
        return request.app.state
    scope_app = request.scope.get("app")
    if scope_app is not None and hasattr(scope_app, "state"):
        return scope_app.state
    raise ValidationError("Application state unavailable")


def _get_sessions(request: Request) -> SessionStore:
    state = _get_state(request)
    store = getattr(state, "hitachi_sessions", None)
    if store is None:
        store = SessionStore()
        state.hitachi_sessions = store
    return store


def _get_jobs(request: Request) -> JobTracker:
    state = _get_state(request)
    jobs = getattr(state, "hitachi_jobs", None)
    if jobs is None:
        jobs = JobTracker()
        state.hitachi_jobs = jobs
    return jobs


async def _get_mapper(request: Request) -> HitachiIdMapper:
    state = _get_state(request)
    mappers = getattr(state, "hitachi_mappers", None)
    if mappers is None:
        mappers = {}
        state.hitachi_mappers = mappers

    array_id = _get_array_id(request)
    mapper = mappers.get(array_id)
    if mapper is None:
        mapper = HitachiIdMapper(array_id)
        mappers[array_id] = mapper

    # Refresh mapper state periodically with a per-array async lock. This keeps
    # IDs aligned after topology changes without forcing DB rebuild on every
    # request (which is noisy under Cinder polling load).
    meta = getattr(state, "hitachi_mapper_meta", None)
    if meta is None:
        meta = {}
        state.hitachi_mapper_meta = meta

    locks = getattr(state, "hitachi_mapper_locks", None)
    if locks is None:
        locks = {}
        state.hitachi_mapper_locks = locks

    lock = locks.setdefault(array_id, asyncio.Lock())
    last_refresh = float(meta.get(array_id, 0.0))
    now = time.monotonic()
    should_refresh = (now - last_refresh) >= _MAPPER_REFRESH_INTERVAL_SEC

    if should_refresh:
        async with lock:
            # Re-check after lock acquisition to avoid duplicate rebuilds.
            last_refresh = float(meta.get(array_id, 0.0))
            now = time.monotonic()
            should_refresh = (now - last_refresh) >= _MAPPER_REFRESH_INTERVAL_SEC
            if should_refresh:
                sf = get_session_factory()
                try:
                    async with sf() as session:
                        await mapper.rebuild(session)
                        await session.commit()
                except Exception:
                    # Keep serving with last known mapper state if refresh fails.
                    logger.exception("Hitachi mapper refresh failed for array=%s", array_id)
                else:
                    meta[array_id] = time.monotonic()
    return mapper


async def _resolve_host_group_iscsi_name(
    db: AsyncSession,
    mapper: HitachiIdMapper,
    host: Host,
    port_id: str,
) -> str | None:
    """Resolve iSCSI target IQN for a host-group view.

    Cinder's Hitachi iSCSI driver expects `iscsiName` on host-group payloads
    for iSCSI ports. Prefer explicit per-port host metadata when present,
    then fall back to the endpoint target IQN.
    """
    meta = host.vendor_meta_dict
    configured_name = meta.get("hitachi_iscsi_names", {}).get(port_id)
    if configured_name:
        return configured_name

    ep_uuid = mapper.endpoint_for_port(port_id)
    if ep_uuid is None:
        return None

    result = await db.execute(select(TransportEndpoint).where(TransportEndpoint.id == ep_uuid))
    endpoint = result.scalar_one_or_none()
    if endpoint is None or endpoint.protocol != "iscsi":
        return None

    targets = endpoint.targets_dict
    target_iqn = targets.get("target_iqn")
    if isinstance(target_iqn, str) and target_iqn:
        return target_iqn
    return None


def _get_spdk(request: Request):
    state = _get_state(request)
    spdk = getattr(state, "spdk_client", None)
    if spdk is not None:
        return spdk

    # Personality sub-app state may not carry shared root-app clients.
    scope_app = request.scope.get("app")
    if scope_app is not None and hasattr(scope_app, "state"):
        spdk = getattr(scope_app.state, "spdk_client", None)
        if spdk is not None:
            return spdk

    raise ValidationError("SPDK client unavailable in application state")


async def require_session(
    request: Request,
    authorization: str = Header(default=""),
    session_token: str = Header(default="", alias="Session-Token"),
) -> None:
    """Validate ``Authorization: Session <token>`` header.

    Session endpoints (POST/DELETE /sessions) skip this dependency.
    """
    if authorization.startswith("Session "):
        token = authorization[len("Session "):]
    elif session_token:
        token = session_token
    else:
        return _unauthorized()
    store = _get_sessions(request)
    info = store.validate(token)
    if info is None:
        return _unauthorized()
    # Attach session info to request state for downstream use
    request.state.hitachi_session = info


def _unauthorized():
    raise CoreError("Session token is invalid or expired")


def _job_location(request: Request, job_id: int) -> str:
    return f"/ConfigurationManager/v1/objects/jobs/{job_id}"


def _accepted(
    request: Request,
    job_id: int,
    affected_resources: list[str] | None = None,
) -> JSONResponse:
    """Return 202 Accepted with Location header."""
    payload = {
        "jobId": job_id,
        "status": "Completed",
        "state": "Succeeded",
        "statusResource": _job_location(request, job_id),
    }
    if affected_resources:
        payload["affectedResources"] = affected_resources

    return JSONResponse(
        status_code=202,
        content=payload,
        headers={"Location": _job_location(request, job_id)},
    )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@router.post("/sessions")
async def create_session(request: Request):
    store = _get_sessions(request)
    info = store.create()
    return JSONResponse(
        status_code=200,
        content={
            "sessionId": info.session_id,
            "token": info.token,
        },
    )


@router.post("/storages/{storage_device_id}/sessions")
async def create_storage_session(storage_device_id: str, request: Request):
    mapper = await _get_mapper(request)
    if mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    return await create_session(request)


@router.get("/storages/{storage_device_id}/sessions/{session_id}")
async def get_storage_session(storage_device_id: str, session_id: int, request: Request):
    mapper = await _get_mapper(request)
    if mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    return {"sessionId": session_id}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int, request: Request):
    store = _get_sessions(request)
    store.delete(session_id)
    return Response(status_code=200)


@router.delete("/storages/{storage_device_id}/sessions/{session_id}")
async def delete_storage_session(storage_device_id: str, session_id: int, request: Request):
    mapper = await _get_mapper(request)
    if mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    return await delete_session(session_id, request)


# ---------------------------------------------------------------------------
# Storage systems
# ---------------------------------------------------------------------------

@router.get("/storages", dependencies=[Depends(require_session)])
async def list_storages(request: Request, db: DbSession):
    array_id = _get_array_id(request)
    mapper = await _get_mapper(request)

    from strix_gateway.core.db import Array
    result = await db.execute(select(Array).where(Array.id == array_id))
    arr = result.scalar_one_or_none()
    if arr is None:
        raise NotFoundError("Array", array_id)

    return {"data": [mapper.array_to_storage(arr)]}


@router.get("/storages/{storage_device_id}", dependencies=[Depends(require_session)])
async def get_storage(storage_device_id: str, request: Request, db: DbSession):
    array_id = _get_array_id(request)
    mapper = await _get_mapper(request)

    from strix_gateway.core.db import Array
    result = await db.execute(select(Array).where(Array.id == array_id))
    arr = result.scalar_one_or_none()
    if arr is None:
        raise NotFoundError("Array", array_id)

    if mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)

    return mapper.array_to_storage(arr)


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

@router.get("/pools", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/pools",
    dependencies=[Depends(require_session)],
)
async def list_pools(
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    array_id = _get_array_id(request)
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    pool_stats = await pools_svc.list_pools_with_stats(db, array_id)
    if not pool_stats:
        pool_stats = await pools_svc.list_pools_with_stats(db, array_id="")

    data = []
    for item in pool_stats:
        pool = item["pool"]
        stats = {"volume_count": item["volume_count"], "used_capacity_mb": item["used_capacity_mb"]}
        data.append(mapper.pool_to_hitachi(pool, stats))

    return {"data": data}


@router.get("/pools/{pool_id_int}", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/pools/{pool_id_int}",
    dependencies=[Depends(require_session)],
)
async def get_pool(
    pool_id_int: int,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    pool_uuid = mapper.pool_uuid_for_id(pool_id_int)
    if pool_uuid is None:
        raise NotFoundError("Pool", str(pool_id_int))
    pool = await pools_svc.get_pool(db, pool_uuid)
    return mapper.pool_to_hitachi(pool)


# ---------------------------------------------------------------------------
# LDEVs (volumes)
# ---------------------------------------------------------------------------

@router.post("/ldevs", dependencies=[Depends(require_session)])
@router.post(
    "/storages/{storage_device_id}/ldevs",
    dependencies=[Depends(require_session)],
)
async def create_ldev(
    body: CreateLdevRequest,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    array_id = _get_array_id(request)
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    spdk = _get_spdk(request)
    jobs = _get_jobs(request)

    pool_uuid = mapper.pool_uuid_for_id(body.pool_id)
    if pool_uuid is None:
        raise NotFoundError("Pool", str(body.pool_id))

    size_mb = body.size_bytes // (1024 * 1024)
    if size_mb <= 0:
        raise ValidationError("LDEV size must be at least 1 MB")

    ldev_id = body.ldev_number if body.ldev_number is not None else mapper.next_ldev_id()
    label = body.label or f"ldev-{ldev_id}"

    try:
        vol = await volumes_svc.create_volume(
            db, spdk,
            name=label,
            pool_id=pool_uuid,
            size_mb=size_mb,
            vendor_metadata={"ldev_id": ldev_id},
        )
        mapper.register_ldev(ldev_id, vol.id)
        await db.commit()
    except CoreError:
        raise

    job = jobs.submit_completed(
        affected_resources=[f"/ConfigurationManager/v1/objects/ldevs/{ldev_id}"],
    )
    return _accepted(request, job.job_id, affected_resources=job.affected_resources)


@router.get("/ldevs", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/ldevs",
    dependencies=[Depends(require_session)],
)
async def list_ldevs(
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
    ldevNumber: int | None = None,
    poolId: int | None = None,
    count: int | None = None,
):
    array_id = _get_array_id(request)
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    volumes = await volumes_svc.list_volumes(db, array_id=array_id)
    if not volumes:
        volumes = await volumes_svc.list_volumes(db)

    data = []
    for vol in volumes:
        ldev = mapper.ldev_for_volume(vol.id)
        if ldev is None:
            continue
        # Filter by ldevNumber
        if ldevNumber is not None and ldev != ldevNumber:
            continue
        # Filter by poolId
        if poolId is not None and mapper.pool_id_for_uuid(vol.pool_id) != poolId:
            continue
        pool = await pools_svc.get_pool(db, vol.pool_id)
        data.append(mapper.volume_to_ldev(vol, pool))

    if count is not None:
        data = data[:count]

    return {"data": data}


@router.get("/ldevs/{ldev_id_int}", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/ldevs/{ldev_id_int}",
    dependencies=[Depends(require_session)],
)
async def get_ldev(
    ldev_id_int: int,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    vol_uuid = mapper.volume_for_ldev(ldev_id_int)
    if vol_uuid is None:
        raise NotFoundError("LDEV", str(ldev_id_int))
    vol = await volumes_svc.get_volume(db, vol_uuid)
    pool = await pools_svc.get_pool(db, vol.pool_id)
    return mapper.volume_to_ldev(vol, pool)


@router.delete("/ldevs/{ldev_id_int}", dependencies=[Depends(require_session)])
@router.delete(
    "/storages/{storage_device_id}/ldevs/{ldev_id_int}",
    dependencies=[Depends(require_session)],
)
async def delete_ldev(
    ldev_id_int: int,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    spdk = _get_spdk(request)
    jobs = _get_jobs(request)

    vol_uuid = mapper.volume_for_ldev(ldev_id_int)
    if vol_uuid is None:
        raise NotFoundError("LDEV", str(ldev_id_int))

    try:
        await volumes_svc.delete_volume(db, spdk, vol_uuid)
        mapper.unregister_ldev(vol_uuid)
        await db.commit()
    except CoreError:
        raise

    job = jobs.submit_completed(
        affected_resources=[f"/ConfigurationManager/v1/objects/ldevs/{ldev_id_int}"],
    )
    return _accepted(request, job.job_id)


@router.put("/ldevs/{ldev_id_int}", dependencies=[Depends(require_session)])
@router.put(
    "/storages/{storage_device_id}/ldevs/{ldev_id_int}",
    dependencies=[Depends(require_session)],
)
async def modify_ldev(
    ldev_id_int: int,
    body: ModifyLdevRequest,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    jobs = _get_jobs(request)

    vol_uuid = mapper.volume_for_ldev(ldev_id_int)
    if vol_uuid is None:
        raise NotFoundError("LDEV", str(ldev_id_int))

    if body.label:
        vol = await volumes_svc.get_volume(db, vol_uuid)
        vol.name = body.label
        await db.flush()
        await db.commit()

    job = jobs.submit_completed(
        affected_resources=[f"/ConfigurationManager/v1/objects/ldevs/{ldev_id_int}"],
    )
    return _accepted(request, job.job_id)


@router.put(
    "/ldevs/{ldev_id_int}/actions/expand/invoke",
    dependencies=[Depends(require_session)],
)
@router.put(
    "/storages/{storage_device_id}/ldevs/{ldev_id_int}/actions/expand/invoke",
    dependencies=[Depends(require_session)],
)
async def expand_ldev(
    ldev_id_int: int,
    body: ExpandLdevRequest,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    spdk = _get_spdk(request)
    jobs = _get_jobs(request)

    vol_uuid = mapper.volume_for_ldev(ldev_id_int)
    if vol_uuid is None:
        raise NotFoundError("LDEV", str(ldev_id_int))

    delta_mb = body.parameters.additional_bytes // (1024 * 1024)
    if delta_mb <= 0:
        raise ValidationError("Expand size must be at least 1 MB")

    try:
        await volumes_svc.expand_volume_by_delta(db, spdk, vol_uuid, delta_mb=delta_mb)
        await db.commit()
    except CoreError:
        raise

    job = jobs.submit_completed(
        affected_resources=[f"/ConfigurationManager/v1/objects/ldevs/{ldev_id_int}"],
    )
    return _accepted(request, job.job_id)


# ---------------------------------------------------------------------------
# Host groups (FC)
# ---------------------------------------------------------------------------

@router.post("/host-groups", dependencies=[Depends(require_session)])
@router.post(
    "/storages/{storage_device_id}/host-groups",
    dependencies=[Depends(require_session)],
)
async def create_host_group(
    body: CreateHostGroupRequest,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)

    jobs = _get_jobs(request)

    # Verify port exists
    ep_uuid = mapper.endpoint_for_port(body.port_id)
    if ep_uuid is None:
        raise NotFoundError("Port", body.port_id)

    # Create or find host
    try:
        host = await hosts_svc.get_host_by_name(db, body.host_group_name)
    except NotFoundError:
        host = await hosts_svc.create_host(
            db, name=body.host_group_name,
        )

    # Assign host group number on this port
    meta = host.vendor_meta_dict
    hg_map = meta.setdefault("hitachi_host_groups", {})
    iscsi_name_map = meta.setdefault("hitachi_iscsi_names", {})
    if body.port_id not in hg_map:
        # Next available HG number for this port
        used_nums = {v for v in hg_map.values()}
        hg_num = 0
        while hg_num in used_nums:
            hg_num += 1
        hg_map[body.port_id] = hg_num
        host.vendor_metadata = json.dumps(meta)
        await db.flush()

    if body.iscsi_name:
        iscsi_name_map[body.port_id] = body.iscsi_name
        host.vendor_metadata = json.dumps(meta)
        await db.flush()

    hg_num = hg_map[body.port_id]
    await db.commit()

    hg_id = f"{body.port_id},{hg_num}"
    return JSONResponse(
        status_code=200,
        content={
            "affectedResources": [
                f"/ConfigurationManager/v1/objects/storages/{mapper.storage_device_id}/host-groups/{hg_id}"
            ]
        },
    )


@router.get("/host-groups", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/host-groups",
    dependencies=[Depends(require_session)],
)
async def list_host_groups(
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
    portId: str | None = None,
):
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)

    hosts = await hosts_svc.list_hosts(db)

    data = []
    for host in hosts:
        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        for pid, hg_num in hg_map.items():
            if portId is not None and pid != portId:
                continue
            iscsi_name = await _resolve_host_group_iscsi_name(db, mapper, host, pid)
            data.append(
                mapper.host_to_host_group(
                    host,
                    pid,
                    hg_num,
                    iscsi_name=iscsi_name,
                )
            )

    return {"data": data}


@router.get("/host-groups/{host_group_id}", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/host-groups/{host_group_id}",
    dependencies=[Depends(require_session)],
)
async def get_host_group(
    host_group_id: str,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)

    parts = host_group_id.split(",")
    if len(parts) != 2:
        raise ValidationError("Invalid host group ID format")
    port_id, hg_str = parts
    hg_number = int(hg_str)

    hosts = await hosts_svc.list_hosts(db)
    for host in hosts:
        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        if hg_map.get(port_id) == hg_number:
            iscsi_name = await _resolve_host_group_iscsi_name(db, mapper, host, port_id)
            return mapper.host_to_host_group(
                host,
                port_id,
                hg_number,
                iscsi_name=iscsi_name,
            )

    raise NotFoundError("HostGroup", host_group_id)


@router.put("/host-groups/{host_group_id}", dependencies=[Depends(require_session)])
@router.put(
    "/storages/{storage_device_id}/host-groups/{host_group_id}",
    dependencies=[Depends(require_session)],
)
async def modify_host_group(
    host_group_id: str,
    body: dict,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    if storage_device_id is not None:
        mapper = await _get_mapper(request)
        if mapper.storage_device_id != storage_device_id:
            raise NotFoundError("StorageDevice", storage_device_id)

    jobs = _get_jobs(request)
    parts = host_group_id.split(",")
    if len(parts) != 2:
        raise ValidationError("Invalid host group ID format")
    port_id, hg_str = parts
    hg_number = int(hg_str)

    hosts = await hosts_svc.list_hosts(db)
    for host in hosts:
        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        if hg_map.get(port_id) != hg_number:
            continue

        if "iscsiName" in body and body.get("iscsiName"):
            names = meta.setdefault("hitachi_iscsi_names", {})
            names[port_id] = str(body["iscsiName"])
            host.vendor_metadata = json.dumps(meta)
            await db.flush()

        await db.commit()
        return Response(status_code=200)

    raise NotFoundError("HostGroup", host_group_id)


@router.delete(
    "/host-groups/{host_group_id}",
    dependencies=[Depends(require_session)],
)
@router.delete(
    "/storages/{storage_device_id}/host-groups/{host_group_id}",
    dependencies=[Depends(require_session)],
)
async def delete_host_group(
    host_group_id: str,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    if storage_device_id is not None:
        mapper = await _get_mapper(request)
        if mapper.storage_device_id != storage_device_id:
            raise NotFoundError("StorageDevice", storage_device_id)

    jobs = _get_jobs(request)

    # Parse "portId,hgNumber"
    parts = host_group_id.split(",")
    if len(parts) != 2:
        raise ValidationError("Invalid host group ID format (expected portId,hgNumber)")
    port_id, hg_str = parts
    hg_number = int(hg_str)

    # Find host with this HG assignment
    hosts = await hosts_svc.list_hosts(db)
    for host in hosts:
        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        if hg_map.get(port_id) == hg_number:
            del hg_map[port_id]
            host.vendor_metadata = json.dumps(meta)
            await db.flush()
            break
    else:
        raise NotFoundError("HostGroup", host_group_id)

    await db.commit()
    return Response(status_code=200)


@router.get(
    "/host-groups/{host_group_id}/wwns",
    dependencies=[Depends(require_session)],
)
async def list_host_group_wwns(host_group_id: str, request: Request, db: DbSession):
    parts = host_group_id.split(",")
    if len(parts) != 2:
        raise ValidationError("Invalid host group ID format")
    port_id, hg_str = parts
    hg_number = int(hg_str)

    hosts = await hosts_svc.list_hosts(db)
    for host in hosts:
        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        if hg_map.get(port_id) == hg_number:
            wwns = host.fc_wwpns
            data = [
                {"hostWwn": wwn, "portId": port_id, "hostGroupNumber": hg_number}
                for wwn in wwns
            ]
            return {"data": data}

    raise NotFoundError("HostGroup", host_group_id)


@router.post(
    "/host-groups/{host_group_id}/wwns",
    dependencies=[Depends(require_session)],
)
async def add_host_group_wwn(
    host_group_id: str,
    body: AddWwnRequest,
    request: Request,
    db: DbSession,
):
    jobs = _get_jobs(request)
    parts = host_group_id.split(",")
    if len(parts) != 2:
        raise ValidationError("Invalid host group ID format")
    port_id, hg_str = parts
    hg_number = int(hg_str)

    hosts = await hosts_svc.list_hosts(db)
    for host in hosts:
        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        if hg_map.get(port_id) == hg_number:
            await hosts_svc.add_host_port(
                db, host.id, port_type="fc", port_value=body.host_wwn,
            )
            await db.commit()
            job = jobs.submit_completed(affected_resources=[
                f"/ConfigurationManager/v1/objects/host-groups/{host_group_id}/wwns"
            ])
            return _accepted(request, job.job_id)

    raise NotFoundError("HostGroup", host_group_id)


# ---------------------------------------------------------------------------
# iSCSI targets
# ---------------------------------------------------------------------------

@router.post("/iscsi-targets", dependencies=[Depends(require_session)])
async def create_iscsi_target(
    body: CreateIscsiTargetRequest, request: Request, db: DbSession,
):
    array_id = _get_array_id(request)
    mapper = await _get_mapper(request)
    jobs = _get_jobs(request)

    ep_uuid = mapper.endpoint_for_port(body.port_id)
    if ep_uuid is None:
        raise NotFoundError("Port", body.port_id)

    try:
        host = await hosts_svc.get_host_by_name(db, body.iscsi_target_name)
    except NotFoundError:
        host = await hosts_svc.create_host(db, name=body.iscsi_target_name)

    meta = host.vendor_meta_dict
    it_map = meta.setdefault("hitachi_iscsi_targets", {})
    if body.port_id not in it_map:
        used_nums = set(it_map.values())
        it_num = 0
        while it_num in used_nums:
            it_num += 1
        it_map[body.port_id] = it_num
        host.vendor_metadata = json.dumps(meta)
        await db.flush()

    it_num = it_map[body.port_id]
    await db.commit()

    it_id = f"{body.port_id},{it_num}"
    job = jobs.submit_completed(
        affected_resources=[f"/ConfigurationManager/v1/objects/iscsi-targets/{it_id}"],
    )
    return _accepted(request, job.job_id)


@router.get("/iscsi-targets", dependencies=[Depends(require_session)])
async def list_iscsi_targets(
    request: Request,
    db: DbSession,
    portId: str | None = None,
):
    mapper = await _get_mapper(request)
    hosts = await hosts_svc.list_hosts(db)

    data = []
    for host in hosts:
        meta = host.vendor_meta_dict
        it_map = meta.get("hitachi_iscsi_targets", {})
        for pid, it_num in it_map.items():
            if portId is not None and pid != portId:
                continue
            data.append(mapper.host_to_iscsi_target(host, pid, it_num))

    return {"data": data}


@router.delete(
    "/iscsi-targets/{iscsi_target_id}",
    dependencies=[Depends(require_session)],
)
async def delete_iscsi_target(
    iscsi_target_id: str, request: Request, db: DbSession,
):
    jobs = _get_jobs(request)

    parts = iscsi_target_id.split(",")
    if len(parts) != 2:
        raise ValidationError("Invalid iSCSI target ID format (expected portId,targetNumber)")
    port_id, it_str = parts
    it_number = int(it_str)

    hosts = await hosts_svc.list_hosts(db)
    for host in hosts:
        meta = host.vendor_meta_dict
        it_map = meta.get("hitachi_iscsi_targets", {})
        if it_map.get(port_id) == it_number:
            del it_map[port_id]
            host.vendor_metadata = json.dumps(meta)
            await db.flush()
            break
    else:
        raise NotFoundError("IscsiTarget", iscsi_target_id)

    await db.commit()
    job = jobs.submit_completed(
        affected_resources=[
            f"/ConfigurationManager/v1/objects/iscsi-targets/{iscsi_target_id}"
        ],
    )
    return _accepted(request, job.job_id)


@router.get(
    "/iscsi-targets/{iscsi_target_id}/iscsi-names",
    dependencies=[Depends(require_session)],
)
async def list_iscsi_target_names(
    iscsi_target_id: str, request: Request, db: DbSession,
):
    parts = iscsi_target_id.split(",")
    if len(parts) != 2:
        raise ValidationError("Invalid iSCSI target ID format")
    port_id, it_str = parts
    it_number = int(it_str)

    hosts = await hosts_svc.list_hosts(db)
    for host in hosts:
        meta = host.vendor_meta_dict
        it_map = meta.get("hitachi_iscsi_targets", {})
        if it_map.get(port_id) == it_number:
            names = host.iscsi_iqns
            data = [
                {"iscsiName": name, "portId": port_id, "iscsiTargetNumber": it_number}
                for name in names
            ]
            return {"data": data}

    raise NotFoundError("IscsiTarget", iscsi_target_id)


@router.post(
    "/iscsi-targets/{iscsi_target_id}/iscsi-names",
    dependencies=[Depends(require_session)],
)
async def add_iscsi_target_name(
    iscsi_target_id: str,
    body: AddIscsiNameRequest,
    request: Request,
    db: DbSession,
):
    jobs = _get_jobs(request)
    parts = iscsi_target_id.split(",")
    if len(parts) != 2:
        raise ValidationError("Invalid iSCSI target ID format")
    port_id, it_str = parts
    it_number = int(it_str)

    hosts = await hosts_svc.list_hosts(db)
    for host in hosts:
        meta = host.vendor_meta_dict
        it_map = meta.get("hitachi_iscsi_targets", {})
        if it_map.get(port_id) == it_number:
            await hosts_svc.add_host_port(
                db, host.id, port_type="iscsi", port_value=body.iscsi_name,
            )
            await db.commit()
            job = jobs.submit_completed(affected_resources=[
                f"/ConfigurationManager/v1/objects/iscsi-targets/{iscsi_target_id}/iscsi-names"
            ])
            return _accepted(request, job.job_id)

    raise NotFoundError("IscsiTarget", iscsi_target_id)


@router.get("/host-iscsis", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/host-iscsis",
    dependencies=[Depends(require_session)],
)
async def list_host_iscsis(
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
    portId: str | None = None,
    hostGroupNumber: int | None = None,
    hostGroupName: str | None = None,
):
    if storage_device_id is not None:
        mapper = await _get_mapper(request)
        if mapper.storage_device_id != storage_device_id:
            raise NotFoundError("StorageDevice", storage_device_id)

    hosts = await hosts_svc.list_hosts(db)
    data = []

    for host in hosts:
        if hostGroupName is not None and host.name != hostGroupName:
            continue

        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        for pid, hg_num in hg_map.items():
            if portId is not None and pid != portId:
                continue
            if hostGroupNumber is not None and hg_num != hostGroupNumber:
                continue

            for iqn in host.iscsi_iqns:
                data.append(
                    {
                        "iscsiName": iqn,
                        "portId": pid,
                        "hostGroupNumber": hg_num,
                        "hostGroupName": host.name,
                    }
                )

    return {"data": data}


@router.post("/host-iscsis", dependencies=[Depends(require_session)])
@router.post(
    "/storages/{storage_device_id}/host-iscsis",
    dependencies=[Depends(require_session)],
)
async def add_host_iscsi(
    body: AddHostIscsiRequest,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    if storage_device_id is not None:
        mapper = await _get_mapper(request)
        if mapper.storage_device_id != storage_device_id:
            raise NotFoundError("StorageDevice", storage_device_id)

    jobs = _get_jobs(request)
    hosts = await hosts_svc.list_hosts(db)

    for host in hosts:
        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        if hg_map.get(body.port_id) != body.host_group_number:
            continue

        await hosts_svc.add_host_port(
            db,
            host.id,
            port_type="iscsi",
            port_value=body.iscsi_name,
        )
        await db.commit()
        mapper = await _get_mapper(request)
        return JSONResponse(
            status_code=200,
            content={
                "affectedResources": [
                    (
                        "/ConfigurationManager/v1/objects/storages/"
                        f"{mapper.storage_device_id}/host-iscsis/"
                        f"{body.port_id},{body.host_group_number}"
                    )
                ]
            },
        )

    raise NotFoundError(
        "HostGroup",
        f"{body.port_id},{body.host_group_number}",
    )


# ---------------------------------------------------------------------------
# LUNs (mappings)
# ---------------------------------------------------------------------------

@router.post("/luns", dependencies=[Depends(require_session)])
@router.post(
    "/storages/{storage_device_id}/luns",
    dependencies=[Depends(require_session)],
)
async def create_lun(
    body: CreateLunRequest,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    array_id = _get_array_id(request)
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    spdk = _get_spdk(request)
    jobs = _get_jobs(request)
    settings = request.app.state.settings

    # Resolve LDEV -> volume within the current array first.
    volume = None
    array_volumes = await volumes_svc.list_volumes(db, array_id=array_id)
    for candidate in array_volumes:
        ldev_meta = candidate.vendor_meta_dict.get("ldev_id")
        try:
            if ldev_meta is not None and int(ldev_meta) == body.ldev_id:
                volume = candidate
                break
        except (TypeError, ValueError):
            continue

    # Fallback for older/stale mapper-only flows.
    if volume is None:
        vol_uuid = mapper.volume_for_ldev(body.ldev_id)
        if vol_uuid is None:
            raise NotFoundError("LDEV", str(body.ldev_id))
        volume = await volumes_svc.get_volume(db, vol_uuid)
    vol_uuid = volume.id

    # Resolve port -> endpoint
    # Mapper state can briefly drift during topology churn/rebuild windows.
    # Validate the resolved endpoint belongs to the same array as the LDEV.
    ep_uuid = mapper.endpoint_for_port(body.port_id)
    endpoint: TransportEndpoint | None = None
    if ep_uuid is not None:
        result = await db.execute(
            select(TransportEndpoint).where(TransportEndpoint.id == ep_uuid)
        )
        endpoint = result.scalar_one_or_none()

    if endpoint is None or endpoint.array_id != volume.array_id:
        endpoint = None
        ep_uuid = None
        eps_result = await db.execute(
            select(TransportEndpoint).where(TransportEndpoint.array_id == volume.array_id)
        )
        array_eps = list(eps_result.scalars().all())
        for ep in array_eps:
            if ep.vendor_meta_dict.get("hitachi_port_id") == body.port_id:
                endpoint = ep
                ep_uuid = ep.id
                break

        # Fallback for minimal/default endpoints that do not carry a
        # hitachi_port_id mapping but are the sole iSCSI endpoint on array.
        if endpoint is None:
            iscsi_eps = [ep for ep in array_eps if ep.protocol == "iscsi"]
            if len(iscsi_eps) == 1:
                endpoint = iscsi_eps[0]
                ep_uuid = endpoint.id

    if ep_uuid is None or endpoint is None:
        raise NotFoundError("Port", body.port_id)

    # Resolve host group → host
    hosts = await hosts_svc.list_hosts(db)
    target_host = None
    for host in hosts:
        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        it_map = meta.get("hitachi_iscsi_targets", {})
        if hg_map.get(body.port_id) == body.host_group_number:
            target_host = host
            break
        if it_map.get(body.port_id) == body.host_group_number:
            target_host = host
            break

    if target_host is None:
        raise NotFoundError("HostGroup", f"{body.port_id},{body.host_group_number}")

    try:
        mapping = await mappings_svc.create_mapping(
            db, spdk, settings,
            host_id=target_host.id,
            volume_id=vol_uuid,
            persona_endpoint_id=ep_uuid,
        )
        await db.commit()
    except CoreError:
        raise

    lun_id_str = f"{body.port_id},{body.host_group_number},{mapping.lun_id}"
    job = jobs.submit_completed(
        affected_resources=[f"/ConfigurationManager/v1/objects/luns/{lun_id_str}"],
    )
    return _accepted(request, job.job_id, affected_resources=job.affected_resources)


@router.get("/luns", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/luns",
    dependencies=[Depends(require_session)],
)
async def list_luns(
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
    portId: str | None = None,
    hostGroupNumber: int | None = None,
):
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    hosts = await hosts_svc.list_hosts(db)

    data = []
    for host in hosts:
        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        it_map = meta.get("hitachi_iscsi_targets", {})

        # Collect (port_id, hg_number) pairs for this host
        pairs = []
        for pid, num in hg_map.items():
            pairs.append((pid, num))
        for pid, num in it_map.items():
            pairs.append((pid, num))

        for pid, num in pairs:
            if portId is not None and pid != portId:
                continue
            if hostGroupNumber is not None and num != hostGroupNumber:
                continue

            host_mappings = await mappings_svc.list_mappings_by_host(db, host.id)
            for m in host_mappings:
                ep_port = mapper.port_id_for_endpoint(m.persona_endpoint_id)
                if ep_port == pid:
                    data.append(mapper.mapping_to_lun(m, pid, num))

    return {"data": data}


@router.delete("/luns/{lun_id}", dependencies=[Depends(require_session)])
@router.delete(
    "/storages/{storage_device_id}/luns/{lun_id}",
    dependencies=[Depends(require_session)],
)
async def delete_lun(
    lun_id: str,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    mapper = await _get_mapper(request)
    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)
    spdk = _get_spdk(request)
    jobs = _get_jobs(request)

    # Parse "portId,hgNumber,lun"
    parts = lun_id.split(",")
    if len(parts) != 3:
        raise ValidationError("Invalid LUN ID format (expected portId,hgNumber,lun)")
    port_id, hg_str, lun_str = parts
    hg_number = int(hg_str)
    lun_number = int(lun_str)

    # Find the mapping
    hosts = await hosts_svc.list_hosts(db)
    for host in hosts:
        meta = host.vendor_meta_dict
        hg_map = meta.get("hitachi_host_groups", {})
        it_map = meta.get("hitachi_iscsi_targets", {})
        host_num = hg_map.get(port_id, it_map.get(port_id))
        if host_num != hg_number:
            continue

        host_mappings = await mappings_svc.list_mappings_by_host(db, host.id)
        for m in host_mappings:
            ep_port = mapper.port_id_for_endpoint(m.persona_endpoint_id)
            if ep_port == port_id and m.lun_id == lun_number:
                await mappings_svc.delete_mapping(db, spdk, m.id)
                await db.commit()
                job = jobs.submit_completed(
                    affected_resources=[
                        f"/ConfigurationManager/v1/objects/luns/{lun_id}"
                    ],
                )
                return _accepted(request, job.job_id)

    raise NotFoundError("LUN", lun_id)


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

@router.get("/ports", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/ports",
    dependencies=[Depends(require_session)],
)
async def list_ports(
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
    portType: str | None = None,
    portAttributes: str | None = None,
):
    from strix_gateway.core import endpoints as ep_svc

    array_id = _get_array_id(request)
    mapper = await _get_mapper(request)

    if storage_device_id is not None and mapper.storage_device_id != storage_device_id:
        raise NotFoundError("StorageDevice", storage_device_id)

    eps = await ep_svc.list_endpoints(db, array_id=array_id)
    data = [mapper.port_to_hitachi(ep) for ep in eps]

    if portType is not None:
        want = portType.upper()
        data = [p for p in data if str(p.get("portType", "")).upper() == want]

    if portAttributes is not None:
        attrs = {a.strip().upper() for a in portAttributes.split(",") if a.strip()}
        if attrs:
            data = [
                p
                for p in data
                if attrs.issubset({str(v).upper() for v in p.get("portAttributes", [])})
            ]

    return {"data": data}


@router.get("/ports/{port_id}", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/ports/{port_id}",
    dependencies=[Depends(require_session)],
)
async def get_port(
    port_id: str,
    request: Request,
    db: DbSession,
    storage_device_id: str | None = None,
):
    if storage_device_id is not None:
        mapper = await _get_mapper(request)
        if mapper.storage_device_id != storage_device_id:
            raise NotFoundError("StorageDevice", storage_device_id)

    ports = await list_ports(request, db, storage_device_id)
    for port in ports["data"]:
        if str(port.get("portId")) == port_id:
            return port
    raise NotFoundError("Port", port_id)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@router.get("/jobs/{job_id_int}", dependencies=[Depends(require_session)])
@router.get(
    "/storages/{storage_device_id}/jobs/{job_id_int}",
    dependencies=[Depends(require_session)],
)
async def get_job(
    job_id_int: int,
    request: Request,
    storage_device_id: str | None = None,
):
    if storage_device_id is not None:
        mapper = await _get_mapper(request)
        if mapper.storage_device_id != storage_device_id:
            raise NotFoundError("StorageDevice", storage_device_id)

    jobs = _get_jobs(request)
    status = jobs.get(job_id_int)
    if status is None:
        raise NotFoundError("Job", str(job_id_int))

    # Cinder Hitachi driver expects async-job success as:
    # status == "Completed" and state == "Succeeded".
    if status.state.value == "Completed":
        state_value = "Succeeded"
    elif status.state.value == "Failed":
        state_value = "Failed"
    else:
        state_value = "InProgress"

    return {
        "jobId": status.job_id,
        "status": status.state.value,
        "state": state_value,
        "affectedResources": status.affected_resources,
        "errorResource": status.error_message,
    }
