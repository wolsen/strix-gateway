# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Core service layer — mapping lifecycle and host-attachments query.

This module centralises business rules that both the REST API and vendor
façades call.  SPDK operations flow through it to ``spdk.ensure`` — vendor
façades must never call SPDK directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo_gateway.core.db import (
    Array,
    Host,
    Mapping,
    TransportEndpoint,
    Volume,
)
from apollo_gateway.core.models import (
    AttachmentPersona,
    AttachmentUnderlay,
    AttachmentsResponse,
    AttachmentView,
    DesiredState,
    Protocol,
    VolumeStatus,
)
from apollo_gateway.spdk.ensure import allocate_lun, allocate_nsid

if TYPE_CHECKING:
    from apollo_gateway.config import Settings
    from apollo_gateway.spdk.rpc import SPDKClient

logger = logging.getLogger("apollo_gateway.core.services")


def _parse_json_dict(raw) -> dict:
    """Parse a JSON str (or passthrough dict/list) into a dict safely."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict):
        return raw
    return {}


# ---------------------------------------------------------------------------
# Mapping creation
# ---------------------------------------------------------------------------

async def resolve_endpoint(
    session: AsyncSession,
    *,
    array_id: str,
    protocol: str,
    endpoint_id: Optional[str] = None,
) -> TransportEndpoint:
    """Resolve a transport endpoint for *array_id* by explicit ID or first
    match for *protocol*.  Raises ``ValueError`` on mismatch / not-found."""
    if endpoint_id:
        result = await session.execute(
            select(TransportEndpoint).where(TransportEndpoint.id == endpoint_id)
        )
        ep = result.scalar_one_or_none()
        if ep is None:
            raise ValueError(f"Transport endpoint {endpoint_id} not found")
        if ep.array_id != array_id:
            raise ValueError(
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
        raise ValueError(
            f"No {protocol} endpoint found on array {array_id}"
        )
    return ep


async def create_mapping(
    session: AsyncSession,
    spdk: "SPDKClient",
    settings: "Settings",
    *,
    host_id: str,
    volume_id: str,
    persona_endpoint_id: Optional[str] = None,
    underlay_endpoint_id: Optional[str] = None,
    persona_protocol: Optional[str] = None,
    underlay_protocol: Optional[str] = None,
) -> Mapping:
    """Create and persist a Mapping, allocate LUN, wire SPDK underlay.

    Accepts either explicit endpoint IDs *or* protocol selectors (the server
    picks the first matching endpoint on the volume's array).
    """
    # Resolve volume + array
    vol_result = await session.execute(select(Volume).where(Volume.id == volume_id))
    volume = vol_result.scalar_one_or_none()
    if volume is None:
        raise LookupError(f"Volume {volume_id} not found")
    if volume.status not in (VolumeStatus.available, VolumeStatus.in_use):
        raise ValueError(f"Volume status is {volume.status}, expected available/in_use")

    array_id = volume.array_id

    # Resolve host
    host_result = await session.execute(select(Host).where(Host.id == host_id))
    host = host_result.scalar_one_or_none()
    if host is None:
        raise LookupError(f"Host {host_id} not found")

    # Resolve endpoints
    persona_ep = await resolve_endpoint(
        session,
        array_id=array_id,
        protocol=persona_protocol or Protocol.fc,
        endpoint_id=persona_endpoint_id,
    )
    underlay_ep = await resolve_endpoint(
        session,
        array_id=array_id,
        protocol=underlay_protocol or Protocol.iscsi,
        endpoint_id=underlay_endpoint_id,
    )

    # Allocate LUN: smallest free per host + persona endpoint
    existing_result = await session.execute(
        select(Mapping).where(
            Mapping.host_id == host_id,
            Mapping.persona_endpoint_id == persona_ep.id,
        )
    )
    used_luns = [m.lun_id for m in existing_result.scalars().all()]
    lun_id = allocate_lun(used_luns)

    # Underlay ID: LUN for iSCSI (starts at 0), NSID for NVMeoF (starts at 1)
    if underlay_ep.protocol == Protocol.nvmeof_tcp:
        existing_underlay = await session.execute(
            select(Mapping).where(
                Mapping.underlay_endpoint_id == underlay_ep.id,
            )
        )
        used_nsids = [m.underlay_id for m in existing_underlay.scalars().all()
                      if m.underlay_id is not None]
        underlay_id = allocate_nsid(used_nsids)
    else:
        underlay_id = lun_id

    mapping = Mapping(
        host_id=host_id,
        volume_id=volume_id,
        persona_endpoint_id=persona_ep.id,
        underlay_endpoint_id=underlay_ep.id,
        lun_id=lun_id,
        underlay_id=underlay_id,
        desired_state=DesiredState.attached,
        revision=1,
    )
    session.add(mapping)
    await session.flush()

    # Wire SPDK underlay
    from apollo_gateway.spdk.ensure import (
        ensure_iscsi_export,
        ensure_iscsi_mapping,
        ensure_nvmef_export,
        ensure_nvmef_mapping,
    )

    if underlay_ep.protocol == Protocol.iscsi:
        await asyncio.to_thread(ensure_iscsi_export, spdk, underlay_ep, settings)
        await asyncio.to_thread(ensure_iscsi_mapping, spdk, mapping, volume, underlay_ep)
    elif underlay_ep.protocol == Protocol.nvmeof_tcp:
        arr_result = await session.execute(select(Array).where(Array.id == array_id))
        arr = arr_result.scalar_one()
        profile = json.loads(arr.profile)
        model_str = profile.get("model", "Apollo Gateway")
        serial_str = f"APOLLO-{arr.name[:8].upper()}"
        await asyncio.to_thread(
            ensure_nvmef_export, spdk, underlay_ep, settings,
            model_str, serial_str,
        )
        await asyncio.to_thread(ensure_nvmef_mapping, spdk, mapping, volume, underlay_ep)

    # Mark volume in_use
    volume.status = VolumeStatus.in_use

    return mapping


# ---------------------------------------------------------------------------
# Mapping deletion
# ---------------------------------------------------------------------------

async def delete_mapping(
    session: AsyncSession,
    spdk: "SPDKClient",
    mapping_id: str,
) -> None:
    """Set desired_state=detached, clean up SPDK, hard-delete the mapping."""
    from apollo_gateway.spdk import iscsi as iscsi_rpc
    from apollo_gateway.spdk import nvmf as nvmf_rpc

    map_result = await session.execute(select(Mapping).where(Mapping.id == mapping_id))
    mapping = map_result.scalar_one_or_none()
    if mapping is None:
        raise ValueError(f"Mapping {mapping_id} not found")

    mapping.desired_state = DesiredState.detached
    mapping.revision += 1
    await session.flush()

    underlay_ep = mapping.underlay_endpoint
    targets = _parse_json_dict(underlay_ep.targets)

    try:
        if underlay_ep.protocol == Protocol.iscsi:
            target_iqn = targets.get("target_iqn", "")
            if target_iqn:
                await asyncio.to_thread(iscsi_rpc.delete_target_node, spdk, target_iqn)
        elif underlay_ep.protocol == Protocol.nvmeof_tcp:
            target_nqn = targets.get("subsystem_nqn", "")
            if target_nqn and mapping.underlay_id is not None:
                await asyncio.to_thread(
                    nvmf_rpc.remove_namespace, spdk, target_nqn, mapping.underlay_id
                )
    except Exception as exc:
        logger.error("SPDK cleanup failed for mapping %s: %s", mapping_id, exc)

    volume = mapping.volume
    await session.delete(mapping)
    await session.flush()

    # If no more mappings on this volume, reset status
    remaining = await session.execute(
        select(Mapping).where(Mapping.volume_id == volume.id)
    )
    if not remaining.scalars().first():
        volume.status = VolumeStatus.available


# ---------------------------------------------------------------------------
# Host attachments query (for compute-side agent)
# ---------------------------------------------------------------------------

async def get_host_attachments(
    session: AsyncSession,
    host_id: str,
) -> AttachmentsResponse:
    """Build the full attachments payload for a host."""
    host_result = await session.execute(select(Host).where(Host.id == host_id))
    host = host_result.scalar_one_or_none()
    if host is None:
        raise ValueError(f"Host {host_id} not found")

    maps_result = await session.execute(
        select(Mapping).where(
            Mapping.host_id == host_id,
            Mapping.desired_state == DesiredState.attached,
        )
    )
    mappings = maps_result.scalars().all()

    attachments: list[AttachmentView] = []
    for m in mappings:
        persona_ep = m.persona_endpoint
        underlay_ep = m.underlay_endpoint
        volume = m.volume

        persona_targets = _parse_json_dict(persona_ep.targets)
        underlay_targets = _parse_json_dict(underlay_ep.targets)
        underlay_addresses = _parse_json_dict(underlay_ep.addresses)
        underlay_auth = _parse_json_dict(underlay_ep.auth)

        # Build persona view
        persona = AttachmentPersona(
            protocol=persona_ep.protocol,
            target_wwpns=persona_targets.get("target_wwpns", []),
            lun_id=m.lun_id,
        )

        # Build underlay view — target_lun or nsid depending on protocol
        underlay_kwargs: dict = dict(
            protocol=underlay_ep.protocol,
            targets=underlay_targets,
            addresses=underlay_addresses,
            auth=underlay_auth,
        )
        if underlay_ep.protocol == Protocol.iscsi:
            underlay_kwargs["target_lun"] = m.underlay_id
        elif underlay_ep.protocol == Protocol.nvmeof_tcp:
            underlay_kwargs["nsid"] = m.underlay_id

        underlay = AttachmentUnderlay(**underlay_kwargs)

        attachments.append(AttachmentView(
            attachment_id=m.id,
            volume_id=volume.id,
            array_id=volume.array_id,
            revision=m.revision,
            desired_state=m.desired_state,
            persona=persona,
            underlay=underlay,
        ))

    return AttachmentsResponse(
        host_id=host_id,
        generated_at=datetime.now(timezone.utc),
        attachments=attachments,
    )
