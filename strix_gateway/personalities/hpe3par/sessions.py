# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""In-memory session store for HPE 3PAR WSAPI auth.

The WSAPI uses ``X-HP3PAR-WSAPI-SessionKey`` header for
authentication.  ``POST /api/v1/credentials`` creates a session and
returns the key in the response body.  ``DELETE /api/v1/credentials/{key}``
destroys it.

On restart all sessions are lost — the Cinder driver
re-authenticates transparently.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger("strix_gateway.personalities.hpe3par.sessions")

_DEFAULT_TTL = 1200  # 20 min — matches real 3PAR default


@dataclass
class WsapiSessionInfo:
    key: str
    created_at: float
    ttl: float


class WsapiSessionStore:
    """Manages WSAPI session keys."""

    def __init__(self, ttl: float = _DEFAULT_TTL) -> None:
        self._sessions: dict[str, WsapiSessionInfo] = {}
        self._ttl = ttl

    def create(self) -> WsapiSessionInfo:
        """Issue a new session key."""
        self._evict_expired()
        key = secrets.token_hex(12)
        info = WsapiSessionInfo(
            key=key,
            created_at=time.monotonic(),
            ttl=self._ttl,
        )
        self._sessions[key] = info
        logger.debug("WSAPI session created: %s…", key[:8])
        return info

    def validate(self, key: str) -> WsapiSessionInfo | None:
        """Return session info if valid, else ``None``."""
        info = self._sessions.get(key)
        if info is None:
            return None
        if time.monotonic() - info.created_at > info.ttl:
            self.delete(key)
            return None
        return info

    def delete(self, key: str) -> bool:
        """Delete a session key.  Returns ``True`` if it existed."""
        return self._sessions.pop(key, None) is not None

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            k for k, info in self._sessions.items()
            if now - info.created_at > info.ttl
        ]
        for k in expired:
            self._sessions.pop(k, None)
