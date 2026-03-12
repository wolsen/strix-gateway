# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""SVC personality — IBM-SVC capability defaults.

CLI dispatch lives in ``handlers.py`` and delegates to core services.
"""

from __future__ import annotations

from strix_gateway.personalities.capabilities import SVC_PROFILE


class SvcPersonality:
    """IBM Spectrum Virtualize / Storwize personality."""

    capability_profile = SVC_PROFILE
