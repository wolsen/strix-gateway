# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Centralised capability-check functions.

These raise :class:`fastapi.HTTPException` (422) so they can be called
directly from API endpoints and IBM SVC handlers that raise SvcError.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import HTTPException

from strix_gateway.core.personas import CapabilityProfile

if TYPE_CHECKING:
    from strix_gateway.core.db import Array


def assert_feature_enabled(
    profile: CapabilityProfile,
    feature: str,
    resource_type: str,
) -> None:
    """Raise HTTP 422 if *feature* is disabled in *profile*.

    Parameters
    ----------
    profile:
        Merged (effective) :class:`~strix_gateway.core.personas.CapabilityProfile`.
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
                f"in array capability profile"
            ),
        )
