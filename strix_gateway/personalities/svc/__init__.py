# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""IBM Spectrum Virtualize (SVC) personality.

SVC-specific capability profile and CLI command dispatch.
"""

from strix_gateway.personalities.svc.handlers import SvcContext, dispatch
from strix_gateway.personalities.svc.personality import SvcPersonality

__all__ = ["SvcPersonality", "SvcContext", "dispatch"]
