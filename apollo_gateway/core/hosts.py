# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Core service — Host lifecycle.

Centralises host CRUD with both PATCH-replace semantics (REST API) and
append-single-port semantics (SVC ``addhostport``).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.core.db import Host, Mapping
from apollo_gateway.core.exceptions import (
    AlreadyExistsError,
    NotFoundError,
    ResourceInUseError,
)

logger = logging.getLogger("apollo_gateway.core.hosts")


async def create_host(
    session: AsyncSession,
    *,
    name: str,
    iscsi_iqns: list[str] | None = None,
    nvme_nqns: list[str] | None = None,
    fc_wwpns: list[str] | None = None,
) -> Host:
    """Create a new host.  Raises :class:`AlreadyExistsError` if name taken."""
    existing = await session.execute(select(Host).where(Host.name == name))
    if existing.scalar_one_or_none():
        raise AlreadyExistsError("Host", name)

    host = Host(
        name=name,
        initiators_iscsi_iqns=json.dumps(iscsi_iqns or []),
        initiators_nvme_host_nqns=json.dumps(nvme_nqns or []),
        initiators_fc_wwpns=json.dumps(fc_wwpns or []),
    )
    session.add(host)
    await session.flush()
    return host


async def get_host(session: AsyncSession, host_id: str) -> Host:
    """Get a host by ID.  Raises :class:`NotFoundError`."""
    result = await session.execute(select(Host).where(Host.id == host_id))
    host = result.scalar_one_or_none()
    if host is None:
        raise NotFoundError("Host", host_id)
    return host


async def get_host_by_name(session: AsyncSession, name: str) -> Host:
    """Get a host by name.  Raises :class:`NotFoundError`."""
    result = await session.execute(select(Host).where(Host.name == name))
    host = result.scalar_one_or_none()
    if host is None:
        raise NotFoundError("Host", name)
    return host


async def list_hosts(session: AsyncSession) -> list[Host]:
    """Return all hosts."""
    result = await session.execute(select(Host))
    return list(result.scalars().all())


async def update_host_initiators(
    session: AsyncSession,
    host_id: str,
    *,
    iscsi_iqns: Optional[list[str]] = None,
    nvme_nqns: Optional[list[str]] = None,
    fc_wwpns: Optional[list[str]] = None,
) -> Host:
    """Replace initiator lists on a host (PATCH semantics).

    Only non-None lists are replaced; ``None`` means "leave unchanged".
    """
    host = await get_host(session, host_id)

    if iscsi_iqns is not None:
        host.initiators_iscsi_iqns = json.dumps(iscsi_iqns)
    if nvme_nqns is not None:
        host.initiators_nvme_host_nqns = json.dumps(nvme_nqns)
    if fc_wwpns is not None:
        host.initiators_fc_wwpns = json.dumps(fc_wwpns)

    await session.flush()
    return host


async def add_host_port(
    session: AsyncSession,
    host_id: str,
    *,
    port_type: str,
    port_value: str,
) -> Host:
    """Append a single initiator port to a host (SVC ``addhostport`` semantics).

    ``port_type`` must be one of ``"iscsi"``, ``"fc"``.
    The port is added idempotently — if it already exists, no change is made.
    """
    host = await get_host(session, host_id)

    if port_type == "iscsi":
        current = host.iscsi_iqns
        if port_value not in current:
            current.append(port_value)
            host.initiators_iscsi_iqns = json.dumps(current)
    elif port_type == "fc":
        current = host.fc_wwpns
        if port_value not in current:
            current.append(port_value)
            host.initiators_fc_wwpns = json.dumps(current)
    else:
        current = host.nvme_nqns
        if port_value not in current:
            current.append(port_value)
            host.initiators_nvme_host_nqns = json.dumps(current)

    await session.flush()
    return host


async def delete_host(session: AsyncSession, host_id: str) -> None:
    """Delete a host.  Raises :class:`ResourceInUseError` if it has mappings."""
    host = await get_host(session, host_id)

    maps_result = await session.execute(
        select(Mapping).where(Mapping.host_id == host_id)
    )
    if maps_result.scalars().first():
        raise ResourceInUseError("Host", host_id, "has active mappings; remove them first")

    await session.delete(host)
    await session.flush()
