# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""In-memory session store for Hitachi Configuration Manager auth.

The Hitachi Configuration Manager REST API uses session-token
authentication.  The Cinder driver creates a session, receives a token,
then includes it as ``Authorization: Session <token>`` on subsequent
requests.  Sessions expire after a configurable TTL (default 300 s).

On gateway restart all sessions are lost — the Cinder driver will
re-authenticate transparently.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field

logger = logging.getLogger("strix_gateway.personalities.hitachi.sessions")

_DEFAULT_TTL = 300  # seconds


@dataclass
class SessionInfo:
    session_id: int
    token: str
    created_at: float
    ttl: float


class SessionStore:
    """Thread-safe (single-writer async) session store."""

    def __init__(self, ttl: float = _DEFAULT_TTL) -> None:
        self._sessions: dict[int, SessionInfo] = {}
        self._tokens: dict[str, int] = {}  # token → session_id
        self._next_id: int = 1
        self._ttl = ttl

    def create(self) -> SessionInfo:
        """Create a new session and return its info."""
        self._evict_expired()
        sid = self._next_id
        self._next_id += 1
        token = secrets.token_hex(16)
        info = SessionInfo(
            session_id=sid,
            token=token,
            created_at=time.monotonic(),
            ttl=self._ttl,
        )
        self._sessions[sid] = info
        self._tokens[token] = sid
        logger.debug("Session %d created", sid)
        return info

    def validate(self, token: str) -> SessionInfo | None:
        """Return session info if the token is valid, else ``None``."""
        sid = self._tokens.get(token)
        if sid is None:
            return None
        info = self._sessions.get(sid)
        if info is None:
            self._tokens.pop(token, None)
            return None
        if time.monotonic() - info.created_at > info.ttl:
            self._remove(sid)
            return None
        return info

    def delete(self, session_id: int) -> bool:
        """Delete a session.  Returns ``True`` if it existed."""
        return self._remove(session_id)

    def _remove(self, session_id: int) -> bool:
        info = self._sessions.pop(session_id, None)
        if info is None:
            return False
        self._tokens.pop(info.token, None)
        return True

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, info in self._sessions.items()
            if now - info.created_at > info.ttl
        ]
        for sid in expired:
            self._remove(sid)
