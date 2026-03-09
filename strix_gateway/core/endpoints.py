# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Core service — Transport endpoint lifecycle and FC queries.

Centralises endpoint CRUD plus FC-specific queries (``lsportfc``,
``lsfabric``) that were previously buried in the SVC handler layer.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.core.db import Array, Host, Mapping, TransportEndpoint
from strix_gateway.core.exceptions import NotFoundError, ValidationError
from strix_gateway.core.models import Protocol

logger = logging.getLogger("strix_gateway.core.endpoints")


async def create_endpoint(
    session: AsyncSession,
    *,
    array_id: str,
    protocol: str,
    targets: dict,
    addresses: dict | None = None,
    auth: dict | None = None,
) -> TransportEndpoint:
    """Create a transport endpoint on an array."""
    arr_result = await session.execute(select(Array).where(Array.id == array_id))
    if arr_result.scalar_one_or_none() is None:
        raise NotFoundError("Array", array_id)

    ep = TransportEndpoint(
        array_id=array_id,
        protocol=protocol if isinstance(protocol, str) else protocol.value,
        targets=json.dumps(targets),
        addresses=json.dumps(addresses or {}),
        auth=json.dumps(auth or {"method": "none"}),
    )
    session.add(ep)
    await session.flush()
    return ep


async def get_endpoint(
    session: AsyncSession,
    endpoint_id: str,
    array_id: Optional[str] = None,
) -> TransportEndpoint:
    """Get an endpoint by ID, optionally scoped to an array."""
    stmt = select(TransportEndpoint).where(TransportEndpoint.id == endpoint_id)
    if array_id:
        stmt = stmt.where(TransportEndpoint.array_id == array_id)
    result = await session.execute(stmt)
    ep = result.scalar_one_or_none()
    if ep is None:
        raise NotFoundError("TransportEndpoint", endpoint_id)
    return ep


async def list_endpoints(
    session: AsyncSession,
    array_id: Optional[str] = None,
    protocol: Optional[str] = None,
) -> list[TransportEndpoint]:
    """List endpoints, optionally filtered by array and/or protocol."""
    stmt = select(TransportEndpoint)
    if array_id:
        stmt = stmt.where(TransportEndpoint.array_id == array_id)
    if protocol:
        stmt = stmt.where(TransportEndpoint.protocol == protocol)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def delete_endpoint(
    session: AsyncSession,
    endpoint_id: str,
    array_id: Optional[str] = None,
) -> None:
    """Delete an endpoint."""
    ep = await get_endpoint(session, endpoint_id, array_id=array_id)
    await session.delete(ep)
    await session.flush()


async def resolve_endpoint(
    session: AsyncSession,
    *,
    array_id: str,
    protocol: str,
    endpoint_id: Optional[str] = None,
) -> TransportEndpoint:
    """Resolve a transport endpoint by explicit ID or first match for protocol.

    Raises :class:`NotFoundError` on mismatch / not-found.
    """
    if endpoint_id:
        result = await session.execute(
            select(TransportEndpoint).where(TransportEndpoint.id == endpoint_id)
        )
        ep = result.scalar_one_or_none()
        if ep is None:
            raise NotFoundError("TransportEndpoint", endpoint_id)
        if ep.array_id != array_id:
            raise ValidationError(
                f"Endpoint {endpoint_id} belongs to a different array"
            )
        return ep
    # Auto-select: first endpoint matching protocol on the array
    result = await session.execute(
        select(TransportEndpoint).where(
            TransportEndpoint.array_id == array_id,
            TransportEndpoint.protocol == protocol,
        )
    )
    ep = result.scalars().first()
    if ep is None:
        raise NotFoundError("TransportEndpoint", f"{protocol} on array {array_id}")
    return ep


# ---------------------------------------------------------------------------
# FC-specific queries (extracted from IBM SVC handlers)
# ---------------------------------------------------------------------------

async def list_fc_target_ports(
    session: AsyncSession,
    array_id: str,
) -> list[dict]:
    """Return per-WWPN rows for FC endpoints on an array.

    Each row has ``endpoint_id``, ``wwpn``, ``protocol``.
    Used by ``lsportfc``-style queries.
    """
    eps = await list_endpoints(session, array_id=array_id, protocol=Protocol.fc)
    rows = []
    for ep in eps:
        targets = ep.targets_dict
        for wwpn in targets.get("target_wwpns", []):
            rows.append({
                "endpoint_id": ep.id,
                "wwpn": wwpn,
                "protocol": ep.protocol,
            })
    return rows


async def list_fc_fabric_paths(
    session: AsyncSession,
    array_id: str,
    host_id: Optional[str] = None,
) -> list[dict]:
    """Return FC fabric paths (cross-product of host WWPNs × target WWPNs).

    Used by ``lsfabric``-style queries.  If *host_id* is given, only that
    host's WWPNs are included; otherwise all hosts with FC WWPNs.
    """
    # Get target WWPNs
    target_ports = await list_fc_target_ports(session, array_id)
    target_wwpns = [p["wwpn"] for p in target_ports]

    if not target_wwpns:
        return []

    # Get host WWPNs
    if host_id:
        host_result = await session.execute(select(Host).where(Host.id == host_id))
        host = host_result.scalar_one_or_none()
        if host is None:
            raise NotFoundError("Host", host_id)
        hosts = [host]
    else:
        host_result = await session.execute(select(Host))
        hosts = list(host_result.scalars().all())

    paths = []
    for host in hosts:
        host_wwpns = host.fc_wwpns
        if not host_wwpns:
            continue
        for hw in host_wwpns:
            for tw in target_wwpns:
                paths.append({
                    "host_id": host.id,
                    "host_name": host.name,
                    "local_wwpn": hw,
                    "remote_wwpn": tw,
                })
    return paths
