# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Enterprise array personality base class.

Provides a composition-first design where vendor personalities inherit
reusable storage operations and override only what differs.  The base
class delegates to canonical core services for all actual state changes.

Usage::

    class MyVendorPersonality(EnterpriseArrayPersonality):
        capability_profile = MyVendorCapabilityProfile()

        def _pre_create_volume(self, **kwargs):
            # vendor-specific validation
            ...
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from apollo_gateway.core import (
    arrays as arrays_svc,
    endpoints as endpoints_svc,
    hosts as hosts_svc,
    mappings as mappings_svc,
    pools as pools_svc,
    volumes as volumes_svc,
)
from apollo_gateway.core.db import Host, Mapping, Pool, Volume
from apollo_gateway.core.exceptions import CapabilityDisabledError
from apollo_gateway.personalities.capabilities import CapabilityProfile

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from apollo_gateway.config import Settings
    from apollo_gateway.spdk.rpc import SPDKClient

logger = logging.getLogger("apollo_gateway.personalities.base")


class EnterpriseArrayPersonality:
    """Base class for all storage array personalities.

    Holds references to core service modules and exposes reusable
    operations with hook points for vendor quirks.  Vendor subclasses
    override hooks and declare a :attr:`capability_profile`.

    Parameters
    ----------
    settings:
        Gateway configuration (SPDK addresses, IQN prefixes, etc.).
    """

    #: Override in subclass with vendor-specific profile.
    capability_profile: CapabilityProfile = CapabilityProfile()

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings

    # ------------------------------------------------------------------
    # Capability checks
    # ------------------------------------------------------------------

    def assert_capable(self, feature: str, label: str = "") -> None:
        """Raise if *feature* is disabled in the capability profile."""
        if not getattr(self.capability_profile, feature, True):
            raise CapabilityDisabledError(feature, label or feature)

    # ------------------------------------------------------------------
    # Volume lifecycle
    # ------------------------------------------------------------------

    async def create_volume(
        self,
        session: "AsyncSession",
        spdk: "SPDKClient",
        *,
        name: str,
        pool_id: str,
        size_mb: int,
        **kwargs: Any,
    ) -> Volume:
        """Create a volume.  Calls hooks before/after core service."""
        self._pre_create_volume(name=name, pool_id=pool_id, size_mb=size_mb, **kwargs)
        vol = await volumes_svc.create_volume(
            session, spdk, name=name, pool_id=pool_id, size_mb=size_mb,
        )
        self._post_create_volume(vol)
        return vol

    def _pre_create_volume(self, **kwargs: Any) -> None:
        """Hook: called before volume creation.  Override for validation."""

    def _post_create_volume(self, volume: Volume) -> None:
        """Hook: called after successful volume creation."""

    async def delete_volume(
        self,
        session: "AsyncSession",
        spdk: "SPDKClient",
        volume_id: str,
    ) -> None:
        """Delete a volume."""
        await volumes_svc.delete_volume(session, spdk, volume_id)

    async def extend_volume(
        self,
        session: "AsyncSession",
        spdk: "SPDKClient",
        volume_id: str,
        new_size_mb: int,
    ) -> Volume:
        """Extend a volume to an absolute new size."""
        return await volumes_svc.extend_volume(session, spdk, volume_id, new_size_mb)

    async def expand_volume_by_delta(
        self,
        session: "AsyncSession",
        spdk: "SPDKClient",
        volume_id: str,
        delta_mb: int,
    ) -> Volume:
        """Expand a volume by a delta (SVC semantics)."""
        return await volumes_svc.expand_volume_by_delta(session, spdk, volume_id, delta_mb)

    async def get_volume(self, session: "AsyncSession", volume_id: str) -> Volume:
        return await volumes_svc.get_volume(session, volume_id)

    async def get_volume_by_name(
        self, session: "AsyncSession", name: str, array_id: str,
    ) -> Volume:
        return await volumes_svc.get_volume_by_name(session, name, array_id)

    async def list_volumes(
        self, session: "AsyncSession", array_id: Optional[str] = None,
    ) -> list[Volume]:
        return await volumes_svc.list_volumes(session, array_id=array_id)

    # ------------------------------------------------------------------
    # Pool lifecycle
    # ------------------------------------------------------------------

    async def create_pool(
        self,
        session: "AsyncSession",
        spdk: "SPDKClient",
        **kwargs: Any,
    ) -> Pool:
        return await pools_svc.create_pool(session, spdk, **kwargs)

    async def get_pool(self, session: "AsyncSession", pool_id: str) -> Pool:
        return await pools_svc.get_pool(session, pool_id)

    async def list_pools(
        self, session: "AsyncSession", array_id: Optional[str] = None,
    ) -> list[Pool]:
        return await pools_svc.list_pools(session, array_id=array_id)

    async def list_pools_with_stats(
        self, session: "AsyncSession", array_id: str,
    ) -> list[dict]:
        return await pools_svc.list_pools_with_stats(session, array_id)

    async def delete_pool(self, session: "AsyncSession", pool_id: str) -> None:
        return await pools_svc.delete_pool(session, pool_id)

    # ------------------------------------------------------------------
    # Host lifecycle
    # ------------------------------------------------------------------

    async def create_host(
        self, session: "AsyncSession", **kwargs: Any,
    ) -> Host:
        return await hosts_svc.create_host(session, **kwargs)

    async def get_host(self, session: "AsyncSession", host_id: str) -> Host:
        return await hosts_svc.get_host(session, host_id)

    async def get_host_by_name(self, session: "AsyncSession", name: str) -> Host:
        return await hosts_svc.get_host_by_name(session, name)

    async def list_hosts(self, session: "AsyncSession") -> list[Host]:
        return await hosts_svc.list_hosts(session)

    async def update_host_initiators(
        self, session: "AsyncSession", host_id: str, **kwargs: Any,
    ) -> Host:
        return await hosts_svc.update_host_initiators(session, host_id, **kwargs)

    async def add_host_port(
        self, session: "AsyncSession", host_id: str, *, port_type: str, port_value: str,
    ) -> Host:
        return await hosts_svc.add_host_port(
            session, host_id, port_type=port_type, port_value=port_value,
        )

    async def delete_host(self, session: "AsyncSession", host_id: str) -> None:
        return await hosts_svc.delete_host(session, host_id)

    # ------------------------------------------------------------------
    # Mapping lifecycle
    # ------------------------------------------------------------------

    async def map_volume(
        self,
        session: "AsyncSession",
        spdk: "SPDKClient",
        *,
        host_id: str,
        volume_id: str,
        persona_endpoint_id: Optional[str] = None,
        underlay_endpoint_id: Optional[str] = None,
        persona_protocol: Optional[str] = None,
        underlay_protocol: Optional[str] = None,
    ) -> Mapping:
        """Map a volume to a host."""
        return await mappings_svc.create_mapping(
            session, spdk, self.settings,
            host_id=host_id,
            volume_id=volume_id,
            persona_endpoint_id=persona_endpoint_id,
            underlay_endpoint_id=underlay_endpoint_id,
            persona_protocol=persona_protocol,
            underlay_protocol=underlay_protocol,
        )

    async def unmap_volume(
        self,
        session: "AsyncSession",
        spdk: "SPDKClient",
        mapping_id: str,
    ) -> None:
        """Unmap a volume from a host."""
        return await mappings_svc.delete_mapping(session, spdk, mapping_id)

    async def list_mappings(
        self, session: "AsyncSession", array_id: Optional[str] = None,
    ) -> list[Mapping]:
        return await mappings_svc.list_mappings(session, array_id=array_id)

    async def list_host_mappings(
        self, session: "AsyncSession", host_id: str, array_id: Optional[str] = None,
    ) -> list[Mapping]:
        return await mappings_svc.list_mappings_by_host(session, host_id, array_id=array_id)

    async def list_volume_mappings(
        self, session: "AsyncSession", volume_id: str,
    ) -> list[Mapping]:
        return await mappings_svc.list_mappings_by_volume(session, volume_id)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    async def get_host_attachments(self, session: "AsyncSession", host_id: str):
        return await mappings_svc.get_host_attachments(session, host_id)

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def list_fc_target_ports(
        self, session: "AsyncSession", array_id: str,
    ) -> list[dict]:
        return await endpoints_svc.list_fc_target_ports(session, array_id)

    async def list_fc_fabric_paths(
        self, session: "AsyncSession", array_id: str, host_id: Optional[str] = None,
    ) -> list[dict]:
        return await endpoints_svc.list_fc_fabric_paths(session, array_id, host_id=host_id)

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def get_capabilities(self) -> CapabilityProfile:
        """Return this personality's capability profile."""
        return self.capability_profile
