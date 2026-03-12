# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""HPE 3PAR WSAPI REST API routes.

Endpoints mirror the python-3parclient expectations.  All routes live
under ``/api/v1``.

The WSAPI uses ``X-HP3PAR-WSAPI-SessionKey`` for auth.  Protected
routes declare the ``require_wsapi_session`` dependency.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.core.db import (
    Pool,
    TransportEndpoint,
    Volume,
    get_session,
)
from strix_gateway.core.exceptions import (
    AlreadyExistsError,
    CoreError,
    NotFoundError,
    ValidationError,
)
from strix_gateway.core import (
    endpoints as endpoints_svc,
    hosts as hosts_svc,
    mappings as mappings_svc,
    pools as pools_svc,
    volumes as volumes_svc,
)
from strix_gateway.core.models import VolumeStatus
from strix_gateway.personalities.hpe3par.models import (
    CreateCredentialRequest,
    CreateHostRequest,
    CreateVlunRequest,
    CreateVolumeRequest,
    GrowVolumeRequest,
    ModifyHostRequest,
)
from strix_gateway.personalities.hpe3par.sessions import WsapiSessionStore

logger = logging.getLogger("strix_gateway.personalities.hpe3par.routes")

router = APIRouter(prefix="/api/v1")

DbSession = Annotated[AsyncSession, Depends(get_session)]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def _get_array_id(request: Request) -> str:
    array_info = request.scope.get("state", {}).get("array")
    if array_info is None:
        raise ValidationError("No array context")
    return array_info.id


def _get_state(request: Request):
    if hasattr(request.app, "state"):
        return request.app.state
    scope_app = request.scope.get("app")
    if scope_app is not None and hasattr(scope_app, "state"):
        return scope_app.state
    raise ValidationError("Application state unavailable")


def _get_sessions(request: Request) -> WsapiSessionStore:
    state = _get_state(request)
    store = getattr(state, "hpe3par_sessions", None)
    if store is None:
        store = WsapiSessionStore()
        state.hpe3par_sessions = store
    return store


def _get_spdk(request: Request):
    state = _get_state(request)
    return state.spdk_client


def _get_settings(request: Request):
    state = _get_state(request)
    return state.settings


async def require_wsapi_session(
    request: Request,
    x_hp3par_wsapi_sessionkey: str | None = Header(None, alias="X-HP3PAR-WSAPI-SessionKey"),
) -> None:
    """Validate the WSAPI session key header."""
    if not x_hp3par_wsapi_sessionkey:
        raise ValidationError("Missing X-HP3PAR-WSAPI-SessionKey header")
    store = _get_sessions(request)
    info = store.validate(x_hp3par_wsapi_sessionkey)
    if info is None:
        raise ValidationError("Invalid or expired session key")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _volume_dict(vol, pool_name: str) -> dict:
    """Convert a Volume DB row to a WSAPI VV dict."""
    return {
        "id": abs(hash(vol.id)) % 100000,
        "name": vol.name,
        "sizeMiB": vol.size_mb,
        "provisioningType": 2,  # tpvv
        "copyType": 1,  # base
        "userCPG": pool_name,
        "state": 1 if vol.status in (VolumeStatus.available, VolumeStatus.in_use) else 6,
        "wwn": vol.wwn if hasattr(vol, "wwn") and vol.wwn else "",
    }


def _host_dict(host) -> dict:
    """Convert a Host DB row to a WSAPI host dict."""
    d: dict = {
        "id": abs(hash(host.id)) % 100000,
        "name": host.name,
        "persona": 1,
    }
    iqns = host.iscsi_iqns
    wwpns = host.fc_wwpns
    if iqns:
        d["iSCSINames"] = iqns
    if wwpns:
        d["FCWWNs"] = wwpns
    return d


def _vlun_dict(mapping, vol_name: str, host_name: str) -> dict:
    """Convert a Mapping DB row to a WSAPI VLUN dict."""
    return {
        "lun": mapping.lun_id,
        "volumeName": vol_name,
        "hostname": host_name,
        "type": 4,  # host-sees
        "active": True,
    }


def _members_envelope(members: list[dict]) -> dict:
    """Wrap a list of dicts in the WSAPI collection envelope."""
    return {"members": members, "total": len(members)}


# ---------------------------------------------------------------------------
# Session / Credential routes  (no auth required)
# ---------------------------------------------------------------------------

@router.post("/credentials")
async def create_credential(
    body: CreateCredentialRequest,
    request: Request,
):
    """Create a WSAPI session.  Credentials are not validated."""
    store = _get_sessions(request)
    info = store.create()
    return JSONResponse(
        status_code=201,
        content={"key": info.key},
    )


@router.delete("/credentials/{session_key}")
async def delete_credential(session_key: str, request: Request):
    """Destroy a WSAPI session."""
    store = _get_sessions(request)
    store.delete(session_key)
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------

@router.get(
    "/system",
    dependencies=[Depends(require_wsapi_session)],
)
async def get_system(
    request: Request,
    session: DbSession,
    array_id: str = Depends(_get_array_id),
):
    """GET /api/v1/system — system overview."""
    result = await session.execute(
        select(Pool).where(Pool.array_id == array_id)
    )
    pools = list(result.scalars().all())
    total_mb = sum(p.size_mb or 0 for p in pools)

    vol_result = await session.execute(
        select(Volume).where(Volume.array_id == array_id)
    )
    volumes = list(vol_result.scalars().all())
    alloc_mb = sum(v.size_mb for v in volumes)

    return {
        "name": request.scope.get("state", {}).get("array", type("", (), {"name": ""})()).name
        if request.scope.get("state", {}).get("array") else "3par",
        "systemVersion": "3.3.1.484",
        "totalCapacityMiB": total_mb,
        "allocatedCapacityMiB": alloc_mb,
        "freeCapacityMiB": total_mb - alloc_mb,
        "serialNumber": str(abs(hash(array_id)) % 10000000),
    }


# ---------------------------------------------------------------------------
# CPGs (pools)
# ---------------------------------------------------------------------------

@router.get(
    "/cpgs",
    dependencies=[Depends(require_wsapi_session)],
)
async def list_cpgs(
    session: DbSession,
    array_id: str = Depends(_get_array_id),
):
    """GET /api/v1/cpgs — list Common Provisioning Groups."""
    result = await session.execute(
        select(Pool).where(Pool.array_id == array_id)
    )
    pools = list(result.scalars().all())
    members = []
    for idx, p in enumerate(pools):
        vol_result = await session.execute(
            select(Volume).where(Volume.pool_id == p.id)
        )
        vols = list(vol_result.scalars().all())
        members.append({
            "id": idx,
            "name": p.name,
            "numTDVVs": len(vols),
            "numTPVVs": len(vols),
        })
    return _members_envelope(members)


@router.get(
    "/cpgs/{cpg_name}",
    dependencies=[Depends(require_wsapi_session)],
)
async def get_cpg(
    cpg_name: str,
    session: DbSession,
    array_id: str = Depends(_get_array_id),
):
    """GET /api/v1/cpgs/{name}"""
    result = await session.execute(
        select(Pool).where(Pool.name == cpg_name, Pool.array_id == array_id)
    )
    pool = result.scalar_one_or_none()
    if pool is None:
        raise NotFoundError("cpg", cpg_name)

    vol_result = await session.execute(
        select(Volume).where(Volume.pool_id == pool.id)
    )
    vols = list(vol_result.scalars().all())
    return {
        "id": 0,
        "name": pool.name,
        "numTDVVs": len(vols),
        "numTPVVs": len(vols),
    }


# ---------------------------------------------------------------------------
# Volumes (VVs)
# ---------------------------------------------------------------------------

@router.get(
    "/volumes",
    dependencies=[Depends(require_wsapi_session)],
)
async def list_volumes(
    session: DbSession,
    array_id: str = Depends(_get_array_id),
):
    """GET /api/v1/volumes"""
    result = await session.execute(
        select(Volume).where(Volume.array_id == array_id)
    )
    volumes = list(result.scalars().all())
    members = []
    for v in volumes:
        pool = await pools_svc.get_pool(session, v.pool_id)
        members.append(_volume_dict(v, pool.name))
    return _members_envelope(members)


@router.get(
    "/volumes/{name}",
    dependencies=[Depends(require_wsapi_session)],
)
async def get_volume(
    name: str,
    session: DbSession,
    array_id: str = Depends(_get_array_id),
):
    """GET /api/v1/volumes/{name}"""
    volume = await volumes_svc.get_volume_by_name(session, name, array_id)
    pool = await pools_svc.get_pool(session, volume.pool_id)
    return _volume_dict(volume, pool.name)


@router.post(
    "/volumes",
    dependencies=[Depends(require_wsapi_session)],
)
async def create_volume(
    body: CreateVolumeRequest,
    session: DbSession,
    request: Request,
    array_id: str = Depends(_get_array_id),
    spdk=Depends(_get_spdk),
):
    """POST /api/v1/volumes"""
    if body.size_mib <= 0:
        raise ValidationError("sizeMiB must be positive")

    pool_result = await session.execute(
        select(Pool).where(Pool.name == body.cpg, Pool.array_id == array_id)
    )
    pool = pool_result.scalar_one_or_none()
    if pool is None:
        raise NotFoundError("cpg", body.cpg)

    await volumes_svc.create_volume(
        session, spdk,
        name=body.name,
        pool_id=pool.id,
        size_mb=body.size_mib,
    )
    await session.commit()

    return Response(status_code=201)


@router.put(
    "/volumes/{name}",
    dependencies=[Depends(require_wsapi_session)],
)
async def modify_volume(
    name: str,
    body: GrowVolumeRequest,
    session: DbSession,
    array_id: str = Depends(_get_array_id),
    spdk=Depends(_get_spdk),
):
    """PUT /api/v1/volumes/{name} — grow a volume."""
    volume = await volumes_svc.get_volume_by_name(session, name, array_id)

    if body.action == "growvv":
        await volumes_svc.expand_volume_by_delta(session, spdk, volume.id, body.size_mib)
        await session.commit()
        return Response(status_code=200)

    raise ValidationError(f"Unknown action: {body.action}")


@router.delete(
    "/volumes/{name}",
    dependencies=[Depends(require_wsapi_session)],
)
async def delete_volume(
    name: str,
    session: DbSession,
    array_id: str = Depends(_get_array_id),
    spdk=Depends(_get_spdk),
):
    """DELETE /api/v1/volumes/{name}"""
    volume = await volumes_svc.get_volume_by_name(session, name, array_id)
    await volumes_svc.delete_volume(session, spdk, volume.id)
    await session.commit()
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

@router.get(
    "/hosts",
    dependencies=[Depends(require_wsapi_session)],
)
async def list_hosts(session: DbSession):
    """GET /api/v1/hosts"""
    hosts = await hosts_svc.list_hosts(session)
    return _members_envelope([_host_dict(h) for h in hosts])


@router.get(
    "/hosts/{name}",
    dependencies=[Depends(require_wsapi_session)],
)
async def get_host(name: str, session: DbSession):
    """GET /api/v1/hosts/{name}"""
    host = await hosts_svc.get_host_by_name(session, name)
    return _host_dict(host)


@router.post(
    "/hosts",
    dependencies=[Depends(require_wsapi_session)],
)
async def create_host(body: CreateHostRequest, session: DbSession):
    """POST /api/v1/hosts"""
    host = await hosts_svc.create_host(session, name=body.name)

    for iqn in body.i_scsi_names:
        await hosts_svc.add_host_port(session, host.id, port_type="iscsi", port_value=iqn)
    for wwpn in body.fc_wwns:
        await hosts_svc.add_host_port(
            session, host.id, port_type="fc",
            port_value=wwpn.upper().replace(":", ""),
        )

    await session.commit()
    return Response(status_code=201)


@router.put(
    "/hosts/{name}",
    dependencies=[Depends(require_wsapi_session)],
)
async def modify_host(name: str, body: ModifyHostRequest, session: DbSession):
    """PUT /api/v1/hosts/{name} — add initiator paths."""
    host = await hosts_svc.get_host_by_name(session, name)

    # pathOperation 1 = add
    if body.path_operation == 1:
        for iqn in body.i_scsi_names:
            await hosts_svc.add_host_port(session, host.id, port_type="iscsi", port_value=iqn)
        for wwpn in body.fc_wwns:
            await hosts_svc.add_host_port(
                session, host.id, port_type="fc",
                port_value=wwpn.upper().replace(":", ""),
            )
    else:
        raise ValidationError(f"Unsupported pathOperation: {body.path_operation}")

    await session.commit()
    return Response(status_code=200)


@router.delete(
    "/hosts/{name}",
    dependencies=[Depends(require_wsapi_session)],
)
async def delete_host(name: str, session: DbSession):
    """DELETE /api/v1/hosts/{name}"""
    host = await hosts_svc.get_host_by_name(session, name)
    await hosts_svc.delete_host(session, host.id)
    await session.commit()
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# VLUNs (mappings)
# ---------------------------------------------------------------------------

@router.get(
    "/vluns",
    dependencies=[Depends(require_wsapi_session)],
)
async def list_vluns(
    session: DbSession,
    array_id: str = Depends(_get_array_id),
):
    """GET /api/v1/vluns"""
    mappings = await mappings_svc.list_mappings(session, array_id=array_id)
    members = []
    for m in mappings:
        vol = await volumes_svc.get_volume(session, m.volume_id)
        host = await hosts_svc.get_host(session, m.host_id)
        members.append(_vlun_dict(m, vol.name, host.name))
    return _members_envelope(members)


@router.post(
    "/vluns",
    dependencies=[Depends(require_wsapi_session)],
)
async def create_vlun(
    body: CreateVlunRequest,
    session: DbSession,
    request: Request,
    array_id: str = Depends(_get_array_id),
    spdk=Depends(_get_spdk),
    settings=Depends(_get_settings),
):
    """POST /api/v1/vluns — map a volume to a host."""
    volume = await volumes_svc.get_volume_by_name(session, body.volume_name, array_id)
    host = await hosts_svc.get_host_by_name(session, body.hostname)

    existing = await mappings_svc.find_mapping_by_host_and_volume(
        session, host.id, volume.id,
    )
    if existing:
        raise AlreadyExistsError("vlun", f"{body.volume_name} → {body.hostname}")

    mapping = await mappings_svc.create_mapping(
        session, spdk, settings,
        host_id=host.id,
        volume_id=volume.id,
    )
    await session.commit()

    return JSONResponse(
        status_code=201,
        content=_vlun_dict(mapping, volume.name, host.name),
    )


@router.delete(
    "/vluns/{volume_name},{lun},{hostname}",
    dependencies=[Depends(require_wsapi_session)],
)
async def delete_vlun(
    volume_name: str,
    lun: int,
    hostname: str,
    session: DbSession,
    array_id: str = Depends(_get_array_id),
    spdk=Depends(_get_spdk),
):
    """DELETE /api/v1/vluns/{volumeName},{lun},{hostname}"""
    volume = await volumes_svc.get_volume_by_name(session, volume_name, array_id)
    host = await hosts_svc.get_host_by_name(session, hostname)

    mapping = await mappings_svc.find_mapping_by_host_and_volume(
        session, host.id, volume.id,
    )
    if mapping is None:
        raise NotFoundError("vlun", f"{volume_name} → {hostname}")

    await mappings_svc.delete_mapping(session, spdk, mapping.id)
    await session.commit()
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

@router.get(
    "/ports",
    dependencies=[Depends(require_wsapi_session)],
)
async def list_ports(
    session: DbSession,
    array_id: str = Depends(_get_array_id),
):
    """GET /api/v1/ports — list storage ports."""
    eps = await endpoints_svc.list_endpoints(session, array_id=array_id)
    members = []
    for idx, ep in enumerate(eps):
        node = idx // 2
        slot = 0 if ep.protocol == "fc" else 2
        port = idx % 2
        entry: dict = {
            "portPos": {"node": node, "slot": slot, "cardPort": port},
            "mode": 2,  # target
            "linkState": 4,  # ready
            "protocol": 1 if ep.protocol == "fc" else 2,
        }
        targets = ep.targets_dict
        if ep.protocol == "fc":
            entry["portWWN"] = targets.get("target_wwpns", [""])[0] if targets.get("target_wwpns") else ""
        else:
            entry["iSCSIName"] = targets.get("target_iqn", "")
            addrs = ep.addresses_dict
            portals = addrs.get("portals", [])
            if portals:
                entry["IPAddr"] = portals[0].split(":")[0]
        members.append(entry)
    return _members_envelope(members)
