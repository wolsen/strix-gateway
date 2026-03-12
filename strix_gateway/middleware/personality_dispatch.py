# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""ASGI middleware that dispatches vendor requests to personality sub-apps.

Supports two dispatch modes:

- **Vhost mode**: Sits after :class:`VhostMiddleware`.  When the resolved
  array has a registered vendor sub-app, the request is dispatched there.
- **Non-vhost mode**: Falls back to path-prefix matching.  Each vendor
  factory declares a ``route_prefix``; requests matching that prefix are
  dispatched after resolving the array from the database.

Requests without an array context and no matching path prefix always fall
through to the inner app (admin, docs, ``/v1`` REST API, ``/healthz``).
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("strix_gateway.middleware.personality_dispatch")

_ARRAY_CACHE_TTL = 5.0


class PersonalityDispatcher:
    """Route vendor requests to the correct personality sub-app.

    Dispatch modes:

    1. **Vhost** — ``scope["state"]["array"]`` set by :class:`VhostMiddleware`;
       vendor looked up from the array record.
    2. **Path-prefix** — no array in scope; the request path is matched against
       ``app.state.vendor_route_prefixes`` and the array is resolved from the DB.
    """

    def __init__(self, app: ASGIApp):
        self.app = app
        # Per-vendor array cache: vendor → (ArrayInfo | None, monotonic ts)
        self._array_cache: dict[str, tuple[object, float]] = {}

    # ------------------------------------------------------------------
    # Non-vhost helpers
    # ------------------------------------------------------------------

    async def _resolve_by_path(self, scope: Scope, path: str):
        """Match *path* against vendor route prefixes and resolve the array."""
        asgi_app = scope.get("app")
        if asgi_app is None:
            return None

        prefixes: dict[str, str] = getattr(asgi_app.state, "vendor_route_prefixes", {})
        personality_apps: dict[str, ASGIApp] = getattr(asgi_app.state, "personality_apps", {})

        for prefix, vendor in prefixes.items():
            if path.startswith(prefix) and vendor in personality_apps:
                info = await self._get_vendor_array(vendor)
                if info is not None:
                    scope.setdefault("state", {})["array"] = info
                return info
        return None

    async def _get_vendor_array(self, vendor: str):
        """Return the first array for *vendor* from the DB (cached with TTL)."""
        now = time.monotonic()
        cached = self._array_cache.get(vendor)
        if cached is not None:
            info, ts = cached
            if (now - ts) < _ARRAY_CACHE_TTL:
                return info

        from sqlalchemy import select

        from strix_gateway.core.db import Array, get_session_factory
        from strix_gateway.tls.vhost import ArrayInfo

        try:
            sf = get_session_factory()
            async with sf() as session:
                result = await session.execute(
                    select(Array).where(Array.vendor == vendor).limit(1)
                )
                arr = result.scalar_one_or_none()
                if arr is not None:
                    info = ArrayInfo(
                        id=arr.id, name=arr.name, fqdn="", vendor=arr.vendor,
                    )
                    self._array_cache[vendor] = (info, now)
                    return info
        except Exception:
            logger.debug("Array lookup failed for vendor=%s", vendor, exc_info=True)

        self._array_cache[vendor] = (None, now)
        return None

    # ------------------------------------------------------------------
    # ASGI entrypoint
    # ------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # /healthz always handled by inner app
        path: str = scope.get("path", "")
        if path == "/healthz":
            await self.app(scope, receive, send)
            return

        # Check if VhostMiddleware resolved an array
        state = scope.get("state", {})
        array_info = state.get("array")

        if array_info is None:
            # No vhost resolution — try path-prefix dispatch (non-vhost mode)
            array_info = await self._resolve_by_path(scope, path)

        if array_info is None:
            await self.app(scope, receive, send)
            return

        vendor = getattr(array_info, "vendor", "generic")

        # Look up vendor sub-apps from app.state (populated during lifespan)
        asgi_app = scope.get("app")
        personality_apps: dict[str, ASGIApp] = {}
        if asgi_app is not None:
            personality_apps = getattr(asgi_app.state, "personality_apps", {})

        vendor_app = personality_apps.get(vendor)

        if vendor_app is not None:
            logger.debug(
                "Dispatching to %s personality sub-app for array=%s path=%s",
                vendor, array_info.name, path,
            )
            await vendor_app(scope, receive, send)
        else:
            # No sub-app registered for this vendor — fall through to main
            # app (backwards-compatible; e.g. "generic" arrays)
            await self.app(scope, receive, send)
