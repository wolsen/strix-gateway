# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Generic personality — default capability profile for the v1 REST frontend."""

from __future__ import annotations

from strix_gateway.personalities.capabilities import GENERIC_PROFILE


class GenericPersonality:
    """Default personality: all protocols enabled, no vendor quirks."""

    capability_profile = GENERIC_PROFILE
