# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""SVC personality — extends base with IBM-SVC capability defaults.

The SVC personality carries:
- SVC_PROFILE capability defaults (FC, compression, easy_tier, copy services)
- Pre/post hooks that translate CoreError → SvcError for CLI callers
- Expand-by-delta semantics for ``expandvdisksize``
- Append-only host-port semantics for ``addhostport``
"""

from __future__ import annotations

from strix_gateway.personalities.base import EnterpriseArrayPersonality
from strix_gateway.personalities.capabilities import SVC_PROFILE


class SvcPersonality(EnterpriseArrayPersonality):
    """IBM Spectrum Virtualize / Storwize personality.

    Overrides the base capability profile to advertise FC, compression,
    easy-tier, and copy-services by default. CLI dispatch lives under
    ``strix_gateway.personalities.svc`` and delegates to core services.
    """

    capability_profile = SVC_PROFILE
