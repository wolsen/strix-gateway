# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""HPE 3PAR personality.

Dual-interface personality exposing both an InForm OS SSH CLI and a
WSAPI REST interface for OpenStack Cinder driver testing.
"""

from strix_gateway.personalities.hpe3par.handlers import Hpe3parContext, dispatch

__all__ = ["Hpe3parContext", "dispatch"]
