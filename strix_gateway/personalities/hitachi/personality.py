# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Hitachi VSP personality class.

Subclasses :class:`EnterpriseArrayPersonality` with Hitachi-specific
validation and post-creation hooks (LDEV number assignment).
"""

from __future__ import annotations

import logging

from strix_gateway.personalities.base import EnterpriseArrayPersonality
from strix_gateway.personalities.hitachi.capabilities import HITACHI_PROFILE

logger = logging.getLogger("strix_gateway.personalities.hitachi")


class HitachiPersonality(EnterpriseArrayPersonality):
    """Hitachi VSP array personality."""

    capability_profile = HITACHI_PROFILE  # type: ignore[assignment]

    def _pre_create_volume(self, **kwargs) -> None:
        """Validate Hitachi-specific volume constraints."""
        # LDEV numbers range 0–65279; validated at translation layer.
        # Size must be positive and divisible by the minimum allocation unit.
        size_mb = kwargs.get("size_mb", 0)
        if size_mb <= 0:
            from strix_gateway.core.exceptions import ValidationError
            raise ValidationError("LDEV size must be positive")
