# FILE: apollo_gateway/middleware/vhost.py
"""ASGI middleware that resolves the array from the HTTP Host header."""

from __future__ import annotations

import logging
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("apollo_gateway.middleware.vhost")

# Paths that bypass vhost matching (always pass through).
_BYPASS_PREFIXES = ("/healthz", "/admin", "/docs", "/openapi.json", "/redoc", "/v1/tls/")


class VhostMiddleware:
    """Route requests to arrays based on the HTTP ``Host`` header.

    When ``require_match`` is True and the Host header does not match any
    known array FQDN, a 404 JSON response is returned.  When False,
    the request passes through with ``scope["state"]["array"]`` unset.
    """

    def __init__(self, app: ASGIApp, *, require_match: bool = True):
        self.app = app
        self.require_match = require_match

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Bypass paths that should never require vhost matching
        path: str = scope.get("path", "")
        if any(path.startswith(p) for p in _BYPASS_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Extract Host header
        host_raw = ""
        for name, value in scope.get("headers", []):
            if name == b"host":
                host_raw = value.decode("latin-1")
                break
        host = host_raw.split(":")[0].lower()  # strip port

        # Look up in registry
        app_state: Any = scope.get("app")
        registry = getattr(app_state.state, "vhost_registry", None) if app_state else None

        if registry is None:
            # vhost not enabled — pass through
            await self.app(scope, receive, send)
            return

        info = registry.lookup(host)
        if info is not None:
            scope.setdefault("state", {})["array"] = info
            scope["state"]["vhost_matched"] = True
            logger.debug("Vhost matched: host=%s array=%s", host, info.name)
        elif self.require_match:
            response = JSONResponse(
                status_code=404,
                content={"detail": f"Unknown host: {host}"},
            )
            await response(scope, receive, send)
            return
        else:
            scope.setdefault("state", {})["array"] = None
            scope["state"]["vhost_matched"] = False
            logger.debug("Vhost no match for host=%s, passing through", host)

        await self.app(scope, receive, send)
