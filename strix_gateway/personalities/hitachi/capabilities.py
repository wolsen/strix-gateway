# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Hitachi VSP capability profile."""

from __future__ import annotations

from strix_gateway.core.personas import CapabilityFeatures, CapabilityProfile

HITACHI_PROFILE = CapabilityProfile(
    model="VSP-stub",
    version="93-06-01-80/00",
    features=CapabilityFeatures(
        thin_provisioning=True,
        snapshots=True,
        clones=True,
        replication=False,
        consistency_groups=False,
        multiattach=False,
        compression=False,
        easy_tier=False,
    ),
)
