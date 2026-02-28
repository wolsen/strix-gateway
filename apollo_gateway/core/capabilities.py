# FILE: apollo_gateway/core/capabilities.py
"""Centralised capability-check functions.

These raise :class:`fastapi.HTTPException` (422) so they can be called
directly from API endpoints and IBM SVC handlers that raise SvcError.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import HTTPException

from apollo_gateway.core.personas import CapabilityProfile

if TYPE_CHECKING:
    from apollo_gateway.core.db import Subsystem


def assert_feature_enabled(
    profile: CapabilityProfile,
    feature: str,
    resource_type: str,
) -> None:
    """Raise HTTP 422 if *feature* is disabled in *profile*.

    Parameters
    ----------
    profile:
        Merged (effective) :class:`~apollo_gateway.core.personas.CapabilityProfile`.
    feature:
        Attribute name on ``profile.features``, e.g. ``"snapshots"``.
    resource_type:
        Human-readable name used in the error message, e.g. ``"Snapshot"``.
    """
    if not getattr(profile.features, feature, True):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{resource_type} not supported: '{feature}' is disabled "
                f"in subsystem capability profile"
            ),
        )


def assert_protocol_allowed(subsystem: Subsystem, protocol: str) -> None:
    """Raise HTTP 422 if *protocol* is not in ``subsystem.protocols_enabled``.

    Parameters
    ----------
    subsystem:
        ORM :class:`~apollo_gateway.core.db.Subsystem` instance.
    protocol:
        Protocol string, e.g. ``"iscsi"`` or ``"nvmeof_tcp"``.
    """
    enabled: list[str] = json.loads(subsystem.protocols_enabled)
    if protocol not in enabled:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Protocol '{protocol}' is not enabled for subsystem "
                f"'{subsystem.name}'. Enabled: {enabled}"
            ),
        )
