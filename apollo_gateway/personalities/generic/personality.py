# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Generic personality — default implementation for the v1 REST frontend.

The generic personality supports all protocols and delegates every
operation to the shared core services with no vendor-specific overrides.
It serves as the reference implementation for the personality architecture.
"""

from __future__ import annotations

from apollo_gateway.personalities.base import EnterpriseArrayPersonality
from apollo_gateway.personalities.capabilities import GENERIC_PROFILE


class GenericPersonality(EnterpriseArrayPersonality):
    """Default personality: all protocols enabled, no vendor quirks."""

    capability_profile = GENERIC_PROFILE
