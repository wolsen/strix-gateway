# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Capability profile system for storage personalities.

Each vendor personality declares a :class:`CapabilityProfile` instance
describing which features and protocols it supports.  This replaces
ad-hoc feature-flag checks scattered across the codebase.
"""

from __future__ import annotations

from pydantic import BaseModel


class CapabilityProfile(BaseModel):
    """Describes the feature support matrix for a storage personality."""

    # Protocol support
    supports_iscsi: bool = True
    supports_fc: bool = False
    supports_nvmeof_tcp: bool = False

    # Data services
    supports_snapshots: bool = True
    supports_clones: bool = True
    supports_qos: bool = False
    supports_replication: bool = False

    # Host features
    supports_host_groups: bool = False
    supports_multiattach: bool = False

    # Provisioning
    supports_thin_provisioning: bool = True
    supports_compression: bool = False
    supports_easy_tier: bool = False

    # Limits
    max_volumes: int | None = None
    max_snapshots_per_volume: int | None = None
    max_hosts: int | None = None

    # Vendor metadata
    model: str = "generic"
    version: str = "1.0.0"


# ---------------------------------------------------------------------------
# Pre-defined profiles
# ---------------------------------------------------------------------------

GENERIC_PROFILE = CapabilityProfile(
    supports_iscsi=True,
    supports_fc=True,
    supports_nvmeof_tcp=True,
    supports_snapshots=True,
    supports_clones=True,
    model="Strix-Generic",
    version="1.0.0",
)

SVC_PROFILE = CapabilityProfile(
    supports_iscsi=True,
    supports_fc=True,
    supports_nvmeof_tcp=False,
    supports_snapshots=True,
    supports_clones=True,
    supports_qos=False,
    supports_replication=False,
    supports_host_groups=False,
    supports_multiattach=True,
    supports_thin_provisioning=True,
    supports_compression=True,
    supports_easy_tier=True,
    model="SVC-SAFER-FAKE-9000",
    version="8.6.0.0",
)

PURE_PROFILE = CapabilityProfile(
    supports_iscsi=True,
    supports_fc=True,
    supports_nvmeof_tcp=True,
    supports_snapshots=True,
    supports_clones=True,
    supports_replication=True,
    supports_host_groups=True,
    supports_multiattach=True,
    supports_thin_provisioning=True,
    model="FlashArray-stub",
    version="6.4.0",
)

ONTAP_PROFILE = CapabilityProfile(
    supports_iscsi=True,
    supports_fc=True,
    supports_nvmeof_tcp=True,
    supports_snapshots=True,
    supports_clones=True,
    supports_replication=True,
    supports_host_groups=True,
    supports_multiattach=False,
    supports_thin_provisioning=True,
    model="ONTAP-stub",
    version="9.13.0",
)
