# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""HPE 3PAR personality — capability defaults.

CLI dispatch lives in ``handlers.py``, WSAPI routes in ``routes.py``.
"""

from __future__ import annotations

from strix_gateway.personalities.capabilities import HPE_3PAR_PROFILE


class Hpe3parPersonality:
    """HPE 3PAR StoreServ personality."""

    capability_profile = HPE_3PAR_PROFILE
