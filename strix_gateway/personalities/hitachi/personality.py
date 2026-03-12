# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Hitachi VSP personality — capability defaults.

Volume validation (LDEV constraints) is handled in ``routes.py``.
"""

from __future__ import annotations

from strix_gateway.personalities.hitachi.capabilities import HITACHI_PROFILE


class HitachiPersonality:
    """Hitachi VSP array personality."""

    capability_profile = HITACHI_PROFILE
