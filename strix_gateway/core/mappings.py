# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Core service — Mapping lifecycle and host-attachments query.

This is the **single** implementation of mapping creation/deletion.  It
consolidates logic that was previously duplicated in:
- ``core.services.create_mapping``
- ``personalities.svc.handlers._mkvdiskhostmap``
- ``topology.apply`` mapping section

All callers (REST routes, SVC handlers, topology apply) delegate here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strix_gateway.core.db import (
    Array,
    Host,
    Mapping,
    TransportEndpoint,
    Volume,
)
from strix_gateway.core.endpoints import resolve_endpoint
from strix_gateway.core.exceptions import (
    BackendError,
    InvalidStateError,
    NotFoundError,
)
from strix_gateway.core.models import (
    AttachmentPersona,
    AttachmentUnderlay,
    AttachmentsResponse,
    AttachmentView,
    DesiredState,
    Protocol,
    VolumeStatus,
)
from strix_gateway.spdk.ensure import allocate_lun, allocate_lun_from_base, allocate_nsid

if TYPE_CHECKING:
    from strix_gateway.config import Settings
    from strix_gateway.spdk.rpc import SPDKClient

logger = logging.getLogger("strix_gateway.core.mappings")


# ---------------------------------------------------------------------------
# Mapping creation
# ---------------------------------------------------------------------------

async def _resolve_fc_aware_endpoints(
    session: AsyncSession,
    *,
    array_id: str,
    host: Host,
    persona_endpoint_id: Optional[str] = None,
    underlay_endpoint_id: Optional[str] = None,
    persona_protocol: Optional[str] = None,
    underlay_protocol: Optional[str] = None,
) -> tuple[TransportEndpoint, TransportEndpoint]:
    """Resolve persona + underlay endpoints with FC-aware fallback.

    If the host has FC WWPNs and the array has an FC endpoint, the persona
    defaults to FC and the underlay to iSCSI.  Otherwise both default to the
    specified protocols (or iSCSI).

    This consolidates the FC-aware logic from ``handlers._mkvdiskhostmap``
    and the generic resolution from ``services.create_mapping``.
    """
    # If explicit IDs are given, honour them
    if persona_endpoint_id and underlay_endpoint_id:
        persona_ep = await resolve_endpoint(
            session, array_id=array_id, protocol=persona_protocol or Protocol.fc,
            endpoint_id=persona_endpoint_id,
        )
        underlay_ep = await resolve_endpoint(
            session, array_id=array_id, protocol=underlay_protocol or Protocol.iscsi,
            endpoint_id=underlay_endpoint_id,
        )
        return persona_ep, underlay_ep

    # FC-aware auto-resolution
    host_has_fc = bool(host.fc_wwpns)

    if host_has_fc and not persona_protocol:
        # Try to find an FC persona endpoint
        try:
            fc_ep = await resolve_endpoint(
                session, array_id=array_id, protocol=Protocol.fc,
            )
            # FC persona found — underlay defaults to iSCSI
            underlay_ep = await resolve_endpoint(
                session, array_id=array_id,
                protocol=underlay_protocol or Protocol.iscsi,
                endpoint_id=underlay_endpoint_id,
            )
            return fc_ep, underlay_ep
        except NotFoundError:
            # No FC endpoint on array — fall through to non-FC path
            pass

    # Non-FC path: use specified protocols or defaults
    p_proto = persona_protocol or Protocol.iscsi
    u_proto = underlay_protocol or Protocol.iscsi

    persona_ep = await resolve_endpoint(
        session, array_id=array_id, protocol=p_proto,
        endpoint_id=persona_endpoint_id,
    )
    underlay_ep = await resolve_endpoint(
        session, array_id=array_id, protocol=u_proto,
        endpoint_id=underlay_endpoint_id,
    )
    return persona_ep, underlay_ep


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
    """Create a mapping, allocate LUN, wire SPDK underlay.

    Accepts either explicit endpoint IDs *or* protocol selectors (the server
    picks the first matching endpoint on the volume's array).
    Supports FC-aware auto-resolution when neither is specified.
    """
    # Resolve volume + array
    vol_result = await session.execute(select(Volume).where(Volume.id == volume_id))
    volume = vol_result.scalar_one_or_none()
    if volume is None:
        raise NotFoundError("Volume", volume_id)
    if volume.status not in (VolumeStatus.available, VolumeStatus.in_use):
        raise InvalidStateError("Volume", volume_id, volume.status,
                                "expected available or in_use")

    array_id = volume.array_id

    # Resolve host
    host_result = await session.execute(select(Host).where(Host.id == host_id))
    host = host_result.scalar_one_or_none()
    if host is None:
        raise NotFoundError("Host", host_id)

    # Resolve endpoints (FC-aware)
    persona_ep, underlay_ep = await _resolve_fc_aware_endpoints(
        session,
        array_id=array_id,
        host=host,
        persona_endpoint_id=persona_endpoint_id,
        underlay_endpoint_id=underlay_endpoint_id,
        persona_protocol=persona_protocol,
        underlay_protocol=underlay_protocol,
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

    # Underlay ID: LUN for iSCSI, NSID for NVMeoF
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
        # iSCSI: allocate per underlay endpoint
        existing_underlay = await session.execute(
            select(Mapping).where(
                Mapping.underlay_endpoint_id == underlay_ep.id,
            )
        )
        used_underlay_luns = [m.underlay_id for m in existing_underlay.scalars().all()]
        underlay_id = allocate_lun_from_base(
            used_underlay_luns, settings.iscsi_underlay_lun_base,
        )

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
    await _wire_spdk_underlay(spdk, settings, session, mapping, volume, underlay_ep)

    # Mark volume in_use
    volume.status = VolumeStatus.in_use

    return mapping


async def _wire_spdk_underlay(
    spdk: "SPDKClient",
    settings: "Settings",
    session: AsyncSession,
    mapping: Mapping,
    volume: Volume,
    underlay_ep: TransportEndpoint,
) -> None:
    """Wire up the SPDK underlay for a mapping."""
    from strix_gateway.spdk.ensure import (
        ensure_iscsi_export,
        ensure_iscsi_mapping,
        ensure_nvmef_export,
        ensure_nvmef_mapping,
    )

    try:
        if underlay_ep.protocol == Protocol.iscsi:
            await asyncio.to_thread(ensure_iscsi_export, spdk, underlay_ep, settings)
            await asyncio.to_thread(ensure_iscsi_mapping, spdk, mapping, volume, underlay_ep)
        elif underlay_ep.protocol == Protocol.nvmeof_tcp:
            arr_result = await session.execute(
                select(Array).where(Array.id == volume.array_id)
            )
            arr = arr_result.scalar_one()
            profile = arr.profile_dict
            model_str = profile.get("model", "Strix Gateway")
            serial_str = f"STRIX-{arr.name[:8].upper()}"
            await asyncio.to_thread(
                ensure_nvmef_export, spdk, underlay_ep, settings,
                model_str, serial_str,
            )
            await asyncio.to_thread(
                ensure_nvmef_mapping, spdk, mapping, volume, underlay_ep,
            )
        # FC endpoints have no SPDK-side underlay wiring
    except Exception as exc:
        logger.error("SPDK underlay wiring failed for mapping %s: %s", mapping.id, exc)
        raise BackendError(str(exc), cause=exc)


# ---------------------------------------------------------------------------
# Mapping deletion
# ---------------------------------------------------------------------------

async def delete_mapping(
    session: AsyncSession,
    spdk: "SPDKClient",
    mapping_id: str,
) -> None:
    """Set desired_state=detached, clean up SPDK, hard-delete the mapping."""
    from strix_gateway.spdk import iscsi as iscsi_rpc
    from strix_gateway.spdk import nvmf as nvmf_rpc

    map_result = await session.execute(select(Mapping).where(Mapping.id == mapping_id))
    mapping = map_result.scalar_one_or_none()
    if mapping is None:
        raise NotFoundError("Mapping", mapping_id)

    mapping.desired_state = DesiredState.detached
    mapping.revision += 1
    await session.flush()

    underlay_ep = mapping.underlay_endpoint
    targets = underlay_ep.targets_dict

    try:
        if underlay_ep.protocol == Protocol.iscsi:
            target_iqn = targets.get("target_iqn", "")
            if target_iqn:
                await asyncio.to_thread(iscsi_rpc.delete_target_node, spdk, target_iqn)
        elif underlay_ep.protocol == Protocol.nvmeof_tcp:
            target_nqn = targets.get("subsystem_nqn", "")
            if target_nqn and mapping.underlay_id is not None:
                await asyncio.to_thread(
                    nvmf_rpc.remove_namespace, spdk, target_nqn, mapping.underlay_id,
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
# Mapping queries
# ---------------------------------------------------------------------------

async def get_mapping(session: AsyncSession, mapping_id: str) -> Mapping:
    """Get a mapping by ID.  Raises :class:`NotFoundError`."""
    result = await session.execute(select(Mapping).where(Mapping.id == mapping_id))
    mapping = result.scalar_one_or_none()
    if mapping is None:
        raise NotFoundError("Mapping", mapping_id)
    return mapping


async def list_mappings(
    session: AsyncSession,
    array_id: Optional[str] = None,
) -> list[Mapping]:
    """List mappings, optionally filtered by array."""
    stmt = select(Mapping)
    if array_id:
        stmt = stmt.join(Volume, Mapping.volume_id == Volume.id).where(
            Volume.array_id == array_id,
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_mappings_by_host(
    session: AsyncSession,
    host_id: str,
    array_id: Optional[str] = None,
) -> list[Mapping]:
    """List all mappings for a host, optionally scoped to an array."""
    stmt = select(Mapping).where(Mapping.host_id == host_id)
    if array_id:
        stmt = stmt.join(Volume, Mapping.volume_id == Volume.id).where(
            Volume.array_id == array_id,
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_mappings_by_volume(
    session: AsyncSession,
    volume_id: str,
) -> list[Mapping]:
    """List all mappings for a volume."""
    result = await session.execute(
        select(Mapping).where(Mapping.volume_id == volume_id)
    )
    return list(result.scalars().all())


async def find_mapping_by_host_and_volume(
    session: AsyncSession,
    host_id: str,
    volume_id: str,
) -> Mapping | None:
    """Find a mapping by host + volume pair. Returns None if not found."""
    result = await session.execute(
        select(Mapping).where(
            Mapping.host_id == host_id,
            Mapping.volume_id == volume_id,
        )
    )
    return result.scalar_one_or_none()


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
        raise NotFoundError("Host", host_id)

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

        persona_targets = persona_ep.targets_dict
        underlay_targets = underlay_ep.targets_dict
        underlay_addresses = underlay_ep.addresses_dict
        underlay_auth = underlay_ep.auth_dict

        # Build persona view
        persona = AttachmentPersona(
            protocol=persona_ep.protocol,
            target_wwpns=persona_targets.get("target_wwpns", []),
            lun_id=m.lun_id,
        )

        # Build underlay view
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
